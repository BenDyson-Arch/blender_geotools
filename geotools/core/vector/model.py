"""The in-memory vector model shared by the GeoJSON and GeoPackage readers and writers.

A :class:`VectorData` is one source layer: a table of features (``fids`` and
attribute :class:`Column` s, one row per feature) and, per geometry
:class:`Kind`, a :class:`Geometries` with the coordinates of every feature of
that kind. A feature of a GeometryCollection can have geometry of several
kinds; each kind refers back to the same row.

Geometries use three levels of offsets, like Arrow's nested lists:

- ``feature_offsets`` (features + 1) index ``part_offsets``;
- ``part_offsets`` (parts + 1) index ``ring_offsets``;
- ``ring_offsets`` (rings + 1) index ``coords`` rows.

A point is a part with one ring of one coordinate, a line string a part with
one ring, and a polygon a part with its exterior ring and then its holes.
Coordinates are binary64, ``(n, 3)``, with NaN for a missing Z. Written
geometries can also carry a measure (M) per coordinate, such as a section's
chainage; readers drop M.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from ..crs_match import DataCrs


class Kind(StrEnum):
    POINTS = "points"
    LINES = "lines"
    POLYGONS = "polygons"


KIND_ORDER = (Kind.POLYGONS, Kind.LINES, Kind.POINTS)
"""Order in which kinds become Blender objects (the first one takes the layer's name)."""


class ColumnType(StrEnum):
    FLOAT = "float"  # binary64, NaN for null
    INT = "int"  # int64, 0 for null (see Column.null)
    BOOL = "bool"
    STRING = "string"  # list of str, "" for null


INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


@dataclass(slots=True)
class Column:
    """One attribute column, one value per feature row."""

    name: str
    type: ColumnType
    values: np.ndarray | list[str]
    null: np.ndarray | None = None  # bool mask of null values; None if there are none
    source_type: str = ""  # the declared SQL type of a GeoPackage column

    def __len__(self) -> int:
        return len(self.values)

    def take(self, rows: np.ndarray) -> Column:
        if self.type is ColumnType.STRING:
            values: np.ndarray | list[str] = [self.values[int(i)] for i in rows]
        else:
            values = np.asarray(self.values)[rows]
        null = None if self.null is None else self.null[rows]
        if null is not None and not null.any():
            null = None
        return Column(self.name, self.type, values, null, self.source_type)

    def python_value(self, row: int) -> object:
        """The value of one row as a JSON or SQLite value (None for null)."""
        if self.null is not None and self.null[row]:
            return None
        value = self.values[row]
        if self.type is ColumnType.FLOAT:
            value = float(value)
            return None if np.isnan(value) else value
        if self.type is ColumnType.INT:
            return int(value)
        if self.type is ColumnType.BOOL:
            return bool(value)
        return str(value)


def _empty_offsets() -> np.ndarray:
    return np.zeros(1, dtype=np.int64)


@dataclass(slots=True)
class Geometries:
    """Every geometry of one kind in a layer."""

    kind: Kind
    coords: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), dtype=np.float64))
    ring_offsets: np.ndarray = field(default_factory=_empty_offsets)
    part_offsets: np.ndarray = field(default_factory=_empty_offsets)
    feature_offsets: np.ndarray = field(default_factory=_empty_offsets)
    rows: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    measures: np.ndarray | None = None
    """``(n,)`` binary64 M of each coordinate (NaN for none), or None: no M."""
    measure_name: str = ""
    """What M is (``chainage``): the property GeoJSON holds it in."""

    @property
    def n_features(self) -> int:
        return len(self.rows)

    @property
    def n_parts(self) -> int:
        return len(self.part_offsets) - 1

    @property
    def n_rings(self) -> int:
        return len(self.ring_offsets) - 1

    @property
    def has_z(self) -> bool:
        return bool(len(self.coords)) and bool(np.isfinite(self.coords[:, 2]).any())

    @property
    def has_m(self) -> bool:
        return self.measures is not None and bool(np.isfinite(self.measures).any())

    def ring_lengths(self) -> np.ndarray:
        return np.diff(self.ring_offsets)

    def part_feature(self) -> np.ndarray:
        """The feature index (into ``rows``) of each part."""
        return np.repeat(np.arange(self.n_features), np.diff(self.feature_offsets))

    def ring_part(self) -> np.ndarray:
        return np.repeat(np.arange(self.n_parts), np.diff(self.part_offsets))

    def coord_ring(self) -> np.ndarray:
        return np.repeat(np.arange(self.n_rings), self.ring_lengths())

    def coord_feature(self) -> np.ndarray:
        """The feature index (into ``rows``) of each coordinate."""
        return self.part_feature()[self.ring_part()][self.coord_ring()]

    def feature_range(self, axis: int) -> np.ndarray:
        """``(features, 2)`` min and max of one axis per feature, ignoring NaN (NaN if none)."""
        out = np.full((self.n_features, 2), np.nan)
        values = self.coords[:, axis]
        owner = self.coord_feature()
        valid = ~np.isnan(values)
        values, owner = values[valid], owner[valid]
        if not len(values):
            return out
        counts = np.bincount(owner, minlength=self.n_features)
        has = counts > 0
        # Coordinates are stored feature by feature, so each feature is one run.
        starts = np.concatenate(([0], np.cumsum(counts)[:-1]))[has]
        out[has, 0] = np.minimum.reduceat(values, starts)
        out[has, 1] = np.maximum.reduceat(values, starts)
        return out

    def feature_bounds(self) -> np.ndarray:
        """``(features, 4)`` min x, min y, max x, max y; NaN for a feature without coordinates."""
        x, y = self.feature_range(0), self.feature_range(1)
        return np.stack([x[:, 0], y[:, 0], x[:, 1], y[:, 1]], axis=1)

    def select(self, keep: np.ndarray) -> Geometries:
        """The features where ``keep`` (one bool per feature) is true."""
        keep = np.asarray(keep, dtype=bool)
        f0, f1 = self.feature_offsets[:-1][keep], self.feature_offsets[1:][keep]
        parts = segment_indices(f0, f1 - f0)
        p0, p1 = self.part_offsets[:-1][parts], self.part_offsets[1:][parts]
        rings = segment_indices(p0, p1 - p0)
        r0, r1 = self.ring_offsets[:-1][rings], self.ring_offsets[1:][rings]
        coords = segment_indices(r0, r1 - r0)
        return Geometries(
            kind=self.kind,
            coords=self.coords[coords],
            ring_offsets=_offsets(r1 - r0),
            part_offsets=_offsets(p1 - p0),
            feature_offsets=_offsets(f1 - f0),
            rows=self.rows[keep],
            measures=None if self.measures is None else self.measures[coords],
            measure_name=self.measure_name,
        )


