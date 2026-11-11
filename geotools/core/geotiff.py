"""GeoTIFF georeference and pixels, with the standard library and numpy.

:func:`read_info` parses the first image (IFD) of a TIFF or BigTIFF, in
either byte order: size, samples, sample format, compression, predictor,
strips or tiles, and the GeoTIFF tags (ModelPixelScale, ModelTiepoint,
ModelTransformation, GeoKeyDirectory) and GDAL_NODATA (42113). It returns a
:class:`GeoTiffInfo` with a north-up affine geotransform in binary64. Rotated
or sheared transforms and ground control points are refused: warp them to a
north-up grid in QGIS first.

The geotransform is GDAL's ``(x0, dx, 0, y0, 0, dy)``, always for pixel
*corners*: raster point (col, row) is at ``(x0 + col*dx, y0 + row*dy)``, so the
centre of pixel (c, r) is at ``c + 0.5, r + 0.5``. A PixelIsPoint file's
tiepoint names a pixel centre, so it is shifted by half a pixel, as GDAL does,
and its pixel centres land exactly on the tiepoint grid.

Pixels. Blender's image loader (OpenImageIO and libtiff) was checked against
GeoTIFFs written by the test fixtures and by GDAL. It reads these exactly:

- float32, one band or more, uncompressed, deflate or LZW, with or without
  the horizontal (2) or floating-point (3) predictor, strips or tiles, classic
  TIFF or BigTIFF, either byte order, NaN and ±3.4e38 kept;
- uint8, uint16 and int16, normalised to float (divided by 255, 65535 and
  32767; -32768 gives -1.00003). :func:`denormalise` undoes that exactly.

It does not keep float64 (rounded to float32), int32 or uint32 (normalised in
float32, so the low bits are lost), and int8 and float16 were not checked. For
those :func:`read_band` decodes the pixels itself: uncompressed or deflate
(Adobe 8 and the old 32946), predictor none, horizontal (2) or floating-point
(3), strips or tiles, chunky or planar. LZW with those types is refused with a
message to re-save as deflate; a pure-Python LZW decoder is far too slow for a
large DEM. :func:`dem_route` picks the reader.
"""

from __future__ import annotations

import math
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .crs_match import DataCrs

# --- tags ---------------------------------------------------------------------------

IMAGE_WIDTH = 256
IMAGE_LENGTH = 257
BITS_PER_SAMPLE = 258
COMPRESSION = 259
PHOTOMETRIC = 262
STRIP_OFFSETS = 273
SAMPLES_PER_PIXEL = 277
ROWS_PER_STRIP = 278
STRIP_BYTE_COUNTS = 279
PLANAR_CONFIGURATION = 284
PREDICTOR = 317
TILE_WIDTH = 322
TILE_LENGTH = 323
TILE_OFFSETS = 324
TILE_BYTE_COUNTS = 325
EXTRA_SAMPLES = 338
SAMPLE_FORMAT = 339
MODEL_PIXEL_SCALE = 33550
MODEL_TIEPOINT = 33922
MODEL_TRANSFORMATION = 34264
GEO_KEY_DIRECTORY = 34735
GEO_DOUBLE_PARAMS = 34736
GEO_ASCII_PARAMS = 34737
GDAL_NODATA = 42113

# GeoKeys
GT_MODEL_TYPE = 1024
GT_RASTER_TYPE = 1025
GT_CITATION = 1026
GEOGRAPHIC_TYPE = 2048
GEOG_CITATION = 2049
PROJECTED_CS_TYPE = 3072
PCS_CITATION = 3073
USER_DEFINED = 32767

MODEL_PROJECTED, MODEL_GEOGRAPHIC, MODEL_GEOCENTRIC = 1, 2, 3
RASTER_PIXEL_IS_AREA, RASTER_PIXEL_IS_POINT = 1, 2

