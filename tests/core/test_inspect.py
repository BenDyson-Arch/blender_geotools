"""Tests for geotools.core.inspect: the build record, mesh assembly, tracing, texel density."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from geotools.core.facemap import FaceMap, save_face_map
from geotools.core.inspect import (
    InspectError,
    OutputRecord,
    assemble,
    depth_counts,
    placement_differences,
    selected_rows,
    source_faces,
    summarise_density,
    texel_density,
    tile_face_mask,
    topology_problems,
    triangle_areas,
)
from geotools.core.output_frame import SceneFrame
from geotools.core.output_tiles import NO_ID, DecodedPrimitive, DecodedTile
from geotools.core.placement import Placement

# Three primitives: rows 0-3 (Ground polygons 10, 10, 11, 11), 4 (Wall 0), 5-7 (Ground 12, Wall 1, 2).
FACE_MAP = FaceMap(
    object_names=("Ground", "Wall"),
    primitive_offsets=np.array([0, 4, 5, 8], np.int64),
    source_object=np.array([0, 0, 0, 0, 1, 0, 1, 1], np.int32),
    source_polygon=np.array([10, 10, 11, 11, 0, 12, 1, 2], np.int32),
    topology={"Ground": "a" * 64, "Wall": "evaluated:" + "b" * 64},
)


# --- the build record -------------------------------------------------------------------------


def test_record_beside_an_archive(tmp_path: Path) -> None:
    archive = tmp_path / "site.3tz"
    with pytest.raises(InspectError, match="does not exist"):
        OutputRecord.load(archive)
    archive.write_bytes(b"")
    bare = OutputRecord.load(archive)
    assert bare.record is None and bare.face_map_path is None and bare.leaf_error is None
    staging = tmp_path / "site.geotools"
    staging.mkdir()
    (staging / "build.json").write_text("{not json")
    with pytest.raises(InspectError, match="Cannot read"):
        OutputRecord.load(archive)
    record = {
        "output_type": "3tz",
        "placement": {"anchor": [1, 2, 3]},
        "geotools": {"source_collection": "Source"},
        "settings": {"profile": "fast"},
        "report_summary": {"maximum_leaf_geometry_error": 0.002},
        "face_map": "facemap.npz",
        "source_glb": {"path": "source.glb"},
    }
    (staging / "build.json").write_text(json.dumps(record))
    loaded = OutputRecord.load(archive)
    assert loaded.placement_record == {"anchor": [1, 2, 3]}
    assert loaded.source_collection == "Source" and loaded.profile == "fast"
    assert loaded.leaf_error == 0.002
    assert loaded.face_map_path is None and loaded.source_glb_path is None
    with pytest.raises(InspectError, match="no face map"):
        loaded.face_map()
    save_face_map(FACE_MAP, staging / "facemap.npz")
    (staging / "source.glb").write_bytes(b"glb")
    assert loaded.face_map().object_names == ("Ground", "Wall")
    assert loaded.source_glb_path == staging / "source.glb"
    (staging / "build.json").write_text(json.dumps({"output_type": "glb"}))
    with pytest.raises(InspectError, match="not a .3tz"):
        OutputRecord.load(archive)


def test_placement_differences() -> None:
    recorded = {"anchor": [153.0, -27.0, 10.0], "orientation_xyzw": [0, 0, 0, 1.0]}
    assert placement_differences(recorded, dict(recorded)) == []
    moved = {**recorded, "anchor": [153.0, -27.0, 10.5]}
    assert placement_differences(recorded, moved) == ["anchor"]
    crs = {"source_crs": "EPSG:7856", "source_offset": [1.0, 2.0, 3.0], "height_offset": 0.0}
    assert placement_differences(crs, {**crs, "height_offset": 1.5}) == ["height offset"]
    assert placement_differences(crs, {}) == ["source crs", "source offset", "height offset"]


# --- assembling a tile ------------------------------------------------------------------------


def _prim(material, first, *, triangle=True, uvs=True) -> DecodedPrimitive:
    positions = np.array([[0, 0, 0], [2, 0, 0], [0, 1, 0], [2, 1, 0]], np.float64) + first
    return DecodedPrimitive(
        material=material,
        positions=positions,
        indices=np.array([[0, 1, 2], [1, 3, 2]], np.uint32),
        normals=np.tile(np.array([0, 0, 1], np.float32), (4, 1)),
        uvs=(np.array([[0, 0], [1, 0], [0, 0.25], [1, 0.25]], np.float32),) if uvs else (),
        colors=None,
        source_primitive=np.array([0, 2], np.int64),
        source_triangle=np.array([3, 0], np.int64) if triangle else None,
    )


def _tile(*prims, role="leaf") -> DecodedTile:
    return DecodedTile(
        uri="content/t.glb",
        depth=1,
        role=role,
        geometric_error=0.0,
        transform=np.eye(4),
        root_transform=np.eye(4),
        primitives=prims,
        pages=(np.zeros((4, 8, 4), np.uint8),),
        materials=({"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}, {}),
        textures=({"source": 0},),
    )


def test_assemble_joins_primitives() -> None:
    mesh = assemble(
        _tile(_prim(1, 0.0), _prim(0, 10.0, uvs=False), _prim(1, 20.0)), SceneFrame(Placement())
    )
    assert mesh.faces == 6 and mesh.positions.shape == (12, 3)
    np.testing.assert_array_equal(mesh.corner_verts[:6], [0, 1, 2, 1, 3, 2])
    np.testing.assert_array_equal(mesh.corner_verts[6:9], [4, 5, 6])
    assert mesh.materials == (1, 0)
    np.testing.assert_array_equal(mesh.material, [0, 0, 1, 1, 0, 0])
    np.testing.assert_array_equal(mesh.source_triangle, [3, 0, 3, 0, 3, 0])
    assert mesh.uvs.shape == (18, 2)
    np.testing.assert_allclose(mesh.uvs[:3], [[0, 1], [1, 1], [0, 0.75]])  # v flipped
    np.testing.assert_allclose(mesh.uvs[6:9], [[0, 1], [0, 1], [0, 1]])  # no UVs: (0, 0) flipped
    np.testing.assert_allclose(mesh.normals, np.tile([0, 0, 1], (12, 1)), atol=1e-7)
    assert mesh.colors is None


def test_assemble_a_coarse_tile_has_no_triangle_ids() -> None:
    mesh = assemble(_tile(_prim(0, 0.0, triangle=False), role="coarse"), SceneFrame(Placement()))
    assert mesh.source_triangle is None
    np.testing.assert_array_equal(mesh.source_primitive, [0, 2])


def test_assemble_refuses_an_empty_tile() -> None:
    with pytest.raises(InspectError, match="no triangles"):
        assemble(_tile(), SceneFrame(Placement()))


# --- tracing --------------------------------------------------------------------------------


def test_leaf_faces_trace_to_their_polygons() -> None:
    found = source_faces(FACE_MAP, np.array([0, 0, 2, 2, 9, 1]), np.array([1, 3, 1, 2, 0, 1]))
    assert set(found.polygons) == {"Ground", "Wall"}
    np.testing.assert_array_equal(found.polygons["Ground"], [10, 11])
    np.testing.assert_array_equal(found.polygons["Wall"], [1, 2])
    assert found.unknown == 2 and not found.whole_primitives  # primitive 9, triangle 1 of 1
    assert found.count == 4
    np.testing.assert_array_equal(found.primitives, [0, 2])


def test_coarse_faces_trace_to_whole_primitives() -> None:
    found = source_faces(FACE_MAP, np.array([2, 2, NO_ID]), None)
    assert found.whole_primitives and found.unknown == 1
    np.testing.assert_array_equal(found.polygons["Ground"], [12])
    np.testing.assert_array_equal(found.polygons["Wall"], [1, 2])


def test_source_selection_to_tile_faces() -> None:
    ground = np.zeros(20, bool)
    ground[11] = True
    rows = selected_rows(FACE_MAP, {"Ground": ground, "Elsewhere": np.ones(3, bool)})
    np.testing.assert_array_equal(np.flatnonzero(rows), [2, 3])
    leaf = tile_face_mask(FACE_MAP, rows, np.array([0, 0, 0, 2, NO_ID]), np.array([2, 1, 3, 0, 0]))
    np.testing.assert_array_equal(leaf, [True, False, True, False, False])
    coarse = tile_face_mask(FACE_MAP, rows, np.array([0, 1, 2, NO_ID]), None)
    np.testing.assert_array_equal(coarse, [True, False, False, False])
    short = selected_rows(FACE_MAP, {"Wall": np.array([False, True])})  # polygon 2 beyond the mask
    np.testing.assert_array_equal(np.flatnonzero(short), [6])


def test_topology_problems() -> None:
    evaluated = (
        "'Wall' was published with modifiers that change its topology, so the face map "
        "numbers evaluated faces, which cannot be selected"
    )
    assert topology_problems(FACE_MAP, {"Ground": "a" * 64, "Wall": "c"}) == [evaluated]
    problems = topology_problems(FACE_MAP, {"Ground": "z" * 64, "Wall": None})
    assert problems == [
        "'Ground' has been edited since it was published",
        "'Wall' is not in the scene any more",
    ]


# --- texel density ----------------------------------------------------------------------------


def test_texel_density() -> None:
    positions = np.array([[0, 0, 0], [2, 0, 0], [0, 2, 0], [0, 0, 5]], np.float64)
    corners = np.array([0, 1, 2, 0, 1, 2, 0, 0, 3])  # a 2 m² triangle twice, then a degenerate one
    uvs = np.array([[0, 0], [0.5, 0], [0, 0.5]] * 3, np.float32)  # UV area 0.125
    np.testing.assert_allclose(triangle_areas(positions, corners), [2.0, 2.0, 0.0])
    density = texel_density(positions, corners, uvs, np.array([1024 * 1024, 0, 4096]))
    assert density[0] == pytest.approx(0.125 * 1024 * 1024 / 2.0)
    assert np.isnan(density[1]) and np.isnan(density[2])  # untextured; no area
    summary = summarise_density(density)
    assert summary.faces == 1 and summary.median == pytest.approx(65536.0)
    assert summary.spacing_m == pytest.approx(1 / 256)
    assert "65,536 texels/m²" in summary.as_text() and "3.9 mm" in summary.as_text()
    with pytest.raises(InspectError, match="No face is textured"):
        summarise_density(np.array([np.nan]))


def test_depth_counts() -> None:
    assert depth_counts([0, 1, 1, 2, 1]) == {0: 1, 1: 3, 2: 1}
