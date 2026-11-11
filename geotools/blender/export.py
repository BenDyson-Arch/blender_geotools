"""Extract a ``core.gltf_writer.SceneExport`` from the source collection (M2 publish).

Runs on the main thread. Everything is read with ``foreach_get`` into numpy;
nothing loops over vertices, loops or faces in Python.

What is exported
----------------
* Every mesh object in the collection and its children, sorted by name. The
  EVALUATED mesh is exported (modifiers and Geometry Nodes applied), with the
  evaluated world matrix baked into the positions in float64 before the cast to
  float32. Normals use the inverse transpose. A negative determinant (a mirror)
  reverses the triangle winding and the tangent handedness, so faces still
  point the way their normals do.
* Triangles come from ``mesh.loop_triangles``. They are grouped by material,
  keeping Blender's order within each group, and every (material, attribute
  layout) pair across all objects becomes one primitive.
* Corners are welded per object and material: two corners become one vertex
  when they share a Blender vertex AND every exported attribute bit for bit, so
  UV seams and sharp edges stay split. See :func:`weld`.

UV maps
-------
At most two, chosen per material so that a material's ``TextureRef.texcoord``
means the same thing on every object that uses it:

1. the UV maps the material's textures sample, in slot order (base colour,
   metallic-roughness, normal, emissive). An Image Texture with nothing in its
   Vector input samples the active RENDER UV map, as it does when Blender
   renders; a UV Map node names its map;
2. then the object's other maps fill the remaining slots, active render map
   first, then the rest in the mesh's order.

The render map comes first rather than the active (edit) one because it is what
Blender renders an unconnected Image Texture with, and selecting a map to edit
it should not change what is published.

COLOR_0 comes from the render colour attribute (the camera icon), falling back
to the active one, for the same reason. Byte colours are read through the
``color`` property, which Blender converts from sRGB to linear; everything is
clamped to [0, 1].

Evaluated versus original topology
----------------------------------
``source_polygon`` holds polygon indices of the EVALUATED mesh, since that is
what is triangulated. The face map, though, must refer to editable source
polygons. When the evaluated mesh has exactly the original topology (same
polygon sizes, same loop vertices, same vertex count, e.g. a Displace or Smooth
modifier, or no modifiers) its polygon indices are the original's, and the
topology entry is the plain :func:`core.facemap.topology_digest` of the original
mesh. Otherwise (Subdivision, Mirror, Geometry Nodes that change topology) the
indices refer to evaluated polygons, which no editable polygon matches; the
entry is then ``"evaluated:" + digest of the evaluated topology``, so later
milestones know to transfer values by position rather than by index, and a
stale map is still detected when the evaluated topology changes. Comparing the
whole topology rather than the polygon count also catches modifiers that keep
the count but reorder or rewire polygons.

Images
------
An unmodified PNG or JPEG file on disk is referenced in place. A packed,
unmodified PNG or JPEG is written out byte for byte. Anything else (generated,
EXR, TIFF, a dirty or painted image, a 16-bit or float buffer) is written as a
PNG of its CURRENT pixels, through a temporary image datablock, so the user's
image, its file path and its dirty state are never touched. Float colour
buffers are converted from linear to sRGB first; non-colour data is written
as is. Files go into ``image_dir`` as ``<sanitised name>-<hash of the full
name>.png``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

import bpy
import numpy as np

from ..core.facemap import topology_digest
from ..core.gltf_writer import (
    ExportError,
    ImageData,
    MaterialData,
    PrimitiveData,
    SceneExport,
    TextureRef,
)
from . import nodes

log = logging.getLogger(__name__)

MAX_TEXCOORDS = 2
EVALUATED_PREFIX = "evaluated:"
GENERATOR = "GeoTools"

_CHUNK = 1 << 20  # rows per float64 transform step, bounds temporary memory
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_SUFFIXES = {"image/png": (".png",), "image/jpeg": (".jpg", ".jpeg")}


# --- public entry point ---------------------------------------------------------------


def extract_scene(
    context: bpy.types.Context, collection: bpy.types.Collection, image_dir: Path
) -> tuple[SceneExport, dict[str, str]]:
    """The collection's mesh objects as a ``SceneExport``, plus object name ->
    topology digest (prefixed ``"evaluated:"`` when polygon indices refer to the
    evaluated mesh; see the module docstring)."""
    objects = sorted((o for o in collection.all_objects if o.type == "MESH"), key=lambda o: o.name)
    _sync_edit_mode(context, objects)
    depsgraph = context.evaluated_depsgraph_get()

    materials = _MaterialTable(Path(image_dir))
    groups: dict[tuple[int | None, _Layout], list[_Chunk]] = {}
    topology: dict[str, str] = {}
    for index, obj in enumerate(objects):
        topology[obj.name] = _extract_object(obj, index, depsgraph, materials, groups)

    def order(key: tuple[int | None, _Layout]) -> tuple:
        material, layout = key
        return (material is None, material or 0, layout)

    primitives = tuple(_merge(key[0], groups[key]) for key in sorted(groups, key=order))
    scene = SceneExport(
        object_names=tuple(o.name for o in objects),
        materials=tuple(info.data for info in materials.infos),
        images=tuple(materials.images.data),
        primitives=primitives,
        generator=GENERATOR,
    )
    return scene, topology


def _sync_edit_mode(context: bpy.types.Context, objects: list[bpy.types.Object]) -> None:
    """Write edit-mode changes back to the mesh, so data and evaluation agree."""
    editing = [o for o in objects if o.mode == "EDIT"]
    for obj in editing:
        obj.update_from_editmode()
    if editing:
        context.view_layer.update()


# --- reading mesh data ------------------------------------------------------------------


def _get(collection, attr: str, count: int, width: int, dtype) -> np.ndarray:
    out = np.empty(count * width, dtype=dtype)
    if count:
        collection.foreach_get(attr, out)
    return out.reshape(count, width) if width > 1 else out


# Generic RNA collections (``loops``, ``loop_triangles``, ``vertices``) are read
# one element at a time; attributes are copied as raw arrays, 10 to 100 times
# faster. So prefer the attribute where Blender stores one.


def _attribute(mesh: bpy.types.Mesh, name: str, domain: str, prop: str, width: int, dtype):
    attr = mesh.attributes.get(name)
    if attr is None or attr.domain != domain:
        return None
    return _get(attr.data, prop, len(attr.data), width, dtype)


def _corner_verts(mesh: bpy.types.Mesh) -> np.ndarray:
    verts = _attribute(mesh, ".corner_vert", "CORNER", "value", 1, np.int32)
    if verts is None:
        verts = _get(mesh.loops, "vertex_index", len(mesh.loops), 1, np.int32)
    return verts


def _vertex_positions(mesh: bpy.types.Mesh) -> np.ndarray:
    co = _attribute(mesh, "position", "POINT", "vector", 3, np.float32)
    if co is None:
        co = _get(mesh.vertices, "co", len(mesh.vertices), 3, np.float32)
    return co


def _loop_totals(mesh: bpy.types.Mesh) -> np.ndarray:
    # Face offsets are sorted (Blender's OffsetIndices), so sizes are differences.
    starts = _get(mesh.polygons, "loop_start", len(mesh.polygons), 1, np.int32)
    return np.diff(starts, append=np.int32(len(mesh.loops))).astype(np.int32)


def _triangles(mesh: bpy.types.Mesh) -> tuple[np.ndarray, np.ndarray]:
    """``(tri_loops (T, 3), tri_poly (T,))``, as ``mesh.loop_triangles`` has them."""
    polygons, loops = len(mesh.polygons), len(mesh.loops)
    if 3 * polygons == loops:
        # Only triangles (every polygon has at least three corners), which Blender
        # keeps as they are: triangle t is polygon t with corners 3t, 3t+1, 3t+2.
        return np.arange(loops, dtype=np.int32).reshape(polygons, 3), np.arange(
            polygons, dtype=np.int32
        )
    count = len(mesh.loop_triangles)
    tri_loops = _get(mesh.loop_triangles, "loops", count, 3, np.int32)
    tri_poly = _get(mesh.loop_triangle_polygons, "value", count, 1, np.int32)
    return tri_loops, tri_poly


def _topology_arrays(mesh: bpy.types.Mesh) -> tuple[np.ndarray, np.ndarray, int]:
    return _loop_totals(mesh), _corner_verts(mesh), len(mesh.vertices)


def mesh_topology_digest(original: bpy.types.Mesh, evaluated: bpy.types.Mesh) -> str:
    """The topology entry for one object; see the module docstring."""
    orig = _topology_arrays(original)
    if evaluated is original:
        return topology_digest(*orig)
    ev = _topology_arrays(evaluated)
    if orig[2] == ev[2] and np.array_equal(orig[0], ev[0]) and np.array_equal(orig[1], ev[1]):
        return topology_digest(*orig)
    return EVALUATED_PREFIX + topology_digest(*ev)


def _transform(
    rows: np.ndarray,
    matrix: np.ndarray,
    translation: np.ndarray | None = None,
    normalize: bool = False,
) -> np.ndarray:
    """``rows @ matrix.T (+ translation)`` in float64, chunked, returned as float32."""
    out = np.empty(rows.shape, dtype=np.float32)
    mt = matrix.T
    for start in range(0, rows.shape[0], _CHUNK):
        block = rows[start : start + _CHUNK].astype(np.float64) @ mt
        if translation is not None:
            block += translation
        if normalize:
            block = _unit(block)
        out[start : start + _CHUNK] = block
    return out


def _unit(block: np.ndarray, normals: np.ndarray | None = None) -> np.ndarray:
    """Normalise rows. Degenerate rows become +Z, or with ``normals`` (tangents)
    a unit vector perpendicular to the row's normal."""
    length = np.sqrt(np.einsum("ij,ij->i", block, block))
    bad = ~(length > 1e-12)
    block = block / np.where(bad, 1.0, length)[:, None]
    if bad.any():
        block[bad] = (0.0, 0.0, 1.0) if normals is None else _perpendicular(normals[bad])
    return block


