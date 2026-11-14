"""The EPSG picker's catalogue, rusty-tiles' CRS check and the preset migration."""

from __future__ import annotations

import importlib.util

import numpy as np
import pytest

from geotools.core import crs_catalog, deps
from geotools.core.crs_catalog import CrsEntry
from geotools.core.crs_migration import PRESET_CODES, preset_code
from geotools.core.issues import Severity
from geotools.core.placement import Placement, PlacementMode

HAS_PYPROJ = importlib.util.find_spec("pyproj") is not None
needs_pyproj = pytest.mark.skipif(not HAS_PYPROJ, reason="pyproj is not installed")
MGA56_PRESET = next(text for text, code in PRESET_CODES.items() if code == "EPSG:7856")


def _real_rusty_tiles() -> bool:
    deps.reset_cache()
    return crs_catalog.available()


real = pytest.mark.skipif(
    not _real_rusty_tiles(), reason="needs rusty-tiles 0.4.1 (supported_epsg_codes)"
)


# --- codes and labels ---------------------------------------------------------------------


def test_migration_table() -> None:
    assert len(PRESET_CODES) == 15
    assert sorted(PRESET_CODES.values()) == sorted(
        [f"EPSG:{7800 + zone}" for zone in range(46, 60)] + ["EPSG:2193"]
    )
    assert preset_code(f"  {MGA56_PRESET}\n") == "EPSG:7856"
    assert preset_code("EPSG:7856") is None and preset_code(None) is None


def test_epsg_code() -> None:
    for text, code in PRESET_CODES.items():
        assert crs_catalog.epsg_code(text) == code  # a saved preset, not migrated yet
    assert crs_catalog.epsg_code("EPSG:32756") == "EPSG:32756"
    assert crs_catalog.epsg_code(" epsg:4326 ") == "EPSG:4326"
    assert crs_catalog.epsg_code("EPSG:032756") == "EPSG:32756"
    for other in ("", None, "+proj=utm +zone=56 +south", "EPSG:", "EPSG:abc", "ESRI:102100"):
        assert crs_catalog.epsg_code(other) is None, other


def test_choice_value_stores_the_plain_code() -> None:
    assert crs_catalog.choice_value("EPSG:7856 — GDA2020 / MGA zone 56") == "EPSG:7856"
    assert crs_catalog.choice_value("epsg:07856 — anything") == "EPSG:7856"
    assert crs_catalog.choice_value(" epsg:7856 ") == "EPSG:7856"
    assert crs_catalog.choice_value("EPSG:7856") == "EPSG:7856"
    proj = "+proj=tmerc +lon_0=151 +ellps=GRS80 +towgs84=0,0,0 +units=m"
    assert crs_catalog.choice_value(proj) == proj  # free text is kept as typed
    wkt = 'PROJCRS["x — y",BASEGEOGCRS["GDA2020"]]'
    assert crs_catalog.choice_value(wkt) == wkt
    assert crs_catalog.choice_value("") == ""


def test_entry_label_and_area() -> None:
    entry = CrsEntry("EPSG:7856", "GDA2020 / MGA zone 56", (150.0, -58.96, 156.0, -13.87))
    assert entry.label == "EPSG:7856 — GDA2020 / MGA zone 56"
    assert CrsEntry("EPSG:7856", "").label == "EPSG:7856"
    assert entry.contains((153.0, -27.5)) and not entry.contains((149.0, -27.5))
    across = CrsEntry("EPSG:1", "x", (170.0, -50.0, -170.0, -30.0))  # crosses 180
    assert across.contains((175.0, -40.0)) and across.contains((-175.0, -40.0))
    assert not across.contains((0.0, -40.0))
    assert across.area_size() == pytest.approx(20.0 * 20.0)


def test_refusal_text_drops_the_rebuild_advice() -> None:
    text = crs_catalog.refusal_text(
        "pure-Rust CRS transform unavailable: EPSG:27700 is not in the standard build's "
        "GDAL-verified EPSG allow-list; use a build with --features native-geospatial "
        "(GDAL >= 3.12, PROJ >= 9.2) and the required local PROJ database/grids"
    )
    assert text.startswith("EPSG:27700 is not in the standard build's GDAL-verified")
    assert "native-geospatial" not in text and "pure-Rust" not in text
    assert text.endswith("The bundled rusty-tiles cannot place data in this CRS.")


# --- search -------------------------------------------------------------------------------

