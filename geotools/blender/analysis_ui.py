"""Sidebar panels of the M5 analysis tools (``analysis``).

- Analysis: the parent, with what the tools need (pyproj for a CRS project).
  - Sun: date, time and time zone, site, Place Sun and Animate Day, and the
    last sun position with the convergence applied.
  - Sun Hours: hours of direct sun and clear-sky insolation over a day, dates
    or a year, into layers (``sun_hours``), with the last run's summary.
  - Shadow Study: Sun light keys at the equinoxes and solstices.
  - Coordinates: points A and B (from the 3D cursor or a pick), their E, N, RL
    and latitude and longitude, the distance between them, and the area of
    the selected faces. Each result has a copy button.
  - Presets: the Add Analysis menu of Geometry Nodes presets
    (``analysis_presets``) and the presets on the active object.

Panels draw stored results only; the operators compute. ``panels`` registers
these classes before About.
"""

from __future__ import annotations

import json

import bpy

from ..analysis import site, sun
from ..core.placement import PlacementMode
from . import analysis, analysis_presets
from .properties import ANCHOR_FIELDS, number_is_valid
from .ui import big_button, info_row, split_column, wrapped


class _AnalysisPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


def _mode(context: bpy.types.Context) -> str:
    return context.scene.geotools.placement_mode


def _needs_pyproj_missing(context: bpy.types.Context) -> bool:
    return _mode(context) == "SOURCE_CRS" and not site.available()


def _deg(value: float, places: int = 4) -> str:
    return f"{value:.{places}f}°"


def _copy_button(layout: bpy.types.UILayout, what: str) -> None:
    layout.operator("geotools.copy_reading", text="", icon="COPYDOWN").what = what


class GEOTOOLS_PT_analysis(_AnalysisPanel, bpy.types.Panel):
    """Analysis tools that need the georeference."""

    bl_idname = "GEOTOOLS_PT_analysis"
    bl_label = "Analysis"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        if _needs_pyproj_missing(context):
            col = layout.column()
            col.alert = True
            wrapped(col, context, analysis.NEEDS_PYPROJ, "ERROR")
        elif _mode(context) == "LOCAL":
            wrapped(
                layout,
                context,
                "Local project: the sun uses the latitude and longitude below with +Y as "
                "true north, and points have local coordinates only.",
                "INFO",
            )


class GEOTOOLS_PT_analysis_sun(_AnalysisPanel, bpy.types.Panel):
    """The sun at a moment or over a day, on a Blender Sun light."""

    bl_idname = "GEOTOOLS_PT_analysis_sun"
    bl_label = "Sun"
    bl_parent_id = "GEOTOOLS_PT_analysis"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = analysis.settings(context).sun
        project = context.scene.geotools

        col = layout.column(align=True)
        row = col.row(align=True)
        row.prop(props, "year", text="")
        row.prop(props, "month", text="")
        row.prop(props, "day", text="")
        row = col.row(align=True)
        row.prop(props, "hour", text="")
        row.prop(props, "minute", text="")
        row.prop(props, "second", text="")

        layout.row().prop(props, "tz_mode", expand=True)
        col = split_column(layout)
        if props.tz_mode == "ZONE":
            col.prop(props, "tz_name", text="Zone")
            if not sun.time_zone_database_available():
                box = layout.column()
                box.alert = props.tz_name.strip().upper() not in {"UTC", "GMT", "Z", "ETC/UTC"}
                wrapped(box, context, analysis.TZ_NO_DATABASE_NOTE, "ERROR")
        else:
            col.prop(props, "utc_offset", text="Hours")
            layout.label(text="Fixed offset: no daylight saving", icon="INFO")

        col = split_column(layout)
        if _mode(context) == "LOCAL":
            for field, label in zip(ANCHOR_FIELDS[:2], ("Longitude", "Latitude"), strict=True):
                row = col.row(align=True)
                row.alert = not number_is_valid(project, field.attr)
                row.prop(project, field.attr, text=label)
        else:
            col.prop(props, "site")
        col.prop(props, "light")
        col.prop(props, "strength")
        col.prop(props, "refraction")

        big_button(layout).operator("geotools.sun_place", icon="LIGHT_SUN")

        box = layout.box()
        box.label(text="Over a Day")
        col = split_column(box)
        row = col.row(align=True)
        row.prop(props, "start_hour", text="Hours")
        row.prop(props, "end_hour", text="")
        col.prop(props, "step_minutes", text="Step (min)")
        col.prop(props, "frame_start")
        col.prop(props, "frames_per_step")
        box.operator("geotools.sun_animate", icon="KEYFRAME_HLT")

        _draw_sun_result(layout, context, props)