COMPRESSION_NAMES = {
    1: "none",
    2: "CCITT RLE",
    5: "LZW",
    6: "old JPEG",
    7: "JPEG",
    8: "deflate",
    32773: "PackBits",
    32946: "deflate",
    34887: "LERC",
    34925: "LZMA",
    50000: "ZSTD",
    50001: "WebP",
    50002: "JPEG XL",
}
NUMPY_COMPRESSIONS = frozenset({1, 8, 32946})
NUMPY_PREDICTORS = frozenset({1, 2, 3})
PHOTOMETRIC_PALETTE = 3

# TIFF field type -> (struct character, size)
_TYPES: dict[int, tuple[str, int]] = {
    1: ("B", 1),  # BYTE
    2: ("s", 1),  # ASCII
    3: ("H", 2),  # SHORT
    4: ("I", 4),  # LONG
    5: ("II", 8),  # RATIONAL
    6: ("b", 1),  # SBYTE
    7: ("B", 1),  # UNDEFINED
    8: ("h", 2),  # SSHORT
    9: ("i", 4),  # SLONG
    10: ("ii", 8),  # SRATIONAL
    11: ("f", 4),  # FLOAT
    12: ("d", 8),  # DOUBLE
    13: ("I", 4),  # IFD
    16: ("Q", 8),  # LONG8
    17: ("q", 8),  # SLONG8
    18: ("Q", 8),  # IFD8
}

_KIND = {1: "u", 2: "i", 3: "f"}
_SAMPLE_FORMAT_NAMES = {1: "uint", 2: "int", 3: "float"}


class GeoTiffError(ValueError):
    """The file is not a GeoTIFF GeoTools can place or read; the message says why."""


# --- the result ---------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class GeoTiffInfo:
    """The first image of a GeoTIFF: layout, georeference and nodata."""

    path: str
    width: int
    height: int
    samples_per_pixel: int
    bits_per_sample: int
    sample_format: int
    """1 unsigned integer, 2 signed integer, 3 IEEE float."""
    compression: int
    predictor: int
    planar: int
    """1 chunky (samples interleaved), 2 planar (one plane per band)."""
    photometric: int | None
    extra_samples: tuple[int, ...]
    tile_size: tuple[int, int] | None
    """(width, height) of a tile, or None for strips."""
    rows_per_strip: int
    chunk_offsets: np.ndarray
    chunk_counts: np.ndarray
    byteorder: str
    """``"<"`` (II) or ``">"`` (MM)."""
    bigtiff: bool
    geotransform: tuple[float, float, float, float, float, float]
    """GDAL order ``(x0, dx, 0, y0, 0, dy)`` for pixel corners, binary64."""
    pixel_is_point: bool
    crs: DataCrs | None
    crs_citation: str
    geographic: bool
    """True when the GeoKeys say the CRS is geographic (degrees)."""
    nodata: float | None
    nodata_text: str | None

    # Layout -----------------------------------------------------------------------

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def tiled(self) -> bool:
        return self.tile_size is not None

    @property
    def dtype(self) -> np.dtype:
        """The sample type in the file's byte order."""
        kind = _KIND.get(self.sample_format)
        if kind is None or self.bits_per_sample % 8:
            raise GeoTiffError(f"{self.name}: unsupported sample type {self.band_layout}")
        dtype = np.dtype(f"{kind}{self.bits_per_sample // 8}")
        return dtype.newbyteorder(self.byteorder)

    @property
    def sample_type(self) -> str:
        """``"float32"``, ``"int16"``, ``"uint8"``...: the sample type's name."""
        name = _SAMPLE_FORMAT_NAMES.get(self.sample_format, f"format{self.sample_format}-")
        return f"{name}{self.bits_per_sample}"

    @property
    def band_layout(self) -> str:
        """For messages, e.g. ``"1 band float32"`` or ``"3 bands uint8, RGB"``."""
        bands = f"{self.samples_per_pixel} band{'s' if self.samples_per_pixel != 1 else ''}"
        extra = {2: ", RGB", 3: ", palette", 6: ", YCbCr"}.get(self.photometric or 0, "")
        if self.extra_samples and extra == ", RGB":
            extra = ", RGBA"
        return f"{bands} {self.sample_type}{extra}"

    @property
    def compression_name(self) -> str:
        return COMPRESSION_NAMES.get(self.compression, f"compression {self.compression}")

    @property
    def has_alpha(self) -> bool:
        return bool(self.extra_samples) and self.extra_samples[0] in (1, 2)

    # Georeference -------------------------------------------------------------------

    @property
    def pixel_size(self) -> tuple[float, float]:
        """``(dx, dy)``: dy is negative for the usual north-up raster."""
        return self.geotransform[1], self.geotransform[5]

    def corner(self, col: float, row: float) -> tuple[float, float]:
        """Map coordinates of raster point (col, row); pixel (c, r) spans c..c+1."""
        x0, dx, _, y0, _, dy = self.geotransform
        return x0 + col * dx, y0 + row * dy

    def bounds(self) -> tuple[float, float, float, float]:
        """``(xmin, ymin, xmax, ymax)`` of the raster's footprint (pixel edges)."""
        xa, ya = self.corner(0.0, 0.0)
        xb, yb = self.corner(float(self.width), float(self.height))
        return min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)

    def centres_x(self, cols: np.ndarray) -> np.ndarray:
        """Map x of the centres of columns ``cols``, binary64."""
        x0, dx = self.geotransform[0], self.geotransform[1]
        return x0 + (np.asarray(cols, np.float64) + 0.5) * dx

    def centres_y(self, rows: np.ndarray) -> np.ndarray:
        """Map y of the centres of rows ``rows``, binary64."""
        y0, dy = self.geotransform[3], self.geotransform[5]
        return y0 + (np.asarray(rows, np.float64) + 0.5) * dy


