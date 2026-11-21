"""Neighbourhoods on a 3D surface: ball queries, area-weighted fits and the resolution rule.

bpy-free, numpy only (Blender bundles no SciPy). The 3D surface metrics
(curvature, TPI, roughness, slope at a stated scale, discontinuity sets) are
statistics of the surface inside a ball around a point. Unlike the presets'
Window Heights and Mean Rise, which read heights with vertical rays, a ball
works the same on a wall, under an overhang or on a ceiling. This module finds
what each ball holds and sums it.

**Samples are faces** (:func:`face_samples`). Each face is a sample at its
area-weighted centre, weighted by its area, with its unit normal, its mean
edge length and its own *spread*: the second moment of the face about its
centre. For a triangle that is ``(1/12) Σ (v − c)(v − c)ᵀ`` over its corners,
exactly the mean of ``(p − c)(p − c)ᵀ`` over its area. A fit that adds the
spread sees each face as the surface it covers rather than a point, so large
faces on a plane add spread along the plane and none across it, and the result
depends less on how the surface was triangulated. A face is in a ball when its
centre is. Faces about as large as the ball are what the resolution rule
catches.

**Ball queries** (:func:`ball_pairs`) use a voxel hash (:func:`grid`): the
samples sorted by cell, cells of side at least the radius, so a ball lies
within the 27 cells around its centre's cell. Candidate pairs are expanded and
filtered in chunks of about ``max_pairs``, each query whole in one chunk, so
memory stays bounded and per-query statistics (the weighted median) need no
second pass. Queries are taken cell by cell and the samples are read from
copies sorted by cell, one column per value, so a chunk reads a few runs of
consecutive rows (1-D takes, several times faster than gathering rows of an
``(n, 3)`` array). Each query's pairs arrive as one run.

**The normal filter.** With query normals, a sample counts only when
``n_sample · n_query > min_cos`` (default 0: faces turned more than 90° from
the query's normal are left out). A ball on one side of a thin lip, or of a
narrow cleft, then does not average in the other side.

**Fits.** :func:`ball_stats` sums, per query, the area, the area-weighted
offsets and their second moments (plus the spread), the vector area
``Σ a n`` and the area-weighted median edge length. :func:`plane` turns them
into a least-squares plane: its normal is the eigenvector of the smallest
eigenvalue of the covariance, turned to the side of the vector area, and the
RMS distance of the surface from it is that eigenvalue's square root. The RMS
is over surface area, as from a uniformly dense point cloud, not over plan
area as a profilometer's Rq: on steep micro-relief the flanks hold more surface
than the crests, so it reads lower (a sinusoid of amplitude 3 mm and
wavelength 1 cm: 9 % below A/√2). Plan area has no meaning under an overhang.
:func:`quadric` fits ``w = a u² + b uv + c v² + d u + e v + f`` in a frame
given by a normal per query (face centres, weighted by area, no spread).

**The resolution rule** (:func:`resolution`), for a ball of diameter
``s = 2r``. A value at that scale counts only where:

* **spacing**: ``s ≥ k h``, with h the area-weighted median edge length in
  the ball (k = :data:`SPACING_FACTOR`, 5). That puts about 20 samples across
  the ball, and at five spacings the presets' curvature facet bias is about
  1 % (``docs/analysis-presets.md``);
* **coverage**: ``|Σ a n| ≥ c π r²`` (c = :data:`MIN_COVERAGE`, 0.8).
  ``Σ a n`` over a surface patch depends only on the patch's boundary (it is
  the patch's vector area), so a patch that fills the ball gives about
  ``π r²`` however rough it is. Holes, the mesh's edge and faces dropped by
  the normal filter lower it;
* **no open edge**: no boundary edge (a hole's rim or the end of the scan)
  within ``s/4`` (:data:`EDGE_FRACTION`) of the point (:func:`edge_distance`,
  measured once per mesh for every scale).

Each failed test sets one bit of the status; 0 means resolved.

Loops here run over chunks and over value columns, never over samples or
queries. ``scripts/bench_neighbourhood.py`` times each step at a chosen size.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

MAX_PAIRS = 1 << 18
"""Candidate pairs per chunk of :func:`ball_pairs` (a query with more is a chunk alone).
Small enough that the chunk's arrays are reused from the heap rather than mapped
afresh: at 2^22 the page faults alone made it four times slower."""
CHUNK = 1 << 20
"""Faces or queries per block in the array passes, to bound the temporary arrays."""

SPACING_FACTOR = 5.0
"""A scale s is resolved where s ≥ this × the area-weighted median edge length in the ball."""
MIN_COVERAGE = 0.8
"""A ball is covered when its vector area is at least this share of π r²."""
EDGE_FRACTION = 0.25
"""No open edge may lie within this share of the scale s (the ball's diameter)."""

RESOLVED = 0
COARSE = 1
"""Status bit: the mesh is too coarse for the scale (spacing test)."""
SPARSE = 2
"""Status bit: the surface fills too little of the ball (coverage test)."""
EDGE = 4
"""Status bit: an open edge (hole or the end of the scan) is too close."""
EMPTY = 8
"""Status bit: fewer than three samples in the ball."""

_MIN_SAMPLES = 3

_OFFSETS = np.array(
    [(i, j, k) for i in (-1, 0, 1) for j in (-1, 0, 1) for k in (-1, 0, 1)], dtype=np.int64
)
_PAIRS_3 = ((0, 0), (1, 1), (2, 2), (0, 1), (0, 2), (1, 2))
"""The six distinct entries of a symmetric 3 × 3 matrix, in the spread's order."""
_COLUMNS = 13
"""Per-pair sums of :func:`ball_stats`: area, 3 first moments, 6 second, 3 vector area."""
_PAD = 2
"""Cells of padding on each side of the grid, so a neighbour of any query cell
within one cell of the samples has a key of its own (no wrap-around)."""


# --- samples ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Samples:
    """A surface as weighted samples, one per face."""

    points: np.ndarray
    """``(S, 3)`` float64, the area-weighted centre of each face."""
    area: np.ndarray
    """``(S,)`` float64, each face's area."""
    normal: np.ndarray
    """``(S, 3)`` float32 unit normals (zero for a face of no area)."""
    size: np.ndarray
    """``(S,)`` float32, each face's mean edge length."""
    spread: np.ndarray | None
    """``(S, 6)`` float64 second moment of each face about its centre, per unit
    area, as ``xx, yy, zz, xy, xz, yz``; None to treat faces as points. Float64:
    in float32 its rounding alone reads as a roughness of about 10⁻⁵ × the face size."""

    def __len__(self) -> int:
        return len(self.area)


def _triangles(face_starts: np.ndarray, corner_verts: np.ndarray, f0: int, f1: int):
    """Faces ``f0:f1`` as a fan of triangles: ``(tri (T, 3) vertex ids, face (T,) local ids)``."""
    starts = face_starts[f0 : f1 + 1]
    sizes = np.diff(starts)
    if (sizes == 3).all():
        tri = corner_verts[starts[0] : starts[-1]].reshape(-1, 3)
        return tri, np.arange(f1 - f0, dtype=np.int64)
    count = np.maximum(sizes - 2, 0)
    face = np.repeat(np.arange(f1 - f0, dtype=np.int64), count)
    first = np.repeat(starts[:-1], count)
    step = np.arange(len(face), dtype=np.int64) - np.repeat(np.cumsum(count) - count, count)
    tri = np.stack(
        [corner_verts[first], corner_verts[first + step + 1], corner_verts[first + step + 2]],
        axis=1,
    )
    return tri, face


def face_samples(
    positions: np.ndarray,
    face_starts: np.ndarray,
    corner_verts: np.ndarray,
    *,
    spread: bool = True,
    chunk: int = CHUNK,
) -> Samples:
    """One sample per face of a mesh (``positions (V, 3)``, ``face_starts (F + 1,)``,
    ``corner_verts (C,)``, Blender's layout). Polygons are taken as fans of
    triangles from their first corner. Computed ``chunk`` faces at a time."""
    positions = np.asarray(positions, dtype=np.float64)
    face_starts = np.asarray(face_starts, dtype=np.int64)
    corner_verts = np.asarray(corner_verts, dtype=np.int64)
    n_faces = len(face_starts) - 1
    points = np.zeros((n_faces, 3), dtype=np.float64)
    area = np.zeros(n_faces, dtype=np.float64)
    normal = np.zeros((n_faces, 3), dtype=np.float32)
    size = np.zeros(n_faces, dtype=np.float32)
    second = np.zeros((n_faces, 6), dtype=np.float64) if spread else None
    for f0 in range(0, n_faces, chunk):
        f1 = min(f0 + chunk, n_faces)
        m = f1 - f0
        tri, face = _triangles(face_starts, corner_verts, f0, f1)
        p0 = positions[tri[:, 0]]
        e1 = positions[tri[:, 1]] - p0
        e2 = positions[tri[:, 2]] - p0
        vec = 0.5 * np.cross(e1, e2)
        a = np.sqrt(np.einsum("ij,ij->i", vec, vec))
        centre = (e1 + e2) / 3.0  # each triangle's centre, from its first corner
        face_vec = np.stack([np.bincount(face, vec[:, k], minlength=m) for k in range(3)], 1)
        face_area = np.bincount(face, a, minlength=m)
        # Area-weighted centre, from the face's first corner (no large-coordinate loss).
        anchor = positions[corner_verts[face_starts[f0:f1]]]
        rel = centre + (p0 - anchor[face])
        with np.errstate(invalid="ignore", divide="ignore"):
            moment = np.stack([np.bincount(face, a * rel[:, k], minlength=m) for k in range(3)], 1)
            c = np.where(face_area[:, None] > 0, moment / face_area[:, None], 0.0)
            length = np.sqrt(np.einsum("ij,ij->i", face_vec, face_vec))
            normal[f0:f1] = np.where(length[:, None] > 0, face_vec / length[:, None], 0.0)
        points[f0:f1] = anchor + c
        area[f0:f1] = face_area
        starts = face_starts[f0 : f1 + 1]
        sizes = np.diff(starts)
        corners = np.arange(starts[0], starts[-1], dtype=np.int64)
        nxt = corners + 1
        last = starts[1:] - 1
        nxt[last - starts[0]] = starts[:-1]
        edge = positions[corner_verts[nxt]] - positions[corner_verts[corners]]
        perimeter = np.add.reduceat(
            np.sqrt(np.einsum("ij,ij->i", edge, edge)), starts[:-1] - starts[0]
        )
        size[f0:f1] = perimeter / sizes
        if second is not None:
            # Each triangle's own moment (1/12) Σ (v − g)(v − g)ᵀ, moved to the face centre.
            d = np.stack([-centre, e1 - centre, e2 - centre], axis=1)  # corners less the centre
            shift = rel - c[face]
            cols = []
            for i, j in _PAIRS_3:
                own = np.einsum("tk,tk->t", d[:, :, i], d[:, :, j]) / 12.0
                cols.append(np.bincount(face, a * (own + shift[:, i] * shift[:, j]), minlength=m))
            with np.errstate(invalid="ignore", divide="ignore"):
                s = np.stack(cols, 1)
                second[f0:f1] = np.where(face_area[:, None] > 0, s / face_area[:, None], 0.0)
    return Samples(points=points, area=area, normal=normal, size=size, spread=second)


def open_edges(face_starts: np.ndarray, corner_verts: np.ndarray) -> np.ndarray:
    """``(E, 2)`` the vertex pairs of the edges with exactly one face: a hole's rim or
    the end of the scan. Edges are matched by vertex ids, so unwelded seams count."""
    face_starts = np.asarray(face_starts, dtype=np.int64)
    corner_verts = np.asarray(corner_verts, dtype=np.int64)
    if len(corner_verts) == 0:
        return np.zeros((0, 2), dtype=np.int64)
    corners = np.arange(len(corner_verts), dtype=np.int64)
    nxt = corners + 1
    nxt[face_starts[1:] - 1] = face_starts[:-1]
    a, b = corner_verts, corner_verts[nxt]
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    n = int(corner_verts.max()) + 1
    key, count = np.unique(lo * n + hi, return_counts=True)
    single = key[count == 1]
    return np.stack([single // n, single % n], axis=1)


def edge_points(positions: np.ndarray, edges: np.ndarray, spacing: float) -> np.ndarray:
    """Points along ``edges`` (``(E, 2)`` vertex ids) no more than ``spacing`` apart,
    ends included, so a ball query against them finds an edge to within ``spacing / 2``."""
    if spacing <= 0:
        raise ValueError("The spacing must be positive.")
    positions = np.asarray(positions, dtype=np.float64)
    edges = np.asarray(edges, dtype=np.int64).reshape(-1, 2)
    p, q = positions[edges[:, 0]], positions[edges[:, 1]]
    length = np.sqrt(np.einsum("ij,ij->i", q - p, q - p))
    steps = np.maximum(np.ceil(length / spacing).astype(np.int64), 1)
    edge = np.repeat(np.arange(len(edges), dtype=np.int64), steps + 1)
    k = np.arange(len(edge), dtype=np.int64) - np.repeat(
        np.cumsum(steps + 1) - (steps + 1), steps + 1
    )
    t = (k / np.repeat(steps, steps + 1))[:, None]
    return p[edge] + t * (q[edge] - p[edge])


def edge_distance(
    positions: np.ndarray,
    face_starts: np.ndarray,
    corner_verts: np.ndarray,
    points: np.ndarray,
    max_distance: float,
    *,
    resolution: float | None = None,
) -> np.ndarray:
    """``(P,)`` distance from each point to the nearest open edge, ``inf`` beyond
    ``max_distance``. The edges are sampled every ``resolution`` (default
    ``max_distance / 16``), so a distance is at most ``resolution / 2`` too long.
    Computed once per mesh, it gives the edge test of every scale:
    ``edge_distance(...) < EDGE_FRACTION × s``."""
    step = max_distance / 16 if resolution is None else resolution
    rim = edge_points(positions, open_edges(face_starts, corner_verts), step)
    return nearest(rim, points, max_distance)[1]


# --- the voxel hash and ball queries ----------------------------------------------------


@dataclass(frozen=True)
class Grid:
    """Points sorted into cubic cells (:func:`grid`)."""

    cell: float
    origin: np.ndarray
    """``(3,)`` the corner of cell (0, 0, 0)."""
    extent: np.ndarray
    """``(3,)`` int64, cells along each axis that hold points."""
    dims: np.ndarray
    """``(3,)`` int64, ``extent`` plus the padding: the key space."""
    keys: np.ndarray
    """``(K,)`` int64, the occupied cells' keys, ascending."""
    starts: np.ndarray
    """``(K + 1,)`` int64, where each occupied cell's points start in ``order``."""
    order: np.ndarray
    """``(N,)`` int64, point ids cell by cell."""

    def cells(self, points: np.ndarray) -> np.ndarray:
        """``(n, 3)`` int64 cell index of each point (may lie outside the extent)."""
        return np.floor((np.asarray(points, dtype=np.float64) - self.origin) / self.cell).astype(
            np.int64
        )

    def key(self, ijk: np.ndarray) -> np.ndarray:
        d = self.dims
        return ((ijk[:, 0] + _PAD) * d[1] + (ijk[:, 1] + _PAD)) * d[2] + (ijk[:, 2] + _PAD)


def grid(points: np.ndarray, cell: float) -> Grid:
    """Sort ``points`` into cubes of side ``cell``. A ball query of radius up to
    ``cell`` needs only the 27 cells around its centre's."""
    if not (cell > 0 and math.isfinite(cell)):
        raise ValueError("The cell size must be a positive number.")
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(points) == 0:
        origin = np.zeros(3)
        extent = np.zeros(3, dtype=np.int64)
    else:
        if not np.isfinite(points).all():
            raise ValueError("Points must be finite.")
        origin = points.min(axis=0)
        extent = np.floor((points.max(axis=0) - origin) / cell).astype(np.int64) + 1
    dims = extent + 2 * _PAD
    if float(np.prod(dims.astype(np.float64))) >= 2.0**62:
        raise ValueError("The cell size is too small for the points' extent.")
    g = Grid(
        cell=float(cell),
        origin=origin,
        extent=extent,
        dims=dims,
        keys=np.zeros(0, dtype=np.int64),
        starts=np.zeros(1, dtype=np.int64),
        order=np.zeros(0, dtype=np.int64),
    )
    if len(points) == 0:
        return g
    key = g.key(g.cells(points))
    order = np.argsort(key, kind="stable")
    keys, first = np.unique(key[order], return_index=True)
    starts = np.append(first, len(order)).astype(np.int64)
    return Grid(
        cell=g.cell, origin=origin, extent=extent, dims=dims, keys=keys, starts=starts, order=order
    )


@dataclass(frozen=True)
class Pairs:
    """One chunk of :func:`ball_pairs`: every sample within the radius of each of its queries."""

    queries: np.ndarray
    """``(m,)`` int64, the global ids of this chunk's queries. Each query is whole in one chunk."""
    query: np.ndarray
    """``(p,)`` int64, the local index (into ``queries``) of each pair's query,
    non-decreasing: each query's pairs are one run."""
    point: np.ndarray
    """``(p,)`` int64, each pair's sample id."""
    rank: np.ndarray
    """``(p,)`` int64, each pair's sample's position in the grid's ``order``
    (``point == g.order[rank]``): an index into arrays sorted by cell, which
    neighbouring queries read close together."""
    offset: np.ndarray
    """``(p, 3)`` float64, the sample less the query position (column-major: each
    coordinate is contiguous)."""

    def runs(self) -> tuple[np.ndarray, np.ndarray]:
        """``(starts, local)``: where each query's run of pairs starts, and that query's
        local index, for ``np.add.reduceat``."""
        q = self.query
        if len(q) == 0:
            return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
        starts = np.flatnonzero(np.concatenate([[True], q[1:] != q[:-1]]))
        return starts, q[starts]


def _columns(cols: list[np.ndarray]) -> np.ndarray:
    """``(p, k)`` view of k equal-length arrays, each column contiguous."""
    out = np.empty((len(cols), len(cols[0])))
    for k, c in enumerate(cols):
        out[k] = c
    return out.T


def ball_pairs(
    g: Grid,
    points: np.ndarray,
    queries: np.ndarray,
    radius: float,
    *,
    point_normals: np.ndarray | None = None,
    query_normals: np.ndarray | None = None,
    min_cos: float = 0.0,
    max_pairs: int = MAX_PAIRS,
) -> Iterator[Pairs]:
    """Chunks of the pairs (query, sample) with ``|sample − query| ≤ radius``.

    ``g`` is ``grid(points, cell)`` with ``cell ≥ radius``. With both normal
    arrays, a pair also needs ``n_sample · n_query > min_cos``. Queries with no
    sample in range appear in no chunk.
    """
    if not (0 < radius <= g.cell * (1 + 1e-12)):
        raise ValueError("The radius must be positive and at most the grid's cell size.")
    points = np.asarray(points, dtype=np.float64)
    queries = np.asarray(queries, dtype=np.float64).reshape(-1, 3)
    filtered = point_normals is not None and query_normals is not None
    if len(queries) == 0 or len(g.keys) == 0:
        return
    ijk = g.cells(queries)
    inside = ((ijk >= -1) & (ijk <= g.extent)).all(axis=1)
    ids = np.flatnonzero(inside)
    if len(ids) == 0:
        return
    qkey = g.key(ijk[ids])
    ucell, inv = np.unique(qkey, return_inverse=True)
    d = g.dims
    step = (_OFFSETS[:, 0] * d[1] + _OFFSETS[:, 1]) * d[2] + _OFFSETS[:, 2]
    near_keys = ucell[:, None] + step[None, :]
    slot = np.minimum(np.searchsorted(g.keys, near_keys), len(g.keys) - 1)
    hit = g.keys[slot] == near_keys
    start = np.where(hit, g.starts[slot], 0)
    count = np.where(hit, g.starts[slot + 1] - g.starts[slot], 0)
    del near_keys, slot, hit
    # Queries cell by cell: neighbouring queries read neighbouring samples.
    by_cell = np.argsort(inv, kind="stable")
    ids, inv = ids[by_cell], inv[by_cell]
    per_query = count.sum(axis=1)[inv]
    keep = per_query > 0
    ids, inv, per_query = ids[keep], inv[keep], per_query[keep]
    if len(ids) == 0:
        return
    first = np.cumsum(per_query) - per_query
    chunk_of = first // max(int(max_pairs), 1)
    bounds = np.concatenate([[0], np.flatnonzero(np.diff(chunk_of)) + 1, [len(ids)]])
    r2 = float(radius) ** 2
    # Samples in cell order, one contiguous column per coordinate: a query's
    # candidates are a few runs of consecutive rows, and 1-D takes are several
    # times faster than gathering rows of an (n, 3) array.
    cols = [np.ascontiguousarray(points[g.order, k]) for k in range(3)]
    ncols = (
        [np.ascontiguousarray(point_normals[g.order, k], dtype=np.float32) for k in range(3)]
        if filtered and point_normals is not None
        else None
    )
    for c0, c1 in itertools.pairwise(bounds):
        cells = inv[c0:c1]
        st = start[cells].ravel()
        ct = count[cells].ravel()
        total = int(ct.sum())
        pos = np.repeat(st - (np.cumsum(ct) - ct), ct) + np.arange(total, dtype=np.int64)
        local = np.repeat(np.arange(c1 - c0, dtype=np.int64), per_query[c0:c1])
        qid = ids[c0:c1]
        qp = queries[qid]
        off = [np.take(cols[k], pos) - np.take(qp[:, k], local) for k in range(3)]
        ok = off[0] * off[0] + off[1] * off[1] + off[2] * off[2] <= r2
        if ncols is not None and query_normals is not None:
            qn = np.asarray(query_normals[qid], dtype=np.float32)
            cos = sum(np.take(ncols[k], pos) * np.take(qn[:, k], local) for k in range(3))
            ok &= cos > min_cos
        keep = np.flatnonzero(ok)
        pos = np.take(pos, keep)
        yield Pairs(
            queries=qid,
            query=np.take(local, keep),
            point=np.take(g.order, pos),
            rank=pos,
            offset=_columns([np.take(o, keep) for o in off]),
        )


def near(
    points: np.ndarray,
    queries: np.ndarray,
    radius: float,
    *,
    g: Grid | None = None,
    max_pairs: int = MAX_PAIRS,
) -> np.ndarray:
    """``(Q,)`` bool: is any of ``points`` within ``radius`` of each query?"""
    queries = np.asarray(queries, dtype=np.float64).reshape(-1, 3)
    found = np.zeros(len(queries), dtype=bool)
    if g is None:
        g = grid(points, radius)
    for chunk in ball_pairs(g, points, queries, radius, max_pairs=max_pairs):
        found[chunk.queries[chunk.runs()[1]]] = True
    return found


def nearest(
    points: np.ndarray,
    queries: np.ndarray,
    radius: float,
    *,
    g: Grid | None = None,
    max_pairs: int = MAX_PAIRS,
) -> tuple[np.ndarray, np.ndarray]:
    """The nearest of ``points`` to each query within ``radius``: ``(index (Q,) int64,
    -1 where none; distance (Q,) float64, inf where none)``. Ties go to the lower index."""
    queries = np.asarray(queries, dtype=np.float64).reshape(-1, 3)
    index = np.full(len(queries), -1, dtype=np.int64)
    dist2 = np.full(len(queries), np.inf)
    if g is None:
        g = grid(points, radius)
    for chunk in ball_pairs(g, points, queries, radius, max_pairs=max_pairs):
        starts, local = chunk.runs()
        if len(starts) == 0:
            continue
        d2 = np.einsum("ij,ij->i", chunk.offset, chunk.offset)
        run = np.repeat(np.arange(len(starts)), np.diff(np.append(starts, len(d2))))
        best = np.minimum.reduceat(d2, starts)
        tied = np.where(d2 == best[run], chunk.point, np.iinfo(np.int64).max)
        index[chunk.queries[local]] = np.minimum.reduceat(tied, starts)
        dist2[chunk.queries[local]] = best
    return index, np.sqrt(dist2)


def thin(points: np.ndarray, spacing: float) -> np.ndarray:
    """Ids of a subset of ``points``, one per cube of side ``spacing`` (the point nearest
    the cube's centre; ties to the lower id), ascending. Query points at about
    ``spacing`` apart, for values that :func:`nearest` then carries to every face."""
    points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(points) == 0:
        return np.zeros(0, dtype=np.int64)
    g = grid(points, spacing)
    ijk = g.cells(points)
    centre = g.origin + (ijk + 0.5) * spacing
    d2 = np.einsum("ij,ij->i", points - centre, points - centre)
    order = np.lexsort((np.arange(len(points)), d2, g.key(ijk)))
    _, first = np.unique(g.key(ijk)[order], return_index=True)
    return np.sort(order[first])


# --- statistics in a ball ----------------------------------------------------------------


@dataclass(frozen=True)
class Ball:
    """Area-weighted statistics of the samples in each query's ball (:func:`ball_stats`).

    Offsets are from the query point, in float64, so precision does not depend on
    how far the scene is from its origin. Empty balls hold zeros and NaN.
    """

    radius: float
    count: np.ndarray
    """``(Q,)`` int64, samples in the ball."""
    area: np.ndarray
    """``(Q,)`` float64, their total area."""
    centre: np.ndarray
    """``(Q, 3)`` area-weighted mean position, less the query point."""
    cov: np.ndarray
    """``(Q, 3, 3)`` area-weighted covariance about ``centre`` (with each face's spread)."""
    vector_area: np.ndarray
    """``(Q, 3)`` ``Σ a n``: the patch's vector area."""
    size: np.ndarray
    """``(Q,)`` area-weighted median edge length; NaN for an empty ball."""

    @property
    def coverage(self) -> np.ndarray:
        """``|Σ a n| / π r²``: about 1 where the surface fills the ball, less with holes or edges."""
        return np.sqrt(np.einsum("ij,ij->i", self.vector_area, self.vector_area)) / (
            math.pi * self.radius**2
        )


def ball_stats(
    samples: Samples,
    queries: np.ndarray,
    radius: float,
    *,
    g: Grid | None = None,
    query_normals: np.ndarray | None = None,
    min_cos: float = 0.0,
    max_pairs: int = MAX_PAIRS,
) -> Ball:
    """Area-weighted statistics of the samples within ``radius`` of each query.

    ``g`` is ``grid(samples.points, cell)`` with ``cell ≥ radius`` (made when
    None). With ``query_normals`` only samples whose normal is within
    ``acos(min_cos)`` of the query's count.

    Per chunk, the samples' values are read from copies sorted by cell (one
    packed row per pair) and summed over each query's run of pairs with one
    ``np.add.reduceat``.
    """
    queries = np.asarray(queries, dtype=np.float64).reshape(-1, 3)
    n = len(queries)
    if g is None:
        g = grid(samples.points, radius)
    count = np.zeros(n, dtype=np.int64)
    sums = np.zeros((n, _COLUMNS))
    size = np.full(n, np.nan)
    # Per-sample values in cell order, one column each: area, area x normal,
    # area x spread, and each sample's rank by edge length.
    order = g.order
    area = samples.area[order]
    extra = [samples.normal[order, k] * area for k in range(3)]
    if samples.spread is not None:
        extra += [samples.spread[order, k] * area for k in range(6)]
    by_size = np.argsort(samples.size, kind="stable")
    size_rank = np.empty(len(order), dtype=np.int64)
    size_rank[by_size] = np.arange(len(order), dtype=np.int64)
    size_rank = size_rank[order]
    sizes = samples.size[order]
    del by_size
    pairs = ball_pairs(
        g,
        samples.points,
        queries,
        radius,
        point_normals=samples.normal if query_normals is not None else None,
        query_normals=query_normals,
        min_cos=min_cos,
        max_pairs=max_pairs,
    )
    total = max(len(order), 1)
    for chunk in pairs:
        if len(chunk.rank) == 0:
            continue
        m = len(chunk.queries)
        loc, rank, o = chunk.query, chunk.rank, chunk.offset
        a = np.take(area, rank)
        ao = [a * o[:, k] for k in range(3)]
        cols = [a, *ao]
        cols += [ao[i] * o[:, j] for i, j in _PAIRS_3]
        got = [np.take(x, rank) for x in extra]
        cols += got[:3]
        if samples.spread is not None:
            for k in range(6):
                cols[4 + k] = cols[4 + k] + got[3 + k]
        per = np.bincount(loc, minlength=m)
        has = np.flatnonzero(per)
        q = chunk.queries[has]
        for k, c in enumerate(cols):
            sums[q, k] = np.bincount(loc, c, minlength=m)[has]
        count[q] = per[has]
        # Area-weighted median edge length: each run sorted by size (one integer sort),
        # then the first sample whose running area reaches half the run's.
        ends = np.cumsum(per)[has]
        starts = ends - per[has]
        by = np.argsort(loc * total + np.take(size_rank, rank))
        run = np.cumsum(a[by])
        base = np.where(starts > 0, run[np.maximum(starts - 1, 0)], 0.0)
        at = np.searchsorted(run, base + 0.5 * sums[q, 0], side="left")
        at = np.clip(at, starts, ends - 1)
        size[q] = sizes[rank[by[at]]]
    s0 = sums[:, 0]
    with np.errstate(invalid="ignore", divide="ignore"):
        centre = np.where(s0[:, None] > 0, sums[:, 1:4] / s0[:, None], 0.0)
        mom = np.where(s0[:, None] > 0, sums[:, 4:10] / s0[:, None], np.nan)
    cov = np.empty((n, 3, 3))
    for col, (i, j) in enumerate(_PAIRS_3):
        cov[:, i, j] = mom[:, col] - centre[:, i] * centre[:, j]
        cov[:, j, i] = cov[:, i, j]
    return Ball(
        radius=float(radius),
        count=count,
        area=s0,
        centre=centre,
        cov=cov,
        vector_area=sums[:, 10:13],
        size=size,
    )


@dataclass(frozen=True)
class Plane:
    """Area-weighted least-squares planes (:func:`plane`). NaN where a ball has fewer
    than three samples."""

    normal: np.ndarray
    """``(Q, 3)`` unit normal, on the side of the ball's vector area."""
    point: np.ndarray
    """``(Q, 3)`` a point on the plane: the ball's area-weighted centre."""
    rms: np.ndarray
    """``(Q,)`` RMS distance of the surface in the ball from the plane."""
    eigenvalues: np.ndarray
    """``(Q, 3)`` the covariance's eigenvalues, ascending."""


def plane(ball: Ball, queries: np.ndarray, *, chunk: int = CHUNK) -> Plane:
    """The least-squares plane of each ball: the eigenvector of the smallest eigenvalue."""
    queries = np.asarray(queries, dtype=np.float64).reshape(-1, 3)
    n = len(queries)
    normal = np.full((n, 3), np.nan)
    values = np.full((n, 3), np.nan)
    ok = (ball.count >= _MIN_SAMPLES) & (ball.area > 0) & np.isfinite(ball.cov).all(axis=(1, 2))
    idx = np.flatnonzero(ok)
    for c0 in range(0, len(idx), chunk):
        part = idx[c0 : c0 + chunk]
        w, v = np.linalg.eigh(ball.cov[part])
        nrm = v[:, :, 0]
        side = np.einsum("ij,ij->i", nrm, ball.vector_area[part])
        nrm = np.where((side < 0)[:, None], -nrm, nrm)
        normal[part] = nrm
        values[part] = w
    rms = np.sqrt(np.maximum(values[:, 0], 0.0))
    point = np.where(ok[:, None], queries + ball.centre, np.nan)
    return Plane(normal=normal, point=point, rms=rms, eigenvalues=values)


def resolution(
    ball: Ball,
    *,
    edge: np.ndarray | None = None,
    spacing_factor: float = SPACING_FACTOR,
    min_coverage: float = MIN_COVERAGE,
) -> np.ndarray:
    """``(Q,)`` uint8 status of each ball at the scale ``s = 2 r``: 0 resolved, else the
    bits :data:`COARSE`, :data:`SPARSE`, :data:`EDGE` and :data:`EMPTY`.

    ``edge`` marks the queries with an open edge within ``EDGE_FRACTION × s``
    (``edge_distance(...) < EDGE_FRACTION * 2 * r``).
    """
    s = 2.0 * ball.radius
    status = np.zeros(len(ball.count), dtype=np.uint8)
    status[ball.count < _MIN_SAMPLES] |= EMPTY
    with np.errstate(invalid="ignore"):
        coarse = ~(s >= spacing_factor * ball.size)
    status[coarse] |= COARSE
    status[ball.coverage < min_coverage] |= SPARSE
    if edge is not None:
        status[np.asarray(edge, dtype=bool)] |= EDGE
    return status


# --- quadric fits ------------------------------------------------------------------------


def tangent_frames(normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors ``(u, v)`` per normal such that ``(u, v, n)`` is right-handed.

    ``u`` is horizontal where the normal is not vertical (along the contour,
    ``z × n`` normalised), else +X, so frames are stable on walls and ceilings.
    """
    n = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    u = np.stack([-n[:, 1], n[:, 0], np.zeros(len(n))], axis=1)
    length = np.sqrt(np.einsum("ij,ij->i", u, u))
    flat = length < 1e-9
    u[flat] = (1.0, 0.0, 0.0)
    length[flat] = 1.0
    u /= length[:, None]
    u -= np.einsum("ij,ij->i", u, n)[:, None] * n
    u /= np.sqrt(np.einsum("ij,ij->i", u, u))[:, None]
    v = np.cross(n, u)
    return u, v


@dataclass(frozen=True)
class Quadric:
    """Least-squares quadrics ``w = a u² + b uv + c v² + d u + e v + f`` in each query's
    frame (:func:`quadric`). NaN where the fit is singular (fewer than six samples
    or all on a line)."""

    coef: np.ndarray
    """``(Q, 6)`` ``a, b, c, d, e, f`` in metres (``a, b, c`` in 1/m)."""
    rms: np.ndarray
    """``(Q,)`` area-weighted RMS residual along ``w``."""
    u: np.ndarray
    v: np.ndarray
    n: np.ndarray


_QUAD_TERMS = 6
_UPPER = [(i, j) for i in range(_QUAD_TERMS) for j in range(i, _QUAD_TERMS)]


def quadric(
    samples: Samples,
    queries: np.ndarray,
    radius: float,
    normals: np.ndarray,
    *,
    g: Grid | None = None,
    min_cos: float | None = 0.0,
    max_pairs: int = MAX_PAIRS,
) -> Quadric:
    """Fit a quadric to the face centres within ``radius`` of each query, weighted by
    area, in the frame of ``normals`` (:func:`tangent_frames`; w along the normal,
    origin at the query). ``min_cos`` filters samples against those normals (None:
    no filter). Coordinates are scaled by the radius for the solve."""
    queries = np.asarray(queries, dtype=np.float64).reshape(-1, 3)
    n_dir = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    nq = len(queries)
    if g is None:
        g = grid(samples.points, radius)
    u_ax, v_ax = tangent_frames(n_dir)
    mat = np.zeros((nq, len(_UPPER)))
    rhs = np.zeros((nq, _QUAD_TERMS))
    ww = np.zeros(nq)
    wsum = np.zeros(nq)
    count = np.zeros(nq, dtype=np.int64)
    pairs = ball_pairs(
        g,
        samples.points,
        queries,
        radius,
        point_normals=samples.normal if min_cos is not None else None,
        query_normals=n_dir if min_cos is not None else None,
        min_cos=min_cos if min_cos is not None else 0.0,
        max_pairs=max_pairs,
    )
    area = samples.area[g.order]
    for chunk in pairs:
        if len(chunk.rank) == 0:
            continue
        m = len(chunk.queries)
        loc, q, o = chunk.query, chunk.queries, chunk.offset

        def along(axes: np.ndarray, o=o, loc=loc, q=q) -> np.ndarray:
            ax = axes[q]
            return (
                o[:, 0] * np.take(ax[:, 0], loc)
                + o[:, 1] * np.take(ax[:, 1], loc)
                + o[:, 2] * np.take(ax[:, 2], loc)
            )

        a = np.take(area, chunk.rank)
        uu = along(u_ax) / radius
        vv = along(v_ax) / radius
        w = along(n_dir)
        # The six monomials; the last is 1, so its products are skipped.
        terms = (uu * uu, uu * vv, vv * vv, uu, vv)
        weighted = [a * t for t in terms] + [a]
        per = np.bincount(loc, minlength=m)
        has = np.flatnonzero(per)
        qh = q[has]
        for col, (i, j) in enumerate(_UPPER):
            x = weighted[i] if j == _QUAD_TERMS - 1 else weighted[i] * terms[j]
            mat[qh, col] = np.bincount(loc, x, minlength=m)[has]
        for i in range(_QUAD_TERMS):
            rhs[qh, i] = np.bincount(loc, weighted[i] * w, minlength=m)[has]
        ww[qh] = np.bincount(loc, a * w * w, minlength=m)[has]
        wsum[qh] = np.bincount(loc, a, minlength=m)[has]
        count[qh] = per[has]
    full = np.zeros((nq, _QUAD_TERMS, _QUAD_TERMS))
    for col, (i, j) in enumerate(_UPPER):
        full[:, i, j] = mat[:, col]
        full[:, j, i] = mat[:, col]
    coef = np.full((nq, _QUAD_TERMS), np.nan)
    ok = (count >= _QUAD_TERMS) & (wsum > 0)
    idx = np.flatnonzero(ok)
    if len(idx):
        scale = np.maximum(np.abs(np.diagonal(full[idx], axis1=1, axis2=2)).max(axis=1), 1e-300)
        cond = np.linalg.cond(full[idx] / scale[:, None, None])
        good = idx[np.isfinite(cond) & (cond < 1e12)]
        if len(good):
            coef[good] = np.linalg.solve(full[good], rhs[good][:, :, None])[:, :, 0]
    with np.errstate(invalid="ignore", divide="ignore"):
        resid = (ww - np.einsum("ij,ij->i", np.nan_to_num(coef), rhs)) / wsum
    rms = np.where(np.isfinite(coef).all(axis=1), np.sqrt(np.maximum(resid, 0.0)), np.nan)
    # Undo the scaling: u_true = r u, so a_true = a / r², d_true = d / r.
    coef[:, :3] /= radius**2
    coef[:, 3:5] /= radius
    return Quadric(coef=coef, rms=rms, u=u_ax, v=v_ax, n=n_dir)
