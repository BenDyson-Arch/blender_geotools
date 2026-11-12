"""Sun hours, clear-sky insolation and shadow studies, with Blender's ray machinery.

The maths is bpy-free (``geotools.analysis.sun_hours`` and ``.clearsky``):
which rays to cast and what each is worth. Blender casts them:

* **The engine is a Cycles EMIT bake.** A shader node group (the "probe")
  holds one Cycles ``Raycast`` node per sun direction, up to
  :data:`RAYS_PER_BAKE` at a time, as a chain of blocks of :data:`BLOCK`
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
from .bake_texture import (
    _assign_material,
    _deselect_all,
    _run_bake,
    _Saved,
    _show,
    check_object_uvs,
    uv_map_name,
)
from .common import extension_version, redraw_ui
from .export import same_topology
from .layers import (
    apply_modifiers,
    ensure_layer,
    ensure_layer_image,
    evaluated_depsgraph,
    find_layer,
    layer_meshes,
    refresh_layer,
    remove_layer_data,
    save_layer_image,
    source_objects,
    spec_from_props,
    write_layer,
)
from .properties import FieldError

log = logging.getLogger(__name__)

TOOL = "sun_hours"
"""Provenance tool name recorded on the layers."""
TEMP_PREFIX = "GeoTools Sun Hours"
PROBE_UV = "gt_probe"
POSITION_ATTRIBUTE = "gt_probe_position"
NORMAL_ATTRIBUTE = "gt_probe_normal"

RAYS_PER_BAKE = 1024
"""Rays per bake at most. Bake time is linear in rays; this keeps the fixed cost
of a bake (scene sync, BVH, shader compile) small next to the rays."""
BLOCK = 32
"""Rays per block group: the probe is a chain of blocks, each a chain of rays."""
RAY_LENGTH = 1.0e7
"""How far a ray looks for an occluder, in scene units."""
CORNER_INSET = 0.1
"""A corner sample sits this fraction of the way from its corner to the face centre."""

ALGORITHM = (
    "NREL SPA sun positions; Cycles Raycast nodes in an EMIT bake (one ray per sun "
    "direction per sample, offset along the normal); midpoint rule in time"
)
CLEAR_SKY = "Ineichen-Perez (2002), Kasten-Young air mass, solar constant 1366.1 W/m2"


class SunHoursError(RuntimeError):
    """The tool cannot run; the message says why."""


# --- the probe shader ------------------------------------------------------------------------


def _socket(tree, name: str, kind: str, in_out: str = "INPUT"):
    return tree.interface.new_socket(name, in_out=in_out, socket_type=f"NodeSocket{kind}")


def _ray_group(name: str):
    """One ray: Sum + (hours, energy x cos(incidence), 0) when it is in front and unblocked.

    Inputs Sum, Origin, Normal, Direction and Weights (hours, energy, 0);
    output Sum. Eight shader nodes. The sums travel as one vector, so each
    ray's own values are used up within the ray: Cycles' shader stack (255
    floats) would overflow if two chains kept every ray's values alive.
    """
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for socket in ("Sum", "Origin", "Normal", "Direction", "Weights"):
        _socket(tree, socket, "Vector")
    _socket(tree, "Sum", "Vector", "OUTPUT")
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


def _sums_group(name: str):
    """An empty tree with the chain's sockets: inputs Sum, Origin, Normal; output Sum."""
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for socket in ("Sum", "Origin", "Normal"):
        _socket(tree, socket, "Vector")
    _socket(tree, "Sum", "Vector", "OUTPUT")
    return tree


def _chain(tree, groups: list) -> list:
    """Chain one node of each of ``groups`` through Sum; returns the nodes.

    Origin and Normal go to every node. (Building a node tree from Python, or
    setting a socket value in one, slows down with the tree's size, so the
    probe is two levels of short chains: blocks of rays, then blocks.)
    """
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    total = group_in.outputs["Sum"]
    chained = []
    for group in groups:
        node = nodes.new("ShaderNodeGroup")
        node.node_tree = group
        links.new(total, node.inputs["Sum"])
        links.new(group_in.outputs["Origin"], node.inputs["Origin"])
        links.new(group_in.outputs["Normal"], node.inputs["Normal"])
        total = node.outputs["Sum"]
        chained.append(node)
    links.new(total, group_out.inputs["Sum"])
    return chained


