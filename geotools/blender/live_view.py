"""The live layer view: Display > Show in Viewport.

Turning a layer's ``presentation.show_in_viewport`` on shows its values in the
viewport in the colours of its variant tileset, and keeps them current. It is
built from stock nodes the user can open, and GeoTools only keeps them in step
with the layer's Display settings:

* A Geometry Nodes group per layer, ``GeoTools View <layer>``, tagged with the
  ID property :data:`TAG` (the layer's name). Each source object gets a
  ``GeoTools View`` modifier using it, pinned last in its stack, viewport only
  (``show_render`` off). For a FACE or CORNER layer the group reads the
  layer's attribute (Named Attribute), checks that it exists and is not NaN,
  infinite or the no-data value, and maps it like ``core.ramps``:
  ``apply_ramp`` as Map Range (Logarithm first for a log scale) into a Color
  Ramp of the palette's sRGB stops, then the exact sRGB-to-linear curve
  (Vector Math), or ``apply_categories`` as an Index Switch of the category
  colours. The colour goes into :data:`VIEW_ATTRIBUTE`, a byte colour
  attribute on the face corners, and Set Material gives every face the shared
  ``GeoTools View`` material, which shows that attribute.
* A TEXTURE layer's values are texels, so its colours are worked out in the
  material: each object gets its own ``GeoTools View <layer> <object>``
  material (UV Map, the layer's image with Closest interpolation, Separate
  Color) feeding a shared shader group ``GeoTools View <layer> Ramp`` with
  the same ramp, into the base colour (or the emission, for the EMISSIVE
  target). The Geometry Nodes group then only sets that material, passed in
  as a modifier input.

Only the modifier's output carries the view: the user's material slots,
materials, node groups and attribute values are never changed. The one change
to source data is the mesh's active colour attribute (the one Solid mode's
Attribute colour shows, not the render one the export reads), which is
pointed at :data:`VIEW_ATTRIBUTE` while the view is on; the previous name is
kept in the mesh's ``export.VIEW_ACTIVE_KEY`` ID property and put back.
Solid mode is switched to the Attribute colour in every 3D viewport (for a
FACE or CORNER layer), and the previous colour type kept in the screen's
:data:`SHADING_KEY` ID property: screens are outside undo, so ``_on_undo``
puts the shading in step with the layer state after an undo or redo. A
texture layer shows in Material Preview (EEVEE). Cycles reads a NaN or
infinite texel as 0, so in a Cycles viewport such a texel shows as the value
0 does, not in the no-data colour.

The modifiers follow the layer: Geometry Nodes changes and topology changes
upstream show at once, since the view is evaluated last. The Display settings
(and the layer's attribute, no-data value and categories) rebuild the group
through property updates; an automatic range end uses the cached
``auto_min``/``auto_max``, which turning the view on (and every layer refresh)
works out again. A range that cannot be resolved shows every face in the
no-data colour, as the Display panel already flags it.

``export.view_hidden`` takes the view off while the exporter evaluates the
source, so a publish with the view on is byte-identical to one with it off. Turning the
view off removes the modifiers, the groups and materials no longer used,
the active colour change and the shading change. Only one layer is shown at
a time per scene. A source object whose modifiers come from a library is
skipped. With the build profile's Apply Modifiers off a publish reads the
original meshes, while the view shows the end of the modifier stack.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass

import bpy
import numpy as np
from bpy.app.handlers import persistent

from ..core.layers import DataType, Domain, effective_attribute_name, parse_number
from ..core.presentation import VIEW_ATTRIBUTE, Target
from ..core.ramps import palette_stops, srgb_bytes_to_linear
from .export import VIEW_ACTIVE_KEY, VIEW_NAME, is_view_modifier
from .layers import source_objects, spec_from_props, update_value_range
from .presentation import BOOLEAN_RANGE, category_colours, presentation_settings

log = logging.getLogger(__name__)

TAG = "geotools_view"
"""ID property naming the layer a view node group or material belongs to."""
KEY = "geotools_view_key"
"""ID property holding the settings a view node group or material was built from."""
SHADING_KEY = "geotools_view_shading"
"""Screen ID property: JSON of area index -> Solid colour type before the view."""
VIEW_COLOR_TYPE = "VERTEX"  # Solid mode's "Attribute" colour

_ROUGHNESS = 0.85  # a matt map: the colours, not highlights
_MATERIAL_VERSION = 1  # bump to rebuild the view materials of saved files
_LINEAR_THRESHOLD = 0.04045  # sRGB IEC 61966-2-1, as core.ramps.srgb_bytes_to_linear
_COLUMN = 220.0
_ROW = 180.0


def shown_layer(scene: bpy.types.Scene) -> bpy.types.PropertyGroup | None:
    """The layer the scene shows in the viewport, if any."""
    for item in scene.geotools.layers:
        if item.presentation.show_in_viewport:
            return item
    return None


# --- what the view is built from -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewParams:
    """What a layer's view is built from; equal params give an equal node tree."""

    layer: str
    domain: str
    data_type: str
    attribute: str
    nodata: float | None
    palette: str
    log: bool
    value_range: tuple[float, float] | None  # None: no usable range, all no-data
    nodata_rgba: tuple[int, int, int, int]
    category_rgba: tuple[tuple[int, int, int, int], ...]
    target: str

    @property
    def is_category(self) -> bool:
        return self.data_type == DataType.CATEGORY.value

    @property
    def reads_int(self) -> bool:
        return self.data_type != DataType.FLOAT.value

    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _resolved_range(item, settings) -> tuple[float, float] | None:
    """The ramp range a publish would use, from the settings and the cached auto range."""
    low, high = settings.range_min, settings.range_max
    if low is None or high is None:
        if item.data_type == DataType.BOOLEAN.value:
            auto = BOOLEAN_RANGE
        else:
            auto = (parse_number(item.auto_min), parse_number(item.auto_max))
        low = auto[0] if low is None else low
        high = auto[1] if high is None else high
    if low is None or high is None or not (math.isfinite(low) and math.isfinite(high)):
        return None
    if low > high or (settings.scale == "LOG" and low <= 0):
        return None
    return float(low), float(high)