def _perpendicular(normals: np.ndarray) -> np.ndarray:
    """A unit vector perpendicular to each normal (for degenerate tangents)."""
    normals = normals.astype(np.float64)
    axis = np.zeros_like(normals)
    use_y = np.abs(normals[:, 0]) > 0.9
    axis[~use_y, 0] = 1.0
    axis[use_y, 1] = 1.0
    return _unit(np.cross(normals, axis))


# --- the weld -----------------------------------------------------------------------------

_M1 = np.uint64(0x9E3779B97F4A7C15)
_M2 = np.uint64(0xBF58476D1CE4E5B9)
_S = np.uint64(31)


def _row_hash(data: np.ndarray) -> np.ndarray:
    """A 64-bit hash of each row's bit pattern."""
    bits = np.ascontiguousarray(data).view(np.uint32).reshape(data.shape[0], -1)
    h = np.full(data.shape[0], 0x243F6A8885A308D3, dtype=np.uint64)
    for j in range(bits.shape[1]):
        h ^= bits[:, j]
        h *= _M1
        h ^= h >> _S
    return h


@dataclass(frozen=True, slots=True)
class _Source:
    """One per-corner attribute: rows of ``data`` picked by ``index`` (one per corner)."""

    data: np.ndarray  # (N, k)
    index: np.ndarray  # (C,) int


