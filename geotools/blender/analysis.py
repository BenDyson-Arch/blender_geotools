"""M5 analysis tools that need the georeference: the sun, and coordinates and measurement.

The maths is bpy-free (``geotools.analysis.sun`` and ``geotools.analysis.site``);
this module stores the settings and results and drives Blender:

* **Sun at a moment** (``geotools.sun_place``) and **over a day**
  (``geotools.sun_animate``): NREL SPA from the project's site, date, time and
  time zone, turned from true north to the scene's +Y with the grid
  convergence, placed on (or keyframed into) a Blender Sun light. Blender
  renders and bakes the shadows.
* **Coordinates** (``geotools.point_from_cursor``, ``geotools.point_pick``):
  E, N, RL and latitude and longitude of a point, at full binary64 precision.
* **Measurement**: grid and ground distance between points A and B, with the
  combined scale factor, and the ground area of the selected faces
  (``geotools.measure_area``). ``geotools.copy_reading`` copies any result.

Settings and results live on ``scene.geotools_analysis``. Every geodetic number
is stored as binary64 text (JSON of Python floats, or ``repr``), never a
``FloatProperty``. Panels only read what the operators stored; the panels are
in ``analysis_ui``.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from dataclasses import asdict

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..analysis import site, sun
from ..core.placement import Placement, PlacementMode
from .common import redraw_ui
from .properties import FieldError, format_vec, parse_vec, placement_from_props, read_number

log = logging.getLogger(__name__)

SUN_OBJECT_NAME = "GeoTools Sun"
SUN_ACTION_PREFIX = "GeoTools Sun"

TZ_MODE_ITEMS = (
    ("ZONE", "Time Zone", "An IANA time zone such as Australia/Brisbane, with daylight saving"),
    ("OFFSET", "UTC Offset", "A fixed offset from UTC in hours, with no daylight saving"),
)

SITE_ITEMS = (
    ("ORIGIN", "Scene Origin", "The sun as seen from the scene origin (the project offset)"),
    ("CURSOR", "3D Cursor", "The sun as seen from the 3D cursor"),
)

TZ_NO_DATABASE_NOTE = (
    "This Python has no time zone database (usual for Blender on Windows), so named zones "
    "other than UTC cannot be used. Use a UTC offset; it has no daylight saving."
)

NEEDS_PYPROJ = (
    "Needs pyproj, which is not available (see About): a CRS project cannot be related "
    "to the Earth without it."
)


# --- properties --------------------------------------------------------------------------


def _is_sun(_self, obj: bpy.types.Object) -> bool:
    return obj.type == "LIGHT" and obj.data is not None and obj.data.type == "SUN"


class GeoToolsSunSettings(bpy.types.PropertyGroup):
    """Settings and last result of the sun tool (``scene.geotools_analysis.sun``)."""

    year: IntProperty(name="Year", default=2026, min=1, max=9999)
    month: IntProperty(name="Month", default=6, min=1, max=12)
    day: IntProperty(name="Day", default=21, min=1, max=31)
    hour: IntProperty(name="Hour", default=12, min=0, max=23)
    minute: IntProperty(name="Minute", default=0, min=0, max=59)
    second: IntProperty(name="Second", default=0, min=0, max=59)
    tz_mode: EnumProperty(name="Time", items=TZ_MODE_ITEMS, default="ZONE")
    tz_name: StringProperty(
        name="Time Zone",
        description='IANA time zone name, e.g. "Australia/Brisbane" or "Europe/London"; '
        '"UTC" always works',
        default="UTC",
    )
    utc_offset: FloatProperty(
        name="UTC Offset",
        description="Hours ahead of UTC, e.g. 10 for AEST or -7 for MST (rounded to the minute)",
        default=0.0,
        min=-23.75,
        max=23.75,
        step=25,
        precision=2,
    )
    site: EnumProperty(name="Site", items=SITE_ITEMS, default="ORIGIN")
    refraction: BoolProperty(
        name="Refraction",
        description="Raise the sun by atmospheric refraction (standard pressure and "
        "temperature), as it is seen",
        default=True,
    )
    strength: FloatProperty(
        name="Strength",
        description="Sun light strength while the sun is up; 0 while it is below the horizon",
        default=1.0,
        min=0.0,
        soft_max=10.0,
    )
    light: PointerProperty(
        name="Light",
        type=bpy.types.Object,
        poll=_is_sun,
        description="Sun light to place; empty to create (or reuse) one named GeoTools Sun",
    )
    # Over a day (geotools.sun_animate)
    start_hour: FloatProperty(
        name="From",
        description="Local time of the first keyframe, hours",
        default=5.0,
        min=0.0,
        max=24.0,
    )
    end_hour: FloatProperty(
        name="To",
        description="Local time of the last keyframe, hours",
        default=19.0,
        min=0.0,
        max=24.0,
    )
    step_minutes: IntProperty(
        name="Step", description="Minutes between keyframes", default=15, min=1, max=720
    )
    frame_start: IntProperty(name="First Frame", default=1, min=-1_048_574, max=1_048_574)
    frames_per_step: IntProperty(name="Frames per Step", default=1, min=1, max=1000)

    # Last result (written by the operators). JSON of binary64 floats.
    result: StringProperty(default="")
    error: StringProperty(default="")


PERIOD_ITEMS = (
    ("DAY", "Day", "The date set above"),
    ("RANGE", "Dates", "From the date set above to an end date, inclusive (a season)"),
    ("YEAR", "Year", "The whole calendar year of the date set above"),
)

SUN_HOURS_DOMAIN_ITEMS = (
    ("FACE", "Face", "One value per face, at its centre"),
    ("CORNER", "Corner", "One value per face corner, just inside the face"),
    ("TEXTURE", "Texture", "One value per texel of an image on the source UVs"),
)


class GeoToolsSunHoursSettings(bpy.types.PropertyGroup):
    """Settings and last result of sun hours and insolation (``.sun_hours``).

    The date, time zone, site and refraction are the Sun panel's.
    """

    period: EnumProperty(name="Period", items=PERIOD_ITEMS, default="DAY")
    end_year: IntProperty(name="End Year", default=2026, min=1, max=9999)
    end_month: IntProperty(name="End Month", default=8, min=1, max=12)
    end_day: IntProperty(name="End Day", default=31, min=1, max=31)
    step_minutes: IntProperty(
        name="Step",
        description="Minutes between sun positions; each counts for its whole step",
        default=15,
        min=1,
        max=720,
    )
    domain: EnumProperty(name="Values", items=SUN_HOURS_DOMAIN_ITEMS, default="FACE")
    hours_layer: StringProperty(
        name="Layer",
        description="Layer that receives the hours of direct sun (created if missing)",
        default="sun_hours",
    )
    insolation: BoolProperty(
        name="Insolation",
        description="Also compute clear-sky irradiation in kWh/m² (Ineichen-Perez model)",
        default=False,
    )
    insolation_layer: StringProperty(
        name="Layer",
        description="Layer that receives the clear-sky irradiation (created if missing)",
        default="insolation",
    )
    linke_turbidity: FloatProperty(
        name="Linke Turbidity",
        description="Haziness of the clear sky: 2 very clean, 3 clean rural, 4-5 humid or "
        "hazy, 6-7 polluted",
        default=3.0,
        min=1.0,
        max=10.0,
        step=10,
    )
    diffuse: BoolProperty(
        name="Diffuse Sky",
        description="Add the isotropic diffuse sky, seen through the sky-view of each sample",
        default=True,
    )
    diffuse_rays: IntProperty(
        name="Sky Rays",
        description="Rays per sample spread over the sky for the diffuse term",
        default=64,
        min=8,
        max=1024,
    )
    tolerance: FloatProperty(
        name="Merge Within",
        description="Sun directions closer than this share one ray (degrees). 0 casts one "
        "ray per step; larger is faster with softer shadow edges",
        default=2.0,
        min=0.0,
        max=10.0,
        step=25,
        precision=2,
    )
    horizon: FloatProperty(
        name="Horizon",
        description="Elevation of the far horizon in every direction (degrees); terrain "
        "beyond the scene is not otherwise known",
        default=0.0,
        min=0.0,
        max=60.0,
    )
    offset: FloatProperty(
        name="Ray Offset",
        description="How far rays start off the surface, in scene units; 0 for automatic",
        default=0.0,
        min=0.0,
        soft_max=0.1,
        precision=4,
    )
    resolution: IntProperty(
        name="Resolution",
        description="Image size of a new texture layer (an existing layer keeps its own)",
        default=1024,
        min=16,
        max=16384,
    )
    result: StringProperty(default="")
    error: StringProperty(default="")


class GeoToolsShadowStudySettings(bpy.types.PropertyGroup):
    """Solstice and equinox shadow-study frames (``.shadow_study``)."""

    march: BoolProperty(name="March Equinox", default=True)
    june: BoolProperty(name="June Solstice", default=True)
    september: BoolProperty(name="September Equinox", default=False)
    december: BoolProperty(name="December Solstice", default=True)
    start_hour: FloatProperty(
        name="From",
        description="Local time of the first frame each day",
        default=9.0,
        min=0.0,
        max=24.0,
    )
    end_hour: FloatProperty(
        name="To",
        description="Local time of the last frame each day",
        default=15.0,
        min=0.0,
        max=24.0,
    )
    step_hours: FloatProperty(
        name="Every", description="Hours between frames", default=3.0, min=0.25, max=24.0
    )
    result: StringProperty(default="")
    error: StringProperty(default="")


class GeoToolsMeasureSettings(bpy.types.PropertyGroup):
    """Points A and B and the last readings (``scene.geotools_analysis.measure``)."""

    point_a: StringProperty(default="")  # format_vec of the float32 world position
    point_b: StringProperty(default="")
    reading_a: StringProperty(default="")  # JSON of a site.PointReading
    reading_b: StringProperty(default="")
    distance: StringProperty(default="")  # JSON of a site.Measurement
    area: StringProperty(default="")  # JSON of a site.AreaReading
    area_object: StringProperty(default="")
    key: StringProperty(default="")  # repr of the Placement the readings were made with
    error: StringProperty(default="")
    note: StringProperty(default="")


class GeoToolsAnalysis(bpy.types.PropertyGroup):
    """The M5 analysis tools of a scene."""

    sun: PointerProperty(type=GeoToolsSunSettings)
    measure: PointerProperty(type=GeoToolsMeasureSettings)
    sun_hours: PointerProperty(type=GeoToolsSunHoursSettings)
    shadow_study: PointerProperty(type=GeoToolsShadowStudySettings)


# --- shared --------------------------------------------------------------------------------


def settings(context: bpy.types.Context) -> GeoToolsAnalysis:
    return context.scene.geotools_analysis


def _placement(context: bpy.types.Context) -> Placement:
    return placement_from_props(context.scene.geotools)


def placement_key(context: bpy.types.Context) -> str:
    """What a reading depends on; empty if the project's numbers do not parse."""
    try:
        return repr(_placement(context))
    except FieldError:
        return ""


