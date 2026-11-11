"""Synthetic GeoJSON and GeoPackage fixtures, written by the tests themselves.

Independent of ``geotools.core.vector``: WKB is packed by hand with
``struct`` and GeoPackages are made with ``sqlite3``. No real site data.
bpy-free, so the core pytest suite imports it too.

Geometries are GeoJSON-style dicts, e.g. ``{"type": "Polygon",
"coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}``.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from pathlib import Path

# A projected site near Brisbane in GDA2020 / MGA zone 56 (synthetic numbers).
E0, N0 = 502_000.0, 6_961_000.0
MGA56 = "EPSG:7856"
MGA56_WKT = (
    'PROJCS["GDA2020 / MGA zone 56",GEOGCS["GDA2020",DATUM["Geocentric_Datum_of_Australia_2020",'
    'SPHEROID["GRS 1980",6378137,298.257222101,AUTHORITY["EPSG","7019"]],'
    'AUTHORITY["EPSG","1168"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","7844"]],'
    'PROJECTION["Transverse_Mercator"],PARAMETER["latitude_of_origin",0],'
    'PARAMETER["central_meridian",153],PARAMETER["scale_factor",0.9996],'
    'PARAMETER["false_easting",500000],PARAMETER["false_northing",10000000],'
    'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],AXIS["Northing",NORTH],'
    'AUTHORITY["EPSG","7856"]]'
)

_CODES = {
    "Point": 1,
    "LineString": 2,
    "Polygon": 3,
    "MultiPoint": 4,
    "MultiLineString": 5,
    "MultiPolygon": 6,
    "GeometryCollection": 7,
}


def square(x: float, y: float, size: float, z: float | None = None) -> list[list[float]]:
    """A closed counter-clockwise square ring."""
    pts = [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]
    return [p + [z] for p in pts] if z is not None else pts


# --- WKB -------------------------------------------------------------------------


def _dims(geom: dict) -> int:
    def first(c: object) -> list:
        while isinstance(c, list) and c and isinstance(c[0], list):
            c = c[0]
        return c  # type: ignore[return-value]

    if geom["type"] == "GeometryCollection":
        return max((_dims(g) for g in geom["geometries"]), default=2)
    pos = first(geom["coordinates"])
    return 3 if len(pos) > 2 else 2


def wkb(geom: dict, big_endian: bool = False, dims: int | None = None) -> bytes:
    """ISO WKB of a GeoJSON-style geometry."""
    e = ">" if big_endian else "<"
    order = 0 if big_endian else 1
    dims = dims or _dims(geom)
    code = _CODES[geom["type"]] + (1000 if dims == 3 else 0)
    head = struct.pack(e + "BI", order, code)

    def pos(p: list[float]) -> bytes:
        values = [*p[:2], (p[2] if len(p) > 2 else 0.0)] if dims == 3 else p[:2]
        return struct.pack(e + "d" * dims, *values)

    def ring(r: list) -> bytes:
        return struct.pack(e + "I", len(r)) + b"".join(pos(p) for p in r)

    t, c = geom["type"], geom.get("coordinates")
    if t == "Point":
        return head + pos(c)
    if t == "LineString":
        return head + ring(c)
    if t == "Polygon":
        return head + struct.pack(e + "I", len(c)) + b"".join(ring(r) for r in c)
    if t == "GeometryCollection":
        parts = geom["geometries"]
        return (
            head
            + struct.pack(e + "I", len(parts))
            + b"".join(wkb(g, big_endian, dims) for g in parts)
        )
    child = {"MultiPoint": "Point", "MultiLineString": "LineString", "MultiPolygon": "Polygon"}[t]
    return (
        head
        + struct.pack(e + "I", len(c))
        + b"".join(wkb({"type": child, "coordinates": x}, big_endian, dims) for x in c)
    )


def gpkg_geometry(
    geom: dict | None, srs_id: int, envelope: int = 1, big_endian: bool = False
) -> bytes:
    """A GeoPackage geometry blob; ``envelope`` 0 none, 1 XY, 2 XYZ."""
    e = ">" if big_endian else "<"
    if geom is None:
        return struct.pack(e + "2sBBi", b"GP", 0, (0 if big_endian else 1) | 0x10, srs_id)
    body = wkb(geom, big_endian)
    flags = (0 if big_endian else 1) | (envelope << 1)
    head = struct.pack(e + "2sBBi", b"GP", 0, flags, srs_id)
    if envelope == 0:
        return head + body
    pts = list(_positions(geom))
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    env = [min(xs), max(xs), min(ys), max(ys)]
    if envelope == 2:
        zs = [p[2] if len(p) > 2 else 0.0 for p in pts]
        env += [min(zs), max(zs)]
    return head + struct.pack(e + "d" * len(env), *env) + body


def _positions(geom: dict):
    if geom["type"] == "GeometryCollection":
        for g in geom["geometries"]:
            yield from _positions(g)
        return

    def walk(c: object):
        if isinstance(c, list) and c and not isinstance(c[0], list):
            yield c
        elif isinstance(c, list):
            for x in c:
                yield from walk(x)

    yield from walk(geom["coordinates"])


# --- GeoPackage -------------------------------------------------------------------


def write_gpkg(
    path: Path,
    table: str,
    features: list[tuple[int, dict | None, dict]],
    columns: dict[str, str],
    *,
    srs_id: int = 7856,
    organization: str = "EPSG",
    definition: str = MGA56_WKT,
    geometry_type: str = "GEOMETRY",
    z: int = 2,
    envelope: int = 1,
    big_endian: bool = False,
    wal: bool = False,
) -> Path:
    """A GeoPackage with one feature table: ``features`` are ``(fid, geometry, properties)``."""
    con = sqlite3.connect(path)
    try:
        if wal:
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA application_id = 1196444487")
        con.execute("PRAGMA user_version = 10400")
        con.execute(
            "CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, "
            "srs_id INTEGER PRIMARY KEY, organization TEXT NOT NULL, "
            "organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, "
            "description TEXT)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT PRIMARY KEY, "
            "data_type TEXT NOT NULL, identifier TEXT, description TEXT, last_change TEXT, "
            "min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER)"
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT, "
            "column_name TEXT, geometry_type_name TEXT, srs_id INTEGER, z INTEGER, m INTEGER)"
        )
        con.execute(
            "INSERT OR REPLACE INTO gpkg_spatial_ref_sys VALUES (?, ?, ?, ?, ?, NULL)",
            ("site", srs_id, organization, srs_id if organization == "EPSG" else 0, definition),
        )
        cols = "".join(f', "{n}" {t}' for n, t in columns.items())
        con.execute(f'CREATE TABLE "{table}" (fid INTEGER PRIMARY KEY, geom {geometry_type}{cols})')
        con.execute(
            "INSERT INTO gpkg_contents VALUES (?, 'features', ?, '', '2026-01-01T00:00:00Z', "
            "NULL, NULL, NULL, NULL, ?)",
            (table, table, srs_id),
        )
        con.execute(
            "INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, ?, 0)",
            (table, geometry_type, srs_id, z),
        )
        names = ["fid", "geom", *columns]
        sql = f'INSERT INTO "{table}" ({", ".join(chr(34) + n + chr(34) for n in names)}) ' + (
            f"VALUES ({', '.join('?' for _ in names)})"
        )
        for fid, geom, props in features:
            blob = None if geom is None else gpkg_geometry(geom, srs_id, envelope, big_endian)
            con.execute(sql, [fid, blob, *[props.get(n) for n in columns]])
        con.commit()
    finally:
        con.close()
    return path


def rows(path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(path)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


# --- GeoJSON ----------------------------------------------------------------------


def feature(geom: dict | None, props: dict, fid: object = None) -> dict:
    out: dict = {"type": "Feature", "geometry": geom, "properties": props}
    if fid is not None:
        out["id"] = fid
    return out


def write_geojson(
    path: Path, features: list[dict], crs: str | None = MGA56, extra: dict | None = None
) -> Path:
    """A FeatureCollection; ``crs`` as the legacy member (None for RFC 7946 WGS84)."""
    data: dict = {"type": "FeatureCollection"}
    if crs:
        data["crs"] = {"type": "name", "properties": {"name": crs}}
    if extra:
        data.update(extra)
    data["features"] = features
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def contexts(n_side: int = 3, z: float | None = None, holes: bool = True) -> list[dict]:
    """A grid of square "context" polygons near (E0, N0), every other one with a hole."""
    out = []
    fid = 1
    for i in range(n_side):
        for j in range(n_side):
            x, y = E0 + 20.0 * i, N0 + 20.0 * j
            rings = [square(x, y, 10.0, z)]
            if holes and (i + j) % 2 == 0:
                hole = square(x + 3.0, y + 3.0, 4.0, z)
                rings.append(hole[::-1])
            out.append(
                feature(
                    {"type": "Polygon", "coordinates": rings},
                    {
                        "context": f"C{fid:03d}",
                        "depth": 0.25 * fid,
                        "phase": fid % 3,
                        "sealed": fid % 2 == 0,
                    },
                    fid,
                )
            )
            fid += 1
    return out
