"""View3D sidebar panels, category "GeoTools".

Order: Project (with a Verify sub-panel), Source, Checks, Build Profiles, and
About last. Panels draw cached state only; operators do the work.
"""

import textwrap
from pathlib import Path

import bpy

from ..core import deps
from ..core.issues import Severity
from .common import draw_dependency_status, extension_version
from .properties import (
    ANCHOR_FIELDS,
    SEVERITY_ICONS,
    SOURCE_CRS_FIELDS,
    FieldError,
    NumberField,
    number_is_valid,
    parse_vec,
    placement_from_props,
)


class _GeoToolsPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


def _wrapped(layout: bpy.types.UILayout, text: str, width: int = 44, icon: str = "NONE") -> None:
    col = layout.column(align=True)
    for i, line in enumerate(textwrap.wrap(text, width) or [""]):
        col.label(text=line, icon=icon if i == 0 else "NONE")


def _number_rows(
    layout: bpy.types.UILayout, props: bpy.types.PropertyGroup, fields: tuple[NumberField, ...]
) -> None:
    col = layout.column(align=True)
    for field in fields:
        row = col.row(align=True)
        row.alert = not number_is_valid(props, field.attr)
        row.prop(props, field.attr, text=field.label)


def _format_bounds(text: str) -> str:
    v = parse_vec(text)
    return "" if v is None else f"{v[0]:.3f}, {v[1]:.3f}, {v[2]:.3f}"


# --- Project -------------------------------------------------------------------------


class GEOTOOLS_PT_project(_GeoToolsPanel, bpy.types.Panel):
    """Placement of the scene on Earth."""

    bl_idname = "GEOTOOLS_PT_project"
    bl_label = "Project"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        layout.row().prop(props, "placement_mode", expand=True)

        mode = props.placement_mode
        if mode == "LOCAL":
            _wrapped(layout, "Local metres. The output is not placed on Earth.", icon="INFO")
        elif mode == "ANCHOR":
            layout.label(text="Anchor (WGS84)")
            _number_rows(layout, props, ANCHOR_FIELDS[:3])
            layout.label(text="Orientation")
            _number_rows(layout, props, ANCHOR_FIELDS[3:4])
            layout.label(text="Scene Offset (metres)")
            _number_rows(layout, props, ANCHOR_FIELDS[4:])
        else:
            row = layout.row()
            row.alert = not props.source_crs.strip()
            row.prop(props, "source_crs")
            if not props.source_crs.strip():
                layout.label(text="Set the source CRS, e.g. EPSG:32756", icon="ERROR")
            layout.label(text="Source Offset")
            _number_rows(layout, props, SOURCE_CRS_FIELDS[:3])
            _number_rows(layout, props, SOURCE_CRS_FIELDS[3:])
            row = layout.row(align=True)
            row.operator("geotools.load_offset_file", icon="FILEBROWSER")
            if props.offset_file:
                layout.label(text=f"From {Path(props.offset_file).name}", icon="TEXT")


class GEOTOOLS_PT_project_verify(_GeoToolsPanel, bpy.types.Panel):
    """Where rusty-tiles puts the source with the current placement."""

    bl_idname = "GEOTOOLS_PT_project_verify"
    bl_label = "Verify Placement"
    bl_parent_id = "GEOTOOLS_PT_project"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        layout.operator("geotools.verify_placement", icon="WORLD")
        if not deps.probe_rusty_tiles().available:
            layout.label(text="Needs rusty-tiles (see About)", icon="INFO")
        if props.probe_status == "NONE":
            return
        try:
            current = repr(placement_from_props(props))
        except FieldError:
            current = None
        box = layout.box()
        if props.probe_status == "OK":
            col = box.column(align=True)
            col.label(text="Origin (WGS84)", icon="CHECKMARK")
            col.label(text=f"Longitude  {float(props.probe_lon):.9f}°")
            col.label(text=f"Latitude  {float(props.probe_lat):.9f}°")
            col.label(text=f"Height  {float(props.probe_height):.4f} m (ellipsoidal)")
            if props.probe_operation:
                col.label(text=f"Operation  {props.probe_operation}")
        else:
            col = box.column(align=True)
            col.alert = True
            _wrapped(col, props.probe_error or "Placement failed", icon="ERROR")
        if props.probe_key and current != props.probe_key:
            box.label(text="Placement changed since; verify again", icon="FILE_REFRESH")


# --- Source --------------------------------------------------------------------------


