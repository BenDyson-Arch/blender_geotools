"""The layer registry in Blender: storage helpers, operators and the layers UIList.

The registry is ``scene.geotools.layers`` (``properties.GeoToolsLayer``). A FACE
or CORNER layer lives in the mesh attribute ``gt_<name>`` on every mesh of the
source collection; a TEXTURE layer lives in one float image per object, listed
in ``GeoToolsLayer.images`` and saved as OpenEXR in ``//geotools_layers/``.
See docs/design/m3-layers.md.

Every bake and tool goes through these helpers. They never loop over faces or
corners in Python: values move with ``foreach_get``/``foreach_set`` and numpy.
The attribute helpers need Object Mode (an Edit Mode mesh keeps its own copy of
the data) and raise ``RuntimeError`` otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, StringProperty

from ..core.issues import Issue, Severity
from ..core.layers import (
    Aggregation,
    DataType,
    Domain,
    LayerSpec,
    Origin,
    attribute_name,
    fill_value,
    format_categories,
    format_nodata,
    numpy_dtype,
    parse_categories,
    parse_nodata,
    to_record,
    validate,
)
from .common import redraw_ui
from .properties import (
    LAYER_AGGREGATION_DEFAULT,
    LAYER_DATA_TYPE_ITEMS,
    LAYER_DOMAIN_ITEMS,
    GeoToolsLayer,
)

log = logging.getLogger(__name__)

LAYER_IMAGE_DIR = "//geotools_layers/"
"""Where TEXTURE layer images are saved, relative to the ``.blend``."""

# Issue codes of layer_status and ensure_attribute, besides core.layers.validate's.
ATTRIBUTE_MISSING = "layer-attribute-missing"
ATTRIBUTE_MISMATCH = "layer-attribute-mismatch"
ATTRIBUTE_REPLACED = "layer-attribute-replaced"
IMAGE_MISSING = "layer-image-missing"
IMAGE_UNSAVED = "layer-image-unsaved"
UV_MAP_MISSING = "layer-uv-map-missing"
NO_SOURCE = "layer-no-source"

_ATTRIBUTE_TYPES: dict[DataType, str] = {
    DataType.FLOAT: "FLOAT",
    DataType.INT: "INT",
    DataType.BOOLEAN: "BOOLEAN",
    DataType.CATEGORY: "INT",
}

DOMAIN_ICONS = {"FACE": "FACESEL", "CORNER": "VERTEXSEL", "TEXTURE": "TEXTURE"}


def attribute_type(data_type: DataType) -> str:
    """The Blender attribute type of a layer: FLOAT, INT or BOOLEAN (CATEGORY is INT)."""
    return _ATTRIBUTE_TYPES[DataType(data_type)]


# --- registry ----------------------------------------------------------------------------


def spec_from_props(item: GeoToolsLayer) -> LayerSpec:
    """The ``LayerSpec`` a registry entry describes. Never raises.

    No-data text that is not a number becomes NaN, which ``validate`` reports.
    """
    aggregation = item.aggregation
    return LayerSpec(
        name=item.name,
        domain=Domain(item.domain),
        data_type=DataType(item.data_type),
        units=item.units,
        nodata=parse_nodata(item.nodata),
        origin=Origin(item.origin),
        aggregation=None if aggregation == LAYER_AGGREGATION_DEFAULT else Aggregation(aggregation),
        description=item.description,
        categories=parse_categories(item.categories),
    )


def write_props(item: GeoToolsLayer, spec: LayerSpec) -> None:
    """Store ``spec`` in a registry entry (the LayerSpec fields only)."""
    item.name = spec.name
    item.domain = Domain(spec.domain).value
    item.data_type = DataType(spec.data_type).value
    item.units = spec.units
    item.nodata = format_nodata(spec.nodata)
    item.origin = Origin(spec.origin).value
    item.aggregation = (
        LAYER_AGGREGATION_DEFAULT
        if spec.aggregation is None
        else Aggregation(spec.aggregation).value
    )
    item.description = spec.description
    item.categories = format_categories(spec.categories)


def find_layer(scene: bpy.types.Scene, name: str) -> GeoToolsLayer | None:
    return scene.geotools.layers.get(name)


def other_layer_names(scene: bpy.types.Scene, item: GeoToolsLayer) -> list[str]:
    """Names of every layer except ``item``, for ``validate``."""
    pointer = item.as_pointer()
    return [layer.name for layer in scene.geotools.layers if layer.as_pointer() != pointer]


def ensure_layer(scene: bpy.types.Scene, spec: LayerSpec) -> GeoToolsLayer:
    """Create the registry entry for ``spec``, or update the one with its name.

    Only the LayerSpec fields are written: a TEXTURE layer keeps its images, UV
    map, resolution and margin. Raises ``ValueError`` if ``validate`` finds an
    error in ``spec``; the registry is then unchanged.
    """
    layers = scene.geotools.layers
    item = layers.get(spec.name)
    others = [layer.name for layer in layers if layer.name != spec.name]
    errors = [i for i in validate(spec, others) if i.severity is Severity.ERROR]
    if errors:
        raise ValueError(" ".join(i.message for i in errors))
    if item is None:
        item = layers.add()
    write_props(item, spec)
    return item


def unique_layer_name(layers: Iterable[GeoToolsLayer], base: str = "layer") -> str:
    names = {layer.name for layer in layers}
    n = 1
    while f"{base}_{n}" in names:
        n += 1
    return f"{base}_{n}"


def layer_records(scene: bpy.types.Scene) -> tuple[list[dict[str, object]], list[str]]:
    """``to_record`` of every valid layer for build.json, and the names of invalid ones."""
    records: list[dict[str, object]] = []
    invalid: list[str] = []
    for item in scene.geotools.layers:
        spec = spec_from_props(item)
        issues = validate(spec, other_layer_names(scene, item))
        if any(i.severity is Severity.ERROR for i in issues):
            invalid.append(item.name)
        else:
            records.append(to_record(spec))
    return records, invalid


# --- source ------------------------------------------------------------------------------


def source_objects(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """Mesh objects of the source collection, sorted by name (as the exporter does)."""
    collection = scene.geotools.source_collection
    if collection is None:
        return []
    return sorted((o for o in collection.all_objects if o.type == "MESH"), key=lambda o: o.name)


def source_meshes(scene: bpy.types.Scene) -> list[bpy.types.Mesh]:
    """The distinct meshes of :func:`source_objects`, in the same order."""
    seen: set[int] = set()
    meshes = []
    for obj in source_objects(scene):
        pointer = obj.data.as_pointer()
        if pointer not in seen:
            seen.add(pointer)
            meshes.append(obj.data)
    return meshes


# --- attributes ----------------------------------------------------------------------------


def _attribute_domain(spec: LayerSpec) -> Domain:
    domain = Domain(spec.domain)
    if domain is Domain.TEXTURE:
        raise ValueError(f"Layer {spec.name!r} is a TEXTURE layer; it has images, not attributes")
    return domain


def domain_size(mesh: bpy.types.Mesh, domain: Domain) -> int:
    """Number of values of a FACE or CORNER layer on ``mesh``."""
    domain = Domain(domain)
    if domain is Domain.FACE:
        return len(mesh.polygons)
    if domain is Domain.CORNER:
        return len(mesh.loops)
    raise ValueError(f"{domain.value} layers have no attribute")


def _check_object_mode(mesh: bpy.types.Mesh) -> None:
    if mesh.is_editmode:
        raise RuntimeError(f"Mesh {mesh.name!r} is in Edit Mode; switch to Object Mode first")


def _check_editable(mesh: bpy.types.Mesh) -> None:
    _check_object_mode(mesh)
    if mesh.library is not None:
        raise RuntimeError(f"Mesh {mesh.name!r} is linked from a library; make it local first")


def ensure_attribute(
    mesh: bpy.types.Mesh, spec: LayerSpec, *, issues: list[Issue] | None = None
) -> bpy.types.Attribute:
    """The layer's ``gt_<name>`` attribute on ``mesh``, created if needed.

    A new attribute is filled with ``core.layers.fill_value`` (no-data, or NaN
    for a float layer without one). An existing attribute with the wrong domain
    or type is replaced, losing its values: that is logged and, with
    ``issues``, appended as a ``layer-attribute-replaced`` warning.
    """
    domain = _attribute_domain(spec)
    _check_editable(mesh)
    name = attribute_name(spec)
    wanted = attribute_type(spec.data_type)
    attr = mesh.attributes.get(name)
    if attr is not None:
        if attr.domain == domain.value and attr.data_type == wanted:
            return attr
        message = (
            f"Replaced attribute {name!r} on mesh {mesh.name!r}: it was "
            f"{attr.domain} {attr.data_type}, layer {spec.name!r} needs {domain.value} "
            f"{wanted}. Its old values were discarded."
        )
        log.warning(message)
        if issues is not None:
            issues.append(Issue(Severity.WARNING, ATTRIBUTE_REPLACED, message, mesh.name))
        mesh.attributes.remove(attr)
    attr = mesh.attributes.new(name, wanted, domain.value)
    fill = fill_value(spec)
    if fill:  # NaN is truthy; zeros are what a new attribute already holds
        values = np.full(domain_size(mesh, domain), fill, dtype=numpy_dtype(spec.data_type))
        attr.data.foreach_set("value", values)
    mesh.update_tag()
    return attr


def attribute_problem(mesh: bpy.types.Mesh, spec: LayerSpec) -> str | None:
    """Why ``mesh`` does not hold the layer: "missing", "mismatch", or None if it does."""
    attr = mesh.attributes.get(attribute_name(spec))
    if attr is None:
        return "missing"
    if attr.domain != Domain(spec.domain).value or attr.data_type != attribute_type(spec.data_type):
        return "mismatch"
    return None


def read_layer(mesh: bpy.types.Mesh, spec: LayerSpec) -> np.ndarray:
    """The layer's values on ``mesh``: float32, int32 or bool, one per face or corner.

    Raises ``KeyError`` if the attribute is missing and ``ValueError`` if it has
    the wrong domain or type.
    """
    domain = _attribute_domain(spec)
    _check_object_mode(mesh)
    name = attribute_name(spec)
    problem = attribute_problem(mesh, spec)
    if problem == "missing":
        raise KeyError(f"Mesh {mesh.name!r} has no attribute {name!r}")
    attr = mesh.attributes[name]
    if problem == "mismatch":
        raise ValueError(
            f"Attribute {name!r} on mesh {mesh.name!r} is {attr.domain} {attr.data_type}, "
            f"layer {spec.name!r} needs {domain.value} {attribute_type(spec.data_type)}"
        )
    count = domain_size(mesh, domain)
    if len(attr.data) != count:
        raise RuntimeError(f"Attribute {name!r} has {len(attr.data)} values, expected {count}")
    values = np.empty(count, dtype=numpy_dtype(spec.data_type))
    attr.data.foreach_get("value", values)
    return values


def write_layer(
    mesh: bpy.types.Mesh,
    spec: LayerSpec,
    values: np.ndarray,
    *,
    issues: list[Issue] | None = None,
) -> None:
    """Store ``values`` (one per face or corner) in the layer's attribute on ``mesh``.

    The attribute is created or replaced with :func:`ensure_attribute`. Raises
    ``ValueError`` before changing anything if ``values`` is not 1-D with one
    value per element, or if an INT or CATEGORY layer gets non-finite floats.
    """
    domain = _attribute_domain(spec)
    count = domain_size(mesh, domain)
    array = np.asarray(values)
    if array.ndim != 1 or array.shape[0] != count:
        raise ValueError(
            f"Layer {spec.name!r} on mesh {mesh.name!r} needs {count} values "
            f"(one per {domain.value.lower()}), got an array of shape {array.shape}"
        )
    dtype = numpy_dtype(spec.data_type)
    if dtype.kind == "i" and array.dtype.kind == "f" and not np.isfinite(array).all():
        raise ValueError(f"Layer {spec.name!r} is {spec.data_type}; it cannot hold NaN or inf")
    array = np.ascontiguousarray(array, dtype=dtype)
    attr = ensure_attribute(mesh, spec, issues=issues)
    attr.data.foreach_set("value", array)
    mesh.update_tag()


def remove_attribute(mesh: bpy.types.Mesh, name: str) -> bool:
    """Remove the layer's ``gt_<name>`` attribute from ``mesh``; False if it had none."""
    _check_editable(mesh)
    attr = mesh.attributes.get(attribute_name(name))
    if attr is None:
        return False
    mesh.attributes.remove(attr)
    mesh.update_tag()
    return True


