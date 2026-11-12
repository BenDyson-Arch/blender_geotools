"""Extract a ``core.gltf_writer.SceneExport`` from the source collection (M2 publish).

Runs on the main thread. Everything is read with ``foreach_get`` into numpy;
nothing loops over vertices, loops or faces in Python.

What is exported
----------------
* Every mesh object in the collection and its children, sorted by name. The
  EVALUATED mesh is exported (modifiers and Geometry Nodes applied), or, with
  ``apply_modifiers=False`` (the build profile's Apply Modifiers off), the
  object's original mesh data, as Blender's own glTF exporter does. Either way
  the evaluated world matrix is baked into the positions in float64 before the
  cast to float32. Normals use the inverse transpose. A negative determinant (a mirror)
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
the count but reorder or rewire polygons. Without Apply Modifiers the exported
mesh is the original, so every entry is the plain digest.

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

Image remedies
--------------
An image with a remedy (``image.geotools_remedy``, chosen by the user in the
Checks panel; see ``core.images``) is always written from its current pixels
through the same kind of temporary datablock, never referenced in place:

* ``JPEG``: a JPEG at the chosen quality. An image with transparency is refused,
  since JPEG cannot hold it.
* ``DOWNSCALE``: the temporary copy is scaled with ``Image.scale`` so its longest
  side is the max edge, then written as PNG, or, if that PNG is over the size
  limit, as JPEG at the chosen quality (refused with transparency).
* ``CONVERT_8BIT``: an 8-bit PNG.

The file name adds the remedy and its parameters, e.g.
``albedo-<hash>-jpeg-q95.jpg`` or ``albedo-<hash>-downscale-4096.png``. The
result is checked against rusty-tiles' size and edge limits straight away; a
result still over them is an ``ExportError``, never a silent retry. The user's
image, its file, pixels and dirty state are left alone.

Presentation (variant tilesets)
-------------------------------
``extract_scene(..., presentation=Presentation(...))`` publishes a layer's
presentation (``blender.presentation``) in place of the user's colours, without
changing the user's materials, images or colour attributes. Only what is
written changes:

* ``COLOR_0`` target: COLOR_0 is the presentation's own colours
  (``Presentation.colors``) instead of the render colour attribute. They are
  worked out at publish time from the layer's values on the very meshes
  exported (``blender.presentation.build_presentation``), one 8-bit sRGB
  colour per face or per corner of each object, and converted to linear
  floats here. No colour attribute has to survive the modifiers, so a layer
  computed by Geometry Nodes that change the topology presents like any
  other. An object without colours gets the no-data colour; colours whose
  count is not the exported mesh's faces or corners are an ``ExportError``
  (the meshes changed since the presentation was built). REPLACE writes every
  material with a white base colour factor and no base-colour texture, so
  COLOR_0 shows as it is; MULTIPLY keeps the materials, and COLOR_0
  multiplies their base colour.
* ``BASE_COLOR`` and ``EMISSIVE`` targets (TEXTURE layers): each object gets its
  own copy of each of its materials, with the object's 8-bit presentation
  image in that slot (sampling the layer's UV map), so a material shared by
  several objects becomes one glTF material per object. A slot without a
  material gets a plain white one. REPLACE on BASE_COLOR sets the base colour
  factor to white and drops COLOR_0; REPLACE on EMISSIVE makes the base colour
  black with no texture, metallic 0 and no metallic-roughness texture, drops
  COLOR_0 and sets the emissive factor to 1, so the ramp shows unlit. MULTIPLY
  keeps the base colour factor and COLOR_0 (BASE_COLOR) or the whole material
  (EMISSIVE, factor 1).

``gt_present``, the colour attribute the Write Colours button paints for a
viewport preview, is never published: COLOR_0 skips it even when it is the
render or active colour attribute, and a COLOR_0 presentation does not read it.

Metadata (the M4 metadata GLB)
------------------------------
``extract_scene(..., metadata=Metadata(...))`` adds what ``core.gltf_metadata``
needs, as custom vertex attributes on every primitive (the plain export never
writes them):

* ``_FEATURE_ID_0``: the object's index in ``SceneExport.object_names``;
* with ``Metadata.face_table``, ``_FEATURE_ID_1``: the face's row in the face
  table (objects in order, each object's exported faces in order). It is also a
  weld source, so vertices are shared only within a face;
* each of ``Metadata.vertex_layers``: the values in ``Metadata.values``
  (object name -> one value per face or corner of the exported mesh, read by
  ``blender.glb_export`` from the same evaluated meshes), as the layer's
  attribute type (``core.gltf_metadata.layer_attribute_values``). FACE values
  repeat on the face's corners. They are weld sources too, so corners with
  different values never become one vertex. An object without values gets the
  layer's fill value; values whose count is not the mesh's are an
  ``ExportError``.

With ``Metadata.per_object`` (texture layers: every object has its own layer
images, and a primitive refers to one property texture) primitives are split
by object as well as by material and layout, and ``Metadata.texture_uv``
names the UV maps each object's layer images use; they are put in the
texcoord sets (an ``ExportError`` if the material already uses two others).
``PrimitiveData.texcoord_names`` then says which map each set is.
``Metadata.stats`` receives the face counts per object and, with the face
table, the vertex count the export would have without it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Self

import bpy
import numpy as np

from ..core import gltf_writer
from ..core.facemap import topology_digest
from ..core.gltf_metadata import FACE_FEATURE_ID, OBJECT_FEATURE_ID, layer_attribute_values
from ..core.gltf_writer import (
    ExportError,
    ImageData,
    MaterialData,
    PrimitiveData,
    SceneExport,
    TextureRef,
)
from ..core.images import ImageRemedy, RemedyKind, scaled_size
from ..core.layers import Domain, LayerSpec, fill_value
from ..core.metadata_schema import attribute_semantic
from ..core.presentation import PRESENT_ATTRIBUTE, Blend, Target
from ..core.ramps import srgb_bytes_to_linear
from . import blend_paths, nodes
from .properties import image_remedy

log = logging.getLogger(__name__)

MAX_TEXCOORDS = 2
EVALUATED_PREFIX = "evaluated:"
GENERATOR = "GeoTools"

_CHUNK = 1 << 20  # rows per float64 transform step, bounds temporary memory
_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_SUFFIXES = {"image/png": (".png",), "image/jpeg": (".jpg", ".jpeg")}


# --- public entry point ---------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class Presentation:
    """A layer presentation to publish instead of the user's colours.

    Made by ``blender.presentation.build_presentation``; see the module docstring.
    """

    target: Target
    blend: Blend
    colors: Mapping[str, np.ndarray] = field(default_factory=dict)
    """COLOR_0 target: object name -> ``(N, 4) uint8`` sRGB colours, one per face
    (``domain`` FACE) or per corner (CORNER) of the mesh the object exports."""
    domain: str = "FACE"
    """COLOR_0 target: what the rows of ``colors`` are, FACE or CORNER."""
    nodata_rgba: tuple[int, int, int, int] = (0, 0, 0, 255)
    """COLOR_0 target: the colour of an object missing from ``colors``."""
    images: Mapping[str, bpy.types.Image] = field(default_factory=dict)
    """BASE_COLOR and EMISSIVE targets: object name -> its 8-bit presentation image."""
    uv_map: str = ""
    """The UV map the images use; empty for each object's render UV map."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", Target(self.target))
        object.__setattr__(self, "blend", Blend(self.blend))
        if self.domain not in ("FACE", "CORNER"):
            raise ValueError(f"presentation colours are FACE or CORNER, not {self.domain!r}")

    @property
    def per_object(self) -> bool:
        """True if every object gets its own materials (a texture target)."""
        return self.target is not Target.COLOR_0


