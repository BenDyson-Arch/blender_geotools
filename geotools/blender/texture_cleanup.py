"""M6 texture cleanup: the tools Blender's texture painting lacks.

Painting itself is Blender's (projection paint, clone, smear, soften, masks; see
``docs/texture-cleanup.md``). This module adds, on a source image:

- **Fill Holes**: no-data texels (black or transparent) inside UV islands,
  filled from the island texels around them (``core.texture_cleanup``
  push-pull). Reads and writes stay inside the islands of the faces that use
  the image.
- **Remove Masked**: the same fill over a mask the user paints, either in a
  key colour on the texture itself or in a separate mask image.
- **Pad Islands**: island-aware edge padding outwards from UV islands, so
  coarse levels and mipmaps do not average in the background. Blender's bake
  margin only pads images it bakes; it does nothing for a photogrammetry
  texture.
- **Balance Pages**: a per-page gain and offset, fitted where pages meet across
  3D seams, previewed with a shader node, then applied to the pixels.

Every tool marks the image dirty (so Publish saves it first, see
``texture_state``) and keeps a copy of the pixels it changed: **Undo Cleanup**
puts back the images as they were before the last tool ran. Blender's Ctrl+Z
does not cover pixel edits made outside paint strokes; Image > Reload also
goes back to the saved file.

Settings and the last fit live in ``scene.geotools_texture``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
)

from ..core import texture_cleanup as tc
from ..core.uv_overlap import Coverage, texel_coverage
from .bake_texture import uv_map_name, uv_triangles
from .common import redraw_ui
from .nodes import from_link, principled_of
from .texture_state import PREVIEW_NODE, forget, invalidate, material_images

log = logging.getLogger(__name__)

MAX_BALANCE_SAMPLES = 200_000
"""Seam samples kept for the balance fit (a random subset beyond this)."""

SAMPLES_PER_EDGE = 5
INSET_TEXELS = 1.5
"""Seam samples move this far into their face, away from the gutter."""


# --- settings ------------------------------------------------------------------------------


class GeoToolsBalancePage(bpy.types.PropertyGroup):
    """One page (image) of the last colour balance fit."""

    image: PointerProperty(type=bpy.types.Image, name="Image")
    gain: FloatVectorProperty(name="Gain", size=3, default=(1.0, 1.0, 1.0))
    offset: FloatVectorProperty(name="Offset", size=3, default=(0.0, 0.0, 0.0))
    samples: IntProperty(name="Samples", default=0)
    connected: BoolProperty(name="Connected", default=True)
    reference: BoolProperty(name="Reference", default=False)


MASK_SOURCE_ITEMS = (
    ("KEY", "Key Colour", "Texels painted in the key colour on the texture itself"),
    ("IMAGE", "Mask Image", "White texels of a separate mask image of the same size"),
)


def _image_poll(_self, image: bpy.types.Image) -> bool:
    return not image.name.startswith(".")


class GeoToolsTextureSettings(bpy.types.PropertyGroup):
    """Texture cleanup settings and the last balance fit (``scene.geotools_texture``)."""

    image: PointerProperty(
        type=bpy.types.Image,
        name="Image",
        description="Image to paint and clean. Empty: the base colour image of the active "
        "object's active material",
        poll=_image_poll,
    )
    protect_overlaps: BoolProperty(
        name="Protect Overlaps",
        description="Mask texels that more than one UV triangle uses with a stencil, so a "
        "stroke cannot change two places at once. The mask overlay shows them",
        default=True,
    )
    overlap_texels: IntProperty(name="Overlapping Texels", default=-1)  # -1: not checked
    overlap_note: bpy.props.StringProperty(name="Overlap")
    # Fill Holes
    holes_black: BoolProperty(
        name="Black", description="Fill texels that are black in every colour channel", default=True
    )
    holes_transparent: BoolProperty(
        name="Transparent", description="Fill texels whose alpha is zero", default=True
    )
    black_level: IntProperty(
        name="Black Level",
        description="A texel at or below this 8-bit level in every channel counts as black",
        default=1,
        min=0,
        max=64,
    )
    grow: IntProperty(
        name="Grow",
        description="Grow the holes or mask by this many texels first, to cover soft edges",
        default=1,
        min=0,
        max=64,
    )
    # Remove Masked
    mask_source: EnumProperty(
        name="Mask",
        description="How Remove Masked finds what to remove",
        items=MASK_SOURCE_ITEMS,
        default="KEY",
    )
    key_color: FloatVectorProperty(
        name="Key Colour",
        description="Paint over what to remove in this colour (a hard brush keeps edges clean)",
        subtype="COLOR_GAMMA",
        size=3,
        min=0.0,
        max=1.0,
        default=(1.0, 0.0, 1.0),
    )
    key_tolerance: FloatProperty(
        name="Tolerance",
        description="Largest difference per channel from the key colour that still counts",
        default=0.12,
        min=0.0,
        max=0.5,
    )
    mask_image: PointerProperty(
        type=bpy.types.Image,
        name="Mask Image",
        description="White texels are removed and filled",
        poll=_image_poll,
    )
    mask_grow: IntProperty(
        name="Grow",
        description="Grow the mask by this many texels first, to cover the brush's soft edge",
        default=2,
        min=0,
        max=64,
    )
    # Pad Islands
    pad_margin: IntProperty(
        name="Margin",
        description="Texels of padding grown outwards from each UV island",
        default=16,
        min=1,
        max=512,
    )
    pad_fill_rest: BoolProperty(
        name="Fill Background",
        description="Then fill every texel still outside the islands, so even the coarsest "
        "mipmap averages no background",
        default=False,
    )
    # Balance
    balance_reference: PointerProperty(
        type=bpy.types.Image,
        name="Reference",
        description="Page kept as it is; the others are matched to it. Empty: the page with "
        "the most seam samples",
        poll=_image_poll,
    )
    balance_pages: CollectionProperty(type=GeoToolsBalancePage)
    balance_rms_before: FloatProperty(name="Seam Error Before", default=0.0)
    balance_rms_after: FloatProperty(name="Seam Error After", default=0.0)
    balance_samples: IntProperty(name="Seam Samples", default=0)
    balance_preview: BoolProperty(name="Preview", default=False)


def settings(context: bpy.types.Context) -> GeoToolsTextureSettings:
    return context.scene.geotools_texture


# --- images and their users ----------------------------------------------------------------


def base_colour_node(material: bpy.types.Material | None) -> bpy.types.Node | None:
    """The Image Texture node feeding the Principled Base Color (through reroutes or preview)."""
    if material is None:
        return None
    principled = principled_of(material)
    if principled is None:
        return None
    socket = principled.inputs["Base Color"]
    for _ in range(4):
        found = from_link(socket)
        if found is None:
            return None
        node = found[0]
        if node.bl_idname == "ShaderNodeTexImage":
            return node if node.image is not None else None
        if node.name.startswith(PREVIEW_NODE):
            socket = node.inputs[0]
            continue
        return None
    return None


def base_colour_image(material: bpy.types.Material | None) -> bpy.types.Image | None:
    node = base_colour_node(material)
    return node.image if node is not None else None


def node_uv_map(node: bpy.types.Node | None, mesh: bpy.types.Mesh) -> str | None:
    """The UV map an Image Texture node reads on ``mesh`` (its UV Map node, else the render one)."""
    if node is not None:
        found = from_link(node.inputs["Vector"])
        if found is not None and found[0].bl_idname == "ShaderNodeUVMap" and found[0].uv_map:
            return uv_map_name(mesh, found[0].uv_map)
    return uv_map_name(mesh, "")


def target_image(context: bpy.types.Context) -> bpy.types.Image | None:
    """The image the tools work on: the chosen one, else the active object's base colour."""
    chosen = settings(context).image
    if chosen is not None:
        return chosen
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        return None
    return base_colour_image(obj.active_material)


