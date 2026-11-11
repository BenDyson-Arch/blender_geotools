"""Layer presentation in Blender: colours for a variant tileset (design section 4).

A layer's presentation (``GeoToolsLayer.presentation``) says how its values are
made visible: a colour ramp (or one colour per category) into COLOR_0 for FACE
and CORNER layers, or into an 8-bit base-colour or emissive texture for
TEXTURE layers. :func:`write_presentation` turns the values into colours:

* COLOR_0: the ``gt_present`` byte colour attribute (CORNER domain) on every
  source mesh. A mesh without the layer's attribute is painted with the
  no-data colour. The mesh's active and render colour attributes are put back
  as they were, so adding ``gt_present`` never changes what the user sees or
  what the plain tileset publishes (the exporter skips ``gt_present`` without
  a presentation).
* BASE_COLOR and EMISSIVE: one 8-bit sRGB image per object, the same size as
  the object's float layer image, listed in ``presentation.images``. They are
  not saved: every publish of the variant writes them again.

The user's materials are never touched; ``export.extract_scene`` publishes the
presentation in place of them (see ``export.Presentation``). Everything goes
through numpy and ``foreach_get``/``foreach_set``; nothing loops over faces,
corners or texels in Python.

``geotools.publish`` publishes a variant (``<stem>.<layer>.3tz``) when its
``layer`` property or the build profile's ``variant_layer`` names a layer. It
runs :func:`variant_issues` with the other checks, then
:func:`write_presentation`, and records the variant in build.json.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

import bpy
import numpy as np

from ..core.issues import Issue, Severity
from ..core.layers import DataType, Domain, LayerSpec, parse_number, validate
from ..core.presentation import (
    PRESENT_ATTRIBUTE,
    Blend,
    Target,
    presentation_issues,
    targets_for,
    variant_output,
    variant_record,
)
from ..core.ramps import apply_categories, apply_ramp, auto_range, default_category_colours
from .common import redraw_ui
from .export import Presentation
from .layers import (
    attribute_problem,
    find_layer,
    other_layer_names,
    read_layer,
    source_meshes,
    source_objects,
    spec_from_props,
)
from .properties import PRESENT_TARGET_ITEMS

log = logging.getLogger(__name__)

# Issue codes of variant_issues, besides core.presentation's.
VARIANT_LAYER_MISSING = "variant-layer-missing"
VARIANT_LAYER_INVALID = "variant-layer-invalid"
VARIANT_DATA_MISSING = "variant-data-missing"
VARIANT_DATA_MISMATCH = "variant-data-mismatch"
VARIANT_NO_DATA = "variant-no-data"
VARIANT_IMAGE_MISSING = "variant-image-missing"
VARIANT_UV_MAP_MISSING = "variant-uv-map-missing"
VARIANT_EDIT_MODE = "variant-edit-mode"

BOOLEAN_RANGE = (0.0, 1.0)


class PresentationError(RuntimeError):
    """The layer cannot be presented (no values, a bad range, a missing image)."""


# --- settings ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PresentationSettings:
    """A layer's presentation settings, parsed (``range_*`` None means automatic)."""

    target: Target
    blend: Blend
    palette: str
    range_min: float | None
    range_max: float | None
    nodata_rgba: tuple[int, int, int, int]


def _byte(value: float) -> int:
    return int(min(max(round(float(value) * 255.0), 0), 255))


def presentation_settings(item: bpy.types.PropertyGroup) -> PresentationSettings:
    """The settings of a ``GeoToolsLayer``'s presentation. Never raises: number
    text that is not a number becomes NaN, which :func:`variant_issues` reports."""
    p = item.presentation
    return PresentationSettings(
        target=Target(p.target),
        blend=Blend(p.blend),
        palette=p.palette,
        range_min=parse_number(p.range_min),
        range_max=parse_number(p.range_max),
        nodata_rgba=(*(_byte(c) for c in p.nodata_color), 255),  # type: ignore[arg-type]
    )