# --- reading tags ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    ftype: int
    count: int
    inline: bytes  # the value slot (4 or 8 bytes)


class _Reader:
    def __init__(self, fh, path: str) -> None:
        self.fh = fh
        self.name = Path(path).name
        head = fh.read(16)
        if len(head) < 8 or head[:2] not in (b"II", b"MM"):
            raise GeoTiffError(f"{self.name} is not a TIFF file")
        self.order = "<" if head[:2] == b"II" else ">"
        (magic,) = struct.unpack(self.order + "H", head[2:4])
        if magic == 42:
            self.bigtiff = False
            (self.first_ifd,) = struct.unpack(self.order + "I", head[4:8])
        elif magic == 43:
            if len(head) < 16:
                raise GeoTiffError(f"{self.name}: truncated BigTIFF header")
            size, _zero, first = struct.unpack(self.order + "HHQ", head[4:16])
            if size != 8:
                raise GeoTiffError(f"{self.name}: unsupported BigTIFF offset size {size}")
            self.bigtiff = True
            self.first_ifd = first
        else:
            raise GeoTiffError(f"{self.name} is not a TIFF file (version {magic})")

    def read_at(self, offset: int, size: int) -> bytes:
        self.fh.seek(offset)
        data = self.fh.read(size)
        if len(data) != size:
            raise GeoTiffError(f"{self.name} is truncated (needs {size} bytes at {offset})")
        return data

    def entries(self) -> dict[int, _Entry]:
        o = self.order
        if self.bigtiff:
            (count,) = struct.unpack(o + "Q", self.read_at(self.first_ifd, 8))
            raw = self.read_at(self.first_ifd + 8, count * 20)
            step, fmt, slot = 20, "HHQ", 8
        else:
            (count,) = struct.unpack(o + "H", self.read_at(self.first_ifd, 2))
            raw = self.read_at(self.first_ifd + 2, count * 12)
            step, fmt, slot = 12, "HHI", 4
        result = {}
        for i in range(count):
            tag, ftype, n = struct.unpack_from(o + fmt, raw, i * step)
            value = raw[i * step + step - slot : i * step + step]
            result[tag] = _Entry(ftype, n, value)
        return result

    def raw(self, entry: _Entry) -> bytes:
        if entry.ftype not in _TYPES:
            raise GeoTiffError(f"{self.name}: unknown TIFF field type {entry.ftype}")
        size = _TYPES[entry.ftype][1] * entry.count
        slot = 8 if self.bigtiff else 4
        if size <= slot:
            return entry.inline[:size]
        (offset,) = struct.unpack(self.order + ("Q" if self.bigtiff else "I"), entry.inline)
        return self.read_at(offset, size)

    def numbers(self, entry: _Entry) -> np.ndarray:
        """The values as a numpy array (float64 for rationals)."""
        data = self.raw(entry)
        char = _TYPES[entry.ftype][0]
        if entry.ftype == 2:
            raise GeoTiffError(f"{self.name}: expected numbers, found text")
        dtype = np.dtype(_NUMPY_CHAR[char[0]]).newbyteorder(self.order)
        values = np.frombuffer(data, dtype)
        if entry.ftype in (5, 10):  # rationals: numerator, denominator pairs
            pairs = values.astype(np.float64).reshape(-1, 2)
            return pairs[:, 0] / pairs[:, 1]
        return values

    def text(self, entry: _Entry) -> str:
        return self.raw(entry).split(b"\x00", 1)[0].decode("latin-1").strip()


