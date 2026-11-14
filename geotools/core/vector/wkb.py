"""GeoPackage binary geometry: the GP header plus (ISO) WKB.

Ported from the 0.2 ``gpkg/wkb.py`` and vectorised: each ring's coordinates
are read with one ``numpy.frombuffer`` and written with one ``tobytes``.

GeoPackage header (GeoPackage 1.4, 2.1.3):

- bytes 0-1: ``GP``; byte 2: version; byte 3: flags
  (bit 0: header byte order, 1 little-endian; bits 1-3: envelope type
  0 none, 1 XY, 2 XYZ, 3 XYM, 4 XYZM; bit 4: empty geometry;
  bit 5: extended GeoPackage geometry);
- bytes 4-7: srs_id (int32);
- the envelope, 0, 32, 48, 48 or 64 bytes;
- then the WKB.

WKB types: 1-7 (Point ... GeometryCollection), plus 1000 for Z, 2000 for M and
3000 for ZM (ISO), or the EWKB high bits 0x80000000 (Z), 0x40000000 (M) and
0x20000000 (an SRID follows the type). Polyhedral surfaces (15), TINs (16) and
triangles (17) are read as polygons. M values are ignored. Curves are refused.
"""

from __future__ import annotations

import struct

import numpy as np

from .model import Geometries, GeometryBuilder, Kind

_ENVELOPE_BYTES = (0, 32, 48, 48, 64)

POINT, LINESTRING, POLYGON, MULTIPOINT, MULTILINESTRING, MULTIPOLYGON, COLLECTION = range(1, 8)
POLYHEDRAL, TIN, TRIANGLE = 15, 16, 17

TYPE_NAMES = {
    POINT: "POINT",
    LINESTRING: "LINESTRING",
    POLYGON: "POLYGON",
    MULTIPOINT: "MULTIPOINT",
    MULTILINESTRING: "MULTILINESTRING",
    MULTIPOLYGON: "MULTIPOLYGON",
    COLLECTION: "GEOMETRYCOLLECTION",
}


class WkbError(ValueError):
    """A geometry blob is corrupt or of a type GeoTools does not read."""


def gpkg_payload(blob: bytes) -> tuple[int, bool, int]:
    """``(wkb offset, empty, srs_id)`` of a GeoPackage geometry blob.

    A blob without the ``GP`` magic is taken as plain WKB (offset 0), which
    some writers produce.
    """
    if len(blob) < 2 or blob[0:2] != b"GP":
        return 0, False, 0
    if len(blob) < 8:
        raise WkbError("GeoPackage geometry is shorter than its 8-byte header")
    flags = blob[3]
    endian = "<" if flags & 0x01 else ">"
    envelope = (flags >> 1) & 0x07
    if envelope >= len(_ENVELOPE_BYTES):
        raise WkbError(f"GeoPackage geometry has an invalid envelope type {envelope}")
    if flags & 0x20:
        raise WkbError("extended GeoPackage geometries are not supported")
    srs_id = struct.unpack_from(endian + "i", blob, 4)[0]
    return 8 + _ENVELOPE_BYTES[envelope], bool(flags & 0x10), srs_id


def read_gpkg(builder: GeometryBuilder, row: int, blob: bytes) -> None:
    """Parse one GeoPackage geometry into ``builder`` as feature ``row``."""
    if not blob:
        return
    blob = bytes(blob)
    offset, empty, _srs = gpkg_payload(blob)
    if empty or offset >= len(blob):
        return
    _Reader(blob, builder, row).geometry(offset)