@dataclass(frozen=True, slots=True)
class ImageUser:
    obj: bpy.types.Object
    uv_map: str
    slots: tuple[int, ...]  # material slot indices whose material uses the image


def candidate_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    """Source collection meshes, plus the active mesh if it is not one of them."""
    collection = context.scene.geotools.source_collection
    objects = []
    if collection is not None:
        objects = sorted(
            (o for o in collection.all_objects if o.type == "MESH"), key=lambda o: o.name
        )
    obj = context.active_object
    if obj is not None and obj.type == "MESH" and obj not in objects:
        objects.append(obj)
    return objects


def image_users(objects: Iterable[bpy.types.Object], image: bpy.types.Image) -> list[ImageUser]:
    """Objects whose materials sample ``image``, with the UV map and slots that do."""
    users = []
    for obj in objects:
        per_map: dict[str, list[int]] = {}
        for index, slot in enumerate(obj.material_slots):
            material = slot.material
            if material is None or image not in material_images(material):
                continue
            node = next(
                (
                    n
                    for n in material.node_tree.nodes
                    if n.bl_idname == "ShaderNodeTexImage" and n.image == image
                ),
                None,
            )
            uv = node_uv_map(node, obj.data)
            if uv is not None:
                per_map.setdefault(uv, []).append(index)
        users += [ImageUser(obj, uv, tuple(slots)) for uv, slots in per_map.items()]
    return users


def user_corners(context: bpy.types.Context, users: list[ImageUser]) -> np.ndarray:
    """UV corners ``(T, 3, 2)`` of the evaluated triangles that sample the image."""
    depsgraph = context.evaluated_depsgraph_get()
    parts = []
    for user in users:
        mesh = user.obj.evaluated_get(depsgraph).data
        if user.uv_map not in mesh.uv_layers:
            continue
        mesh.calc_loop_triangles()
        corners, _faces = uv_triangles(mesh, user.uv_map)
        count = len(mesh.loop_triangles)
        material = np.empty(count, np.int32)
        mesh.loop_triangles.foreach_get("material_index", material)
        parts.append(corners[np.isin(material, np.array(user.slots, np.int32))])
    if not parts:
        return np.empty((0, 3, 2), np.float32)
    return np.ascontiguousarray(np.concatenate(parts))


