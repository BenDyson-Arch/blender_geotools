"""Vertex-space bake: freeze a Geometry Nodes output into a layer's ``gt_<name>`` attribute.

Operator ``geotools.bake_geonodes_layer`` (docs/design/m3-layers.md, section 2).
For every source object that has the chosen Geometry Nodes modifier, it reads
the named attribute from the mesh evaluated up to and including that modifier,
and stores it as the layer's attribute on the original mesh:

- **Same topology** (the evaluated polygons and corners match the original, as
  ``export.mesh_topology_digest`` decides): the values are copied, with domain
  conversions in numpy (``core.transfer``).
- **Changed topology**: the values are transferred with Geometry Nodes
  evaluations on a temporary object that shares the original mesh and reads the
  evaluated one with an Object Info node:

  - ``NEAREST``: Sample Nearest (face index) and Sample Nearest Surface (the
    interpolated value) at each source face centre, or, for a CORNER layer, at
    each corner moved 1% towards its face centre;
  - ``RAYCAST``: Raycast along the source face normal, both ways, within the
    maximum distance; the nearer hit wins and a miss falls back to NEAREST;
  - ``AREA_MEAN``: Sample Nearest gives each evaluated face the source face its
    centre is nearest to; numpy reduces by area (``core.transfer.area_reduce``)
    and source faces that receive nothing fall back to NEAREST. A CORNER layer
    gets each face's value on all its corners.

Mode layers (BOOLEAN, CATEGORY, INT aggregated by MODE) never interpolate: they
take the value of the evaluated face found, and AREA_MEAN takes the value with
the largest area. Every value is computed before anything is written, so a
failure leaves the source as it was. Temporary objects and node groups, the
modifiers turned off for the evaluation, the selection, the active object and
the mode are restored in ``finally`` blocks.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty, StringProperty

from ..core import transfer
from ..core.issues import Issue, Severity
from ..core.layers import Domain, LayerSpec, Origin, make_provenance, validate
from ..core.transfer import MeshArrays, TransferCounts
from .common import extension_version, redraw_ui
from .export import EVALUATED_PREFIX, mesh_topology_digest
from .layers import (
    ensure_layer,
    find_layer,
    other_layer_names,
    source_objects,
    spec_from_props,
    update_value_range,
    write_layer,
)
from .properties import GeoToolsLayer

log = logging.getLogger(__name__)

TOOL = "bake_geonodes"
"""``provenance["tool"]`` of a layer made by this bake."""

METHOD_ITEMS = (
    (
        "NEAREST",
        "Nearest",
        "Value at the nearest point of the evaluated surface to each source face centre",
    ),
    (
        "RAYCAST",
        "Raycast",
        (
            "Value where the source face normal, cast both ways, meets the evaluated surface; "
            "Nearest where it misses"
        ),
    ),
    (
        "AREA_MEAN",
        "Area Mean",
        (
            "Area-weighted mean of the evaluated faces nearest to each source face (the "
            "largest-area value for boolean and category layers); Nearest where none is"
        ),
    ),
)

DOMAIN_ITEMS = (
    ("LAYER", "Layer's Domain", "Bake into the layer's own domain"),
    ("FACE", "Face", "One value per face; the layer becomes a face layer"),
    ("CORNER", "Corner", "One value per face corner; the layer becomes a corner layer"),
)

CORNER_INSET = 0.01
"""How far a CORNER layer's sample point moves from the corner towards its face centre."""

TEMP_NAME = "GeoTools Bake (temporary)"
"""Name of the temporary objects and node groups, removed after every bake."""

# Attributes the temporary evaluations store.
_NEAREST = "geotools_bake_nearest"
_VALUE = "geotools_bake_value"
_OWNER = "geotools_bake_owner"
_RAY = ("geotools_bake_ray_a", "geotools_bake_ray_b")

# Evaluated attribute types the bake reads (as numpy dtype for foreach_get).
_ATTRIBUTE_DTYPES = {"FLOAT": np.float32, "INT": np.int32, "INT8": np.int32, "BOOLEAN": np.bool_}
_ATTRIBUTE_DOMAINS = ("POINT", "EDGE", "FACE", "CORNER")


class BakeError(Exception):
    """The bake cannot go on; the message is shown to the user."""


# --- mesh reading -----------------------------------------------------------------------


def _get(collection: bpy.types.bpy_prop_collection, attr: str, dtype: type) -> np.ndarray:
    out = np.empty(len(collection), dtype=dtype)
    collection.foreach_get(attr, out)
    return out


