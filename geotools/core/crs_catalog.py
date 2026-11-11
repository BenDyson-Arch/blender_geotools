"""The project CRS: the EPSG picker's catalogue, rusty-tiles' CRS check, preset migration.

rusty-tiles 0.4.1 resolves 3,211 EPSG codes (a GDAL-verified allow-list on top
of WGS84 geographic, UTM and Mercator), and GeoTools needs its Python
functions (``deps.CRS_FUNCTIONS``; ``deps.probe_rusty_tiles`` reports an older
wheel as not available):

- ``supported_epsg_codes()``: every accepted code as ``"EPSG:nnnn"``;
- ``resolve_source_crs(definition)``: what the converters would do with a
  definition, without converting anything: ``accepted``, ``kind``,
  ``definition``, ``crsOperation`` (the EPSG datum operation and its stated
  accuracy), ``summary`` and ``refusal``.

This module turns that into:

- :func:`catalog` and :func:`search`, the picker's entries
  (``EPSG:7856 — GDA2020 / MGA zone 56``) with names and areas of use from the
  bundled pyproj (codes only without it), ordered or filtered by whether their
  area of use holds the site;
- :func:`resolve` (cached by definition) and :func:`check_issues`, what Run
  Checks and the Project panel show before any bake;
- :func:`migrated`, the EPSG code that replaces a saved preset's PROJ string
  (:mod:`geotools.core.crs_migration`), only when rusty-tiles resolves that
  code to the very same definition, so the published placement cannot change.

The plain ``EPSG:nnnn`` is what the project stores and passes to rusty-tiles.
WKT and PROJ text still work as typed. Without rusty-tiles the catalogue is
empty and :func:`resolve` returns None; the panel says rusty-tiles is missing.
No ``bpy``.
"""

from __future__ import annotations

import importlib
import logging
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from types import ModuleType
from typing import Any

from . import deps
from .crs_migration import preset_code
from .issues import Issue, Severity

log = logging.getLogger(__name__)

SEPARATOR = " — "
"""Between the code and the name in a picker entry."""

LonLat = tuple[float, float]
Area = tuple[float, float, float, float]
"""west, south, east, north in degrees; west > east crosses the antimeridian."""


# --- rusty-tiles' CRS functions ---------------------------------------------------------


def _rusty_tiles() -> ModuleType | None:
    """rusty-tiles, when ``deps`` reports it available (0.4.1 or later); else None."""
    if not deps.probe_rusty_tiles().available:
        return None
    try:
        return importlib.import_module(deps.MODULE_NAME)
    except Exception:  # probed above; a broken wheel can still raise anything
        log.debug("rusty-tiles import failed", exc_info=True)
        return None


def available() -> bool:
    """True when rusty-tiles can list and resolve EPSG codes."""
    return _rusty_tiles() is not None


@lru_cache(maxsize=1)
def supported_codes() -> tuple[str, ...]:
    """Every EPSG code rusty-tiles resolves, in numeric order; empty without rusty-tiles."""
    rt = _rusty_tiles()
    return () if rt is None else tuple(str(code) for code in rt.supported_epsg_codes())


@dataclass(frozen=True, slots=True)
class CrsResolution:
    """``rusty_tiles.resolve_source_crs(definition)``, as GeoTools reads it."""

    definition: str
    """The text that was resolved, as the project holds it."""
    accepted: bool
    kind: str | None
    """``builtin``, ``allowlist``, ``wkt``, ``proj``, or None for unknown text."""
    resolved: str | None
    """The definition the transform is built from (a PROJ string for EPSG codes)."""
    operation: dict[str, Any] | None
    """``crsOperation``: the EPSG datum operation and its stated accuracy (allow-list codes)."""
    summary: str | None
    refusal: str | None

    @property
    def accuracy_m(self) -> float | None:
        value = (self.operation or {}).get("accuracyMetres")
        return float(value) if isinstance(value, (int, float)) else None

    def message(self) -> str:
        """One line for the panel and Check: the summary, or why it is refused."""
        if not self.accepted:
            return refusal_text(self.refusal)
        if self.summary:
            return self.summary
        if self.kind in ("wkt", "proj"):
            return f"Accepted as a {self.kind.upper()} definition, applied exactly as written."
        return "Accepted."


_NATIVE_HINT = re.compile(r";\s*use a build with --features native-geospatial.*$", re.DOTALL)
_PREFIX = "pure-Rust CRS transform unavailable: "


def refusal_text(refusal: str | None) -> str:
    """rusty-tiles' refusal without the advice to rebuild it, which a user cannot follow."""
    text = (refusal or "rusty-tiles refused this CRS").strip()
    text = _NATIVE_HINT.sub("", text)
    text = text.removeprefix(_PREFIX)
    text = text[:1].upper() + text[1:]
    return f"{text.rstrip('.')}. The bundled rusty-tiles cannot place data in this CRS."