def category_colours(item: bpy.types.PropertyGroup, count: int) -> np.ndarray:
    """``(count, 4) uint8`` colours of categories 0 to count - 1: the presentation's
    colours, then ``core.ramps.default_category_colours`` for the rest."""
    colours = default_category_colours(count)
    stored = item.presentation.category_colors
    n = min(len(stored), count)
    if n:
        rgb = np.empty(len(stored) * 3, dtype=np.float32)
        stored.foreach_get("color", rgb)
        colours[:n, :3] = np.clip(np.round(rgb.reshape(-1, 3)[:n] * 255.0), 0, 255)
    return colours


# --- checks -----------------------------------------------------------------------------


def variant_issues(scene: bpy.types.Scene, layer_name: str) -> list[Issue]:
    """Why the layer ``layer_name`` cannot be published as a variant, without reading values.

    Errors: the layer is missing or invalid, its target does not suit its domain,
    the palette or range is bad, a source mesh is in Edit Mode or holds the
    attribute with the wrong domain or type, no source mesh holds it, or (for a
    TEXTURE layer) an object has no image or UV map. A mesh without the
    attribute is a warning: it is painted with the no-data colour.
    """
    item = find_layer(scene, layer_name)
    if item is None:
        return [
            Issue(
                Severity.ERROR,
                VARIANT_LAYER_MISSING,
                f"The variant layer {layer_name!r} is not in the layer registry.",
                layer_name or None,
            )
        ]
    spec = spec_from_props(item)
    issues: list[Issue] = []
    errors = [
        i for i in validate(spec, other_layer_names(scene, item)) if i.severity is Severity.ERROR
    ]
    if errors:
        issues.append(
            Issue(
                Severity.ERROR,
                VARIANT_LAYER_INVALID,
                f"Layer {spec.name!r} cannot be presented: {errors[0].message}",
                spec.name,
            )
        )
    settings = presentation_settings(item)
    issues.extend(
        presentation_issues(
            spec, settings.target, settings.palette, settings.range_min, settings.range_max
        )
    )
    objects = source_objects(scene)
    editing = [o.name for o in objects if o.data.is_editmode]
    if editing:
        issues.append(
            Issue(
                Severity.ERROR,
                VARIANT_EDIT_MODE,
                f"{editing[0]} is in Edit Mode; switch to Object Mode to publish a variant.",
                editing[0],
            )
        )
    if Domain(spec.domain) is Domain.TEXTURE:
        issues.extend(_texture_issues(item, spec, objects))
    else:
        issues.extend(_attribute_issues(scene, spec))
    return issues


def _attribute_issues(scene: bpy.types.Scene, spec: LayerSpec) -> list[Issue]:
    issues: list[Issue] = []
    meshes = source_meshes(scene)
    held = 0
    for mesh in meshes:
        problem = attribute_problem(mesh, spec)
        if problem is None:
            held += 1
        elif problem == "mismatch":
            issues.append(
                Issue(
                    Severity.ERROR,
                    VARIANT_DATA_MISMATCH,
                    f"Mesh {mesh.name}: gt_{spec.name} has the wrong domain or type for layer "
                    f"{spec.name}.",
                    mesh.name,
                )
            )
        else:
            issues.append(
                Issue(
                    Severity.WARNING,
                    VARIANT_DATA_MISSING,
                    f"Mesh {mesh.name} has no gt_{spec.name} attribute; the variant shows it "
                    "in the no-data colour.",
                    mesh.name,
                )
            )
    if meshes and not held and not any(i.severity is Severity.ERROR for i in issues):
        issues.append(
            Issue(
                Severity.ERROR,
                VARIANT_NO_DATA,
                f"No source mesh holds layer {spec.name} (gt_{spec.name}); bake or create it "
                "first.",
                spec.name,
            )
        )
    return issues


