"""Domain conversions and transfer reductions of the vertex-space bake."""

from __future__ import annotations

import math

import numpy as np
import pytest

from geotools.core import transfer as T
from geotools.core.layers import Aggregation, DataType, Domain, LayerSpec

# Two quads and a triangle: faces (0 1 4 3), (1 2 5 4), (2 6 5).
LOOP_START = np.array([0, 4, 8], np.int32)
LOOP_TOTAL = np.array([4, 4, 3], np.int32)
CORNER_VERT = np.array([0, 1, 4, 3, 1, 2, 5, 4, 2, 6, 5], np.int32)
# Edges: 0-1 0, 1-4 1, 4-3 2, 3-0 3, 1-2 4, 2-5 5, 5-4 6, 2-6 7, 6-5 8.
CORNER_EDGE = np.array([0, 1, 2, 3, 4, 5, 6, 1, 7, 8, 5], np.int32)
MESH = T.MeshArrays(LOOP_START, LOOP_TOTAL, CORNER_VERT, CORNER_EDGE)


def spec(data_type: DataType = DataType.FLOAT, **kwargs: object) -> LayerSpec:
    return LayerSpec("h", Domain.FACE, data_type, **kwargs)  # type: ignore[arg-type]


def ones(n: int) -> np.ndarray:
    return np.ones(n, bool)


# --- reduction kind and masks ----------------------------------------------------------


def test_reduction_kind() -> None:
    assert T.reduction_kind(spec(DataType.FLOAT)) == "mean"
    assert T.reduction_kind(spec(DataType.INT)) == "mean"
    assert T.reduction_kind(spec(DataType.INT, aggregation=Aggregation.MODE)) == "mode"
    assert T.reduction_kind(spec(DataType.BOOLEAN)) == "mode"
    assert T.reduction_kind(spec(DataType.CATEGORY, categories=("a",))) == "mode"


def test_valid_mask_float() -> None:
    values = np.array([1.0, np.nan, np.inf, -9999.0, -np.inf], np.float32)
    assert T.valid_mask(values).tolist() == [True, False, False, True, False]
    assert T.valid_mask(values, -9999.0).tolist() == [True, False, False, False, False]
    # A non-finite no-data adds nothing to the NaN rule.
    assert T.valid_mask(values, math.nan).tolist() == [True, False, False, True, False]


def test_valid_mask_compares_in_stored_precision() -> None:
    nodata = 0.1  # not exact in float32
    values = np.array([0.1, 0.2], np.float32)
    assert T.valid_mask(values, nodata).tolist() == [False, True]


def test_valid_mask_integers_and_booleans() -> None:
    ints = np.array([0, -1, 5], np.int32)
    assert T.valid_mask(ints).all()
    assert T.valid_mask(ints, -1.0).tolist() == [True, False, True]
    assert T.valid_mask(ints, -1.5).all()  # a fractional no-data matches no integer
    assert T.valid_mask(np.array([True, False]), 0.0).all()


# --- domain conversions -----------------------------------------------------------------


def test_point_edge_face_to_corner() -> None:
    points = np.arange(7, dtype=np.float64) * 10
    assert T.point_to_corner(points, CORNER_VERT).tolist() == (CORNER_VERT * 10).tolist()
    edges = np.arange(9, dtype=np.float64)
    assert T.gather(edges, CORNER_EDGE).tolist() == CORNER_EDGE.tolist()
    faces = np.array([1.0, 2.0, 3.0])
    assert T.face_to_corner(faces, LOOP_TOTAL).tolist() == [1.0] * 4 + [2.0] * 4 + [3.0] * 3
    assert T.corner_to_face_ids(LOOP_TOTAL).tolist() == [0] * 4 + [1] * 4 + [2] * 3


def test_corner_to_face_mean() -> None:
    corners = np.arange(11, dtype=np.float64)
    values, valid = T.corner_to_face(corners, ones(11), LOOP_START, LOOP_TOTAL)
    assert values.tolist() == [1.5, 5.5, 9.0]
    assert valid.all()


def test_corner_to_face_skips_invalid_corners() -> None:
    corners = np.arange(11, dtype=np.float64)
    valid = ones(11)
    valid[[0, 1]] = False  # face 0 keeps corners 2 and 3
    valid[8:] = False  # face 2 has none left
    values, face_valid = T.corner_to_face(corners, valid, LOOP_START, LOOP_TOTAL)
    assert values[:2].tolist() == [2.5, 5.5]
    assert face_valid.tolist() == [True, True, False]


def test_corner_to_face_nan_does_not_leak() -> None:
    corners = np.array([np.nan, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3], np.float64)
    values, valid = T.corner_to_face(corners, T.valid_mask(corners), LOOP_START, LOOP_TOTAL)
    assert values.tolist() == [1.0, 2.0, 3.0]
    assert valid.all()


def test_corner_to_face_mode() -> None:
    corners = np.array([2, 2, 1, 1, 7, 7, 7, 0, 4, 5, 6], np.float64)
    values, valid = T.corner_to_face(corners, ones(11), LOOP_START, LOOP_TOTAL, "mode")
    # Face 0 ties 1 and 2: the smaller wins. Face 2 ties three ways.
    assert values.tolist() == [1.0, 7.0, 4.0]
    assert valid.all()


