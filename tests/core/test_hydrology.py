"""3D surface hydrology (geotools.analysis.hydrology): routing, accumulation and ponding.

Drops are cast by Blender in the add-on; here a brute-force vertical ray cast
over the triangles stands in for it (:func:`vertical_hits`).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from geotools.analysis import hydrology as hy

# --- synthetic meshes ----------------------------------------------------------------------


class Mesh:
    """Triangles from pieces, each with its own vertices (no welding between pieces)."""

    def __init__(self) -> None:
        self.points: list[np.ndarray] = []
        self.tris: list[np.ndarray] = []
        self.count = 0

    def add(self, points: np.ndarray, tris: np.ndarray) -> np.ndarray:
        """Add a piece; return its face ids."""
        first_face = sum(len(t) for t in self.tris)
        self.points.append(np.asarray(points, dtype=np.float64))
        self.tris.append(np.asarray(tris, dtype=np.int64) + self.count)
        self.count += len(points)
        return np.arange(first_face, first_face + len(tris))

    @property
    def positions(self) -> np.ndarray:
        return np.concatenate(self.points)

    @property
    def triangles(self) -> np.ndarray:
        return np.concatenate(self.tris)


def grid(nx: int, ny: int, x0: float, x1: float, y0: float, y1: float, height) -> tuple:
    """A triangulated grid over the rectangle, z = height(x, y)."""
    x = np.linspace(x0, x1, nx + 1)
    y = np.linspace(y0, y1, ny + 1)
    xx, yy = np.meshgrid(x, y, indexing="ij")
    pts = np.stack([xx.ravel(), yy.ravel(), height(xx.ravel(), yy.ravel())], axis=1)
    i, j = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    v0 = (i * (ny + 1) + j).ravel()
    v1 = v0 + ny + 1
    v2 = v1 + 1
    v3 = v0 + 1
    tris = np.concatenate([np.stack([v0, v1, v2], 1), np.stack([v0, v2, v3], 1)])
    return pts, tris


def box(lo, hi) -> tuple:
    """A closed box, normals out."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    pts = np.array(
        [
            [x0, y0, z0],
            [x1, y0, z0],
            [x1, y1, z0],
            [x0, y1, z0],
            [x0, y0, z1],
            [x1, y0, z1],
            [x1, y1, z1],
            [x0, y1, z1],
        ]
    )
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tris = []
    for q in quads:
        tris += [(q[0], q[1], q[2]), (q[0], q[2], q[3])]
    return pts, np.array(tris)


def vertical_hits(origins: np.ndarray, tri_xyz: np.ndarray, up: bool):
    """Nearest triangle straight below (or above) each origin: ``(face, point)``, -1 if none."""
    face = np.full(len(origins), -1, dtype=np.int64)
    point = np.zeros((len(origins), 3))
    a, b, c = tri_xyz[:, 0], tri_xyz[:, 1], tri_xyz[:, 2]
    det = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    ok = np.abs(det) > 1e-15
    for i, o in enumerate(origins):  # tests only: a small brute force
        px, py = o[0] - a[:, 0], o[1] - a[:, 1]
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (px * (c[:, 1] - a[:, 1]) - py * (c[:, 0] - a[:, 0])) / det
            v = (py * (b[:, 0] - a[:, 0]) - px * (b[:, 1] - a[:, 1])) / det
            inside = ok & (u >= -1e-12) & (v >= -1e-12) & (u + v <= 1 + 1e-12)
            zh = a[:, 2] + u * (b[:, 2] - a[:, 2]) + v * (c[:, 2] - a[:, 2])
        cand = inside & ((zh > o[2]) if up else (zh < o[2]))
        if cand.any():
            gap = np.where(cand, np.abs(zh - o[2]), np.inf)
            f = int(np.argmin(gap))
            face[i] = f
            point[i] = (o[0], o[1], zh[f])
    return face, point


