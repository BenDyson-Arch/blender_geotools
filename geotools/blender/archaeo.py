"""Archaeoastronomy (M5): the horizon from a point, rise and set events, sightlines, alignments.

The maths is bpy-free (``geotools.analysis.horizon``, ``.archaeo``, ``.moon``);
this module stores settings and results and drives Blender:

* **Horizon profile** (``geotools.archaeo_horizon``): rays from the observer's
  eye in a fan of elevations at every azimuth, cast by Cycles in one EMIT bake
  per pass, as sun hours casts its rays. Here each ray is one texel of a
  temporary sample object (``ray_bake.build_sample_object``): its direction
  is the sample's face attribute, and a probe shader with one ``Raycast``
  node emits (hit, distance, 1). A coarse pass (every 0.1°) and a fine pass
  (64 rays inside each 0.1° bracket that can hold the skyline,
  ``horizon.fine_brackets``) find the skyline to about 0.002°. Everything that renders is terrain; the source objects are shown
  for the bake.
* **Events** (``geotools.archaeo_events``): the epoch's solstice, equinox and
  lunar standstill rises and sets, and the Sun panel year's dated sunrises
  and sunsets, against the astronomical horizon and the profile.
* **Sightlines** (``geotools.archaeo_sightlines``): an ordinary mesh of loose
  edges, one per event, with EDGE attributes, so Export Vectors and the
  vector publish carry them as GeoPackage or GeoJSON lines.
* **Alignment check** (``geotools.archaeo_alignment``): the selected edges of
  the active mesh against the events, with the declination each implies.

Settings and results live on ``scene.geotools_analysis.archaeo``; geodetic
numbers are JSON of binary64 floats in ``StringProperty``s. See
docs/design/m5-analysis.md.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from ..analysis import archaeo as core
from ..analysis import horizon, site, sun
from ..analysis import sun_hours as sun_hours_core
from ..core.placement import PlacementMode
from . import analysis
from .bake_texture import _Saved
from .common import redraw_ui
from .layers import source_objects
from .properties import FieldError, format_vec, parse_vec
from .ray_bake import (
    NORMAL_ATTRIBUTE,
    PROBE_UV,
    RAY_LENGTH,
    Run,
    bake,
    build_sample_object,
    float_image,
    parking,
    pixels,
    setup_cycles,
    show,
)

log = logging.getLogger(__name__)

TEMP_PREFIX = "GeoTools Horizon"
SIGHTLINES_NAME = "GeoTools Sightlines"
EDGE_MARGIN = 0.01
"""A skyline point this fraction of the terrain's extent from its edge is flagged."""
MAX_ALIGNMENT_ROWS = 2000

LIMB_ITEMS = (
    ("UPPER", "Upper Limb", "The first or last gleam: the top of the disc on the horizon"),
    ("CENTRE", "Centre", "The centre of the disc on the horizon"),
    ("LOWER", "Lower Limb", "The whole disc just clear of the horizon"),
)
LUNAR_ITEMS = (
    ("MEAN", "Node Cycle", "±(ε + i) and ±(ε − i): the standstill geometry from the node cycle"),
    (
        "EXTREME",
        "Ephemeris",
        (
            "The most extreme declinations the Moon reaches at the standstills nearest the epoch "
            "(Meeus), including the 173-day wobble of the inclination"
        ),
    ),
)
OBSERVER_ITEMS = (("OBSERVER", "Observer", "The archaeoastronomy observer"),)


class ArchaeoError(RuntimeError):
    """The tool cannot run; the message says why."""


# --- properties --------------------------------------------------------------------------------


