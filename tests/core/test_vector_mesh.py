"""Vector geometries to mesh arrays and back (core.vector.mesh), and refresh digests."""

from __future__ import annotations

import numpy as np

from geotools.core.vector import ColumnType, Kind
from geotools.core.vector import digest as dg
from geotools.core.vector import mesh as vm
from geotools.core.vector.model import Column, GeometryBuilder

E0, N0, A0 = 502_000.0, 6_961_000.0, 10.0
OFFSET = (E0, N0, A0)


def _square(x: float, y: float, s: float, z: float | None = None) -> np.ndarray:
    pts = [[x, y], [x + s, y], [x + s, y + s], [x, y + s], [x, y]]
    a = np.array(pts, dtype=np.float64)
    return np.column_stack([a, np.full(len(a), z)]) if z is not None else a


def _area(positions: np.ndarray, loops: np.ndarray) -> float:
    p = positions[loops].astype(np.float64)
    x, y = p[:, 0], p[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _faces(m: vm.MeshArrays) -> list[np.ndarray]:
    ends = np.append(m.loop_starts[1:], len(m.loop_vertices))
    return [m.loop_vertices[s:e] for s, e in zip(m.loop_starts, ends, strict=True)]


def _no_tessellation(rings: list[np.ndarray]) -> np.ndarray:
    raise AssertionError("not called for polygons without holes")


def test_to_local_is_binary64_then_float32() -> None:
    coords = np.array([[E0 + 0.123456789, N0 + 0.987654321, 25.5], [E0, N0, np.nan]])
    local = vm.to_local(coords, OFFSET)
    assert local.dtype == np.float32
    np.testing.assert_array_equal(
        local, np.array([[0.123456789, 0.987654321, 15.5], [0, 0, 0]], np.float32)
    )
    # Shifting float32 coordinates directly would lose the decimetres.
    assert np.float32(N0 + 0.987654321) - np.float32(N0) != local[0, 1]
    back = vm.from_local(local, OFFSET)
    assert back.dtype == np.float64
    assert back[0, 0] == E0 + float(np.float32(0.123456789))  # exact in binary64
    assert abs(back[0, 1] - (N0 + 0.987654321)) < 1e-7


def test_polygons_become_ccw_ngons() -> None:
    b = GeometryBuilder()
    b.add_polygon(0, [_square(E0, N0, 10.0)])
    b.add_polygon(1, [_square(E0 + 20, N0, 5.0)[::-1]])  # clockwise in the file
    b.add_polygon(1, [_square(E0 + 40, N0, 5.0, 30.0)])
    m = vm.polygon_mesh(b.finish()[Kind.POLYGONS], OFFSET, _no_tessellation)
    faces = _faces(m)
    assert [len(f) for f in faces] == [4, 4, 4]
    assert [_area(m.positions, f) for f in faces] == [100.0, 25.0, 25.0]
    assert m.element_feature.tolist() == [0, 1, 1]
    assert len(m.positions) == 12  # closing points dropped
    assert m.positions[0].tolist() == [0.0, 0.0, 0.0]  # 2D: z = 0
    assert m.positions[-1].tolist() == [40.0, 5.0, 20.0]  # Z - offset A
    assert m.dropped_parts == 0


def test_degenerate_parts_are_skipped() -> None:
    b = GeometryBuilder()
    # The 0.2 PYSU case: a MultiPolygon part with two distinct exterior points.
    b.add_polygon(0, [np.array([[E0, N0], [E0 + 1, N0], [E0, N0], [E0 + 1, N0]])])
    b.add_polygon(0, [_square(E0, N0, 2.0)])
    b.add_polygon(1, [np.array([[E0, N0], [E0, N0]])])  # a single point
    b.add_polygon(2, [np.array([[E0, N0], [E0 + 1, N0], [E0 + 2, N0], [E0, N0]])])  # no area
    # Consecutive duplicates and a repeated closing point are cleaned, not dropped.
    dup = [[E0, N0], [E0, N0], [E0 + 4, N0], [E0 + 4, N0 + 4], [E0 + 4, N0 + 4], [E0, N0]]
    b.add_polygon(3, [np.array(dup + [[E0, N0]])])
    m = vm.polygon_mesh(b.finish()[Kind.POLYGONS], OFFSET, _no_tessellation)
    assert m.dropped_parts == 3
    assert m.element_feature.tolist() == [0, 3]
    assert [len(f) for f in _faces(m)] == [4, 3]
    assert _area(m.positions, _faces(m)[1]) == 8.0


def test_holes_are_tessellated() -> None:
    calls = []

    def tessellate(rings: list[np.ndarray]) -> np.ndarray:
        calls.append([r.copy() for r in rings])
        # Exterior 0..3, hole 4..7: a known triangulation of the frame, some clockwise.
        return np.array(
            [[0, 1, 4], [1, 7, 4], [1, 2, 7], [2, 6, 7], [2, 3, 6], [3, 5, 6], [3, 0, 5], [0, 4, 5]]
        )[:, [0, 2, 1]]

    b = GeometryBuilder()
    hole = _square(E0 + 3, N0 + 3, 4.0)[::-1]  # clockwise, as RFC 7946 has it
    b.add_polygon(0, [_square(E0, N0, 10.0), hole, np.array([[E0, N0], [E0, N0]])])
    m = vm.polygon_mesh(b.finish()[Kind.POLYGONS], OFFSET, tessellate)
    (rings,) = calls
    assert len(rings) == 2  # the degenerate hole is ignored
    assert rings[0][0].tolist() == [0.0, 0.0]  # relative to the first point
    faces = _faces(m)
    assert len(faces) == 8 and all(len(f) == 3 for f in faces)
    areas = [_area(m.positions, f) for f in faces]
    assert min(areas) > 0  # rewound counter-clockwise
    assert sum(areas) == 100.0 - 16.0
    assert len(m.positions) == 8
    assert m.dropped_parts == 0


def test_lines_and_points() -> None:
    b = GeometryBuilder()
    b.add_line(0, np.array([[E0, N0, 1.0], [E0 + 1, N0, 2.0], [E0 + 1, N0, 2.0], [E0 + 2, N0, 3]]))
    b.add_line(1, _square(E0, N0, 1.0))  # closed
    b.add_line(2, np.array([[E0, N0], [E0, N0]]))  # degenerate
    b.add_points(3, np.array([[E0, N0, 5.0], [E0 + 1, N0 + 1, 6.0]]))
    geoms = b.finish()
    lines = vm.line_mesh(geoms[Kind.LINES], OFFSET)
    assert lines.edges.tolist() == [[0, 1], [1, 2], [3, 4], [4, 5], [5, 6], [6, 3]]
    assert lines.element_feature.tolist() == [0, 0, 1, 1, 1, 1]
    assert lines.positions[:3, 2].tolist() == [-9.0, -8.0, -7.0]
    assert lines.dropped_parts == 1
    points = vm.point_mesh(geoms[Kind.POINTS], OFFSET)
    assert points.positions.tolist() == [[0, 0, -5], [1, 1, -4]]
    assert points.element_feature.tolist() == [0, 0]


def _mesh_input(
    positions: np.ndarray,
    faces: list[list[int]],
    edges: list[list[int]] | None = None,
    face_fid: list[int] | None = None,
    **attrs,
) -> vm.MeshInput:
    sizes = np.array([len(f) for f in faces], dtype=np.int64)
    starts = np.concatenate(([0], np.cumsum(sizes)[:-1])) if len(faces) else sizes
    loops = np.array([v for f in faces for v in f], dtype=np.int64)
    face_edges = {tuple(sorted((f[i], f[(i + 1) % len(f)]))) for f in faces for i in range(len(f))}
    all_edges = sorted(face_edges) + [tuple(e) for e in (edges or [])]
    index = {e: i for i, e in enumerate(all_edges)}
    loop_edges = np.array(
        [index[tuple(sorted((f[i], f[(i + 1) % len(f)])))] for f in faces for i in range(len(f))],
        dtype=np.int64,
    )
    face_attrs = {}
    if face_fid is not None:
        face_attrs["fid"] = (ColumnType.INT, np.array(face_fid))
    face_attrs.update(attrs.get("face_attrs", {}))
    return vm.MeshInput(
        positions=positions,
        loop_starts=starts,
        loop_totals=sizes,
        loop_vertices=loops,
        edges=np.array(all_edges, dtype=np.int64).reshape(-1, 2),
        loop_edges=loop_edges,
        face_attrs=face_attrs,
        edge_attrs=attrs.get("edge_attrs", {}),
        point_attrs=attrs.get("point_attrs", {}),
    )


def _grid_with_hole() -> tuple[np.ndarray, list[list[int]]]:
    """A 3 x 3 grid of unit quads with the middle one missing, z = 2 + x."""
    xs, ys = np.meshgrid(np.arange(4.0), np.arange(4.0))
    pos = np.column_stack([xs.ravel(), ys.ravel(), 2.0 + xs.ravel()])
    faces = [
        [j * 4 + i, j * 4 + i + 1, (j + 1) * 4 + i + 1, (j + 1) * 4 + i]
        for j in range(3)
        for i in range(3)
        if (i, j) != (1, 1)
    ]
    return pos, faces


def test_polygons_from_faces_rebuild_holes() -> None:
    pos, faces = _grid_with_hole()
    pos[:, 0] += E0
    mesh = _mesh_input(
        pos,
        faces,
        face_fid=[7] * len(faces),
        face_attrs={"depth": (ColumnType.FLOAT, np.full(len(faces), 1.5, np.float32))},
    )
    geoms, fids, columns = vm.polygons_from_faces(mesh)
    assert fids.tolist() == [7]
    assert (geoms.n_features, geoms.n_parts, geoms.n_rings) == (1, 1, 2)
    ext = geoms.coords[geoms.ring_offsets[0] : geoms.ring_offsets[1]]
    hole = geoms.coords[geoms.ring_offsets[1] : geoms.ring_offsets[2]]
    assert len(ext) == 13 and len(hole) == 5  # closed rings
    assert (ext[0] == ext[-1]).all() and (hole[0] == hole[-1]).all()
    shoelace = lambda r: 0.5 * np.sum(r[:-1, 0] * r[1:, 1] - r[1:, 0] * r[:-1, 1])
    rel = lambda r: r - [E0, 0.0, 0.0]
    assert shoelace(rel(ext)) == 9.0 and shoelace(rel(hole)) == -1.0  # CCW exterior, CW hole
    np.testing.assert_array_equal(ext[:, 2], 2.0 + ext[:, 0] - E0)  # Z kept per vertex
    assert columns["depth"].values.tolist() == [1.5] and "fid" not in columns


def test_polygons_from_faces_multipolygon_and_flipped() -> None:
    a = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=np.float64)
    pos = np.concatenate([a, a + [5, 0, 0], a + [10, 0, 0]])
    faces = [[0, 1, 2, 3], [4, 5, 6, 7], [11, 10, 9, 8]]  # the last one points down
    geoms, fids, _ = vm.polygons_from_faces(_mesh_input(pos, faces, face_fid=[1, 1, 2]))
    assert fids.tolist() == [1, 2]
    assert np.diff(geoms.feature_offsets).tolist() == [2, 1]
    for r in range(geoms.n_rings):
        ring = geoms.coords[geoms.ring_offsets[r] : geoms.ring_offsets[r + 1]]
        x, y = ring[:, 0], ring[:, 1]
        assert 0.5 * np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]) == 1.0