def weld(vertex: np.ndarray, sources: list[_Source]) -> tuple[np.ndarray, np.ndarray]:
    """Weld corners that share a vertex and every attribute bit for bit.

    Returns ``(first, inverse)``: ``first[v]`` is a corner holding welded vertex
    ``v``, and ``inverse[c]`` the welded vertex of corner ``c``.

    Deterministic: corners are sorted (stably) by vertex index, then by a hash
    of their attribute bits, and each run of equal neighbours becomes one
    vertex. Neighbours are compared exactly, so a hash collision can only leave
    two equal corners unwelded, never weld two different ones. Vertex order is
    by source vertex index. One stable sort of a 64-bit key replaces
    ``np.unique`` on packed rows, which sorts rows as opaque bytes and was far
    slower (see the report in the M2 notes).
    """
    count = vertex.shape[0]
    h = vertex.astype(np.uint64) * _M2
    for src in sources:
        h ^= _row_hash(src.data)[src.index]
        h *= _M1
        h ^= h >> _S
    key = (vertex.astype(np.uint64) << np.uint64(32)) | (h >> np.uint64(32))
    del h
    order = np.argsort(key, kind="stable")
    key = key[order]
    new = np.empty(count, dtype=bool)
    new[:1] = True
    np.not_equal(key[1:], key[:-1], out=new[1:])
    del key
    for src in sources:
        idx = src.index[order]
        candidates = np.flatnonzero(~new[1:] & (idx[1:] != idx[:-1]))
        if candidates.size:
            a, b = src.data[idx[candidates]], src.data[idx[candidates + 1]]
            width = a.shape[1] if a.ndim > 1 else 1
            a = np.ascontiguousarray(a).view(np.uint32).reshape(-1, width)
            b = np.ascontiguousarray(b).view(np.uint32).reshape(-1, width)
            new[candidates[(a != b).any(axis=1)] + 1] = True
    group = np.cumsum(new, dtype=np.int64) - 1
    inverse = np.empty(count, dtype=np.uint32)
    inverse[order] = group
    return order[new], inverse


# --- materials and images -----------------------------------------------------------------


def _sanitise(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")[:64]
    return clean or "image"


def _magic_mime(head: bytes) -> str | None:
    if head.startswith(_PNG):
        return "image/png"
    if head.startswith(_JPEG):
        return "image/jpeg"
    return None


def _file_path(image: bpy.types.Image) -> str:
    if not image.filepath:
        return ""
    return os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))


def _srgb_from_linear(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1.0 / 2.4) - 0.055)