class GeoToolsArchaeoSettings(bpy.types.PropertyGroup):
    """Settings and results of archaeoastronomy (``scene.geotools_analysis.archaeo``)."""

    observer: StringProperty(default="")  # format_vec of the float32 ground point
    eye_height: FloatProperty(
        name="Eye Height",
        description="Height of the eye above the picked point, scene units",
        default=1.6,
        min=0.0,
        soft_max=10.0,
    )
    azimuth_step: FloatProperty(
        name="Azimuth Step",
        description="Degrees between the profile's azimuths",
        default=0.5,
        min=0.05,
        max=10.0,
        step=10,
        precision=2,
    )
    ignore_within: FloatProperty(
        name="Ignore Within",
        description="Rays start this far from the eye, so nearer terrain is left out (for a far "
        "horizon to use in Sun Hours), scene units",
        default=0.0,
        min=0.0,
        soft_max=10000.0,
    )
    curvature: BoolProperty(
        name="Earth Curvature",
        description="Lower distant terrain by the Earth's curvature, (1 − k) d / 2R",
        default=True,
    )
    refraction: BoolProperty(
        name="Refraction",
        description="Astronomical refraction (Bennett, about 34′ at the horizon) for the sun and "
        "Moon, and terrestrial refraction (k = 0.13) for the terrain",
        default=True,
    )
    open_elevation: FloatProperty(
        name="Open Horizon",
        description="Altitude used where no terrain is hit (degrees)",
        default=0.0,
        min=-5.0,
        max=10.0,
        precision=2,
    )
    profile: StringProperty(default="")  # JSON of horizon.HorizonProfile
    profile_error: StringProperty(default="")

    epoch_year: IntProperty(
        name="Epoch",
        description="Year of the event geometry, astronomical numbering: 0 is 1 BCE, −2999 is "
        "3000 BCE",
        default=2026,
        min=core.EPOCH_MIN,
        max=core.EPOCH_MAX,
    )
    limb: EnumProperty(
        name="Limb",
        description="Which part of the sun's or moon's disc marks the rise or set",
        items=LIMB_ITEMS,
        default="UPPER",
    )
    lunar: EnumProperty(
        name="Standstills",
        description="How the lunar standstill declinations are worked out",
        items=LUNAR_ITEMS,
        default="MEAN",
    )
    moon_events: BoolProperty(
        name="Lunar Standstills", description="Include the lunar standstills", default=True
    )
    dated_events: BoolProperty(
        name="Dated Sunrises",
        description="Also sunrise and sunset times and azimuths on the Sun panel year's "
        "equinoxes and solstices and on the Sun panel date",
        default=True,
    )
    events: StringProperty(default="")  # JSON: {"events": [...], "summary": {...}}
    events_error: StringProperty(default="")

    sightline_length: FloatProperty(
        name="Length",
        description="Length of a sightline with no horizon hit, scene units",
        default=1000.0,
        min=0.001,
        soft_max=100000.0,
    )
    sightlines_result: StringProperty(default="")
    alignment: StringProperty(default="")  # JSON
    alignment_error: StringProperty(default="")
    key: StringProperty(default="")  # placement the observer was read with


def settings(context: bpy.types.Context) -> GeoToolsArchaeoSettings:
    return context.scene.geotools_analysis.archaeo


def load_profile(context: bpy.types.Context) -> horizon.HorizonProfile | None:
    return horizon.HorizonProfile.from_json(settings(context).profile)


def load_events(context: bpy.types.Context) -> tuple[list[core.Event], dict]:
    text = settings(context).events
    if not text:
        return [], {}
    data = json.loads(text)
    return [core.Event(**row) for row in data["events"]], data["summary"]


# --- the observer ------------------------------------------------------------------------------


def _lon_lat(context: bpy.types.Context) -> tuple[float, float] | None:
    placement = analysis._placement(context)
    if PlacementMode(placement.mode) is PlacementMode.LOCAL:
        return analysis._anchor_lon_lat(context)
    return None


def observer_site(context: bpy.types.Context) -> tuple[tuple[float, float, float], site.SunSite]:
    """The observer's ground point and its site; raises ArchaeoError and the site errors."""
    point = parse_vec(settings(context).observer)
    if point is None:
        raise ArchaeoError("Set the observer first: 3D cursor or Pick.")
    placement = analysis._placement(context)
    if PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS and not site.available():
        raise site.GeodesyUnavailable(analysis.NEEDS_PYPROJ)
    return point, site.sun_site(placement, point, local_lon_lat=_lon_lat(context))


def _scale_factor(context: bpy.types.Context, point) -> float:
    placement = analysis._placement(context)
    if PlacementMode(placement.mode) is not PlacementMode.SOURCE_CRS:
        return 1.0
    reading = site.read_point(placement, point)
    return reading.combined_scale if reading.latitude is not None else 1.0


def set_observer(context: bpy.types.Context, location) -> None:
    props = settings(context)
    point = tuple(float(v) for v in np.asarray(location, dtype=np.float32))
    props.observer = format_vec(point)
    props.key = analysis.placement_key(context)
    props.profile = ""  # a profile belongs to its observer
    props.profile_error = ""


# --- the horizon bake ----------------------------------------------------------------------------


