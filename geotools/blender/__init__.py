"""Blender glue: preferences, scene properties, operators and panels.

Each module in :data:`MODULES` exposes ``register``/``unregister``. They are
registered in order and unregistered in reverse.
"""

from __future__ import annotations

import logging
from types import ModuleType

from ..core import deps
from ..core.manifest import MANIFEST_PATH
from . import (
    analysis,
    analysis_presets,
    archaeo,
    bake_texture,
    blend_paths,
    checks,
    common,
    contours,
    crs_picker,
    data_origin,
    glb_export,
    hydrology,
    inspect,
    layers,
    least_cost,
    links,
    live_view,
    operators,
    panels,
    preferences,
    presentation,
    profiles,
    properties,
    publish,
    raster_io,
    remedies,
    scene_georef,
    source,
    sun_hours,
    texture_cleanup,
    texture_paint,
    vectors,
)

log = logging.getLogger(__name__)

MODULES: tuple[ModuleType, ...] = (
    preferences,
    blend_paths,
    properties,
    crs_picker,
    scene_georef,
    operators,
    source,
    links,
    data_origin,
    vectors,
    raster_io,
    checks,
    remedies,
    profiles,
    layers,
    presentation,
    live_view,
    glb_export,
    publish,
    inspect,
    bake_texture,
    analysis,
    sun_hours,
    archaeo,
    analysis_presets,
    least_cost,
    hydrology,
    contours,
    texture_cleanup,
    texture_paint,
    panels,  # last: the sidebar draws what the modules above register
)


def register() -> None:
    """Register every module, rolling back the ones done if one fails."""
    done: list[ModuleType] = []
    try:
        for module in MODULES:
            module.register()
            done.append(module)
    except Exception:
        for module in reversed(done):
            try:
                module.unregister()
            except Exception:  # keep rolling back
                log.exception("rollback of %s failed", module.__name__)
        raise
    # Probe once at enable time so the status is logged and cached for the UI.
    # The probe never raises, so a broken wheel cannot block registration.
    deps.probe_rusty_tiles()
    # What this process holds, to tell an in-place update later (os.stat only).
    deps.record_session(common.extension_version(), (str(MANIFEST_PATH),))


def unregister() -> None:
    """Unregister every module in reverse order."""
    for module in reversed(MODULES):
        module.unregister()
