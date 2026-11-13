"""Small drawing helpers shared by the sidebar panels (no classes to register).

The sidebar is about 270 px wide at UI scale 1. The helpers keep to that:
labels in a 40% column like ``use_property_split``, text wrapped to the
region's width, and one status icon per panel header.
"""

from __future__ import annotations

import textwrap

import bpy

# Panel header states, shown with draw_header. ERROR and WARNING use the same
# icons as issue severities (properties.SEVERITY_ICONS).
DONE = "DONE"
TODO = "TODO"
WARNING = "WARNING"
ERROR = "ERROR"
BUSY = "BUSY"

STATUS_ICONS = {
    DONE: "CHECKMARK",
    TODO: "RADIOBUT_OFF",
    WARNING: "ERROR",
    ERROR: "CANCEL",
    BUSY: "SORTTIME",
}

SPLIT_FACTOR = 0.4
"""The label column of ``use_property_split``."""

_CHAR_PX = 6.5  # average glyph width of the UI font at scale 1
_MARGIN_PX = 56  # tab strip, panel margins and an icon
DEFAULT_WRAP = 36


def wrap_chars(context: bpy.types.Context | None) -> int:
    """How many characters fit on a line of the current region (36 without one)."""
    region = getattr(context, "region", None) if context is not None else None
    width = getattr(region, "width", 0) if region is not None else 0
    if not width:
        return DEFAULT_WRAP
    scale = context.preferences.system.ui_scale if context.preferences else 1.0
    return max(int((width / max(scale, 0.1) - _MARGIN_PX) / _CHAR_PX), 16)


def wrapped(
    layout: bpy.types.UILayout,
    context: bpy.types.Context | None,
    text: str,
    icon: str = "NONE",
    max_lines: int = 8,
) -> None:
    """Draw ``text`` as labels wrapped to the region, with ``icon`` on the first line."""
    col = layout.column(align=True)
    lines = textwrap.wrap(text, wrap_chars(context)) or [""]
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1].rstrip(".,;") + " ..."]
    for i, line in enumerate(lines):
        col.label(text=line, icon=icon if i == 0 else "BLANK1" if icon != "NONE" else "NONE")


def clipped(text: str, chars: int) -> str:
    """``text`` cut at the end to ``chars`` characters, with an ellipsis.

    Blender elides an over-long label in the middle, which hides both the
    subject's end and the detail's start; cutting at the end keeps the start
    readable. ``chars`` below 8 is treated as 8.
    """
    chars = max(chars, 8)
    if len(text) <= chars:
        return text
    return text[: chars - 1].rstrip(" ,;:") + "\u2026"


def info_wrapped(
    layout: bpy.types.UILayout,
    context: bpy.types.Context | None,
    label: str,
    text: str,
    icon: str = "NONE",
    max_lines: int = 3,
) -> None:
    """Like :func:`info_row`, but ``text`` wraps in the value column instead of being cut."""
    split = layout.split(factor=SPLIT_FACTOR)
    left = split.column()
    left.alignment = "RIGHT"
    left.label(text=label)
    right = split.column(align=True)
    chars = max(int(wrap_chars(context) * (1.0 - SPLIT_FACTOR)), 12)
    lines = textwrap.wrap(text, chars) or [""]
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1].rstrip(".,;") + " ..."]
    for i, line in enumerate(lines):
        right.label(text=line, icon=icon if i == 0 else "BLANK1" if icon != "NONE" else "NONE")


def split_column(layout: bpy.types.UILayout) -> bpy.types.UILayout:
    """A column with ``use_property_split`` on and no decorators."""
    col = layout.column()
    col.use_property_split = True
    col.use_property_decorate = False
    return col


def info_row(
    layout: bpy.types.UILayout, label: str, value: str, icon: str = "NONE"
) -> bpy.types.UILayout:
    """A read-only ``label  value`` row aligned with ``use_property_split`` rows.

    Returns the value side, so a caller can add a button after the value.
    """
    split = layout.split(factor=SPLIT_FACTOR)
    left = split.column()
    left.alignment = "RIGHT"
    left.label(text=label)
    right = split.row(align=True)
    right.label(text=value, icon=icon)
    return right


def header_status(layout: bpy.types.UILayout, state: str | None) -> None:
    """The status icon of a panel header; nothing for ``None``."""
    if state is not None:
        layout.label(text="", icon=STATUS_ICONS[state])


def big_button(layout: bpy.types.UILayout, scale: float = 1.4) -> bpy.types.UILayout:
    """A row for a primary action button."""
    row = layout.row()
    row.scale_y = scale
    return row