def _probe_material(name: str, image, origin, start: float) -> bpy.types.Material:
    """Emit (is hit, hit distance, 1) of one ray from ``origin`` along the sample's direction."""
    material = bpy.data.materials.new(name)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    direction = nodes.new("ShaderNodeAttribute")
    direction.attribute_type = "GEOMETRY"
    direction.attribute_name = NORMAL_ATTRIBUTE
    eye = nodes.new("ShaderNodeCombineXYZ")
    for i, value in enumerate(origin):
        eye.inputs[i].default_value = float(value)
    skip = nodes.new("ShaderNodeVectorMath")
    skip.operation = "SCALE"
    skip.inputs["Scale"].default_value = float(start)
    links.new(direction.outputs["Vector"], skip.inputs[0])
    begin = nodes.new("ShaderNodeVectorMath")
    begin.operation = "ADD"
    links.new(eye.outputs[0], begin.inputs[0])
    links.new(skip.outputs[0], begin.inputs[1])
    cast = nodes.new("ShaderNodeRaycast")
    cast.inputs["Length"].default_value = RAY_LENGTH
    # Position and Direction fall back to the shading point and normal unless linked.
    links.new(begin.outputs[0], cast.inputs["Position"])
    links.new(direction.outputs["Vector"], cast.inputs["Direction"])
    out = nodes.new("ShaderNodeCombineXYZ")
    out.inputs[2].default_value = 1.0
    links.new(cast.outputs["Is Hit"], out.inputs[0])
    links.new(cast.outputs["Hit Distance"], out.inputs[1])
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    links.new(out.outputs[0], emission.inputs["Color"])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(emission.outputs[0], output.inputs["Surface"])
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Closest"
    nodes.active = texture
    return material


def cast_rays(
    context: bpy.types.Context, origin: np.ndarray, directions: np.ndarray, start: float = 0.0
) -> tuple[np.ndarray, np.ndarray]:
    """Cast ``directions`` from ``origin`` in one Cycles bake; (hit, distance from origin).

    Rays begin ``start`` along their direction; the distance includes it.
    Everything that renders is an occluder, and the source objects are
    shown for the bake.
    """
    scene = context.scene
    if not hasattr(scene, "cycles"):
        raise ArchaeoError("Enable the Cycles add-on: the horizon uses a Cycles bake.")
    n = len(directions)
    saved = _Saved.capture(context)
    run = Run()
    try:
        setup_cycles(saved, scene)
        show(saved, source_objects(scene))
        # The triangles must be in the scene for Cycles to bake them, so they
        # are far below and close together: a ray would have to point almost
        # straight down to meet one, and the fan never does.
        depth = max(1.0e4, 4.0 * _terrain_extent(context))
        points = parking(np.asarray(origin, dtype=np.float64), n, depth)
        obj, width, height = build_sample_object(
            saved, context, points, directions, name=f"{TEMP_PREFIX} Samples"
        )
        image = float_image(f"{TEMP_PREFIX} Rays", width, height)
        run.images.append(image)
        material = _probe_material(f"{TEMP_PREFIX} Probe", image, origin, start)
        saved.temp_materials.append(material)
        obj.data.materials.append(material)
        bake(context, [obj], PROBE_UV, error=ArchaeoError)
        baked = pixels(image)[:n].astype(np.float64)
    finally:
        unrestored = saved.restore(context)
        run.clean()
        if unrestored:
            log.warning("Could not restore the %s", ", ".join(unrestored))
    if np.count_nonzero(baked[:, 2] <= 0.5):
        raise ArchaeoError("Some rays were not baked (texel layout).")
    hit = baked[:, 0] > 0.5
    return hit, np.where(hit, baked[:, 1] + float(start), np.nan)


def _occluders(context: bpy.types.Context) -> list[bpy.types.Object]:
    sources = set(source_objects(context.scene))
    return [
        o
        for o in context.scene.objects
        if o.type == "MESH"
        and (o in sources or (not o.hide_render and o.visible_camera))
        and not o.name.startswith(SIGHTLINES_NAME)
    ]


def _terrain_bounds(context: bpy.types.Context) -> np.ndarray | None:
    """(xmin, ymin, xmax, ymax) of the occluders' world bounding boxes, or None."""
    corners = []
    for obj in _occluders(context):
        box = np.array([tuple(c) for c in obj.bound_box], dtype=np.float64)
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        corners.append(box @ matrix[:3, :3].T + matrix[:3, 3])
    if not corners:
        return None
    every = np.concatenate(corners)
    return np.array([*every[:, :2].min(axis=0), *every[:, :2].max(axis=0)])


def _terrain_extent(context: bpy.types.Context) -> float:
    bounds = _terrain_bounds(context)
    if bounds is None:
        return 0.0
    return float(np.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1]))


