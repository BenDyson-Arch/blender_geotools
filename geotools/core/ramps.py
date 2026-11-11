"""Colour ramps for presenting layers (see docs/design/m3-layers.md, section 4).

``bpy``-free numpy. A palette is a compact table of sRGB stops, evenly spaced
from the low end (row 0) to the high end (last row), and values are mapped to
colours by linear interpolation between neighbouring stops in sRGB, as
matplotlib does with its listed colormaps. No matplotlib is needed: the stop
tables below are sampled from the published palettes.

Every function returns 8-bit sRGB RGBA (``(N, 4) uint8``), ready for a byte
colour attribute or an 8-bit PNG, except :func:`srgb_bytes_to_linear`, which
turns those bytes into the linear floats glTF's COLOR_0 holds. Large inputs are
processed in row chunks, so the only full-size arrays are the input and the
output.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

__all__ = [
    "CATEGORY_COLOURS",
    "DEFAULT_PALETTE",
    "PALETTES",
    "apply_categories",
    "apply_ramp",
    "auto_range",
    "default_category_colours",
    "palette_stops",
    "ramp_stops",
    "srgb_bytes_to_linear",
]


def _hex_table(colours: str) -> np.ndarray:
    """``(K, 3)`` float64 sRGB in [0, 1] from space-separated ``#rrggbb``; read-only."""
    rows = [[int(c[i : i + 2], 16) for i in (1, 3, 5)] for c in colours.split()]
    table = np.array(rows, dtype=np.float64) / 255.0
    table.setflags(write=False)
    return table


PALETTES: dict[str, np.ndarray] = {
    # matplotlib viridis, magma and cividis, sampled at 10 even points.
    "viridis": _hex_table(
        "#440154 #482878 #3e4989 #31688e #26828e #1f9e89 #35b779 #6ece58 #b5de2b #fde725"
    ),
    "magma": _hex_table(
        "#000004 #180f3d #440f76 #721f81 #9e2f7f #cd4071 #f1605d #fd9668 #feca8d #fcfdbf"
    ),
    "cividis": _hex_table(
        "#00224e #123570 #3b496c #575d6d #707173 #8a8678 #a59c74 #c3b369 #e1cc55 #fee838"
    ),
    # Google's Turbo at 17 even points: the published polynomial approximation,
    # with the end points of the published lookup table.
    "turbo": _hex_table(
        "#30123b #493eaf #446aee #3295f7 #26bde1 #29ddbb #40f392 #66fd6d #96fa50 "
        "#c6eb3b #eed02d #ffab24 #ff801d #ee5415 #c92d0c #a11202 #7a0403"
    ),
    # ColorBrewer RdBu (11 classes): red for low values, blue for high ones.
    "diverging_rdbu": _hex_table(
        "#67001f #b2182b #d6604d #f4a582 #fddbc7 #f7f7f7 #d1e5f0 #92c5de #4393c3 #2166ac #053061"
    ),
    # Black for low values, white for high ones.
    "greys": _hex_table("#000000 #ffffff"),
}
"""Palette name -> ``(K, 3)`` sRGB stops in [0, 1], low end first."""

DEFAULT_PALETTE = "viridis"

CATEGORY_COLOURS: np.ndarray = np.round(
    _hex_table("#4e79a7 #f28e2b #e15759 #76b7b2 #59a14f #edc948 #b07aa1 #ff9da7 #9c755f #bab0ac")
    * 255.0
).astype(np.uint8)
"""Default category colours (Tableau 10), ``(10, 3) uint8`` sRGB, repeated when
there are more categories."""
CATEGORY_COLOURS.setflags(write=False)

_CHUNK = 1 << 20  # values per step, bounds temporary memory
_MAX_SAMPLES = 1 << 22  # values auto_range looks at, at most


def palette_stops(palette: str | np.ndarray) -> np.ndarray:
    """The ``(K, 3)`` float sRGB stops of a palette name or of a stop table itself.

    Raises ``KeyError`` for an unknown name and ``ValueError`` for a table that
    is not ``(K >= 2, 3 or 4)`` with values in [0, 1].
    """
    if isinstance(palette, str):
        if palette not in PALETTES:
            raise KeyError(f"unknown palette {palette!r}; use one of {', '.join(PALETTES)}")
        return PALETTES[palette]
    stops = np.asarray(palette, dtype=np.float64)
    if stops.ndim != 2 or stops.shape[0] < 2 or stops.shape[1] not in (3, 4):
        raise ValueError(f"a palette needs at least two RGB stops, got shape {stops.shape}")
    if not np.isfinite(stops).all() or stops.min() < 0.0 or stops.max() > 1.0:
        raise ValueError("palette stops must be in [0, 1]")
    return stops[:, :3]


