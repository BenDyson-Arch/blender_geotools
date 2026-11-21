"""SVG legends of variant tilesets (core.legend): the colours match core.ramps exactly."""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest

from geotools.core import legend
from geotools.core.layers import DataType, Domain, LayerSpec, to_record
from geotools.core.presentation import variant_record
from geotools.core.ramps import PALETTES, apply_ramp, default_category_colours

SVG = "{http://www.w3.org/2000/svg}"
SLOPE = LayerSpec("slope", Domain.FACE, DataType.FLOAT, units="deg", label="Slope")
PHASE = LayerSpec(
    "phase", Domain.FACE, DataType.CATEGORY, categories=("Natural", "Pit <A>", "Wall & bank")
)


def _parse(text: str) -> ET.Element:
    return ET.fromstring(text.encode("utf-8"))


def _hex(rgba: list[int] | np.ndarray) -> str:
    r, g, b = (int(c) for c in list(rgba)[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def _texts(root: ET.Element, cls: str) -> list[str]:
    return [t.text or "" for t in root.iter(f"{SVG}text") if t.get("class") == cls]


def _gradient(root: ET.Element) -> tuple[np.ndarray, np.ndarray]:
    stops = list(root.iter(f"{SVG}stop"))
    offsets = np.array([float(s.get("offset", "nan")) for s in stops])
    colours = np.array(
        [[int(s.get("stop-color", "")[i : i + 2], 16) for i in (1, 3, 5)] for s in stops]
    )
    return offsets, colours


def test_legend_path_is_beside_the_variant() -> None:
    assert legend.legend_path(Path("/out/site.slope.3tz")) == Path("/out/site.slope.legend.svg")
    assert legend.legend_path(Path("/out/site.slope.glb")) == Path("/out/site.slope.legend.svg")


@pytest.mark.parametrize("palette", sorted(PALETTES))
@pytest.mark.parametrize("scale", ["LINEAR", "LOG"])
def test_ramp_bar_shows_the_published_colours(palette: str, scale: str) -> None:
    low, high = (0.5, 2500.0) if scale == "LOG" else (-3.0, 45.0)
    record = variant_record(
        SLOPE, "COLOR_0", "REPLACE", palette=palette, value_range=(low, high), scale=scale
    )
    root = _parse(legend.legend_svg(record, to_record(SLOPE)))
    offsets, colours = _gradient(root)
    assert len(offsets) == len(PALETTES[palette])
    log = scale == "LOG"
    # Every legend stop's colour, read off the bar as an SVG renderer interpolates it
    # (linearly in sRGB between gradient stops), is the colour the ramp published.
    for stop in record["ramp_stops"]:
        v = stop["value"]
        t = (
            (math.log10(v) - math.log10(low)) / (math.log10(high) - math.log10(low))
            if log
            else (v - low) / (high - low)
        )
        shown = [np.interp(t, offsets, colours[:, i]) for i in range(3)]
        expected = apply_ramp([v], low, high, palette, log=log)[0, :3]
        assert np.abs(np.array(shown) - expected).max() <= 1.0, (v, shown, expected)
        assert list(expected) == stop["rgba"][:3]
    ticks = _texts(root, "tick")
    assert len(ticks) == len(record["ramp_stops"])
    assert float(ticks[0]) == pytest.approx(low) and float(ticks[-1]) == pytest.approx(high)


def test_ramp_legend_titles_units_range_and_no_data() -> None:
    record = variant_record(
        SLOPE,
        "COLOR_0",
        "REPLACE",
        palette="viridis",
        value_range=(0.0, 45.0),
        range_auto=(False, True),
        nodata_rgba=(128, 64, 32, 255),
    )
    root = _parse(legend.legend_svg(record, to_record(SLOPE)))
    assert _texts(root, "title") == ["Slope (deg)"]
    assert _texts(root, "subtitle") == ["viridis, linear scale"]
    assert _texts(root, "tick") == [f"{4.5 * i:.1f}" for i in range(11)]
    assert _texts(root, "range") == ["Range 0.0 to 45.0 deg (maximum automatic)"]
    (nodata,) = [r for r in root.iter(f"{SVG}rect") if r.get("class") == "nodata"]
    assert nodata.get("fill") == "#804020" and nodata.get("fill-opacity") is None
    # Ticks run from the bottom (minimum) to the top (maximum), evenly.
    ys = [float(line.get("y1", "nan")) for line in root.iter(f"{SVG}line")]
    assert ys == sorted(ys, reverse=True) and np.ptp(np.diff(ys)) < 1e-6


def test_log_ticks_are_evenly_spaced_on_the_bar() -> None:
    record = variant_record(
        SLOPE, "COLOR_0", "REPLACE", palette="turbo", value_range=(1.0, 1e4), scale="LOG"
    )
    root = _parse(legend.legend_svg(record, to_record(SLOPE)))
    ys = [float(line.get("y1", "nan")) for line in root.iter(f"{SVG}line")]
    assert np.ptp(np.diff(ys)) < 0.02
    assert _texts(root, "tick")[::5] == ["1", "100", "10000"]  # no exponent notation


def test_a_single_value_range_has_one_tick() -> None:
    record = variant_record(SLOPE, "COLOR_0", "REPLACE", palette="magma", value_range=(7.0, 7.0))
    root = _parse(legend.legend_svg(record, to_record(SLOPE)))
    assert _texts(root, "tick") == ["7"]
    _offsets, colours = _gradient(root)
    middle = apply_ramp([7.0], 7.0, 7.0, "magma")[0, :3]
    assert (colours == middle).all()


def test_category_legend_lists_each_category_and_no_data() -> None:
    colours = default_category_colours(3)
    colours[1] = (10, 20, 30, 255)
    record = variant_record(
        PHASE, "COLOR_0", "REPLACE", category_rgba=colours, nodata_rgba=(0, 0, 0, 0)
    )
    text = legend.legend_svg(record, to_record(PHASE))
    root = _parse(text)  # the labels' < and & are escaped
    assert _texts(root, "title") == ["phase"]
    swatches = [r for r in root.iter(f"{SVG}rect") if r.get("class") == "category"]
    assert [s.get("fill") for s in swatches] == [_hex(c["rgba"]) for c in record["categories"]]
    assert swatches[1].get("fill") == "#0a141e"
    labels = [t.text for t in root.iter(f"{SVG}text")]
    assert "1  Pit <A>" in labels and "2  Wall & bank" in labels and "No data" in labels
    (nodata,) = [r for r in root.iter(f"{SVG}rect") if r.get("class") == "nodata"]
    assert nodata.get("fill-opacity") == "0"  # transparent, as published


def test_write_legend_names_the_layer_from_its_record(tmp_path: Path) -> None:
    record = variant_record(SLOPE, "COLOR_0", "REPLACE", palette="viridis", value_range=(0, 1))
    path = legend.write_legend(
        tmp_path / "site.slope.3tz", record, [to_record(PHASE), to_record(SLOPE)]
    )
    assert path == tmp_path / "site.slope.legend.svg"
    assert _texts(_parse(path.read_text(encoding="utf-8")), "title") == ["Slope (deg)"]
    assert [p.name for p in tmp_path.iterdir()] == [path.name]  # no temporary file left
    # A layer missing from the records: the variant's layer name is the title.
    path = legend.write_legend(tmp_path / "site.slope.3tz", record, [])
    assert _texts(_parse(path.read_text(encoding="utf-8")), "title") == ["slope"]