def _save_png(image: bpy.types.Image, path: Path) -> None:
    """Write the image's current pixels as a PNG, without touching the image."""
    if image.source == "TILED":
        raise ExportError(f"Image {image.name!r} is a UDIM tile set, which glTF cannot hold.")
    width, height = image.size
    channels = image.channels
    if width == 0 or height == 0 or channels == 0:
        raise ExportError(f"Image {image.name!r} has no pixels (is its file missing?).")
    px = np.empty(width * height * channels, dtype=np.float32)
    image.pixels.foreach_get(px)
    px = px.reshape(-1, channels)
    rgba = np.ones((px.shape[0], 4), dtype=np.float32)
    if channels >= 3:
        rgba[:, :3] = px[:, :3]
    else:
        rgba[:, :3] = px[:, :1]
    if channels in (2, 4):
        rgba[:, 3] = px[:, -1]
    del px
    is_data = bool(image.colorspace_settings.is_data)
    if image.is_float and not is_data:
        rgba[:, :3] = _srgb_from_linear(rgba[:, :3])
    np.clip(rgba, 0.0, 1.0, out=rgba)
    alpha = bool((rgba[:, 3] < 1.0).any())
    tmp = bpy.data.images.new(
        "GeoTools export", width, height, alpha=alpha, float_buffer=False, is_data=is_data
    )
    try:
        tmp.pixels.foreach_set(rgba.ravel())
        tmp.filepath_raw = str(path)
        tmp.file_format = "PNG"
        tmp.save()
    finally:
        bpy.data.images.remove(tmp)


class _ImageTable:
    def __init__(self, image_dir: Path) -> None:
        self.image_dir = image_dir
        self.data: list[ImageData] = []
        self._index: dict[str, int] = {}

    def add(self, image: bpy.types.Image) -> int:
        key = image.name_full
        if key not in self._index:
            self._index[key] = len(self.data)
            self.data.append(self._export(image))
        return self._index[key]

    def _target(self, image: bpy.types.Image, suffix: str) -> Path:
        digest = hashlib.sha256(image.name_full.encode("utf-8")).hexdigest()[:10]
        self.image_dir.mkdir(parents=True, exist_ok=True)
        return (self.image_dir / f"{_sanitise(image.name)}-{digest}{suffix}").resolve()

    def _export(self, image: bpy.types.Image) -> ImageData:
        clean = not image.is_dirty and image.source == "FILE"
        packed = image.packed_file is not None
        if clean and not packed and image.file_format in ("PNG", "JPEG"):
            path = _file_path(image)
            if path and os.path.isfile(path):
                with open(path, "rb") as f:
                    mime = _magic_mime(f.read(8))
                if mime is not None and path.lower().endswith(_SUFFIXES[mime]):
                    return ImageData(name=image.name, path=Path(path), mime_type=mime)
        if clean and packed and len(image.packed_files) == 1:
            data = image.packed_file.data
            mime = _magic_mime(data[:8])
            if mime is not None:
                target = self._target(image, _SUFFIXES[mime][0])
                target.write_bytes(data)
                return ImageData(name=image.name, path=target, mime_type=mime)
        target = self._target(image, ".png")
        _save_png(image, target)
        return ImageData(name=image.name, path=target, mime_type="image/png")


@dataclass(frozen=True, slots=True)
class _MaterialInfo:
    data: MaterialData
    uv_refs: tuple[str, ...]  # UV maps the textures sample, "" = the render map
    tangent_ref: str | None  # the normal texture's UV map, if it has one


def _image_node(socket: bpy.types.NodeSocket, output: str = "Color") -> bpy.types.Node | None:
    found = nodes.from_link(socket)
    if found is None:
        return None
    node, out = found
    if node.bl_idname == "ShaderNodeTexImage" and node.image is not None and out.name == output:
        return node
    return None


def _is_identity_mapping(node: bpy.types.Node) -> bool:
    def value(name: str):
        socket = node.inputs.get(name)
        return None if socket is None or socket.is_linked else tuple(socket.default_value)

    return (
        value("Location") in ((0.0, 0.0, 0.0), None)
        and value("Rotation") in ((0.0, 0.0, 0.0), None)
        and value("Scale") in ((1.0, 1.0, 1.0), None)
    )


def _uv_ref(material: bpy.types.Material, image_node: bpy.types.Node) -> str:
    """The UV map an Image Texture samples: a name, or "" for the render map."""
    found = nodes.from_link(image_node.inputs["Vector"])
    hops = 0
    while found is not None and found[0].bl_idname == "ShaderNodeMapping" and hops < 8:
        if not _is_identity_mapping(found[0]):
            log.warning(
                "material %r: Mapping node %r is ignored; glTF texture transforms are an "
                "extension rusty-tiles refuses",
                material.name,
                found[0].name,
            )
        found = nodes.from_link(found[0].inputs["Vector"])
        hops += 1
    if found is None:
        return ""
    node, out = found
    if node.bl_idname == "ShaderNodeUVMap":
        return node.uv_map
    if node.bl_idname == "ShaderNodeTexCoord" and out.name == "UV":
        return ""
    log.warning(
        "material %r: texture %r uses %s coordinates; exporting the render UV map instead",
        material.name,
        image_node.name,
        node.bl_idname,
    )
    return ""


