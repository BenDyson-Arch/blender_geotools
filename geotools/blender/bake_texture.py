"""Texture-space bake: an attribute into a TEXTURE layer's per-object images, with Cycles.

``geotools.bake_texture_layer`` bakes a FACE or CORNER layer's ``gt_<name>``
attribute, or any named attribute of the evaluated mesh (a Geometry Nodes
output), into the layer's images on the source UVs. See section 3 of
docs/design/m3-layers.md.

- Cycles EMIT through a temporary bake material per object: Attribute, then
  Emission, then Material Output, with an active Image Texture of the layer's
  image. Cycles writes 0 for NaN and inf, so the material emits the value in R
  and a validity mask in G (``(v - v) == 0``); invalid texels become the
  layer's no-data afterwards, and R / G is the value in the gutter, which
  ``margin_type="EXTEND"`` averages.
- When the attribute exists only on a topology-changing evaluated mesh, the bake
  runs selected-to-active, from a temporary object made from the evaluated mesh
  onto the object with its modifiers off.
- The ``uv-overlap`` check (``core.uv_overlap``) runs on each target first; an
  overlapping UV map is refused unless "Bake Anyway" is ticked.
- Everything touched is restored in ``try/finally``: material slots, modifier
  and visibility flags, render engine, Cycles device and samples, bake settings,
  selection and active object. Temporary materials, meshes and objects are
  removed. Images are saved as EXR with ``layers.save_layer_image``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from ..core import uv_overlap
from ..core.issues import Issue, Severity
from ..core.layers import (
    DataType,
    Domain,
    Origin,
    attribute_name,
    fill_value,
    format_provenance,
    make_provenance,
    validate,
)
from .common import extension_version, redraw_ui
from .layers import (
    ensure_layer_image,
    find_layer,
    layer_image,
    other_layer_names,
    save_layer_image,
    source_objects,
    spec_from_props,
    update_value_range,
)
from .ui import big_button, split_column

log = logging.getLogger(__name__)

TOOL = "bake_texture"
"""Provenance tool name, like the vertex bake's "bake_geonodes"."""
TEMP_PREFIX = "GeoTools Bake"
"""Name prefix of the temporary materials, meshes and objects a bake makes."""

METHOD_ITEMS = (
    (
        "AUTO",
        "Automatic",
        (
            "Bake each object directly when its evaluated mesh has the attribute on the same "
            "topology, selected-to-active when the topology changed"
        ),
    ),
    ("DIRECT", "Direct", "Bake the evaluated object onto its own UVs"),
    (
        "SELECTED_TO_ACTIVE",
        "Selected to Active",
        "Bake from a copy of the evaluated mesh onto the object with its modifiers off",
    ),
)

AUTO_EXTRUSION = 0.01
"""Automatic cage extrusion, as a fraction of the object's bounding-box diagonal."""

# Settings a bake changes, restored afterwards.
_BAKE_SETTINGS = (
    "margin",
    "margin_type",
    "use_clear",
    "target",
    "use_selected_to_active",
    "cage_extrusion",
    "max_ray_distance",
    "use_cage",
    "cage_object",
    "use_split_materials",
)
_CYCLES_SETTINGS = (
    "device",
    "samples",
    "use_adaptive_sampling",
    "use_denoising",
    "time_limit",
    "bake_type",
)


# --- UV overlap --------------------------------------------------------------------------


def uv_map_name(mesh: bpy.types.Mesh, wanted: str) -> str | None:
    """The UV map a layer uses on ``mesh``: ``wanted``, or the active render one if empty."""
    if wanted:
        return wanted if wanted in mesh.uv_layers else None
    for layer in mesh.uv_layers:
        if layer.active_render:
            return layer.name
    return mesh.uv_layers[0].name if len(mesh.uv_layers) else None


