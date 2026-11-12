"""Image remedies: set or clear one, and the image list of the Images sub-panel.

A remedy is the user's explicit choice of how a source image that rusty-tiles
would refuse is made publishable (see ``core.images``). It is stored on the
image (``image.geotools_remedy``) and only read when exporting.
"""

import bpy
from bpy.props import EnumProperty, StringProperty

from ..core import gltf_writer
from ..core.images import default_max_edge, is_lossless_profile
from .checks import active_rusty_tiles_profile, run_checks, store_issues
from .common import redraw_ui
from .properties import REMEDY_ITEMS, image_remedy
from .summarise import image_summary

# Issue codes about one image, whose subject is the image name.
SIZE_CODES = frozenset(("image-file-too-large", "image-png-size"))
IMAGE_CODES = SIZE_CODES | {"image-too-large", "image-16-bit", "image-remedy", "image-remedy-lossy"}

BUTTONS = {
    "JPEG": ("Re-encode as JPEG", "IMAGE_RGB"),
    "DOWNSCALE": ("Downscale", "FULLSCREEN_EXIT"),
    "CONVERT_8BIT": ("Convert to 8-bit", "IMAGE_RGB_ALPHA"),
    "NONE": ("Clear Remedy", "X"),
}

_DESCRIPTIONS = {
    "JPEG": "Publish a JPEG copy of this image (quality 95 unless changed in the Images "
    "panel). Lossy: small next to the fast, balanced and quality profiles' own compression, "
    "but it defeats the lossless profile. The image and its file are not changed",
    "DOWNSCALE": "Publish a copy scaled to the largest power of two expected to fit (4096 "
    "if the size is unknown), as PNG, or as JPEG if the PNG is still over 32 MiB. The image "
    "and its file are not changed",
    "CONVERT_8BIT": "Publish an 8-bit PNG copy of this 16-bit image. The image and its "
    "file are not changed",
    "NONE": "Publish this image as it is again; checks refuse it if rusty-tiles would",
}


def _find_image(name: str) -> bpy.types.Image | None:
    return bpy.data.images.get(name)


def _rerun_checks(context: bpy.types.Context, image_name: str) -> None:
    """Refresh the issue list and keep the image's first issue selected."""
    props = context.scene.geotools
    if props.source_collection is None:
        return
    store_issues(props, run_checks(context))
    for index, item in enumerate(props.issues):
        if item.subject == image_name and item.code in IMAGE_CODES:
            props.issues_index = index
            break
    for index, item in enumerate(props.summary_images):
        if item.name == image_name:
            props.summary_images_index = index
            break


class GEOTOOLS_OT_set_image_remedy(bpy.types.Operator):
    """Choose how an image is made publishable; the image itself is never changed"""

    bl_idname = "geotools.set_image_remedy"
    bl_label = "Set Image Remedy"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    image: StringProperty(name="Image", description="Name of the image")
    remedy: EnumProperty(
        name="Remedy",
        description="How the published copy of the image is made",
        items=REMEDY_ITEMS,
        default="NONE",
    )

    @classmethod
    def description(cls, _context: bpy.types.Context, properties) -> str:
        return _DESCRIPTIONS[properties.remedy]

    def execute(self, context: bpy.types.Context) -> set[str]:
        image = _find_image(self.image)
        if image is None:
            self.report({"ERROR"}, f"There is no image {self.image!r}")
            return {"CANCELLED"}
        if image.library is not None:
            self.report(
                {"ERROR"},
                f"Image {image.name} is linked from {image.library.filepath}; make it local "
                "to choose a remedy",
            )
            return {"CANCELLED"}
        props = image.geotools_remedy
        props.kind = self.remedy
        if self.remedy == "DOWNSCALE":
            summary = image_summary(image)
            props.max_edge = default_max_edge(
                summary.width,
                summary.height,
                summary.file_bytes,
                gltf_writer.MAX_IMAGE_BYTES,
                gltf_writer.MAX_IMAGE_EDGE,
            )
        remedy = image_remedy(image)
        _rerun_checks(context, image.name)
        redraw_ui(context)
        if not remedy.active:
            self.report({"INFO"}, f"Image {image.name} is published as it is")
        elif remedy.lossy and is_lossless_profile(
            active_rusty_tiles_profile(context.scene.geotools)
        ):
            self.report(
                {"WARNING"},
                f"Image {image.name} is published from a copy {remedy.describe()}, which is "
                "lossy in a lossless build",
            )
        else:
            width, height = (int(v) for v in image.size[:2])
            self.report(
                {"INFO"},
                f"Image {image.name} is published from a copy {remedy.describe(width, height)}",
            )
        return {"FINISHED"}


def draw_remedy_buttons(layout: bpy.types.UILayout, code: str, image_name: str) -> None:
    """Remedy buttons for one image issue of the Checks panel."""
    image = _find_image(image_name)
    if image is None or code not in IMAGE_CODES:
        return
    if code in SIZE_CODES:
        kinds = ["JPEG", "DOWNSCALE"]
    elif code == "image-too-large":
        kinds = ["DOWNSCALE"]
    elif code == "image-16-bit":
        kinds = ["CONVERT_8BIT"]
    else:
        kinds = []
    current = image.geotools_remedy.kind
    kinds = [k for k in kinds if k != current]
    if current != "NONE":
        kinds.append("NONE")
    if not kinds:
        return
    row = layout.row(align=True)
    row.enabled = image.library is None
    for kind in kinds:
        text, icon = BUTTONS[kind]
        op = row.operator(GEOTOOLS_OT_set_image_remedy.bl_idname, text=text, icon=icon)
        op.image = image.name
        op.remedy = kind


def format_mib(file_bytes: float) -> str:
    return "" if file_bytes < 0 else f"{file_bytes / (1 << 20):.1f} MiB"


def describe_image(item: bpy.types.PropertyGroup) -> str:
    """The cached facts of one source image: "8192x8192 PNG, 120.4 MiB, 16-bit"."""
    parts = [f"{item.width}x{item.height} {item.file_format}"]
    if item.file_bytes >= 0:
        parts.append(format_mib(item.file_bytes))
    else:
        parts.append("written by GeoTools")
    if item.bit_depth and item.bit_depth != 8:
        parts.append(f"{item.bit_depth}-bit")
    return ", ".join(parts)


def remedy_label(image: bpy.types.Image | None) -> str:
    """A short name for the image's remedy, "" if none: "JPEG 95", "Max 4096", "8-bit"."""
    if image is None:
        return ""
    props = image.geotools_remedy
    return {
        "NONE": "",
        "JPEG": f"JPEG {props.quality}",
        "DOWNSCALE": f"Max {props.max_edge}",
        "CONVERT_8BIT": "8-bit",
    }[props.kind]


class GEOTOOLS_UL_images(bpy.types.UIList):
    """Source images with their remedy."""

    bl_idname = "GEOTOOLS_UL_images"

    def draw_item(
        self,
        context: bpy.types.Context,
        layout: bpy.types.UILayout,
        data: bpy.types.AnyType,
        item: bpy.types.PropertyGroup,
        icon: int,
        active_data: bpy.types.AnyType,
        active_property: str,
        index: int = 0,
        flt_flag: int = 0,
    ) -> None:
        image = _find_image(item.name)
        row = layout.row(align=True)
        row.label(text=item.name, icon="IMAGE_DATA" if image is not None else "ERROR")
        label = remedy_label(image)
        if label:
            row.label(text=label, icon="MODIFIER")


classes = (GEOTOOLS_OT_set_image_remedy, GEOTOOLS_UL_images)

register, unregister = bpy.utils.register_classes_factory(classes)
