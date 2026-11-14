"""Hydrology, contours and sections validation, the pure-Python half (docs/validation/hydrology.md).

GeoTools against closed forms, against independent re-implementations of the
algorithms it claims (a heap priority-flood, brute-force steepest descent and
accumulation, breadth-first flat routing) on the same face graph, and against
the committed outputs of WhiteboxTools 2.4.0, richdem 0.3.4 and GDAL 3.12.0
(``tests/data/validation/hydrology/references.npz``, made by
``scripts/validation/hydrology/make_references.py``). The Blender half
(``tests/blender/test_validation_hydrology.py``) runs the operators, the
Flow Path (Quick Look) preset and Export Vectors.

Every DEM is a mesh with a vertex at each cell centre, so GeoTools and the
raster tools see the same heights; what differs is where they sample them
(face centres against cell centres) and the graph water moves on (three
edge-neighbours against eight cells).
"""

from __future__ import annotations

import heapq
import math
import sys
from collections import deque
from pathlib import Path

import numpy as np
import pytest

from geotools.analysis import hydrology as hy
from geotools.analysis import slicing

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "blender"))
import _hydrology_surfaces as hs  # shared with the Blender suite and the reference script

REFERENCES = REPO / "tests" / "data" / "validation" / "hydrology" / "references.npz"


@pytest.fixture(scope="module")
def refs() -> dict[str, np.ndarray]:
    with np.load(REFERENCES) as data:
        return {k: data[k] for k in data.files}


def surface_dem(name: str, refs=None, table=hs.HYDRO):
    height, x0, x1, y0, y1, h, *_rest = table[name]
    if name == "terrain":
        x, y = hs.axes(x0, x1, y0, y1, h)
        return refs["terrain_dem"], x, y
    return hs.dem(height, x0, x1, y0, y1, h)


def run(z, x, y, **kwargs):
    """GeoTools on a single-sheet DEM mesh: every drop leaves the model (nothing is below)."""
    pts, tris = hs.grid_mesh(z, x, y)
    starts = np.arange(0, 3 * len(tris) + 1, 3)
    corner_verts = tris.ravel()
    edges, n_edges = hy.corner_edges(starts, corner_verts)
    surface = hy.surface_from_mesh(pts, starts, corner_verts, edges, n_edges)
    queries = hy.drop_queries(surface, 1e-6)
    miss = np.full(len(queries.face), -1)
    drops = hy.resolve_drops(queries, surface, miss, queries.origin, miss, queries.origin)
    return pts, tris, surface, drops, hy.analyse(surface, drops, **kwargs)


def neighbours(surface: hy.Surface) -> list[list[int]]:
    adj: list[list[int]] = [[] for _ in range(surface.n)]
    for a, b in surface.pairs.tolist():
        adj[a].append(b)
        adj[b].append(a)
    return adj


# --- the algorithms, re-implemented independently on the same face graph ----------------------


def priority_flood(surface: hy.Surface, drops: hy.Drops) -> np.ndarray:
    """Barnes, Lehman and Mulla (2014), Algorithm 1, on the face graph: seed the faces that
    drop out of the model at their own height, then pop the lowest and raise each unvisited
    neighbour to at least the popped level."""
    z = surface.centroids[:, 2]
    adj = neighbours(surface)
    heap = [(z[f], f) for f, t in zip(drops.face.tolist(), drops.target.tolist()) if t < 0]
    heapq.heapify(heap)
    filled = np.full(surface.n, np.nan)
    while heap:
        level, f = heapq.heappop(heap)
        if not np.isnan(filled[f]):
            continue
        filled[f] = level
        for g in adj[f]:
            if np.isnan(filled[g]):
                heapq.heappush(heap, (max(z[g], level), g))
    return filled


@pytest.mark.parametrize("name", ["terrain", "two_bowls", "bowl", "valley"])
def test_filling_is_a_priority_flood(name, refs) -> None:
    _pts, _tris, surface, drops, result = run(*surface_dem(name, refs))
    np.testing.assert_array_equal(result.filled, priority_flood(surface, drops))


