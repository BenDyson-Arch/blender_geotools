"""View3D sidebar panels, category "GeoTools".

The panels follow the workflow top to bottom (set up the project, bring data
in, analyse, look after the layers and textures, check, publish, inspect the
output), and each header shows its state (``ui.STATUS_ICONS``: done, warning,
error, not set up yet). docs/design/ui-structure.md has the tree and the naming
rules.

- Project: where the source sits on Earth, and Verify Placement.
  - CRS Details: the CRS's area of use, definition and datum operation.
- Source: the source collection, Import Source Mesh (with an offset file it did
  not apply, and its buttons) and the summary.
- Linked Data: GeoJSON, GeoPackage and GeoTIFF files linked to objects
  (``links_ui``).
- Analysis (``analysis_ui``): Measure, Presets, Hydrology, Contours and
  Sections, and Sun (with Sun Hours and Shadow Study), then Archaeoastronomy.
- Layers: the layer registry and the selected layer at a glance, with the
  attribute a face or corner layer is read from (a Geometry Nodes output,
  for one).
  - Bake: a Cycles bake into a texture layer's images (texture layers only).
  - Display: the colours of the layer's variant tileset.
  - Details: no-data, origin, aggregation and the other rarely changed fields.
- Textures (``texture_ui``): paint setup with UV-overlap stencil, and the M6
  cleanup tools (fill holes, remove masked objects, pad islands, balance pages).
- Checks: preflight issues and their fixes.
  - Images: the source images and their remedies.
- Publish: build profiles, format (.3tz, the metadata .glb or Vectors), output,
  variant (or, for Vectors, the vector collection), the GLB options (Face
  Table, Earth-Placed) and the Publish (or Export) button.
  - Build Settings: Apply Modifiers, rusty-tiles quality, threads and memory.
  - Last Build: what the last publish wrote, and Preview (.3tz only).
- Inspect Output: open published tiles over the source (``inspect_ui``).
  - Trace to Source: output faces to source faces and back.
  - Quality: distance to the source, texel density and levels compared.
- About: version and dependencies.

Registration order is the sidebar order. Panels draw cached state only and
never evaluate a mesh; operators do the work. A layer's status is
``layers.cached_status``, which the layer operators, Run Checks and Publish
refresh.
"""

from __future__ import annotations

import bpy

from ..core import crs_catalog, deps, paths, sysmem
from ..core.display import format_count, format_gib, format_range, format_size
from ..core.display import plural as plural_text
from ..core.issues import Severity
from ..core.layers import (
    DataType,
    Domain,
    attribute_name,
    default_aggregation,
    parse_number,
    validate,
)
from . import (
    analysis_ui,
    bake_texture,
    crs_picker,
    inspect_ui,
    links_ui,
    presentation,
    publish,
    scene_georef,
    source,
    texture_state,
    texture_ui,
    vector_publish,
)
from .common import draw_dependency_status, extension_version
from .layers import (
    ATTRIBUTE_MISSING,
    DOMAIN_ICONS,
    IMAGE_MISSING,
    NOT_CHECKED,
    cached_status,
    other_layer_names,
    spec_from_props,
)
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
from .remedies import describe_image, draw_remedy_buttons
from .ui import (
    DONE,
    ERROR,
    TODO,
    WARNING,
    big_button,
    header_status,
    info_row,
    split_column,
    wrapped,
)

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


class _GeoToolsPanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


def _number_rows(
    layout: bpy.types.UILayout,
    props: bpy.types.PropertyGroup,
    fields: tuple[NumberField, ...],
    labels: tuple[str, ...],
) -> None:
    """Binary64 text fields in an aligned block, each flagged if it does not parse."""
    col = layout.column(align=True)
    for field, label in zip(fields, labels, strict=True):
        row = col.row(align=True)
        row.alert = not number_is_valid(props, field.attr)
        row.prop(props, field.attr, text=label)


def _active_layer(context: bpy.types.Context):
    props = context.scene.geotools if context.scene is not None else None
    if props is not None and 0 <= props.layers_index < len(props.layers):
        return props.layers[props.layers_index]
    return None


_NOT_FILLED = {ATTRIBUTE_MISSING, IMAGE_MISSING}


def layer_state(scene: bpy.types.Scene, item) -> str:
    """A layer's header state from its cached status: ERROR, WARNING, TODO (no
    values yet, or not checked yet) or DONE."""
    issues = cached_status(scene, item)
    state = _worst(issues)
    if state == ERROR:
        return state
    if any(i.code == NOT_CHECKED for i in issues):
        return TODO
    if state == WARNING and all(
        i.code in _NOT_FILLED for i in issues if _severity(i) is Severity.WARNING
    ):
        return TODO
    return state


