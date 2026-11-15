"""Least-cost routes validation, the pure-Python half (docs/validation/least-cost.md).

GeoTools' directed search (``geotools.analysis.least_cost``) against:

* closed forms: each walking model's time per metre, and routes straight up
  and down planes of constant slope, both ways;
* GRASS r.walk's own times per metre on planar ramps (measured for the
  Least-Cost Path preset, ``tests/data/validation/visibility/paths.json``);
* networkx 3.7 Dijkstra on the same directed graph, built independently by
  ``scripts/validation/least_cost/make_references.py`` (times at sampled
  nodes and the routes both ways, four models, with and without Steiner points
  and a cost field), and a heap Dijkstra written here on GeoTools' own links;
* the straight line on planes at 72 bearings (the bias that remains with
  Steiner points), on a regular and a jittered triangulation;
* GRASS r.walk's accumulated cost on the same DEM (8 and 16 neighbours),
  compared statistically.

Lines starting ``VALIDATION`` print the figures the page quotes (``pytest -s``).
The Blender half (``tests/blender/test_least_cost.py``) runs the operator.
"""

from __future__ import annotations

import heapq
import importlib.util
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from geotools.analysis import least_cost as lc

REPO = Path(__file__).resolve().parents[2]
REFS = REPO / "tests" / "data" / "validation" / "least_cost"
PATHS_JSON = REPO / "tests" / "data" / "validation" / "visibility" / "paths.json"


def _cases():
    """``scripts/validation/visibility/cases.py``, which the reference script builds from."""
    name = "geotools_validation_cases"
    if name not in sys.modules:
        path = REPO / "scripts" / "validation" / "visibility" / "cases.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@pytest.fixture(scope="module")
def refs() -> dict[str, np.ndarray]:
    with np.load(REFS / "references.npz") as data:
        return {k: data[k] for k in data.files}


@pytest.fixture(scope="module")
def summary() -> dict:
    return json.loads((REFS / "references.json").read_text())


def plane(n: int, h: float, height=lambda x, y: 0.0 * x, jitter: float = 0.0):
    """A square grid of n x n cells centred on the origin, two triangles per cell."""
    xs = (np.arange(n + 1) - n / 2) * h
    gx, gy = np.meshgrid(xs, xs)
    pts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1)
    if jitter:
        rng = np.random.default_rng(7)
        inner = (np.abs(pts[:, 0]) < xs[-1]) & (np.abs(pts[:, 1]) < xs[-1])
        pts[inner, :2] += rng.uniform(-jitter, jitter, (int(inner.sum()), 2)) * h
    pts[:, 2] = height(pts[:, 0], pts[:, 1])
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    b, c, d = a + 1, a + n + 2, a + n + 1
    return pts, np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])


def route_time(graph: lc.Graph, start, end) -> float | None:
    s, e = lc.snap(graph, start), lc.snap(graph, end)
    r = lc.route(graph, lc.search(graph, s, end=e), e)
    return None if r is None else r.time


# --- the models --------------------------------------------------------------------------------


def test_model_times_per_metre() -> None:
    """Each model's documented formula, at slopes either side of its breaks."""
    for deg in (-30.0, -13.0, -11.0, -5.0, -2.0, 0.0, 2.0, 4.0, 8.0, 20.0, 30.0):
        s = math.tan(math.radians(deg))
        tobler = 1.0 / (6.0 / 3.6 * math.exp(-3.5 * abs(s + 0.05)))
        assert lc.segment_time("TOBLER", 1.0, s) == pytest.approx(tobler, rel=1e-14)
        if s >= 0:
            lang = 0.72 + 6.0 * s
        elif s > -0.2125:
            lang = 0.72 + 1.9998 * s
        else:
            lang = 0.72 - 1.9998 * s
        assert lc.segment_time("LANGMUIR", 1.0, s) == pytest.approx(lang, rel=1e-14)
        assert lc.segment_time("NAISMITH", 1.0, s) == pytest.approx(
            0.72 + 6.0 * max(s, 0.0), rel=1e-14
        )
        wheel = lc.segment_time("WHEELCHAIR", 1.0, s)
        if abs(s) > 1.0 / 12.0:
            assert wheel == math.inf
        else:
            v = 1.12 - 0.53 * max(s, 0.0) / math.tan(math.radians(4.8))
            assert wheel == pytest.approx(1.0 / v, rel=1e-14)
    # The anchors: Tobler's 6 km/h at 5 % down, 5.04 on the level; the wheelchair's
    # measured speeds; Naismith's 600 m of ascent per hour.
    assert lc.segment_time("TOBLER", 1.0, -0.05) == pytest.approx(0.6)
    assert 1.0 / lc.segment_time("TOBLER", 1.0, 0.0) * 3.6 == pytest.approx(5.0367, abs=1e-4)
    assert lc.segment_time("WHEELCHAIR", 1.0, 0.0) == pytest.approx(1.0 / 1.12)
    s48 = math.tan(math.radians(4.8))
    assert lc.segment_time("WHEELCHAIR", 1.0, s48, max_gradient=0.1) == pytest.approx(1 / 0.59)
    assert lc.segment_time("NAISMITH", 0.0, 600.0) == math.inf  # a vertical step
    assert lc.segment_time("NAISMITH", 5000.0, 600.0) == pytest.approx(7200.0)
    assert lc.segment_time("TOBLER", 0.0, 0.0) == 0.0