def view_params(item: bpy.types.PropertyGroup) -> ViewParams:
    """The params of ``item``'s view (cheap: reads no mesh)."""
    spec = spec_from_props(item)
    settings = presentation_settings(item)
    nodata = spec.nodata if spec.nodata is not None and math.isfinite(spec.nodata) else None
    categories: tuple[tuple[int, int, int, int], ...] = ()
    value_range = None
    if spec.data_type is DataType.CATEGORY:
        rgba = category_colours(item, len(spec.categories))
        categories = tuple(tuple(int(c) for c in row) for row in rgba)  # type: ignore[misc]
    else:
        value_range = _resolved_range(item, settings)
    return ViewParams(
        layer=item.name,
        domain=spec.domain.value,
        data_type=spec.data_type.value,
        attribute=effective_attribute_name(spec),
        nodata=nodata,
        palette=settings.palette,
        log=settings.scale == "LOG",
        value_range=value_range,
        nodata_rgba=settings.nodata_rgba,
        category_rgba=categories,
        target=settings.target.value,
    )


def _linear(rgba: Sequence[int]) -> tuple[float, float, float, float]:
    linear = srgb_bytes_to_linear(np.array([rgba], dtype=np.uint8))[0]
    return (float(linear[0]), float(linear[1]), float(linear[2]), 1.0)


# --- node building -------------------------------------------------------------------------


class _Nodes:
    """Adds nodes to a tree on a grid, so the tree reads left to right when opened."""

    def __init__(self, tree: bpy.types.NodeTree) -> None:
        self.tree = tree
        self.geometry = tree.bl_idname == "GeometryNodeTree"

    def add(
        self, idname: str, column: float, row: float, label: str = "", **props
    ) -> bpy.types.Node:
        node = self.tree.nodes.new(idname)
        node.location = (column * _COLUMN, -row * _ROW)
        if label:
            node.label = label
        for name, value in props.items():
            setattr(node, name, value)
        return node

    def link(self, out: bpy.types.NodeSocket, into: bpy.types.NodeSocket) -> None:
        self.tree.links.new(out, into)

    def math(
        self, operation: str, a, b=None, column=0.0, row=0.0, label=""
    ) -> bpy.types.NodeSocket:
        node = self.add("ShaderNodeMath", column, row, label, operation=operation)
        if operation == "COMPARE":
            node.inputs[2].default_value = 0.0  # Blender still allows 1e-5
        for socket, value in ((node.inputs[0], a), (node.inputs[1], b)):
            if isinstance(value, bpy.types.NodeSocket):
                self.link(value, socket)
            elif value is not None:
                socket.default_value = value
        return node.outputs[0]

    def vector(self, operation: str, a, b=None, column=0.0, row=0.0, label=""):
        node = self.add("ShaderNodeVectorMath", column, row, label, operation=operation)
        self.link(a, node.inputs[0])
        if operation == "SCALE":
            node.inputs["Scale"].default_value = b
        elif b is not None:
            node.inputs[1].default_value = (b, b, b)
        return node.outputs["Vector"]