@dataclass(frozen=True, slots=True, eq=False)
class Metadata:
    """What the metadata GLB adds to the export; see "Metadata" in the module docstring."""

    vertex_layers: tuple[LayerSpec, ...] = ()
    """FACE and CORNER layers written as vertex attributes."""
    values: Mapping[str, Mapping[str, np.ndarray]] = field(default_factory=dict)
    """Layer name -> object name -> values, one per face (FACE) or corner (CORNER)."""
    face_table: bool = False
    """Write ``_FEATURE_ID_1`` (the face table's row) and share vertices only within a face."""
    per_object: bool = False
    """One primitive per object (and material and layout), for property textures."""
    texture_uv: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Object name -> UV maps its texture layers use ("" for the render UV map)."""
    stats: dict = field(default_factory=dict)
    """Filled by the export: ``face_counts`` (per object, in order), ``vertices``
    and, with the face table, ``vertices_without_face_table``."""


def render_uv_name(mesh: bpy.types.Mesh) -> str | None:
    """The UV map an unconnected Image Texture samples: the render map, else the first."""
    layers = list(mesh.uv_layers)
    render = next((layer.name for layer in layers if layer.active_render), None)
    return render or (layers[0].name if layers else None)


def extract_scene(
    context: bpy.types.Context,
    collection: bpy.types.Collection,
    image_dir: Path,
    *,
    remedies: list[dict] | None = None,
    presentation: Presentation | None = None,
    apply_modifiers: bool = True,
    metadata: Metadata | None = None,
) -> tuple[SceneExport, dict[str, str]]:
    """The collection's mesh objects as a ``SceneExport``, plus object name ->
    topology digest (prefixed ``"evaluated:"`` when polygon indices refer to the
    evaluated mesh; see the module docstring).

    With ``remedies``, one dict per image written with a remedy is appended to
    it: the image, the remedy tag, and the file, format and size written.
    With ``presentation``, that layer presentation is published in place of the
    user's colours (see "Presentation" in the module docstring). With
    ``apply_modifiers`` False the original meshes are exported, without their
    modifiers. With ``metadata``, the feature ID and layer attributes of the
    metadata GLB are added (see "Metadata")."""
    objects = sorted((o for o in collection.all_objects if o.type == "MESH"), key=lambda o: o.name)
    _sync_edit_mode(context, objects)
    depsgraph = context.evaluated_depsgraph_get()

    materials = _MaterialTable(Path(image_dir), presentation)
    groups: dict[tuple[int | None, _Layout], list[_Chunk]] = {}
    topology: dict[str, str] = {}
    meta = _MetaState(metadata) if metadata is not None else None
    for index, obj in enumerate(objects):
        topology[obj.name] = _extract_object(
            obj, index, depsgraph, materials, groups, presentation, apply_modifiers, meta
        )
    if meta is not None:
        metadata.stats.update(meta.stats())

    def order(key: tuple[int | None, _Layout]) -> tuple:
        material, layout = key
        return (material is None, material or 0, layout)

    primitives = tuple(_merge(key[0], groups[key]) for key in sorted(groups, key=order))
    if remedies is not None:
        remedies.extend(materials.images.remedies)
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


def _same_arrays(a: tuple, b: tuple) -> bool:
    return a[2] == b[2] and np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])


def same_topology(original: bpy.types.Mesh, evaluated: bpy.types.Mesh) -> bool:
    """True if ``evaluated`` has ``original``'s topology: the same polygon sizes,
    corner vertices and vertex count, so polygon and corner indices match."""
    if evaluated == original:
        return True
    if len(evaluated.polygons) != len(original.polygons):
        return False
    return _same_arrays(_topology_arrays(original), _topology_arrays(evaluated))


def mesh_topology_digest(original: bpy.types.Mesh, evaluated: bpy.types.Mesh) -> str:
    """The topology entry for one object; see the module docstring."""
    orig = _topology_arrays(original)
    if evaluated is original:
        return topology_digest(*orig)
    ev = _topology_arrays(evaluated)
    if _same_arrays(orig, ev):
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
    return blend_paths.image_path(image)


def _srgb_from_linear(c: np.ndarray) -> np.ndarray:
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1.0 / 2.4) - 0.055)


_SRGB_CHUNK = 1 << 20  # pixels per linear-to-sRGB step, bounds temporary memory


def _rgba_pixels(image: bpy.types.Image) -> tuple[np.ndarray, int, int, bool]:
    """The image's current pixels as (N, 4) float32 in [0, 1], ready for an 8-bit file.

    Float colour buffers are converted from linear to sRGB; non-colour data is
    kept as is. Returns the pixels, width, height and whether they are data.
    For a four-channel image (all byte images) the only full-size array is the
    one ``foreach_get`` fills: an 8K image costs 1 GiB here.
    """
    if image.source == "TILED":
        raise ExportError(f"Image {image.name!r} is a UDIM tile set, which glTF cannot hold.")
    width, height = (int(v) for v in image.size[:2])
    channels = image.channels
    if width == 0 or height == 0 or channels == 0:
        raise ExportError(f"Image {image.name!r} has no pixels (is its file missing?).")
    px = np.empty(width * height * channels, dtype=np.float32)
    image.pixels.foreach_get(px)
    px = px.reshape(-1, channels)
    if channels == 4:
        rgba = px
    else:
        rgba = np.ones((px.shape[0], 4), dtype=np.float32)
        if channels >= 3:
            rgba[:, :3] = px[:, :3]
        else:
            rgba[:, :3] = px[:, :1]
        if channels == 2:
            rgba[:, 3] = px[:, -1]
    del px
    is_data = bool(image.colorspace_settings.is_data)
    if image.is_float and not is_data:
        for start in range(0, rgba.shape[0], _SRGB_CHUNK):
            block = rgba[start : start + _SRGB_CHUNK, :3]
            block[...] = _srgb_from_linear(block)
    np.clip(rgba, 0.0, 1.0, out=rgba)
    return rgba, width, height, is_data


def _has_alpha(rgba: np.ndarray) -> bool:
    return bool((rgba[:, 3] < 1.0).any())


class _TempImage:
    """A temporary 8-bit datablock holding pixels, removed again on exit."""

    def __init__(self, rgba: np.ndarray, width: int, height: int, alpha: bool, is_data: bool):
        self.image = bpy.data.images.new(
            "GeoTools export", width, height, alpha=alpha, float_buffer=False, is_data=is_data
        )
        try:
            self.image.pixels.foreach_set(rgba.ravel())
        except BaseException:
            self.close()
            raise

    def save(self, path: Path, file_format: str, quality: int | None = None) -> None:
        self.image.filepath_raw = str(path)
        self.image.file_format = file_format
        if quality is None:
            self.image.save()
        else:
            self.image.save(quality=quality)

    def close(self) -> None:
        if self.image is not None:
            bpy.data.images.remove(self.image)
            self.image = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _save_png(image: bpy.types.Image, path: Path) -> None:
    """Write the image's current pixels as a PNG, without touching the image."""
    rgba, width, height, is_data = _rgba_pixels(image)
    alpha = _has_alpha(rgba)
    with _TempImage(rgba, width, height, alpha, is_data) as tmp:
        del rgba
        tmp.save(path, "PNG")