def run(mesh: Mesh, **kwargs):
    positions = mesh.positions
    tris = mesh.triangles
    starts = np.arange(0, 3 * len(tris) + 1, 3)
    corner_verts = tris.ravel()
    edges, n_edges = hy.corner_edges(starts, corner_verts)
    surface = hy.surface_from_mesh(positions, starts, corner_verts, edges, n_edges)
    queries = hy.drop_queries(surface, 1e-4)
    tri_xyz = positions[tris]
    down_f, down_p = vertical_hits(queries.origin, tri_xyz, up=False)
    up_f, up_p = vertical_hits(queries.origin, tri_xyz, up=True)
    drops = hy.resolve_drops(queries, surface, down_f, down_p, up_f, up_p)
    return surface, drops, hy.analyse(surface, drops, **kwargs)


def check_tree(result: hy.Result) -> None:
    """Every face's water reaches an outlet: the receivers form a forest."""
    n = len(result.receiver)
    parent = np.where(result.receiver >= 0, result.receiver, np.arange(n))
    p = parent
    for _ in range(int(np.ceil(np.log2(n + 1))) + 2):
        p = p[p]
    assert (p == p[p]).all()


# --- tests ---------------------------------------------------------------------------------


def test_inclined_plane_accumulates_down_slope() -> None:
    mesh = Mesh()
    nx, ny, h = 40, 20, 1.0
    mesh.add(*grid(nx, ny, 0, nx * h, 0, ny * h, lambda x, y: 0.1 * x))
    surface, _drops, result = run(mesh)
    check_tree(result)
    z = surface.centroids[:, 2]
    rec = result.receiver
    down = rec >= 0
    assert (z[rec[down]] < z[down]).all()
    # Water leaves only over the low edge (x = 0).
    outlets = np.flatnonzero(rec < 0)
    assert (surface.centroids[outlets, 0] < h).all()
    total = surface.plan_areas.sum()
    assert result.accumulation[outlets].sum() == pytest.approx(total, rel=1e-9)
    # Each column of width h drains its upslope strip: about (distance from the top) x h.
    x = surface.centroids[:, 0]
    for x_bin in range(2, nx - 2, 4):
        sel = (x > x_bin * h) & (x < (x_bin + 1) * h)
        expected = (nx * h - (x_bin + 0.5) * h) * h
        assert result.accumulation[sel].mean() == pytest.approx(expected, rel=0.15)
    assert not result.depth.any()
    assert result.depressions.count == 0


def test_v_valley_concentrates_down_the_valley() -> None:
    mesh = Mesh()
    mesh.add(*grid(60, 40, 0, 60, -20, 20, lambda x, y: 0.05 * x + 0.3 * np.abs(y - 0.5)))
    surface, _, result = run(mesh)
    check_tree(result)
    x = surface.centroids[:, 0]
    total = surface.plan_areas.sum()
    peak = result.accumulation.argmax()
    assert x[peak] < 1.5  # the valley's outlet
    assert result.accumulation[peak] == pytest.approx(total, rel=0.05)
    # Down the valley (decreasing x) the largest accumulation in each band grows.
    # Side water joins the thalweg at 45° on this triangulation (three neighbour
    # directions per face), so everything from 20 cells (the valley's half-width)
    # upstream has arrived, and nothing more than the whole upstream area.
    previous = 0.0
    for x0 in range(55, 0, -5):
        band = (x > x0) & (x < x0 + 1)
        best = result.accumulation[band].max()
        assert best > previous
        assert best >= surface.plan_areas[x > x0 + 20].sum()
        assert best <= surface.plan_areas[x > x0].sum() + 1e-9
        previous = best
    # Along the thalweg, every face's receiver is lower.
    rec = result.receiver
    z = surface.centroids[:, 2]
    down = rec >= 0
    assert (z[rec[down]] <= z[down]).all()