def _clamp01(values, what: str, material: bpy.types.Material) -> tuple[float, ...]:
    out = tuple(min(max(float(v), 0.0), 1.0) for v in values)
    if any(abs(a - float(b)) > 1e-6 for a, b in zip(out, values, strict=True)):
        log.warning(
            "material %r: %s %s clamped to [0, 1] for glTF",
            material.name,
            what,
            tuple(round(float(v), 4) for v in values),
        )
    return out


def material_info(material: bpy.types.Material, images: _ImageTable) -> _MaterialInfo:
    """Map one material's Principled BSDF to core PBR."""
    double_sided = not material.use_backface_culling
    principled = nodes.principled_of(material)
    if principled is None:
        color = _clamp01(material.diffuse_color, "viewport colour", material)
        data = MaterialData(name=material.name, base_color_factor=color, double_sided=double_sided)
        return _MaterialInfo(data, (), None)

    refs: list[str] = []

    def texture(node: bpy.types.Node) -> TextureRef:
        ref = _uv_ref(material, node)
        if ref not in refs:
            refs.append(ref)
        if len(refs) > MAX_TEXCOORDS:
            raise ExportError(
                f"Material {material.name!r} samples {len(refs)} UV maps; glTF allows "
                f"{MAX_TEXCOORDS}."
            )
        return TextureRef(image=images.add(node.image), texcoord=refs.index(ref))

    inputs = principled.inputs
    fields: dict[str, object] = {"name": material.name, "double_sided": double_sided}

    # Alpha mode (summarise.blend_mode is the single rule).
    mode = nodes.blend_mode(principled)
    if mode == "BLEND":
        raise ExportError(
            f"Material {material.name!r} uses partial transparency (alpha blending), which "
            "rusty-tiles refuses. Make it opaque or use an alpha clip."
        )
    if mode == "CLIP":
        fields["alpha_mode"] = "MASK"
        fields["alpha_cutoff"] = nodes.alpha_cutoff(principled)

    # Base colour: a texture, a colour attribute (COLOR_0 multiplies it), or a value.
    base = inputs["Base Color"]
    base_image = _image_node(base)
    if base_image is not None:
        fields["base_color_texture"] = texture(base_image)
    elif not base.is_linked:
        rgb = _clamp01(tuple(base.default_value)[:3], "base colour", material)
        fields["base_color_factor"] = (*rgb, 1.0)
    elif nodes.from_node(base) is None or nodes.from_node(base).bl_idname != (
        "ShaderNodeVertexColor"
    ):
        log.warning("material %r: base colour input is not exportable", material.name)

    # Metallic and roughness: one image through Separate Color (G roughness, B metallic).
    metallic, roughness = inputs["Metallic"], inputs["Roughness"]
    m_link, r_link = nodes.from_link(metallic), nodes.from_link(roughness)
    mr_image = None
    if (
        m_link is not None
        and r_link is not None
        and m_link[0] == r_link[0]
        and m_link[0].bl_idname == "ShaderNodeSeparateColor"
        and m_link[0].mode == "RGB"
        and m_link[1].identifier == "Blue"
        and r_link[1].identifier == "Green"
    ):
        mr_image = _image_node(m_link[0].inputs["Color"])
    if mr_image is not None:
        fields["metallic_roughness_texture"] = texture(mr_image)
        fields["metallic_factor"] = 1.0
        fields["roughness_factor"] = 1.0
    else:
        if metallic.is_linked or roughness.is_linked:
            log.warning(
                "material %r: metallic/roughness links are not one image through Separate "
                "Color (G, B); exporting the socket values",
                material.name,
            )
        fields["metallic_factor"] = _clamp01((metallic.default_value,), "metallic", material)[0]
        fields["roughness_factor"] = _clamp01((roughness.default_value,), "roughness", material)[0]

    # Normal map (tangent space only).
    tangent_ref = None
    n_link = nodes.from_link(inputs["Normal"])
    if n_link is not None and n_link[0].bl_idname == "ShaderNodeNormalMap":
        normal_map = n_link[0]
        normal_image = _image_node(normal_map.inputs["Color"])
        if normal_map.space != "TANGENT" or normal_image is None:
            log.warning("material %r: only tangent-space normal maps are exported", material.name)
        else:
            ref = texture(normal_image)
            fields["normal_texture"] = ref
            strength = normal_map.inputs["Strength"]
            fields["normal_scale"] = 1.0 if strength.is_linked else float(strength.default_value)
            tangent_ref = refs[ref.texcoord]
            if normal_map.uv_map and normal_map.uv_map != tangent_ref:
                log.warning(
                    "material %r: Normal Map node uses UV map %r but its texture samples %r; "
                    "glTF uses the texture's",
                    material.name,
                    normal_map.uv_map,
                    tangent_ref or "(render map)",
                )

    # Emission: colour x strength. glTF without extensions caps the factor at 1.
    emission, strength = inputs["Emission Color"], inputs["Emission Strength"]
    power = 1.0 if strength.is_linked else float(strength.default_value)
    emission_image = _image_node(emission)
    if emission_image is not None and power > 0.0:
        fields["emissive_texture"] = texture(emission_image)
        fields["emissive_factor"] = _clamp01((power,) * 3, "emission strength", material)
    elif not emission.is_linked:
        rgb = tuple(float(c) * power for c in tuple(emission.default_value)[:3])
        fields["emissive_factor"] = _clamp01(rgb, "emission", material)

    return _MaterialInfo(MaterialData(**fields), tuple(refs), tangent_ref)