def _severity(issue) -> Severity:
    """An issue's severity: a ``Severity``, or its name as stored on ``GeoToolsIssue``."""
    value = issue.severity
    return value if isinstance(value, Severity) else Severity[value]


def _worst(issues) -> str:
    severities = {_severity(i) for i in issues}
    if Severity.ERROR in severities:
        return ERROR
    if Severity.WARNING in severities:
        return WARNING
    return DONE


# --- Project -------------------------------------------------------------------------


def placement_state(props: bpy.types.PropertyGroup) -> str:
    """Project header state: ERROR if the placement cannot be built or failed to verify."""
    if props.placement_mode == "LOCAL":
        return DONE
    if props.placement_mode == "SOURCE_CRS" and not props.crs.strip():
        return ERROR
    try:
        placement = placement_from_props(props)
    except FieldError:
        return ERROR
    key = repr(placement)
    resolved = crs_catalog.resolve(placement.source_crs) if placement.source_crs else None
    if resolved is not None and not resolved.accepted:
        return ERROR
    if props.probe_status == "FAILED" and props.probe_key == key:
        return ERROR
    return DONE


class GEOTOOLS_PT_project(_GeoToolsPanel, bpy.types.Panel):
    """Placement of the scene on Earth."""

    bl_idname = "GEOTOOLS_PT_project"
    bl_label = "Project"

    def draw_header(self, context: bpy.types.Context) -> None:
        header_status(self.layout, placement_state(context.scene.geotools))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        layout.row().prop(props, "placement_mode", expand=True)

        mode = props.placement_mode
        if mode == "LOCAL":
            wrapped(layout, context, "Local metres: the output is not placed on Earth.", "INFO")
        elif mode == "ANCHOR":
            col = split_column(layout)
            _number_rows(col, props, ANCHOR_FIELDS[:3], ("Longitude", "Latitude", "Height"))
            _number_rows(col, props, ANCHOR_FIELDS[3:4], ("Heading",))
            _number_rows(col, props, ANCHOR_FIELDS[4:], ("Offset E", "N", "U"))
        else:
            _draw_source_crs(layout, context, props)
        scene_georef.draw_notes(layout, context, props)
        layout.separator()
        _draw_verify(layout, context, props)


def _draw_source_crs(layout, context, props) -> None:
    crs_picker.draw_source_crs(layout, context, props)
    col = split_column(layout)
    _number_rows(col, props, SOURCE_CRS_FIELDS[:3], ("Offset E", "N", "A"))
    scene_georef.draw_storage(layout, context, props)
    _number_rows(col, props, SOURCE_CRS_FIELDS[3:], ("Height Offset",))
    row = layout.row(align=True)
    row.operator("geotools.load_offset_file", icon="FILEBROWSER")
    if props.offset_file:
        layout.label(text=f"From {paths.name(props.offset_file)}", icon="TEXT")


def _draw_verify(layout, context, props) -> None:
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
        info_row(col, "Longitude", f"{float(props.probe_lon):.7f}°")
        info_row(col, "Latitude", f"{float(props.probe_lat):.7f}°")
        info_row(col, "Height", f"{float(props.probe_height):,.3f} m")
        if props.probe_operation:
            wrapped(col, context, props.probe_operation, icon="INFO")
    else:
        col = box.column(align=True)
        col.alert = True
        wrapped(col, context, props.probe_error or "Placement failed", icon="ERROR")
    if props.probe_key and current != props.probe_key:
        box.label(text="Placement changed; verify again", icon="FILE_REFRESH")


class GEOTOOLS_PT_project_crs(_GeoToolsPanel, bpy.types.Panel):
    """The CRS's area of use, the definition rusty-tiles uses and its datum operation."""

    bl_idname = "GEOTOOLS_PT_project_crs"
    bl_label = "CRS Details"
    bl_parent_id = "GEOTOOLS_PT_project"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        props = context.scene.geotools if context.scene is not None else None
        return props is not None and crs_picker.details_poll(props)

    def draw(self, context: bpy.types.Context) -> None:
        crs_picker.draw_details(self.layout, context, context.scene.geotools)


# --- Source --------------------------------------------------------------------------


def source_state(props: bpy.types.PropertyGroup) -> str:
    if props.source_collection is None or not props.summary_valid:
        return TODO
    return DONE if props.summary_meshes else ERROR


