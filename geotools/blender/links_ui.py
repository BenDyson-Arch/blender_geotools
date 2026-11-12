"""The Linked Data panel (after Source); its list, ``GEOTOOLS_UL_links``, is in ``links``.

Registered by ``panels`` so it sits in the sidebar order; it only draws cached
link state (status, options), never reads files.
"""

from __future__ import annotations

import json

import bpy

from ..core import crs_match, paths
from ..core.reproject import format_accuracy
from ..core.vector.options import VectorOptions
from . import data_origin
from .links import KIND_ICONS, STATUS_ICONS, active_link, linked_objects, links_state
from .ui import DONE, ERROR, WARNING, header_status, wrapped

GEOTIFF_OPERATOR = "GEOTOOLS_OT_import_geotiff"
"""Stream 2's GeoTIFF import; its button shows once that operator is registered."""

_HEADER_STATES = {"OK": DONE, "CHANGED": WARNING, "ERROR": ERROR}


def geotiff_available() -> bool:
    return hasattr(bpy.types, GEOTIFF_OPERATOR)


def _filters_text(link) -> str:
    options = VectorOptions.from_text(link.options)
    parts = []
    if options.where:
        parts.append(options.where)
    if options.site_bbox is not None:
        parts.append("within site")
    return ", ".join(parts) or "None"


def _reprojection_text(link) -> str | None:
    report = crs_match.report_from_dict(VectorOptions.from_text(link.options).reprojection)
    if report is None:
        return None
    return f"From {report.source}, accuracy {format_accuracy(report.accuracy_m)}"


def _raster_text(link) -> str:
    try:
        options = json.loads(link.options) if link.options else {}
    except ValueError:
        options = {}
    if not isinstance(options, dict):
        options = {}
    if options.get("mode") == "ORTHO":
        return "Orthophoto"
    step = int(options.get("step", 1) or 1)
    return "DEM, every pixel" if step == 1 else f"DEM, every {step} pixels"


class GEOTOOLS_PT_linked_data(bpy.types.Panel):
    """GeoJSON, GeoPackage and GeoTIFF files in the project frame, kept linked to their files."""

    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"
    bl_idname = "GEOTOOLS_PT_linked_data"
    bl_label = "Linked Data"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context: bpy.types.Context) -> None:
        state = links_state(linked_objects())
        header_status(self.layout, _HEADER_STATES.get(state) if state else None)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        row = layout.row()
        row.template_list(
            "GEOTOOLS_UL_links", "", bpy.data, "objects", scene, "geotools_link_index", rows=3
        )
        col = row.column(align=True)
        col.operator("geotools.link_check", text="", icon="FILE_REFRESH")

        layout.label(text="Import", icon="IMPORT")
        row = layout.row(align=True)
        row.operator("geotools.import_geojson", text="GeoJSON")
        row.operator("geotools.import_gpkg", text="GeoPackage")
        if geotiff_available():
            row.operator("geotools.import_geotiff", text="GeoTIFF")

        pending = data_origin.pending_offer(context)
        if pending is not None:
            data_origin.draw_offer(layout, context, pending)

        obj = active_link(context)
        if obj is None:
            if not linked_objects():
                wrapped(layout, context, "No linked data yet. Import a file above.", "INFO")
            return
        link = obj.geotools_link
        box = layout.box()
        name = paths.name(link.filepath)
        if link.layer and link.kind == "GPKG":
            name = f"{name} ({link.layer})"
        box.label(text=name, icon=KIND_ICONS.get(link.kind, "FILE"))
        if link.status != "OK":
            sub = box.column(align=True)
            sub.alert = link.status in {"ERROR", "MISSING"}
            status = link.bl_rna.properties["status"].enum_items[link.status].name
            wrapped(sub, context, link.message or status, STATUS_ICONS[link.status], max_lines=3)
        row = box.row(align=True)
        row.operator("geotools.link_refresh", icon="FILE_REFRESH").name = obj.name
        row.prop(link, "auto_refresh", text="Auto", toggle=True, icon="TIME")
        if link.kind in {"GPKG", "GEOJSON"}:
            row = box.row(align=True)
            row.label(text=f"Filters: {_filters_text(link)}", icon="FILTER")
            row.operator("geotools.link_filters", text="", icon="GREASEPENCIL").name = obj.name
            row = box.row(align=True)
            row.operator(
                "geotools.link_write_back", text="Write Back", icon="EXPORT"
            ).name = obj.name
            op = row.operator("geotools.export_vector", text="Export", icon="FILE_NEW")
            op.object_name = obj.name
            op.file_format = "GEOJSON" if link.kind == "GEOJSON" else "GPKG"
            reprojected = _reprojection_text(link)
            if reprojected:
                box.label(text=reprojected, icon="ORIENTATION_GLOBAL")
        elif link.kind == "GEOTIFF":
            box.label(text=_raster_text(link), icon="MESH_GRID")
        if link.last_sync:
            box.label(text=f"Synced {link.last_sync.replace('T', ' ')}", icon="BLANK1")


classes = (GEOTOOLS_PT_linked_data,)
