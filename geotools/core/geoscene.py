"""The scene georeference as BlenderGIS GeoScene keys: the one authority for CRS and origin.

BlenderGIS (and other Blender GIS tools that follow it) keep a scene's
georeference in five custom properties on the Scene ID, the "GeoScene" keys:

- ``SRID``: the CRS, a string: ``"EPSG:7856"``, ``"AUTH:code"`` or a proj4
  string;
- ``crs x``, ``crs y``: the scene origin in CRS units (doubles);
- ``latitude``, ``longitude``: the same origin in WGS84 degrees (doubles),
  kept consistent with the projected origin when it can be computed.

(``scale`` and ``zoom`` belong to BlenderGIS's map viewer; GeoTools never
touches them.) The frame is Blender X = E - ``crs x``, Y = N - ``crs y``,
+Y grid north, no rotation, and Z absolute height. That is exactly a GeoTools
SOURCE_CRS project with offset (``crs x``, ``crs y``, A) and A = 0.

This module implements those semantics independently (BlenderGIS is GPL-3 and
is never imported or copied; key names and their meaning are facts). It is
bpy-free: ``keys`` is any mutable mapping (the Scene's ID properties in
Blender, a dict in tests), and ``store`` is GeoTools' own saved
``source_crs``/``source_offset_e``/``source_offset_n`` text, which only holds the
CRS and E/N while the scene has no ``SRID`` (see :func:`keys_live`). The Blender
glue is ``geotools.blender.scene_georef``. See
docs/design/georeference-interop.md.

Rules, in short:

- In a SOURCE_CRS project with a ``SRID`` key, the keys are the CRS and E/N;
  GeoTools keeps no copy. A and the height offset stay GeoTools properties.
- GeoTools writes ``SRID`` only as a plain horizontal ``EPSG:nnnn`` or a proj4
  string (:func:`srid_for`): BlenderGIS's parser refuses compound codes such as
  ``EPSG:7856+5711`` and WKT, and every BlenderGIS operator reading the scene
  would then fail. Such a CRS stays on the GeoTools side and the scene gets no
  keys. A ``SRID`` some other tool wrote that BlenderGIS cannot parse is still
  read (:func:`definition_of`), and never rewritten.
- Whenever GeoTools sets the CRS or origin it rewrites ``latitude`` and
  ``longitude`` (:func:`sync_lat_lon`). When they cannot be computed, it deletes
  them, as BlenderGIS's own ``setOriginPrj`` does: stale geographic
  coordinates are never left beside a changed projected origin.
- A scene with ``latitude``/``longitude`` but no projected origin (other
  GeoScene tools can leave one) keeps them: GeoTools invents no E/N over them
  and never recomputes them from E/N it held (:func:`reconcile`). The user can
  set E/N from them (:func:`set_origin_from_lat_lon`, offered by
  :func:`lat_lon_origin`), which projects them into the project CRS as
  BlenderGIS's own CRS switch does. Latitude/longitude that disagree with E/N
  are reported, in metres, and neither is changed.
- LOCAL and ANCHOR projects cannot be expressed as GeoScene keys: GeoTools
  writes none for them, and leaves any it finds alone (the panel and Check warn).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from . import crs_catalog, deps
from .issues import Issue, Severity

log = logging.getLogger(__name__)

# --- the keys -----------------------------------------------------------------------------

SRID = "SRID"
CRS_X = "crs x"
CRS_Y = "crs y"
LATITUDE = "latitude"
LONGITUDE = "longitude"
GEOSCENE_KEYS = (SRID, CRS_X, CRS_Y, LATITUDE, LONGITUDE)
"""The keys GeoTools reads and writes. ``scale`` and ``zoom`` are not among them."""

# GeoTools' own saved text (``scene.geotools``), used while the scene has no SRID.
STORE_CRS = "source_crs"
STORE_E = "source_offset_e"
STORE_N = "source_offset_n"
STORE_DEFAULTS = {STORE_CRS: "", STORE_E: "0", STORE_N: "0"}
ORIGIN_KEYS = {STORE_E: CRS_X, STORE_N: CRS_Y}

LonLatFn = Callable[[str, float, float], "tuple[float, float] | None"]
"""``locate(definition, easting, northing) -> (lon, lat)`` or None."""
ProjectFn = Callable[[str, float, float], "tuple[float, float] | None"]
"""``project(definition, lon, lat) -> (easting, northing)`` or None: ``locate``'s inverse."""


