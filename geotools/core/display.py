"""Numbers and counts formatted for people to read in the sidebar.

Storage keeps binary64 text (``repr``) so nothing is lost; these functions are
only for display. They never raise on a finite or non-finite float, and give
"" for ``None`` where that makes sense, so a panel can call them on any
cached value.
"""

from __future__ import annotations

import math

EN_DASH = "–"
TIMES = "×"

_SCI_HIGH = 1e15
_SCI_LOW = 1e-4


def format_number(value: float | None, digits: int = 3) -> str:
    """``value`` with about ``digits`` significant figures and thousands separators.

    Whole parts are never rounded away: 14400.0 is "14,400", 0.46055 is
    "0.461", 39.884 is "39.9" and 1.0 is "1". Trailing zeros after the point
    are dropped. Very large or very small magnitudes use scientific notation.
    """
    if value is None:
        return ""
    value = float(value)
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "∞" if value > 0 else "-∞"
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= _SCI_HIGH or magnitude < _SCI_LOW:
        return f"{value:.{max(digits - 1, 0)}e}"
    decimals = max(digits - 1 - math.floor(math.log10(magnitude)), 0)
    text = f"{value:,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def format_range(low: float | None, high: float | None, units: str = "", digits: int = 3) -> str:
    """ "0.461 – 39.9 deg"; one value when both ends are equal; "" if either is unknown."""
    if low is None or high is None:
        return ""
    text = format_number(low, digits)
    if format_number(high, digits) != text:
        text = f"{text} {EN_DASH} {format_number(high, digits)}"
    return f"{text} {units}".strip() if units.strip() else text


def format_count(count: int) -> str:
    """``count`` with thousands separators."""
    return f"{int(count):,}"


def plural(count: int, word: str, many: str | None = None) -> str:
    """ "1 object", "2 objects", "14,400 faces"."""
    noun = word if count == 1 else (many or f"{word}s")
    return f"{format_count(count)} {noun}"


def format_size(size: tuple[float, float, float], digits: int = 3) -> str:
    """Bounding-box size in metres: "200 × 200 × 25.2 m" ("1.25 km" sides when large)."""
    largest = max(abs(float(v)) for v in size)
    if largest >= 10_000:
        parts = [format_number(float(v) / 1000.0, digits) for v in size]
        unit = "km"
    else:
        parts = [format_number(float(v), digits) for v in size]
        unit = "m"
    return f" {TIMES} ".join(parts) + f" {unit}"


def format_seconds(seconds: float) -> str:
    """ "42 s", "3 min 05 s", "1 h 02 min"."""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def format_gib(nbytes: float) -> str:
    """Bytes as GiB with one decimal: "23.5 GiB"."""
    return f"{float(nbytes) / 2**30:,.1f} GiB"
