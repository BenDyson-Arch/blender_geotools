"""Vector geometries to mesh arrays and back, without Python loops over mesh elements.

To a mesh (import):

- polygons become faces: one n-gon per polygon without holes, triangles from
  ``tessellate`` (``mathutils.geometry.tessellate_polygon`` in Blender) per
  polygon with holes. Each polygon is the only loop over parts;
- lines become loose edges, a closed line a closed loop of edges;
- points become loose vertices.

Ring clean-up follows 0.2 (CHANGELOG 0.2.0): consecutive duplicate points and
the closing point are dropped, and a ring with fewer than three distinct
points (or no area) is degenerate. A polygon whose exterior is degenerate is
skipped (counted in ``dropped_parts``) instead of failing the import; a
degenerate hole is ignored. Exteriors are wound counter-clockwise, so faces
point up.

Coordinates become float32 project-frame positions only at the end:
``local = (E - offset_E, N - offset_N, Z - offset_A)``, computed in binary64.
A missing Z gives z = 0.

From a mesh (export): faces grouped by their ``fid`` become polygons, rebuilt
from the boundary edges of each group (holes included); loose edges become
lines and loose vertices points. Positions go back to binary64 before the
offset is added.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .model import (
    Column,
    ColumnType,
    Geometries,
    Kind,
    VectorData,
    offsets_from_lengths,
    segment_indices,
)

Tessellator = Callable[[list[np.ndarray]], np.ndarray]
"""Triangulates one polygon: rings ``(k, 2)`` (exterior first) -> ``(t, 3)`` indices
into the rings' points taken in order."""

Vec3 = tuple[float, float, float]


# --- binary64 shifting ------------------------------------------------------------


def to_local(coords: np.ndarray, offset: Vec3) -> np.ndarray:
    """``(n, 3)`` binary64 source coordinates to float32 project-frame positions.

    The subtraction is done in binary64; only the result is rounded to float32.
    A NaN (missing) Z becomes 0.
    """
    out = np.empty(coords.shape, dtype=np.float64)
    out[:, 0] = coords[:, 0] - float(offset[0])
    out[:, 1] = coords[:, 1] - float(offset[1])
    z = coords[:, 2]
    out[:, 2] = np.where(np.isnan(z), 0.0, z - float(offset[2]))
    return out.astype(np.float32)


def from_local(positions: np.ndarray, offset: Vec3) -> np.ndarray:
    """float32 (or binary64) project-frame positions to binary64 source coordinates."""
    out = np.asarray(positions, dtype=np.float64).copy()
    out += np.asarray(offset, dtype=np.float64)
    return out


# --- to a mesh ----------------------------------------------------------------------


@dataclass(slots=True)
class MeshArrays:
    """One geometry kind as Blender mesh arrays."""

    kind: Kind
    positions: np.ndarray  # (v, 3) float32
    edges: np.ndarray = field(default_factory=lambda: np.zeros((0, 2), dtype=np.int32))
    loop_starts: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))
    loop_vertices: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int32))
    element_feature: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    """Per face (polygons), edge (lines) or vertex (points): the feature index in the kind."""
    vertex_feature: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    dropped_parts: int = 0

    @property
    def n_elements(self) -> int:
        return len(self.element_feature)


def _ring_ids(ring_offsets: np.ndarray) -> np.ndarray:
    return np.repeat(np.arange(len(ring_offsets) - 1), np.diff(ring_offsets))


def _clean_rings(
    xy: np.ndarray, ring_offsets: np.ndarray, closed: bool
) -> tuple[np.ndarray, np.ndarray]:
    """Drop consecutive duplicate points, and (``closed``) a closing point.

    Returns the keep mask and the kept length of each ring.
    """
    n = len(xy)
    lengths = np.diff(ring_offsets)
    keep = np.ones(n, dtype=bool)
    if n > 1:
        keep[1:] = ~(xy[1:] == xy[:-1]).all(axis=1)
    nonempty = lengths > 0
    keep[ring_offsets[:-1][nonempty]] = True
    if closed and n:
        first = ring_offsets[:-1][nonempty]
        last = ring_offsets[1:][nonempty] - 1
        index = np.where(keep, np.arange(n), -1)
        last_kept = np.maximum.reduceat(index, first)
        ends_on_first = (xy[last] == xy[first]).all(axis=1) & (last_kept != first)
        keep[last_kept[ends_on_first]] = False
    counts = np.concatenate(([0], np.cumsum(keep)))
    return keep, counts[ring_offsets[1:]] - counts[ring_offsets[:-1]]