def mesh_arrays(mesh: bpy.types.Mesh) -> MeshArrays:
    """The topology arrays ``core.transfer`` needs, with ``foreach_get``."""
    return MeshArrays(
        loop_start=_get(mesh.polygons, "loop_start", np.int32),
        loop_total=_get(mesh.polygons, "loop_total", np.int32),
        corner_vert=_get(mesh.loops, "vertex_index", np.int32),
        corner_edge=_get(mesh.loops, "edge_index", np.int32),
    )


def read_attribute(mesh: bpy.types.Mesh, name: str, nodata: float | None) -> tuple:
    """``(values, valid, domain)`` of a scalar attribute of an evaluated mesh.

    Raises ``BakeError`` if the attribute is missing, not a scalar, or on a
    domain other than POINT, EDGE, FACE and CORNER.
    """
    attr = mesh.attributes.get(name)
    if attr is None:
        raise BakeError(f"the evaluated mesh has no attribute {name!r}")
    if attr.domain not in _ATTRIBUTE_DOMAINS:
        raise BakeError(f"attribute {name!r} is on the {attr.domain} domain")
    dtype = _ATTRIBUTE_DTYPES.get(attr.data_type)
    if dtype is None:
        raise BakeError(
            f"attribute {name!r} is {attr.data_type}; the bake reads float, integer and "
            "boolean attributes"
        )
    values = np.empty(len(attr.data), dtype=dtype)
    attr.data.foreach_get("value", values)
    return values.astype(np.float64), transfer.valid_mask(values, nodata), attr.domain


def _read(mesh: bpy.types.Mesh, name: str, dtype: type, size: int) -> np.ndarray:
    attr = mesh.attributes.get(name)
    if attr is None or len(attr.data) != size:
        raise BakeError(f"the transfer evaluation did not produce {name!r}")
    out = np.empty(size, dtype=dtype)
    attr.data.foreach_get("value", out)
    return out


def _polygon_areas(mesh: bpy.types.Mesh) -> np.ndarray:
    return _get(mesh.polygons, "area", np.float32).astype(np.float64)


# --- node trees ------------------------------------------------------------------------------


def _socket(sockets: bpy.types.bpy_prop_collection, identifier: str) -> bpy.types.NodeSocket:
    """A node socket by identifier (names repeat on nodes such as Mix)."""
    return next(socket for socket in sockets if socket.identifier == identifier)


class _Tree:
    """A Geometry Nodes modifier tree built from Python."""

    def __init__(self, owner: list[bpy.types.NodeTree] | None = None) -> None:
        tree = bpy.data.node_groups.new(TEMP_NAME, "GeometryNodeTree")
        if owner is not None:  # tracked at once, so a failed build is removed too
            owner.append(tree)
        tree.is_modifier = True
        tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        self.tree = tree
        self.input = self.node("NodeGroupInput").outputs[0]
        self.output = self.node("NodeGroupOutput")

    def node(self, idname: str, **props: object) -> bpy.types.Node:
        node = self.tree.nodes.new(idname)
        for key, value in props.items():
            setattr(node, key, value)
        return node

    def link(self, socket_out: bpy.types.NodeSocket, socket_in: bpy.types.NodeSocket) -> None:
        self.tree.links.new(socket_out, socket_in)

    def surface(self, obj: bpy.types.Object) -> bpy.types.NodeSocket:
        """The evaluated geometry of ``obj``, in its own object space."""
        info = self.node("GeometryNodeObjectInfo", transform_space="ORIGINAL")
        info.inputs["Object"].default_value = obj
        return info.outputs["Geometry"]

    def on_domain(self, socket: bpy.types.NodeSocket, domain: str, data_type: str):
        node = self.node("GeometryNodeFieldOnDomain", domain=domain, data_type=data_type)
        self.link(socket, node.inputs[0])
        return node.outputs[0]

    def value(self, attribute: str, nodata: float | None) -> bpy.types.NodeSocket:
        """The attribute as a float field, with ``nodata`` mapped to NaN."""
        named = self.node("GeometryNodeInputNamedAttribute", data_type="FLOAT")
        named.inputs["Name"].default_value = attribute
        value = named.outputs["Attribute"]
        if nodata is None or not math.isfinite(nodata):
            return value
        compare = self.node("FunctionNodeCompare", data_type="FLOAT", operation="EQUAL")
        self.link(value, compare.inputs["A"])
        compare.inputs["B"].default_value = float(np.float32(nodata))
        compare.inputs["Epsilon"].default_value = 0.0
        switch = self.node("GeometryNodeSwitch", input_type="FLOAT")
        self.link(compare.outputs[0], switch.inputs["Switch"])
        self.link(value, switch.inputs["False"])
        switch.inputs["True"].default_value = math.nan
        return switch.outputs[0]

    def store(
        self,
        geometry: bpy.types.NodeSocket,
        name: str,
        socket: bpy.types.NodeSocket,
        data_type: str,
        domain: str,
    ) -> bpy.types.NodeSocket:
        node = self.node("GeometryNodeStoreNamedAttribute", data_type=data_type, domain=domain)
        self.link(geometry, node.inputs["Geometry"])
        node.inputs["Name"].default_value = name
        self.link(socket, node.inputs["Value"])
        return node.outputs["Geometry"]

    def finish(self, geometry: bpy.types.NodeSocket) -> bpy.types.GeometryNodeTree:
        self.link(geometry, self.output.inputs[0])
        return self.tree