def compute_horizon(context: bpy.types.Context) -> horizon.HorizonProfile:
    """The horizon profile from the observer; stored and returned."""
    props = settings(context)
    began = time.perf_counter()
    point, where = observer_site(context)
    if not _occluders(context):
        raise ArchaeoError("There are no meshes to be the terrain.")
    eye = np.array(point, dtype=np.float64) + np.array([0.0, 0.0, float(props.eye_height)])
    true_az = horizon.azimuths(props.azimuth_step)
    scene_az = (true_az - where.convergence) % 360.0
    csf = _scale_factor(context, point)
    k = horizon.TERRESTRIAL_K if props.refraction else 0.0
    start = float(props.ignore_within)

    coarse = horizon.coarse_elevations()
    hit, distance = cast_rays(
        context, eye, horizon.ray_directions(scene_az, coarse).astype(np.float32), start
    )
    shape = (len(true_az), len(coarse))
    hit, distance = hit.reshape(shape), distance.reshape(shape)
    hits = horizon.best_hits(
        coarse, hit, distance, scale_factor=csf, curvature=props.curvature, k=k
    )
    closed = hit[:, -1]
    first = time.perf_counter() - began

    # Every bracket that can hold the skyline (usually one or two per azimuth).
    rows, base = horizon.fine_brackets(
        coarse, hit, distance, scale_factor=csf, curvature=props.curvature, k=k
    )
    fine_rays = 0
    if len(rows):
        fine = horizon.fine_elevations(base)
        f_hit, f_distance = cast_rays(
            context, eye, horizon.ray_directions(scene_az[rows], fine).astype(np.float32), start
        )
        refined = horizon.best_hits(
            fine,
            f_hit.reshape(fine.shape),
            f_distance.reshape(fine.shape),
            scale_factor=csf,
            curvature=props.curvature,
            k=k,
        )
        hits = horizon.merge_rows(hits, rows, refined)
        fine_rays = fine.size
    edge = _edge_flags(context, eye, scene_az, hits)
    seconds = time.perf_counter() - began
    reading = site.read_point(analysis._placement(context), point, geodetic=False)
    info = {
        "observer": list(point),
        "eye": [float(v) for v in eye],
        "eye_height": float(props.eye_height),
        "latitude": where.latitude,
        "longitude": where.longitude,
        "height": where.height,
        "geodetic_crs": where.geodetic_crs,
        "convergence": where.convergence,
        "north": where.north,
        "east": reading.east,
        "north_coordinate": reading.north,
        "level": reading.level,
        "combined_scale": csf,
        "curvature": bool(props.curvature),
        "refraction": bool(props.refraction),
        "terrestrial_k": k,
        "earth_radius": horizon.EARTH_RADIUS,
        "ignore_within": start,
        "azimuth_step": float(360.0 / len(true_az)),
        "rays": int(hit.size + fine_rays),
        "fine_brackets": len(rows),
        "seconds": seconds,
        "first_pass_seconds": first,
        "key": analysis.placement_key(context),
    }
    profile = horizon.make_profile(
        true_az,
        hits,
        open_elevation=float(props.open_elevation),
        closed=closed,
        edge=edge,
        info=info,
    )
    props.profile = profile.to_json()
    props.profile_error = ""
    return profile


def _edge_flags(context, eye: np.ndarray, scene_az: np.ndarray, hits: horizon.Hits) -> np.ndarray:
    """True where the skyline point is within EDGE_MARGIN of the terrain's bounding box edge."""
    bounds = _terrain_bounds(context)
    if bounds is None:
        return np.zeros(len(scene_az), dtype=bool)
    el = np.radians(np.nan_to_num(hits.geometric))
    reach = np.nan_to_num(hits.distance) * np.cos(el)
    az = np.radians(scene_az)
    x = eye[0] + reach * np.sin(az)
    y = eye[1] + reach * np.cos(az)
    margin = EDGE_MARGIN * max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    near = (
        (x - bounds[0] < margin)
        | (bounds[2] - x < margin)
        | (y - bounds[1] < margin)
        | (bounds[3] - y < margin)
    )
    return near & np.isfinite(hits.distance)


# --- events --------------------------------------------------------------------------------------