# --- images ---------------------------------------------------------------------------------


def layer_image(item: GeoToolsLayer, obj: bpy.types.Object) -> bpy.types.Image | None:
    """The image that holds a TEXTURE layer on ``obj``, if any."""
    for entry in item.images:
        if entry.object == obj:
            return entry.image
    return None


def ensure_layer_image(item: GeoToolsLayer, obj: bpy.types.Object) -> bpy.types.Image:
    """The layer's image for ``obj``; a new one is made if it has none.

    A new image is ``resolution`` square, float, Non-Color, without alpha, and
    filled with the layer's fill value in every channel. It is not saved: call
    :func:`save_layer_image` once it holds data.
    """
    entry = next((e for e in item.images if e.object == obj), None)
    if entry is not None and entry.image is not None:
        return entry.image
    if entry is None:
        entry = item.images.add()
        entry.object = obj
    size = item.resolution
    image = bpy.data.images.new(
        f"{attribute_name(item.name)}.{obj.name}",
        size,
        size,
        alpha=False,
        float_buffer=True,
        is_data=True,
    )
    image.colorspace_settings.name = "Non-Color"
    fill = float(fill_value(spec_from_props(item)))
    if fill:  # NaN included
        pixels = np.full((size * size, 4), fill, dtype=np.float32)
        pixels[:, 3] = 1.0
        image.pixels.foreach_set(pixels.ravel())
    entry.image = image
    return image