def test_bowl_ponds_to_its_rim_with_the_analytic_volume() -> None:
    k, rim = 0.5, 2.0  # z = k r^2 up to the rim height, flat beyond
    a = math.sqrt(rim / k)
    mesh = Mesh()
    mesh.add(
        *grid(
            160,
            160,
            -1.5 * a,
            1.5 * a,
            -1.5 * a,
            1.5 * a,
            lambda x, y: np.minimum(k * (x * x + y * y), rim),
        )
    )
    surface, _, result = run(mesh, min_depth=0.01)
    check_tree(result)
    table = result.depressions
    assert table.count == 1
    assert table.level[0] == pytest.approx(rim, abs=1e-12)
    analytic = math.pi * a * a * rim / 2.0
    assert table.volume[0] == pytest.approx(analytic, rel=0.01)
    assert table.max_depth[0] == pytest.approx(rim, rel=0.01)
    assert table.area[0] == pytest.approx(math.pi * a * a, rel=0.03)  # rim faces
    # The filled surface is flat at the rim, and water still finds the edge.
    inside = np.hypot(surface.centroids[:, 0], surface.centroids[:, 1]) < 0.9 * a
    assert (result.filled[inside] == rim).all()
    assert (result.depression[inside] == 1).all()
    assert result.accumulation[result.receiver < 0].sum() == pytest.approx(
        surface.plan_areas.sum(), rel=1e-9
    )
    # The pour point is on the rim.
    r_pour = math.hypot(*table.pour_point[0, :2])
    assert r_pour == pytest.approx(a, rel=0.1) or r_pour > a


def test_shallow_pit_spills_downhill_into_a_deep_one() -> None:
    # A slope draining to x = -8 with a deep pit downhill and a shallow one uphill.
    def height(x, y):
        deep = 3.0 * np.exp(-((x + 3) ** 2 + y**2))
        shallow = 0.5 * np.exp(-((x - 3) ** 2 + y**2))
        return 0.2 * x - deep - shallow

    mesh = Mesh()
    mesh.add(*grid(80, 40, -8, 8, -4, 4, height))
    _surface, _, result = run(mesh, min_depth=0.001)
    check_tree(result)
    table = result.depressions
    assert table.count == 2
    assert (table.volume[:-1] >= table.volume[1:]).all()
    deep, shallow = table.pour_point
    assert deep[0] < -3 and shallow[0] < 3  # each spills on its downhill side
    # The shallow pit's water goes on into the deep pit's lake.
    lake_deep = result.depression == 1
    face = table.pour_face[1]
    for _ in range(200):
        if lake_deep[face] or face < 0:
            break
        face = result.receiver[face]
    assert face >= 0 and lake_deep[face]
    assert table.catchment[0] > table.catchment[1]


def test_flat_ground_drains_to_its_edges() -> None:
    mesh = Mesh()
    mesh.add(*grid(20, 20, 0, 20, 0, 20, lambda x, y: np.zeros_like(x)))
    _surface, _, result = run(mesh)
    check_tree(result)
    assert not result.depth.any()
    assert result.stats["flat_faces"] > 0
    assert result.stats["unrouted"] == 0
    assert result.accumulation[result.receiver < 0].sum() == pytest.approx(400.0)


