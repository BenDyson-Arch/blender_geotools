"""gpkg/writer.py — Pure-Python GeoPackage writer using sqlite3.

All write operations open a dedicated connection, run inside a single
transaction, and roll back automatically on any error.

Column names supplied by the caller are validated against the actual table
schema before use in SQL — this prevents SQL injection while still allowing
dynamic attribute names.  Table and geometry column names come from the GPKG
metadata tables (trusted source), so they are double-quoted as SQL identifiers.

Public API:
  update_feature_geometry(filepath, layer, fid, geometry_dict, srid=0)
  update_feature_attributes(filepath, layer, fid, properties)
  insert_feature(filepath, layer, geometry_dict, properties, srid=0) -> int
  delete_feature(filepath, layer, fid)

Raises:
  GPKGWriteError  — fid not found, no columns to update, or other write failure.
"""

import sqlite3
from contextlib import contextmanager

from . import wkb as _wkb
from .reader import _get_layer_meta, _get_table_columns


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------

class GPKGWriteError(Exception):
    """Raised when a GPKG write operation fails for a logical reason."""


# ---------------------------------------------------------------------------
# Public write functions
# ---------------------------------------------------------------------------

def update_feature_geometry(
    filepath: str,
    layer: str,
    fid: int,
    geometry_dict: dict | None,
    srid: int = 0,
) -> None:
    """Replace the geometry of an existing feature.

    Args:
        filepath:      Path to the .gpkg file.
        layer:         Feature table name.
        fid:           Integer primary-key value of the row to update.
        geometry_dict: GeoJSON-style geometry dict, or ``None`` for empty.
        srid:          SRID written into the GPKG binary header.
    """
    geom_blob = _wkb.encode(geometry_dict, srid=srid)

    with _transaction(filepath) as con:
        cur = con.cursor()
        geom_col, fid_col, _ = _get_layer_meta(cur, layer)

        cur.execute(
            f'UPDATE "{layer}" SET "{geom_col}" = ? WHERE "{fid_col}" = ?',
            (geom_blob, fid),
        )
        if cur.rowcount == 0:
            raise GPKGWriteError(
                f"update_feature_geometry: no row with {fid_col}={fid!r} in layer {layer!r}"
            )


def update_feature_attributes(
    filepath: str,
    layer: str,
    fid: int,
    properties: dict,
) -> None:
    """Update non-geometry attribute columns for an existing feature.

    Only columns that exist in the table AND are not the fid or geometry
    column are written.  Unknown keys are silently ignored.

    Args:
        filepath:   Path to the .gpkg file.
        layer:      Feature table name.
        fid:        Integer primary-key value of the row to update.
        properties: Dict of ``{column_name: value}`` pairs to update.
    """
    if not properties:
        return

    with _transaction(filepath) as con:
        cur = con.cursor()
        geom_col, fid_col, _ = _get_layer_meta(cur, layer)
        table_cols = set(_get_table_columns(cur, layer))

        safe = _validated_props(properties, table_cols, exclude={geom_col, fid_col})
        if not safe:
            return

        set_clause = ', '.join(f'"{k}" = ?' for k in safe)
        values = list(safe.values()) + [fid]

        cur.execute(
            f'UPDATE "{layer}" SET {set_clause} WHERE "{fid_col}" = ?',
            values,
        )
        if cur.rowcount == 0:
            raise GPKGWriteError(
                f"update_feature_attributes: no row with {fid_col}={fid!r} in layer {layer!r}"
            )


def insert_feature(
    filepath: str,
    layer: str,
    geometry_dict: dict | None,
    properties: dict,
    srid: int = 0,
) -> int:
    """Insert a new feature and return its new fid.

    Args:
        filepath:      Path to the .gpkg file.
        layer:         Feature table name.
        geometry_dict: GeoJSON-style geometry dict, or ``None`` for empty.
        properties:    Dict of ``{column_name: value}`` attribute pairs.
        srid:          SRID written into the GPKG binary header.

    Returns:
        The integer primary-key value assigned to the new row.
    """
    geom_blob = _wkb.encode(geometry_dict, srid=srid)

    with _transaction(filepath) as con:
        cur = con.cursor()
        geom_col, fid_col, _ = _get_layer_meta(cur, layer)
        table_cols = set(_get_table_columns(cur, layer))

        safe = _validated_props(properties, table_cols, exclude={geom_col, fid_col})

        cols = [geom_col] + list(safe.keys())
        vals = [geom_blob] + list(safe.values())

        col_str = ', '.join(f'"{c}"' for c in cols)
        placeholders = ', '.join('?' for _ in cols)

        cur.execute(
            f'INSERT INTO "{layer}" ({col_str}) VALUES ({placeholders})',
            vals,
        )
        return cur.lastrowid


