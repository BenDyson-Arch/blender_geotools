"""Sun hours, clear-sky insolation and shadow studies, with Blender's ray machinery.

The maths is bpy-free (``geotools.analysis.sun_hours`` and ``.clearsky``):
which rays to cast and what each is worth. Blender casts them:

* **The engine is a Cycles EMIT bake** (:mod:`.ray_bake`, shared with the
  horizon profile and the fast preset paths). A shader node group (the
  "probe") holds one Cycles ``Raycast`` node per sun direction, up to
  :data:`RAYS_PER_BAKE` at a time, as a chain of blocks of ``ray_bake.BLOCK``
  rays. For each sample it sums, per ray, the hours (when the ray is in front
  of the surface and nothing blocks it) and the energy x cos(incidence), and
  emits ``(hours, energy, 1)``. A bake evaluates it once per texel, with
  Cycles' own BVH, and numpy adds the batches up in binary64. Everything in
  the scene that renders is an occluder; the source objects are shown for
  the bake.
* **TEXTURE layers** bake on the source objects' own UVs, through a temporary
  material, as ``bake_texture`` does.
* **FACE and CORNER layers** bake a temporary "sample" object: one tiny
  triangle per face (at its centre) or per corner (just inside the face, at
  its corner), each owning one texel of its own UV map. The sample's exact
  position and normal are face attributes the probe reads, so its own shape
  does not matter. The triangles are a few float32 steps across, so they
  shadow nothing (see :func:`build_sample_object`).
  The values are written to the original meshes' ``gt_<name>`` attributes.

Shadow studies (``geotools.sun_shadow_study``) keyframe the Sun light, one
frame per moment, at the equinoxes and solstices; rendering stays Blender's.

See docs/design/m5-analysis.md.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import time
from dataclasses import dataclass, field

import bpy
import numpy as np

from ..analysis import site, sun
from ..analysis import sun_hours as core
from ..core.layers import (
    Aggregation,
    DataType,
    Domain,
    LayerSpec,
    Origin,
    fill_value,
    format_provenance,
    make_provenance,
)
from . import analysis
from .bake_texture import _assign_material, _Saved, check_object_uvs, uv_map_name
from .common import extension_version, redraw_ui
from .layers import (
    ensure_layer,
    ensure_layer_image,
    evaluated_depsgraph,
    find_layer,
    refresh_layer,
    remove_layer_data,
    save_layer_image,
    source_objects,
    spec_from_props,
    write_layer,
)
from .properties import FieldError
from .ray_bake import (
    PROBE_UV,
    RAY_LENGTH,
    Probe,
    RayBakeError,
    Run,
    float_image,
    get_attribute,
    new_socket,
    pixels,
    probe_material,
    setup_cycles,
    show,
)
from .ray_bake import bake as ray_bake
from .ray_bake import build_sample_object as _build_sample_object
from .ray_bake import collect_samples as _collect_samples

log = logging.getLogger(__name__)

TOOL = "sun_hours"
"""Provenance tool name recorded on the layers."""
TEMP_PREFIX = "GeoTools Sun Hours"

RAYS_PER_BAKE = 1024
"""Rays per bake at most. Bake time is linear in rays; this keeps the fixed cost
of a bake (scene sync, BVH, shader compile) small next to the rays."""

ALGORITHM = (
    "NREL SPA sun positions; Cycles Raycast nodes in an EMIT bake (one ray per sun "
    "direction per sample, offset along the normal); midpoint rule in time"
)
CLEAR_SKY = "Ineichen-Perez (2002), Kasten-Young air mass, solar constant 1366.1 W/m2"


class SunHoursError(RayBakeError):
    """The tool cannot run; the message says why."""


# --- the probe shader ------------------------------------------------------------------------


def _ray_group(name: str):
    """One ray: Sum + (hours, energy x cos(incidence), 0) when it is in front and unblocked.

    Inputs Sum, Origin, Normal, Direction and Weights (hours, energy, 0);
    output Sum. Eight shader nodes. The sums travel as one vector, so each
    ray's own values are used up within the ray: Cycles' shader stack (255
    floats) would overflow if two chains kept every ray's values alive.
    """
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for socket in ("Sum", "Origin", "Normal", "Direction", "Weights"):
        new_socket(tree, socket, "Vector")
    new_socket(tree, "Sum", "Vector", "OUTPUT")
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    get = group_in.outputs.get

    cosine = nodes.new("ShaderNodeVectorMath")
    cosine.operation = "DOT_PRODUCT"
    links.new(get("Normal"), cosine.inputs[0])
    links.new(get("Direction"), cosine.inputs[1])
    front = nodes.new("ShaderNodeMath")
    front.operation = "GREATER_THAN"
    front.inputs[1].default_value = 0.0
    links.new(cosine.outputs["Value"], front.inputs[0])
    length = nodes.new("ShaderNodeMath")  # no traversal for a ray behind the surface
    length.operation = "MULTIPLY"
    length.inputs[1].default_value = RAY_LENGTH
    links.new(front.outputs[0], length.inputs[0])
    cast = nodes.new("ShaderNodeRaycast")
    # Direction and Position fall back to the normal and the shading point when
    # unlinked, whatever their default values, so both are linked.
    links.new(get("Origin"), cast.inputs["Position"])
    links.new(get("Direction"), cast.inputs["Direction"])
    links.new(length.outputs[0], cast.inputs["Length"])
    lit = nodes.new("ShaderNodeMath")  # 1 when in front and not hit
    lit.operation = "LESS_THAN"
    links.new(cast.outputs["Is Hit"], lit.inputs[0])
    links.new(front.outputs[0], lit.inputs[1])
    lit_cosine = nodes.new("ShaderNodeMath")
    lit_cosine.operation = "MULTIPLY"
    links.new(lit.outputs[0], lit_cosine.inputs[0])
    links.new(cosine.outputs["Value"], lit_cosine.inputs[1])
    gains = nodes.new("ShaderNodeCombineXYZ")
    links.new(lit.outputs[0], gains.inputs[0])
    links.new(lit_cosine.outputs[0], gains.inputs[1])
    add = nodes.new("ShaderNodeVectorMath")  # Sum + gains x weights
    add.operation = "MULTIPLY_ADD"
    links.new(gains.outputs[0], add.inputs[0])
    links.new(get("Weights"), add.inputs[1])
    links.new(get("Sum"), add.inputs[2])
    links.new(add.outputs[0], group_out.inputs["Sum"])
    return tree


_BLANK = {"Direction": (0.0, 0.0, 0.0), "Weights": (0.0, 0.0, 0.0)}
"""A spare ray slot: a zero direction is never in front, so never cast."""


def _probe(run: Run, offset: float) -> Probe:
    """The sun probe: rays from the sample moved ``offset`` along its normal."""

    def head(tree, group_in) -> dict:
        normal = group_in.outputs["Normal"]
        lift = tree.nodes.new("ShaderNodeVectorMath")
        lift.operation = "SCALE"
        lift.inputs["Scale"].default_value = float(offset)
        tree.links.new(normal, lift.inputs[0])
        origin = tree.nodes.new("ShaderNodeVectorMath")
        origin.operation = "ADD"
        tree.links.new(group_in.outputs["Position"], origin.inputs[0])
        tree.links.new(lift.outputs[0], origin.inputs[1])
        return {"Origin": origin.outputs[0], "Normal": normal}

    ray = _ray_group(f"{TEMP_PREFIX} Ray")
    run.groups.append(ray)
    return Probe(run, TEMP_PREFIX, ray, ("Origin", "Normal"), head)


def _load(probe: Probe, rays: core.Rays, start: int, stop: int):
    """The probe for rays ``start:stop``, with their directions and weights set."""
    values = [
        {
            "Direction": tuple(map(float, rays.directions[k])),
            "Weights": (rays.hours[k], rays.energy[k], 0.0),
        }
        for k in range(start, stop)
    ]
    return probe.load(values, _BLANK)


def _probe_material(name: str, group, image, *, attributes: bool):
    return probe_material(name, group, image, attributes=attributes)


_float_image = float_image
_pixels = pixels
_Run = Run
_setup_cycles = setup_cycles


# --- samples for FACE and CORNER layers ------------------------------------------------------


def collect_samples(scene: bpy.types.Scene, domain: Domain, depsgraph):
    """World-space samples of every source object that can take the layer (see ray_bake)."""
    return _collect_samples(scene, source_objects(scene), domain, depsgraph)


def build_sample_object(saved: _Saved, context, points: np.ndarray, normals: np.ndarray):
    """The temporary sample object at the sample points (``ray_bake.build_sample_object``)."""
    return _build_sample_object(saved, context, points, normals, name=f"{TEMP_PREFIX} Samples")


# --- running -------------------------------------------------------------------------------------


@dataclass
class Plan:
    """What a run computes: the rays and where they came from."""

    rays: core.Rays
    steps: core.TimeSteps
    site: site.SunSite
    first_day: dt.date
    last_day: dt.date
    zone: str
    parameters: dict[str, object]


def _date(year: int, month: int, day: int, what: str) -> dt.date:
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise ValueError(
            f"Not a date for the {what}: {year}-{month:02d}-{day:02d} ({exc})."
        ) from None


def plan(context: bpy.types.Context) -> Plan:
    """The rays for the settings; raises the sun tool's errors and ValueError."""
    settings = analysis.settings(context)
    sun_props, props = settings.sun, settings.sun_hours
    tz, zone = analysis._time_zone(sun_props)
    where = analysis.compute_site(context, sun_props)
    first = _date(sun_props.year, sun_props.month, sun_props.day, "date")
    if props.period == "DAY":
        last = first
    elif props.period == "YEAR":
        first, last = dt.date(first.year, 1, 1), dt.date(first.year, 12, 31)
    else:
        last = _date(props.end_year, props.end_month, props.end_day, "end date")
        if last < first:
            raise ValueError("The end date is before the start date.")
    steps = core.period_steps(first, last, tz, props.step_minutes)
    horizon = float(props.horizon) if props.horizon > 0 else None
    from .archaeo import sun_hours_horizon  # archaeo builds on this module

    profile = sun_hours_horizon(context)
    if profile is not None:  # the far horizon from archaeoastronomy, raised to Horizon
        azimuths, elevations = profile.as_pair()
        horizon = (azimuths, np.maximum(elevations, float(props.horizon)))
    sun_steps = core.sun_steps(
        steps,
        where.latitude,
        where.longitude,
        where.height,
        where.convergence,
        refraction=sun_props.refraction,
        horizon=horizon,
    )
    rays = core.build_rays(
        sun_steps,
        tolerance_deg=props.tolerance,
        insolation=props.insolation,
        linke_turbidity=props.linke_turbidity,
        altitude_m=where.height,
        diffuse_rays=props.diffuse_rays if props.insolation and props.diffuse else 0,
        convergence=where.convergence,
        horizon=horizon,
    )
    parameters: dict[str, object] = {
        "algorithm": ALGORITHM,
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "time_zone": zone,
        "start_utc": steps.start_utc.isoformat(),
        "end_utc": steps.end_utc.isoformat(),
        "step_minutes": props.step_minutes,
        "refraction": bool(sun_props.refraction),
        "site": {
            "latitude": where.latitude,
            "longitude": where.longitude,
            "height": where.height,
            "geodetic_crs": where.geodetic_crs,
            "from": sun_props.site,
        },
        "convergence_deg": where.convergence,
        "north": where.north,
        "horizon_deg": float(props.horizon),
        "horizon_profile": (
            None
            if profile is None
            else {
                "from": profile.info.get("eye"),
                "latitude": profile.info.get("latitude"),
                "longitude": profile.info.get("longitude"),
                "azimuths": len(profile.azimuth),
                "highest_deg": float(profile.elevation.max()),
                "ignore_within": profile.info.get("ignore_within"),
            }
        ),
        **rays.stats,
    }
    if props.insolation:
        parameters["clear_sky"] = {
            "model": CLEAR_SKY,
            "linke_turbidity": float(props.linke_turbidity),
            "altitude_m": where.height,
            "diffuse": (
                f"isotropic sky x sky view, {props.diffuse_rays} rays" if props.diffuse else "none"
            ),
            "ground_reflected": "none",
        }
    return Plan(rays, steps, where, first, last, zone, parameters)