class _ImageTable:
    def __init__(self, image_dir: Path) -> None:
        self.image_dir = image_dir
        self.data: list[ImageData] = []
        self.remedies: list[dict] = []
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
        remedy = image_remedy(image)
        if remedy.active:
            return self._remediate(image, remedy)
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

    def _remediate(self, image: bpy.types.Image, remedy: ImageRemedy) -> ImageData:
        """Write the image's current pixels with its remedy applied, then check the file."""
        name = image.name
        tag = remedy.tag()
        rgba, width, height, is_data = _rgba_pixels(image)
        alpha = _has_alpha(rgba)
        if remedy.kind is RemedyKind.JPEG and alpha:
            raise ExportError(
                f"Image {name!r} has transparency, which JPEG cannot hold; choose Downscale "
                "for it instead, which keeps PNG while it fits."
            )
        out_w, out_h = width, height
        with _TempImage(
            rgba, width, height, alpha and remedy.kind is not RemedyKind.JPEG, is_data
        ) as tmp:
            del rgba
            if remedy.kind is RemedyKind.JPEG:
                target = self._target(image, f"-{tag}.jpg")
                tmp.save(target, "JPEG", remedy.quality)
                mime = "image/jpeg"
            else:
                if remedy.kind is RemedyKind.DOWNSCALE:
                    out_w, out_h = scaled_size(width, height, remedy.max_edge)
                    if (out_w, out_h) != (width, height):
                        tmp.image.scale(out_w, out_h)
                target = self._target(image, f"-{tag}.png")
                tmp.save(target, "PNG")
                mime = "image/png"
                too_big = target.stat().st_size > gltf_writer.MAX_IMAGE_BYTES
                if remedy.kind is RemedyKind.DOWNSCALE and too_big:
                    if alpha:
                        raise ExportError(
                            f"Image {name!r} is still over "
                            f"{_mib(gltf_writer.MAX_IMAGE_BYTES)} as a {out_w}x{out_h} PNG and "
                            "has transparency, so it cannot fall back to JPEG; lower its max "
                            "edge."
                        )
                    target.unlink()
                    target = self._target(image, f"-{tag}-q{remedy.quality}.jpg")
                    tmp.save(target, "JPEG", remedy.quality)
                    mime = "image/jpeg"
        self._check_remedied(name, remedy, target, out_w, out_h)
        size = target.stat().st_size
        log.info(
            "image %r: %s, wrote %s (%dx%d, %s, %d bytes)",
            name,
            remedy.describe(width, height),
            target.name,
            out_w,
            out_h,
            mime,
            size,
        )
        self.remedies.append(
            {
                "image": name,
                "remedy": tag,
                "file": target.name,
                "mime_type": mime,
                "width": out_w,
                "height": out_h,
                "bytes": size,
            }
        )
        return ImageData(name=name, path=target, mime_type=mime)

    @staticmethod
    def _check_remedied(
        name: str, remedy: ImageRemedy, path: Path, width: int, height: int
    ) -> None:
        limit = gltf_writer.MAX_IMAGE_BYTES
        size = path.stat().st_size
        if size > limit:
            hint = {
                RemedyKind.JPEG: "lower its JPEG quality or choose Downscale",
                RemedyKind.DOWNSCALE: "lower its max edge or JPEG quality",
                RemedyKind.CONVERT_8BIT: "choose Re-encode as JPEG or Downscale",
            }[remedy.kind]
            raise ExportError(
                f"Image {name!r} is still {_mib(size)} {remedy.describe()} ({path.name}), "
                f"over rusty-tiles' {_mib(limit)} limit; {hint}."
            )
        if max(width, height) > gltf_writer.MAX_IMAGE_EDGE:
            raise ExportError(
                f"Image {name!r} is {width}x{height} {remedy.describe()}, over rusty-tiles' "
                f"{gltf_writer.MAX_IMAGE_EDGE} pixels a side; choose Downscale."
            )