def test_corner_to_face_checks_sizes() -> None:
    with pytest.raises(ValueError, match="corner"):
        T.corner_to_face(np.zeros(10), ones(10), LOOP_START, LOOP_TOTAL)
    values, valid = T.corner_to_face(np.zeros(0), np.zeros(0, bool), [], [])
    assert values.size == 0 and valid.size == 0


def test_to_faces_and_to_corners_from_every_domain() -> None:
    points = np.arange(7, dtype=np.float64)
    faces, _ = T.to_faces(points, ones(7), "POINT", MESH)
    assert faces.tolist() == [2.0, 3.0, 13 / 3]
    corners, _ = T.to_corners(points, ones(7), "POINT", MESH)
    assert corners.tolist() == CORNER_VERT.tolist()

    edge_values = np.arange(9, dtype=np.float64)
    faces, _ = T.to_faces(edge_values, ones(9), "EDGE", MESH)
    assert faces.tolist() == [1.5, (4 + 5 + 6 + 1) / 4, (7 + 8 + 5) / 3]

    face_values = np.array([1.0, 2.0, 3.0])
    same, _ = T.to_faces(face_values, ones(3), "FACE", MESH)
    assert same.tolist() == face_values.tolist()
    corners, _ = T.to_corners(face_values, ones(3), "FACE", MESH)
    assert corners.tolist() == [1.0] * 4 + [2.0] * 4 + [3.0] * 3

    with pytest.raises(ValueError, match="INSTANCE"):
        T.to_faces(face_values, ones(3), "INSTANCE", MESH)
    no_edges = T.MeshArrays(LOOP_START, LOOP_TOTAL, CORNER_VERT)
    with pytest.raises(ValueError, match="corner_edge"):
        T.to_corners(edge_values, ones(9), "EDGE", no_edges)


def test_point_to_face_mode_for_categories() -> None:
    points = np.array([1, 1, 2, 2, 1, 2, 2], np.float64)
    faces, _ = T.to_faces(points, ones(7), "POINT", MESH, "mode")
    # Face 0: verts 0 1 4 3 -> 1 1 1 2; face 1: 1 2 2 1 -> tie, 1; face 2: 2 2 2.
    assert faces.tolist() == [1.0, 1.0, 2.0]


# --- segment mode -------------------------------------------------------------------------


def test_segment_mode_weights_and_empty_segments() -> None:
    ids = np.array([0, 0, 0, 2, 2, -1])
    values = np.array([5, 6, 6, 1, 3, 9], np.float64)
    weights = np.array([10.0, 1.0, 1.0, 1.0, 3.0, 100.0])
    out, found = T.segment_mode(ids, values, ones(6), 4, weights)
    assert out.tolist()[0] == 5.0 and out.tolist()[2] == 3.0
    assert found.tolist() == [True, False, True, False]
    out, found = T.segment_mode(ids, values, ones(6), 4)  # counts
    assert out[0] == 6.0 and out[2] == 1.0  # 2 x 6 beats 1 x 5; 1 and 3 tie
    out, found = T.segment_mode(ids[:0], values[:0], ones(0), 2)
    assert not found.any()


# --- AREA_MEAN ---------------------------------------------------------------------------------


def test_area_reduce_mean() -> None:
    index = np.array([0, 0, 1, 1, 1])
    values = np.array([1.0, 3.0, 10.0, 20.0, 30.0])
    area = np.array([1.0, 3.0, 1.0, 1.0, 2.0])
    out, valid, received = T.area_reduce(index, values, ones(5), area, 3)
    assert out[0] == pytest.approx((1 + 9) / 4)
    assert out[1] == pytest.approx((10 + 20 + 60) / 4)
    assert valid.tolist() == [True, True, False]
    assert received.tolist() == [True, True, False]


def test_area_reduce_matches_bincount_formula() -> None:
    rng = np.random.default_rng(1)
    index = rng.integers(0, 50, 2000)
    values = rng.normal(size=2000)
    area = rng.uniform(0.1, 2.0, 2000)
    out, valid, received = T.area_reduce(index, values, ones(2000), area, 60)
    expected = np.bincount(index, values * area, 60)[:50] / np.bincount(index, area, 60)[:50]
    np.testing.assert_allclose(out[:50], expected)
    assert received[:50].all() and not received[50:].any()
    assert (valid == received).all()


def test_area_reduce_nodata_and_nan() -> None:
    index = np.array([0, 0, 1, 2])
    values = np.array([np.nan, 4.0, -9999.0, 2.0])
    valid = T.valid_mask(values, -9999.0)
    out, ok, received = T.area_reduce(index, values, valid, np.ones(4), 4)
    assert out[0] == 4.0  # the NaN face is skipped, not averaged in
    assert ok.tolist() == [True, False, True, False]  # face 1: reached, but only by no-data
    assert received.tolist() == [True, True, True, False]  # face 3 needs the fallback