def _socket(sockets, identifier: str) -> bpy.types.NodeSocket:
    """A node's socket by identifier (several Mix sockets share a name)."""
    return next(s for s in sockets if s.identifier == identifier)


def _ramp_position(nodes: _Nodes, value, params: ViewParams, column: float, row: float):
    """``apply_ramp``'s position along the palette, 0 to 1, of ``value``."""
    low, high = params.value_range  # type: ignore[misc]
    if low == high:  # below: low end, above: high end, equal: the middle
        sign = nodes.math(
            "SIGN",
            nodes.math("SUBTRACT", value, low, column, row),
            None,
            column + 1,
            row,
            "Above or Below",
        )
        node = nodes.add(
            "ShaderNodeMath",
            column + 2,
            row,
            "Ramp Position",
            operation="MULTIPLY_ADD",
            use_clamp=True,
        )
        nodes.link(sign, node.inputs[0])
        node.inputs[1].default_value = 0.5
        node.inputs[2].default_value = 0.5
        return node.outputs[0]
    if params.log:  # values at or below the minimum (0 included) get the low end
        value = nodes.math("MAXIMUM", value, low, column, row, "Not Below Minimum")
        value = nodes.math("LOGARITHM", value, 10.0, column + 1, row, "Log10")
        low, high = math.log10(low), math.log10(high)
    node = nodes.add(
        "ShaderNodeMapRange",
        column + 2,
        row,
        "Ramp Position",
        data_type="FLOAT",
        interpolation_type="LINEAR",
        clamp=True,
    )
    nodes.link(value, node.inputs[0])
    node.inputs[1].default_value = low
    node.inputs[2].default_value = high
    node.inputs[3].default_value = 0.0
    node.inputs[4].default_value = 1.0
    return node.outputs[0]


def _palette(nodes: _Nodes, position, params: ViewParams, column: float, row: float):
    """Color Ramp of the palette's sRGB stops, then the sRGB-to-linear curve: linear RGB."""
    stops = palette_stops(params.palette)
    ramp_node = nodes.add("ShaderNodeValToRGB", column, row, "Palette (sRGB)")
    ramp = ramp_node.color_ramp
    ramp.interpolation = "LINEAR"
    ramp.color_mode = "RGB"
    last = len(stops) - 1
    while len(ramp.elements) < len(stops):
        ramp.elements.new(0.5)
    for index, element in enumerate(ramp.elements):
        element.position = index / last
    for element, rgb in zip(ramp.elements, stops, strict=True):
        element.color = (float(rgb[0]), float(rgb[1]), float(rgb[2]), 1.0)
    nodes.link(position, ramp_node.inputs["Fac"])
    srgb = ramp_node.outputs["Color"]
    # Per channel: c / 12.92 below the threshold, ((c + 0.055) / 1.055) ^ 2.4 above.
    c = column + 1.5
    low = nodes.vector("SCALE", srgb, 1.0 / 12.92, c, row + 1, "Low Part")
    high = nodes.vector("ADD", srgb, 0.055, c, row - 1)
    high = nodes.vector("SCALE", high, 1.0 / 1.055, c + 1, row - 1)
    high = nodes.vector("POWER", high, 2.4, c + 2, row - 1, "High Part")
    above = nodes.vector("SUBTRACT", srgb, _LINEAR_THRESHOLD, c, row)
    above = nodes.vector("SIGN", above, None, c + 1, row)
    above = nodes.vector("MAXIMUM", above, 0.0, c + 2, row, "Above Threshold")
    mix = nodes.add(
        "ShaderNodeMix",
        c + 3,
        row,
        "sRGB to Linear",
        data_type="VECTOR",
        factor_mode="NON_UNIFORM",
        clamp_factor=True,
    )
    nodes.link(above, _socket(mix.inputs, "Factor_Vector"))
    nodes.link(low, _socket(mix.inputs, "A_Vector"))
    nodes.link(high, _socket(mix.inputs, "B_Vector"))
    return _socket(mix.outputs, "Result_Vector")


# --- Geometry Nodes: FACE and CORNER layers --------------------------------------------------


