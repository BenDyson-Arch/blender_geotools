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


# --- an origin from latitude and longitude ----------------------------------------------------


def project(definition: str, lon: float, lat: float) -> tuple[float, float]:
    """``locate``'s inverse: records what it was asked."""
    project.calls.append((definition, lon, lat))
    return E + (lon - LON) * 1e5, N + (lat - LAT) * 1e5


project.calls = []


def no_project(definition: str, lon: float, lat: float) -> None:
    """No pyproj: the origin cannot be computed."""
    return


GEOGRAPHIC = {"latitude": LAT, "longitude": LON}


def test_origin_from_lat_lon_projects_the_keys_into_the_crs() -> None:
    project.calls.clear()
    keys = {"SRID": "EPSG:7856", **GEOGRAPHIC}
    assert gs.origin_from_lat_lon(keys, "EPSG:7856", project) == (E, N)
    assert project.calls == [("EPSG:7856", LON, LAT)]  # lon first: always_xy
    shifted = {"latitude": LAT + 1e-5, "longitude": LON - 2e-5}
    x, y = gs.origin_from_lat_lon(shifted, "EPSG:28356", project)
    assert (x, y) == project("EPSG:28356", LON - 2e-5, LAT + 1e-5)  # doubles, unrounded
    assert type(x) is float and not x.is_integer()


@pytest.mark.parametrize(
    "keys",
    [
        {},
        {"latitude": LAT},
        {"longitude": LON},
        {"latitude": math.nan, "longitude": LON},
        {"latitude": LAT, "longitude": math.inf},
        {"latitude": "-27.47", "longitude": LON},  # not a number
        {"latitude": 91.0, "longitude": LON},
        {"latitude": LAT, "longitude": 181.0},
    ],
)
def test_origin_from_lat_lon_needs_finite_lat_lon(keys: dict) -> None:
    assert gs.origin_from_lat_lon(keys, "EPSG:7856", project) is None


@pytest.mark.parametrize("crs", ["", "EPSG:7856+5711", 'PROJCRS["x"]', "ESRI:102100"])
def test_origin_from_lat_lon_needs_a_crs_geoscene_can_hold(crs: str) -> None:
    assert gs.origin_from_lat_lon(dict(GEOGRAPHIC), crs, project) is None


def test_origin_from_lat_lon_without_a_projection() -> None:
    assert gs.origin_from_lat_lon(dict(GEOGRAPHIC), "EPSG:7856", no_project) is None
    nan = lambda *_: (math.nan, N)
    assert gs.origin_from_lat_lon(dict(GEOGRAPHIC), "EPSG:7856", nan) is None


def test_ground_distance() -> None:
    assert gs.ground_distance(LON, LAT, LON, LAT) == 0.0
    assert gs.ground_distance(0.0, 0.0, 0.0, 1.0) == pytest.approx(111_195.08, abs=0.01)
    # A centimetre stays a centimetre in doubles (no cancellation).
    assert gs.ground_distance(LON, LAT, LON, LAT + 1e-7) == pytest.approx(0.0111195, rel=1e-6)


def test_offered_when_the_scene_has_lat_lon_but_no_projected_origin() -> None:
    keys = {"SRID": "EPSG:7856", **GEOGRAPHIC}
    offer = gs.lat_lon_origin(keys, {}, CRS_MODE, locate)
    assert offer is not None and offer.missing and offer.distance_m is None
    assert (offer.crs, offer.latitude, offer.longitude) == ("EPSG:7856", LAT, LON)
    issue = offer.issue()
    assert issue.code == "georef-origin-lat-lon-only" and issue.severity is Severity.WARNING
    assert "Origin from Latitude/Longitude" in issue.message and "-27.4700000" in issue.message
    # One origin key is not an origin.
    assert gs.lat_lon_origin({**keys, "crs x": E}, {}, CRS_MODE, locate).missing


def test_not_offered_when_lat_lon_agree_with_the_origin() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N, **GEOGRAPHIC}
    assert gs.lat_lon_origin(keys, {}, CRS_MODE, locate) is None
    # Within a centimetre (1e-7 degrees of latitude is 1.1 cm; 5e-8 is 5.6 mm).
    keys["latitude"] = LAT + 5e-8
    assert gs.lat_lon_origin(keys, {}, CRS_MODE, locate) is None


