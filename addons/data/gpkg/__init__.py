"""blender_geotools.gpkg — GeoPackage importer/exporter submodule.

Core I/O (no Blender dependency):
  gpkg.wkb     — WKB binary geometry parser and encoder
  gpkg.reader  — sqlite3-based vector layer reader
  gpkg.writer  — sqlite3-based CRUD writer

Blender integration (lazily imported inside register/unregister):
  gpkg.properties — PropertyGroup for import settings + provenance constants
  gpkg.operators  — Import, Reload, and Write-back operators
"""


def register():
    from . import properties, operators
    properties.register()
    operators.register()


def unregister():
    from . import operators, properties
    operators.unregister()
    properties.unregister()