class GEOTOOLS_PT_source(_GeoToolsPanel, bpy.types.Panel):
    """The source collection, ingest and summary."""

    bl_idname = "GEOTOOLS_PT_source"
    bl_label = "Source"

    def draw_header(self, context: bpy.types.Context) -> None:
        header_status(self.layout, source_state(context.scene.geotools))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        if props.source_collection is None:
            wrapped(
                layout, context, "Choose a source collection, or use Import Source Mesh.", "INFO"
            )
        col = split_column(layout)
        col.prop(props, "source_collection", text="Collection")
        layout.operator("geotools.ingest_source", text="Import Source Mesh...", icon="IMPORT")
        if props.source_filepath:
            layout.label(text=f"From {paths.name(props.source_filepath)}", icon="FILE")
        source.draw_offset_offer(layout, context)

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
        meshes = plural_text(props.summary_meshes, "mesh", "meshes")
        info_row(col, "Objects", f"{format_count(props.summary_objects)} ({meshes})")
        info_row(col, "Faces", format_count(props.summary_faces))
        info_row(col, "Vertices", format_count(props.summary_vertices))
        low, high = parse_vec(props.summary_bounds_min), parse_vec(props.summary_bounds_max)
        if low is not None and high is not None:
            size = tuple(h - lo for lo, h in zip(low, high, strict=True))
            info_row(col, "Size", format_size(size))  # type: ignore[arg-type]
        else:
            col.label(text="No meshes", icon="ERROR")


# --- Layers --------------------------------------------------------------------------

LAYER_STATUS_ROWS = 3
"""Issues the Layers panel lists before summarising the rest."""


def _values_text(item) -> str:
    low, high = parse_number(item.value_min), parse_number(item.value_max)
    if item.data_type == DataType.BOOLEAN.value and low is not None and high is not None:
        return {(0.0, 0.0): "all false", (1.0, 1.0): "all true"}.get((low, high), "false, true")
    return format_range(low, high, item.units.strip()) or "Not known"


def _draw_layer_status(layout: bpy.types.UILayout, context: bpy.types.Context, issues) -> None:
    issues = sorted(issues, key=lambda i: _SEVERITY_ORDER[Severity(i.severity)])
    if not issues:
        layout.label(text="Stored on every source object", icon="CHECKMARK")
        return
    col = layout.column(align=True)
    for issue in issues[:LAYER_STATUS_ROWS]:
        sub = col.column(align=True)
        sub.alert = issue.severity is Severity.ERROR
        wrapped(sub, context, issue.message, icon=SEVERITY_ICONS[Severity(issue.severity).name])
    if len(issues) > LAYER_STATUS_ROWS:
        col.label(text=f"... and {len(issues) - LAYER_STATUS_ROWS} more", icon="BLANK1")


class GEOTOOLS_PT_layers(_GeoToolsPanel, bpy.types.Panel):
    """The layer registry: named values per face, corner or texel of the source."""

    bl_idname = "GEOTOOLS_PT_layers"
    bl_label = "Layers"

    def draw_header(self, context: bpy.types.Context) -> None:
        item = _active_layer(context)
        if item is not None:
            header_status(self.layout, layer_state(context.scene, item))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        row = layout.row()
        row.template_list("GEOTOOLS_UL_layers", "", props, "layers", props, "layers_index", rows=3)
        col = row.column(align=True)
        col.operator("geotools.layer_add", text="", icon="ADD")
        col.operator("geotools.layer_remove", text="", icon="REMOVE")
        col.separator()
        col.operator("geotools.layer_rename", text="", icon="GREASEPENCIL")

        item = _active_layer(context)
        if item is None:
            if not len(props.layers):
                wrapped(
                    layout,
                    context,
                    "No layers yet. Add one to publish values per face, corner or texel.",
                    "INFO",
                )
            return

        col = split_column(layout)
        col.prop(item, "label", placeholder=item.name)
        col.prop(item, "units", placeholder="None")
        if item.domain != Domain.TEXTURE.value:
            row = col.row()
            row.alert = "layer-attribute-invalid" in {
                i.code for i in validate(spec_from_props(item), ())
            }
            row.prop(item, "source_attribute", placeholder=attribute_name(item.name))
        if item.data_type == DataType.CATEGORY.value:
            count = len(spec_from_props(item).categories)
            row = col.row()
            row.alert = not count
            value = info_row(row, "Categories", f"{count}" if count else "None set")
            value.operator("geotools.layer_set_categories", text="", icon="GREASEPENCIL")
        else:
            value = info_row(col, "Values", _values_text(item))
            value.operator("geotools.layer_update_range", text="", icon="FILE_REFRESH")
        _draw_layer_status(layout, context, cached_status(context.scene, item))