def needs_pyproj(placement: Placement, *, geodetic: bool = True) -> bool:
    """True if this placement needs pyproj (SOURCE_CRS, or ANCHOR latitude and longitude)."""
    mode = PlacementMode(placement.mode)
    return mode is PlacementMode.SOURCE_CRS or (geodetic and mode is PlacementMode.ANCHOR)


def _dumps(value) -> str:
    data = asdict(value)
    for key, item in data.items():
        if isinstance(item, PlacementMode):
            data[key] = item.value
    return json.dumps(data)


def load_reading(text: str) -> site.PointReading | None:
    if not text:
        return None
    data = json.loads(text)
    data["mode"] = PlacementMode(data["mode"])
    data["local"] = tuple(data["local"])
    return site.PointReading(**data)


def load_measurement(text: str) -> site.Measurement | None:
    return site.Measurement(**json.loads(text)) if text else None


def load_area(text: str) -> site.AreaReading | None:
    return site.AreaReading(**json.loads(text)) if text else None


def load_sun(text: str) -> dict | None:
    return json.loads(text) if text else None


# --- sun -----------------------------------------------------------------------------------


def _time_zone(props: GeoToolsSunSettings) -> tuple[dt.tzinfo, str]:
    """The chosen time zone and its label; raises sun.TimeZoneUnavailable or ValueError."""
    if props.tz_mode == "OFFSET":
        tz = sun.fixed_offset(props.utc_offset)
        minutes = round(props.utc_offset * 60)
        sign = "+" if minutes >= 0 else "-"
        return tz, f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
    name = props.tz_name.strip() or "UTC"
    return sun.zone(name), name


