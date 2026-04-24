"""
dem/properties.py — PropertyGroups for DEM import settings.
"""

import bpy
from bpy.props import BoolProperty, EnumProperty
from bpy.types import PropertyGroup


class DEMImportSettings(PropertyGroup):
    """Persistent DEM import settings stored on the scene."""

    downsample: EnumProperty(
        name="Downsample",
        description="Skip factor applied to rows and columns when building the mesh. "
                    "Higher values produce coarser but faster geometry",
        items=[
            ("1", "1× — Full resolution", ""),
            ("2", "2× — Half resolution", ""),
            ("4", "4× — Quarter resolution", ""),
            ("8", "8× — Eighth resolution", ""),
        ],
        default="4",
    )

    apply_offset: BoolProperty(
        name="Apply scene offset",
        description="Subtract the scene georeferencing origin from all vertex positions. "
                    "Disable only if you intend to place the terrain manually",
        default=True,
    )

    add_subdivision: BoolProperty(
        name="Add Subdivision modifier",
        description="Add a Subdivision Surface modifier rather than baking subdivisions "
                    "into the mesh. Keeps the mesh light while allowing smooth rendering",
        default=False,
    )


_classes = (DEMImportSettings,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.dem_import = bpy.props.PointerProperty(type=DEMImportSettings)


def unregister():
    del bpy.types.Scene.dem_import
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