def test_routing_is_steepest_descent_and_accumulation_sums_upstream(refs) -> None:
    _pts, _tris, surface, _drops, result = run(*surface_dem("terrain", refs))
    c, filled, rec = surface.centroids, result.filled, result.receiver
    adj = neighbours(surface)
    flats = 0
    for f in range(surface.n):
        slopes = [(filled[f] - filled[g]) / np.linalg.norm(c[f] - c[g]) for g in adj[f]]
        best = max(slopes, default=0.0)
        if best > 0:
            # Down the steepest edge-neighbour, by drop over the 3D distance between centres.
            g = rec[f]
            assert g >= 0 and math.isclose(
                (filled[f] - filled[g]) / np.linalg.norm(c[f] - c[g]), best, rel_tol=1e-12
            )
        else:
            flats += 1  # a lake or an edge face: across the flat, or out of the model
            assert rec[f] < 0 or filled[rec[f]] == filled[f]
    assert flats > 1000  # the lakes
    # Accumulation: each face's plan area plus everything that reaches it (Kahn's order).
    acc = np.maximum(surface.plan_areas, 0.0)
    indegree = np.bincount(rec[rec >= 0], minlength=surface.n)
    ready = deque(np.flatnonzero(indegree == 0).tolist())
    seen = 0
    while ready:
        f = ready.popleft()
        seen += 1
        if rec[f] >= 0:
            acc[rec[f]] += acc[f]
            indegree[rec[f]] -= 1
            if indegree[rec[f]] == 0:
                ready.append(rec[f])
    assert seen == surface.n  # a forest: no face's water goes round in a loop
    np.testing.assert_allclose(result.accumulation, acc, rtol=1e-12)
    out = rec < 0
    assert result.accumulation[out].sum() == pytest.approx(np.maximum(surface.plan_areas, 0).sum())


def test_flats_drain_by_the_shortest_way_to_their_exit() -> None:
    # A level 20 x 10 m floor walled on three sides, open to the west: every floor face
    # drains across the flat to a face one step nearer the exit (breadth-first distance).
    def height(x, y):
        wall = (x >= 19.5) | (y <= 0.5) | (y >= 9.5)
        return np.where(wall, 1.0, 0.0)

    z, x, y = hs.dem(height, 0, 20, 0, 10, 0.5)
    _pts, _tris, surface, _drops, result = run(z, x, y)
    floor = np.flatnonzero(surface.centroids[:, 2] == 0.0)
    adj = neighbours(surface)
    exits = [f for f in floor if result.receiver[f] < 0]
    dist = {f: 0 for f in exits}
    queue = deque(exits)
    on_floor = set(floor.tolist())
    while queue:
        f = queue.popleft()
        for g in adj[f]:
            if g in on_floor and g not in dist:
                dist[g] = dist[f] + 1
                queue.append(g)
    assert len(dist) == len(floor)
    assert all(surface.centroids[f, 0] < 0.5 for f in exits)  # the open west edge
    for f in floor:
        if f not in exits:
            assert dist[int(result.receiver[f])] == dist[int(f)] - 1
    assert result.depressions.count == 0


# --- analytic surfaces ------------------------------------------------------------------------


def test_inclined_plane_accumulation_grows_linearly() -> None:
    # z = -0.1 x: parallel flow east. Upslope area per unit width is the distance from the
    # high (west) edge; a face's mean accumulation, over a band across the slope, is that
    # distance times 1 m (two half-square faces per 1 m cell), plus a quarter of a cell:
    # a face's own area counts in full at its centre, a third or two thirds into the cell.
    z, x, y = surface_dem("plane")
    _pts, _tris, surface, _drops, result = run(z, x, y)
    assert result.depressions.count == 0 and not (result.depth > 0).any()
    c, rec = surface.centroids, result.receiver
    inner = rec >= 0
    assert (c[rec[inner], 2] < c[inner, 2]).all()  # always downhill
    band = np.floor(c[:, 0] / 4.0).astype(int)
    for b in np.unique(band)[1:]:
        on = band == b
        assert result.accumulation[on].mean() == pytest.approx(c[on, 0].mean() + 0.25, abs=0.01)
    out = rec < 0
    total = np.maximum(surface.plan_areas, 0).sum()
    assert result.accumulation[out].sum() == pytest.approx(total)
    assert result.accumulation[out & (c[:, 0] > x[-1] - 1)].sum() > 0.99 * total


