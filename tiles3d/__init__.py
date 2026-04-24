"""
blender_geotools.tiles3d — OGC 3D Tiles importer submodule.
"""

from . import properties, operators


def register():
    properties.register()
    operators.register()


def unregister():
    operators.unregister()
    properties.unregister()