_NUMPY_CHAR = {
    "B": "u1",
    "b": "i1",
    "H": "u2",
    "h": "i2",
    "I": "u4",
    "i": "i4",
    "Q": "u8",
    "q": "i8",
    "f": "f4",
    "d": "f8",
}


def _one(reader: _Reader, entries: dict[int, _Entry], tag: int, default: int | None = None) -> int:
    entry = entries.get(tag)
    if entry is None:
        if default is None:
            raise GeoTiffError(f"{reader.name}: missing required TIFF tag {tag}")
        return default
    return int(reader.numbers(entry)[0])


def read_info(path: str | os.PathLike[str]) -> GeoTiffInfo:
    """Parse the first image of a GeoTIFF. Raises :class:`GeoTiffError`."""
    path = os.fspath(path)
    try:
        with open(path, "rb") as fh:
            reader = _Reader(fh, path)
            entries = reader.entries()
            return _info(reader, entries, path)
    except OSError as ex:
        raise GeoTiffError(f"Cannot read {Path(path).name}: {ex.strerror or ex}") from ex
    except struct.error as ex:
        raise GeoTiffError(f"{Path(path).name} is truncated or not a TIFF file: {ex}") from ex


def _info(reader: _Reader, entries: dict[int, _Entry], path: str) -> GeoTiffInfo:
    name = reader.name
    width = _one(reader, entries, IMAGE_WIDTH)
    height = _one(reader, entries, IMAGE_LENGTH)
    spp = _one(reader, entries, SAMPLES_PER_PIXEL, 1)
    if width < 1 or height < 1 or spp < 1:
        raise GeoTiffError(f"{name}: empty image ({width} x {height}, {spp} samples)")

    def per_sample(tag: int, default: int) -> int:
        entry = entries.get(tag)
        if entry is None:
            return default
        values = {int(v) for v in reader.numbers(entry)}
        if len(values) != 1:
            raise GeoTiffError(f"{name}: bands with different sample types are not supported")
        return values.pop()

    bits = per_sample(BITS_PER_SAMPLE, 1)
    sample_format = per_sample(SAMPLE_FORMAT, 1)
    photometric = _one(reader, entries, PHOTOMETRIC, -1)
    extra = entries.get(EXTRA_SAMPLES)
    if TILE_WIDTH in entries or TILE_OFFSETS in entries:
        tile = (_one(reader, entries, TILE_WIDTH), _one(reader, entries, TILE_LENGTH))
        offsets, counts = entries.get(TILE_OFFSETS), entries.get(TILE_BYTE_COUNTS)
    else:
        tile = None
        offsets, counts = entries.get(STRIP_OFFSETS), entries.get(STRIP_BYTE_COUNTS)
    if offsets is None or counts is None:
        raise GeoTiffError(f"{name}: missing strip or tile offsets")
    rows_per_strip = min(_one(reader, entries, ROWS_PER_STRIP, height), height)
    if rows_per_strip < 1:
        rows_per_strip = height

    geotransform = _geotransform(reader, entries)
    keys = _geokeys(reader, entries)
    raster_type = keys.get(GT_RASTER_TYPE, RASTER_PIXEL_IS_AREA)
    pixel_is_point = raster_type == RASTER_PIXEL_IS_POINT
    if pixel_is_point:
        x0, dx, _, y0, _, dy = geotransform
        geotransform = (x0 - 0.5 * dx, dx, 0.0, y0 - 0.5 * dy, 0.0, dy)
    crs, citation = _crs(name, keys)
    nodata, nodata_text = _nodata(reader, entries)

    return GeoTiffInfo(
        path=path,
        width=width,
        height=height,
        samples_per_pixel=spp,
        bits_per_sample=bits,
        sample_format=sample_format,
        compression=_one(reader, entries, COMPRESSION, 1),
        predictor=_one(reader, entries, PREDICTOR, 1),
        planar=_one(reader, entries, PLANAR_CONFIGURATION, 1),
        photometric=None if photometric < 0 else photometric,
        extra_samples=tuple(int(v) for v in reader.numbers(extra)) if extra else (),
        tile_size=tile,
        rows_per_strip=rows_per_strip,
        chunk_offsets=reader.numbers(offsets).astype(np.int64),
        chunk_counts=reader.numbers(counts).astype(np.int64),
        byteorder=reader.order,
        bigtiff=reader.bigtiff,
        geotransform=geotransform,
        pixel_is_point=pixel_is_point,
        crs=crs,
        crs_citation=citation,
        geographic=keys.get(GT_MODEL_TYPE) == MODEL_GEOGRAPHIC
        or (
            GT_MODEL_TYPE not in keys and GEOGRAPHIC_TYPE in keys and PROJECTED_CS_TYPE not in keys
        ),
        nodata=nodata,
        nodata_text=nodata_text,
    )


