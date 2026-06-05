"""gpkg/wkb.py — Pure-Python WKB geometry parser and encoder for GeoPackage.

GeoPackage stores geometries as a GPKG binary header followed by standard WKB:

  bytes 0-1:  magic 'GP' (0x47 0x50)
  byte  2:    version (0x00)
  byte  3:    flags
                bit 0:    byte-order for header (0=big-endian, 1=little-endian)
                bits 1-3: envelope indicator
                            0 = no envelope
                            1 = X/Y       (4 doubles, 32 bytes)
                            2 = X/Y/Z     (6 doubles, 48 bytes)
                            3 = X/Y/M     (6 doubles, 48 bytes)
                            4 = X/Y/Z/M   (8 doubles, 64 bytes)
                bit 4:    empty-geometry flag
                bits 5-7: reserved (0)
  bytes 4-7:  srs_id (int32, header byte-order)
  then:       optional envelope (see above)
  then:       standard ISO WKB payload (with its own byte-order byte)

Geometry type codes (ISO WKB):
  1  = Point          1001 = PointZ
  2  = LineString     1002 = LineStringZ
  3  = Polygon        1003 = PolygonZ
  4  = MultiPoint     1004 = MultiPointZ
  5  = MultiLineString  1005 = MultiLineStringZ
  6  = MultiPolygon   1006 = MultiPolygonZ
  7  = GeometryCollection  1007 = GeometryCollectionZ

Also handles EWKB (PostGIS) extended flags:
  bit 31 (0x80000000): has Z
  bit 30 (0x40000000): has M
  bit 29 (0x20000000): has embedded SRID (4 extra bytes after type)

Input/output always use GeoJSON-style dicts:
  {"type": "Point",   "coordinates": [x, y]}
  {"type": "Point",   "coordinates": [x, y, z]}
  {"type": "Polygon", "coordinates": [[[x,y], ...]]}
  {"type": "GeometryCollection", "geometries": [...]}
"""

import struct

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(data: bytes) -> dict | None:
    """Parse a GPKG binary geometry blob.

    Returns a GeoJSON-style dict, or ``None`` for empty/null geometries.
    Raises ``ValueError`` on corrupt or unrecognised input.
    Falls back to plain WKB if the GPKG magic bytes are absent.
    """
    if not data:
        return None
    if len(data) >= 2 and data[0:2] == b'GP':
        return _parse_gpkg(data)
    # Fallback: plain WKB (non-standard but seen in some GPKG writers)
    geom, _ = _parse_wkb(data, 0)
    return geom


def encode(geom: dict | None, srid: int = 0) -> bytes:
    """Encode a GeoJSON-style geometry dict to GPKG binary format.

    Always writes little-endian with an XY envelope for non-empty geometries.
    Pass ``None`` to encode an empty geometry (only the 8-byte header).
    ``srid`` is written into the GPKG header; it does NOT affect the WKB payload.
    """
    if geom is None:
        flags = 0x01 | (0x01 << 4)          # little-endian | empty
        return struct.pack('<2sBBi', b'GP', 0, flags, srid)
    min_x, max_x, min_y, max_y = _compute_envelope_xy(geom)
    flags = 0x01 | (0x01 << 1)              # little-endian, XY envelope, non-empty
    header = struct.pack('<2sBBi', b'GP', 0, flags, srid)
    envelope = struct.pack('<dddd', min_x, max_x, min_y, max_y)
    return header + envelope + _encode_wkb(geom)


# ---------------------------------------------------------------------------
# Parser internals
# ---------------------------------------------------------------------------

# Bytes consumed by each envelope indicator (0–4); values 5-7 are reserved.
_ENVELOPE_BYTES = [0, 32, 48, 48, 64]

# WKB base-type code → GeoJSON type name
_WKB_TYPE_MAP = {
    1: 'Point',
    2: 'LineString',
    3: 'Polygon',
    4: 'MultiPoint',
    5: 'MultiLineString',
    6: 'MultiPolygon',
    7: 'GeometryCollection',
}


def _parse_gpkg(data: bytes) -> dict | None:
    if len(data) < 8:
        raise ValueError("GPKG geometry blob too short (< 8 bytes)")
    if data[0:2] != b'GP':
        raise ValueError(f"Bad GPKG magic: {data[0:2]!r}")

    flags = data[3]
    byte_order = flags & 0x01               # 0 = big-endian, 1 = little-endian
    envelope_ind = (flags >> 1) & 0x07
    is_empty = (flags >> 4) & 0x01

    if is_empty:
        return None

    endian = '<' if byte_order else '>'
    # srs_id is informational only; the operator already knows the layer SRID
    # srid = struct.unpack_from(endian + 'i', data, 4)[0]

    env_bytes = _ENVELOPE_BYTES[envelope_ind] if envelope_ind < len(_ENVELOPE_BYTES) else 0
    wkb_offset = 8 + env_bytes

    geom, _ = _parse_wkb(data, wkb_offset)
    return geom


