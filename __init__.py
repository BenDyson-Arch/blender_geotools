"""
blender_geotools — GeoJSON and 3D Tiles importer with shared CRS infrastructure.

GNU General Public License v3.0 — see LICENSE file.
Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS),
Copyright (C) domlysz — GPL-3.0.
"""

bl_info = {
    "name": "GeoTools",
    "description": "Import and analyse geospatial data: GeoJSON, OGC 3D Tiles, and more.",
    "author": "blender_geotools contributors",
    "version": (0, 1, 0),
    "blender": (5, 1, 0),
    "location": "File > Import  /  View3D > Sidebar > GeoTools",
    "license": "GPL-3.0-or-later",
    "category": "Import-Export",
    "doc_url": "",
    "tracker_url": "",
}

import logging

import bpy

from . import geoscene as _geoscene
from . import prefs as _prefs
from . import ui as _ui
from .core import operators as _core_ops

log = logging.getLogger(__name__)

# Submodule registry — add entries here as submodules are implemented.
# Each entry: (pref_flag, module_path)
_SUBMODULES = [
    ("mod_geojson",   ".geojson"),
    ("mod_gpkg",      ".gpkg"),
    ("mod_tiles3d",   ".tiles3d"),
    ("mod_dem",       ".dem"),
    ("mod_analysis",  ".analysis"),
    # ("mod_shapefile", ".shapefile"),  # not planned
    # ("mod_raster",    ".raster"),     # not planned
]

_loaded_submodules = []


def _get_prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return None


def register():
    # Register preferences first so submodules can read them
    _prefs.register()
    _geoscene.register()
    _core_ops.register()
    _ui.register()

    addon_prefs = _get_prefs()

    for flag, mod_path in _SUBMODULES:
        enabled = getattr(addon_prefs, flag, True) if addon_prefs else True
        if not enabled:
            continue
        try:
            import importlib
            mod = importlib.import_module(mod_path, package=__package__)
            mod.register()
            _loaded_submodules.append(mod)
            log.debug("Registered submodule: %s", mod_path)
        except Exception:
            log.error("Failed to register submodule %s", mod_path, exc_info=True)


def unregister():
    for mod in reversed(_loaded_submodules):
        try:
            mod.unregister()
        except Exception:
            log.error("Failed to unregister submodule %s", mod, exc_info=True)
    _loaded_submodules.clear()

    _ui.unregister()
    _core_ops.unregister()
    _geoscene.unregister()
    _prefs.unregister()
