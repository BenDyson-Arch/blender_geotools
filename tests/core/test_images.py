"""Image remedy model and encoded-header facts (core.images)."""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest

from geotools.core.gltf_writer import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE
from geotools.core.images import (
    DOWNSCALE_FALLBACK_EDGE,
    NO_REMEDY,
    ImageRemedy,
    RemedyKind,
    default_max_edge,
    encoded_bit_depth,
    is_lossless_profile,
    jpeg_bit_depth,
    png_bit_depth,
    scaled_size,
)

MIB = 1 << 20


def png(depth: int = 8, color_type: int = 2) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, depth, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def jpeg(precision: int = 8, frame: int = 0xC0) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    sof = bytes((0xFF, frame)) + struct.pack(">HBHHB", 11, precision, 4, 6, 1) + bytes(3)
    return b"\xff\xd8" + app0 + b"\xff\xff" + sof + b"\xff\xda" + bytes(4) + b"\xff\xd9"


# --- remedy model --------------------------------------------------------------------------


def test_no_remedy_is_inactive() -> None:
    assert not NO_REMEDY.active and NO_REMEDY.kind is RemedyKind.NONE
    assert NO_REMEDY.tag() == ""
    assert ImageRemedy("JPEG").kind is RemedyKind.JPEG  # plain strings are accepted


@pytest.mark.parametrize(
    ("remedy", "tag", "lossy"),
    [
        (ImageRemedy(RemedyKind.JPEG), "jpeg-q95", True),
        (ImageRemedy(RemedyKind.JPEG, quality=90), "jpeg-q90", True),
        (ImageRemedy(RemedyKind.DOWNSCALE, max_edge=2048), "downscale-2048", True),
        (ImageRemedy(RemedyKind.CONVERT_8BIT), "8bit", False),
    ],
)
def test_tags_name_the_remedy_and_its_parameters(remedy, tag, lossy) -> None:
    assert remedy.active and remedy.tag() == tag and remedy.lossy is lossy


@pytest.mark.parametrize("quality", [89, 101])
def test_jpeg_quality_range(quality: int) -> None:
    with pytest.raises(ValueError, match="quality"):
        ImageRemedy(RemedyKind.JPEG, quality=quality)


def test_describe() -> None:
    assert ImageRemedy(RemedyKind.JPEG, quality=97).describe() == "re-encoded as JPEG quality 97"
    down = ImageRemedy(RemedyKind.DOWNSCALE, max_edge=4096)
    text = down.describe(8192, 6144)
    assert "4096x3072 as PNG" in text and "JPEG quality 95" in text, text
    assert "own size" in down.describe(1024, 1024)
    assert "at most 4096 px" in down.describe()
    assert ImageRemedy(RemedyKind.CONVERT_8BIT).describe() == "converted to an 8-bit PNG"


@pytest.mark.parametrize(
    ("size", "edge", "expected"),
    [
        ((8192, 8192), 4096, (4096, 4096)),
        ((8192, 4096), 4096, (4096, 2048)),
        ((3000, 9000), 4096, (1365, 4096)),
        ((20000, 1), 16384, (16384, 1)),  # never zero
        ((1024, 512), 4096, (1024, 512)),  # never enlarged
    ],
)
def test_scaled_size(size, edge, expected) -> None:
    assert scaled_size(*size, edge) == expected


@pytest.mark.parametrize(
    ("width", "height", "file_bytes", "expected"),
    [
        (8192, 8192, 100 * MIB, 4096),  # 25 MiB expected at 4096
        (8192, 8192, 150 * MIB, 2048),  # 37.5 MiB at 4096 would not fit
        (8192, 8192, None, DOWNSCALE_FALLBACK_EDGE),  # size unknown
        (3400, 3400, 35 * MIB, 2048),  # below the current size
        (20000, 100, 1 * MIB, 16384),  # only the edge is too large
        (2000, 2000, None, 1024),  # never the current size or larger
        (32768, 32768, 1_000_000 * MIB, 256),  # stops at the minimum
    ],
)
def test_default_max_edge(width, height, file_bytes, expected) -> None:
    edge = default_max_edge(width, height, file_bytes, MAX_IMAGE_BYTES, MAX_IMAGE_EDGE)
    assert edge == expected
    assert edge & (edge - 1) == 0  # a power of two


def test_lossless_profile() -> None:
    assert is_lossless_profile("lossless") and is_lossless_profile("LOSSLESS")
    assert not any(is_lossless_profile(p) for p in ("fast", "balanced", "quality", "", None))


# --- headers -------------------------------------------------------------------------------


@pytest.mark.parametrize("depth", [1, 8, 16])
def test_png_bit_depth(depth: int) -> None:
    assert png_bit_depth(png(depth, color_type=0)) == depth
    assert png_bit_depth(b"not a png" * 4) is None


@pytest.mark.parametrize(("precision", "frame"), [(8, 0xC0), (12, 0xC1), (8, 0xC2)])
def test_jpeg_bit_depth(precision: int, frame: int) -> None:
    assert jpeg_bit_depth(io.BytesIO(jpeg(precision, frame))) == precision


def test_jpeg_without_frame_header() -> None:
    assert jpeg_bit_depth(io.BytesIO(b"\xff\xd8\xff\xda" + bytes(8))) is None
    assert jpeg_bit_depth(io.BytesIO(b"\xff\xd8")) is None
    assert jpeg_bit_depth(io.BytesIO(b"GIF89a")) is None


def test_encoded_bit_depth_as_rusty_tiles_decodes(tmp_path: Path) -> None:
    cases = {
        "rgb8.png": (png(8, 2), 8),
        "rgb16.png": (png(16, 2), 16),
        "grey16.png": (png(16, 0), 16),
        "palette4.png": (png(4, 3), 8),  # palettes and low depths expand to 8 bits
        "grey1.png": (png(1, 0), 8),
        "photo.jpg": (jpeg(8), 8),
        "deep.jpg": (jpeg(12, 0xC1), 12),
    }
    for name, (data, expected) in cases.items():
        (tmp_path / name).write_bytes(data)
        assert encoded_bit_depth(str(tmp_path / name)) == expected, name
    assert encoded_bit_depth(str(tmp_path / "missing.png")) is None