def test_polygons_from_faces_without_fid_and_pinched() -> None:
    # Two triangles touching at one vertex (a pinch): written face by face.
    pos = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [2, 1, 0], [2, 2, 0]], dtype=np.float64)
    geoms, fids, _ = vm.polygons_from_faces(
        _mesh_input(pos, [[0, 1, 2], [2, 3, 4]], face_fid=[3, 3])
    )
    assert fids.tolist() == [3] and geoms.n_parts == 2 and geoms.n_rings == 2
    geoms, fids, _ = vm.polygons_from_faces(_mesh_input(pos, [[0, 1, 2], [2, 3, 4]]))
    assert fids.tolist() == [1, 2] and geoms.n_features == 2


def test_lines_and_points_from_mesh() -> None:
    pos = np.array([[float(i), 0.0, float(i)] for i in range(8)])
    mesh = _mesh_input(
        pos,
        [],
        edges=[[0, 1], [1, 2], [3, 4], [5, 6]],
        edge_attrs={
            "fid": (ColumnType.INT, np.array([4, 4, 4, 9])),
            "name": (ColumnType.STRING, [b"a", b"a", b"a", b"b"]),
        },
        point_attrs={"fid": (ColumnType.INT, np.arange(8))},
    )
    geoms, fids, columns = vm.lines_from_edges(mesh)
    assert fids.tolist() == [4, 9]
    assert np.diff(geoms.feature_offsets).tolist() == [2, 1]  # fid 4 is a MultiLineString
    assert np.diff(geoms.ring_offsets).tolist() == [3, 2, 2]
    assert geoms.coords[:3, 0].tolist() == [0, 1, 2]
    assert columns["name"].values == ["a", "b"]
    points, pfids, _ = vm.points_from_vertices(mesh)
    assert pfids.tolist() == [7] and points.coords.tolist() == [[7, 0, 7]]
    datas = vm.vectors_from_mesh(mesh, "x", None)
    assert [d.kinds() for d in datas] == [[Kind.LINES], [Kind.POINTS]]