def _compare(nodes: _Nodes, data_type: str, operation: str, a, b, column, row, label=""):
    node = nodes.add(
        "FunctionNodeCompare",
        column,
        row,
        label,
        data_type=data_type,
        operation=operation,
        mode="ELEMENT",
    )
    nodes.link(a, node.inputs["A"])
    if isinstance(b, bpy.types.NodeSocket):
        nodes.link(b, node.inputs["B"])
    else:
        node.inputs["B"].default_value = b
    if "Epsilon" in node.inputs:
        node.inputs["Epsilon"].default_value = 0.0
    return node.outputs[0]


def _all(nodes: _Nodes, conditions: list, column: float, row: float):
    result = conditions[0]
    for index, condition in enumerate(conditions[1:]):
        node = nodes.add("FunctionNodeBooleanMath", column + index * 0.5, row, operation="AND")
        nodes.link(result, node.inputs[0])
        nodes.link(condition, node.inputs[1])
        result = node.outputs[0]
    return result


def _attribute_colour(nodes: _Nodes, params: ViewParams):
    """The view colour of each element (linear RGBA) for a FACE or CORNER layer."""
    read = nodes.add(
        "GeometryNodeInputNamedAttribute",
        0,
        3,
        "Layer Values",
        data_type="INT" if params.reads_int else "FLOAT",
    )
    read.inputs["Name"].default_value = params.attribute
    value, exists = read.outputs["Attribute"], read.outputs["Exists"]
    valid = [exists]
    kind = "INT" if params.reads_int else "FLOAT"
    if not params.reads_int:  # NaN and infinities differ from themselves
        valid.append(_compare(nodes, "FLOAT", "EQUAL", value, value, 1, 1, "Is Finite"))
    nodata = params.nodata
    if nodata is not None and (not params.reads_int or float(nodata).is_integer()):
        b = int(nodata) if params.reads_int else nodata
        valid.append(_compare(nodes, kind, "NOT_EQUAL", value, b, 1, 2, "Not No-Data"))
    if params.is_category:
        count = len(params.category_rgba)
        if count:
            valid.append(_compare(nodes, "INT", "GREATER_EQUAL", value, 0, 1, 4, "Category"))
            valid.append(_compare(nodes, "INT", "LESS_THAN", value, count, 1, 5, "Category"))
            switch = nodes.add(
                "GeometryNodeIndexSwitch", 3, 4, "Category Colours", data_type="RGBA"
            )
            items = switch.index_switch_items
            while len(items) < count:
                items.new()
            while len(items) > count:
                items.remove(items[len(items) - 1])
            nodes.link(value, switch.inputs[0])
            for index, rgba in enumerate(params.category_rgba):
                switch.inputs[index + 1].default_value = _linear(rgba)
            colour = switch.outputs[0]
        else:
            colour = None
    elif params.value_range is not None:
        colour = _palette(nodes, _ramp_position(nodes, value, params, 1, 4), params, 4, 4)
    else:
        colour = None
    choose = nodes.add("GeometryNodeSwitch", 9, 2, "No-Data Colour", input_type="RGBA")
    choose.inputs["False"].default_value = _linear(params.nodata_rgba)
    if colour is None:  # nothing to show but no data
        choose.inputs["Switch"].default_value = False
    else:
        nodes.link(_all(nodes, valid, 7, 1), choose.inputs["Switch"])
        nodes.link(colour, choose.inputs["True"])
    return choose.outputs[0]


def _new_group(name: str, tree_type: str) -> bpy.types.NodeTree:
    group = bpy.data.node_groups.new(name, tree_type)
    if tree_type == "GeometryNodeTree":
        group.is_modifier = True
    return group


def _build_geometry(group: bpy.types.NodeTree, params: ViewParams) -> None:
    """(Re)build the Geometry Nodes group of a view."""
    group.nodes.clear()
    interface = group.interface
    if not interface.items_tree:  # made once: the modifiers' inputs keep their identifiers
        interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        material_socket = interface.new_socket(
            "Material", in_out="INPUT", socket_type="NodeSocketMaterial"
        )
        material_socket.description = "The material the view shows (set by GeoTools)"
        interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes = _Nodes(group)
    inputs = nodes.add("NodeGroupInput", -2, 0)
    geometry = inputs.outputs["Geometry"]
    if params.domain != Domain.TEXTURE.value:
        colour = _attribute_colour(nodes, params)
        if params.domain == Domain.FACE.value:  # work it out once per face
            capture = nodes.add("GeometryNodeCaptureAttribute", 10, 0, "Per Face", domain="FACE")
            item = capture.capture_items.new("RGBA", "Colour")
            nodes.link(geometry, capture.inputs["Geometry"])
            nodes.link(colour, capture.inputs[item.name])
            geometry, colour = capture.outputs["Geometry"], capture.outputs[item.name]
        store = nodes.add(
            "GeometryNodeStoreNamedAttribute",
            11,
            0,
            "View Colour",
            data_type="BYTE_COLOR",
            domain="CORNER",
        )
        store.inputs["Name"].default_value = VIEW_ATTRIBUTE
        nodes.link(geometry, store.inputs["Geometry"])
        nodes.link(colour, store.inputs["Value"])
        geometry = store.outputs["Geometry"]
    set_material = nodes.add("GeometryNodeSetMaterial", 12, 0, "View Material")
    nodes.link(geometry, set_material.inputs["Geometry"])
    nodes.link(inputs.outputs["Material"], set_material.inputs["Material"])
    output = nodes.add("NodeGroupOutput", 13, 0)
    nodes.link(set_material.outputs["Geometry"], output.inputs["Geometry"])