def _bake_state(context: bpy.types.Context, item) -> str:
    if context.scene.geotools.source_collection is None:
        return TODO
    issues = cached_status(context.scene, item)
    return TODO if any(i.code in _NOT_FILLED | {NOT_CHECKED} for i in issues) else DONE


class GEOTOOLS_PT_layers_bake(_GeoToolsPanel, bpy.types.Panel):
    """Fill the selected texture layer with a Cycles bake. Face and corner layers need no
    bake: they read their attribute from the evaluated mesh."""

    bl_idname = "GEOTOOLS_PT_layers_bake"
    bl_label = "Bake"
    bl_parent_id = "GEOTOOLS_PT_layers"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        item = _active_layer(context)
        return item is not None and item.domain == Domain.TEXTURE.value

    def draw_header(self, context: bpy.types.Context) -> None:
        item = _active_layer(context)
        if item is not None:
            header_status(self.layout, _bake_state(context, item))

    def draw(self, context: bpy.types.Context) -> None:
        item = _active_layer(context)
        if item is None:
            return
        if context.scene.geotools.source_collection is None:
            wrapped(self.layout, context, "Choose a source collection first.", "INFO")
            return
        bake_texture.draw_bake(self.layout, context, item)


class GEOTOOLS_PT_layers_display(_GeoToolsPanel, bpy.types.Panel):
    """How the selected layer is coloured in its variant tileset."""

    bl_idname = "GEOTOOLS_PT_layers_display"
    bl_label = "Display"
    bl_parent_id = "GEOTOOLS_PT_layers"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_layer(context) is not None

    def draw_header(self, context: bpy.types.Context) -> None:
        item = _active_layer(context)
        if item is not None and presentation.display_issue_codes(item):
            header_status(self.layout, ERROR)

    def draw(self, context: bpy.types.Context) -> None:
        item = _active_layer(context)
        if item is not None:
            presentation.draw_display(self.layout, context, item)


def _aggregation_label(item, data_type: str) -> str:
    value = default_aggregation(data_type).value
    return item.bl_rna.properties["aggregation"].enum_items[value].name


class GEOTOOLS_PT_layers_details(_GeoToolsPanel, bpy.types.Panel):
    """The selected layer's rarely changed fields."""

    bl_idname = "GEOTOOLS_PT_layers_details"
    bl_label = "Details"
    bl_parent_id = "GEOTOOLS_PT_layers"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_layer(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = _active_layer(context)
        if item is None:
            return
        spec = spec_from_props(item)
        codes = {i.code for i in validate(spec, other_layer_names(context.scene, item))}
        col = split_column(layout)
        info_row(col, "Identifier", item.name, icon=DOMAIN_ICONS.get(item.domain, "NONE"))
        col.prop(item, "domain")
        col.prop(item, "data_type")
        row = col.row()
        row.alert = "layer-nodata-invalid" in codes
        row.prop(item, "nodata", placeholder="None")
        col.prop(item, "origin")
        row = col.row()
        row.alert = "layer-aggregation-invalid" in codes
        row.prop(item, "aggregation")
        if item.aggregation == LAYER_AGGREGATION_DEFAULT:
            info_row(col, "", f"{_aggregation_label(item, spec.data_type)}", icon="INFO")
        # Full width: a description is longer than the value column.
        col.label(text="Description")
        col.prop(item, "description", text="")

        if "layer-range-invalid" in codes:
            # The range is worked out by Update Range; show the stored text only to fix it.
            values = col.column(align=True)
            values.alert = True
            values.prop(item, "value_min", text="Range Min")
            values.prop(item, "value_max", text="Max")

        provenance = spec.provenance
        if "layer-provenance-invalid" in codes:
            layout.label(text="Provenance is not valid JSON", icon="ERROR")
        elif provenance is not None:
            version = provenance.get("geotools_version") or "unknown version"
            wrapped(layout, context, f"Made by {provenance['tool']} (GeoTools {version})", "INFO")
        if item.domain != Domain.TEXTURE.value:
            layout.operator("geotools.layer_create_attributes")


# --- Checks --------------------------------------------------------------------------


def checks_state(props: bpy.types.PropertyGroup) -> str:
    if not props.checks_run:
        return TODO
    return _worst(props.issues)


class GEOTOOLS_PT_checks(_GeoToolsPanel, bpy.types.Panel):
    """Preflight issues against rusty-tiles' input rules."""

    bl_idname = "GEOTOOLS_PT_checks"
    bl_label = "Checks"

    def draw_header(self, context: bpy.types.Context) -> None:
        header_status(self.layout, checks_state(context.scene.geotools))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        big_button(layout, 1.2).operator("geotools.run_checks", icon="CHECKMARK")
        if not props.checks_run:
            wrapped(layout, context, "Publish runs them too; run them here to fix issues early.")
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
            "GEOTOOLS_UL_issues", "", props, "issues", props, "issues_index", rows=3
        )
        if 0 <= props.issues_index < len(props.issues):
            item = props.issues[props.issues_index]
            box = layout.box()
            wrapped(box, context, item.message, icon=SEVERITY_ICONS[item.severity])
            box.label(text=f"Code: {item.code}", icon="BLANK1")
            draw_remedy_buttons(box, item.code, item.subject)
            scene_georef.draw_remedy(box, item.code)


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
            layout.label(text="Run Checks to list the images")
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
            wrapped(box, context, "The image no longer exists; run Checks again.", "ERROR")
            return
        remedy = image.geotools_remedy
        col = split_column(box)
        col.enabled = image.library is None
        col.prop(remedy, "kind", text="Remedy")
        if remedy.kind in {"JPEG", "DOWNSCALE"}:
            col.prop(remedy, "quality", text="Quality")
        if remedy.kind == "DOWNSCALE":
            col.prop(remedy, "max_edge")
        if image.library is not None:
            wrapped(
                box,
                context,
                "Linked image: make it local to choose a remedy.",
                "LIBRARY_DATA_DIRECT",
            )
        elif remedy.kind != "NONE":
            box.label(text="Only the published copy changes", icon="INFO")