_coverage: dict[tuple, Coverage] = {}


def image_coverage(
    context: bpy.types.Context, image: bpy.types.Image, objects: Iterable[bpy.types.Object] = ()
) -> tuple[Coverage | None, int]:
    """Texel coverage of ``image`` by the triangles that use it, and their count.

    Cached by the UVs' digest, so tools run one after another rasterise once.
    """
    users = image_users(list(objects) or candidate_objects(context), image)
    corners = user_corners(context, users)
    if not corners.shape[0]:
        return None, 0
    width, height = (int(v) for v in image.size[:2])
    key = (image.name_full, width, height, hashlib.sha1(memoryview(corners)).hexdigest())
    found = _coverage.get(key)
    if found is None:
        found = texel_coverage(corners, resolution=(width, height))
        _coverage.clear()  # one entry: an 8K mask is 64 MiB
        _coverage[key] = found
    return found, corners.shape[0]


# --- pixels and undo -----------------------------------------------------------------------


def read_pixels(image: bpy.types.Image) -> np.ndarray:
    """``(H, W, C)`` float32 pixels, row 0 at the bottom (``Image.pixels`` order)."""
    width, height = (int(v) for v in image.size[:2])
    channels = int(image.channels)
    if not width or not height or not channels:
        raise CleanupError(f"Image {image.name} has no pixels (is its file missing?)")
    pixels = np.empty(width * height * channels, np.float32)
    image.pixels.foreach_get(pixels)
    return pixels.reshape(height, width, channels)


def write_pixels(image: bpy.types.Image, pixels: np.ndarray) -> None:
    """Write pixels back; the image becomes dirty, so Publish saves it first."""
    image.pixels.foreach_set(np.ascontiguousarray(pixels, np.float32).ravel())
    image.update()
    forget(image)
    invalidate()


class CleanupError(RuntimeError):
    pass


_undo: dict[str, object] = {"label": "", "images": {}}


def begin_undo(label: str) -> None:
    """Start a new undo step, dropping the previous one (one level)."""
    _undo["label"] = label
    _undo["images"] = {}


def keep_for_undo(image: bpy.types.Image, pixels: np.ndarray) -> None:
    """Keep a copy of ``pixels`` (8-bit for byte images, which is exact) for Undo Cleanup."""
    images: dict = _undo["images"]  # type: ignore[assignment]
    if image.name_full in images:
        return
    if image.is_float:
        images[image.name_full] = pixels.copy()
    else:
        images[image.name_full] = np.rint(pixels * 255.0).astype(np.uint8)


def undo_label() -> str:
    return str(_undo["label"]) if _undo["images"] else ""


def undo_cleanup() -> list[str]:
    """Put back the images as they were before the last tool; return their names."""
    images: dict = _undo["images"]  # type: ignore[assignment]
    restored = []
    for name, saved in images.items():
        image = bpy.data.images.get(name)
        if image is None:
            continue
        pixels = saved if saved.dtype == np.float32 else saved.astype(np.float32) / 255.0
        if pixels.size != len(image.pixels):
            continue
        write_pixels(image, pixels)
        restored.append(image.name)
    begin_undo("")
    return restored


# --- masks ---------------------------------------------------------------------------------


def key_mask(pixels: np.ndarray, colour, tolerance: float) -> np.ndarray:
    """Texels within ``tolerance`` of ``colour`` in every colour channel (display values)."""
    key = np.asarray(colour, np.float32)[:3]
    channels = min(3, pixels.shape[2])
    near = np.ones(pixels.shape[:2], bool)
    for c in range(channels):
        near &= np.abs(pixels[..., c] - key[c]) <= tolerance
    return near


def mask_from_image(mask: bpy.types.Image, shape: tuple[int, int]) -> np.ndarray:
    """``(H, W)`` bool: texels where ``mask`` is brighter than mid grey."""
    height, width = shape
    if tuple(int(v) for v in mask.size[:2]) != (width, height):
        raise CleanupError(
            f"Mask image {mask.name} is {mask.size[0]}x{mask.size[1]}, not {width}x{height}"
        )
    pixels = read_pixels(mask)
    channels = min(3, pixels.shape[2])
    return pixels[..., :channels].mean(axis=2) > 0.5


# --- operators -----------------------------------------------------------------------------


class _ImageTool:
    """Shared poll, target and reporting of the cleanup operators."""

    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        image = target_image(context)
        if image is None:
            cls.poll_message_set("Choose an image, or select an object with a base colour image")
            return False
        if image.library is not None:
            cls.poll_message_set("The image is linked from a library")
            return False
        return True

    def _islands(
        self, context: bpy.types.Context, image: bpy.types.Image
    ) -> tuple[np.ndarray | None, str]:
        coverage, triangles = image_coverage(context, image)
        if coverage is None:
            return None, "no source face uses it, so the whole image counts as one island"
        note = f"{triangles:,} triangles"
        if coverage.coarsened:
            note += f", islands rasterised at {coverage.width}x{coverage.height}"
        return coverage.inside, note