_resolutions: dict[str, CrsResolution] = {}


def resolve(definition: str | None) -> CrsResolution | None:
    """rusty-tiles' verdict on ``definition``, cached by definition.

    None for empty text, or when rusty-tiles is not available.
    """
    text = (definition or "").strip()
    if not text:
        return None
    cached = _resolutions.get(text)
    if cached is not None:
        return cached
    rt = _rusty_tiles()
    if rt is None:
        return None
    raw = dict(rt.resolve_source_crs(text))  # ordinary refusals never raise
    operation = raw.get("crsOperation")
    result = CrsResolution(
        definition=text,
        accepted=bool(raw.get("accepted")),
        kind=raw.get("kind") or None,
        resolved=raw.get("definition") or None,
        operation=dict(operation) if isinstance(operation, dict) else None,
        summary=raw.get("summary") or None,
        refusal=raw.get("refusal") or None,
    )
    _resolutions[text] = result
    return result


def check_issues(definition: str) -> list[Issue]:
    """Run Checks' CRS issues: the refusal as an error, else the datum operation as a note."""
    result = resolve(definition)
    if result is None:
        return []
    if not result.accepted:
        return [Issue(Severity.ERROR, "crs-refused", result.message())]
    return [Issue(Severity.INFO, "crs-operation", f"Source CRS: {result.message()}")]


# --- the catalogue -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrsEntry:
    code: str
    """``"EPSG:7856"``, what choosing the entry stores."""
    name: str
    """``"GDA2020 / MGA zone 56"``, or empty without pyproj."""
    area: Area | None = None
    area_name: str = ""

    @property
    def label(self) -> str:
        """``"EPSG:7856 — GDA2020 / MGA zone 56"`` (just the code without a name)."""
        return f"{self.code}{SEPARATOR}{self.name}" if self.name else self.code

    def contains(self, lon_lat: LonLat) -> bool:
        if self.area is None:
            return False
        lon, lat = lon_lat
        west, south, east, north = self.area
        if not south <= lat <= north:
            return False
        if west <= east:
            return west <= lon <= east
        return lon >= west or lon <= east  # crosses the antimeridian

    def area_size(self) -> float:
        """Square degrees, to put the most local CRS first."""
        if self.area is None:
            return math.inf
        west, south, east, north = self.area
        width = east - west if west <= east else east - west + 360.0
        return width * (north - south)


def _code_number(code: str) -> int:
    return int(code.partition(":")[2] or 0)


def _pyproj_info() -> dict[str, Any]:
    """EPSG code number -> pyproj ``CRSInfo``; empty when pyproj is not available."""
    if not deps.probe_pyproj().available:
        return {}
    try:
        from pyproj.database import query_crs_info

        return {info.code: info for info in query_crs_info(auth_name="EPSG")}
    except Exception:  # names are a convenience; codes still list without them
        log.warning("pyproj CRS names unavailable", exc_info=True)
        return {}


def _entry(code: str, info: Any) -> CrsEntry:
    if info is None:
        return CrsEntry(code, "")
    area = info.area_of_use
    bounds = None
    if area is not None and all(
        math.isfinite(v) for v in (area.west, area.south, area.east, area.north)
    ):
        bounds = (float(area.west), float(area.south), float(area.east), float(area.north))
    return CrsEntry(
        code, str(info.name or ""), bounds, str(getattr(area, "name", "") or "").rstrip(".")
    )


@lru_cache(maxsize=1)
def catalog() -> tuple[CrsEntry, ...]:
    """The picker's entries in code order: every code rusty-tiles resolves.

    Names and areas of use come from pyproj, read once.
    """
    codes = supported_codes()
    info = _pyproj_info() if codes else {}
    return tuple(_entry(code, info.get(code.partition(":")[2])) for code in codes)


@lru_cache(maxsize=1)
def _haystacks() -> tuple[str, ...]:
    return tuple(f"{e.code} {e.name} {e.area_name}".lower() for e in catalog())


_BY_CODE: dict[str, CrsEntry] = {}


def entry(code: str | None) -> CrsEntry | None:
    """The catalogue entry for ``EPSG:nnnn`` (any spelling), if rusty-tiles lists it."""
    normal = normalise_epsg(code)
    if normal is None:
        return None
    if not _BY_CODE:
        _BY_CODE.update((e.code, e) for e in catalog())
    return _BY_CODE.get(normal)


