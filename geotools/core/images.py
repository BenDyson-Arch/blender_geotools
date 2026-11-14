"""Image remediation choices and encoded-image header facts (bpy-free).

rusty-tiles 0.4.1 refuses a source image that is over 1 GiB encoded, over
16384 px a side, or not 8 bits per decoded channel (``admitted_decoder`` in
``src/gltf_source/source/texture.rs`` accepts only the ``L8``, ``La8``, ``Rgb8``
and ``Rgba8`` colour types after the PNG decoder expands palettes and low bit
depths). Below 1 GiB it admits an image by memory budget (#338; 0.4.0 refused
anything over 32 MiB). For the refusals, GeoTools offers an explicit, per-image
remedy. A remedy is stored, never
chosen for the user, and only changes the file written for the export; the
user's image, its file and its pixels are never touched.

* ``JPEG``: re-encode as JPEG at ``quality`` (90 to 100). Lossy; in the lossless
  build profile the loss is not otherwise there, so checks warn about it.
* ``DOWNSCALE``: scale so the longest side is at most ``max_edge``, written as
  PNG, or as JPEG at ``quality`` if the PNG is still over the 1 GiB limit.
* ``CONVERT_8BIT``: write the same pixels as an 8-bit PNG.

Every remedy writes 8-bit output, so each one also clears a 16-bit refusal.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import StrEnum
from typing import BinaryIO

JPEG_QUALITY_DEFAULT = 95
JPEG_QUALITY_MIN = 90
JPEG_QUALITY_MAX = 100
DOWNSCALE_FALLBACK_EDGE = 4096
"""Downscale default when the encoded size cannot be estimated."""
DOWNSCALE_MIN_EDGE = 256
ESTIMATE_MARGIN = 0.9
"""A downscale default must be estimated to fit in this share of the size limit."""

LOSSLESS_PROFILE = "lossless"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_FRAMES = frozenset(
    (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)
)


class RemedyKind(StrEnum):
    NONE = "NONE"
    JPEG = "JPEG"
    DOWNSCALE = "DOWNSCALE"
    CONVERT_8BIT = "CONVERT_8BIT"


@dataclass(frozen=True, slots=True)
class ImageRemedy:
    kind: RemedyKind = RemedyKind.NONE
    quality: int = JPEG_QUALITY_DEFAULT  # JPEG, and the DOWNSCALE JPEG fallback
    max_edge: int = DOWNSCALE_FALLBACK_EDGE  # DOWNSCALE only

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RemedyKind(self.kind))
        if not JPEG_QUALITY_MIN <= self.quality <= JPEG_QUALITY_MAX:
            raise ValueError(
                f"JPEG quality must be {JPEG_QUALITY_MIN} to {JPEG_QUALITY_MAX}, got {self.quality}"
            )
        if self.max_edge < 1:
            raise ValueError(f"max_edge must be positive, got {self.max_edge}")

    @property
    def active(self) -> bool:
        return self.kind is not RemedyKind.NONE

    def tag(self) -> str:
        """Deterministic file-name part naming the remedy and its parameters."""
        if self.kind is RemedyKind.JPEG:
            return f"jpeg-q{self.quality}"
        if self.kind is RemedyKind.DOWNSCALE:
            return f"downscale-{self.max_edge}"
        if self.kind is RemedyKind.CONVERT_8BIT:
            return "8bit"
        return ""

    def describe(self, width: int = 0, height: int = 0) -> str:
        """What the remedy does, as a phrase: "re-encoded as JPEG quality 95"."""
        if self.kind is RemedyKind.JPEG:
            return f"re-encoded as JPEG quality {self.quality}"
        if self.kind is RemedyKind.DOWNSCALE:
            if width and height:
                w, h = scaled_size(width, height, self.max_edge)
                size = f"{w}x{h}" if (w, h) != (width, height) else "its own size (no smaller)"
            else:
                size = f"at most {self.max_edge} px a side"
            return (
                f"downscaled to {size} as PNG, or as JPEG quality {self.quality} if the PNG "
                "is still over rusty-tiles' 1 GiB limit"
            )
        if self.kind is RemedyKind.CONVERT_8BIT:
            return "converted to an 8-bit PNG"
        return "used as is"

    @property
    def lossy(self) -> bool:
        """Whether the published pixels can differ from the source's (at 8 bits)."""
        return self.kind in (RemedyKind.JPEG, RemedyKind.DOWNSCALE)