class _Reader:
    __slots__ = ("builder", "data", "row")

    def __init__(self, data: bytes, builder: GeometryBuilder, row: int) -> None:
        self.data = data
        self.builder = builder
        self.row = row

    def _header(self, offset: int) -> tuple[str, int, bool, int, int]:
        """``(endian, base type, has Z, dims, offset after the header)``."""
        data = self.data
        if offset + 5 > len(data):
            raise WkbError("WKB ends before a geometry header")
        endian = "<" if data[offset] else ">"
        code = struct.unpack_from(endian + "I", data, offset + 1)[0]
        offset += 5
        has_z, has_m = bool(code & 0x80000000), bool(code & 0x40000000)
        if code & 0x20000000:
            offset += 4  # EWKB SRID; the GeoPackage header has the srs_id
        thousands, base = divmod(code & 0x0FFFFFFF, 1000)
        if thousands not in (0, 1, 2, 3):
            raise WkbError(f"unknown WKB geometry type {code}")
        has_z = has_z or thousands in (1, 3)
        has_m = has_m or thousands in (2, 3)
        return endian, base, has_z, 2 + has_z + has_m, offset

    def _count(self, endian: str, offset: int) -> tuple[int, int]:
        if offset + 4 > len(self.data):
            raise WkbError("WKB ends before a count")
        return struct.unpack_from(endian + "I", self.data, offset)[0], offset + 4

    def _coords(
        self, endian: str, has_z: bool, dims: int, n: int, offset: int
    ) -> tuple[np.ndarray, int]:
        """``n`` positions as ``(n, 3)`` binary64 (NaN Z when there is none); M is dropped."""
        size = 8 * dims * n
        if offset + size > len(self.data):
            raise WkbError("WKB ends inside a coordinate list")
        raw = np.frombuffer(self.data, dtype=endian + "f8", count=dims * n, offset=offset)
        raw = raw.reshape(n, dims)
        out = np.full((n, 3), np.nan)
        out[:, :2] = raw[:, :2]
        if has_z:  # Z is always the third ordinate when present
            out[:, 2] = raw[:, 2]
        return out, offset + size

    def geometry(self, offset: int) -> int:
        endian, base, has_z, dims, offset = self._header(offset)
        if base == POINT:
            xyz, offset = self._coords(endian, has_z, dims, 1, offset)
            if not np.isnan(xyz[0, :2]).all():  # POINT EMPTY is NaN, NaN
                self.builder.add_points(self.row, xyz)
            return offset
        if base == LINESTRING:
            n, offset = self._count(endian, offset)
            xyz, offset = self._coords(endian, has_z, dims, n, offset)
            if n:
                self.builder.add_line(self.row, xyz)
            return offset
        if base in (POLYGON, TRIANGLE):
            n_rings, offset = self._count(endian, offset)
            rings = []
            for _ in range(n_rings):
                n, offset = self._count(endian, offset)
                xyz, offset = self._coords(endian, has_z, dims, n, offset)
                rings.append(xyz)
            if rings and len(rings[0]):
                self.builder.add_polygon(self.row, rings)
            return offset
        if base in (MULTIPOINT, MULTILINESTRING, MULTIPOLYGON, COLLECTION, POLYHEDRAL, TIN):
            n, offset = self._count(endian, offset)
            for _ in range(n):
                offset = self.geometry(offset)
            return offset
        raise WkbError(f"WKB geometry type {base} (curves and surfaces) is not supported")


# --- writing ---------------------------------------------------------------------


def encode_feature(
    geoms: Geometries, feature: int, multi: bool, has_z: bool, has_m: bool = False
) -> bytes:
    """ISO WKB (little-endian) of one feature of ``geoms``.

    ``multi`` writes a Multi* even for one part. ``has_z`` writes XYZ, with 0
    for a missing Z; ``has_m`` adds ``geoms.measures`` as M (XYM or XYZM, NaN
    for a missing M).
    """
    p0, p1 = int(geoms.feature_offsets[feature]), int(geoms.feature_offsets[feature + 1])
    base = {Kind.POINTS: POINT, Kind.LINES: LINESTRING, Kind.POLYGONS: POLYGON}[geoms.kind]
    dims = (1000 if has_z else 0) + (2000 if has_m else 0)
    parts = [_encode_part(geoms, p, base, dims, has_z, has_m) for p in range(p0, p1)]
    if not multi and len(parts) == 1:
        return parts[0]
    return struct.pack("<BII", 1, base + 3 + dims, len(parts)) + b"".join(parts)


def _ring_bytes(geoms: Geometries, ring: int, has_z: bool, has_m: bool, with_count: bool) -> bytes:
    r0, r1 = int(geoms.ring_offsets[ring]), int(geoms.ring_offsets[ring + 1])
    coords = geoms.coords[r0:r1, : 3 if has_z else 2]
    if has_z:
        coords = np.where(np.isnan(coords), 0.0, coords)
    if has_m:
        measures = geoms.measures if geoms.measures is not None else np.full(r1, np.nan)
        coords = np.column_stack([coords, measures[r0:r1]])
    body = np.ascontiguousarray(coords, dtype="<f8").tobytes()
    return (struct.pack("<I", r1 - r0) + body) if with_count else body


def _encode_part(
    geoms: Geometries, part: int, base: int, dims: int, has_z: bool, has_m: bool
) -> bytes:
    q0, q1 = int(geoms.part_offsets[part]), int(geoms.part_offsets[part + 1])
    header = struct.pack("<BI", 1, base + dims)
    if base == POINT:
        return header + _ring_bytes(geoms, q0, has_z, has_m, with_count=False)
    if base == LINESTRING:
        return header + _ring_bytes(geoms, q0, has_z, has_m, with_count=True)
    rings = [_ring_bytes(geoms, r, has_z, has_m, with_count=True) for r in range(q0, q1)]
    return header + struct.pack("<I", q1 - q0) + b"".join(rings)


def gpkg_blob(wkb: bytes, srs_id: int, envelope: np.ndarray | None) -> bytes:
    """A GeoPackage geometry: little-endian header, then ``envelope`` and ``wkb``.

    ``envelope`` is ``[minx, maxx, miny, maxy]`` or with ``minz, maxz`` too
    (XYZ envelope); None for an empty geometry.
    """
    if envelope is None:
        return struct.pack("<2sBBi", b"GP", 0, 0x01 | 0x10, srs_id) + wkb
    kind = 2 if len(envelope) == 6 else 1
    flags = 0x01 | (kind << 1)
    return (
        struct.pack("<2sBBi", b"GP", 0, flags, srs_id)
        + np.asarray(envelope, dtype="<f8").tobytes()
        + wkb
    )
