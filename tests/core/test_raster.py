"""DEM grid, orthophoto footprint and CRS checks (core.raster), and core.crs_match."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))

import _geotiff as gw

from geotools.core import crs_match, crs_presets, fingerprint, raster
from geotools.core import geotiff as gt
from geotools.core.crs_match import DataCrs
from geotools.core.placement import Placement, PlacementMode

ORIGIN = (512_345.0, 6_961_789.0)
OFFSET = (512_000.0, 6_961_000.0, 100.0)


def _info(tmp_path: Path, data: np.ndarray, **spec) -> gt.GeoTiffInfo:
    spec.setdefault("origin", ORIGIN)
    spec.setdefault("pixel_size", (2.0, 2.0))
    return gt.read_info(gw.write_geotiff(tmp_path / "d.tif", data, gw.GeoTiffSpec(**spec)))


def _expected(info: gt.GeoTiffInfo, z: np.ndarray, step: int = 1) -> np.ndarray:
    """Vertex positions worked out independently: centres in binary64, shift, float32."""
    rows = np.arange(0, info.height, step)
    cols = np.arange(0, info.width, step)
    x0, dx, _, y0, _, dy = info.geotransform
    xx, yy = np.meshgrid(x0 + (cols + 0.5) * dx, y0 + (rows + 0.5) * dy)
    pts = np.stack([xx - OFFSET[0], yy - OFFSET[1], z[::step, ::step] - OFFSET[2]], axis=-1)
    return pts.reshape(-1, 3).astype(np.float32)


def _normals_z(grid: raster.GridMesh) -> np.ndarray:
    quad = grid.positions[grid.corners.reshape(-1, 4)].astype(np.float64)
    a, b, c = quad[:, 0], quad[:, 1], quad[:, 2]
    return np.cross(b - a, c - a)[:, 2]


# --- DEM grid -----------------------------------------------------------------------


def test_dem_positions_in_the_project_frame(tmp_path: Path) -> None:
    z = gw.dem(6, 9)
    info = _info(tmp_path, z)
    grid = raster.dem_grid(info, z, OFFSET)
    assert grid.grid_shape == (6, 9)
    assert grid.vertex_count == 54 and grid.face_count == 5 * 8
    assert grid.positions.dtype == np.float32
    np.testing.assert_array_equal(grid.positions, _expected(info, z.astype(np.float64)))
    # First vertex: the top-left pixel's centre.
    assert tuple(grid.positions[0][:2]) == (345.0 + 1.0, 789.0 - 1.0)
    assert grid.spacing == (2.0, 2.0)
    assert (_normals_z(grid) > 0).all()


def test_dem_pixel_is_point(tmp_path: Path) -> None:
    z = gw.dem(4, 5)
    info = _info(tmp_path, z, pixel_is_point=True)
    grid = raster.dem_grid(info, z, OFFSET)
    # The tiepoint names the top-left pixel centre: that is the first vertex.
    assert tuple(grid.positions[0][:2]) == (345.0, 789.0)
    np.testing.assert_array_equal(grid.positions, _expected(info, z.astype(np.float64)))


def test_dem_integer_heights(tmp_path: Path) -> None:
    z = gw.dem(5, 5, "int16")
    info = _info(tmp_path, z)
    grid = raster.dem_grid(info, z, OFFSET)
    np.testing.assert_array_equal(grid.positions[:, 2], (z.astype(np.float64) - 100.0).ravel())


def test_south_up_raster_keeps_normals_up(tmp_path: Path) -> None:
    z = gw.dem(4, 4)
    m = (1.0, 0.0, 0.0, ORIGIN[0], 0.0, 1.0, 0.0, ORIGIN[1], 0, 0, 0, 0, 0, 0, 0, 1)
    info = gt.read_info(gw.write_geotiff(tmp_path / "s.tif", z, gw.GeoTiffSpec(transformation=m)))
    assert info.pixel_size == (1.0, 1.0)
    grid = raster.dem_grid(info, z, OFFSET)
    assert (_normals_z(grid) > 0).all()
    quad = raster.footprint(info, OFFSET)
    a, b, c = quad.positions[:3].astype(np.float64)
    assert np.cross(b - a, c - a)[2] > 0


def test_nodata_faces_and_vertices_are_removed(tmp_path: Path) -> None:
    z = gw.dem(5, 6)
    z[2, 3] = -9999.0  # an inner vertex: its four faces go
    z[0, 0] = -9999.0  # a corner: one face goes, and the vertex
    z[4, 5] = np.nan  # non-finite counts as nodata too
    info = _info(tmp_path, z, nodata="-9999")
    grid = raster.dem_grid(info, z, OFFSET)
    assert grid.nodata_vertices == 3
    assert grid.face_count == 4 * 5 - 4 - 1 - 1
    assert grid.vertex_count == 30 - 3
    assert np.isfinite(grid.positions).all()
    assert (grid.positions[:, 2] > -9000).all()
    assert grid.corners.max() == grid.vertex_count - 1
    assert (_normals_z(grid) > 0).all()
    # Every remaining vertex is one of the expected positions.
    expected = _expected(info, z.astype(np.float64))
    keep = np.isfinite(expected[:, 2]) & (z.ravel() != -9999.0)
    np.testing.assert_array_equal(grid.positions, expected[keep])


def test_integer_nodata(tmp_path: Path) -> None:
    z = gw.dem(4, 4, "int16")
    z[1, 1] = -32768
    info = _info(tmp_path, z, nodata="-32768")
    grid = raster.dem_grid(info, z, OFFSET)
    assert grid.nodata_vertices == 1 and grid.face_count == 9 - 4


def test_all_nodata_is_refused(tmp_path: Path) -> None:
    z = np.full((4, 4), -9999.0, np.float32)
    info = _info(tmp_path, z, nodata="-9999")
    with pytest.raises(raster.RasterError, match="no 2 x 2 block"):
        raster.dem_grid(info, z, OFFSET)


@pytest.mark.parametrize("step", [2, 3, 4])
def test_decimation(tmp_path: Path, step: int) -> None:
    z = gw.dem(13, 17)
    info = _info(tmp_path, z)
    grid = raster.dem_grid(info, z, OFFSET, step)
    rows, cols = -(-13 // step), -(-17 // step)
    assert grid.grid_shape == (rows, cols) == raster.decimated_shape(info, step)
    assert grid.spacing == (2.0 * step, 2.0 * step) == raster.spacing(info, step)
    assert grid.face_count == (rows - 1) * (cols - 1)
    np.testing.assert_array_equal(grid.positions, _expected(info, z.astype(np.float64), step))


def test_decimation_too_coarse_is_refused(tmp_path: Path) -> None:
    z = gw.dem(4, 4)
    with pytest.raises(raster.RasterError, match="at least 2 x 2"):
        raster.dem_grid(_info(tmp_path, z), z, OFFSET, 4)


def test_uvs_span_the_raster_extent(tmp_path: Path) -> None:
    z = gw.dem(4, 8)
    info = _info(tmp_path, z)
    grid = raster.dem_grid(info, z, OFFSET)
    # Map each corner's position to the footprint: that is its UV.
    xmin, ymin, xmax, ymax = info.bounds()
    pos = grid.positions[grid.corners].astype(np.float64)
    u = (pos[:, 0] + OFFSET[0] - xmin) / (xmax - xmin)
    v = (pos[:, 1] + OFFSET[1] - ymin) / (ymax - ymin)
    np.testing.assert_allclose(grid.uvs[:, 0], u, atol=1e-6)
    np.testing.assert_allclose(grid.uvs[:, 1], v, atol=1e-6)


# --- orthophoto footprint -------------------------------------------------------------


def test_footprint_is_the_pixel_edges(tmp_path: Path) -> None:
    info = _info(tmp_path, gw.rgb(30, 40), pixel_size=(0.25, 0.5))
    quad = raster.footprint(info, OFFSET)
    assert quad.size == (10.0, 15.0)
    expected = [(345.0, 789.0), (345.0, 774.0), (355.0, 774.0), (355.0, 789.0)]
    np.testing.assert_array_equal(quad.positions[:, :2], np.array(expected, np.float32))
    assert (quad.positions[:, 2] == 0.0).all()
    np.testing.assert_array_equal(quad.uvs, [(0, 1), (0, 0), (1, 0), (1, 1)])


# --- CRS ------------------------------------------------------------------------------

MGA56 = crs_presets.get("MGA2020_56")


def _project(crs: str, offset=OFFSET) -> Placement:
    return Placement(mode=PlacementMode.SOURCE_CRS, source_crs=crs, source_offset=offset)


def test_project_crs_from_a_preset() -> None:
    project = crs_match.project_crs(_project(MGA56.definition))
    assert project.epsg == "EPSG:7856"
    assert project.definition == MGA56.definition
    assert crs_match.project_crs(Placement()) is None


def test_matches() -> None:
    project = crs_match.project_crs(_project("epsg:7856"))
    assert project.epsg == "EPSG:7856"
    assert crs_match.matches(project, DataCrs("EPSG:7856", None, "geotiff-geokeys")) is True
    assert crs_match.matches(project, DataCrs("EPSG:28356", None, "geotiff-geokeys")) is False
    assert crs_match.matches(project, DataCrs(None, None, "geotiff-geokeys")) is None
    assert crs_match.matches(None, DataCrs("EPSG:7856", None, "user")) is None
    custom = crs_match.project_crs(_project("+proj=tmerc +lon_0=151"))
    assert custom.epsg is None
    assert crs_match.matches(custom, DataCrs(None, "+proj=tmerc  +lon_0=151", "user")) is True
    assert crs_match.matches(custom, DataCrs("EPSG:7856", None, "user")) is None


def test_reproject_is_not_available() -> None:
    data = DataCrs("EPSG:4326", None, "rfc7946-default")
    with pytest.raises(crs_match.ReprojectionUnavailable, match="QGIS"):
        crs_match.reproject(np.zeros((1, 3)), data, data)


def test_check_crs_match_returns_the_offset() -> None:
    check = raster.check_crs(
        _project(MGA56.definition), DataCrs("EPSG:7856", None, "geotiff-geokeys"), "d.tif"
    )
    assert check.offset == OFFSET and check.warnings == ()


def test_check_crs_mismatch_is_refused() -> None:
    with pytest.raises(raster.RasterError, match=r"EPSG:4326.*EPSG:7856.*QGIS"):
        raster.check_crs(
            _project(MGA56.definition), DataCrs("EPSG:4326", None, "geotiff-geokeys"), "d.tif"
        )


@pytest.mark.parametrize("data", [None, DataCrs(None, None, "geotiff-geokeys")])
def test_check_crs_unknown_needs_confirming(data: DataCrs | None) -> None:
    with pytest.raises(raster.RasterError, match="Data is in the project CRS"):
        raster.check_crs(_project("EPSG:7856"), data, "d.tif")
    check = raster.check_crs(_project("EPSG:7856"), data, "d.tif", confirmed=True)
    assert check.offset == OFFSET and len(check.warnings) == 1


def test_check_crs_confirming_does_not_override_a_mismatch() -> None:
    with pytest.raises(raster.RasterError, match="does not reproject"):
        raster.check_crs(
            _project("EPSG:7856"),
            DataCrs("EPSG:32756", None, "geotiff-geokeys"),
            "d.tif",
            confirmed=True,
        )


@pytest.mark.parametrize("mode", [PlacementMode.LOCAL, PlacementMode.ANCHOR])
def test_check_crs_local_and_anchor_warn_and_do_not_shift(mode: PlacementMode) -> None:
    check = raster.check_crs(
        Placement(mode=mode), DataCrs("EPSG:7856", None, "geotiff-geokeys"), "d.tif"
    )
    assert check.offset == (0.0, 0.0, 0.0)
    assert len(check.warnings) == 1 and "local metres" in check.warnings[0]


def test_check_crs_needs_a_project_crs() -> None:
    with pytest.raises(raster.RasterError, match="project CRS first"):
        raster.check_crs(_project(""), DataCrs("EPSG:7856", None, "user"), "d.tif")


# --- fingerprints ---------------------------------------------------------------------


def test_fingerprint_detects_changes(tmp_path: Path) -> None:
    path = tmp_path / "f.bin"
    path.write_bytes(b"one")
    stored = fingerprint.to_text(fingerprint.fingerprint(path))
    assert fingerprint.from_text(stored)["size"] == 3
    assert fingerprint.changed(path, stored) is False
    st = path.stat()
    path.write_bytes(b"two")  # same size, same content length
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert fingerprint.changed(path, stored) is True
    # Touched but identical contents: the hash says unchanged.
    path.write_bytes(b"one")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000))
    assert fingerprint.changed(path, stored) is False
    path.unlink()
    assert fingerprint.changed(path, stored) is None
