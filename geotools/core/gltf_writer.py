"""Deterministic GLB writer for rusty-tiles mesh input. CONTRACT (M2).

The Blender side extracts plain numpy arrays (``SceneExport``); this module
writes them as one standard glTF 2.0 GLB that rusty-tiles' strict mesh input
admits:

- one scene, ONE node (identity transform), ONE mesh; one primitive per
  ``PrimitiveData``, in the given order. rusty-tiles' ``source_primitive`` is
  therefore the index into ``SceneExport.primitives`` and ``source_triangle``
  the row of ``PrimitiveData.indices``. This is what the face map relies on;
- attributes only POSITION, NORMAL, TANGENT, TEXCOORD_0/1, COLOR_0;
- core PBR materials only; images embedded as PNG or JPEG buffer views;
- no extras, no extensions, no animation, no data URIs;
- positions are converted from Blender Z-up to glTF Y-up (x, z, -y); normals and
  tangents likewise; UV v is flipped (glTF v = 1 - Blender v);
- byte-identical output for identical input (no timestamps, fixed key order).

The metadata GLB (M4, ``core.gltf_metadata``) is the one exception to "no
extensions": ``write_glb(..., extension=...)`` hands the laid-out document to a
:class:`GlbExtension`, which adds extensions, buffer views, images and nodes.
Primitives may then carry ``custom_attributes`` (``_FEATURE_ID_0``, ``_SLOPE``,
...), and every buffer view and the BIN chunk start on 8 bytes, as
``EXT_structural_metadata`` asks. Without an extension the output is exactly
the rusty-tiles input described here, and custom attributes are refused.

Implementation choices (M2):

- Empty primitives (no triangles) are REFUSED, never skipped. rusty-tiles
  refuses zero-count accessors, and skipping would make the glTF primitive
  index differ from the ``SceneExport.primitives`` index the face map uses. The
  exporter drops material slots that own no faces.
- Indices are always UNSIGNED_INT (uint32): the input is already uint32, so it
  is written without a copy, and a 10M-triangle source needs 32 bits anyway.
- Every attribute is float32 (COLOR_0 is a float32 VEC4), one tightly packed
  buffer view each; POSITION carries exact float32 ``min``/``max``.
- Tangent W is kept: the axis change is a proper rotation, and the v flip is
  cancelled by glTF's "+Y up" normal-map convention, so ``w * cross(N, T)``
  still points towards increasing Blender v (image up), as glTF expects.
- Images are written once each, in ``SceneExport.images`` order, but only those
  some material references; each has one texture. All textures share one
  sampler: LINEAR magnification, LINEAR_MIPMAP_LINEAR minification, REPEAT on
  both axes. That is Blender's "Linear"/"Repeat" image node and also what
  rusty-tiles assumes for an omitted sampler, written out for other readers.
- The BIN chunk is written sequentially: per primitive the indices, POSITION,
  NORMAL, TANGENT, TEXCOORD_0/1 and COLOR_0, then the images, each view
  4-byte aligned. Converted attributes are produced in row chunks and image
  files are streamed in 1 MiB pieces, so nothing is held twice in memory.

Validation mirrors rusty-tiles 0.4.0 mesh admission (``src/gltf_source``) so a
scene it would refuse fails here with an ``ExportError`` naming the primitive,
material or image, before any file is created.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class TextureRef:
    image: int  # index into SceneExport.images
    texcoord: int = 0  # 0 or 1


@dataclass(frozen=True, slots=True)
class MaterialData:
    name: str
    base_color_factor: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    base_color_texture: TextureRef | None = None
    metallic_factor: float = 0.0
    roughness_factor: float = 0.5
    metallic_roughness_texture: TextureRef | None = None  # G = roughness, B = metallic
    normal_texture: TextureRef | None = None
    normal_scale: float = 1.0
    emissive_factor: tuple[float, float, float] = (0.0, 0.0, 0.0)
    emissive_texture: TextureRef | None = None
    occlusion_texture: TextureRef | None = None
    alpha_mode: str = "OPAQUE"  # "OPAQUE" or "MASK" (rusty-tiles refuses BLEND)
    alpha_cutoff: float = 0.5
    double_sided: bool = False


@dataclass(frozen=True, slots=True)
class ImageData:
    name: str
    path: Path  # an existing PNG or JPEG file; streamed into the GLB
    mime_type: str  # "image/png" or "image/jpeg"


@dataclass(frozen=True, slots=True)
class PrimitiveData:
    material: int | None  # index into SceneExport.materials
    positions: np.ndarray  # (V, 3) float32, Blender world space, Z-up
    indices: np.ndarray  # (T, 3) uint32
    normals: np.ndarray | None = None  # (V, 3) float32, unit length
    tangents: np.ndarray | None = None  # (V, 4) float32, w = +-1 handedness
    texcoords: tuple[np.ndarray, ...] = ()  # up to 2 x (V, 2) float32, Blender UV
    color: np.ndarray | None = None  # (V, 4) float32, LINEAR, in [0, 1]
    # Provenance per triangle (rows of ``indices``), for the face map.
    source_object: np.ndarray | None = None  # (T,) int32 index into object_names
    source_polygon: np.ndarray | None = None  # (T,) int32 Blender polygon index
    # Metadata GLB only (needs ``write_glb(..., extension=...)``): extra SCALAR
    # vertex attributes, (name, (V,) array of float32, int8, uint8, int16 or
    # uint16), written after COLOR_0 in this order. Names start with "_".
    custom_attributes: tuple[tuple[str, np.ndarray], ...] = ()
    # The UV map of each texcoord set, when the exporter knows it (not written).
    texcoord_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SceneExport:
    object_names: tuple[str, ...]
    materials: tuple[MaterialData, ...]
    images: tuple[ImageData, ...]
    primitives: tuple[PrimitiveData, ...]
    generator: str = "GeoTools"


@dataclass(frozen=True, slots=True)
class GlbWriteResult:
    path: Path
    sha256: str
    bytes: int
    primitives: int
    triangles: int


class ExportError(ValueError):
    """The export data is inconsistent or would be refused by rusty-tiles."""


# rusty-tiles 0.4.1 admission limits (src/gltf_source/source.rs, the LARGE
# profile used by mesh_to_tileset, and src/mesh_large/images.rs).
MAX_COORDINATE = 1_000_000.0  # POSITION and TEXCOORD magnitude
UNIT_TOLERANCE = 1e-4  # | |NORMAL| - 1 |, | |TANGENT.xyz| - 1 |
MAX_PRIMITIVES = 65_536
MAX_MATERIALS = 4096
MAX_IMAGES = 4096
MAX_IMAGE_BYTES = 1 << 30  # encoded bytes per image: rusty-tiles 0.4.1's sanity ceiling
# Below MAX_IMAGE_BYTES, rusty-tiles 0.4.1 admits an image when the memory budget
# holds its encoded bytes and its decode (#338); one it cannot hold fails the
# build with ResourceLimitError, which names the bytes needed.
MAX_IMAGE_EDGE = 16_384


def size_limit_text(size: int) -> str:
    """A byte limit as text: "1 GiB", "32 MiB" (whole GiB as GiB, else MiB)."""
    if size >= 1 << 30 and size % (1 << 30) == 0:
        return f"{size >> 30} GiB"
    return f"{size / (1 << 20):g} MiB"


MAX_JSON_BYTES = 16 << 20
MAX_GLB_BYTES = 2**32 - 1  # the GLB header's u32 length

_CHUNK_ROWS = 1 << 18  # rows per conversion/validation chunk
_IMAGE_READ = 1 << 20  # bytes per image read

_FLOAT = 5126
_UNSIGNED_INT = 5125
# Component types of custom SCALAR attributes (numpy dtype -> glTF).
_CUSTOM_COMPONENTS = {
    np.dtype("<f4"): 5126,
    np.dtype("i1"): 5120,
    np.dtype("u1"): 5121,
    np.dtype("<i2"): 5122,
    np.dtype("<u2"): 5123,
}
_ARRAY_BUFFER = 34962
_ELEMENT_ARRAY_BUFFER = 34963
_SAMPLER = {"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497}
_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_CHUNK_BIN = 0x004E4942
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MIME_TYPES = ("image/png", "image/jpeg")
# Texture slots in a fixed order: (MaterialData field, label).
_SLOTS = (
    ("base_color_texture", "base colour"),
    ("metallic_roughness_texture", "metallic-roughness"),
    ("normal_texture", "normal"),
    ("occlusion_texture", "occlusion"),
    ("emissive_texture", "emissive"),
)


class GlbExtension(Protocol):
    """Adds extensions to the laid-out document (the metadata GLB)."""

    def apply(self, layout: GlbLayout) -> None: ...


class GlbLayout:
    """What a :class:`GlbExtension` may change: the document and the BIN chunk.

    ``document`` is the full glTF JSON (``images``, ``textures`` and
    ``samplers`` are added from :attr:`images`, :attr:`textures` and
    :attr:`samplers` afterwards); ``primitives`` its mesh primitives, in
    ``SceneExport.primitives`` order.
    """

    def __init__(self, document: dict, primitives: list[dict], add_view, images, textures):
        self.document = document
        self.primitives = primitives
        self._add_view = add_view
        self.images: list[dict] = images
        self.textures: list[dict] = textures
        self.samplers: list[dict] = [dict(_SAMPLER)] if textures else []
        self.extensions_used: list[str] = []

    def add_view(self, label: str, data: bytes | np.ndarray, *, byte_stride: int = 0) -> int:
        """A buffer view holding ``data`` (bytes, or an array written as it is)."""
        array = np.ascontiguousarray(data) if isinstance(data, np.ndarray) else None
        length = array.nbytes if array is not None else len(data)
        view = self._add_view(label, length, _emit_bytes(data), None)
        if byte_stride:
            self.document["bufferViews"][view]["byteStride"] = int(byte_stride)
        return view

    def add_image(self, name: str, data: bytes, mime_type: str = "image/png") -> int:
        view = self.add_view(f"image {name}", data)
        self.images.append({"name": name, "bufferView": view, "mimeType": mime_type})
        return len(self.images) - 1

    def add_texture(self, image: int, sampler: dict) -> int:
        sampler = dict(sampler)
        if sampler not in self.samplers:
            self.samplers.append(sampler)
        self.textures.append({"sampler": self.samplers.index(sampler), "source": int(image)})
        return len(self.textures) - 1

    def use(self, extension: str) -> None:
        if extension not in self.extensions_used:
            self.extensions_used.append(extension)


def write_glb(
    scene: SceneExport, path: Path, *, extension: GlbExtension | None = None
) -> GlbWriteResult:
    """Validate ``scene`` and write it to ``path`` (atomically: temp then rename).

    With ``extension``, it adds its extensions to the document (see the module
    docstring); without, the output is the plain rusty-tiles input."""
    path = Path(path)
    image_sizes = _validate_scene(scene, custom=extension is not None)
    document, segments, bin_length = _layout(scene, image_sizes, extension)
    json_bytes = json.dumps(
        document, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    json_bytes += b" " * (-len(json_bytes) % 4)
    if extension is not None:
        # The BIN data starts 8-byte aligned in the file (28 bytes of headers).
        json_bytes += b" " * ((4 - len(json_bytes)) % 8)
    if len(json_bytes) > MAX_JSON_BYTES:
        raise ExportError(
            f"The glTF JSON would be {len(json_bytes)} bytes; rusty-tiles accepts at most "
            f"{MAX_JSON_BYTES}. Use fewer primitives or materials."
        )
    total = 12 + 8 + len(json_bytes) + 8 + bin_length
    if total > MAX_GLB_BYTES:
        raise ExportError(
            f"The GLB would be {total} bytes; a GLB holds at most {MAX_GLB_BYTES} (4 GiB)."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            sink = _Sink(f)
            sink.write(struct.pack("<III", _GLB_MAGIC, 2, total))
            sink.write(struct.pack("<II", len(json_bytes), _CHUNK_JSON))
            sink.write(json_bytes)
            sink.write(struct.pack("<II", bin_length, _CHUNK_BIN))
            for segment in segments:
                start = sink.count
                segment.emit(sink)
                if sink.count - start != segment.length:
                    raise ExportError(f"Internal error: {segment.label} wrote the wrong length.")
                sink.write(b"\0" * segment.padding)
            if sink.count != total:
                raise ExportError("Internal error: the GLB length does not match its header.")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes it owner-only
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return GlbWriteResult(
        path=path,
        sha256=sink.hash.hexdigest(),
        bytes=total,
        primitives=len(scene.primitives),
        triangles=sum(int(p.indices.shape[0]) for p in scene.primitives),
    )


# --------------------------------------------------------------------------- layout


@dataclass(frozen=True, slots=True)
class _Segment:
    label: str
    length: int
    emit: Callable[[_Sink], None]
    padding: int = 0


class _Sink:
    """Writes to a file and hashes what it writes."""

    def __init__(self, f) -> None:
        self.f = f
        self.hash = hashlib.sha256()
        self.count = 0

    def write(self, data) -> None:
        view = memoryview(data).cast("B")
        self.f.write(view)
        self.hash.update(view)
        self.count += len(view)


def _layout(
    scene: SceneExport, image_sizes: dict[int, int], extension: GlbExtension | None = None
) -> tuple[dict, list[_Segment], int]:
    accessors: list[dict] = []
    views: list[dict] = []
    segments: list[_Segment] = []
    offset = 0
    align = 4 if extension is None else 8

    def add_view(label: str, length: int, emit, target: int | None) -> int:
        nonlocal offset
        view: dict = {"buffer": 0, "byteOffset": offset, "byteLength": length}
        if target is not None:
            view["target"] = target
        views.append(view)
        segments.append(_Segment(label, length, emit, -length % align))
        offset += length + (-length % align)
        return len(views) - 1

    def add_accessor(label: str, array: np.ndarray, kind: str, emit, target: int, **extra) -> int:
        count = int(array.shape[0])
        width = 1 if kind == "SCALAR" else int(kind[3])
        length = count * width * 4
        component = _UNSIGNED_INT if target == _ELEMENT_ARRAY_BUFFER else _FLOAT
        view = add_view(label, length, emit, target)
        accessors.append(
            {"bufferView": view, "componentType": component, "count": count, "type": kind} | extra
        )
        return len(accessors) - 1

    primitives = []
    for p, prim in enumerate(scene.primitives):
        label = f"primitive {p}"
        indices = add_accessor(
            f"{label} indices",
            prim.indices.reshape(-1),
            "SCALAR",
            _emit_plain(prim.indices, "<u4"),
            _ELEMENT_ARRAY_BUFFER,
        )
        lo, hi = _position_bounds(prim.positions)
        attributes = {
            "POSITION": add_accessor(
                f"{label} POSITION",
                prim.positions,
                "VEC3",
                _emit_yup(prim.positions),
                _ARRAY_BUFFER,
                min=lo,
                max=hi,
            )
        }
        if prim.normals is not None:
            attributes["NORMAL"] = add_accessor(
                f"{label} NORMAL", prim.normals, "VEC3", _emit_yup(prim.normals), _ARRAY_BUFFER
            )
        if prim.tangents is not None:
            attributes["TANGENT"] = add_accessor(
                f"{label} TANGENT", prim.tangents, "VEC4", _emit_yup(prim.tangents), _ARRAY_BUFFER
            )
        for s, uv in enumerate(prim.texcoords):
            attributes[f"TEXCOORD_{s}"] = add_accessor(
                f"{label} TEXCOORD_{s}", uv, "VEC2", _emit_uv(uv), _ARRAY_BUFFER
            )
        if prim.color is not None:
            attributes["COLOR_0"] = add_accessor(
                f"{label} COLOR_0",
                prim.color,
                "VEC4",
                _emit_plain(prim.color, "<f4"),
                _ARRAY_BUFFER,
            )
        for name, values in prim.custom_attributes:
            dtype = values.dtype.newbyteorder("<") if values.dtype.itemsize > 1 else values.dtype
            size = dtype.itemsize
            view = add_view(f"{label} {name}", values.shape[0] * 4, _emit_padded(values), None)
            views[view]["target"] = _ARRAY_BUFFER
            if size < 4:
                views[view]["byteStride"] = 4  # vertex attribute elements align to 4 bytes
            accessors.append(
                {
                    "bufferView": view,
                    "componentType": _CUSTOM_COMPONENTS[np.dtype(dtype)],
                    "count": int(values.shape[0]),
                    "type": "SCALAR",
                }
            )
            attributes[name] = len(accessors) - 1
        entry: dict = {"attributes": attributes, "indices": indices}
        if prim.material is not None:
            entry["material"] = int(prim.material)
        entry["mode"] = 4
        primitives.append(entry)

    # Referenced images only, in SceneExport order; texture t shows image t.
    texture_of = {old: new for new, old in enumerate(sorted(image_sizes))}
    images = []
    for old in sorted(image_sizes):
        image = scene.images[old]
        view = add_view(
            f"image {old}", image_sizes[old], _emit_file(old, image.path, image_sizes[old]), None
        )
        images.append({"name": image.name, "bufferView": view, "mimeType": image.mime_type})

    document: dict = {
        "asset": {"version": "2.0", "generator": scene.generator},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": primitives}],
    }
    if scene.materials:
        document["materials"] = [_material_json(m, texture_of) for m in scene.materials]
    textures = [{"sampler": 0, "source": t} for t in range(len(images))]
    samplers = [dict(_SAMPLER)] if images else []
    extensions_used: list[str] = []
    if extension is not None:
        document["bufferViews"] = views
        layout = GlbLayout(document, primitives, add_view, images, textures)
        extension.apply(layout)
        samplers = layout.samplers
        extensions_used = layout.extensions_used
    if extensions_used:
        document = {"extensionsUsed": list(extensions_used), **document}
    if images:
        document["textures"] = textures
        document["images"] = images
        document["samplers"] = samplers
    document["accessors"] = accessors
    document["bufferViews"] = views
    document["buffers"] = [{"byteLength": offset}]
    return document, segments, offset


def _material_json(m: MaterialData, texture_of: dict[int, int]) -> dict:
    def ref(t: TextureRef) -> dict:
        return {"index": texture_of[int(t.image)], "texCoord": int(t.texcoord)}

    pbr: dict = {
        "baseColorFactor": [float(v) for v in m.base_color_factor],
        "metallicFactor": float(m.metallic_factor),
        "roughnessFactor": float(m.roughness_factor),
    }
    if m.base_color_texture is not None:
        pbr["baseColorTexture"] = ref(m.base_color_texture)
    if m.metallic_roughness_texture is not None:
        pbr["metallicRoughnessTexture"] = ref(m.metallic_roughness_texture)
    out: dict = {"name": m.name, "pbrMetallicRoughness": pbr}
    if m.normal_texture is not None:
        out["normalTexture"] = ref(m.normal_texture) | {"scale": float(m.normal_scale)}
    if m.occlusion_texture is not None:
        out["occlusionTexture"] = ref(m.occlusion_texture)
    if m.emissive_texture is not None:
        out["emissiveTexture"] = ref(m.emissive_texture)
    out["emissiveFactor"] = [float(v) for v in m.emissive_factor]
    out["alphaMode"] = m.alpha_mode
    if m.alpha_mode == "MASK":
        out["alphaCutoff"] = float(m.alpha_cutoff)
    out["doubleSided"] = bool(m.double_sided)
    return out


def _position_bounds(positions: np.ndarray) -> tuple[list[float], list[float]]:
    """Exact float32 glTF-space bounds (rusty-tiles compares them bit for bit)."""
    lo = positions.min(axis=0)
    hi = positions.max(axis=0)
    zero = np.float32(0.0)
    return (
        [float(lo[0]), float(lo[2]), float(zero - hi[1])],
        [float(hi[0]), float(hi[2]), float(zero - lo[1])],
    )


def _row_chunks(n: int) -> Iterator[slice]:
    for start in range(0, n, _CHUNK_ROWS):
        yield slice(start, min(start + _CHUNK_ROWS, n))


def _emit_plain(array: np.ndarray, dtype: str):
    def emit(sink: _Sink) -> None:
        if array.flags.c_contiguous and array.dtype == np.dtype(dtype):
            sink.write(array)  # zero-copy
            return
        for rows in _row_chunks(array.shape[0]):
            sink.write(np.ascontiguousarray(array[rows], dtype=dtype))

    return emit


def _emit_bytes(data: bytes | np.ndarray):
    def emit(sink: _Sink) -> None:
        sink.write(np.ascontiguousarray(data) if isinstance(data, np.ndarray) else data)

    return emit


def _emit_padded(values: np.ndarray):
    """A SCALAR attribute with every element in its own 4 bytes (little-endian)."""
    dtype = values.dtype.newbyteorder("<") if values.dtype.itemsize > 1 else values.dtype
    size = dtype.itemsize

    def emit(sink: _Sink) -> None:
        for rows in _row_chunks(values.shape[0]):
            chunk = np.ascontiguousarray(values[rows], dtype=dtype)
            if size == 4:
                sink.write(chunk)
                continue
            out = np.zeros((chunk.shape[0], 4 // size), dtype=dtype)
            out[:, 0] = chunk
            sink.write(out)

    return emit


def _emit_yup(array: np.ndarray):
    """Blender (x, y, z[, w]) -> glTF (x, z, -y[, w]); 0 - y keeps +0.0 for zero."""

    def emit(sink: _Sink) -> None:
        for rows in _row_chunks(array.shape[0]):
            chunk = array[rows]
            out = np.empty(chunk.shape, dtype="<f4")
            out[:, 0] = chunk[:, 0]
            out[:, 1] = chunk[:, 2]
            np.subtract(np.float32(0.0), chunk[:, 1], out=out[:, 2])
            if chunk.shape[1] == 4:
                out[:, 3] = chunk[:, 3]
            sink.write(out)

    return emit


def _emit_uv(array: np.ndarray):
    """Blender (u, v) -> glTF (u, 1 - v)."""

    def emit(sink: _Sink) -> None:
        for rows in _row_chunks(array.shape[0]):
            chunk = array[rows]
            out = np.empty(chunk.shape, dtype="<f4")
            out[:, 0] = chunk[:, 0]
            np.subtract(np.float32(1.0), chunk[:, 1], out=out[:, 1])
            sink.write(out)

    return emit


def _emit_file(index: int, path: Path, length: int):
    def emit(sink: _Sink) -> None:
        written = 0
        with open(path, "rb") as f:
            while piece := f.read(min(_IMAGE_READ, length - written + 1)):
                written += len(piece)
                if written > length:
                    break
                sink.write(piece)
        if written != length:
            raise ExportError(f"Image {index} ({path}) changed size while it was being written.")

    return emit


# ----------------------------------------------------------------------- validation


def _validate_scene(scene: SceneExport, *, custom: bool = False) -> dict[int, int]:
    """Check everything rusty-tiles would check. Returns {image index: byte size}
    for the images some material references. ``custom``: custom attributes are
    allowed (the metadata GLB)."""
    if not isinstance(scene, SceneExport):
        raise ExportError(f"Expected a SceneExport, got {type(scene).__name__}.")
    if not isinstance(scene.generator, str):
        raise ExportError("SceneExport.generator must be a string.")
    if not all(isinstance(n, str) for n in scene.object_names):
        raise ExportError("SceneExport.object_names must all be strings.")
    if not scene.primitives:
        raise ExportError("The scene has no primitives, so there is nothing to publish.")
    if len(scene.primitives) > MAX_PRIMITIVES:
        raise ExportError(
            f"The scene has {len(scene.primitives)} primitives; rusty-tiles accepts at most "
            f"{MAX_PRIMITIVES}."
        )
    if len(scene.materials) > MAX_MATERIALS:
        raise ExportError(
            f"The scene has {len(scene.materials)} materials; rusty-tiles accepts at most "
            f"{MAX_MATERIALS}."
        )
    if len(scene.images) > MAX_IMAGES:
        raise ExportError(
            f"The scene has {len(scene.images)} images; rusty-tiles accepts at most {MAX_IMAGES}."
        )
    used_images: set[int] = set()
    for m, material in enumerate(scene.materials):
        used_images |= _validate_material(m, material, len(scene.images))
    for p, prim in enumerate(scene.primitives):
        _validate_primitive(p, prim, scene)
        _validate_custom(p, prim, custom)
    return {i: _validate_image(i, scene.images[i]) for i in sorted(used_images)}


def _is_index(value: object) -> bool:
    return isinstance(value, int | np.integer) and not isinstance(value, bool)


def _finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _validate_material(m: int, material: MaterialData, image_count: int) -> set[int]:
    where = f"Material {m}"
    if not isinstance(material, MaterialData):
        raise ExportError(f"{where} is a {type(material).__name__}, not a MaterialData.")
    where = f"Material {m} ({material.name!r})"
    if not isinstance(material.name, str):
        raise ExportError(f"Material {m}: the name must be a string.")
    factors = {
        "base_color_factor": (material.base_color_factor, 4),
        "emissive_factor": (material.emissive_factor, 3),
        "metallic_factor": ((material.metallic_factor,), 1),
        "roughness_factor": ((material.roughness_factor,), 1),
    }
    for name, (values, width) in factors.items():
        values = tuple(values)
        if len(values) != width or not all(_finite_number(v) for v in values):
            raise ExportError(f"{where}: {name} must be {width} finite number(s).")
        if not all(0.0 <= float(v) <= 1.0 for v in values):
            raise ExportError(f"{where}: {name} must be in [0, 1]; rusty-tiles refuses others.")
    if not _finite_number(material.normal_scale):
        raise ExportError(f"{where}: normal_scale must be a finite number.")
    if material.alpha_mode not in ("OPAQUE", "MASK"):
        raise ExportError(
            f"{where}: alpha_mode is {material.alpha_mode!r}; only 'OPAQUE' and 'MASK' are "
            "supported (rusty-tiles refuses BLEND because tiling cannot keep its draw order)."
        )
    if not _finite_number(material.alpha_cutoff) or float(material.alpha_cutoff) < 0.0:
        raise ExportError(f"{where}: alpha_cutoff must be a finite number >= 0.")
    if not isinstance(material.double_sided, bool | np.bool_):
        raise ExportError(f"{where}: double_sided must be a bool.")
    used = set()
    for field_name, label in _SLOTS:
        ref = getattr(material, field_name)
        if ref is None:
            continue
        if not isinstance(ref, TextureRef):
            raise ExportError(f"{where}: the {label} texture must be a TextureRef.")
        if not _is_index(ref.image) or not 0 <= ref.image < image_count:
            raise ExportError(
                f"{where}: the {label} texture refers to image {ref.image}, but the scene has "
                f"{image_count} image(s)."
            )
        if not _is_index(ref.texcoord) or ref.texcoord not in (0, 1):
            raise ExportError(f"{where}: the {label} texture's texcoord must be 0 or 1.")
        used.add(int(ref.image))
    return used


def _check_array(where: str, name: str, array: object, width: int, dtype, rows: int | None):
    if not isinstance(array, np.ndarray):
        raise ExportError(f"{where}: {name} must be a numpy array, got {type(array).__name__}.")
    want = np.dtype(dtype)
    if array.dtype != want:
        raise ExportError(f"{where}: {name} must be {want.name}, got {array.dtype.name}.")
    shape_ok = array.ndim == 2 and array.shape[1] == width if width else array.ndim == 1
    if not shape_ok or (rows is not None and array.shape[0] != rows):
        expected = f"({'V' if rows is None else rows}, {width})" if width else f"({rows},)"
        raise ExportError(f"{where}: {name} must have shape {expected}, got {array.shape}.")


def _all_chunks(array: np.ndarray, test) -> bool:
    return all(bool(test(array[rows])) for rows in _row_chunks(array.shape[0]))


def _check_finite(where: str, name: str, array: np.ndarray) -> None:
    if not _all_chunks(array, lambda c: np.isfinite(c).all()):
        raise ExportError(f"{where}: {name} contains NaN or infinite values.")


def _check_unit(where: str, name: str, xyz_width: np.ndarray) -> None:
    def unit(c: np.ndarray) -> bool:
        length = np.sqrt(np.square(c[:, :3], dtype=np.float64).sum(axis=1))
        return bool((np.abs(length - 1.0) <= UNIT_TOLERANCE).all())

    if not _all_chunks(xyz_width, unit):
        raise ExportError(
            f"{where}: {name} must be unit length (within {UNIT_TOLERANCE}); rusty-tiles "
            "refuses others."
        )


def _validate_primitive(p: int, prim: PrimitiveData, scene: SceneExport) -> None:
    where = f"Primitive {p}"
    if not isinstance(prim, PrimitiveData):
        raise ExportError(f"{where} is a {type(prim).__name__}, not a PrimitiveData.")
    _check_array(where, "positions", prim.positions, 3, np.float32, None)
    _check_array(where, "indices", prim.indices, 3, np.uint32, None)
    vertices = prim.positions.shape[0]
    triangles = prim.indices.shape[0]
    if triangles == 0 or vertices == 0:
        raise ExportError(
            f"{where} has no triangles. Empty primitives are refused rather than skipped, "
            "so primitive indices match the face map; leave unused materials out."
        )
    if int(prim.indices.max()) >= vertices:
        raise ExportError(
            f"{where}: indices refer to vertex {int(prim.indices.max())}, but there are only "
            f"{vertices} vertices."
        )
    _check_finite(where, "positions", prim.positions)
    if not _all_chunks(prim.positions, lambda c: (np.abs(c) <= MAX_COORDINATE).all()):
        raise ExportError(
            f"{where}: a position is beyond +-{MAX_COORDINATE:.0f}; rusty-tiles refuses it. "
            "Move the source nearer the origin and declare the shift as a source offset."
        )

    if prim.material is not None and (
        not _is_index(prim.material) or not 0 <= prim.material < len(scene.materials)
    ):
        raise ExportError(
            f"{where}: material {prim.material!r} is out of range; the scene has "
            f"{len(scene.materials)} material(s)."
        )

    if prim.normals is not None:
        _check_array(where, "normals", prim.normals, 3, np.float32, vertices)
        _check_finite(where, "normals", prim.normals)
        _check_unit(where, "normals", prim.normals)

    if prim.tangents is not None:
        if prim.normals is None:
            raise ExportError(f"{where}: tangents need normals; rusty-tiles refuses them alone.")
        _check_array(where, "tangents", prim.tangents, 4, np.float32, vertices)
        _check_finite(where, "tangents", prim.tangents)
        _check_unit(where, "tangents", prim.tangents)
        w = prim.tangents[:, 3]
        if not _all_chunks(w, lambda c: ((c == 1.0) | (c == -1.0)).all()):
            raise ExportError(f"{where}: tangent w must be exactly +1 or -1.")

        def same_sign(rows: np.ndarray) -> bool:
            signs = w[rows]
            return bool(((signs[:, 0] == signs[:, 1]) & (signs[:, 0] == signs[:, 2])).all())

        if not _all_chunks(prim.indices, same_sign):
            raise ExportError(
                f"{where}: a triangle mixes tangent w signs; rusty-tiles refuses it. Split "
                "vertices by tangent sign as well as by UV."
            )

    if not isinstance(prim.texcoords, tuple | list):
        raise ExportError(f"{where}: texcoords must be a tuple of arrays.")
    if len(prim.texcoords) > 2:
        raise ExportError(
            f"{where} has {len(prim.texcoords)} UV sets; rusty-tiles accepts at most 2."
        )
    for s, uv in enumerate(prim.texcoords):
        name = f"texcoords[{s}]"
        _check_array(where, name, uv, 2, np.float32, vertices)
        _check_finite(where, name, uv)
        # Checked as written: u and 1 - v.
        if not _all_chunks(
            uv,
            lambda c: (
                (np.abs(c[:, 0]) <= MAX_COORDINATE).all()
                and (np.abs(np.float32(1.0) - c[:, 1]) <= MAX_COORDINATE).all()
            ),
        ):
            raise ExportError(f"{where}: {name} has a value beyond +-{MAX_COORDINATE:.0f}.")

    if prim.color is not None:
        _check_array(where, "color", prim.color, 4, np.float32, vertices)
        if not _all_chunks(prim.color, lambda c: ((c >= 0.0) & (c <= 1.0)).all()):
            raise ExportError(
                f"{where}: color must be finite and in [0, 1]; rusty-tiles refuses others."
            )

    if prim.material is not None:
        material = scene.materials[prim.material]
        for field_name, label in _SLOTS:
            ref = getattr(material, field_name)
            if ref is not None and ref.texcoord >= len(prim.texcoords):
                raise ExportError(
                    f"{where}: material {prim.material} ({material.name!r}) samples its {label} "
                    f"texture with UV set {ref.texcoord}, but the primitive has "
                    f"{len(prim.texcoords)} UV set(s)."
                )
        if material.normal_texture is not None and (prim.normals is None or prim.tangents is None):
            raise ExportError(
                f"{where}: material {prim.material} ({material.name!r}) has a normal texture, "
                "which rusty-tiles accepts only with authored normals and tangents."
            )

    has_object = prim.source_object is not None
    if has_object != (prim.source_polygon is not None):
        raise ExportError(
            f"{where}: source_object and source_polygon must both be given or both be None."
        )
    if has_object:
        _check_array(where, "source_object", prim.source_object, 0, np.int32, triangles)
        _check_array(where, "source_polygon", prim.source_polygon, 0, np.int32, triangles)
        if int(prim.source_object.min()) < 0 or int(prim.source_object.max()) >= len(
            scene.object_names
        ):
            raise ExportError(
                f"{where}: source_object is out of range; the scene has "
                f"{len(scene.object_names)} object name(s)."
            )
        if int(prim.source_polygon.min()) < 0:
            raise ExportError(f"{where}: source_polygon has a negative polygon index.")


_RESERVED_ATTRIBUTES = ("POSITION", "NORMAL", "TANGENT", "TEXCOORD_0", "TEXCOORD_1", "COLOR_0")


def _validate_custom(p: int, prim: PrimitiveData, allowed: bool) -> None:
    where = f"Primitive {p}"
    attributes = prim.custom_attributes
    if not attributes:
        return
    if not allowed:
        raise ExportError(
            f"{where} has custom attributes, which rusty-tiles refuses; only the metadata "
            "GLB (an extension) writes them."
        )
    names = set()
    vertices = prim.positions.shape[0]
    for item in attributes:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ExportError(f"{where}: custom_attributes must be (name, array) pairs.")
        name, values = item
        if not isinstance(name, str) or not name.startswith("_") or name in _RESERVED_ATTRIBUTES:
            raise ExportError(f"{where}: custom attribute {name!r} must start with '_'.")
        if name in names:
            raise ExportError(f"{where}: custom attribute {name!r} is given twice.")
        names.add(name)
        if not isinstance(values, np.ndarray) or values.ndim != 1 or values.shape[0] != vertices:
            raise ExportError(f"{where}: custom attribute {name} must be a ({vertices},) array.")
        dtype = values.dtype.newbyteorder("<") if values.dtype.itemsize > 1 else values.dtype
        if np.dtype(dtype) not in _CUSTOM_COMPONENTS:
            raise ExportError(
                f"{where}: custom attribute {name} is {values.dtype}; use float32, int8, "
                "uint8, int16 or uint16."
            )


def _validate_image(i: int, image: ImageData) -> int:
    """Check one image as rusty-tiles would without decoding it; return its size."""
    where = f"Image {i}"
    if not isinstance(image, ImageData):
        raise ExportError(f"{where} is a {type(image).__name__}, not an ImageData.")
    where = f"Image {i} ({image.name!r}, {image.path})"
    if not isinstance(image.name, str):
        raise ExportError(f"Image {i}: the name must be a string.")
    if image.mime_type not in _MIME_TYPES:
        raise ExportError(f"{where}: mime_type {image.mime_type!r} is not PNG or JPEG.")
    try:
        path = Path(image.path)
        size = path.stat().st_size
        if not path.is_file():
            raise ExportError(f"{where} is not a regular file.")
        if size == 0:
            raise ExportError(f"{where} is empty.")
        if size > MAX_IMAGE_BYTES:
            raise ExportError(
                f"{where} is {size} bytes; rusty-tiles accepts encoded images of at most "
                f"{MAX_IMAGE_BYTES} bytes ({size_limit_text(MAX_IMAGE_BYTES)}). Re-encode or "
                "split it."
            )
        with open(path, "rb") as f:
            if image.mime_type == "image/png":
                width, height = _png_header(where, f)
            else:
                width, height = _jpeg_header(where, f, size)
    except OSError as exc:
        raise ExportError(f"{where} cannot be read: {exc}") from exc
    if width == 0 or height == 0:
        raise ExportError(f"{where} has a zero dimension.")
    if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
        raise ExportError(
            f"{where} is {width}x{height}; rusty-tiles accepts at most {MAX_IMAGE_EDGE} a side."
        )
    return size


def _png_header(where: str, f) -> tuple[int, int]:
    head = f.read(33)
    if len(head) < 33 or head[:8] != _PNG_SIGNATURE or head[12:16] != b"IHDR":
        raise ExportError(f"{where} is not a PNG file, but its mime_type says PNG.")
    width, height, depth = struct.unpack(">IIB", head[16:25])
    if depth == 16:
        raise ExportError(
            f"{where} is a 16-bit PNG; rusty-tiles accepts 8-bit images only. Save it as 8-bit."
        )
    return width, height


def _jpeg_header(where: str, f, size: int) -> tuple[int, int]:
    if f.read(2) != b"\xff\xd8":
        raise ExportError(f"{where} is not a JPEG file, but its mime_type says JPEG.")
    f.seek(size - 2)
    if f.read(2) != b"\xff\xd9":
        raise ExportError(f"{where} has no JPEG end marker (truncated?); rusty-tiles refuses it.")
    position = 2
    while position + 4 <= size:
        f.seek(position)
        marker = f.read(2)
        if marker[0] != 0xFF:
            break
        code = marker[1]
        if code == 0xFF:  # fill byte
            position += 1
            continue
        if code in (0x01, *range(0xD0, 0xD8)):  # no length
            position += 2
            continue
        (length,) = struct.unpack(">H", f.read(2))
        if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            precision, height, width = struct.unpack(">BHH", f.read(5))
            if precision != 8:
                raise ExportError(
                    f"{where} is a {precision}-bit JPEG; rusty-tiles accepts 8-bit images only."
                )
            return width, height
        if code in (0xD9, 0xDA):  # end or scan before any frame header
            break
        position += 2 + length
    raise ExportError(f"{where} has no JPEG frame header.")