@dataclass(slots=True)
class VectorData:
    """One source layer."""

    name: str
    fids: np.ndarray  # int64, one per feature row
    columns: dict[str, Column]
    geometries: dict[Kind, Geometries]
    crs: DataCrs
    fid_field: str = "fid"
    dropped_parts: int = 0  # degenerate parts the reader skipped
    skipped_columns: list[str] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.fids)

    def kinds(self) -> list[Kind]:
        """The kinds with at least one feature, in :data:`KIND_ORDER`."""
        return [k for k in KIND_ORDER if k in self.geometries and self.geometries[k].n_features]

    def columns_for(self, kind: Kind) -> dict[str, Column]:
        """The attribute columns of the features of ``kind``, one value per feature."""
        rows = self.geometries[kind].rows
        return {name: column.take(rows) for name, column in self.columns.items()}

    def fids_for(self, kind: Kind) -> np.ndarray:
        return self.fids[self.geometries[kind].rows]

    def select_rows(self, keep: np.ndarray) -> VectorData:
        """The rows where ``keep`` (one bool per row) is true, renumbered."""
        keep = np.asarray(keep, dtype=bool)
        new_row = np.cumsum(keep) - 1
        geometries = {}
        for kind, geoms in self.geometries.items():
            kept = geoms.select(keep[geoms.rows])
            kept.rows = new_row[kept.rows]
            geometries[kind] = kept
        rows = np.flatnonzero(keep)
        return VectorData(
            name=self.name,
            fids=self.fids[keep],
            columns={n: c.take(rows) for n, c in self.columns.items()},
            geometries=geometries,
            crs=self.crs,
            fid_field=self.fid_field,
            dropped_parts=self.dropped_parts,
            skipped_columns=list(self.skipped_columns),
        )


def concat_geometries(parts: list[tuple[Geometries, int]]) -> Geometries:
    """Geometries of one kind one after another; each with its row offset."""
    kind = parts[0][0].kind
    coords, rings, part_rings, feature_parts, rows = [], [], [], [], []
    with_m = any(geoms.measures is not None for geoms, _offset in parts)
    measures = [
        geoms.measures if geoms.measures is not None else np.full(len(geoms.coords), np.nan)
        for geoms, _offset in parts
    ]
    for geoms, row_offset in parts:
        coords.append(geoms.coords)
        rings.append(np.diff(geoms.ring_offsets))
        part_rings.append(np.diff(geoms.part_offsets))
        feature_parts.append(np.diff(geoms.feature_offsets))
        rows.append(geoms.rows + row_offset)
    return Geometries(
        kind=kind,
        coords=np.concatenate(coords),
        ring_offsets=_offsets(np.concatenate(rings)),
        part_offsets=_offsets(np.concatenate(part_rings)),
        feature_offsets=_offsets(np.concatenate(feature_parts)),
        rows=np.concatenate(rows),
        measures=np.concatenate(measures) if with_m else None,
        measure_name=next((g.measure_name for g, _o in parts if g.measure_name), ""),
    )