NO_REMEDY = ImageRemedy()


def is_lossless_profile(profile: str | None) -> bool:
    return (profile or "").strip().lower() == LOSSLESS_PROFILE


def scaled_size(width: int, height: int, max_edge: int) -> tuple[int, int]:
    """``(width, height)`` scaled so the longest side is ``max_edge``; never enlarged."""
    longest = max(width, height)
    if longest <= max_edge:
        return width, height
    if width >= height:
        return max_edge, max(1, round(height * max_edge / width))
    return max(1, round(width * max_edge / height)), max_edge


def default_max_edge(
    width: int, height: int, file_bytes: int | None, byte_limit: int, edge_limit: int
) -> int:
    """The downscale default: the largest power of two that fits, otherwise 4096.

    "Fits" means below the current longest side, at most ``edge_limit``, and an
    encoded size estimated at no more than :data:`ESTIMATE_MARGIN` of
    ``byte_limit``. The estimate scales ``file_bytes`` with the pixel count, so
    it is only made when the file used today is known; otherwise the default is
    :data:`DOWNSCALE_FALLBACK_EDGE`. Export checks the real size either way.
    """
    longest = max(width, height)
    if longest <= 1:
        return DOWNSCALE_FALLBACK_EDGE
    edge = 1 << (min(longest - 1, edge_limit).bit_length() - 1)  # power of two < longest
    if file_bytes is None:
        return min(DOWNSCALE_FALLBACK_EDGE, edge)
    while edge > DOWNSCALE_MIN_EDGE:
        w, h = scaled_size(width, height, edge)
        if file_bytes * (w * h) / (width * height) <= ESTIMATE_MARGIN * byte_limit:
            break
        edge //= 2
    return edge


# --- encoded headers ----------------------------------------------------------------------


def png_bit_depth(head: bytes) -> int | None:
    """Bits per sample from a PNG's IHDR (the first 33 bytes), or None if not a PNG."""
    if len(head) < 25 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        return None
    return head[24]


def jpeg_bit_depth(f: BinaryIO) -> int | None:
    """Sample precision from a JPEG's frame header, or None if none is found."""
    f.seek(0)
    if f.read(2) != b"\xff\xd8":
        return None
    while True:
        marker = f.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None
        code = marker[1]
        if code == 0xFF:  # fill byte
            f.seek(-1, 1)
            continue
        if code in (0x01, *range(0xD0, 0xD8)):  # no length
            continue
        if code in (0xD9, 0xDA):  # end, or a scan before any frame header
            return None
        raw = f.read(2)
        if len(raw) < 2:
            return None
        (length,) = struct.unpack(">H", raw)
        if code in _JPEG_FRAMES:
            precision = f.read(1)
            return precision[0] if precision else None
        f.seek(length - 2, 1)


def encoded_bit_depth(path: str) -> int | None:
    """Bits per channel of a PNG or JPEG file as rusty-tiles decodes it, if known.

    PNG palettes and 1, 2 and 4-bit samples decode to 8 bits; only 16-bit PNGs
    and non-8-bit JPEGs decode to something rusty-tiles refuses.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(33)
            if head.startswith(_PNG_SIGNATURE):
                depth = png_bit_depth(head)
                return None if depth is None else max(depth, 8)
            return jpeg_bit_depth(f)
    except OSError:
        return None