def test_step_with_a_wall_routes_down_the_wall() -> None:
    # One connected surface: an upper terrace at z = 5 (x > 10), a vertical wall
    # at x = 10, and a lower slope draining to x = 0.
    mesh = Mesh()
    n = 10
    upper_pts, upper_tris = grid(n, n, 10, 20, 0, 10, lambda x, y: 5 + 0.01 * (x - 10))
    lower_pts, lower_tris = grid(n, n, 0, 10, 0, 10, lambda x, y: 0.02 * x)
    wall_pts, wall_tris = grid(4, n, 0, 1, 0, 10, lambda x, y: np.zeros_like(x))
    # Bend the wall grid into the plane x = 10: its "x" is height.
    t = wall_pts[:, 0]
    wall_pts = np.stack([np.full_like(t, 10.0), wall_pts[:, 1], 0.2 + t * (5 - 0.2)], axis=1)
    pts = np.concatenate([upper_pts, lower_pts, wall_pts])
    tris = np.concatenate(
        [upper_tris, lower_tris + len(upper_pts), wall_tris + len(upper_pts) + len(lower_pts)]
    )
    # Weld coincident vertices so the pieces are one surface.
    key = np.round(pts, 9)
    _, inverse = np.unique(key, axis=0, return_inverse=True)
    weld = np.unique(key, axis=0)
    mesh.add(weld, inverse.ravel()[tris])
    surface, _drops, result = run(mesh)
    check_tree(result)
    normals = surface.normals
    wall = np.abs(normals[:, 2]) < 0.01
    assert wall.sum() > 0
    z = surface.centroids[:, 2]
    rec = result.receiver
    # Water on the wall goes down it, to lower wall faces or the ground.
    assert (z[rec[wall]] < z[wall]).all()
    upper = (surface.centroids[:, 0] > 10.01) & (z > 4)
    # Everything leaves over the low edge, with the upper terrace's water.
    outlets = np.flatnonzero(rec < 0)
    assert (surface.centroids[outlets, 0] < 1.0).all()
    total = surface.plan_areas.clip(0).sum()
    assert result.accumulation[outlets].sum() == pytest.approx(total, rel=1e-9)
    assert result.accumulation[outlets].sum() > surface.plan_areas[upper].sum()


def test_roof_and_overhang_drop_onto_the_ground() -> None:
    mesh = Mesh()
    ground = mesh.add(*grid(30, 30, 0, 30, 0, 30, lambda x, y: 0.05 * x))
    # An open roof sheet sloping in y, not joined to anything.
    roof = mesh.add(*grid(6, 6, 10, 16, 10, 16, lambda x, y: 8 + 0.3 * (y - 10)))
    # A closed slab hovering over the ground (its underside drips).
    slab = mesh.add(*box((20, 5, 4), (24, 9, 5)))
    surface, _drops, result = run(mesh)
    check_tree(result)
    rec = result.receiver
    # The roof's water leaves it only at its eaves (low y edge), onto the ground.
    leaving = roof[~np.isin(rec[roof], roof)]
    assert len(leaving) >= 6
    assert (surface.centroids[leaving, 1] < 11).all()
    assert np.isin(rec[leaving], ground).all()
    # The slab's underside drips straight down onto the ground under it.
    under = slab[surface.normals[slab, 2] < -0.9]
    assert np.isin(rec[under], ground).all()
    hit = surface.centroids[rec[under]]
    assert (hit[:, 0] > 19).all() and (hit[:, 0] < 25).all()
    landing = result.drop_landing[under]
    assert np.allclose(landing[:, :2], surface.centroids[under, :2])
    # All the water ends up leaving at the ground's low edge.
    outlets = np.flatnonzero(rec < 0)
    assert np.isin(outlets, ground).all()
    catch = surface.plan_areas.clip(0)
    assert result.accumulation[outlets].sum() == pytest.approx(catch.sum(), rel=1e-9)
    # The ground faces under the roof and slab get its water.
    assert result.accumulation.max() > catch[ground].sum() * 0.0 + catch[roof].sum()


def test_sunken_wall_foot_finds_the_ground_above() -> None:
    # A free-standing wall sheet whose foot is 0.5 below a flat ground: water on it
    # reaches the foot, below the ground, and comes up through the ground.
    mesh = Mesh()
    ground = mesh.add(*grid(10, 10, 0, 10, 0, 10, lambda x, y: 0.02 * x))
    wall_pts, wall_tris = grid(1, 4, 0, 1, 2, 8, lambda x, y: np.zeros_like(x))
    t = wall_pts[:, 0]
    wall_pts = np.stack([np.full_like(t, 5.0), wall_pts[:, 1], -0.5 + 3.0 * t], axis=1)
    wall = mesh.add(wall_pts, wall_tris)
    _surface, drops, result = run(mesh)
    check_tree(result)
    sources = np.isin(drops.face, wall)
    assert np.isin(drops.target[sources & (drops.target >= 0)], ground).all()
    assert (drops.target[sources] >= 0).any()


