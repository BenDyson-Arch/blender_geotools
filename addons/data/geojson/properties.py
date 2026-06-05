"""
properties.py — Addon properties and import settings.
"""

import bpy

from ....api.vector_properties import (
    import_holes_property,
    offset_origin_property,
    store_custom_props_property,
    vector_mode_property,
)


class GeoJSONImportSettings(bpy.types.PropertyGroup):
    """Import settings exposed in the N-panel and file browser."""

    mode: vector_mode_property()

    offset_origin: offset_origin_property()

    flip_yz: bpy.props.BoolProperty(
        name="Flip Y/Z (Z-up from Y-up source)",
        description="Swap Y and Z axes for sources with Y-up orientation",
        default=False,
    )

    store_custom_props: store_custom_props_property()

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

    import_holes: import_holes_property()

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
    bpy.types.Scene.geojson_import = bpy.props.PointerProperty(
        type=GeoJSONImportSettings
    )


def unregister():
    del bpy.types.Scene.geojson_import
    bpy.utils.unregister_class(GeoJSONImportSettings)
