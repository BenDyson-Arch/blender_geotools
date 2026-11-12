"""Blender glue: preferences, scene properties, operators and panels.

Each module in :data:`MODULES` exposes ``register``/``unregister``. They are
registered in order and unregistered in reverse.
"""

from __future__ import annotations

import logging
from types import ModuleType

from ..core import deps
from . import (
    analysis,
    analysis_presets,
    archaeo,
    bake_texture,
    checks,
    contours,
    crs_picker,
    data_origin,
    glb_export,
    hydrology,
    layers,
    links,
    operators,
    panels,
    preferences,
    presentation,
    profiles,
    properties,
    publish,
    raster_io,
    remedies,
    source,
    sun_hours,
    vectors,
)

log = logging.getLogger(__name__)

MODULES: tuple[ModuleType, ...] = (
    preferences,
    properties,
    crs_picker,
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
    glb_export,
    publish,
    bake_texture,
    analysis,
    sun_hours,
    archaeo,
    analysis_presets,
    hydrology,
    contours,
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


def unregister() -> None:
    """Unregister every module in reverse order."""
    for module in reversed(MODULES):
        module.unregister()