def _mib(size: int) -> str:
    return f"{size / (1 << 20):.1f} MiB"


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


# MaterialData texture fields, in gltf_writer's slot order.
_TEXTURE_FIELDS = (
    "base_color_texture",
    "metallic_roughness_texture",
    "normal_texture",
    "occlusion_texture",
    "emissive_texture",
)


def _presented(
    info: _MaterialInfo,
    presentation: Presentation,
    images: _ImageTable,
    obj: bpy.types.Object | None,
) -> _MaterialInfo:
    """``info`` with the presentation applied (see the module docstring)."""
    data = info.data
    white = (1.0, 1.0, 1.0, 1.0)
    if presentation.target is Target.COLOR_0:
        if presentation.blend is Blend.MULTIPLY:
            return info
        return replace(info, data=replace(data, base_color_factor=white, base_color_texture=None))

    image = presentation.images.get(obj.name) if obj is not None else None
    if image is None:
        raise ExportError(
            f"Object {getattr(obj, 'name', '?')!r} has no presentation image; write the "
            "presentation again (Layers panel)."
        )
    slot = "base_color_texture" if presentation.target is Target.BASE_COLOR else "emissive_texture"
    fields: dict[str, object] = {"name": f"{data.name} [{obj.name}]"}
    textures = {
        name: getattr(data, name)
        for name in _TEXTURE_FIELDS
        if name != slot and getattr(data, name) is not None
    }
    if presentation.target is Target.EMISSIVE:
        fields["emissive_factor"] = (1.0, 1.0, 1.0)
        if presentation.blend is Blend.REPLACE:
            fields["base_color_factor"] = (0.0, 0.0, 0.0, 1.0)
            fields["metallic_factor"] = 0.0
            for name in ("base_color_texture", "metallic_roughness_texture"):
                textures.pop(name, None)
                fields[name] = None
    elif presentation.blend is Blend.REPLACE:
        fields["base_color_factor"] = white

    # Texture coordinates: the remaining textures keep their UV maps, the
    # presentation image samples the layer's; renumber in slot order.
    uv_of = {name: info.uv_refs[ref.texcoord] for name, ref in textures.items()}
    uv_of[slot] = presentation.uv_map
    refs: list[str] = []
    for name in _TEXTURE_FIELDS:
        if name in uv_of and uv_of[name] not in refs:
            refs.append(uv_of[name])
    if len(refs) > MAX_TEXCOORDS:
        raise ExportError(
            f"Material {data.name!r} on {obj.name!r} would sample {len(refs)} UV maps with "
            f"the presentation texture; glTF allows {MAX_TEXCOORDS}."
        )
    for name, ref in textures.items():
        fields[name] = TextureRef(image=ref.image, texcoord=refs.index(uv_of[name]))
    fields[slot] = TextureRef(image=images.add(image), texcoord=refs.index(presentation.uv_map))
    return _MaterialInfo(replace(data, **fields), tuple(refs), info.tangent_ref)


