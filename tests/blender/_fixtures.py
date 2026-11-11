"""Source files generated at test time (no binary fixtures in the repository).

Each writer takes coordinates in the file's own axes, so the tests can check
how the importer maps them into Blender.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np

Vec3 = tuple[float, float, float]

# One triangle, deliberately asymmetric so every axis swap and sign shows.
TRIANGLE: tuple[Vec3, Vec3, Vec3] = ((1.0, 2.0, 3.0), (4.0, 2.0, 3.0), (1.0, 5.0, 7.0))


def png_rgba(width: int = 2, height: int = 2, alpha: int = 128) -> bytes:
    """A tiny RGBA PNG, written by hand."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes((200, 100, 50, alpha)) * width
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _png(width: int, height: int, depth: int, color_type: int, rows: bytes, level: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, depth, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, level))
        + chunk(b"IEND", b"")
    )


def png_noise(width: int, height: int, seed: int = 0) -> bytes:
    """An 8-bit RGB PNG of uniform noise, which deflate cannot shrink.

    3400 x 3400 is just over 32 MiB encoded (34.9 MiB) and takes well under a
    second to write.
    """
    rng = np.random.default_rng(seed)
    px = rng.integers(0, 256, (height, width * 3), dtype=np.uint8)
    rows = np.concatenate([np.zeros((height, 1), np.uint8), px], axis=1).tobytes()
    return _png(width, height, 8, 2, rows, level=1)


def samples16(width: int = 8, height: int = 8, seed: int = 0) -> np.ndarray:
    """The (height, width, 3) uint16 samples :func:`png16` writes, top row first."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 65536, (height, width, 3), dtype=np.uint32).astype(np.uint16)


def png16(width: int = 8, height: int = 8, seed: int = 0) -> bytes:
    """A 16-bit RGB PNG (big-endian samples), which rusty-tiles refuses."""
    px = samples16(width, height, seed).reshape(height, width * 3).astype(">u2")
    rows = b"".join(b"\x00" + px[r].tobytes() for r in range(height))
    return _png(width, height, 16, 2, rows, level=6)


def _pad(data: bytes, fill: bytes = b"\x00") -> bytes:
    return data + fill * (-len(data) % 4)


def write_glb(
    path: Path,
    triangles: list[tuple[Vec3, Vec3, Vec3]],
    alpha_modes: list[str] | None = None,
) -> None:
    """A GLB with one primitive per triangle, positions in glTF (Y-up) axes.

    With ``alpha_modes``, primitive *i* gets its own material with that
    ``alphaMode`` (``OPAQUE``, ``MASK`` or ``BLEND``) and a base-colour texture
    whose alpha is 0.5, so the importer has an alpha to wire up.
    """
    bin_parts: list[bytes] = []
    buffer_views: list[dict] = []
    accessors: list[dict] = []
    offset = 0

    def add_view(data: bytes) -> int:
        nonlocal offset
        data = _pad(data)
        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data)})
        bin_parts.append(data)
        offset += len(data)
        return len(buffer_views) - 1

    textured = alpha_modes is not None
    primitives = []
    for i, tri in enumerate(triangles):
        positions = np.array(tri, dtype=np.float32)
        accessors.append(
            {
                "bufferView": add_view(positions.tobytes()),
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
                "min": positions.min(axis=0).tolist(),
                "max": positions.max(axis=0).tolist(),
            }
        )
        attributes = {"POSITION": len(accessors) - 1}
        if textured:
            uvs = np.array(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)), dtype=np.float32)
            accessors.append(
                {
                    "bufferView": add_view(uvs.tobytes()),
                    "componentType": 5126,
                    "count": 3,
                    "type": "VEC2",
                }
            )
            attributes["TEXCOORD_0"] = len(accessors) - 1
        primitive: dict = {"attributes": attributes}
        if textured:
            primitive["material"] = i
        primitives.append(primitive)

    gltf: dict = {
        "asset": {"version": "2.0", "generator": "geotools tests"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "Source"}],
        "meshes": [{"name": "Source", "primitives": primitives}],
        "accessors": accessors,
        "bufferViews": buffer_views,
    }
    if textured:
        image_view = add_view(png_rgba())
        gltf["images"] = [{"bufferView": image_view, "mimeType": "image/png", "name": "Texture"}]
        gltf["textures"] = [{"source": 0}]
        gltf["materials"] = [
            {
                "name": f"Mat{mode.title()}",
                "alphaMode": mode,
                "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}},
            }
            for mode in alpha_modes
        ]
    gltf["buffers"] = [{"byteLength": offset}]

    json_chunk = _pad(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    bin_chunk = b"".join(bin_parts)
    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    with Path(path).open("wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<I4s", len(json_chunk), b"JSON") + json_chunk)
        f.write(struct.pack("<I4s", len(bin_chunk), b"BIN\x00") + bin_chunk)


def write_obj(path: Path, triangles: list[tuple[Vec3, Vec3, Vec3]]) -> None:
    lines = ["# geotools test"]
    for tri in triangles:
        lines += [f"v {x!r} {y!r} {z!r}" for x, y, z in tri]
    lines += [f"f {3 * i + 1} {3 * i + 2} {3 * i + 3}" for i in range(len(triangles))]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ply(path: Path, triangles: list[tuple[Vec3, Vec3, Vec3]]) -> None:
    n = len(triangles)
    lines = [
        "ply",
        "format ascii 1.0",
        f"element vertex {3 * n}",
        "property float x",
        "property float y",
        "property float z",
        f"element face {n}",
        "property list uchar int vertex_indices",
        "end_header",
    ]
    for tri in triangles:
        lines += [f"{x!r} {y!r} {z!r}" for x, y, z in tri]
    lines += [f"3 {3 * i} {3 * i + 1} {3 * i + 2}" for i in range(n)]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_offset(path: Path, e: str, n: str, a: str) -> None:
    Path(path).write_text(f"E: {e}\nN: {n}\nA: {a}\n", encoding="utf-8")
