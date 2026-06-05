from __future__ import annotations

from ...api import AddonCapabilities, AddonSpec, register_addon


def draw_import_panel(layout, context) -> None:
    settings = getattr(context.scene, "dem_import", None)
    if settings is None:
        layout.operator("geotools.import_dem", text="Import DEM…", icon="IMPORT")
        return

    col = layout.column(align=True)
    col.prop(settings, "downsample")
    col.prop(settings, "apply_offset")
    col.prop(settings, "add_subdivision")

    op = layout.operator("geotools.import_dem", text="Import DEM…", icon="IMPORT")
    op.downsample = settings.downsample
    op.apply_offset = settings.apply_offset
    op.add_subdivision = settings.add_subdivision


ADDON_SPEC = register_addon(
    AddonSpec(
        id="DEM",
        label="DEM",
        description="Import a Digital Elevation Model",
        pref_flag="mod_dem",
        module_path="blender_geotools.addons.data.dem",
        kind="source",
        group="data",
        order=40,
        format_id="dem",
        provenance_formats=("dem",),
        capabilities=AddonCapabilities(
            supports_import=True,
            supports_linked_layers=True,
        ),
        import_panel_draw="blender_geotools.addons.data.dem_addon:draw_import_panel",
    )
)
