from __future__ import annotations

from ...api import AddonCapabilities, AddonSpec, register_addon


def draw_import_panel(layout, context) -> None:
    settings = getattr(context.scene, "geojson_import", None)
    if settings is None:
        layout.operator(
            "geotools.import_geojson", text="Import GeoJSON…", icon="IMPORT"
        )
        return

    col = layout.column(align=True)
    col.prop(settings, "mode")
    col.separator(factor=0.5)
    col.prop(settings, "offset_origin")
    col.prop(settings, "flip_yz")

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.label(text="Feature Data:")
    col.prop(settings, "store_custom_props")
    col.prop(settings, "auto_normal")
    row = col.row(align=True)
    row.enabled = not settings.auto_normal
    row.prop(settings, "normal_property")

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.label(text="Geometry:")
    col.prop(settings, "import_holes")
    col.prop(settings, "use_extrude")
    row = col.row(align=True)
    row.enabled = settings.use_extrude
    row.prop(settings, "extrude_prop")

    op = layout.operator(
        "geotools.import_geojson", text="Import GeoJSON…", icon="IMPORT"
    )
    op.mode = settings.mode
    op.offset_origin = settings.offset_origin
    op.flip_yz = settings.flip_yz
    op.store_custom_props = settings.store_custom_props
    op.auto_normal = settings.auto_normal
    op.normal_property = settings.normal_property
    op.import_holes = settings.import_holes
    op.extrude_prop = settings.extrude_prop if settings.use_extrude else ""


ADDON_SPEC = register_addon(
    AddonSpec(
        id="GEOJSON",
        label="GeoJSON",
        description="Import a GeoJSON file",
        pref_flag="mod_geojson",
        module_path="blender_geotools.addons.data.geojson",
        kind="source",
        group="data",
        order=10,
        format_id="geojson",
        provenance_formats=("geojson",),
        capabilities=AddonCapabilities(
            supports_import=True,
            supports_linked_layers=True,
            supports_feature_editing=True,
        ),
        import_panel_draw="blender_geotools.addons.data.geojson_addon:draw_import_panel",
    )
)