def _is_source_crs(mode: object) -> bool:
    """True for the SOURCE_CRS placement mode, as Blender's enum or ``PlacementMode``."""
    return str(getattr(mode, "value", mode)).lower() == "source_crs"


def number_of(value: object) -> float | None:
    """A key's number as a binary64 float; None if it is not a finite int or float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# --- SRID text -----------------------------------------------------------------------------


def blendergis_parses(srid: object) -> bool:
    """Whether BlenderGIS's CRS parser accepts ``srid`` (its rules, implemented here).

    It takes a bare number (an EPSG code), ``AUTH:digits`` with exactly one
    colon (``+init=epsg:4326`` included), or a proj4 string whose every
    parameter starts with ``+``. Anything else (WKT, ``EPSG:7856+5711``,
    ``IGNF:LAMB93``) makes it raise.
    """
    if isinstance(srid, bool):
        return False
    if isinstance(srid, int):
        return True
    if not isinstance(srid, str):
        return False
    text = srid.strip()
    if not text:
        return False
    if text.isdigit():
        return True
    if ":" in text:
        parts = text.split(":")
        return len(parts) == 2 and parts[1].isdigit()
    return all(token.startswith("+") for token in text.split())


def _is_proj4(text: str) -> bool:
    tokens = text.split()
    return bool(tokens) and all(t.startswith("+") for t in tokens) and ":" not in text


@lru_cache(maxsize=256)
def _horizontal(definition: str) -> bool | None:
    """True for a 2D horizontal CRS, False for compound, vertical or 3D; None without pyproj."""
    if not deps.probe_pyproj().available:
        return None
    try:
        import pyproj

        crs = pyproj.CRS.from_user_input(definition)
    except Exception:
        log.debug("pyproj cannot read %r", definition, exc_info=True)
        return None
    if crs.is_compound or crs.is_vertical:
        return False
    if not (crs.is_projected or crs.is_geographic):
        return False
    return len(crs.axis_info) == 2


def srid_for(definition: str | None) -> str | None:
    """The ``SRID`` GeoTools writes for a project CRS, or None when it must not write one.

    Only a plain horizontal ``EPSG:nnnn`` or a proj4 string: BlenderGIS reads
    both. A compound or vertical code, WKT, another authority, a URN or empty
    text gives None, and the CRS stays on the GeoTools side. With pyproj, a
    code or proj4 string pyproj knows to be compound, vertical or 3D is refused
    too; without it, the form alone decides.
    """
    text = (definition or "").strip()
    if not text:
        return None
    code = crs_catalog.normalise_epsg(text)
    candidate = code if code is not None else (" ".join(text.split()) if _is_proj4(text) else None)
    if candidate is None or not blendergis_parses(candidate):
        return None
    if _horizontal(candidate) is False:
        return None
    # A proj4 string is written as the project holds it (whitespace aside).
    return code if code is not None else text


def definition_of(srid: object) -> str:
    """The CRS GeoTools uses for a ``SRID`` value; the key itself is never rewritten.

    BlenderGIS reads a bare number and ``+init=epsg:nnnn`` as EPSG codes, so
    GeoTools does too (``EPSG:nnnn``). ``epsg:07856`` reads as ``EPSG:7856``.
    Anything else is used as written: another authority's code, a proj4
    string, or WKT some other tool stored (pyproj and rusty-tiles may still
    read it). A non-text value gives an empty CRS.
    """
    if isinstance(srid, bool):
        return ""
    if isinstance(srid, int):
        return f"EPSG:{srid}"
    if not isinstance(srid, str):
        return ""
    text = srid.strip()
    if text.isdigit():
        return f"EPSG:{int(text)}"
    lowered = text.lower()
    if lowered.startswith("+init=epsg:") and " " not in text:
        return crs_catalog.normalise_epsg(text[len("+init=") :]) or text
    return crs_catalog.normalise_epsg(text) or text


def same_crs(a: str | None, b: str | None) -> bool:
    """Whether two CRS texts name the same CRS as GeoTools would use it."""

    def norm(text: str | None) -> str:
        text = crs_catalog.current_definition(definition_of(text or ""))
        return " ".join(text.split())

    return norm(a) == norm(b)


# --- reading ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeoScene:
    """The GeoScene keys of one scene, as read (nothing is normalised in place)."""

    srid: object = None
    """The raw ``SRID`` value, None when absent."""
    x: float | None = None
    y: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    present: tuple[str, ...] = ()
    """Which of :data:`GEOSCENE_KEYS` the scene has."""

    @property
    def has_crs(self) -> bool:
        return SRID in self.present

    @property
    def crs(self) -> str:
        """The CRS GeoTools uses for ``SRID`` (:func:`definition_of`); empty if absent."""
        return definition_of(self.srid) if self.has_crs else ""

    @property
    def has_origin(self) -> bool:
        return self.x is not None and self.y is not None

    @property
    def complete(self) -> bool:
        """A CRS and a projected origin: what BlenderGIS calls georeferenced."""
        return self.has_crs and bool(self.crs) and self.has_origin

    @property
    def parses(self) -> bool:
        """Whether BlenderGIS can parse ``SRID``."""
        return self.has_crs and blendergis_parses(self.srid)

    def describe(self) -> str:
        """``"EPSG:7856, origin E 502000, N 6961000"`` for warnings and notices."""
        parts = [self.crs or "no CRS"]
        if self.has_origin:
            parts.append(f"origin E {_fmt(self.x)}, N {_fmt(self.y)}")
        elif self.latitude is not None and self.longitude is not None:
            parts.append(f"origin {self.latitude:.6f}°, {self.longitude:.6f}°")
        else:
            parts.append("no origin")
        return ", ".join(parts)


def _fmt(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.3f}"


def read(keys: MutableMapping[str, Any] | Any) -> GeoScene:
    """The scene's GeoScene keys (absent or non-numeric numbers read as None)."""
    present = tuple(k for k in GEOSCENE_KEYS if k in keys)
    return GeoScene(
        srid=keys.get(SRID) if SRID in present else None,
        x=number_of(keys.get(CRS_X)),
        y=number_of(keys.get(CRS_Y)),
        latitude=number_of(keys.get(LATITUDE)),
        longitude=number_of(keys.get(LONGITUDE)),
        present=present,
    )