# --- shader nodes: TEXTURE layers --------------------------------------------------------------


def _build_shader_ramp(group: bpy.types.NodeTree, params: ViewParams) -> None:
    """(Re)build a texture layer's shared ramp: Value (a texel) in, Color (linear) out.

    Shader Math compares within 1e-5, so a texel that close to the no-data
    value (or to a category) counts as it.
    """
    group.nodes.clear()
    if not group.interface.items_tree:
        group.interface.new_socket("Value", in_out="INPUT", socket_type="NodeSocketFloat")
        group.interface.new_socket("Color", in_out="OUTPUT", socket_type="NodeSocketColor")
    nodes = _Nodes(group)
    value = nodes.add("NodeGroupInput", 0, 2).outputs["Value"]
    nodata = _linear(params.nodata_rgba)
    colour = None
    valid = nodes.math("COMPARE", value, value, 1, 1, "Is Finite")
    if params.nodata is not None:
        same = nodes.math("COMPARE", value, params.nodata, 1, 2)
        valid = nodes.math(
            "MULTIPLY", valid, nodes.math("SUBTRACT", 1.0, same, 2, 2), 3, 1, "Not No-Data"
        )
    if params.is_category:
        for index, rgba in enumerate(params.category_rgba):
            mix = nodes.add(
                "ShaderNodeMix",
                4 + index * 0.5,
                4,
                f"Category {index}",
                data_type="RGBA",
                blend_type="MIX",
                clamp_factor=True,
            )
            nodes.link(
                nodes.math("COMPARE", value, float(index), 3 + index * 0.5, 5),
                _socket(mix.inputs, "Factor_Float"),
            )
            if colour is None:
                _socket(mix.inputs, "A_Color").default_value = nodata
            else:
                nodes.link(colour, _socket(mix.inputs, "A_Color"))
            _socket(mix.inputs, "B_Color").default_value = _linear(rgba)
            colour = _socket(mix.outputs, "Result_Color")
    elif params.value_range is not None:
        colour = _palette(nodes, _ramp_position(nodes, value, params, 1, 4), params, 4, 4)
    choose = nodes.add(
        "ShaderNodeMix",
        11,
        2,
        "No-Data Colour",
        data_type="RGBA",
        blend_type="MIX",
        clamp_factor=True,
    )
    _socket(choose.inputs, "A_Color").default_value = nodata
    if colour is None:
        _socket(choose.inputs, "Factor_Float").default_value = 0.0
    else:
        nodes.link(valid, _socket(choose.inputs, "Factor_Float"))
        nodes.link(colour, _socket(choose.inputs, "B_Color"))
    output = nodes.add("NodeGroupOutput", 12, 2)
    nodes.link(_socket(choose.outputs, "Result_Color"), output.inputs["Color"])


