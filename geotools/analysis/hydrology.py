"""3D surface hydrology on a mesh's own connectivity (bpy-free, vectorised numpy).

Geometric only: where water would run over the surface, how much surface
drains through each face, where it ponds and how much each depression holds.
No rainfall, infiltration, evaporation or hydraulics.

**The graph is the faces.** Each face is a node at its centre (the mean of its
corners, as Blender's ``polygon.center``), linked to the faces it shares a
mesh edge with. Faces were chosen over vertices because a face has one
orientation: a face whose normal points down is an overhang's underside, and
its water drips; a vertex where a wall meets the ground has no such answer.
Layers are FACE layers (M3) with no interpolation, and contributing area is a
plain sum of face areas. The cost is coarser directions than a vertex graph
(three neighbours for a triangle), which straightens out after a few faces.
Water is on the side a face's normal points to, so normals must point out of
solids.

**Steepest descent** goes to the neighbour with the largest drop over the 3D
distance between centres (the sine of the slope), so a wall's water goes down
the wall. Two kinds of face leave the surface graph straight down, through a
**drop**: a face whose normal points down (z below :data:`DRIP_NZ`) always
drips from its centre, and a face on an open edge drops from that edge when
no neighbour is lower (a roof's eaves, the foot of a wall that is not joined
to the ground, the edge of the model). Where a drop lands is a vertical ray
cast (Blender's, in ``blender.hydrology``): the first face below, or, when the
start is under a surface (a wall sunk into the ground), the first upward
facing face above; with neither, the water leaves the model (``OCEAN``).

**Depressions** are filled with the levels a priority-flood would give, worked
out on basins rather than faces: steepest descent splits the faces into
basins (one per pit), each pair of touching basins is linked at the lowest
pass between them (the higher face of the lowest linking edge or drop), and
the minimum spanning tree of that basin graph, rooted at the outside, gives
each basin's spill level (the highest pass on its way out). That is
Borůvka's algorithm, vectorised. A face's filled height is the larger of its
own and its basin's level. A set of faces that never reaches the outside (a
closed object with no drop out of it) drains at its lowest pit.

**Flow** is then routed over the filled surface: down the steepest neighbour,
a drop, or on a flat (a lake or flat ground) to the neighbour nearer the
flat's exit (breadth-first from the exits). **Accumulation** is each face's
area plus everything upstream of it, summed deepest first along the routing
tree (depths by pointer jumping). Areas are plan areas by default: rain falls
on the plan projection of upward faces, so walls and undersides add none.

**Ponding:** depth is filled minus actual height; connected ponded faces are
one depression, with its level (the spill level), its pour point (where the
main exit leaves it), volume, plan area and greatest depth. Volume is
``sum(depth x signed plan area)``, so an overhang's underside below the water
subtracts the solid above it.

See docs/design/m5-analysis.md.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

DRIP_NZ = -0.1
"""A face whose unit normal has a z below this drips (more than about 5.7° past vertical)."""

OCEAN = -1
"""Receiver of a face whose water leaves the model."""

_NONE = -2

AREA_MODES = ("PLAN", "SURFACE")


class HydrologyError(ValueError):
    """The surface cannot be analysed (no faces, bad input)."""


# --- the surface ---------------------------------------------------------------------------


@dataclass(slots=True)
class Surface:
    """Faces as graph nodes: their centres, vector areas, shared edges and open edges."""

    centroids: np.ndarray  # (n, 3) float64
    vector_areas: np.ndarray  # (n, 3) float64: area x unit normal
    pairs: np.ndarray  # (m, 2) int64: faces sharing a mesh edge
    boundary_faces: np.ndarray  # (b,) int64: faces with an open edge
    boundary_points: np.ndarray  # (b, 3): the middle of each such face's lowest open edge
    boundary_tangents: np.ndarray = None  # (b, 3): that edge's direction  # type: ignore[assignment]
    non_manifold_edges: int = 0

    @property
    def n(self) -> int:
        return len(self.centroids)

    @property
    def areas(self) -> np.ndarray:
        return np.linalg.norm(self.vector_areas, axis=1)

    @property
    def normals(self) -> np.ndarray:
        area = self.areas[:, None]
        return np.divide(
            self.vector_areas, area, out=np.zeros_like(self.vector_areas), where=area > 0
        )

    @property
    def plan_areas(self) -> np.ndarray:
        """Signed plan (horizontal projected) area: positive facing up."""
        return self.vector_areas[:, 2]


def _group_pairs(keys: np.ndarray, values: np.ndarray) -> np.ndarray:
    """All pairs ``(values[i], values[j])``, i < j, within runs of equal sorted ``keys``."""
    n = len(keys)
    if n < 2:
        return np.zeros((0, 2), dtype=np.int64)
    new = np.ones(n, dtype=bool)
    new[1:] = keys[1:] != keys[:-1]
    start = np.flatnonzero(new)
    size = np.diff(np.append(start, n))
    group = np.repeat(np.arange(len(start)), size)
    pos = np.arange(n) - start[group]
    # Element i pairs with the size - pos - 1 elements after it in its group.
    after = size[group] - pos - 1
    first = np.repeat(np.arange(n), after)
    offset = np.arange(int(after.sum())) - np.repeat(np.cumsum(after) - after, after) + 1
    return np.stack([values[first], values[first + offset]], axis=1)


def corner_edges(face_starts: np.ndarray, corner_verts: np.ndarray) -> tuple[np.ndarray, int]:
    """Edge ids of each corner (the edge to the next corner), as Blender's ``.corner_edge``.

    For meshes built outside Blender (tests). Returns the ids and the edge count.
    """
    face_starts = np.asarray(face_starts, dtype=np.int64)
    corner_verts = np.asarray(corner_verts, dtype=np.int64)
    nxt = _next_corner(face_starts, len(corner_verts))
    a = corner_verts
    b = corner_verts[nxt]
    lo = np.minimum(a, b)
    hi = np.maximum(a, b)
    key = lo * (int(corner_verts.max(initial=0)) + 1) + hi
    _, ids = np.unique(key, return_inverse=True)
    return ids.astype(np.int64), int(ids.max(initial=-1)) + 1


def _next_corner(face_starts: np.ndarray, n_corners: int) -> np.ndarray:
    nxt = np.arange(1, n_corners + 1, dtype=np.int64)
    sizes = np.diff(face_starts)
    nonempty = sizes > 0
    nxt[face_starts[1:][nonempty] - 1] = face_starts[:-1][nonempty]
    return nxt


def surface_from_mesh(
    positions: np.ndarray,
    face_starts: np.ndarray,
    corner_verts: np.ndarray,
    corner_edge_ids: np.ndarray,
    n_edges: int,
    flip: np.ndarray | None = None,
) -> Surface:
    """The face graph of a mesh: ``(V, 3)`` binary64 positions, ``(F + 1,)`` corner
    offsets of the faces, the vertex and edge of each corner, and the edge count.

    Several objects are one mesh with their ids offset; faces are linked only
    through shared edges, never by position. ``flip`` marks faces whose normal
    is the reverse of their winding (an object with a mirroring transform).
    """
    positions = np.asarray(positions, dtype=np.float64)
    face_starts = np.asarray(face_starts, dtype=np.int64)
    corner_verts = np.asarray(corner_verts, dtype=np.int64)
    corner_edge_ids = np.asarray(corner_edge_ids, dtype=np.int64)
    n_faces = len(face_starts) - 1
    n_corners = len(corner_verts)
    if n_faces <= 0:
        raise HydrologyError("The surface has no faces.")
    sizes = np.diff(face_starts)
    if (sizes < 3).any():
        raise HydrologyError("A face has fewer than three corners.")
    corner_face = np.repeat(np.arange(n_faces, dtype=np.int64), sizes)

    if (sizes == 3).all():
        tri = positions[corner_verts.reshape(-1, 3)]
        centroids = tri.mean(axis=1)
        vector_areas = 0.5 * np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        del tri
    else:
        corners = positions[corner_verts]
        centroids = np.add.reduceat(corners, face_starts[:-1], axis=0) / sizes[:, None]
        # Newell's method about the centre, for any polygon.
        q = corners - centroids[corner_face]
        del corners
        nxt = _next_corner(face_starts, n_corners)
        vector_areas = 0.5 * np.add.reduceat(np.cross(q, q[nxt]), face_starts[:-1], axis=0)
        del q

    if flip is not None:
        vector_areas[np.asarray(flip, dtype=bool)] *= -1.0

    # Faces of each edge without a sort: one face of the edge, then another.
    count = np.bincount(corner_edge_ids, minlength=n_edges)
    one = np.full(n_edges, -1, dtype=np.int64)
    one[corner_edge_ids] = corner_face
    other_mask = corner_face != one[corner_edge_ids]
    two = np.full(n_edges, -1, dtype=np.int64)
    two[corner_edge_ids[other_mask]] = corner_face[other_mask]
    manifold = (count == 2) & (two >= 0)
    pairs = np.stack([one[manifold], two[manifold]], axis=1)
    non_manifold = np.flatnonzero(count > 2)
    if len(non_manifold):
        is_nm = count[corner_edge_ids] > 2
        e = corner_edge_ids[is_nm]
        f = corner_face[is_nm]
        order = np.lexsort((f, e))
        extra = _group_pairs(e[order], f[order])
        extra = extra[extra[:, 0] != extra[:, 1]]
        pairs = np.concatenate([pairs, extra])

    # Open edges: the middle of each face's lowest one.
    open_corner = np.flatnonzero(count[corner_edge_ids] == 1)
    if len(open_corner):
        nxt = _next_corner(face_starts, n_corners)
        pa = positions[corner_verts[open_corner]]
        pb = positions[corner_verts[nxt[open_corner]]]
        mid = 0.5 * (pa + pb)
        face = corner_face[open_corner]
        order = np.lexsort((mid[:, 2], face))
        face = face[order]
        first = np.ones(len(face), dtype=bool)
        first[1:] = face[1:] != face[:-1]
        boundary_faces = face[first]
        boundary_points = mid[order][first]
        boundary_tangents = (pb - pa)[order][first]
    else:
        boundary_faces = np.zeros(0, dtype=np.int64)
        boundary_points = np.zeros((0, 3))
        boundary_tangents = np.zeros((0, 3))
    return Surface(
        centroids=centroids,
        vector_areas=vector_areas,
        pairs=pairs,
        boundary_faces=boundary_faces,
        boundary_points=boundary_points,
        boundary_tangents=boundary_tangents,
        non_manifold_edges=len(non_manifold),
    )


# --- drops ---------------------------------------------------------------------------------


@dataclass(slots=True)
class DropQueries:
    """Where water leaves the surface graph: one vertical ray (down, then up) per face."""

    face: np.ndarray  # (q,) int64
    drip: np.ndarray  # (q,) bool: a downward face (always drips) or an open edge
    origin: np.ndarray  # (q, 3) float64


def drop_queries(surface: Surface, offset: float) -> DropQueries:
    """The drops to cast: every downward face from just below its centre, and every
    other face with an open edge from just outside and above the middle of its
    lowest open edge.

    ``offset`` (scene units) keeps the rays off the surface they leave.
    """
    normals = surface.normals
    drip_faces = np.flatnonzero(normals[:, 2] < DRIP_NZ)
    drip_origin = surface.centroids[drip_faces].copy()
    drip_origin[:, 2] -= offset
    is_drip = np.zeros(surface.n, dtype=bool)
    is_drip[drip_faces] = True
    keep = ~is_drip[surface.boundary_faces]
    edge_faces = surface.boundary_faces[keep]
    mid = surface.boundary_points[keep]
    # Outward: across the edge, away from the face, in the face's plane, seen
    # in plan; where that is (nearly) vertical (the foot of a wall), the way
    # the face looks.
    tangent = surface.boundary_tangents[keep]
    to_edge = mid - surface.centroids[edge_faces]
    t_len2 = np.maximum((tangent * tangent).sum(axis=1, keepdims=True), 1e-300)
    across = to_edge - (to_edge * tangent).sum(axis=1, keepdims=True) / t_len2 * tangent
    plan = np.linalg.norm(across[:, :2], axis=1)
    level = plan > 0.1 * np.linalg.norm(across, axis=1)
    out = np.where(level[:, None], across[:, :2], normals[edge_faces, :2])
    length = np.linalg.norm(out, axis=1, keepdims=True)
    out = np.divide(out, length, out=np.zeros_like(out), where=length > 0)
    edge_origin = mid.copy()
    edge_origin[:, :2] += offset * out
    edge_origin[:, 2] += offset
    return DropQueries(
        face=np.concatenate([drip_faces, edge_faces]).astype(np.int64),
        drip=np.concatenate([np.ones(len(drip_faces), bool), np.zeros(len(edge_faces), bool)]),
        origin=np.concatenate([drip_origin, edge_origin]),
    )


@dataclass(slots=True)
class Drops:
    """The drops with their landings: ``target`` is a face or :data:`OCEAN`."""

    face: np.ndarray
    drip: np.ndarray
    origin: np.ndarray
    target: np.ndarray
    landing: np.ndarray  # (q, 3): the hit, or the origin when the water leaves

    @classmethod
    def none(cls) -> Drops:
        e = np.zeros(0, dtype=np.int64)
        return cls(e, np.zeros(0, bool), np.zeros((0, 3)), e.copy(), np.zeros((0, 3)))


def resolve_drops(
    queries: DropQueries,
    surface: Surface,
    down_face: np.ndarray,
    down_point: np.ndarray,
    up_face: np.ndarray,
    up_point: np.ndarray,
) -> Drops:
    """Landings from the two ray casts (face ``-1`` where a ray hit nothing).

    The first face below wins; failing that the first face above, if it faces
    up (the start is under the ground, as at the foot of a sunken wall); failing
    that the water leaves the model.
    """
    down_face = np.asarray(down_face, dtype=np.int64)
    up_face = np.asarray(up_face, dtype=np.int64)
    normals_z = surface.normals[:, 2]
    up_ok = (up_face >= 0) & (normals_z[np.maximum(up_face, 0)] > 0) & (down_face < 0)
    target = np.where(down_face >= 0, down_face, np.where(up_ok, up_face, OCEAN))
    landing = np.where(
        (down_face >= 0)[:, None],
        down_point,
        np.where(up_ok[:, None], up_point, queries.origin),
    )
    # A drop onto its own face (a grazing ray) is no drop.
    target = np.where(target == queries.face, OCEAN, target)
    return Drops(queries.face, queries.drip, queries.origin, target.astype(np.int64), landing)


def faces_under(
    points: np.ndarray, face_lo: np.ndarray, face_hi: np.ndarray, cells: int = 2048
) -> np.ndarray:
    """Faces a vertical ray from any of ``points`` might hit: those whose plan bounding
    box (``face_lo``, ``face_hi``: ``(F, 2)``) covers the grid cell of some point.

    Exact (no face a ray can hit is left out); it only spares the ray caster
    the rest of the mesh. A summed-area table of the cells holding points
    answers each face's box in four lookups.
    """
    if not len(points) or not len(face_lo):
        return np.zeros(0, dtype=np.int64)
    lo = np.minimum(face_lo.min(axis=0), points[:, :2].min(axis=0))
    hi = np.maximum(face_hi.max(axis=0), points[:, :2].max(axis=0))
    size = np.maximum((hi - lo) / cells, 1e-9)

    def cell(xy: np.ndarray) -> np.ndarray:
        return np.clip(((xy - lo) / size).astype(np.int64), 0, cells - 1)

    pc = cell(points[:, :2])
    grid = np.zeros((cells + 1, cells + 1), dtype=np.int32)
    np.add.at(grid, (pc[:, 0] + 1, pc[:, 1] + 1), 1)
    table = grid.cumsum(axis=0).cumsum(axis=1)
    a = cell(face_lo)
    b = cell(face_hi) + 1
    count = table[b[:, 0], b[:, 1]] - table[a[:, 0], b[:, 1]] - table[b[:, 0], a[:, 1]]
    count += table[a[:, 0], a[:, 1]]
    return np.flatnonzero(count > 0)


def face_bounds(
    positions: np.ndarray, face_starts: np.ndarray, corner_verts: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Plan bounding boxes of the faces: ``(lo, hi)``, each ``(F, 2)``."""
    xy = positions[corner_verts, :2]
    lo = np.minimum.reduceat(xy, face_starts[:-1], axis=0)
    hi = np.maximum.reduceat(xy, face_starts[:-1], axis=0)
    return lo, hi


