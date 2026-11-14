"""Make ``import geotools`` work from a source checkout, without Blender."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

for path in (REPO_ROOT, REPO_ROOT / "tests"):  # tests/: glbcheck, shared with the Blender suite
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


# --- the project CRS: rusty-tiles 0.4.1's allow-list, or the 0.4.0 wheel ----------------

import pytest

from geotools.core import crs_catalog
from geotools.core.crs_migration import PRESET_CODES

PRESET_DEFINITION = {code: text for text, code in PRESET_CODES.items()}
"""EPSG code -> the old preset's PROJ string, which rusty-tiles 0.4.1 resolves it to."""

REFUSED_27700 = (
    "pure-Rust CRS transform unavailable: EPSG:27700 is not in the standard build's "
    "GDAL-verified EPSG allow-list; use a build with --features native-geospatial "
    "(GDAL >= 3.12, PROJ >= 9.2) and the required local PROJ database/grids"
)


class FakeAllowList:
    """The two rusty-tiles 0.4.1 CRS functions, for the old presets' codes and a few others."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def supported_epsg_codes(self) -> list[str]:
        codes = set(PRESET_CODES.values()) | {"EPSG:4326", "EPSG:32756", "EPSG:28356"}
        return sorted(codes, key=lambda c: int(c[5:]))

    def resolve_source_crs(self, definition: str) -> dict[str, object]:
        self.calls.append(definition)
        code = crs_catalog.normalise_epsg(definition)
        if code in PRESET_DEFINITION:
            operation = {
                "source": code,
                "kind": "null",
                "operation": "GDA2020 to WGS 84 (2)",
                "operationId": "EPSG:8450",
                "accuracyMetres": 3.0,
                "verification": "GDAL-verified allow-list (test)",
            }
            return {
                "accepted": True,
                "kind": "allowlist",
                "definition": PRESET_DEFINITION[code],
                "crsOperation": operation,
                "summary": f"{code}: GDA2020 to WGS 84 (2) (EPSG:8450), EPSG-stated accuracy 3 m",
                "refusal": None,
            }
        if code in ("EPSG:4326", "EPSG:32756"):
            return {
                "accepted": True,
                "kind": "builtin",
                "definition": "+proj=utm +zone=56 +south +datum=WGS84 +units=m",
                "crsOperation": None,
                "summary": f"{code}: WGS 84, no datum change",
                "refusal": None,
            }
        if code is None and definition.lstrip().startswith("+proj"):
            return {
                "accepted": True,
                "kind": "proj",
                "definition": definition,
                "crsOperation": None,
                "summary": None,
                "refusal": None,
            }
        return {
            "accepted": False,
            "kind": "allowlist" if code else None,
            "definition": None,
            "crsOperation": None,
            "summary": None,
            "refusal": REFUSED_27700,
        }


@pytest.fixture
def allow_list(monkeypatch: pytest.MonkeyPatch):
    """rusty-tiles 0.4.1's CRS functions (a fake), whether or not a wheel is installed."""
    fake = FakeAllowList()
    monkeypatch.setattr(crs_catalog, "_rusty_tiles", lambda: fake)
    crs_catalog.reset_cache()
    yield fake
    crs_catalog.reset_cache()
