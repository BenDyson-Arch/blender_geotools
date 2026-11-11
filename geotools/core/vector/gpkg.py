"""GeoPackage feature tables with the standard ``sqlite3`` module.

Ported from the 0.2 ``gpkg/reader.py`` and ``gpkg/writer.py``.

- Reading always uses a read-only connection (``mode=ro``). An attribute
  filter (an SQL ``WHERE`` clause) is checked by :func:`validate_where` and
  run under an authorizer that only allows reading the layer's own table.
- :func:`write_layer` creates or replaces a feature table (creating the file
  with the GeoPackage core tables if needed).
- :func:`update_attributes` writes attribute columns back by fid, adding
  columns when needed, in one transaction. It never touches geometry.

Triggers QGIS and GDAL add (the R-tree index ones) call SpatiaLite functions
that plain SQLite lacks; :func:`_register_spatialite_stubs` provides them, as
0.2 did.
"""

from __future__ import annotations

import os
import re
import sqlite3
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from ..crs_match import DataCrs, from_gpkg_srs
from . import wkb as _wkb
from .model import (
    INT32_MAX,
    INT32_MIN,
    Column,
    ColumnType,
    GeometryBuilder,
    Kind,
    VectorData,
    column_from_values,
)

APPLICATION_ID = 0x47504B47  # "GPKG"
USER_VERSION = 10400  # GeoPackage 1.4


class GpkgError(ValueError):
    """The GeoPackage, layer or filter cannot be used."""


@dataclass(frozen=True, slots=True)
class LayerInfo:
    name: str
    geometry_type: str
    srs_id: int
    feature_count: int


@dataclass(frozen=True, slots=True)
class TableMeta:
    table: str
    geometry_column: str
    fid_column: str
    srs_id: int
    columns: dict[str, str]  # attribute column -> declared type (no fid, no geometry)


def quote(identifier: str) -> str:
    """An SQL identifier in double quotes, with embedded quotes doubled."""
    return '"' + identifier.replace('"', '""') + '"'


# --- connections -------------------------------------------------------------------


def _uri(path: str | os.PathLike[str]) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


@contextmanager
def _read_only(path: str | os.PathLike[str]):
    if not Path(path).is_file():
        raise GpkgError(f"file not found: {path}")
    try:
        con = sqlite3.connect(_uri(path), uri=True)
    except sqlite3.Error as ex:
        raise GpkgError(f"cannot open {Path(path).name}: {ex}") from None
    try:
        yield con
    finally:
        con.close()  # always close: on Windows an open connection locks the file


@contextmanager
def _transaction(path: str | os.PathLike[str]):
    con = sqlite3.connect(os.fspath(path), isolation_level=None)
    _register_spatialite_stubs(con)
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


# --- metadata ----------------------------------------------------------------------


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def list_layers(path: str | os.PathLike[str]) -> list[LayerInfo]:
    """The feature tables listed in ``gpkg_contents``, by name."""
    with _read_only(path) as con:
        try:
            if not _has_table(con, "gpkg_contents"):
                raise GpkgError(f"{Path(path).name} is not a GeoPackage (no gpkg_contents)")
            rows = con.execute(
                "SELECT c.table_name, g.geometry_type_name, COALESCE(g.srs_id, c.srs_id) "
                "FROM gpkg_contents c LEFT JOIN gpkg_geometry_columns g "
                "ON c.table_name = g.table_name "
                "WHERE c.data_type = 'features' ORDER BY c.table_name"
            ).fetchall()
            layers = []
            for name, geometry_type, srs_id in rows:
                try:
                    count = con.execute(f"SELECT COUNT(*) FROM {quote(name)}").fetchone()[0]
                except sqlite3.Error:
                    count = -1
                layers.append(LayerInfo(name, geometry_type or "GEOMETRY", int(srs_id or 0), count))
            return layers
        except sqlite3.DatabaseError as ex:
            raise GpkgError(f"cannot read {Path(path).name}: {ex}") from None


