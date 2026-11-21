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


def draw_dependency_status(layout: bpy.types.UILayout, *, show_paths: bool = False) -> None:
    """Draw the restart notice, the rusty-tiles and pyproj status, and the buttons.

    ``show_paths`` adds the PROJ data directory (for the wider preferences).
    The notice is the last ``deps.check_for_update`` verdict; this never stats
    a file (About's ``draw`` compares first).
    """
    if deps.update_pending():
        box = layout.column(align=True)
        box.alert = True
        for i, line in enumerate(textwrap.wrap(deps.RESTART_MESSAGE, 48)):
            box.label(text=line, icon="ERROR" if i == 0 else "NONE")
    _draw_one(layout, deps.probe_rusty_tiles(), consequence=None)
    pyproj = deps.probe_pyproj()
    col = _draw_one(layout, pyproj, consequence="Imports cannot reproject")
    if show_paths and pyproj.available and pyproj.data_dir:
        for line in textwrap.wrap(f"PROJ data: {pyproj.data_dir}", 72)[:3]:
            col.label(text=line)
    row = layout.row(align=True)
    row.operator("geotools.check_dependencies", icon="FILE_REFRESH")
    row.operator("geotools.copy_details", icon="COPYDOWN")


def _draw_one(
    layout: bpy.types.UILayout, status: deps.DependencyStatus, consequence: str | None
) -> bpy.types.UILayout:
    col = layout.column(align=True)
    if status.available:
        col.label(text=status.summary, icon="CHECKMARK")
        return col
    col.alert = True
    col.label(text=f"{status.name} not available", icon="ERROR")
    for line in textwrap.wrap(status.error or "unknown error", 48)[:6]:
        col.label(text=line)
    if consequence:
        col.label(text=consequence)
    return col


def redraw_ui(context: bpy.types.Context) -> None:
    """Redraw every area, so panels show state an operator just changed."""
    window_manager = context.window_manager
    if window_manager is None:
        return
    for window in window_manager.windows:
        for area in window.screen.areas:
            area.tag_redraw()