def layer_image_filepath(item: GeoToolsLayer, obj: bpy.types.Object) -> str:
    """Blender path (``//geotools_layers/<layer>__<object>.exr``) of a layer image."""
    return f"{LAYER_IMAGE_DIR}{item.name}__{bpy.path.clean_name(obj.name)}.exr"


def save_layer_image(item: GeoToolsLayer, obj: bpy.types.Object) -> Path:
    """Save the layer's image for ``obj`` as full-float OpenEXR next to the ``.blend``.

    The image's path becomes relative (``//geotools_layers/...``), so the
    ``.blend`` and its folder move together. Raises ``KeyError`` if ``obj`` has
    no image and ``RuntimeError`` if the ``.blend`` has never been saved.
    """
    image = layer_image(item, obj)
    if image is None:
        raise KeyError(f"Layer {item.name!r} has no image for {obj.name!r}")
    if not bpy.data.filepath:
        raise RuntimeError(
            "Save the .blend first: layer images are saved next to it in geotools_layers/"
        )
    filepath = layer_image_filepath(item, obj)
    path = Path(bpy.path.abspath(filepath))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.filepath_raw = filepath
    image.file_format = "OPEN_EXR"
    image.use_half_precision = False
    image.save()
    return path


def image_is_saved(image: bpy.types.Image) -> bool:
    """True if the image's pixels survive reopening the ``.blend``."""
    if image.packed_file is not None:
        return True
    if image.is_dirty or image.source not in {"FILE", "SEQUENCE", "TILED"}:
        return False
    if not image.filepath:
        return False
    return Path(bpy.path.abspath(image.filepath, library=image.library)).is_file()