def _finish(op, context, image, message: str, started: float) -> set[str]:
    op.report({"INFO"}, f"{message} in {time.perf_counter() - started:.1f} s")
    redraw_ui(context)
    return {"FINISHED"}


class GEOTOOLS_OT_texture_fill_holes(_ImageTool, bpy.types.Operator):
    """Fill black or transparent texels inside UV islands from the island texels around them (push-pull). Undo Cleanup puts them back"""

    bl_idname = "geotools.texture_fill_holes"
    bl_label = "Fill Holes"

    def execute(self, context: bpy.types.Context) -> set[str]:
        started = time.perf_counter()
        props = settings(context)
        image = target_image(context)
        try:
            pixels = read_pixels(image)
        except CleanupError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        inside, note = self._islands(context, image)
        holes = tc.hole_mask(
            pixels,
            black=props.holes_black,
            transparent=props.holes_transparent,
            dark_threshold=props.black_level / 255.0 + 1e-6,
        )
        if inside is not None:
            holes &= inside
        holes = tc.grow_mask(holes, props.grow) if props.grow else holes
        if inside is not None:
            holes &= inside
        if not holes.any():
            self.report({"INFO"}, f"Image {image.name} has no holes inside its UV islands")
            return {"CANCELLED"}
        begin_undo("Fill Holes")
        keep_for_undo(image, pixels)
        result = tc.fill_holes(pixels, holes, inside)
        write_pixels(image, pixels)
        left = f", {result.unfilled:,} left (no island texel near)" if result.unfilled else ""
        return _finish(
            self,
            context,
            image,
            f"Filled {result.filled:,} texels of {image.name}{left} ({note})",
            started,
        )


class GEOTOOLS_OT_texture_remove_masked(_ImageTool, bpy.types.Operator):
    """Remove what the mask covers (people, vehicles, scale bars) by filling it from the texels around it. A diffusion fill: it blurs, it does not invent texture"""

    bl_idname = "geotools.texture_remove_masked"
    bl_label = "Remove Masked"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not super().poll(context):
            return False
        props = settings(context)
        if props.mask_source == "IMAGE" and props.mask_image is None:
            cls.poll_message_set("Choose a mask image, or use a key colour")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        started = time.perf_counter()
        props = settings(context)
        image = target_image(context)
        try:
            pixels = read_pixels(image)
            if props.mask_source == "IMAGE":
                mask = mask_from_image(props.mask_image, pixels.shape[:2])
            else:
                mask = key_mask(pixels, props.key_color, props.key_tolerance)
        except CleanupError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        if not mask.any():
            what = "mask image" if props.mask_source == "IMAGE" else "key colour"
            self.report({"INFO"}, f"Nothing in {image.name} is covered by the {what}")
            return {"CANCELLED"}
        inside, note = self._islands(context, image)
        mask = tc.grow_mask(mask, props.mask_grow) if props.mask_grow else mask
        if inside is not None:
            mask &= inside
        begin_undo("Remove Masked")
        keep_for_undo(image, pixels)
        result = tc.fill_holes(pixels, mask, inside)
        write_pixels(image, pixels)
        left = f", {result.unfilled:,} left (no island texel near)" if result.unfilled else ""
        return _finish(
            self,
            context,
            image,
            f"Removed {result.filled:,} masked texels of {image.name}{left} ({note})",
            started,
        )


class GEOTOOLS_OT_texture_new_mask(_ImageTool, bpy.types.Operator):
    """Make a black mask image the size of the texture and paint on it: white marks what Remove Masked fills"""

    bl_idname = "geotools.texture_new_mask"
    bl_label = "New Mask Image"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = settings(context)
        image = target_image(context)
        width, height = (int(v) for v in image.size[:2])
        mask = bpy.data.images.new(f"{image.name} mask", width, height, alpha=False)
        mask.generated_color = (0.0, 0.0, 0.0, 1.0)
        props.mask_image = mask
        props.mask_source = "IMAGE"
        paint = context.scene.tool_settings.image_paint
        paint.mode = "IMAGE"
        paint.canvas = mask
        self.report({"INFO"}, f"Paint white on {mask.name} over what to remove")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_texture_pad_islands(_ImageTool, bpy.types.Operator):
    """Pad UV islands outwards with their own edge colours, so coarse levels and mipmaps do not bleed the background in. Never changes a texel inside an island"""

    bl_idname = "geotools.texture_pad_islands"
    bl_label = "Pad Islands"

    def execute(self, context: bpy.types.Context) -> set[str]:
        started = time.perf_counter()
        props = settings(context)
        image = target_image(context)
        coverage, _triangles = image_coverage(context, image)
        if coverage is None:
            self.report({"ERROR"}, f"No source face uses {image.name}, so it has no UV islands")
            return {"CANCELLED"}
        try:
            pixels = read_pixels(image)
        except CleanupError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        inside = coverage.inside
        begin_undo("Pad Islands")
        keep_for_undo(image, pixels)
        result = tc.pad_islands(pixels, inside, props.pad_margin, fill_rest=props.pad_fill_rest)
        write_pixels(image, pixels)
        rest = f", then {result.background:,} background texels" if result.background else ""
        return _finish(
            self,
            context,
            image,
            f"Padded {image.name}: {result.padded:,} texels in {result.rings} rings{rest}",
            started,
        )


