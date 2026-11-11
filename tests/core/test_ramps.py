"""Colour ramps (geotools.core.ramps)."""

from __future__ import annotations

import sys

import numpy as np
import pytest

from geotools.core import ramps
from geotools.core.ramps import (
    PALETTES,
    apply_categories,
    apply_ramp,
    auto_range,
    default_category_colours,
    palette_stops,
    ramp_stops,
)


def _hex(rgba: np.ndarray) -> str:
    return "#" + "".join(f"{int(c):02x}" for c in rgba[:3])


def test_palettes_are_compact_stop_tables() -> None:
    assert set(PALETTES) == {"viridis", "magma", "cividis", "turbo", "diverging_rdbu", "greys"}
    for name, stops in PALETTES.items():
        assert stops.ndim == 2 and stops.shape[1] == 3 and 2 <= stops.shape[0] <= 32, name
        assert stops.min() >= 0.0 and stops.max() <= 1.0, name
        assert not stops.flags.writeable, name
    assert "matplotlib" not in sys.modules


@pytest.mark.parametrize(
    ("palette", "low", "high"),
    [
        ("viridis", "#440154", "#fde725"),
        ("magma", "#000004", "#fcfdbf"),
        ("cividis", "#00224e", "#fee838"),
        ("turbo", "#30123b", "#7a0403"),
        ("diverging_rdbu", "#67001f", "#053061"),
        ("greys", "#000000", "#ffffff"),
    ],
)
def test_ramp_ends_are_the_palette_ends(palette: str, low: str, high: str) -> None:
    rgba = apply_ramp([0.0, 10.0], 0.0, 10.0, palette)
    assert rgba.dtype == np.uint8 and rgba.shape == (2, 4)
    assert (_hex(rgba[0]), _hex(rgba[1])) == (low, high)
    assert (rgba[:, 3] == 255).all()


def test_ramp_interpolates_between_stops_in_srgb() -> None:
    # Greys has two stops, so the ramp is linear in sRGB bytes.
    rgba = apply_ramp(np.array([0.0, 0.25, 0.5, 1.0]), 0.0, 1.0, "greys")
    np.testing.assert_array_equal(rgba[:, 0], [0, 64, 128, 255])
    assert (rgba[:, 0] == rgba[:, 1]).all() and (rgba[:, 1] == rgba[:, 2]).all()
    # Halfway between two viridis stops is their mean.
    stops = PALETTES["viridis"]
    k = stops.shape[0] - 1
    mid = apply_ramp([1.5 / k], 0.0, 1.0, "viridis")[0, :3]
    expected = np.round((stops[1] + stops[2]) / 2 * 255)
    np.testing.assert_array_equal(mid, expected)


def test_ramp_clamps_and_handles_nodata() -> None:
    values = np.array([-5.0, 0.0, 5.0, 99.0, np.nan, np.inf, -9999.0], dtype=np.float32)
    rgba = apply_ramp(values, 0.0, 10.0, "greys", nodata=-9999.0, nodata_rgba=(1, 2, 3, 4))
    np.testing.assert_array_equal(rgba[0], [0, 0, 0, 255])  # below: low end
    np.testing.assert_array_equal(rgba[3], [255, 255, 255, 255])  # above: high end
    for row in (4, 5, 6):  # NaN, inf and no-data
        np.testing.assert_array_equal(rgba[row], [1, 2, 3, 4])
    default = apply_ramp([np.nan], 0.0, 1.0, "viridis")
    np.testing.assert_array_equal(default[0], [0, 0, 0, 0])


def test_ramp_nodata_compares_in_float32() -> None:
    nodata = 0.1  # not exact in float32
    values = np.array([np.float32(nodata), 0.2], dtype=np.float32)
    rgba = apply_ramp(values, 0.0, 1.0, "greys", nodata=nodata, nodata_rgba=(9, 9, 9, 9))
    np.testing.assert_array_equal(rgba[0], [9, 9, 9, 9])
    assert rgba[1, 3] == 255


def test_ramp_integer_and_boolean_values() -> None:
    ints = apply_ramp(np.array([0, 5, 10, -1], dtype=np.int32), 0, 10, "greys", nodata=-1)
    np.testing.assert_array_equal(ints[:3, 0], [0, 128, 255])
    assert ints[3, 3] == 0
    bools = apply_ramp(np.array([False, True]), 0.0, 1.0, "greys")
    np.testing.assert_array_equal(bools[:, 0], [0, 255])