def test_area_reduce_zero_area_uses_plain_mean() -> None:
    index = np.array([0, 0, 1, 1])
    values = np.array([1.0, 3.0, 5.0, 7.0])
    area = np.array([0.0, 0.0, 0.0, np.nan])
    out, ok, _ = T.area_reduce(index, values, ones(4), area, 2)
    assert out.tolist() == [2.0, 6.0]
    assert ok.all()


def test_area_reduce_mode() -> None:
    index = np.array([0, 0, 0, 1, 1])
    values = np.array([1, 2, 2, 3, 4], np.float64)
    area = np.array([5.0, 1.0, 1.0, 0.0, 0.0])
    out, ok, received = T.area_reduce(index, values, ones(5), area, 2, "mode")
    assert out.tolist() == [1.0, 3.0]  # largest area wins; zero area falls back to counts
    assert ok.all() and received.all()


def test_area_reduce_checks_input() -> None:
    with pytest.raises(ValueError, match="shape"):
        T.area_reduce(np.zeros(2, int), np.zeros(3), ones(3), np.ones(3), 1)
    with pytest.raises(ValueError, match="outside"):
        T.area_reduce(np.array([3]), np.zeros(1), ones(1), np.ones(1), 2)
    _out, ok, received = T.area_reduce(np.array([-1]), np.ones(1), ones(1), np.ones(1), 1)
    assert not received.any() and not ok.any()


# --- raycast and nearest -------------------------------------------------------------------


def test_choose_nearer_hit() -> None:
    hit_a = np.array([True, True, False, False, True])
    dist_a = np.array([1.0, 3.0, 0.0, 0.0, 2.0])
    hit_b = np.array([False, True, True, False, True])
    dist_b = np.array([0.5, 1.0, 4.0, 0.0, 2.0])
    choice = T.choose_nearer_hit(hit_a, dist_a, hit_b, dist_b)
    assert choice.tolist() == [0, 1, 1, -1, 0]


def test_merge_interpolated() -> None:
    face_values = np.array([10.0, 20.0, -9999.0])
    face_valid = np.array([True, True, False])
    index = np.array([0, 1, 2, -1, 1])
    interpolated = np.array([11.0, np.nan, 5.0, 7.0, 21.0])
    values, valid = T.merge_interpolated(interpolated, face_values, face_valid, index)
    # NaN falls back to the face value; a miss (-1) is invalid whatever was sampled.
    assert values.tolist()[:3] == [11.0, 20.0, 5.0]
    assert values[4] == 21.0
    assert valid.tolist() == [True, True, True, False, True]
    values, valid = T.merge_interpolated(None, face_values, face_valid, index)
    assert values.tolist()[:3] == [10.0, 20.0, -9999.0]
    assert valid.tolist() == [True, True, False, False, True]
    values, valid = T.merge_interpolated(None, np.zeros(0), np.zeros(0, bool), index)
    assert not valid.any()


# --- storing ------------------------------------------------------------------------------------


def test_to_layer_values_float_fill() -> None:
    values = np.array([1.5, 2.5, np.nan])
    valid = np.array([True, False, True])
    out = T.to_layer_values(values, valid, spec())
    assert out.dtype == np.float32
    assert out[0] == 1.5 and math.isnan(out[1]) and math.isnan(out[2])
    out = T.to_layer_values(values, valid, spec(nodata=-9999.0))
    assert out.tolist() == [1.5, -9999.0, -9999.0]


def test_to_layer_values_integers_and_booleans() -> None:
    values = np.array([1.4, 1.6, -2.5, 1e12])
    valid = np.array([True, True, True, True])
    out = T.to_layer_values(values, valid, spec(DataType.INT))
    assert out.dtype == np.int32
    assert out.tolist() == [1, 2, -2, 2**31 - 1]
    out = T.to_layer_values(values, ~valid, spec(DataType.INT, nodata=-1.0))
    assert out.tolist() == [-1] * 4
    out = T.to_layer_values(np.array([0.2, 0.5, 1.0]), ones(3), spec(DataType.BOOLEAN))
    assert out.dtype == np.bool_ and out.tolist() == [False, True, True]
    out = T.to_layer_values(np.array([1.0]), np.zeros(1, bool), spec(DataType.BOOLEAN))
    assert out.tolist() == [False]


def test_transfer_counts() -> None:
    counts = T.TransferCounts()
    counts.add("area_mean", np.array([True, True, False]), np.array([True, False, True]))
    counts.add("fallback", np.array([False, False, True]), np.array([True, True, True]))
    assert counts.as_dict() == {
        "copied": 0,
        "area_mean": 1,
        "raycast": 0,
        "nearest": 0,
        "fallback": 1,
        "nodata": 1,
    }
    assert counts.total == 3
    assert counts.summary() == "3 faces: 1 area mean, 1 fallback, 1 no-data"
    other = T.TransferCounts()
    other.add("copied", ones(2), ones(2))
    counts.merge(other)
    assert counts.counts["copied"] == 2 and counts.total == 5
    with pytest.raises(ValueError, match="path"):
        counts.add("guess", ones(1), ones(1))