_WARP_HINT = "Warp it to a north-up grid in QGIS (Raster > Projections > Warp) first."


def _geotransform(
    reader: _Reader, entries: dict[int, _Entry]
) -> tuple[float, float, float, float, float, float]:
    name = reader.name
    if MODEL_TRANSFORMATION in entries:
        m = [float(v) for v in reader.numbers(entries[MODEL_TRANSFORMATION])]
        if len(m) != 16:
            raise GeoTiffError(f"{name}: ModelTransformation must hold 16 numbers")
        dx, rx, x0 = m[0], m[1], m[3]
        ry, dy, y0 = m[4], m[5], m[7]
        scale = max(abs(dx), abs(dy))
        if scale == 0.0 or abs(rx) > 1e-12 * scale or abs(ry) > 1e-12 * scale:
            raise GeoTiffError(
                f"{name} has a rotated or sheared geotransform "
                f"(ModelTransformation terms {rx:g} and {ry:g}); GeoTools only places "
                f"north-up rasters. {_WARP_HINT}"
            )
        transform = (x0, dx, 0.0, y0, 0.0, dy)
    elif MODEL_TIEPOINT in entries and MODEL_PIXEL_SCALE in entries:
        tie = [float(v) for v in reader.numbers(entries[MODEL_TIEPOINT])]
        scale = [float(v) for v in reader.numbers(entries[MODEL_PIXEL_SCALE])]
        if len(tie) < 6 or len(scale) < 2:
            raise GeoTiffError(f"{name}: ModelTiepoint or ModelPixelScale is too short")
        if len(tie) > 6:
            raise GeoTiffError(
                f"{name} is georeferenced by {len(tie) // 6} ground control points, "
                f"not a grid. {_WARP_HINT}"
            )
        i, j, _k, x, y, _z = tie[:6]
        sx, sy = scale[0], scale[1]
        dx, dy = sx, -sy
        transform = (x - i * dx, dx, 0.0, y - j * dy, 0.0, dy)
    elif MODEL_TIEPOINT in entries:
        raise GeoTiffError(
            f"{name} is georeferenced by ground control points only, not a grid. {_WARP_HINT}"
        )
    else:
        raise GeoTiffError(
            f"{name} has no georeference (no ModelPixelScale and ModelTiepoint, or "
            "ModelTransformation). Georeference it in QGIS first."
        )
    x0, dx, _, y0, _, dy = transform
    if not all(math.isfinite(v) for v in transform) or dx == 0.0 or dy == 0.0:
        raise GeoTiffError(f"{name}: the pixel size must be finite and non-zero")
    return transform


