"""The scene georeference over BlenderGIS GeoScene keys (``core.geoscene``), without Blender.

``keys`` stands in for the Scene's ID properties and ``store`` for GeoTools' own
saved text; both are plain dicts here.
"""

from __future__ import annotations

import math

import pytest

from geotools.core import geoscene as gs
from geotools.core.issues import Severity

E, N = 502_000.123456789, 6_961_000.987654321
LON, LAT = 153.02, -27.47
CRS_MODE = "SOURCE_CRS"


def locate(definition: str, e: float, n: float) -> tuple[float, float]:
    """A stand-in for pyproj: records what it was asked."""
    locate.calls.append((definition, e, n))
    return LON + (e - E) * 1e-5, LAT + (n - N) * 1e-5


locate.calls = []


def no_locate(definition: str, e: float, n: float) -> None:
    """No pyproj: the position cannot be computed."""
    return


# --- SRID text ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("srid", "parses"),
    [
        ("EPSG:7856", True),
        ("7856", True),
        (7856, True),
        ("+init=epsg:4326", True),
        ("+proj=utm +zone=56 +south +ellps=GRS80 +units=m +no_defs", True),
        ("EPSG:7856+5711", False),  # BlenderGIS's parser refuses compound codes
        ("IGNF:LAMB93", False),
        ('PROJCRS["GDA2020 / MGA zone 56",BASEGEOGCRS[...]]', False),
        ("urn:ogc:def:crs:EPSG::7856", False),
        ("", False),
        (None, False),
        (True, False),
    ],
)
def test_blendergis_parses(srid: object, parses: bool) -> None:
    assert gs.blendergis_parses(srid) is parses


@pytest.mark.parametrize(
    ("definition", "srid"),
    [
        ("EPSG:7856", "EPSG:7856"),
        (" epsg:07856 ", "EPSG:7856"),
        (
            "+proj=utm +zone=56 +south +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
            "+proj=utm +zone=56 +south +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs",
        ),
        ("EPSG:7856+5711", None),  # compound: never written
        ('PROJCRS["x"]', None),  # WKT: BlenderGIS cannot read it
        ("ESRI:102100", None),  # only plain EPSG codes
        ("", None),
        ("local", None),
    ],
)
def test_srid_for(definition: str, srid: str | None) -> None:
    assert gs.srid_for(definition) == srid


def test_srid_for_refuses_single_compound_and_vertical_codes() -> None:
    pytest.importorskip("pyproj")
    assert gs.srid_for("EPSG:5555") is None  # ETRS89 / UTM 32N + DHHN92 height
    assert gs.srid_for("EPSG:5711") is None  # AHD height: vertical
    assert gs.srid_for("EPSG:4979") is None  # WGS 84 3D
    assert gs.srid_for("EPSG:4326") == "EPSG:4326"


@pytest.mark.parametrize(
    ("srid", "definition"),
    [
        ("EPSG:7856", "EPSG:7856"),
        ("epsg:7856", "EPSG:7856"),
        ("7856", "EPSG:7856"),
        (7856, "EPSG:7856"),
        ("+init=epsg:28356", "EPSG:28356"),
        ("+proj=utm +zone=56 +south", "+proj=utm +zone=56 +south"),
        ('PROJCRS["x"]', 'PROJCRS["x"]'),  # read as written, never rewritten
        (1.5, ""),
        (None, ""),
    ],
)
def test_definition_of(srid: object, definition: str) -> None:
    assert gs.definition_of(srid) == definition


def test_read_takes_blendergis_ints_and_floats() -> None:
    scene = gs.read({"SRID": "EPSG:7856", "crs x": 502000, "crs y": 6961000.5, "scale": 1})
    assert scene.crs == "EPSG:7856" and scene.complete and scene.parses
    assert scene.x == 502000.0 and isinstance(scene.x, float)
    assert scene.y == 6961000.5
    assert scene.latitude is None and scene.present == ("SRID", "crs x", "crs y")
    assert gs.read({"crs x": "502000"}).x is None  # not a number
    assert gs.read({"crs x": math.nan}).x is None


# --- reading and writing through the accessor ----------------------------------------------


def test_crs_project_reads_the_keys_and_never_its_own_text() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N}
    store = {"source_crs": "EPSG:28356", "source_offset_e": "1.0"}  # stale: ignored
    assert gs.keys_live(keys, CRS_MODE)
    assert gs.get_crs(keys, store, CRS_MODE) == "EPSG:7856"
    assert gs.get_origin(keys, store, CRS_MODE, gs.STORE_E) == repr(E)
    assert float(gs.get_origin(keys, store, CRS_MODE, gs.STORE_N)) == N  # binary64 exact
    # LOCAL and ANCHOR read GeoTools' own text: the keys are not theirs.
    assert not gs.keys_live(keys, "LOCAL")
    assert gs.get_crs(keys, store, "LOCAL") == "EPSG:28356"
    assert gs.get_origin(keys, store, "ANCHOR", gs.STORE_N) == "0"