def _local_moment(props: GeoToolsSunSettings) -> dt.datetime:
    try:
        return dt.datetime(
            props.year, props.month, props.day, props.hour, props.minute, props.second
        )
    except ValueError as exc:
        raise ValueError(
            f"Not a date: {props.year}-{props.month:02d}-{props.day:02d} ({exc})."
        ) from None


def _anchor_lon_lat(context: bpy.types.Context) -> tuple[float, float]:
    props = context.scene.geotools
    return read_number(props, "anchor_lon"), read_number(props, "anchor_lat")


def compute_site(context: bpy.types.Context, sun_props: GeoToolsSunSettings) -> site.SunSite:
    """The sun's site for the scene; raises site.GeodesyUnavailable, site.SiteError, FieldError."""
    placement = _placement(context)
    local = tuple(context.scene.cursor.location) if sun_props.site == "CURSOR" else (0.0, 0.0, 0.0)
    if PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS and not site.available():
        raise site.GeodesyUnavailable(NEEDS_PYPROJ)
    lon_lat = (
        _anchor_lon_lat(context) if PlacementMode(placement.mode) is PlacementMode.LOCAL else None
    )
    return site.sun_site(placement, local, local_lon_lat=lon_lat)


def ensure_sun_light(
    context: bpy.types.Context, sun_props: GeoToolsSunSettings
) -> bpy.types.Object:
    """The light to drive: the chosen one, else GeoTools Sun (created if missing)."""
    obj = sun_props.light
    if obj is not None and _is_sun(None, obj):
        return obj
    obj = bpy.data.objects.get(SUN_OBJECT_NAME)
    if obj is None or not _is_sun(None, obj):
        light = bpy.data.lights.new(SUN_OBJECT_NAME, "SUN")
        light.angle = math.radians(0.533)  # the sun's mean apparent diameter
        obj = bpy.data.objects.new(SUN_OBJECT_NAME, light)
        context.scene.collection.objects.link(obj)
    sun_props.light = obj
    return obj