def _band_ratios(acc, x, y, keep, length):
    band = np.floor(length / 8).astype(int)
    return np.array([acc[keep & (band == b)].mean() / length[keep & (band == b)].mean()
                     for b in np.unique(band[keep])])  # fmt: skip


def test_oblique_plane_has_the_same_direction_bias_as_d8(refs) -> None:
    # Falling 30° north of east, across the triangulation. The upslope flow length of a
    # point is min(x / cos 30°, y / sin 30°); accumulation over it, in bands, away from
    # the outlet edges (where every method runs water along the edge).
    z, x, y = surface_dem("plane30")
    _pts, _tris, surface, _drops, result = run(z, x, y)
    assert result.depressions.count == 0
    c = surface.centroids
    length = np.minimum(c[:, 0] / hs.C30, c[:, 1] / hs.S30)
    ours = _band_ratios(result.accumulation, x, y, (c[:, 0] < 56) & (c[:, 1] < 56), length)
    xx, yy = np.meshgrid(x, y, indexing="ij")
    cells = np.minimum((xx + 0.5) / hs.C30, (yy + 0.5) / hs.S30)  # cells reach half a cell out
    keep = (xx < 56) & (yy < 56)
    d8 = _band_ratios(refs["hy_plane30_wbt_d8"].astype(float), x, y, keep, cells)
    dinf = _band_ratios(refs["hy_plane30_wbt_dinf"].astype(float), x, y, keep, cells)
    # Single-direction routing (GeoTools' three neighbours, D8's eight) drifts water off the
    # fall line the same way: the ratio falls downslope, by the same amounts.
    assert np.abs(ours - d8).max() < 0.07
    assert ours[-1] < ours[1] - 0.05 and d8[-1] < d8[1] - 0.05
    # D-infinity (Tarboton 1997) splits flow between two cells and holds a steady ratio.
    assert np.ptp(dinf[1:]) < 0.03


def test_cone_flow_diverges() -> None:
    z, x, y = surface_dem("cone")
    _pts, _tris, surface, _drops, result = run(z, x, y)
    c = surface.centroids[:, :2] - 32.0
    rec = result.receiver
    inner = rec >= 0
    r = np.hypot(c[:, 0], c[:, 1])
    assert (r[rec[inner]] > r[inner]).all()  # every step away from the summit
    step = c[rec[inner]] - c[inner]
    off = np.degrees(
        np.abs(np.angle((step[:, 0] + 1j * step[:, 1]) / (c[inner, 0] + 1j * c[inner, 1])))
    )
    # Three edge-neighbours: a step can run nearly across the slope (still outward); the
    # median is 28° off radial (D8's eight directions: at most 22.5°).
    assert off.max() < 91.0 and np.median(off) < 30.0
    # Divergent: accumulation stays small. The analytic specific catchment area is r / 2;
    # a face of width ~1 m 25 m out drains about 12.5 m²; single-direction streaks
    # carry more, but nothing like a channel.
    far = (r > 20) & (r < 30)
    assert np.median(result.accumulation[far]) < 12.5 * 2


@pytest.mark.parametrize("h", [2.0, 1.0, 0.5])
def test_inverted_cone_ponds_to_its_rim(h) -> None:
    # z = r on [-32, 32]²: spills over the middle of each edge at z = 32. GeoTools' spill
    # level is the height of a face centre at the pass, h/3 inside the edge.
    z, x, y = hs.dem(hs.inverted_cone, -32, 32, -32, 32, h)
    _pts, _tris, surface, _drops, result = run(z, x, y, min_depth=0.01)
    table = result.depressions
    assert table.count == 1
    level = table.level[0]
    assert 32.0 - h / 3 - 0.02 * h < level < 32.0
    # The water below that level holds pi level³ / 3, to the discretisation.
    assert table.volume[0] == pytest.approx(math.pi * level**3 / 3, rel=0.01 * h)
    assert table.max_depth[0] == pytest.approx(level - surface.centroids[:, 2].min())


