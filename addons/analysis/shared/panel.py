"""
analysis/panel.py — Analysis N-panel with searchable tool selector.

The combo box uses a standard EnumProperty which Blender renders as a
searchable dropdown (the search field appears automatically when the popup
opens). The panel body updates dynamically based on which tool is selected.
"""

import bpy
from bpy.types import Panel

from .registry import first_enabled_tool_spec, has_enabled_tools, resolve_drawer

# ---------------------------------------------------------------------------
# Tool-specific panel body drawers
# ---------------------------------------------------------------------------


def _no_mesh_hint(col, context):
    if not any(obj.type == "MESH" for obj in context.selected_objects):
        col.label(text="Select one or more mesh objects first.", icon="INFO")
        return True
    return False


def _draw_slope(layout: bpy.types.UILayout, context: bpy.types.Context):
    col = layout.column(align=True)
    col.label(text="Writes 'slope_deg' face attribute.", icon="RNDCURVE")
    col.label(text="0\u00b0 = flat  |  90\u00b0 = vertical wall")
    col.separator(factor=0.5)
    disabled = _no_mesh_hint(col, context)
    sub = col.column()
    sub.enabled = not disabled
    sub.operator("geotools.compute_slope", text="Run on Selected Meshes", icon="PLAY")


def _draw_aspect(layout: bpy.types.UILayout, context: bpy.types.Context):
    col = layout.column(align=True)
    col.label(text="Writes 'aspect_deg' face attribute.", icon="ORIENTATION_GLOBAL")
    col.label(text="0/360=N  90=E  180=S  270=W  (-1 = flat)")
    col.separator(factor=0.5)
    disabled = _no_mesh_hint(col, context)
    sub = col.column()
    sub.enabled = not disabled
    sub.operator("geotools.compute_aspect", text="Run on Selected Meshes", icon="PLAY")


def _draw_roughness(layout: bpy.types.UILayout, context: bpy.types.Context):
    col = layout.column(align=True)
    col.label(text="Writes 'roughness' face attribute.", icon="MESH_GRID")
    col.label(text="Scale-independent local relief (0 = planar face).")
    col.separator(factor=0.5)
    disabled = _no_mesh_hint(col, context)
    sub = col.column()
    sub.enabled = not disabled
    sub.operator(
        "geotools.compute_roughness", text="Run on Selected Meshes", icon="PLAY"
    )


def _draw_los(layout: bpy.types.UILayout, context: bpy.types.Context):
    settings = context.scene.geotools_analysis
    col = layout.column(align=True)
    col.label(text="Check visibility from observer to targets.", icon="HIDE_OFF")
    col.separator(factor=0.5)

    col.prop(settings, "los_observer")
    col.prop(settings, "los_observer_height")
    col.prop(settings, "los_target_height")
    col.prop(settings, "los_terrain_collection", icon="OUTLINER_COLLECTION")
    col.separator(factor=0.5)

    # Determine whether the poll condition is met for a useful hint
    active = context.active_object
    has_targets = any(
        obj.type == "MESH" and obj is not active for obj in context.selected_objects
    )
    if not has_targets:
        hint = (
            "Make observer active; select target meshes."
            if settings.los_observer == "ACTIVE"
            else "Select one or more target mesh objects."
        )
        col.label(text=hint, icon="INFO")
    sub = col.column()
    sub.enabled = has_targets
    sub.operator("geotools.compute_los", text="Check LoS", icon="PLAY")


def _draw_surface_tag(layout: bpy.types.UILayout, context: bpy.types.Context):
    settings = context.scene.geotools_analysis
    col = layout.column(align=True)
    col.label(text="Write a boolean face attribute.", icon="BRUSH_DATA")
    col.separator(factor=0.5)

    col.prop(settings, "tag_attribute_name", icon="SPREADSHEET")
    col.prop(settings, "tag_mode")
    col.separator(factor=0.3)

    if settings.tag_mode == "SLOPE":
        row = col.row(align=True)
        row.prop(settings, "tag_slope_min", text="Min °")
        row.prop(settings, "tag_slope_max", text="Max °")
        col.prop(settings, "tag_auto_slope")
    else:
        col.label(text="Select faces in Edit Mode, exit, then run.", icon="INFO")

    col.separator(factor=0.5)
    has_mesh = any(obj.type == "MESH" for obj in context.selected_objects)
    if not has_mesh:
        col.label(text="Select one or more mesh objects first.", icon="INFO")
    sub = col.column()
    sub.enabled = has_mesh
    sub.operator("geotools.tag_surface", text="Tag Selected Meshes", icon="PLAY")


