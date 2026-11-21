"""SVG legends for variant tilesets: the colours a variant's layer was published in.

``bpy``-free. A variant such as ``site.slope.3tz`` is colour only; its legend
``site.slope.legend.svg`` is written beside it (:func:`legend_path`) and named
in its build.json (``legend``). The legend is drawn from the variant's record
(``core.presentation.variant_record``) and the layer's record
(``core.layers.to_record``), both as build.json holds them:

- the layer's label (or name) and units as the title, and the palette and
  scale under it;
- a ramp layer: a bar of the palette over the published range, with the
  record's ``ramp_stops`` as ticks, highest value at the top, and the range,
  marked automatic where it was;
- a category layer: one swatch per category, value and label;
- a no-data swatch.

The bar's colours come from :func:`geotools.core.ramps.apply_ramp` at the
palette's own stops, spaced as the ramp spaces them (by ``log10`` on a log
scale). Between those stops both the ramp and an SVG gradient interpolate
linearly in sRGB, so the bar shows the colour each published value got.
"""

from __future__ import annotations

import math
import os
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.sax.saxutils import escape, unescape

import numpy as np

from .ramps import apply_ramp, palette_stops

__all__ = ["LEGEND_SUFFIX", "legend_path", "legend_svg", "write_legend"]

LEGEND_SUFFIX = ".legend.svg"

_FONT = "font-family='sans-serif'"
_PAD = 16
_BAR_WIDTH = 24
_BAR_HEIGHT = 220
_ROW = 22
_SWATCH = 16
_CHAR = 0.6  # rough width of a sans-serif character, in font sizes, for the canvas width
_TEXT = re.compile(r"<text x='([0-9.]+)'[^>]* font-size='([0-9]+)'[^>]*>([^<]*)</text>")


def legend_path(output: Path) -> Path:
    """``<dir>/<stem>.legend.svg`` beside the variant ``<dir>/<stem>.3tz`` (or ``.glb``)."""
    output = Path(output)
    return output.with_name(output.stem + LEGEND_SUFFIX)


def _layer_of(variant: Mapping[str, object], layers: Sequence[Mapping[str, object]]) -> Mapping:
    name = variant.get("layer")
    return next((layer for layer in layers if layer.get("name") == name), {"name": name})


def _rgb(rgba: Sequence[object]) -> tuple[str, float]:
    """``(fill, opacity)`` of an 8-bit sRGB RGBA list."""
    r, g, b, *alpha = (int(c) for c in rgba)  # pyright: ignore[reportArgumentType]
    return f"#{r:02x}{g:02x}{b:02x}", (alpha[0] if alpha else 255) / 255.0


def _fill(rgba: Sequence[object]) -> str:
    colour, opacity = _rgb(rgba)
    if opacity >= 1.0:
        return f"fill='{colour}'"
    return f"fill='{colour}' fill-opacity='{opacity:.3g}'"


def _text(x: float, y: float, text: str, size: int = 12, extra: str = "") -> str:
    return (
        f"<text x='{x:g}' y='{y:g}' {_FONT} font-size='{size}' fill='#222222'{extra}>"
        f"{escape(text)}</text>"
    )


def _decimals(values: Sequence[float], step: float) -> int:
    """Fewest decimals that show every value exactly, at most about three significant
    digits of the step between them."""
    most = max(0, 2 - math.floor(math.log10(step)))
    for d in range(most + 1):
        if all(abs(round(v, d) - v) <= step * 1e-6 for v in values):
            return d
    return most


def _significant(value: float) -> str:
    """Three significant digits, never in exponent notation."""
    text = f"{value:.3g}"
    if "e" in text:
        text = f"{float(text):f}".rstrip("0").rstrip(".")
    return text


def _labels(values: list[float], log: bool) -> list[str]:
    step = (values[-1] - values[0]) / (len(values) - 1) if len(values) > 1 else 0.0
    if log or not step > 0:
        return [_significant(v) for v in values]
    d = _decimals(values, step)
    return [f"{v:.{d}f}" for v in values]


def _title(layer: Mapping[str, object]) -> tuple[str, str]:
    name = str(layer.get("label") or "").strip() or str(layer.get("name") or "")
    units = str(layer.get("units") or "").strip()
    return name, units


def _position(value: float, low: float, high: float, log: bool) -> float:
    """Where ``value`` sits on the bar, 0 at the low end and 1 at the high end."""
    if high == low:
        return 0.5
    if log:
        return (math.log10(value) - math.log10(low)) / (math.log10(high) - math.log10(low))
    return (value - low) / (high - low)