def test_ramp_degenerate_and_invalid_range() -> None:
    rgba = apply_ramp([1.0, 2.0, 3.0], 2.0, 2.0, "greys")
    np.testing.assert_array_equal(rgba[:, 0], [0, 128, 255])
    with pytest.raises(ValueError):
        apply_ramp([1.0], 2.0, 1.0, "greys")
    with pytest.raises(ValueError):
        apply_ramp([1.0], 0.0, float("nan"), "greys")
    with pytest.raises(KeyError):
        apply_ramp([1.0], 0.0, 1.0, "jet")


def test_ramp_accepts_a_stop_table_and_large_inputs(monkeypatch) -> None:
    table = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    np.testing.assert_array_equal(palette_stops(table), table)
    with pytest.raises(ValueError):
        palette_stops(np.array([[0.0, 0.0, 0.0]]))
    monkeypatch.setattr(ramps, "_CHUNK", 7)  # several chunks
    values = np.linspace(0.0, 1.0, 50)
    chunked = apply_ramp(values, 0.0, 1.0, table)
    monkeypatch.setattr(ramps, "_CHUNK", 1 << 20)
    np.testing.assert_array_equal(chunked, apply_ramp(values, 0.0, 1.0, table))
    np.testing.assert_array_equal(chunked[[0, -1], :3], [[255, 0, 0], [0, 0, 255]])


def test_categories() -> None:
    colours = np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], dtype=np.uint8)
    values = np.array([0, 2, 1, 3, -1, 7], dtype=np.int32)
    rgba = apply_categories(values, colours, nodata=7, nodata_rgba=(5, 5, 5, 255))
    np.testing.assert_array_equal(
        rgba,
        [
            [255, 0, 0, 255],
            [0, 0, 255, 255],
            [0, 255, 0, 255],
            [5, 5, 5, 255],  # out of range
            [5, 5, 5, 255],  # negative
            [5, 5, 5, 255],  # no-data
        ],
    )
    floats = apply_categories(np.array([1.0, 1.5, np.nan]), colours)
    np.testing.assert_array_equal(floats[0], [0, 255, 0, 255])
    np.testing.assert_array_equal(floats[1:], [[0, 0, 0, 0], [0, 0, 0, 0]])
    with_alpha = apply_categories([0], np.array([[1, 2, 3, 4]]))
    np.testing.assert_array_equal(with_alpha[0], [1, 2, 3, 4])
    with pytest.raises(ValueError):
        apply_categories([0], np.zeros((2, 2)))


def test_default_category_colours_repeat() -> None:
    colours = default_category_colours(12)
    assert colours.shape == (12, 4) and colours.dtype == np.uint8
    assert (colours[:, 3] == 255).all()
    np.testing.assert_array_equal(colours[10], colours[0])
    assert len({tuple(c) for c in colours[:10]}) == 10
    assert default_category_colours(0).shape == (0, 4)


def test_auto_range_percentiles_ignore_nodata() -> None:
    values = np.arange(101, dtype=np.float32)
    assert auto_range(values) == (2.0, 98.0)
    assert auto_range(values, percentiles=(0, 100)) == (0.0, 100.0)
    noisy = np.concatenate([values, [np.nan, np.inf, -9999.0] * 50]).astype(np.float32)
    assert auto_range(noisy, nodata=-9999.0) == (2.0, 98.0)
    assert auto_range(np.array([3, 3, 3], dtype=np.int32)) == (3.0, 3.0)
    with pytest.raises(ValueError):
        auto_range(np.array([np.nan, -9999.0]), nodata=-9999.0)
    with pytest.raises(ValueError):
        auto_range(values, percentiles=(90, 10))


def test_auto_range_subsamples_large_inputs() -> None:
    values = np.linspace(0.0, 1000.0, 100_001)
    low, high = auto_range(values, max_samples=1000)
    assert abs(low - 20.0) < 2.0 and abs(high - 980.0) < 2.0
    assert auto_range(values, max_samples=1000) == (low, high)  # deterministic


def test_ramp_stops_match_apply_ramp() -> None:
    stops = ramp_stops("magma", -10.0, 30.0, 5)
    assert [s["value"] for s in stops] == [-10.0, 0.0, 10.0, 20.0, 30.0]
    colours = apply_ramp([s["value"] for s in stops], -10.0, 30.0, "magma")
    assert [s["rgba"] for s in stops] == colours.tolist()
    assert all(isinstance(c, int) for s in stops for c in s["rgba"])
    flat = ramp_stops("greys", 4.0, 4.0, 3)
    assert [s["value"] for s in flat] == [4.0, 4.0, 4.0]
    with pytest.raises(ValueError):
        ramp_stops("greys", 0.0, 1.0, 1)