def _table_meta(con: sqlite3.Connection, table: str) -> TableMeta:
    info = con.execute(f"PRAGMA table_info({quote(table)})").fetchall()
    if not info:
        raise GpkgError(f"there is no table {table!r}")
    row = None
    if _has_table(con, "gpkg_geometry_columns"):
        row = con.execute(
            "SELECT column_name, srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
            (table,),
        ).fetchone()
    if row is not None:
        geometry_column, srs_id = row[0], int(row[1] or 0)
    else:
        geometry_column = next(
            (r[1] for r in info if "geom" in r[1].lower() or (r[2] or "").upper() == "BLOB"),
            "geom",
        )
        srs_id = 0
    pk = [r for r in info if r[5]]
    fid_column = pk[0][1] if len(pk) == 1 else "rowid"
    columns = {
        r[1]: (r[2] or "").upper() for r in info if r[1] not in (geometry_column, fid_column)
    }
    return TableMeta(table, geometry_column, fid_column, srs_id, columns)


def _crs(con: sqlite3.Connection, srs_id: int) -> DataCrs:
    row = None
    if _has_table(con, "gpkg_spatial_ref_sys"):
        row = con.execute(
            "SELECT organization, organization_coordsys_id, definition "
            "FROM gpkg_spatial_ref_sys WHERE srs_id = ?",
            (srs_id,),
        ).fetchone()
    if row is None:
        return from_gpkg_srs(None, None, None, srs_id)
    return from_gpkg_srs(row[0], row[1], row[2], srs_id)


def table_meta(path: str | os.PathLike[str], table: str) -> TableMeta:
    with _read_only(path) as con:
        return _table_meta(con, table)


# --- the attribute filter ------------------------------------------------------------------

_FORBIDDEN = re.compile(r";|--|/\*")


def _check_where_text(where: str) -> None:
    """Refuse statement separators, comments and unbalanced brackets outside quotes."""
    depth = 0
    quote_char = ""
    for ch in where:
        if quote_char:
            if ch == quote_char:
                quote_char = ""
            continue
        if ch in "'\"`[":
            quote_char = "]" if ch == "[" else ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise GpkgError("the filter has an unmatched ')'")
    if quote_char:
        raise GpkgError("the filter has an unclosed quote")
    if depth:
        raise GpkgError("the filter has an unmatched '('")
    stripped = re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"", "''", where)
    if _FORBIDDEN.search(stripped):
        raise GpkgError("the filter may not contain ';' or comments")


def _authorizer(table: str):
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION}

    def authorize(action: int, arg1: str | None, arg2: str | None, *_rest: object) -> int:
        if action in allowed:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 == table:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def validate_where(path: str | os.PathLike[str], table: str, where: str) -> str | None:
    """None if ``where`` is a usable filter for ``table``, else why it is not."""
    if not where.strip():
        return None
    try:
        _check_where_text(where)
        with _read_only(path) as con:
            _table_meta(con, table)
            con.set_authorizer(_authorizer(table))
            con.execute(f"SELECT COUNT(*) FROM {quote(table)} WHERE ({where})").fetchone()
    except GpkgError as ex:
        return str(ex)
    except sqlite3.Error as ex:
        return f"the filter is not valid SQL here: {ex}"
    return None


# --- reading -----------------------------------------------------------------------


def _null_mask(values: list[object]) -> np.ndarray | None:
    null = np.fromiter((v is None for v in values), bool, len(values))
    return null if null.any() else None


def _column(name: str, declared: str, values: list[object]) -> Column | None:
    """A column from SQLite values, typed by the declared type where the values agree.

    SQLite does not enforce declared types, so values that do not fit the
    declared type fall back to :func:`column_from_values`.
    """
    if declared == "BLOB" or all(v is None for v in values):
        return None
    present = [v for v in values if v is not None]
    numeric = all(isinstance(v, int | float) and not isinstance(v, bool) for v in present)
    if declared == "BOOLEAN" and numeric:
        arr = np.fromiter((bool(v) if v is not None else False for v in values), bool, len(values))
        return Column(name, ColumnType.BOOL, arr, _null_mask(values), declared)
    is_int = "INT" in declared  # SQLite's integer affinity rule
    is_float = any(t in declared for t in ("REAL", "FLOA", "DOUB"))
    ints = is_int and numeric and all(isinstance(v, int) for v in present)
    if ints and min(present) >= INT32_MIN and max(present) <= INT32_MAX:
        arr = np.asarray([0 if v is None else v for v in values], dtype=np.int64)
        return Column(name, ColumnType.INT, arr, _null_mask(values), declared)
    if (is_int or is_float) and numeric:
        arr = np.asarray([np.nan if v is None else float(v) for v in values], dtype=np.float64)
        return Column(name, ColumnType.FLOAT, arr, _null_mask(values), declared)
    if any(t in declared for t in ("CHAR", "CLOB", "TEXT", "DATE", "TIME")):
        text = ["" if v is None else v if isinstance(v, str) else str(v) for v in values]
        return Column(name, ColumnType.STRING, text, _null_mask(values), declared)
    return column_from_values(name, values, declared)


