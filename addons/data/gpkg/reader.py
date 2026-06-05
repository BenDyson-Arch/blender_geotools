"""gpkg/reader.py — Pure-Python GeoPackage reader using sqlite3.

Reads vector layers from .gpkg files without any external dependencies.
The GPKG binary geometry blobs are decoded by gpkg.wkb.

Public API:
  list_layers(filepath)                            -> list[LayerInfo]
  read_layer(filepath, layer, *, bbox, limit)      -> LayerData
  read_crs(filepath, layer)                        -> str | None
"""

import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import wkb as _wkb


@contextmanager
def _connection(filepath: str):
    """Open a read-only-style sqlite3 connection, always closing it on exit.

    On Windows, ``with sqlite3.connect(...) as con`` only commits/rolls-back
    the transaction but does NOT close the connection, leaving the file locked.
    This context manager always calls ``con.close()`` in the finally block.
    """
    con = sqlite3.connect(filepath)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Public data classes
# ---------------------------------------------------------------------------

@dataclass
class LayerInfo:
    """Metadata for one vector layer in a GPKG file."""
    name: str
    geom_type: str          # e.g. 'POLYGON', 'POINT', 'GEOMETRY'
    srid: int
    feature_count: int = -1 # -1 = count unavailable


@dataclass
class LayerData:
    """All features read from one layer."""
    name: str
    srid: int
    crs_wkt: str | None     # WKT definition from gpkg_spatial_ref_sys; None if absent
    geom_column: str
    fid_column: str
    features: list = field(default_factory=list)
    # Each feature is a dict:
    #   {'fid': int, 'geometry': geojson_dict_or_None, 'properties': {col: val}}


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def list_layers(filepath: str) -> list[LayerInfo]:
    """Return all vector layers declared in gpkg_contents.

    Joins gpkg_geometry_columns for geometry type and SRID information.
    Returns an empty list if the file contains no feature layers or the
    required metadata tables are missing.
    """
    with _connection(filepath) as con:
        cur = con.cursor()

        # Confirm that gpkg_contents exists
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gpkg_contents'"
        )
        if cur.fetchone() is None:
            return []

        has_geom_cols = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='gpkg_geometry_columns'"
        ).fetchone() is not None

        if has_geom_cols:
            cur.execute("""
                SELECT c.table_name, g.geometry_type_name, g.srs_id
                FROM   gpkg_contents c
                LEFT JOIN gpkg_geometry_columns g ON c.table_name = g.table_name
                WHERE  c.data_type = 'features'
                ORDER  BY c.table_name
            """)
        else:
            cur.execute("""
                SELECT table_name, NULL AS geometry_type_name, srs_id
                FROM   gpkg_contents
                WHERE  data_type = 'features'
                ORDER  BY table_name
            """)

        layers = []
        for row in cur.fetchall():
            name = row['table_name']
            geom_type = row['geometry_type_name'] or 'GEOMETRY'
            srid = row['srs_id'] or 0

            try:
                count = cur.execute(
                    f'SELECT COUNT(*) FROM "{name}"'
                ).fetchone()[0]
            except sqlite3.Error:
                count = -1

            layers.append(LayerInfo(name=name, geom_type=geom_type, srid=srid, feature_count=count))

        return layers