def uv_triangles(mesh: bpy.types.Mesh, uv_name: str) -> tuple[np.ndarray, np.ndarray]:
    """Corner UVs ``(T, 3, 2)`` float32 of each loop triangle, and its face index."""
    tris = mesh.loop_triangles
    count = len(tris)
    loops = np.empty(count * 3, np.int32)
    faces = np.empty(count, np.int32)
    tris.foreach_get("loops", loops)
    tris.foreach_get("polygon_index", faces)
    uv = np.empty(len(mesh.loops) * 2, np.float32)
    mesh.attributes[uv_name].data.foreach_get("vector", uv)
    return uv.reshape(-1, 2)[loops].reshape(count, 3, 2), faces


def check_object_uvs(
    obj: bpy.types.Object,
    depsgraph: bpy.types.Depsgraph,
    uv_name: str,
    resolution: int,
    *,
    severity: Severity = Severity.WARNING,
) -> tuple[uv_overlap.OverlapResult, Issue | None]:
    """The uv-overlap check of ``obj``'s evaluated mesh on ``uv_name``."""
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.data
    corners, faces = uv_triangles(mesh, uv_name)
    result = uv_overlap.find_overlaps(corners, resolution=resolution)
    examples = list(dict.fromkeys(int(f) for f in faces[list(result.examples)]))
    issue = uv_overlap.overlap_issue(
        result,
        obj.name,
        uv_map=uv_name,
        severity=severity,
        examples=examples,
        example_label="faces",
    )
    return result, issue


def uv_overlap_issues(context: bpy.types.Context) -> list[Issue]:
    """``uv-overlap`` warnings for the Checks panel.

    Each source object is checked on the UV map of every TEXTURE layer, at the
    largest resolution a layer asks for on that map. Without TEXTURE layers
    nothing is checked: rusty-tiles only records overlap, it never refuses it.
    """
    scene = context.scene
    wanted: dict[str, int] = {}
    for item in scene.geotools.layers:
        if item.domain == Domain.TEXTURE.value:
            wanted[item.uv_map] = max(wanted.get(item.uv_map, 0), item.resolution)
    if not wanted:
        return []
    depsgraph = context.evaluated_depsgraph_get()
    issues = []
    for obj in source_objects(scene):
        mesh = obj.evaluated_get(depsgraph).data
        per_map: dict[str, int] = {}
        for uv_map, resolution in wanted.items():
            name = uv_map_name(mesh, uv_map)
            if name is not None:  # layer_status reports a missing UV map
                per_map[name] = max(per_map.get(name, 0), resolution)
        for name, resolution in per_map.items():
            _, issue = check_object_uvs(obj, depsgraph, name, resolution)
            if issue is not None:
                issues.append(issue)
    return issues


# --- bake state --------------------------------------------------------------------------


