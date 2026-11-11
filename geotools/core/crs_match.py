"""Which CRS geo data is in, and whether it is the project's.

GeoTools never reprojects geometry itself (see the roadmap's CRS decision).
Data in the project CRS is placed exactly; data in another CRS is refused with
a message naming both. When the data's CRS cannot be determined, the user may
confirm that it is in the project CRS.

:func:`reproject` is the single hook for rusty-tiles' planned
``transform_points``. It raises :class:`ReprojectionUnavailable` until that
function ships.

The design is in ``docs/design/m8-geo-formats.md`` ("CRS matching").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import crs_presets
from .placement import Placement, PlacementMode

# Where a DataCrs came from.
SOURCE_GEOJSON_MEMBER = "geojson-crs-member"
SOURCE_RFC7946 = "rfc7946-default"
SOURCE_GPKG = "gpkg-srs"
SOURCE_GEOTIFF = "geotiff-geokeys"
SOURCE_USER = "user"
SOURCE_PROJECT = "project"

WGS84 = "EPSG:4326"


@dataclass(frozen=True, slots=True)
class DataCrs:
    epsg: str | None  # "EPSG:7856"
    definition: str | None  # WKT or PROJ text when there is no code
    source: str  # "geojson-crs-member", "rfc7946-default", "gpkg-srs", "geotiff-geokeys", "user"

    @property
    def known(self) -> bool:
        """True if the CRS is named by a code or a definition."""
        return bool(self.epsg or (self.definition and self.definition.strip()))

    def label(self) -> str:
        """The shortest name for messages: the EPSG code, else the start of the definition."""
        if self.epsg:
            return self.epsg
        return self.describe()

    def describe(self) -> str:
        """A name for messages: the EPSG code (with the preset's label when one stands in
        for it), else the start of the definition."""
        if self.epsg:
            preset = preset_for_epsg(self.epsg)
            return f"{self.epsg} ({preset.label})" if preset else self.epsg
        if self.definition and self.definition.strip():
            text = " ".join(self.definition.split())
            return text if len(text) <= 60 else text[:57] + "..."
        return "an unknown CRS"


class ReprojectionUnavailable(RuntimeError):
    """Reprojection is not available yet (rusty-tiles has no transform_points)."""


def reproject(points: object, data_crs: DataCrs, project_crs: DataCrs) -> object:
    """Reproject ``points`` (n, 3) from ``data_crs`` to ``project_crs``.

    Not available: rusty-tiles' ``transform_points`` has not shipped. When it
    does, this calls it and returns the points with its accuracy report.
    """
    raise ReprojectionUnavailable(
        f"GeoTools cannot reproject from {data_crs.describe()} to {project_crs.describe()} "
        "yet. Reproject the data to the project CRS in QGIS first."
    )


# --- the project's CRS ----------------------------------------------------------


def project_crs(placement: Placement) -> DataCrs | None:
    """The project's CRS, or None for LOCAL and ANCHOR projects (local metres)."""
    if PlacementMode(placement.mode) is not PlacementMode.SOURCE_CRS:
        return None
    definition = placement.source_crs.strip()
    if not definition:
        return None
    return DataCrs(
        epsg=normalise_epsg(definition) or crs_presets.epsg_code(definition),
        definition=definition,
        source=SOURCE_PROJECT,
    )


def matches(project: DataCrs | None, data: DataCrs) -> bool | None:
    """Whether ``data`` is in the ``project`` CRS. None means it can't be told.

    Codes are compared when both sides have one; otherwise definitions are
    compared as normalised text. A LOCAL or ANCHOR project (``project`` None)
    or data of unknown CRS gives None.
    """
    if project is None or not data.known or not project.known:
        return None
    if project.epsg and data.epsg:
        return _same_code(project.epsg, data.epsg)
    if (
        project.definition
        and data.definition
        and _normal_text(project.definition) == _normal_text(data.definition)
    ):
        return True
    return None


def mismatch_message(project: DataCrs, data: DataCrs, what: str) -> str:
    """The refusal for data in another CRS than the project's."""
    return (
        f"{what} is in {data.describe()}, but the project CRS is {project.describe()}. "
        "Reproject it to the project CRS in QGIS (Export > Save Features As, with the "
        "project CRS) and import that file."
    )


def unknown_message(data: DataCrs, what: str) -> str:
    return (
        f"Cannot tell which CRS {what} is in ({data.describe()}). If it is in the project "
        "CRS, tick 'Data Is in Project CRS' and import again."
    )


# --- text form (GeoToolsLink.crs) -----------------------------------------------