def compute_events(context: bpy.types.Context) -> tuple[list[core.Event], dict]:
    """The epoch's events and (optionally) the Sun panel year's dated sun events; stored."""
    props = settings(context)
    sun_props = analysis.settings(context).sun
    profile = load_profile(context)
    if props.observer:
        _point, where = observer_site(context)
        site_from = "observer"
    else:
        where = analysis.compute_site(context, sun_props)
        site_from = "Sun panel site"
    # Bennett's 1010 hPa at sea level, scaled to the site's height (standard atmosphere).
    pressure = sun.standard_pressure(where.height, horizon.REFRACTION_PRESSURE_HPA)
    events, summary = core.epoch_events(
        props.epoch_year,
        where.latitude,
        profile,
        refract=props.refraction,
        limb=props.limb,
        lunar=props.lunar,
        moon_events=props.moon_events,
        pressure_hpa=pressure,
    )
    if props.dated_events:
        tz, zone = analysis._time_zone(sun_props)
        year = sun_props.year
        seasons = sun_hours_core.season_moments(year)
        days = [
            (label, seasons[key].astimezone(tz).date())
            for key, label in (
                ("march", "March equinox"),
                ("june", "June solstice"),
                ("september", "September equinox"),
                ("december", "December solstice"),
            )
        ]
        try:
            chosen = dt.date(year, sun_props.month, sun_props.day)
        except ValueError as exc:
            raise ValueError(f"Not a date in the Sun panel: {exc}") from None
        if chosen not in {d for _l, d in days}:
            days.append((f"{chosen:%d %b %Y}", chosen))
        events += core.dated_sun_events(
            days,
            tz,
            where.latitude,
            where.longitude,
            where.height,
            profile,
            refract=props.refraction,
            limb=props.limb,
            pressure_hpa=pressure,
        )
        summary["zone"] = zone
        summary["dated_year"] = year
        if year > 6000:
            summary["trust"].append("SPA is validated to the year 6000.")
    summary.update(
        {
            "latitude": where.latitude,
            "longitude": where.longitude,
            "site": site_from,
            "profile": profile is not None,
            "limb": props.limb,
            "lunar": props.lunar,
            "refraction": bool(props.refraction),
            "height": where.height,
            "pressure_hpa": pressure,
            "convergence": where.convergence,
        }
    )
    props.events = json.dumps({"events": [e.to_dict() for e in events], "summary": summary})
    props.events_error = ""
    return events, summary


def events_text(events: list[core.Event], summary: dict) -> str:
    """The events table as tab-separated lines at full precision."""

    def num(value: float) -> str:
        return "" if not math.isfinite(value) else repr(float(value))

    lines = [
        f"Latitude\t{summary.get('latitude')!r}",
        f"Longitude\t{summary.get('longitude')!r}",
        f"Epoch\t{summary.get('epoch')}",
        f"Obliquity (deg)\t{summary.get('obliquity')!r}",
        (
            "Event\tWhen\tDeclination (deg)\tAzimuth on 0 deg horizon (deg true)\t"
            "Azimuth on horizon profile (deg true)\tHorizon altitude (deg)\tTime (0 deg)\t"
            "Time (profile)"
        ),
    ]
    for e in events:
        lines.append(
            "\t".join(
                [
                    e.name,
                    e.when,
                    num(e.declination),
                    num(e.azimuth),
                    num(e.horizon_azimuth),
                    num(e.horizon_altitude),
                    e.time,
                    e.horizon_time,
                ]
            )
        )
    for kind in ("major", "minor"):
        if kind in summary:
            s = summary[kind]
            lines.append(
                f"{kind.capitalize()} standstill\tnode {s['nearest_text']}\t"
                f"north {s['north']!r} ({s['north_text']})\tsouth {s['south']!r} "
                f"({s['south_text']})"
            )
    return "\n".join(lines)


# --- sightlines ----------------------------------------------------------------------------------


