"""Projected data in LOCAL and ANCHOR projects, and the data's centre as origin."""

from __future__ import annotations

import importlib.util
import math

import pytest

from geotools.core import reproject
from geotools.core.crs_match import DataCrs
from geotools.core.data_origin import (
    LOCAL_RADIUS_M,
    DataOrigin,
    NeedsProjectOrigin,
    OffsetAction,
    check_local_project,
    check_offset_file,
    farthest,
    float32_step,
    is_local,
    offer_project_crs,
    suggest_origin,
)
from geotools.core.geoscene import read as read_keys
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


# --- an offset file beside an imported source mesh ---------------------------------------------

FILE = (512_345.987654321, 6_961_000.123456, 12.5)
NO_KEYS = read_keys({})


def _action(mode: str, project, keys: dict | None = None, offset=FILE) -> OffsetAction:
    return check_offset_file(mode, read_keys(keys or {}), project, offset).action


def test_offset_file_sets_the_origin_of_a_project_that_has_none() -> None:
    assert _action("LOCAL", (0.0, 0.0, 0.0)) is OffsetAction.APPLY
    assert _action(PlacementMode.LOCAL, (0.0, 0.0, 0.0)) is OffsetAction.APPLY
    # A CRS project whose CRS was chosen first, offsets left at 0 (getting started).
    keys = {"SRID": "EPSG:7856", "crs x": 0.0, "crs y": 0.0}
    assert _action("SOURCE_CRS", (0.0, 0.0, 0.0), keys) is OffsetAction.APPLY
    assert _action("SOURCE_CRS", (None, None, 0.0), {"SRID": "EPSG:7856"}) is OffsetAction.APPLY


def test_a_matching_offset_file_is_silent() -> None:
    """A chunk imported again, or several chunks sharing one offset file."""
    check = check_offset_file("SOURCE_CRS", NO_KEYS, FILE, FILE)
    assert check.action is OffsetAction.MATCH and check.reason == ""
    keys = {"SRID": "EPSG:7856", "crs x": FILE[0], "crs y": FILE[1]}
    assert _action("SOURCE_CRS", FILE, keys) is OffsetAction.MATCH
    # Exact in float64, A included: one ulp or another A is another origin.
    e = math.nextafter(FILE[0], math.inf)
    assert _action("SOURCE_CRS", (e, FILE[1], FILE[2])) is OffsetAction.OFFER
    assert _action("SOURCE_CRS", (FILE[0], FILE[1], 0.0)) is OffsetAction.OFFER
    # A Local project whose keys are the file's: the file only adds A.
    assert _action("LOCAL", (0.0, 0.0, 0.0), keys) is OffsetAction.APPLY


def test_a_different_offset_in_a_crs_project_is_offered_with_the_shift() -> None:
    project = (512_000.0, 6_961_000.0, 10.0)
    check = check_offset_file("SOURCE_CRS", NO_KEYS, project, FILE)
    assert check.action is OffsetAction.OFFER
    assert check.shift == (FILE[0] - 512_000.0, FILE[1] - 6_961_000.0, 2.5)
    text = check.message("chunk2_offset.txt")
    assert "chunk2_offset.txt holds E 512345.987654321, N 6961000.123456, A 12.5" in text, text
    assert "the project's origin is E 512000, N 6961000, A 10" in text, text
    assert "imported unshifted" in text and "place the imported objects" in text


def test_complete_blendergis_keys_that_differ_are_offered() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": 502_000.0, "crs y": 6_961_000.0}
    check = check_offset_file("LOCAL", read_keys(keys), (0.0, 0.0, 0.0), FILE)
    assert check.action is OffsetAction.OFFER and check.shift is None
    assert "BlenderGIS (GeoScene) georeference (EPSG:7856, origin E 502000" in check.reason
    # The same keys in a CRS project: case (b), with the shift.
    check = check_offset_file("SOURCE_CRS", read_keys(keys), (502_000.0, 6_961_000.0, 0.0), FILE)
    assert check.action is OffsetAction.OFFER and check.shift is not None


def test_an_anchor_project_is_offered_not_switched() -> None:
    check = check_offset_file("ANCHOR", NO_KEYS, (0.0, 0.0, 0.0), FILE)
    assert check.action is OffsetAction.OFFER and "Anchor project" in check.reason
    assert check.shift is None


def test_a_local_project_with_keys_or_an_old_origin_is_offered() -> None:
    lat_lon = {"latitude": -27.47, "longitude": 153.02}
    assert _action("LOCAL", (0.0, 0.0, 0.0), lat_lon) is OffsetAction.OFFER
    assert _action("LOCAL", (500.0, 0.0, 0.0)) is OffsetAction.OFFER
    assert _action("LOCAL", (0.0, 0.0, 3.0)) is OffsetAction.OFFER
    assert _action("LOCAL", FILE) is OffsetAction.APPLY  # the same origin from before


def test_a_geographic_origin_in_a_crs_project_is_offered() -> None:
    keys = {"SRID": "EPSG:7856", "latitude": -27.47, "longitude": 153.02}
    check = check_offset_file("SOURCE_CRS", read_keys(keys), (None, None, 0.0), FILE)
    assert check.action is OffsetAction.OFFER and "latitude -27.4700000" in check.reason
    assert check.shift is None


def test_float32_step() -> None:
    assert float32_step(0.0) == 0.0
    assert float32_step(1.0) == 2.0**-23
    assert float32_step(16383.0) < 0.001 < float32_step(16384.0)
    assert float32_step(-10_000.0) == float32_step(10_000.0) == 2.0**-10