def _build_texture_material(
    material: bpy.types.Material,
    ramp: bpy.types.NodeTree,
    image: bpy.types.Image | None,
    uv_map: str,
    target: str,
    nodata: tuple[float, float, float, float],
) -> None:
    """(Re)build an object's texture view material: its layer image through ``ramp``."""
    tree = material.node_tree
    tree.nodes.clear()
    nodes = _Nodes(tree)
    bsdf = nodes.add("ShaderNodeBsdfPrincipled", 5, 0)
    bsdf.inputs["Roughness"].default_value = _ROUGHNESS
    out = nodes.add("ShaderNodeOutputMaterial", 7, 0)
    nodes.link(bsdf.outputs["BSDF"], out.inputs["Surface"])
    if image is None:
        colour_socket = None
    else:
        uv = nodes.add("ShaderNodeUVMap", 0, 0, "Layer UV Map", uv_map=uv_map)
        texture = nodes.add(
            "ShaderNodeTexImage",
            1,
            0,
            "Layer Image",
            image=image,
            interpolation="Closest",
            extension="EXTEND",
        )
        nodes.link(uv.outputs["UV"], texture.inputs["Vector"])
        split = nodes.add("ShaderNodeSeparateColor", 2, 0, "First Channel", mode="RGB")
        nodes.link(texture.outputs["Color"], split.inputs["Color"])
        group = nodes.add("ShaderNodeGroup", 3, 0, "Layer Ramp", node_tree=ramp)
        nodes.link(split.outputs["Red"], group.inputs["Value"])
        colour_socket = group.outputs["Color"]
    slot = "Emission Color" if target == Target.EMISSIVE.value else "Base Color"
    if target == Target.EMISSIVE.value:
        bsdf.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
        bsdf.inputs["Emission Strength"].default_value = 1.0
    if colour_socket is None:
        bsdf.inputs[slot].default_value = nodata
    else:
        nodes.link(colour_socket, bsdf.inputs[slot])


def _build_attribute_material(material: bpy.types.Material) -> None:
    tree = material.node_tree
    tree.nodes.clear()
    nodes = _Nodes(tree)
    colour = nodes.add("ShaderNodeVertexColor", 0, 0, "View Colour", layer_name=VIEW_ATTRIBUTE)
    bsdf = nodes.add("ShaderNodeBsdfPrincipled", 1, 0)
    bsdf.inputs["Roughness"].default_value = _ROUGHNESS
    out = nodes.add("ShaderNodeOutputMaterial", 3, 0)
    nodes.link(colour.outputs["Color"], bsdf.inputs["Base Color"])
    nodes.link(bsdf.outputs["BSDF"], out.inputs["Surface"])


def _new_material(name: str, layer: str) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    if material.node_tree is None:
        material.use_nodes = True
    material[TAG] = layer
    return material


def _tagged(blocks, layer: str | None, kind: str | None = None) -> Iterator:
    """The view's own data blocks (by :data:`TAG`), of ``layer`` (None: any)."""
    for block in blocks:
        if block.library is not None or TAG not in block:
            continue
        if (layer is None or block[TAG] == layer) and (
            kind is None or getattr(block, "bl_idname", kind) == kind
        ):
            yield block


def _rebuild_if_changed(block, key: str, build) -> None:
    if block.get(KEY) != key:
        build()
        block[KEY] = key


def _ensure_group(name: str, layer: str, tree_type: str) -> bpy.types.NodeTree:
    group = next(_tagged(bpy.data.node_groups, layer, tree_type), None)
    if group is None:
        group = _new_group(name, tree_type)
        group[TAG] = layer
    elif group.name != name:
        group.name = name
    return group


def _attribute_material() -> bpy.types.Material:
    material = next(
        (m for m in _tagged(bpy.data.materials, "") if m.name.startswith(VIEW_NAME)), None
    )
    if material is None:
        material = _new_material(VIEW_NAME, "")
    _rebuild_if_changed(
        material,
        f"{VIEW_ATTRIBUTE}:{_MATERIAL_VERSION}",
        lambda: _build_attribute_material(material),
    )
    return material


# --- keeping the scene in step -----------------------------------------------------------------


_busy = {"sync": False}


def _layer_images(item) -> dict[int, bpy.types.Image]:
    return {e.object.as_pointer(): e.image for e in item.images if e.object is not None}


def _texture_materials(item, params: ViewParams, objects) -> dict[int, bpy.types.Material]:
    ramp_name = f"{VIEW_NAME} {item.name} Ramp"
    ramp = _ensure_group(ramp_name, item.name, "ShaderNodeTree")
    _rebuild_if_changed(ramp, params.key(), lambda: _build_shader_ramp(ramp, params))
    images = _layer_images(item)
    existing = {m.name: m for m in _tagged(bpy.data.materials, item.name)}
    nodata = _linear(params.nodata_rgba)
    out: dict[int, bpy.types.Material] = {}
    for obj in objects:
        name = f"{VIEW_NAME} {item.name} {obj.name}"
        material = existing.get(name) or _new_material(name, item.name)
        image = images.get(obj.as_pointer())
        key = json.dumps(
            [_MATERIAL_VERSION, params.target, item.uv_map, image.name if image else None, nodata]
        )
        _rebuild_if_changed(
            material,
            key,
            lambda m=material, i=image: _build_texture_material(
                m, ramp, i, item.uv_map, params.target, nodata
            ),
        )
        out[obj.as_pointer()] = material
    return out