def test_missing_origin_key_reads_empty() -> None:
    keys = {"SRID": "EPSG:7856"}
    assert gs.get_origin(keys, {}, CRS_MODE, gs.STORE_E) == ""


def test_setting_the_origin_writes_the_key_as_a_double_and_syncs_lat_lon() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": 0.0, "crs y": 0.0}
    locate.calls.clear()
    assert gs.set_origin(keys, {}, CRS_MODE, gs.STORE_E, repr(E), locate)
    assert gs.set_origin(keys, {}, CRS_MODE, gs.STORE_N, repr(N), locate)
    assert keys["crs x"] == E and type(keys["crs x"]) is float
    assert keys["crs y"] == N
    assert (keys["longitude"], keys["latitude"]) == (LON, LAT)
    assert locate.calls[-1] == ("EPSG:7856", E, N)


def test_setting_the_origin_refuses_text_a_key_cannot_hold() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N}
    for bad in ("", "abc", "nan", "inf"):
        assert not gs.set_origin(keys, {}, CRS_MODE, gs.STORE_E, bad, locate)
    assert keys["crs x"] == E


def test_without_pyproj_stale_lat_lon_are_deleted() -> None:
    # BlenderGIS's setOriginPrj: a failed reprojection deletes the geographic origin.
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N, "latitude": LAT, "longitude": LON}
    gs.set_origin(keys, {}, CRS_MODE, gs.STORE_E, "502100.0", no_locate)
    assert keys["crs x"] == 502100.0
    assert "latitude" not in keys and "longitude" not in keys


def test_sync_leaves_lat_lon_alone_without_a_complete_origin() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "latitude": LAT, "longitude": LON}
    assert not gs.sync_lat_lon(keys, no_locate)
    assert keys["latitude"] == LAT


def test_scale_and_zoom_are_never_touched() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N, "scale": 2000, "zoom": 12}
    gs.set_crs(keys, {}, CRS_MODE, "EPSG:28356", locate)
    gs.set_origin(keys, {}, CRS_MODE, gs.STORE_N, "1.0", no_locate)
    gs.set_crs(keys, {}, CRS_MODE, 'PROJCRS["x"]', locate)
    assert keys == {"scale": 2000, "zoom": 12}


def test_changing_the_crs_keeps_the_numbers_and_recomputes_lat_lon() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N, "latitude": 0.0, "longitude": 0.0}
    locate.calls.clear()
    gs.set_crs(keys, {}, CRS_MODE, "EPSG:28356 — GDA94 / MGA zone 56", locate)
    assert keys["SRID"] == "EPSG:28356"  # a picker entry is stored as its plain code
    assert (keys["crs x"], keys["crs y"]) == (E, N)
    assert locate.calls == [("EPSG:28356", E, N)]
    assert keys["latitude"] == LAT


def test_a_crs_geoscene_cannot_hold_moves_the_georeference_off_the_keys() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N, "latitude": LAT, "longitude": LON}
    store: dict[str, str] = {}
    for crs in ("EPSG:7856+5711", 'PROJCRS["GDA2020 / MGA zone 56"]'):
        gs.set_crs(keys, store, CRS_MODE, "EPSG:7856", locate)
        gs.set_crs(keys, store, CRS_MODE, crs, locate)
        assert not any(k in keys for k in gs.GEOSCENE_KEYS)  # never a compound SRID
        assert store == {"source_crs": crs, "source_offset_e": repr(E), "source_offset_n": repr(N)}
        assert gs.get_crs(keys, store, CRS_MODE) == crs
        assert float(gs.get_origin(keys, store, CRS_MODE, gs.STORE_E)) == E


def test_choosing_a_writable_crs_promotes_geotools_text_into_the_keys() -> None:
    keys: dict[str, object] = {}
    store = {"source_offset_e": repr(E), "source_offset_n": repr(N)}
    gs.set_crs(keys, store, CRS_MODE, "EPSG:7856", locate)
    assert keys == {
        "SRID": "EPSG:7856",
        "crs x": E,
        "crs y": N,
        "longitude": LON,
        "latitude": LAT,
    }
    assert store == {}  # no second copy


def test_local_and_anchor_write_no_keys() -> None:
    for mode in ("LOCAL", "ANCHOR"):
        keys: dict[str, object] = {}
        store: dict[str, str] = {}
        gs.set_crs(keys, store, mode, "EPSG:7856", locate)
        gs.set_origin(keys, store, mode, gs.STORE_E, "502000", locate)
        assert keys == {} and store == {"source_crs": "EPSG:7856", "source_offset_e": "502000"}
        assert gs.reconcile(keys, store, mode, locate) is None