# --- Publish ---------------------------------------------------------------------------


def _active_profile(context: bpy.types.Context):
    return publish.active_profile(context.scene) if context.scene is not None else None


class GEOTOOLS_PT_publish(_GeoToolsPanel, bpy.types.Panel):
    """Publish the source as a .3tz, or a variant of one layer, with a build profile."""

    bl_idname = "GEOTOOLS_PT_publish"
    bl_label = "Publish"

    def draw_header(self, context: bpy.types.Context) -> None:
        header_status(self.layout, publish.publish_state(context))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = context.scene.geotools
        status_ = publish.status(context)
        running = publish.is_running()

        if not len(props.build_profiles):
            wrapped(
                layout, context, "Add a build profile: it says where and how to publish.", "INFO"
            )
            big_button(layout).operator(
                "geotools.build_profile_add", text="Add Build Profile", icon="ADD"
            )
            return

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

        profile = _active_profile(context)
        if profile is None:
            return
        glb = publish.is_glb(profile)
        suffix = publish.profile_suffix(profile)
        path = profile.output_path.strip()
        wrong_suffix = bool(path) and not path.endswith(suffix)
        # Labelled above: ".3tz .glb Vectors" and a path do not fit beside a label.
        col = layout.column(align=True)
        col.label(text="Format")
        col.row().prop(profile, "output_type", expand=True)
        col = layout.column(align=True)
        col.label(text="Output")
        row = col.row(align=True)
        row.alert = not path or wrong_suffix
        row.prop(profile, "output_path", text="", placeholder=f"//site{suffix}")
        col = split_column(layout)
        if vector_publish.is_vector(profile):
            vector_publish.draw_profile(col, context, profile)
        else:
            row = col.row()
            variant = profile.variant_layer.strip()
            row.alert = bool(variant) and props.layers.get(variant) is None
            row.prop_search(profile, "variant_layer", props, "layers", icon="RNA")
        row = layout.row(align=True)
        row.prop(profile, "overwrite")
        if glb:
            row.prop(profile, "glb_face_table")
            layout.prop(profile, "glb_earth_placed")
        else:
            row.prop(profile, "validate")

        name = publish.output_name(context.scene, profile)
        if paths.is_blend_relative(profile.output_path.strip()) and not bpy.data.is_saved:
            layout.label(text="Save the .blend first (// path)", icon="ERROR")
        rusty = deps.probe_rusty_tiles().available
        if not rusty and not glb:
            layout.label(text="Needs rusty-tiles (see About)", icon="ERROR")
        elif not rusty and profile.glb_earth_placed:
            layout.label(text="Earth-placed needs rusty-tiles", icon="ERROR")

        if not vector_publish.is_vector(profile):
            texture_state.draw_changes(layout, context, profile)
        if running and status_ is not None:
            publish.draw_progress(layout, context, status_)
            return
        verb = "Export" if glb else "Publish"
        button = big_button(layout, 1.6)
        button.enabled = bool(name)
        button.operator("geotools.publish", text=f"{verb} {name}" if name else verb, icon="EXPORT")
        if wrong_suffix:
            layout.label(text=f"The output must end in {suffix}", icon="INFO")
        elif not name:
            layout.label(text="Set the output path", icon="INFO")
        publish.draw_end_status(layout, context, status_)