def remove_layer_data(scene: bpy.types.Scene, item: GeoToolsLayer) -> tuple[int, int]:
    """Delete a layer's ``gt_<name>`` attributes from the source meshes and its images.

    Image files on disk are kept. Returns the number of attributes and images removed.
    """
    attributes = sum(remove_attribute(mesh, item.name) for mesh in source_meshes(scene))
    images = {e.image.as_pointer(): e.image for e in item.images if e.image is not None}
    item.images.clear()
    for image in images.values():
        bpy.data.images.remove(image)
    return attributes, len(images)


# --- status -----------------------------------------------------------------------------


def layer_status(scene: bpy.types.Scene, item: GeoToolsLayer) -> list[Issue]:
    """Problems with a registry entry and its data on the source.

    Validation errors first, then, per source object: a missing attribute
    (``layer-attribute-missing``, warning: not baked yet) or one with the wrong
    domain or type (``layer-attribute-mismatch``, error); for TEXTURE layers a
    missing image (``layer-image-missing``), an image that would be lost on
    reopening (``layer-image-unsaved``), or a missing UV map
    (``layer-uv-map-missing``). Loops over objects, never over mesh elements.
    """
    spec = spec_from_props(item)
    issues = validate(spec, other_layer_names(scene, item))
    if scene.geotools.source_collection is None:
        issues.append(
            Issue(Severity.INFO, NO_SOURCE, "Set a source collection to hold the layer's data.")
        )
        return issues
    objects = source_objects(scene)
    if Domain(spec.domain) is Domain.TEXTURE:
        issues.extend(_image_status(item, spec, objects))
        return issues

    name = attribute_name(spec)
    wanted = f"{Domain(spec.domain).value} {attribute_type(spec.data_type)}"
    seen: set[int] = set()
    for obj in objects:
        mesh = obj.data
        if mesh.as_pointer() in seen:
            continue
        seen.add(mesh.as_pointer())
        problem = attribute_problem(mesh, spec)
        if problem == "missing":
            issues.append(
                Issue(
                    Severity.WARNING,
                    ATTRIBUTE_MISSING,
                    f"{obj.name} has no {name} attribute yet.",
                    obj.name,
                )
            )
        elif problem == "mismatch":
            attr = mesh.attributes[name]
            issues.append(
                Issue(
                    Severity.ERROR,
                    ATTRIBUTE_MISMATCH,
                    f"{obj.name}: {name} is {attr.domain} {attr.data_type}, the layer needs "
                    f"{wanted}.",
                    obj.name,
                )
            )
    return issues