def make_sightlines(context: bpy.types.Context) -> bpy.types.Object:
    """A mesh of one loose edge per event, from the eye towards its azimuth; replaces the old one."""
    props = settings(context)
    events, summary = load_events(context)
    if not events:
        raise ArchaeoError("Compute the events first.")
    point, where = observer_site(context)
    profile = load_profile(context)
    eye = np.array(point, dtype=np.float64) + np.array([0.0, 0.0, float(props.eye_height)])
    rows = [e for e in events if math.isfinite(e.best_azimuth)]
    if not rows:
        raise ArchaeoError("No event rises or sets here.")
    starts, ends, lengths, sources = [], [], [], []
    for e in rows:
        true_az = e.best_azimuth
        scene_az = math.radians((true_az - where.convergence) % 360.0)
        use_profile = profile is not None and math.isfinite(e.horizon_azimuth)
        if use_profile:
            i = profile.nearest(true_az)
            distance = float(profile.distance[i])
            elevation = math.radians(float(profile.geometric[i]))
            if not math.isfinite(distance):  # open: the chosen length, level
                distance, elevation = float(props.sightline_length), 0.0
        else:
            distance, elevation = float(props.sightline_length), 0.0
        direction = np.array(
            [
                math.sin(scene_az) * math.cos(elevation),
                math.cos(scene_az) * math.cos(elevation),
                math.sin(elevation),
            ]
        )
        starts.append(eye)
        ends.append(eye + distance * direction)
        lengths.append(distance)
        sources.append("profile" if use_profile else "astronomical")
    n = len(rows)
    positions = np.empty((2 * n, 3), dtype=np.float32)
    positions[0::2] = np.array(starts)
    positions[1::2] = np.array(ends)

    old = bpy.data.objects.get(SIGHTLINES_NAME)
    old_mesh = old.data if old is not None and old.type == "MESH" else None
    mesh = bpy.data.meshes.new(SIGHTLINES_NAME)
    mesh.vertices.add(2 * n)
    mesh.vertices.foreach_set("co", positions.ravel())
    mesh.edges.add(n)
    mesh.edges.foreach_set("vertices", np.arange(2 * n, dtype=np.int32))
    mesh.update()
    floats = {
        "azimuth": [e.best_azimuth for e in rows],
        "altitude": [e.best_altitude for e in rows],
        "declination": [e.declination for e in rows],
        "length": lengths,
    }
    attr = mesh.attributes.new("fid", "INT", "EDGE")
    attr.data.foreach_set("value", np.arange(1, n + 1, dtype=np.int32))
    for name, values in floats.items():
        attr = mesh.attributes.new(name, "FLOAT", "EDGE")
        attr.data.foreach_set("value", np.asarray(values, dtype=np.float32))
    strings = {
        "event": [e.name for e in rows],
        "body": [e.body for e in rows],
        "phase": [e.phase for e in rows],
        "epoch": [e.when for e in rows],
        "time": [
            e.horizon_time if s == "profile" else e.time for e, s in zip(rows, sources, strict=True)
        ],
        "horizon": sources,
    }
    for name, values in strings.items():
        attr = mesh.attributes.new(name, "STRING", "EDGE")
        for item, value in zip(attr.data, values, strict=True):
            item.value = value.encode("utf-8")  # foreach_set refuses STRING attributes

    if old is not None and old.type == "MESH":
        old.data = mesh
        obj = old
        if old_mesh is not None and old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
    else:
        obj = bpy.data.objects.new(SIGHTLINES_NAME, mesh)
        collection = bpy.data.collections.get(SIGHTLINES_NAME)
        if collection is None:
            collection = bpy.data.collections.new(SIGHTLINES_NAME)
            context.scene.collection.children.link(collection)
        collection.objects.link(obj)
    props.sightlines_result = json.dumps(
        {"object": obj.name, "lines": n, "epoch": summary.get("epoch", "")}
    )
    return obj


# --- alignment check -----------------------------------------------------------------------------


def check_alignment(context: bpy.types.Context, obj: bpy.types.Object) -> dict:
    """The selected edges of ``obj`` (all edges when none are) against the events; stored."""
    props = settings(context)
    events, summary = load_events(context)
    if not events:
        raise ArchaeoError("Compute the events first.")
    if obj.mode == "EDIT":
        obj.update_from_editmode()
    mesh = obj.data
    n_edges = len(mesh.edges)
    if not n_edges:
        raise ArchaeoError(f"{obj.name} has no edges.")
    selected = np.zeros(n_edges, dtype=bool)
    mesh.edges.foreach_get("select", selected)
    notes = []
    index = np.flatnonzero(selected)
    if not len(index):
        index = np.arange(n_edges)
        notes.append(f"No edges of {obj.name} are selected, so all {n_edges:,} were checked.")
    pairs = np.empty(n_edges * 2, dtype=np.int32)
    mesh.edges.foreach_get("vertices", pairs)
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = co.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
    pairs = pairs.reshape(-1, 2)[index]
    placement = analysis._placement(context)
    if PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS and not site.available():
        raise site.GeodesyUnavailable(analysis.NEEDS_PYPROJ)
    azimuth, latitude = site.line_bearings(
        placement, world[pairs[:, 0]], world[pairs[:, 1]], local_lon_lat=_lon_lat(context)
    )
    keep = np.isfinite(azimuth)
    if not keep.all():
        notes.append(f"{int((~keep).sum()):,} edges have no length in plan and were skipped.")
    fids = None
    if "fid" in mesh.attributes and mesh.attributes["fid"].domain == "EDGE":
        values = np.empty(n_edges, dtype=np.int32)
        mesh.attributes["fid"].data.foreach_get("value", values)
        fids = values[index][keep]
    rows = core.alignments(
        azimuth[keep],
        latitude[keep],
        events,
        load_profile(context),
        refract=props.refraction,
        indices=index[keep],
        # The events' refraction: the pressure at their site's height.
        pressure_hpa=float(summary.get("pressure_hpa", horizon.REFRACTION_PRESSURE_HPA)),
    )
    if len(rows) > MAX_ALIGNMENT_ROWS:
        notes.append(f"Only the first {MAX_ALIGNMENT_ROWS:,} of {len(rows):,} lines are kept.")
        rows = rows[:MAX_ALIGNMENT_ROWS]
    result = {
        "object": obj.name,
        "epoch": summary.get("epoch", ""),
        "profile": summary.get("profile", False),
        "rows": [
            {
                "edge": r.index,
                "fid": int(fids[k]) if fids is not None else None,
                "azimuth": r.azimuth,
                "back_azimuth": r.back_azimuth,
                "altitude": r.altitude,
                "back_altitude": r.back_altitude,
                "declination": r.declination,
                "back_declination": r.back_declination,
                "nearest": [list(item) for item in r.nearest],
            }
            for k, r in enumerate(rows)
        ],
        "notes": notes,
    }
    props.alignment = json.dumps(result)
    props.alignment_error = ""
    return result