def test_an_unparseable_srid_is_read_and_never_rewritten() -> None:
    wkt = 'PROJCRS["GDA2020 / MGA zone 56",BASEGEOGCRS["GDA2020"]]'
    keys = {"SRID": wkt, "crs x": E, "crs y": N}
    store = {"source_crs": "EPSG:7856"}
    assert gs.get_crs(keys, store, CRS_MODE) == wkt
    gs.reconcile(keys, store, CRS_MODE, locate)
    gs.set_origin(keys, {}, CRS_MODE, gs.STORE_E, "1.0", locate)
    assert keys["SRID"] == wkt


# --- migration ----------------------------------------------------------------------------


def test_migration_moves_an_earlier_geotools_project_into_the_keys() -> None:
    keys: dict[str, object] = {}
    store = {"source_crs": "EPSG:7856", "source_offset_e": repr(E), "source_offset_n": repr(N)}
    done = gs.reconcile(keys, store, CRS_MODE, locate)
    assert done is not None and done.changed and not done.dropped
    assert (keys["SRID"], keys["crs x"], keys["crs y"]) == ("EPSG:7856", E, N)
    assert keys["latitude"] == LAT and store == {}
    text = done.notice()
    assert "EPSG:7856" in text and repr(E) in text and "Offset A" in text
    # Once only: nothing is left to migrate.
    again = gs.reconcile(keys, store, CRS_MODE, locate)
    assert again is not None and not again.changed


def test_migration_of_a_project_whose_offset_was_never_set_keeps_zero() -> None:
    keys: dict[str, object] = {}
    gs.reconcile(keys, {"source_crs": "EPSG:7856"}, CRS_MODE, locate)
    assert (keys["crs x"], keys["crs y"]) == (0.0, 0.0)


def test_migration_when_both_exist_and_differ_the_keys_win() -> None:
    keys = {"SRID": "EPSG:28356", "crs x": 1000.0, "crs y": N}
    store = {"source_crs": "EPSG:7856", "source_offset_e": repr(E), "source_offset_n": repr(N)}
    done = gs.reconcile(keys, store, CRS_MODE, locate)
    assert done is not None
    assert (keys["SRID"], keys["crs x"], keys["crs y"]) == ("EPSG:28356", 1000.0, N)
    assert store == {}
    assert len(done.dropped) == 2  # the CRS and E; N agreed
    text = done.notice()
    assert "EPSG:28356" in text and "EPSG:7856" in text and "win" in text


def test_migration_fills_only_missing_origin_keys() -> None:
    keys = {"SRID": "EPSG:7856", "crs y": N}
    store = {"source_crs": "EPSG:7856", "source_offset_e": repr(E), "source_offset_n": "5.0"}
    done = gs.reconcile(keys, store, CRS_MODE, locate)
    assert keys["crs x"] == E and keys["crs y"] == N
    assert done is not None and any("crs x" in m for m in done.moved)


def test_migration_leaves_a_crs_geoscene_cannot_hold_with_geotools() -> None:
    store = {"source_crs": 'PROJCRS["x"]', "source_offset_e": repr(E)}
    keys: dict[str, object] = {}
    assert gs.reconcile(keys, dict(store), CRS_MODE, locate) is None
    assert keys == {}


def test_migration_without_pyproj_writes_no_lat_lon() -> None:
    keys: dict[str, object] = {}
    store = {"source_crs": "EPSG:7856", "source_offset_e": repr(E), "source_offset_n": repr(N)}
    gs.reconcile(keys, store, CRS_MODE, no_locate)
    assert set(keys) == {"SRID", "crs x", "crs y"}


# --- warnings ----------------------------------------------------------------------------


def test_local_and_anchor_projects_warn_about_keys() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N}
    for mode in ("LOCAL", "ANCHOR"):
        (issue,) = gs.interop_issues(keys, mode, 0.0)
        assert issue.code == "georef-keys-ignored" and issue.severity is Severity.WARNING
        assert "EPSG:7856" in issue.message and "Use the scene's BlenderGIS" in issue.message
    assert gs.interop_issues({}, "LOCAL", 0.0) == []


def test_a_not_zero_warns_only_with_keys() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N}
    assert gs.interop_issues(keys, CRS_MODE, 0.0) == []
    (issue,) = gs.interop_issues(keys, CRS_MODE, 12.5)
    assert issue.code == "georef-height-shift" and "12.5 m lower" in issue.message
    assert gs.interop_issues({}, CRS_MODE, 12.5) == []


# --- lat/lon with pyproj ---------------------------------------------------------------------


def test_origin_lon_lat_with_pyproj() -> None:
    pytest.importorskip("pyproj")
    found = gs.origin_lon_lat("EPSG:7856", 502000.0, 6961000.0)
    assert found is not None
    lon, lat = found
    assert lon == pytest.approx(153.02024387, abs=1e-7)
    assert lat == pytest.approx(-27.47456841, abs=1e-7)
    assert gs.origin_lon_lat("EPSG:4326", 153.0, -27.0) == pytest.approx((153.0, -27.0))
    assert gs.origin_lon_lat("not a crs", 1.0, 2.0) is None