# --- graph helpers ---------------------------------------------------------------------------


def _roots(parent: np.ndarray) -> np.ndarray:
    """The root of every node of a forest given by ``parent`` (roots point to themselves)."""
    p = parent
    while True:
        q = p[p]
        if np.array_equal(q, p):
            return p
        p = q


def _depths(parent: np.ndarray) -> np.ndarray:
    """Steps from each node to its root (pointer jumping)."""
    n = len(parent)
    depth = (parent != np.arange(n)).astype(np.int64)
    jump = parent
    while True:
        nxt = jump[jump]
        if np.array_equal(nxt, jump):
            return depth
        depth = depth + depth[jump]
        jump = nxt


def _steepest(n: int, a: np.ndarray, b: np.ndarray, z: np.ndarray, dist: np.ndarray):
    """For each node, the neighbour with the steepest drop: ``(receiver, slope)``.

    The receiver is ``_NONE`` where no neighbour is strictly lower.
    """
    slope = (z[a] - z[b]) / dist
    best = np.zeros(n)
    np.maximum.at(best, a, slope)
    np.maximum.at(best, b, -slope)
    receiver = np.full(n, _NONE, dtype=np.int64)
    m = (slope > 0) & (slope == best[a])
    receiver[a[m]] = b[m]
    m = (slope < 0) & (-slope == best[b])
    receiver[b[m]] = a[m]
    return receiver, best