def _concat_column(name: str, pieces: list[tuple[Column | None, int]]) -> Column:
    """One column from pieces (None where a layer lacks it: null rows)."""
    types = {c.type for c, _n in pieces if c is not None}
    if len(types) == 1:
        (ctype,) = types
    elif types <= {ColumnType.INT, ColumnType.FLOAT, ColumnType.BOOL}:
        ctype = ColumnType.FLOAT
    else:
        ctype = ColumnType.STRING
    values: list[object] = []
    for column, n in pieces:
        if column is None:
            values.extend([None] * n)
        else:
            values.extend(column.python_value(i) for i in range(n))
    if ctype is ColumnType.STRING:
        values = [None if v is None else str(v) for v in values]
    built = column_from_values(name, values)
    if built is not None and built.type is ctype:
        return built
    null = np.fromiter((v is None for v in values), bool, len(values))
    mask = null if null.any() else None
    if ctype is ColumnType.STRING:
        return Column(name, ctype, ["" if v is None else str(v) for v in values], mask)
    fill = {ColumnType.FLOAT: np.nan, ColumnType.INT: 0, ColumnType.BOOL: False}[ctype]
    dtype = {ColumnType.FLOAT: np.float64, ColumnType.INT: np.int64, ColumnType.BOOL: bool}[ctype]
    arr = np.asarray([fill if v is None else v for v in values], dtype=dtype)
    return Column(name, ctype, arr, mask)


def concat(datas: list[VectorData], name: str) -> VectorData:
    """Layers one after another, with the union of their columns (null where absent).

    Fids are kept when they stay distinct, otherwise renumbered from 1.
    """
    if len(datas) == 1:
        data = datas[0]
        return VectorData(name, data.fids, data.columns, data.geometries, data.crs, data.fid_field)
    offsets = np.cumsum([0] + [d.n_rows for d in datas])
    fids = np.concatenate([d.fids for d in datas]) if datas else np.zeros(0, np.int64)
    if len(np.unique(fids)) != len(fids):
        fids = np.arange(1, len(fids) + 1, dtype=np.int64)
    names: dict[str, None] = {}
    for data in datas:
        names.update(dict.fromkeys(data.columns))
    columns = {n: _concat_column(n, [(d.columns.get(n), d.n_rows) for d in datas]) for n in names}
    geometries = {}
    for kind in Kind:
        parts = [
            (d.geometries[kind], int(offsets[i]))
            for i, d in enumerate(datas)
            if kind in d.geometries and d.geometries[kind].n_features
        ]
        if parts:
            geometries[kind] = concat_geometries(parts)
    return VectorData(name, fids, columns, geometries, datas[0].crs if datas else None)  # type: ignore[arg-type]


# --- building --------------------------------------------------------------------