def _setup_cycles(saved: _Saved, scene: bpy.types.Scene) -> None:
    scene.render.engine = "CYCLES"
    saved.capture_cycles(scene)
    cycles = scene.cycles
    cycles.device = "CPU"
    cycles.samples = 1
    cycles.use_adaptive_sampling = False
    cycles.use_denoising = False
    if hasattr(cycles, "time_limit"):
        cycles.time_limit = 0.0
    cycles.bake_type = "EMIT"


@dataclass
class _Run:
    """Temporary data of one run, removed afterwards (with ``_Saved``'s)."""

    groups: list = field(default_factory=list)
    images: list = field(default_factory=list)

    def clean(self) -> None:
        for image in self.images:
            bpy.data.images.remove(image)
        for group in self.groups:
            if group.name in bpy.data.node_groups:
                bpy.data.node_groups.remove(group)
        self.images.clear()
        self.groups.clear()


def _bake_batches(
    context: bpy.types.Context,
    saved: _Saved,
    run: _Run,
    rays: core.Rays,
    offset: float,
    targets: list[tuple[bpy.types.Object, bpy.types.Image, str]],
    *,
    attributes: bool,
    margin: int,
) -> dict[str, np.ndarray]:
    """Bake every batch of rays; returns the (texels, 3) float64 sums per object name.

    ``targets`` are (object, scratch image, UV map name). Objects with the same
    UV map name bake together.
    """
    materials = {}
    probes = _probe(run, offset)
    totals: dict[str, np.ndarray] = {}
    batches = max(1, math.ceil(len(rays) / RAYS_PER_BAKE))
    wm = context.window_manager
    wm.progress_begin(0, batches)
    try:
        for batch in range(batches):
            start, stop = batch * RAYS_PER_BAKE, min(len(rays), (batch + 1) * RAYS_PER_BAKE)
            probe = _load(probes, rays, start, stop)
            for material in materials.values():
                node = next(n for n in material.node_tree.nodes if n.type == "GROUP")
                node.node_tree = probe
            for obj, image, _uv in targets:
                if obj.name in materials:
                    continue
                material = _probe_material(
                    f"{TEMP_PREFIX} {obj.name}", probe, image, attributes=attributes
                )
                saved.temp_materials.append(material)
                if obj in saved.temp_objects:
                    obj.data.materials.append(material)
                else:
                    _assign_material(saved, obj, material)
                materials[obj.name] = material
            by_uv: dict[str, list[bpy.types.Object]] = {}
            for obj, _image, uv in targets:
                by_uv.setdefault(uv, []).append(obj)
            for uv, objects in by_uv.items():
                ray_bake(context, objects, uv, margin=margin, error=SunHoursError)
            for obj, image, _uv in targets:
                baked = _pixels(image)[:, :3].astype(np.float64)
                if obj.name in totals:
                    totals[obj.name][:, :2] += baked[:, :2]
                else:
                    totals[obj.name] = baked
            wm.progress_update(batch + 1)
    finally:
        wm.progress_end()
    return totals


