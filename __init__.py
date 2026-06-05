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
    "version": (0, 2, 0),
    "blender": (5, 1, 0),
    "location": "File > Import  /  View3D > Sidebar > GeoTools",
    "license": "GPL-3.0-or-later",
    "category": "Import-Export",
    "doc_url": "",
    "tracker_url": "",
}

import importlib
import logging

import bpy

from . import geoscene as _geoscene
from . import prefs as _prefs
from . import ui as _ui
from .api.registry import iter_addons
from .core import operators as _core_ops

log = logging.getLogger(__name__)

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
    loaded_paths = set()

    for addon in iter_addons():
        enabled = getattr(addon_prefs, addon.pref_flag, True) if addon_prefs else True
        if not enabled or addon.module_path in loaded_paths:
            continue
        try:
            mod = importlib.import_module(addon.module_path)
            mod.register()
            _loaded_submodules.append(mod)
            loaded_paths.add(addon.module_path)
            log.debug("Registered addon module: %s", addon.module_path)
        except Exception:
            log.error(
                "Failed to register addon module %s", addon.module_path, exc_info=True
            )


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
