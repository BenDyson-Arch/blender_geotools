"""Projected data in LOCAL and ANCHOR projects, and the data's centre as origin."""

from __future__ import annotations

import importlib.util

import pytest

from geotools.core import reproject
from geotools.core.crs_match import DataCrs
from geotools.core.data_origin import (
    LOCAL_RADIUS_M,
    DataOrigin,
    NeedsProjectOrigin,
    check_local_project,
    farthest,
    is_local,
    offer_project_crs,
    suggest_origin,
)
from geotools.core.placement import PlacementMode

needs_pyproj = pytest.mark.skipif(
    importlib.util.find_spec("pyproj") is None, reason="pyproj is not installed"
)
SITE = (501_960.25, 6_960_970.0, 502_063.75, 6_961_051.5)


def test_local_extent() -> None:
    assert is_local((-LOCAL_RADIUS_M / 2, 0.0, 10.0, 10.0))
    assert not is_local(SITE)
    assert farthest((-3.0, -4.0, 1.0, 1.0)) == 5.0


@needs_pyproj
@pytest.mark.usefixtures("allow_list")
def test_suggest_origin_uses_the_epsg_code_and_names_it() -> None:
    origin = suggest_origin(DataCrs("EPSG:7856", None, "gpkg-srs"), SITE)
    assert origin.source_crs == "EPSG:7856"
    assert origin.epsg == "EPSG:7856" and origin.crs_name == "GDA2020 / MGA zone 56"
    assert origin.offset == (502_012.0, 6_961_011.0, 0.0)  # whole metres, A 0
    assert "EPSG:7856 (GDA2020 / MGA zone 56)" in origin.describe()
    assert DataOrigin.from_dict(origin.to_dict()) == origin


def test_suggest_origin_without_rusty_tiles_has_no_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from geotools.core import crs_catalog

    monkeypatch.setattr(crs_catalog, "_rusty_tiles", lambda: None)
    crs_catalog.reset_cache()
    origin = suggest_origin(DataCrs("EPSG:32756", None, "x"), (500_000.4, 0.0, 500_001.0, 1.0))
    crs_catalog.reset_cache()
    assert origin.source_crs == "EPSG:32756" and origin.crs_name is None
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


# --- a SOURCE_CRS project with no CRS yet: offer the data's ---------------------------


def test_offer_project_crs_with_a_zero_offset_takes_the_data_centre() -> None:
    with pytest.raises(NeedsProjectOrigin, match="no CRS yet") as caught:
        offer_project_crs(DataCrs("EPSG:7856", None, "gpkg-srs"), SITE, (0.0, 0.0, 0.0), "a.gpkg")
    origin = caught.value.origin
    assert origin.source_crs == "EPSG:7856" and origin.set_offset
    assert origin.offset == (502_012.0, 6_961_011.0, 0.0)
    assert DataOrigin.from_dict(origin.to_dict()) == origin


def test_offer_project_crs_keeps_an_offset_already_set() -> None:
    offset = (502_000.0, 6_961_000.0, 3.5)
    with pytest.raises(NeedsProjectOrigin) as caught:
        offer_project_crs(DataCrs("EPSG:7856", None, "geotiff-geokeys"), SITE, offset, "dem.tif")
    origin = caught.value.origin
    assert origin.source_crs == "EPSG:7856" and not origin.set_offset
    assert origin.offset == offset
    assert "keeping the project's offset" in origin.describe()
    assert DataOrigin.from_dict(origin.to_dict()) == origin


def test_offer_project_crs_offers_nothing_for_unknown_or_geographic_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reproject, "available", lambda: False)
    zero = (0.0, 0.0, 0.0)
    assert offer_project_crs(None, SITE, zero, "a") is None
    assert offer_project_crs(DataCrs(None, None, "x"), SITE, zero, "a") is None
    assert offer_project_crs(DataCrs("EPSG:4326", None, "rfc7946"), SITE, zero, "a") is None