def test_langmuir_is_r_walks_time_on_ramps() -> None:
    """r.walk's own cost per metre on 50 m planar ramps (measured with GRASS 8.5.0 for the
    Least-Cost Path preset): Langmuir equals it except at -13°, where r.walk, limited to
    8 directions, walks the ramp diagonally."""
    ramps = json.loads(PATHS_JSON.read_text())["ramps"]
    worst = 0.0
    for slope, row in ramps.items():
        s = math.tan(math.radians(float(slope)))
        ours = float(lc.segment_time("LANGMUIR", 1.0, s))
        if float(slope) == -13.0:
            assert row["r_walk_s_per_m"] < ours
            continue
        worst = max(worst, abs(ours / row["r_walk_s_per_m"] - 1))
    print(f"VALIDATION langmuir-vs-r.walk ramps: largest relative difference {worst:.1e}")
    assert worst < 1e-6


@pytest.mark.parametrize("model", list(lc.Model))
@pytest.mark.parametrize("grade", [0.0, 0.02, 0.05, 0.15])
def test_constant_slope_routes_both_ways(model, grade) -> None:
    """Straight up and straight down a plane of constant slope: the closed form, both ways."""
    pts, tris = plane(32, 0.5, lambda x, y: grade * x)
    graph = lc.build_graph(pts, tris, model=model, steiner=3)
    a, b = (-6.0, 0.25, -6.0 * grade), (6.0, 0.25, 6.0 * grade)
    up, down = route_time(graph, a, b), route_time(graph, b, a)
    rise = 12.0 * grade
    exp_up = float(lc.segment_time(model, 12.0, rise))
    exp_down = float(lc.segment_time(model, 12.0, -rise))
    if math.isinf(exp_up):
        # Too steep straight up for a wheelchair: the route traverses the slope instead,
        # every segment within 1:12 (there is no cross-slope limit).
        s, e = lc.snap(graph, a), lc.snap(graph, b)
        r = lc.route(graph, lc.search(graph, s, end=e), e)
        d = np.diff(r.points, axis=0)
        assert (np.abs(d[:, 2]) <= np.hypot(d[:, 0], d[:, 1]) / 12.0 + 1e-12).all()
        assert r.length > 1.5 * 12.0
        return
    assert up == pytest.approx(exp_up, rel=1e-12)
    assert down == pytest.approx(exp_down, rel=1e-12)
    if grade and model is not lc.Model.WHEELCHAIR:
        assert up != pytest.approx(down, rel=1e-6)  # directed


def test_routes_are_directed_where_tobler_says() -> None:
    """Across a slope, uphill costs more than down; the time back is not the time there."""
    pts, tris = plane(40, 0.5, lambda x, y: 0.2 * x + 0.1 * np.sin(y))
    graph = lc.build_graph(pts, tris, steiner=2)
    there = route_time(graph, (-8.0, -8.0, 0.0), (8.0, 7.0, 0.0))
    back = route_time(graph, (8.0, 7.0, 0.0), (-8.0, -8.0, 0.0))
    assert there > 1.3 * back


# --- networkx on the same graph -------------------------------------------------------------------


def _hills_graph(model: str, k: int, costs: bool, refs) -> tuple[lc.Graph, int, int]:
    C = _cases()
    g = C.hills(1.0)
    factors = refs["cost_factors"] if costs else None
    graph = lc.build_graph(
        g.nodes(), g.triangles(), model=model, steiner=k, max_slope=30.0, face_factor=factors
    )

    def vertex(x: float, y: float) -> int:
        return round((y - g.y0) / g.h) * (g.nx + 1) + round((x - g.x0) / g.h)

    return graph, vertex(-28.0, -28.0), vertex(28.0, 26.0)