def read_crs(filepath: str, layer: str) -> str | None:
    """Return the CRS identifier for *layer* (e.g. ``'EPSG:4326'``), or ``None``.

    Constructs the identifier from the ``organization`` and
    ``organization_coordsys_id`` columns of ``gpkg_spatial_ref_sys``.
    """
    with _connection(filepath) as con:
        cur = con.cursor()

        try:
            row = cur.execute(
                "SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
                (layer,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None

        srs_id = row['srs_id']

        try:
            srs_row = cur.execute(
                "SELECT organization, organization_coordsys_id "
                "FROM gpkg_spatial_ref_sys WHERE srs_id = ?",
                (srs_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        if srs_row is None:
            return None

        org = (srs_row['organization'] or '').upper()
        org_id = srs_row['organization_coordsys_id']
        if org and org_id:
            return f"{org}:{org_id}"
        return None


def read_column_schema(filepath: str, layer: str) -> dict:
    """Return ``{column_name: sql_type}`` for attribute columns in *layer*.

    The geometry column and the primary-key column are excluded.
    """
    with _connection(filepath) as con:
        cur = con.cursor()
        geom_col, fid_col, _ = _get_layer_meta(cur, layer)
        info = cur.execute(f'PRAGMA table_info("{layer}")').fetchall()
        return {
            row[1]: (row[2] or "TEXT")
            for row in info
            if row[1] != geom_col and row[1] != fid_col
        }


def read_layer(
    filepath: str,
    layer: str,
    *,
    bbox: tuple | None = None,
    limit: int | None = None,
) -> LayerData:
    """Read features from *layer* into a :class:`LayerData`.

    Args:
        filepath: Path to the .gpkg file.
        layer:    Table name of the layer (must appear in gpkg_contents).
        bbox:     Optional ``(min_x, min_y, max_x, max_y)`` bounding-box
                  filter.  Applied in Python after decoding geometry; no
                  spatial index is required.
        limit:    Maximum number of features to return.

    Returns:
        :class:`LayerData` whose ``features`` list contains dicts with keys
        ``fid``, ``geometry`` (GeoJSON dict or ``None``), and ``properties``.
    """
    with _connection(filepath) as con:
        cur = con.cursor()

        geom_col, fid_col, srid = _get_layer_meta(cur, layer)

        # WKT definition for the layer CRS
        try:
            srs_row = cur.execute(
                "SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id = "
                "(SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?)",
                (layer,)
            ).fetchone()
            crs_wkt = srs_row['definition'] if srs_row else None
        except sqlite3.Error:
            crs_wkt = None

        # Column list: fid + geom first, then all attribute columns
        table_cols = _get_table_columns(cur, layer)
        attr_cols = [c for c in table_cols if c != geom_col and c != fid_col]

        col_expr = ', '.join(
            [f'"{fid_col}"', f'"{geom_col}"'] + [f'"{c}"' for c in attr_cols]
        )
        sql = f'SELECT {col_expr} FROM "{layer}"'
        if limit is not None:
            sql += f' LIMIT {int(limit)}'

        cur.execute(sql)

        features = []
        for row in cur:
            fid = row[fid_col]
            raw_geom = row[geom_col]
            try:
                geom = _wkb.parse(bytes(raw_geom)) if raw_geom is not None else None
            except (ValueError, struct.error):
                geom = None

            if bbox is not None and geom is not None:
                if not _geom_intersects_bbox(geom, bbox):
                    continue

            props = {c: row[c] for c in attr_cols}
            features.append({'fid': fid, 'geometry': geom, 'properties': props})

        return LayerData(
            name=layer,
            srid=srid,
            crs_wkt=crs_wkt,
            geom_column=geom_col,
            fid_column=fid_col,
            features=features,
        )


# ---------------------------------------------------------------------------
# Internal helpers (used by writer.py as well)
# ---------------------------------------------------------------------------

def _get_layer_meta(cur: sqlite3.Cursor, layer: str) -> tuple:
    """Return ``(geom_column, fid_column, srid)`` for *layer*."""
    try:
        row = cur.execute(
            "SELECT column_name, srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
            (layer,)
        ).fetchone()
    except sqlite3.Error:
        row = None

    if row:
        geom_col = row[0]
        srid = int(row[1] or 0)
    else:
        geom_col = _guess_geom_column(cur, layer)
        srid = 0

    fid_col = _get_fid_column(cur, layer)
    return geom_col, fid_col, srid


def _get_table_columns(cur: sqlite3.Cursor, layer: str) -> list:
    """Return all column names for *layer*."""
    info = cur.execute(f'PRAGMA table_info("{layer}")').fetchall()
    return [row[1] for row in info]


def _get_fid_column(cur: sqlite3.Cursor, layer: str) -> str:
    """Return the integer primary-key column name, defaulting to ``'fid'``."""
    info = cur.execute(f'PRAGMA table_info("{layer}")').fetchall()
    for row in info:
        if row[5] != 0:         # pk column (non-zero = part of primary key)
            return row[1]
    return 'fid'


def _guess_geom_column(cur: sqlite3.Cursor, layer: str) -> str:
    """Heuristically identify the geometry (BLOB) column when gpkg_geometry_columns is absent."""
    info = cur.execute(f'PRAGMA table_info("{layer}")').fetchall()
    for row in info:
        name_lower = row[1].lower()
        type_lower = (row[2] or '').lower()
        if 'geom' in name_lower or type_lower == 'blob':
            return row[1]
    return 'geom'


def _geom_intersects_bbox(geom: dict, bbox: tuple) -> bool:
    """Return True if the geometry's envelope overlaps the filter bbox."""
    min_x, min_y, max_x, max_y = bbox
    coords = _flatten_coords(geom)
    if not coords:
        return False
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (max(xs) >= min_x and min(xs) <= max_x and
            max(ys) >= min_y and min(ys) <= max_y)


def _flatten_coords(geom: dict) -> list:
    """Return a flat list of all leaf coordinate positions from a GeoJSON geometry."""
    t = geom.get('type')
    c = geom.get('coordinates')
    if t == 'Point':
        return [c]
    elif t in ('LineString', 'MultiPoint'):
        return list(c)
    elif t in ('Polygon', 'MultiLineString'):
        return [pt for ring in c for pt in ring]
    elif t == 'MultiPolygon':
        return [pt for poly in c for ring in poly for pt in ring]
    elif t == 'GeometryCollection':
        result = []
        for g in geom.get('geometries', []):
            result.extend(_flatten_coords(g))
        return result
    return []