def _rgba(colour: Sequence[float] | np.ndarray) -> np.ndarray:
    """A ``(4,) uint8`` RGBA from 3 or 4 integers in [0, 255]."""
    rgba = np.asarray(colour, dtype=np.float64).ravel()
    if rgba.shape[0] == 3:
        rgba = np.append(rgba, 255.0)
    if rgba.shape != (4,) or not np.isfinite(rgba).all():
        raise ValueError(f"a colour needs 3 or 4 components, got {colour!r}")
    return np.clip(np.round(rgba), 0, 255).astype(np.uint8)


def _missing(values: np.ndarray, nodata: float | None) -> np.ndarray:
    """True where a value is NaN, infinite or the no-data value."""
    if values.dtype.kind == "f":
        missing = ~np.isfinite(values)
        if nodata is not None and math.isfinite(float(nodata)):
            # Compare in the stored precision: a float32 attribute holds float32(nodata).
            missing |= values == values.dtype.type(nodata)
        return missing
    missing = np.zeros(values.shape, dtype=bool)
    if nodata is not None and math.isfinite(float(nodata)) and float(nodata).is_integer():
        missing |= values == int(nodata)
    return missing


def _as_values(values: object) -> np.ndarray:
    array = np.asarray(values)
    if array.dtype == np.bool_:
        array = array.view(np.uint8)
    if array.dtype.kind not in "fiu":
        raise ValueError(f"values must be numbers, got dtype {array.dtype}")
    return array.ravel()


def apply_ramp(
    values: object,
    vmin: float,
    vmax: float,
    palette: str | np.ndarray,
    *,
    nodata: float | None = None,
    nodata_rgba: Sequence[float] = (0, 0, 0, 0),
) -> np.ndarray:
    """Map ``values`` through ``palette``: ``(N, 4) uint8`` sRGB RGBA.

    ``vmin`` maps to the first stop and ``vmax`` to the last; values outside
    are clamped to the ends. NaN, infinities and ``nodata`` (compared in the
    values' own precision) get ``nodata_rgba``; every other value is opaque.
    With ``vmin == vmax``, values below get the low end, values above the high
    end and equal values the middle of the palette.
    """
    data = _as_values(values)
    stops = palette_stops(palette)
    vmin, vmax = float(vmin), float(vmax)
    if not (math.isfinite(vmin) and math.isfinite(vmax)) or vmin > vmax:
        raise ValueError(f"the ramp range must be finite with min <= max, got {vmin!r}, {vmax!r}")
    missing_rgba = _rgba(nodata_rgba)
    last = stops.shape[0] - 1
    out = np.empty((data.shape[0], 4), dtype=np.uint8)
    out[:, 3] = 255
    span = vmax - vmin
    for start in range(0, data.shape[0], _CHUNK):
        block = data[start : start + _CHUNK].astype(np.float64)
        missing = _missing(data[start : start + _CHUNK], nodata)
        if span > 0.0:
            t = (block - vmin) / span
        else:
            t = np.where(block < vmin, 0.0, np.where(block > vmax, 1.0, 0.5))
        np.nan_to_num(t, copy=False, nan=0.0, posinf=1.0, neginf=0.0)
        np.clip(t, 0.0, 1.0, out=t)
        position = t * last
        index = np.minimum(position.astype(np.int64), last - 1)
        frac = (position - index)[:, None]
        rgb = stops[index] * (1.0 - frac) + stops[index + 1] * frac
        rows = out[start : start + _CHUNK]
        rows[:, :3] = np.clip(np.round(rgb * 255.0), 0, 255).astype(np.uint8)
        if missing.any():
            rows[missing] = missing_rgba
    return out


def default_category_colours(count: int) -> np.ndarray:
    """``(count, 4) uint8`` opaque colours for categories 0 to count - 1."""
    rgb = CATEGORY_COLOURS[np.arange(max(int(count), 0)) % CATEGORY_COLOURS.shape[0]]
    return np.column_stack((rgb, np.full(rgb.shape[0], 255, dtype=np.uint8)))