def delete_feature(filepath: str, layer: str, fid: int) -> None:
    """Delete a feature by its integer primary-key value.

    Args:
        filepath: Path to the .gpkg file.
        layer:    Feature table name.
        fid:      Integer primary-key value of the row to delete.
    """
    with _transaction(filepath) as con:
        cur = con.cursor()
        _, fid_col, _ = _get_layer_meta(cur, layer)

        cur.execute(
            f'DELETE FROM "{layer}" WHERE "{fid_col}" = ?',
            (fid,),
        )
        if cur.rowcount == 0:
            raise GPKGWriteError(
                f"delete_feature: no row with {fid_col}={fid!r} in layer {layer!r}"
            )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

@contextmanager
def _transaction(filepath: str):
    """Open a connection, yield it, commit on success, roll back on error."""
    con = sqlite3.connect(filepath)
    con.row_factory = sqlite3.Row
    _register_spatialite_stubs(con)
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _register_spatialite_stubs(con: sqlite3.Connection) -> None:
    """Register Python stubs for SpatiaLite functions used by GPKG triggers.

    GPKG files produced by QGIS/GDAL often have geometry-column triggers that
    call SpatiaLite functions (e.g. ``ST_IsEmpty``, ``ST_MinX``, …).  Plain
    sqlite3 does not load SpatiaLite, so those triggers fail.  We register
    minimal Python implementations that satisfy the trigger contracts.

    The stubs only need to be *correct enough* for the trigger to not raise.
    ``ST_IsEmpty`` returns 0 (not empty — our encoder never writes empty
    geometries).  Envelope functions parse the GPKG binary header to extract
    the envelope values stored there, or return ``None`` if unavailable.
    """
    # 1 arg — geometry blob or None
    con.create_function("ST_IsEmpty",   1, lambda g: 0 if g else 1)
    con.create_function("ST_IsValid",   1, lambda g: 1 if g else 0)

    # Envelope extraction from the GPKG binary header.
    # The header layout (ISO 13249 GPKG spec §2.1.3):
    #   bytes 0-1: magic 'GP'
    #   byte  2:   version (1)
    #   byte  3:   flags  (bits 1-3 = envelope type, bit 0 = empty)
    #   bytes 4-7: SRID (int32 LE)
    #   [envelope: 0, 2, 3 or 4 × float64 depending on flags bits 1-3]
    def _envelope(blob, idx):
        """Extract an envelope value by index, falling back to parsed bounds."""
        if blob is None or len(blob) < 8:
            return None
        flags = blob[3]
        env_type = (flags >> 1) & 7   # 0=none, 1=bbox, 2=bbox+Z, 3=bbox+M, 4=bbox+ZM
        if env_type != 0:
            offset = 8 + idx * 8          # float64 at 8 bytes each
            if len(blob) >= offset + 8:
                import struct
                return struct.unpack_from('<d', blob, offset)[0]

        # Legacy/no-envelope geometry: parse coordinates and compute XY bounds.
        try:
            geom = _wkb.parse(blob)
        except Exception:
            return None
        coords = _flatten_coords(geom)
        if not coords:
            return None
        xs = [pt[0] for pt in coords]
        ys = [pt[1] for pt in coords]
        fallback = (min(xs), max(xs), min(ys), max(ys))
        if idx < len(fallback):
            return fallback[idx]
        return None

    # GPKG envelope order: min_x, max_x, min_y, max_y [, min_z, max_z …]
    con.create_function("ST_MinX", 1, lambda g: _envelope(g, 0))
    con.create_function("ST_MaxX", 1, lambda g: _envelope(g, 1))
    con.create_function("ST_MinY", 1, lambda g: _envelope(g, 2))
    con.create_function("ST_MaxY", 1, lambda g: _envelope(g, 3))
    con.create_function("ST_MinZ", 1, lambda g: _envelope(g, 4))
    con.create_function("ST_MaxZ", 1, lambda g: _envelope(g, 5))
    con.create_function("ST_MinM", 1, lambda g: _envelope(g, 4))
    con.create_function("ST_MaxM", 1, lambda g: _envelope(g, 5))


def _validated_props(properties: dict, table_cols: set, exclude: set) -> dict:
    """Return a filtered copy of *properties* keeping only valid, writable columns.

    A column is kept if it appears in *table_cols* and is not in *exclude*.
    Columns not present in the table are silently dropped (not an error).
    """
    return {
        k: v
        for k, v in properties.items()
        if k in table_cols and k not in exclude
    }


def _flatten_coords(geom: dict | None) -> list:
    """Return a flat list of [x, y, ...] positions for any GeoJSON geometry."""
    if not geom:
        return []

    t = geom.get('type')
    c = geom.get('coordinates')

    if t == 'Point':
        return [c] if c else []
    if t in ('LineString', 'MultiPoint'):
        return list(c or [])
    if t in ('Polygon', 'MultiLineString'):
        return [pt for ring in (c or []) for pt in ring]
    if t == 'MultiPolygon':
        return [pt for poly in (c or []) for ring in poly for pt in ring]
    if t == 'GeometryCollection':
        out = []
        for g in geom.get('geometries', []):
            out.extend(_flatten_coords(g))
        return out
    return []