def test_two_bowls_spill_in_order() -> None:
    z, x, y = surface_dem("two_bowls")
    h = float(x[1] - x[0])
    _pts, _tris, surface, _drops, result = run(z, x, y, min_depth=0.01)
    table = result.depressions
    assert table.count == 2
    # Sort east (the saddle) and west (the edge) by pour point.
    east, west = np.argsort(-table.pour_point[:, 0])
    assert table.pour_point[east, 0] == pytest.approx(hs.TWO_BOWLS_SADDLE_X, abs=2 * h)
    assert abs(table.pour_point[east, 1]) < 2 * h
    assert table.pour_point[west, 0] < -2.5 + h
    # Levels: the saddle and the west edge, a little low (face centres at the pass).
    assert hs.TWO_BOWLS_SADDLE_Z - h < table.level[east] < hs.TWO_BOWLS_SADDLE_Z
    assert hs.TWO_BOWLS_WEST_SPILL - h < table.level[west] < hs.TWO_BOWLS_WEST_SPILL
    # Volumes: pi d² below the level actually found.
    for i, bottom in ((east, -0.3), (west, -1.0)):
        assert table.volume[i] == pytest.approx(math.pi * (table.level[i] - bottom) ** 2, rel=0.01)
    # Against the closed form at the true levels: low by the levels' bias (8 % and 6 % at
    # 5 cm cells, the bowls 2 m across).
    exact_east, exact_west = hs.two_bowls_volumes()
    assert 0.9 * exact_east < table.volume[east] < exact_east
    assert 0.9 * exact_west < table.volume[west] < exact_west
    # Spill order: the east lake's exit leads into the west lake, which leaves the model.
    labels = result.depression
    f = int(table.pour_face[east])
    for _ in range(surface.n):
        if f < 0 or labels[f] == west + 1:
            break
        f = int(result.receiver[f])
    assert f >= 0 and labels[f] == west + 1
    assert table.catchment[west] > table.catchment[east]


def test_valley_thalweg_carries_everything_but_breaks_into_shallow_pits() -> None:
    z, x, y = surface_dem("valley")
    _pts, _tris, surface, _drops, result = run(z, x, y, min_depth=0.0)
    c = surface.centroids
    total = np.maximum(surface.plan_areas, 0).sum()
    out = result.receiver < 0
    # All of it leaves at the low end (x = 0), along the thalweg.
    mouth = out & (c[:, 0] < 1.0) & (np.abs(c[:, 1]) < 1.0)
    assert result.accumulation[mouth].sum() / total > 0.9
    # Down the valley the thalweg's accumulation grows.
    thalweg = np.abs(c[:, 1]) < 0.5
    order = np.argsort(c[thalweg, 0])
    best = np.maximum.accumulate(result.accumulation[thalweg][order][::-1])[::-1]
    assert best[0] > 0.9 * total and best[-1] < 0.1 * total
    # Documented limit (docs/validation/hydrology.md): with three edge-neighbours, a V
    # along the mesh's edges is a chain of pits a third of a face deep in the side
    # slope. The raster tools see no depression at all here.
    ponded = result.depth > 0
    assert ponded.any() and (np.abs(c[ponded, 1]) < 1.0).all()
    assert result.depth.max() <= 0.3 * 1.0 / 3 + 1e-9


# --- against the reference tools ---------------------------------------------------------------


def test_the_two_priority_flood_references_agree(refs) -> None:
    for name in hs.HYDRO:
        np.testing.assert_array_equal(
            refs[f"hy_{name}_wbt_depth"], refs[f"hy_{name}_richdem_depth"], err_msg=name
        )