def _material_input(group: bpy.types.NodeTree) -> str:
    return next(
        s.identifier
        for s in group.interface.items_tree
        if getattr(s, "socket_type", "") == "NodeSocketMaterial"
    )


def _place(obj: bpy.types.Object, group, material, socket: str) -> None:
    """Give ``obj`` one view modifier, last, using ``group`` and ``material``."""
    views = [m for m in obj.modifiers if is_view_modifier(m)]
    for extra in views[1:]:
        obj.modifiers.remove(extra)
    modifier = views[0] if views else obj.modifiers.new(VIEW_NAME, "NODES")
    if modifier.node_group != group:
        modifier.node_group = group
    value = getattr(modifier.properties.inputs, socket)
    if value.value != material:
        value.value = material
    if modifier.show_render:
        modifier.show_render = False
    if not modifier.show_viewport:
        modifier.show_viewport = True
    if not modifier.use_pin_to_last:  # stays last when the user adds modifiers
        modifier.use_pin_to_last = True
    index = list(obj.modifiers).index(modifier)
    if index != len(obj.modifiers) - 1:  # another pinned modifier came after it
        try:
            obj.modifiers.move(index, len(obj.modifiers) - 1)
        except RuntimeError as ex:  # the user's pinned modifier keeps its place
            log.info("%s: the view stays before a pinned modifier: %s", obj.name, ex)


def _take_active_colour(mesh: bpy.types.Mesh) -> None:
    if mesh.library is not None:
        return
    colours = mesh.color_attributes
    if VIEW_ACTIVE_KEY not in mesh:
        mesh[VIEW_ACTIVE_KEY] = colours.active_color_name
    if colours.active_color_name != VIEW_ATTRIBUTE:
        colours.active_color_name = VIEW_ATTRIBUTE


def _give_back_active_colour(mesh: bpy.types.Mesh) -> None:
    previous = mesh.get(VIEW_ACTIVE_KEY)
    if previous is None:
        return
    if mesh.color_attributes.active_color_name == VIEW_ATTRIBUTE:
        mesh.color_attributes.active_color_name = str(previous)
    del mesh[VIEW_ACTIVE_KEY]


def _remove_unused() -> None:
    """Remove the view's node groups and materials no data block uses any more.

    By ``user_map``, not ``users``: a material once held by a modifier input
    can keep an extra user after the modifier is gone.
    """
    for _round in range(2):  # removing a material frees its ramp group
        blocks = [*_tagged(bpy.data.materials, None), *_tagged(bpy.data.node_groups, None)]
        if not blocks:
            return
        users = bpy.data.user_map(subset=blocks)
        unused = [b for b in blocks if not (users.get(b, set()) - {b})]
        if not unused:
            return
        bpy.data.batch_remove(unused)


def sync(scene: bpy.types.Scene) -> None:
    """Put the scene's view in step with its layers: the modifiers, groups,
    materials, active colours and shading. Idempotent; reads no mesh data."""
    if _busy["sync"]:
        return
    _busy["sync"] = True
    try:
        _sync(scene)
    finally:
        _busy["sync"] = False
    sync_shading()


def _sync(scene: bpy.types.Scene) -> None:
    item = shown_layer(scene)
    wanted: dict[int, tuple[bpy.types.NodeTree, bpy.types.Material]] = {}
    socket = ""
    if item is not None:
        params = view_params(item)
        objects = [o for o in source_objects(scene) if o.library is None]
        group = _ensure_group(f"{VIEW_NAME} {item.name}", item.name, "GeometryNodeTree")
        _rebuild_if_changed(group, params.key(), lambda: _build_geometry(group, params))
        if params.domain == Domain.TEXTURE.value:
            materials = _texture_materials(item, params, objects)
        else:
            shared = _attribute_material()
            materials = {o.as_pointer(): shared for o in objects}
        wanted = {o.as_pointer(): (group, materials[o.as_pointer()]) for o in objects}
        socket = _material_input(group)
    attribute_view = item is not None and item.domain != Domain.TEXTURE.value
    taken: set[int] = set()
    for obj in scene.objects:
        if obj.library is not None or obj.type != "MESH":
            continue
        entry = wanted.get(obj.as_pointer())
        if entry is not None:
            _place(obj, entry[0], entry[1], socket)
            if attribute_view:
                taken.add(obj.data.as_pointer())
                _take_active_colour(obj.data)
            continue
        for modifier in [m for m in obj.modifiers if is_view_modifier(m)]:
            obj.modifiers.remove(modifier)
    for mesh in bpy.data.meshes:
        if VIEW_ACTIVE_KEY in mesh and mesh.as_pointer() not in taken:
            _give_back_active_colour(mesh)
    _remove_unused()