def _components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Connected component label (smallest node id) of each node, by hooking and jumping."""
    parent = np.arange(n, dtype=np.int64)
    a = np.asarray(a, dtype=np.int64)
    b = np.asarray(b, dtype=np.int64)
    while len(a):
        ra = parent[a]
        rb = parent[b]
        differ = ra != rb
        if not differ.any():
            break
        a, b, ra, rb = a[differ], b[differ], ra[differ], rb[differ]
        np.minimum.at(parent, np.maximum(ra, rb), np.minimum(ra, rb))
        parent = _roots(parent)
    return parent


def _boruvka(nv: int, u: np.ndarray, v: np.ndarray, rank: np.ndarray) -> np.ndarray:
    """Edges of a minimum spanning forest; ``rank`` is each edge's distinct priority."""
    comp = np.arange(nv, dtype=np.int64)
    chosen = np.zeros(len(u), dtype=bool)
    live = np.arange(len(u))
    big = np.iinfo(np.int64).max
    while len(live):
        cu = comp[u[live]]
        cv = comp[v[live]]
        cross = cu != cv
        live, cu, cv = live[cross], cu[cross], cv[cross]
        if not len(live):
            break
        best = np.full(nv, big, dtype=np.int64)
        r = rank[live]
        np.minimum.at(best, cu, r)
        np.minimum.at(best, cv, r)
        picked = (best[cu] == r) | (best[cv] == r)
        chosen[live[picked]] = True
        # Hook each component to the far end of its own cheapest edge.
        parent = np.arange(nv, dtype=np.int64)
        mine_u = best[cu] == r
        mine_v = best[cv] == r
        parent[cu[mine_u]] = cv[mine_u]
        parent[cv[mine_v]] = cu[mine_v]
        # Two components that picked the same edge point at each other: the
        # smaller becomes the root.
        mutual = parent[parent] == np.arange(nv)
        idx = np.flatnonzero(mutual & (np.arange(nv) < parent))
        parent[idx] = idx
        comp = _roots(parent)[comp]
    return np.flatnonzero(chosen)