def test_disagreement_is_reported_in_metres_and_nothing_changes() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": E, "crs y": N, "latitude": LAT + 1e-3, "longitude": LON}
    before = dict(keys)
    offer = gs.lat_lon_origin(keys, {}, CRS_MODE, locate)
    assert offer is not None and not offer.missing
    assert offer.current == (E, N)
    assert offer.distance_m == pytest.approx(111.195, abs=1e-3)
    issue = offer.issue()
    assert issue.code == "georef-origin-lat-lon-differ"
    assert "111.20 m" in issue.message and "502000.123" in issue.message, issue.message
    assert keys == before
    assert gs.format_metres(0.0421) == "0.042 m"
    assert gs.format_metres(2345.0) == "2.35 km"
    # Cannot tell where E/N lie: nothing to say.
    assert gs.lat_lon_origin(keys, {}, CRS_MODE, no_locate) is None


def test_not_offered_outside_a_crs_project_with_a_geoscene_crs() -> None:
    keys = {"SRID": "EPSG:7856", **GEOGRAPHIC}
    for mode in ("LOCAL", "ANCHOR"):
        assert gs.lat_lon_origin(keys, {"source_crs": "EPSG:7856"}, mode, locate) is None
    assert gs.lat_lon_origin(dict(GEOGRAPHIC), {}, CRS_MODE, locate) is None  # no CRS
    compound = {"source_crs": "EPSG:7856+5711"}
    assert gs.lat_lon_origin(dict(GEOGRAPHIC), compound, CRS_MODE, locate) is None
    assert gs.lat_lon_origin({"SRID": "EPSG:7856"}, {}, CRS_MODE, locate) is None  # no lat/lon


def test_geotools_own_origin_counts_while_the_crs_is_not_in_the_keys() -> None:
    store = {"source_crs": "EPSG:7856"}
    assert gs.lat_lon_origin(dict(GEOGRAPHIC), store, CRS_MODE, locate).missing
    store = {"source_crs": "EPSG:7856", "source_offset_e": repr(E), "source_offset_n": repr(N)}
    assert gs.lat_lon_origin(dict(GEOGRAPHIC), store, CRS_MODE, locate) is None  # they agree
    store["source_offset_e"] = repr(E + 50.0)
    offer = gs.lat_lon_origin(dict(GEOGRAPHIC), store, CRS_MODE, locate)
    assert offer is not None and offer.current == (E + 50.0, N)


def test_choosing_a_crs_keeps_a_geographic_only_origin() -> None:
    """Earlier GeoTools wrote crs x/y = 0 here, then overwrote lat/lon from them."""
    keys = dict(GEOGRAPHIC)
    gs.set_crs(keys, {}, CRS_MODE, "EPSG:7856", locate)
    assert keys == {"SRID": "EPSG:7856", **GEOGRAPHIC}
    assert gs.lat_lon_origin(keys, {}, CRS_MODE, locate).missing
    # Without lat/lon the old rule stands: E/N 0, as the project used.
    keys = {}
    gs.set_crs(keys, {}, CRS_MODE, "EPSG:7856", locate)
    assert (keys["crs x"], keys["crs y"]) == (0.0, 0.0)


def test_held_geotools_origin_moves_but_lat_lon_are_not_recomputed() -> None:
    keys = {"latitude": LAT + 1e-3, "longitude": LON}
    store = {"source_crs": "EPSG:7856", "source_offset_e": repr(E), "source_offset_n": repr(N)}
    done = gs.reconcile(keys, store, CRS_MODE, locate)
    assert (keys["crs x"], keys["crs y"]) == (E, N)
    assert keys["latitude"] == LAT + 1e-3  # not overwritten
    assert done is not None and any("as found" in k for k in done.kept)
    offer = gs.lat_lon_origin(keys, store, CRS_MODE, locate)
    assert offer is not None and offer.distance_m == pytest.approx(111.195, abs=1e-3)