def to_text(crs: DataCrs) -> str:
    """The JSON text stored in a link's ``crs`` field."""
    return json.dumps(
        {"epsg": crs.epsg, "definition": crs.definition, "source": crs.source}, sort_keys=True
    )


def from_text(text: str) -> DataCrs | None:
    """Read :func:`to_text` output back; None for empty or invalid text."""
    if not text or not text.strip():
        return None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    epsg = raw.get("epsg")
    definition = raw.get("definition")
    return DataCrs(
        epsg=epsg if isinstance(epsg, str) and epsg else None,
        definition=definition if isinstance(definition, str) and definition else None,
        source=str(raw.get("source") or SOURCE_USER),
    )


# --- parsing names ----------------------------------------------------------------

_EPSG_PLAIN = re.compile(r"^\s*EPSG\s*:\s*:?\s*(\d+)\s*$", re.IGNORECASE)
_URN = re.compile(r"^\s*urn:ogc:def:crs:([A-Za-z0-9.]+):[^:]*:(\w+)\s*$", re.IGNORECASE)
_URL = re.compile(
    r"^\s*https?://www\.opengis\.net/def/crs/([A-Za-z0-9.]+)/[^/]*/(\w+)\s*$", re.IGNORECASE
)
_CRS84 = {("OGC", "CRS84"), ("OGC", "CRS84H"), ("OGC", "84")}


def normalise_epsg(name: str | None) -> str | None:
    """``"EPSG:nnnn"`` for the common ways of naming an EPSG CRS, else None.

    Accepts ``EPSG:7856``, ``urn:ogc:def:crs:EPSG::7856``,
    ``http://www.opengis.net/def/crs/EPSG/0/7856`` and OGC CRS84 (WGS84
    longitude, latitude, as EPSG:4326 with GeoJSON's axis order).
    """
    if not name:
        return None
    match = _EPSG_PLAIN.match(name)
    if match:
        return f"EPSG:{int(match.group(1))}"
    match = _URN.match(name) or _URL.match(name)
    if match:
        authority, code = match.group(1).upper(), match.group(2).upper()
        if authority == "EPSG" and code.isdigit():
            return f"EPSG:{int(code)}"
        if (authority, code) in _CRS84 or (authority == "OGC" and code.startswith("CRS84")):
            return WGS84
    return None


_WKT_ID = re.compile(r'(?:AUTHORITY|ID)\s*\[\s*"([^"]+)"\s*,\s*"?(\d+)"?\s*\]', re.IGNORECASE)


def epsg_from_wkt(wkt: str | None) -> str | None:
    """The top-level EPSG code of a WKT1 or WKT2 definition, if it states one.

    The root object's ``AUTHORITY``/``ID`` is the last one in the text, after
    those of its datum, units and axes.
    """
    if not wkt:
        return None
    for match in reversed(list(_WKT_ID.finditer(wkt))):
        if _bracket_depth(wkt, match.start()) == 1:  # a child of the root object
            authority, code = match.groups()
            return f"EPSG:{int(code)}" if authority.upper() == "EPSG" else None
    return None


def _bracket_depth(text: str, end: int) -> int:
    """How many ``[`` or ``(`` are open before ``end``, outside quoted strings."""
    depth, quoted = 0, False
    for ch in text[:end]:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in "[(":
            depth += 1
        elif not quoted and ch in "])":
            depth -= 1
    return depth


def from_gpkg_srs(
    organization: str | None, code: int | None, definition: str | None, srs_id: int
) -> DataCrs:
    """The CRS a GeoPackage ``gpkg_spatial_ref_sys`` row describes.

    srs_id 0 and -1 are the spec's undefined geographic and Cartesian systems.
    """
    definition = (definition or "").strip()
    if definition.lower() == "undefined":
        definition = ""
    if srs_id in (0, -1) and not definition:
        return DataCrs(None, None, SOURCE_GPKG)
    epsg = None
    if organization and organization.strip().upper() == "EPSG" and code and code > 0:
        epsg = f"EPSG:{int(code)}"
    epsg = epsg or epsg_from_wkt(definition)
    return DataCrs(epsg, definition or None, SOURCE_GPKG)


# --- helpers ----------------------------------------------------------------------


def _same_code(a: str, b: str) -> bool:
    return (normalise_epsg(a) or a.strip().upper()) == (normalise_epsg(b) or b.strip().upper())


def _normal_text(text: str) -> str:
    return " ".join(text.split()).lower()


def preset_for_epsg(epsg: str | None) -> crs_presets.CrsPreset | None:
    """The GeoTools CRS preset that stands in for ``epsg``, if there is one."""
    code = normalise_epsg(epsg) if epsg else None
    return next((p for p in crs_presets.PRESETS if p.epsg == code), None) if code else None
