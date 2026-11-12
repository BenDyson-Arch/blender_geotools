"""Sidebar panels of the surface tools: Hydrology, and Contours and Sections.

Sub-panels of Analysis (``analysis_ui.GEOTOOLS_PT_analysis``), listed by
``analysis_ui`` after Measure and Presets. They draw stored settings and
results only; the operators (``hydrology``, ``contours``) compute.
"""

from __future__ import annotations

import json

import bpy

from . import contours, hydrology
from .ui import big_button, info_row, split_column, wrapped


class _SurfacePanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"
    bl_parent_id = "GEOTOOLS_PT_analysis"
    bl_options = {"DEFAULT_CLOSED"}


def _error(layout, context, text: str) -> None:
    col = layout.column()
    col.alert = True
    wrapped(col, context, text, "ERROR")


def _area(value: float) -> str:
    return f"{value / 1e4:,.2f} ha" if value >= 1e5 else f"{value:,.1f} m²"


class GEOTOOLS_PT_analysis_hydrology(_SurfacePanel, bpy.types.Panel):
    """Flow accumulation, ponding and depressions on the 3D surface."""

    bl_idname = "GEOTOOLS_PT_analysis_hydrology"
    bl_label = "Hydrology"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = hydrology.settings(context)
        wrapped(layout, context, hydrology.GEOMETRIC_NOTE, "INFO")
        col = split_column(layout)
        col.prop(props, "surface")
        col.prop(props, "area")
        col.prop(props, "min_depth")
        layers = col.column(align=True)
        layers.prop(props, "accumulation_layer", text="Layers")
        layers.prop(props, "depth_layer", text=" ")
        layers.prop(props, "depression_layer", text=" ")
        col.prop(props, "depression_points")
        col.prop(props, "flow_lines")
        sub = col.column()
        sub.active = props.flow_lines
        sub.prop(props, "channel_area")
        sub.prop(props, "channel_count")
        col.prop(props, "offset")
        big_button(layout).operator("geotools.hydrology", icon="MOD_FLUIDSIM")
        if props.error:
            _error(layout, context, props.error)
            return
        if not props.result:
            return
        result = json.loads(props.result)
        box = layout.box()
        box.label(
            text=f"{result['faces']:,} faces, {result['objects']} object(s)", icon="MESH_DATA"
        )
        col = box.column(align=True)
        stats = result["stats"]
        info_row(col, "Largest", _area(result["max_accumulation"]))
        info_row(col, "Outlets", f"{stats['outlets']:,}")
        info_row(col, "Drops", f"{stats['drops']:,} ({stats['drops_out']:,} out)")
        info_row(col, "Depressions", f"{result['depressions']:,}")
        info_row(col, "Ponded", f"{result['ponded_volume']:,.2f} m³")
        for row in result["top"][:3]:
            info_row(
                col,
                f"#{row['id']}",
                f"{row['volume']:,.1f} m³ to {row['level']:,.3f} {result['datum']}",
            )
        if result["flow_lines"]:
            info_row(col, "Flow Paths", f"{result['flow_lines']:,} lines")
        info_row(col, "Time", f"{result['seconds']:.1f} s")
        for note in result["notes"]:
            wrapped(box, context, note, "INFO")


class GEOTOOLS_PT_analysis_contours(_SurfacePanel, bpy.types.Panel):
    """Contour and section lines, for Export Vectors."""

    bl_idname = "GEOTOOLS_PT_analysis_contours"
    bl_label = "Contours and Sections"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = contours.settings(context)
        col = split_column(layout)
        col.prop(props, "surface")
        box = layout.box()
        box.label(text="Contours", icon="IPO_CONSTANT")
        col = split_column(box)
        col.prop(props, "interval")
        col.prop(props, "base")
        col.prop(props, "major_every")
        col.prop(props, "contour_name")
        box.operator("geotools.contours", icon="IPO_CONSTANT")
        if props.contour_error:
            _error(box, context, props.contour_error)
        elif props.contour_result:
            result = json.loads(props.contour_result)
            col = box.column(align=True)
            info_row(col, "Lines", f"{result['lines']:,} ({result['closed']:,} closed)")
            info_row(
                col,
                "Levels",
                f"{result['lowest']:,.3f} to {result['highest']:,.3f} {result['datum']}",
            )
        box = layout.box()
        box.label(text="Sections", icon="SNAP_EDGE")
        col = split_column(box)
        col.prop(props, "line")
        col.prop(props, "selected_edges")
        col.prop(props, "section_name")
        box.operator("geotools.sections", icon="SNAP_EDGE")
        if props.section_error:
            _error(box, context, props.section_error)
        elif props.section_result:
            result = json.loads(props.section_result)
            col = box.column(align=True)
            info_row(col, "Pieces", f"{result['pieces']:,} on {result['sections']} line(s)")
            info_row(
                col,
                "Heights",
                f"{result['lowest']:,.3f} to {result['highest']:,.3f} {result['datum']}",
            )
        layout.label(text="Export: File > Export > GeoPackage", icon="EXPORT")


classes = (GEOTOOLS_PT_analysis_hydrology, GEOTOOLS_PT_analysis_contours)