def _texture_issues(
    item: bpy.types.PropertyGroup, spec: LayerSpec, objects: list[bpy.types.Object]
) -> list[Issue]:
    issues: list[Issue] = []
    images = {e.object.as_pointer(): e.image for e in item.images if e.object is not None}
    for obj in objects:
        image = images.get(obj.as_pointer())
        if image is None:
            issues.append(
                Issue(
                    Severity.ERROR,
                    VARIANT_IMAGE_MISSING,
                    f"{obj.name} has no image for texture layer {spec.name}; bake it first.",
                    obj.name,
                )
            )
        uv_layers = obj.data.uv_layers
        if (item.uv_map and item.uv_map not in uv_layers) or not len(uv_layers):
            what = f"UV map {item.uv_map!r}" if item.uv_map else "a UV map"
            issues.append(
                Issue(
                    Severity.ERROR,
                    VARIANT_UV_MAP_MISSING,
                    f"{obj.name} has no {what} for the layer's images.",
                    obj.name,
                )
            )
    return issues


# --- colours ------------------------------------------------------------------------------


def _nodata(spec: LayerSpec) -> float | None:
    nodata = spec.nodata
    return nodata if nodata is not None and math.isfinite(nodata) else None


def _resolve_range(
    spec: LayerSpec, settings: PresentationSettings, values: list[np.ndarray]
) -> tuple[tuple[float, float], tuple[bool, bool]]:
    """The ramp range and which ends were automatic."""
    low, high = settings.range_min, settings.range_max
    auto = (low is None, high is None)
    if low is None or high is None:
        if DataType(spec.data_type) is DataType.BOOLEAN:
            auto_low, auto_high = BOOLEAN_RANGE
        else:
            kept = [v for v in values if v.size]
            try:
                auto_low, auto_high = auto_range(
                    np.concatenate(kept) if kept else np.empty(0, np.float32), _nodata(spec)
                )
            except ValueError:
                raise PresentationError(
                    f"Layer {spec.name} has no values to take an automatic ramp range from; "
                    "bake it first or set the ramp minimum and maximum."
                ) from None
        low = auto_low if low is None else low
        high = auto_high if high is None else high
    if not (math.isfinite(low) and math.isfinite(high)) or low > high:
        raise PresentationError(
            f"Layer {spec.name}: the ramp range {low!r} to {high!r} is not valid "
            "(one end is automatic; set both)."
        )
    return (float(low), float(high)), auto


def _colours(
    spec: LayerSpec,
    settings: PresentationSettings,
    values: np.ndarray,
    value_range: tuple[float, float] | None,
    categories: np.ndarray | None,
) -> np.ndarray:
    if categories is not None:
        return apply_categories(
            values, categories, nodata=_nodata(spec), nodata_rgba=settings.nodata_rgba
        )
    low, high = value_range  # type: ignore[misc]
    return apply_ramp(
        values,
        low,
        high,
        settings.palette,
        nodata=_nodata(spec),
        nodata_rgba=settings.nodata_rgba,
    )


def write_present_attribute(mesh: bpy.types.Mesh, rgba: np.ndarray) -> None:
    """Store ``(corners, 4) uint8`` sRGB colours in the mesh's ``gt_present``.

    The attribute is created (BYTE_COLOR, CORNER) or replaced if it has another
    type or domain. The active and render colour attributes are restored, since
    adding a colour attribute can make it both.
    """
    if mesh.is_editmode:
        raise RuntimeError(f"Mesh {mesh.name!r} is in Edit Mode; switch to Object Mode first")
    if mesh.library is not None:
        raise RuntimeError(f"Mesh {mesh.name!r} is linked from a library; make it local first")
    if rgba.shape != (len(mesh.loops), 4):
        raise ValueError(f"mesh {mesh.name!r} needs {len(mesh.loops)} colours, got {rgba.shape}")
    colours = mesh.color_attributes
    active, render = colours.active_color_name, colours.default_color_name
    attr = mesh.attributes.get(PRESENT_ATTRIBUTE)
    if attr is not None and (attr.domain != "CORNER" or attr.data_type != "BYTE_COLOR"):
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(PRESENT_ATTRIBUTE, "BYTE_COLOR", "CORNER")
    floats = rgba.astype(np.float32)
    floats *= np.float32(1.0 / 255.0)
    attr.data.foreach_set("color_srgb", floats.ravel())
    if colours.active_color_name != active:
        colours.active_color_name = active
    if colours.default_color_name != render:
        colours.default_color_name = render
    mesh.update_tag()


