"""
blender_geotools.dem — DEM (Digital Elevation Model) importer submodule.
"""

from . import properties, operators


def register():
    properties.register()
    operators.register()


def unregister():
    operators.unregister()
    properties.unregister()
