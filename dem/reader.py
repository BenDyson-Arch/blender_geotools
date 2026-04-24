"""
dem/reader.py — DEM reading and terrain mesh construction.

Three reading backends in priority order:
  1. GDAL     — handles all GeoTIFF variants; reads CRS natively
  2. rasterio — cleaner API, same capability as GDAL
  3. struct   — pure-Python fallback; single-band, uncompressed GeoTIFF only
               (float32, int16, uint16 sample formats; stripped or tiled layout)

All backends return a ``DEMData`` dataclass.  The mesh builder
``build_terrain_mesh`` accepts a ``DEMData`` and produces a ``bpy.types.Mesh``
ready to attach to a new object.  CRS and georeferencing are handled by the
caller (see ``operators.py``).
"""

from __future__ import annotations

import logging
import struct as _struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class DEMData:
    """Elevation grid and spatial metadata read from a DEM file."""

    elevations: list          # flat list of floats, row-major, length = cols * rows
    cols: int                 # number of columns (x)
    rows: int                 # number of rows (y)
    x_origin: float           # projected X of the top-left pixel corner
    y_origin: float           # projected Y of the top-left pixel corner
    pixel_width: float        # cell size in X (metres or degrees, matches CRS)
    pixel_height: float       # cell size in Y — negative in north-up rasters
    nodata: Optional[float]   # nodata sentinel value, or None
    srid: Optional[str]       # CRS as "AUTH:CODE" string, or None


# ---------------------------------------------------------------------------
# Nodata handling
# ---------------------------------------------------------------------------

def _clean(value: float, nodata: Optional[float], fill: float = 0.0) -> float:
    """Replace nodata values with *fill*."""
    if nodata is not None and value == nodata:
        return fill
    return value


# ---------------------------------------------------------------------------
# Backend 1: GDAL
# ---------------------------------------------------------------------------

def _read_gdal(path: Path, band: int = 1) -> DEMData:
    from osgeo import gdal, osr  # noqa: PLC0415

    ds = gdal.Open(str(path), gdal.GA_ReadOnly)
    if ds is None:
        raise ValueError(f"GDAL could not open: {path}")

    gt = ds.GetGeoTransform()          # (x_origin, px_w, rot_x, y_origin, rot_y, px_h)
    x_origin    = gt[0]
    y_origin    = gt[3]
    pixel_width  = gt[1]
    pixel_height = gt[5]               # negative for north-up

    cols = ds.RasterXSize
    rows = ds.RasterYSize

    # CRS
    srid = None
    wkt = ds.GetProjection()
    if wkt:
        sref = osr.SpatialReference()
        sref.ImportFromWkt(wkt)
        auth = sref.GetAuthorityName(None)
        code = sref.GetAuthorityCode(None)
        if auth and code:
            srid = f"{auth}:{code}"

    rb = ds.GetRasterBand(band)
    nodata = rb.GetNoDataValue()

    raw = rb.ReadAsArray()             # numpy array (rows, cols)
    elevations = [
        _clean(float(raw[r, c]), nodata)
        for r in range(rows)
        for c in range(cols)
    ]

    ds = None  # close file handle
    return DEMData(
        elevations=elevations,
        cols=cols, rows=rows,
        x_origin=x_origin, y_origin=y_origin,
        pixel_width=pixel_width, pixel_height=pixel_height,
        nodata=nodata, srid=srid,
    )


# ---------------------------------------------------------------------------
# Backend 2: rasterio
# ---------------------------------------------------------------------------

def _read_rasterio(path: Path, band: int = 1) -> DEMData:
    import rasterio  # noqa: PLC0415

    with rasterio.open(str(path)) as ds:
        tf = ds.transform
        x_origin    = tf.c
        y_origin    = tf.f
        pixel_width  = tf.a
        pixel_height = tf.e   # negative for north-up

        cols = ds.width
        rows = ds.height

        srid = None
        if ds.crs:
            srid = ds.crs.to_authority()          # returns ("EPSG", "4326") or None
            if srid:
                srid = f"{srid[0]}:{srid[1]}"

        nodata = ds.nodata
        raw = ds.read(band)   # numpy array (rows, cols)

    elevations = [
        _clean(float(raw[r, c]), nodata)
        for r in range(rows)
        for c in range(cols)
    ]

    return DEMData(
        elevations=elevations,
        cols=cols, rows=rows,
        x_origin=x_origin, y_origin=y_origin,
        pixel_width=pixel_width, pixel_height=pixel_height,
        nodata=nodata, srid=srid,
    )


# ---------------------------------------------------------------------------
# Backend 3: pure-Python struct fallback
# ---------------------------------------------------------------------------

# Minimal TIFF tag constants
_TIFF_LITTLE = b"II"
_TIFF_BIG    = b"MM"

