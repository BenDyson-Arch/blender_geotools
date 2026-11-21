"""Sidebar panels of the Analyses list mock-up (#157), and the Tools panel.

- Analyses: the Add Analysis search field (``analyses.search_entries``), the
  Selected / All toggle, the list of results (``GEOTOOLS_UL_analyses``, which
  shows the results on the selection, ``analyses.visible_flags``) and, for
  the active row while the list shows it (``analyses.shown_item``):
  - Inputs: Applies to (the result's meshes, active first) with Add Selected
    and Remove Selected; a preset's modifier inputs, drawn from the active
    mesh's modifier and kept in step on the others; a tool's own settings
    with Compute, out of date once they change; a plain layer's fields (and
    Bake).
  - Display: the layer's colours and Show in Viewport (``presentation``).
  - Outputs: the layers and objects it made, and Publish as Variant.
- Tools: the interactive readings that are not layers: Measure, Sun (with
  Shadow Study) and Archaeoastronomy, drawn by the classic panels' code.

The classic Analysis and Layers panels are shown instead when the
``Use Classic Analysis Panels`` preference is on. Panels draw stored state
only: the modifier inputs are RNA reads, never an evaluation.
"""

from __future__ import annotations

import json

import bpy

from ..analysis import presets as catalog
from ..core.layers import attribute_name
from . import (
    analyses,
    analysis,
    analysis_presets,
    analysis_ui,
    archaeo_ui,
    bake_texture,
    preferences,
)
from .layers import DOMAIN_ICONS, find_layer
from .presentation import draw_display
from .ui import big_button, header_status, info_row, split_column, wrapped


class _Panel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


def _new_ui(context: bpy.types.Context) -> bool:
    return not preferences.classic_panels(context)


def _item(context: bpy.types.Context):
    """The active result when the list shows it: what Inputs, Display and Outputs draw."""
    return analyses.shown_item(context)


def _error(layout, context, text: str) -> None:
    col = layout.column()
    col.alert = True
    wrapped(col, context, text, "ERROR")


# --- Analyses ---------------------------------------------------------------------------


class GEOTOOLS_PT_analyses(_Panel, bpy.types.Panel):
    """Every analysis result in one list, with a searchable Add Analysis field."""

    bl_idname = "GEOTOOLS_PT_analyses"
    bl_label = "Analyses"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _new_ui(context)

    def draw_header(self, context: bpy.types.Context) -> None:
        from .panels import layer_state  # panels imports this module

        layer = analyses.item_layer(context.scene, _item(context))
        if layer is not None:
            header_status(self.layout, layer_state(context.scene, layer))

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        props = analyses.settings(scene)
        row = layout.row(align=True)
        row.scale_y = 1.2
        row.prop(props, "add_search", text="", icon="ADD", placeholder="Add Analysis (search)")
        if props.error:
            _error(layout, context, props.error)

        _draw_filter(layout, context, props)
        row = layout.row()
        row.template_list("GEOTOOLS_UL_analyses", "", props, "items", props, "index", rows=4)
        col = row.column(align=True)
        col.operator("geotools.analyses_remove", text="", icon="REMOVE")
        _draw_filter_note(layout, context, props)

        unlisted = analyses.unlisted_layers(scene)
        if unlisted:
            row = layout.row(align=True)
            count = len(unlisted)
            row.label(text=f"{count} layer{'s' * (count != 1)} not listed", icon="INFO")
            row.operator("geotools.analyses_list_layers")
        if not len(props.items):
            wrapped(
                layout,
                context,
                "Select the meshes to analyse, then search for an analysis to add.",
                "INFO",
            )


def _draw_filter(layout, context, props) -> None:
    """The list header: how many results it shows, and the Selected / All toggle."""
    split = layout.split(factor=0.4, align=True)
    label = split.row()
    label.active = False
    label.label(text=f"{sum(analyses.visible_flags(context))} of {len(props.items)}")
    split.row(align=True).prop(props, "show", expand=True)


def _draw_filter_note(layout, context, props) -> None:
    """One muted line under the list when Selected shows everything."""
    if props.show == "SELECTED" and len(props.items) and not analyses.selection_pointers(context):
        row = layout.row()
        row.active = False
        row.label(text="Nothing selected: showing all", icon="INFO")


def _applies_row(layout, context, item) -> None:
    """``Applies to: hills (+2)``, and Add Selected / Remove Selected below it."""
    meshes = analyses.applies_to(context.scene, item)
    active = context.view_layer.objects.active if context.view_layer else None
    if active in meshes:  # the active mesh first, as the modifier stack would show it
        meshes = [active, *[o for o in meshes if o != active]]
    col = layout.column(align=True)
    icon = "OBJECT_DATA" if meshes else "ERROR"
    col.label(text=f"Applies to: {analyses._summary(meshes)}", icon=icon)
    if item.kind == "LAYER":
        return
    row = col.row(align=True)
    row.operator("geotools.analyses_add_selected", icon="ADD")
    row.operator("geotools.analyses_remove_selected", icon="REMOVE")


