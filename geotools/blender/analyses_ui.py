"""Sidebar panels of the Analyses list (``analyses``): a mesh's analyses and layers.

- Analyses: the Add Analysis search field (``analyses.search_entries``; it
  adds one result to each selected mesh) and the active object's results
  (``GEOTOOLS_UL_analyses``), like its modifier stack. With no active mesh,
  one muted line and the Add field, greyed out until a mesh is selected.
  The header shows the active row's layer state. For the active row:
  - Inputs: a preset's modifier inputs, drawn from the modifier itself; a
    Fast Bake's or a tool's own inputs, its Surface, and Compute (out of date
    once they change); a plain layer's label, units, attribute and values,
    and the Bake box for a texture layer.
  - Display: the layer's colours and Show in Viewport (``presentation``).
  - Details: the layer's rarely changed fields, Rename Layer and Create
    Empty Attributes.
  - Outputs: the layers and objects the result made, and Publish as Variant.

Panels draw stored state only: the modifier inputs and the layer status are
RNA reads (``layers.cached_status``), never an evaluation.
"""

from __future__ import annotations

import json

import bpy

from ..analysis import preset_bake as fast
from ..analysis import presets as catalog
from ..core.display import format_range
from ..core.issues import Severity
from ..core.layers import (
    DataType,
    Domain,
    attribute_name,
    default_aggregation,
    parse_number,
    validate,
)
from . import analyses, analysis_presets, bake_texture
from .layers import (
    ATTRIBUTE_MISSING,
    DOMAIN_ICONS,
    IMAGE_MISSING,
    NOT_CHECKED,
    cached_status,
    find_layer,
    other_layer_names,
    spec_from_props,
)
from .presentation import display_issue_codes, draw_display
from .properties import LAYER_AGGREGATION_DEFAULT, SEVERITY_ICONS
from .ui import (
    ERROR,
    TODO,
    WARNING,
    big_button,
    header_status,
    info_row,
    issue_severity,
    split_column,
    worst_state,
    wrapped,
)


class _Panel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


def _item(context: bpy.types.Context):
    """The active row when it belongs to the active mesh: what the sub-panels draw."""
    return analyses.shown_item(context)


def _error(layout, context, text: str) -> None:
    col = layout.column()
    col.alert = True
    wrapped(col, context, text, "ERROR")


def _muted(layout, text: str, icon: str = "NONE") -> None:
    row = layout.row()
    row.active = False
    row.label(text=text, icon=icon)


# --- a layer at a glance ---------------------------------------------------------------


_NOT_FILLED = {ATTRIBUTE_MISSING, IMAGE_MISSING}
_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

LAYER_STATUS_ROWS = 3
"""Issues the Inputs panel lists before summarising the rest."""


def layer_state(scene: bpy.types.Scene, item) -> str:
    """A layer's header state from its cached status: ERROR, WARNING, TODO (no values
    yet, or not checked yet) or DONE."""
    issues = cached_status(scene, item)
    state = worst_state(issues)
    if state == ERROR:
        return state
    if any(i.code == NOT_CHECKED for i in issues):
        return TODO
    if state == WARNING and all(
        i.code in _NOT_FILLED for i in issues if issue_severity(i) is Severity.WARNING
    ):
        return TODO
    return state


def _values_text(item) -> str:
    low, high = parse_number(item.value_min), parse_number(item.value_max)
    if item.data_type == DataType.BOOLEAN.value and low is not None and high is not None:
        return {(0.0, 0.0): "all false", (1.0, 1.0): "all true"}.get((low, high), "false, true")
    return format_range(low, high, item.units.strip()) or "Not known"


def _draw_layer_status(layout, context, issues) -> None:
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


def draw_layer_fields(layout: bpy.types.UILayout, context: bpy.types.Context, item) -> None:
    """A layer at a glance: label, units, attribute, values and status."""
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


def _aggregation_label(item, data_type: str) -> str:
    value = default_aggregation(data_type).value
    return item.bl_rna.properties["aggregation"].enum_items[value].name


def draw_layer_details(layout: bpy.types.UILayout, context: bpy.types.Context, item) -> None:
    """A layer's rarely changed fields: identifier, domain, type, no-data, origin, ..."""
    spec = spec_from_props(item)
    codes = {i.code for i in validate(spec, other_layer_names(context.scene, item))}
    col = split_column(layout)
    value = info_row(col, "Identifier", item.name, icon=DOMAIN_ICONS.get(item.domain, "NONE"))
    value.operator("geotools.layer_rename", text="", icon="GREASEPENCIL")
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
    col.label(text="Description")  # full width: a description is longer than a value
    col.prop(item, "description", text="")
    if "layer-range-invalid" in codes:
        values = col.column(align=True)  # worked out by Update Range; shown only to fix it
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