class GEOTOOLS_OT_texture_cleanup_undo(bpy.types.Operator):
    """Put back the images as they were before the last GeoTools cleanup tool ran"""

    bl_idname = "geotools.texture_cleanup_undo"
    bl_label = "Undo Cleanup"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not undo_label():
            cls.poll_message_set("No cleanup to undo")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        label = undo_label()
        restored = undo_cleanup()
        self.report({"INFO"}, f"Undid {label}: {', '.join(restored) or 'nothing'}")
        redraw_ui(context)
        return {"FINISHED"}


# --- balance -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeamSamples:
    images: list[bpy.types.Image]  # page index -> image
    page_a: np.ndarray
    page_b: np.ndarray
    uv_a: np.ndarray  # (K, 2)
    uv_b: np.ndarray
    edges: int  # seam edges found


def pages_of(objects: Iterable[bpy.types.Object]) -> list[bpy.types.Image]:
    """The base colour images (pages) of the objects' materials, sorted by name."""
    found: dict[str, bpy.types.Image] = {}
    for obj in objects:
        for slot in obj.material_slots:
            image = base_colour_image(slot.material)
            if image is not None:
                found.setdefault(image.name_full, image)
    return [found[n] for n in sorted(found)]


def seam_samples(context: bpy.types.Context, objects: list[bpy.types.Object]) -> SeamSamples:
    """Colour sample points on both sides of every 3D edge between two pages.

    Edges are matched by position (so split seams and separate objects count),
    at ``SAMPLES_PER_EDGE`` points each, moved ``INSET_TEXELS`` into each face.
    """
    images = pages_of(objects)
    page_index = {image.name_full: i for i, image in enumerate(images)}
    sizes = np.array([[max(1, i.size[0]), max(1, i.size[1])] for i in images] or [[1, 1]], float)
    depsgraph = context.evaluated_depsgraph_get()
    evaluated = [obj.evaluated_get(depsgraph) for obj in objects]
    empty = SeamSamples(images, *(np.empty(0, np.int64),) * 2, *(np.empty((0, 2)),) * 2, 0)
    if not evaluated:
        return empty
    # Vertices are matched on a grid of a millionth of the site's diagonal, so a
    # quantised position packs into one int64 (20 bits per axis).
    corners = np.concatenate([_world_box(obj) for obj in evaluated])
    origin = corners.min(axis=0)
    tol = max(float(np.linalg.norm(np.ptp(corners, axis=0))) * 1e-6, 1e-9)
    parts: list[tuple[np.ndarray, ...]] = []
    offset = 0
    for obj in evaluated:
        part = _half_edges(obj, page_index, origin, tol)
        if part is not None:
            keys, v0, v1, *rest = part
            parts.append((keys, v0 + offset, v1 + offset, *rest))
            offset += keys.size
    if not parts:
        return empty
    keys, v0, v1, uv0, uv1, page, centre = (np.concatenate(a) for a in zip(*parts, strict=True))
    _, vid = np.unique(keys, return_inverse=True)
    vid = vid.ravel()
    id0, id1 = vid[v0], vid[v1]
    lo, hi = np.minimum(id0, id1), np.maximum(id0, id1)
    edge = lo * (int(vid.max()) + 1) + hi
    order = np.argsort(edge, kind="stable")
    edge_sorted = edge[order]
    starts = np.flatnonzero(np.r_[True, edge_sorted[1:] != edge_sorted[:-1]])
    counts = np.diff(np.r_[starts, edge_sorted.size])
    pairs = starts[counts == 2]
    a, b = order[pairs], order[pairs + 1]
    keep = (page[a] != page[b]) & (lo[a] != hi[a])
    a, b = a[keep], b[keep]
    if not a.size:
        return empty
    # Orient b like a.
    flip = id0[b] != id0[a]
    ub0 = np.where(flip[:, None], uv1[b], uv0[b])
    ub1 = np.where(flip[:, None], uv0[b], uv1[b])
    t = np.linspace(0.2, 0.8, SAMPLES_PER_EDGE)[None, :, None]
    ua = uv0[a][:, None] + t * (uv1[a] - uv0[a])[:, None]
    ub = ub0[:, None] + t * (ub1 - ub0)[:, None]
    ua = _inset(ua, centre[a], sizes[page[a]])
    ub = _inset(ub, centre[b], sizes[page[b]])
    pa = np.repeat(page[a], SAMPLES_PER_EDGE)
    pb = np.repeat(page[b], SAMPLES_PER_EDGE)
    ua, ub = ua.reshape(-1, 2), ub.reshape(-1, 2)
    if pa.size > MAX_BALANCE_SAMPLES:
        pick = np.sort(np.random.default_rng(0).choice(pa.size, MAX_BALANCE_SAMPLES, replace=False))
        pa, pb, ua, ub = pa[pick], pb[pick], ua[pick], ub[pick]
    return SeamSamples(images, pa, pb, ua, ub, int(a.size))