def alignment_text(result: dict) -> str:
    lines = [
        (
            "Edge\tfid\tAzimuth (deg true)\tBack azimuth\tAltitude\tDeclination (deg)\t"
            "Back altitude\tBack declination (deg)\tNearest event\tEvent azimuth\tDifference (deg)"
        )
    ]
    for r in result["rows"]:
        first = r["nearest"][0] if r["nearest"] else ["", math.nan, math.nan]
        lines.append(
            "\t".join(
                str(v)
                for v in (
                    r["edge"],
                    "" if r["fid"] is None else r["fid"],
                    repr(r["azimuth"]),
                    repr(r["back_azimuth"]),
                    repr(r["altitude"]),
                    repr(r["declination"]),
                    repr(r["back_altitude"]),
                    repr(r["back_declination"]),
                    first[0],
                    repr(first[1]),
                    repr(first[2]),
                )
            )
        )
    return "\n".join(lines)


# --- operators -------------------------------------------------------------------------------------

_ERRORS = (
    ArchaeoError,
    site.GeodesyUnavailable,
    site.SiteError,
    sun.TimeZoneUnavailable,
    FieldError,
    ValueError,
)


def _run(operator, context, work, error_attr: str) -> set[str]:
    props = settings(context)
    try:
        message = work()
    except _ERRORS as exc:
        setattr(props, error_attr, str(exc))
        operator.report({"ERROR"}, str(exc))
        redraw_ui(context)
        return {"CANCELLED"}
    setattr(props, error_attr, "")
    if message:
        operator.report({"INFO"}, message)
    redraw_ui(context)
    return {"FINISHED"}


class GEOTOOLS_OT_archaeo_observer_cursor(bpy.types.Operator):
    """Set the observer at the 3D cursor (snap it to a surface with Shift+Right Click)"""

    bl_idname = "geotools.archaeo_observer_cursor"
    bl_label = "Observer from 3D Cursor"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        set_observer(context, tuple(context.scene.cursor.location))
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_archaeo_observer_pick(bpy.types.Operator):
    """Click a surface in the viewport to set the observer there"""

    bl_idname = "geotools.archaeo_observer_pick"
    bl_label = "Pick Observer"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context: bpy.types.Context, _event) -> set[str]:
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set("EYEDROPPER")
        context.workspace.status_text_set("Click a surface for the observer; Esc to cancel")
        return {"RUNNING_MODAL"}

    def _finish(self, context: bpy.types.Context) -> None:
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)

    def modal(self, context: bpy.types.Context, event) -> set[str]:
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"CANCELLED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            hit = analysis._ray_cast(context, event)
            if hit is None:
                return {"RUNNING_MODAL"}
            self._finish(context)
            context.scene.cursor.location = hit
            set_observer(context, tuple(hit))
            redraw_ui(context)
            return {"FINISHED"}
        return (
            {"PASS_THROUGH"}
            if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}
            else {"RUNNING_MODAL"}
        )


class GEOTOOLS_OT_archaeo_horizon(bpy.types.Operator):
    """Cast rays from the observer's eye at every azimuth to find the horizon's altitude (Cycles)"""

    bl_idname = "geotools.archaeo_horizon"
    bl_label = "Compute Horizon"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        def work() -> str:
            profile = compute_horizon(context)
            peak = int(np.argmax(profile.elevation))
            return (
                f"Horizon of {len(profile.azimuth)} azimuths in "
                f"{profile.info['seconds']:.1f} s; highest {profile.elevation[peak]:.2f}° at "
                f"{profile.azimuth[peak]:.1f}°"
            )

        try:
            return _run(self, context, work, "profile_error")
        except RuntimeError as exc:  # a bake error outside ArchaeoError
            settings(context).profile_error = str(exc)
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class GEOTOOLS_OT_archaeo_clear_horizon(bpy.types.Operator):
    """Forget the horizon profile (events then use the astronomical horizon only)"""

    bl_idname = "geotools.archaeo_clear_horizon"
    bl_label = "Clear Horizon"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = settings(context)
        props.profile = ""
        props.profile_error = ""
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_archaeo_events(bpy.types.Operator):
    """Sunrise and sunset at the solstices and equinoxes and the lunar standstill rises and sets, at the epoch, on the astronomical and the real horizon"""

    bl_idname = "geotools.archaeo_events"
    bl_label = "Compute Events"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        def work() -> str:
            events, summary = compute_events(context)
            return f"{len(events)} events for {summary['epoch']}"

        return _run(self, context, work, "events_error")


