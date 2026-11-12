"""One click into Texture Paint on a source texture, with UV overlaps masked.

Painting is Blender's own projection paint. ``geotools.texture_paint_setup``
only sets what Blender exposes, with defaults suited to photogrammetry:

- Texture Paint mode on the object, in single-image mode with the base colour
  image as the canvas, painting on the UV map that image uses;
- projection paint with occlusion and backface culling on, so a stroke does
  not reach through the model, and normal falloff on;
- the 3D views in Solid shading with Flat lighting and Texture colour, so the
  texture is seen without lighting influence.

**UV overlaps.** Overlapping UV islands share texels, so painting one place
also changes another. The operator rasterises the faces that use the image
(``core.uv_overlap.texel_coverage``, the rule of the ``uv-overlap`` check) and,
when some texels are used twice, warns and writes an overlap mask image (black
where texels overlap, white elsewhere). With **Protect Overlaps** on, the mask
is the paint stencil (Texture Paint > Masking > Stencil Mask): strokes cannot
change overlapping texels, and the viewport's mask overlay shows them.
"""

from __future__ import annotations

import time

import bpy
import numpy as np
from bpy.props import BoolProperty

from .common import redraw_ui
from .texture_cleanup import (
    base_colour_node,
    image_coverage,
    node_uv_map,
    settings,
    target_image,
)

OVERLAP_PREFIX = "GeoTools overlaps "
"""Name prefix of the overlap mask image of a texture."""


def overlap_image_name(image: bpy.types.Image) -> str:
    return f"{OVERLAP_PREFIX}{image.name}"[:63]


def write_overlap_mask(image: bpy.types.Image, overlapping: np.ndarray) -> bpy.types.Image:
    """The overlap mask of ``image``: black where texels overlap, white elsewhere."""
    height, width = overlapping.shape
    name = overlap_image_name(image)
    mask = bpy.data.images.get(name)
    if mask is not None and tuple(mask.size) != (width, height):
        bpy.data.images.remove(mask)
        mask = None
    if mask is None:
        mask = bpy.data.images.new(name, width, height, alpha=False)
    pixels = np.ones((height, width, 4), np.float32)
    pixels[overlapping, :3] = 0.0
    mask.pixels.foreach_set(pixels.ravel())
    mask.update()
    return mask


def _shade_flat(context: bpy.types.Context) -> None:
    """Solid shading, Flat lighting, Texture colour in every 3D view of the screen."""
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type != "VIEW_3D":
            continue
        for space in area.spaces:
            if space.type == "VIEW_3D":
                shading = space.shading
                shading.type = "SOLID"
                shading.light = "FLAT"
                shading.color_type = "TEXTURE"


class GEOTOOLS_OT_texture_paint_setup(bpy.types.Operator):
    """Paint the active object's base colour texture: Texture Paint with projection settings for photogrammetry, no lighting in the view, and UV overlaps masked"""

    bl_idname = "geotools.texture_paint_setup"
    bl_label = "Paint Texture"
    bl_options = {"REGISTER", "UNDO"}

    protect_overlaps: BoolProperty(
        name="Protect Overlaps",
        description="Use the overlap mask as the paint stencil, so overlapping texels "
        "cannot be painted",
        default=True,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            cls.poll_message_set("Select the mesh to paint")
            return False
        if target_image(context) is None:
            cls.poll_message_set("The active material has no base colour image")
            return False
        return True

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        self.protect_overlaps = settings(context).protect_overlaps
        return self.execute(context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        started = time.perf_counter()
        props = settings(context)
        obj = context.active_object
        image = target_image(context)
        mesh = obj.data
        node = None
        for slot in obj.material_slots:
            candidate = base_colour_node(slot.material)
            if candidate is not None and candidate.image == image:
                node = candidate
                break
        uv_name = node_uv_map(node, mesh)
        if uv_name is None:
            self.report({"ERROR"}, f"{obj.name} has no UV map to paint on")
            return {"CANCELLED"}

        # The UV map single-image painting uses is the active one.
        mesh.uv_layers.active = mesh.uv_layers[uv_name]
        if context.view_layer.objects.active is not obj:
            context.view_layer.objects.active = obj
        obj.select_set(True)
        if obj.mode != "TEXTURE_PAINT":
            bpy.ops.object.mode_set(mode="TEXTURE_PAINT")

        paint = context.scene.tool_settings.image_paint
        paint.mode = "IMAGE"
        paint.canvas = image
        paint.use_occlude = True
        paint.use_backface_culling = True
        paint.use_normal_falloff = True
        paint.seam_bleed = max(paint.seam_bleed, 2)
        paint.dither = 0.0
        _shade_flat(context)

        # Every object sharing the image counts: a stroke on one changes the others.
        coverage, _triangles = image_coverage(context, image)
        overlapping = coverage.overlapping if coverage is not None else None
        count = int(np.count_nonzero(overlapping)) if overlapping is not None else 0
        props.overlap_texels = count
        props.protect_overlaps = self.protect_overlaps
        old = bpy.data.images.get(overlap_image_name(image))
        if count:
            mask = write_overlap_mask(image, overlapping)
            grid = ""
            if coverage.coarsened:
                grid = f" (UVs rasterised at {coverage.width}x{coverage.height})"
            props.overlap_note = (
                f"{count:,} texels are shared by overlapping UVs{grid}: painting one place "
                "changes another"
            )
            if self.protect_overlaps:
                paint.stencil_image = mask
                paint.use_stencil_layer = True
                paint.invert_stencil = False
                mesh.uv_layer_stencil = mesh.uv_layers[uv_name]
                props.overlap_note += "; they are masked by the stencil"
            elif paint.stencil_image == mask:
                paint.use_stencil_layer = False
            self.report({"WARNING"}, f"{image.name}: {props.overlap_note}")
        else:
            props.overlap_note = ""
            if old is not None:
                if paint.stencil_image == old:
                    paint.stencil_image = None
                    paint.use_stencil_layer = False
                bpy.data.images.remove(old)
            self.report(
                {"INFO"},
                f"Painting {image.name} on UV map {uv_name}; no UV overlaps "
                f"({time.perf_counter() - started:.1f} s)",
            )
        redraw_ui(context)
        return {"FINISHED"}


classes = (GEOTOOLS_OT_texture_paint_setup,)

register, unregister = bpy.utils.register_classes_factory(classes)