def _tree_levels(nv: int, root: int, u: np.ndarray, v: np.ndarray, w: np.ndarray) -> np.ndarray:
    """On a tree, the largest edge weight on each node's path to ``root``; NaN if unreached.

    Breadth-first from the root, one numpy pass per depth.
    """
    level = np.full(nv, np.nan)
    if nv == 0:
        return level
    src = np.concatenate([u, v])
    dst = np.concatenate([v, u])
    ww = np.concatenate([w, w])
    order = np.argsort(src, kind="stable")
    src, dst, ww = src[order], dst[order], ww[order]
    indptr = np.searchsorted(src, np.arange(nv + 1))
    level[root] = -np.inf
    seen = np.zeros(nv, dtype=bool)
    seen[root] = True
    frontier = np.array([root], dtype=np.int64)
    while len(frontier):
        starts = indptr[frontier]
        counts = indptr[frontier + 1] - starts
        idx = _ranges(starts, counts)
        nb = dst[idx]
        par = np.repeat(frontier, counts)
        fresh = ~seen[nb]
        nb, par, wt = nb[fresh], par[fresh], ww[idx][fresh]
        level[nb] = np.maximum(wt, level[par])
        seen[nb] = True
        frontier = nb
    return level


def _ranges(starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    total = int(counts.sum())
    if not total:
        return np.zeros(0, dtype=np.int64)
    first = np.cumsum(counts) - counts
    return np.repeat(starts - first, counts) + np.arange(total)


# --- the analysis ----------------------------------------------------------------------------


@dataclass(slots=True)
class Depressions:
    """One row per depression, largest volume first (label ``i + 1``)."""

    level: np.ndarray  # spill level (scene z)
    volume: np.ndarray  # scene units³
    area: np.ndarray  # plan area of the ponded faces
    max_depth: np.ndarray
    faces: np.ndarray  # ponded face count
    pour_face: np.ndarray  # face where the main exit leaves the depression
    pour_point: np.ndarray  # (k, 3) its centre
    catchment: np.ndarray  # area draining out through the main exit (incl. the depression)

    @property
    def count(self) -> int:
        return len(self.level)


@dataclass(slots=True)
class Result:
    receiver: np.ndarray  # (n,) face the water goes to, or OCEAN (leaves or ends here)
    filled: np.ndarray  # (n,) filled height of each face's centre
    depth: np.ndarray  # (n,) ponded depth
    accumulation: np.ndarray  # (n,) contributing area, the face's own included
    depression: np.ndarray  # (n,) int64: 0 none, i + 1 for depressions[i]
    depressions: Depressions
    drop_landing: np.ndarray  # (n, 3) where a face's drop lands (NaN if it has none in use)
    stats: dict = field(default_factory=dict)


def analyse(
    surface: Surface,
    drops: Drops,
    *,
    area: str = "PLAN",
    min_depth: float = 0.0,
) -> Result:
    """Flow routing, accumulation and ponding on ``surface`` with its ``drops``.

    ``area`` is ``"PLAN"`` (plan area of upward faces: rain catch) or
    ``"SURFACE"`` (true area). A depression is reported (and labelled) when
    its greatest depth is at least ``min_depth``; every depression is filled
    whatever its depth.
    """
    if area not in AREA_MODES:
        raise HydrologyError(f"Unknown area mode {area!r}")
    n = surface.n
    if n == 0:
        raise HydrologyError("The surface has no faces.")
    z = surface.centroids[:, 2]
    a = surface.pairs[:, 0]
    b = surface.pairs[:, 1]
    dist = np.linalg.norm(surface.centroids[a] - surface.centroids[b], axis=1)
    np.maximum(dist, 1e-12, out=dist)
    qf = drops.face
    qt = drops.target
    ocean = n  # the outside, as one extra node

    # 1. Basins of steepest descent on the surface as it is.
    receiver, _ = _steepest(n, a, b, z, dist)
    zt = np.where(qt >= 0, z[np.maximum(qt, 0)], -np.inf)
    use = (zt < z[qf]) & (drops.drip | (receiver[qf] == _NONE))
    parent = np.append(receiver, ocean)
    parent[qf[use]] = np.where(qt[use] >= 0, qt[use], ocean)
    pit = parent == _NONE
    parent[pit] = np.flatnonzero(pit)
    label = _roots(parent)
    del parent

    # 2. The basin graph: each pair of touching basins at its lowest pass.
    basin_ids, basin_of = np.unique(label, return_inverse=True)
    nb = len(basin_ids)
    ocean_basin = int(np.searchsorted(basin_ids, ocean))
    zz = np.append(z, -np.inf)
    eu = np.concatenate([a, qf])
    ev = np.concatenate([b, np.where(qt >= 0, qt, ocean)])
    bu = basin_of[eu]
    bv = basin_of[ev]
    cross = bu != bv
    bu, bv = bu[cross], bv[cross]
    weight = np.maximum(zz[eu[cross]], zz[ev[cross]])
    lo = np.minimum(bu, bv)
    hi = np.maximum(bu, bv)
    order = np.lexsort((weight, hi, lo))
    lo, hi, weight = lo[order], hi[order], weight[order]
    first = np.ones(len(lo), dtype=bool)
    first[1:] = (lo[1:] != lo[:-1]) | (hi[1:] != hi[:-1])
    lo, hi, weight = lo[first], hi[first], weight[first]

    # 3. Spill levels: the minimum spanning tree of the basin graph, from the outside.
    rank = np.empty(len(lo), dtype=np.int64)
    rank[np.lexsort((np.arange(len(lo)), weight))] = np.arange(len(lo))
    tree = _boruvka(nb, lo, hi, rank)
    tu, tv, tw = lo[tree], hi[tree], weight[tree]
    comp = _components(nb, tu, tv)
    closed_pits = np.zeros(0, dtype=np.int64)
    if (comp != comp[ocean_basin]).any():
        # Faces that never reach the outside drain at their lowest pit.
        pit_z = zz[basin_ids]
        isolated = np.flatnonzero(comp != comp[ocean_basin])
        order = np.lexsort((pit_z[isolated], comp[isolated]))
        iso = isolated[order]
        head = np.ones(len(iso), dtype=bool)
        head[1:] = comp[iso][1:] != comp[iso][:-1]
        sinks = iso[head]
        tu = np.concatenate([tu, sinks])
        tv = np.concatenate([tv, np.full(len(sinks), ocean_basin)])
        tw = np.concatenate([tw, pit_z[sinks]])
        closed_pits = basin_ids[sinks]
    level = _tree_levels(nb, ocean_basin, tu, tv, tw)
    filled = np.fmax(z, level[basin_of[:n]])  # NaN (never reached) keeps z
    del label, basin_of

    # 4. Routing over the filled surface.
    receiver, _ = _steepest(n, a, b, filled, dist)
    ft = np.where(qt >= 0, filled[np.maximum(qt, 0)], -np.inf)
    use = (ft < filled[qf]) & (drops.drip | (receiver[qf] == _NONE))
    receiver[qf[use]] = qt[use]
    landing = np.full((n, 3), np.nan)
    landing[qf[use]] = drops.landing[use]
    receiver[closed_pits[receiver[closed_pits] == _NONE]] = OCEAN
    flats = int(np.count_nonzero(receiver == _NONE))
    if flats:
        _route_flats(receiver, filled, a, b, qf, qt)
    stuck = receiver == _NONE
    receiver[stuck] = OCEAN

    # 5. Accumulation, deepest first.
    weights = np.maximum(surface.plan_areas, 0.0) if area == "PLAN" else surface.areas
    accumulation = _accumulate(receiver, weights)

    # 6. Depressions.
    depth = filled - z
    ponded = depth > 0
    depressions, label = _depressions(
        surface, receiver, filled, depth, ponded, accumulation, a, b, qf, qt, min_depth
    )
    stats = {
        "faces": n,
        "links": len(a),
        "drops": len(qf),
        "drips": int(np.count_nonzero(drops.drip)),
        "drops_out": int(np.count_nonzero(qt == OCEAN)),
        "basins": nb - 1,
        "flat_faces": flats,
        "unrouted": int(np.count_nonzero(stuck)),
        "closed_parts": len(closed_pits),
        "outlets": int(np.count_nonzero(receiver == OCEAN)),
        "ponded_faces": int(np.count_nonzero(ponded)),
        "non_manifold_edges": surface.non_manifold_edges,
    }
    return Result(
        receiver=receiver,
        filled=filled,
        depth=depth,
        accumulation=accumulation,
        depression=label,
        depressions=depressions,
        drop_landing=landing,
        stats=stats,
    )


def _route_flats(receiver, filled, a, b, qf, qt) -> None:
    """Faces with no lower neighbour drain across their flat towards its exits.

    Breadth-first from the faces that already drain, through links (and drops)
    between faces of equal filled height.
    """
    drop_in = qt >= 0
    ea = np.concatenate([a, qf[drop_in]])
    eb = np.concatenate([b, qt[drop_in]])
    flat = filled[ea] == filled[eb]
    open_a = receiver[ea] == _NONE
    open_b = receiver[eb] == _NONE
    flat &= open_a | open_b
    ea, eb = ea[flat], eb[flat]
    # Directed: from a face that may drain (src) to a waiting face (dst).
    src = np.concatenate([ea, eb])
    dst = np.concatenate([eb, ea])
    keep = receiver[dst] == _NONE
    src, dst = src[keep], dst[keep]
    order = np.argsort(src, kind="stable")
    src, dst = src[order], dst[order]
    n = len(receiver)
    indptr = np.searchsorted(src, np.arange(n + 1))
    frontier = np.unique(src[receiver[src] != _NONE])
    while len(frontier):
        starts = indptr[frontier]
        counts = indptr[frontier + 1] - starts
        idx = _ranges(starts, counts)
        to = dst[idx]
        frm = np.repeat(frontier, counts)
        waiting = receiver[to] == _NONE
        to, frm = to[waiting], frm[waiting]
        receiver[to] = frm
        frontier = np.unique(to)


def _accumulate(receiver: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Each face's weight plus all its upstream faces', along the routing tree."""
    n = len(receiver)
    parent = np.where(receiver >= 0, receiver, np.arange(n))
    depth = _depths(parent)
    acc = weights.astype(np.float64).copy()
    order = np.argsort(-depth, kind="stable")
    d = depth[order]
    cut = np.flatnonzero(d[1:] != d[:-1]) + 1
    bounds = np.concatenate([[0], cut, [n]])
    for start, end in itertools.pairwise(bounds):  # one pass per depth
        if d[start] == 0:
            break
        nodes = order[start:end]
        np.add.at(acc, receiver[nodes], acc[nodes])
    return acc


def _depressions(surface, receiver, filled, depth, ponded, accumulation, a, b, qf, qt, min_depth):
    n = len(receiver)
    label = np.zeros(n, dtype=np.int64)
    faces = np.flatnonzero(ponded)
    empty = Depressions(
        *(np.zeros(0) for _ in range(5)),
        pour_face=np.zeros(0, dtype=np.int64),
        pour_point=np.zeros((0, 3)),
        catchment=np.zeros(0),
    )
    if not len(faces):
        return empty, label
    drop_in = qt >= 0
    ea = np.concatenate([a, qf[drop_in]])
    eb = np.concatenate([b, qt[drop_in]])
    both = ponded[ea] & ponded[eb]
    comp = _components(n, ea[both], eb[both])
    ids, lake = np.unique(comp[faces], return_inverse=True)
    k = len(ids)
    plan = surface.plan_areas[faces]
    volume = np.bincount(lake, weights=depth[faces] * plan, minlength=k)
    area = np.bincount(lake, weights=np.maximum(plan, 0.0), minlength=k)
    max_depth = np.zeros(k)
    np.maximum.at(max_depth, lake, depth[faces])
    count = np.bincount(lake, minlength=k)
    lake_level = np.zeros(k)
    lake_level[lake] = filled[faces]
    # The main exit: the lake face draining out of the lake with the most water.
    lake_of = np.full(n, -1, dtype=np.int64)
    lake_of[faces] = lake
    rec = receiver[faces]
    leaves = (rec < 0) | (lake_of[np.maximum(rec, 0)] != lake)
    exit_faces = faces[leaves]
    exit_lake = lake[leaves]
    order = np.lexsort((-accumulation[exit_faces], exit_lake))
    exit_faces, exit_lake = exit_faces[order], exit_lake[order]
    head = np.ones(len(exit_lake), dtype=bool)
    head[1:] = exit_lake[1:] != exit_lake[:-1]
    main = np.full(k, -1, dtype=np.int64)
    main[exit_lake[head]] = exit_faces[head]
    catchment = np.where(main >= 0, accumulation[np.maximum(main, 0)], 0.0)
    pour = np.where(main >= 0, receiver[np.maximum(main, 0)], -1)
    pour = np.where(pour >= 0, pour, main)
    pour_point = np.where((pour >= 0)[:, None], surface.centroids[np.maximum(pour, 0)], np.nan)
    keep = max_depth >= min_depth
    rank = np.flatnonzero(keep)
    rank = rank[np.lexsort((rank, -volume[rank]))]
    new_label = np.zeros(k, dtype=np.int64)
    new_label[rank] = np.arange(1, len(rank) + 1)
    label[faces] = new_label[lake]
    table = Depressions(
        level=lake_level[rank],
        volume=volume[rank],
        area=area[rank],
        max_depth=max_depth[rank],
        faces=count[rank].astype(np.float64),
        pour_face=pour[rank],
        pour_point=pour_point[rank],
        catchment=catchment[rank],
    )
    return table, label


# --- flow paths ------------------------------------------------------------------------------


@dataclass(slots=True)
class Channels:
    """Faces on channels and their links, for flow-path lines."""

    faces: np.ndarray  # channel faces
    network: np.ndarray  # per channel face: 1 = the network with the largest outlet
    outlet_area: np.ndarray  # per network, accumulation at its outlet


def channels(result: Result, threshold: float, count: int = 0) -> Channels:
    """Faces whose accumulation is at least ``threshold``, grouped by outlet.

    With ``count`` > 0 only the ``count`` networks with the largest outlets are kept.
    """
    acc = result.accumulation
    receiver = result.receiver
    on = acc >= threshold
    faces = np.flatnonzero(on)
    if not len(faces):
        return Channels(faces, np.zeros(0, np.int64), np.zeros(0))
    n = len(acc)
    parent = np.where((receiver >= 0) & on[np.maximum(receiver, 0)], receiver, np.arange(n))
    root = _roots(parent)[faces]
    roots, net = np.unique(root, return_inverse=True)
    rank = np.argsort(-acc[roots], kind="stable")
    position = np.empty(len(roots), dtype=np.int64)
    position[rank] = np.arange(1, len(roots) + 1)
    network = position[net]
    if count > 0:
        keep = network <= count
        faces, network = faces[keep], network[keep]
    return Channels(faces, network, acc[roots[rank]][: count if count > 0 else None])


def ground_scale(csf: float) -> tuple[float, float]:
    """Factors from grid to ground for areas and volumes: ``(1 / csf², 1 / csf²)``.

    Heights are unchanged, so a volume scales like its plan area.
    """
    if not (math.isfinite(csf) and csf > 0):
        return 1.0, 1.0
    f = 1.0 / (csf * csf)
    return f, f