# --- viewport shading ------------------------------------------------------------------------


def _wants_attribute_shading() -> bool:
    for scene in bpy.data.scenes:
        item = shown_layer(scene)
        if item is not None and item.domain != Domain.TEXTURE.value:
            return True
    return False


def sync_shading() -> None:
    """Solid mode shows the view's attribute in every 3D viewport while a FACE or
    CORNER layer is shown, and the user's colour type otherwise."""
    want = _wants_attribute_shading()
    for screen in bpy.data.screens:
        try:
            saved = json.loads(screen.get(SHADING_KEY, "{}"))
        except ValueError:
            saved = {}
        before = dict(saved)
        for index, area in enumerate(screen.areas):
            if area.type != "VIEW_3D":
                continue
            shading = area.spaces.active.shading
            key = str(index)
            if want and key not in saved:
                saved[key] = shading.color_type
                shading.color_type = VIEW_COLOR_TYPE
            elif not want and key in saved:
                if shading.color_type == VIEW_COLOR_TYPE:
                    shading.color_type = saved[key]
                del saved[key]
        if saved != before:
            if saved:
                screen[SHADING_KEY] = json.dumps(saved, sort_keys=True)
            elif SHADING_KEY in screen:
                del screen[SHADING_KEY]


# --- property updates ---------------------------------------------------------------------------


def _owner(group: bpy.types.PropertyGroup) -> bpy.types.PropertyGroup | None:
    """The ``GeoToolsLayer`` a layer, presentation or category colour belongs to."""
    path = group.path_from_id()
    cut = path.find(".presentation")
    try:
        return group.id_data.path_resolve(path[:cut] if cut >= 0 else path)
    except ValueError:
        return None


def show_changed(presentation: bpy.types.PropertyGroup, context: bpy.types.Context) -> None:
    """``show_in_viewport`` update: show this layer alone, or take the view off."""
    item = _owner(presentation)
    if item is None:
        return
    scene = presentation.id_data
    if presentation.show_in_viewport:
        _busy["sync"] = True  # one sync below, not one per layer turned off
        try:
            for other in scene.geotools.layers:
                if other != item and other.presentation.show_in_viewport:
                    other.presentation.show_in_viewport = False
        finally:
            _busy["sync"] = False
        refresh_auto_range(scene, item)
    sync(scene)


def display_changed(group: bpy.types.PropertyGroup, context: bpy.types.Context) -> None:
    """Update of a setting the view is built from: rebuild it if it is shown."""
    item = _owner(group)
    if item is not None and item.presentation.show_in_viewport:
        sync(group.id_data)


def scale_changed(presentation: bpy.types.PropertyGroup, context: bpy.types.Context) -> None:
    """A log scale takes its automatic range from the positive values only."""
    item = _owner(presentation)
    if item is not None and presentation.show_in_viewport:
        refresh_auto_range(presentation.id_data, item)
        sync(presentation.id_data)


def source_changed(project: bpy.types.PropertyGroup, context: bpy.types.Context) -> None:
    if shown_layer(project.id_data) is not None:
        sync(project.id_data)


def refresh_auto_range(scene: bpy.types.Scene, item) -> None:
    """Work out the automatic range ends again if the view uses one (reads the values)."""
    p = item.presentation
    if item.data_type in (DataType.CATEGORY.value, DataType.BOOLEAN.value):
        return
    if parse_number(p.range_min) is not None and parse_number(p.range_max) is not None:
        return
    try:
        update_value_range(scene, item)
    except RuntimeError as ex:  # a source mesh in Edit Mode: keep the cached range
        log.info("view of %s keeps its automatic range: %s", item.name, ex)


# --- handlers -----------------------------------------------------------------------------------


@persistent
def _on_undo(*_args: object) -> None:
    """Screens are outside undo: put the shading in step with the restored layers."""
    try:
        sync_shading()
    except Exception:  # never break undo
        log.exception("restoring the viewport shading after undo failed")


classes: tuple[type, ...] = ()  # handlers only; the toggle is a layer property

_HANDLERS = (("undo_post", _on_undo), ("redo_post", _on_undo), ("load_post", _on_undo))


def register() -> None:
    for name, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)


def unregister() -> None:
    for name, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