def read_layer(
    path: str | os.PathLike[str], table: str, where: str = "", *, name: str | None = None
) -> VectorData:
    """Read the features of ``table`` (those matching ``where``), ordered by fid."""
    if where.strip():
        problem = validate_where(path, table, where)
        if problem:
            raise GpkgError(problem)
    with _read_only(path) as con:
        try:
            meta = _table_meta(con, table)
            crs = _crs(con, meta.srs_id)
            names = list(meta.columns)
            selected = ", ".join(
                [quote(meta.fid_column), quote(meta.geometry_column)] + [quote(c) for c in names]
            )
            sql = f"SELECT {selected} FROM {quote(table)}"
            if where.strip():
                con.set_authorizer(_authorizer(table))
                sql += f" WHERE ({where})"
            sql += f" ORDER BY {quote(meta.fid_column)}"
            rows = con.execute(sql).fetchall()
        except sqlite3.Error as ex:
            raise GpkgError(f"cannot read {table!r}: {ex}") from None
    builder = GeometryBuilder()
    bad = 0
    for row, record in enumerate(rows):
        blob = record[1]
        if blob is None:
            continue
        try:
            _wkb.read_gpkg(builder, row, blob)
        except (_wkb.WkbError, struct.error, ValueError):
            bad += 1
    fids = np.asarray([r[0] for r in rows], dtype=np.int64)
    columns: dict[str, Column] = {}
    skipped = []
    for k, column_name in enumerate(names):
        column = _column(column_name, meta.columns[column_name], [r[k + 2] for r in rows])
        if column is None:
            skipped.append(column_name)
        else:
            columns[column_name] = column
    data = VectorData(
        name=name or table,
        fids=fids,
        columns=columns,
        geometries=builder.finish(),
        crs=crs,
        fid_field=meta.fid_column,
        skipped_columns=skipped,
    )
    data.dropped_parts = bad
    return data


# --- SRS for writing ------------------------------------------------------------------


def _srs_row(crs: DataCrs | None) -> tuple[int, str, str, int, str]:
    """``(srs_id, name, organization, organization_coordsys_id, definition)``."""
    if crs is None or not crs.known:
        return -1, "Undefined Cartesian SRS", "NONE", -1, "undefined"
    if crs.epsg:
        code = int(crs.epsg.split(":", 1)[1])
        return code, crs.epsg, "EPSG", code, crs.definition or "undefined"
    return 100000, "GeoTools project CRS", "NONE", 100000, crs.definition or "undefined"


_BASE_SRS = (
    (
        4326,
        "WGS 84 geodetic",
        "EPSG",
        4326,
        (
            'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
            'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
            'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
            'AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]'
        ),
    ),
    (-1, "Undefined Cartesian SRS", "NONE", -1, "undefined"),
    (0, "Undefined geographic SRS", "NONE", 0, "undefined"),
)


def _ensure_core_tables(con: sqlite3.Connection) -> None:
    con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    con.execute(f"PRAGMA user_version = {USER_VERSION}")
    con.execute(
        "CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, "
        "srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL, "
        "organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, "
        "data_type TEXT NOT NULL, identifier TEXT UNIQUE, description TEXT DEFAULT '', "
        "last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
        "min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER, "
        "CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id))"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT NOT NULL, "
        "column_name TEXT NOT NULL, geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, "
        "z TINYINT NOT NULL, m TINYINT NOT NULL, "
        "CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name), "
        "CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name), "
        "CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id))"
    )
    for row in _BASE_SRS:
        con.execute(
            "INSERT OR IGNORE INTO gpkg_spatial_ref_sys (srs_id, srs_name, organization, "
            "organization_coordsys_id, definition) VALUES (?, ?, ?, ?, ?)",
            (row[0], row[1], row[2], row[3], row[4]),
        )


