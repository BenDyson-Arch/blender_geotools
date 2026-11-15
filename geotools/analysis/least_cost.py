"""Directed least-cost movement over a triangulated 3D surface (bpy-free, vectorised numpy).

**What it computes.** From a Start point on the surface, the least travel time
to every point of a graph laid over the surface (directed: up and down cost
differently), the quickest route to an End point, and the time to reach each
face's centre (the accumulated-cost layer). Times come from a walking model
(:class:`Model`) times a per-face cost factor.

**The graph.** Nodes are the mesh's vertices plus ``steiner`` evenly spaced
points inside every triangle edge (Steiner points). Within each triangle every
node on its boundary links to every other one by a straight segment across
the triangle, in both directions. A segment along a shared edge appears in
both triangles, each with its own cost factor, so the cheaper side counts.
With ``steiner = 0`` the graph is the mesh's edges; Steiner points add
directions across faces, which cuts the bias of routes tied to edges (the
validation page measures what remains). This is the Steiner-point scheme of
Lanthier, Maheshwari and Sack (2001) and Aleksandrov, Maheshwari and Sack
(2005) with uniform spacing. The links are never stored: a node's links are
made from its triangles when it is relaxed (``Graph.node_tris``), so memory
grows with nodes, not links.

**Cost of a link** from p to q across triangle t: the model's time for the
horizontal run ``|q - p|`` in plan and the rise ``q.z - p.z``, times the
cost factor of t's face. A triangle can be crossed when its face's cost factor
is finite and positive, its unit normal points up and its slope (from the
normal) is at most ``max_slope``: a wall, an overhang's underside or ground
steeper than Max Slope is impassable. So normals must point out of solids, as
for hydrology. The running slope of any segment inside a triangle is at most
the triangle's slope, so the face test also bounds every segment's slope. The
wheelchair model also refuses any segment whose running gradient is above its
limit, either way.

**The search** is label-correcting Dijkstra in buckets (delta-stepping, Meyer
and Sanders 2003), vectorised: each pass takes every active node within
``delta`` of the smallest active time, relaxes all their links at once and
keeps the minimum per target (``np.minimum.at``). Nodes that improve become
active again, so the result is the exact shortest-path tree of the directed
graph, as Dijkstra's (the validation compares it with networkx). ``delta``
only trades passes for re-relaxations. A route-only search stops once no
active node is quicker than the best way to End.

**Start and End** snap to the closest point of the nearest crossable triangle
(3D distance), which joins the graph through straight segments to that
triangle's nodes. A face's accumulated cost is the time to its centre: the
best of its triangles' nodes plus the straight segment to the triangle's
centroid (a face of several triangles takes the quickest).

Models, per directed segment of horizontal run ``d`` (m) and rise ``dh`` (m),
``S = dh / d``:

* ``TOBLER``: Tobler (1993), ``v = 6 exp(-3.5 |S + 0.05|)`` km/h, ``t = d / v``;
* ``LANGMUIR``: GRASS ``r.walk``'s movement time (Aitken 1977, Langmuir 1984):
  ``t = a d + b dh`` uphill, ``a d + c dh`` for ``slope_factor < S < 0`` and
  ``a d + d' dh`` below, with r.walk's defaults a 0.72 s/m, b 6.0, c 1.9998,
  d' -1.9998 and slope_factor -0.2125 (tan 12 degrees);
* ``NAISMITH``: Naismith (1892), 5 km/h on the level plus an hour per 600 m of
  ascent: ``t = 0.72 d + 6 max(dh, 0)``; descents add nothing;
* ``WHEELCHAIR``: a running gradient limit of 1:12 (8.33 %) both ways, the
  maximum ramp slope of the ADA Standards (2010, section 405.2) and of most
  building codes, and speeds measured for manual wheelchairs by Jahanian et al.
  (2016): 1.12 m/s on the level, 0.59 m/s up a 4.8 degree ramp. Uphill the
  speed falls linearly in S between those two; level and downhill it is the
  level speed (a controlled descent). ``t = d / v``.

Times are seconds. Limits: no cross-slope limit (the ADA's 1:48), no
connectors between objects (one connected surface), a Start or End on
impassable ground snaps to the nearest crossable triangle. Accuracy:
``docs/validation/least-cost.md``.
"""

from __future__ import annotations

import math
from collections.abc import Generator
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np


class LeastCostError(ValueError):
    """The surface or the settings cannot be routed (no faces, bad input)."""