def _layer_spec(name: str, domain: Domain, units: str, label: str, description: str, provenance):
    return LayerSpec(
        name=name,
        domain=domain,
        data_type=DataType.FLOAT,
        units=units,
        origin=Origin.ANALYSIS,
        aggregation=Aggregation.AREA_MEAN,
        description=description,
        label=label,
        provenance=provenance,
    )


def _check_layer_name(scene, name: str, domain: Domain) -> None:
    existing = find_layer(scene, name)
    if existing is not None and existing.domain != domain.value:
        raise SunHoursError(
            f"Layer {name} is a {existing.domain.lower()} layer; choose another name or "
            f"the {existing.domain.lower()} values."
        )


def _forget_layers(scene: bpy.types.Scene, names: list[str]) -> None:
    layers = scene.geotools.layers
    for name in names:
        index = layers.find(name)
        if index >= 0:
            remove_layer_data(scene, layers[index])
            layers.remove(index)


def compute(context: bpy.types.Context) -> dict:
    """Run sun hours (and insolation) for the settings; store and return the result."""
    scene = context.scene
    props = analysis.settings(context).sun_hours
    domain = Domain(props.domain)
    if not hasattr(scene, "cycles"):
        raise SunHoursError("Enable the Cycles add-on: sun hours use Cycles bakes.")
    if scene.geotools.source_collection is None or not source_objects(scene):
        raise SunHoursError("The source collection has no meshes.")
    names = [props.hours_layer.strip()]
    if props.insolation:
        names.append(props.insolation_layer.strip())
    if len(set(names)) != len(names):
        raise SunHoursError("Sun hours and insolation need two different layer names.")
    for name in names:
        _check_layer_name(scene, name, domain)
    if domain is Domain.TEXTURE and not bpy.data.filepath:
        raise SunHoursError(
            "Save the .blend first: texture layer images are saved next to it in geotools_layers/"
        )
    began = time.perf_counter()
    timings: dict[str, float] = {}
    the_plan = plan(context)
    timings["plan"] = time.perf_counter() - began
    rays = the_plan.rays
    notes = list(the_plan.steps.notes)

    # Register the layers first (validation errors stop here, before any bake).
    labels = {
        names[0]: ("h", "Sun Hours", "Hours of direct sun (clear sky, sun's centre visible)"),
    }
    if props.insolation:
        labels[names[1]] = (
            "kWh/m²",
            "Insolation",
            "Clear-sky irradiation on the surface: direct beam x cos(incidence)"
            + (" plus isotropic diffuse sky x sky view" if props.diffuse else ""),
        )
    created = [name for name in labels if find_layer(scene, name) is None]
    try:
        for name, (units, label, description) in labels.items():
            spec = _layer_spec(name, domain, units, label, description, None)
            item = find_layer(scene, name)
            if item is not None:  # keep the layer's own label, description and display
                current = spec_from_props(item)
                spec = _layer_spec(
                    name,
                    domain,
                    units,
                    current.label or label,
                    current.description or description,
                    None,
                )
            try:
                item = ensure_layer(scene, spec)
            except ValueError as ex:
                raise SunHoursError(str(ex)) from None
            if domain is Domain.TEXTURE and not item.images:
                item.resolution = props.resolution

        saved = _Saved.capture(context)
        run = _Run()
        samples = None
        try:
            depsgraph = evaluated_depsgraph(scene)
            _setup_cycles(saved, scene)
            objects = source_objects(scene)
            show(saved, objects)
            if domain is Domain.TEXTURE:
                values, texels = _run_texture(context, saved, run, rays, names, depsgraph, notes)
                count = texels
            else:
                samples = collect_samples(scene, domain, depsgraph)
                notes.extend(samples.skipped)
                if not samples.parts:
                    raise SunHoursError(
                        "No source object can hold the values. " + " ".join(samples.skipped)
                    )
                points = np.concatenate(samples.points)
                normals = np.concatenate(samples.normals)
                offset = core.ray_offset(points, props.offset)
                the_plan.parameters["ray_offset"] = offset
                obj, width, height = build_sample_object(saved, context, points, normals)
                image = _float_image(f"{TEMP_PREFIX} Samples", width, height)
                run.images.append(image)
                timings["samples"] = time.perf_counter() - began - timings["plan"]
                totals = _bake_batches(
                    context,
                    saved,
                    run,
                    rays,
                    offset,
                    [(obj, image, PROBE_UV)],
                    attributes=True,
                    margin=0,
                )[obj.name][: len(points)]
                timings["bakes"] = (
                    time.perf_counter() - began - timings["plan"] - timings["samples"]
                )
                missing = int(np.count_nonzero(totals[:, 2] <= 0.5))
                if missing:
                    raise SunHoursError(f"{missing:,} samples were not baked (texel layout).")
                values = (totals[:, 0], totals[:, 1] / 1000.0)
                count = len(points)
        finally:
            unrestored = saved.restore(context)
            run.clean()
            if unrestored:
                notes.append(f"Could not restore the {', '.join(unrestored)}.")

        if domain is not Domain.TEXTURE:
            for part in samples.parts:
                window = slice(part.start, part.start + part.count)
                for name, array in zip(names, values, strict=False):
                    spec = spec_from_props(find_layer(scene, name))
                    write_layer(part.obj.data, spec, array[window].astype(np.float32))

    except BaseException:
        _forget_layers(scene, created)  # a failed run leaves no new, empty layers
        raise

    seconds = time.perf_counter() - began
    provenance = make_provenance(
        TOOL,
        {**the_plan.parameters, "domain": domain.value, "samples": count},
        extension_version(),
    )
    for name in names:
        item = find_layer(scene, name)
        item.provenance = format_provenance(provenance)
        item.origin = Origin.ANALYSIS.value
        refresh_layer(scene, item)
    ranges = {}
    for name in names:
        item = find_layer(scene, name)
        ranges[name] = [item.value_min, item.value_max]
    result = {
        "layers": names,
        "domain": domain.value,
        "samples": count,
        "rays": len(rays),
        "sun_rays": rays.sun_rays,
        "bakes": max(1, math.ceil(len(rays) / RAYS_PER_BAKE)),
        "seconds": seconds,
        "timings": timings,
        "first_day": the_plan.first_day.isoformat(),
        "last_day": the_plan.last_day.isoformat(),
        "zone": the_plan.zone,
        "ranges": ranges,
        "stats": rays.stats,
        "notes": notes,
    }
    props.result = json.dumps(result)
    props.error = ""
    return result


