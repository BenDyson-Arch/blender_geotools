"""A minimal GeoTIFF writer for synthetic test fixtures (stdlib and numpy only).

Used by the Blender tests (``tests/blender``) and the core tests
(``tests/core``, which put this directory on ``sys.path``). It writes one
image, chunky (``PlanarConfiguration`` 1), as classic TIFF or BigTIFF, in
either byte order:

- samples: uint8, int16, uint16, int32, uint32, float32, float64; one band
  (grey) or three or four (RGB, RGBA);
- compression: none, deflate (Adobe, 8) or LZW (5);
- strips or tiles, with the horizontal (2) or floating-point (3) predictor;
- georeference: ModelPixelScale + ModelTiepoint, or ModelTransformation, a
  GeoKeyDirectory (projected or geographic EPSG code, PixelIsArea or
  PixelIsPoint) and GDAL_NODATA.

It is checked against GDAL by hand, never in the tests (GDAL is not a test
dependency).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# TIFF field types: (code, struct format character, size in bytes)
_SHORT, _LONG, _DOUBLE, _ASCII, _LONG8 = 3, 4, 12, 2, 16
_FORMAT = {3: "H", 4: "I", 12: "d", 2: "s", 16: "Q"}
_SIZE = {3: 2, 4: 4, 12: 8, 2: 1, 16: 8}

_SAMPLE_FORMAT = {"u": 1, "i": 2, "f": 3}
COMPRESSION = {"none": 1, "lzw": 5, "deflate": 8}


@dataclass
class GeoTiffSpec:
    """Georeference for :func:`write_geotiff`; every field has a sensible default."""

    origin: tuple[float, float] = (500_000.0, 7_000_000.0)
    """Map x, y of the raster's top-left corner (PixelIsArea) or top-left pixel
    centre (PixelIsPoint), as written to the tiepoint."""
    pixel_size: tuple[float, float] = (1.0, 1.0)
    """ModelPixelScale x, y (both positive; y grows down the rows)."""
    epsg: int | None = 7856
    geographic: bool = False
    pixel_is_point: bool = False
    nodata: str | None = None
    transformation: tuple[float, ...] | None = None
    """16 doubles for ModelTransformation instead of scale + tiepoint."""
    citation: str | None = None
    ground_control: list[tuple[float, float, float, float, float, float]] = field(
        default_factory=list
    )
    """Extra (i, j, k, x, y, z) tiepoints: with any, no ModelPixelScale is written."""
    extra_keys: list[tuple[int, int, int, int]] = field(default_factory=list)
    geokeys: bool = True
    """False to write no GeoKeyDirectory (a georeferenced file with no CRS)."""


def _lzw_encode(data: bytes) -> bytes:
    """TIFF LZW: MSB-first codes, 9 to 12 bits, early change, Clear and EOI."""
    clear, eoi = 256, 257
    out = bytearray()
    acc = nbits = 0

    def emit(code: int, width: int) -> None:
        nonlocal acc, nbits
        acc = (acc << width) | code
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
        acc &= (1 << nbits) - 1

    table: dict[bytes, int] = {bytes([i]): i for i in range(256)}
    next_code, width = 258, 9
    emit(clear, width)
    prefix = b""
    for byte in data:
        candidate = prefix + bytes([byte])
        if candidate in table:
            prefix = candidate
            continue
        emit(table[prefix], width)
        table[candidate] = next_code
        next_code += 1
        if next_code + 1 > (1 << width) and width < 12:
            width += 1
        if next_code >= 4093:
            emit(clear, width)
            table = {bytes([i]): i for i in range(256)}
            next_code, width = 258, 9
        prefix = bytes([byte])
    if prefix:
        emit(table[prefix], width)
        next_code += 1
        if next_code + 1 > (1 << width) and width < 12:
            width += 1
    emit(eoi, width)
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def _predict(block: np.ndarray, predictor: int, order: str) -> bytes:
    """Encode one (rows, cols, samples) block with the predictor, as file bytes."""
    rows, cols, spp = block.shape
    if predictor == 1:
        return block.astype(block.dtype.newbyteorder(order), copy=False).tobytes()
    if predictor == 2:
        unsigned = block.view(block.dtype.str.replace("i", "u").replace("f", "u"))
        diff = unsigned.copy()
        diff[:, 1:, :] = unsigned[:, 1:, :] - unsigned[:, :-1, :]
        return diff.astype(diff.dtype.newbyteorder(order), copy=False).tobytes()
    if predictor == 3:
        size = block.dtype.itemsize
        # Byte planes, most significant first, then byte differencing per row.
        be = block.astype(block.dtype.newbyteorder(">"), copy=False)
        raw = np.frombuffer(be.tobytes(), np.uint8).reshape(rows, cols * spp, size)
        planes = raw.transpose(0, 2, 1).reshape(rows, cols * spp * size)
        # Differencing with a stride of samples-per-pixel bytes.
        strided = planes.reshape(rows, -1, spp)
        diff = strided.copy()
        diff[:, 1:, :] = strided[:, 1:, :] - strided[:, :-1, :]
        return diff.tobytes()
    raise ValueError(f"predictor {predictor}")


def _compress(data: bytes, compression: str) -> bytes:
    if compression == "none":
        return data
    if compression == "deflate":
        return zlib.compress(data, 6)
    if compression == "lzw":
        return _lzw_encode(data)
    raise ValueError(compression)


def geokeys(spec: GeoTiffSpec) -> tuple[list[int], list[float], str]:
    """The GeoKeyDirectory SHORTs, GeoDoubleParams and GeoAsciiParams for ``spec``."""
    keys: list[tuple[int, int, int, int]] = [
        (1024, 0, 1, 2 if spec.geographic else 1),  # GTModelTypeGeoKey
        (1025, 0, 1, 2 if spec.pixel_is_point else 1),  # GTRasterTypeGeoKey
    ]
    ascii_params = ""
    if spec.citation is not None:
        text = spec.citation + "|"
        keys.append((1026, 34737, len(text), len(ascii_params)))
        ascii_params += text
    if spec.epsg is not None:
        keys.append((2048 if spec.geographic else 3072, 0, 1, spec.epsg))
    elif not spec.geographic:
        keys.append((3072, 0, 1, 32767))  # user-defined
    if not spec.geographic:
        keys.append((3076, 0, 1, 9001))  # ProjLinearUnitsGeoKey: metre
    keys += spec.extra_keys
    keys.sort()
    shorts = [1, 1, 0, len(keys)]
    for key in keys:
        shorts += list(key)
    return shorts, [], ascii_params


def write_geotiff(
    path: Path,
    data: np.ndarray,
    spec: GeoTiffSpec | None = None,
    *,
    compression: str = "none",
    predictor: int = 1,
    tile: tuple[int, int] | None = None,
    rows_per_strip: int | None = None,
    bigtiff: bool = False,
    byteorder: str = "<",
    geo: bool = True,
) -> Path:
    """Write ``data`` ((rows, cols) or (rows, cols, samples)) as a GeoTIFF.

    ``tile`` is (width, height), each a multiple of 16. Without ``tile`` the
    image is written in strips of ``rows_per_strip`` rows (default: all rows).
    """
    spec = spec or GeoTiffSpec()
    data = np.asarray(data)
    if data.ndim == 2:
        data = data[:, :, None]
    height, width, spp = data.shape
    kind = data.dtype.kind
    bits = data.dtype.itemsize * 8

    chunks: list[bytes] = []
    if tile is not None:
        tw, th = tile
        across, down = -(-width // tw), -(-height // th)
        padded = np.zeros((down * th, across * tw, spp), data.dtype)
        padded[:height, :width] = data
        for ty in range(down):
            for tx in range(across):
                block = padded[ty * th : (ty + 1) * th, tx * tw : (tx + 1) * tw]
                chunks.append(_compress(_predict(block, predictor, byteorder), compression))
    else:
        rps = rows_per_strip or height
        for top in range(0, height, rps):
            block = data[top : top + rps]
            chunks.append(_compress(_predict(block, predictor, byteorder), compression))

    header_size = 16 if bigtiff else 8
    offsets, pos = [], header_size
    for chunk in chunks:
        offsets.append(pos)
        pos += len(chunk) + (len(chunk) & 1)
    counts = [len(c) for c in chunks]
    offset_type = _LONG8 if bigtiff else _LONG

    entries: list[tuple[int, int, list | str]] = [
        (256, _LONG, [width]),
        (257, _LONG, [height]),
        (258, _SHORT, [bits] * spp),
        (259, _SHORT, [COMPRESSION[compression]]),
        (262, _SHORT, [2 if spp >= 3 else 1]),
        (277, _SHORT, [spp]),
        (284, _SHORT, [1]),
        (339, _SHORT, [_SAMPLE_FORMAT[kind]] * spp),
    ]
    if predictor != 1:
        entries.append((317, _SHORT, [predictor]))
    if spp == 4:
        entries.append((338, _SHORT, [2]))  # unassociated alpha
    if tile is not None:
        entries += [
            (322, _LONG, [tile[0]]),
            (323, _LONG, [tile[1]]),
            (324, offset_type, offsets),
            (325, offset_type, counts),
        ]
    else:
        entries += [
            (273, offset_type, offsets),
            (278, _LONG, [rows_per_strip or height]),
            (279, offset_type, counts),
        ]
    if geo:
        if spec.transformation is not None:
            entries.append((34264, _DOUBLE, list(spec.transformation)))
        elif spec.ground_control:
            ties = [0.0, 0.0, 0.0, spec.origin[0], spec.origin[1], 0.0]
            for point in spec.ground_control:
                ties += list(point)
            entries.append((33922, _DOUBLE, ties))
        else:
            sx, sy = spec.pixel_size
            entries.append((33550, _DOUBLE, [sx, sy, 0.0]))
            entries.append((33922, _DOUBLE, [0.0, 0.0, 0.0, spec.origin[0], spec.origin[1], 0.0]))
        if spec.geokeys:
            shorts, doubles, ascii_params = geokeys(spec)
            entries.append((34735, _SHORT, shorts))
            if doubles:
                entries.append((34736, _DOUBLE, doubles))
            if ascii_params:
                entries.append((34737, _ASCII, ascii_params))
        if spec.nodata is not None:
            entries.append((42113, _ASCII, spec.nodata))
    entries.sort(key=lambda e: e[0])

    o = byteorder
    count_fmt, offset_fmt, slot = ("Q", "Q", 8) if bigtiff else ("I", "I", 4)
    entry_size = 20 if bigtiff else 12
    ifd_offset = pos
    ifd_size = (8 if bigtiff else 2) + len(entries) * entry_size + slot
    extra_pos = ifd_offset + ifd_size
    ifd = bytearray(struct.pack(o + ("Q" if bigtiff else "H"), len(entries)))
    extra = bytearray()
    for tag, ftype, values in entries:
        if ftype == _ASCII:
            raw = (values if isinstance(values, str) else "").encode("ascii") + b"\x00"
            count = len(raw)
        else:
            raw = struct.pack(o + _FORMAT[ftype] * len(values), *values)
            count = len(values)
        head = struct.pack(o + "HH" + count_fmt, tag, ftype, count)
        if len(raw) <= slot:
            ifd += head + raw.ljust(slot, b"\x00")
        else:
            ifd += head + struct.pack(o + offset_fmt, extra_pos + len(extra))
            extra += raw + (b"\x00" if len(raw) & 1 else b"")
    ifd += struct.pack(o + offset_fmt, 0)

    magic = b"II" if byteorder == "<" else b"MM"
    if bigtiff:
        header = magic + struct.pack(o + "HHHQ", 43, 8, 0, ifd_offset)
    else:
        header = magic + struct.pack(o + "HI", 42, ifd_offset)
    with open(path, "wb") as fh:
        fh.write(header)
        for chunk in chunks:
            fh.write(chunk + (b"\x00" if len(chunk) & 1 else b""))
        fh.write(ifd)
        fh.write(extra)
    return path


def dem(rows: int, cols: int, dtype: str = "float32", seed: int = 0) -> np.ndarray:
    """A smooth synthetic height field with a little noise, ``rows`` x ``cols``."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:rows, 0:cols].astype(np.float64)
    z = 100.0 + 20.0 * np.sin(x / 7.0) + 10.0 * np.cos(y / 5.0) + x * 0.25 - y * 0.5
    z += rng.normal(0.0, 0.3, z.shape)
    if np.dtype(dtype).kind == "f":
        return z.astype(dtype)
    return np.round(z).astype(dtype)


def rgb(rows: int, cols: int, seed: int = 0) -> np.ndarray:
    """An 8-bit RGB image of noise with a gradient, ``rows`` x ``cols`` x 3."""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (rows, cols, 3), dtype=np.uint8)
    img[:, :, 0] = np.linspace(0, 255, cols, dtype=np.uint8)[None, :]
    return img
