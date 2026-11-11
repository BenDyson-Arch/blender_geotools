"""View3D sidebar panels, category "GeoTools".

Order: Project (with a Verify sub-panel), Source, Layers, Checks (with an
Images sub-panel), Build Profiles, Publish (defined in publish.py), and About
last. Panels draw cached state only; operators do the work. The exception is
the Layers panel's status, which ``layers.layer_status`` works out per object
(never per face) while drawing.
"""

import textwrap
from pathlib import Path

import bpy

from ..core import crs_presets, deps, sysmem
from ..core.issues import Severity
from ..core.layers import default_aggregation, validate
from .common import draw_dependency_status, extension_version
from .layers import layer_status, other_layer_names, spec_from_props
from .properties import (
    ANCHOR_FIELDS,
    LAYER_AGGREGATION_DEFAULT,
    SEVERITY_ICONS,
    SOURCE_CRS_FIELDS,
    FieldError,
    NumberField,
    number_is_valid,
    parse_vec,
    placement_from_props,
)
from .publish import GEOTOOLS_PT_publish
from .remedies import describe_image, draw_remedy_buttons

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


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
            row.operator("geotools.apply_crs_preset", text="", icon="PRESET")
            preset = crs_presets.for_definition(props.source_crs)
            if preset is not None:
                _wrapped(layout, f"{preset.label} ({preset.epsg}). {preset.note}", icon="INFO")
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


# --- Layers --------------------------------------------------------------------------

LAYER_STATUS_ROWS = 6
"""Issues the Layers panel lists before summarising the rest."""


def _draw_layer_status(layout: bpy.types.UILayout, context: bpy.types.Context, item) -> None:
    issues = sorted(
        layer_status(context.scene, item), key=lambda i: _SEVERITY_ORDER[Severity(i.severity)]
    )
    box = layout.box()
    if not issues:
        box.label(text="Stored on every source object", icon="CHECKMARK")
        return
    for issue in issues[:LAYER_STATUS_ROWS]:
        col = box.column(align=True)
        col.alert = issue.severity is Severity.ERROR
        _wrapped(col, issue.message, icon=SEVERITY_ICONS[Severity(issue.severity).name])
    if len(issues) > LAYER_STATUS_ROWS:
        box.label(text=f"... and {len(issues) - LAYER_STATUS_ROWS} more")


class GEOTOOLS_PT_layers(_GeoToolsPanel, bpy.types.Panel):
    """The layer registry: named values per face, corner or texel of the source."""

    bl_idname = "GEOTOOLS_PT_layers"
    bl_label = "Layers"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        row = layout.row()
        row.template_list("GEOTOOLS_UL_layers", "", props, "layers", props, "layers_index", rows=3)
        col = row.column(align=True)
        col.operator("geotools.layer_add", text="", icon="ADD")
        col.operator("geotools.layer_remove", text="", icon="REMOVE")

        if not 0 <= props.layers_index < len(props.layers):
            if not len(props.layers):
                layout.label(text="No layers yet", icon="INFO")
            return
        item = props.layers[props.layers_index]
        row = layout.row(align=True)
        row.label(text=item.name, icon="RNA")
        row.operator("geotools.layer_rename", text="", icon="GREASEPENCIL")

        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(item, "domain")
        col.prop(item, "data_type")
        col.prop(item, "units")
        spec = spec_from_props(item)
        issues = validate(spec, other_layer_names(context.scene, item))
        nodata = col.row()
        nodata.alert = any(i.code == "layer-nodata-invalid" for i in issues)
        nodata.prop(item, "nodata")
        col.prop(item, "origin")
        agg = col.row()
        agg.alert = any(i.code == "layer-aggregation-invalid" for i in issues)
        agg.prop(item, "aggregation")
        if item.aggregation == LAYER_AGGREGATION_DEFAULT:
            text = f"Default: {_aggregation_label(item, spec.data_type)}"
            col.label(text=text, icon="INFO")
        col.prop(item, "description")

        if item.data_type == "CATEGORY":
            box = layout.box()
            header = box.row()
            header.label(text="Categories")
            header.operator("geotools.layer_set_categories", text="", icon="GREASEPENCIL")
            labels = spec.categories
            if not labels:
                box.label(text="None set", icon="ERROR")
            sub = box.column(align=True)
            for value, label in enumerate(labels[:16]):
                sub.label(text=f"{value}  {label}")
            if len(labels) > 16:
                sub.label(text=f"... and {len(labels) - 16} more")

        if item.domain == "TEXTURE":
            col = layout.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(item, "uv_map")
            col.prop(item, "resolution")
            col.prop(item, "margin")
            count = sum(1 for entry in item.images if entry.image is not None)
            col.label(text=f"{count} image{'s' * (count != 1)}", icon="IMAGE_DATA")
        else:
            layout.operator("geotools.layer_create_attributes", icon="ADD")

        _draw_layer_status(layout, context, item)


