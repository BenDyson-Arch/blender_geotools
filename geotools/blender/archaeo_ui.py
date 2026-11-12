"""The Archaeoastronomy sub-panel of Analysis (``archaeo``).

Observer, horizon profile, events at an epoch (with how far back they can be
trusted), sightlines and the alignment check, each with its last result and a
copy button. Panels draw stored results only; the operators compute.
``analysis_ui`` lists these classes among its own.
"""

from __future__ import annotations

import json
import math

import bpy

from ..analysis import archaeo as core
from . import analysis
from .properties import parse_vec
from .ui import big_button, info_row, split_column, wrapped

MAX_ALIGNMENT_DRAWN = 12


def _deg(value: float, places: int = 2) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:.{places}f}°"


def _copy(layout: bpy.types.UILayout, what: str) -> None:
    layout.operator("geotools.archaeo_copy", text="", icon="COPYDOWN").what = what


def _error(layout, context, text: str) -> None:
    if text:
        col = layout.column()
        col.alert = True
        wrapped(col, context, text, "ERROR")


class GEOTOOLS_PT_analysis_archaeo(bpy.types.Panel):
    """Horizon, solar and lunar events at an epoch, sightlines and alignments."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"
    bl_idname = "GEOTOOLS_PT_analysis_archaeo"
    bl_label = "Archaeoastronomy"
    bl_parent_id = "GEOTOOLS_PT_analysis"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = analysis.settings(context).archaeo
        _draw_observer(layout, context, props)
        _draw_horizon(layout, context, props)
        _draw_events(layout, context, props)
        _draw_outputs(layout, context, props)


def _draw_observer(layout, context, props) -> None:
    row = layout.row(align=True)
    row.label(text="Observer")
    row.operator("geotools.archaeo_observer_cursor", text="Cursor", icon="PIVOT_CURSOR")
    row.operator("geotools.archaeo_observer_pick", text="Pick", icon="EYEDROPPER")
    point = parse_vec(props.observer)
    if point is None:
        layout.label(text="No observer: events use the Sun panel's site", icon="INFO")
        return
    col = layout.column(align=True)
    info_row(col, "Point", ", ".join(f"{v:,.2f}" for v in point))
    if props.key and props.key != analysis.placement_key(context):
        layout.label(text="Project changed: set the observer again", icon="FILE_REFRESH")


def _draw_horizon(layout, context, props) -> None:
    box = layout.box()
    box.label(text="Horizon", icon="WORLD")
    col = split_column(box)
    col.prop(props, "eye_height")
    col.prop(props, "azimuth_step")
    col.prop(props, "ignore_within")
    col.prop(props, "open_elevation")
    col.prop(props, "curvature")
    col.prop(props, "refraction")
    row = big_button(box)
    row.enabled = bool(props.observer)
    row.operator("geotools.archaeo_horizon", icon="WORLD")
    _error(box, context, props.profile_error)
    if not props.profile:
        return
    data = json.loads(props.profile)
    info = data["info"]
    elevations = [v for v in data["elevation"] if v is not None]
    n = len(data["azimuth"])
    header = box.row()
    header.label(text=f"{n} azimuths, {info['seconds']:.1f} s", icon="CHECKMARK")
    _copy(header, "PROFILE")
    col = box.column(align=True)
    if elevations:
        peak = max(
            range(n), key=lambda i: -90.0 if data["elevation"][i] is None else data["elevation"][i]
        )
        info_row(col, "Highest", f"{_deg(data['elevation'][peak])} at {data['azimuth'][peak]:.1f}°")
        info_row(col, "Lowest", _deg(min(elevations)))
    info_row(col, "Site", f"{info['latitude']:.5f}, {info['longitude']:.5f}")
    info_row(col, "Convergence", _deg(info["convergence"], 4))
    if data["open"]:
        wrapped(
            box,
            context,
            f"{len(data['open'])} azimuths see no terrain; they use the open horizon "
            f"({props.open_elevation:.2f}°).",
            "INFO",
        )
    if data["edge"]:
        wrapped(
            box,
            context,
            f"At {len(data['edge'])} azimuths the skyline is the edge of the terrain mesh, "
            "not necessarily the real horizon: extend the terrain there.",
            "ERROR",
        )
    if data["closed"]:
        wrapped(box, context, f"{len(data['closed'])} azimuths are closed above 60°.", "INFO")
    sun_hours = analysis.settings(context).sun_hours
    box.prop(sun_hours, "horizon_profile", text="Far Horizon for Sun Hours")
    box.operator("geotools.archaeo_clear_horizon", icon="X")


def _draw_events(layout, context, props) -> None:
    box = layout.box()
    box.label(text="Events", icon="LIGHT_SUN")
    col = split_column(box)
    row = col.row(align=True)
    row.prop(props, "epoch_year")
    row.label(text=core.format_year(props.epoch_year))
    col.prop(props, "limb")
    col.prop(props, "moon_events")
    sub = col.column()
    sub.active = props.moon_events
    sub.prop(props, "lunar")
    col.prop(props, "dated_events")
    if props.dated_events:
        sun_props = analysis.settings(context).sun
        box.label(
            text=f"Dated: {sun_props.year}, and {sun_props.year}-{sun_props.month:02d}-"
            f"{sun_props.day:02d}; zone in Sun",
            icon="TIME",
        )
    big_button(box).operator("geotools.archaeo_events", icon="LIGHT_SUN")
    _error(box, context, props.events_error)
    if not props.events:
        return
    data = json.loads(props.events)
    summary = data["summary"]
    header = box.row()
    header.label(text=f"{summary['epoch']}: ε {summary['obliquity']:.4f}°", icon="CHECKMARK")
    _copy(header, "EVENTS")
    for note in summary.get("trust", []):
        wrapped(box, context, note, "INFO")
    col = box.column(align=True)
    with_profile = summary.get("profile", False)
    for row in data["events"]:
        best = row["horizon_azimuth"] if with_profile else row["azimuth"]
        value = _deg(best)
        if with_profile and row["horizon_altitude"] is not None:
            value += f" @ {_deg(row['horizon_altitude'])}"
        when = row["horizon_time"] if with_profile else row["time"]
        if when:
            value += f" {when[:5]}"
        label = row["name"].replace("standstill ", "").replace(" solstice", " sol.")
        info_row(col, label, value)
    if with_profile:
        col.label(text="Azimuths on the horizon profile @ altitude", icon="WORLD")
    for kind in ("major", "minor"):
        if kind in summary:
            s = summary[kind]
            wrapped(
                box,
                context,
                f"{kind.capitalize()} standstill (node): {s['node_last_text']} and "
                f"{s['node_next_text']}. Extremes {s['north']:+.3f}° ({s['north_text']}), "
                f"{s['south']:+.3f}° ({s['south_text']}).",
                "INFO",
            )


def _draw_outputs(layout, context, props) -> None:
    box = layout.box()
    box.label(text="Sightlines and Alignments", icon="LIGHT_SUN")
    col = split_column(box)
    col.prop(props, "sightline_length")
    row = box.row(align=True)
    row.enabled = bool(props.events) and bool(props.observer)
    row.operator("geotools.archaeo_sightlines", icon="MOD_LINEART")
    if props.sightlines_result:
        result = json.loads(props.sightlines_result)
        wrapped(
            box,
            context,
            f"{result['lines']} lines in {result['object']}: export with File > Export > "
            "GeoPackage, or publish its collection as Vectors.",
            "INFO",
        )
    row = box.row(align=True)
    row.enabled = bool(props.events)
    row.operator("geotools.archaeo_alignment", icon="DRIVER_ROTATIONAL_DIFFERENCE")
    _error(box, context, props.alignment_error)
    if not props.alignment:
        return
    result = json.loads(props.alignment)
    header = box.row()
    header.label(text=f"{result['object']}: {len(result['rows'])} lines")
    _copy(header, "ALIGNMENT")
    col = box.column(align=True)
    for r in result["rows"][:MAX_ALIGNMENT_DRAWN]:
        label = f"Edge {r['edge']}" if r["fid"] is None else f"fid {r['fid']}"
        info_row(col, label, f"{r['azimuth']:.2f}° / {r['back_azimuth']:.2f}°")
        info_row(col, "Decl.", f"{_deg(r['declination'])} / {_deg(r['back_declination'])}")
        if r["nearest"]:
            name, _az, diff = r["nearest"][0]
            info_row(col, "Nearest", f"{name} {diff:+.2f}°")
    if len(result["rows"]) > MAX_ALIGNMENT_DRAWN:
        col.label(text=f"… {len(result['rows']) - MAX_ALIGNMENT_DRAWN} more: copy for all")
    for note in result["notes"]:
        wrapped(box, context, note, "INFO")


classes = (GEOTOOLS_PT_analysis_archaeo,)