class GeometryBuilder:
    """Collects geometries feature by feature, then makes the arrays once.

    Coordinates arrive as ``(k, 2)`` or ``(k, 3)`` arrays (one per ring); they
    are only concatenated in :meth:`finish`.
    """

    def __init__(self) -> None:
        self._chunks: dict[Kind, list[np.ndarray]] = {k: [] for k in Kind}
        self._ring_lengths: dict[Kind, list[int]] = {k: [] for k in Kind}
        self._part_rings: dict[Kind, list[int]] = {k: [] for k in Kind}
        self._feature_parts: dict[Kind, list[int]] = {k: [] for k in Kind}
        self._rows: dict[Kind, list[int]] = {k: [] for k in Kind}

    def _begin_part(self, kind: Kind, row: int) -> None:
        rows = self._rows[kind]
        if rows and rows[-1] == row:
            self._feature_parts[kind][-1] += 1
        else:
            rows.append(row)
            self._feature_parts[kind].append(1)

    def _ring(self, kind: Kind, coords: np.ndarray) -> None:
        self._chunks[kind].append(coords)
        self._ring_lengths[kind].append(len(coords))

    def add_points(self, row: int, coords: np.ndarray) -> None:
        """One or more points (a Point or a MultiPoint) of feature ``row``."""
        for point in _as_xyz(coords):
            self._begin_part(Kind.POINTS, row)
            self._ring(Kind.POINTS, point[None, :])
            self._part_rings[Kind.POINTS].append(1)

    def add_line(self, row: int, coords: np.ndarray) -> None:
        self._begin_part(Kind.LINES, row)
        self._ring(Kind.LINES, _as_xyz(coords))
        self._part_rings[Kind.LINES].append(1)

    def add_polygon(self, row: int, rings: list[np.ndarray]) -> None:
        """One polygon: the exterior ring, then its holes."""
        if not rings:
            return
        self._begin_part(Kind.POLYGONS, row)
        for ring in rings:
            self._ring(Kind.POLYGONS, _as_xyz(ring))
        self._part_rings[Kind.POLYGONS].append(len(rings))

    def finish(self) -> dict[Kind, Geometries]:
        out = {}
        for kind in Kind:
            if not self._rows[kind]:
                continue
            chunks = self._chunks[kind]
            out[kind] = Geometries(
                kind=kind,
                coords=np.concatenate(chunks) if chunks else np.zeros((0, 3)),
                ring_offsets=_offsets(self._ring_lengths[kind]),
                part_offsets=_offsets(self._part_rings[kind]),
                feature_offsets=_offsets(self._feature_parts[kind]),
                rows=np.asarray(self._rows[kind], dtype=np.int64),
            )
        return out


def _as_xyz(coords: np.ndarray) -> np.ndarray:
    coords = np.asarray(coords, dtype=np.float64)
    if coords.ndim == 1:
        coords = coords[None, :]
    if coords.shape[1] == 3:
        return coords
    out = np.full((len(coords), 3), np.nan)
    out[:, : min(coords.shape[1], 3)] = coords[:, :3]
    return out


def _offsets(lengths: object) -> np.ndarray:
    lengths = np.asarray(lengths, dtype=np.int64)
    out = np.zeros(len(lengths) + 1, dtype=np.int64)
    np.cumsum(lengths, out=out[1:])
    return out


def offsets_from_lengths(lengths: object) -> np.ndarray:
    """``[0, l0, l0 + l1, ...]`` as int64."""
    return _offsets(lengths)


def segment_indices(starts: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """``concatenate([arange(s, s + n) for s, n in zip(starts, lengths)])``, vectorised."""
    starts = np.asarray(starts, dtype=np.int64)
    lengths = np.asarray(lengths, dtype=np.int64)
    total = int(lengths.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64)
    nonzero = lengths > 0
    starts, lengths = starts[nonzero], lengths[nonzero]
    ends = np.cumsum(lengths)
    out = np.ones(total, dtype=np.int64)
    out[0] = starts[0]
    # At each segment start, jump from the previous segment's last index.
    out[ends[:-1]] = starts[1:] - (starts[:-1] + lengths[:-1] - 1)
    return np.cumsum(out)


# --- attribute columns from Python values -------------------------------------------


def column_from_values(name: str, values: list[object], source_type: str = "") -> Column | None:
    """A column from one Python value per row (None for null), with its type inferred.

    All bools: BOOL. Ints (bools count as ints) in int32: INT; other numbers:
    FLOAT; anything else: STRING (dicts and lists as JSON). None if every value
    is null or a bytes blob (skipped).
    """
    import json

    present = [v for v in values if v is not None]
    if not present or all(isinstance(v, bytes | bytearray | memoryview) for v in present):
        return None
    null = np.fromiter((v is None for v in values), dtype=bool, count=len(values))
    null_mask = null if null.any() else None
    if all(isinstance(v, bool) for v in present):
        arr = np.fromiter((bool(v) if v is not None else False for v in values), bool, len(values))
        return Column(name, ColumnType.BOOL, arr, null_mask, source_type)
    if all(isinstance(v, int) for v in present):  # bool is an int
        ints = [int(v) if v is not None else 0 for v in values]
        if min(ints) >= INT32_MIN and max(ints) <= INT32_MAX:
            arr = np.asarray(ints, dtype=np.int64)
            return Column(name, ColumnType.INT, arr, null_mask, source_type)
    if all(isinstance(v, int | float) for v in present):
        arr = np.fromiter(
            (float(v) if v is not None else np.nan for v in values), np.float64, len(values)
        )
        return Column(name, ColumnType.FLOAT, arr, null_mask, source_type)

    def text(v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, bytes | bytearray | memoryview):
            return bytes(v).hex()
        if isinstance(v, dict | list):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, bool):
            return "true" if v else "false"
        return str(v)

    return Column(name, ColumnType.STRING, [text(v) for v in values], null_mask, source_type)