def keys_live(keys: Any, mode: object) -> bool:
    """True when the keys are the project's CRS and E/N: a SOURCE_CRS project with ``SRID``."""
    return _is_source_crs(mode) and SRID in keys


# --- latitude and longitude ----------------------------------------------------------------


@lru_cache(maxsize=64)
def _to_wgs84(source: str) -> Any:
    import pyproj

    return pyproj.Transformer.from_crs(
        pyproj.CRS.from_user_input(source), "EPSG:4326", always_xy=True
    )


def _datum_source(text: str) -> str:
    """The definition a GeoScene position is transformed from: rusty-tiles' resolution."""
    resolution = crs_catalog.resolve(text)
    if resolution is not None and resolution.accepted and resolution.resolved:
        return resolution.resolved
    return crs_catalog.epsg_code(text) or text


def origin_lon_lat(definition: str, easting: float, northing: float) -> tuple[float, float] | None:
    """WGS84 (lon, lat) of a projected origin with the bundled pyproj; None if it can't tell.

    Datum convention: rusty-tiles' own. The point is transformed from the
    definition rusty-tiles resolves the CRS to (``resolve_source_crs``), so
    GDA2020, GDA94 and NZGD2000 take its null ``+towgs84=0,0,0,0,0,0,0`` shift
    to WGS84, exactly as the published tiles are placed (and as the EPSG
    picker's old presets were). Without rusty-tiles (or for a definition it
    refuses), pyproj's default operation for the definition is used; for these
    datums that is the same null shift (EPSG's "to WGS 84" ensemble
    transformations).
    """
    text = (definition or "").strip()
    if not text or not deps.probe_pyproj().available:
        return None
    source = _datum_source(text)
    try:
        lon, lat = _to_wgs84(source).transform(float(easting), float(northing))
    except Exception:
        log.debug("cannot locate %r at %r, %r", text, easting, northing, exc_info=True)
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)) or abs(lat) > 90.0 or abs(lon) > 180.0:
        return None
    return float(lon), float(lat)


