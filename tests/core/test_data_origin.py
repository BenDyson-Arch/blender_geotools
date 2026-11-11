"""Projected data in LOCAL and ANCHOR projects, and the data's centre as origin."""

from __future__ import annotations

import pytest

from geotools.core import crs_presets, reproject
from geotools.core.crs_match import DataCrs
from geotools.core.data_origin import (
    LOCAL_RADIUS_M,
    DataOrigin,
    NeedsProjectOrigin,
    check_local_project,
    farthest,
    is_local,
    suggest_origin,
)
from geotools.core.placement import PlacementMode

MGA56 = crs_presets.get("MGA2020_56")
SITE = (501_960.25, 6_960_970.0, 502_063.75, 6_961_051.5)


def test_local_extent() -> None:
    assert is_local((-LOCAL_RADIUS_M / 2, 0.0, 10.0, 10.0))
    assert not is_local(SITE)
    assert farthest((-3.0, -4.0, 1.0, 1.0)) == 5.0


def test_suggest_origin_uses_the_preset_for_its_epsg_code() -> None:
    origin = suggest_origin(DataCrs("EPSG:7856", None, "gpkg-srs"), SITE)
    assert origin.source_crs == MGA56.definition
    assert origin.epsg == "EPSG:7856" and origin.preset_label == MGA56.label
    assert origin.offset == (502_012.0, 6_961_011.0, 0.0)  # whole metres, A 0
    assert "EPSG:7856 (GDA2020 / MGA zone 56)" in origin.describe()
    assert DataOrigin.from_dict(origin.to_dict()) == origin


def test_suggest_origin_without_a_preset() -> None:
    origin = suggest_origin(DataCrs("EPSG:32756", None, "x"), (500_000.4, 0.0, 500_001.0, 1.0))
    assert origin.source_crs == "EPSG:32756" and origin.preset_label is None
    assert origin.offset == (500_001.0, 0.0, 0.0)
    wkt = 'PROJCRS["Custom",...]'
    assert suggest_origin(DataCrs(None, wkt, "x"), SITE).source_crs == wkt


@pytest.mark.parametrize("mode", [PlacementMode.LOCAL, PlacementMode.ANCHOR])
def test_check_local_project(mode: PlacementMode) -> None:
    kind = "Local" if mode is PlacementMode.LOCAL else "Anchor"
    with pytest.raises(NeedsProjectOrigin, match=f"{kind} project") as caught:
        check_local_project(mode, DataCrs("EPSG:7856", None, "x"), SITE, "dig.gpkg")
    assert caught.value.origin.offset == (502_012.0, 6_961_011.0, 0.0)
    assert "dig.gpkg" in str(caught.value) and "6,979 km" in str(caught.value)
    # Small local metres, an unknown CRS, or no extent: a warning.
    small = (0.0, 0.0, 50.0, 50.0)
    assert "local metres" in check_local_project(mode, DataCrs("EPSG:7856", None, "x"), small, "a")
    assert "local metres" in check_local_project(mode, DataCrs(None, None, "x"), SITE, "a")
    assert "local metres" in check_local_project(mode, None, SITE, "a")
    assert "local metres" in check_local_project(mode, DataCrs("EPSG:7856", None, "x"), None, "a")


def test_geographic_data_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reproject, "available", lambda: False)
    huge = (0.0, 0.0, 200_000.0, 10.0)  # nonsense degrees, but degrees all the same
    text = check_local_project(PlacementMode.LOCAL, DataCrs("EPSG:4326", None, "x"), huge, "a")
    assert "local metres" in text