RUNS = [
    ("TOBLER", 2, False),
    ("LANGMUIR", 2, False),
    ("NAISMITH", 2, False),
    ("WHEELCHAIR", 2, False),
    ("TOBLER", 0, False),
    ("TOBLER", 2, True),
]


@pytest.mark.parametrize(("model", "k", "costs"), RUNS)
def test_matches_networkx_on_the_same_graph(model, k, costs, refs, summary) -> None:
    tag = f"{model.lower()}_k{k}{'_cost' if costs else ''}"
    graph, s, e = _hills_graph(model, k, costs, refs)
    nodes = refs[f"{tag}__nodes"]
    # Stored on x86-64; other CPUs may round the Steiner interpolation's last bit
    # differently (fused multiply-add on arm64), so compare to a few ULP.
    np.testing.assert_allclose(graph.nodes[nodes], refs[f"{tag}__xyz"], rtol=1e-12, atol=1e-12)
    start = lc.snap(graph, graph.nodes[s])
    assert start.distance == 0.0
    found = lc.search(graph, start)
    expected = refs[f"{tag}__time"]
    worst = float(np.max(np.abs(found.time[nodes] - expected) / np.maximum(expected, 1e-9)))
    assert worst < 1e-12, worst
    assert int(np.isfinite(found.time).sum()) == summary["runs"][tag]["reached"]
    for name, a, b in (("there", s, e), ("back", e, s)):
        ref = summary["runs"][tag][name]
        anchor_a, anchor_b = lc.snap(graph, graph.nodes[a]), lc.snap(graph, graph.nodes[b])
        r = lc.route(graph, lc.search(graph, anchor_a, end=anchor_b), anchor_b)
        if ref is None:
            assert r is None
            continue
        assert r.time == pytest.approx(ref["time"], rel=1e-12)
        # A path of the graph: Start, its nodes, End.
        np.testing.assert_array_equal(r.points[0], graph.nodes[a])
        np.testing.assert_array_equal(r.points[-1], graph.nodes[b])
        assert (np.diff(r.elapsed) >= 0).all()
        print(
            f"VALIDATION networkx {tag} {name}: {r.time:.6f} s against {ref['time']:.6f} "
            f"({abs(r.time / ref['time'] - 1):.0e}); nodes {worst:.0e}"
        )


def _heap_dijkstra(n: int, u, v, w, seeds: dict[int, float]) -> np.ndarray:
    """Dijkstra (1959) with a binary heap, on an adjacency list, written out."""
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for a, b, c in zip(u.tolist(), v.tolist(), w.tolist(), strict=True):
        adj[a].append((b, c))
    dist = [math.inf] * n
    heap = []
    for node, c in seeds.items():
        if c < dist[node]:
            dist[node] = c
            heapq.heappush(heap, (c, node))
    done = [False] * n
    while heap:
        d, a = heapq.heappop(heap)
        if done[a]:
            continue
        done[a] = True
        for b, c in adj[a]:
            if d + c < dist[b]:
                dist[b] = d + c
                heapq.heappush(heap, (d + c, b))
    return np.array(dist)


@pytest.mark.parametrize("model", list(lc.Model))
def test_matches_a_heap_dijkstra_on_its_links(model) -> None:
    """An irregular, mixed-slope mesh with a cost field: every node's time is a heap
    Dijkstra's on ``links`` to float precision, and stopping at End gives the same route."""
    pts, tris = plane(24, 0.5, lambda x, y: 0.6 * np.sin(x / 1.5) * np.cos(y / 2.0), 0.3)
    centres = pts[tris].mean(axis=1)
    factors = np.where(np.abs(centres[:, 0] - 1.0) < 0.6, 3.0, 1.0)
    factors[(np.abs(centres[:, 1]) < 0.5) & (centres[:, 0] < -2)] = 0.0
    graph = lc.build_graph(pts, tris, model=model, steiner=2, max_slope=35, face_factor=factors)
    start = lc.snap(graph, (-4.3, -3.9, 5.0))
    found = lc.search(graph, start)
    nodes, cost = lc._seed(graph, start)
    seeds: dict[int, float] = {}
    for a, c in zip(nodes.tolist(), cost.tolist(), strict=True):
        seeds[a] = min(seeds.get(a, math.inf), c)
    ref = _heap_dijkstra(graph.n, *lc.links(graph), seeds)
    np.testing.assert_array_equal(np.isfinite(found.time), np.isfinite(ref))
    ok = np.isfinite(ref) & (ref > 0)
    assert np.max(np.abs(found.time[ok] / ref[ok] - 1)) < 1e-12
    end = lc.snap(graph, (4.6, 4.1, 0.0))
    full = lc.route(graph, found, end)
    early = lc.search(graph, start, end=end)
    assert early.stopped_early or not np.isfinite(early.time).all()
    quick = lc.route(graph, early, end)
    if full is None:
        assert quick is None
    else:
        assert quick.time == full.time
        # The elapsed time at each node is the sum of its links along the route.
        legs = graph.time(full.points[:-1], full.points[1:], 1.0)
        assert (np.diff(full.elapsed) >= legs * (1 - 1e-12) * factors.min(initial=1)).all()