def _draw_sun_result(layout, context, props) -> None:
    if props.error:
        col = layout.column()
        col.alert = True
        wrapped(col, context, props.error, "ERROR")
        return
    result = analysis.load_sun(props.result)
    if result is None:
        return
    box = layout.box()
    header = box.row()
    title = f"Frame {result['frame']}" if "frame" in result else "Sun"
    header.label(text=title, icon="LIGHT_SUN")
    _copy_button(header, "SUN")
    col = box.column(align=True)
    info_row(col, "UTC", result["utc"].replace("+00:00", "").replace("T", " "))
    info_row(col, "Azimuth", f"{_deg(result['azimuth'])} true")
    info_row(col, "Elevation", _deg(result["elevation"]))
    info_row(col, "Convergence", _deg(result["convergence"]))
    info_row(col, "Scene Az.", f"{_deg(result['scene_azimuth'])} from +Y")
    info_row(col, "Site", f"{result['latitude']:.5f}, {result['longitude']:.5f}")
    north = {
        "grid north": "+Y is grid north; convergence applied",
        "anchor heading": "+Y is the anchor's heading",
        "true north": "+Y is taken as true north",
    }[result["north"]]
    col.label(text=north, icon="ORIENTATION_GLOBAL")
    if "steps" in result:
        first, last = result["frames"]
        col.label(text=f"{result['steps']} keys, frames {first}-{last}", icon="KEYFRAME")
    for note in result["notes"]:
        wrapped(box, context, note, "INFO")


def _draw_period(layout, props, sun_props) -> None:
    layout.row().prop(props, "period", expand=True)
    col = layout.column(align=True)
    row = col.row(align=True)
    if props.period == "YEAR":
        row.prop(sun_props, "year", text="Year")
        return
    row.label(text="From" if props.period == "RANGE" else "Date")
    row.prop(sun_props, "year", text="")
    row.prop(sun_props, "month", text="")
    row.prop(sun_props, "day", text="")
    if props.period == "RANGE":
        row = col.row(align=True)
        row.label(text="To")
        row.prop(props, "end_year", text="")
        row.prop(props, "end_month", text="")
        row.prop(props, "end_day", text="")


class GEOTOOLS_PT_analysis_sun_hours(_AnalysisPanel, bpy.types.Panel):
    """Hours of direct sun and clear-sky insolation, into layers."""

    bl_idname = "GEOTOOLS_PT_analysis_sun_hours"
    bl_label = "Sun Hours"
    bl_parent_id = "GEOTOOLS_PT_analysis"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = analysis.settings(context)
        props, sun_props = settings.sun_hours, settings.sun
        layout.label(text="Time zone, site and refraction: see Sun", icon="LIGHT_SUN")
        _draw_period(layout, props, sun_props)
        col = split_column(layout)
        col.prop(props, "step_minutes", text="Step (min)")
        col.prop(props, "tolerance")
        col.prop(props, "horizon")
        col.prop(props, "domain")
        if props.domain == "TEXTURE":
            col.prop(props, "resolution")
        col.prop(props, "hours_layer", text="Hours Layer")
        col.prop(props, "insolation")
        if props.insolation:
            sub = col.column()
            sub.prop(props, "insolation_layer", text="kWh/m² Layer")
            sub.prop(props, "linke_turbidity")
            sub.prop(props, "diffuse")
            row = sub.row()
            row.active = props.diffuse
            row.prop(props, "diffuse_rays")
        col.prop(props, "offset")
        big_button(layout).operator("geotools.sun_hours", icon="LIGHT_SUN")
        _draw_sun_hours_result(layout, context, props)