class _MaterialTable:
    def __init__(self, image_dir: Path, presentation: Presentation | None = None) -> None:
        self.images = _ImageTable(image_dir)
        self.presentation = presentation
        self.infos: list[_MaterialInfo] = []
        self._index: dict[tuple[str | None, str | None], int] = {}

    @property
    def per_object(self) -> bool:
        """True if each object gets its own materials (a texture presentation)."""
        return self.presentation is not None and self.presentation.per_object

    def index(
        self, material: bpy.types.Material | None, obj: bpy.types.Object | None = None
    ) -> int:
        """The material's index; ``obj`` matters only for a texture presentation,
        where ``material`` may also be None (a plain white material)."""
        per_object = self.per_object
        key = (
            material.name_full if material is not None else None,
            obj.name if per_object and obj is not None else None,
        )
        if key not in self._index:
            if material is not None:
                info = material_info(material, self.images)
            else:
                info = _MaterialInfo(MaterialData(name="GeoTools presentation"), (), None)
            if self.presentation is not None:
                info = _presented(info, self.presentation, self.images, obj)
            self._index[key] = len(self.infos)
            self.infos.append(info)
        return self._index[key]


# --- objects ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class _Layout:
    texcoords: int
    color: bool
    tangents: bool
    owner: int = -1  # the object, when primitives are per object (metadata textures)