# --- the bias that remains --------------------------------------------------------------------------

BEARINGS = np.arange(0.0, 360.0, 5.0)
BIAS_LIMITS = {0: 1.43, 1: 1.09, 2: 1.07, 3: 1.03, 4: 1.021, 6: 1.012, 8: 1.009}
"""The worst ratio to the straight line over 72 bearings, regular grid."""


def bias(pts, tris, k: int) -> np.ndarray:
    graph = lc.build_graph(pts, tris, steiner=k)
    start = lc.snap(graph, (0.0, 0.0, 0.0))
    found = lc.search(graph, start)
    out = []
    for b in np.radians(BEARINGS):
        end = lc.snap(graph, (10.0 * math.sin(b), 10.0 * math.cos(b), 0.0))
        r = lc.route(graph, found, end)
        run = float(np.linalg.norm(end.on_surface[:2] - start.on_surface[:2]))
        out.append(r.time / float(lc.segment_time("TOBLER", run, 0.0)))
    return np.array(out)


@pytest.mark.parametrize("k", sorted(BIAS_LIMITS))
def test_any_angle_bias_on_level_ground(k) -> None:
    """Route time over straight-line time from the centre of a 24 m plane at 0.5 m to
    points 10 m away at 72 bearings, on a regular and a jittered triangulation."""
    regular = bias(*plane(48, 0.5), k)
    jittered = bias(*plane(48, 0.5, jitter=0.3), k)
    for name, ratios in (("regular", regular), ("jittered", jittered)):
        print(
            f"VALIDATION bias {name} k={k}: worst {ratios.max():.4f} at "
            f"{BEARINGS[ratios.argmax()]:g}°, mean {ratios.mean():.4f}"
        )
        assert ratios.min() > 1 - 1e-12  # never quicker than the straight line
    assert regular.max() <= BIAS_LIMITS[k]
    assert jittered.max() <= BIAS_LIMITS[k]


def test_face_times_on_level_ground() -> None:
    """A face's time is to its centre: never quicker than the straight line, and within
    the bias of it."""
    pts, tris = plane(40, 0.5)
    graph = lc.build_graph(pts, tris, steiner=3)
    start = lc.snap(graph, (0.1, 0.2, 0.0))
    times = lc.face_times(graph, lc.search(graph, start))
    centres = pts[tris].mean(axis=1)
    straight = np.linalg.norm(centres[:, :2] - start.on_surface[:2], axis=1)
    pace = float(lc.segment_time("TOBLER", 1.0, 0.0))
    ratio = times / (straight * pace)
    assert np.isfinite(times).all()
    assert ratio.min() > 1 - 1e-12 and np.percentile(ratio[straight > 2], 99) < 1.03


# --- r.walk ------------------------------------------------------------------------------------------


