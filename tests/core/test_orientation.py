"""Normal orientation (geotools.analysis.orientation) on synthetic meshes.

Boxes (outward, flipped, one face reversed), an open terrain, a rock shelter
with an overhang, and unwelded tiles. Blender casts the sky test's rays in
the add-on; here a brute-force vertical ray stands in (:func:`open_sky`).
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from geotools.analysis import orientation as orient
from geotools.core.issues import Severity

# --- synthetic meshes ----------------------------------------------------------------------


class Mesh:
    """Polygons as lists of vertex ids, with Blender's corner edges built from them."""

    def __init__(self, positions, faces) -> None:
        self.positions = np.asarray(positions, dtype=np.float64)
        sizes = [len(f) for f in faces]
        self.face_starts = np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64)
        self.corner_verts = np.concatenate([np.asarray(f) for f in faces]).astype(np.int64)
        nxt = np.arange(1, len(self.corner_verts) + 1)
        nxt[self.face_starts[1:] - 1] = self.face_starts[:-1]
        a, b = self.corner_verts, self.corner_verts[nxt]
        keys = np.stack([np.minimum(a, b), np.maximum(a, b)], axis=1)
        edges, self.corner_edges = np.unique(keys, axis=0, return_inverse=True)
        self.corner_edges = self.corner_edges.ravel()
        self.n_edges = len(edges)

    def check(self, *, flip: bool = False, sky: bool = False) -> orient.Orientation:
        area, nz = orient.area_normals(
            self.positions, self.face_starts, self.corner_verts, flip=flip
        )
        wound = orient.winding(self.face_starts, self.corner_verts, self.corner_edges, self.n_edges)
        found = orient.check(area, nz, wound)
        if not sky:
            return found
        faces, draws = orient.sky_sample(area, nz)
        points = orient.centres(self.positions, self.face_starts, self.corner_verts, faces)
        origins = orient.sky_origins(points, nz[faces], 1e-6)
        hit = open_sky(self, origins)
        return orient.sky_votes(
            found, wound.labels[faces], wound.inconsistent, draws, nz[faces], hit
        )


def merge(*meshes: tuple[np.ndarray, list]) -> tuple[np.ndarray, list]:
    """Pieces side by side, unwelded: ``(positions, faces)`` with ids offset."""
    positions, faces, base = [], [], 0
    for p, f in meshes:
        positions.append(np.asarray(p, dtype=np.float64))
        faces += [[v + base for v in face] for face in f]
        base += len(p)
    return np.concatenate(positions), faces