@dataclass
class _Saved:
    """What a bake changed, to put back in ``restore``."""

    engine: str = ""
    bake: dict[str, object] = field(default_factory=dict)
    cycles: dict[str, object] = field(default_factory=dict)
    selected: list[bpy.types.Object] = field(default_factory=list)
    active: bpy.types.Object | None = None
    slots: list[tuple[bpy.types.Object, int, str, bpy.types.Material | None]] = field(
        default_factory=list
    )
    padded_meshes: list[bpy.types.Mesh] = field(default_factory=list)
    modifiers: list[tuple[bpy.types.Modifier, bool, bool]] = field(default_factory=list)
    visibility: list[tuple[bpy.types.Object, bool, bool, bool]] = field(default_factory=list)
    temp_objects: list[bpy.types.Object] = field(default_factory=list)
    temp_meshes: list[bpy.types.Mesh] = field(default_factory=list)
    temp_materials: list[bpy.types.Material] = field(default_factory=list)

    @classmethod
    def capture(cls, context: bpy.types.Context) -> _Saved:
        scene = context.scene
        saved = cls(engine=scene.render.engine)
        saved.bake = {name: getattr(scene.render.bake, name) for name in _BAKE_SETTINGS}
        saved.selected = list(context.view_layer.objects.selected)
        saved.active = context.view_layer.objects.active
        return saved

    def capture_cycles(self, scene: bpy.types.Scene) -> None:
        self.cycles = {
            name: getattr(scene.cycles, name)
            for name in _CYCLES_SETTINGS
            if hasattr(scene.cycles, name)
        }

    def restore(self, context: bpy.types.Context) -> list[str]:
        """Put everything back. Never raises; returns (and logs) what could not be restored."""
        scene = context.scene
        steps = (
            ("temporary data", self._remove_temporaries),
            ("materials", self._restore_slots),
            ("modifiers and visibility", self._restore_flags),
            ("render and bake settings", lambda: self._restore_settings(scene)),
            ("selection", lambda: self._restore_selection(context)),
        )
        failed = []
        for what, step in steps:
            try:
                step()
            except Exception as ex:  # keep restoring the rest
                log.exception("restoring the %s after a texture bake failed", what)
                failed.append(f"{what} ({ex})")
        return failed

    def _remove_temporaries(self) -> None:
        for obj in self.temp_objects:
            bpy.data.objects.remove(obj)
        for mesh in self.temp_meshes:
            bpy.data.meshes.remove(mesh)
        self.temp_objects.clear()
        self.temp_meshes.clear()

    def _restore_slots(self) -> None:
        for obj, index, link, material in reversed(self.slots):
            slot = obj.material_slots[index]
            slot.link = "OBJECT"
            slot.material = material
            slot.link = link
        self.slots.clear()
        for mesh in self.padded_meshes:
            mesh.materials.pop()
        self.padded_meshes.clear()
        for material in self.temp_materials:
            bpy.data.materials.remove(material)
        self.temp_materials.clear()

    def _restore_flags(self) -> None:
        for modifier, viewport, render in self.modifiers:
            modifier.show_viewport = viewport
            modifier.show_render = render
        self.modifiers.clear()
        for obj, hidden, hide_viewport, hide_render in self.visibility:
            obj.hide_viewport = hide_viewport
            obj.hide_render = hide_render
            obj.hide_set(hidden)
        self.visibility.clear()

    def _restore_settings(self, scene: bpy.types.Scene) -> None:
        for name, value in self.bake.items():
            setattr(scene.render.bake, name, value)
        if self.engine:
            scene.render.engine = self.engine
        for name, value in self.cycles.items():
            setattr(scene.cycles, name, value)

    def _restore_selection(self, context: bpy.types.Context) -> None:
        view_layer = context.view_layer
        view_layer.update()  # drop the removed temporary objects
        _deselect_all(view_layer)
        for obj in self.selected:
            if obj.name in view_layer.objects:
                obj.select_set(True)
        view_layer.objects.active = self.active


def _deselect_all(view_layer: bpy.types.ViewLayer) -> None:
    for obj in view_layer.objects:
        if obj is not None:  # a removed object can linger until the next update
            obj.select_set(False)


def _run_bake(**kwargs: object) -> set[str]:
    """``bpy.ops.object.bake``; a seam for tests that make the bake fail."""
    return bpy.ops.object.bake(**kwargs)


# --- the bake material -------------------------------------------------------------------


def make_bake_material(name: str, attribute: str, image: bpy.types.Image) -> bpy.types.Material:
    """Attribute -> (value, valid, 0) -> Emission -> Output, plus the active image node.

    ``valid`` is 1 where ``value - value == 0``, i.e. where the value is finite.
    """
    material = bpy.data.materials.new(name)
    tree = material.node_tree
    nodes, links = tree.nodes, tree.links
    nodes.clear()
    attr = nodes.new("ShaderNodeAttribute")
    attr.attribute_type = "GEOMETRY"
    attr.attribute_name = attribute
    split = nodes.new("ShaderNodeSeparateColor")
    zero = nodes.new("ShaderNodeMath")
    zero.operation = "SUBTRACT"
    valid = nodes.new("ShaderNodeMath")
    valid.operation = "COMPARE"
    valid.inputs[1].default_value = 0.0
    valid.inputs[2].default_value = 0.0
    join = nodes.new("ShaderNodeCombineColor")
    join.inputs[2].default_value = 0.0
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    output = nodes.new("ShaderNodeOutputMaterial")
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Closest"
    # The Color output is exact; Fac averages the channels and can be an ulp off.
    links.new(attr.outputs["Color"], split.inputs[0])
    links.new(split.outputs[0], zero.inputs[0])
    links.new(split.outputs[0], zero.inputs[1])
    links.new(zero.outputs[0], valid.inputs[0])
    links.new(split.outputs[0], join.inputs[0])
    links.new(valid.outputs[0], join.inputs[1])
    links.new(join.outputs[0], emission.inputs["Color"])
    links.new(emission.outputs[0], output.inputs["Surface"])
    nodes.active = texture
    return material