def _draw_attr_transfer(layout: bpy.types.UILayout, context: bpy.types.Context):
    settings = context.scene.geotools_analysis
    col = layout.column(align=True)
    col.label(text="Copy attribute from active \u2192 selected.", icon="COPYDOWN")
    col.separator(factor=0.5)

    col.prop(settings, "xfer_source_attr", icon="SPREADSHEET")
    col.prop(settings, "xfer_dest_attr", icon="PASTEDOWN")
    col.separator(factor=0.3)

    col.prop(settings, "xfer_method")
    if settings.xfer_method == "RAYCAST":
        col.prop(settings, "xfer_ray_direction")
    col.prop(settings, "xfer_max_distance")

    col.separator(factor=0.5)

    # Readiness check
    active = context.active_object
    src_ok = active is not None and active.type == "MESH"
    attr_ok = settings.xfer_source_attr.strip() != ""
    has_tgt = any(
        obj.type == "MESH" and obj is not active for obj in context.selected_objects
    )
    ready = src_ok and attr_ok and has_tgt

    if not src_ok:
        col.label(text="Make the source mesh the active object.", icon="INFO")
    elif not attr_ok:
        col.label(text="Enter the source attribute name above.", icon="INFO")
    elif not has_tgt:
        col.label(text="Select target mesh objects (source = active).", icon="INFO")

    sub = col.column()
    sub.enabled = ready
    sub.operator("geotools.transfer_attributes", text="Transfer Attribute", icon="PLAY")


def _draw_attr_vis(layout: bpy.types.UILayout, context: bpy.types.Context):
    settings = context.scene.geotools_analysis
    col = layout.column(align=True)
    col.label(text="Colour faces by attribute value.", icon="RESTRICT_COLOR_OFF")
    col.separator(factor=0.5)

    col.prop(settings, "vis_source")

    is_obj_prop = settings.vis_source == "OBJECT_PROP"
    src_icon = "OBJECT_DATA" if is_obj_prop else "SPREADSHEET"
    src_label = "Property Name" if is_obj_prop else "Attribute Name"
    col.prop(settings, "vis_source_attr", text=src_label, icon=src_icon)
    col.prop(settings, "vis_output_attr", icon="PASTEDOWN")
    col.separator(factor=0.3)

    col.prop(settings, "vis_mode")

    show_ramp = settings.vis_mode in ("RAMP", "AUTO")
    if show_ramp:
        col.separator(factor=0.3)
        col.prop(settings, "vis_ramp")
        col.prop(settings, "vis_invert")
        col.prop(settings, "vis_auto_range")
        if not settings.vis_auto_range:
            row = col.row(align=True)
            row.prop(settings, "vis_range_min", text="Min")
            row.prop(settings, "vis_range_max", text="Max")

    col.separator(factor=0.5)

    has_mesh = any(obj.type == "MESH" for obj in context.selected_objects)
    attr_ok = settings.vis_source_attr.strip() != ""
    ready = has_mesh and attr_ok

    if not attr_ok:
        hint = (
            "Enter the custom property name above."
            if is_obj_prop
            else "Enter the attribute name above."
        )
        col.label(text=hint, icon="INFO")
    elif not has_mesh:
        col.label(text="Select one or more mesh objects first.", icon="INFO")

    sub = col.column()
    sub.enabled = ready
    sub.operator("geotools.visualise_attribute", text="Visualise", icon="PLAY")