def sample_tree(
    obj: bpy.types.Object,
    attribute: str,
    domain: str,
    *,
    nodata: float | None,
    interpolate: bool,
    raycast: bool,
    max_distance: float,
    owner: list[bpy.types.NodeTree] | None = None,
) -> bpy.types.GeometryNodeTree:
    """NEAREST and RAYCAST: sample ``obj``'s evaluated surface on the input mesh.

    Stores, on ``domain`` (FACE or CORNER) of the input mesh, the nearest
    evaluated face (``_NEAREST``), the interpolated value at the nearest point
    (``_VALUE``, with ``interpolate``) and, with ``raycast``, per direction
    ``<ray>_hit``, ``<ray>_distance``, ``<ray>_face`` and ``<ray>_value``.
    """
    t = _Tree(owner)
    surface = t.surface(obj)
    position = t.node("GeometryNodeInputPosition").outputs[0]
    if domain == "CORNER":
        mix = t.node("ShaderNodeMix", data_type="VECTOR")
        _socket(mix.inputs, "Factor_Float").default_value = CORNER_INSET
        t.link(position, _socket(mix.inputs, "A_Vector"))
        t.link(t.on_domain(position, "FACE", "FLOAT_VECTOR"), _socket(mix.inputs, "B_Vector"))
        position = _socket(mix.outputs, "Result_Vector")
    geometry = t.input

    nearest = t.node("GeometryNodeSampleNearest", domain="FACE")
    t.link(surface, nearest.inputs["Geometry"])
    t.link(position, nearest.inputs["Sample Position"])
    geometry = t.store(geometry, _NEAREST, nearest.outputs["Index"], "INT", domain)

    value = t.value(attribute, nodata) if interpolate else None
    if value is not None:
        sampled = t.node("GeometryNodeSampleNearestSurface", data_type="FLOAT")
        t.link(surface, sampled.inputs["Mesh"])
        t.link(value, sampled.inputs["Value"])
        t.link(position, sampled.inputs["Sample Position"])
        geometry = t.store(geometry, _VALUE, sampled.outputs["Value"], "FLOAT", domain)

    if raycast:
        normal = t.on_domain(
            t.node("GeometryNodeInputNormal").outputs["Normal"], "FACE", "FLOAT_VECTOR"
        )
        flip = t.node("ShaderNodeVectorMath", operation="SCALE")
        flip.inputs["Scale"].default_value = -1.0
        t.link(normal, flip.inputs[0])
        face_index = t.on_domain(t.node("GeometryNodeInputIndex").outputs[0], "FACE", "INT")
        for ray, direction in zip(_RAY, (normal, flip.outputs["Vector"]), strict=True):
            hit = t.node("GeometryNodeRaycast", data_type="INT")
            t.link(surface, hit.inputs["Target Geometry"])
            t.link(face_index, hit.inputs["Attribute"])
            hit.inputs["Interpolation"].default_value = "Nearest"
            t.link(position, hit.inputs["Source Position"])
            t.link(direction, hit.inputs["Ray Direction"])
            hit.inputs["Ray Length"].default_value = max_distance
            geometry = t.store(geometry, f"{ray}_hit", hit.outputs["Is Hit"], "BOOLEAN", domain)
            geometry = t.store(
                geometry, f"{ray}_distance", hit.outputs["Hit Distance"], "FLOAT", domain
            )
            geometry = t.store(geometry, f"{ray}_face", hit.outputs["Attribute"], "INT", domain)
            if value is not None:
                cast = t.node("GeometryNodeRaycast", data_type="FLOAT")
                t.link(surface, cast.inputs["Target Geometry"])
                t.link(value, cast.inputs["Attribute"])
                t.link(position, cast.inputs["Source Position"])
                t.link(direction, cast.inputs["Ray Direction"])
                cast.inputs["Ray Length"].default_value = max_distance
                geometry = t.store(
                    geometry, f"{ray}_value", cast.outputs["Attribute"], "FLOAT", domain
                )
    return t.finish(geometry)