class Model(StrEnum):
    TOBLER = "TOBLER"
    LANGMUIR = "LANGMUIR"
    NAISMITH = "NAISMITH"
    WHEELCHAIR = "WHEELCHAIR"


TOBLER_KMH = 6.0
TOBLER_K = 3.5
TOBLER_S0 = 0.05
"""Tobler (1993): v = 6 exp(-3.5 |S + 0.05|) km/h, fastest 5 % downhill."""

R_WALK = {"a": 0.72, "b": 6.0, "c": 1.9998, "d": -1.9998, "slope_factor": -0.2125}
"""GRASS r.walk's default ``walk_coeff`` and ``slope_factor`` (Langmuir 1984)."""

NAISMITH = {"a": 0.72, "b": 6.0}
"""Naismith (1892): 5 km/h (0.72 s/m) plus an hour per 600 m of ascent (6 s/m)."""

WHEELCHAIR_LEVEL = 1.12
WHEELCHAIR_RAMP = 0.59
WHEELCHAIR_RAMP_SLOPE = math.tan(math.radians(4.8))
"""Jahanian et al. (2016), standard wheels: 1.12 m/s level, 0.59 m/s up 4.8 degrees."""

WHEELCHAIR_MAX_GRADIENT = 1.0 / 12.0
"""ADA Standards (2010) 405.2: ramp running slope no steeper than 1:12."""

_MIN_SPEED = 1e-3  # m/s; the wheelchair's extrapolated speed never reaches 0


def segment_time(
    model: Model | str,
    run: np.ndarray,
    rise: np.ndarray,
    *,
    max_gradient: float = WHEELCHAIR_MAX_GRADIENT,
) -> np.ndarray:
    """The model's time (s) for horizontal runs ``run`` (m) rising ``rise`` (m).

    ``inf`` where the segment is impassable: a vertical step (no run, some
    rise), or for the wheelchair a gradient steeper than ``max_gradient``.
    A segment with neither run nor rise takes no time.
    """
    model = Model(model)
    run = np.asarray(run, dtype=np.float64)
    rise = np.asarray(rise, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        slope = rise / run
        if model is Model.TOBLER:
            speed = TOBLER_KMH / 3.6 * np.exp(-TOBLER_K * np.abs(slope + TOBLER_S0))
            t = run / speed
        elif model is Model.LANGMUIR:
            w = R_WALK
            down = np.where(slope > w["slope_factor"], w["c"], w["d"])
            t = w["a"] * run + np.where(rise >= 0, w["b"], down) * rise
        elif model is Model.NAISMITH:
            t = NAISMITH["a"] * run + NAISMITH["b"] * np.maximum(rise, 0.0)
        else:
            drop = (WHEELCHAIR_LEVEL - WHEELCHAIR_RAMP) / WHEELCHAIR_RAMP_SLOPE
            speed = np.maximum(WHEELCHAIR_LEVEL - drop * np.maximum(slope, 0.0), _MIN_SPEED)
            t = np.where(np.abs(slope) <= max_gradient, run / speed, np.inf)
    t = np.where(run > 0, t, np.where(rise == 0, 0.0, np.inf))
    return t


# --- the graph -------------------------------------------------------------------------------


@dataclass(slots=True)
class Graph:
    """Nodes on a triangulated surface and, per triangle, the nodes on its boundary.

    ``nodes`` are the vertices (first ``n_vertices``) then the Steiner points;
    ``tri_nodes[t]`` lists triangle t's corners then its edges' Steiner points;
    ``node_tris`` is the CSR incidence of nodes in crossable triangles only.
    """

    nodes: np.ndarray  # (N, 3) float64
    n_vertices: int
    triangles: np.ndarray  # (T, 3) int64 vertex ids
    tri_face: np.ndarray  # (T,) int64: the face each triangle belongs to
    n_faces: int
    tri_nodes: np.ndarray  # (T, m) int32 (int64 past 2**31 nodes)
    factor: np.ndarray  # (T,) float64: the cost factor, inf where impassable
    tri_indptr: np.ndarray  # (N + 1,) int64
    tri_index: np.ndarray  # (K,) as tri_nodes: crossable triangles around each node
    model: Model
    max_gradient: float
    steiner: int
    stats: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.nodes)

    @property
    def crossable(self) -> np.ndarray:
        return np.isfinite(self.factor)

    def time(self, a: np.ndarray, b: np.ndarray, factor: np.ndarray) -> np.ndarray:
        """Time (s) of straight segments from points ``a`` to ``b`` with cost ``factor``."""
        d = b - a
        run = np.hypot(d[..., 0], d[..., 1])
        return segment_time(self.model, run, d[..., 2], max_gradient=self.max_gradient) * factor