def _draw_sun_hours_result(layout, context, props) -> None:
    if props.error:
        col = layout.column()
        col.alert = True
        wrapped(col, context, props.error, "ERROR")
        return
    if not props.result:
        return
    result = json.loads(props.result)
    box = layout.box()
    box.label(text=f"{result['first_day']} to {result['last_day']}", icon="TIME")
    col = box.column(align=True)
    stats = result["stats"]
    info_row(col, "Sun Up", f"{stats['sun_hours_possible']:,.1f} h")
    info_row(col, "Samples", f"{result['samples']:,} ({result['domain'].lower()})")
    info_row(col, "Rays", f"{result['sun_rays']:,} sun + {stats['sky_rays']:,} sky")
    if stats["group_tolerance_deg"] > 0:
        info_row(col, "Merged", f"within {stats['max_group_deviation_deg']:.2f}°")
    info_row(col, "Time", f"{result['seconds']:.1f} s, {result['bakes']} bake(s)")
    for name, (low, high) in result["ranges"].items():
        if low and high:
            info_row(col, name, f"{float(low):,.2f} to {float(high):,.2f}")
    for note in result["notes"]:
        wrapped(box, context, note, "INFO")


class GEOTOOLS_PT_analysis_shadow_study(_AnalysisPanel, bpy.types.Panel):
    """Sun light keys at the equinoxes and solstices, one frame per moment."""

    bl_idname = "GEOTOOLS_PT_analysis_shadow_study"
    bl_label = "Shadow Study"
    bl_parent_id = "GEOTOOLS_PT_analysis"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        settings = analysis.settings(context)
        props, sun_props = settings.shadow_study, settings.sun
        layout.label(text=f"Year {sun_props.year}; time zone and site: see Sun", icon="TIME")
        col = layout.column(align=True)
        for name in ("march", "june", "september", "december"):
            col.prop(props, name)
        col = split_column(layout)
        row = col.row(align=True)
        row.prop(props, "start_hour", text="Hours")
        row.prop(props, "end_hour", text="")
        col.prop(props, "step_hours")
        col.prop(sun_props, "frame_start")
        big_button(layout).operator("geotools.sun_shadow_study", icon="RENDER_ANIMATION")
        if props.error:
            col = layout.column()
            col.alert = True
            wrapped(col, context, props.error, "ERROR")
            return
        if not props.result:
            return
        result = json.loads(props.result)
        box = layout.box()
        first, last = result["frames"]
        box.label(text=f"Frames {first}-{last}: render them", icon="RENDER_ANIMATION")
        col = box.column(align=True)
        for row in result["moments"]:
            info_row(col, f"Frame {row['frame']}", row["local"].replace("T", " ")[:16])
        for note in result["notes"]:
            wrapped(box, context, note, "INFO")


class GEOTOOLS_PT_analysis_coordinates(_AnalysisPanel, bpy.types.Panel):
    """Points on Earth, the distance between them and the area of selected faces."""

    bl_idname = "GEOTOOLS_PT_analysis_coordinates"
    bl_label = "Coordinates"
    bl_parent_id = "GEOTOOLS_PT_analysis"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = analysis.settings(context).measure
        for which in ("A", "B"):
            row = layout.row(align=True)
            row.label(text=f"Point {which}")
            row.operator(
                "geotools.point_from_cursor", text="Cursor", icon="PIVOT_CURSOR"
            ).point = which
            row.operator("geotools.point_pick", text="Pick", icon="EYEDROPPER").point = which
            reading = analysis.load_reading(getattr(props, f"reading_{which.lower()}"))
            if reading is not None:
                _draw_reading(layout, reading, which)

        if props.error:
            col = layout.column()
            col.alert = True
            wrapped(col, context, props.error, "ERROR")
        if props.note:
            wrapped(layout, context, props.note, "INFO")

        measurement = analysis.load_measurement(props.distance)
        if measurement is not None:
            _draw_measurement(layout, measurement)

        layout.operator("geotools.measure_area", icon="FACESEL")
        area = analysis.load_area(props.area)
        if area is not None:
            _draw_area(layout, area, props.area_object)

        stale = (
            props.reading_a or props.reading_b or props.area
        ) and props.key != analysis.placement_key(context)
        row = layout.row(align=True)
        if stale:
            row.label(text="Project changed", icon="FILE_REFRESH")
            row.operator("geotools.measure_refresh", text="Read Again")
        if props.point_a or props.point_b or props.area:
            row.operator("geotools.measure_clear", icon="X")