def apply_categories(
    values: object,
    colours: object,
    *,
    nodata: float | None = None,
    nodata_rgba: Sequence[float] = (0, 0, 0, 0),
) -> np.ndarray:
    """Colour category values: value ``i`` gets ``colours[i]``. ``(N, 4) uint8``.

    ``colours`` is ``(C, 3)`` or ``(C, 4)`` with integers in [0, 255]. ``nodata``,
    non-integral and out-of-range values (and NaN or inf) get ``nodata_rgba``.
    """
    data = _as_values(values)
    table = np.asarray(colours, dtype=np.float64)
    if table.ndim != 2 or table.shape[1] not in (3, 4):
        raise ValueError(f"category colours must be (C, 3) or (C, 4), got shape {table.shape}")
    if table.shape[1] == 3:
        table = np.column_stack((table, np.full(table.shape[0], 255.0)))
    table = np.clip(np.round(table), 0, 255).astype(np.uint8)
    table = np.vstack((table, _rgba(nodata_rgba)[None, :]))  # last row: no data
    count = table.shape[0] - 1
    out = np.empty((data.shape[0], 4), dtype=np.uint8)
    for start in range(0, data.shape[0], _CHUNK):
        chunk = data[start : start + _CHUNK]
        missing = _missing(chunk, nodata)
        if chunk.dtype.kind == "f":
            whole = np.isfinite(chunk) & (chunk == np.floor(chunk))
            missing |= ~whole
            index = np.where(whole, chunk, -1).astype(np.int64)
        else:
            index = chunk.astype(np.int64)
        index = np.where(missing | (index < 0) | (index >= count), count, index)
        out[start : start + _CHUNK] = table[index]
    return out


def auto_range(
    values: object,
    nodata: float | None = None,
    percentiles: tuple[float, float] = (2, 98),
    *,
    max_samples: int = _MAX_SAMPLES,
) -> tuple[float, float]:
    """A ramp range from the values present: the given percentiles.

    NaN, infinities and ``nodata`` are ignored. Arrays longer than
    ``max_samples`` are subsampled with an even stride (deterministic), which
    moves a percentile by far less than one ramp step. Raises ``ValueError``
    when no value is left.
    """
    data = _as_values(values)
    low_p, high_p = (float(p) for p in percentiles)
    if not 0.0 <= low_p <= high_p <= 100.0:
        raise ValueError(f"percentiles must be 0 <= low <= high <= 100, got {percentiles!r}")
    if max_samples > 0 and data.shape[0] > max_samples:
        data = data[:: -(-data.shape[0] // max_samples)]
    kept = data[~_missing(data, nodata)]
    if kept.size == 0:
        raise ValueError("there are no values to take a range from (all are no-data)")
    low, high = np.percentile(kept.astype(np.float64), (low_p, high_p))
    return float(low), float(high)


def ramp_stops(
    palette: str | np.ndarray, vmin: float, vmax: float, n: int = 11
) -> list[dict[str, object]]:
    """``n`` evenly spaced legend entries ``{"value": float, "rgba": [r, g, b, a]}``.

    The colours are exactly what :func:`apply_ramp` gives at those values, so a
    legend drawn from them matches the published tiles.
    """
    n = int(n)
    if n < 2:
        raise ValueError(f"a legend needs at least two stops, got {n}")
    values = np.linspace(float(vmin), float(vmax), n)
    if vmin == vmax:
        values[:] = float(vmin)
    colours = apply_ramp(values, vmin, vmax, palette)
    return [
        {"value": float(v), "rgba": [int(c) for c in rgba]}
        for v, rgba in zip(values.tolist(), colours, strict=True)
    ]


def _srgb_table() -> np.ndarray:
    c = np.arange(256, dtype=np.float64) / 255.0
    return np.where(c < 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


_SRGB_TO_LINEAR = _srgb_table()


def srgb_bytes_to_linear(rgba: np.ndarray) -> np.ndarray:
    """``(N, 4) uint8`` sRGB RGBA as ``(N, 4) float32`` linear RGB and alpha in [0, 1].

    The same conversion Blender applies when a byte colour attribute is read
    through its ``color`` property: the IEC 61966-2-1 curve on red, green and
    blue, and alpha divided by 255.
    """
    rgba = np.asarray(rgba)
    if rgba.dtype != np.uint8 or rgba.ndim != 2 or rgba.shape[1] != 4:
        raise ValueError(f"expected (N, 4) uint8 colours, got {rgba.dtype} {rgba.shape}")
    out = np.empty(rgba.shape, dtype=np.float32)
    out[:, :3] = _SRGB_TO_LINEAR[rgba[:, :3]]
    out[:, 3] = rgba[:, 3]
    out[:, 3] *= np.float32(1.0 / 255.0)
    return out