def test_set_origin_from_lat_lon_writes_the_keys_and_resyncs() -> None:
    project.calls.clear()
    locate.calls.clear()
    keys = {"SRID": "EPSG:7856", "latitude": LAT + 1e-3, "longitude": LON - 2e-3, "scale": 1}
    x, y = gs.set_origin_from_lat_lon(keys, {}, CRS_MODE, project, locate)
    assert keys["crs x"] == x and keys["crs y"] == y
    assert type(keys["crs x"]) is float
    assert x == pytest.approx(E - 200.0) and y == pytest.approx(N + 100.0)
    assert locate.calls[-1] == ("EPSG:7856", x, y)  # lat/lon re-synced from the new origin
    assert keys["latitude"] == pytest.approx(LAT + 1e-3, abs=1e-12)
    assert keys["scale"] == 1
    assert gs.lat_lon_origin(keys, {}, CRS_MODE, locate) is None  # agreed now


def test_set_origin_from_lat_lon_replaces_a_disagreeing_origin() -> None:
    keys = {"SRID": "EPSG:7856", "crs x": 1.0, "crs y": 2.0, **GEOGRAPHIC}
    assert gs.set_origin_from_lat_lon(keys, {}, CRS_MODE, project, locate) == (E, N)
    assert (keys["crs x"], keys["crs y"]) == (E, N)
    assert (keys["longitude"], keys["latitude"]) == (LON, LAT)


def test_set_origin_from_lat_lon_puts_a_held_crs_into_the_keys() -> None:
    keys = dict(GEOGRAPHIC)
    store = {"source_crs": "EPSG:7856"}
    assert gs.set_origin_from_lat_lon(keys, store, CRS_MODE, project, locate) == (E, N)
    assert (keys["SRID"], keys["crs x"], keys["crs y"]) == ("EPSG:7856", E, N)
    assert store == {}


def test_set_origin_from_lat_lon_refuses_and_changes_nothing() -> None:
    keys = {"SRID": "EPSG:7856", **GEOGRAPHIC}
    for mode in ("LOCAL", "ANCHOR"):
        assert gs.set_origin_from_lat_lon(keys, {}, mode, project, locate) is None
    assert gs.set_origin_from_lat_lon(keys, {}, CRS_MODE, no_project, locate) is None
    assert keys == {"SRID": "EPSG:7856", **GEOGRAPHIC}
    no_lat_lon = {"SRID": "EPSG:7856", "crs x": E, "crs y": N}
    assert gs.set_origin_from_lat_lon(no_lat_lon, {}, CRS_MODE, project, locate) is None
    assert no_lat_lon == {"SRID": "EPSG:7856", "crs x": E, "crs y": N}


def test_interop_issues_include_the_lat_lon_offer_in_a_crs_project_only() -> None:
    keys = {"SRID": "EPSG:7856", **GEOGRAPHIC}
    codes = [i.code for i in gs.interop_issues(keys, CRS_MODE, 3.0, {}, locate)]
    assert codes == ["georef-origin-lat-lon-only", "georef-height-shift"]
    codes = [i.code for i in gs.interop_issues(keys, "LOCAL", 0.0, {}, locate)]
    assert codes == ["georef-keys-ignored"]


def test_lon_lat_to_origin_with_pyproj_inverts_origin_lon_lat() -> None:
    pytest.importorskip("pyproj")
    lon, lat = gs.origin_lon_lat("EPSG:7856", 502000.0, 6961000.0)
    x, y = gs.lon_lat_to_origin("EPSG:7856", lon, lat)
    assert x == pytest.approx(502000.0, abs=1e-6) and y == pytest.approx(6961000.0, abs=1e-6)
    assert gs.lon_lat_to_origin("not a crs", lon, lat) is None
    assert gs.lon_lat_to_origin("EPSG:7856", math.nan, lat) is None
    assert gs.lon_lat_to_origin("", lon, lat) is None
    # Disagreement in metres, with the real projection: 100 m east of the origin.
    keys = {"SRID": "EPSG:7856", "crs x": 502100.0, "crs y": 6961000.0}
    keys["longitude"], keys["latitude"] = lon, lat
    offer = gs.lat_lon_origin(keys, {}, CRS_MODE)
    assert offer is not None and offer.distance_m == pytest.approx(100.0, rel=0.01)
    assert gs.set_origin_from_lat_lon(keys, {}, CRS_MODE) is not None
    assert keys["crs x"] == pytest.approx(502000.0, abs=1e-6)
    assert gs.lat_lon_origin(keys, {}, CRS_MODE) is None
