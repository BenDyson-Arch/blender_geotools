"""
blender_geotools.analysis — Geospatial analysis tools submodule.
"""

from . import properties, operators, panel


def register():
    properties.register()
    operators.register()
    panel.register()


def unregister():
    panel.unregister()
    operators.unregister()
    properties.unregister()