def owner_tree(
    obj: bpy.types.Object, owner: list[bpy.types.NodeTree] | None = None
) -> bpy.types.GeometryNodeTree:
    """AREA_MEAN: ``obj``'s evaluated mesh, each face storing the nearest input-mesh face."""
    t = _Tree(owner)
    surface = t.surface(obj)
    nearest = t.node("GeometryNodeSampleNearest", domain="FACE")
    t.link(t.input, nearest.inputs["Geometry"])
    t.link(t.node("GeometryNodeInputPosition").outputs[0], nearest.inputs["Sample Position"])
    return t.finish(t.store(surface, _OWNER, nearest.outputs["Index"], "INT", "FACE"))


# --- state ---------------------------------------------------------------------------------------


@contextmanager
def preserved_state(context: bpy.types.Context) -> Iterator[None]:
    """Object Mode for the body; the mode, selection and active object restored after."""
    view_layer = context.view_layer
    active = view_layer.objects.active
    mode = active.mode if active is not None else "OBJECT"
    selected = [o for o in view_layer.objects if o is not None and o.select_get()]
    if mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    try:
        yield
    finally:
        # The view layer can list removed temporary objects as None until it resyncs.
        for obj in view_layer.objects:
            if obj is not None and obj.select_get():
                obj.select_set(False)
        for obj in selected:
            if obj.name in view_layer.objects:
                obj.select_set(True)
        if active is None or active.name in view_layer.objects:
            view_layer.objects.active = active
        if mode != "OBJECT" and active is not None and context.view_layer.objects.active:
            try:
                bpy.ops.object.mode_set(mode=mode)
            except RuntimeError:
                log.warning("could not return %s to %s mode", active.name, mode, exc_info=True)


@contextmanager
def evaluated_through(obj: bpy.types.Object, modifier: bpy.types.Modifier) -> Iterator[None]:
    """Evaluate ``obj`` up to and including ``modifier``: it is on, later ones are off.

    Viewport and render visibility of every modifier are restored afterwards.
    """
    saved = [(m, m.show_viewport, m.show_render) for m in obj.modifiers]
    try:
        after = False
        for m in obj.modifiers:
            if m == modifier:
                m.show_viewport = True
                after = True
            elif after and m.show_viewport:
                m.show_viewport = False
        yield
    finally:
        for m, viewport, render in saved:
            if m.show_viewport != viewport:
                m.show_viewport = viewport
            if m.show_render != render:
                m.show_render = render


class _TempObjects:
    """Temporary objects and node groups, all removed by :meth:`clear`."""

    def __init__(self, scene: bpy.types.Scene) -> None:
        self.scene = scene
        self.objects: list[bpy.types.Object] = []
        self.trees: list[bpy.types.NodeTree] = []

    def evaluate(
        self,
        context: bpy.types.Context,
        mesh: bpy.types.Mesh,
        tree: bpy.types.GeometryNodeTree,
    ) -> tuple[bpy.types.Object, bpy.types.Mesh]:
        """A temporary object with ``mesh`` and ``tree`` as its modifier, evaluated.

        Call ``obj.to_mesh_clear()`` on the returned evaluated object when done.
        """
        if tree not in self.trees:
            self.trees.append(tree)
        obj = bpy.data.objects.new(TEMP_NAME, mesh)
        self.objects.append(obj)
        self.scene.collection.objects.link(obj)
        modifier = obj.modifiers.new(TEMP_NAME, "NODES")
        modifier.node_group = tree
        evaluated = obj.evaluated_get(context.evaluated_depsgraph_get())
        return evaluated, evaluated.to_mesh()

    def clear(self) -> None:
        for obj in self.objects:
            bpy.data.objects.remove(obj)
        for tree in self.trees:
            bpy.data.node_groups.remove(tree)
        self.objects.clear()
        self.trees.clear()


# --- the bake ------------------------------------------------------------------------------------


@dataclass
class ObjectBake:
    """The values computed for one object, written once every object succeeded."""

    obj: bpy.types.Object
    values: np.ndarray
    counts: TransferCounts
    same_topology: bool
    source_domain: str