def _geokeys(reader: _Reader, entries: dict[int, _Entry]) -> dict[int, object]:
    """GeoKey id -> int, float, tuple of floats, or str."""
    entry = entries.get(GEO_KEY_DIRECTORY)
    if entry is None:
        return {}
    shorts = [int(v) for v in reader.numbers(entry)]
    if len(shorts) < 4:
        raise GeoTiffError(f"{reader.name}: GeoKeyDirectory is too short")
    count = shorts[3]
    doubles = (
        [float(v) for v in reader.numbers(entries[GEO_DOUBLE_PARAMS])]
        if GEO_DOUBLE_PARAMS in entries
        else []
    )
    ascii_params = (
        reader.raw(entries[GEO_ASCII_PARAMS]).decode("latin-1")
        if GEO_ASCII_PARAMS in entries
        else ""
    )
    keys: dict[int, object] = {}
    for k in range(count):
        base = 4 + 4 * k
        if base + 4 > len(shorts):
            break
        key, location, n, value = shorts[base : base + 4]
        if location == 0:
            keys[key] = value
        elif location == GEO_DOUBLE_PARAMS:
            part = doubles[value : value + n]
            keys[key] = part[0] if n == 1 and part else tuple(part)
        elif location == GEO_ASCII_PARAMS:
            keys[key] = ascii_params[value : value + n].rstrip("|\x00").strip()
        elif location == GEO_KEY_DIRECTORY:
            keys[key] = tuple(shorts[value : value + n])
    return keys


def _crs(name: str, keys: dict[int, object]) -> tuple[DataCrs | None, str]:
    citation = next(
        (str(keys[k]) for k in (PCS_CITATION, GT_CITATION, GEOG_CITATION) if k in keys), ""
    )
    if not keys:
        return None, citation
    model = keys.get(GT_MODEL_TYPE)
    if model == MODEL_GEOCENTRIC:
        raise GeoTiffError(f"{name} is in a geocentric CRS; GeoTools places map rasters only")
    projected = keys.get(PROJECTED_CS_TYPE)
    geographic = keys.get(GEOGRAPHIC_TYPE)
    code = None
    if model in (MODEL_PROJECTED, None) and isinstance(projected, int):
        code = projected
    elif model in (MODEL_GEOGRAPHIC, None) and isinstance(geographic, int):
        code = geographic
    if code is not None and 0 < code < USER_DEFINED:
        return DataCrs(epsg=f"EPSG:{code}", definition=None, source="geotiff-geokeys"), citation
    if model is None and code is None:
        return None, citation
    # User-defined (32767) or no code: GeoTools cannot tell which CRS it is.
    return DataCrs(epsg=None, definition=None, source="geotiff-geokeys"), citation