def test_accumulated_cost_against_r_walk(refs) -> None:
    """GeoTools' Langmuir times at the vertices (the raster's cell centres) against r.walk's
    accumulated cost, 8 and 16 neighbours, no Max Slope (r.walk has none)."""
    C = _cases()
    g = C.hills(1.0)
    stats = {}
    for k in (0, 3):
        graph = lc.build_graph(g.nodes(), g.triangles(), model="LANGMUIR", steiner=k, max_slope=89)
        found = lc.search(graph, lc.snap(graph, (-28.0, -28.0, g.height(-28.0, -28.0))))
        ours = found.time[: graph.n_vertices].reshape(g.z.shape)[::-1]  # rows north first
        for n in (8, 16):
            theirs = refs[f"rwalk_{n}"]
            far = np.isfinite(theirs) & (theirs > 10.0)
            ratio = ours[far] / theirs[far]
            p5, p50, p95 = np.percentile(ratio, [5, 50, 95])
            stats[(k, n)] = (p5, p50, p95)
            print(
                f"VALIDATION r.walk {n} neighbours, GeoTools k={k}: ratio p5 {p5:.4f} "
                f"median {p50:.4f} p95 {p95:.4f} (cells {far.sum()})"
            )
    # With Steiner points GeoTools walks straighter than either raster: quicker than r.walk
    # with 8 neighbours, and within 3 % of 16 neighbours for nine cells in ten.
    p5, p50, p95 = stats[(3, 8)]
    assert p95 <= 1.0 + 1e-9 and p50 < 0.99
    p5, p50, p95 = stats[(3, 16)]
    assert p5 > 0.97 and p95 < 1.03 and abs(p50 - 1) < 0.01
    # On mesh edges only (6 directions) it is as quick as r.walk's 8 along the grid and
    # slower between (a twentieth of cells 5 % slower or more).
    assert abs(stats[(0, 8)][1] - 1) < 1e-3 and stats[(0, 8)][2] > 1.04


# --- outputs ----------------------------------------------------------------------------------------


def test_isochrone_bands() -> None:
    times = np.array([0.0, 59.9, 60.0, 299.0, np.nan, np.inf])
    ids, labels = lc.bands(times, 120.0)
    assert ids.tolist() == [1, 1, 1, 3, 0, 0]
    assert labels == ("Unreached", "0–2 min", "2–4 min", "4–6 min")
    with pytest.raises(lc.LeastCostError):
        lc.bands(times, 0.0)


def test_impassable_faces() -> None:
    """Steeper than Max Slope, facing down, or a cost of 0 or no data: not crossed."""
    pts, tris = plane(10, 1.0, lambda x, y: np.where(x > 0, 2.0 * x, 0.0))
    graph = lc.build_graph(pts, tris, max_slope=64)  # atan 2 = 63.4°
    assert graph.stats["too_steep"] == 0
    graph = lc.build_graph(pts, tris, max_slope=63)
    steep = pts[tris].mean(axis=1)[:, 0] > 0
    assert (~graph.crossable == steep).all()
    end = lc.snap(graph, (4.0, 0.0, 8.0))  # on the steep side: taken from the nearest face
    assert end.distance > 4.0 and end.on_surface[0] <= 1e-12
    flipped = lc.build_graph(pts, tris[:, ::-1], max_slope=64)
    assert not flipped.crossable.any()
    with pytest.raises(lc.LeastCostError, match="No face can be crossed"):
        lc.snap(flipped, (0.0, 0.0, 0.0))
    unflipped = lc.build_graph(pts, tris[:, ::-1], max_slope=64, flip=np.ones(len(tris), bool))
    assert unflipped.crossable.all()
    factors = np.ones(len(tris))
    factors[:4] = [0.0, -1.0, np.nan, np.inf]
    graph = lc.build_graph(pts, tris, max_slope=64, face_factor=factors)
    assert not graph.crossable[:4].any() and graph.crossable[4:].all()
    with pytest.raises(lc.LeastCostError, match="Max Slope"):
        lc.build_graph(pts, tris, max_slope=90)


def test_a_cost_factor_scales_time() -> None:
    pts, tris = plane(20, 0.5)
    once = lc.build_graph(pts, tris, steiner=1)
    twice = lc.build_graph(pts, tris, steiner=1, face_factor=np.full(len(tris), 2.0))
    a, b = (-4.0, 0.0, 0.0), (4.0, 0.0, 0.0)
    assert route_time(twice, a, b) == pytest.approx(2.0 * route_time(once, a, b), rel=1e-12)


def test_quads_take_their_quickest_triangle() -> None:
    """A face of two triangles gets one time: the quicker of its triangles' centres."""
    pts, tris = plane(6, 1.0)
    tri_face = np.concatenate([np.arange(36), np.arange(36)])  # cell i's two halves
    graph = lc.build_graph(pts, tris, tri_face=tri_face)
    per_tri = lc.build_graph(pts, tris)
    start = (0.2, 0.1, 0.0)
    faces = lc.face_times(graph, lc.search(graph, lc.snap(graph, start)))
    tris_t = lc.face_times(per_tri, lc.search(per_tri, lc.snap(per_tri, start)))
    assert faces.shape == (36,)
    np.testing.assert_array_equal(faces, np.minimum(tris_t[:36], tris_t[36:]))