def _aggregation_label(item, data_type: str) -> str:
    value = default_aggregation(data_type).value
    return item.bl_rna.properties["aggregation"].enum_items[value].name


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
            draw_remedy_buttons(box, item.code, item.subject)


class GEOTOOLS_PT_checks_images(_GeoToolsPanel, bpy.types.Panel):
    """Source images as last summarised, with their remedy."""

    bl_idname = "GEOTOOLS_PT_checks_images"
    bl_label = "Images"
    bl_parent_id = "GEOTOOLS_PT_checks"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        if not props.summary_valid:
            layout.label(text="Run Checks to list the source images")
            return
        if not len(props.summary_images):
            layout.label(text="The source uses no images")
            return
        layout.template_list(
            "GEOTOOLS_UL_images", "", props, "summary_images", props, "summary_images_index", rows=3
        )
        if not 0 <= props.summary_images_index < len(props.summary_images):
            return
        item = props.summary_images[props.summary_images_index]
        image = bpy.data.images.get(item.name)
        box = layout.box()
        box.label(text=describe_image(item))
        if image is None:
            box.label(text="The image no longer exists; run Checks again", icon="ERROR")
            return
        remedy = image.geotools_remedy
        col = box.column(align=True)
        col.enabled = image.library is None
        col.prop(remedy, "kind", text="Remedy")
        if remedy.kind in {"JPEG", "DOWNSCALE"}:
            col.prop(remedy, "quality")
        if remedy.kind == "DOWNSCALE":
            col.prop(remedy, "max_edge")
        if image.library is not None:
            box.label(
                text="Linked image; make it local to choose a remedy", icon="LIBRARY_DATA_DIRECT"
            )
        elif remedy.kind != "NONE":
            box.label(text="Only the published copy changes", icon="INFO")


# --- Build profiles --------------------------------------------------------------------


class GEOTOOLS_PT_build_profiles(_GeoToolsPanel, bpy.types.Panel):
    """Saved build settings, used by Publish."""

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
        col.prop(profile, "memory_budget_gib", text="Memory GiB (0 = Auto)")
        if profile.memory_budget_gib <= 0:
            available = sysmem.available_memory_bytes()
            budget = sysmem.auto_memory_budget(available)
            if budget is None:
                col.label(text="Auto: rusty-tiles default (memory unknown)", icon="INFO")
            else:
                col.label(
                    text=f"Auto: {budget / sysmem.GIB:.1f} GiB of "
                    f"{available / sysmem.GIB:.1f} GiB available",
                    icon="INFO",
                )
        col.prop(profile, "output_path")
        col.prop(profile, "overwrite")
        col.prop(profile, "validate")


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
    GEOTOOLS_PT_layers,
    GEOTOOLS_PT_checks,
    GEOTOOLS_PT_checks_images,
    GEOTOOLS_PT_build_profiles,
    GEOTOOLS_PT_publish,
    GEOTOOLS_PT_about,
)

register, unregister = bpy.utils.register_classes_factory(classes)