@dataclass
class _Probe:
    """The probe shader groups of a run: blocks of BLOCK rays, and probes of n blocks."""

    run: _Run
    offset: float
    ray: object = None
    blocks: list = field(default_factory=list)  # (tree, its ray nodes)
    probes: dict = field(default_factory=dict)  # block count -> (probe tree, chain node)

    def __post_init__(self) -> None:
        self.ray = _ray_group(f"{TEMP_PREFIX} Ray")
        self.run.groups.append(self.ray)

    def _block(self, index: int):
        while len(self.blocks) <= index:
            tree = _sums_group(f"{TEMP_PREFIX} Block {len(self.blocks)}")
            self.run.groups.append(tree)
            self.blocks.append((tree, _chain(tree, [self.ray] * BLOCK)))
        return self.blocks[index]

    def load(self, rays: core.Rays, start: int, stop: int):
        """The probe for rays ``start:stop``, with their directions and weights set."""
        count = math.ceil((stop - start) / BLOCK)
        for b in range(count):
            _tree, ray_nodes = self._block(b)
            for i, node in enumerate(ray_nodes):
                k = start + b * BLOCK + i
                inputs = node.inputs
                if k < stop:
                    inputs["Direction"].default_value = tuple(map(float, rays.directions[k]))
                    inputs["Weights"].default_value = (rays.hours[k], rays.energy[k], 0.0)
                else:  # no ray: a zero direction is never in front, so never cast
                    inputs["Direction"].default_value = (0.0, 0.0, 0.0)
                    inputs["Weights"].default_value = (0.0, 0.0, 0.0)
        if count not in self.probes:
            self.probes[count] = self._probe(count)
        return self.probes[count]

    def _probe(self, count: int):
        """Inputs Position and Normal; output (hours, energy, 1) over ``count`` blocks."""
        chain = _sums_group(f"{TEMP_PREFIX} Chain {count}")
        self.run.groups.append(chain)
        _chain(chain, [self._block(b)[0] for b in range(count)])
        tree = bpy.data.node_groups.new(f"{TEMP_PREFIX} Probe {count}", "ShaderNodeTree")
        self.run.groups.append(tree)
        _socket(tree, "Position", "Vector")
        _socket(tree, "Normal", "Vector")
        _socket(tree, "Color", "Color", "OUTPUT")
        nodes, links = tree.nodes, tree.links
        group_in = nodes.new("NodeGroupInput")
        group_out = nodes.new("NodeGroupOutput")
        normal = group_in.outputs["Normal"]
        lift = nodes.new("ShaderNodeVectorMath")
        lift.operation = "SCALE"
        lift.inputs["Scale"].default_value = float(self.offset)
        links.new(normal, lift.inputs[0])
        origin = nodes.new("ShaderNodeVectorMath")
        origin.operation = "ADD"
        links.new(group_in.outputs["Position"], origin.inputs[0])
        links.new(lift.outputs[0], origin.inputs[1])
        mask = nodes.new("ShaderNodeCombineXYZ")  # the sums start at (0, 0, 1): "baked"
        mask.inputs[2].default_value = 1.0
        rays = nodes.new("ShaderNodeGroup")
        rays.node_tree = chain
        links.new(mask.outputs[0], rays.inputs["Sum"])
        links.new(origin.outputs[0], rays.inputs["Origin"])
        links.new(normal, rays.inputs["Normal"])
        links.new(rays.outputs["Sum"], group_out.inputs["Color"])
        return tree


def _probe_material(
    name: str, group, image: bpy.types.Image, *, attributes: bool
) -> bpy.types.Material:
    """Emission of the probe group, with ``image`` as the active (bake) node.

    ``attributes``: the sample position and normal come from the sample
    object's face attributes; otherwise the shading position and true normal.
    """
    material = bpy.data.materials.new(name)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    probe = nodes.new("ShaderNodeGroup")
    probe.node_tree = group
    if attributes:
        for socket, attribute in (("Position", POSITION_ATTRIBUTE), ("Normal", NORMAL_ATTRIBUTE)):
            node = nodes.new("ShaderNodeAttribute")
            node.attribute_type = "GEOMETRY"
            node.attribute_name = attribute
            links.new(node.outputs["Vector"], probe.inputs[socket])
    else:
        geometry = nodes.new("ShaderNodeNewGeometry")
        links.new(geometry.outputs["Position"], probe.inputs["Position"])
        links.new(geometry.outputs["True Normal"], probe.inputs["Normal"])
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    links.new(probe.outputs["Color"], emission.inputs["Color"])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(emission.outputs[0], output.inputs["Surface"])
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Closest"
    nodes.active = texture
    return material


def _float_image(name: str, width: int, height: int) -> bpy.types.Image:
    image = bpy.data.images.new(name, width, height, alpha=False, float_buffer=True, is_data=True)
    image.colorspace_settings.name = "Non-Color"
    return image