def test_lines_carry_a_point_attribute_as_m(tmp_path) -> None:
    """A section's chainage: LineString ZM in a GeoPackage, a per-point property in GeoJSON."""
    import json
    import sqlite3
    import struct

    from geotools.core.vector import geojson, gpkg

    pos = np.array([[float(i), 0.0, 10.0 + i] for i in range(6)])
    chainage = np.array([0.0, 1.0, 2.0, 0.0, 1.5, 0.1], dtype=np.float32)
    mesh = _mesh_input(
        pos,
        [],
        edges=[[0, 1], [1, 2], [3, 4], [4, 5]],
        edge_attrs={"fid": (ColumnType.INT, np.array([1, 1, 2, 2]))},
        point_attrs={"chainage": (ColumnType.FLOAT, chainage)},
    )
    mesh.measure = "chainage"
    (data,) = vm.vectors_from_mesh(mesh, "sections", None)
    geoms = data.geometries[Kind.LINES]
    assert geoms.measure_name == "chainage"
    assert geoms.measures.tolist() == [0.0, 1.0, 2.0, 0.0, 1.5, 0.1]  # shortest decimals
    out = tmp_path / "s.gpkg"
    gpkg.write_layer(out, "sections", data, Kind.LINES, None)
    with sqlite3.connect(out) as con:
        assert con.execute("SELECT z, m FROM gpkg_geometry_columns").fetchone() == (1, 1)
        blobs = [r[0] for r in con.execute("SELECT geom FROM sections ORDER BY fid")]
    for blob, expected in zip(blobs, ([0.0, 1.0, 2.0], [0.0, 1.5, 0.1]), strict=True):
        wkb = blob[8 + 48 :]  # header, XYZ envelope
        byte_order, code, n = struct.unpack("<BII", wkb[:9])
        assert (byte_order, code, n) == (1, 3002, 3)  # ISO LineString ZM
        xyzm = np.frombuffer(wkb[9:], "<f8").reshape(n, 4)
        assert xyzm[:, 3].tolist() == expected
        assert xyzm[:, 2].tolist() == [10.0 + v for v in xyzm[:, 0]]
    # The readers drop M; the geometry is unchanged.
    back = gpkg.read_layer(out, "sections").geometries[Kind.LINES]
    np.testing.assert_array_equal(back.coords, geoms.coords)
    path = tmp_path / "s.geojson"
    geojson.write(path, data, None)
    features = json.loads(path.read_text())["features"]
    assert [f["properties"]["chainage"] for f in features] == [[0.0, 1.0, 2.0], [0.0, 1.5, 0.1]]
    assert [len(f["geometry"]["coordinates"][0]) for f in features] == [3, 3]  # XYZ, no M
    # Without a measure, plain XYZ as before.
    mesh.measure = None
    (plain,) = vm.vectors_from_mesh(mesh, "lines", None)
    assert plain.geometries[Kind.LINES].measures is None
    gpkg.write_layer(tmp_path / "p.gpkg", "lines", plain, Kind.LINES, None)
    with sqlite3.connect(tmp_path / "p.gpkg") as con:
        assert con.execute("SELECT z, m FROM gpkg_geometry_columns").fetchone() == (1, 0)


