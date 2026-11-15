"""Small drawing helpers shared by the sidebar panels (no classes to register).

The sidebar is about 270 px wide at UI scale 1. The helpers keep to that:
labels in a 40% column like ``use_property_split``, text wrapped to the
region's width, and one status icon per panel header.
"""

from __future__ import annotations

import math
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


LINE_MARGIN_PX = 92
"""What a sidebar line loses besides its text, in pixels at UI scale 1: the
category tabs, the panel's and a box's margins, an icon and the label's
padding. Measured in Blender 5.2: a label with an icon in a box clips beyond
about 190 px of text in the default 280 px sidebar."""
MEASURE_SLACK = 1.03
"""``blf`` and Blender's own layout can disagree on a text's width by a few
pixels, so measured text must fit with this much to spare."""


def _ui_scale(context: bpy.types.Context | None) -> float:
    prefs = getattr(context, "preferences", None) if context is not None else None
    scale = prefs.system.ui_scale if prefs else 1.0
    return scale if scale > 0 else 1.0  # 0 in background mode


def text_px(context: bpy.types.Context | None, text: str) -> float | None:
    """``text``'s width in pixels in the UI font at the current UI scale.

    Measured with ``blf``, which is cheap enough for ``draw()`` (no mesh, file
    or subprocess). ``None`` where ``blf`` is not available.
    """
    try:
        import blf
    except ImportError:
        return None
    prefs = getattr(context, "preferences", None) if context is not None else None
    points = prefs.ui_styles[0].widget.points if prefs else 11.0
    blf.size(0, points * _ui_scale(context))
    return blf.dimensions(0, text)[0]


def line_px(context: bpy.types.Context | None, reserve_px: float = 0.0) -> float | None:
    """The text room on a line of the current region, in pixels at the current scale.

    ``reserve_px`` (at UI scale 1) is what the line needs besides
    :data:`LINE_MARGIN_PX`, such as a list's box. ``None`` without a region.
    """
    region = getattr(context, "region", None) if context is not None else None
    width = getattr(region, "width", 0) if region is not None else 0
    if not width:
        return None
    return width - (LINE_MARGIN_PX + reserve_px) * _ui_scale(context)


def fits(context: bpy.types.Context | None, text: str, room: float) -> bool | None:
    """Whether ``text`` fits in ``room`` pixels (:data:`MEASURE_SLACK`); ``None`` without blf."""
    width = text_px(context, text)
    return None if width is None else width * MEASURE_SLACK <= room


def _lines(
    context: bpy.types.Context | None, text: str, room: float | None, chars: int
) -> list[str]:
    """``text`` wrapped to ``room`` pixels, measured; to ``chars`` characters without blf."""
    if room is None or fits(context, "", room) is None:
        return textwrap.wrap(text, chars) or [""]
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if not line or fits(context, candidate, room):
            line = candidate
            continue
        lines.append(line)
        line = word
    if line:
        lines.append(line)
    out: list[str] = []
    for whole in lines:  # a word wider than the line is broken where it must be
        rest = whole
        while not fits(context, rest, room) and len(rest) > 1:
            keep = len(rest) - 1
            while keep > 1 and not fits(context, rest[:keep], room):
                keep -= 1
            out.append(rest[:keep])
            rest = rest[keep:]
        out.append(rest)
    return out or [""]


def _draw_lines(layout: bpy.types.UILayout, lines: list[str], icon: str, max_lines: int) -> None:
    if len(lines) > max_lines:
        lines = [*lines[: max_lines - 1], lines[max_lines - 1].rstrip(".,;") + " ..."]
    for i, line in enumerate(lines):
        layout.label(text=line, icon=icon if i == 0 else "BLANK1" if icon != "NONE" else "NONE")


def wrapped(
    layout: bpy.types.UILayout,
    context: bpy.types.Context | None,
    text: str,
    icon: str = "NONE",
    max_lines: int = 8,
) -> None:
    """Draw ``text`` as labels wrapped to the region, with ``icon`` on the first line.

    Lines are measured (:func:`text_px`) against :func:`line_px`; only without
    ``blf`` or a region do they fall back to :func:`wrap_chars`.
    """
    col = layout.column(align=True)
    _draw_lines(col, _lines(context, text, line_px(context), wrap_chars(context)), icon, max_lines)


def clipped(context: bpy.types.Context | None, text: str, reserve_px: float = 0.0) -> str:
    """``text`` cut at the end, with an ellipsis, to fit a line of the current region.

    ``reserve_px`` is as for :func:`line_px`. Blender elides an over-long
    label in the middle, which hides both the subject's end and the detail's
    start; cutting at the end keeps the start readable. The text is measured
    (:func:`fits`); only without ``blf`` or a region does the cut fall back to
    the character estimate of :func:`wrap_chars`. At least 8 characters are
    kept.
    """
    room = line_px(context, reserve_px)
    whole = None if room is None else fits(context, text, room)
    if room is None or whole is None:
        chars = max(wrap_chars(context) - math.ceil(reserve_px / _CHAR_PX), 8)
        return text if len(text) <= chars else _cut(text, chars - 1)
    if whole:
        return text
    # The longest cut that fits; cuts only grow with the characters kept.
    low, high = 7, len(text) - 1
    while low < high:
        mid = (low + high + 1) // 2
        if fits(context, _cut(text, mid), room):
            low = mid
        else:
            high = mid - 1
    return _cut(text, low)


def _cut(text: str, keep: int) -> str:
    return text[:keep].rstrip(" ,;:") + "\u2026"


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
    line = line_px(context)
    room = None if line is None else line * (1.0 - SPLIT_FACTOR)
    chars = max(int(wrap_chars(context) * (1.0 - SPLIT_FACTOR)), 12)
    _draw_lines(right, _lines(context, text, room, chars), icon, max_lines)


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
