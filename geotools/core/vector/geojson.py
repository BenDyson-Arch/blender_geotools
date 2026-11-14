"""GeoJSON (RFC 7946) reading, writing and attribute rewriting.

Ported from the 0.2 ``geojson/parser.py``. Reads a FeatureCollection, a
Feature or a bare geometry: Point, MultiPoint, LineString, MultiLineString,
Polygon, MultiPolygon and GeometryCollection (split by kind), 2D or 3D.

The CRS is the legacy ``crs`` member when there is one, otherwise RFC 7946's
WGS84 longitude, latitude (EPSG:4326). Files GeoTools writes in a projected
CRS carry the legacy ``crs`` member, which is outside RFC 7946 but read by
QGIS and GDAL.

Feature ids: when every feature has a distinct integer ``id`` member it is
the fid (``fid_field`` ``"id"``); otherwise the fid is the feature's position
in the file, from 0 (``fid_field`` :data:`INDEX_FID`).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import numpy as np

from ..crs_match import SOURCE_GEOJSON_MEMBER, SOURCE_RFC7946, WGS84, DataCrs, normalise_epsg
from .model import (
    KIND_ORDER,
    Column,
    Geometries,
    GeometryBuilder,
    Kind,
    VectorData,
    column_from_values,
)

INDEX_FID = "@index"
"""``fid_field`` meaning "the feature's position in the file"."""

GEOMETRY_TYPES = frozenset(
    {
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
        "GeometryCollection",
    }
)


class GeoJsonError(ValueError):
    """The file is not GeoJSON GeoTools can read."""


# --- reading -----------------------------------------------------------------------


def load(path: str | os.PathLike[str]) -> dict:
    """Parse a GeoJSON file into a dict, checking its top-level type."""
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as ex:
        raise GeoJsonError(f"not valid JSON: {ex}") from None
    if not isinstance(data, dict):
        raise GeoJsonError("the top level is not a JSON object")
    kind = data.get("type")
    if kind not in GEOMETRY_TYPES | {"Feature", "FeatureCollection"}:
        raise GeoJsonError(f"unrecognised GeoJSON type {kind!r}")
    return data


def features_of(data: dict) -> list[dict]:
    """The features of a FeatureCollection, a Feature or a bare geometry."""
    kind = data.get("type")
    if kind == "FeatureCollection":
        features = data.get("features") or []
        if not isinstance(features, list):
            raise GeoJsonError("'features' is not a list")
        return features
    if kind == "Feature":
        return [data]
    return [{"type": "Feature", "geometry": data, "properties": {}}]


def crs_of(data: dict) -> DataCrs:
    """The CRS a GeoJSON object declares (legacy ``crs`` member), or RFC 7946 WGS84."""
    member = data.get("crs")
    if not isinstance(member, dict):
        return DataCrs(WGS84, None, SOURCE_RFC7946)
    props = member.get("properties") or {}
    if member.get("type") == "name":
        name = str(props.get("name") or "")
        epsg = normalise_epsg(name)
        return DataCrs(epsg, None if epsg else name or None, SOURCE_GEOJSON_MEMBER)
    if member.get("type") == "link":
        href = str(props.get("href") or "")
        return DataCrs(normalise_epsg(href), href or None, SOURCE_GEOJSON_MEMBER)
    return DataCrs(None, None, SOURCE_GEOJSON_MEMBER)


def detect_fids(features: list[dict]) -> tuple[str, np.ndarray]:
    """``(fid_field, fids)``: the ``id`` members if all are distinct ints, else positions."""
    ids = [f.get("id") if isinstance(f, dict) else None for f in features]
    ints = bool(ids) and all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
    if ints and len(set(ids)) == len(ids) and all(-(2**63) <= i < 2**63 for i in ids):
        return "id", np.asarray(ids, dtype=np.int64)
    return INDEX_FID, np.arange(len(features), dtype=np.int64)


def _ring(coords: object) -> np.ndarray:
    """``(k, 2)`` or ``(k, 3)`` binary64 from a GeoJSON position list."""
    try:
        arr = np.asarray(coords, dtype=np.float64)
    except (TypeError, ValueError):
        arr = None
    if arr is not None and arr.ndim == 2 and arr.shape[1] >= 2:
        return arr[:, :3]
    if arr is not None and arr.ndim == 1 and arr.size == 0:
        return np.zeros((0, 2))
    # Mixed 2D and 3D positions in one list: pad the 2D ones with NaN.
    if not isinstance(coords, list):
        raise GeoJsonError("a coordinate list is not a list")
    out = np.full((len(coords), 3), np.nan)
    for i, position in enumerate(coords):
        if not isinstance(position, list) or len(position) < 2:
            raise GeoJsonError("a position has fewer than two numbers")
        out[i, : min(len(position), 3)] = [float(v) for v in position[:3]]
    return out


def _add_geometry(builder: GeometryBuilder, row: int, geometry: object) -> None:
    if geometry is None:
        return
    if not isinstance(geometry, dict):
        raise GeoJsonError("a geometry is not an object")
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "GeometryCollection":
        for child in geometry.get("geometries") or []:
            _add_geometry(builder, row, child)
        return
    if kind not in GEOMETRY_TYPES:
        raise GeoJsonError(f"unknown geometry type {kind!r}")
    if coords is None or coords == []:
        return  # an empty geometry
    if kind == "Point":
        builder.add_points(row, _ring([coords]))
    elif kind == "MultiPoint":
        builder.add_points(row, _ring(coords))
    elif kind == "LineString":
        builder.add_line(row, _ring(coords))
    elif kind == "MultiLineString":
        for line in coords:
            if line:
                builder.add_line(row, _ring(line))
    elif kind == "Polygon":
        if coords and coords[0]:
            builder.add_polygon(row, [_ring(r) for r in coords])
    elif kind == "MultiPolygon":
        for polygon in coords:
            if polygon and polygon[0]:
                builder.add_polygon(row, [_ring(r) for r in polygon])


def from_dict(data: dict, name: str) -> VectorData:
    """A :class:`VectorData` from parsed GeoJSON."""
    features = features_of(data)
    fid_field, fids = detect_fids(features)
    builder = GeometryBuilder()
    keys: dict[str, None] = {}
    props_per_row: list[dict] = []
    for row, feature in enumerate(features):
        if not isinstance(feature, dict):
            raise GeoJsonError(f"feature {row} is not an object")
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            props = {}
        props_per_row.append(props)
        for key in props:
            keys.setdefault(key, None)
        try:
            _add_geometry(builder, row, feature.get("geometry"))
        except GeoJsonError as ex:
            raise GeoJsonError(f"feature {row}: {ex}") from None
    columns: dict[str, Column] = {}
    skipped = []
    for key in keys:
        column = column_from_values(key, [p.get(key) for p in props_per_row])
        if column is None:
            skipped.append(key)
        else:
            columns[key] = column
    return VectorData(
        name=name,
        fids=fids,
        columns=columns,
        geometries=builder.finish(),
        crs=crs_of(data),
        fid_field=fid_field,
        skipped_columns=skipped,
    )


def read(path: str | os.PathLike[str]) -> VectorData:
    """Read a GeoJSON file; the layer is named after the file's stem."""
    return from_dict(load(path), Path(path).stem)


