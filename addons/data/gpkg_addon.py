from __future__ import annotations

from ...api import AddonCapabilities, AddonSpec, register_addon


def draw_import_panel(layout, context) -> None:
    settings = getattr(context.scene, "gpkg_import", None)
    if settings is None:
        layout.operator("geotools.import_gpkg", text="Import GPKG…", icon="IMPORT")
        return

    col = layout.column(align=True)
    col.prop(settings, "filepath", text="File")
    col.prop(settings, "layer_name", text="Layer")
    col.prop(settings, "mode")

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.prop(settings, "offset_origin")

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.label(text="Feature Data:")
    col.prop(settings, "store_custom_props")

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.label(text="Geometry:")
    col.prop(settings, "import_holes")

    layer_ready = settings.filepath and settings.layer_name not in ("", "__none__")
    row = layout.row()
    row.enabled = bool(layer_ready)
    op = row.operator("geotools.import_gpkg", text="Import GPKG", icon="IMPORT")
    if layer_ready:
        op.filepath = settings.filepath
        op.layer_name = settings.layer_name
        op.mode = settings.mode
        op.offset_origin = settings.offset_origin
        op.store_custom_props = settings.store_custom_props
        op.import_holes = settings.import_holes


def draw_layer_panel(layout, _context, _collection) -> None:
    col = layout.column(align=True)
    col.operator("geotools.reload_gpkg", text="Reload from File", icon="FILE_REFRESH")
    col.operator("geotools.write_gpkg", text="Write to File", icon="EXPORT")


ADDON_SPEC = register_addon(
    AddonSpec(
        id="GPKG",
        label="GeoPackage",
        description="Import a GeoPackage layer",
        pref_flag="mod_gpkg",
        module_path="blender_geotools.addons.data.gpkg",
        kind="source",
        group="data",
        order=20,
        format_id="gpkg",
        provenance_formats=("gpkg",),
        capabilities=AddonCapabilities(
            supports_import=True,
            supports_linked_layers=True,
            supports_feature_editing=True,
            supports_reload=True,
            supports_writeback=True,
        ),
        import_panel_draw="blender_geotools.addons.data.gpkg_addon:draw_import_panel",
        layer_panel_draw="blender_geotools.addons.data.gpkg_addon:draw_layer_panel",
    )
)