ENTRIES = (
    CrsEntry("EPSG:4326", "WGS 84", (-180.0, -90.0, 180.0, 90.0), "World"),
    CrsEntry("EPSG:7855", "GDA2020 / MGA zone 55", (144.0, -60.0, 150.0, -9.0), "Australia"),
    CrsEntry("EPSG:7856", "GDA2020 / MGA zone 56", (150.0, -59.0, 156.0, -13.0), "Australia"),
    CrsEntry("EPSG:28356", "GDA94 / MGA zone 56", (150.0, -59.0, 156.0, -13.0), "Australia"),
    CrsEntry("EPSG:2193", "NZGD2000 / New Zealand Transverse Mercator 2000", None, ""),
)


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch):
    crs_catalog.reset_cache()
    monkeypatch.setattr(crs_catalog, "catalog", lambda: ENTRIES)
    monkeypatch.setattr(
        crs_catalog,
        "_haystacks",
        lambda: tuple(f"{e.code} {e.name} {e.area_name}".lower() for e in ENTRIES),
    )
    yield
    monkeypatch.undo()
    crs_catalog.reset_cache()


@pytest.mark.usefixtures("fake_catalog")
def test_search_by_code_and_name_words() -> None:
    def codes(query: str, **kwargs: object) -> list[str]:
        return [e.code for e in crs_catalog.search(query, **kwargs)]

    assert codes("") == [e.code for e in ENTRIES]
    assert codes("7856") == ["EPSG:7856"]
    assert codes("EPSG:7856") == ["EPSG:7856"]
    assert codes("mga 56") == ["EPSG:7856", "EPSG:28356"]
    assert codes("GDA2020 zone") == ["EPSG:7855", "EPSG:7856"]
    assert codes("new zealand") == ["EPSG:2193"]
    assert codes("australia 55") == ["EPSG:7855"]
    assert codes("nothing like it") == []
    assert codes("mga", limit=1) == ["EPSG:7855"]


@pytest.mark.usefixtures("fake_catalog")
def test_search_near_the_site() -> None:
    brisbane = (153.03, -27.47)
    near = [e.code for e in crs_catalog.search("", near=brisbane)]
    # The local zones first (smallest area, then code), then the world, then the rest.
    assert near == ["EPSG:7856", "EPSG:28356", "EPSG:4326", "EPSG:7855", "EPSG:2193"]
    only = [e.code for e in crs_catalog.search("", near=brisbane, near_only=True)]
    assert only == ["EPSG:7856", "EPSG:28356", "EPSG:4326"]
    assert [e.code for e in crs_catalog.search("gda94", near=brisbane, near_only=True)] == [
        "EPSG:28356"
    ]


# --- rusty-tiles' verdict (the 0.4.1 functions, faked) --------------------------------


def test_resolve_is_cached_by_definition(allow_list) -> None:
    first = crs_catalog.resolve("EPSG:7856")
    assert first is not None and first.accepted and first.kind == "allowlist"
    assert first.resolved == MGA56_PRESET and first.accuracy_m == 3.0
    assert crs_catalog.resolve(" EPSG:7856 ") is first
    assert allow_list.calls == ["EPSG:7856"]
    assert crs_catalog.resolve("") is None and crs_catalog.resolve(None) is None
    assert allow_list.calls == ["EPSG:7856"]


def test_check_issues(allow_list) -> None:
    (note,) = crs_catalog.check_issues("EPSG:7856")
    assert note.severity is Severity.INFO and note.code == "crs-operation"
    assert "GDA2020 to WGS 84 (2)" in note.message and "accuracy 3 m" in note.message
    (error,) = crs_catalog.check_issues("EPSG:27700")
    assert error.severity is Severity.ERROR and error.code == "crs-refused"
    assert "EPSG:27700" in error.message and "native-geospatial" not in error.message
    (proj,) = crs_catalog.check_issues("+proj=tmerc +lon_0=151 +ellps=GRS80 +towgs84=0,0,0")
    assert "PROJ definition, applied exactly as written" in proj.message


def test_without_rusty_tiles_nothing_is_listed_or_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crs_catalog, "_rusty_tiles", lambda: None)
    crs_catalog.reset_cache()
    try:
        assert not crs_catalog.available()
        assert crs_catalog.catalog() == () and crs_catalog.search("mga") == []
        assert crs_catalog.resolve("EPSG:7856") is None
        assert crs_catalog.check_issues("EPSG:7856") == []
        assert crs_catalog.migrated(MGA56_PRESET) is None  # can't verify: kept
        assert crs_catalog.current_definition(MGA56_PRESET) == MGA56_PRESET
    finally:
        crs_catalog.reset_cache()


@needs_pyproj
def test_catalog_names_come_from_pyproj(allow_list) -> None:
    entries = {e.code: e for e in crs_catalog.catalog()}
    assert set(entries) == set(allow_list.supported_epsg_codes())
    mga = entries["EPSG:7856"]
    assert mga.label == "EPSG:7856 — GDA2020 / MGA zone 56"
    assert mga.area is not None and mga.contains((153.03, -27.47))
    assert crs_catalog.name("EPSG:7856") == "GDA2020 / MGA zone 56"
    assert crs_catalog.name(MGA56_PRESET) == "GDA2020 / MGA zone 56"
    assert crs_catalog.name("EPSG:27700") == ""  # not listed: rusty-tiles refuses it