@pytest.mark.parametrize(
    ("name", "volume_ratio", "level_error"),
    [
        ("bowl", (0.965, 0.972), 0.34),
        ("two_bowls", (0.93, 0.97), 0.02),
        ("terrain", (0.93, 0.98), 0.06),
    ],
)
def test_filling_against_priority_flood_rasters(name, volume_ratio, level_error, refs) -> None:
    z, x, y = surface_dem(name, refs)
    _pts, tris, surface, _drops, result = run(z, x, y, min_depth=0.01)
    depth = refs[f"hy_{name}_richdem_depth"].astype(np.float64)
    m = hs.hydrology_measures(
        z, x, y, tris, surface.centroids, surface.plan_areas, result,
        refs[f"hy_{name}_wbt_depth"].astype(np.float64), depth,
        {k: refs[f"hy_{name}_wbt_{k}"] for k in ("d8", "dinf")},
    )  # fmt: skip
    # GeoTools holds a little less: its spill level is a face centre's height at the pass.
    ratio = m["geotools_volume"] / m["richdem_volume"]
    assert volume_ratio[0] < ratio < volume_ratio[1], ratio
    assert m["geotools_depressions_1cm"] == m["richdem_lakes_1cm"]
    assert m["lakes_matched"] >= 0.75 * m["geotools_depressions_1cm"]
    assert m["lake_level_vs_richdem"]["max"] < level_error
    assert np.median(m["lake_volume_ratio"]) == pytest.approx(ratio, abs=0.05)


def test_channel_accumulation_against_d8(refs) -> None:
    z, x, y = surface_dem("terrain", refs)
    _pts, tris, surface, _drops, result = run(z, x, y, min_depth=0.01)
    m = hs.hydrology_measures(
        z, x, y, tris, surface.centroids, surface.plan_areas, result,
        refs["hy_terrain_wbt_depth"].astype(np.float64),
        refs["hy_terrain_richdem_depth"].astype(np.float64),
        {k: refs[f"hy_terrain_wbt_{k}"] for k in ("d8", "dinf")},
    )  # fmt: skip
    d8 = m["channel_ratio_wbt_d8"]
    assert d8["cells"] > 50
    assert 0.95 < d8["median"] < 1.05 and d8["p90"] < 1.3
    assert 0.9 < m["channel_ratio_wbt_dinf"]["median"] < 1.3


# --- contours ----------------------------------------------------------------------------------


def contour_lines(name, refs):
    _h, _x0, _x1, _y0, _y1, _cell, interval, base = hs.CONTOURS[name]
    z, x, y = surface_dem(name, refs, hs.CONTOURS)
    pts, tris = hs.grid_mesh(z, x, y)
    return slicing.contours(pts, tris, interval, base=base, major_every=0), x, y


def gdal_contours(name, refs):
    return (
        refs[f"ct_{name}_gdal_points"].astype(np.float64),
        refs[f"ct_{name}_gdal_line"],
        refs[f"ct_{name}_gdal_elev"],
    )


def crossings(lines: slicing.Lines) -> int:
    """Pairs of segments that properly cross in plan (sharing no end)."""
    p = lines.positions[:, :2]
    a, b = p[lines.edges[:, 0]], p[lines.edges[:, 1]]

    def orient(o, u, v):
        return (u[..., 0] - o[..., 0]) * (v[..., 1] - o[..., 1]) - (u[..., 1] - o[..., 1]) * (
            v[..., 0] - o[..., 0]
        )

    count = 0
    for i in range(len(a)):
        o1 = orient(a[i], b[i], a[i + 1 :])
        o2 = orient(a[i], b[i], b[i + 1 :])
        o3 = orient(a[i + 1 :], b[i + 1 :], a[i])
        o4 = orient(a[i + 1 :], b[i + 1 :], b[i])
        count += int(((o1 * o2 < 0) & (o3 * o4 < 0)).sum())
    return count