class _MetaState:
    """Running state of a metadata export: face offsets and vertex counts."""

    def __init__(self, metadata: Metadata) -> None:
        self.metadata = metadata
        self.face_counts: list[int] = []
        self.face_offset = 0  # faces of the objects before this one
        self.vertices = 0
        self.vertices_plain = 0

    def add_object(self, faces: int) -> int:
        """Count an object's faces; returns its first face table row."""
        first = self.face_offset
        self.face_counts.append(faces)
        self.face_offset += faces
        return first

    def stats(self) -> dict:
        out: dict = {"face_counts": list(self.face_counts), "vertices": self.vertices}
        if self.metadata.face_table:
            out["vertices_without_face_table"] = self.vertices_plain
        return out


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
    custom: tuple[tuple[str, np.ndarray], ...] = ()
    texcoord_names: tuple[str, ...] = ()


class _ObjectData:
    """Lazily read attributes of one exported mesh (evaluated or original), in world space."""

    def __init__(
        self,
        obj: bpy.types.Object,
        mesh: bpy.types.Mesh,
        presentation: Presentation | None = None,
    ) -> None:
        self.obj, self.mesh = obj, mesh
        self.presentation = presentation
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
        presentation = self.presentation
        if presentation is None:
            return self._render_color()
        if presentation.target is Target.COLOR_0:
            return self._present_color(presentation)
        if presentation.blend is Blend.REPLACE:
            return None
        return self._render_color()

    def _render_color(self) -> tuple[np.ndarray, str] | None:
        """The render colour attribute, else the active one, else the first; never
        ``gt_present``, which only a presentation publishes."""
        attrs = self.mesh.color_attributes
        attr = None
        for name in (attrs.default_color_name, attrs.active_color_name):
            if name and name != PRESENT_ATTRIBUTE and attrs.get(name) is not None:
                attr = attrs.get(name)
                break
        if attr is None:
            attr = next((a for a in attrs if a.name != PRESENT_ATTRIBUTE), None)
        if attr is None or attr.domain not in ("POINT", "CORNER"):
            return None
        return self._color_data(attr)

    def _present_color(self, presentation: Presentation) -> tuple[np.ndarray, str]:
        """The presentation's colours of this object as linear floats, and their domain."""
        domain = presentation.domain
        count = len(self.mesh.polygons) if domain == "FACE" else len(self.mesh.loops)
        rgba = presentation.colors.get(self.obj.name)
        if rgba is None:
            rgba = np.array([presentation.nodata_rgba], dtype=np.uint8)
            return srgb_bytes_to_linear(rgba), "CONSTANT"
        if rgba.shape != (count, 4):
            what = "faces" if domain == "FACE" else "corners"
            raise ExportError(
                f"Object {self.obj.name!r}: the presentation has {rgba.shape[0]} colours but "
                f"the exported mesh has {count} {what}; the mesh changed after the "
                "presentation was built. Publish again."
            )
        return srgb_bytes_to_linear(np.ascontiguousarray(rgba, dtype=np.uint8)), domain

    @staticmethod
    def _color_data(attr: bpy.types.Attribute) -> tuple[np.ndarray, str]:
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
    presentation: Presentation | None = None,
    apply_modifiers: bool = True,
    meta: _MetaState | None = None,
) -> str:
    evaluated = obj.evaluated_get(depsgraph)
    # Without Apply Modifiers, the original object's to_mesh(): a copy of its own
    # mesh data. Transforms still come from the evaluated object.
    owner = evaluated if apply_modifiers else obj
    mesh = owner.to_mesh()
    try:
        digest = mesh_topology_digest(obj.data, mesh)
        face_offset = meta.add_object(len(mesh.polygons)) if meta is not None else 0
        if len(mesh.polygons) == 0:
            return digest
        data = _ObjectData(evaluated, mesh, presentation)
        if data.tri_verts.shape[0] == 0:
            return digest
        extra = _metadata_sources(obj, mesh, obj_index, face_offset, meta)

        # Material of each triangle; Blender clamps material_index to the last slot.
        # A texture presentation gives each object its own materials, and a white
        # one where a slot is empty.
        per_object = materials.per_object
        slots = [s.material.original if s.material else None for s in owner.material_slots]
        if slots:
            slot = np.clip(data.tri_mat, 0, len(slots) - 1)
            used = np.flatnonzero(np.bincount(slot, minlength=len(slots)))
            keys = np.full(len(slots), -1, dtype=np.int64)
            for s in used.tolist():
                if slots[s] is not None or per_object:
                    keys[s] = materials.index(slots[s], obj)
            tri_key = keys[slot]
        else:
            fill = materials.index(None, obj) if per_object else -1
            tri_key = np.full(data.tri_verts.shape[0], fill, dtype=np.int64)

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
            layout, chunk = _chunk(data, obj_index, selection, info, name, extra, meta)
            groups.setdefault((material, layout), []).append(chunk)
        return digest
    finally:
        owner.to_mesh_clear()