class _MaterialTable:
    def __init__(self, image_dir: Path) -> None:
        self.images = _ImageTable(image_dir)
        self.infos: list[_MaterialInfo] = []
        self._index: dict[str, int] = {}

    def index(self, material: bpy.types.Material) -> int:
        key = material.name_full
        if key not in self._index:
            self._index[key] = len(self.infos)
            self.infos.append(material_info(material, self.images))
        return self._index[key]


# --- objects ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class _Layout:
    texcoords: int
    color: bool
    tangents: bool


@dataclass(frozen=True, slots=True)
class _Chunk:
    positions: np.ndarray
    normals: np.ndarray
    tangents: np.ndarray | None
    texcoords: tuple[np.ndarray, ...]
    color: np.ndarray | None
    indices: np.ndarray
    source_object: int
    source_polygon: np.ndarray


class _ObjectData:
    """Lazily read attributes of one evaluated mesh, in world space."""

    def __init__(self, obj: bpy.types.Object, mesh: bpy.types.Mesh) -> None:
        self.obj, self.mesh = obj, mesh
        self.tri_loops, self.tri_poly = _triangles(mesh)
        self.tri_verts = _corner_verts(mesh)[self.tri_loops]
        face_material = _attribute(mesh, "material_index", "FACE", "value", 1, np.int32)
        if face_material is None:
            self.tri_mat = np.zeros(self.tri_poly.shape[0], dtype=np.int32)
        else:
            self.tri_mat = face_material[self.tri_poly]

        matrix = np.array(obj.matrix_world, dtype=np.float64)
        self.linear, self.translation = matrix[:3, :3], matrix[:3, 3]
        det = float(np.linalg.det(self.linear))
        if not np.isfinite(det) or abs(det) < 1e-300:
            raise ExportError(f"Object {obj.name!r} has a degenerate (zero-scale) transform.")
        self.mirrored = det < 0.0
        self.normal_matrix = np.linalg.inv(self.linear).T

        local = _vertex_positions(mesh)
        self.positions = _transform(local, self.linear, self.translation)
        corner = _get(mesh.corner_normals, "vector", len(mesh.loops), 3, np.float32)
        self.normals = _transform(corner, self.normal_matrix, normalize=True)

        layers = list(mesh.uv_layers)
        self.uv_names = [layer.name for layer in layers]
        render = next((layer.name for layer in layers if layer.active_render), None)
        self.render_uv = render or (self.uv_names[0] if self.uv_names else None)
        self.uv_order = (
            [self.render_uv] + [n for n in self.uv_names if n != self.render_uv]
            if self.render_uv
            else []
        )
        self._uvs: dict[str, np.ndarray] = {}
        self._tangents: dict[str, tuple[np.ndarray, str]] = {}
        self.color = self._read_color()

    # UV maps ----------------------------------------------------------------

    def uv(self, name: str) -> np.ndarray:
        if name not in self._uvs:
            layer = self.mesh.uv_layers[name]
            self._uvs[name] = _get(layer.uv, "vector", len(self.mesh.loops), 2, np.float32)
        return self._uvs[name]

    def texcoord_names(self, info: _MaterialInfo | None, material_name: str) -> list[str]:
        names: list[str] = []
        for ref in info.uv_refs if info else ():
            name = ref or self.render_uv
            if name is None or name not in self.uv_names:
                what = f"UV map {ref!r}" if ref else "a UV map"
                raise ExportError(
                    f"Material {material_name!r} samples {what}, which object "
                    f"{self.obj.name!r} does not have."
                )
            names.append(name)
        for name in self.uv_order:
            if len(names) >= MAX_TEXCOORDS:
                break
            if name not in names:
                names.append(name)
        return names

    # colour ---------------------------------------------------------------------

    def _read_color(self) -> tuple[np.ndarray, str] | None:
        attrs = self.mesh.color_attributes
        if not len(attrs):
            return None
        attr = attrs.get(attrs.default_color_name) or attrs.get(attrs.active_color_name)
        if attr is None:
            attr = attrs[0]
        if attr.domain not in ("POINT", "CORNER"):
            return None
        data = _get(attr.data, "color", len(attr.data), 4, np.float32)
        np.nan_to_num(data, copy=False, nan=0.0)
        np.clip(data, 0.0, 1.0, out=data)
        return data, attr.domain

    # tangents -------------------------------------------------------------------

    def tangents(self, uv_name: str) -> tuple[np.ndarray, str]:
        """World-space tangents for ``uv_name`` and their domain: ``"CORNER"``
        (indexed by loop) or ``"TRIANGLE"`` (row ``3 t + k`` for corner k of
        triangle t, before any mirror reversal)."""
        if uv_name not in self._tangents:
            mesh = self.mesh
            totals = _loop_totals(mesh)
            if totals.size == 0 or int(totals.max()) <= 4:
                mesh.calc_tangents(uvmap=uv_name)
                count = len(mesh.loops)
                tangent = _get(mesh.loops, "tangent", count, 3, np.float32)
                sign = _get(mesh.loops, "bitangent_sign", count, 1, np.float32)
                normals = self.normals
                domain = "CORNER"
            else:
                # calc_tangents refuses n-gons, so use a triangulated copy.
                tangent, sign = self._triangulated_tangents(uv_name)
                normals = self.normals[self.tri_loops.ravel()]
                domain = "TRIANGLE"
            world = _unit(_transform(tangent, self.linear).astype(np.float64), normals)
            w = np.where(sign < 0.0, -1.0, 1.0).astype(np.float32)
            if self.mirrored:
                w = -w
            self._tangents[uv_name] = (np.column_stack((world.astype(np.float32), w)), domain)
        return self._tangents[uv_name]

    def _triangulated_tangents(self, uv_name: str) -> tuple[np.ndarray, np.ndarray]:
        mesh = self.mesh
        tri_count = self.tri_verts.shape[0]
        corners = self.tri_loops.ravel()
        tmp = bpy.data.meshes.new("GeoTools tangents")
        try:
            co = _vertex_positions(mesh)
            tmp.vertices.add(len(mesh.vertices))
            tmp.vertices.foreach_set("co", co.ravel())
            tmp.loops.add(3 * tri_count)
            tmp.loops.foreach_set("vertex_index", self.tri_verts.ravel())
            tmp.polygons.add(tri_count)
            tmp.polygons.foreach_set("loop_start", np.arange(0, 3 * tri_count, 3, dtype=np.int32))
            layer = tmp.uv_layers.new(name=uv_name)
            layer.uv.foreach_set("vector", self.uv(uv_name)[corners].ravel())
            tmp.update()
            local_normals = _get(mesh.corner_normals, "vector", len(mesh.loops), 3, np.float32)
            tmp.normals_split_custom_set(local_normals[corners])
            tmp.calc_tangents(uvmap=uv_name)
            count = 3 * tri_count
            return (
                _get(tmp.loops, "tangent", count, 3, np.float32),
                _get(tmp.loops, "bitangent_sign", count, 1, np.float32),
            )
        finally:
            bpy.data.meshes.remove(tmp)