def _parse_wkb(data: bytes, offset: int) -> tuple:
    """Parse one WKB geometry from *data* starting at *offset*.

    Returns ``(geom_dict, new_offset)``.
    """
    if offset >= len(data):
        return None, offset

    # --- byte order byte ---
    byte_order = data[offset]
    endian = '<' if byte_order else '>'
    offset += 1

    # --- geometry type (uint32) ---
    wkb_type = struct.unpack_from(endian + 'I', data, offset)[0]
    offset += 4

    # --- decode type code and Z/M flags ---
    # Strip EWKB high bits first, then check ISO ranges
    ewkb_has_z   = bool(wkb_type & 0x80000000)
    ewkb_has_m   = bool(wkb_type & 0x40000000)
    ewkb_has_srid = bool(wkb_type & 0x20000000)
    clean = wkb_type & 0x1FFFFFFF          # strip three EWKB flag bits

    if 3000 <= clean < 4000:               # ISO WKB ZM
        base_type = clean - 3000
        has_z, has_m = True, True
    elif 2000 <= clean < 3000:             # ISO WKB M
        base_type = clean - 2000
        has_z, has_m = False, True
    elif 1000 <= clean < 2000:             # ISO WKB Z
        base_type = clean - 1000
        has_z, has_m = True, False
    else:                                   # plain WKB (or EWKB with flags above)
        base_type = clean
        has_z = ewkb_has_z
        has_m = ewkb_has_m

    if ewkb_has_srid:
        offset += 4                        # skip embedded SRID (already in GPKG header)

    geom_type_name = _WKB_TYPE_MAP.get(base_type)
    if geom_type_name is None:
        raise ValueError(f"Unknown WKB geometry type code: {wkb_type} (base={base_type})")

    # Number of ordinates per coordinate position
    n_coords = 2 + (1 if has_z else 0) + (1 if has_m else 0)
    coord_fmt = endian + 'd' * n_coords
    coord_size = 8 * n_coords

    # --- inner reader helpers (share offset via nonlocal) ---

    def read_uint32() -> int:
        nonlocal offset
        val = struct.unpack_from(endian + 'I', data, offset)[0]
        offset += 4
        return val

    def read_point() -> list:
        nonlocal offset
        vals = struct.unpack_from(coord_fmt, data, offset)
        offset += coord_size
        if has_z:
            return [vals[0], vals[1], vals[2]]
        return [vals[0], vals[1]]

    def read_ring() -> list:
        n = read_uint32()
        return [read_point() for _ in range(n)]

    # --- parse geometry body ---

    if geom_type_name == 'Point':
        coords = read_point()
        geom = {'type': 'Point', 'coordinates': coords}

    elif geom_type_name == 'LineString':
        geom = {'type': 'LineString', 'coordinates': read_ring()}

    elif geom_type_name == 'Polygon':
        n_rings = read_uint32()
        geom = {'type': 'Polygon', 'coordinates': [read_ring() for _ in range(n_rings)]}

    elif geom_type_name == 'MultiPoint':
        n = read_uint32()
        points = []
        for _ in range(n):
            child, offset = _parse_wkb(data, offset)
            points.append(child['coordinates'])
        geom = {'type': 'MultiPoint', 'coordinates': points}

    elif geom_type_name == 'MultiLineString':
        n = read_uint32()
        lines = []
        for _ in range(n):
            child, offset = _parse_wkb(data, offset)
            lines.append(child['coordinates'])
        geom = {'type': 'MultiLineString', 'coordinates': lines}

    elif geom_type_name == 'MultiPolygon':
        n = read_uint32()
        polys = []
        for _ in range(n):
            child, offset = _parse_wkb(data, offset)
            polys.append(child['coordinates'])
        geom = {'type': 'MultiPolygon', 'coordinates': polys}

    elif geom_type_name == 'GeometryCollection':
        n = read_uint32()
        geoms = []
        for _ in range(n):
            child, offset = _parse_wkb(data, offset)
            geoms.append(child)
        geom = {'type': 'GeometryCollection', 'geometries': geoms}

    else:
        raise ValueError(f"Unhandled geometry type: {geom_type_name!r}")

    return geom, offset