_TAG_IMAGE_WIDTH       = 256
_TAG_IMAGE_LENGTH      = 257
_TAG_BITS_PER_SAMPLE   = 258
_TAG_SAMPLE_FORMAT     = 339   # 1=uint, 2=int, 3=ieee float
_TAG_STRIP_OFFSETS     = 273
_TAG_ROWS_PER_STRIP    = 278
_TAG_STRIP_BYTE_COUNTS = 279
_TAG_TILE_WIDTH        = 322
_TAG_TILE_LENGTH       = 323
_TAG_TILE_OFFSETS      = 324
_TAG_TILE_BYTE_COUNTS  = 325
_TAG_MODEL_PIXEL_SCALE = 33550   # GeoTIFF: (scaleX, scaleY, scaleZ)
_TAG_MODEL_TIE_POINT   = 33922   # GeoTIFF: (i,j,k, x,y,z) tiepoint(s)


def _read_struct(path: Path) -> DEMData:
    """Minimal GeoTIFF reader — single-band float32/int16/uint16, no compression."""

    data = path.read_bytes()

    # Byte order
    if data[:2] == _TIFF_LITTLE:
        endian = "<"
    elif data[:2] == _TIFF_BIG:
        endian = ">"
    else:
        raise ValueError(f"{path.name}: not a TIFF file")

    magic = _struct.unpack_from(f"{endian}H", data, 2)[0]
    if magic != 42:
        raise ValueError(f"{path.name}: not a TIFF file (magic={magic})")

    ifd_offset = _struct.unpack_from(f"{endian}I", data, 4)[0]

    def read_tag_values(offset, count, field_type, endian):
        fmt_map = {1: "B", 2: "s", 3: "H", 4: "I", 5: "II",
                   11: "f", 12: "d", 16: "Q"}
        fmt = fmt_map.get(field_type, "I")
        if field_type == 2:
            raw = data[offset: offset + count]
            return raw.rstrip(b"\x00").decode("ascii", errors="replace")
        if field_type == 5:  # rational
            vals = []
            for i in range(count):
                n, d = _struct.unpack_from(f"{endian}II", data, offset + i * 8)
                vals.append(n / d if d else 0.0)
            return vals
        size = _struct.calcsize(fmt)
        return [_struct.unpack_from(f"{endian}{fmt}", data, offset + i * size)[0]
                for i in range(count)]

    # Parse IFD
    tags = {}
    n_entries = _struct.unpack_from(f"{endian}H", data, ifd_offset)[0]
    pos = ifd_offset + 2
    for _ in range(n_entries):
        tag, field_type, count = _struct.unpack_from(f"{endian}HHI", data, pos)
        value_offset_pos = pos + 8
        size_map = {1:1, 2:1, 3:2, 4:4, 5:8, 11:4, 12:8, 16:8}
        unit = size_map.get(field_type, 4)
        total = unit * count
        if total <= 4:
            raw_val = read_tag_values(value_offset_pos, count, field_type, endian)
        else:
            offset = _struct.unpack_from(f"{endian}I", data, value_offset_pos)[0]
            raw_val = read_tag_values(offset, count, field_type, endian)
        tags[tag] = raw_val
        pos += 12

    def scalar(tag, default=None):
        v = tags.get(tag, default)
        if isinstance(v, list):
            return v[0] if v else default
        return v

    cols    = int(scalar(_TAG_IMAGE_WIDTH))
    rows    = int(scalar(_TAG_IMAGE_LENGTH))
    bps     = int(scalar(_TAG_BITS_PER_SAMPLE, 16))
    sfmt    = int(scalar(_TAG_SAMPLE_FORMAT, 1))   # 1=uint, 2=int, 3=float

    if bps == 32 and sfmt == 3:
        sample_fmt, sample_size = "f", 4
    elif bps == 16 and sfmt == 2:
        sample_fmt, sample_size = "h", 2
    elif bps == 16 and sfmt == 1:
        sample_fmt, sample_size = "H", 2
    elif bps == 32 and sfmt == 1:
        sample_fmt, sample_size = "I", 4
    else:
        raise ValueError(
            f"{path.name}: unsupported sample format {bps}-bit sfmt={sfmt}. "
            "Install GDAL or rasterio for full format support."
        )

    # GeoTIFF spatial metadata
    pixel_scale = tags.get(_TAG_MODEL_PIXEL_SCALE, [1.0, 1.0, 0.0])
    tie_point   = tags.get(_TAG_MODEL_TIE_POINT,   [0, 0, 0, 0, 0, 0])
    pixel_width  = float(pixel_scale[0])
    pixel_height = -float(pixel_scale[1])   # north-up → negative
    # Tiepoint gives (pixel_i, pixel_j, k, world_x, world_y, world_z)
    x_origin = float(tie_point[3]) - float(tie_point[0]) * pixel_width
    y_origin = float(tie_point[4]) - float(tie_point[1]) * pixel_height

    # Read elevation data
    elevations_raw = []
    if _TAG_TILE_OFFSETS in tags:
        # Tiled layout
        tile_w = int(scalar(_TAG_TILE_WIDTH))
        tile_h = int(scalar(_TAG_TILE_LENGTH))
        offsets = tags[_TAG_TILE_OFFSETS]
        if not isinstance(offsets, list):
            offsets = [offsets]
        tiles_across = (cols + tile_w - 1) // tile_w

        # Build a row-major grid
        grid = [[0.0] * cols for _ in range(rows)]
        for idx, off in enumerate(offsets):
            tx = (idx % tiles_across) * tile_w
            ty = (idx // tiles_across) * tile_h
            for tr in range(tile_h):
                gy = ty + tr
                if gy >= rows:
                    break
                for tc in range(tile_w):
                    gx = tx + tc
                    if gx >= cols:
                        break
                    byte_off = off + (tr * tile_w + tc) * sample_size
                    val = _struct.unpack_from(f"{endian}{sample_fmt}", data, byte_off)[0]
                    grid[gy][gx] = float(val)

        elevations_raw = [grid[r][c] for r in range(rows) for c in range(cols)]

    else:
        # Stripped layout
        offsets = tags.get(_TAG_STRIP_OFFSETS, [0])
        if not isinstance(offsets, list):
            offsets = [offsets]
        rps = int(scalar(_TAG_ROWS_PER_STRIP, rows))
        for strip_idx, off in enumerate(offsets):
            start_row = strip_idx * rps
            end_row = min(start_row + rps, rows)
            for r in range(start_row, end_row):
                for c in range(cols):
                    byte_off = off + ((r - start_row) * cols + c) * sample_size
                    val = _struct.unpack_from(f"{endian}{sample_fmt}", data, byte_off)[0]
                    elevations_raw.append(float(val))

    return DEMData(
        elevations=elevations_raw,
        cols=cols, rows=rows,
        x_origin=x_origin, y_origin=y_origin,
        pixel_width=pixel_width, pixel_height=pixel_height,
        nodata=None, srid=None,
    )


# ---------------------------------------------------------------------------
# Public reader — selects best available backend
# ---------------------------------------------------------------------------

def read_dem(path, band: int = 1) -> DEMData:
    """Read a DEM file and return a ``DEMData`` instance.

    Backend selection (first available wins):
    - GDAL
    - rasterio
    - struct fallback (single-band uncompressed GeoTIFF only)
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"DEM file not found: {path}")

    try:
        from osgeo import gdal  # noqa: F401
        log.debug("DEM: using GDAL backend")
        return _read_gdal(path, band)
    except ImportError:
        pass

    try:
        import rasterio  # noqa: F401
        log.debug("DEM: using rasterio backend")
        return _read_rasterio(path, band)
    except ImportError:
        pass

    log.debug("DEM: using struct fallback backend")
    return _read_struct(path)


# ---------------------------------------------------------------------------
# Mesh builder
# ---------------------------------------------------------------------------

def build_terrain_mesh(
    dem: DEMData,
    name: str = "Terrain",
    downsample: int = 1,
) -> "bpy.types.Mesh":
    """Build a Blender mesh from *dem*.

    Vertex positions are in the DEM's own projected coordinate space.
    The caller is responsible for offsetting to the Blender scene origin.
    """
    import bpy   # noqa: PLC0415
    import bmesh as _bmesh  # noqa: PLC0415

    step = max(1, int(downsample))
    sample_cols = list(range(0, dem.cols, step))
    sample_rows = list(range(0, dem.rows, step))

    sc = len(sample_cols)
    sr = len(sample_rows)

    verts = []
    for ri, r in enumerate(sample_rows):
        for ci, c in enumerate(sample_cols):
            x = dem.x_origin + c * dem.pixel_width
            y = dem.y_origin + r * dem.pixel_height
            z = dem.elevations[r * dem.cols + c]
            verts.append((x, y, z))

    faces = []
    for ri in range(sr - 1):
        for ci in range(sc - 1):
            v00 = ri * sc + ci
            v10 = ri * sc + ci + 1
            v01 = (ri + 1) * sc + ci
            v11 = (ri + 1) * sc + ci + 1
            faces.append((v00, v10, v11, v01))

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    return mesh


# ---------------------------------------------------------------------------
# Bounds helper
# ---------------------------------------------------------------------------

def dem_bounds(dem: DEMData) -> Tuple[float, float, float, float]:
    """Return (x_min, y_min, x_max, y_max) in the DEM's projected space."""
    x_min = dem.x_origin
    x_max = dem.x_origin + dem.cols * dem.pixel_width
    y_top  = dem.y_origin
    y_bot  = dem.y_origin + dem.rows * dem.pixel_height
    y_min  = min(y_top, y_bot)
    y_max  = max(y_top, y_bot)
    return x_min, y_min, x_max, y_max