def _extract_object(
    obj: bpy.types.Object,
    obj_index: int,
    depsgraph: bpy.types.Depsgraph,
    materials: _MaterialTable,
    groups: dict[tuple[int | None, _Layout], list[_Chunk]],
) -> str:
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh()
    try:
        digest = mesh_topology_digest(obj.data, mesh)
        if len(mesh.polygons) == 0:
            return digest
        data = _ObjectData(evaluated, mesh)
        if data.tri_verts.shape[0] == 0:
            return digest

        # Material of each triangle; Blender clamps material_index to the last slot.
        slots = [s.material.original if s.material else None for s in evaluated.material_slots]
        if slots:
            slot = np.clip(data.tri_mat, 0, len(slots) - 1)
            used = np.flatnonzero(np.bincount(slot, minlength=len(slots)))
            keys = np.full(len(slots), -1, dtype=np.int64)
            for s in used.tolist():
                if slots[s] is not None:
                    keys[s] = materials.index(slots[s])
            tri_key = keys[slot]
        else:
            tri_key = np.full(data.tri_verts.shape[0], -1, dtype=np.int64)

        distinct = np.unique(tri_key)
        if distinct.size == 1:
            selections = [(int(distinct[0]), None)]
        else:
            order = np.argsort(tri_key, kind="stable")
            bounds = np.searchsorted(tri_key[order], distinct, side="left").tolist()
            bounds.append(order.size)
            selections = [
                (int(k), order[bounds[i] : bounds[i + 1]]) for i, k in enumerate(distinct.tolist())
            ]
        for key, selection in selections:
            material = None if key < 0 else key
            info = None if material is None else materials.infos[material]
            name = "" if info is None else info.data.name
            layout, chunk = _chunk(data, obj_index, selection, info, name)
            groups.setdefault((material, layout), []).append(chunk)
        return digest
    finally:
        evaluated.to_mesh_clear()