class GEOTOOLS_PT_analyses_inputs(_Panel, bpy.types.Panel):
    """The selected analysis' inputs: its modifier, its settings or its layer."""

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
        elif item.kind in analyses.TOOL_KINDS:
            _draw_tool(layout, context, item)
        else:
            _draw_layer(layout, context, item)


# --- a preset's modifier ---------------------------------------------------------------


def preset_modifier(context: bpy.types.Context, item):
    """The modifier whose inputs the sidebar shows, and the object it is on.

    A layer preset's modifier on the active object when that is one of the
    item's meshes, else on the first; an object preset's on its new object.
    """
    try:
        preset = catalog.get(item.preset)
    except KeyError:
        return None, None
    group = analysis_presets.find_group(preset.id, preset.version)
    if group is None:
        return None, None
    if preset.output is catalog.Output.OBJECT:
        for obj in analyses.output_objects(item):
            for modifier in obj.modifiers:
                if modifier.type == "NODES" and modifier.node_group == group:
                    return modifier, obj
        return None, None
    objects = analyses.item_objects(item)
    active = context.active_object
    if active in objects:
        objects = [active, *[o for o in objects if o != active]]
    attribute = attribute_name(item.layer)
    for obj in objects:
        modifier = analysis_presets.find_modifier(obj, group, attribute)
        if modifier is not None:
            return modifier, obj
    return None, None


HIDDEN_INPUTS = frozenset({catalog.ATTRIBUTE_INPUT, catalog.CONVERGENCE_INPUT})
"""Inputs GeoTools fills and the user never edits (north is shown as a line instead)."""


def draw_modifier_inputs(layout: bpy.types.UILayout, modifier: bpy.types.Modifier) -> int:
    """The modifier's group inputs as properties of the modifier itself; returns how many."""
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
    preset = catalog.get(item.preset)
    if preset.output is catalog.Output.LAYER:
        _applies_row(layout, context, item)
    modifier, _obj = preset_modifier(context, item)
    if modifier is None:
        _error(layout, context, "The modifier is gone: Add Selected puts it back.")
        return
    count = len(analyses.item_objects(item))
    if count > 1:
        note = layout.row()
        note.active = False
        meshes = "both meshes" if count == 2 else f"all {count} meshes"
        note.label(text=f"Edits apply to {meshes}", icon="LINKED")
    if not draw_modifier_inputs(layout, modifier):
        layout.label(text="No inputs to set", icon="INFO")
    if preset.north:
        north = "true north" if item.north == "TRUE" else "grid north (+Y)"
        layout.label(text=f"Measured from {north}", icon="ORIENTATION_GLOBAL")
    if preset.id in analysis_presets.TOTALS:
        layout.operator("geotools.analysis_totals", icon="INFO")


# --- a tool's settings -----------------------------------------------------------------


def _draw_hydrology(col, _context, props) -> None:
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


def _draw_least_cost(col, context, props) -> None:
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


def _draw_sun_hours(col, context, props) -> None:
    sun = analysis.settings(context).sun
    analysis_ui._draw_period(col, props, sun)
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


def _draw_contours(col, _context, props) -> None:
    col.prop(props, "interval")
    col.prop(props, "base")
    col.prop(props, "major_every")
    col.prop(props, "contour_name")


def _draw_sections(col, _context, props) -> None:
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
    """A few ``(label, value)`` rows of the item's last result (stored JSON, no mesh read)."""
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
    _applies_row(layout, context, item)
    if item.kind == "SUN_HOURS":
        wrapped(layout, context, "Date, time zone and site: Tools > Sun.", "LIGHT_SUN")
    col = split_column(layout)
    _TOOL_DRAW[item.kind](col, context, props)
    text = "Recompute" if item.computed else "Compute"
    if analyses.out_of_date(item):
        wrapped(layout, context, "Inputs changed since the last run: out of date.", "FILE_REFRESH")
    big_button(layout).operator(
        "geotools.analyses_compute", text=text, icon=analyses.KIND_ICONS[item.kind]
    )
    error = analyses.tool_error(item)
    if error:
        _error(layout, context, error)
        return
    rows = result_lines(item)
    if rows:
        col = layout.box().column(align=True)
        for label, value in rows:
            info_row(col, label, value)


# --- a plain layer ---------------------------------------------------------------------