def _image_status(
    item: GeoToolsLayer, spec: LayerSpec, objects: list[bpy.types.Object]
) -> list[Issue]:
    issues = []
    images = {e.object.as_pointer(): e.image for e in item.images if e.object is not None}
    for obj in objects:
        image = images.get(obj.as_pointer())
        if image is None:
            issues.append(
                Issue(
                    Severity.WARNING,
                    IMAGE_MISSING,
                    f"{obj.name} has no image for layer {spec.name} yet.",
                    obj.name,
                )
            )
        elif not image_is_saved(image):
            issues.append(
                Issue(
                    Severity.WARNING,
                    IMAGE_UNSAVED,
                    f"Image {image.name} of {obj.name} is not saved; it is lost when the "
                    ".blend is reopened.",
                    obj.name,
                )
            )
        uv_layers = obj.data.uv_layers
        if item.uv_map and item.uv_map not in uv_layers:
            issues.append(
                Issue(
                    Severity.ERROR,
                    UV_MAP_MISSING,
                    f"{obj.name} has no UV map {item.uv_map!r}.",
                    obj.name,
                )
            )
        elif not item.uv_map and not len(uv_layers):
            issues.append(
                Issue(Severity.ERROR, UV_MAP_MISSING, f"{obj.name} has no UV map.", obj.name)
            )
    return issues


# --- operators -------------------------------------------------------------------------------


def _active_layer(context: bpy.types.Context) -> GeoToolsLayer | None:
    if context.scene is None:
        return None
    props = context.scene.geotools
    if 0 <= props.layers_index < len(props.layers):
        return props.layers[props.layers_index]
    return None