def test_mesh_round_trip_through_tessellation() -> None:
    """Polygons -> mesh arrays -> polygons gives the same rings (as sets of points)."""

    def fan(rings: list[np.ndarray]) -> np.ndarray:
        # Valid only for the frame below: exterior 0..3, hole 4..7 (CW).
        return np.array(
            [[0, 1, 7], [0, 7, 4], [1, 2, 6], [1, 6, 7], [2, 3, 5], [2, 5, 6], [3, 0, 4], [3, 4, 5]]
        )

    b = GeometryBuilder()
    b.add_polygon(0, [_square(E0, N0, 10.0, 50.0), _square(E0 + 3, N0 + 3, 4.0, 50.0)[::-1]])
    b.add_polygon(1, [_square(E0 + 20, N0, 5.0, 51.0)])
    m = vm.polygon_mesh(b.finish()[Kind.POLYGONS], OFFSET, fan)
    fid = np.array([10, 11])[m.element_feature]
    sizes = np.diff(np.append(m.loop_starts, len(m.loop_vertices)))
    faces = [m.loop_vertices[s : s + n].tolist() for s, n in zip(m.loop_starts, sizes, strict=True)]
    mesh = _mesh_input(vm.from_local(m.positions, OFFSET), faces, face_fid=fid.tolist())
    geoms, fids, _ = vm.polygons_from_faces(mesh)
    assert fids.tolist() == [10, 11]
    assert np.diff(geoms.part_offsets).tolist() == [2, 1]
    ext = geoms.coords[geoms.ring_offsets[0] : geoms.ring_offsets[1] - 1]
    assert {tuple(p) for p in ext.tolist()} == {
        tuple(p) for p in _square(E0, N0, 10.0, 50.0)[:-1].tolist()
    }