def _draw_shadow(layout: bpy.types.UILayout, context: bpy.types.Context):
    settings = context.scene.geotools_analysis
    col = layout.column(align=True)
    col.label(text="Cast sun rays to find shadowed faces.", icon="LIGHT_SUN")
    col.separator(factor=0.5)

    col.prop(settings, "shadow_mode")
    col.separator(factor=0.3)

    is_daily = settings.shadow_mode == "DAILY"

    if not is_daily:
        # ── Instant mode — source selector ────────────────────────────
        col.prop(settings, "shadow_source")
        col.separator(factor=0.3)

        src = settings.shadow_source
        if src == "MANUAL":
            col.prop(settings, "shadow_azimuth")
            col.prop(settings, "shadow_elevation")
        elif src == "SUN_LAMP":
            col.prop(settings, "shadow_sun_object", icon="LIGHT_SUN")
        else:  # SOLAR
            col.prop(settings, "shadow_date", icon="TIME")
            col.prop(settings, "shadow_utc_offset")
            col.prop(settings, "shadow_hour_utc")
            col.separator(factor=0.2)
            col.prop(settings, "shadow_use_geoscene")
            if not settings.shadow_use_geoscene:
                col.prop(settings, "shadow_lat")
                col.prop(settings, "shadow_lon")
    else:
        # ── Daily mode — always Solar ──────────────────────────────────
        col.prop(settings, "shadow_date", icon="TIME")
        col.prop(settings, "shadow_utc_offset")
        row = col.row(align=True)
        row.prop(settings, "shadow_hour_start", text="Start")
        row.prop(settings, "shadow_hour_end", text="End")
        col.prop(settings, "shadow_num_steps")
        col.separator(factor=0.2)
        col.prop(settings, "shadow_use_geoscene")
        if not settings.shadow_use_geoscene:
            col.prop(settings, "shadow_lat")
            col.prop(settings, "shadow_lon")

    col.separator(factor=0.3)
    col.prop(settings, "shadow_terrain_collection", icon="OUTLINER_COLLECTION")
    col.prop(
        settings,
        "shadow_attr_daily" if is_daily else "shadow_attr_instant",
        icon="SPREADSHEET",
    )

    col.separator(factor=0.5)

    # Place Sun Lamp button — only when the direction can be derived
    can_place = not is_daily and settings.shadow_source != "SUN_LAMP"
    if can_place:
        col.operator("geotools.place_sun_lamp", text="Place Sun Lamp", icon="LIGHT_SUN")
        col.separator(factor=0.3)

    has_mesh = any(obj.type == "MESH" for obj in context.selected_objects)
    if not has_mesh:
        col.label(text="Select one or more mesh objects first.", icon="INFO")
    sub = col.column()
    sub.enabled = has_mesh
    label = "Compute Shadow Fraction" if is_daily else "Compute Shadow"
    sub.operator("geotools.compute_shadow", text=label, icon="PLAY")


# ---------------------------------------------------------------------------
# Panel
# ---------------------------------------------------------------------------


class GEOTOOLS_PT_analysis(Panel):
    bl_label = "Analysis"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"
    bl_order = 30

    @classmethod
    def poll(cls, context):
        return has_enabled_tools()

    def draw(self, context):
        layout = self.layout
        props = context.scene.geotools_analysis

        # ── Tool selector (searchable enum dropdown) ──────────────────
        row = layout.row(align=True)
        row.prop(props, "active_tool", text="", icon="TOOL_SETTINGS")

        # ── Separator ─────────────────────────────────────────────────
        layout.separator(factor=0.5)

        # ── Dynamic body ──────────────────────────────────────────────
        active_spec = first_enabled_tool_spec()
        drawer = resolve_drawer(props.active_tool)
        if drawer is None and active_spec is not None:
            props.active_tool = active_spec.id
            drawer = resolve_drawer(props.active_tool)
        if drawer is not None:
            box = layout.box()
            drawer(box, context)
        else:
            layout.label(text="No tool selected.", icon="INFO")


_classes = (GEOTOOLS_PT_analysis,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