def _active_layer_poll(cls: type, context: bpy.types.Context, *, object_mode: bool) -> bool:
    if _active_layer(context) is None:
        cls.poll_message_set("No layer selected")
        return False
    if object_mode and context.mode != "OBJECT":
        cls.poll_message_set("Switch to Object Mode first")
        return False
    return True


def _name_errors(name: str, others: list[str]) -> list[Issue]:
    spec = LayerSpec(name, Domain.FACE, DataType.FLOAT)
    return [
        i
        for i in validate(spec, others)
        if i.code in {"layer-name-invalid", "layer-name-duplicate"}
    ]


class GEOTOOLS_OT_layer_add(bpy.types.Operator):
    """Add a layer to the registry"""

    bl_idname = "geotools.layer_add"
    bl_label = "Add Layer"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(
        name="Name",
        description="Lowercase letters, digits and underscores; empty for an automatic name",
        options={"SKIP_SAVE"},
    )
    domain: EnumProperty(name="Domain", items=LAYER_DOMAIN_ITEMS, default="FACE")
    data_type: EnumProperty(name="Type", items=LAYER_DATA_TYPE_ITEMS, default="FLOAT")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene is not None

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if not self.name:
            self.name = unique_layer_name(context.scene.geotools.layers)
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        name = self.name.strip() or unique_layer_name(props.layers)
        errors = _name_errors(name, [layer.name for layer in props.layers])
        if errors:
            self.report({"ERROR"}, errors[0].message)
            return {"CANCELLED"}
        item = props.layers.add()
        # Not ensure_layer: a new CATEGORY layer has no categories yet.
        write_props(item, LayerSpec(name, Domain(self.domain), DataType(self.data_type)))
        props.layers_index = len(props.layers) - 1
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_layer_remove(bpy.types.Operator):
    """Remove the selected layer from the registry, optionally with its data"""

    bl_idname = "geotools.layer_remove"
    bl_label = "Remove Layer"
    bl_options = {"REGISTER", "UNDO"}

    delete_data: BoolProperty(
        name="Delete Attributes and Images",
        description="Also delete the layer's gt_<name> attributes from the source meshes "
        "and its images. Image files on disk are kept",
        default=False,
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_layer_poll(cls, context, object_mode=True)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context: bpy.types.Context) -> None:
        item = _active_layer(context)
        self.layout.label(text=f"Remove layer {item.name if item else ''}?")
        self.layout.prop(self, "delete_data")

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        props = scene.geotools
        index = props.layers_index
        if not 0 <= index < len(props.layers):
            return {"CANCELLED"}
        item = props.layers[index]
        name = item.name
        note = ""
        if self.delete_data:
            try:
                attributes, images = remove_layer_data(scene, item)
            except RuntimeError as ex:
                self.report({"ERROR"}, f"Cannot delete the data of {name}: {ex}")
                return {"CANCELLED"}
            note = (
                f" with {attributes} attribute{'s' * (attributes != 1)} and "
                f"{images} image{'s' * (images != 1)}"
            )
            if images:
                note += " (image files on disk are kept)"
        props.layers.remove(index)
        props.layers_index = min(index, len(props.layers) - 1) if index else 0
        self.report({"INFO"}, f"Removed layer {name}{note}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_layer_rename(bpy.types.Operator):
    """Rename the selected layer and its gt_<name> attributes on the source meshes"""

    bl_idname = "geotools.layer_rename"
    bl_label = "Rename Layer"
    bl_options = {"REGISTER", "UNDO"}

    new_name: StringProperty(
        name="Name",
        description="Lowercase letters, digits and underscores",
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_layer_poll(cls, context, object_mode=True)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        self.new_name = _active_layer(context).name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        item = _active_layer(context)
        old, new = item.name, self.new_name.strip()
        if new == old:
            return {"CANCELLED"}
        errors = _name_errors(new, other_layer_names(scene, item))
        if errors:
            self.report({"ERROR"}, errors[0].message)
            return {"CANCELLED"}
        meshes = source_meshes(scene)
        old_attr, new_attr = attribute_name(old), attribute_name(new)
        taken = [m.name for m in meshes if new_attr in m.attributes]
        if taken:
            self.report({"ERROR"}, f"Mesh {taken[0]!r} already has an attribute {new_attr!r}")
            return {"CANCELLED"}
        locked = [m.name for m in meshes if old_attr in m.attributes and m.library]
        if locked:
            self.report({"ERROR"}, f"Mesh {locked[0]!r} is linked from a library")
            return {"CANCELLED"}
        renamed = 0
        for mesh in meshes:
            attr = mesh.attributes.get(old_attr)
            if attr is not None:
                attr.name = new_attr
                renamed += 1
        item.name = new
        self.report(
            {"INFO"},
            f"Renamed layer {old} to {new} ({renamed} attribute{'s' * (renamed != 1)})",
        )
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_layer_create_attributes(bpy.types.Operator):
    """Create the selected layer's gt_<name> attribute on every source mesh, filled with no-data.
    An attribute with the wrong domain or type is replaced"""

    bl_idname = "geotools.layer_create_attributes"
    bl_label = "Create Attributes"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _active_layer_poll(cls, context, object_mode=True):
            return False
        if _active_layer(context).domain == "TEXTURE":
            cls.poll_message_set("A texture layer has images, made by a texture bake")
            return False
        if context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        item = _active_layer(context)
        spec = spec_from_props(item)
        errors = [
            i
            for i in validate(spec, other_layer_names(scene, item))
            if i.severity is Severity.ERROR
        ]
        if errors:
            self.report({"ERROR"}, f"Cannot create attributes: {errors[0].message}")
            return {"CANCELLED"}
        issues: list[Issue] = []
        created = 0
        try:
            for mesh in source_meshes(scene):
                if attribute_problem(mesh, spec) is not None:
                    ensure_attribute(mesh, spec, issues=issues)
                    created += 1
        except RuntimeError as ex:
            self.report({"ERROR"}, f"Cannot create attributes: {ex}")
            return {"CANCELLED"}
        name = attribute_name(spec)
        meshes = f"{created} mesh{'es' * (created != 1)}"
        if issues:
            self.report(
                {"WARNING"},
                f"Created {name} on {meshes}, replacing {len(issues)} with the wrong domain "
                f"or type: {issues[0].message}",
            )
        else:
            self.report({"INFO"}, f"Created {name} on {meshes}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_layer_set_categories(bpy.types.Operator):
    """Set the category labels of the selected layer: label i names value i"""

    bl_idname = "geotools.layer_set_categories"
    bl_label = "Set Categories"
    bl_options = {"REGISTER", "UNDO"}

    labels: StringProperty(
        name="Categories",
        description="Labels separated by commas, for values 0, 1, 2 ...",
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_layer_poll(cls, context, object_mode=False)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        self.labels = ", ".join(parse_categories(_active_layer(context).categories))
        return context.window_manager.invoke_props_dialog(self, width=400)

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = _active_layer(context)
        labels = [label.strip() for label in self.labels.split(",")]
        if not any(labels):
            labels = []
        item.categories = format_categories(labels)
        self.report({"INFO"}, f"Layer {item.name}: {len(labels)} categories")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_UL_layers(bpy.types.UIList):
    """Layers: name, domain icon and type."""

    bl_idname = "GEOTOOLS_UL_layers"

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
        row = layout.row(align=True)
        row.label(text=item.name, icon=DOMAIN_ICONS.get(item.domain, "NONE"))
        row.label(text=item.bl_rna.properties["data_type"].enum_items[item.data_type].name)


classes = (
    GEOTOOLS_OT_layer_add,
    GEOTOOLS_OT_layer_remove,
    GEOTOOLS_OT_layer_rename,
    GEOTOOLS_OT_layer_create_attributes,
    GEOTOOLS_OT_layer_set_categories,
    GEOTOOLS_UL_layers,
)

register, unregister = bpy.utils.register_classes_factory(classes)