def _draw_layer(layout, context, item) -> None:
    from .panels import draw_layer_fields  # panels imports this module

    layer = analyses.item_layer(context.scene, item)
    if layer is None:
        _error(layout, context, f"Layer {item.layer!r} is gone.")
        return
    _applies_row(layout, context, item)
    draw_layer_fields(layout, context, layer)
    if layer.domain == "TEXTURE":
        box = layout.box()
        box.label(text="Bake", icon="RENDER_STILL")
        if context.scene.geotools.source_collection is None:
            wrapped(box, context, "Choose a source collection first.", "INFO")
        else:
            bake_texture.draw_bake(box, context, layer)


# --- Display and Outputs ---------------------------------------------------------------


class GEOTOOLS_PT_analyses_display(_Panel, bpy.types.Panel):
    """How the selected analysis' layer is coloured, in the viewport and its variant."""

    bl_idname = "GEOTOOLS_PT_analyses_display"
    bl_label = "Display"
    bl_parent_id = "GEOTOOLS_PT_analyses"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return analyses.item_layer(context.scene, _item(context)) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layer = analyses.item_layer(context.scene, _item(context))
        if layer is not None:
            draw_display(self.layout, context, layer)


class GEOTOOLS_PT_analyses_outputs(_Panel, bpy.types.Panel):
    """The layers and objects the selected analysis made."""

    bl_idname = "GEOTOOLS_PT_analyses_outputs"
    bl_label = "Outputs"
    bl_parent_id = "GEOTOOLS_PT_analyses"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _item(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        item = _item(context)
        if item is None:
            return
        layers = [find_layer(scene, entry.name) for entry in item.layers]
        layers = [layer for layer in layers if layer is not None]
        objects = analyses.output_objects(item)
        if not layers and not objects:
            text = "Nothing yet: Compute first." if not item.computed else "No outputs."
            layout.label(text=text, icon="INFO")
            return
        col = layout.column(align=True)
        for layer in layers:
            row = col.row(align=True)
            shown = layer.name == item.layer
            op = row.operator(
                "geotools.analyses_show_layer",
                text="",
                icon="RADIOBUT_ON" if shown else "RADIOBUT_OFF",
                emboss=False,
            )
            op.layer = layer.name
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


# --- Tools -----------------------------------------------------------------------------


class GEOTOOLS_PT_tools(_Panel, bpy.types.Panel):
    """Interactive readings that are not layers: Measure, Sun and Archaeoastronomy."""

    bl_idname = "GEOTOOLS_PT_tools"
    bl_label = "Tools"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _new_ui(context)

    draw = analysis_ui.GEOTOOLS_PT_analysis.draw


class GEOTOOLS_PT_tools_measure(_Panel, bpy.types.Panel):
    """Points on Earth, the distance between them and the area of selected faces."""

    bl_idname = "GEOTOOLS_PT_tools_measure"
    bl_label = "Measure"
    bl_parent_id = "GEOTOOLS_PT_tools"

    draw = analysis_ui.GEOTOOLS_PT_analysis_coordinates.draw


class GEOTOOLS_PT_tools_sun(_Panel, bpy.types.Panel):
    """The sun at a moment or over a day, on a Blender Sun light."""

    bl_idname = "GEOTOOLS_PT_tools_sun"
    bl_label = "Sun"
    bl_parent_id = "GEOTOOLS_PT_tools"
    bl_options = {"DEFAULT_CLOSED"}

    draw = analysis_ui.GEOTOOLS_PT_analysis_sun.draw


class GEOTOOLS_PT_tools_shadow_study(_Panel, bpy.types.Panel):
    """Sun light keys at the equinoxes and solstices, one frame per moment."""

    bl_idname = "GEOTOOLS_PT_tools_shadow_study"
    bl_label = "Shadow Study"
    bl_parent_id = "GEOTOOLS_PT_tools_sun"
    bl_options = {"DEFAULT_CLOSED"}

    draw = analysis_ui.GEOTOOLS_PT_analysis_shadow_study.draw


class GEOTOOLS_PT_tools_archaeo(_Panel, bpy.types.Panel):
    """Horizon, solar and lunar events at an epoch, sightlines and alignments."""

    bl_idname = "GEOTOOLS_PT_tools_archaeo"
    bl_label = "Archaeoastronomy"
    bl_parent_id = "GEOTOOLS_PT_tools"
    bl_options = {"DEFAULT_CLOSED"}

    draw = archaeo_ui.GEOTOOLS_PT_analysis_archaeo.draw


# Registration order is the sidebar order (a parent before its children).
classes = (
    GEOTOOLS_PT_analyses,
    GEOTOOLS_PT_analyses_inputs,
    GEOTOOLS_PT_analyses_display,
    GEOTOOLS_PT_analyses_outputs,
    GEOTOOLS_PT_tools,
    GEOTOOLS_PT_tools_measure,
    GEOTOOLS_PT_tools_sun,
    GEOTOOLS_PT_tools_shadow_study,
    GEOTOOLS_PT_tools_archaeo,
)