@pytest.mark.parametrize("name", ["cone", "terrain"])
def test_contours_agree_with_gdal_contour(name, refs) -> None:
    lines, x, y = contour_lines(name, refs)
    gp, gline, gelev = gdal_contours(name, refs)
    m = hs.contour_measures(lines, gp, gline, gelev, x, y)
    h = float(x[1] - x[0])
    # Where the contour crosses a grid edge both interpolate linearly along it: the same
    # point. GDAL nudges a value exactly on a level (about 1e-6 in height here).
    assert m["gdal_vertex_to_geotools"].max() < 1e-5
    assert m["geotools_edge_vertex_to_gdal"].max() < 1e-5
    assert m["gdal_vertices_without_geotools_level"] == 0
    # Inside a cell GeoTools follows the triangles, GDAL its marching squares: the two
    # differ by up to the cell's twist, never more than a cell.
    assert m["geotools_diagonal_vertex_to_gdal"].max() < h
    assert len(np.unique(lines.line_values["elevation"])) == len(np.unique(gelev))
    # A saddle cell may be joined differently (diagonal against cell-centre average).
    assert abs(lines.count - len(gelev)) <= 1 + len(np.unique(gelev)) // 8


def test_contours_on_vertex_levels_and_gdal(refs) -> None:
    # z = x, levels on every vertex column. A vertex on a level counts as above it (GDAL's
    # convention too: it nudges such values up). GeoTools draws the highest level along
    # the edge and not the lowest; GDAL draws both.
    lines, x, y = contour_lines("plane", refs)
    gp, gline, gelev = gdal_contours("plane", refs)
    assert lines.line_values["elevation"].tolist() == [float(k) for k in range(1, 21)]
    assert sorted(gelev.tolist()) == [float(k) for k in range(21)]
    for i in range(lines.count):
        e = lines.edges[lines.edge_line == i]
        assert np.allclose(lines.positions[e.ravel(), 0], lines.line_values["elevation"][i])
    m = hs.contour_measures(lines, gp, gline, gelev, x, y)
    assert m["gdal_vertex_to_geotools"].max() < 2e-6


def test_monkey_saddle_contour_topology(refs) -> None:
    lines, x, y = contour_lines("monkey", refs)
    p = lines.positions
    # Every point exactly on its level; no segment twice; no two lines cross.
    level = lines.line_values["elevation"][lines.edge_line]
    np.testing.assert_array_equal(p[lines.edges[:, 0], 2], level)
    ends = np.sort(lines.edges, axis=1)
    key = np.round(np.concatenate([p[ends[:, 0]], p[ends[:, 1]]], 1), 12)
    assert len(np.unique(key, axis=0)) == len(key)
    assert crossings(lines) == 0
    # The TIN surface is the true one to O(h²) away from the saddle point.
    far = np.hypot(p[:, 0], p[:, 1]) > 0.3
    assert np.abs(hs.monkey(p[far, 0], p[far, 1]) - p[far, 2]).max() < 3 * 0.05**2 * 3
    # The zero level is three lines through the saddle: its points lie on x = 0 or y = ±x/√3.
    zero = np.flatnonzero(lines.line_values["elevation"] == 0.0)
    q = p[np.unique(lines.edges[np.isin(lines.edge_line, zero)])]
    gap = np.minimum(np.abs(q[:, 0]), np.abs(np.abs(q[:, 1]) - np.abs(q[:, 0]) / math.sqrt(3)))
    assert gap.max() < 0.06
    # GDAL: the same lines wherever no vertex sits exactly on a level.
    gp, gline, gelev = gdal_contours("monkey", refs)
    m = hs.contour_measures(lines, gp, gline, gelev, x, y)
    assert np.median(m["gdal_vertex_to_geotools"]) < 1e-12
    assert np.percentile(m["gdal_vertex_to_geotools"], 98) < 1e-3