def _run_texture(context, saved, run, rays, names, depsgraph, notes):
    """Bake the probe on each source object's UVs; write the layer images."""
    scene = context.scene
    item = find_layer(scene, names[0])
    uv_wanted, size, margin = item.uv_map, item.resolution, item.margin
    for name in names[1:]:  # insolation: the same texels as the hours
        other = find_layer(scene, name)
        other.uv_map, other.resolution, other.margin = uv_wanted, size, margin
    targets = []
    points = []
    for obj in source_objects(scene):
        mesh = obj.evaluated_get(depsgraph).data
        uv = uv_map_name(mesh, uv_wanted)
        if uv is None:
            which = f"UV map {uv_wanted!r}" if uv_wanted else "UV map"
            raise SunHoursError(f"{obj.name} has no {which}.")
        _, issue = check_object_uvs(obj, depsgraph, uv, size)
        if issue is not None:
            notes.append(issue.message)
        image = _float_image(f"{TEMP_PREFIX} {obj.name}", size, size)
        run.images.append(image)
        targets.append((obj, image, uv))
        co = get_attribute(mesh, "position", "vector", np.float32, len(mesh.vertices) * 3)
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        points.append(co.reshape(-1, 3)[:: max(1, len(co) // 3 // 4096)] @ matrix[:3, :3].T)
        points[-1] += matrix[:3, 3]
    props = analysis.settings(context).sun_hours
    offset = core.ray_offset(np.concatenate(points), props.offset)
    totals = _bake_batches(
        context, saved, run, rays, offset, targets, attributes=False, margin=margin
    )
    texels = 0
    hours_all, energy_all = [], []
    for obj, _image, _uv in targets:
        total = totals[obj.name]
        valid = total[:, 2] > 0.5
        texels += int(np.count_nonzero(valid))
        for name, column, scale in zip(names, (0, 1), (1.0, 1e-3), strict=False):
            layer = find_layer(scene, name)
            fill = float(fill_value(spec_from_props(layer)))
            values = np.where(valid, total[:, column] * scale, fill).astype(np.float32)
            image = ensure_layer_image(layer, obj)
            if tuple(image.size) != (size, size):
                image.scale(size, size)
            pixels = np.empty((size * size, 4), np.float32)
            pixels[:, :3] = values[:, None]
            pixels[:, 3] = 1.0
            image.pixels.foreach_set(pixels.ravel())
            image.update()
            try:
                save_layer_image(find_layer(scene, name), obj)
            except (OSError, RuntimeError) as ex:
                raise SunHoursError(f"Cannot save the image of {obj.name}: {ex}") from None
            (hours_all if column == 0 else energy_all).append(values)
    return (hours_all, energy_all), texels


class GEOTOOLS_OT_sun_hours(bpy.types.Operator):
    """Hours of direct sun (and clear-sky insolation) over the period, into layers, with Cycles"""

    bl_idname = "geotools.sun_hours"
    bl_label = "Compute Sun Hours"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        if context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = analysis.settings(context).sun_hours
        try:
            result = compute(context)
        except (
            SunHoursError,
            site.GeodesyUnavailable,
            site.SiteError,
            sun.TimeZoneUnavailable,
            FieldError,
            ValueError,
            RuntimeError,
        ) as exc:
            props.error = str(exc)
            props.result = ""
            self.report({"ERROR"}, str(exc))
            redraw_ui(context)
            return {"CANCELLED"}
        for note in result["notes"]:
            self.report({"WARNING"}, note)
        self.report(
            {"INFO"},
            f"Sun hours on {result['samples']:,} samples, {result['rays']:,} rays, "
            f"{result['seconds']:.1f} s",
        )
        redraw_ui(context)
        return {"FINISHED"}


# --- shadow studies ----------------------------------------------------------------------------

SEASON_LABELS = {
    "march": "March equinox",
    "june": "June solstice",
    "september": "September equinox",
    "december": "December solstice",
}


def study_moments(context: bpy.types.Context) -> tuple[list[dict], tuple[str, ...]]:
    """The study's moments: one per chosen season day and time of day, in order."""
    settings = analysis.settings(context)
    sun_props, props = settings.sun, settings.shadow_study
    tz, _zone = analysis._time_zone(sun_props)
    chosen = [name for name in core.SEASONS if getattr(props, name)]
    if not chosen:
        raise ValueError("Choose at least one equinox or solstice.")
    if props.end_hour < props.start_hour:
        raise ValueError("The day's last frame must not be before its first.")
    count = math.floor((props.end_hour - props.start_hour) / props.step_hours + 1e-9) + 1
    seasons = core.season_moments(sun_props.year)
    moments, notes = [], []
    for name in chosen:
        day = seasons[name].astimezone(tz).date()
        for i in range(count):
            hours = props.start_hour + i * props.step_hours
            local = dt.datetime(day.year, day.month, day.day) + dt.timedelta(hours=hours)
            utc, more = sun.to_utc(local, tz)
            notes.extend(more)
            moments.append({"season": name, "local": local, "utc": utc})
    return moments, tuple(notes)


def shadow_study(context: bpy.types.Context) -> dict:
    """Keyframe the Sun light at each study moment, one frame each, with timeline markers."""
    settings = analysis.settings(context)
    sun_props, props = settings.sun, settings.shadow_study
    scene = context.scene
    where = analysis.compute_site(context, sun_props)
    moments, notes = study_moments(context)
    times = np.array([m["utc"].timestamp() for m in moments], dtype=np.float64)
    position = sun.solar_position(
        times, where.latitude, where.longitude, where.height, refraction=sun_props.refraction
    )
    scene_azimuth = (position.azimuth - where.convergence) % 360.0
    euler = sun.light_euler(scene_azimuth, position.elevation)
    energy = np.where(position.elevation > 0.0, sun_props.strength, 0.0)
    first = sun_props.frame_start
    frames = first + np.arange(len(moments), dtype=np.float64)

    obj = analysis.ensure_sun_light(context, sun_props)
    analysis._clear_sun_animation(obj)
    obj.rotation_mode = "XYZ"
    analysis._keyframe(
        obj,
        "rotation_euler",
        frames,
        euler.T,
        f"{analysis.SUN_ACTION_PREFIX} Study Rotation",
        analysis.CONSTANT,
    )
    analysis._keyframe(
        obj.data,
        "energy",
        frames,
        energy[None, :],
        f"{analysis.SUN_ACTION_PREFIX} Study Strength",
        analysis.CONSTANT,
    )
    old = json.loads(props.result) if props.result else {}
    for marker in list(scene.timeline_markers):
        if [marker.name, marker.frame] in old.get("markers", []):
            scene.timeline_markers.remove(marker)
    markers = []
    rows = []
    for i, moment in enumerate(moments):
        frame = int(frames[i])
        label = f"{moment['local']:%d %b %H:%M}"
        scene.timeline_markers.new(label, frame=frame)
        markers.append([label, frame])
        up = bool(position.elevation[i] > 0.0)
        if not up:
            notes = (*notes, f"{label}: the sun is below the horizon, so the light is off.")
        rows.append(
            {
                "frame": frame,
                "season": moment["season"],
                "local": moment["local"].isoformat(),
                "utc": moment["utc"].isoformat(),
                "azimuth": float(position.azimuth[i]),
                "elevation": float(position.elevation[i]),
            }
        )
    scene.frame_start = first
    scene.frame_end = int(frames[-1])
    scene.frame_set(first)
    result = {
        "light": obj.name,
        "frames": [first, int(frames[-1])],
        "moments": rows,
        "markers": markers,
        "convergence": where.convergence,
        "notes": list(dict.fromkeys(notes)),
    }
    props.result = json.dumps(result)
    props.error = ""
    return result


class GEOTOOLS_OT_sun_shadow_study(bpy.types.Operator):
    """Key the Sun light at the chosen times on the equinoxes and solstices, one frame each"""

    bl_idname = "geotools.sun_shadow_study"
    bl_label = "Set Up Shadow Study"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = analysis.settings(context).shadow_study
        try:
            result = shadow_study(context)
        except (
            site.GeodesyUnavailable,
            site.SiteError,
            sun.TimeZoneUnavailable,
            FieldError,
            ValueError,
        ) as exc:
            props.error = str(exc)
            props.result = ""
            self.report({"ERROR"}, str(exc))
            redraw_ui(context)
            return {"CANCELLED"}
        for note in result["notes"]:
            self.report({"WARNING"}, note)
        first, last = result["frames"]
        self.report({"INFO"}, f"Shadow study on frames {first}-{last}; render them to see it")
        redraw_ui(context)
        return {"FINISHED"}


classes = (GEOTOOLS_OT_sun_hours, GEOTOOLS_OT_sun_shadow_study)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()


def unregister() -> None:
    _unregister_classes()
