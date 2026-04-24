"""
properties.py — Addon properties and import settings.
"""

import bpy


class GeoJSONImportSettings(bpy.types.PropertyGroup):
    """Import settings exposed in the N-panel and file browser."""

    mode: bpy.props.EnumProperty(
        name="Import Mode",
        items=[
            ("PER_FEATURE", "One object per feature",
             "Each GeoJSON feature becomes a separate Blender object"),
            ("BY_TYPE", "One object per geometry type",
             "All features of the same type are merged into one object"),
            ("JOINED", "Single joined mesh",
             "All geometry joined into one object"),
        ],
        default="PER_FEATURE",
    )

    offset_origin: bpy.props.BoolProperty(
        name="Offset to world origin",
        description="Subtract the dataset bounding box centroid from all coordinates",
        default=True,
    )

    flip_yz: bpy.props.BoolProperty(
        name="Flip Y/Z (Z-up from Y-up source)",
        description="Swap Y and Z axes for sources with Y-up orientation",
        default=False,
    )

    store_custom_props: bpy.props.BoolProperty(
        name="Duplicate as custom properties",
        description="Also write numeric attributes as object custom properties (legacy)",
        default=False,
    )

    auto_normal: bpy.props.BoolProperty(
        name="Auto-detect normal vector property",
        description="Automatically write a matching property as a FLOAT_VECTOR mesh attribute",
        default=True,
    )

    normal_property: bpy.props.StringProperty(
        name="Normal property name",
        description="Name of the feature property to treat as a normal vector",
        default="normal",
        maxlen=64,
    )

    import_holes: bpy.props.BoolProperty(
        name="Import holes (interior rings)",
        description="Cut interior rings as holes in polygon faces",
        default=True,
    )

    use_extrude: bpy.props.BoolProperty(
        name="Extrude by property",
        description="Extrude polygon faces along Z by a scalar property value",
        default=False,
    )

    extrude_prop: bpy.props.StringProperty(
        name="Extrude property",
        description="Name of the numeric property to use for extrusion height",
        default="",
        maxlen=64,
    )


def register():
    bpy.utils.register_class(GeoJSONImportSettings)
    bpy.types.Scene.geojson_import = bpy.props.PointerProperty(type=GeoJSONImportSettings)


def unregister():
    del bpy.types.Scene.geojson_import
    bpy.utils.unregister_class(GeoJSONImportSettings)