class GEOTOOLS_PT_source(_GeoToolsPanel, bpy.types.Panel):
    """The source collection, ingest and summary."""

    bl_idname = "GEOTOOLS_PT_source"
    bl_label = "Source"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        layout.prop(props, "source_collection", text="Collection")
        row = layout.row(align=True)
        row.operator("geotools.ingest_source", icon="IMPORT")
        row.operator("geotools.load_offset_file", text="Offset File", icon="FILEBROWSER")
        if props.source_filepath:
            layout.label(text=f"From {Path(props.source_filepath).name}", icon="FILE")

        if props.source_collection is None:
            return
        box = layout.box()
        header = box.row()
        header.label(text="Summary")
        header.operator("geotools.refresh_source_summary", text="", icon="FILE_REFRESH")
        if not props.summary_valid:
            box.label(text="Not summarised yet")
            return
        col = box.column(align=True)
        col.label(text=f"Objects  {props.summary_objects:,} ({props.summary_meshes:,} meshes)")
        col.label(text=f"Faces  {props.summary_faces:,}")
        col.label(text=f"Vertices  {props.summary_vertices:,}")
        if props.summary_bounds_min:
            col.label(text="World Bounds")
            col.label(text=f"Min  {_format_bounds(props.summary_bounds_min)}")
            col.label(text=f"Max  {_format_bounds(props.summary_bounds_max)}")
        else:
            col.label(text="No meshes", icon="ERROR")


# --- Checks --------------------------------------------------------------------------


class GEOTOOLS_PT_checks(_GeoToolsPanel, bpy.types.Panel):
    """Preflight issues against rusty-tiles' input rules."""

    bl_idname = "GEOTOOLS_PT_checks"
    bl_label = "Checks"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        layout.operator("geotools.run_checks", icon="CHECKMARK")
        if not props.checks_run:
            return
        counts = dict.fromkeys(("ERROR", "WARNING", "INFO"), 0)
        for item in props.issues:
            counts[item.severity] += 1
        if not len(props.issues):
            layout.label(text="No issues found", icon="CHECKMARK")
            return
        row = layout.row(align=True)
        for severity in Severity:
            key = severity.name
            row.label(text=str(counts[key]), icon=SEVERITY_ICONS[key])
        layout.template_list(
            "GEOTOOLS_UL_issues", "", props, "issues", props, "issues_index", rows=4
        )
        if 0 <= props.issues_index < len(props.issues):
            item = props.issues[props.issues_index]
            box = layout.box()
            _wrapped(box, item.message, icon=SEVERITY_ICONS[item.severity])
            box.label(text=f"Code: {item.code}")


# --- Build profiles --------------------------------------------------------------------


class GEOTOOLS_PT_build_profiles(_GeoToolsPanel, bpy.types.Panel):
    """Saved build settings (used by publishing, M2)."""

    bl_idname = "GEOTOOLS_PT_build_profiles"
    bl_label = "Build Profiles"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        row = layout.row()
        row.template_list(
            "GEOTOOLS_UL_build_profiles",
            "",
            props,
            "build_profiles",
            props,
            "build_profile_index",
            rows=3,
        )
        col = row.column(align=True)
        col.operator("geotools.build_profile_add", text="", icon="ADD")
        col.operator("geotools.build_profile_remove", text="", icon="REMOVE")
        col.separator()
        col.operator("geotools.build_profile_move", text="", icon="TRIA_UP").direction = "UP"
        col.operator("geotools.build_profile_move", text="", icon="TRIA_DOWN").direction = "DOWN"

        if not 0 <= props.build_profile_index < len(props.build_profiles):
            return
        profile = props.build_profiles[props.build_profile_index]
        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(profile, "profile")
        col.prop(profile, "jobs", text="Jobs (0 = Auto)")
        col.prop(profile, "memory_budget_gib", text="Memory GiB (0 = Default)")
        col.prop(profile, "output_path")


# --- About ---------------------------------------------------------------------------


class GEOTOOLS_PT_about(_GeoToolsPanel, bpy.types.Panel):
    """Extension version and dependency status."""

    bl_idname = "GEOTOOLS_PT_about"
    bl_label = "About"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text=f"Version {extension_version()}")
        draw_dependency_status(layout)


# Registration order is the sidebar order; About stays last.
classes = (
    GEOTOOLS_PT_project,
    GEOTOOLS_PT_project_verify,
    GEOTOOLS_PT_source,
    GEOTOOLS_PT_checks,
    GEOTOOLS_PT_build_profiles,
    GEOTOOLS_PT_about,
)

register, unregister = bpy.utils.register_classes_factory(classes)