def build_graph(
    positions: np.ndarray,
    triangles: np.ndarray,
    *,
    model: Model | str = Model.TOBLER,
    max_slope: float = 30.0,
    steiner: int = 3,
    tri_face: np.ndarray | None = None,
    face_factor: np.ndarray | None = None,
    flip: np.ndarray | None = None,
    max_gradient: float = WHEELCHAIR_MAX_GRADIENT,
) -> Graph:
    """The routing graph of a triangulated surface.

    ``positions`` (V, 3) binary64; ``triangles`` (T, 3) vertex ids, wound so
    the normal points up out of the ground (``flip`` per triangle reverses
    it, for a mirrored object); ``tri_face`` maps triangles to faces (default:
    each triangle is a face); ``face_factor`` multiplies the time on each face
    (0, negative or not finite: impassable). ``max_slope`` in degrees.
    """
    positions = np.asarray(positions, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    n_t = len(triangles)
    if n_t == 0:
        raise LeastCostError("The surface has no faces.")
    steiner = int(steiner)
    if steiner < 0:
        raise LeastCostError("Steiner points per edge cannot be negative.")
    if not 0.0 <= max_slope < 90.0:
        raise LeastCostError("Max Slope must be at least 0° and below 90°.")
    tri_face = (
        np.arange(n_t, dtype=np.int64) if tri_face is None else np.asarray(tri_face, np.int64)
    )
    n_faces = int(tri_face.max()) + 1
    if face_factor is None:
        face_factor = np.ones(n_faces)
    face_factor = np.asarray(face_factor, dtype=np.float64)
    if len(face_factor) < n_faces:
        raise LeastCostError("The cost factors have fewer values than the faces.")

    p = positions[triangles]
    normal = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
    if flip is not None:
        normal[np.asarray(flip, bool)] *= -1.0
    length = np.linalg.norm(normal, axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        nz = normal[:, 2] / length
    walkable = (length > 0) & (nz > 0) & (nz >= math.cos(math.radians(max_slope)) - 1e-12)
    f = face_factor[tri_face]
    ok = walkable & np.isfinite(f) & (f > 0)
    factor = np.where(ok, f, np.inf)

    # Unique edges, and each triangle side's edge and direction.
    a = triangles
    b = np.roll(triangles, -1, axis=1)
    lo, hi = np.minimum(a, b).ravel(), np.maximum(a, b).ravel()
    n_v = len(positions)
    edges, side_edge = np.unique(lo * n_v + hi, return_inverse=True)
    side_edge = side_edge.reshape(n_t, 3)
    forward = a < b  # the side runs from the edge's lower vertex id to its higher
    e_lo, e_hi = edges // n_v, edges % n_v
    k = steiner
    if k:
        frac = np.arange(1, k + 1, dtype=np.float64) / (k + 1)
        pa, pb = positions[e_lo], positions[e_hi]
        steiner_pts = (pa[:, None, :] + frac[None, :, None] * (pb - pa)[:, None, :]).reshape(-1, 3)
        nodes = np.concatenate([positions, steiner_pts])
        ids = n_v + side_edge[..., None] * k + np.arange(k)  # (T, 3, k), lower id first
        ids = np.where(forward[..., None], ids, ids[..., ::-1])
        tri_nodes = np.concatenate([triangles, ids.reshape(n_t, 3 * k)], axis=1)
    else:
        nodes = positions.copy()
        tri_nodes = triangles.copy()

    # Incidence of nodes in crossable triangles (CSR). int32 ids halve the memory.
    index_type = np.int32 if len(nodes) < 2**31 else np.int64
    tri_nodes = tri_nodes.astype(index_type)
    m = tri_nodes.shape[1]
    live = np.flatnonzero(ok)
    flat = tri_nodes[live].ravel()
    order = np.argsort(flat, kind="stable")
    tri_index = np.repeat(live, m)[order]
    tri_indptr = np.zeros(len(nodes) + 1, dtype=np.int64)
    np.cumsum(np.bincount(flat, minlength=len(nodes)), out=tri_indptr[1:])
    stats = {
        "triangles": n_t,
        "faces": n_faces,
        "crossable_triangles": int(ok.sum()),
        "too_steep": int((~walkable).sum()),
        "impassable_cost": int((walkable & ~ok).sum()),
        "nodes": len(nodes),
        "edges": len(edges),
        "links": int(len(live) * m * (m - 1)),
    }
    return Graph(
        nodes=nodes,
        n_vertices=n_v,
        triangles=triangles,
        tri_face=tri_face,
        n_faces=n_faces,
        tri_nodes=tri_nodes,
        factor=factor,
        tri_indptr=tri_indptr,
        tri_index=tri_index.astype(index_type),
        model=Model(model),
        max_gradient=float(max_gradient),
        steiner=k,
        stats=stats,
    )


def links(graph: Graph) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Every directed link ``(u, v, time)`` of the graph, the cheaper of duplicates.

    For validation (networkx on the same graph) and small meshes: it holds
    ``T m (m - 1)`` links before de-duplication.
    """
    live = np.flatnonzero(graph.crossable)
    tn = graph.tri_nodes[live]
    m = tn.shape[1]
    i, j = np.nonzero(~np.eye(m, dtype=bool))
    u = tn[:, i].ravel()
    v = tn[:, j].ravel()
    t = np.repeat(live, len(i))
    w = graph.time(graph.nodes[u], graph.nodes[v], graph.factor[t])
    keep = np.isfinite(w)
    u, v, w = u[keep], v[keep], w[keep]
    order = np.lexsort((w, v, u))
    u, v, w = u[order], v[order], w[order]
    first = np.ones(len(u), dtype=bool)
    first[1:] = (u[1:] != u[:-1]) | (v[1:] != v[:-1])
    return u[first], v[first], w[first]


# --- snapping points to the surface -------------------------------------------------------------


def closest_on_triangles(point: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """The closest point to ``point`` on each triangle ``tri`` (T, 3, 3) (Ericson 2005, 5.1.5)."""
    p = np.asarray(point, dtype=np.float64)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ij,ij->i", ab, ap)
    d2 = np.einsum("ij,ij->i", ac, ap)
    bp = p - b
    d3 = np.einsum("ij,ij->i", ab, bp)
    d4 = np.einsum("ij,ij->i", ac, bp)
    cp = p - c
    d5 = np.einsum("ij,ij->i", ab, cp)
    d6 = np.einsum("ij,ij->i", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = va + vb + vc
        v = np.where(denom != 0, vb / denom, 0.0)
        w = np.where(denom != 0, vc / denom, 0.0)
        out = a + ab * v[:, None] + ac * w[:, None]  # inside the face
        # Edge regions, then vertex regions (the later assignments win).
        t_ab = d1 / (d1 - d3)
        on = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        out = np.where(on[:, None], a + t_ab[:, None] * ab, out)
        t_ac = d2 / (d2 - d6)
        on = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        out = np.where(on[:, None], a + t_ac[:, None] * ac, out)
        t_bc = (d4 - d3) / ((d4 - d3) + (d5 - d6))
        on = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        out = np.where(on[:, None], b + t_bc[:, None] * (c - b), out)
    out = np.where(((d1 <= 0) & (d2 <= 0))[:, None], a, out)
    out = np.where(((d3 >= 0) & (d4 <= d3))[:, None], b, out)
    out = np.where(((d6 >= 0) & (d5 <= d6))[:, None], c, out)
    return out


@dataclass(slots=True)
class Anchor:
    """A point snapped onto the surface: its triangle, where it lands and how far it moved."""

    point: np.ndarray  # (3,) the given point
    on_surface: np.ndarray  # (3,)
    triangle: int
    distance: float


def snap(graph: Graph, point, chunk: int = 1 << 20) -> Anchor:
    """The closest point of the crossable triangles to ``point``."""
    point = np.asarray(point, dtype=np.float64)
    live = np.flatnonzero(graph.crossable)
    if not len(live):
        raise LeastCostError(
            "No face can be crossed: every face is steeper than Max Slope, faces down or has "
            "a cost of 0 or no data."
        )
    best_d, best_t, best_q = np.inf, -1, point
    for lo in range(0, len(live), chunk):
        t = live[lo : lo + chunk]
        q = closest_on_triangles(point, graph.nodes[graph.triangles[t]])
        d = np.einsum("ij,ij->i", q - point, q - point)
        i = int(np.argmin(d))
        if d[i] < best_d:
            best_d, best_t, best_q = float(d[i]), int(t[i]), q[i]
    return Anchor(point, best_q, best_t, math.sqrt(best_d))


# --- the search ----------------------------------------------------------------------------------


@dataclass(slots=True)
class Search:
    """Times from Start to every node, and the tree that reaches them."""

    start: Anchor
    time: np.ndarray  # (N,) s; inf where unreached
    pred: np.ndarray  # (N,) int64: previous node, -1 from Start (or unreached)
    passes: int
    relaxed: int
    stopped_early: bool


def _ranges(starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    total = int(counts.sum())
    first = np.cumsum(counts) - counts
    return np.repeat(starts - first, counts) + np.arange(total, dtype=np.int64)


def _typical_link(graph: Graph) -> float:
    """A typical link time, from a sample of crossable triangles' first sides."""
    live = np.flatnonzero(graph.crossable)
    sample = live[:: max(1, len(live) // 4096)]
    tri = graph.nodes[graph.triangles[sample]]
    level = segment_time(graph.model, np.ones(1), np.zeros(1))[0]
    run = np.linalg.norm(tri[:, 1] - tri[:, 0], axis=1) / (graph.steiner + 1)
    return float(np.median(run * level * graph.factor[sample])) or 1.0


def _seed(graph: Graph, start: Anchor) -> tuple[np.ndarray, np.ndarray]:
    t = start.triangle
    nodes = graph.tri_nodes[t]
    cost = graph.time(start.on_surface[None], graph.nodes[nodes], graph.factor[t])
    return nodes, cost


def _to_end(graph: Graph, end: Anchor) -> tuple[np.ndarray, np.ndarray]:
    t = end.triangle
    nodes = graph.tri_nodes[t]
    cost = graph.time(graph.nodes[nodes], end.on_surface[None], graph.factor[t])
    return nodes, cost


def search(
    graph: Graph,
    start: Anchor,
    *,
    end: Anchor | None = None,
    delta: float | None = None,
    max_batch: int = 1 << 21,
) -> Search:
    """Least times from ``start`` to every node; with ``end``, only as far as End needs."""
    steps = search_steps(graph, start, end=end, delta=delta, max_batch=max_batch)
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def search_steps(
    graph: Graph,
    start: Anchor,
    *,
    end: Anchor | None = None,
    delta: float | None = None,
    max_batch: int = 1 << 21,
) -> Generator[int, None, Search]:
    """:func:`search` one pass at a time: yields the passes done, returns the Search.

    For a modal operator that keeps Blender responsive and can be cancelled.
    """
    n = graph.n
    time = np.full(n, np.inf)
    pred = np.full(n, -1, dtype=np.int64)
    nodes, cost = _seed(graph, start)
    np.minimum.at(time, nodes, cost)
    if delta is None:
        delta = 2.0 * _typical_link(graph)
    active_flag = np.zeros(n, dtype=bool)
    active = np.unique(nodes[np.isfinite(cost)])
    active_flag[active] = True
    end_nodes = end_cost = None
    if end is not None:
        end_nodes, end_cost = _to_end(graph, end)
    indptr, index, tri_nodes = graph.tri_indptr, graph.tri_index, graph.tri_nodes
    m = tri_nodes.shape[1]
    passes = relaxed = 0
    stopped = False
    while len(active):
        t_act = time[active]
        lo = float(t_act.min())
        if end_nodes is not None and lo >= float((time[end_nodes] + end_cost).min()):
            stopped = True
            break
        take = t_act < lo + delta
        frontier = active[take]
        # Keep each pass's link arrays bounded.
        counts = indptr[frontier + 1] - indptr[frontier]
        if int(counts.sum()) * m > max_batch:
            cut = max(1, int(np.searchsorted(np.cumsum(counts) * m, max_batch)))
            order = np.argsort(t_act[take], kind="stable")
            keep = np.zeros(len(frontier), dtype=bool)
            keep[order[:cut]] = True
            take[np.flatnonzero(take)[~keep]] = False
            frontier = active[take]
            counts = indptr[frontier + 1] - indptr[frontier]
        active = active[~take]
        active_flag[frontier] = False
        passes += 1
        yield passes
        # One row per (node, triangle): the node's links to the triangle's m nodes. A link
        # to itself costs nothing, so it never improves and needs no mask.
        rep = np.repeat(np.arange(len(frontier)), counts)
        tri = index[_ranges(indptr[frontier], counts)]
        u = frontier[rep]
        v = tri_nodes[tri]
        relaxed += v.size
        leg = graph.time(graph.nodes[u][:, None, :], graph.nodes[v], graph.factor[tri][:, None])
        nd = (time[u][:, None] + leg).ravel()
        v = v.ravel()
        better = np.flatnonzero(nd < time[v])
        if not len(better):
            continue
        u, v, nd = u[better // m], v[better], nd[better]
        np.minimum.at(time, v, nd)
        won = nd == time[v]
        pred[v[won]] = u[won]
        changed = np.unique(v[won])
        new = changed[~active_flag[changed]]
        active_flag[new] = True
        active = np.concatenate([active, new])
    return Search(start, time, pred, passes, relaxed, stopped)


# --- results -------------------------------------------------------------------------------


@dataclass(slots=True)
class Route:
    """The quickest route from Start to End: points on the surface and the time at each."""

    points: np.ndarray  # (P, 3)
    elapsed: np.ndarray  # (P,) s from Start
    nodes: np.ndarray  # (P - 2,) graph nodes between Start and End

    @property
    def time(self) -> float:
        return float(self.elapsed[-1])

    @property
    def length(self) -> float:
        return float(np.linalg.norm(np.diff(self.points, axis=0), axis=1).sum())


def route(graph: Graph, found: Search, end: Anchor) -> Route | None:
    """The quickest route to ``end``, or None when End cannot be reached."""
    nodes, cost = _to_end(graph, end)
    total = found.time[nodes] + cost
    i = int(np.argmin(total))
    best = float(total[i])
    start = found.start
    direct = np.inf
    if start.triangle == end.triangle:
        direct = float(graph.time(start.on_surface, end.on_surface, graph.factor[end.triangle]))
    if not math.isfinite(min(best, direct)):
        return None
    if direct <= best:
        points = np.stack([start.on_surface, end.on_surface])
        return Route(points, np.array([0.0, direct]), np.zeros(0, dtype=np.int64))
    # Walk the predecessor tree back to Start (a node of the start triangle).
    chain = [int(nodes[i])]
    while found.pred[chain[-1]] >= 0:
        chain.append(int(found.pred[chain[-1]]))
        if len(chain) > graph.n:
            raise LeastCostError("The route does not lead back to Start.")
    path = np.array(chain[::-1], dtype=np.int64)
    points = np.concatenate([start.on_surface[None], graph.nodes[path], end.on_surface[None]])
    elapsed = np.concatenate([[0.0], found.time[path], [best]])
    return Route(points, elapsed, path)


def face_times(graph: Graph, found: Search) -> np.ndarray:
    """Time (s) from Start to each face's centre; NaN where it cannot be reached."""
    live = np.flatnonzero(graph.crossable)
    centroid = graph.nodes[graph.triangles[live]].mean(axis=1)
    tri_t = np.full(len(graph.triangles), np.inf)
    m = graph.tri_nodes.shape[1]
    chunk = max(1, (1 << 22) // m)
    for lo in range(0, len(live), chunk):
        t = live[lo : lo + chunk]
        tn = graph.tri_nodes[t]
        c = centroid[lo : lo + chunk]
        leg = graph.time(graph.nodes[tn], c[:, None, :], graph.factor[t][:, None])
        tri_t[t] = (found.time[tn] + leg).min(axis=1)
    s = found.start
    own = graph.time(
        s.on_surface,
        graph.nodes[graph.triangles[s.triangle]].mean(axis=0),
        graph.factor[s.triangle],
    )
    tri_t[s.triangle] = min(tri_t[s.triangle], float(own))
    out = np.full(graph.n_faces, np.inf)
    np.minimum.at(out, graph.tri_face, tri_t)
    out[~np.isfinite(out)] = np.nan
    return out


def bands(times: np.ndarray, step: float) -> tuple[np.ndarray, tuple[str, ...]]:
    """Isochrone bands of ``times`` (s) every ``step`` seconds: category ids (0 for no
    time) and their labels in minutes."""
    if step <= 0:
        raise LeastCostError("The isochrone step must be above 0.")
    finite = np.isfinite(times)
    ids = np.zeros(len(times), dtype=np.int32)
    ids[finite] = (np.floor(times[finite] / step) + 1).astype(np.int32)
    top = int(ids.max(initial=0))
    minutes = step / 60.0
    labels = ["Unreached"] + [
        f"{_fmt(i * minutes)}–{_fmt((i + 1) * minutes)} min" for i in range(top)
    ]
    return ids, tuple(labels)


def _fmt(value: float) -> str:
    return f"{round(value, 4):g}"  # a float32 step of 0.1 min is 0.10000000149