def _assign_material(saved: _Saved, obj: bpy.types.Object, material: bpy.types.Material) -> None:
    """Put ``material`` in every slot of ``obj`` (object-linked), recording the old state."""
    mesh = obj.data
    if not len(obj.material_slots) and mesh not in saved.padded_meshes:
        mesh.materials.append(None)
        saved.padded_meshes.append(mesh)
    for index, slot in enumerate(obj.material_slots):
        link = slot.link
        slot.link = "OBJECT"
        saved.slots.append((obj, index, link, slot.material))
        slot.material = material


def _show(saved: _Saved, obj: bpy.types.Object) -> None:
    """Make ``obj`` visible and renderable for the bake."""
    if obj.hide_get() or obj.hide_viewport or obj.hide_render:
        saved.visibility.append((obj, obj.hide_get(), obj.hide_viewport, obj.hide_render))
        obj.hide_viewport = False
        obj.hide_render = False
        obj.hide_set(False)


def _modifiers_off(saved: _Saved, obj: bpy.types.Object) -> None:
    for modifier in obj.modifiers:
        if modifier.show_viewport or modifier.show_render:
            saved.modifiers.append((modifier, modifier.show_viewport, modifier.show_render))
            modifier.show_viewport = False
            modifier.show_render = False


# --- planning ----------------------------------------------------------------------------


@dataclass
class _Target:
    obj: bpy.types.Object
    method: str  # "DIRECT", "ORIGINAL" (direct, modifiers off) or "SELECTED_TO_ACTIVE"
    uv_map: str = ""
    source: bpy.types.Object | None = None


def _plan(
    obj: bpy.types.Object, depsgraph: bpy.types.Depsgraph, attribute: str, method: str
) -> str:
    """How to bake ``obj``; raises ``ValueError`` with the reason if it cannot be baked."""
    evaluated = obj.evaluated_get(depsgraph).data
    original = obj.data
    on_evaluated = attribute in evaluated.attributes
    same = len(evaluated.polygons) == len(original.polygons) and len(evaluated.loops) == len(
        original.loops
    )
    if method == "DIRECT":
        if not on_evaluated:
            raise ValueError(f"{obj.name}: the evaluated mesh has no attribute {attribute!r}")
        return "DIRECT"
    if method == "SELECTED_TO_ACTIVE":
        if not on_evaluated:
            raise ValueError(f"{obj.name}: the evaluated mesh has no attribute {attribute!r}")
        return "SELECTED_TO_ACTIVE"
    if on_evaluated:
        return "DIRECT" if same else "SELECTED_TO_ACTIVE"
    if attribute in original.attributes:
        return "ORIGINAL"
    raise ValueError(f"{obj.name} has no attribute {attribute!r}")


def _source_copy(
    saved: _Saved, context: bpy.types.Context, obj: bpy.types.Object, depsgraph
) -> bpy.types.Object:
    """A temporary object holding ``obj``'s evaluated mesh, in the same place."""
    evaluated = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(
        evaluated, preserve_all_data_layers=True, depsgraph=depsgraph
    )
    mesh.name = f"{TEMP_PREFIX} {obj.name}"
    saved.temp_meshes.append(mesh)
    copy = bpy.data.objects.new(mesh.name, mesh)
    saved.temp_objects.append(copy)
    copy.matrix_world = obj.matrix_world.copy()
    context.scene.collection.objects.link(copy)
    return copy


