"""blender_geotools.addons.data.tiles3d — 3D Tiles importer submodule."""


def register():
    from . import operators, properties

    properties.register()
    operators.register()


def unregister():
    from . import operators, properties

    operators.unregister()
    properties.unregister()