def _distinct_counts(xy: np.ndarray, ring: np.ndarray, n_rings: int) -> np.ndarray:
    """Distinct XY points per ring."""
    if not len(xy):
        return np.zeros(n_rings, dtype=np.int64)
    order = np.lexsort((xy[:, 1], xy[:, 0], ring))
    r, s = ring[order], xy[order]
    new = np.ones(len(order), dtype=bool)
    new[1:] = (r[1:] != r[:-1]) | (s[1:] != s[:-1]).any(axis=1)
    return np.bincount(r[new], minlength=n_rings)


def _signed_areas(xy: np.ndarray, ring_offsets: np.ndarray) -> np.ndarray:
    """Shoelace area of each ring (positive counter-clockwise), relative to its first point."""
    n_rings = len(ring_offsets) - 1
    if not len(xy):
        return np.zeros(n_rings)
    ring = _ring_ids(ring_offsets)
    lengths = np.diff(ring_offsets)
    first = np.repeat(ring_offsets[:-1], lengths)
    rel = xy - xy[first]
    nxt = np.arange(len(xy)) + 1
    ends = ring_offsets[1:][lengths > 0] - 1
    nxt[ends] = ring_offsets[:-1][lengths > 0]
    term = rel[:, 0] * rel[nxt, 1] - rel[nxt, 0] * rel[:, 1]
    return np.bincount(ring, weights=term, minlength=n_rings) / 2.0


def _reversed_within(offsets: np.ndarray, flip: np.ndarray) -> np.ndarray:
    """Index permutation that reverses the segments of ``offsets`` where ``flip``."""
    lengths = np.diff(offsets)
    index = np.arange(int(offsets[-1]))
    starts = np.repeat(offsets[:-1], lengths)
    ends = np.repeat(offsets[1:] - 1, lengths)
    flipped = np.repeat(flip, lengths)
    index[flipped] = starts[flipped] + ends[flipped] - index[flipped]
    return index


