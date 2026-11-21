"""The neighbourhood engine (``geotools.analysis.neighbourhood``) on synthetic surfaces.

Ball queries agree with brute force on random points; fits and the resolution
rule are checked on surfaces with known answers: tilted planes and ceilings, a
sinusoid of known RMS, a thin plate (both sides of a lip), a sphere, and
coarse or holed patches.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from synthetic_surfaces import drop_faces, plane_xy, surface

from geotools.analysis import neighbourhood as nb


def samples_of(mesh, **kw) -> nb.Samples:
    return nb.face_samples(*mesh, **kw)


def fit(mesh, queries, radius, *, query_normals=None, min_cos=0.0, edge=True):
    smp = samples_of(mesh)
    ball = nb.ball_stats(smp, queries, radius, query_normals=query_normals, min_cos=min_cos)
    pl = nb.plane(ball, queries)
    near_edge = None
    if edge:
        limit = nb.EDGE_FRACTION * 2 * radius
        near_edge = nb.edge_distance(*mesh, queries, 2 * limit) < limit
    return ball, pl, nb.resolution(ball, edge=near_edge)


# --- ball queries against brute force ----------------------------------------------------


def brute_pairs(points, queries, radius, pn=None, qn=None, min_cos=0.0):
    d2 = ((queries[:, None, :] - points[None, :, :]) ** 2).sum(-1)
    ok = d2 <= radius**2
    if pn is not None and qn is not None:
        ok &= (qn.astype(np.float64) @ pn.astype(np.float64).T) > min_cos
    return set(zip(*np.nonzero(ok), strict=True))


coords = hnp.arrays(
    np.float64,
    st.tuples(st.integers(1, 60), st.just(3)),
    elements=st.floats(-2.0, 2.0, allow_nan=False, width=32),
)


@given(
    points=coords,
    queries=hnp.arrays(
        np.float64,
        st.tuples(st.integers(1, 30), st.just(3)),
        elements=st.floats(-4.0, 4.0, allow_nan=False, width=32),
    ),
    radius=st.floats(0.05, 3.0),
    extra=st.floats(1.0, 3.0),
    max_pairs=st.integers(1, 200),
    dup=st.booleans(),
)
def test_ball_pairs_match_brute_force(points, queries, radius, extra, max_pairs, dup) -> None:
    if dup:  # repeated points and queries sitting exactly on samples
        points = np.concatenate([points, points[:3]])
        queries = np.concatenate([queries, points[:2]])
    g = nb.grid(points, radius * extra)
    found = set()
    seen_queries: list[int] = []
    for chunk in nb.ball_pairs(g, points, queries, radius, max_pairs=max_pairs):
        seen_queries.extend(chunk.queries.tolist())
        q = chunk.queries[chunk.query]
        assert np.allclose(chunk.offset, points[chunk.point] - queries[q])
        found |= set(zip(q.tolist(), chunk.point.tolist(), strict=True))
    assert len(seen_queries) == len(set(seen_queries)), "a query was split across chunks"
    assert found == {(int(a), int(b)) for a, b in brute_pairs(points, queries, radius)}


@given(
    points=coords,
    seed=st.integers(0, 2**16),
    radius=st.floats(0.1, 3.0),
    min_cos=st.floats(-0.9, 0.9),
)
def test_normal_filter_matches_brute_force(points, seed, radius, min_cos) -> None:
    rng = np.random.default_rng(seed)
    queries = rng.uniform(-2, 2, (12, 3))
    pn = rng.normal(size=(len(points), 3))
    pn /= np.linalg.norm(pn, axis=1, keepdims=True)
    qn = rng.normal(size=(len(queries), 3))
    qn /= np.linalg.norm(qn, axis=1, keepdims=True)
    g = nb.grid(points, radius)
    found = set()
    for chunk in nb.ball_pairs(
        g, points, queries, radius, point_normals=pn, query_normals=qn, min_cos=min_cos
    ):
        found |= set(zip(chunk.queries[chunk.query].tolist(), chunk.point.tolist(), strict=True))
    want = brute_pairs(points, queries, radius, pn, qn, min_cos)
    assert found == {(int(a), int(b)) for a, b in want}


@given(points=coords, seed=st.integers(0, 2**16), radius=st.floats(0.05, 3.0))
def test_nearest_and_near_match_brute_force(points, seed, radius) -> None:
    queries = np.random.default_rng(seed).uniform(-3, 3, (20, 3))
    index, dist = nb.nearest(points, queries, radius)
    d = np.sqrt(((queries[:, None] - points[None]) ** 2).sum(-1))
    best = d.min(axis=1)
    has = best <= radius
    assert np.array_equal(index >= 0, has)
    assert np.allclose(dist[has], best[has])
    assert np.allclose(d[np.flatnonzero(has), index[has]], best[has])
    assert np.isinf(dist[~has]).all()
    assert np.array_equal(nb.near(points, queries, radius), has)


def test_grid_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="cell size"):
        nb.grid(np.zeros((2, 3)), 0.0)
    with pytest.raises(ValueError, match="too small"):
        nb.grid(np.array([[0.0, 0, 0], [1e7, 1e7, 1e7]]), 1e-6)
    g = nb.grid(np.zeros((1, 3)), 1.0)
    with pytest.raises(ValueError, match="radius"):
        list(nb.ball_pairs(g, np.zeros((1, 3)), np.zeros((1, 3)), 2.0))
    assert (
        list(nb.ball_pairs(nb.grid(np.zeros((0, 3)), 1.0), np.zeros((0, 3)), g.origin[None], 1.0))
        == []
    )


def test_thin_keeps_one_point_per_cell() -> None:
    pts = np.random.default_rng(1).uniform(0, 1, (5000, 3))
    kept = nb.thin(pts, 0.25)
    g = nb.grid(pts, 0.25)
    keys = g.key(g.cells(pts))
    assert len(np.unique(keys[kept])) == len(kept) == len(np.unique(keys))
    assert np.array_equal(kept, np.sort(kept))


# --- samples ------------------------------------------------------------------------------


def test_triangle_spread_is_its_second_moment() -> None:
    v = np.array([[0.0, 0.0, 0.0], [2.0, 0.2, 0.1], [0.3, 1.0, 0.8]])
    smp = nb.face_samples(v, np.array([0, 3]), np.array([0, 1, 2]))
    r = np.random.default_rng(0).uniform(size=(400_000, 2))
    r = np.where(r.sum(1, keepdims=True) > 1, 1 - r, r)
    p = v[0] + r[:, :1] * (v[1] - v[0]) + r[:, 1:] * (v[2] - v[0])
    c = p.mean(0)
    cov = np.cov(p.T, bias=True)
    assert np.allclose(smp.points[0], v.mean(0))
    assert np.allclose(smp.points[0], c, atol=2e-3)
    want = [cov[0, 0], cov[1, 1], cov[2, 2], cov[0, 1], cov[0, 2], cov[1, 2]]
    assert np.allclose(smp.spread[0], want, atol=3e-3)
    assert smp.area[0] == pytest.approx(0.5 * np.linalg.norm(np.cross(v[1] - v[0], v[2] - v[0])))


def test_polygon_is_its_fan_of_triangles() -> None:
    v = np.array([[0.0, 0, 0], [3, 0, 0], [3, 1, 0], [1, 2, 0], [0, 1, 0]])
    poly = nb.face_samples(v, np.array([0, 5]), np.arange(5))
    tris = nb.face_samples(v, np.array([0, 3, 6, 9]), np.array([0, 1, 2, 0, 2, 3, 0, 3, 4]))
    a = tris.area
    centre = (tris.points * a[:, None]).sum(0) / a.sum()
    assert poly.area[0] == pytest.approx(a.sum())
    assert np.allclose(poly.points[0], centre)
    assert np.allclose(poly.normal[0], [0, 0, 1])
    d = tris.points - centre
    xx = ((tris.spread[:, 0] + d[:, 0] ** 2) * a).sum() / a.sum()
    assert poly.spread[0, 0] == pytest.approx(xx, rel=1e-6)
    edges = np.linalg.norm(np.roll(v, -1, 0) - v, axis=1)
    assert poly.size[0] == pytest.approx(edges.mean(), rel=1e-6)


def test_open_edges_and_edge_points() -> None:
    mesh = surface(plane_xy(), 4, 3)
    edges = nb.open_edges(mesh[1], mesh[2])
    assert len(edges) == 2 * (4 + 3)
    pts = nb.edge_points(mesh[0], edges, 0.05)
    assert np.abs(pts[:, :2] - np.clip(pts[:, :2], 0, 1)).max() == 0
    on_rim = (np.isclose(pts[:, 0], 0) | np.isclose(pts[:, 0], 1)) | (
        np.isclose(pts[:, 1], 0) | np.isclose(pts[:, 1], 1)
    )
    assert on_rim.all()
    # Spacing: consecutive points along one edge are at most 0.05 apart.
    one = nb.edge_points(mesh[0], edges[:1], 0.05)
    assert np.linalg.norm(np.diff(one, axis=0), axis=1).max() <= 0.05 + 1e-12


def test_edge_distance_of_a_square() -> None:
    mesh = surface(plane_xy(), 20, 20)
    q = np.array([[0.5, 0.5, 0.0], [0.5, 0.03, 0.0], [0.01, 0.02, 0.0], [0.5, 0.5, 0.2]])
    d = nb.edge_distance(*mesh, q, 0.1, resolution=0.001)
    assert np.isinf(d[0])
    assert d[1] == pytest.approx(0.03, abs=0.0005)
    assert d[2] == pytest.approx(0.01, abs=0.0005)
    assert np.isinf(d[3])


# --- fits on known surfaces -------------------------------------------------------------


TILT = math.radians(30)


def test_tilted_plane_normal_rms_and_status() -> None:
    mesh = surface(plane_xy(lambda x, y: y * math.tan(TILT)), 100, 100)
    q = np.array([[0.5, 0.5, 0.5 * math.tan(TILT)], [0.5, 0.002, 0.002 * math.tan(TILT)]])
    ball, pl, status = fit(mesh, q, 0.05)
    want = np.array([0.0, -math.sin(TILT), math.cos(TILT)])
    assert np.allclose(pl.normal[0], want, atol=1e-9)
    assert pl.rms[0] < 1e-9
    assert ball.coverage[0] == pytest.approx(1.0, abs=0.08)
    assert ball.size[0] == pytest.approx((0.01 + 0.01 + 0.01 * math.sqrt(2)) / 3, rel=0.2)
    assert status[0] == nb.RESOLVED
    assert status[1] & nb.EDGE and status[1] & nb.SPARSE


def test_ceiling_normal_points_down() -> None:
    mesh = surface(plane_xy(lambda x, y: 2.0 + 0 * x), 60, 60, flip=True)
    _, pl, status = fit(mesh, np.array([[0.5, 0.5, 2.0]]), 0.1)
    assert np.allclose(pl.normal[0], [0, 0, -1], atol=1e-9)
    assert status[0] == nb.RESOLVED


def test_far_from_the_origin_keeps_precision() -> None:
    e0, n0, z0 = 289_600.0, 8_635_700.0, 140.0
    mesh = surface(lambda s, t: np.stack([s + e0, t + n0, z0 + 0.3 * t], -1), 80, 80)
    _, pl, _ = fit(mesh, np.array([[e0 + 0.5, n0 + 0.5, z0 + 0.15]]), 0.05, edge=False)
    assert pl.rms[0] < 1e-8
    assert np.allclose(pl.normal[0], np.array([0, -0.3, 1]) / math.hypot(0.3, 1), atol=1e-7)


def surface_rms(amplitude: float, wavelength: float) -> float:
    """RMS height of ``A sin(2πx/λ)`` over its surface area (not its plan area)."""
    x = np.linspace(0, wavelength, 200_001)
    z = amplitude * np.sin(2 * math.pi * x / wavelength)
    ds = np.sqrt(
        1 + (2 * math.pi * amplitude / wavelength * np.cos(2 * math.pi * x / wavelength)) ** 2
    )
    return math.sqrt(float((z * z * ds).sum() / ds.sum()))


@pytest.mark.parametrize("amplitude", [0.0002, 0.001, 0.003])
def test_sinusoid_rms_is_its_rms_over_surface_area(amplitude) -> None:
    """Gentle relief gives A/√2, as a profilometer would. On steep relief the flanks
    hold more surface than the crests, so the RMS over surface area is lower (−9 %
    for A = 3 mm at λ = 1 cm): what a uniformly dense point cloud measures."""
    wavelength = 0.01  # many waves inside a ball of radius 5 cm, 20 vertices per wave
    mesh = surface(
        plane_xy(lambda x, y: amplitude * np.sin(2 * math.pi * x / wavelength)),
        400,
        40,
        s_range=(0.4, 0.6),
        t_range=(0, 0.4),
    )
    q = np.array([[0.5, 0.2, 0.0]])
    _, pl, status = fit(mesh, q, 0.05)
    assert pl.rms[0] == pytest.approx(surface_rms(amplitude, wavelength), rel=0.02)
    if amplitude <= 0.0002:
        assert pl.rms[0] == pytest.approx(amplitude / math.sqrt(2), rel=0.02)
    assert np.allclose(pl.normal[0], [0, 0, 1], atol=1e-3)
    assert status[0] == nb.RESOLVED


def test_thin_plate_sides_stay_apart_with_the_normal_filter() -> None:
    gap = 0.002
    top = surface(plane_xy(lambda x, y: gap + 0 * x), 50, 50)
    bottom = surface(plane_xy(), 50, 50, flip=True)
    n_top = len(top[0])
    mesh = (
        np.concatenate([top[0], bottom[0]]),
        np.concatenate([top[1], top[1][-1] + bottom[1][1:]]),
        np.concatenate([top[2], bottom[2] + n_top]),
    )
    q = np.array([[0.5, 0.5, gap]])
    _, filtered, _ = fit(mesh, q, 0.05, query_normals=np.array([[0, 0, 1.0]]), edge=False)
    _, mixed, _ = fit(mesh, q, 0.05, edge=False)
    assert filtered.rms[0] < 1e-9
    assert mixed.rms[0] == pytest.approx(gap / 2, rel=0.05)


def test_quadric_on_a_sphere() -> None:
    radius_sphere = 1.0
    mesh = surface(
        lambda s, t: (
            radius_sphere * np.stack([np.cos(t) * np.cos(s), np.cos(t) * np.sin(s), np.sin(t)], -1)
        ),
        720,
        360,
        (0, 2 * math.pi),
        (-math.pi / 2 + 0.01, math.pi / 2 - 0.01),
    )
    smp = samples_of(mesh)
    direction = np.array([[0.6, 0.0, 0.8], [0.0, 1.0, 0.0]])
    q = direction * radius_sphere
    # The fan of each grid cell winds the sphere's normals inward; fit against outward ones.
    fit_q = nb.quadric(smp, q, 0.1, direction, min_cos=None)
    a, b, c, d, e, _ = fit_q.coef.T
    assert np.allclose(a, -0.5 / radius_sphere, rtol=0.01)
    assert np.allclose(c, -0.5 / radius_sphere, rtol=0.01)
    assert np.allclose(b, 0, atol=1e-3)
    assert np.allclose(d, 0, atol=1e-3) and np.allclose(e, 0, atol=1e-3)
    assert (fit_q.rms < 1e-4).all()


def test_tangent_frames_are_orthonormal_and_horizontal() -> None:
    n = np.random.default_rng(3).normal(size=(200, 3))
    n = np.concatenate([n, [[0, 0, 1.0], [0, 0, -1.0]]])
    n /= np.linalg.norm(n, axis=1, keepdims=True)
    u, v = nb.tangent_frames(n)
    for x, y in ((u, v), (u, n), (v, n)):
        assert np.allclose((x * y).sum(1), 0, atol=1e-12)
    assert np.allclose(np.linalg.norm(u, axis=1), 1) and np.allclose(np.linalg.norm(v, axis=1), 1)
    assert np.allclose(np.cross(u, v), n)
    assert np.allclose(u[:-2, 2], 0, atol=1e-12)


# --- the resolution rule ------------------------------------------------------------------


def test_coarse_mesh_resolves_only_large_scales() -> None:
    mesh = surface(plane_xy(), 50, 50)  # 2 cm cells
    q = np.array([[0.5, 0.5, 0.0]])
    statuses = {s: fit(mesh, q, s / 2)[2][0] for s in (0.03, 0.10, 0.20)}
    assert statuses[0.03] & nb.COARSE
    assert statuses[0.10] & nb.COARSE  # 2.4 cm median edge: 10 cm is just below 5 h
    assert statuses[0.20] == nb.RESOLVED


def test_hole_lowers_coverage() -> None:
    mesh = surface(plane_xy(), 200, 200)
    centres = nb.face_samples(*mesh).points
    keep = np.hypot(centres[:, 0] - 0.5, centres[:, 1] - 0.5) > 0.03
    holed = drop_faces(mesh, keep)
    q = np.array([[0.5, 0.54, 0.0], [0.2, 0.2, 0.0]])  # 1 cm from the hole's rim
    ball, _, status = fit(holed, q, 0.05)
    assert ball.coverage[0] < 0.8 and status[0] & nb.SPARSE
    assert status[0] & nb.EDGE  # the rim is within s/4 = 2.5 cm
    assert status[1] == nb.RESOLVED


def test_weighted_median_edge_length() -> None:
    rng = np.random.default_rng(7)
    n = 300
    smp = nb.Samples(
        points=rng.uniform(-0.01, 0.01, (n, 3)),
        area=rng.uniform(0.1, 2.0, n),
        normal=np.tile(np.float32([0, 0, 1]), (n, 1)),
        size=rng.uniform(0.001, 0.02, n).astype(np.float32),
        spread=None,
    )
    q = np.zeros((1, 3))
    ball = nb.ball_stats(smp, q, 0.5, max_pairs=7)
    order = np.argsort(smp.size, kind="stable")
    run = np.cumsum(smp.area[order])
    want = smp.size[order][np.searchsorted(run, 0.5 * run[-1])]
    assert ball.size[0] == pytest.approx(want)
    assert ball.count[0] == n


def test_many_queries_in_small_chunks_agree_with_one_chunk() -> None:
    mesh = surface(plane_xy(lambda x, y: 0.01 * np.sin(9 * x) * np.cos(7 * y)), 60, 60)
    smp = samples_of(mesh)
    q = smp.points[::7]
    one = nb.ball_stats(smp, q, 0.06)
    small = nb.ball_stats(smp, q, 0.06, max_pairs=50)
    for name in ("count", "area", "centre", "cov", "vector_area", "size"):
        assert np.allclose(getattr(one, name), getattr(small, name), equal_nan=True), name
