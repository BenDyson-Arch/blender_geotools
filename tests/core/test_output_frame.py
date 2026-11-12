"""Tests for geotools.core.output_frame: the publish placement, inverted."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from geotools.core import deps
from geotools.core.gltf_writer import MaterialData, PrimitiveData, SceneExport, write_glb
from geotools.core.output_frame import (
    FrameError,
    SceneFrame,
    anchor_matrix,
    placement_from_record,
    scene_frame,
    source_bounds,
)
from geotools.core.placement import Placement, PlacementMode

pyproj = pytest.importorskip("pyproj")


def _real() -> bool:
    deps.reset_cache()
    return deps.probe_rusty_tiles().available


real = pytest.mark.skipif(not _real(), reason="needs rusty-tiles 0.4.1")

ANCHOR = Placement(
    mode=PlacementMode.ANCHOR,
    anchor=(153.02, -27.47, 10.0),
    heading_degrees=30.0,
    scene_offset=(5.0, -3.0, 1.0),
)
MGA56 = Placement(
    mode=PlacementMode.SOURCE_CRS,
    source_crs="EPSG:7856",
    source_offset=(502000.0, 6960000.0, 20.0),
    height_offset=40.0,
)


@pytest.mark.parametrize("placement", [Placement(), ANCHOR, MGA56])
def test_placement_round_trips_through_the_record(placement: Placement) -> None:
    record = placement.mesh_to_tileset_kwargs()
    back = placement_from_record({**record, "crs_epsg": None, "crs_operation": None})
    assert back.mode == placement.mode
    assert back.mesh_to_tileset_kwargs() == record
    if placement.mode is PlacementMode.ANCHOR:
        assert math.isclose(back.heading_degrees, 30.0, abs_tol=1e-12)
        assert back.scene_offset == (5.0, -3.0, 1.0)


def test_record_refusals() -> None:
    with pytest.raises(FrameError, match="tilted"):
        placement_from_record({"anchor": [0, 0, 0], "orientation_xyzw": [0.1, 0, 0, 0.99]})
    with pytest.raises(FrameError, match="axes"):
        placement_from_record({"source_crs": "EPSG:7856", "source_axes": "z-up"})
    with pytest.raises(FrameError, match="malformed source offset"):
        placement_from_record({"source_crs": "EPSG:7856", "source_offset": [1, 2]})
    with pytest.raises(FrameError, match="malformed placement"):
        scene_frame({"placement": "nope"})
    assert placement_from_record(None).mode is PlacementMode.LOCAL
    assert scene_frame({}).mode is PlacementMode.LOCAL


def test_local_applies_the_tile_transform() -> None:
    transform = np.eye(4)
    transform[:3, 3] = (10.0, 20.0, 30.0)
    frame = SceneFrame(Placement())
    out = frame.to_scene(np.array([[1.0, 2.0, 3.0]]), transform)
    np.testing.assert_array_equal(out, [[11.0, 22.0, 33.0]])
    assert frame.root_mismatch(transform) is None
    assert frame.centre_residual(transform, (0, 0, 0)) is None
    shifted = frame.with_correction(np.array([0.0, 0.0, -1.0]))
    np.testing.assert_array_equal(shifted.to_scene(np.zeros((1, 3)), np.eye(4)), [[0, 0, -1.0]])


def test_anchor_matrix_is_the_site_frame() -> None:
    """The same rigid frame analysis.site uses: offset, heading, then ENU at the anchor."""
    from geotools.analysis import site

    points = np.array([[0.0, 0.0, 0.0], [100.0, -50.0, 7.0], [-3.0, 2.0, -1.0]])
    ecef = points @ anchor_matrix(ANCHOR)[:3, :3].T + anchor_matrix(ANCHOR)[:3, 3]
    lon, lat, height = site._anchor_geodetic(ANCHOR, site._anchor_enu(ANCHOR, points))
    transformer = pyproj.Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
    x, y, z = transformer.transform(lon, lat, height)
    np.testing.assert_allclose(ecef, np.stack([x, y, z], axis=1), atol=1e-6)
    # And the inverse brings ECEF back to the scene.
    back = SceneFrame(ANCHOR).to_scene(points, anchor_matrix(ANCHOR))
    np.testing.assert_allclose(back, points, atol=1e-9)
    assert SceneFrame(ANCHOR).root_mismatch(anchor_matrix(ANCHOR)) == 0.0


def _enu(lon: float, lat: float, height: float) -> np.ndarray:
    from geotools.core.georef import ecef_from_lon_lat_height

    lo, la = math.radians(lon), math.radians(lat)
    matrix = np.eye(4)
    matrix[:3, 0] = (-math.sin(lo), math.cos(lo), 0.0)
    matrix[:3, 1] = (-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la))
    matrix[:3, 2] = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    matrix[:3, 3] = ecef_from_lon_lat_height(lon, lat, height)
    return matrix


def test_source_crs_inverse_of_a_pyproj_forward() -> None:
    """Scene points -> CRS -> WGS84 ECEF -> a local ENU frame, then back."""
    scene = np.array([[0.0, 0.0, 0.0], [250.0, -125.0, 12.5], [-400.0, 380.0, -3.0]])
    offset = np.asarray(MGA56.source_offset)
    # The same pyproj operation run forward, so this checks GeoTools' own arithmetic.
    crs = pyproj.CRS("EPSG:7856").to_3d()
    transformer = pyproj.Transformer.from_crs("EPSG:4978", crs, always_xy=True)
    x, y, z = transformer.transform(
        scene[:, 0] + offset[0],
        scene[:, 1] + offset[1],
        scene[:, 2] + offset[2] + 40.0,
        direction="INVERSE",
    )
    ecef = np.stack([x, y, z], axis=1)
    root = _enu(153.02, -27.48, 60.0)
    tile = (ecef - root[:3, 3]) @ root[:3, :3]  # ECEF -> the ENU tile frame
    back = SceneFrame(MGA56).to_scene(tile, root)
    np.testing.assert_allclose(back, scene, atol=1e-8)


def test_source_crs_refuses_points_outside_the_crs() -> None:
    root = _enu(63.0, 0.0, 0.0)  # 90 degrees from zone 56's meridian: no transverse Mercator
    with pytest.raises(FrameError, match="outside the recorded CRS"):
        SceneFrame(MGA56).to_scene(np.zeros((1, 3)), root)


def test_unknown_crs() -> None:
    bad = Placement(mode=PlacementMode.SOURCE_CRS, source_crs="EPSG:1")
    with pytest.raises(FrameError, match="does not know"):
        SceneFrame(bad).to_scene(np.zeros((1, 3)), np.eye(4))


def test_source_bounds_from_the_glb(tmp_path: Path) -> None:
    positions = np.array([[1.0, -2.0, 3.0], [4.0, 5.0, -6.0], [0.0, 0.0, 0.0]], np.float32)
    prim = PrimitiveData(
        material=0,
        positions=positions,
        indices=np.array([[0, 1, 2]], np.uint32),
        source_object=np.zeros(1, np.int32),
        source_polygon=np.zeros(1, np.int32),
    )
    write_glb(SceneExport(("A",), (MaterialData("m"),), (), (prim,)), tmp_path / "s.glb")
    lo, hi = source_bounds(tmp_path / "s.glb")
    assert lo == (0.0, -2.0, -6.0) and hi == (4.0, 5.0, 3.0)
    assert source_bounds(tmp_path / "missing.glb") is None
    (tmp_path / "junk.glb").write_bytes(b"junk" * 10)
    assert source_bounds(tmp_path / "junk.glb") is None


# --- against rusty-tiles ----------------------------------------------------------------------


def _publish(tmp_path: Path, placement: Placement):
    import rusty_tiles

    n = 20
    xs, ys = np.meshgrid(np.arange(n + 1, dtype=np.float32), np.arange(n + 1, dtype=np.float32))
    co = np.stack([xs.ravel() * 5, ys.ravel() * 5, np.cos(xs.ravel())], 1).astype(np.float32)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    tris = np.concatenate(
        [np.stack([a, a + 1, a + n + 2], 1), np.stack([a, a + n + 2, a + n + 1], 1)]
    )
    prim = PrimitiveData(
        material=0,
        positions=co,
        indices=tris.astype(np.uint32),
        source_object=np.zeros(len(tris), np.int32),
        source_polygon=np.arange(len(tris), dtype=np.int32),
    )
    write_glb(SceneExport(("G",), (MaterialData("m"),), (), (prim,)), tmp_path / "source.glb")
    kwargs = placement.mesh_to_tileset_kwargs()
    result = rusty_tiles.mesh_to_tileset(
        tmp_path / "source.glb", tmp_path / "site.3tz", profile="lossless", **kwargs
    )
    operation = (result.report.get("source_crs") or {}).get("datum_operation")
    return co, tris, {"placement": {**kwargs, "crs_operation": operation}}, result.report


@real
@pytest.mark.parametrize(
    "placement", [Placement(), ANCHOR, MGA56], ids=["local", "anchor", "mga56"]
)
def test_tiles_land_on_the_source(tmp_path: Path, placement: Placement) -> None:
    from geotools.core.output_tiles import decode_tile, list_tiles

    co, tris, record, report = _publish(tmp_path, placement)
    frame = scene_frame(record)
    tiles = list_tiles(tmp_path / "site.3tz")
    root = tiles[0].transform
    if placement.mode is PlacementMode.ANCHOR:
        assert frame.root_mismatch(root) < 1e-6
        np.testing.assert_allclose(
            np.asarray(report["root_transform"]).reshape(4, 4).T, anchor_matrix(ANCHOR), atol=1e-6
        )
    if placement.mode is PlacementMode.SOURCE_CRS:
        lo, hi = source_bounds(tmp_path / "source.glb")
        residual = frame.centre_residual(root, tuple((np.asarray(lo) + np.asarray(hi)) / 2))
        assert np.abs(residual).max() < 1e-6, residual  # pyproj and rusty-tiles agree
        assert frame.operation_note() is None
    for entry in tiles:
        if not entry.is_leaf:
            continue
        tile = decode_tile(tmp_path / "site.3tz", entry.uri)
        for prim in tile.primitives:
            got = frame.to_scene(prim.positions, tile.transform)[prim.indices]
            want = co.astype(np.float64)[tris[prim.source_triangle]]
            gaps = np.stack(
                [np.linalg.norm(np.roll(got, r, axis=1) - want, axis=2).max(1) for r in range(3)]
            )
            # Lossless: float32 in the tile frame, binary64 back (CRS: through pyproj).
            assert gaps.min(axis=0).max() < 1e-5