@lru_cache(maxsize=64)
def _from_wgs84(target: str) -> Any:
    import pyproj

    return pyproj.Transformer.from_crs(
        "EPSG:4326", pyproj.CRS.from_user_input(target), always_xy=True
    )


def lon_lat_to_origin(definition: str, lon: float, lat: float) -> tuple[float, float] | None:
    """A WGS84 (lon, lat) as (easting, northing) in ``definition``; None if it can't tell.

    The inverse of :func:`origin_lon_lat`, with the same datum convention, so an
    origin set from latitude/longitude gives the same latitude/longitude back
    when they are synced. Doubles throughout, nothing rounded.
    """
    text = (definition or "").strip()
    if not text or not deps.probe_pyproj().available:
        return None
    try:
        lon, lat = float(lon), float(lat)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)) or abs(lat) > 90.0 or abs(lon) > 180.0:
        return None
    try:
        x, y = _from_wgs84(_datum_source(text)).transform(lon, lat)
    except Exception:
        log.debug("cannot project %r, %r into %r", lon, lat, text, exc_info=True)
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return float(x), float(y)


def sync_lat_lon(keys: Any, locate: LonLatFn = origin_lon_lat) -> bool:
    """Rewrite ``latitude``/``longitude`` from ``SRID`` and ``crs x``/``crs y``.

    Called whenever GeoTools sets the CRS or the origin. When the position
    cannot be computed (no pyproj, a CRS it cannot read), the old values are
    deleted rather than left stale: BlenderGIS's ``setOriginPrj`` does the
    same when its reprojection fails. Without a CRS and a complete projected
    origin there is nothing to compute from, and the keys are left alone (as
    BlenderGIS does). Returns True when lat/lon were written.
    """
    scene = read(keys)
    if not scene.has_crs or not scene.has_origin or not scene.crs:
        return False
    found = locate(scene.crs, scene.x, scene.y)  # type: ignore[arg-type]
    if found is None:
        for key in (LATITUDE, LONGITUDE):
            if key in keys:
                del keys[key]
        return False
    keys[LONGITUDE], keys[LATITUDE] = float(found[0]), float(found[1])
    return True


# --- GeoTools' accessors --------------------------------------------------------------------


def get_crs(keys: Any, store: Any, mode: object) -> str:
    """The project CRS: ``SRID`` (as :func:`definition_of` reads it) or GeoTools' own text."""
    if keys_live(keys, mode):
        return definition_of(keys.get(SRID))
    return str(store.get(STORE_CRS, STORE_DEFAULTS[STORE_CRS]))


def get_origin(keys: Any, store: Any, mode: object, attr: str) -> str:
    """Offset E or N as exact text (``repr`` of the double); empty when the key is absent."""
    if keys_live(keys, mode):
        value = number_of(keys.get(ORIGIN_KEYS[attr]))
        return repr(value) if value is not None else ""
    return str(store.get(attr, STORE_DEFAULTS[attr]))