SQL_TYPES = {
    ColumnType.FLOAT: "REAL",
    ColumnType.INT: "INTEGER",
    ColumnType.BOOL: "BOOLEAN",
    ColumnType.STRING: "TEXT",
}

_GEOMETRY_TYPE = {Kind.POINTS: "POINT", Kind.LINES: "LINESTRING", Kind.POLYGONS: "POLYGON"}


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def write_layer(
    path: str | os.PathLike[str],
    table: str,
    data: VectorData,
    kind: Kind,
    crs: DataCrs | None,
    *,
    overwrite: bool = False,
) -> int:
    """Write the ``kind`` features of ``data`` to a new feature table; return the count.

    Coordinates are written as XYZ (0 for a missing Z) when the geometries have
    any Z. Fids are the data's fids when they are distinct and positive,
    otherwise 1, 2, 3 ... Raises :class:`GpkgError` if the table exists and
    ``overwrite`` is false.
    """
    geoms = data.geometries[kind]
    has_z = geoms.has_z
    multi = bool((np.diff(geoms.feature_offsets) > 1).any())
    srs = _srs_row(crs)
    fids = data.fids[geoms.rows]
    if len(np.unique(fids)) != len(fids) or (len(fids) and fids.min() < 1):
        fids = np.arange(1, len(fids) + 1, dtype=np.int64)
    columns = data.columns_for(kind)
    geometry_column = "geom" if "geom" not in columns else "geometry"
    geometry_type = ("MULTI" if multi else "") + _GEOMETRY_TYPE[kind]

    bounds = geoms.feature_bounds()
    z_range = geoms.feature_range(2) if has_z else None
    records = []
    for i in range(len(fids)):
        wkb = _wkb.encode_feature(geoms, i, multi, has_z)
        b = bounds[i]
        if np.isnan(b).any():
            blob = _wkb.gpkg_blob(wkb, srs[0], None)
        elif z_range is not None:
            z0, z1 = z_range[i]
            blob = _wkb.gpkg_blob(wkb, srs[0], [b[0], b[2], b[1], b[3], z0, z1])
        else:
            blob = _wkb.gpkg_blob(wkb, srs[0], [b[0], b[2], b[1], b[3]])
        values = [c.python_value(i) for c in columns.values()]
        records.append((int(fids[i]), blob, *values))

    path = Path(path)
    with _transaction(path) as con:
        _ensure_core_tables(con)
        if _has_table(con, table):
            if not overwrite:
                raise GpkgError(f"{path.name} already has a table {table!r}")
            _drop_table(con, table)
        con.execute(
            "INSERT OR REPLACE INTO gpkg_spatial_ref_sys (srs_id, srs_name, organization, "
            "organization_coordsys_id, definition) VALUES (?, ?, ?, ?, ?)",
            srs,
        )
        column_sql = "".join(f", {quote(n)} {SQL_TYPES[c.type]}" for n, c in columns.items())
        con.execute(
            f"CREATE TABLE {quote(table)} (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, "
            f"{quote(geometry_column)} {geometry_type}{column_sql})"
        )
        extent = bounds[~np.isnan(bounds).any(axis=1)]
        box = (
            (extent[:, 0].min(), extent[:, 1].min(), extent[:, 2].max(), extent[:, 3].max())
            if len(extent)
            else (None, None, None, None)
        )
        con.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, identifier, last_change, "
            "min_x, min_y, max_x, max_y, srs_id) VALUES (?, 'features', ?, ?, ?, ?, ?, ?, ?)",
            (table, table, _now(), *[None if v is None else float(v) for v in box], srs[0]),
        )
        con.execute(
            "INSERT INTO gpkg_geometry_columns (table_name, column_name, geometry_type_name, "
            "srs_id, z, m) VALUES (?, ?, ?, ?, ?, 0)",
            (table, geometry_column, geometry_type, srs[0], 1 if has_z else 0),
        )
        placeholders = ", ".join("?" for _ in range(2 + len(columns)))
        names = ", ".join(["fid", quote(geometry_column)] + [quote(n) for n in columns])
        con.executemany(f"INSERT INTO {quote(table)} ({names}) VALUES ({placeholders})", records)
    return len(records)


