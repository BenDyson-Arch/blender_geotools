"""Tests for geotools.core.gltf_writer, including rusty-tiles admission.

The rusty-tiles tests are skipped when the wheel is not installed. Images are
made at test time: PNG with zlib, JPEG with a tiny baseline encoder below (flat
8x8 blocks), so no imaging library is needed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import struct
import zipfile
import zlib
from pathlib import Path

import numpy as np
import pytest

from geotools.core.facemap import build_face_map
from geotools.core.gltf_writer import (
    ExportError,
    ImageData,
    MaterialData,
    PrimitiveData,
    SceneExport,
    TextureRef,
    write_glb,
)

# --------------------------------------------------------------------------- helpers


def grid(
    n: int,
    *,
    material: int | None = None,
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0),
    normals: bool = False,
    tangents: bool = False,
    uv_sets: int = 0,
    color: bool = False,
    obj: int = 0,
) -> PrimitiveData:
    """An n x n grid of quads in Blender's XY plane, two triangles per quad.

    The surface is tilted a little so every axis has a real range."""
    ys, xs = np.mgrid[0 : n + 1, 0 : n + 1].astype(np.float32)
    x = xs.ravel() + origin[0]
    y = ys.ravel() * 1.5 + origin[1]
    z = 0.25 * xs.ravel() - 0.5 * ys.ravel() + origin[2]
    positions = np.stack([x, y, z], axis=1).astype(np.float32)
    quad = (np.arange(n)[None, :] + (n + 1) * np.arange(n)[:, None]).ravel()
    a, b, c, d = quad, quad + 1, quad + n + 1, quad + n + 2
    indices = np.stack([np.stack([a, b, d], 1), np.stack([a, d, c], 1)], 1).reshape(-1, 3)
    indices = indices.astype(np.uint32)
    vertices = positions.shape[0]
    rng = np.random.default_rng(n * 7 + vertices)
    kwargs: dict = {}
    if normals:
        nrm = rng.normal(size=(vertices, 3))
        kwargs["normals"] = (nrm / np.linalg.norm(nrm, axis=1, keepdims=True)).astype(np.float32)
    if tangents:
        # Unit tangents with one handedness, as one tangent space per face.
        tan = np.zeros((vertices, 4), np.float32)
        tan[:, 0] = 1.0
        tan[:, 3] = -1.0
        kwargs["tangents"] = tan
    if uv_sets:
        uv0 = np.stack([xs.ravel() / n, ys.ravel() / n], 1).astype(np.float32)
        uv1 = rng.uniform(-0.5, 2.0, size=(vertices, 2)).astype(np.float32)
        kwargs["texcoords"] = (uv0, uv1)[:uv_sets]
    if color:
        kwargs["color"] = rng.uniform(0.0, 1.0, size=(vertices, 4)).astype(np.float32)
    triangles = indices.shape[0]
    return PrimitiveData(
        material=material,
        positions=positions,
        indices=indices,
        source_object=np.full(triangles, obj, np.int32),
        source_polygon=(np.arange(triangles) // 2).astype(np.int32),
        **kwargs,
    )


def scene_of(*prims: PrimitiveData, materials=(), images=(), names=("a", "b", "c")) -> SceneExport:
    return SceneExport(
        object_names=tuple(names),
        materials=tuple(materials),
        images=tuple(images),
        primitives=tuple(prims),
        generator="GeoTools test",
    )


def write_png(path: Path, rgba: np.ndarray, bit_depth: int = 8) -> Path:
    """Minimal PNG encoder (RGBA, filter 0)."""
    h, w, _ = rgba.shape

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    sample = ">u2" if bit_depth == 16 else "u1"
    rows = np.ascontiguousarray(rgba.astype(sample)).reshape(h, -1).view("u1")
    raw = b"".join(b"\0" + row.tobytes() for row in rows)
    ihdr = struct.pack(">IIBBBBB", w, h, bit_depth, 6, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    return path


def write_jpeg(path: Path, blocks: np.ndarray) -> Path:
    """Minimal baseline greyscale JPEG: each 8x8 block is one flat grey level.

    Quantisation is all ones, so a block of level v has DC = 8 * (v - 128) and
    no AC terms. DC table: twelve 4-bit codes (categories 0-11); AC table: one
    1-bit code for end-of-block."""
    rows, cols = blocks.shape
    out = bytearray(b"\xff\xd8")
    out += b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + b"\x01" * 64
    out += b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, rows * 8, cols * 8, 1) + b"\x01\x11\x00"
    dc = b"\x00" + bytes([0, 0, 0, 12] + [0] * 12) + bytes(range(12))
    ac = b"\x10" + bytes([1] + [0] * 15) + b"\x00"
    out += b"\xff\xc4" + struct.pack(">H", 2 + len(dc) + len(ac)) + dc + ac
    out += b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00" + b"\x00\x3f\x00"
    bits = []
    previous = 0
    for level in blocks.ravel():
        value = 8 * (int(level) - 128)
        diff, previous = value - previous, value
        category = abs(diff).bit_length()
        bits += [int(c) for c in format(category, "04b")]
        if category:
            extra = diff if diff > 0 else diff - 1 + (1 << category)
            bits += [int(c) for c in format(extra, f"0{category}b")]
        bits.append(0)  # end of block
    bits += [1] * (-len(bits) % 8)
    for i in range(0, len(bits), 8):
        byte = int("".join(map(str, bits[i : i + 8])), 2)
        out.append(byte)
        if byte == 0xFF:
            out.append(0)
    out += b"\xff\xd9"
    path.write_bytes(bytes(out))
    return path


def read_glb(path: Path) -> tuple[dict, bytes]:
    """Test-only GLB reader that also checks the container rules."""
    data = Path(path).read_bytes()
    magic, version, length = struct.unpack_from("<III", data, 0)
    assert (magic, version, length) == (0x46546C67, 2, len(data))
    json_length, json_type = struct.unpack_from("<II", data, 12)
    assert json_type == 0x4E4F534A and json_length % 4 == 0
    raw_json = data[20 : 20 + json_length]
    assert raw_json.rstrip(b" ").endswith(b"}")  # padded with spaces only
    document = json.loads(raw_json)
    bin_offset = 20 + json_length
    bin_length, bin_type = struct.unpack_from("<II", data, bin_offset)
    assert bin_type == 0x004E4942 and bin_length % 4 == 0
    assert bin_offset + 8 + bin_length == len(data)
    binary = data[bin_offset + 8 :]
    assert document["buffers"] == [{"byteLength": bin_length}]
    return document, binary


def accessor(document: dict, binary: bytes, index: int) -> np.ndarray:
    acc = document["accessors"][index]
    view = document["bufferViews"][acc["bufferView"]]
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[acc["type"]]
    dtype = {5126: "<f4", 5125: "<u4"}[acc["componentType"]]
    assert view["byteLength"] == acc["count"] * width * 4
    values = np.frombuffer(binary, dtype, count=acc["count"] * width, offset=view["byteOffset"])
    return values.reshape(acc["count"], width) if width > 1 else values


def all_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(all_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(all_keys(v) for v in value)) if value else set()
    return set()


# ------------------------------------------------------------------ layout and conversion


def test_layout_axis_and_uv_conversion(tmp_path: Path) -> None:
    prim = grid(3, normals=True, tangents=True, uv_sets=2, color=True)
    scene = scene_of(prim)
    result = write_glb(scene, tmp_path / "s.glb")
    document, binary = read_glb(tmp_path / "s.glb")

    assert document["asset"] == {"version": "2.0", "generator": "GeoTools test"}
    assert document["scene"] == 0 and document["scenes"] == [{"nodes": [0]}]
    assert document["nodes"] == [{"mesh": 0}]
    assert len(document["meshes"]) == 1
    assert not all_keys(document) & {"extras", "extensions", "extensionsUsed", "uri"}
    (entry,) = document["meshes"][0]["primitives"]
    assert entry["mode"] == 4 and "material" not in entry
    attributes = entry["attributes"]
    assert list(attributes) == [
        "POSITION",
        "NORMAL",
        "TANGENT",
        "TEXCOORD_0",
        "TEXCOORD_1",
        "COLOR_0",
    ]

    def blender_to_gltf(v: np.ndarray) -> np.ndarray:
        return np.stack([v[:, 0], v[:, 2], np.float32(0.0) - v[:, 1]], axis=1)

    position = accessor(document, binary, attributes["POSITION"])
    np.testing.assert_array_equal(position, blender_to_gltf(prim.positions))
    acc = document["accessors"][attributes["POSITION"]]
    assert acc["min"] == position.min(axis=0).tolist()
    assert acc["max"] == position.max(axis=0).tolist()
    np.testing.assert_array_equal(
        accessor(document, binary, attributes["NORMAL"]), blender_to_gltf(prim.normals)
    )
    tangent = accessor(document, binary, attributes["TANGENT"])
    np.testing.assert_array_equal(tangent[:, :3], blender_to_gltf(prim.tangents))
    np.testing.assert_array_equal(tangent[:, 3], prim.tangents[:, 3])
    for s in range(2):
        uv = accessor(document, binary, attributes[f"TEXCOORD_{s}"])
        np.testing.assert_array_equal(uv[:, 0], prim.texcoords[s][:, 0])
        np.testing.assert_array_equal(uv[:, 1], np.float32(1.0) - prim.texcoords[s][:, 1])
    np.testing.assert_array_equal(accessor(document, binary, attributes["COLOR_0"]), prim.color)
    indices = accessor(document, binary, entry["indices"])
    np.testing.assert_array_equal(indices, prim.indices.ravel())
    assert document["accessors"][entry["indices"]]["componentType"] == 5125

    # A Blender point (1, 2, 3) is glTF (1, 3, -2); Blender UV (0.25, 0.75) is (0.25, 0.25).
    single = scene_of(
        PrimitiveData(
            None,
            np.array([[1, 2, 3], [0, 0, 0], [1, 0, 0]], np.float32),
            np.array([[0, 1, 2]], np.uint32),
            texcoords=(np.array([[0.25, 0.75], [0, 0], [1, 0]], np.float32),),
        )
    )
    write_glb(single, tmp_path / "one.glb")
    doc, binary1 = read_glb(tmp_path / "one.glb")
    attrs = doc["meshes"][0]["primitives"][0]["attributes"]
    assert accessor(doc, binary1, attrs["POSITION"])[0].tolist() == [1.0, 3.0, -2.0]
    assert accessor(doc, binary1, attrs["TEXCOORD_0"])[0].tolist() == [0.25, 0.25]
    # Zero northing stays +0.0, as in placement.blender_to_gltf.
    assert np.signbit(accessor(doc, binary1, attrs["POSITION"])[1, 2]) == np.False_

    for view in document["bufferViews"]:
        assert view["byteOffset"] % 4 == 0
        assert view["buffer"] == 0
    assert result.triangles == prim.indices.shape[0]
    assert result.primitives == 1


def test_output_is_deterministic(tmp_path: Path) -> None:
    png = write_png(tmp_path / "t.png", np.full((8, 8, 4), 200, np.uint8))
    scene = scene_of(
        grid(4, material=0, normals=True, uv_sets=1, color=True),
        grid(2, material=1, origin=(10, 0, 0)),
        materials=(
            MaterialData("tex", base_color_texture=TextureRef(0)),
            MaterialData("flat", alpha_mode="MASK", double_sided=True),
        ),
        images=(ImageData("t", png, "image/png"),),
    )
    first = write_glb(scene, tmp_path / "a.glb")
    second = write_glb(scene, tmp_path / "b.glb")
    data = (tmp_path / "a.glb").read_bytes()
    assert data == (tmp_path / "b.glb").read_bytes()
    assert first.sha256 == second.sha256 == hashlib.sha256(data).hexdigest()
    assert first.bytes == len(data)
    assert first.triangles == 2 * 16 + 2 * 4 and first.primitives == 2

    # Strided (non-contiguous) inputs give the same bytes.
    def strided(a: np.ndarray | None) -> np.ndarray | None:
        if a is None:
            return None
        wide = np.zeros((a.shape[0], a.shape[1] * 2) if a.ndim == 2 else (a.shape[0], 2), a.dtype)
        if a.ndim == 2:
            wide[:, ::2] = a
            return wide[:, ::2]
        wide[:, 0] = a
        return wide[:, 0]

    prims = tuple(
        dataclasses.replace(
            p,
            positions=strided(p.positions),
            indices=strided(p.indices),
            normals=strided(p.normals),
            texcoords=tuple(strided(t) for t in p.texcoords),
            color=strided(p.color),
        )
        for p in scene.primitives
    )
    assert not prims[0].positions.flags.c_contiguous
    third = write_glb(dataclasses.replace(scene, primitives=prims), tmp_path / "c.glb")
    assert third.sha256 == first.sha256


def test_write_is_atomic_and_replaces(tmp_path: Path) -> None:
    target = tmp_path / "out" / "s.glb"
    target.parent.mkdir()
    target.write_bytes(b"old")
    write_glb(scene_of(grid(1)), target)
    assert target.read_bytes()[:4] == b"glTF"
    assert sorted(p.name for p in target.parent.iterdir()) == ["s.glb"]

    # A refused scene leaves the old file and no temp file behind.
    bad = scene_of(dataclasses.replace(grid(1), indices=np.zeros((0, 3), np.uint32)))
    before = target.read_bytes()
    with pytest.raises(ExportError):
        write_glb(bad, target)
    assert target.read_bytes() == before
    assert sorted(p.name for p in target.parent.iterdir()) == ["s.glb"]


def test_materials_images_and_samplers(tmp_path: Path) -> None:
    png = write_png(tmp_path / "c.png", np.full((8, 8, 4), 90, np.uint8))
    jpg = write_jpeg(tmp_path / "c.jpg", np.array([[60, 200], [128, 30]]))
    unused = write_png(tmp_path / "unused.png", np.zeros((8, 8, 4), np.uint8))
    materials = (
        MaterialData(
            "jpeg",
            base_color_factor=(0.5, 0.25, 1.0, 1.0),
            base_color_texture=TextureRef(2, 1),
            metallic_factor=0.1,
            roughness_factor=0.9,
            emissive_factor=(0.0, 0.5, 0.0),
        ),
        MaterialData(
            "mask",
            base_color_texture=TextureRef(0),
            alpha_mode="MASK",
            alpha_cutoff=0.25,
            double_sided=True,
        ),
    )
    images = (
        ImageData("c", png, "image/png"),
        ImageData("unused", unused, "image/png"),
        ImageData("j", jpg, "image/jpeg"),
    )
    scene = scene_of(
        grid(2, material=0, uv_sets=2),
        grid(2, material=1, uv_sets=1, origin=(5, 0, 0)),
        materials=materials,
        images=images,
    )
    write_glb(scene, tmp_path / "s.glb")
    document, binary = read_glb(tmp_path / "s.glb")

    # Only referenced images, in SceneExport order; texture t shows image t.
    assert [i["name"] for i in document["images"]] == ["c", "j"]
    assert [i["mimeType"] for i in document["images"]] == ["image/png", "image/jpeg"]
    for image, path in zip(document["images"], (png, jpg), strict=True):
        view = document["bufferViews"][image["bufferView"]]
        assert "target" not in view and "byteStride" not in view
        start = view["byteOffset"]
        assert binary[start : start + view["byteLength"]] == path.read_bytes()
    assert document["textures"] == [{"sampler": 0, "source": 0}, {"sampler": 0, "source": 1}]
    assert document["samplers"] == [
        {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
    ]
    m0, m1 = document["materials"]
    assert m0["pbrMetallicRoughness"] == {
        "baseColorFactor": [0.5, 0.25, 1.0, 1.0],
        "metallicFactor": 0.1,
        "roughnessFactor": 0.9,
        "baseColorTexture": {"index": 1, "texCoord": 1},
    }
    assert m0["emissiveFactor"] == [0.0, 0.5, 0.0]
    assert m0["alphaMode"] == "OPAQUE" and "alphaCutoff" not in m0
    assert m1["alphaMode"] == "MASK" and m1["alphaCutoff"] == 0.25 and m1["doubleSided"]
    assert [p["material"] for p in document["meshes"][0]["primitives"]] == [0, 1]


def test_scene_without_materials_or_images_omits_them(tmp_path: Path) -> None:
    write_glb(scene_of(grid(1)), tmp_path / "s.glb")
    document, _ = read_glb(tmp_path / "s.glb")
    assert not {"materials", "textures", "images", "samplers"} & set(document)


# ----------------------------------------------------------------------------- validation


def _replace(**changes):
    return lambda p: dataclasses.replace(p, **changes)


def _mixed_tangent_signs(p: PrimitiveData) -> PrimitiveData:
    tangents = p.tangents.copy()
    tangents[p.indices[0, 0], 3] = 1.0
    return dataclasses.replace(p, tangents=tangents)


def _with(name, fn):
    return lambda p: dataclasses.replace(p, **{name: fn(getattr(p, name))})


def _set_first(value):
    def change(a: np.ndarray) -> np.ndarray:
        a = a.copy()
        a.flat[0] = value
        return a

    return change


PRIMITIVE_ERRORS = [
    ("positions float64", _with("positions", lambda a: a.astype(np.float64)), "float32"),
    ("positions 2 columns", _with("positions", lambda a: a[:, :2].copy()), "shape"),
    ("indices int32", _with("indices", lambda a: a.astype(np.int32)), "uint32"),
    ("indices flat", _with("indices", lambda a: a.ravel().copy()), "shape"),
    ("index out of range", _with("indices", _set_first(10_000)), "only 16 vertices"),
    ("empty primitive", _with("indices", lambda a: a[:0]), "no triangles"),
    ("NaN position", _with("positions", _set_first(np.nan)), "NaN"),
    ("huge position", _with("positions", _set_first(2e6)), "1000000"),
    ("normals wrong length", _replace(normals=np.zeros((3, 3), np.float32)), r"\(16, 3\)"),
    ("normals not unit", _with("normals", lambda a: a * np.float32(1.01)), "unit length"),
    ("tangents without normals", _replace(normals=None), "tangents need normals"),
    (
        "tangent w not sign",
        _with("tangents", lambda a: a * np.float32([1, 1, 1, 0.5])),
        r"\+1 or -1",
    ),
    (
        "tangent xyz not unit",
        _with("tangents", lambda a: a * np.float32([2, 1, 1, 1])),
        "unit length",
    ),
    ("mixed tangent signs", _mixed_tangent_signs, "mixes tangent w"),
    ("three UV sets", _with("texcoords", lambda t: (t[0], t[0], t[0])), "at most 2"),
    ("UV wrong length", _with("texcoords", lambda t: (t[0][:5],)), r"\(16, 2\)"),
    ("UV NaN", _with("texcoords", lambda t: (_set_first(np.inf)(t[0]),)), "infinite"),
    ("color above 1", _with("color", _set_first(1.5)), r"\[0, 1\]"),
    ("color negative", _with("color", _set_first(-0.1)), r"\[0, 1\]"),
    ("color NaN", _with("color", _set_first(np.nan)), r"\[0, 1\]"),
    ("color 3 columns", _with("color", lambda a: a[:, :3].copy()), "shape"),
    ("material out of range", _replace(material=5), "material 5 is out of range"),
    ("only source_object", _replace(source_polygon=None), "both be given"),
    ("provenance length", _with("source_polygon", lambda a: a[:-1].copy()), r"\(18,\)"),
    ("provenance int64", _with("source_object", lambda a: a.astype(np.int64)), "int32"),
    ("source_object range", _with("source_object", _set_first(3)), "out of range"),
    ("negative polygon", _with("source_polygon", _set_first(-1)), "negative"),
]


@pytest.mark.parametrize(
    ("change", "message"),
    [pytest.param(c, m, id=i) for i, c, m in PRIMITIVE_ERRORS],
)
def test_primitive_validation(tmp_path: Path, change, message: str) -> None:
    base = grid(3, material=0, normals=True, tangents=True, uv_sets=1, color=True)
    scene = scene_of(change(base), materials=(MaterialData("m"),))
    with pytest.raises(ExportError, match=message):
        write_glb(scene, tmp_path / "s.glb")
    assert list(tmp_path.iterdir()) == []


def test_valid_base_scene_for_validation_is_accepted(tmp_path: Path) -> None:
    base = grid(3, material=0, normals=True, tangents=True, uv_sets=1, color=True)
    write_glb(scene_of(base, materials=(MaterialData("m"),)), tmp_path / "s.glb")


@pytest.mark.parametrize(
    ("material", "prim_kwargs", "message"),
    [
        pytest.param(MaterialData("m", alpha_mode="BLEND"), {}, "refuses BLEND", id="blend"),
        pytest.param(
            MaterialData("m", base_color_factor=(1, 1, 1, 1.5)), {}, r"\[0, 1\]", id="factor"
        ),
        pytest.param(MaterialData("m", base_color_factor=(1, 1, 1)), {}, "4 finite", id="width"),
        pytest.param(MaterialData("m", roughness_factor=np.nan), {}, "finite", id="nan"),
        pytest.param(MaterialData("m", alpha_cutoff=-1.0), {}, ">= 0", id="cutoff"),
        pytest.param(MaterialData("m", normal_scale=np.inf), {}, "normal_scale", id="scale"),
        pytest.param(
            MaterialData("m", base_color_texture=TextureRef(3)), {}, "image 3", id="image"
        ),
        pytest.param(
            MaterialData("m", emissive_texture=TextureRef(0, 2)), {}, "0 or 1", id="texcoord"
        ),
        pytest.param(
            MaterialData("m", base_color_texture=TextureRef(0, 1)),
            {"uv_sets": 1},
            "UV set 1",
            id="missing-uv-set",
        ),
        pytest.param(
            MaterialData("m", normal_texture=TextureRef(0)),
            {"uv_sets": 1, "normals": True},
            "normals and tangents",
            id="normal-map-needs-tangents",
        ),
    ],
)
def test_material_validation(tmp_path: Path, material, prim_kwargs, message: str) -> None:
    png = write_png(tmp_path / "t.png", np.zeros((8, 8, 4), np.uint8))
    scene = scene_of(
        grid(2, material=0, **prim_kwargs),
        materials=(material,),
        images=(ImageData("t", png, "image/png"),),
    )
    with pytest.raises(ExportError, match=message):
        write_glb(scene, tmp_path / "s.glb")
    assert not (tmp_path / "s.glb").exists()


def test_image_validation(tmp_path: Path) -> None:
    png = write_png(tmp_path / "ok.png", np.zeros((8, 8, 4), np.uint8))
    jpg = write_jpeg(tmp_path / "ok.jpg", np.array([[100]]))
    wide = write_png(tmp_path / "png16.png", np.zeros((8, 8, 4), np.uint16), bit_depth=16)
    huge = tmp_path / "huge.png"
    with open(huge, "wb") as f:  # sparse; the size is refused before reading
        f.write(png.read_bytes())
        f.truncate(32 * 1024 * 1024 + 1)
    truncated = tmp_path / "truncated.jpg"
    truncated.write_bytes(jpg.read_bytes()[:-2])
    big_header = tmp_path / "big.png"
    data = bytearray(png.read_bytes())
    data[16:24] = struct.pack(">II", 20_000, 8)
    big_header.write_bytes(bytes(data))
    cases = [
        (ImageData("x", tmp_path / "missing.png", "image/png"), "cannot be read"),
        (ImageData("x", png, "image/jpeg"), "not a JPEG"),
        (ImageData("x", jpg, "image/png"), "not a PNG"),
        (ImageData("x", png, "image/webp"), "not PNG or JPEG"),
        (ImageData("x", wide, "image/png"), "16-bit"),
        (ImageData("x", huge, "image/png"), "32 MiB"),
        (ImageData("x", truncated, "image/jpeg"), "end marker"),
        (ImageData("x", big_header, "image/png"), "20000x8"),
        (ImageData("x", tmp_path, "image/png"), "not a regular file"),
    ]
    for image, message in cases:
        scene = scene_of(
            grid(1, material=0, uv_sets=1),
            materials=(MaterialData("m", base_color_texture=TextureRef(0)),),
            images=(image,),
        )
        with pytest.raises(ExportError, match=message):
            write_glb(scene, tmp_path / "s.glb")
    assert not (tmp_path / "s.glb").exists()


def test_scene_validation(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="no primitives"):
        write_glb(scene_of(), tmp_path / "s.glb")
    with pytest.raises(ExportError, match="generator"):
        write_glb(dataclasses.replace(scene_of(grid(1)), generator=None), tmp_path / "s.glb")
    with pytest.raises(ExportError, match="SceneExport"):
        write_glb(object(), tmp_path / "s.glb")  # type: ignore[arg-type]
    assert not (tmp_path / "s.glb").exists()


def test_test_jpeg_encoder_is_wellformed(tmp_path: Path) -> None:
    data = write_jpeg(tmp_path / "x.jpg", np.array([[0, 255], [128, 1]])).read_bytes()
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9"
    assert struct.unpack(">HH", data[data.index(b"\xff\xc0") + 5 :][:4]) == (16, 16)


# --------------------------------------------------------------------- rusty-tiles admission


@pytest.fixture(scope="module")
def rusty_tiles():
    return pytest.importorskip("rusty_tiles")


def tile(rusty_tiles, scene: SceneExport, workdir: Path, profile: str = "balanced"):
    result = write_glb(scene, workdir / "source.glb")
    tiled = rusty_tiles.mesh_to_tileset(
        workdir / "source.glb", workdir / "out.3tz", profile=profile, jobs=1
    )
    assert tiled.report["triangles"] == result.triangles
    validation = rusty_tiles.validate(workdir / "out.3tz")
    assert validation["ok"], validation
    with zipfile.ZipFile(workdir / "out.3tz") as archive:
        conversion = json.loads(archive.read("conversion.json"))
    assert conversion["triangles"] == result.triangles
    return result, tiled


def _images(tmp_path: Path) -> tuple[ImageData, ImageData]:
    colour = np.zeros((8, 8, 4), np.uint8)
    colour[..., 0] = np.arange(8)[None, :] * 30
    colour[..., 1] = np.arange(8)[:, None] * 30
    colour[..., 3] = 255
    normal = np.zeros((8, 8, 4), np.uint8)
    normal[...] = (128, 128, 255, 255)
    images = (
        ImageData("colour", write_png(tmp_path / "colour.png", colour), "image/png"),
        ImageData("normal", write_png(tmp_path / "normal.png", normal), "image/png"),
    )
    return images


COMBINATIONS = {
    "positions": {},
    "normals": {"normals": True},
    "uv0": {"uv_sets": 1},
    "uv0_uv1": {"uv_sets": 2},
    "color": {"color": True},
    "normal_map": {"normals": True, "tangents": True, "uv_sets": 1},
}


@pytest.mark.parametrize("combination", list(COMBINATIONS))
def test_rusty_tiles_admits_each_attribute_combination(
    rusty_tiles, tmp_path: Path, combination: str
) -> None:
    kwargs = COMBINATIONS[combination]
    materials: tuple = ()
    images: tuple = ()
    material = None
    if combination in ("uv0", "uv0_uv1", "normal_map"):
        images = _images(tmp_path)
        material = 0
        materials = (
            MaterialData(
                "textured",
                base_color_texture=TextureRef(0, 1 if combination == "uv0_uv1" else 0),
                normal_texture=TextureRef(1) if combination == "normal_map" else None,
            ),
        )
    scene = scene_of(grid(4, material=material, **kwargs), materials=materials, images=images)
    tile(rusty_tiles, scene, tmp_path)


def test_rusty_tiles_admits_materials_mask_double_sided_png_and_jpeg(
    rusty_tiles, tmp_path: Path
) -> None:
    images = _images(tmp_path)
    jpeg = write_jpeg(tmp_path / "grey.jpg", np.array([[40, 90], [160, 220]]))
    images = (*images, ImageData("grey", jpeg, "image/jpeg"))
    materials = (
        MaterialData("png", base_color_texture=TextureRef(0)),
        MaterialData(
            "jpeg-mask",
            base_color_texture=TextureRef(2),
            alpha_mode="MASK",
            alpha_cutoff=0.4,
            double_sided=True,
        ),
        MaterialData(
            "full",
            base_color_factor=(0.8, 0.7, 0.6, 1.0),
            base_color_texture=TextureRef(0),
            metallic_roughness_texture=TextureRef(0, 1),
            normal_texture=TextureRef(1),
            normal_scale=0.5,
            occlusion_texture=TextureRef(2, 1),
            emissive_texture=TextureRef(0),
            emissive_factor=(0.1, 0.1, 0.1),
        ),
        MaterialData("flat", base_color_factor=(0.2, 0.4, 0.6, 1.0), double_sided=True),
    )
    scene = scene_of(
        grid(3, material=0, uv_sets=1, normals=True, obj=0),
        grid(3, material=1, uv_sets=1, origin=(5, 0, 0), obj=1),
        grid(3, material=2, uv_sets=2, normals=True, tangents=True, origin=(0, 6, 0), obj=2),
        grid(2, material=3, color=True, origin=(5, 6, 1), obj=0),
        grid(2, material=None, origin=(10, 0, 0), obj=1),
        materials=materials,
        images=images,
    )
    result, _ = tile(rusty_tiles, scene, tmp_path)
    assert result.primitives == 5


# ------------------------------------------------- provenance numbering (the face-map premise)
#
# rusty-tiles numbers source triangles as follows (develop at 0.4.0):
# - src/gltf_source/source.rs, Document::scene_primitives (~l.1628): for each
#   node instance, `meshes[instance.mesh].iter().enumerate()` yields
#   `primitive: primitive as u32` = the index into mesh.primitives, and
#   `triangles: p.count / 3`.
# - src/mesh_large/capture.rs (~l.178-281): for each scene primitive,
#   `for triangle in 0..p.triangles` reads that triangle's corners from the
#   indices accessor in order and records
#   `Provenance { node: p.node, mesh: p.mesh, primitive: p.primitive, triangle }`.
# - src/mesh_large/leaves/writer.rs (~l.765-800): each leaf's `source_triangle`
#   property table stores node_index, mesh_index, primitive_index,
#   triangle_index and the corner keys vertex_0..2 for every leaf triangle.
# With one node and one mesh, primitive_index is the SceneExport primitive
# index and triangle_index the row of its `indices`. The test below decodes
# those tables (meshopt v0 ATTRIBUTES, filter NONE) and checks every row,
# including that vertex_0..2 equal our index row.


def _meshopt_bytes(data: bytes, pos: int, count: int) -> tuple[bytearray, int]:
    groups = count // 16
    header = data[pos : pos + (groups + 3) // 4]
    pos += (groups + 3) // 4
    out = bytearray(count)
    for g in range(groups):
        bits = (header[g // 4] >> ((g % 4) * 2)) & 3
        if bits == 0:
            continue
        if bits == 3:
            out[g * 16 : g * 16 + 16] = data[pos : pos + 16]
            pos += 16
            continue
        width = 2 if bits == 1 else 4
        extra = pos + 2 * width
        values = [
            (byte >> shift) & ((1 << width) - 1)
            for byte in data[pos : pos + 2 * width]
            for shift in range(8 - width, -1, -width)
        ]
        for i, enc in enumerate(values):
            if enc == (1 << width) - 1:
                enc = data[extra]
                extra += 1
            out[g * 16 + i] = enc
        pos = extra
    return out, pos


def meshopt_decode_attributes(data: bytes, count: int, stride: int) -> bytes:
    """Test-only decoder for meshoptimizer's vertex codec, version 0."""
    assert data[0] == 0xA0, "only meshopt vertex codec v0 is supported here"
    last = bytearray(data[len(data) - stride :])
    block = min((8192 // stride) & ~15, 256)
    out = bytearray(count * stride)
    pos, done = 1, 0
    while done < count:
        n = min(block, count - done)
        for k in range(stride):
            deltas, pos = _meshopt_bytes(data, pos, (n + 15) & ~15)
            p = last[k]
            for i in range(n):
                v = deltas[i]
                p = (p + ((v >> 1) ^ (-(v & 1) & 0xFF))) & 0xFF
                out[(done + i) * stride + k] = p
        last = out[(done + n - 1) * stride : (done + n) * stride]
        done += n
    assert len(data) - pos == max(stride, 32)
    return bytes(out)


def source_triangle_rows(glb: bytes) -> dict[str, np.ndarray] | None:
    """A leaf's decoded source_triangle table; None for a coarse tile (it has none)."""
    json_length = struct.unpack_from("<I", glb, 12)[0]
    document = json.loads(glb[20 : 20 + json_length])
    binary = glb[20 + json_length + 8 :]
    metadata = document["extensions"]["EXT_structural_metadata"]
    tables = [t for t in metadata["propertyTables"] if t["class"] == "source_triangle"]
    if not tables:
        return None
    (table,) = tables
    schema = metadata["schema"]["classes"]["source_triangle"]["properties"]
    dtypes = {"UINT8": "<u1", "UINT32": "<u4", "UINT64": "<u8"}
    columns = {}
    for name, prop in table["properties"].items():
        view = document["bufferViews"][prop["values"]]
        meshopt = view.get("extensions", {}).get("EXT_meshopt_compression")
        if meshopt is None:
            assert view["buffer"] == 0
            start = view.get("byteOffset", 0)
            raw = binary[start : start + view["byteLength"]]
        else:
            assert meshopt["mode"] == "ATTRIBUTES" and meshopt.get("filter", "NONE") == "NONE"
            start = meshopt.get("byteOffset", 0)
            raw = meshopt_decode_attributes(
                binary[start : start + meshopt["byteLength"]],
                meshopt["count"],
                meshopt["byteStride"],
            )[: view["byteLength"]]
        columns[name] = np.frombuffer(raw, dtypes[schema[name]["componentType"]])
        assert columns[name].shape == (table["count"],)
    return columns


def test_rusty_tiles_source_ids_are_our_primitive_and_row(rusty_tiles, tmp_path: Path) -> None:
    rng = np.random.default_rng(3)
    prims = []
    for p, (n, origin) in enumerate([(6, (0, 0, 0)), (4, (8, 0, 0)), (5, (0, 9, 2))]):
        prim = grid(n, material=p % 2, origin=origin, obj=p)
        # Shuffle triangle rows and rotate corners so row order is not spatial order.
        order = rng.permutation(prim.indices.shape[0])
        indices = np.roll(prim.indices[order], p, axis=1)
        polygons = rng.integers(0, 10_000, size=order.size).astype(np.int32)
        prims.append(dataclasses.replace(prim, indices=indices, source_polygon=polygons))
    scene = scene_of(*prims, materials=(MaterialData("a"), MaterialData("b", double_sided=True)))
    result, _ = tile(rusty_tiles, scene, tmp_path, profile="lossless")
    face_map = build_face_map(scene, {})

    seen = []
    with zipfile.ZipFile(tmp_path / "out.3tz") as archive:
        leaves = [n for n in archive.namelist() if n.endswith(".glb")]
        for name in leaves:
            rows = source_triangle_rows(archive.read(name))
            if rows is None:
                continue
            assert (rows["node_index"] == 0).all() and (rows["mesh_index"] == 0).all()
            primitive = rows["primitive_index"].astype(np.int64)
            triangle = rows["triangle_index"].astype(np.int64)
            for p in np.unique(primitive):
                mask = primitive == p
                expected = scene.primitives[p].indices[triangle[mask]]
                got = np.stack([rows[f"vertex_{c}"][mask] for c in range(3)], axis=1)
                np.testing.assert_array_equal(got, expected)
            objects, polygons = face_map.lookup_many(primitive, triangle)
            np.testing.assert_array_equal(objects, primitive)  # obj == p in this scene
            for p in np.unique(primitive):
                mask = primitive == p
                np.testing.assert_array_equal(
                    polygons[mask], scene.primitives[p].source_polygon[triangle[mask]]
                )
            seen.append(np.stack([primitive, triangle], axis=1))
    seen_rows = np.concatenate(seen)
    # Leaves partition the source: every source triangle is in exactly one leaf.
    assert seen_rows.shape[0] == result.triangles
    assert np.unique(seen_rows, axis=0).shape[0] == result.triangles