# --- writing -----------------------------------------------------------------------


def crs_member(crs: DataCrs | None) -> dict | None:
    """The legacy ``crs`` member for ``crs``; None for WGS84 (RFC 7946) or no CRS."""
    if crs is None or not crs.known:
        return None
    if crs.epsg == WGS84:
        return None
    if crs.epsg:
        code = crs.epsg.split(":", 1)[1]
        return {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{code}"}}
    return {"type": "name", "properties": {"name": crs.definition}}


def _positions(coords: np.ndarray, has_z: bool, close: bool) -> list:
    if close and len(coords) > 1 and not np.array_equal(coords[0, :2], coords[-1, :2]):
        coords = np.concatenate([coords, coords[:1]])
    if has_z:
        xyz = np.where(np.isnan(coords), 0.0, coords)
        return xyz.tolist()
    return coords[:, :2].tolist()


def _geometry(geoms: Geometries, feature: int, has_z: bool) -> dict:
    rings = geoms.ring_offsets
    parts = geoms.part_offsets
    p0, p1 = int(geoms.feature_offsets[feature]), int(geoms.feature_offsets[feature + 1])

    def ring(r: int, close: bool = False) -> list:
        return _positions(geoms.coords[rings[r] : rings[r + 1]], has_z, close)

    if geoms.kind is Kind.POINTS:
        points = [ring(int(parts[p]))[0] for p in range(p0, p1)]
        if len(points) == 1:
            return {"type": "Point", "coordinates": points[0]}
        return {"type": "MultiPoint", "coordinates": points}
    if geoms.kind is Kind.LINES:
        lines = [ring(int(parts[p])) for p in range(p0, p1)]
        if len(lines) == 1:
            return {"type": "LineString", "coordinates": lines[0]}
        return {"type": "MultiLineString", "coordinates": lines}
    polygons = [
        [ring(r, close=True) for r in range(int(parts[p]), int(parts[p + 1]))]
        for p in range(p0, p1)
    ]
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def _measures(geoms: Geometries, feature: int) -> list:
    """A feature's M values, shaped like its coordinates: one list per line string
    (a list of them for a MultiLineString), one value per point."""
    m = np.asarray(geoms.measures, dtype=np.float64)
    rings, parts = geoms.ring_offsets, geoms.part_offsets
    p0, p1 = int(geoms.feature_offsets[feature]), int(geoms.feature_offsets[feature + 1])

    def ring(r: int) -> list:
        return [_json_value(v) for v in m[rings[r] : rings[r + 1]].tolist()]

    if geoms.kind is Kind.POINTS:
        points = [ring(int(parts[p]))[0] for p in range(p0, p1)]
        return points[0] if len(points) == 1 else points
    if geoms.kind is Kind.LINES:
        lines = [ring(int(parts[p])) for p in range(p0, p1)]
        return lines[0] if len(lines) == 1 else lines
    polygons = [[ring(r) for r in range(int(parts[p]), int(parts[p + 1]))] for p in range(p0, p1)]
    return polygons[0] if len(polygons) == 1 else polygons


def _json_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def to_dict(data: VectorData, crs: DataCrs | None) -> dict:
    """A FeatureCollection of every row of ``data``, in row order.

    GeoJSON positions hold no measure (RFC 7946 advises against a fourth
    element), so geometries with M (a section's chainage) carry it in the
    property named by ``measure_name``: one value per position, shaped like
    the coordinates. An attribute column of that name takes precedence.
    """
    geometry_of: list[list[dict]] = [[] for _ in range(data.n_rows)]
    measures_of: dict[int, tuple[str, list]] = {}
    for kind in KIND_ORDER:
        geoms = data.geometries.get(kind)
        if geoms is None:
            continue
        has_z = geoms.has_z
        has_m = geoms.has_m and bool(geoms.measure_name)
        for i, row in enumerate(geoms.rows.tolist()):
            geometry_of[row].append(_geometry(geoms, i, has_z))
            if has_m:
                measures_of[row] = (geoms.measure_name, _measures(geoms, i))
    columns = list(data.columns.values())
    features = []
    for row in range(data.n_rows):
        geometries = geometry_of[row]
        if not geometries:
            geometry = None
        elif len(geometries) == 1:
            geometry = geometries[0]
        else:
            geometry = {"type": "GeometryCollection", "geometries": geometries}
        properties = {c.name: _json_value(c.python_value(row)) for c in columns}
        if row in measures_of:
            properties.setdefault(*measures_of[row])
        features.append(
            {
                "type": "Feature",
                "id": int(data.fids[row]),
                "geometry": geometry,
                "properties": properties,
            }
        )
    out: dict = {"type": "FeatureCollection", "name": data.name}
    member = crs_member(crs)
    if member is not None:
        out["crs"] = member
    out["features"] = features
    return out


def write(path: str | os.PathLike[str], data: VectorData, crs: DataCrs | None) -> None:
    """Write ``data`` as a GeoJSON FeatureCollection, atomically."""
    _write_json(Path(path), to_dict(data, crs), indent=None)


# --- attribute rewriting -----------------------------------------------------------------


def rewrite_properties(
    path: str | os.PathLike[str],
    fid_field: str,
    fids: np.ndarray,
    updates: dict[str, list[object]],
) -> tuple[int, int]:
    """Set ``updates`` (column -> one value per fid) in the file's features.

    Everything else (unknown members, geometry, feature order, other
    properties) is kept. The file is replaced atomically. Returns
    ``(features updated, fids not found)``.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8-sig")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as ex:
        raise GeoJsonError(f"not valid JSON: {ex}") from None
    features = features_of(data)
    if fid_field == INDEX_FID:
        index = {i: i for i in range(len(features))}
    else:
        index = {f.get(fid_field): i for i, f in enumerate(features) if isinstance(f, dict)}
    updated = missing = 0
    for k, fid in enumerate(np.asarray(fids).tolist()):
        i = index.get(int(fid))
        if i is None:
            missing += 1
            continue
        feature = features[i]
        props = feature.get("properties")
        if not isinstance(props, dict):
            props = {}
            feature["properties"] = props
        for name, values in updates.items():
            props[name] = _json_value(values[k])
        updated += 1
    _write_json(path, data, indent=_indent_of(text))
    return updated, missing


def _indent_of(text: str) -> int | None:
    """The indent of a pretty-printed file (None for compact JSON)."""
    for line in text.splitlines()[1:6]:
        stripped = line.lstrip(" ")
        if stripped and len(stripped) < len(line):
            return len(line) - len(stripped)
    return None


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _write_json(path: Path, data: dict, indent: int | None) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=indent, allow_nan=False)
            fh.write("\n")
        # mkstemp makes the file 0600; keep the mode of the file being replaced.
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o666 & ~_umask()
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