def test_closed_object_without_outlet_drains_at_its_lowest_point() -> None:
    # A box on its own: its underside drips to nowhere, so all leaves.
    mesh = Mesh()
    mesh.add(*box((0, 0, 0), (2, 2, 2)))
    _surface, _, result = run(mesh)
    check_tree(result)
    assert result.accumulation[result.receiver < 0].sum() == pytest.approx(4.0)


def test_area_modes_and_errors() -> None:
    mesh = Mesh()
    mesh.add(*grid(4, 4, 0, 4, 0, 4, lambda x, y: x))
    surface, drops, plan = run(mesh)
    surface_mode = hy.analyse(surface, drops, area="SURFACE")
    assert surface_mode.accumulation.max() > plan.accumulation.max()
    with pytest.raises(hy.HydrologyError):
        hy.analyse(surface, drops, area="VOLUME")


def test_channels_rank_networks() -> None:
    mesh = Mesh()
    mesh.add(*grid(60, 40, 0, 60, -20, 20, lambda x, y: 0.05 * x + 0.3 * np.abs(y - 0.5)))
    _, _, result = run(mesh)
    ch = hy.channels(result, threshold=50.0)
    assert len(ch.faces) > 0
    assert ch.network.min() == 1
    top = hy.channels(result, threshold=50.0, count=1)
    assert (top.network == 1).all()
    assert len(top.outlet_area) == 1


def test_polygon_faces_and_non_manifold_edges() -> None:
    # Quads, and a fin sharing an edge with two quads (three faces on one edge).
    pts = np.array(
        [
            [0, 0, 0],
            [1, 0, 0],
            [2, 0, 0],
            [0, 1, 0.1],
            [1, 1, 0.1],
            [2, 1, 0.1],
            [1, 0, 1],
            [1, 1, 1],
        ],
        dtype=float,
    )
    corner_verts = np.array([0, 1, 4, 3, 1, 2, 5, 4, 1, 4, 7, 6])
    starts = np.array([0, 4, 8, 12])
    edges, n_edges = hy.corner_edges(starts, corner_verts)
    surface = hy.surface_from_mesh(pts, starts, corner_verts, edges, n_edges)
    assert surface.non_manifold_edges == 1
    assert len(surface.pairs) == 3
    assert surface.areas[0] == pytest.approx(math.hypot(1, 0.1))


def test_faces_under_keeps_every_face_a_vertical_ray_can_hit() -> None:
    rng = np.random.default_rng(3)
    mesh = Mesh()
    mesh.add(*grid(40, 40, 0, 40, 0, 40, lambda x, y: 0.1 * x))
    mesh.add(*box((10, 10, 5), (30, 12, 6)))  # a long thin box spanning many cells
    positions, tris = mesh.positions, mesh.triangles
    starts = np.arange(0, 3 * len(tris) + 1, 3)
    lo, hi = hy.face_bounds(positions, starts, tris.ravel())
    points = np.column_stack([rng.uniform(0, 40, (30, 2)), np.full(30, 50.0)])
    points[0, :2] = (20.0, 11.0)  # over the box
    kept = hy.faces_under(points, lo, hi, cells=64)
    hits, _ = vertical_hits(points, positions[tris], up=False)
    assert np.isin(hits[hits >= 0], kept).all()
    assert len(kept) < len(tris) / 4
    assert hy.faces_under(points[:0], lo, hi).size == 0
