"""Tests for geotools.core.facemap."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from geotools.core.facemap import (
    FACEMAP_VERSION,
    FaceMap,
    build_face_map,
    load_face_map,
    save_face_map,
    topology_digest,
)
from geotools.core.gltf_writer import ExportError, PrimitiveData, SceneExport


def prim(triangles: int, obj: int, first_polygon: int) -> PrimitiveData:
    return PrimitiveData(
        material=None,
        positions=np.zeros((3, 3), np.float32),
        indices=np.tile(np.array([0, 1, 2], np.uint32), (triangles, 1)),
        source_object=np.full(triangles, obj, np.int32),
        source_polygon=(first_polygon + np.arange(triangles) // 2).astype(np.int32),
    )


def scene(*prims: PrimitiveData) -> SceneExport:
    return SceneExport(("Ground", "Wall", "Roof é"), (), (), tuple(prims))


SCENE = scene(prim(4, 0, 100), prim(1, 2, 7), prim(6, 1, 0))
TOPOLOGY = {"Wall": "b" * 64, "Ground": "a" * 64}


def test_rows_follow_primitives_then_index_rows() -> None:
    fm = build_face_map(SCENE, TOPOLOGY)
    assert fm.object_names == ("Ground", "Wall", "Roof é")
    np.testing.assert_array_equal(fm.primitive_offsets, [0, 4, 5, 11])
    assert fm.primitive_offsets.dtype == np.int64
    np.testing.assert_array_equal(fm.source_object, [0, 0, 0, 0, 2, 1, 1, 1, 1, 1, 1])
    np.testing.assert_array_equal(fm.source_polygon, [100, 100, 101, 101, 7, 0, 0, 1, 1, 2, 2])
    assert fm.source_object.dtype == fm.source_polygon.dtype == np.int32
    assert fm.topology == TOPOLOGY
    # The face map owns its arrays.
    assert not np.shares_memory(fm.source_polygon, SCENE.primitives[0].source_polygon)


def test_lookup() -> None:
    fm = build_face_map(SCENE, TOPOLOGY)
    assert fm.lookup(0, 0) == ("Ground", 100)
    assert fm.lookup(0, 3) == ("Ground", 101)
    assert fm.lookup(1, 0) == ("Roof é", 7)
    assert fm.lookup(2, 5) == ("Wall", 2)
    for primitive, triangle in [(-1, 0), (3, 0), (1, 1), (0, 4), (2, -1)]:
        with pytest.raises(IndexError):
            fm.lookup(primitive, triangle)


def test_lookup_many() -> None:
    fm = build_face_map(SCENE, TOPOLOGY)
    primitive = np.array([[2, 0], [1, 2]], np.uint32)
    triangle = np.array([[0, 2], [0, 5]], np.uint64)
    objects, polygons = fm.lookup_many(primitive, triangle)
    np.testing.assert_array_equal(objects, [[1, 0], [2, 1]])
    np.testing.assert_array_equal(polygons, [[0, 101], [7, 2]])
    empty_objects, empty_polygons = fm.lookup_many(np.array([], int), np.array([], int))
    assert empty_objects.shape == empty_polygons.shape == (0,)
    with pytest.raises(IndexError):
        fm.lookup_many(np.array([0, 1]), np.array([3, 1]))
    with pytest.raises(IndexError):
        fm.lookup_many(np.array([0, 3]), np.array([0, 0]))
    with pytest.raises(ValueError, match="same shape"):
        fm.lookup_many(np.array([0, 1]), np.array([0]))
    with pytest.raises(TypeError, match="integers"):
        fm.lookup_many(np.array([0.0]), np.array([0]))


def test_build_refuses_missing_provenance_and_empty_primitives() -> None:
    with pytest.raises(ExportError, match="provenance"):
        build_face_map(scene(dataclasses.replace(prim(2, 0, 0), source_object=None)), {})
    empty = dataclasses.replace(
        prim(1, 0, 0),
        indices=np.zeros((0, 3), np.uint32),
        source_object=np.zeros(0, np.int32),
        source_polygon=np.zeros(0, np.int32),
    )
    with pytest.raises(ExportError, match="no triangles"):
        build_face_map(scene(prim(1, 0, 0), empty), {})
    with pytest.raises(ExportError, match="out of range"):
        build_face_map(scene(prim(1, 3, 0)), {})
    with pytest.raises(ExportError, match="int32"):
        bad = dataclasses.replace(prim(2, 0, 0), source_polygon=np.zeros(2, np.int64))
        build_face_map(scene(bad), {})
    with pytest.raises(ExportError, match="no primitives"):
        build_face_map(scene(), {})


# ------------------------------------------------------------------------- topology


# A unit cube: 8 vertices, 6 quads.
CUBE_TOTALS = np.full(6, 4, np.int32)
CUBE_LOOPS = np.array(
    [0, 1, 3, 2, 2, 3, 7, 6, 6, 7, 5, 4, 4, 5, 1, 0, 2, 6, 4, 0, 7, 3, 1, 5], np.int32
)


def test_topology_digest_is_stable() -> None:
    digest = topology_digest(CUBE_TOTALS, CUBE_LOOPS, 8)
    # Pinned: a change here invalidates every saved face map's staleness check.
    assert digest == "f99bc4b46138db4aed0388566d3e400cb0c202b340cfb0e5ff0cb7e07db1c067"
    # Same values in another integer dtype or memory layout: same digest.
    assert topology_digest(CUBE_TOTALS.astype(np.int64), CUBE_LOOPS.astype(np.uint32), 8) == digest
    assert topology_digest(np.repeat(CUBE_TOTALS, 2)[::2], CUBE_LOOPS, np.int64(8)) == digest


def test_topology_digest_detects_topology_changes_only() -> None:
    digest = topology_digest(CUBE_TOTALS, CUBE_LOOPS, 8)
    rotated = CUBE_LOOPS.copy()
    rotated[:4] = np.roll(rotated[:4], 1)  # same faces, different corner order
    split = np.concatenate([[3, 3], CUBE_TOTALS[1:]]).astype(np.int32)
    split_loops = np.concatenate([[0, 1, 3, 0, 3, 2], CUBE_LOOPS[4:]]).astype(np.int32)
    changed = {
        topology_digest(CUBE_TOTALS, rotated, 8),
        topology_digest(CUBE_TOTALS, CUBE_LOOPS, 9),  # an extra loose vertex
        topology_digest(split, split_loops, 8),
        topology_digest(CUBE_TOTALS[:-1], CUBE_LOOPS[:-4], 8),
    }
    assert digest not in changed and len(changed) == 4
    # Counts are hashed too, so an extra empty polygon changes the digest.
    loops = np.array([0, 1, 2], np.int32)
    assert topology_digest(np.array([3], np.int32), loops, 3) != topology_digest(
        np.array([3, 0], np.int32), loops, 3
    )
    assert len(topology_digest(np.array([], np.int32), np.array([], np.int32), 0)) == 64


def test_topology_digest_rejects_inconsistent_input() -> None:
    with pytest.raises(ValueError, match="sum to"):
        topology_digest(CUBE_TOTALS, CUBE_LOOPS[:-1], 8)
    with pytest.raises(ValueError, match="outside vertex_count"):
        topology_digest(CUBE_TOTALS, CUBE_LOOPS, 7)
    with pytest.raises(ValueError, match="integers"):
        topology_digest(CUBE_TOTALS.astype(np.float32), CUBE_LOOPS, 8)
    with pytest.raises(ValueError, match="one-dimensional"):
        topology_digest(CUBE_TOTALS.reshape(2, 3), CUBE_LOOPS, 8)


# ------------------------------------------------------------------------ save/load


def test_save_load_round_trip(tmp_path: Path) -> None:
    fm = build_face_map(SCENE, TOPOLOGY)
    path = tmp_path / "staging" / "facemap.npz"
    save_face_map(fm, path)
    assert sorted(p.name for p in path.parent.iterdir()) == ["facemap.npz"]
    loaded = load_face_map(path)
    assert loaded.object_names == fm.object_names
    assert loaded.topology == fm.topology
    for name in ("primitive_offsets", "source_object", "source_polygon"):
        np.testing.assert_array_equal(getattr(loaded, name), getattr(fm, name))
        assert getattr(loaded, name).dtype == getattr(fm, name).dtype
    assert loaded.lookup(1, 0) == ("Roof é", 7)
    # No pickles: the file loads with allow_pickle=False.
    with np.load(path, allow_pickle=False) as data:
        assert int(data["version"]) == FACEMAP_VERSION

    empty_topology = dataclasses.replace(fm, topology={})
    save_face_map(empty_topology, path)
    assert load_face_map(path).topology == {}


def _write_npz(path: Path, **overrides) -> None:
    fm = build_face_map(SCENE, TOPOLOGY)
    arrays = {
        "version": np.asarray(FACEMAP_VERSION),
        "object_names": np.asarray(fm.object_names),
        "primitive_offsets": fm.primitive_offsets,
        "source_object": fm.source_object,
        "source_polygon": fm.source_polygon,
        "topology_names": np.asarray(["Ground"]),
        "topology_digests": np.asarray(["a" * 64]),
    }
    arrays.update(overrides)
    with open(path, "wb") as f:
        np.savez(f, **{k: v for k, v in arrays.items() if v is not None})


def test_load_refuses_unknown_version_and_bad_contents(tmp_path: Path) -> None:
    path = tmp_path / "facemap.npz"
    _write_npz(path)
    assert isinstance(load_face_map(path), FaceMap)
    _write_npz(path, version=np.asarray(FACEMAP_VERSION + 1))
    with pytest.raises(ValueError, match="version 2"):
        load_face_map(path)
    _write_npz(path, version=None)
    with pytest.raises(ValueError, match="no version"):
        load_face_map(path)
    _write_npz(path, source_polygon=None)
    with pytest.raises(ValueError, match="lacks source_polygon"):
        load_face_map(path)
    _write_npz(path, primitive_offsets=np.array([0, 4, 5, 12]))
    with pytest.raises(ValueError, match="inconsistent"):
        load_face_map(path)
    _write_npz(path, source_object=np.zeros(11, np.int64))
    with pytest.raises(ValueError, match="inconsistent"):
        load_face_map(path)