def _sun_result(
    position: sun.SunPosition,
    index: int,
    where: site.SunSite,
    utc: dt.datetime,
    zone_label: str,
    notes: tuple[str, ...],
    **extra,
) -> str:
    azimuth = float(position.azimuth[index])
    return json.dumps(
        {
            "utc": utc.isoformat(),
            "zone": zone_label,
            "azimuth": azimuth,
            "elevation": float(position.elevation[index]),
            "convergence": where.convergence,
            "scene_azimuth": (azimuth - where.convergence) % 360.0,
            "north": where.north,
            "longitude": where.longitude,
            "latitude": where.latitude,
            "geodetic_crs": where.geodetic_crs,
            "notes": list(notes),
            **extra,
        }
    )


def _prepare(
    context: bpy.types.Context,
) -> tuple[GeoToolsSunSettings, site.SunSite, dt.tzinfo, str]:
    props = settings(context).sun
    tz, label = _time_zone(props)
    where = compute_site(context, props)
    return props, where, tz, label


def place_sun(context: bpy.types.Context) -> dict:
    """Place the sun light for the moment in the settings; returns the stored result."""
    props, where, tz, label = _prepare(context)
    utc, notes = sun.to_utc(_local_moment(props), tz)
    position = sun.solar_position(
        utc.timestamp(), where.latitude, where.longitude, where.height, refraction=props.refraction
    )
    scene_azimuth = (position.azimuth - where.convergence) % 360.0
    euler = sun.light_euler(scene_azimuth, position.elevation)[0]
    obj = ensure_sun_light(context, props)
    _clear_sun_animation(obj)
    obj.rotation_mode = "XYZ"
    obj.rotation_euler = tuple(float(v) for v in euler)
    up = bool(position.elevation[0] > 0.0)
    obj.data.energy = props.strength if up else 0.0
    if not up:
        notes = (*notes, "The sun is below the horizon, so the light is off.")
    props.result = _sun_result(position, 0, where, utc, label, notes, light=obj.name)
    props.error = ""
    return json.loads(props.result)


