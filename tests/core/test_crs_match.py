"""CRS detection and matching for geo data (core.crs_match)."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from geotools.core import crs_match as cm
from geotools.core import reproject
from geotools.core.crs_migration import PRESET_CODES
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


MGA56_PRESET = next(text for text, code in PRESET_CODES.items() if code == "EPSG:7856")


def _project(definition: str) -> Placement:
    return Placement(mode=PlacementMode.SOURCE_CRS, source_crs=definition)


def test_project_crs() -> None:
    assert cm.project_crs(Placement()) is None
    assert cm.project_crs(Placement(mode=PlacementMode.ANCHOR)) is None
    assert cm.project_crs(_project("")) is None
    crs = cm.project_crs(_project("EPSG:32756"))
    assert crs == cm.DataCrs("EPSG:32756", "EPSG:32756", cm.SOURCE_PROJECT)
    crs = cm.project_crs(_project(MGA56_PRESET))  # a saved preset, not migrated yet
    assert crs is not None and crs.epsg == "EPSG:7856"
    assert crs.definition == MGA56_PRESET


def test_matches() -> None:
    project = cm.project_crs(_project("EPSG:7856"))
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


@pytest.mark.skipif(importlib.util.find_spec("pyproj") is None, reason="needs pyproj")
@pytest.mark.usefixtures("allow_list")
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


# --- reprojection (the hook into core.reproject) --------------------------------------

HAS_PYPROJ = importlib.util.find_spec("pyproj") is not None
needs_pyproj = pytest.mark.skipif(not HAS_PYPROJ, reason="pyproj is not installed")
WGS84 = cm.DataCrs("EPSG:4326", None, cm.SOURCE_RFC7946)
GDA94_MGA56 = cm.DataCrs("EPSG:28356", None, cm.SOURCE_GPKG)
PROJECT = cm.DataCrs("EPSG:7856", "EPSG:7856", cm.SOURCE_PROJECT)
PRESET_PROJECT = cm.DataCrs("EPSG:7856", MGA56_PRESET, cm.SOURCE_PROJECT)  # not migrated yet


def test_reprojection_target_is_the_epsg_code() -> None:
    assert cm.reprojection_target(PROJECT) == "EPSG:7856"
    assert cm.reprojection_target(PRESET_PROJECT) == "EPSG:7856"
    other = cm.DataCrs(None, "+proj=tmerc +lon_0=151 +ellps=GRS80", cm.SOURCE_PROJECT)
    assert cm.reprojection_target(other) == other.definition
    assert cm.reprojection_target(cm.DataCrs("EPSG:32756", "EPSG:32756", "project")) == (
        "EPSG:32756"
    )


def test_reproject_without_pyproj_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reproject, "available", lambda: False)
    with pytest.raises(cm.ReprojectionUnavailable, match="not available"):
        cm.reproject([[153.0, -27.5, 0.0]], WGS84, PRESET_PROJECT)
    assert cm.ReprojectionUnavailable is reproject.ReprojectionUnavailable


@needs_pyproj
def test_reproject_wgs84_into_the_project() -> None:
    import pyproj

    points = np.array([[153.0251, -27.4698]])
    out, report = cm.reproject(points, WGS84, PROJECT)
    e, n = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:7856", always_xy=True).transform(
        153.0251, -27.4698
    )
    np.testing.assert_allclose(out[0], (e, n), atol=1e-6)
    assert report.source == "EPSG:4326" and report.target == "EPSG:7856"
    assert points[0, 0] == 153.0251  # input untouched


@needs_pyproj
@pytest.mark.parametrize("project", [PROJECT, PRESET_PROJECT], ids=["code", "saved-preset"])
def test_reproject_gda94_into_gda2020_moves_about_1_5_m(project: cm.DataCrs) -> None:
    # A saved preset not migrated yet goes into its EPSG code too: the real datum
    # shift, not the PROJ string's null +towgs84.
    points = np.array([[502_000.0, 6_961_000.0, 12.0]])
    out, report = cm.reproject(points, GDA94_MGA56, project)
    shift = float(np.hypot(*(out[0, :2] - points[0, :2])))
    assert 1.4 < shift < 1.6, shift
    assert out[0, 2] == 12.0  # 2D CRSs: Z passed through
    assert "GDA2020" in report.operation and report.accuracy_m == 0.01


def test_report_round_trip() -> None:
    report = reproject.ReprojectionReport("EPSG:4326", "EPSG:7856", "op", 1.0, False, ("a",))
    assert cm.report_from_dict(cm.report_to_dict(report)) == report
    unknown = reproject.ReprojectionReport("a", "b", "op", None, True, ())
    assert cm.report_from_dict(cm.report_to_dict(unknown)) == unknown
    assert cm.report_from_dict(None) is None and cm.report_from_dict({"source": 1}) is None
    assert "accuracy 1 m" in cm.report_text(report)
    assert "accuracy unknown" in cm.report_text(unknown)


def test_label() -> None:
    assert cm.DataCrs("EPSG:7856", None, "x").label() == "EPSG:7856"
    assert cm.DataCrs(None, "+proj=tmerc", "x").label() == "+proj=tmerc"
    assert cm.DataCrs(None, None, "x").label() == "an unknown CRS"


def test_is_geographic_without_pyproj(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reproject, "available", lambda: False)
    assert cm.is_geographic(WGS84) is True
    assert cm.is_geographic(cm.DataCrs("EPSG:7856", None, "x")) is None
    assert cm.is_geographic(cm.DataCrs(None, None, "x")) is None


@needs_pyproj
def test_is_geographic_with_pyproj() -> None:
    assert cm.is_geographic(WGS84) is True
    assert cm.is_geographic(GDA94_MGA56) is False
    assert cm.is_geographic(cm.DataCrs(None, MGA56_PRESET, "x")) is False