def box(lo=(0.0, 0.0, 0.0), hi=(1.0, 1.0, 1.0)) -> tuple[np.ndarray, list]:
    """A closed box of quads wound counter-clockwise seen from outside."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    p = [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    ]
    faces = [
        [0, 3, 2, 1],  # bottom
        [4, 5, 6, 7],  # top
        [0, 1, 5, 4],
        [1, 2, 6, 5],
        [2, 3, 7, 6],
        [3, 0, 4, 7],
    ]
    return np.array(p), faces


def reverse(faces: list, which=None) -> list:
    which = range(len(faces)) if which is None else which
    return [f[::-1] if i in which else f for i, f in enumerate(faces)]


def grid(n: int, z=lambda x, y: 0.0 * x, size: float = 1.0) -> tuple[np.ndarray, list]:
    """An open terrain of n x n quads split into triangles, normals up."""
    u = np.linspace(0.0, size, n + 1)
    x, y = np.meshgrid(u, u, indexing="ij")
    p = np.stack([x.ravel(), y.ravel(), z(x, y).ravel()], axis=1)
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    a = (i * (n + 1) + j).ravel()
    b, c, d = a + (n + 1), a + (n + 1) + 1, a + 1
    tris = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    return p, tris.tolist()


def shelter(top: bool) -> tuple[np.ndarray, list]:
    """A rock shelter in section, extruded 4 along y: floor, back wall, ceiling and
    (``top``) the rock above it, one welded strip of 1-wide quads with normals into
    the air.

    The section runs floor (z 0, x 8..0) -> back wall (x 0, z 0..1) -> ceiling
    (z 1, x 0..3) -> brow (x 3, z 1..1.5) -> top (z 1.5, x 3..0).
    """
    section = [(float(x), 0.0) for x in range(8, -1, -1)]
    section += [(0.0, 1.0), (1.0, 1.0), (2.0, 1.0), (3.0, 1.0)]
    if top:
        section += [(3.0, 1.5), (2.0, 1.5), (1.0, 1.5), (0.0, 1.5)]
    k = len(section)
    p = [(x, y, z) for y in (0.0, 4.0) for x, z in section]
    # With this order the normals face the air side: up on the floor and the
    # top, +x on the back wall and the brow, down under the ceiling.
    faces = [[i, k + i, k + i + 1, i + 1] for i in range(k - 1)]
    return np.array(p), faces


def open_sky(mesh: Mesh, origins: np.ndarray) -> np.ndarray:
    """Whether a ray straight up from each origin misses every face (brute force,
    fan-triangulated, a stand-in for Blender's Raycast)."""
    tris = []
    for f0, f1 in zip(mesh.face_starts[:-1], mesh.face_starts[1:], strict=True):
        ids = mesh.corner_verts[f0:f1]
        tris += [(ids[0], ids[i], ids[i + 1]) for i in range(1, len(ids) - 1)]
    t = mesh.positions[np.array(tris)]
    a, b, c = t[:, 0, :2], t[:, 1, :2], t[:, 2, :2]
    out = np.ones(len(origins), dtype=bool)
    for k, o in enumerate(origins):

        def side(p, q, o=o):
            return (q[:, 0] - p[:, 0]) * (o[1] - p[:, 1]) - (q[:, 1] - p[:, 1]) * (o[0] - p[:, 0])

        s1, s2, s3 = side(a, b), side(b, c), side(c, a)
        inside = ((s1 >= 0) & (s2 >= 0) & (s3 >= 0)) | ((s1 <= 0) & (s2 <= 0) & (s3 <= 0))
        # Height of the triangle's plane above the origin's plan point.
        n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        ok = inside & (np.abs(n[:, 2]) > 1e-12)
        z = (
            t[ok, 0, 2]
            - (n[ok, 0] * (o[0] - t[ok, 0, 0]) + n[ok, 1] * (o[1] - t[ok, 0, 1])) / n[ok, 2]
        )
        out[k] = not (z > o[2]).any()
    return out


# --- area and downward share ---------------------------------------------------------------


def test_area_normals_of_a_box() -> None:
    p, f = box(hi=(2.0, 3.0, 4.0))
    m = Mesh(p, f)
    area, nz = orient.area_normals(m.positions, m.face_starts, m.corner_verts)
    assert area.tolist() == pytest.approx([6.0, 6.0, 8.0, 12.0, 8.0, 12.0])
    assert nz.tolist() == pytest.approx([-1.0, 1.0, 0.0, 0.0, 0.0, 0.0])


def test_area_normals_in_chunks_match_and_far_from_the_origin() -> None:
    p, f = grid(30, z=lambda x, y: np.sin(3 * x) * np.cos(2 * y))
    m = Mesh(p, f)
    whole = orient.area_normals(m.positions, m.face_starts, m.corner_verts)
    parts = orient.area_normals(m.positions, m.face_starts, m.corner_verts, chunk=7)
    far = orient.area_normals(m.positions + 5e6, m.face_starts, m.corner_verts)
    for a, b in zip(whole, parts, strict=True):
        np.testing.assert_allclose(a, b)
    np.testing.assert_allclose(whole[0], far[0], rtol=1e-6)
    np.testing.assert_allclose(whole[1], far[1], atol=1e-5)


def test_polygons_and_triangles_agree() -> None:
    p, f = box()
    quads = Mesh(p, f)
    tris = Mesh(p, [t for q in f for t in ([q[0], q[1], q[2]], [q[0], q[2], q[3]])])
    assert quads.check().area == pytest.approx(tris.check().area) == pytest.approx(6.0)
    assert quads.check().down_share == pytest.approx(tris.check().down_share)


def test_downward_share() -> None:
    p, f = box()
    assert Mesh(p, f).check().down_share == pytest.approx(1 / 6)
    p, f = grid(8)
    assert Mesh(p, f).check().down_share == 0.0
    assert Mesh(p, reverse(f)).check().down_share == pytest.approx(1.0)
    # A mirroring transform reverses the normals Blender shows.
    assert Mesh(p, f).check(flip=True).down_share == pytest.approx(1.0)


def test_vertical_walls_do_not_face_down() -> None:
    p, f = box()
    # Tilt the walls by float noise only.
    p = p + np.where(p[:, 2:3] > 0.5, [1e-9, 0.0, 0.0], 0.0)
    found = Mesh(p, f).check()
    assert found.down_share == pytest.approx(1 / 6)


# --- winding ---------------------------------------------------------------------------------


def test_a_consistent_box_and_a_flipped_one() -> None:
    p, f = box()
    for faces in (f, reverse(f)):
        found = Mesh(p, faces).check()
        assert (found.parts, found.inconsistent_parts, found.inconsistent_edges) == (1, 0, 0)


def test_one_reversed_face_is_inconsistent_on_its_four_edges() -> None:
    p, f = box()
    found = Mesh(p, reverse(f, {1})).check()
    assert (found.parts, found.inconsistent_parts, found.inconsistent_edges) == (1, 1, 4)


def test_mixed_winding_on_a_triangulated_terrain() -> None:
    p, f = grid(10)
    flipped = set(range(0, len(f), 7))
    found = Mesh(p, reverse(f, flipped)).check()
    assert found.parts == 1
    assert found.inconsistent_parts == 1
    # Every edge between a flipped and an unflipped triangle.
    m = Mesh(p, f)
    nxt_faces = np.repeat(np.arange(len(f)), 3)
    by_edge = {}
    for e, face in zip(m.corner_edges.tolist(), nxt_faces.tolist(), strict=True):
        by_edge.setdefault(e, []).append(face)
    expected = sum(
        1 for fs in by_edge.values() if len(fs) == 2 and ((fs[0] in flipped) != (fs[1] in flipped))
    )
    assert found.inconsistent_edges == expected > 0


def test_parts_are_counted_by_shared_edges_not_position() -> None:
    # Two boxes touching at a face, unwelded, and a third apart; the second is flipped,
    # the third has one reversed face.
    p, f = merge(
        box(),
        (box((1.0, 0.0, 0.0), (2.0, 1.0, 1.0))[0], reverse(box()[1])),
        (box((5.0, 0.0, 0.0), (6.0, 1.0, 1.0))[0], reverse(box()[1], {2})),
    )
    found = Mesh(p, f).check()
    assert (found.parts, found.inconsistent_parts, found.inconsistent_edges) == (3, 1, 4)


def test_non_manifold_edges_link_parts_but_are_not_judged() -> None:
    # Three quads on one edge (a fin), whatever their winding.
    p = [(0, 0, 0), (0, 1, 0), (1, 0, 0), (1, 1, 0), (-1, 0, 0), (-1, 1, 0), (0, 0, 1), (0, 1, 1)]
    f = [[0, 2, 3, 1], [0, 1, 5, 4], [1, 0, 6, 7]]
    found = Mesh(p, f).check()
    assert (found.parts, found.inconsistent_edges) == (1, 0)


# --- sky test --------------------------------------------------------------------------------


def test_the_sky_test_finds_a_flipped_box_and_terrain() -> None:
    p, f = box()
    assert Mesh(p, f).check(sky=True).inward_parts == 0
    assert Mesh(p, reverse(f)).check(sky=True).inward_parts == 1
    p, f = grid(8, z=lambda x, y: 0.1 * x * y)
    assert Mesh(p, f).check(sky=True).inward_parts == 0
    flipped = Mesh(p, reverse(f)).check(sky=True)
    assert (flipped.sky_parts, flipped.inward_parts) == (1, 1)


def test_the_sky_test_judges_each_part() -> None:
    p, f = merge(
        box(),
        (box((3.0, 0.0, 0.0), (4.0, 1.0, 1.0))[0], reverse(box()[1])),
        (box((6.0, 0.0, 0.0), (7.0, 1.0, 1.0))[0], reverse(box()[1], {1})),
    )
    found = Mesh(p, f).check(sky=True)
    # The third box is left to the winding issue.
    assert (found.parts, found.sky_parts, found.inward_parts) == (3, 2, 1)


def test_an_overhang_is_not_inward() -> None:
    p, f = shelter(top=True)
    found = Mesh(p, f).check(sky=True)
    assert (found.parts, found.inconsistent_parts) == (1, 0)
    assert found.down_share == pytest.approx(12 / 62)
    assert (found.sky_parts, found.inward_parts) == (1, 0)
    assert Mesh(p, reverse(f)).check(sky=True).inward_parts == 1


def test_a_shelter_without_its_top_is_mostly_overhang_but_not_inward() -> None:
    # The ceiling is open to the sky too (its rock was not scanned), but the
    # open floor in front outvotes it: 12 of the open 32 m² face down.
    p, f = shelter(top=False)
    found = Mesh(p, f).check(sky=True)
    assert found.down_share == pytest.approx(12 / 48)
    assert (found.sky_parts, found.inward_parts) == (1, 0)


def test_steep_faces_do_not_vote() -> None:
    p, f = box()
    walls = Mesh(p, [face for i, face in enumerate(f) if i > 1])
    found = walls.check(sky=True)
    assert (found.sky_parts, found.inward_parts) == (0, 0)


def test_the_sample_is_area_weighted_and_fixed() -> None:
    area = np.array([1.0, 3.0, 0.0, 1.0])
    nz = np.array([1.0, 1.0, 1.0, 0.0])  # the last is vertical
    faces, draws = orient.sky_sample(area, nz, n=4000)
    assert faces.tolist() == [0, 1]
    assert draws.sum() == 4000
    assert draws[1] / draws[0] == pytest.approx(3.0, rel=0.1)
    again = orient.sky_sample(area, nz, n=4000)
    assert again[1].tolist() == draws.tolist()
    empty = orient.sky_sample(area, np.zeros(4))
    assert len(empty[0]) == 0


def test_submesh_and_centres() -> None:
    p, f = box()
    m = Mesh(p, f)
    sp, ss, sc = orient.submesh(m.positions, m.face_starts, m.corner_verts, np.array([1, 3]))
    assert ss.tolist() == [0, 4, 8]
    assert len(sp) == 6
    sub = Mesh(sp, [sc[0:4].tolist(), sc[4:8].tolist()])
    assert sub.check().area == pytest.approx(2.0)
    c = orient.centres(m.positions, m.face_starts, m.corner_verts, np.array([0, 1]))
    np.testing.assert_allclose(c, [[0.5, 0.5, 0.0], [0.5, 0.5, 1.0]])


def test_sky_origins_clear_the_face_plane_by_the_offset() -> None:
    o = orient.sky_origins(np.zeros((2, 3)), np.array([1.0, -0.2]), 0.01)
    np.testing.assert_allclose(o[:, 2], [0.01, 0.05])


# --- issues ----------------------------------------------------------------------------------


def _codes(found, *, sky=True):
    return [(i.code, i.severity) for i in orient.issues("Rock", found, sky=sky)]


def test_issues_name_the_object_and_the_blender_fix() -> None:
    p, f = box()
    assert _codes(Mesh(p, f).check(sky=True)) == []
    [issue] = orient.issues("Rock", Mesh(p, reverse(f, {1})).check(sky=True), sky=True)
    assert (issue.code, issue.severity, issue.subject) == (
        orient.INCONSISTENT,
        Severity.WARNING,
        "Rock",
    )
    assert "Recalculate Outside" in issue.message and "4 edges" in issue.message
    [issue] = orient.issues("Rock", Mesh(p, reverse(f)).check(sky=True), sky=True)
    assert issue.code == orient.INWARD and "Flip" in issue.message


def test_a_mostly_downward_surface() -> None:
    p, f = grid(6)
    flipped = Mesh(p, reverse(f))
    # Without rays (before an analysis) a warning; the sky test makes it inward.
    assert _codes(flipped.check(), sky=False) == [(orient.DOWN, Severity.WARNING)]
    assert _codes(flipped.check(sky=True)) == [(orient.INWARD, Severity.WARNING)]
    # Cleared by the sky test: a note that it is overhang.
    cleared = orient.Orientation(10, 1.0, 0.6, 1, 0, 0, sky_parts=1, inward_parts=0)
    assert _codes(cleared) == [(orient.DOWN, Severity.INFO)]
    assert "60%" in orient.issues("Rock", cleared, sky=True)[0].message
    # Winding comes first: no downward warning while faces disagree.
    mixed = orient.Orientation(10, 1.0, 0.6, 1, 3, 1)
    assert _codes(mixed, sky=False) == [(orient.INCONSISTENT, Severity.WARNING)]


def test_issue_codes_are_documented() -> None:
    from pathlib import Path

    guide = (Path(__file__).resolve().parents[2] / "docs/guide/publish.md").read_text()
    for code in orient.CODES:
        assert f"`{code}`" in guide


# --- cost --------------------------------------------------------------------------------------


def test_a_million_triangles_take_well_under_a_few_seconds() -> None:
    n = 707  # 2 x 707² ≈ 1.0 million triangles
    u = np.linspace(0.0, 1.0, n + 1)
    x, y = np.meshgrid(u, u, indexing="ij")
    positions = np.stack([x.ravel(), y.ravel(), np.zeros(x.size)], axis=1)
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    a = (i * (n + 1) + j).ravel()
    tris = np.concatenate(
        [np.stack([a, a + n + 1, a + n + 2], 1), np.stack([a, a + n + 2, a + 1], 1)]
    )
    m = Mesh.__new__(Mesh)
    m.positions = positions
    m.face_starts = np.arange(0, 3 * len(tris) + 1, 3)
    m.corner_verts = tris.ravel()
    nxt = tris[:, [1, 2, 0]].ravel()
    keys = np.minimum(m.corner_verts, nxt) * len(positions) + np.maximum(m.corner_verts, nxt)
    _, m.corner_edges = np.unique(keys, return_inverse=True)
    m.n_edges = int(m.corner_edges.max()) + 1
    began = time.perf_counter()
    found = m.check()
    seconds = time.perf_counter() - began
    assert (found.faces, found.parts, found.inconsistent_edges) == (len(tris), 1, 0)
    assert seconds < 5.0