def _nodata(reader: _Reader, entries: dict[int, _Entry]) -> tuple[float | None, str | None]:
    entry = entries.get(GDAL_NODATA)
    if entry is None:
        return None, None
    text = reader.text(entry)
    try:
        return float(text), text
    except ValueError:
        return None, text


# --- pixels -------------------------------------------------------------------------

BLENDER_FAITHFUL = frozenset({"float32", "uint8", "uint16", "int16"})
"""Sample types Blender's image loader reads exactly (after :func:`denormalise`)."""

_NORMALISATION = {"uint8": 255.0, "uint16": 65535.0, "int16": 32767.0}


def dem_route(info: GeoTiffInfo) -> str:
    """How to read band 1 for a DEM: ``"blender"`` or ``"numpy"``.

    Raises :class:`GeoTiffError` for files neither reads faithfully.
    """
    if info.photometric == PHOTOMETRIC_PALETTE:
        raise GeoTiffError(
            f"{info.name} is a palette (indexed colour) image, not heights; import it as "
            "an orthophoto"
        )
    if info.sample_format not in _KIND or info.bits_per_sample not in (8, 16, 32, 64):
        raise GeoTiffError(
            f"{info.name}: {info.band_layout} samples are not supported for a DEM; "
            "save it as Float32 (gdal_translate -ot Float32)"
        )
    if info.sample_type in BLENDER_FAITHFUL and info.samples_per_pixel <= 4:
        return "blender"
    numpy_problem = numpy_unsupported(info)
    if numpy_problem is None:
        return "numpy"
    raise GeoTiffError(
        f"{info.name}: {info.sample_type} with {numpy_problem} cannot be read exactly. "
        "Re-save it as deflate (gdal_translate -co COMPRESS=DEFLATE) or as Float32 "
        "(-ot Float32)"
    )


def numpy_unsupported(info: GeoTiffInfo) -> str | None:
    """Why :func:`read_band` cannot read ``info``, or None if it can."""
    if info.compression not in NUMPY_COMPRESSIONS:
        return f"{info.compression_name} compression"
    if info.predictor not in NUMPY_PREDICTORS:
        return f"predictor {info.predictor}"
    if info.predictor == 3 and info.sample_format != 3:
        return "the floating-point predictor on integers"
    if info.sample_format not in _KIND or info.bits_per_sample not in (8, 16, 32, 64):
        return f"{info.bits_per_sample}-bit samples"
    if info.sample_format == 3 and info.bits_per_sample == 8:
        return "8-bit float samples"
    if info.planar not in (1, 2):
        return f"planar configuration {info.planar}"
    return None


def denormalise(values: np.ndarray, sample_type: str) -> np.ndarray:
    """Undo Blender's integer-to-float normalisation, in the file's sample type.

    ``values`` are what ``Image.pixels`` returned for one channel. uint8,
    uint16 and int16 come back as those exact integers; float32 passes
    through unchanged, so both readers give the same array.
    """
    values = np.asarray(values, np.float32)
    scale = _NORMALISATION.get(sample_type)
    if scale is None:
        return values
    return np.rint(values.astype(np.float64) * scale).astype(sample_type)