def _write_colour_attributes(
    scene: bpy.types.Scene, spec: LayerSpec, settings: PresentationSettings, item
) -> dict[str, object]:
    meshes = source_meshes(scene)
    for mesh in meshes:
        if mesh.is_editmode:
            raise PresentationError(
                f"Mesh {mesh.name} is in Edit Mode; switch to Object Mode first."
            )
    values: list[np.ndarray | None] = []
    for mesh in meshes:
        values.append(read_layer(mesh, spec) if attribute_problem(mesh, spec) is None else None)
    if all(v is None for v in values):
        raise PresentationError(f"No source mesh holds layer {spec.name} (gt_{spec.name}).")

    categories = None
    value_range: tuple[float, float] | None = None
    auto = (False, False)
    if DataType(spec.data_type) is DataType.CATEGORY:
        categories = category_colours(item, len(spec.categories))
    else:
        value_range, auto = _resolve_range(spec, settings, [v for v in values if v is not None])

    face = Domain(spec.domain) is Domain.FACE
    for mesh, mesh_values in zip(meshes, values, strict=True):
        if mesh_values is None:
            rgba = np.empty((len(mesh.loops), 4), dtype=np.uint8)
            rgba[:] = settings.nodata_rgba
        else:
            rgba = _colours(spec, settings, mesh_values, value_range, categories)
            if face:
                totals = np.empty(len(mesh.polygons), dtype=np.int32)
                mesh.polygons.foreach_get("loop_total", totals)
                rgba = np.repeat(rgba, totals, axis=0)
        write_present_attribute(mesh, rgba)
    return _record(spec, settings, value_range, auto, categories)


