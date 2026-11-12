"""Contours and sections (geotools.analysis.slicing) and chaining (.polylines)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from geotools.analysis import polylines, slicing


def grid(n: int, lo: float, hi: float, height) -> tuple[np.ndarray, np.ndarray]:
    x = np.linspace(lo, hi, n + 1)
    xx, yy = np.meshgrid(x, x, indexing="ij")
    pts = np.stack([xx.ravel(), yy.ravel(), height(xx.ravel(), yy.ravel())], axis=1)
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    v0 = (i * (n + 1) + j).ravel()
    v1, v3 = v0 + n + 1, v0 + 1
    v2 = v1 + 1
    return pts, np.concatenate([np.stack([v0, v1, v2], 1), np.stack([v0, v2, v3], 1)])


def uv_sphere(radius: float, nu: int = 96, nv: int = 48) -> tuple[np.ndarray, np.ndarray]:
    theta = np.linspace(0, math.pi, nv + 1)[1:-1]
    phi = np.linspace(0, 2 * math.pi, nu, endpoint=False)
    t, p = np.meshgrid(theta, phi, indexing="ij")
    pts = np.stack(
        [radius * np.sin(t) * np.cos(p), radius * np.sin(t) * np.sin(p), radius * np.cos(t)], -1
    ).reshape(-1, 3)
    pts = np.vstack([pts, [[0, 0, radius], [0, 0, -radius]]])
    top, bottom = len(pts) - 2, len(pts) - 1
    i, j = np.meshgrid(np.arange(nv - 2), np.arange(nu), indexing="ij")
    a = (i * nu + j).ravel()
    b = (i * nu + (j + 1) % nu).ravel()
    c = a + nu
    d = b + nu
    ring = np.arange(nu)
    last = (nv - 2) * nu
    tris = np.concatenate(
        [
            np.stack([a, c, d], 1),
            np.stack([a, d, b], 1),
            np.stack([np.full(nu, top), ring, (ring + 1) % nu], 1),
            np.stack([np.full(nu, bottom), last + (ring + 1) % nu, last + ring], 1),
        ]
    )
    return pts, tris


def line_points(lines: slicing.Lines, i: int) -> np.ndarray:
    e = lines.edges[lines.edge_line == i]
    return lines.positions[np.append(e[:, 0], e[-1, 1])]


# --- chaining ------------------------------------------------------------------------------


def test_chain_open_closed_and_branching() -> None:
    # A closed square 0-1-2-3, an open path 4-5-6 given backwards and shuffled,
    # and a star at 7 (three arms): arms end there.
    a = np.array([0, 2, 1, 3, 6, 4, 7, 7, 7])
    b = np.array([1, 3, 2, 0, 5, 5, 8, 9, 10])
    chains = polylines.chain(11, a, b)
    assert chains.count == 5
    closed = [
        chains.nodes[s:e]
        for s, e, c in zip(chains.offsets[:-1], chains.offsets[1:], chains.closed, strict=True)
        if c
    ]
    assert len(closed) == 1
    ring = closed[0]
    assert ring[0] == ring[-1] and sorted(ring[:-1]) == [0, 1, 2, 3]
    opened = [
        chains.nodes[s:e].tolist()
        for s, e, c in zip(chains.offsets[:-1], chains.offsets[1:], chains.closed, strict=True)
        if not c
    ]
    assert sorted(map(sorted, opened)) == [[4, 5, 6], [7, 8], [7, 9], [7, 10]]
    edges = polylines.edges(chains)
    assert len(edges) == len(a)
    # Every step is an input segment.
    given = {tuple(sorted(p)) for p in zip(a, b, strict=True)}
    assert {tuple(sorted(p)) for p in edges.tolist()} == given
    assert sorted(chains.segments.tolist()) == list(range(len(a)))


def test_chain_reverse_and_empty() -> None:
    chains = polylines.chain(3, np.array([0, 1]), np.array([1, 2]))
    flipped = polylines.reverse(chains, np.array([True]))
    assert flipped.nodes.tolist() == chains.nodes[::-1].tolist()
    assert polylines.chain(2, np.array([1]), np.array([1])).count == 0


def test_chain_two_parallel_segments_make_a_ring() -> None:
    chains = polylines.chain(2, np.array([0, 0]), np.array([1, 1]))
    assert chains.count == 1 and chains.closed[0]


# --- contours ------------------------------------------------------------------------------


def test_cone_contours_are_circles_at_the_right_radii() -> None:
    # z = 10 - r over a square of half-width 10, in a datum 100 m up.
    pts, tris = grid(200, -10, 10, lambda x, y: 10 - np.hypot(x, y))
    lines = slicing.contours(pts, tris, 2.0, base=0.0, datum=100.0)
    elevation = lines.line_values["elevation"]
    full = np.flatnonzero(lines.closed)
    assert sorted(elevation[full].tolist()) == [100.0, 102.0, 104.0, 106.0, 108.0]
    for i in full:
        level = elevation[i]
        p = line_points(lines, i)
        radius = 110.0 - level
        r = np.hypot(p[:, 0], p[:, 1])
        assert np.abs(r - radius).max() < 1e-3 * radius + 2e-4
        assert np.allclose(p[:, 2] + 100.0, level)
        assert lines.length[i] == pytest.approx(2 * math.pi * radius, rel=1e-3)
    # Lower levels leave the square: open lines whose ends are on its edge.
    for i in np.flatnonzero(~lines.closed):
        p = line_points(lines, i)
        for end in (p[0], p[-1]):
            assert np.isclose(np.abs(end[:2]).max(), 10.0)
    assert set(lines.line_values["index"].tolist()) == {48, 49, 50, 51, 52, 53, 54}
    assert lines.line_values["major"][lines.line_values["index"] == 50].all()
    assert not lines.line_values["major"][lines.line_values["index"] == 51].any()


def test_contours_with_a_base_and_vertices_on_a_level() -> None:
    # A plane whose vertices lie exactly on the levels: one straight line per level.
    pts, tris = grid(10, 0, 10, lambda x, y: x.astype(float))
    lines = slicing.contours(pts, tris, 1.0, base=0.5)
    assert lines.count == 10
    assert lines.line_values["elevation"].tolist() == [0.5 + k for k in range(10)]
    lines = slicing.contours(pts, tris, 1.0)
    # A vertex on a level counts as above it, so the level of the lowest vertices
    # (x = 0) crosses no triangle and the highest (x = 10) is drawn along the edge.
    assert lines.line_values["elevation"].tolist() == [float(k) for k in range(1, 11)]
    for i in range(lines.count):
        p = line_points(lines, i)
        assert len(p) == 11  # every grid vertex of the column, no duplicates
        assert np.ptp(p[:, 0]) == 0


def test_contour_level_guards() -> None:
    with pytest.raises(slicing.SliceError):
        slicing.contour_levels(0, 10, 0.0)
    with pytest.raises(slicing.SliceError):
        slicing.contour_levels(0, 1e6, 1e-3)
    levels, k0 = slicing.contour_levels(-2.5, 7.1, 2.5, base=1.0)
    assert levels.tolist() == [-1.5, 1.0, 3.5, 6.0] and k0 == -1


# --- sections ------------------------------------------------------------------------------


def test_section_through_a_sphere_is_its_circle() -> None:
    radius, y = 5.0, 0.3
    pts, tris = uv_sphere(radius)
    path = np.array([[-8.0, y, 0.0], [8.0, y, 0.0]])
    lines = slicing.sections(pts, tris, path, np.array([0, 2]))
    assert lines.count == 1 and lines.closed[0]
    p = lines.positions
    expected = math.sqrt(radius**2 - y**2)
    assert np.abs(np.hypot(p[:, 0], p[:, 2]) - expected).max() < 0.01 * radius
    assert np.allclose(p[:, 1], y)
    chainage = lines.point_values["chainage"]
    assert np.allclose(chainage, p[:, 0] + 8.0)
    assert lines.line_values["chainage_from"][0] == pytest.approx(8.0 - expected, abs=0.02)
    assert lines.line_values["chainage_to"][0] == pytest.approx(8.0 + expected, abs=0.02)


def test_section_inside_a_sphere_gives_two_arcs_across_a_bend() -> None:
    radius = 5.0
    pts, tris = uv_sphere(radius)
    # Three points, the middle one a vertex of the section line.
    path = np.array([[-3.0, 0.3, 0.0], [0.0, 0.3, 0.0], [3.0, 0.3, 0.0]])
    lines = slicing.sections(pts, tris, path, np.array([0, 3]))
    assert lines.count == 2  # the top arc and the bottom arc, each one piece
    assert not lines.closed.any()
    tops = []
    for i in range(2):
        p = line_points(lines, i)
        c = lines.point_values["chainage"][
            np.append(
                lines.edges[lines.edge_line == i][:, 0], lines.edges[lines.edge_line == i][-1, 1]
            )
        ]
        assert np.all(np.diff(c) >= -1e-12)  # runs the way the line goes
        assert c[0] == pytest.approx(0.0) and c[-1] == pytest.approx(6.0)
        expected = np.sqrt(radius**2 - 0.09 - p[:, 0] ** 2)
        assert np.abs(np.abs(p[:, 2]) - expected).max() < 0.01 * radius
        tops.append(p[:, 2].mean() > 0)
    assert sorted(tops) == [False, True]
    assert (lines.line_values["section"] == 0).all()


def test_section_along_a_bent_line_over_a_plane() -> None:
    pts, tris = grid(20, 0, 20, lambda x, y: 0.5 * x + 0.25 * y)
    path = np.array([[2.0, 2.0, 0.0], [12.0, 2.0, 0.0], [12.0, 10.0, 0.0]])
    lines = slicing.sections(pts, tris, path, np.array([0, 3]))
    assert lines.count == 1
    p = lines.positions
    assert np.allclose(p[:, 2], 0.5 * p[:, 0] + 0.25 * p[:, 1])
    assert lines.line_values["chainage_from"][0] == pytest.approx(0.0)
    assert lines.line_values["chainage_to"][0] == pytest.approx(18.0)
    assert lines.length[0] > 18.0


def test_section_misses_and_degenerate_lines() -> None:
    pts, tris = grid(4, 0, 4, lambda x, y: x * 0)
    with pytest.raises(slicing.SliceError):
        slicing.sections(pts, tris, np.array([[10.0, 10, 0], [20.0, 10, 0]]), np.array([0, 2]))
    with pytest.raises(slicing.SliceError):
        slicing.sections(pts, tris, np.array([[1.0, 1, 0], [1.0, 1, 0]]), np.array([0, 2]))
    # Two separate polylines: chainage restarts on each.
    path = np.array([[0.5, 1, 0], [3.5, 1, 0], [0.5, 3, 0], [3.5, 3, 0]], dtype=float)
    lines = slicing.sections(pts, tris, path, np.array([0, 2, 4]))
    assert sorted(lines.line_values["section"].tolist()) == [0, 1]
    assert np.allclose(lines.line_values["chainage_from"], 0.0)
    assert np.allclose(lines.line_values["chainage_to"], 3.0)