def polygon_mesh(geoms: Geometries, offset: Vec3, tessellate: Tessellator) -> MeshArrays:
    """Faces for every polygon of ``geoms``."""
    coords = geoms.coords
    xy = coords[:, :2]
    ro = geoms.ring_offsets
    n_rings = geoms.n_rings
    ring_part = geoms.ring_part()
    part_feature = geoms.part_feature()
    keep, kept_len = _clean_rings(xy, ro, closed=True)

    kept_ro = offsets_from_lengths(kept_len)
    kept_xy = xy[keep]
    kept_ring = _ring_ids(kept_ro)
    distinct = _distinct_counts(kept_xy, kept_ring, n_rings)
    area = _signed_areas(kept_xy, kept_ro)
    ring_valid = (kept_len >= 3) & (distinct >= 3) & (area != 0)

    part_first_ring = geoms.part_offsets[:-1]
    exterior = np.zeros(n_rings, dtype=bool)
    exterior[part_first_ring[part_first_ring < n_rings]] = True
    part_valid = ring_valid[part_first_ring] if n_rings else np.zeros(0, bool)
    ring_used = ring_valid & part_valid[ring_part]
    holes_per_part = np.bincount(ring_part[ring_used & ~exterior], minlength=geoms.n_parts)
    holed = part_valid & (holes_per_part > 0)

    # Vertices: the kept points of the used rings, in file order.
    used_point = np.repeat(ring_used, kept_len)
    vertex_xyz = coords[keep][used_point]
    used_len = np.where(ring_used, kept_len, 0)
    vro = offsets_from_lengths(used_len)  # vertex offsets per ring
    positions = to_local(vertex_xyz, offset)
    vertex_feature = part_feature[ring_part[_ring_ids(vro)]]

    # N-gons: the exterior of each valid part without holes, wound counter-clockwise.
    ngon_ring = part_first_ring[part_valid & ~holed]
    ngon_len = used_len[ngon_ring]
    loops = segment_indices(vro[ngon_ring], ngon_len)
    loop_ro = offsets_from_lengths(ngon_len)
    loops = loops[_reversed_within(loop_ro, area[ngon_ring] < 0)]
    face_part = [ring_part[ngon_ring]]
    face_size = [ngon_len]
    face_loops = [loops]

    # Polygons with holes: tessellated, one call per polygon.
    tris_all = []
    tri_part = []
    vertex_xy = vertex_xyz[:, :2]
    for part in np.flatnonzero(holed).tolist():
        rings = np.arange(geoms.part_offsets[part], geoms.part_offsets[part + 1])
        rings = rings[ring_used[rings]]
        base = vertex_xy[vro[rings[0]]]  # relative to the first point, for precision
        ring_xy = [vertex_xy[vro[r] : vro[r + 1]] - base for r in rings.tolist()]
        tris = np.asarray(tessellate(ring_xy), dtype=np.int64).reshape(-1, 3)
        vertex_ids = segment_indices(vro[rings], used_len[rings])
        tris_all.append(vertex_ids[tris])
        tri_part.append(np.full(len(tris), part, dtype=np.int64))
    if tris_all:
        tris = np.concatenate(tris_all)
        p = positions.astype(np.float64)
        a, b, c = p[tris[:, 0], :2], p[tris[:, 1], :2], p[tris[:, 2], :2]
        cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (
            c[:, 0] - a[:, 0]
        )
        clockwise = cross < 0
        tris[clockwise] = tris[clockwise][:, ::-1]
        face_part.append(np.concatenate(tri_part))
        face_size.append(np.full(len(tris), 3, dtype=np.int64))
        face_loops.append(tris.ravel())

    # Faces in part order (so each feature's faces are contiguous).
    fp = np.concatenate(face_part)
    fs = np.concatenate(face_size)
    fl = np.concatenate(face_loops)
    order = np.argsort(fp, kind="stable")
    src_off = offsets_from_lengths(fs)
    loop_order = segment_indices(src_off[:-1][order], fs[order])
    sizes = fs[order]
    return MeshArrays(
        kind=Kind.POLYGONS,
        positions=positions,
        loop_starts=offsets_from_lengths(sizes)[:-1].astype(np.int32),
        loop_vertices=fl[loop_order].astype(np.int32),
        element_feature=part_feature[fp[order]],
        vertex_feature=vertex_feature,
        dropped_parts=int((~part_valid).sum()),
    )


def line_mesh(geoms: Geometries, offset: Vec3) -> MeshArrays:
    """Loose edges for every line of ``geoms``; a closed line is a closed loop."""
    coords = geoms.coords
    xy = coords[:, :2]
    ro = geoms.ring_offsets
    ring_part = geoms.ring_part()
    part_feature = geoms.part_feature()
    lengths = np.diff(ro)
    keep, kept_len = _clean_rings(xy, ro, closed=False)
    # A closed line: last kept point equals the first; drop it and close with an edge.
    nonempty = lengths > 0
    closed = np.zeros(len(lengths), dtype=bool)
    first = ro[:-1][nonempty]
    last = ro[1:][nonempty] - 1
    closed[nonempty] = (xy[first] == xy[last]).all(axis=1) & (kept_len[nonempty] >= 4)
    if closed.any():
        index = np.where(keep, np.arange(len(xy)), -1)
        last_kept = np.maximum.reduceat(index, first) if len(first) else first
        keep[last_kept[closed[nonempty]]] = False
        kept_len = kept_len - closed
    valid = kept_len >= 2
    used_point = keep & np.repeat(valid, lengths)
    used_len = np.where(valid, kept_len, 0)
    vro = offsets_from_lengths(used_len)
    positions = to_local(coords[used_point], offset)
    vertex_ring = _ring_ids(vro)
    n = len(positions)
    is_last = np.zeros(n, dtype=bool)
    is_last[vro[1:][valid] - 1] = True
    starts = np.flatnonzero(~is_last)
    open_edges = np.stack([starts, starts + 1], axis=1)
    open_ring = vertex_ring[starts]
    closing = np.flatnonzero(closed & valid)
    close_edges = np.stack([vro[1:][closing] - 1, vro[:-1][closing]], axis=1)
    edges = np.concatenate([open_edges, close_edges])
    edge_ring = np.concatenate([open_ring, closing])
    order = np.argsort(edge_ring, kind="stable")
    return MeshArrays(
        kind=Kind.LINES,
        positions=positions,
        edges=edges[order].astype(np.int32),
        element_feature=part_feature[ring_part[edge_ring[order]]],
        vertex_feature=part_feature[ring_part[vertex_ring]],
        dropped_parts=int((~valid).sum()),
    )