def _pixels(image: bpy.types.Image) -> np.ndarray:
    pixels = np.empty(len(image.pixels), np.float32)
    image.pixels.foreach_get(pixels)
    return pixels.reshape(-1, 4)


# --- samples for FACE and CORNER layers ------------------------------------------------------


@dataclass
class _Part:
    obj: bpy.types.Object
    start: int
    count: int


@dataclass
class _Samples:
    parts: list[_Part] = field(default_factory=list)
    points: list[np.ndarray] = field(default_factory=list)
    normals: list[np.ndarray] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(p.count for p in self.parts)


def _world(matrix: np.ndarray, points: np.ndarray, normals: np.ndarray):
    linear = matrix[:3, :3]
    world = points @ linear.T + matrix[:3, 3]
    # Normals are covectors: the inverse transpose keeps them perpendicular.
    turned = normals @ np.linalg.inv(linear)
    length = np.linalg.norm(turned, axis=1, keepdims=True)
    turned = np.divide(turned, length, out=np.zeros_like(turned), where=length > 0)
    return world, turned


def _set_attribute(mesh: bpy.types.Mesh, name: str, field: str, values: np.ndarray) -> None:
    mesh.attributes[name].data.foreach_set(field, np.ascontiguousarray(values).ravel())


def _get_attribute(mesh: bpy.types.Mesh, name: str, field: str, dtype, size: int) -> np.ndarray:
    values = np.empty(size, dtype)
    mesh.attributes[name].data.foreach_get(field, values)
    return values


