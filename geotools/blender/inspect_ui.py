"""Sidebar panels of output inspection (M7): Inspect Output, with Trace to Source
and Quality.

Registered by ``panels`` after Publish. They draw stored settings and results
only; the operators (``inspect``) decode, place and trace.
"""

from __future__ import annotations

from pathlib import Path

import bpy

from ..core import deps
from . import inspect
from .ui import big_button, info_row, split_column, wrapped


class _InspectPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


class GEOTOOLS_PT_inspect(_InspectPanel, bpy.types.Panel):
    """Open published tiles back in Blender, placed over the source."""

    bl_idname = "GEOTOOLS_PT_inspect"
    bl_label = "Inspect Output"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = inspect.settings(context)
        status = deps.probe_rusty_tiles()
        if not status.available:
            col = layout.column()
            col.alert = True
            wrapped(col, context, f"Cannot open tiles: {status.summary}", "ERROR")
        col = split_column(layout)
        col.prop(props, "source")
        if props.source == "FILE":
            col.prop(props, "filepath")
        else:
            archive = inspect.last_output(context.scene)
            info_row(col, "Last", archive.name if archive else "nothing published yet")
        layout.operator("geotools.inspect_list", icon="OUTLINER_OB_GROUP_INSTANCE")
        if not props.tiles:
            draw_result(layout, context)
            return
        info_row(layout, "Archive", Path(props.archive).name)
        info_row(layout, "Tiles", inspect.depth_summary(props))
        if props.notes:
            for note in props.notes.split("\n"):
                wrapped(layout, context, note, "INFO")
        layout.template_list(
            "GEOTOOLS_UL_output_tiles", "", props, "tiles", props, "tiles_index", rows=5
        )
        col = split_column(layout)
        col.prop(props, "scope")
        if props.scope == "DEPTH":
            col.prop(props, "open_depth")
        col.prop(props, "limit")
        row = big_button(layout)
        row.operator("geotools.inspect_open", icon="IMPORT")
        layout.operator("geotools.inspect_remove", icon="TRASH")
        draw_result(layout, context)


class GEOTOOLS_PT_inspect_trace(_InspectPanel, bpy.types.Panel):
    """Pick output faces to select their source faces, and the reverse."""

    bl_idname = "GEOTOOLS_PT_inspect_trace"
    bl_label = "Trace to Source"
    bl_parent_id = "GEOTOOLS_PT_inspect"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        wrapped(
            layout,
            context,
            "Select faces in Edit Mode, then trace. Coarse tiles trace to whole source primitives.",
            "INFO",
        )
        col = layout.column(align=True)
        col.operator("geotools.inspect_select_source", icon="RESTRICT_SELECT_OFF")
        col.operator("geotools.inspect_select_output", icon="RESTRICT_SELECT_OFF")


class GEOTOOLS_PT_inspect_quality(_InspectPanel, bpy.types.Panel):
    """Distance to the source, texel density and levels side by side."""

    bl_idname = "GEOTOOLS_PT_inspect_quality"
    bl_label = "Quality"
    bl_parent_id = "GEOTOOLS_PT_inspect"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = inspect.settings(context)
        col = split_column(layout)
        col.prop(props, "max_distance")
        col = layout.column(align=True)
        col.operator("geotools.inspect_distance", icon="MOD_SHRINKWRAP")
        col.operator("geotools.inspect_texel_density", icon="TEXTURE")
        col = split_column(layout)
        col.prop(props, "view")
        if props.view == "DEPTH":
            col.prop(props, "view_depth")
        elif props.view == "SIDE":
            col.prop(props, "gap")
        obj = context.active_object
        if inspect.tile_info(obj) is not None:
            for key in ("geotools_distance", "geotools_texel_density"):
                if key in obj:
                    wrapped(layout, context, str(obj[key]), "INFO")


def draw_result(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    result = inspect.settings(context).result
    if result:
        wrapped(layout, context, result, "INFO")


classes = (GEOTOOLS_PT_inspect, GEOTOOLS_PT_inspect_trace, GEOTOOLS_PT_inspect_quality)
