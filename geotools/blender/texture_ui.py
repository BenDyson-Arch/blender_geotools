"""Sidebar panels of M6 texture cleanup: Textures, with Paint, Cleanup and Balance.

Registered by ``panels`` between Layers and Checks. They draw stored settings
and results only; the operators (``texture_paint``, ``texture_cleanup``) work.
"""

from __future__ import annotations

import bpy

from .texture_cleanup import settings, target_image, undo_label
from .texture_state import has_unsaved_edits
from .ui import big_button, split_column, wrapped


class _TexturePanel:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"


class GEOTOOLS_PT_textures(_TexturePanel, bpy.types.Panel):
    """Paint and clean the source textures before publishing."""

    bl_idname = "GEOTOOLS_PT_textures"
    bl_label = "Textures"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = settings(context)
        layout.prop(props, "image", text="", placeholder="Active object's base colour")
        image = target_image(context)
        if image is None:
            wrapped(
                layout,
                context,
                "Select a mesh whose material has a base colour image, or choose an image.",
                "INFO",
            )
        else:
            width, height = (int(v) for v in image.size[:2])
            text = f"{image.name}: {width}x{height}"
            icon = "IMAGE_DATA"
            if has_unsaved_edits(image):
                text += ", unsaved edits"
                icon = "FILE_TICK"
            layout.label(text=text, icon=icon)
        label = undo_label()
        if label:
            layout.operator("geotools.texture_cleanup_undo", text=f"Undo {label}", icon="LOOP_BACK")


class GEOTOOLS_PT_textures_paint(_TexturePanel, bpy.types.Panel):
    """Blender's texture painting, set up for the source texture."""

    bl_idname = "GEOTOOLS_PT_textures_paint"
    bl_label = "Paint"
    bl_parent_id = "GEOTOOLS_PT_textures"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = settings(context)
        layout.prop(props, "protect_overlaps")
        button = big_button(layout)
        button.operator("geotools.texture_paint_setup", icon="BRUSH_DATA")
        if props.overlap_texels > 0:
            col = layout.column()
            col.alert = True
            wrapped(col, context, props.overlap_note, "ERROR")
        elif props.overlap_texels == 0:
            layout.label(text="No UV overlaps", icon="CHECKMARK")
        wrapped(
            layout,
            context,
            "Clone, smear, soften and masks are Blender's brushes; see the "
            "texture cleanup recipes.",
            "INFO",
        )


class GEOTOOLS_PT_textures_cleanup(_TexturePanel, bpy.types.Panel):
    """Fill holes, remove masked objects and pad UV islands."""

    bl_idname = "GEOTOOLS_PT_textures_cleanup"
    bl_label = "Cleanup"
    bl_parent_id = "GEOTOOLS_PT_textures"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = settings(context)

        box = layout.box()
        box.label(text="Fill Holes", icon="MOD_MASK")
        row = box.row(align=True)
        row.prop(props, "holes_black", toggle=True)
        row.prop(props, "holes_transparent", toggle=True)
        col = split_column(box)
        col.prop(props, "black_level")
        col.prop(props, "grow")
        box.operator("geotools.texture_fill_holes", icon="BRUSH_DATA")

        box = layout.box()
        box.label(text="Remove Masked Objects", icon="GHOST_DISABLED")
        box.row().prop(props, "mask_source", expand=True)
        col = split_column(box)
        if props.mask_source == "KEY":
            col.prop(props, "key_color")
            col.prop(props, "key_tolerance")
        else:
            row = col.row(align=True)
            row.prop(props, "mask_image", text="Mask")
            row.operator("geotools.texture_new_mask", text="", icon="ADD")
        col.prop(props, "mask_grow")
        box.operator("geotools.texture_remove_masked", icon="BRUSH_DATA")
        wrapped(box, context, "A smooth fill: it blurs; it does not invent texture.", "INFO")

        box = layout.box()
        box.label(text="Pad Islands", icon="MOD_EDGESPLIT")
        col = split_column(box)
        col.prop(props, "pad_margin")
        col.prop(props, "pad_fill_rest")
        box.operator("geotools.texture_pad_islands", icon="BRUSH_DATA")


class GEOTOOLS_PT_textures_balance(_TexturePanel, bpy.types.Panel):
    """Match colour and exposure across pages (images) where they meet in 3D."""

    bl_idname = "GEOTOOLS_PT_textures_balance"
    bl_label = "Balance"
    bl_parent_id = "GEOTOOLS_PT_textures"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        props = settings(context)
        col = split_column(layout)
        col.prop(props, "balance_reference", placeholder="Automatic")
        layout.operator("geotools.texture_balance_fit", icon="SEQ_HISTOGRAM")
        if not len(props.balance_pages):
            return
        box = layout.box()
        box.label(
            text=f"Seam error {props.balance_rms_before:.1f} -> {props.balance_rms_after:.1f} "
            f"of 255 ({props.balance_samples:,} samples)"
        )
        for item in props.balance_pages:
            name = item.image.name if item.image is not None else "?"
            if item.reference:
                box.label(text=f"{name}: reference", icon="PINNED")
                continue
            gain = ", ".join(f"{v:.3f}" for v in item.gain)
            offset = ", ".join(f"{v:+.3f}" for v in item.offset)
            icon = "IMAGE_DATA" if item.connected else "UNLINKED"
            box.label(text=f"{name}: gain {gain}", icon=icon)
            box.label(text=f"    offset {offset}, {item.samples:,} samples")
        if props.balance_preview:
            box.label(text="Previewed in Material Preview shading", icon="HIDE_OFF")
        row = layout.row(align=True)
        row.operator("geotools.texture_balance_apply", icon="CHECKMARK")
        row.operator("geotools.texture_balance_clear", icon="X")


classes = (
    GEOTOOLS_PT_textures,
    GEOTOOLS_PT_textures_paint,
    GEOTOOLS_PT_textures_cleanup,
    GEOTOOLS_PT_textures_balance,
)