def animate_sun(context: bpy.types.Context) -> dict:
    """Keyframe the sun light over the day in the settings, at the chosen step."""
    props, where, tz, label = _prepare(context)
    if props.end_hour <= props.start_hour:
        raise ValueError("The day's end must be after its start.")
    day = _local_moment(props).replace(hour=0, minute=0, second=0)
    start_local = day + dt.timedelta(hours=props.start_hour)
    end_local = day + dt.timedelta(hours=props.end_hour)
    start_utc, notes = sun.to_utc(start_local, tz)
    end_utc, end_notes = sun.to_utc(end_local, tz)
    step = props.step_minutes * 60.0
    count = math.floor((end_utc - start_utc).total_seconds() / step + 1e-9) + 1
    if count > 100_000:
        raise ValueError(f"{count} keyframes is too many; use a longer step.")
    times = sun.local_times(start_utc, count, step)
    position = sun.solar_position(
        times, where.latitude, where.longitude, where.height, refraction=props.refraction
    )
    scene_azimuth = (position.azimuth - where.convergence) % 360.0
    euler = sun.light_euler(scene_azimuth, position.elevation)
    energy = np.where(position.elevation > 0.0, props.strength, 0.0)
    frames = props.frame_start + props.frames_per_step * np.arange(count, dtype=np.float64)

    obj = ensure_sun_light(context, props)
    _clear_sun_animation(obj)
    obj.rotation_mode = "XYZ"
    _keyframe(obj, "rotation_euler", frames, euler.T, f"{SUN_ACTION_PREFIX} Rotation")
    _keyframe(obj.data, "energy", frames, energy[None, :], f"{SUN_ACTION_PREFIX} Strength")
    context.scene.frame_set(context.scene.frame_current)

    current = _frame_index(
        context.scene.frame_current, props.frame_start, props.frames_per_step, count
    )
    first, last = int(frames[0]), int(frames[-1])
    notes = (*notes, *end_notes)
    props.result = _sun_result(
        position,
        current,
        where,
        dt.datetime.fromtimestamp(float(times[current]), dt.UTC),
        label,
        notes,
        light=obj.name,
        frames=[first, last],
        steps=count,
        frame=props.frame_start + props.frames_per_step * current,
    )
    props.error = ""
    return json.loads(props.result)


def _frame_index(frame: int, start: int, per_step: int, count: int) -> int:
    return int(min(max(round((frame - start) / per_step), 0), count - 1))


def _clear_sun_animation(obj: bpy.types.Object) -> None:
    """Drop GeoTools' sun keyframes from the light (other animation is the user's)."""
    for block in (obj, obj.data):
        animation = block.animation_data
        if animation is None or animation.action is None:
            continue
        action = animation.action
        if action.name.startswith(SUN_ACTION_PREFIX):
            animation.action = None
            if action.users == 0:
                bpy.data.actions.remove(action)


def _keyframe(
    block, path: str, frames: np.ndarray, values: np.ndarray, name: str, interpolation: int = -1
) -> None:
    """Keyframes for each channel of ``path`` at once (``values`` is (channels, n)).

    Linear unless ``interpolation`` (a ``Keyframe.interpolation`` enum value) is given.
    """
    if interpolation < 0:
        interpolation = _LINEAR
    animation = block.animation_data_create()
    action = bpy.data.actions.new(name)
    animation.action = action
    for index, channel in enumerate(values):
        fcurve = action.fcurve_ensure_for_datablock(block, path, index=index)
        points = fcurve.keyframe_points
        points.add(len(frames))
        co = np.empty(2 * len(frames), dtype=np.float32)
        co[0::2] = frames
        co[1::2] = channel
        points.foreach_set("co", co)
        points.foreach_set("interpolation", np.full(len(frames), interpolation, dtype=np.int32))
        fcurve.update()


_LINEAR = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
CONSTANT = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["CONSTANT"].value


class GEOTOOLS_OT_sun_place(bpy.types.Operator):
    """Point a Sun light at the sun for the date, time and time zone, from the project's site"""

    bl_idname = "geotools.sun_place"
    bl_label = "Place Sun"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _run_sun(self, context, place_sun)


