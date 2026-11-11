"""GeoTIFF tags, georeference and the numpy pixel reader (core.geotiff)."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))

import _geotiff as gw

from geotools.core import geotiff as gt
from geotools.core.crs_match import DataCrs


def _write(tmp_path: Path, data: np.ndarray, spec: gw.GeoTiffSpec | None = None, **kw) -> Path:
    return gw.write_geotiff(tmp_path / "t.tif", data, spec, **kw)


# --- tags ---------------------------------------------------------------------------


def test_tags_of_a_tiled_deflate_float_dem(tmp_path: Path) -> None:
    data = gw.dem(37, 53)
    path = _write(tmp_path, data, compression="deflate", predictor=3, tile=(16, 32))
    info = gt.read_info(path)
    assert (info.width, info.height) == (53, 37)
    assert info.samples_per_pixel == 1
    assert info.bits_per_sample == 32 and info.sample_format == 3
    assert info.sample_type == "float32"
    assert info.compression == 8 and info.compression_name == "deflate"
    assert info.predictor == 3
    assert info.tiled and info.tile_size == (16, 32)
    assert len(info.chunk_offsets) == 4 * 2  # 4 tiles across, 2 down
    assert info.byteorder == "<" and not info.bigtiff
    assert info.band_layout == "1 band float32"


def test_tags_of_stripped_rgb(tmp_path: Path) -> None:
    path = _write(tmp_path, gw.rgb(20, 30), rows_per_strip=6)
    info = gt.read_info(path)
    assert info.samples_per_pixel == 3 and info.sample_type == "uint8"
    assert not info.tiled and info.rows_per_strip == 6
    assert len(info.chunk_offsets) == 4
    assert info.photometric == 2
    assert info.band_layout == "3 bands uint8, RGB"


@pytest.mark.parametrize("order", ["<", ">"])
def test_bigtiff_in_both_byte_orders(tmp_path: Path, order: str) -> None:
    data = gw.dem(19, 23, "int16")
    path = _write(tmp_path, data, gw.GeoTiffSpec(nodata="-9999"), bigtiff=True, byteorder=order)
    info = gt.read_info(path)
    assert info.bigtiff and info.byteorder == order
    assert info.nodata == -9999.0
    assert info.geotransform == (500_000.0, 1.0, 0.0, 7_000_000.0, 0.0, -1.0)
    np.testing.assert_array_equal(gt.read_band(info), data)


def test_not_a_tiff_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "x.tif"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(32))
    with pytest.raises(gt.GeoTiffError, match="not a TIFF"):
        gt.read_info(path)


def test_truncated_file_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, gw.dem(8, 8))
    path.write_bytes(path.read_bytes()[:-40])
    with pytest.raises(gt.GeoTiffError):
        gt.read_info(path)


# --- georeference ------------------------------------------------------------------


def test_geotransform_from_pixel_scale_and_tiepoint(tmp_path: Path) -> None:
    spec = gw.GeoTiffSpec(origin=(321_000.25, 6_543_210.75), pixel_size=(0.5, 0.25))
    info = gt.read_info(_write(tmp_path, gw.dem(10, 12), spec))
    assert info.geotransform == (321_000.25, 0.5, 0.0, 6_543_210.75, 0.0, -0.25)
    assert info.pixel_size == (0.5, -0.25)
    assert info.bounds() == (321_000.25, 6_543_210.75 - 2.5, 321_000.25 + 6.0, 6_543_210.75)
    np.testing.assert_array_equal(info.centres_x([0, 11]), [321_000.5, 321_006.0])
    np.testing.assert_array_equal(info.centres_y([0, 9]), [6_543_210.625, 6_543_208.375])
    assert not info.pixel_is_point


def test_pixel_is_point_shifts_by_half_a_pixel(tmp_path: Path) -> None:
    spec = gw.GeoTiffSpec(origin=(1000.0, 2000.0), pixel_size=(2.0, 2.0), pixel_is_point=True)
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), spec))
    assert info.pixel_is_point
    # Corner-based, as GDAL reports it ...
    assert info.geotransform == (999.0, 2.0, 0.0, 2001.0, 0.0, -2.0)
    # ... so the pixel centres are exactly the tiepoint grid.
    assert info.centres_x([0, 1])[0] == 1000.0 and info.centres_x([1])[0] == 1002.0
    assert info.centres_y([0])[0] == 2000.0 and info.centres_y([3])[0] == 1994.0


def test_model_transformation_north_up(tmp_path: Path) -> None:
    m = (0.5, 0.0, 0.0, 400_000.0, 0.0, -0.5, 0.0, 6_000_000.0, 0, 0, 0, 0, 0, 0, 0, 1)
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), gw.GeoTiffSpec(transformation=m)))
    assert info.geotransform == (400_000.0, 0.5, 0.0, 6_000_000.0, 0.0, -0.5)


def test_rotated_transform_is_refused(tmp_path: Path) -> None:
    c, s = np.cos(0.1), np.sin(0.1)
    m = (c, -s, 0.0, 400_000.0, s, c, 0.0, 6_000_000.0, 0, 0, 0, 0, 0, 0, 0, 1)
    path = _write(tmp_path, gw.dem(4, 4), gw.GeoTiffSpec(transformation=m))
    with pytest.raises(gt.GeoTiffError, match="rotated or sheared.*QGIS"):
        gt.read_info(path)


def test_ground_control_points_are_refused(tmp_path: Path) -> None:
    spec = gw.GeoTiffSpec(ground_control=[(10.0, 0.0, 0.0, 500_010.0, 7_000_000.0, 0.0)])
    with pytest.raises(gt.GeoTiffError, match="ground control points"):
        gt.read_info(_write(tmp_path, gw.dem(4, 4), spec))


def test_no_georeference_is_refused(tmp_path: Path) -> None:
    with pytest.raises(gt.GeoTiffError, match="no georeference"):
        gt.read_info(_write(tmp_path, gw.dem(4, 4), geo=False))


# --- CRS ----------------------------------------------------------------------------


def test_projected_crs_from_geokeys(tmp_path: Path) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), gw.GeoTiffSpec(epsg=28356)))
    assert info.crs == DataCrs("EPSG:28356", None, "geotiff-geokeys")
    assert not info.geographic


def test_geographic_crs_from_geokeys(tmp_path: Path) -> None:
    spec = gw.GeoTiffSpec(
        origin=(150.0, -27.0), pixel_size=(0.001, 0.001), epsg=4326, geographic=True
    )
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), spec))
    assert info.crs == DataCrs("EPSG:4326", None, "geotiff-geokeys")
    assert info.geographic


def test_user_defined_crs_is_unknown(tmp_path: Path) -> None:
    spec = gw.GeoTiffSpec(epsg=None, citation="My local grid")
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), spec))
    assert info.crs == DataCrs(None, None, "geotiff-geokeys")
    assert info.crs_citation == "My local grid"


def test_no_geokeys_means_no_crs(tmp_path: Path) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), gw.GeoTiffSpec(geokeys=False)))
    assert info.crs is None
    assert not info.pixel_is_point


@pytest.mark.parametrize(
    ("text", "value"),
    [("-9999", -9999.0), ("nan", None), ("-3.4028234663852886e+38", -3.4028234663852886e38)],
)
def test_gdal_nodata(tmp_path: Path, text: str, value: float | None) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(4, 4), gw.GeoTiffSpec(nodata=text)))
    assert info.nodata_text == text
    if value is None:
        assert np.isnan(info.nodata)
    else:
        assert info.nodata == pytest.approx(value, rel=1e-6)


# --- pixels -------------------------------------------------------------------------

_LAYOUTS = [
    {"compression": "none"},
    {"compression": "none", "rows_per_strip": 5},
    {"compression": "deflate", "rows_per_strip": 7},
    {"compression": "deflate", "tile": (16, 16)},
    {"compression": "deflate", "predictor": 2, "tile": (32, 16)},
    {"compression": "deflate", "predictor": 2, "rows_per_strip": 3, "byteorder": ">"},
    {"compression": "deflate", "bigtiff": True, "tile": (16, 16)},
]


@pytest.mark.parametrize("dtype", ["float32", "float64", "int16", "uint16", "int32", "uint8"])
@pytest.mark.parametrize("layout", _LAYOUTS, ids=lambda d: "-".join(f"{v}" for v in d.values()))
def test_read_band_is_exact(tmp_path: Path, dtype: str, layout: dict) -> None:
    data = gw.dem(37, 53, dtype)
    if dtype in ("int16", "int32"):
        data = (data.astype(np.int64) - 140).astype(dtype)
        data[0, 0] = np.iinfo(dtype).min
    info = gt.read_info(_write(tmp_path, data, **layout))
    band = gt.read_band(info, threads=2)
    assert band.dtype == np.dtype(dtype)
    np.testing.assert_array_equal(band, data)


@pytest.mark.parametrize("dtype", ["float32", "float64"])
@pytest.mark.parametrize("order", ["<", ">"])
def test_floating_point_predictor(tmp_path: Path, dtype: str, order: str) -> None:
    data = gw.dem(21, 34, dtype)
    data[2, 3] = np.nan
    info = gt.read_info(
        _write(tmp_path, data, compression="deflate", predictor=3, tile=(16, 16), byteorder=order)
    )
    np.testing.assert_array_equal(gt.read_band(info), data)


def test_read_every_band_of_rgb(tmp_path: Path) -> None:
    data = gw.rgb(20, 30)
    info = gt.read_info(_write(tmp_path, data, compression="deflate", tile=(16, 16)))
    for band in range(3):
        np.testing.assert_array_equal(gt.read_band(info, band), data[:, :, band])
    with pytest.raises(gt.GeoTiffError, match="no band 4"):
        gt.read_band(info, 3)


def test_lzw_is_not_decoded_by_numpy(tmp_path: Path) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(8, 8), compression="lzw"))
    assert gt.numpy_unsupported(info) == "LZW compression"
    with pytest.raises(gt.GeoTiffError, match="LZW"):
        gt.read_band(info)


# --- which reader -------------------------------------------------------------------


@pytest.mark.parametrize("dtype", ["float32", "int16", "uint16", "uint8"])
@pytest.mark.parametrize("compression", ["none", "deflate", "lzw"])
def test_blender_reads_its_faithful_types(tmp_path: Path, dtype: str, compression: str) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(8, 8, dtype), compression=compression))
    assert gt.dem_route(info) == "blender"


@pytest.mark.parametrize("dtype", ["float64", "int32", "uint32"])
def test_numpy_reads_the_others(tmp_path: Path, dtype: str) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(8, 8, dtype), compression="deflate"))
    assert gt.dem_route(info) == "numpy"


def test_lzw_float64_is_refused_with_a_fix(tmp_path: Path) -> None:
    info = gt.read_info(_write(tmp_path, gw.dem(8, 8, "float64"), compression="lzw"))
    with pytest.raises(gt.GeoTiffError, match="COMPRESS=DEFLATE"):
        gt.dem_route(info)


@pytest.mark.parametrize(
    ("dtype", "scale"), [("uint8", 255.0), ("uint16", 65535.0), ("int16", 32767.0)]
)
def test_denormalise_undoes_blenders_scaling(dtype: str, scale: float) -> None:
    info = np.iinfo(dtype)
    values = np.arange(info.min, info.max + 1, dtype=np.int64).astype(dtype)
    as_blender = (values.astype(np.float32) / np.float32(scale)).astype(np.float32)
    back = gt.denormalise(as_blender, dtype)
    assert back.dtype == np.dtype(dtype)
    np.testing.assert_array_equal(back, values)


def test_denormalise_keeps_float32() -> None:
    values = np.array([np.nan, -3.4028235e38, 1234.5678], np.float32)
    back = gt.denormalise(values, "float32")
    assert back.dtype == np.float32
    np.testing.assert_array_equal(back, values)