@dataclass(frozen=True)
class BakeSettings:
    modifier: str
    attribute: str
    method: str  # NEAREST, RAYCAST or AREA_MEAN
    max_distance: float = 1.0


def nodes_modifier(obj: bpy.types.Object, name: str) -> bpy.types.Modifier | None:
    """``obj``'s Geometry Nodes modifier called ``name``, if any."""
    modifier = obj.modifiers.get(name)
    return modifier if modifier is not None and modifier.type == "NODES" else None


def bake_object(
    context: bpy.types.Context,
    obj: bpy.types.Object,
    spec: LayerSpec,
    settings: BakeSettings,
    temp: _TempObjects,
) -> ObjectBake:
    """Compute ``spec``'s values on ``obj`` (nothing is written). Raises ``BakeError``."""
    modifier = nodes_modifier(obj, settings.modifier)
    if modifier is None:
        raise BakeError(f"{obj.name} has no Geometry Nodes modifier {settings.modifier!r}")
    if modifier.node_group is None:
        raise BakeError(f"modifier {settings.modifier!r} of {obj.name} has no node group")
    domain = Domain(spec.domain)
    target = mesh_arrays(obj.data)
    counts = TransferCounts(unit="faces" if domain is Domain.FACE else "corners")
    kind = transfer.reduction_kind(spec)

    with evaluated_through(obj, modifier):
        evaluated_obj = obj.evaluated_get(context.evaluated_depsgraph_get())
        evaluated = evaluated_obj.to_mesh()
        try:
            values, valid, source_domain = read_attribute(
                evaluated, settings.attribute, spec.nodata
            )
            same = not mesh_topology_digest(obj.data, evaluated).startswith(EVALUATED_PREFIX)
            ev = mesh_arrays(evaluated) if len(evaluated.polygons) else None
            areas = _polygon_areas(evaluated) if settings.method == "AREA_MEAN" else None
        finally:
            evaluated_obj.to_mesh_clear()

        def done(out: np.ndarray, ok: np.ndarray) -> ObjectBake:
            values = transfer.to_layer_values(out, ok, spec)
            return ObjectBake(obj, values, counts, same, source_domain)

        if same:
            if domain is Domain.FACE:
                out, ok = transfer.to_faces(values, valid, source_domain, target, kind)
            else:
                out, ok = transfer.to_corners(values, valid, source_domain, target)
            counts.add("copied", np.ones(ok.shape, bool), ok)
            return done(out, ok)

        size = target.faces if domain is Domain.FACE else target.corners
        if ev is None:  # the evaluated mesh has no faces: nothing to transfer from
            ok = np.zeros(size, bool)
            counts.add("nearest", ~ok, ok)
            return done(np.zeros(size), ok)

        face_values, face_valid = transfer.to_faces(values, valid, source_domain, ev, kind)
        sampler = _Sampler(context, obj, spec, settings, temp, face_values, face_valid)

        if settings.method == "AREA_MEAN":
            evaluated_obj, result = temp.evaluate(context, obj.data, owner_tree(obj, temp.trees))
            try:
                if len(result.polygons) != ev.faces:
                    raise BakeError("the transfer evaluation changed the evaluated mesh")
                owner = _read(result, _OWNER, np.int32, ev.faces)
            finally:
                evaluated_obj.to_mesh_clear()
            out, ok, received = transfer.area_reduce(
                owner, face_values, face_valid, areas, target.faces, kind
            )
            if not received.all():
                fallback, fallback_ok, _hit = sampler.sample("FACE", target.faces, raycast=False)
                out = np.where(received, out, fallback)
                ok = np.where(received, ok, fallback_ok)
            if domain is Domain.CORNER:
                out, ok, received = (
                    transfer.face_to_corner(a, target.loop_total) for a in (out, ok, received)
                )
            counts.add("area_mean", received, ok)
            counts.add("fallback", ~received, ok)
            return done(out, ok)

        raycast = settings.method == "RAYCAST"
        out, ok, hit = sampler.sample(domain.value, size, raycast=raycast)
        if raycast:
            counts.add("raycast", hit, ok)
            counts.add("fallback", ~hit, ok)
        else:
            counts.add("nearest", np.ones(size, bool), ok)
        return done(out, ok)