# ---------------------------------------------------------------------------
# Encoder internals
# ---------------------------------------------------------------------------

_GEOJSON_TO_WKB = {
    'Point': 1,
    'LineString': 2,
    'Polygon': 3,
    'MultiPoint': 4,
    'MultiLineString': 5,
    'MultiPolygon': 6,
    'GeometryCollection': 7,
}


def _encode_wkb(geom: dict) -> bytes:
    """Encode a GeoJSON-style geometry dict to ISO WKB (little-endian)."""
    geom_type = geom.get('type', '')
    base_code = _GEOJSON_TO_WKB.get(geom_type)
    if base_code is None:
        raise ValueError(f"Cannot encode geometry type: {geom_type!r}")

    has_z = _detect_z(geom)
    wkb_type = base_code + (1000 if has_z else 0)
    # byte-order byte (1 = little-endian) + uint32 type
    header = struct.pack('<BI', 1, wkb_type)

    if geom_type == 'Point':
        return header + _enc_coords(geom['coordinates'], has_z)

    elif geom_type == 'LineString':
        return header + _enc_ring(geom['coordinates'], has_z)

    elif geom_type == 'Polygon':
        rings = geom['coordinates']
        body = struct.pack('<I', len(rings))
        for ring in rings:
            body += _enc_ring(ring, has_z)
        return header + body

    elif geom_type == 'MultiPoint':
        pts = geom['coordinates']
        body = struct.pack('<I', len(pts))
        for pt in pts:
            body += _encode_wkb({'type': 'Point', 'coordinates': pt})
        return header + body

    elif geom_type == 'MultiLineString':
        lines = geom['coordinates']
        body = struct.pack('<I', len(lines))
        for line in lines:
            body += _encode_wkb({'type': 'LineString', 'coordinates': line})
        return header + body

    elif geom_type == 'MultiPolygon':
        polys = geom['coordinates']
        body = struct.pack('<I', len(polys))
        for poly in polys:
            body += _encode_wkb({'type': 'Polygon', 'coordinates': poly})
        return header + body

    elif geom_type == 'GeometryCollection':
        geoms = geom.get('geometries', [])
        body = struct.pack('<I', len(geoms))
        for g in geoms:
            body += _encode_wkb(g)
        return header + body

    raise ValueError(f"Unhandled geometry type: {geom_type!r}")


def _enc_coords(coords: list, has_z: bool) -> bytes:
    if has_z and len(coords) >= 3:
        return struct.pack('<ddd', coords[0], coords[1], coords[2])
    return struct.pack('<dd', coords[0], coords[1])


def _enc_ring(ring: list, has_z: bool) -> bytes:
    body = struct.pack('<I', len(ring))
    for pt in ring:
        body += _enc_coords(pt, has_z)
    return body


def _detect_z(geom: dict) -> bool:
    """Return True if any coordinate in the geometry has a Z ordinate."""
    t = geom.get('type')
    c = geom.get('coordinates')
    if t == 'Point':
        return c is not None and len(c) > 2
    elif t in ('LineString', 'MultiPoint'):
        return bool(c) and len(c[0]) > 2
    elif t in ('Polygon', 'MultiLineString'):
        return bool(c) and bool(c[0]) and len(c[0][0]) > 2
    elif t == 'MultiPolygon':
        return bool(c) and bool(c[0]) and bool(c[0][0]) and len(c[0][0][0]) > 2
    elif t == 'GeometryCollection':
        return any(_detect_z(g) for g in geom.get('geometries', []))
    return False


def _compute_envelope_xy(geom: dict) -> tuple[float, float, float, float]:
    """Return (min_x, max_x, min_y, max_y) for a GeoJSON-style geometry."""
    pts = list(_iter_positions(geom))
    if not pts:
        raise ValueError("Cannot compute envelope for geometry with no coordinates")
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), max(xs), min(ys), max(ys)


def _iter_positions(geom: dict):
    """Yield leaf [x, y, ...] positions from a GeoJSON-style geometry."""
    t = geom.get('type')
    c = geom.get('coordinates')

    if t == 'Point':
        if c is not None:
            yield c
        return

    if t in ('LineString', 'MultiPoint'):
        for pt in c or []:
            yield pt
        return

    if t in ('Polygon', 'MultiLineString'):
        for ring in c or []:
            for pt in ring:
                yield pt
        return

    if t == 'MultiPolygon':
        for poly in c or []:
            for ring in poly:
                for pt in ring:
                    yield pt
        return

    if t == 'GeometryCollection':
        for g in geom.get('geometries', []):
            yield from _iter_positions(g)
