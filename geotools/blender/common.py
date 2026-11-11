"""Helpers shared by the Blender modules."""

from __future__ import annotations

import logging
import textwrap
import tomllib

import bpy

from ..core import deps
from ..core.manifest import read_manifest

log = logging.getLogger(__name__)

ADDON_PACKAGE: str = __package__.rpartition(".")[0]
"""Root package of the extension, e.g. ``bl_ext.user_default.geotools``.

This is the key Blender uses for the add-on, so it is the preferences'
``bl_idname``.
"""


def extension_version() -> str:
    """The version declared in the bundled manifest, or ``"unknown"``."""
    try:
        return read_manifest().version
    except (OSError, tomllib.TOMLDecodeError, KeyError):
        log.warning("could not read blender_manifest.toml", exc_info=True)
        return "unknown"


def draw_dependency_status(layout: bpy.types.UILayout) -> None:
    """Draw the rusty-tiles status and a re-check button."""
    status = deps.probe_rusty_tiles()
    col = layout.column(align=True)
    if status.available:
        col.label(text=f"rusty-tiles {status.version or '(unknown version)'}", icon="CHECKMARK")
    else:
        col.alert = True
        col.label(text="rusty-tiles not available", icon="ERROR")
        for line in textwrap.wrap(status.error or "unknown error", 48)[:6]:
            col.label(text=line)
    layout.operator("geotools.check_dependencies", icon="FILE_REFRESH")