def _mesh_samples(mesh: bpy.types.Mesh, domain: Domain) -> tuple[np.ndarray, np.ndarray]:
    """Local sample points and normals of a mesh, float64 (one per face or corner).

    A face's centre is the mean of its corners, as Blender's ``polygon.center``.
    """
    faces = len(mesh.polygons)
    starts = np.empty(faces, np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    sizes = np.diff(np.append(starts, len(mesh.loops)))
    corner_vertex = _get_attribute(mesh, ".corner_vert", "value", np.int32, len(mesh.loops))
    co = _get_attribute(mesh, "position", "vector", np.float32, len(mesh.vertices) * 3)
    corners = co.reshape(-1, 3).astype(np.float64)[corner_vertex]
    centres = np.add.reduceat(corners, starts, axis=0) / sizes[:, None]
    normals = np.empty(faces * 3, np.float32)
    mesh.polygon_normals.foreach_get("vector", normals)
    normals = normals.reshape(-1, 3).astype(np.float64)
    if domain is Domain.FACE:
        return centres, normals
    corner_face = np.repeat(np.arange(faces), sizes)
    points = corners + CORNER_INSET * (centres[corner_face] - corners)
    return points, normals[corner_face]


def collect_samples(scene: bpy.types.Scene, domain: Domain, depsgraph) -> _Samples:
    """World-space sample points and normals of every source object that can take the layer.

    Values are written to the original mesh, so an object is skipped (with a
    note) when its evaluated mesh has another topology, or when an earlier
    object shares its mesh.
    """
    samples = _Samples()
    objects = source_objects(scene)
    for obj in objects:
        if obj.data.is_editmode:
            raise SunHoursError(f"{obj.name} is in Edit Mode; switch to Object Mode first.")
    apply = apply_modifiers(scene)
    seen: dict[int, str] = {}
    start = 0
    for obj, mesh in layer_meshes(scene, objects, depsgraph=depsgraph):
        pointer = obj.data.as_pointer()
        if pointer in seen:
            samples.skipped.append(
                f"{obj.name} shares its mesh with {seen[pointer]}; it has that object's values."
            )
            continue
        seen[pointer] = obj.name
        if apply and not same_topology(obj.data, mesh):
            samples.skipped.append(
                f"{obj.name}: its modifiers change the topology, so face values cannot be "
                "stored on its mesh. Use a texture layer, or apply the modifiers."
            )
            continue
        points, normals = _mesh_samples(mesh, domain)
        if not len(points):
            continue
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        world, turned = _world(matrix, points, normals)
        samples.parts.append(_Part(obj, start, len(points)))
        samples.points.append(world)
        samples.normals.append(turned)
        start += len(points)
    return samples


def _texel_layout(count: int) -> tuple[int, int]:
    width = max(1, math.ceil(math.sqrt(count)))
    return width, max(1, math.ceil(count / width))


def build_sample_object(
    saved: _Saved, context: bpy.types.Context, points: np.ndarray, normals: np.ndarray
) -> tuple[bpy.types.Object, int, int]:
    """The temporary sample object: one tiny triangle per sample, each on its own texel.

    Returns the object and its texel grid (width, height): sample ``i`` is
    texel ``(i % width, i // width)``. The triangle's UVs surround that texel's
    centre (and no other); its 3D shape is a few float32 steps across around
    the point, and the exact point and normal are face attributes. Cycles
    only bakes an object some ray type sees, so it is visible to camera rays,
    and the Raycast node sees it too: but rays start 16 float32 steps off the
    surface, away from their own triangle, and another sample's is hit only
    by a ray passing within a few float32 steps of that sample's point.
    """
    n = len(points)
    width, height = _texel_layout(n)
    p32 = points.astype(np.float32)
    size = 4.0 * np.spacing(np.maximum(np.abs(p32).max(axis=1), np.float32(1.0)))
    offsets = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, -1.0, 0.0]])
    corners = (p32[:, None, :] + size[:, None, None] * offsets[None, :, :]).astype(np.float32)

    mesh = bpy.data.meshes.new(f"{TEMP_PREFIX} Samples")
    saved.temp_meshes.append(mesh)
    mesh.vertices.add(3 * n)
    mesh.loops.add(3 * n)
    mesh.polygons.add(n)
    # The attribute arrays take numpy buffers directly: far faster than
    # vertices.co and loops.vertex_index, which go element by element.
    _set_attribute(mesh, "position", "vector", corners)
    _set_attribute(mesh, ".corner_vert", "value", np.arange(3 * n, dtype=np.int32))
    mesh.polygons.foreach_set("loop_start", np.arange(0, 3 * n, 3, dtype=np.int32))
    mesh.update(calc_edges=False)
    for name, values in ((POSITION_ATTRIBUTE, p32), (NORMAL_ATTRIBUTE, normals)):
        attr = mesh.attributes.new(name, "FLOAT_VECTOR", "FACE")
        attr.data.foreach_set("vector", np.ascontiguousarray(values, np.float32).ravel())

    index = np.arange(n)
    centre = np.stack([(index % width + 0.5) / width, (index // width + 0.5) / height], axis=1)
    angles = np.radians([90.0, 210.0, 330.0])
    # 0.9 texel from the centre: the triangle holds its own texel centre (and the
    # jitter around it), never a neighbour's, which is a full texel away.
    spread = 0.9 * np.stack([np.cos(angles) / width, np.sin(angles) / height], axis=1)
    uv = centre[:, None, :] + spread[None, :, :]
    mesh.uv_layers.new(name=PROBE_UV)
    _set_attribute(mesh, PROBE_UV, "vector", uv.astype(np.float32))

    obj = bpy.data.objects.new(mesh.name, mesh)
    saved.temp_objects.append(obj)
    # Cycles bakes only an object some ray type can see, and the Raycast node
    # then sees it too. Each triangle is a few float32 steps wide, horizontal,
    # at its sample point, and rays leave 16 steps off the surface, so no ray
    # meets one but by a chance of the order of (4 ulp / spacing)^2.
    for flag in (
        "visible_diffuse",
        "visible_glossy",
        "visible_transmission",
        "visible_volume_scatter",
        "visible_shadow",
    ):
        setattr(obj, flag, False)
    context.scene.collection.objects.link(obj)
    return obj, width, height


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
    view_layer = context.view_layer
    materials = {}
    probes = _Probe(run, offset)
    totals: dict[str, np.ndarray] = {}
    batches = max(1, math.ceil(len(rays) / RAYS_PER_BAKE))
    wm = context.window_manager
    wm.progress_begin(0, batches)
    try:
        for batch in range(batches):
            start, stop = batch * RAYS_PER_BAKE, min(len(rays), (batch + 1) * RAYS_PER_BAKE)
            probe = probes.load(rays, start, stop)
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
                _deselect_all(view_layer)
                for obj in objects:
                    obj.select_set(True)
                view_layer.objects.active = objects[0]
                try:
                    _run_bake(
                        type="EMIT",
                        target="IMAGE_TEXTURES",
                        margin=margin,
                        margin_type="EXTEND",
                        use_clear=True,
                        use_selected_to_active=False,
                        use_cage=False,
                        use_split_materials=False,
                        uv_layer=uv,
                    )
                except RuntimeError as ex:
                    raise SunHoursError(f"The bake failed: {ex}") from None
            for obj, image, _uv in targets:
                pixels = _pixels(image)[:, :3].astype(np.float64)
                if obj.name in totals:
                    totals[obj.name][:, :2] += pixels[:, :2]
                else:
                    totals[obj.name] = pixels
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
            for obj in objects:
                _show(saved, obj)
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
        co = _get_attribute(mesh, "position", "vector", np.float32, len(mesh.vertices) * 3)
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