def search(
    query: str = "",
    *,
    near: LonLat | None = None,
    near_only: bool = False,
    limit: int | None = None,
) -> list[CrsEntry]:
    """Entries matching every word of ``query`` (code digits, name or area words).

    With ``near`` (the site's longitude and latitude), entries whose area of use
    holds the site come first, the most local first; with ``near_only`` they are
    the only ones. Otherwise code order.
    """
    words = [w for w in re.split(r"[\s,/()]+", (query or "").lower()) if w]
    words = [w[5:] if w.startswith("epsg:") and w[5:] else w for w in words]
    entries = catalog()
    found = [
        e for e, text in zip(entries, _haystacks(), strict=True) if all(w in text for w in words)
    ]
    if near is not None:
        inside = sorted(
            (e for e in found if e.contains(near)),
            key=lambda e: (e.area_size(), _code_number(e.code)),
        )
        if near_only:
            found = inside
        else:
            held = {e.code for e in inside}
            found = inside + [e for e in found if e.code not in held]
    return found[:limit] if limit is not None else found


_LABEL = re.compile(rf"^\s*(EPSG\s*:\s*\d+)\s*{SEPARATOR.strip()}.*$", re.IGNORECASE | re.DOTALL)


def choice_value(text: str) -> str:
    """What the project stores when ``text`` is entered: a picker label becomes its code.

    ``"EPSG:7856 — GDA2020 / MGA zone 56"`` and ``" epsg:07856"`` become
    ``"EPSG:7856"``; WKT and PROJ text are kept as typed.
    """
    match = _LABEL.match(text or "")
    normal = normalise_epsg(match.group(1) if match else text)
    return normal if normal is not None else text


# --- EPSG codes --------------------------------------------------------------------------

_EPSG_RE = re.compile(r"\s*EPSG\s*:\s*(\d+)\s*", re.IGNORECASE)


def normalise_epsg(text: str | None) -> str | None:
    """``"EPSG:nnnn"`` for a plain EPSG code (any case, spaces, leading zeros); else None."""
    match = _EPSG_RE.fullmatch(text or "")
    return f"EPSG:{int(match.group(1))}" if match else None


def epsg_code(definition: str | None) -> str | None:
    """``"EPSG:nnnn"`` for a plain EPSG code, else None.

    A saved preset's PROJ string not migrated yet (rusty-tiles missing) counts as
    the code it stood for. Recorded in build.json and the GLB georeference
    beside the definition itself.
    """
    return normalise_epsg(definition) or preset_code(definition)


def name(definition: str | None) -> str:
    """The catalogue name of the project's CRS (``"GDA2020 / MGA zone 56"``), or empty."""
    found = entry(epsg_code(definition))
    return found.name if found is not None else ""


# --- migration from the presets -----------------------------------------------------------


def _same_definition(a: str, b: str) -> bool:
    """True when two PROJ strings hold the same parameters (order and spacing aside)."""
    return sorted(a.split()) == sorted(b.split())


def migrated(definition: str | None) -> str | None:
    """The EPSG code that replaces a saved preset's PROJ string, if it is safe now.

    Only when rusty-tiles resolves the preset's code to the same PROJ definition
    as the preset, so the published placement is identical. None for anything
    that is not a preset, and without rusty-tiles.
    """
    code = preset_code(definition)
    if code is None:
        return None
    result = resolve(code)
    if result is None or not result.accepted or not result.resolved:
        return None
    if not _same_definition(result.resolved, (definition or "").strip()):
        log.warning(
            "%s resolves to %r, not the saved preset %r; the preset is kept",
            code,
            result.resolved,
            definition,
        )
        return None
    return code


def current_definition(definition: str) -> str:
    """The project CRS as GeoTools uses it: a migratable preset becomes its EPSG code."""
    return migrated(definition) or definition


# --- where the site is ----------------------------------------------------------------


@lru_cache(maxsize=64)
def lon_lat_of(definition: str, easting: float, northing: float) -> LonLat | None:
    """WGS84 longitude and latitude of a projected point, with pyproj; None if it can't tell."""
    if not definition.strip() or not deps.probe_pyproj().available:
        return None
    try:
        import pyproj

        crs = pyproj.CRS.from_user_input(epsg_code(definition) or definition.strip())
        if not crs.is_projected:
            return None
        lon, lat = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform(
            easting, northing
        )
    except Exception:  # advisory: the picker then lists in code order
        log.debug("locating %r failed", definition, exc_info=True)
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)) or abs(lat) > 90.0:
        return None
    return float(lon), float(lat)


def reset_cache() -> None:
    """Forget everything read from rusty-tiles and pyproj (Check Dependencies reloads)."""
    supported_codes.cache_clear()
    catalog.cache_clear()
    _haystacks.cache_clear()
    lon_lat_of.cache_clear()
    _resolutions.clear()
    _BY_CODE.clear()