class GEOTOOLS_OT_sun_animate(bpy.types.Operator):
    """Keyframe a Sun light over the day from the start to the end time, one key per step"""

    bl_idname = "geotools.sun_animate"
    bl_label = "Animate Day"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _run_sun(self, context, animate_sun)


def _run_sun(operator: bpy.types.Operator, context: bpy.types.Context, action) -> set[str]:
    props = settings(context).sun
    try:
        result = action(context)
    except (
        site.GeodesyUnavailable,
        site.SiteError,
        sun.TimeZoneUnavailable,
        FieldError,
        ValueError,
    ) as exc:
        props.error = str(exc)
        props.result = ""
        operator.report({"ERROR"}, str(exc))
        redraw_ui(context)
        return {"CANCELLED"}
    for note in result["notes"]:
        operator.report({"WARNING"}, note)
    text = f"Sun azimuth {result['azimuth']:.2f}°, elevation {result['elevation']:.2f}°"
    if "steps" in result:
        text = (
            f"Keyed {result['steps']} steps on frames {result['frames'][0]}-{result['frames'][1]}"
        )
    operator.report({"INFO"}, text)
    redraw_ui(context)
    return {"FINISHED"}


# --- coordinates and measurement -------------------------------------------------------------


def _read(context: bpy.types.Context, local) -> tuple[site.PointReading, str]:
    """A point's reading, and a note when part of it needs pyproj."""
    placement = _placement(context)
    mode = PlacementMode(placement.mode)
    if mode is PlacementMode.SOURCE_CRS and not site.available():
        raise site.GeodesyUnavailable(NEEDS_PYPROJ)
    if mode is PlacementMode.ANCHOR and not site.available():
        note = "Latitude and longitude need pyproj (see About)."
        return site.read_point(placement, local, geodetic=False), note
    return site.read_point(placement, local), ""


def set_point(context: bpy.types.Context, which: str, location) -> site.PointReading:
    """Store point A or B from a Blender world position and refresh the readings."""
    props = settings(context).measure
    reading, note = _read(context, location)
    setattr(props, f"point_{which.lower()}", format_vec(reading.local))
    setattr(props, f"reading_{which.lower()}", _dumps(reading))
    props.key = placement_key(context)
    props.note = note
    props.error = ""
    _refresh_distance(context)
    return reading


def _refresh_distance(context: bpy.types.Context) -> None:
    props = settings(context).measure
    a, b = parse_vec(props.point_a), parse_vec(props.point_b)
    if a is None or b is None:
        props.distance = ""
        return
    props.distance = _dumps(site.measure(_placement(context), a, b))


def refresh_readings(context: bpy.types.Context) -> None:
    """Read A and B again with the current project (after the project changed)."""
    props = settings(context).measure
    for which in ("a", "b"):
        point = parse_vec(getattr(props, f"point_{which}"))
        if point is not None:
            set_point(context, which, point)


def _measure_errors(operator: bpy.types.Operator, context: bpy.types.Context, work) -> set[str]:
    props = settings(context).measure
    try:
        work()
    except (site.GeodesyUnavailable, site.SiteError, FieldError) as exc:
        props.error = str(exc)
        operator.report({"ERROR"}, str(exc))
        redraw_ui(context)
        return {"CANCELLED"}
    redraw_ui(context)
    return {"FINISHED"}


POINT_ITEMS = (("A", "A", "Point A"), ("B", "B", "Point B"))


class GEOTOOLS_OT_point_from_cursor(bpy.types.Operator):
    """Read the 3D cursor's position on Earth as point A or B (snap the cursor to a surface with Shift+Right Click)"""

    bl_idname = "geotools.point_from_cursor"
    bl_label = "From 3D Cursor"
    bl_options = {"REGISTER"}

    point: EnumProperty(name="Point", items=POINT_ITEMS, default="A")

    def execute(self, context: bpy.types.Context) -> set[str]:
        location = tuple(context.scene.cursor.location)
        return _measure_errors(self, context, lambda: set_point(context, self.point, location))