class GEOTOOLS_OT_archaeo_sightlines(bpy.types.Operator):
    """Lines from the observer's eye towards each event, to the horizon or the chosen length, as a mesh to export or publish"""

    bl_idname = "geotools.archaeo_sightlines"
    bl_label = "Create Sightlines"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.mode == "OBJECT"

    def execute(self, context: bpy.types.Context) -> set[str]:
        def work() -> str:
            obj = make_sightlines(context)
            return f"{len(obj.data.edges)} sightlines in {obj.name}"

        return _run(self, context, work, "events_error")


class GEOTOOLS_OT_archaeo_alignment(bpy.types.Operator):
    """True azimuth of each selected edge of the active mesh, the nearest events and the declination it points to"""

    bl_idname = "geotools.archaeo_alignment"
    bl_label = "Check Alignment"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            cls.poll_message_set("Make a mesh with the line features active")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.active_object

        def work() -> str:
            result = check_alignment(context, obj)
            for note in result["notes"]:
                self.report({"WARNING"}, note)
            return f"{len(result['rows'])} lines checked"

        return _run(self, context, work, "alignment_error")


COPY_ITEMS = (
    ("EVENTS", "Events", "Copy the events table"),
    ("PROFILE", "Horizon", "Copy the horizon profile"),
    ("ALIGNMENT", "Alignment", "Copy the alignment check"),
)


def copy_text(context: bpy.types.Context, what: str) -> str:
    props = settings(context)
    if what == "EVENTS":
        events, summary = load_events(context)
        return events_text(events, summary) if events else ""
    if what == "PROFILE":
        profile = load_profile(context)
        return profile.as_text() if profile is not None else ""
    return alignment_text(json.loads(props.alignment)) if props.alignment else ""


class GEOTOOLS_OT_archaeo_copy(bpy.types.Operator):
    """Copy a result to the clipboard at full precision, as tab-separated lines"""

    bl_idname = "geotools.archaeo_copy"
    bl_label = "Copy"
    bl_options = {"REGISTER"}

    what: EnumProperty(
        name="What", description="The result to copy", items=COPY_ITEMS, default="EVENTS"
    )

    def execute(self, context: bpy.types.Context) -> set[str]:
        text = copy_text(context, self.what)
        if not text:
            self.report({"WARNING"}, "Nothing to copy yet.")
            return {"CANCELLED"}
        context.window_manager.clipboard = text
        self.report({"INFO"}, "Copied to the clipboard.")
        return {"FINISHED"}


# --- sun hours: the profile as the far horizon ------------------------------------------------------


def sun_hours_horizon(context: bpy.types.Context):
    """The profile for sun hours as (azimuths, elevations), or None; raises when asked for but missing."""
    if not analysis.settings(context).sun_hours.horizon_profile:
        return None
    profile = load_profile(context)
    if profile is None:
        raise ArchaeoError(
            "Use Horizon Profile is on, but there is no profile: compute one under "
            "Archaeoastronomy, or turn it off."
        )
    return profile


classes = (
    GeoToolsArchaeoSettings,
    GEOTOOLS_OT_archaeo_observer_cursor,
    GEOTOOLS_OT_archaeo_observer_pick,
    GEOTOOLS_OT_archaeo_horizon,
    GEOTOOLS_OT_archaeo_clear_horizon,
    GEOTOOLS_OT_archaeo_events,
    GEOTOOLS_OT_archaeo_sightlines,
    GEOTOOLS_OT_archaeo_alignment,
    GEOTOOLS_OT_archaeo_copy,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    analysis.GeoToolsAnalysis.archaeo = bpy.props.PointerProperty(type=GeoToolsArchaeoSettings)


def unregister() -> None:
    if hasattr(analysis.GeoToolsAnalysis, "archaeo"):
        del analysis.GeoToolsAnalysis.archaeo
    _unregister_classes()
