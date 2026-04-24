"""
blender_geotools.geojson — GeoJSON importer submodule.
"""

from . import properties, operators


def register():
    properties.register()
    operators.register()


def unregister():
    operators.unregister()
    properties.unregister()