def _ramp_body(variant: Mapping[str, object], units: str, top: float) -> tuple[list[str], float]:
    palette = str(variant["palette"])
    span = variant["range"]
    assert isinstance(span, Mapping)
    low, high = float(span["min"]), float(span["max"])
    log = variant.get("scale") == "LOG"
    stops = palette_stops(palette)
    t = np.linspace(0.0, 1.0, stops.shape[0])
    if log:
        values = 10.0 ** (math.log10(low) + t * (math.log10(high) - math.log10(low)))
        values[[0, -1]] = low, high
    else:
        values = low + t * (high - low)
        values[[0, -1]] = low, high
    colours = apply_ramp(values, low, high, palette, log=log)
    out = ["<defs><linearGradient id='ramp' x1='0' y1='1' x2='0' y2='0'>"]
    for offset, rgba in zip(t.tolist(), colours.tolist(), strict=True):
        colour, _opacity = _rgb(rgba)
        out.append(f"<stop offset='{offset:.6g}' stop-color='{colour}'/>")
    out.append("</linearGradient></defs>")
    x = _PAD
    out.append(
        f"<rect x='{x}' y='{top:g}' width='{_BAR_WIDTH}' height='{_BAR_HEIGHT}' "
        "fill='url(#ramp)' stroke='#888888' stroke-width='0.5'/>"
    )
    ticks = variant.get("ramp_stops") or []
    assert isinstance(ticks, list)
    tick_values = [float(s["value"]) for s in ticks]
    if low == high:
        tick_values = tick_values[:1]
    labels = _labels(tick_values, log) if tick_values else []
    for value, label in zip(tick_values, labels, strict=True):
        y = top + (1.0 - _position(value, low, high, log)) * _BAR_HEIGHT
        out.append(
            f"<line x1='{x + _BAR_WIDTH}' y1='{y:.2f}' x2='{x + _BAR_WIDTH + 5}' y2='{y:.2f}' "
            "stroke='#222222' stroke-width='1'/>"
        )
        out.append(_text(x + _BAR_WIDTH + 9, y + 4, label, extra=" class='tick'"))
    y = top + _BAR_HEIGHT + 22
    auto = [w for w, k in (("minimum", "auto_min"), ("maximum", "auto_max")) if span.get(k)]
    span_text = f"Range {labels[0]} to {labels[-1]}" if labels else "Range"
    if units:
        span_text += f" {units}"
    if auto:
        span_text += f" ({' and '.join(auto)} automatic)"
    out.append(_text(x, y, span_text, extra=" class='range'"))
    return out, y + 8


def _category_body(variant: Mapping[str, object], top: float) -> tuple[list[str], float]:
    out = []
    y = top
    categories = variant.get("categories") or []
    assert isinstance(categories, list)
    for category in categories:
        out.append(
            f"<rect class='category' x='{_PAD}' y='{y:g}' width='{_SWATCH}' height='{_SWATCH}' "
            f"{_fill(category['rgba'])} stroke='#888888' stroke-width='0.5'/>"
        )
        text = f"{category['value']}  {category['label']}"
        out.append(_text(_PAD + _SWATCH + 8, y + 12.5, text))
        y += _ROW
    return out, y


def legend_svg(variant: Mapping[str, object], layer: Mapping[str, object]) -> str:
    """The SVG legend of a variant record, titled from the layer's record."""
    name, units = _title(layer)
    title = f"{name} ({units})" if units else name
    categories = variant.get("categories") is not None
    if categories:
        subtitle = "Categories"
    else:
        scale = "log scale" if variant.get("scale") == "LOG" else "linear scale"
        subtitle = f"{variant['palette']}, {scale}"
    body = [_text(_PAD, 24, title, 14, " font-weight='bold' class='title'")]
    body.append(_text(_PAD, 42, subtitle, extra=" class='subtitle'"))
    if categories:
        rows, y = _category_body(variant, 56)
    else:
        rows, y = _ramp_body(variant, units, 56)
    body += rows
    nodata = variant.get("nodata_rgba") or [0, 0, 0, 0]
    assert isinstance(nodata, list)
    y += 8
    body.append(
        f"<rect class='nodata' x='{_PAD}' y='{y:g}' width='{_SWATCH}' height='{_SWATCH}' "
        f"{_fill(nodata)} stroke='#888888' stroke-width='0.5'/>"
    )
    body.append(_text(_PAD + _SWATCH + 8, y + 12.5, "No data"))
    height = math.ceil(y + _SWATCH + _PAD)
    right = max(
        float(x) + _CHAR * int(size) * len(unescape(text))
        for x, size, text in _TEXT.findall("\n".join(body))
    )
    width = math.ceil(max(200.0, right + _PAD))
    head = (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}'>\n"
        f"<title>{escape(title)}</title>\n"
        f"<rect width='100%' height='100%' fill='#ffffff'/>\n"
    )
    return head + "\n".join(body) + "\n</svg>\n"


def write_legend(
    output: Path, variant: Mapping[str, object], layers: Sequence[Mapping[str, object]]
) -> Path:
    """Write the legend of ``variant`` beside ``output``, atomically; return its path."""
    path = legend_path(output)
    text = legend_svg(variant, _layer_of(variant, layers))
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path
