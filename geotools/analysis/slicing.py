"""Contours and sections: a triangle mesh cut by planes, vectorised (bpy-free numpy).

**Contours** are horizontal cuts at ``base + k x interval`` in the project's
height datum. Levels are real-world heights (RL); the mesh is local, so a level
is compared with the local z plus the project's binary64 offset A. **Sections**
are vertical cuts along each segment of a polyline, kept within the segment,
with chainage (plan distance along the polyline) on every point.

Both use :func:`plane_cuts`: for each triangle and plane it crosses, the two
points where the plane meets the triangle's edges. A vertex counts as above a
plane when its signed distance is >= the plane's, so every triangle that
crosses a plane gives exactly one segment and a vertex exactly on the plane is
no special case. A crossing point is identified by the mesh edge it lies on
(its two vertex ids) and the plane, or by the vertex when it falls exactly on
one, so the two triangles of an edge share it and :mod:`.polylines` can chain
the segments. Faces with more than three corners come in as Blender's loop
triangles; their diagonals are edges like any other.

See docs/design/m5-analysis.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import polylines

MAX_LEVELS = 20_000
"""More contour levels than this is a mistake (an interval far too small)."""


class SliceError(ValueError):
    """The cut cannot be made as asked (bad interval, too many levels, no line)."""


def _unique_ids(*columns: np.ndarray) -> tuple[np.ndarray, int]:
    """Dense ids of the rows of the int64 ``columns``, and how many there are."""
    n = len(columns[0])
    if n == 0:
        return np.zeros(0, dtype=np.int64), 0
    order = np.lexsort(columns[::-1])
    new = np.zeros(n, dtype=bool)
    new[0] = True
    for column in columns:
        c = column[order]
        new[1:] |= c[1:] != c[:-1]
    ids = np.empty(n, dtype=np.int64)
    ids[order] = np.cumsum(new) - 1
    return ids, int(new.sum())


def _distinct_segments(ends: np.ndarray) -> np.ndarray:
    """``(s, 2)`` point ids with each undirected segment once, first occurrence kept.

    A mesh edge lying exactly in a plane with both its triangles on the side
    below it (a ridge at a contour level, a box corner on a section plane)
    is cut by both triangles; it is one segment of the line.
    """
    if not len(ends):
        return ends
    lo = np.minimum(ends[:, 0], ends[:, 1])
    hi = np.maximum(ends[:, 0], ends[:, 1])
    ids, _count = _unique_ids(lo, hi)
    _, first = np.unique(ids, return_index=True)
    return ends[np.sort(first)]


@dataclass(frozen=True, slots=True)
class Cuts:
    """Segments where planes cross triangles.

    Point ``j`` of segment ``i`` (``j`` = 0, 1) lies on the triangle edge from
    vertex ``va[i, j]`` to ``vb[i, j]`` at fraction ``t[i, j]`` (``va == vb`` and
    ``t == 0`` when it is exactly on vertex ``va``).
    """

    triangle: np.ndarray  # (s,) int64
    plane: np.ndarray  # (s,) int64: index into the planes
    va: np.ndarray  # (s, 2) int64
    vb: np.ndarray  # (s, 2) int64
    t: np.ndarray  # (s, 2) float64

    def points(self, positions: np.ndarray) -> np.ndarray:
        """``(s, 2, 3)`` float64 points."""
        pa = positions[self.va]
        pb = positions[self.vb]
        return pa + self.t[..., None] * (pb - pa)

    def point_keys(self) -> tuple[np.ndarray, ...]:
        """Three int64 columns per point (flattened ``(2s,)``) that identify it."""
        va = self.va.ravel()
        vb = self.vb.ravel()
        lo = np.minimum(va, vb)
        hi = np.maximum(va, vb)
        plane = np.repeat(self.plane, 2)
        return plane, lo, hi


def plane_cuts(
    distance: np.ndarray,
    triangles: np.ndarray,
    levels: np.ndarray,
    triangle_levels: tuple[np.ndarray, np.ndarray] | None = None,
) -> Cuts:
    """Where the planes ``distance == levels[k]`` cross the triangles.

    ``distance`` is each vertex's signed distance (binary64), ``triangles``
    ``(T, 3)`` vertex ids and ``levels`` sorted ascending. ``triangle_levels``
    optionally gives the first and last level index each triangle may cross
    (default: worked out with ``searchsorted``).
    """
    triangles = np.asarray(triangles, dtype=np.int64)
    levels = np.asarray(levels, dtype=np.float64)
    d = distance[triangles]
    if triangle_levels is None:
        low = d.min(axis=1)
        high = d.max(axis=1)
        # Planes with low < level <= high: the triangle has vertices on both sides.
        first = np.searchsorted(levels, low, side="right")
        last = np.searchsorted(levels, high, side="right") - 1
    else:
        first, last = triangle_levels
    count = np.maximum(last - first + 1, 0)
    tri = np.repeat(np.arange(len(triangles), dtype=np.int64), count)
    plane = polylines.segment_ranges(first[count > 0], count[count > 0])
    if not len(tri):
        e2 = np.zeros((0, 2), dtype=np.int64)
        return Cuts(tri, plane, e2, e2.copy(), np.zeros((0, 2)))
    dv = d[tri]  # (s, 3)
    level = levels[plane][:, None]
    above = dv >= level
    # Edges 0-1, 1-2, 2-0; exactly two have their ends on different sides.
    ia = np.array([0, 1, 2])
    ib = np.array([1, 2, 0])
    crosses = above[:, ia] != above[:, ib]  # (s, 3)
    # The two crossing edges, in edge order.
    which = np.argsort(~crosses, axis=1, kind="stable")[:, :2]  # (s, 2)
    ea = ia[which]
    eb = ib[which]
    rows = np.arange(len(tri))[:, None]
    a_above = above[rows, ea]
    lower = np.where(a_above, eb, ea)
    upper = np.where(a_above, ea, eb)
    dl = dv[rows, lower]
    du = dv[rows, upper]
    # From the upper end: t = 0 exactly when the upper vertex is on the plane.
    t = (du - level) / (du - dl)
    tri_v = triangles[tri]
    vu = tri_v[rows, upper]
    vl = tri_v[rows, lower]
    on_vertex = t == 0.0
    vl = np.where(on_vertex, vu, vl)
    return Cuts(tri, plane, vu, vl, t)


# --- contours ----------------------------------------------------------------------------


def contour_levels(
    z_min: float, z_max: float, interval: float, base: float = 0.0
) -> tuple[np.ndarray, int]:
    """Levels ``base + k x interval`` from ``z_min`` to ``z_max`` (heights in the datum).

    Returns the levels and the index ``k`` of the first one.
    """
    if not (math.isfinite(interval) and interval > 0):
        raise SliceError("The contour interval must be a positive number.")
    if not (math.isfinite(z_min) and math.isfinite(z_max)):
        raise SliceError("The surface has no finite heights.")
    k0 = math.ceil((z_min - base) / interval)
    k1 = math.floor((z_max - base) / interval)
    if k1 - k0 + 1 > MAX_LEVELS:
        raise SliceError(
            f"{k1 - k0 + 1:,} contour levels from {z_min:.3f} to {z_max:.3f} at "
            f"{interval:g}: choose a larger interval (at most {MAX_LEVELS:,} levels)."
        )
    k = np.arange(k0, k1 + 1, dtype=np.int64)
    return base + k * interval, k0


@dataclass(frozen=True, slots=True)
class Lines:
    """Polylines with their points, ready for a mesh of loose edges.

    ``positions`` are local (the mesh's frame), binary64. ``edges`` are the
    polyline steps in order, so consecutive edges of one line join end to end.
    Per-line arrays have one row per polyline; ``edge_line`` maps edges to them.
    """

    positions: np.ndarray  # (p, 3)
    edges: np.ndarray  # (e, 2)
    edge_line: np.ndarray  # (e,)
    closed: np.ndarray  # (l,)
    length: np.ndarray  # (l,) 3D length
    point_values: dict[str, np.ndarray]
    line_values: dict[str, np.ndarray]

    @property
    def count(self) -> int:
        return len(self.closed)


def lines_from_chains(
    positions: np.ndarray, chains: polylines.Chains, point_values=None, line_values=None
) -> Lines:
    """:class:`Lines` of ``chains`` over ``positions`` (each line gets its own copies of
    its points); ``point_values`` are per point id, ``line_values`` per chain."""
    used = chains.nodes
    # Renumber so each polyline has its own points in order (a closed line
    # reuses its first point).
    lengths = chains.lengths()
    closed = chains.closed
    own = lengths - closed.astype(np.int64)
    new_start = np.zeros(chains.count + 1, dtype=np.int64)
    new_start[1:] = np.cumsum(own)
    take = polylines.segment_ranges(chains.offsets[:-1], own)
    pos = positions[used[take]]
    steps = lengths - 1
    line = np.repeat(np.arange(chains.count), steps)
    first = new_start[:-1][line]
    k = np.arange(int(steps.sum())) - np.repeat(np.cumsum(steps) - steps, steps)
    a = first + k
    b = first + k + 1
    wrap = closed[line] & (k == steps[line] - 1)
    b = np.where(wrap, first, b)
    edges = np.stack([a, b], axis=1)
    seg = np.linalg.norm(pos[b] - pos[a], axis=1)
    length = np.bincount(line, weights=seg, minlength=chains.count)
    pv = {name: values[used[take]] for name, values in (point_values or {}).items()}
    return Lines(pos, edges, line, closed, length, pv, dict(line_values or {}))


def contours(
    positions: np.ndarray,
    triangles: np.ndarray,
    interval: float,
    *,
    base: float = 0.0,
    datum: float = 0.0,
    major_every: int = 5,
) -> Lines:
    """Contour polylines of a triangle mesh.

    ``positions`` are local binary64 (the float32 mesh positions, in the
    contours' frame); a point's height in the datum is ``z + datum`` (the
    project's offset A). Levels are ``base + k x interval`` in the datum. Each
    line carries ``elevation`` (the level, binary64), ``index`` (k) and
    ``major`` (k divisible by ``major_every``; never when 0).
    """
    positions = np.asarray(positions, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    if not len(triangles):
        raise SliceError("The surface has no faces.")
    z = positions[:, 2] + datum
    used = z[triangles]
    levels, k0 = contour_levels(float(used.min()), float(used.max()), interval, base)
    cuts = plane_cuts(z, triangles, levels)
    plane, lo, hi = cuts.point_keys()
    ids, count = _unique_ids(plane, lo, hi)
    ids = ids.reshape(-1, 2)
    pts = cuts.points(positions).reshape(-1, 3)
    point_pos = np.empty((count, 3), dtype=np.float64)
    point_pos[ids.ravel()] = pts
    # Exact heights: the level, in the local frame.
    point_level = np.empty(count, dtype=np.int64)
    point_level[ids.ravel()] = np.repeat(cuts.plane, 2)
    point_pos[:, 2] = levels[point_level] - datum
    ids = _distinct_segments(ids)
    chains = polylines.chain(count, ids[:, 0], ids[:, 1])
    level_of_line = point_level[chains.nodes[chains.offsets[:-1]]]
    k = k0 + level_of_line
    major = (k % major_every == 0) if major_every > 0 else np.zeros(len(k), dtype=bool)
    return lines_from_chains(
        point_pos,
        chains,
        line_values={"elevation": levels[level_of_line], "index": k, "major": major},
    )


# --- sections ----------------------------------------------------------------------------


def sections(
    positions: np.ndarray,
    triangles: np.ndarray,
    path: np.ndarray,
    path_offsets: np.ndarray,
    path_closed: np.ndarray | None = None,
) -> Lines:
    """Vertical sections of a triangle mesh along polylines.

    ``path`` holds the section lines' points (local, only x and y are used),
    polyline ``i`` being ``path[path_offsets[i]:path_offsets[i + 1]]`` (a
    closed one repeats its first point). Each line segment cuts the mesh with
    the vertical plane through it, kept between its ends; the pieces of
    consecutive segments join at the vertex between them. Every point gets
    ``chainage`` (plan distance along its section line from its start) and every
    line ``section`` (the polyline's index), ``chainage_from`` and ``chainage_to``.
    A multi-level surface (an overhang, a bridge, a closed object) gives
    several pieces, all kept.
    """
    positions = np.asarray(positions, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64)
    path = np.asarray(path, dtype=np.float64)
    offsets = np.asarray(path_offsets, dtype=np.int64)
    if not len(triangles):
        raise SliceError("The surface has no faces.")
    n_lines = len(offsets) - 1
    # Segments of every polyline, with the chainage at their start.
    seg_line = np.repeat(np.arange(n_lines), np.maximum(np.diff(offsets) - 1, 0))
    seg_start = polylines.segment_ranges(offsets[:-1], np.maximum(np.diff(offsets) - 1, 0))
    p0 = path[seg_start, :2]
    p1 = path[seg_start + 1, :2]
    length = np.linalg.norm(p1 - p0, axis=1)
    good = length > 0
    seg_line, seg_start, p0, p1, length = (
        seg_line[good],
        seg_start[good],
        p0[good],
        p1[good],
        length[good],
    )
    if not len(seg_line):
        raise SliceError("The section line has no length.")
    before = np.cumsum(length) - length
    first_seg = np.full(n_lines, -1)
    first_seg[seg_line[::-1]] = np.arange(len(seg_line))[::-1]
    chain_start = before - before[first_seg[seg_line]]

    xy = positions[:, :2]
    tri_lo = xy[triangles].min(axis=1)
    tri_hi = xy[triangles].max(axis=1)
    all_pos: list[np.ndarray] = []
    all_keys: list[tuple[np.ndarray, ...]] = []
    all_chainage: list[np.ndarray] = []
    all_line: list[np.ndarray] = []
    pairs: list[np.ndarray] = []
    base = 0
    for s in range(len(seg_line)):  # one numpy pass per section segment, not per element
        direction = (p1[s] - p0[s]) / length[s]
        normal = np.array([-direction[1], direction[0]])
        lo = np.minimum(p0[s], p1[s])
        hi = np.maximum(p0[s], p1[s])
        near = np.flatnonzero(((tri_hi >= lo) & (tri_lo <= hi)).all(axis=1))
        if not len(near):
            continue
        dist = (xy - p0[s]) @ normal
        cuts = plane_cuts(dist, triangles[near], np.zeros(1))
        if not len(cuts.triangle):
            continue
        pts = cuts.points(positions)  # (c, 2, 3)
        u = (pts[..., :2] - p0[s]) @ direction  # (c, 2)
        # Points this close to a polyline vertex's vertical line are on it, so
        # the two segments meeting there agree on them despite rounding.
        tol = 1e-9 * max(1.0, length[s], float(np.abs(p0[s]).max()))
        lo_u = u.min(axis=1)
        hi_u = u.max(axis=1)
        # Keep pieces with some length inside; a vertical one (a wall) has one u.
        inside = ((hi_u > tol) & (lo_u < length[s] - tol)) | (
            (hi_u - lo_u <= tol) & (hi_u >= -tol) & (hi_u <= length[s] + tol)
        )
        pts, u = pts[inside], u[inside]
        tri = near[cuts.triangle[inside]]
        va, vb = cuts.va[inside], cuts.vb[inside]
        # Point identity, four int64 columns: (0, segment, lo, hi) for a mesh
        # edge crossing; (1, polyline vertex, triangle, 0) for an end clipped
        # inside a triangle and (2, polyline vertex, lo, hi) for an edge
        # crossing on the vertex's line, both shared with the next segment.
        kind = np.zeros(va.shape, dtype=np.int64)
        col_a = np.full(va.shape, s, dtype=np.int64)
        col_b = np.minimum(va, vb)
        col_c = np.maximum(va, vb)
        for bound, vertex_index in ((0.0, seg_start[s]), (length[s], seg_start[s] + 1)):
            on = np.abs(u - bound) <= tol
            kind[on] = 2
            col_a[on] = vertex_index
            u[on] = bound
            out = (u < bound - tol) if bound == 0.0 else (u > bound + tol)
            rows = np.flatnonzero(out.any(axis=1))
            if not len(rows):
                continue
            j = np.argmax(out[rows], axis=1)  # the end outside
            other = 1 - j
            ua = u[rows, j]
            ub = u[rows, other]
            f = (bound - ub) / (ua - ub)
            pa = pts[rows, j]
            pb = pts[rows, other]
            pts[rows, j] = pb + f[:, None] * (pa - pb)
            u[rows, j] = bound
            kind[rows, j] = 1
            col_a[rows, j] = vertex_index
            col_b[rows, j] = tri[rows]
            col_c[rows, j] = 0
        all_keys.append((kind.ravel(), col_a.ravel(), col_b.ravel(), col_c.ravel()))
        all_pos.append(pts.reshape(-1, 3))
        all_chainage.append((chain_start[s] + u).ravel())
        all_line.append(np.full(2 * len(tri), seg_line[s], dtype=np.int64))
        pairs.append(base + np.arange(2 * len(tri)).reshape(-1, 2))
        base += 2 * len(tri)
    if not all_pos:
        raise SliceError("The section line does not cross the surface.")
    keys = [np.concatenate(k) for k in zip(*all_keys, strict=True)]
    ids, count = _unique_ids(*keys)
    flat_pos = np.concatenate(all_pos)
    point_pos = np.empty((count, 3))
    point_pos[ids] = flat_pos
    chainage = np.empty(count)
    chainage[ids] = np.concatenate(all_chainage)
    line_of_point = np.empty(count, dtype=np.int64)
    line_of_point[ids] = np.concatenate(all_line)
    seg_pairs = _distinct_segments(ids[np.concatenate(pairs)])
    chains = polylines.chain(count, seg_pairs[:, 0], seg_pairs[:, 1])
    # Run each piece the way its section line goes.
    first = chainage[chains.nodes[chains.offsets[:-1]]]
    last = chainage[chains.nodes[chains.offsets[1:] - 1]]
    chains = polylines.reverse(chains, (first > last) & ~chains.closed)
    node_chain = chainage[chains.nodes]
    poly = np.repeat(np.arange(chains.count), chains.lengths())
    c_lo = np.full(chains.count, np.inf)
    c_hi = np.full(chains.count, -np.inf)
    np.minimum.at(c_lo, poly, node_chain)
    np.maximum.at(c_hi, poly, node_chain)
    section = line_of_point[chains.nodes[chains.offsets[:-1]]]
    return lines_from_chains(
        point_pos,
        chains,
        point_values={"chainage": chainage},
        line_values={"section": section, "chainage_from": c_lo, "chainage_to": c_hi},
    )