def _inset(points: np.ndarray, centre: np.ndarray, size: np.ndarray) -> np.ndarray:
    """Move ``(E, S, 2)`` UV points ``INSET_TEXELS`` towards their face's UV centre."""
    d = (centre[:, None, :] - points) * size[:, None, :]
    length = np.linalg.norm(d, axis=2, keepdims=True)
    step = np.minimum(INSET_TEXELS, 0.5 * length)
    move = np.divide(d * step, length, out=np.zeros_like(d), where=length > 0)
    return points + move / size[:, None, :]


def _world_box(obj: bpy.types.Object) -> np.ndarray:
    """The 8 world-space corners of ``obj``'s bounding box, binary64."""
    matrix = np.array(obj.matrix_world, np.float64)
    box = np.array(obj.bound_box, np.float64)
    return box @ matrix[:3, :3].T + matrix[:3, 3]


def _half_edges(obj: bpy.types.Object, page_index: dict[str, int], origin: np.ndarray, tol: float):
    """Vertex keys, then per half-edge on a page: its two vertices, UVs, page, face UV centre.

    A vertex key is the world position quantised to ``tol`` from ``origin``,
    packed into one int64 (positions are binary64 until quantised).
    """
    mesh = obj.data
    slots = obj.material_slots
    if not len(slots) or not len(mesh.polygons):
        return None
    lut = np.full(max(1, len(slots)), -1, np.int64)
    uv_names: list[str] = []
    uv_of_slot = np.zeros(len(lut), np.int64)
    for i, slot in enumerate(slots):
        node = base_colour_node(slot.material)
        if node is None or node.image.name_full not in page_index:
            continue
        uv = node_uv_map(node, mesh)
        if uv is None:
            continue
        if uv not in uv_names:
            uv_names.append(uv)
        lut[i] = page_index[node.image.name_full]
        uv_of_slot[i] = uv_names.index(uv)
    if not uv_names:
        return None
    n_poly, n_loop, n_vert = len(mesh.polygons), len(mesh.loops), len(mesh.vertices)
    start = np.empty(n_poly, np.int32)
    total = np.empty(n_poly, np.int32)
    material = np.empty(n_poly, np.int32)
    mesh.polygons.foreach_get("loop_start", start)
    mesh.polygons.foreach_get("loop_total", total)
    mesh.polygons.foreach_get("material_index", material)
    vert = np.empty(n_loop, np.int32)
    mesh.loops.foreach_get("vertex_index", vert)
    co = np.empty(n_vert * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    matrix = np.array(obj.matrix_world, np.float64)
    world = co.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
    uvs = np.empty((len(uv_names), n_loop, 2), np.float32)
    for k, name in enumerate(uv_names):
        flat = np.empty(n_loop * 2, np.float32)
        mesh.attributes[name].data.foreach_get("vector", flat)
        uvs[k] = flat.reshape(-1, 2)
    slot = np.clip(material, 0, len(lut) - 1)
    poly = np.repeat(np.arange(n_poly), total)
    position = np.arange(n_loop) - start[poly]
    following = start[poly] + (position + 1) % total[poly]
    page = lut[slot][poly]
    which = uv_of_slot[slot][poly]
    uv = uvs[which, np.arange(n_loop)]
    keep = page >= 0
    loops = np.flatnonzero(keep)
    centre_poly = np.add.reduceat(uv, start, axis=0) / total[:, None] if n_poly else uv[:0]
    q = np.clip(np.rint((world - origin) / tol), 0, (1 << 20) - 1).astype(np.int64)
    keys = (q[:, 0] << 40) | (q[:, 1] << 20) | q[:, 2]
    return (
        keys,
        vert[loops].astype(np.int64),
        vert[following[loops]].astype(np.int64),
        uv[loops].astype(np.float64),
        uvs[which[loops], following[loops]].astype(np.float64),
        page[loops],
        centre_poly[poly[loops]].astype(np.float64),
    )


def _linearise(image: bpy.types.Image) -> bool:
    """Whether the image's pixel values are sRGB-encoded colour (fitted in linear light)."""
    return not image.is_float and not image.colorspace_settings.is_data


def page_colours(samples: SeamSamples) -> tuple[np.ndarray, np.ndarray]:
    """Linear-light RGB at each sample, reading each page's pixels once."""
    ca = np.zeros((samples.page_a.size, 3))
    cb = np.zeros((samples.page_b.size, 3))
    for index, image in enumerate(samples.images):
        on_a = samples.page_a == index
        on_b = samples.page_b == index
        if not (on_a.any() or on_b.any()):
            continue
        pixels = read_pixels(image)
        rgb = pixels[..., :3] if pixels.shape[2] >= 3 else np.repeat(pixels[..., :1], 3, axis=2)
        values_a = tc.bilinear_sample(rgb, samples.uv_a[on_a])
        values_b = tc.bilinear_sample(rgb, samples.uv_b[on_b])
        del pixels, rgb
        if _linearise(image):
            values_a, values_b = tc.srgb_to_linear(values_a), tc.srgb_to_linear(values_b)
        ca[on_a], cb[on_b] = values_a, values_b
    return ca, cb


def remove_preview(materials: Iterable[bpy.types.Material]) -> int:
    """Take the balance preview nodes out of ``materials``, relinking around them."""
    removed = 0
    for material in materials:
        if material is None or material.node_tree is None:
            continue
        tree = material.node_tree
        for node in [n for n in tree.nodes if n.name.startswith(PREVIEW_NODE)]:
            source = node.inputs[0].links[0].from_socket if node.inputs[0].links else None
            targets = [link.to_socket for link in node.outputs[0].links]
            tree.nodes.remove(node)
            if source is not None:
                for socket in targets:
                    tree.links.new(source, socket)
            removed += 1
    return removed


def add_preview(material: bpy.types.Material, gain, offset) -> bool:
    """Insert ``colour * gain + offset`` (linear) between the base colour image and the BSDF."""
    remove_preview([material])
    node = base_colour_node(material)
    principled = principled_of(material)
    if node is None or principled is None:
        return False
    tree = material.node_tree
    target = principled.inputs["Base Color"]
    link = target.links[0] if target.links else None
    if link is None:
        return False
    source = node.outputs["Color"]
    preview = tree.nodes.new("ShaderNodeVectorMath")
    preview.name = preview.label = PREVIEW_NODE
    preview.operation = "MULTIPLY_ADD"
    preview.location = (node.location.x + 260, node.location.y - 40)
    preview.inputs[1].default_value = tuple(gain)
    preview.inputs[2].default_value = tuple(offset)
    from_socket = link.from_socket if link.from_node.bl_idname == "NodeReroute" else source
    tree.links.new(from_socket, preview.inputs[0])
    tree.links.new(preview.outputs[0], target)
    return True


def _page_materials(
    objects: Iterable[bpy.types.Object], image: bpy.types.Image
) -> list[bpy.types.Material]:
    found: dict[str, bpy.types.Material] = {}
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material is not None and base_colour_image(slot.material) == image:
                found.setdefault(slot.material.name_full, slot.material)
    return list(found.values())


def _all_materials(objects: Iterable[bpy.types.Object]) -> list[bpy.types.Material]:
    found: dict[str, bpy.types.Material] = {}
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material is not None:
                found.setdefault(slot.material.name_full, slot.material)
    return list(found.values())


def _identity(gain, offset) -> bool:
    return bool(np.allclose(gain, 1.0, atol=1e-4) and np.allclose(offset, 0.0, atol=1e-5))


class GEOTOOLS_OT_texture_balance_fit(bpy.types.Operator):
    """Fit a gain and offset per page (image) so colours agree across the seams where pages meet in 3D, and preview it in the materials"""

    bl_idname = "geotools.texture_balance_fit"
    bl_label = "Fit Balance"
    bl_options = {"REGISTER"}

    preview: BoolProperty(
        name="Preview",
        description="Show the correction with a shader node in each page's materials "
        "(Material Preview or Rendered shading); Apply or Clear removes it",
        default=True,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None or not candidate_objects(context):
            cls.poll_message_set("Set a source collection, or select a mesh")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        started = time.perf_counter()
        props = settings(context)
        objects = candidate_objects(context)
        samples = seam_samples(context, objects)
        if len(samples.images) < 2:
            self.report({"ERROR"}, "Balance needs at least two pages (base colour images)")
            return {"CANCELLED"}
        if not samples.page_a.size:
            self.report({"ERROR"}, "No 3D edge joins two pages, so there is nothing to match")
            return {"CANCELLED"}
        try:
            ca, cb = page_colours(samples)
        except CleanupError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        counts = np.bincount(samples.page_a, minlength=len(samples.images)) + np.bincount(
            samples.page_b, minlength=len(samples.images)
        )
        names = [i.name_full for i in samples.images]
        reference = props.balance_reference
        ref = (
            names.index(reference.name_full) if reference and reference.name_full in names else None
        )
        if ref is None:
            ref = int(np.argmax(counts))
        fit = tc.fit_balance(
            samples.page_a, samples.page_b, ca, cb, len(samples.images), reference=ref
        )
        remove_preview(_all_materials(objects))
        props.balance_pages.clear()
        for index, image in enumerate(samples.images):
            item = props.balance_pages.add()
            item.image = image
            item.gain = tuple(float(v) for v in fit.gain[index])
            item.offset = tuple(float(v) for v in fit.offset[index])
            item.samples = int(fit.samples[index])
            item.connected = bool(fit.connected[index])
            item.reference = index == ref
            if self.preview and not _identity(item.gain, item.offset):
                for material in _page_materials(objects, image):
                    add_preview(material, item.gain, item.offset)
        props.balance_rms_before = 255.0 * fit.rms_before
        props.balance_rms_after = 255.0 * fit.rms_after
        props.balance_samples = int(samples.page_a.size)
        props.balance_preview = self.preview
        invalidate()
        loose = [samples.images[i].name for i in np.flatnonzero(~fit.connected)]
        note = f"; not linked to the reference: {', '.join(loose)}" if loose else ""
        self.report(
            {"WARNING" if loose else "INFO"},
            f"Fitted {len(samples.images)} pages on {samples.page_a.size:,} seam samples "
            f"({samples.edges:,} edges): seam error {props.balance_rms_before:.1f} -> "
            f"{props.balance_rms_after:.1f} (linear, of 255) in "
            f"{time.perf_counter() - started:.1f} s{note}",
        )
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_texture_balance_apply(bpy.types.Operator):
    """Apply the fitted gain and offset to each page's pixels and remove the preview. Undo Cleanup puts the pages back"""

    bl_idname = "geotools.texture_balance_apply"
    bl_label = "Apply Balance"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None or not len(settings(context).balance_pages):
            cls.poll_message_set("Fit the balance first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        started = time.perf_counter()
        props = settings(context)
        objects = candidate_objects(context)
        begin_undo("Apply Balance")
        changed = []
        for item in props.balance_pages:
            image = item.image
            if image is None or _identity(item.gain, item.offset) or image.library is not None:
                continue
            try:
                pixels = read_pixels(image)
            except CleanupError as ex:
                self.report({"ERROR"}, str(ex))
                return {"CANCELLED"}
            keep_for_undo(image, pixels)
            apply_encoded(pixels, item.gain, item.offset, _linearise(image), not image.is_float)
            write_pixels(image, pixels)
            changed.append(image.name)
        remove_preview(_all_materials(objects))
        props.balance_pages.clear()
        props.balance_preview = False
        self.report(
            {"INFO"},
            f"Balanced {len(changed)} page{'s' * (len(changed) != 1)}"
            f"{': ' + ', '.join(changed) if changed else ''} in "
            f"{time.perf_counter() - started:.1f} s",
        )
        redraw_ui(context)
        return {"FINISHED"}


def apply_encoded(
    pixels: np.ndarray, gain, offset, linearise: bool, clamp: bool, rows: int = 512
) -> None:
    """Apply ``gain * c + offset`` in linear light to RGB, in place.

    For 8-bit images (``clamp``) every channel maps 256 levels to 256 values,
    so a lookup table per channel does it in one gather; float images are
    worked through in row blocks.
    """
    gain = np.asarray(gain, np.float64)
    offset = np.asarray(offset, np.float64)
    channels = min(3, pixels.shape[2])
    if clamp:
        levels = np.arange(256, dtype=np.float64) / 255.0
        base = tc.srgb_to_linear(levels) if linearise else levels
        for c in range(channels):
            mapped = base * gain[c] + offset[c]
            if linearise:
                mapped = tc.linear_to_srgb(mapped)
            lut = np.clip(mapped, 0.0, 1.0).astype(np.float32)
            for y in range(0, pixels.shape[0], rows):
                block = pixels[y : y + rows, :, c]
                index = np.rint(block * 255.0).astype(np.uint8)
                block[...] = lut[index]
        return
    for y in range(0, pixels.shape[0], rows):
        block = pixels[y : y + rows, :, :channels]
        values = tc.srgb_to_linear(block) if linearise else block.copy()
        values = values * gain[:channels].astype(np.float32) + offset[:channels].astype(np.float32)
        if linearise:
            values = tc.linear_to_srgb(values)
        block[...] = values


class GEOTOOLS_OT_texture_balance_clear(bpy.types.Operator):
    """Forget the fitted balance and remove its preview nodes; no pixel changes"""

    bl_idname = "geotools.texture_balance_clear"
    bl_label = "Clear Balance"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene is not None and (
            len(settings(context).balance_pages) > 0 or settings(context).balance_preview
        )

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = settings(context)
        removed = remove_preview(_all_materials(candidate_objects(context)))
        props.balance_pages.clear()
        props.balance_preview = False
        invalidate()
        self.report({"INFO"}, f"Cleared the balance ({removed} preview nodes removed)")
        redraw_ui(context)
        return {"FINISHED"}


classes = (
    GeoToolsBalancePage,
    GeoToolsTextureSettings,
    GEOTOOLS_OT_texture_fill_holes,
    GEOTOOLS_OT_texture_remove_masked,
    GEOTOOLS_OT_texture_new_mask,
    GEOTOOLS_OT_texture_pad_islands,
    GEOTOOLS_OT_texture_cleanup_undo,
    GEOTOOLS_OT_texture_balance_fit,
    GEOTOOLS_OT_texture_balance_apply,
    GEOTOOLS_OT_texture_balance_clear,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools_texture = PointerProperty(type=GeoToolsTextureSettings)


def unregister() -> None:
    del bpy.types.Scene.geotools_texture
    _coverage.clear()
    begin_undo("")
    _unregister_classes()
