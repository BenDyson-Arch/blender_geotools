"""CRS detection and matching for geo data (core.crs_match)."""

from __future__ import annotations

import pytest

from geotools.core import crs_match as cm
from geotools.core import crs_presets
from geotools.core.placement import Placement, PlacementMode

MGA56_WKT1 = (
    'PROJCS["GDA2020 / MGA zone 56",GEOGCS["GDA2020",DATUM["GDA2020",SPHEROID["GRS 1980",'
    '6378137,298.257222101,AUTHORITY["EPSG","7019"]],AUTHORITY["EPSG","1168"]],'
    'AUTHORITY["EPSG","7844"]],PROJECTION["Transverse_Mercator"],'
    'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AUTHORITY["EPSG","7856"]]'
)
MGA56_WKT2 = (
    'PROJCRS["GDA2020 / MGA zone 56",BASEGEOGCRS["GDA2020",DATUM["GDA2020",'
    'ELLIPSOID["GRS 1980",6378137,298.257222101]],ID["EPSG",7844]],'
    'CONVERSION["MGA zone 56",METHOD["Transverse Mercator",ID["EPSG",9807]]],'
    'CS[Cartesian,2],AXIS["easting",east],ID["EPSG",7856]]'
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("EPSG:7856", "EPSG:7856"),
        ("epsg:4326", "EPSG:4326"),
        ("urn:ogc:def:crs:EPSG::7856", "EPSG:7856"),
        ("urn:ogc:def:crs:EPSG:6.6:28356", "EPSG:28356"),
        ("http://www.opengis.net/def/crs/EPSG/0/2193", "EPSG:2193"),
        ("urn:ogc:def:crs:OGC:1.3:CRS84", "EPSG:4326"),
        ("urn:ogc:def:crs:OGC::CRS84", "EPSG:4326"),
        ("+proj=utm +zone=56 +south", None),
        ("", None),
        (None, None),
    ],
)
def test_normalise_epsg(name: str | None, expected: str | None) -> None:
    assert cm.normalise_epsg(name) == expected


def test_epsg_from_wkt_takes_the_root_id() -> None:
    assert cm.epsg_from_wkt(MGA56_WKT1) == "EPSG:7856"
    assert cm.epsg_from_wkt(MGA56_WKT2) == "EPSG:7856"
    # Only nested IDs (the base CRS's): the root has none.
    nested = MGA56_WKT2.rsplit(',ID["EPSG",7856]', 1)[0] + "]"
    assert cm.epsg_from_wkt(nested) is None
    assert cm.epsg_from_wkt('LOCAL_CS["site",AUTHORITY["ESRI","1"]]') is None
    assert cm.epsg_from_wkt("") is None


def test_from_gpkg_srs() -> None:
    assert cm.from_gpkg_srs("EPSG", 7856, MGA56_WKT1, 7856).epsg == "EPSG:7856"
    assert cm.from_gpkg_srs("epsg", 7856, "undefined", 7856) == cm.DataCrs(
        "EPSG:7856", None, cm.SOURCE_GPKG
    )
    # An organisation other than EPSG: the WKT's own ID is used.
    assert cm.from_gpkg_srs("NONE", 0, MGA56_WKT1, 100000).epsg == "EPSG:7856"
    for srs_id in (0, -1):
        crs = cm.from_gpkg_srs("NONE", srs_id, "undefined", srs_id)
        assert not crs.known
        assert cm.matches(cm.DataCrs("EPSG:7856", None, "project"), crs) is None


def _project(definition: str) -> Placement:
    return Placement(mode=PlacementMode.SOURCE_CRS, source_crs=definition)


def test_project_crs() -> None:
    assert cm.project_crs(Placement()) is None
    assert cm.project_crs(Placement(mode=PlacementMode.ANCHOR)) is None
    assert cm.project_crs(_project("")) is None
    crs = cm.project_crs(_project("EPSG:32756"))
    assert crs == cm.DataCrs("EPSG:32756", "EPSG:32756", cm.SOURCE_PROJECT)
    preset = crs_presets.get("MGA2020_56")
    assert preset is not None
    crs = cm.project_crs(_project(preset.definition))
    assert crs is not None and crs.epsg == "EPSG:7856"
    assert crs.definition == preset.definition


def test_matches() -> None:
    preset = crs_presets.get("MGA2020_56")
    assert preset is not None
    project = cm.project_crs(_project(preset.definition))
    assert cm.matches(project, cm.DataCrs("EPSG:7856", None, cm.SOURCE_GEOJSON_MEMBER)) is True
    assert cm.matches(project, cm.DataCrs("EPSG:4326", None, cm.SOURCE_RFC7946)) is False
    assert cm.matches(project, cm.DataCrs("EPSG:7855", MGA56_WKT1, cm.SOURCE_GPKG)) is False
    assert cm.matches(project, cm.DataCrs(None, None, cm.SOURCE_GPKG)) is None
    # No code on one side: the same definition text matches; another can't be told.
    proj_only = cm.DataCrs(None, "+proj=tmerc +lon_0=173", "project")
    assert cm.matches(proj_only, cm.DataCrs(None, "+proj=tmerc  +lon_0=173", "user")) is True
    assert cm.matches(proj_only, cm.DataCrs(None, "+proj=utm +zone=1", "user")) is None
    # LOCAL and ANCHOR projects can't tell.
    assert cm.matches(None, cm.DataCrs("EPSG:7856", None, cm.SOURCE_GPKG)) is None


def test_messages_name_both_crss() -> None:
    project = cm.DataCrs("EPSG:7856", "x", "project")
    data = cm.DataCrs("EPSG:4326", None, cm.SOURCE_RFC7946)
    text = cm.mismatch_message(project, data, "contexts.geojson")
    assert "EPSG:4326" in text and "EPSG:7856" in text and "QGIS" in text
    assert "GDA2020 / MGA zone 56" in text
    assert "Data Is in Project CRS" in cm.unknown_message(cm.DataCrs(None, None, "x"), "a")


def test_text_round_trip() -> None:
    crs = cm.DataCrs("EPSG:7856", MGA56_WKT1, cm.SOURCE_GPKG)
    assert cm.from_text(cm.to_text(crs)) == crs
    none = cm.DataCrs(None, None, cm.SOURCE_USER)
    assert cm.from_text(cm.to_text(none)) == none
    assert cm.from_text("") is None
    assert cm.from_text("not json") is None


def test_reproject_is_not_available() -> None:
    a = cm.DataCrs("EPSG:4326", None, cm.SOURCE_RFC7946)
    b = cm.DataCrs("EPSG:7856", None, cm.SOURCE_PROJECT)
    with pytest.raises(cm.ReprojectionUnavailable, match="QGIS"):
        cm.reproject([[153.0, -27.5, 0.0]], a, b)
