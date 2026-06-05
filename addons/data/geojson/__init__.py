"""blender_geotools.addons.data.geojson — GeoJSON importer submodule."""


def register():
    from . import operators, properties

    properties.register()
    operators.register()


def unregister():
    from . import operators, properties

    operators.unregister()
    properties.unregister()
