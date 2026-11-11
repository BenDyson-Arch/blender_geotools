"""Blender glue: preferences, scene properties, operators and panels.

Each module in :data:`MODULES` exposes ``register``/``unregister``. They are
registered in order and unregistered in reverse.
"""

from __future__ import annotations

import logging
from types import ModuleType

from ..core import deps
from . import checks, operators, panels, preferences, profiles, properties, publish, source

log = logging.getLogger(__name__)

MODULES: tuple[ModuleType, ...] = (
    preferences,
    properties,
    operators,
    source,
    checks,
    profiles,
    publish,
    panels,
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