def point_mesh(geoms: Geometries, offset: Vec3) -> MeshArrays:
    """One loose vertex per point."""
    feature = geoms.coord_feature()
    return MeshArrays(
        kind=Kind.POINTS,
        positions=to_local(geoms.coords, offset),
        element_feature=feature,
        vertex_feature=feature,
    )


def mesh_arrays(geoms: Geometries, offset: Vec3, tessellate: Tessellator) -> MeshArrays:
    if geoms.kind is Kind.POLYGONS:
        return polygon_mesh(geoms, offset, tessellate)
    if geoms.kind is Kind.LINES:
        return line_mesh(geoms, offset)
    return point_mesh(geoms, offset)


# --- attribute values as Blender stores them ----------------------------------------------


def stored_values(column: Column) -> np.ndarray | list[bytes]:
    """One value per feature as the Blender attribute holds it.

    FLOAT: float32 (NaN for null); INT: int32 (0 for null); BOOL: bool
    (False for null); STRING: UTF-8 bytes ("" for null).
    """
    if column.type is ColumnType.FLOAT:
        return np.asarray(column.values, dtype=np.float32)
    if column.type is ColumnType.INT:
        return np.asarray(column.values, dtype=np.int32)
    if column.type is ColumnType.BOOL:
        return np.asarray(column.values, dtype=bool)
    return [s.encode("utf-8") for s in column.values]


BLENDER_TYPES = {
    ColumnType.FLOAT: "FLOAT",
    ColumnType.INT: "INT",
    ColumnType.BOOL: "BOOLEAN",
    ColumnType.STRING: "STRING",
}
COLUMN_TYPES = {v: k for k, v in BLENDER_TYPES.items()}


# --- from a mesh -----------------------------------------------------------------------


@dataclass(slots=True)
class MeshInput:
    """A mesh read with ``foreach_get``, positions already in source coordinates."""

    positions: np.ndarray  # (v, 3) binary64, offset added
    loop_starts: np.ndarray  # (f,)
    loop_totals: np.ndarray  # (f,)
    loop_vertices: np.ndarray  # (l,)
    edges: np.ndarray  # (e, 2) every edge
    loop_edges: np.ndarray  # (l,) edge index of each loop
    face_attrs: dict[str, tuple[ColumnType, np.ndarray | list]] = field(default_factory=dict)
    edge_attrs: dict[str, tuple[ColumnType, np.ndarray | list]] = field(default_factory=dict)
    point_attrs: dict[str, tuple[ColumnType, np.ndarray | list]] = field(default_factory=dict)
    measure: str | None = None
    """A POINT attribute (float or int) that lines carry as their M values, such as a
    section's ``chainage``; None for no M."""


FID = "fid"


def _groups(attrs: dict, n: int) -> tuple[np.ndarray, np.ndarray | None]:
    """``(group per element, fid per group)`` from an INT ``fid`` attribute, if any."""
    if FID in attrs and attrs[FID][0] is ColumnType.INT and n:
        fids, group = np.unique(np.asarray(attrs[FID][1], dtype=np.int64), return_inverse=True)
        return group, fids
    return np.arange(n), None


def _first_values(attrs: dict, element_index: np.ndarray, skip: set[str]) -> dict[str, Column]:
    columns = {}
    for name, (ctype, values) in attrs.items():
        if name in skip:
            continue
        if ctype is ColumnType.STRING:
            picked: np.ndarray | list[str] = [
                values[int(i)].decode("utf-8", "replace")
                if isinstance(values[int(i)], bytes)
                else str(values[int(i)])
                for i in element_index
            ]
        else:
            picked = np.asarray(values)[element_index]
            if ctype is ColumnType.FLOAT:
                picked = shortest_float64(picked)
            elif ctype is ColumnType.INT:
                picked = picked.astype(np.int64)
        null = None
        if ctype is ColumnType.FLOAT:
            nan = np.isnan(picked)
            null = nan if nan.any() else None
        columns[name] = Column(name, ctype, picked, null)
    return columns


