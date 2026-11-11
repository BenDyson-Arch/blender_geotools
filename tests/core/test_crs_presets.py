"""CRS presets must be accepted by the bundled rusty-tiles and land in the right place."""

from __future__ import annotations

import pytest

from geotools.core import crs_presets
from geotools.core.placement import Placement, PlacementMode


def test_presets_are_unique_and_found() -> None:
    ids = [p.id for p in crs_presets.PRESETS]
    assert len(ids) == len(set(ids))
    for preset in crs_presets.PRESETS:
        assert crs_presets.get(preset.id) is preset
        assert crs_presets.for_definition(f"  {preset.definition} ") is preset
        assert "+towgs84=" in preset.definition


def test_preset_placements_validate() -> None:
    for preset in crs_presets.PRESETS:
        placement = Placement(mode=PlacementMode.SOURCE_CRS, source_crs=preset.definition)
        assert not [i for i in placement.validate() if i.severity == "error"], preset.id


@pytest.mark.parametrize(
    ("preset_id", "offset", "lon", "lat"),
    [
        # Brisbane: same easting/northing as WGS84 UTM 56S.
        ("MGA2020_56", (502000.0, 6961000.0, 0.0), 153.0203, -27.4745),
        # Perth.
        ("MGA2020_50", (391000.0, 6466000.0, 0.0), 115.847, -31.937),
        # Wellington.
        ("NZTM2000", (1748800.0, 5428000.0, 0.0), 174.78, -41.29),
    ],
)
def test_preset_lands_where_expected(
    preset_id: str, offset: tuple[float, float, float], lon: float, lat: float
) -> None:
    pytest.importorskip("rusty_tiles")
    from geotools.core.probe import probe_placement

    preset = crs_presets.get(preset_id)
    assert preset is not None
    placement = Placement(
        mode=PlacementMode.SOURCE_CRS, source_crs=preset.definition, source_offset=offset
    )
    result = probe_placement(placement, (-1.0, -1.0, 0.0), (1.0, 1.0, 1.0))
    assert result.ok, result.error
    got_lon, got_lat, _ = result.origin_lon_lat_height
    assert abs(got_lon - lon) < 0.01 and abs(got_lat - lat) < 0.01, result