def _drop_table(con: sqlite3.Connection, table: str) -> None:
    for meta_table in ("gpkg_geometry_columns", "gpkg_contents"):
        if _has_table(con, meta_table):
            con.execute(f"DELETE FROM {meta_table} WHERE table_name = ?", (table,))
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE ?",
        (f"rtree_{table}_%",),
    ).fetchall():
        con.execute(f"DROP TABLE IF EXISTS {quote(name)}")
    con.execute(f"DROP TABLE {quote(table)}")


# --- attribute write-back -------------------------------------------------------------------


def update_attributes(
    path: str | os.PathLike[str],
    table: str,
    fids: np.ndarray,
    columns: dict[str, tuple[ColumnType, list[object]]],
) -> tuple[int, int, list[str]]:
    """Write ``columns`` (name -> type, one value per fid) to ``table`` by fid.

    Missing columns are added. Only those columns change, in one transaction.
    Returns ``(rows updated, fids not found, columns added)``.
    """
    if not columns:
        return 0, 0, []
    path = Path(path)
    with _transaction(path) as con:
        meta = _table_meta(con, table)
        names = list(columns)
        for reserved in (meta.fid_column, meta.geometry_column):
            if reserved in columns:
                raise GpkgError(f"{reserved!r} is the table's own column and is not written back")
        added = []
        for name in names:
            if name not in meta.columns:
                con.execute(
                    f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} "
                    f"{SQL_TYPES[columns[name][0]]}"
                )
                added.append(name)
        assignments = ", ".join(f"{quote(n)} = ?" for n in names)
        sql = f"UPDATE {quote(table)} SET {assignments} WHERE {quote(meta.fid_column)} = ?"
        fids = np.asarray(fids, dtype=np.int64)
        existing = np.asarray(
            [r[0] for r in con.execute(f"SELECT {quote(meta.fid_column)} FROM {quote(table)}")],
            dtype=np.int64,
        )
        found = np.isin(fids, existing)
        values = [columns[n][1] for n in names]
        rows = [
            [v[k] for v in values] + [int(fid)] for k, fid in enumerate(fids.tolist()) if found[k]
        ]
        con.executemany(sql, rows)
        updated, missing = len(rows), int((~found).sum())
        if _has_table(con, "gpkg_contents"):
            con.execute(
                "UPDATE gpkg_contents SET last_change = ? WHERE table_name = ?", (_now(), table)
            )
    return updated, missing, added


# --- SpatiaLite function stubs for GeoPackage triggers ------------------------------------


def _envelope_value(blob: bytes | None, index: int) -> float | None:
    """One envelope value (min x, max x, min y, max y, min z, max z) of a GeoPackage geometry."""
    if blob is None or len(blob) < 8 or bytes(blob[0:2]) != b"GP":
        return None
    flags = blob[3]
    kind = (flags >> 1) & 0x07
    endian = "<" if flags & 0x01 else ">"
    count = {1: 4, 2: 6, 3: 6, 4: 8}.get(kind, 0)
    if index < count and not (kind == 3 and index >= 4):
        return struct.unpack_from(endian + "d", blob, 8 + 8 * index)[0]
    builder = GeometryBuilder()
    try:
        _wkb.read_gpkg(builder, 0, bytes(blob))
    except (_wkb.WkbError, struct.error):
        return None
    coords = [g.coords for g in builder.finish().values()]
    if not coords:
        return None
    xyz = np.concatenate(coords)
    axis, use_max = divmod(index, 2)
    if axis > 2:
        return None
    values = xyz[:, axis]
    values = values[~np.isnan(values)]
    if not len(values):
        return None
    return float(values.max() if use_max else values.min())


def _register_spatialite_stubs(con: sqlite3.Connection) -> None:
    """Minimal ST_* functions so QGIS/GDAL R-tree triggers run in plain SQLite."""
    con.create_function("ST_IsEmpty", 1, lambda g: 1 if not g or (g[3] & 0x10) else 0)
    con.create_function("ST_IsValid", 1, lambda g: 1 if g else 0)
    con.create_function("GPKG_IsAssignable", 2, lambda a, b: 1)
    for index, name in enumerate(("MinX", "MaxX", "MinY", "MaxY", "MinZ", "MaxZ")):
        con.create_function(
            f"ST_{name}", 1, lambda g, i=index: _envelope_value(g, i), deterministic=True
        )