@pytest.mark.parametrize("h", [0.5, 0.25, 0.125])
def test_cone_contours_converge_as_h_squared(h) -> None:
    z, x, y = hs.dem(hs.contour_cone, -10, 10, -10, 10, h)
    pts, tris = hs.grid_mesh(z, x, y)
    lines = slicing.contours(pts, tris, 1.0, base=0.25)
    p = lines.positions
    r = np.hypot(p[:, 0], p[:, 1])
    inner = r > 1.0
    err = np.abs(r[inner] - (10.0 - p[inner, 2]))
    # A chord of a circle of radius R at most 2h long sags h²/(2R) at most.
    assert err.max() < h * h / (2 * 1.0) + 1e-9
    assert lines.closed[np.abs(lines.line_values["elevation"] - 5.25) < 1e-9].all()


# --- sections ----------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["paraboloid", "cone", "terrain"])
def test_sections_against_gdal_bilinear(name, refs) -> None:
    *_box, h, path = hs.SECTIONS[name]
    z, x, y = surface_dem(name, refs, hs.SECTIONS)
    pts, tris = hs.grid_mesh(z, x, y)
    path3 = np.array([(px, py, 0.0) for px, py in path])
    lines = slicing.sections(pts, tris, path3, np.array([0, len(path3)]))
    p = lines.positions
    np.testing.assert_allclose(p[:, :2], refs[f"sc_{name}_gdal_xy"], atol=1e-12)
    diff = p[:, 2] - refs[f"sc_{name}_gdal_z"]
    # Triangles and bilinear agree on the cell's edges; inside, they differ by the cell's
    # twist b = z00 - z10 - z01 + z11 times s(1 - t) or t(1 - s) <= |b| / 4.
    i = np.clip(np.floor((p[:, 0] - x[0]) / h).astype(int), 0, len(x) - 2)
    j = np.clip(np.floor((p[:, 1] - y[0]) / h).astype(int), 0, len(y) - 2)
    twist = z[i, j] - z[i + 1, j] - z[i, j + 1] + z[i + 1, j + 1]
    assert (np.abs(diff) <= np.abs(twist) / 4 + 1e-12).all()
    if name == "paraboloid":  # x² + y²: no twist, so the two are the same surface
        assert np.abs(diff).max() < 1e-12
        np.testing.assert_allclose(p[:, 2], hs.paraboloid(p[:, 0], p[:, 1]), atol=0.05 * h * h)


def test_section_chainage_and_heights_on_a_plane() -> None:
    def plane(x, y):
        return 0.2 * x - 0.1 * y + 3.0

    z, x, y = hs.dem(plane, 0, 10, 0, 10, 0.5)
    pts, tris = hs.grid_mesh(z, x, y)
    path = np.array([[1, 2, 0], [8, 2, 0], [8, 9, 0], [1.3, 9.7, 0]], dtype=float)
    lines = slicing.sections(pts, tris, path, np.array([0, 4]))
    assert lines.count == 1  # one piece round both bends
    p = lines.positions
    chainage = lines.point_values["chainage"]
    np.testing.assert_allclose(p[:, 2], plane(p[:, 0], p[:, 1]), atol=1e-12)
    seg = np.cumsum([0.0, 7.0, 7.0, math.hypot(6.7, 0.7)])
    # Chainage is the plan distance along the line to the point's projection.
    for q, c in zip(p, chainage, strict=True):
        best = min(
            (
                np.linalg.norm(path[k, :2] + t * (path[k + 1, :2] - path[k, :2]) - q[:2]),
                seg[k] + t * np.linalg.norm(path[k + 1, :2] - path[k, :2]),
            )
            for k in range(3)
            for t in [
                float(
                    np.clip(
                        np.dot(q[:2] - path[k, :2], path[k + 1, :2] - path[k, :2])
                        / np.dot(path[k + 1, :2] - path[k, :2], path[k + 1, :2] - path[k, :2]),
                        0,
                        1,
                    )
                )
            ]
        )
        assert best[0] < 1e-12 and best[1] == pytest.approx(c, abs=1e-12)
    assert lines.line_values["chainage_from"][0] == 0.0
    assert lines.line_values["chainage_to"][0] == pytest.approx(seg[-1], abs=1e-12)