class GEOTOOLS_OT_point_pick(bpy.types.Operator):
    """Click a surface in the viewport to read its position on Earth as point A or B"""

    bl_idname = "geotools.point_pick"
    bl_label = "Pick Point"
    bl_options = {"REGISTER"}

    point: EnumProperty(name="Point", items=POINT_ITEMS, default="A")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.area is not None and context.area.type == "VIEW_3D"

    def invoke(self, context: bpy.types.Context, _event) -> set[str]:
        context.window_manager.modal_handler_add(self)
        context.window.cursor_modal_set("EYEDROPPER")
        context.workspace.status_text_set(f"Click a surface for point {self.point}; Esc to cancel")
        return {"RUNNING_MODAL"}

    def _finish(self, context: bpy.types.Context) -> None:
        context.window.cursor_modal_restore()
        context.workspace.status_text_set(None)

    def modal(self, context: bpy.types.Context, event) -> set[str]:
        if event.type in {"ESC", "RIGHTMOUSE"} and event.value == "PRESS":
            self._finish(context)
            return {"CANCELLED"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            hit = _ray_cast(context, event)
            if hit is None:
                return {"RUNNING_MODAL"}  # missed: keep picking
            self._finish(context)
            context.scene.cursor.location = hit
            return _measure_errors(
                self, context, lambda: set_point(context, self.point, tuple(hit))
            )
        return (
            {"PASS_THROUGH"}
            if event.type in {"MIDDLEMOUSE", "WHEELUPMOUSE", "WHEELDOWNMOUSE"}
            else {"RUNNING_MODAL"}
        )


def _ray_cast(context: bpy.types.Context, event):
    """The first surface under the mouse, in world coordinates, or None."""
    from bpy_extras import view3d_utils

    region = context.region
    region_3d = context.space_data.region_3d if context.space_data is not None else None
    if region is None or region_3d is None:
        return None
    mouse = (event.mouse_region_x, event.mouse_region_y)
    origin = view3d_utils.region_2d_to_origin_3d(region, region_3d, mouse)
    direction = view3d_utils.region_2d_to_vector_3d(region, region_3d, mouse)
    depsgraph = context.evaluated_depsgraph_get()
    found, location, *_rest = context.scene.ray_cast(depsgraph, origin, direction)
    return location if found else None


class GEOTOOLS_OT_measure_refresh(bpy.types.Operator):
    """Read points A and B again with the current project settings"""

    bl_idname = "geotools.measure_refresh"
    bl_label = "Read Again"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _measure_errors(self, context, lambda: refresh_readings(context))


class GEOTOOLS_OT_measure_clear(bpy.types.Operator):
    """Forget points A and B and the area"""

    bl_idname = "geotools.measure_clear"
    bl_label = "Clear"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = settings(context).measure
        for name in (
            "point_a",
            "point_b",
            "reading_a",
            "reading_b",
            "distance",
            "area",
            "area_object",
            "key",
            "error",
            "note",
        ):
            props.property_unset(name)
        redraw_ui(context)
        return {"FINISHED"}


def selected_triangles(obj: bpy.types.Object) -> np.ndarray:
    """The selected faces of a mesh object as world-space triangles (T, 3, 3), float64.

    Uses the mesh's loop triangles and polygon selection with ``foreach_get``,
    so there is no Python loop over faces. In Edit Mode the edit mesh is
    written back first, so the selection is current.
    """
    if obj.mode == "EDIT":
        obj.update_from_editmode()
    mesh = obj.data
    mesh.calc_loop_triangles()
    n_polys = len(mesh.polygons)
    selected = np.zeros(n_polys, dtype=bool)
    mesh.polygons.foreach_get("select", selected)
    n_tris = len(mesh.loop_triangles)
    tri_poly = np.empty(n_tris, dtype=np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", tri_poly)
    tri_verts = np.empty(n_tris * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", tri_verts)
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3).astype(np.float64)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = co @ matrix[:3, :3].T + matrix[:3, 3]
    # Blender holds world positions as float32 too.
    world = world.astype(np.float32).astype(np.float64)
    keep = selected[tri_poly]
    return world[tri_verts.reshape(-1, 3)[keep]]


def measure_area(context: bpy.types.Context, obj: bpy.types.Object) -> site.AreaReading:
    """Store and return the ground area of ``obj``'s selected faces."""
    props = settings(context).measure
    placement = _placement(context)
    if PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS and not site.available():
        raise site.GeodesyUnavailable(NEEDS_PYPROJ)
    reading = site.face_areas(placement, selected_triangles(obj))
    props.area = _dumps(reading)
    props.area_object = obj.name
    props.key = placement_key(context)
    props.error = ""
    return reading


class GEOTOOLS_OT_measure_area(bpy.types.Operator):
    """Grid and ground area of the active mesh's selected faces"""

    bl_idname = "geotools.measure_area"
    bl_label = "Area of Selected Faces"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        return obj is not None and obj.type == "MESH"

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = context.active_object
        result = _measure_errors(self, context, lambda: measure_area(context, obj))
        reading = load_area(settings(context).measure.area)
        if result == {"FINISHED"} and reading is not None and reading.triangles == 0:
            self.report({"WARNING"}, f"No faces of {obj.name} are selected.")
        return result


COPY_ITEMS = (
    ("A", "Point A", "Copy point A's coordinates"),
    ("B", "Point B", "Copy point B's coordinates"),
    ("DISTANCE", "Distance", "Copy the distance from A to B"),
    ("AREA", "Area", "Copy the area of the selected faces"),
    ("SUN", "Sun", "Copy the sun's position"),
)


def reading_text(context: bpy.types.Context, what: str) -> str:
    """The stored result ``what`` as tab-separated lines at full precision; empty if none."""
    analysis = settings(context)
    measure_props = analysis.measure
    if what in {"A", "B"}:
        reading = load_reading(getattr(measure_props, f"reading_{what.lower()}"))
        return reading.as_text() if reading is not None else ""
    if what == "DISTANCE":
        measurement = load_measurement(measure_props.distance)
        return measurement.as_text() if measurement is not None else ""
    if what == "AREA":
        area = load_area(measure_props.area)
        return area.as_text() if area is not None else ""
    result = load_sun(analysis.sun.result)
    if result is None:
        return ""
    rows = (
        ("UTC", result["utc"]),
        ("Time zone", result["zone"]),
        ("Azimuth (deg from true north)", repr(result["azimuth"])),
        ("Elevation (deg)", repr(result["elevation"])),
        ("Convergence (deg)", repr(result["convergence"])),
        (f"Azimuth (deg from {result['north']})", repr(result["scene_azimuth"])),
        ("Latitude", repr(result["latitude"])),
        ("Longitude", repr(result["longitude"])),
    )
    return "\n".join(f"{name}\t{value}" for name, value in rows)


class GEOTOOLS_OT_copy_reading(bpy.types.Operator):
    """Copy a result to the clipboard at full precision, as tab-separated lines"""

    bl_idname = "geotools.copy_reading"
    bl_label = "Copy"
    bl_options = {"REGISTER"}

    what: EnumProperty(name="What", items=COPY_ITEMS, default="A")

    def execute(self, context: bpy.types.Context) -> set[str]:
        text = reading_text(context, self.what)
        if not text:
            self.report({"WARNING"}, "Nothing to copy yet.")
            return {"CANCELLED"}
        context.window_manager.clipboard = text
        self.report({"INFO"}, "Copied to the clipboard.")
        return {"FINISHED"}


# --- registration ------------------------------------------------------------------------------

classes = (
    GeoToolsSunSettings,
    GeoToolsSunHoursSettings,
    GeoToolsShadowStudySettings,
    GeoToolsMeasureSettings,
    GeoToolsAnalysis,
    GEOTOOLS_OT_sun_place,
    GEOTOOLS_OT_sun_animate,
    GEOTOOLS_OT_point_from_cursor,
    GEOTOOLS_OT_point_pick,
    GEOTOOLS_OT_measure_refresh,
    GEOTOOLS_OT_measure_clear,
    GEOTOOLS_OT_measure_area,
    GEOTOOLS_OT_copy_reading,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools_analysis = PointerProperty(type=GeoToolsAnalysis)


def unregister() -> None:
    if hasattr(bpy.types.Scene, "geotools_analysis"):
        del bpy.types.Scene.geotools_analysis
    _unregister_classes()
