"""Numbers formatted for the sidebar (geotools.core.display)."""

import math

import pytest

from geotools.core import display as d


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (0.0, "0"),
        (-0.0, "0"),
        (1.0, "1"),
        (0.46055179834365845, "0.461"),
        (39.88410568237305, "39.9"),
        (14400.0, "14,400"),
        (6961000.0, "6,961,000"),
        (-12.5876, "-12.6"),
        (0.5, "0.5"),
        (0.99996, "1"),
        (999.6, "1,000"),
        (0.0001, "0.0001"),
        (0.00001234, "1.23e-05"),
        (2.5e15, "2.50e+15"),
        (math.nan, "NaN"),
        (math.inf, "∞"),
        (-math.inf, "-∞"),
    ],
)
def test_format_number(value, expected) -> None:
    assert d.format_number(value) == expected


def test_format_number_digits() -> None:
    assert d.format_number(3.14159265, 5) == "3.1416"
    assert d.format_number(152.123456789, 10) == "152.1234568"


def test_format_range() -> None:
    assert d.format_range(0.46055179834365845, 39.88410568237305, "deg") == ("0.461 – 39.9 deg")
    assert d.format_range(0.0, 1.0) == "0 – 1"
    assert d.format_range(2.0, 2.0001, "m") == "2 m"
    assert d.format_range(None, 1.0) == ""
    assert d.format_range(1.0, None, "m") == ""


def test_counts_and_plurals() -> None:
    assert d.format_count(14641) == "14,641"
    assert d.plural(1, "object") == "1 object"
    assert d.plural(2, "object") == "2 objects"
    assert d.plural(3, "mesh", "meshes") == "3 meshes"
    assert d.plural(14400, "face") == "14,400 faces"


def test_format_size() -> None:
    assert d.format_size((200.0, 200.0, 25.1752)) == "200 × 200 × 25.2 m"
    assert d.format_size((12500.0, 800.0, 40.0)) == "12.5 × 0.8 × 0.04 km"


def test_format_seconds_and_gib() -> None:
    assert d.format_seconds(42.9) == "42 s"
    assert d.format_seconds(185) == "3 min 05 s"
    assert d.format_seconds(3720) == "1 h 02 min"
    assert d.format_gib(23.5 * 2**30) == "23.5 GiB"