def test_digests_count_added_removed_changed() -> None:
    positions = np.arange(18, dtype=np.float32).reshape(6, 3)
    owner = np.array([0, 0, 1, 1, 2, 2])
    cols = {
        "depth": (ColumnType.FLOAT, np.array([1.0, 2.0, np.nan])),
        "name": (ColumnType.STRING, [b"a", b"b", b"c"]),
    }
    base = dg.feature_digests(3, positions, owner, cols)
    assert len(set(base.tolist())) == 3
    assert (dg.feature_digests(3, positions, owner, cols) == base).all()
    # Order of vertices within a feature does not matter.
    swapped = positions[[1, 0, 2, 3, 4, 5]]
    assert (dg.feature_digests(3, swapped, owner, cols) == base).all()
    moved = positions.copy()
    moved[2, 0] += 0.5
    renamed = dict(cols, name=(ColumnType.STRING, [b"a", b"b", b"z"]))
    new = dg.feature_digests(3, moved, owner, renamed)
    assert dg.diff(np.array([1, 2, 3]), base, np.array([1, 2, 3]), new) == (0, 0, 2)
    assert dg.diff(np.array([1, 2, 3]), base, np.array([2, 3, 4]), base[[1, 2, 0]]) == (1, 1, 0)


def test_stored_values_match_blender_types() -> None:
    assert vm.stored_values(Column("a", ColumnType.FLOAT, np.array([1.5]))).dtype == np.float32
    assert vm.stored_values(Column("a", ColumnType.INT, np.array([2]))).dtype == np.int32
    assert vm.stored_values(Column("a", ColumnType.STRING, ["é"])) == ["é".encode()]


def test_float32_attributes_are_written_as_their_shortest_decimal() -> None:
    values = np.array([0.1, 123.4, 2345.25, np.nan, -1e-7, 3.4e38], dtype=np.float32)
    out = vm.shortest_float64(values)
    assert out.dtype == np.float64
    assert out[:3].tolist() == [0.1, 123.4, 2345.25]
    assert np.isnan(out[3]) and out[4] == -1e-7
    assert (out.astype(np.float32)[~np.isnan(out)] == values[~np.isnan(values)]).all()
    assert vm.shortest_float64(np.array([0.1])).tolist() == [0.1]