def _layer_pixels(image: bpy.types.Image) -> np.ndarray:
    """The first channel of a float layer image, one value per pixel."""
    channels = image.channels
    count = len(image.pixels)
    if not count or not channels:
        raise PresentationError(f"Image {image.name} has no pixels (is its file missing?).")
    pixels = np.empty(count, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    return pixels[::channels]


def _present_image(item, obj: bpy.types.Object, width: int, height: int) -> bpy.types.Image:
    """The 8-bit presentation image of ``obj``, made or resized as needed."""
    images = item.presentation.images
    entry = next((e for e in images if e.object == obj), None)
    if entry is None:
        entry = images.add()
        entry.object = obj
    image = entry.image
    if image is not None and (tuple(image.size) != (width, height) or image.is_float):
        entry.image = None
        if image.users == 0:
            bpy.data.images.remove(image)
        image = None
    if image is None:
        image = bpy.data.images.new(
            f"{PRESENT_ATTRIBUTE}.{item.name}.{obj.name}",
            width,
            height,
            alpha=False,
            float_buffer=False,
        )
        entry.image = image
    return image


def _write_images(
    scene: bpy.types.Scene, spec: LayerSpec, settings: PresentationSettings, item
) -> tuple[dict[str, bpy.types.Image], dict[str, object]]:
    layer_images = {e.object.as_pointer(): e.image for e in item.images if e.object is not None}
    sources = []
    for obj in source_objects(scene):
        image = layer_images.get(obj.as_pointer())
        if image is None:
            raise PresentationError(f"{obj.name} has no image for texture layer {spec.name}.")
        sources.append((obj, image, _layer_pixels(image)))

    categories = None
    value_range: tuple[float, float] | None = None
    auto = (False, False)
    if DataType(spec.data_type) is DataType.CATEGORY:
        categories = category_colours(item, len(spec.categories))
    else:
        value_range, auto = _resolve_range(spec, settings, [v for _o, _i, v in sources])

    written: dict[str, bpy.types.Image] = {}
    for obj, image, values in sources:
        rgba = _colours(spec, settings, values, value_range, categories)
        rgba[:, 3] = 255  # always opaque, so an alpha-clip material keeps every texel
        width, height = (int(v) for v in image.size[:2])
        present = _present_image(item, obj, width, height)
        floats = rgba.astype(np.float32)
        floats *= np.float32(1.0 / 255.0)
        present.pixels.foreach_set(floats.ravel())
        present.update()
        written[obj.name] = present
    return written, _record(spec, settings, value_range, auto, categories)


def _record(spec, settings, value_range, auto, categories) -> dict[str, object]:
    if categories is not None:
        return variant_record(
            spec,
            settings.target,
            settings.blend,
            category_rgba=categories,
            nodata_rgba=settings.nodata_rgba,
        )
    return variant_record(
        spec,
        settings.target,
        settings.blend,
        palette=settings.palette,
        value_range=value_range,
        range_auto=auto,
        nodata_rgba=settings.nodata_rgba,
    )


def write_presentation(
    scene: bpy.types.Scene, layer_name: str
) -> tuple[Presentation, dict[str, object]]:
    """Write the colours of layer ``layer_name`` and return what the exporter needs.

    Returns the ``export.Presentation`` to pass to ``extract_scene`` and the
    ``"variant"`` record for build.json (``core.presentation.variant_record``).
    Raises :class:`PresentationError` (or ``RuntimeError`` for a mesh in Edit
    Mode) if the layer cannot be presented; run :func:`variant_issues` first
    for the full list of problems.
    """
    item = find_layer(scene, layer_name)
    if item is None:
        raise PresentationError(f"There is no layer {layer_name!r}.")
    spec = spec_from_props(item)
    settings = presentation_settings(item)
    problems = [
        i
        for i in presentation_issues(
            spec, settings.target, settings.palette, settings.range_min, settings.range_max
        )
        if i.severity is Severity.ERROR
    ]
    if problems:
        raise PresentationError(problems[0].message)
    if Domain(spec.domain) is Domain.TEXTURE:
        images, record = _write_images(scene, spec, settings, item)
        item = find_layer(scene, layer_name)  # the entry may have moved
        presentation = Presentation(
            settings.target, settings.blend, images=images, uv_map=item.uv_map
        )
    else:
        record = _write_colour_attributes(scene, spec, settings, item)
        presentation = Presentation(settings.target, settings.blend)
    return presentation, record


# --- operators -------------------------------------------------------------------------------


def _active_layer(context: bpy.types.Context):
    if context.scene is None:
        return None
    props = context.scene.geotools
    if 0 <= props.layers_index < len(props.layers):
        return props.layers[props.layers_index]
    return None


def _describe(record: dict[str, object]) -> str:
    value_range = record.get("range")
    if isinstance(value_range, dict):
        return f"{record['palette']} from {value_range['min']:g} to {value_range['max']:g}"
    return f"{len(record.get('categories') or ())} category colours"


class GEOTOOLS_OT_layer_write_presentation(bpy.types.Operator):
    """Write the selected layer's presentation colours now (the gt_present colour attribute,
    or 8-bit images for a texture layer), to look at them before publishing a variant.
    Publishing a variant writes them again"""

    bl_idname = "geotools.layer_write_presentation"
    bl_label = "Write Presentation"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if _active_layer(context) is None:
            cls.poll_message_set("No layer selected")
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        if context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        name = _active_layer(context).name
        errors = [i for i in variant_issues(scene, name) if i.severity is Severity.ERROR]
        if errors:
            self.report({"ERROR"}, f"Cannot present {name}: {errors[0].message}")
            return {"CANCELLED"}
        try:
            _presentation, record = write_presentation(scene, name)
        except (PresentationError, RuntimeError, KeyError, ValueError) as ex:
            self.report({"ERROR"}, f"Cannot present {name}: {ex}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Presented {name}: {_describe(record)}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_layer_presentation_reset_colors(bpy.types.Operator):
    """Give each category of the selected layer its default presentation colour"""

    bl_idname = "geotools.layer_presentation_reset_colors"
    bl_label = "Reset Category Colours"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        item = _active_layer(context)
        if item is None or item.data_type != DataType.CATEGORY.value:
            cls.poll_message_set("Select a category layer")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = _active_layer(context)
        count = len(spec_from_props(item).categories)
        stored = item.presentation.category_colors
        stored.clear()
        defaults = default_category_colours(count)
        for rgba in defaults:
            stored.add().color = tuple(float(c) / 255.0 for c in rgba[:3])
        self.report({"INFO"}, f"Layer {item.name}: {count} category colours")
        redraw_ui(context)
        return {"FINISHED"}


# --- panel ----------------------------------------------------------------------------------


def variant_output_name(scene: bpy.types.Scene, layer_name: str) -> str | None:
    """File name a variant of ``layer_name`` gets with the active build profile, if any."""
    props = scene.geotools
    if not 0 <= props.build_profile_index < len(props.build_profiles):
        return None
    text = props.build_profiles[props.build_profile_index].output_path.strip()
    if not text:
        return None
    try:
        return variant_output(Path(text.replace("//", "", 1)), layer_name).name
    except ValueError:
        return None


class GEOTOOLS_PT_layers_presentation(bpy.types.Panel):
    """How the selected layer is shown in a variant tileset."""

    bl_idname = "GEOTOOLS_PT_layers_presentation"
    bl_label = "Presentation"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"
    bl_parent_id = "GEOTOOLS_PT_layers"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_layer(context) is not None

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = _active_layer(context)
        if item is None:
            return
        p = item.presentation
        spec = spec_from_props(item)
        settings = presentation_settings(item)
        codes = {
            i.code
            for i in presentation_issues(
                spec, settings.target, settings.palette, settings.range_min, settings.range_max
            )
        }
        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        row = col.row()
        row.alert = "variant-target-invalid" in codes
        row.prop(p, "target")
        if row.alert:
            labels = {key: label for key, label, _description in PRESENT_TARGET_ITEMS}
            names = " or ".join(labels[t.value] for t in targets_for(spec.domain))
            col.label(text=f"Use {names} for this layer", icon="ERROR")
        col.prop(p, "blend")
        if spec.data_type is DataType.CATEGORY:
            box = layout.box()
            header = box.row()
            header.label(text="Category Colours")
            header.operator(
                "geotools.layer_presentation_reset_colors", text="", icon="FILE_REFRESH"
            )
            stored = p.category_colors
            sub = box.column(align=True)
            for value, label in enumerate(spec.categories[:16]):
                row = sub.row(align=True)
                row.label(text=f"{value}  {label}")
                if value < len(stored):
                    row.prop(stored[value], "color", text="")
                else:
                    row.label(text="default")
            if len(spec.categories) > 16:
                sub.label(text=f"... and {len(spec.categories) - 16} more")
        else:
            col.prop(p, "palette")
            values = col.column(align=True)
            values.alert = "variant-range-invalid" in codes
            values.prop(p, "range_min", text="Minimum")
            values.prop(p, "range_max", text="Maximum")
            if not p.range_min.strip() or not p.range_max.strip():
                col.label(text="Empty: automatic (2nd to 98th percentile)", icon="INFO")
        col.prop(p, "nodata_color")

        layout.operator("geotools.layer_write_presentation", icon="BRUSH_DATA")
        name = variant_output_name(context.scene, item.name)
        op = layout.operator("geotools.publish", text="Publish Variant", icon="EXPORT")
        op.layer = item.name
        if name:
            layout.label(text=f"Writes {name}", icon="FILE_3D")
        else:
            layout.label(text="Set an output on the build profile", icon="INFO")


classes = (
    GEOTOOLS_OT_layer_write_presentation,
    GEOTOOLS_OT_layer_presentation_reset_colors,
)

register, unregister = bpy.utils.register_classes_factory(classes)