def _chunk(
    data: _ObjectData,
    obj_index: int,
    selection: np.ndarray | None,
    info: _MaterialInfo | None,
    material_name: str,
) -> tuple[_Layout, _Chunk]:
    """Weld the triangles ``selection`` (None = all) of one object into a chunk."""

    def pick(rows: np.ndarray) -> np.ndarray:
        return rows if selection is None else rows[selection]

    tri_verts, tri_loops, tri_poly = pick(data.tri_verts), pick(data.tri_loops), pick(data.tri_poly)
    tri_count = tri_verts.shape[0]
    # A mirror flips the winding; reversing corners 1 and 2 restores it.
    swap = [0, 2, 1] if data.mirrored else [0, 1, 2]
    vertex = tri_verts[:, swap].ravel()
    loops = tri_loops[:, swap].ravel()

    sources = [_Source(data.normals, loops)]
    uv_names = data.texcoord_names(info, material_name)
    sources += [_Source(data.uv(name), loops) for name in uv_names]

    color_src = None
    if data.color is not None:
        values, domain = data.color
        color_src = _Source(values, vertex if domain == "POINT" else loops)
        sources.append(color_src)

    tangent_src = None
    if info is not None and info.data.normal_texture is not None:
        uv_name = info.tangent_ref or data.render_uv
        values, domain = data.tangents(uv_name)
        if domain == "CORNER":
            index = loops
        else:
            tri_ids = pick(np.arange(data.tri_verts.shape[0], dtype=np.int64))
            index = (3 * tri_ids[:, None] + np.array(swap, dtype=np.int64)).ravel()
        signs = values[index, 3].reshape(tri_count, 3)
        mixed = (signs != signs[:, :1]).any(axis=1)
        if mixed.any():
            # rusty-tiles needs one handedness per triangle: take the majority.
            values = values[index]
            majority = np.where(signs.sum(axis=1) < 0, -1.0, 1.0).astype(np.float32)
            values[:, 3] = np.repeat(majority, 3)
            index = np.arange(values.shape[0], dtype=np.int64)
        tangent_src = _Source(values, index)
        sources.append(tangent_src)

    first, inverse = weld(vertex, sources)
    chunk = _Chunk(
        positions=data.positions[vertex[first]],
        normals=data.normals[loops[first]],
        tangents=None if tangent_src is None else tangent_src.data[tangent_src.index[first]],
        texcoords=tuple(src.data[src.index[first]] for src in sources[1 : 1 + len(uv_names)]),
        color=None if color_src is None else color_src.data[color_src.index[first]],
        indices=inverse.reshape(tri_count, 3),
        source_object=obj_index,
        source_polygon=np.ascontiguousarray(tri_poly, dtype=np.int32),
    )
    layout = _Layout(len(uv_names), color_src is not None, tangent_src is not None)
    return layout, chunk


def _merge(material: int | None, chunks: list[_Chunk]) -> PrimitiveData:
    """One primitive from chunks of the same material and layout, in object order."""
    first = chunks[0]

    def cat(arrays: list[np.ndarray]) -> np.ndarray:
        return arrays[0] if len(arrays) == 1 else np.concatenate(arrays)

    offsets = np.cumsum([0] + [c.positions.shape[0] for c in chunks[:-1]])
    indices = cat(
        [
            c.indices if o == 0 else c.indices + np.uint32(o)
            for c, o in zip(chunks, offsets.tolist(), strict=True)
        ]
    )
    triangles = [c.indices.shape[0] for c in chunks]
    source_object = np.repeat(
        np.array([c.source_object for c in chunks], dtype=np.int32), triangles
    )
    return PrimitiveData(
        material=material,
        positions=cat([c.positions for c in chunks]),
        indices=indices,
        normals=cat([c.normals for c in chunks]),
        tangents=None if first.tangents is None else cat([c.tangents for c in chunks]),
        texcoords=tuple(cat([c.texcoords[i] for c in chunks]) for i in range(len(first.texcoords))),
        color=None if first.color is None else cat([c.color for c in chunks]),
        source_object=source_object,
        source_polygon=cat([c.source_polygon for c in chunks]),
    )