@dataclass
class _Sampler:
    """NEAREST and RAYCAST samples of one object's evaluated surface on its original mesh."""

    context: bpy.types.Context
    obj: bpy.types.Object
    spec: LayerSpec
    settings: BakeSettings
    temp: _TempObjects
    face_values: np.ndarray  # per evaluated face, for the face found and mode layers
    face_valid: np.ndarray

    def sample(
        self, domain: str, size: int, *, raycast: bool
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``(values, valid, hit)`` on ``domain`` of the original mesh; ``hit`` is True
        where a ray hit (all False without ``raycast``)."""
        interpolate = transfer.reduction_kind(self.spec) == "mean"
        tree = sample_tree(
            self.obj,
            self.settings.attribute,
            domain,
            nodata=self.spec.nodata,
            interpolate=interpolate,
            raycast=raycast,
            max_distance=self.settings.max_distance,
            owner=self.temp.trees,
        )
        evaluated_obj, result = self.temp.evaluate(self.context, self.obj.data, tree)
        try:
            nearest = _read(result, _NEAREST, np.int32, size)
            value = _read(result, _VALUE, np.float32, size) if interpolate else None
            rays = [
                (
                    _read(result, f"{ray}_hit", np.bool_, size),
                    _read(result, f"{ray}_distance", np.float32, size),
                    _read(result, f"{ray}_face", np.int32, size),
                    _read(result, f"{ray}_value", np.float32, size) if interpolate else None,
                )
                for ray in (_RAY if raycast else ())
            ]
        finally:
            evaluated_obj.to_mesh_clear()
        out, ok = transfer.merge_interpolated(value, self.face_values, self.face_valid, nearest)
        hit = np.zeros(size, bool)
        if raycast:
            (hit_a, dist_a, face_a, value_a), (hit_b, dist_b, face_b, value_b) = rays
            choice = transfer.choose_nearer_hit(hit_a, dist_a, hit_b, dist_b)
            hit = choice >= 0
            face = np.where(hit, np.where(choice == 0, face_a, face_b), -1)
            ray_value = None if value_a is None else np.where(choice == 0, value_a, value_b)
            ray_out, ray_ok = transfer.merge_interpolated(
                ray_value, self.face_values, self.face_valid, face
            )
            out = np.where(hit, ray_out, out)
            ok = np.where(hit, ray_ok, ok)
        return out, ok, hit


@dataclass
class BakeResult:
    objects: list[ObjectBake]
    skipped: list[str]  # source objects without the modifier
    shared: list[str]  # objects whose mesh another baked object already holds
    counts: TransferCounts
    issues: list[Issue]

    def summary(self, layer: str) -> str:
        n = len(self.objects)
        text = f"Baked {layer} on {n} object{'s' * (n != 1)}: {self.counts.summary()}"
        if self.skipped:
            text += f"; {len(self.skipped)} without the modifier"
        if self.shared:
            text += f"; {len(self.shared)} sharing a baked mesh"
        return text


def bake_layer(
    context: bpy.types.Context,
    layer: str,
    settings: BakeSettings,
    *,
    domain: str = "LAYER",
    disable_modifier: bool = False,
) -> BakeResult:
    """Bake the Geometry Nodes attribute into layer ``layer`` on every source object
    that has the modifier. Raises ``BakeError`` (nothing written) or ``ValueError``.

    The layer becomes a GEONODES layer of ``domain`` (``LAYER`` keeps its own),
    with provenance and its value range updated.
    """
    scene = context.scene
    item = find_layer(scene, layer)
    if item is None:
        raise BakeError(f"there is no layer {layer!r}")
    spec = spec_from_props(item)
    if Domain(spec.domain) is Domain.TEXTURE and domain == "LAYER":
        raise BakeError(f"{layer} is a texture layer; use a texture bake or choose a domain")
    if settings.method not in {m[0] for m in METHOD_ITEMS}:
        raise BakeError(f"unknown transfer method {settings.method!r}")
    if not settings.attribute:
        raise BakeError("name the attribute to bake")
    target = Domain(spec.domain if domain == "LAYER" else domain)
    spec = dataclasses.replace(spec, domain=target)
    errors = [
        i for i in validate(spec, other_layer_names(scene, item)) if i.severity is Severity.ERROR
    ]
    if errors:
        raise BakeError(errors[0].message)

    objects = source_objects(scene)
    if not objects:
        raise BakeError("the source collection has no mesh objects")
    candidates = [o for o in objects if nodes_modifier(o, settings.modifier) is not None]
    skipped = [o.name for o in objects if nodes_modifier(o, settings.modifier) is None]
    if not candidates:
        raise BakeError(f"no source object has a Geometry Nodes modifier {settings.modifier!r}")
    linked = [o.name for o in candidates if o.data.library is not None]
    if linked:
        raise BakeError(f"the mesh of {linked[0]} is linked from a library; make it local first")

    seen: set[int] = set()
    shared: list[str] = []
    baked: list[ObjectBake] = []
    temp = _TempObjects(scene)
    with preserved_state(context):
        try:
            for obj in candidates:
                if obj.data.as_pointer() in seen:
                    shared.append(obj.name)
                    continue
                seen.add(obj.data.as_pointer())
                try:
                    baked.append(bake_object(context, obj, spec, settings, temp))
                except BakeError as ex:
                    raise BakeError(f"{obj.name}: {ex}") from None
                finally:
                    temp.clear()
        finally:
            temp.clear()

        # Everything is computed: now write. ensure_layer may move registry entries.
        modifier_names = sorted(
            {nodes_modifier(b.obj, settings.modifier).node_group.name for b in baked}
        )
        counts = TransferCounts(unit="faces" if target is Domain.FACE else "corners")
        for b in baked:
            counts.merge(b.counts)
        parameters: dict[str, object] = {
            "modifier": settings.modifier,
            "node_group": modifier_names[0] if len(modifier_names) == 1 else modifier_names,
            "attribute": settings.attribute,
            "method": settings.method,
            "domain": target.value,
            "source_domains": sorted({b.source_domain for b in baked}),
            "same_topology": sorted({b.same_topology for b in baked}),
            "objects": len(baked),
            "counts": counts.as_dict(),
            "modifier_disabled": bool(disable_modifier),
        }
        if settings.method == "RAYCAST":
            parameters["max_distance"] = settings.max_distance
        spec = dataclasses.replace(
            spec,
            origin=Origin.GEONODES,
            provenance=make_provenance(TOOL, parameters, extension_version()),
        )
        ensure_layer(scene, spec)
        issues: list[Issue] = []
        for b in baked:
            write_layer(b.obj.data, spec, b.values, issues=issues)
        item = find_layer(scene, layer)
        update_value_range(scene, item)
        if disable_modifier:
            for b in baked:
                modifier = nodes_modifier(b.obj, settings.modifier)
                modifier.show_viewport = False
                modifier.show_render = False
    return BakeResult(baked, skipped, shared, counts, issues)


# --- settings, operator and panel -----------------------------------------------------------


class GeoToolsVertexBake(bpy.types.PropertyGroup):
    """A layer's vertex-bake settings (``GeoToolsLayer.vertex_bake``), kept for re-baking."""

    modifier: StringProperty(
        name="Modifier", description="Geometry Nodes modifier on the source objects"
    )
    attribute: StringProperty(
        name="Attribute",
        description="Named attribute the modifier outputs (Store Named Attribute)",
    )
    method: EnumProperty(name="Transfer", items=METHOD_ITEMS, default="NEAREST")
    max_distance: FloatProperty(
        name="Max Distance",
        description="How far a ray may travel each way from the source face",
        default=1.0,
        min=0.0,
        subtype="DISTANCE",
    )
    disable_modifier: BoolProperty(
        name="Turn Modifier Off",
        description="After the bake, turn the modifier off in the viewport and render, so "
        "the export uses the baked values on the source topology",
        default=False,
    )
    last_report: StringProperty(name="Last Bake", description="What the last bake did")


def _active_layer(context: bpy.types.Context) -> GeoToolsLayer | None:
    props = context.scene.geotools if context.scene is not None else None
    if props is not None and 0 <= props.layers_index < len(props.layers):
        return props.layers[props.layers_index]
    return None


class GEOTOOLS_OT_bake_geonodes_layer(bpy.types.Operator):
    """Bake a Geometry Nodes attribute into a layer's gt_<name> attribute on the source
    meshes, transferring the values when the node tree changes the topology"""

    bl_idname = "geotools.bake_geonodes_layer"
    bl_label = "Bake Layer from Geometry Nodes"
    bl_options = {"REGISTER", "UNDO"}

    layer: StringProperty(name="Layer", description="Layer to bake; empty for the selected one")
    modifier: StringProperty(
        name="Modifier", description="Geometry Nodes modifier on the source objects"
    )
    attribute: StringProperty(name="Attribute", description="Named attribute the modifier outputs")
    domain: EnumProperty(name="Domain", items=DOMAIN_ITEMS, default="LAYER")
    method: EnumProperty(name="Transfer", items=METHOD_ITEMS, default="NEAREST")
    max_distance: FloatProperty(
        name="Max Distance",
        description="Raycast: how far a ray may travel each way from the source face",
        default=1.0,
        min=0.0,
        subtype="DISTANCE",
    )
    disable_modifier: BoolProperty(
        name="Turn Modifier Off",
        description="After the bake, turn the modifier off in the viewport and render",
        default=False,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        if context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        if not len(context.scene.geotools.layers):
            cls.poll_message_set("Add a layer first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        item = find_layer(scene, self.layer) if self.layer else _active_layer(context)
        if item is None:
            self.report(
                {"ERROR"}, f"No layer {self.layer!r}" if self.layer else "No layer selected"
            )
            return {"CANCELLED"}
        name = item.name
        settings = BakeSettings(
            self.modifier.strip(), self.attribute.strip(), self.method, self.max_distance
        )
        try:
            result = bake_layer(
                context,
                name,
                settings,
                domain=self.domain,
                disable_modifier=self.disable_modifier,
            )
        except (BakeError, ValueError, RuntimeError) as ex:
            self.report({"ERROR"}, f"Cannot bake {name}: {ex}")
            return {"CANCELLED"}
        summary = result.summary(name)
        stored = find_layer(scene, name).vertex_bake
        stored.modifier = settings.modifier
        stored.attribute = settings.attribute
        stored.method = settings.method
        stored.max_distance = settings.max_distance
        stored.disable_modifier = self.disable_modifier
        stored.last_report = summary
        log.info(summary)
        level = {"WARNING"} if result.counts.nodata or result.issues else {"INFO"}
        if result.issues:
            summary += f". {result.issues[0].message}"
        self.report(level, summary)
        redraw_ui(context)
        return {"FINISHED"}


def _modifier_owner(context: bpy.types.Context, name: str) -> bpy.types.Object | None:
    """A source object to search modifiers on: the active one if it is a source object."""
    objects = source_objects(context.scene)
    active = context.view_layer.objects.active if context.view_layer else None
    if active is not None and active in objects and active.modifiers:
        return active
    with_name = [o for o in objects if nodes_modifier(o, name) is not None]
    if with_name:
        return with_name[0]
    return next((o for o in objects if any(m.type == "NODES" for m in o.modifiers)), None)


class GEOTOOLS_PT_layers_bake_vertex(bpy.types.Panel):
    """Bake the selected layer from a Geometry Nodes modifier on the source objects."""

    bl_idname = "GEOTOOLS_PT_layers_bake_vertex"
    bl_label = "Bake from Geometry Nodes"
    bl_parent_id = "GEOTOOLS_PT_layers"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        item = _active_layer(context)
        return item is not None and item.domain != "TEXTURE"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = _active_layer(context)
        if item is None:
            return
        settings = item.vertex_bake
        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        owner = _modifier_owner(context, settings.modifier)
        if owner is not None:
            col.prop_search(settings, "modifier", owner, "modifiers", icon="GEOMETRY_NODES")
        else:
            col.prop(settings, "modifier", icon="GEOMETRY_NODES")
        col.prop(settings, "attribute")
        col.prop(settings, "method")
        if settings.method == "RAYCAST":
            col.prop(settings, "max_distance")
        col.prop(settings, "disable_modifier")

        objects = source_objects(context.scene)
        having = sum(1 for o in objects if nodes_modifier(o, settings.modifier) is not None)
        if settings.modifier and not having:
            layout.label(text=f"No source object has {settings.modifier}", icon="ERROR")
        elif settings.modifier:
            layout.label(text=f"On {having} of {len(objects)} source objects", icon="INFO")

        row = layout.row()
        row.enabled = bool(settings.modifier and settings.attribute and having)
        op = row.operator("geotools.bake_geonodes_layer", icon="NODETREE")
        op.layer = item.name
        op.modifier = settings.modifier
        op.attribute = settings.attribute
        op.method = settings.method
        op.max_distance = settings.max_distance
        op.disable_modifier = settings.disable_modifier
        if settings.last_report:
            box = layout.box()
            for line in _wrap(settings.last_report):
                box.label(text=line)


def _wrap(text: str, width: int = 44) -> list[str]:
    return textwrap.wrap(text, width) or [""]


classes = (
    GeoToolsVertexBake,
    GEOTOOLS_OT_bake_geonodes_layer,
    GEOTOOLS_PT_layers_bake_vertex,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    # Added to the registered layer group, so properties.py stays untouched.
    GeoToolsLayer.vertex_bake = PointerProperty(type=GeoToolsVertexBake)


def unregister() -> None:
    del GeoToolsLayer.vertex_bake
    _unregister_classes()