class GEOTOOLS_PT_publish_settings(_GeoToolsPanel, bpy.types.Panel):
    """rusty-tiles settings of the selected build profile."""

    bl_idname = "GEOTOOLS_PT_publish_settings"
    bl_label = "Build Settings"
    bl_parent_id = "GEOTOOLS_PT_publish"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_profile(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        profile = _active_profile(context)
        if profile is None:
            return
        col = split_column(self.layout)
        col.prop(profile, "apply_modifiers")
        if vector_publish.is_vector(profile):
            vector_publish.draw_settings(col, context, profile)
            return
        if publish.is_glb(profile):
            wrapped(
                self.layout,
                context,
                "Quality, threads and memory are rusty-tiles settings, used by .3tz builds only.",
                "INFO",
            )
            return
        col.prop(profile, "profile")
        col.prop(profile, "jobs")
        col.prop(profile, "memory_budget_gib", text="Memory GiB")
        if profile.memory_budget_gib <= 0:
            available = sysmem.available_memory_bytes()
            budget = sysmem.auto_memory_budget(available)
            if budget is None:
                text = "Auto memory: rusty-tiles default"
            else:
                gib = format_gib(budget).removesuffix(" GiB")
                text = f"Auto memory: {gib} of {format_gib(available)}"
            self.layout.label(text=text, icon="INFO")


class GEOTOOLS_PT_publish_last(_GeoToolsPanel, bpy.types.Panel):
    """What the selected profile last published, and a local preview of it."""

    bl_idname = "GEOTOOLS_PT_publish_last"
    bl_label = "Last Build"
    bl_parent_id = "GEOTOOLS_PT_publish"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        profile = _active_profile(context)
        return profile is not None and bool(profile.last_output)

    def draw_header(self, context: bpy.types.Context) -> None:
        profile = _active_profile(context)
        if profile is not None and profile.last_validation == "FAILED":
            header_status(self.layout, ERROR)

    def draw(self, context: bpy.types.Context) -> None:
        profile = _active_profile(context)
        if profile is None:
            return
        publish.draw_last_build(self.layout, context, profile)
        if not profile.last_output.endswith(".glb"):
            publish.draw_preview(self.layout, context)


# --- About ---------------------------------------------------------------------------


class GEOTOOLS_PT_about(_GeoToolsPanel, bpy.types.Panel):
    """Extension version and dependency status."""

    bl_idname = "GEOTOOLS_PT_about"
    bl_label = "About"
    bl_options = {"DEFAULT_CLOSED"}

    def draw_header(self, context: bpy.types.Context) -> None:
        if not deps.probe_rusty_tiles().available:
            state = ERROR
        elif not deps.probe_pyproj().available:
            state = WARNING  # everything but reprojecting imports still works
        else:
            state = DONE
        header_status(self.layout, state)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text=f"Version {extension_version()}")
        draw_dependency_status(layout)


# Registration order is the sidebar order; About stays last.
classes = (
    GEOTOOLS_PT_project,
    GEOTOOLS_PT_project_crs,
    GEOTOOLS_PT_source,
    *links_ui.classes,
    *analysis_ui.classes,
    GEOTOOLS_PT_layers,
    GEOTOOLS_PT_layers_bake,
    GEOTOOLS_PT_layers_display,
    GEOTOOLS_PT_layers_details,
    *texture_ui.classes,
    GEOTOOLS_PT_checks,
    GEOTOOLS_PT_checks_images,
    GEOTOOLS_PT_publish,
    GEOTOOLS_PT_publish_settings,
    GEOTOOLS_PT_publish_last,
    *inspect_ui.classes,
    GEOTOOLS_PT_about,
)

register, unregister = bpy.utils.register_classes_factory(classes)