@dataclass(frozen=True, slots=True)
class _Extra:
    """One metadata attribute of an object: per-face (FACE) or per-corner values."""

    name: str
    weld: np.ndarray  # float32, compared bit for bit by the weld
    values: np.ndarray  # the attribute's own dtype, same rows as ``weld``
    domain: str  # "FACE", "CORNER" or "CONSTANT" (one row)
    is_face_id: bool = False


def _metadata_sources(
    obj: bpy.types.Object,
    mesh: bpy.types.Mesh,
    obj_index: int,
    face_offset: int,
    meta: _MetaState | None,
) -> list[_Extra]:
    """The metadata attributes of one exported mesh (see "Metadata")."""
    if meta is None:
        return []
    metadata = meta.metadata
    extras = [
        _Extra(
            OBJECT_FEATURE_ID,
            np.array([obj_index], np.float32),
            np.array([obj_index], np.float32),
            "CONSTANT",
        )
    ]
    faces, corners = len(mesh.polygons), len(mesh.loops)
    if metadata.face_table:
        ids = np.arange(face_offset, face_offset + faces, dtype=np.float32)
        extras.append(_Extra(FACE_FEATURE_ID, ids, ids, "FACE", is_face_id=True))
    for spec in metadata.vertex_layers:
        domain = Domain(spec.domain).value
        values = metadata.values.get(spec.name, {}).get(obj.name)
        if values is None:
            raw = np.array([fill_value(spec)])
            domain = "CONSTANT"
        else:
            raw = np.asarray(values)
            count = faces if domain == "FACE" else corners
            if raw.shape != (count,):
                what = "faces" if domain == "FACE" else "corners"
                raise ExportError(
                    f"Object {obj.name!r}: layer {spec.name} has {raw.shape[0]} values but the "
                    f"exported mesh has {count} {what}; the mesh changed after they were read. "
                    "Export again."
                )
        typed = layer_attribute_values(raw, spec)
        extras.append(_Extra(attribute_semantic(spec), typed.astype(np.float32), typed, domain))
    return extras