def test_catalog_without_pyproj_lists_codes(allow_list, monkeypatch) -> None:
    monkeypatch.setattr(crs_catalog, "_pyproj_info", dict)
    entries = crs_catalog.catalog()
    assert [e.code for e in entries] == allow_list.supported_epsg_codes()
    assert all(e.name == "" and e.label == e.code for e in entries)
    assert [e.code for e in crs_catalog.search("7856")] == ["EPSG:7856"]


# --- migration ----------------------------------------------------------------------------


def test_every_preset_migrates_to_its_code(allow_list) -> None:
    for text, code in PRESET_CODES.items():
        assert crs_catalog.migrated(text) == code
        assert crs_catalog.migrated(f"  {text} ") == code
        assert crs_catalog.current_definition(text) == code
    for other in ("EPSG:7856", "+proj=utm +zone=56 +south +ellps=GRS80", "", None):
        assert crs_catalog.migrated(other) is None
    assert crs_catalog.current_definition("EPSG:32756") == "EPSG:32756"


def test_no_migration_when_rusty_tiles_resolves_the_code_differently(allow_list, monkeypatch):
    # A future EPSG dataset with a real GDA2020 shift would move the placement:
    # then the saved PROJ string is kept.
    resolve = allow_list.resolve_source_crs

    def shifted(definition: str) -> dict:
        raw = resolve(definition)
        if raw["accepted"] and raw["definition"]:
            raw["definition"] = raw["definition"].replace("towgs84=0,0,0,0,0,0,0", "towgs84=1,2,3")
        return raw

    monkeypatch.setattr(allow_list, "resolve_source_crs", shifted)
    assert crs_catalog.migrated(MGA56_PRESET) is None
    assert crs_catalog.current_definition(MGA56_PRESET) == MGA56_PRESET


def test_same_definition_ignores_order_and_spacing() -> None:
    assert crs_catalog._same_definition("+a=1 +b=2", "  +b=2   +a=1 ")
    assert not crs_catalog._same_definition("+a=1 +b=2", "+a=1 +b=3")


# --- the real rusty-tiles 0.4.1 -----------------------------------------------------------


@real
def test_real_allow_list() -> None:
    codes = crs_catalog.supported_codes()
    assert len(codes) > 3000 and len(set(codes)) == len(codes)
    assert {"EPSG:7856", "EPSG:2193", "EPSG:28356", "EPSG:32756"} <= set(codes)
    assert "EPSG:27700" not in codes
    (error,) = crs_catalog.check_issues("EPSG:27700")
    assert error.code == "crs-refused" and "EPSG:27700" in error.message
    (note,) = crs_catalog.check_issues("EPSG:7856")
    assert "GDA2020 to WGS 84 (2)" in note.message and "3 m" in note.message


@real
def test_real_rusty_tiles_resolves_every_preset_to_its_proj_string() -> None:
    for text, code in PRESET_CODES.items():
        assert crs_catalog.migrated(text) == code, code


# Each preset's zone at a site inside it: (E, N) offset in the CRS.
_SITES = {
    **{f"EPSG:{7800 + zone}": (500_000.0, 7_000_000.0) for zone in range(46, 60)},
    "EPSG:2193": (1_750_000.0, 5_430_000.0),
}
# Probe points around the offset (Blender metres): the centre, 30 km corners, and heights.
_POINTS = [
    (0.0, 0.0, 0.0),
    (30_000.0, 30_000.0, 0.0),
    (-30_000.0, 30_000.0, 500.0),
    (30_000.0, -30_000.0, 1_000.0),
    (-30_000.0, -30_000.0, -50.0),
]


@real
@pytest.mark.parametrize("code", sorted(PRESET_CODES.values(), key=lambda c: int(c[5:])))
def test_migration_keeps_the_published_placement_within_1_mm(code: str) -> None:
    """rusty-tiles places probe points at the same ECEF with the preset's PROJ string
    and with its EPSG code: within 1 mm (in fact identical)."""
    from geotools.core.georef import ecef_from_lon_lat_height
    from geotools.core.probe import probe_placement

    preset = next(text for text, c in PRESET_CODES.items() if c == code)
    east, north = _SITES[code]

    def ecef(source_crs: str, point: tuple[float, float, float]) -> np.ndarray:
        placement = Placement(
            mode=PlacementMode.SOURCE_CRS,
            source_crs=source_crs,
            source_offset=(east, north, 0.0),
            height_offset=12.5,
        )
        lo = tuple(v - 0.5 for v in point)
        hi = tuple(v + 0.5 for v in point)
        result = probe_placement(placement, lo, hi)  # its origin is the point
        assert result.ok, result.error
        return ecef_from_lon_lat_height(*result.origin_lon_lat_height)

    for point in _POINTS:
        before, after = ecef(preset, point), ecef(code, point)
        assert float(np.linalg.norm(after - before)) < 0.001, (code, point)