def _draw_reading(layout, reading: site.PointReading, which: str) -> None:
    box = layout.box()
    col = box.column(align=True)
    names = {
        PlacementMode.SOURCE_CRS: ("E", "N", "RL"),
        PlacementMode.ANCHOR: ("E (anchor)", "N (anchor)", "U (anchor)"),
        PlacementMode.LOCAL: ("X", "Y", "Z"),
    }[reading.mode]
    first = info_row(col, names[0], f"{reading.east:,.4f}")
    _copy_button(first, which)
    info_row(col, names[1], f"{reading.north:,.4f}")
    info_row(col, names[2], f"{reading.level:,.4f}")
    if reading.height is not None and reading.mode is PlacementMode.SOURCE_CRS:
        info_row(col, "Ellipsoidal", f"{reading.height:,.4f}")
    if reading.latitude is not None and reading.longitude is not None:
        info_row(col, "Latitude", f"{reading.latitude:.9f}°")
        info_row(col, "Longitude", f"{reading.longitude:.9f}°")
        if reading.geodetic_crs:
            info_row(col, "Datum", reading.geodetic_crs)
    if reading.mode is not PlacementMode.LOCAL:
        info_row(col, "Scale", f"{reading.combined_scale:.8f}")
    info_row(col, "float32", f"±{reading.resolution / 2:.2g} m local")


def _draw_measurement(layout, m: site.Measurement) -> None:
    box = layout.box()
    header = box.row()
    header.label(text="A to B", icon="DRIVER_DISTANCE")
    _copy_button(header, "DISTANCE")
    col = box.column(align=True)
    info_row(col, "Grid", f"{m.grid_distance:,.4f} m")
    info_row(col, "Ground", f"{m.ground_distance:,.4f} m")
    info_row(col, "Height Diff", f"{m.height_difference:,.4f} m")
    info_row(col, "Slope", f"{m.slope_distance:,.4f} m")
    info_row(col, "Grid Brg", _deg(m.grid_bearing))
    info_row(col, "True Brg", _deg(m.true_bearing))
    info_row(col, "CSF", f"{m.combined_scale:.8f}")


def _draw_area(layout, area: site.AreaReading, name: str) -> None:
    box = layout.box()
    header = box.row()
    header.label(text=f"{name}: {area.triangles:,} triangles", icon="FACESEL")
    _copy_button(header, "AREA")
    col = box.column(align=True)
    info_row(col, "Ground Plan", f"{area.ground_plan:,.3f} m²")
    info_row(col, "Ground Surf.", f"{area.ground_surface:,.3f} m²")
    info_row(col, "Grid Plan", f"{area.grid_plan:,.3f} m²")
    info_row(col, "Grid Surf.", f"{area.grid_surface:,.3f} m²")


class GEOTOOLS_PT_analysis_presets(_AnalysisPanel, bpy.types.Panel):
    """Geometry Nodes analysis presets: a modifier and a layer each."""

    bl_idname = "GEOTOOLS_PT_analysis_presets"
    bl_label = "Presets"
    bl_parent_id = "GEOTOOLS_PT_analysis"

    def draw(self, context: bpy.types.Context) -> None:
        analysis_presets.draw_panel(self.layout, context)


classes = (
    GEOTOOLS_PT_analysis,
    GEOTOOLS_PT_analysis_sun,
    GEOTOOLS_PT_analysis_sun_hours,
    GEOTOOLS_PT_analysis_shadow_study,
    GEOTOOLS_PT_analysis_coordinates,
    GEOTOOLS_PT_analysis_presets,
)