# --- Analyses ---------------------------------------------------------------------------


class GEOTOOLS_PT_analyses(_Panel, bpy.types.Panel):
    """The active mesh's analyses and layers, and Add Analysis."""

    bl_idname = "GEOTOOLS_PT_analyses"
    bl_label = "Analyses"

    def draw_header(self, context: bpy.types.Context) -> None:
        layer = analyses.item_layer(context.scene, _item(context))
        if layer is not None:
            header_status(self.layout, layer_state(context.scene, layer))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        props = analyses.settings(scene)
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.enabled = bool(analysis_presets.target_objects(context))
        row.prop(props, "add_search", text="", icon="ADD", placeholder="Add Analysis (search)")
        if props.error:
            _error(layout, context, props.error)
        mesh = analyses.active_mesh(context)
        if mesh is None:
            _muted(layout, "Select a mesh to see its analyses", "INFO")
            return
        row = layout.row()
        row.template_list("GEOTOOLS_UL_analyses", "", props, "items", props, "index", rows=4)
        col = row.column(align=True)
        col.operator("geotools.analyses_remove", text="", icon="REMOVE")
        if not analyses.results_of(scene, mesh):
            _muted(layout, f"No analyses on {mesh.name} yet", "INFO")


class GEOTOOLS_PT_analyses_inputs(_Panel, bpy.types.Panel):
    """The active result's inputs: its modifier's, its own, or its layer's fields."""

    bl_idname = "GEOTOOLS_PT_analyses_inputs"
    bl_label = "Inputs"
    bl_parent_id = "GEOTOOLS_PT_analyses"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _item(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = _item(context)
        if item is None:
            return
        if item.kind == "PRESET":
            _draw_preset(layout, context, item)
        elif item.kind == "FAST":
            _draw_fast(layout, context, item)
        elif item.kind in analyses.TOOL_KINDS:
            _draw_tool(layout, context, item)
        else:
            _draw_layer(layout, context, item)


# --- a preset's modifier ---------------------------------------------------------------


HIDDEN_INPUTS = frozenset({catalog.ATTRIBUTE_INPUT, catalog.CONVERGENCE_INPUT})
"""Inputs GeoTools fills and the user never edits (north is shown as a line instead)."""


def draw_modifier_inputs(layout: bpy.types.UILayout, modifier: bpy.types.Modifier) -> int:
    """The modifier's group inputs, as properties of the modifier itself; returns how many."""
    col = split_column(layout)
    inputs = modifier.properties.inputs
    drawn = 0
    for socket in modifier.node_group.interface.items_tree:
        if socket.item_type != "SOCKET" or socket.in_out != "INPUT":
            continue
        if socket.socket_type == "NodeSocketGeometry" or socket.name in HIDDEN_INPUTS:
            continue
        value = getattr(inputs, socket.identifier, None)
        if value is None:
            continue
        col.prop(value, "value", text=socket.name)
        drawn += 1
    return drawn


def _draw_preset(layout, context, item) -> None:
    preset = analyses.preset_of(item)
    modifier = analyses.item_modifier(item)
    if preset is None or modifier is None:
        _error(layout, context, "Its modifier is gone: remove the result and add it again.")
        return
    if not draw_modifier_inputs(layout, modifier):
        layout.label(text="No inputs to set", icon="INFO")
    if preset.north:
        north = "true north" if item.north == "TRUE" else "grid north (+Y)"
        layout.label(text=f"Measured from {north}", icon="ORIENTATION_GLOBAL")
    if preset.id in analysis_presets.TOTALS:
        layout.operator("geotools.analysis_totals", icon="INFO")


# --- what runs: Fast Bakes and the tools ------------------------------------------------


def _draw_run(layout, context, item, rows: list[tuple[str, str]]) -> None:
    """Out of date, Compute or Recompute, and the error or the result's summary."""
    if analyses.out_of_date(item):
        wrapped(layout, context, "Inputs changed since the last run: out of date.", "FILE_REFRESH")
    text = "Recompute" if item.computed else "Compute"
    icon = analyses.KIND_ICONS[item.kind]
    big_button(layout).operator("geotools.analyses_compute", text=text, icon=icon)
    error = analyses.run_error(item)
    if error:
        _error(layout, context, error)
        return
    if rows:
        col = layout.box().column(align=True)
        for label, value in rows:
            info_row(col, label, value)


def _draw_fast(layout, context, item) -> None:
    group = item.fast
    col = split_column(layout)
    col.prop(item, "extent")
    for name in fast.input_names(item.preset):
        col.prop(group, analyses.FAST_FIELDS[name])
    if item.preset in fast.VIEWSHED:
        col.prop(group, "observers")
        col.prop(group, "occluders")
    _draw_run(layout, context, item, [])
    if group.result and not group.error:
        wrapped(layout, context, group.result, "TIME")


def _draw_hydrology(col, _context, _item, props) -> None:
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


def _draw_least_cost(col, context, _item, props) -> None:
    col.prop(props, "start", text="Start")
    col.prop(props, "end", text="End")
    col.prop(props, "model")
    col.prop(props, "max_slope")
    sub = col.column()
    sub.active = props.model == "WHEELCHAIR"
    sub.prop(props, "max_gradient")
    col.prop_search(props, "cost_layer", context.scene.geotools, "layers", icon="RNA")
    col.prop(props, "steiner")
    col.prop(props, "make_route")
    col.prop(props, "make_layer")
    sub = col.column()
    sub.active = props.make_layer
    sub.prop(props, "time_layer")
    sub.prop(props, "band_minutes")
    row = sub.row()
    row.active = props.make_layer and props.band_minutes > 0
    row.prop(props, "band_layer")


def _draw_sun_hours(col, _context, item, props) -> None:
    col.prop(props, "period")
    row = col.row(align=True)
    if props.period == "YEAR":
        row.prop(item, "sun_year", text="Year")
    else:
        row.prop(item, "sun_year", text="From" if props.period == "RANGE" else "Date")
        row.prop(item, "sun_month", text="")
        row.prop(item, "sun_day", text="")
    if props.period == "RANGE":
        row = col.row(align=True)
        row.prop(props, "end_year", text="To")
        row.prop(props, "end_month", text="")
        row.prop(props, "end_day", text="")
    col.prop(props, "step_minutes", text="Step (min)")
    col.prop(props, "tolerance")
    col.prop(props, "horizon")
    col.prop(props, "horizon_profile")
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


def _draw_contours(col, _context, _item, props) -> None:
    col.prop(props, "interval")
    col.prop(props, "base")
    col.prop(props, "major_every")
    col.prop(props, "contour_name")


def _draw_sections(col, _context, _item, props) -> None:
    col.prop(props, "line")
    col.row(heading="Edges").prop(props, "selected_edges", text="Selected Only")
    col.prop(props, "section_name")


_TOOL_DRAW = {
    "HYDROLOGY": _draw_hydrology,
    "LEAST_COST": _draw_least_cost,
    "SUN_HOURS": _draw_sun_hours,
    "CONTOURS": _draw_contours,
    "SECTIONS": _draw_sections,
}


def _duration(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:,.2f} h"
    return f"{seconds / 60:,.1f} min" if seconds >= 60 else f"{seconds:,.1f} s"


def result_lines(item) -> list[tuple[str, str]]:
    """A few ``(label, value)`` rows of a tool result's last run (stored JSON, no mesh read)."""
    props = analyses.tool_settings(item)
    if item.kind == "CONTOURS":
        text = props.contour_result
    elif item.kind == "SECTIONS":
        text = props.section_result
    else:
        text = props.result
    if not text:
        return []
    try:
        result = json.loads(text)
        if item.kind == "HYDROLOGY":
            return [
                ("Depressions", f"{result['depressions']:,}"),
                ("Ponded", f"{result['ponded_volume']:,.2f} m³"),
                ("Time", f"{result['seconds']:.1f} s"),
            ]
        if item.kind == "LEAST_COST":
            route = result.get("route") or {}
            rows = [("Route", _duration(route["time"]))] if route else []
            if route:
                rows.append(("Length", f"{route['length']:,.1f} m"))
            return [*rows, ("Time", f"{result['seconds']:.1f} s")]
        if item.kind == "SUN_HOURS":
            return [
                ("Days", f"{result['first_day']} to {result['last_day']}"),
                ("Time", f"{result['seconds']:.1f} s"),
            ]
        if item.kind == "CONTOURS":
            return [("Lines", f"{result['lines']:,} ({result['closed']:,} closed)")]
        return [("Pieces", f"{result['pieces']:,} on {result['sections']} line(s)")]
    except (ValueError, KeyError, TypeError):
        return []


def _draw_tool(layout, context, item) -> None:
    props = analyses.tool_settings(item)
    if item.kind == "SUN_HOURS":
        wrapped(layout, context, "Time zone and site: Tools > Sun.", "LIGHT_SUN")
    col = split_column(layout)
    col.prop(item, "extent")
    _TOOL_DRAW[item.kind](col, context, item, props)
    _draw_run(layout, context, item, result_lines(item))


# --- a plain layer ---------------------------------------------------------------------


def _draw_layer(layout, context, item) -> None:
    layer = analyses.item_layer(context.scene, item)
    if layer is None:
        _error(layout, context, f"Layer {item.layer!r} is gone.")
        return
    draw_layer_fields(layout, context, layer)
    if layer.domain == Domain.TEXTURE.value:
        box = layout.box()
        box.label(text="Bake", icon="RENDER_STILL")
        if context.scene.geotools.source_collection is None:
            wrapped(box, context, "Choose a source collection first.", "INFO")
        else:
            bake_texture.draw_bake(box, context, layer)


# --- Display, Details and Outputs -------------------------------------------------------


def _shown_layer(context: bpy.types.Context):
    return analyses.item_layer(context.scene, _item(context))


class GEOTOOLS_PT_analyses_display(_Panel, bpy.types.Panel):
    """How the active result's layer is coloured, in the viewport and its variant."""

    bl_idname = "GEOTOOLS_PT_analyses_display"
    bl_label = "Display"
    bl_parent_id = "GEOTOOLS_PT_analyses"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _shown_layer(context) is not None

    def draw_header(self, context: bpy.types.Context) -> None:
        layer = _shown_layer(context)
        if layer is not None and display_issue_codes(layer):
            header_status(self.layout, ERROR)

    def draw(self, context: bpy.types.Context) -> None:
        layer = _shown_layer(context)
        if layer is not None:
            draw_display(self.layout, context, layer)


class GEOTOOLS_PT_analyses_details(_Panel, bpy.types.Panel):
    """The active result's layer: its rarely changed fields."""

    bl_idname = "GEOTOOLS_PT_analyses_details"
    bl_label = "Details"
    bl_parent_id = "GEOTOOLS_PT_analyses"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _shown_layer(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layer = _shown_layer(context)
        if layer is not None:
            draw_layer_details(self.layout, context, layer)


class GEOTOOLS_PT_analyses_outputs(_Panel, bpy.types.Panel):
    """The layers and objects the active result made, and Publish as Variant."""

    bl_idname = "GEOTOOLS_PT_analyses_outputs"
    bl_label = "Outputs"
    bl_parent_id = "GEOTOOLS_PT_analyses"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _item(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = _item(context)
        if item is None:
            return
        layers = [find_layer(context.scene, entry.name) for entry in item.layers]
        layers = [layer for layer in layers if layer is not None]
        objects = analyses.output_objects(item)
        if not layers and not objects:
            text = "Nothing yet: Compute first" if not item.computed else "No outputs"
            layout.label(text=text, icon="INFO")
            return
        col = layout.column(align=True)
        for layer in layers:
            row = col.row(align=True)
            shown = layer.name == item.layer
            row.operator(
                "geotools.analyses_show_layer",
                text="",
                icon="RADIOBUT_ON" if shown else "RADIOBUT_OFF",
                emboss=False,
            ).layer = layer.name
            row.label(text=layer.name, icon=DOMAIN_ICONS.get(layer.domain, "NONE"))
            eye = layer.presentation.show_in_viewport
            row.operator(
                "geotools.analyses_show",
                text="",
                icon="HIDE_OFF" if eye else "HIDE_ON",
                emboss=False,
            ).layer = layer.name
        for obj in objects:
            row = col.row(align=True)
            row.label(text=obj.name, icon="OUTLINER_OB_MESH")
            row.operator(
                "geotools.analyses_select_object", text="", icon="RESTRICT_SELECT_OFF"
            ).name = obj.name
        if layers:
            layout.operator("geotools.analyses_publish_variant", icon="FILE_3D")
        if objects:
            layout.label(text="Export: File > Export > GeoPackage", icon="EXPORT")


# Registration order is the sidebar order (a parent before its children).
classes = (
    GEOTOOLS_PT_analyses,
    GEOTOOLS_PT_analyses_inputs,
    GEOTOOLS_PT_analyses_display,
    GEOTOOLS_PT_analyses_details,
    GEOTOOLS_PT_analyses_outputs,
)