def read_band(info: GeoTiffInfo, band: int = 0, *, threads: int | None = None) -> np.ndarray:
    """Band ``band`` (0-based) as a (height, width) array of the file's sample type.

    Decodes uncompressed and deflate strips or tiles with predictors 1, 2 and 3.
    Raises :class:`GeoTiffError` for anything else (see :func:`numpy_unsupported`).
    """
    problem = numpy_unsupported(info)
    if problem is not None:
        raise GeoTiffError(f"{info.name}: GeoTools cannot decode {problem}")
    if not 0 <= band < info.samples_per_pixel:
        raise GeoTiffError(f"{info.name} has no band {band + 1}")
    dtype = info.dtype
    spp = 1 if info.planar == 2 else info.samples_per_pixel
    if info.tiled:
        tw, th = info.tile_size
        across, down = -(-info.width // tw), -(-info.height // th)
        per_plane = across * down
        shapes = [(th, tw)] * per_plane
    else:
        rps = info.rows_per_strip
        per_plane = -(-info.height // rps)
        shapes = [(min(rps, info.height - k * rps), info.width) for k in range(per_plane)]
    first = band * per_plane if info.planar == 2 else 0
    offsets = info.chunk_offsets[first : first + per_plane]
    counts = info.chunk_counts[first : first + per_plane]
    if len(offsets) != per_plane or len(counts) != per_plane:
        raise GeoTiffError(
            f"{info.name}: expected {per_plane} {'tiles' if info.tiled else 'strips'} "
            f"per band, found {len(offsets)}"
        )

    def decode(k: int, raw: bytes) -> np.ndarray:
        rows, cols = shapes[k]
        expected = rows * cols * spp * dtype.itemsize
        if info.compression != 1 and raw:
            try:
                raw = zlib.decompress(raw)
            except zlib.error as ex:
                raise GeoTiffError(f"{info.name}: corrupt deflate data in chunk {k}: {ex}")
        if not raw:  # a sparse (missing) chunk
            fill = info.nodata if info.nodata is not None else 0
            return np.full((rows, cols), fill, dtype=dtype.newbyteorder("="))
        if len(raw) < expected:
            raise GeoTiffError(f"{info.name}: chunk {k} is short ({len(raw)} of {expected})")
        block = _unpredict(raw[:expected], info.predictor, dtype, rows, cols, spp)
        return block[:, :, band if info.planar == 1 else 0]

    # Reading is serial (one file handle); decompressing runs in threads, since
    # zlib releases the GIL.
    with open(info.path, "rb") as fh:
        raws = [_read_chunk(fh, int(offsets[k]), int(counts[k])) for k in range(per_plane)]
    workers = threads if threads is not None else min(8, os.cpu_count() or 1)
    if workers > 1 and per_plane > 1 and info.compression != 1:
        with ThreadPoolExecutor(workers) as pool:
            blocks = list(pool.map(decode, range(per_plane), raws))
    else:
        blocks = [decode(k, raws[k]) for k in range(per_plane)]

    if info.tiled:
        tw, th = info.tile_size
        grid = np.stack(blocks).reshape(down, across, th, tw)
        out = grid.transpose(0, 2, 1, 3).reshape(down * th, across * tw)
        return np.ascontiguousarray(out[: info.height, : info.width])
    return np.concatenate(blocks, axis=0)


def _read_chunk(fh, offset: int, count: int) -> bytes:
    if count <= 0:
        return b""
    fh.seek(offset)
    return fh.read(count)


def _unpredict(
    raw: bytes, predictor: int, dtype: np.dtype, rows: int, cols: int, spp: int
) -> np.ndarray:
    """(rows, cols, spp) samples in native byte order, with the predictor undone."""
    native = dtype.newbyteorder("=")
    if predictor == 3:
        size = dtype.itemsize
        planes = np.frombuffer(raw, np.uint8).reshape(rows, cols * size, spp)
        planes = np.cumsum(planes, axis=1, dtype=np.uint8).reshape(rows, size, cols * spp)
        # Byte planes are most significant first, whatever the file's byte order.
        shuffled = np.ascontiguousarray(planes.transpose(0, 2, 1))
        values = shuffled.view(dtype.newbyteorder(">")).reshape(rows, cols, spp)
        return values.astype(native)
    values = np.frombuffer(raw, dtype).reshape(rows, cols, spp).astype(native)
    if predictor == 2:
        unsigned = np.dtype(f"u{dtype.itemsize}")
        values = np.cumsum(values.view(unsigned), axis=1, dtype=unsigned).view(native)
    return values