def _chunk(
    data: _ObjectData,
    obj_index: int,
    selection: np.ndarray | None,
    info: _MaterialInfo | None,
    material_name: str,
    extras: list[_Extra] | None = None,
    meta: _MetaState | None = None,
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
    if meta is not None:
        uv_names = _with_texture_uvs(data, uv_names, meta.metadata, material_name)
    sources += [_Source(data.uv(name), loops) for name in uv_names]

    color_src = None
    if data.color is not None:
        values, domain = data.color
        if domain == "POINT":
            index = vertex
        elif domain == "FACE":
            index = np.repeat(tri_poly, 3)
        elif domain == "CONSTANT":
            index = np.zeros(vertex.shape[0], dtype=np.int64)
        else:
            index = loops
        color_src = _Source(values, index)
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

    extra_sources: list[tuple[_Extra, _Source]] = []
    if extras:
        indices = {
            "FACE": np.repeat(tri_poly, 3),
            "CORNER": loops,
            "CONSTANT": np.zeros(vertex.shape[0], dtype=np.int64),
        }
        extra_sources = [(e, _Source(e.weld, indices[e.domain])) for e in extras]
    plain_sources = sources + [s for e, s in extra_sources if not e.is_face_id]
    first, inverse = weld(vertex, sources + [s for _e, s in extra_sources])
    if meta is not None:
        meta.vertices += int(first.shape[0])
        if meta.metadata.face_table:
            meta.vertices_plain += int(weld(vertex, plain_sources)[0].shape[0])
    custom = tuple((e.name, e.values[s.index[first]]) for e, s in extra_sources)
    chunk = _Chunk(
        positions=data.positions[vertex[first]],
        normals=data.normals[loops[first]],
        tangents=None if tangent_src is None else tangent_src.data[tangent_src.index[first]],
        texcoords=tuple(src.data[src.index[first]] for src in sources[1 : 1 + len(uv_names)]),
        color=None if color_src is None else color_src.data[color_src.index[first]],
        indices=inverse.reshape(tri_count, 3),
        source_object=obj_index,
        source_polygon=np.ascontiguousarray(tri_poly, dtype=np.int32),
        custom=custom,
        texcoord_names=tuple(uv_names),
    )
    owner = obj_index if meta is not None and meta.metadata.per_object else -1
    layout = _Layout(len(uv_names), color_src is not None, tangent_src is not None, owner)
    return layout, chunk


def _with_texture_uvs(
    data: _ObjectData, uv_names: list[str], metadata: Metadata, material_name: str
) -> list[str]:
    """``uv_names`` with the UV maps of the object's texture layers added."""
    names = list(uv_names)
    for ref in metadata.texture_uv.get(data.obj.name, ()):
        name = ref or data.render_uv
        if name is None or name not in data.uv_names:
            what = f"UV map {ref!r}" if ref else "a UV map"
            raise ExportError(
                f"A texture layer of object {data.obj.name!r} uses {what}, which the object "
                "does not have."
            )
        if name in names:
            continue
        if len(names) >= MAX_TEXCOORDS:
            raise ExportError(
                f"Object {data.obj.name!r}: material {material_name!r} uses UV maps "
                f"{', '.join(names)}, so the texture layers' map {name!r} would be a third; "
                f"the GLB writer allows {MAX_TEXCOORDS}."
            )
        names.append(name)
    return names


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
    custom = tuple(
        (name, cat([c.custom[i][1] for c in chunks])) for i, (name, _v) in enumerate(first.custom)
    )
    same = all(c.texcoord_names == first.texcoord_names for c in chunks)
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
        custom_attributes=custom,
        texcoord_names=first.texcoord_names if same else (),
    )
