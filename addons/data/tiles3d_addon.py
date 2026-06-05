from __future__ import annotations

from ...api import AddonCapabilities, AddonSpec, register_addon


def draw_import_panel(layout, context) -> None:
    props = context.scene.tiles3d
    layout.prop(props, "last_filepath", text="Tileset")

    if props.total_content_nodes <= 0:
        layout.label(text="Select a tileset.json to begin.", icon="INFO")
        return

    info = layout.column(align=True)
    info.label(text=f"Tree nodes: {props.total_content_nodes}", icon="MESH_DATA")
    info.label(
        text=f"ε  {props.error_min:.4f} – {props.error_max:.4f}", icon="MOD_SMOOTH"
    )

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.label(text="LOD Threshold (geometricError ≤):")
    col.prop(props, "error_threshold", text="", slider=False)

    layout.separator(factor=0.5)
    col = layout.column(align=True)
    col.prop(props, "apply_transforms")
    col.prop(props, "join_mesh")

    layout.separator(factor=0.5)
    op = layout.operator("geotools.import_tileset", text="Import Tiles", icon="IMPORT")
    op.filepath = props.last_filepath
    op.geometric_error_threshold = props.error_threshold
    op.apply_transforms = props.apply_transforms
    op.join_mesh = props.join_mesh


ADDON_SPEC = register_addon(
    AddonSpec(
        id="TILES3D",
        label="3D Tiles",
        description="Import an OGC 3D Tileset",
        pref_flag="mod_tiles3d",
        module_path="blender_geotools.addons.data.tiles3d",
        kind="source",
        group="data",
        order=30,
        format_id="tiles3d",
        provenance_formats=("tiles3d",),
        capabilities=AddonCapabilities(
            supports_import=True,
            supports_linked_layers=True,
        ),
        import_panel_draw="blender_geotools.addons.data.tiles3d_addon:draw_import_panel",
    )
)