def _parse(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def set_origin(
    keys: Any, store: Any, mode: object, attr: str, text: str, locate: LonLatFn = origin_lon_lat
) -> bool:
    """Set offset E or N from text. Returns False when the text was refused.

    With live keys the value goes to ``crs x``/``crs y`` as a double, then
    lat/lon are synced. Text that is not a finite number cannot be a key and
    is refused (the field keeps its value). Otherwise the text is GeoTools'
    own and is kept as typed (Check reports it if it is not a number).
    """
    if keys_live(keys, mode):
        value = _parse(text)
        if value is None:
            return False
        keys[ORIGIN_KEYS[attr]] = value
        sync_lat_lon(keys, locate)
        return True
    store[attr] = str(text)
    return True


def _drop_store(store: Any, *names: str) -> None:
    for name in names:
        if name in store:
            del store[name]


def set_crs(
    keys: Any, store: Any, mode: object, text: str, locate: LonLatFn = origin_lon_lat
) -> None:
    """Set the project CRS. A picker entry is stored as its plain ``EPSG:nnnn``.

    SOURCE_CRS:

    - a CRS GeoTools may write as ``SRID`` (:func:`srid_for`) goes into the
      keys: E/N move from GeoTools' text into ``crs x``/``crs y`` if the scene
      had no keys yet. The origin's E/N numbers are kept. (BlenderGIS's own CRS
      switch instead reprojects the origin to keep it on the same spot; in
      GeoTools the CRS names what the scene's coordinates are in, so changing
      it re-reads the same numbers.) Lat/lon are synced.
    - any other CRS (WKT, compound, empty) cannot be a GeoScene: the keys are
      removed and the CRS and E/N become GeoTools' text again.

    LOCAL and ANCHOR: the text is GeoTools' own; no key is written.
    """
    value = crs_catalog.choice_value(text or "")
    if not _is_source_crs(mode):
        store[STORE_CRS] = value
        return
    srid = srid_for(value)
    if SRID in keys:
        if srid is not None:
            if keys.get(SRID) != srid:
                keys[SRID] = srid
                sync_lat_lon(keys, locate)
            return
        scene = read(keys)
        store[STORE_CRS] = value
        for attr, number in ((STORE_E, scene.x), (STORE_N, scene.y)):
            if number is not None:
                store[attr] = repr(number)
            else:
                _drop_store(store, attr)
        for key in GEOSCENE_KEYS:
            if key in keys:
                del keys[key]
        return
    store[STORE_CRS] = value
    if srid is not None:
        reconcile(keys, store, mode, locate)


# --- migration and adoption --------------------------------------------------------------


@dataclass(slots=True)
class Reconciled:
    """What :func:`reconcile` did: each entry names a value and where it came from."""

    moved: list[str] = field(default_factory=list)
    """Taken from GeoTools' own settings into the keys."""
    kept: list[str] = field(default_factory=list)
    """Already in the keys, and kept."""
    dropped: list[str] = field(default_factory=list)
    """GeoTools' own values that differed from the keys and were dropped (the keys won)."""

    @property
    def changed(self) -> bool:
        return bool(self.moved or self.dropped)

    def notice(self) -> str:
        """One paragraph for the Project panel."""
        parts = []
        if self.moved:
            parts.append(
                "Taken from GeoTools' project settings into the scene's GeoScene keys "
                "(the BlenderGIS convention): " + "; ".join(self.moved) + "."
            )
        if self.dropped:
            parts.append(
                "The scene's GeoScene keys already held a georeference, and they win: "
                + "; ".join(self.dropped)
                + "."
            )
        elif self.kept and self.moved:
            parts.append("Kept from the keys: " + "; ".join(self.kept) + ".")
        parts.append("Offset A and the height offset stay GeoTools settings.")
        return " ".join(parts)


def reconcile(
    keys: Any, store: Any, mode: object, locate: LonLatFn = origin_lon_lat
) -> Reconciled | None:
    """Make the keys the authority for a SOURCE_CRS project; return what changed.

    Run on file load (a ``.blend`` from an earlier GeoTools keeps its CRS and
    offset in its own properties), when a project becomes SOURCE_CRS, and when
    a CRS GeoScene can hold is chosen. None for LOCAL and ANCHOR (no keys are
    written for them) and while the CRS cannot be a GeoScene (no ``SRID`` and
    GeoTools' CRS is WKT, compound or empty): GeoTools' own text then stays the
    georeference.

    - No ``SRID`` yet: GeoTools' CRS becomes ``SRID`` and its E/N (``0`` when
      never set, as the project used) become ``crs x``/``crs y`` unless those
      keys exist already, in which case they win.
    - A ``SRID`` exists: the keys win. GeoTools' own CRS and E/N fill only the
      origin keys that are absent; values that differ from the keys are
      dropped and named in the notice.

    - The scene has latitude/longitude but no projected origin: only E/N
      GeoTools actually held are written (no zero default), and the
      latitude/longitude are kept as found, not recomputed. :func:`lat_lon_origin`
      then offers them as the origin, or reports how far they lie from E/N.

    GeoTools' own CRS and E/N text is then removed, so no second copy remains.
    Lat/lon are otherwise synced when a key was written.
    """
    if not _is_source_crs(mode):
        return None
    stored_crs = str(store.get(STORE_CRS, "")).strip()
    result = Reconciled()
    wrote = False
    # A scene with only a geographic origin (latitude/longitude, as BlenderGIS or
    # another GeoScene tool may leave it): no zero E/N is invented over it, and
    # the keys found are never recomputed from E/N GeoTools held. The panel and
    # Check then offer Origin from Latitude/Longitude, or say how far they differ.
    geographic_only = _geographic_only(read(keys))
    if SRID not in keys:
        srid = srid_for(stored_crs)
        if srid is None:
            return None
        keys[SRID] = srid
        wrote = True
        result.moved.append(f"CRS {srid} (SRID)")
        fresh = True
    else:
        fresh = False
        current = definition_of(keys.get(SRID))
        if stored_crs and not same_crs(stored_crs, current):
            result.dropped.append(
                f"CRS {current or 'empty'} (SRID) kept over GeoTools' {_short(stored_crs)}"
            )
        elif stored_crs:
            result.kept.append(f"CRS {current} (SRID)")
    for attr, key in ORIGIN_KEYS.items():
        label = "E" if attr == STORE_E else "N"
        held = attr in store
        text = str(store.get(attr, STORE_DEFAULTS[attr]))
        number = _parse(text)
        existing = number_of(keys.get(key))
        if existing is not None:
            if held and number is not None and number != existing:
                result.dropped.append(
                    f"origin {label} {existing!r} ({key}) kept over GeoTools' {number!r}"
                )
            elif held or fresh:
                result.kept.append(f"origin {label} {existing!r} ({key})")
        elif number is not None and (held or (fresh and not geographic_only)):
            keys[key] = number
            wrote = True
            result.moved.append(f"origin {label} {number!r} ({key})")
        elif held and number is None:
            result.dropped.append(f"origin {label}: GeoTools' text {text!r} is not a number")
    _drop_store(store, STORE_CRS, STORE_E, STORE_N)
    if geographic_only:
        scene = read(keys)
        if wrote:
            result.kept.append(
                f"latitude {scene.latitude!r}, longitude {scene.longitude!r} as found"
            )
    elif wrote:
        sync_lat_lon(keys, locate)
    return result


# --- an origin from latitude and longitude --------------------------------------------------

EARTH_RADIUS_M = 6_371_008.8
"""The mean Earth radius (IUGG), for the distance between two geographic positions."""
LAT_LON_TOLERANCE_M = 0.01
"""Latitude/longitude within this of the projected origin agree with it.

A position synced by GeoTools or BlenderGIS round-trips to well under a
millimetre; a different tool's datum convention can differ by more, and is
reported."""


def _lat_lon(scene: GeoScene) -> tuple[float, float] | None:
    """The scene's (latitude, longitude) when both are finite and in range."""
    lat, lon = scene.latitude, scene.longitude
    if lat is None or lon is None or abs(lat) > 90.0 or abs(lon) > 180.0:
        return None
    return lat, lon


def _geographic_only(scene: GeoScene) -> bool:
    """A geographic origin (latitude/longitude) without a complete projected one."""
    return _lat_lon(scene) is not None and not scene.has_origin


def ground_distance(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Metres between two WGS84 positions on the mean sphere (haversine; 0.5% at worst)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def origin_from_lat_lon(
    keys: Any, definition: str, project: ProjectFn = lon_lat_to_origin
) -> tuple[float, float] | None:
    """``crs x``/``crs y`` for the scene's ``latitude``/``longitude`` in ``definition``.

    ``definition`` must be a CRS GeoTools can write as ``SRID``
    (:func:`srid_for`). The result is the projected doubles, unrounded. None
    when it cannot be computed: no such CRS, no finite latitude and longitude,
    or a projection that fails or is not finite (no pyproj, a CRS it cannot read).
    """
    if srid_for(definition) is None:
        return None
    found = _lat_lon(read(keys))
    if found is None:
        return None
    lat, lon = found
    xy = project(definition, lon, lat)
    if xy is None:
        return None
    x, y = float(xy[0]), float(xy[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return x, y


@dataclass(frozen=True, slots=True)
class LatLonOrigin:
    """A scene whose latitude/longitude could give the project's E/N (:func:`lat_lon_origin`)."""

    crs: str
    latitude: float
    longitude: float
    current: tuple[float, float] | None = None
    """The project's E/N; None when it has no projected origin."""
    distance_m: float | None = None
    """How far E/N lie from latitude/longitude, in metres; None when E/N are missing."""

    @property
    def missing(self) -> bool:
        return self.current is None

    @property
    def code(self) -> str:
        return "georef-origin-lat-lon-only" if self.missing else "georef-origin-lat-lon-differ"

    def message(self) -> str:
        where = f"latitude {self.latitude:.7f}°, longitude {self.longitude:.7f}°"
        if self.current is None:
            return (
                f"The scene has a geographic origin ({where}, written by BlenderGIS or another "
                f"GeoScene tool) but no projected origin (crs x, crs y) in {self.crs}. Use "
                "Origin from Latitude/Longitude to set offset E/N from it."
            )
        e, n = self.current
        return (
            f"The scene's latitude/longitude ({where}) lie {format_metres(self.distance_m)} "
            f"from its projected origin (E {_fmt(e)}, N {_fmt(n)} in {self.crs}); another "
            "GeoScene tool may have moved one of them. GeoTools places the scene by E/N and "
            "changes neither. Use Origin from Latitude/Longitude to take E/N from "
            "latitude/longitude instead."
        )

    def issue(self) -> Issue:
        return Issue(Severity.WARNING, self.code, self.message())


def format_metres(value: float | None) -> str:
    """``"0.042 m"``, ``"12.35 m"``, ``"3.21 km"``."""
    if value is None:
        return "an unknown distance"
    if value >= 1000.0:
        return f"{value / 1000.0:,.2f} km"
    if value >= 1.0:
        return f"{value:,.2f} m"
    return f"{value:.3f} m"


def _current_origin(keys: Any, store: Any, mode: object) -> tuple[float, float] | None:
    """The project's E/N as numbers; None when it has no projected origin."""
    if keys_live(keys, mode):
        scene = read(keys)
        return (scene.x, scene.y) if scene.has_origin else None  # type: ignore[return-value]
    if STORE_E not in store and STORE_N not in store:
        return None
    e = _parse(str(store.get(STORE_E, STORE_DEFAULTS[STORE_E])))
    n = _parse(str(store.get(STORE_N, STORE_DEFAULTS[STORE_N])))
    return (e, n) if e is not None and n is not None else None


def lat_lon_origin(
    keys: Any, store: Any, mode: object, locate: LonLatFn = origin_lon_lat
) -> LatLonOrigin | None:
    """When to offer Origin from Latitude/Longitude, and what to say.

    Only for a SOURCE_CRS project with a CRS GeoTools can write as ``SRID`` and
    a scene with finite ``latitude``/``longitude``:

    - no projected origin (no ``crs x``/``crs y``, no GeoTools E/N): offered;
    - E/N that lie more than :data:`LAT_LON_TOLERANCE_M` from latitude/longitude:
      the disagreement, in metres, with the offer (nothing is changed);
    - otherwise (they agree, or ``locate`` cannot tell): None.

    LOCAL and ANCHOR projects give None: GeoTools leaves their keys alone.
    """
    if not _is_source_crs(mode):
        return None
    crs = get_crs(keys, store, mode).strip()
    if not crs or srid_for(crs) is None:
        return None
    found = _lat_lon(read(keys))
    if found is None:
        return None
    lat, lon = found
    current = _current_origin(keys, store, mode)
    if current is None:
        return LatLonOrigin(crs, lat, lon)
    located = locate(crs, current[0], current[1])
    if located is None:
        return None
    distance = ground_distance(lon, lat, float(located[0]), float(located[1]))
    if distance <= LAT_LON_TOLERANCE_M:
        return None
    return LatLonOrigin(crs, lat, lon, current, distance)


def set_origin_from_lat_lon(
    keys: Any,
    store: Any,
    mode: object,
    project: ProjectFn = lon_lat_to_origin,
    locate: LonLatFn = origin_lon_lat,
) -> tuple[float, float] | None:
    """Set the project's E/N from the scene's latitude/longitude in the project CRS.

    Written as :func:`set_origin` writes a typed E/N (so the keys stay the
    georeference), then latitude/longitude are re-synced from the new origin.
    Returns the E/N set, or None when nothing changed: not a SOURCE_CRS project,
    no CRS GeoTools can write as ``SRID``, no latitude/longitude, or no
    projection (:func:`origin_from_lat_lon`).
    """
    if not _is_source_crs(mode):
        return None
    crs = get_crs(keys, store, mode).strip()
    found = origin_from_lat_lon(keys, crs, project)
    if found is None:
        return None
    if not keys_live(keys, mode):
        reconcile(keys, store, mode, locate)  # the CRS becomes SRID, as choosing it does
        if not keys_live(keys, mode):
            return None
    x, y = found
    set_origin(keys, store, mode, STORE_E, repr(x), locate)
    set_origin(keys, store, mode, STORE_N, repr(y), locate)
    sync_lat_lon(keys, locate)
    return x, y


def _short(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= 48 else flat[:45] + "..."


# --- warnings ------------------------------------------------------------------------------


def interop_issues(
    keys: Any,
    mode: object,
    offset_a: float | None,
    store: Any = None,
    locate: LonLatFn = origin_lon_lat,
) -> list[Issue]:
    """Warnings about how GeoScene tools (BlenderGIS) will see this scene.

    - LOCAL or ANCHOR with GeoScene keys: GeoTools ignores the scene's
      georeference, which other tools still use.
    - SOURCE_CRS with latitude/longitude but no projected origin, or with
      latitude/longitude that disagree with E/N (:func:`lat_lon_origin`).
    - SOURCE_CRS with keys and A not 0: GeoScene's Z is absolute height, so
      other tools see every height shifted by A.
    """
    scene = read(keys)
    if not scene.present:
        return []
    if not _is_source_crs(mode):
        kind = "Local" if str(getattr(mode, "value", mode)).lower() == "local" else "Anchor"
        fix = (
            " Use the scene's BlenderGIS georeference (Project panel) to make this a CRS "
            "project with it."
            if scene.has_crs and scene.crs
            else ""
        )
        return [
            Issue(
                Severity.WARNING,
                "georef-keys-ignored",
                f"The scene carries a BlenderGIS (GeoScene) georeference ({scene.describe()}), "
                f"but this is {'a Local' if kind == 'Local' else 'an Anchor'} project, which "
                f"ignores it. BlenderGIS and other GeoScene tools still use it.{fix}",
            )
        ]
    found: list[Issue] = []
    offer = lat_lon_origin(keys, {} if store is None else store, mode, locate)
    if offer is not None:
        found.append(offer.issue())
    if scene.has_crs and offset_a is not None and offset_a != 0.0:
        found.append(
            Issue(
                Severity.WARNING,
                "georef-height-shift",
                f"Offset A is {offset_a:g} m. BlenderGIS and other GeoScene tools read Blender Z "
                f"as absolute height, so they will see every height {abs(offset_a):g} m "
                f"{'lower' if offset_a > 0 else 'higher'} than GeoTools publishes it. Use A = 0 "
                "for scenes shared with them.",
            )
        )
    return found