def shortest_float64(values: np.ndarray) -> np.ndarray:
    """Float values as binary64; float32 ones as the shortest decimal that reads back
    as the same float32, so 0.1 stored in a Blender attribute is written as 0.1 and not
    0.10000000149011612."""
    values = np.asarray(values)
    if values.dtype != np.float32:
        return values.astype(np.float64)
    return values.astype(str).astype(np.float64)


def _cycle_order(next_index: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cycles of the permutation ``next_index``: ``(order, cycle label in order)``.

    Pointer doubling: each element's label is the smallest index on its cycle,
    and its rank the distance from that element; both in O(n log n) numpy.
    """
    n = len(next_index)
    label = np.arange(n)
    jump = next_index.copy()
    steps = max(int(np.ceil(np.log2(n + 1))), 1) + 1
    for _ in range(steps):
        label = np.minimum(label, label[jump])
        jump = jump[jump]
    # Break each cycle before its label element and rank by distance to the end.
    succ = np.where(next_index == label, np.arange(n), next_index)
    dist = (succ != np.arange(n)).astype(np.int64)
    jump = succ.copy()
    for _ in range(steps):
        dist = dist + dist[jump]
        jump = jump[jump]
    order = np.lexsort((-dist, label))
    return order, label[order]


def _key(group: np.ndarray, vertex: np.ndarray, n_vertices: int) -> np.ndarray:
    return group.astype(np.int64) * (n_vertices + 1) + vertex.astype(np.int64)


def _point_in_ring(point: np.ndarray, ring: np.ndarray) -> bool:
    """Even-odd test of ``point`` (2,) against ``ring`` (k, 2)."""
    x, y = point
    a = ring
    b = np.roll(ring, -1, axis=0)
    crosses = (a[:, 1] > y) != (b[:, 1] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xs = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
    return bool(np.count_nonzero(crosses & (x < xs)) % 2)


def polygons_from_faces(mesh: MeshInput) -> tuple[Geometries, np.ndarray, dict[str, Column]]:
    """Polygons from the faces, one feature per ``fid``: ``(geometries, fids, columns)``.

    Each feature's boundary edges (edges used once within the feature) are
    walked into rings; rings wound like the feature's largest ring are
    exteriors and the others holes. A feature whose boundary pinches (a
    vertex with two outgoing boundary edges) is written face by face instead.
    """
    n_faces = len(mesh.loop_starts)
    n_vertices = len(mesh.positions)
    group, fids = _groups(mesh.face_attrs, n_faces)
    n_groups = int(group.max()) + 1 if n_faces else 0
    starts = mesh.loop_starts.astype(np.int64)
    totals = mesh.loop_totals.astype(np.int64)
    lv = mesh.loop_vertices.astype(np.int64)
    n_loops = len(lv)
    loop_face = np.repeat(np.arange(n_faces), totals)
    nxt = np.arange(n_loops) + 1
    if n_faces:
        nxt[starts + totals - 1] = starts
    src, dst = lv, lv[nxt] if n_loops else lv
    loop_group = group[loop_face]

    # Boundary: undirected edges used once within their group.
    lo, hi = np.minimum(src, dst), np.maximum(src, dst)
    order = np.lexsort((hi, lo, loop_group))
    g_s, lo_s, hi_s = loop_group[order], lo[order], hi[order]
    new = np.ones(n_loops, dtype=bool)
    new[1:] = (g_s[1:] != g_s[:-1]) | (lo_s[1:] != lo_s[:-1]) | (hi_s[1:] != hi_s[:-1])
    run = np.cumsum(new) - 1
    run_count = np.bincount(run)
    boundary = np.zeros(n_loops, dtype=bool)
    boundary[order] = run_count[run] == 1

    # A group is pinched if a vertex has two outgoing or two incoming boundary
    # edges, or an edge leads nowhere; then its boundary is not a set of cycles.
    bad = np.zeros(n_groups, dtype=bool)
    b_index = np.flatnonzero(boundary)
    b_group = loop_group[b_index]
    keys = _key(b_group, src[b_index], n_vertices)
    want = _key(b_group, dst[b_index], n_vertices)
    for k in (keys, want):
        sort = np.argsort(k, kind="stable")
        dup = np.zeros(len(k), dtype=bool)
        dup[1:] = k[sort][1:] == k[sort][:-1]
        dup[:-1] |= dup[1:]
        bad[b_group[sort[dup]]] = True
    sk = np.sort(keys)
    if len(sk):
        pos = np.minimum(np.searchsorted(sk, want), len(sk) - 1)
        bad[b_group[sk[pos] != want]] = True
    b_index = b_index[~bad[loop_group[b_index]]]
    keys = _key(loop_group[b_index], src[b_index], n_vertices)
    sort = np.argsort(keys, kind="stable")
    sk = keys[sort]
    pos = np.searchsorted(sk, _key(loop_group[b_index], dst[b_index], n_vertices))
    next_b = sort[pos] if len(sk) else np.zeros(0, dtype=np.int64)
    cyc_order, cyc_label = _cycle_order(next_b)
    ring_new = np.ones(len(cyc_order), dtype=bool)
    ring_new[1:] = cyc_label[1:] != cyc_label[:-1]
    ring_starts = np.flatnonzero(ring_new)
    walked_vertices = src[b_index[cyc_order]]
    walked_len = np.diff(np.append(ring_starts, len(cyc_order)))
    walked_group = loop_group[b_index[cyc_order[ring_starts]]] if len(ring_starts) else ring_starts

    # Faces of pinched groups, face by face.
    bad_faces = np.flatnonzero(bad[group]) if n_groups else np.zeros(0, dtype=np.int64)
    face_vertices = lv[segment_indices(starts[bad_faces], totals[bad_faces])]

    ring_vertices = np.concatenate([walked_vertices, face_vertices]).astype(np.int64)
    ring_len = np.concatenate([walked_len, totals[bad_faces]]).astype(np.int64)
    ring_group = np.concatenate([walked_group, group[bad_faces]]).astype(np.int64)
    forced = np.concatenate([np.zeros(len(walked_len), bool), np.ones(len(bad_faces), bool)])
    ro = offsets_from_lengths(ring_len)
    xyz = mesh.positions[ring_vertices]
    area = _signed_areas(xyz[:, :2], ro)

    # Orientation: the group's largest ring is an exterior; forced rings are all exteriors.
    flip = area < 0
    n_r = len(ring_len)
    if n_r:
        by_size = np.lexsort((-np.abs(area), ring_group))
        first_of_group = np.ones(n_r, dtype=bool)
        first_of_group[1:] = ring_group[by_size][1:] != ring_group[by_size][:-1]
        group_sign = np.ones(n_groups)
        group_sign[ring_group[by_size][first_of_group]] = np.sign(area[by_size][first_of_group])
        flip = np.where(forced, area < 0, group_sign[ring_group] < 0)
    oriented = np.where(flip, -area, area)
    is_exterior = forced | (oriented > 0)
    xyz = xyz[_reversed_within(ro, flip)]

    # Assign each hole to an exterior of its group.
    ring_part = np.full(n_r, -1, dtype=np.int64)
    ext = np.flatnonzero(is_exterior)
    ext = ext[np.lexsort((ext, ring_group[ext]))]
    ring_part[ext] = np.arange(len(ext))
    ext_count = np.bincount(ring_group[ext], minlength=n_groups)
    group_first_ext = np.full(n_groups, -1, dtype=np.int64)
    first_mask = np.ones(len(ext), dtype=bool)
    first_mask[1:] = ring_group[ext][1:] != ring_group[ext][:-1]
    group_first_ext[ring_group[ext][first_mask]] = ext[first_mask]
    holes = np.flatnonzero(~is_exterior)
    simple = ext_count[ring_group[holes]] == 1
    ring_part[holes[simple]] = ring_part[group_first_ext[ring_group[holes[simple]]]]
    for hole in holes[~simple].tolist():  # only features with several exteriors
        g = ring_group[hole]
        candidates = ext[ring_group[ext] == g]
        if not len(candidates):
            ring_part[hole] = -1
            continue
        point = xyz[ro[hole], :2]
        inside = [
            c for c in candidates.tolist() if _point_in_ring(point, xyz[ro[c] : ro[c + 1], :2])
        ]
        best = min(inside, key=lambda c: abs(area[c])) if inside else int(candidates[0])
        ring_part[hole] = ring_part[best]
    usable = ring_part >= 0

    # Rings in part order (exterior first), each closed.
    ring_sel = np.flatnonzero(usable)
    ring_sel = ring_sel[np.lexsort((ring_sel, ~is_exterior[ring_sel], ring_part[ring_sel]))]
    coord_index = segment_indices(ro[:-1][ring_sel], ring_len[ring_sel])
    closed_len = ring_len[ring_sel] + 1
    out_ro = offsets_from_lengths(closed_len)
    coords = np.empty((int(out_ro[-1]), 3))
    body = np.ones(len(coords), dtype=bool)
    body[out_ro[1:] - 1] = False
    coords[body] = xyz[coord_index]
    coords[~body] = coords[out_ro[:-1]]
    part_of_ring = ring_part[ring_sel]
    part_new = np.ones(len(ring_sel), dtype=bool)
    part_new[1:] = part_of_ring[1:] != part_of_ring[:-1]
    part_ring_count = np.diff(np.append(np.flatnonzero(part_new), len(ring_sel)))
    part_group = ring_group[ring_sel][part_new]
    feature_new = np.ones(len(part_group), dtype=bool)
    feature_new[1:] = part_group[1:] != part_group[:-1]
    feature_parts = np.diff(np.append(np.flatnonzero(feature_new), len(part_group)))
    feature_group = part_group[feature_new]

    geoms = Geometries(
        kind=Kind.POLYGONS,
        coords=coords,
        ring_offsets=out_ro,
        part_offsets=offsets_from_lengths(part_ring_count),
        feature_offsets=offsets_from_lengths(feature_parts),
        rows=np.arange(len(feature_group), dtype=np.int64),
    )
    first_face = np.full(n_groups, -1, dtype=np.int64)
    if n_faces:
        uniq, idx = np.unique(group, return_index=True)
        first_face[uniq] = idx
    columns = _first_values(mesh.face_attrs, first_face[feature_group], {FID})
    out_fids = fids[feature_group] if fids is not None else feature_group + 1
    return geoms, out_fids.astype(np.int64), columns


def loose_edges(mesh: MeshInput) -> np.ndarray:
    used = np.zeros(len(mesh.edges), dtype=bool)
    used[mesh.loop_edges] = True
    return np.flatnonzero(~used)


def loose_vertices(mesh: MeshInput) -> np.ndarray:
    used = np.zeros(len(mesh.positions), dtype=bool)
    used[mesh.edges.ravel()] = True
    used[mesh.loop_vertices] = True
    return np.flatnonzero(~used)


class _Take:
    """``values[index[i]]`` on demand, so string values are only read when picked."""

    __slots__ = ("index", "values")

    def __init__(self, values: object, index: np.ndarray) -> None:
        self.values = values
        self.index = index

    def __len__(self) -> int:
        return len(self.index)

    def __getitem__(self, i: int) -> object:
        return self.values[int(self.index[i])]  # type: ignore[index]


def _subset(attrs: dict, index: np.ndarray) -> dict:
    out = {}
    for name, (ctype, values) in attrs.items():
        if ctype is ColumnType.STRING:
            out[name] = (ctype, _Take(values, index))
        else:
            out[name] = (ctype, np.asarray(values)[index])
    return out


def lines_from_edges(mesh: MeshInput) -> tuple[Geometries, np.ndarray, dict[str, Column]]:
    """Lines from the loose edges: consecutive edges that join become one line string.

    With an EDGE ``fid`` attribute the lines of one fid are one feature
    (a MultiLineString if they don't join); without, each line is a feature.
    """
    index = loose_edges(mesh)
    edges = mesh.edges[index].astype(np.int64)
    attrs = _subset(mesh.edge_attrs, index)
    n = len(edges)
    group, fids = _groups(attrs, n)
    if fids is None:
        group = np.zeros(n, dtype=np.int64)
    joins = np.zeros(n, dtype=bool)
    if n > 1:
        joins[1:] = (group[1:] == group[:-1]) & (edges[1:, 0] == edges[:-1, 1])
    part_start = np.flatnonzero(~joins)
    part_len = np.diff(np.append(part_start, n))  # edges per part
    part_of_edge = np.cumsum(~joins) - 1
    total = n + len(part_start)
    vertices = np.empty(total, dtype=np.int64)
    vertices[np.arange(n) + part_of_edge + 1] = edges[:, 1]
    vertices[part_start + np.arange(len(part_start))] = edges[part_start, 0]
    part_group = group[part_start] if fids is not None else np.arange(len(part_start))
    if fids is not None:
        # Parts of one fid may not be adjacent: sort parts by group first.
        porder = np.argsort(part_group, kind="stable")
    else:
        porder = np.arange(len(part_start))
    vro = offsets_from_lengths(part_len + 1)
    coord_index = segment_indices(vro[:-1][porder], (part_len + 1)[porder])
    pg = part_group[porder]
    feature_new = np.ones(len(pg), dtype=bool)
    feature_new[1:] = pg[1:] != pg[:-1]
    feature_parts = np.diff(np.append(np.flatnonzero(feature_new), len(pg)))
    feature_group = pg[feature_new]
    geoms = Geometries(
        kind=Kind.LINES,
        coords=mesh.positions[vertices[coord_index]],
        ring_offsets=offsets_from_lengths((part_len + 1)[porder]),
        part_offsets=np.arange(len(pg) + 1, dtype=np.int64),
        feature_offsets=offsets_from_lengths(feature_parts),
        rows=np.arange(len(feature_group), dtype=np.int64),
    )
    measure = mesh.point_attrs.get(mesh.measure or "")
    if measure is not None and measure[0] in (ColumnType.FLOAT, ColumnType.INT):
        values = np.asarray(measure[1])[vertices[coord_index]]
        if measure[0] is ColumnType.FLOAT:
            values = shortest_float64(values)
        geoms.measures = values.astype(np.float64)
        geoms.measure_name = str(mesh.measure)
    first_edge = part_start[porder][feature_new]
    columns = _first_values(attrs, first_edge, {FID})
    out_fids = fids[feature_group] if fids is not None else feature_group + 1
    return geoms, out_fids.astype(np.int64), columns


def points_from_vertices(mesh: MeshInput) -> tuple[Geometries, np.ndarray, dict[str, Column]]:
    """Points from the loose vertices; one feature per POINT ``fid`` (else per vertex)."""
    index = loose_vertices(mesh)
    attrs = _subset(mesh.point_attrs, index)
    n = len(index)
    group, fids = _groups(attrs, n)
    order = np.argsort(group, kind="stable")
    g = group[order]
    feature_new = np.ones(n, dtype=bool)
    feature_new[1:] = g[1:] != g[:-1]
    feature_parts = np.diff(np.append(np.flatnonzero(feature_new), n))
    geoms = Geometries(
        kind=Kind.POINTS,
        coords=mesh.positions[index[order]],
        ring_offsets=np.arange(n + 1, dtype=np.int64),
        part_offsets=np.arange(n + 1, dtype=np.int64),
        feature_offsets=offsets_from_lengths(feature_parts),
        rows=np.arange(int(feature_new.sum()), dtype=np.int64),
    )
    columns = _first_values(attrs, order[feature_new], {FID})
    feature_group = g[feature_new]
    out_fids = fids[feature_group] if fids is not None else feature_group + 1
    return geoms, out_fids.astype(np.int64), columns


def vectors_from_mesh(mesh: MeshInput, name: str, crs: object) -> list[VectorData]:
    """One :class:`VectorData` per kind present in the mesh (polygons, lines, points)."""
    out = []
    for build in (polygons_from_faces, lines_from_edges, points_from_vertices):
        geoms, fids, columns = build(mesh)
        if not geoms.n_features:
            continue
        out.append(
            VectorData(
                name=name,
                fids=fids,
                columns=columns,
                geometries={geoms.kind: geoms},
                crs=crs,  # type: ignore[arg-type]
            )
        )
    return out