def _extrusion(obj: bpy.types.Object, cage: float) -> float:
    if cage > 0:
        return cage
    scale = obj.matrix_world.to_scale()
    size = np.ptp(np.array(obj.bound_box, np.float64), axis=0) * np.abs(np.array(scale))
    diagonal = float(np.linalg.norm(size))
    return max(diagonal * AUTO_EXTRUSION, 1e-4)


# --- images ------------------------------------------------------------------------------


def _prepare_image(image: bpy.types.Image, size: int) -> None:
    """Size the image and clear it to (0, 0, 0, 1): nothing valid yet."""
    if tuple(image.size) != (size, size):
        image.scale(size, size)
    pixels = np.zeros((size * size, 4), np.float32)
    pixels[:, 3] = 1.0
    image.pixels.foreach_set(pixels.ravel())


def finish_image(image: bpy.types.Image, fill: float, data_type: DataType) -> int:
    """Turn the baked (value, valid) pixels into the layer's values; return valid texels.

    Valid texels get ``R / G`` (exactly R inside faces, a weighted mean in the
    gutter), the others ``fill``. INT, CATEGORY and BOOLEAN layers are rounded.
    All three colour channels hold the value; alpha is 1.
    """
    pixels = np.empty(len(image.pixels), np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(-1, 4)
    weight = pixels[:, 1]
    valid = weight > 0
    values = np.full(pixels.shape[0], fill, np.float32)
    values[valid] = pixels[valid, 0] / weight[valid]
    if DataType(data_type) is not DataType.FLOAT:
        values[valid] = np.rint(values[valid])
    pixels[:, :3] = values[:, None]
    pixels[:, 3] = 1.0
    image.pixels.foreach_set(pixels.ravel())
    image.update()
    return int(np.count_nonzero(valid))


# --- operator ----------------------------------------------------------------------------


def _target_layer(context: bpy.types.Context, name: str):
    props = context.scene.geotools
    if name:
        return find_layer(context.scene, name)
    if 0 <= props.layers_index < len(props.layers):
        return props.layers[props.layers_index]
    return None


class GEOTOOLS_OT_bake_texture_layer(bpy.types.Operator):
    """Bake an attribute into the texture layer's per-object images with Cycles"""

    bl_idname = "geotools.bake_texture_layer"
    bl_label = "Bake Texture Layer"
    bl_options = {"REGISTER", "UNDO"}

    layer: StringProperty(
        name="Layer",
        description="Texture layer to bake into; empty for the selected layer",
        options={"SKIP_SAVE"},
    )
    source_layer: StringProperty(
        name="Source Layer",
        description="Face or corner layer whose gt_<name> attribute is baked",
    )
    attribute: StringProperty(
        name="Attribute",
        description="Named attribute of the evaluated mesh to bake, e.g. a Geometry Nodes "
        "output. Used when no source layer is set",
    )
    method: EnumProperty(name="Method", items=METHOD_ITEMS, default="AUTO")
    cage_extrusion: FloatProperty(
        name="Extrusion",
        description="Selected to active: how far rays start outside the object. 0 for 1% of "
        "its bounding-box diagonal",
        default=0.0,
        min=0.0,
        subtype="DISTANCE",
    )
    max_ray_distance: FloatProperty(
        name="Max Ray Distance",
        description="Selected to active: how far rays travel. 0 for twice the extrusion",
        default=0.0,
        min=0.0,
        subtype="DISTANCE",
    )
    bake_anyway: BoolProperty(
        name="Bake Anyway",
        description="Bake even where UV triangles share texels; the last triangle baked wins",
        default=False,
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None or context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        if context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        return True

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        item = _target_layer(context, self.layer)
        title = f"Bake {item.name}" if item is not None else "Bake Texture Layer"
        return context.window_manager.invoke_props_dialog(
            self, width=360, title=title, confirm_text="Bake"
        )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        props = context.scene.geotools
        layout.prop_search(self, "source_layer", props, "layers")
        row = layout.row()
        row.enabled = not self.source_layer
        row.prop(self, "attribute")
        layout.prop(self, "method")
        col = layout.column(align=True)
        col.active = self.method != "DIRECT"
        col.prop(self, "cage_extrusion")
        col.prop(self, "max_ray_distance")
        layout.prop(self, "bake_anyway")

    def _fail(self, message: str) -> set[str]:
        self.report({"ERROR"}, message)
        return {"CANCELLED"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        item = _target_layer(context, self.layer)
        if item is None:
            return self._fail(f"No layer {self.layer!r}" if self.layer else "No layer selected")
        if item.domain != Domain.TEXTURE.value:
            return self._fail(f"Layer {item.name} is not a texture layer")
        spec = spec_from_props(item)
        errors = [
            i
            for i in validate(spec, other_layer_names(scene, item))
            if i.severity is Severity.ERROR
        ]
        if errors:
            return self._fail(f"Cannot bake {item.name}: {errors[0].message}")
        if not hasattr(scene, "cycles"):
            return self._fail("Enable the Cycles add-on: texture bakes use Cycles")
        if not bpy.data.filepath:
            return self._fail(
                "Save the .blend first: layer images are saved next to it in geotools_layers/"
            )
        if self.source_layer:
            source = find_layer(scene, self.source_layer)
            if source is None or source.domain == Domain.TEXTURE.value:
                return self._fail(f"No face or corner layer {self.source_layer!r}")
            attribute, source_origin = attribute_name(source.name), source.origin
        else:
            attribute, source_origin = self.attribute.strip(), Origin.GEONODES.value
        if not attribute:
            return self._fail("Choose a source layer or an attribute to bake")
        objects = source_objects(scene)
        if not objects:
            return self._fail("The source collection has no meshes")

        layer_name = item.name
        saved = _Saved.capture(context)
        touched: list[tuple[bpy.types.Object, bool]] = []  # (object, image made by this bake)
        done = False
        try:
            result = self._bake(context, saved, layer_name, attribute, objects, touched)
            done = result is not None
            if not done:
                return {"CANCELLED"}
        finally:
            unrestored = saved.restore(context)
            if not done:
                _undo_images(scene, layer_name, touched)
            if unrestored:
                self.report({"ERROR"}, f"Could not restore the {', '.join(unrestored)}")

        baked, coverage, overlap_notes = result
        item = find_layer(scene, layer_name)
        item.provenance = format_provenance(
            make_provenance(
                TOOL,
                {
                    "attribute": attribute,
                    "source_layer": self.source_layer or None,
                    "method": self.method,
                    "methods": {obj: method for obj, method in baked},
                    "uv_map": item.uv_map,
                    "resolution": item.resolution,
                    "margin": item.margin,
                    "margin_type": "EXTEND",
                    "bake_anyway": self.bake_anyway,
                },
                extension_version(),
            )
        )
        item.origin = source_origin
        low, high = update_value_range(scene, item)
        span = "no values" if low is None else f"{low:g} to {high:g}"
        message = (
            f"Baked {attribute} into {layer_name} on {len(baked)} object"
            f"{'s' * (len(baked) != 1)} ({coverage:,} texels, {span})"
        )
        if overlap_notes:
            self.report({"WARNING"}, f"{message}; overlapping UVs: {overlap_notes[0]}")
        else:
            self.report({"INFO"}, message)
        redraw_ui(context)
        return {"FINISHED"}

    def _bake(
        self,
        context: bpy.types.Context,
        saved: _Saved,
        layer_name: str,
        attribute: str,
        objects: list[bpy.types.Object],
        touched: list[tuple[bpy.types.Object, bool]],
    ) -> tuple[list[tuple[str, str]], int, list[str]] | None:
        """Plan, check and bake every object. None (after a report) if refused or failed."""
        scene = context.scene
        depsgraph = context.evaluated_depsgraph_get()
        item = find_layer(scene, layer_name)

        targets: list[_Target] = []
        for obj in objects:
            try:
                method = _plan(obj, depsgraph, attribute, self.method)
            except ValueError as ex:
                self.report({"ERROR"}, f"Cannot bake: {ex}")
                return None
            targets.append(_Target(obj, method))
        # Copies of the evaluated meshes, before any modifier is turned off.
        for target in targets:
            if target.method == "SELECTED_TO_ACTIVE":
                target.source = _source_copy(saved, context, target.obj, depsgraph)
        for target in targets:
            _show(saved, target.obj)
            if target.method != "DIRECT":
                _modifiers_off(saved, target.obj)
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()

        # UV maps and the overlap check, on the meshes as they will be baked.
        notes: list[str] = []
        refused: list[Issue] = []
        for target in targets:
            mesh = target.obj.evaluated_get(depsgraph).data
            uv_name = uv_map_name(mesh, item.uv_map)
            if uv_name is None:
                which = f"UV map {item.uv_map!r}" if item.uv_map else "UV map"
                self.report({"ERROR"}, f"Cannot bake: {target.obj.name} has no {which}")
                return None
            target.uv_map = uv_name
            severity = Severity.WARNING if self.bake_anyway else Severity.ERROR
            _, issue = check_object_uvs(
                target.obj, depsgraph, uv_name, item.resolution, severity=severity
            )
            if issue is not None:
                (notes if self.bake_anyway else refused).append(issue.message)
        if refused:
            self.report(
                {"ERROR"},
                f"Refused: {refused[0]} Tick Bake Anyway to bake over overlapping UVs.",
            )
            return None

        scene.render.engine = "CYCLES"
        saved.capture_cycles(scene)
        cycles = scene.cycles
        cycles.device = "CPU"
        cycles.samples = 1  # emission needs one sample
        cycles.use_adaptive_sampling = False
        cycles.use_denoising = False
        if hasattr(cycles, "time_limit"):
            cycles.time_limit = 0.0
        cycles.bake_type = "EMIT"

        spec = spec_from_props(item)
        fill = float(fill_value(spec))
        baked: list[tuple[str, str]] = []
        coverage = 0
        for target in targets:
            obj = target.obj
            item = find_layer(scene, layer_name)
            touched.append((obj, layer_image(item, obj) is None))
            image = ensure_layer_image(item, obj)
            _prepare_image(image, item.resolution)
            material = make_bake_material(f"{TEMP_PREFIX} {obj.name}", attribute, image)
            saved.temp_materials.append(material)
            _assign_material(saved, obj, material)
            _deselect_all(context.view_layer)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            options = {}
            if target.source is not None:
                target.source.data.materials.clear()
                target.source.data.materials.append(material)
                target.source.select_set(True)
                extrusion = _extrusion(obj, self.cage_extrusion)
                options = {
                    "use_selected_to_active": True,
                    "cage_extrusion": extrusion,
                    "max_ray_distance": self.max_ray_distance or 2.0 * extrusion,
                }
            try:
                _run_bake(
                    type="EMIT",
                    target="IMAGE_TEXTURES",
                    margin=item.margin,
                    margin_type="EXTEND",
                    use_clear=False,
                    use_cage=False,
                    use_split_materials=False,
                    uv_layer=target.uv_map,
                    **options,
                )
            except RuntimeError as ex:
                self.report({"ERROR"}, f"Bake of {obj.name} failed: {ex}")
                return None
            coverage += finish_image(image, fill, spec.data_type)
            try:
                save_layer_image(item, obj)
            except (OSError, RuntimeError) as ex:
                self.report({"ERROR"}, f"Cannot save the image of {obj.name}: {ex}")
                return None
            baked.append((obj.name, target.method))
        return baked, coverage, notes


def _undo_images(
    scene: bpy.types.Scene, layer_name: str, touched: list[tuple[bpy.types.Object, bool]]
) -> None:
    """After a failed bake: remove the images it made, reload the others from their files.

    Images of objects baked before the failure were saved, so they reload baked.
    """
    item = find_layer(scene, layer_name)
    if item is None:
        return
    for obj, made in touched:
        index = next((i for i, e in enumerate(item.images) if e.object == obj), None)
        if index is None:
            continue
        image = item.images[index].image
        if made:
            item.images.remove(index)
            if image is not None and not image.users:
                bpy.data.images.remove(image)
        elif image is not None and image.filepath:
            try:
                image.reload()
            except RuntimeError:
                log.warning("could not reload %s after a failed bake", image.name)


class GEOTOOLS_OT_check_layer_uvs(bpy.types.Operator):
    """Check the selected texture layer's UV map for triangles that share texels"""

    bl_idname = "geotools.check_layer_uvs"
    bl_label = "Check UV Overlap"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        item = _target_layer(context, "")
        if item is None or item.domain != Domain.TEXTURE.value:
            cls.poll_message_set("Select a texture layer")
            return False
        if context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = _target_layer(context, "")
        depsgraph = context.evaluated_depsgraph_get()
        issues: list[Issue] = []
        checked = 0
        for obj in source_objects(context.scene):
            uv_name = uv_map_name(obj.evaluated_get(depsgraph).data, item.uv_map)
            if uv_name is None:
                continue
            checked += 1
            _, issue = check_object_uvs(obj, depsgraph, uv_name, item.resolution)
            if issue is not None:
                issues.append(issue)
        if issues:
            self.report({"WARNING"}, " ".join(i.message for i in issues[:3]))
        else:
            self.report({"INFO"}, f"No overlapping UVs on {checked} object{'s' * (checked != 1)}")
        return {"FINISHED"}


IMAGE_ROWS = 6
"""Images the Bake sub-panel lists before summarising the rest."""


def draw_bake(layout: bpy.types.UILayout, context: bpy.types.Context, item) -> None:
    """The Bake sub-panel for a TEXTURE layer: image settings and the Cycles bake."""
    col = split_column(layout)
    objects = source_objects(context.scene)
    mesh = objects[0].data if objects else None
    if mesh is not None and len(mesh.uv_layers):
        row = col.row()
        row.alert = bool(item.uv_map) and item.uv_map not in mesh.uv_layers
        row.prop_search(item, "uv_map", mesh, "uv_layers", icon="GROUP_UVS")
    else:
        col.prop(item, "uv_map", icon="GROUP_UVS", placeholder="Active render UV map")
    col.prop(item, "resolution")
    col.prop(item, "margin")

    row = big_button(layout)
    row.operator("geotools.bake_texture_layer", text="Bake...", icon="RENDER_STILL")
    layout.operator("geotools.check_layer_uvs", icon="UV")

    images = [(e.object, e.image) for e in item.images if e.object is not None]
    if not images:
        layout.label(text="Not baked yet", icon="INFO")
        return
    box = layout.box()
    col = box.column(align=True)
    for obj, image in images[:IMAGE_ROWS]:
        name = image.name if image is not None else "missing"
        col.label(text=f"{obj.name}: {name}", icon="IMAGE_DATA" if image else "ERROR")
    if len(images) > IMAGE_ROWS:
        col.label(text=f"... and {len(images) - IMAGE_ROWS} more")
    provenance = spec_from_props(item).provenance
    if provenance and provenance.get("tool") == TOOL:
        parameters = provenance.get("parameters") or {}
        if isinstance(parameters, dict) and parameters.get("attribute"):
            box.label(text=f"Baked from {parameters['attribute']}", icon="INFO")


classes = (
    GEOTOOLS_OT_bake_texture_layer,
    GEOTOOLS_OT_check_layer_uvs,
)

register, unregister = bpy.utils.register_classes_factory(classes)
