"""The ray bake engine: Blender's Cycles ``Raycast`` shader nodes in one EMIT bake.

Shared by sun hours (:mod:`.sun_hours`), the horizon profile
(:mod:`.archaeo`) and the fast paths of the sky-view factor, openness and
cumulative viewshed presets (:mod:`.preset_bake`). See
docs/design/m5-analysis.md ("Visibility: Cycles Raycast nodes in a bake").

* **Samples.** FACE, CORNER and VERTEX values bake a temporary *sample
  object*: one tiny triangle per sample, each on its own texel of its own UV
  map (:data:`PROBE_UV`). The sample's exact position and normal are face
  attributes (:data:`POSITION_ATTRIBUTE`, :data:`NORMAL_ATTRIBUTE`) that the
  probe reads, so the triangle's own shape and place do not matter: it can sit
  at the sample point (sun hours) or be *parked* far below the scene
  (:func:`parking`), where no ray of the tool can reach it.
* **The probe** is a shader node group with inputs Position and Normal and
  output Color, wrapped in an emission material (:func:`probe_material`); an
  EMIT bake evaluates it once per texel with Cycles' own BVH. Many rays per
  sample are a chain of short groups (:class:`Probe`): building a node tree
  from Python, or setting a socket value in one, slows down with the tree's
  size, so no tree holds more than about :data:`BLOCK` nodes.
* **Pitfalls**, all found by measuring: Cycles' shader stack holds 255
  floats, so per-ray values must be used up within the ray (one running sum,
  not one chain per quantity); a Raycast node whose Position or Direction is
  unlinked uses the shading point or the normal whatever its default value,
  so both are always linked; a ray of length 0 is not traversed.

Nothing here loops over faces, vertices or rays in Python: samples are numpy
arrays, rays are shader nodes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import bpy
import numpy as np

from ..core.layers import Domain
from .bake_texture import _deselect_all, _run_bake, _Saved, _show
from .export import same_topology
from .layers import apply_modifiers, layer_meshes

PROBE_UV = "gt_probe"
POSITION_ATTRIBUTE = "gt_probe_position"
NORMAL_ATTRIBUTE = "gt_probe_normal"

RAYS_PER_BAKE = 1024
"""Rays per sample per bake at most (sun hours, viewsheds). Bake time is linear in
rays; this keeps the fixed cost of a bake (scene sync, BVH, shader compile) small
next to the rays."""
BLOCK = 32
"""Rays per block group: a probe is a chain of blocks, each a chain of rays."""
RAY_LENGTH = 1.0e7
"""How far an unbounded ray looks for an occluder, in scene units."""
CORNER_INSET = 0.1
"""A corner sample sits this fraction of the way from its corner to the face centre."""
VERTEX = "VERTEX"
"""Sample domain of one sample per vertex (not a layer domain: values go onto corners)."""


class RayBakeError(RuntimeError):
    """A ray bake cannot run; the message says why."""


# --- shader building blocks ---------------------------------------------------------------------


def new_socket(tree, name: str, kind: str, in_out: str = "INPUT"):
    """A ``NodeSocket<kind>`` on ``tree``'s interface."""
    return tree.interface.new_socket(name, in_out=in_out, socket_type=f"NodeSocket{kind}")


def math_node(tree, operation: str, a=None, b=None, c=None):
    """A Math node; each argument is a socket to link or a number to set."""
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    for index, value in enumerate((a, b, c)):
        _feed(tree, node.inputs[index], value)
    return node.outputs[0]


def vector_node(tree, operation: str, a=None, b=None, *, scale=None, out: str = "Vector"):
    """A Vector Math node; ``scale`` feeds the Scale input (SCALE)."""
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    _feed(tree, node.inputs[0], a)
    _feed(tree, node.inputs[1], b)
    if scale is not None:
        _feed(tree, node.inputs["Scale"], scale)
    return node.outputs[out]


def combine(tree, x=0.0, y=0.0, z=0.0):
    node = tree.nodes.new("ShaderNodeCombineXYZ")
    for index, value in enumerate((x, y, z)):
        _feed(tree, node.inputs[index], value)
    return node.outputs[0]


def separate(tree, vector):
    node = tree.nodes.new("ShaderNodeSeparateXYZ")
    _feed(tree, node.inputs[0], vector)
    return node.outputs[0], node.outputs[1], node.outputs[2]


def raycast(tree, position, direction, length):
    """A Raycast node with Position and Direction linked (never left to fall back)."""
    node = tree.nodes.new("ShaderNodeRaycast")
    tree.links.new(position, node.inputs["Position"])
    tree.links.new(direction, node.inputs["Direction"])
    _feed(tree, node.inputs["Length"], length)
    return node


def _feed(tree, socket, value) -> None:
    if value is None:
        return
    if isinstance(value, bpy.types.NodeSocket):
        tree.links.new(value, socket)
    else:
        socket.default_value = value


def sums_group(name: str, through: Sequence[str], sum_kind: str = "Vector"):
    """An empty tree with a chain's sockets: inputs Sum and ``through``; output Sum.

    ``through`` sockets are vectors; Sum is a ``sum_kind`` ("Vector" or "Float").
    """
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    new_socket(tree, "Sum", sum_kind)
    for socket in through:
        new_socket(tree, socket, "Vector")
    new_socket(tree, "Sum", sum_kind, "OUTPUT")
    return tree


def chain(tree, groups: list, through: Sequence[str]) -> list:
    """Chain one node of each of ``groups`` through Sum; returns the nodes.

    The ``through`` inputs go to every node.
    """
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    total = group_in.outputs["Sum"]
    chained = []
    for group in groups:
        node = nodes.new("ShaderNodeGroup")
        node.node_tree = group
        links.new(total, node.inputs["Sum"])
        for name in through:
            links.new(group_in.outputs[name], node.inputs[name])
        total = node.outputs["Sum"]
        chained.append(node)
    links.new(total, group_out.inputs["Sum"])
    return chained


Head = Callable[[object, object], dict]
"""Builds a probe's head: (tree, group input node) -> {through name: socket}."""


@dataclass
class Probe:
    """The probe shader groups of a run: blocks of BLOCK rays, and probes of n blocks.

    ``ray`` is a group with inputs Sum, the ``through`` inputs and its own
    per-ray inputs, and output Sum. ``head`` builds what the probe passes
    through the chain from the sample's Position and Normal. A vector sum
    starts at (0, 0, 1) and is the output; a float sum (``sum_kind``) starts
    at 0 and the output is (sum, 0, 1). Either way a baked texel has 1 in its
    third channel. Each tree is short: the rays sit in blocks of
    :data:`BLOCK`, the blocks in one chain per probe.
    """

    run: Run
    prefix: str
    ray: object
    through: tuple[str, ...]
    head: Head
    sum_kind: str = "Vector"
    blocks: list = field(default_factory=list)  # full blocks: (tree, its ray nodes)
    tails: dict = field(default_factory=dict)  # rays -> a short last block (tree, nodes)
    probes: dict = field(default_factory=dict)  # (full blocks, tail rays) -> probe tree

    def _new_block(self, name: str, rays: int):
        tree = sums_group(f"{self.prefix} {name}", self.through, self.sum_kind)
        self.run.groups.append(tree)
        return tree, chain(tree, [self.ray] * rays, self.through)

    def _block(self, index: int):
        while len(self.blocks) <= index:
            self.blocks.append(self._new_block(f"Block {len(self.blocks)}", BLOCK))
        return self.blocks[index]

    def _tail(self, rays: int):
        if rays not in self.tails:
            self.tails[rays] = self._new_block(f"Tail {rays}", rays)
        return self.tails[rays]

    def load(
        self, values: Sequence[Mapping[str, object]], blank: Mapping[str, object] | None = None
    ):
        """The probe for these rays, each a {per-ray input: value}.

        With ``blank``, every block is full and spare slots get ``blank``
        (blocks are reused across batches of any size); without, the last
        block holds exactly the rays left, so there are no spare slots.
        """
        if blank is None:
            full, tail = divmod(len(values), BLOCK)
        else:
            full, tail = max(1, math.ceil(len(values) / BLOCK)), 0
        blocks = [self._block(b) for b in range(full)]
        if tail:
            blocks.append(self._tail(tail))
        for b, (_tree, ray_nodes) in enumerate(blocks):
            for i, node in enumerate(ray_nodes):
                k = b * BLOCK + i
                inputs = node.inputs
                for name, value in (values[k] if k < len(values) else blank).items():
                    inputs[name].default_value = value
        key = (full, tail)
        if key not in self.probes:
            self.probes[key] = self._probe(key, [tree for tree, _nodes in blocks])
        return self.probes[key]

    def _probe(self, key: tuple[int, int], blocks: list):
        """Inputs Position and Normal; output Color, from the chain of ``blocks``."""
        label = f"{key[0]}" + (f"+{key[1]}" if key[1] else "")
        sums = sums_group(f"{self.prefix} Chain {label}", self.through, self.sum_kind)
        self.run.groups.append(sums)
        chain(sums, blocks, self.through)
        tree = bpy.data.node_groups.new(f"{self.prefix} Probe {label}", "ShaderNodeTree")
        self.run.groups.append(tree)
        new_socket(tree, "Position", "Vector")
        new_socket(tree, "Normal", "Vector")
        new_socket(tree, "Color", "Color", "OUTPUT")
        nodes, links = tree.nodes, tree.links
        group_in = nodes.new("NodeGroupInput")
        group_out = nodes.new("NodeGroupOutput")
        sockets = self.head(tree, group_in)
        rays = nodes.new("ShaderNodeGroup")
        rays.node_tree = sums
        for name in self.through:
            links.new(sockets[name], rays.inputs[name])
        if self.sum_kind == "Vector":
            links.new(combine(tree, 0.0, 0.0, 1.0), rays.inputs["Sum"])
            links.new(rays.outputs["Sum"], group_out.inputs["Color"])
        else:
            rays.inputs["Sum"].default_value = 0.0
            links.new(combine(tree, rays.outputs["Sum"], 0.0, 1.0), group_out.inputs["Color"])
        return tree


def probe_material(name: str, group, image: bpy.types.Image, *, attributes: bool):
    """Emission of the probe group, with ``image`` as the active (bake) node.

    ``attributes``: the sample position and normal come from the sample
    object's face attributes; otherwise the shading position and true normal.
    """
    material = bpy.data.materials.new(name)
    nodes, links = material.node_tree.nodes, material.node_tree.links
    nodes.clear()
    probe = nodes.new("ShaderNodeGroup")
    probe.node_tree = group
    if attributes:
        for socket, attribute in (("Position", POSITION_ATTRIBUTE), ("Normal", NORMAL_ATTRIBUTE)):
            node = nodes.new("ShaderNodeAttribute")
            node.attribute_type = "GEOMETRY"
            node.attribute_name = attribute
            links.new(node.outputs["Vector"], probe.inputs[socket])
    else:
        geometry = nodes.new("ShaderNodeNewGeometry")
        links.new(geometry.outputs["Position"], probe.inputs["Position"])
        links.new(geometry.outputs["True Normal"], probe.inputs["Normal"])
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Strength"].default_value = 1.0
    links.new(probe.outputs["Color"], emission.inputs["Color"])
    output = nodes.new("ShaderNodeOutputMaterial")
    links.new(emission.outputs[0], output.inputs["Surface"])
    texture = nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.interpolation = "Closest"
    nodes.active = texture
    return material


def float_image(name: str, width: int, height: int) -> bpy.types.Image:
    image = bpy.data.images.new(name, width, height, alpha=False, float_buffer=True, is_data=True)
    image.colorspace_settings.name = "Non-Color"
    return image


def pixels(image: bpy.types.Image) -> np.ndarray:
    """The image's pixels as a (texels, 4) float32 array."""
    values = np.empty(len(image.pixels), np.float32)
    image.pixels.foreach_get(values)
    return values.reshape(-1, 4)


# --- samples for FACE, CORNER and VERTEX values ---------------------------------------------------


@dataclass
class Part:
    obj: bpy.types.Object
    start: int
    count: int


@dataclass
class Samples:
    parts: list[Part] = field(default_factory=list)
    points: list[np.ndarray] = field(default_factory=list)
    normals: list[np.ndarray] = field(default_factory=list)
    corner_vertex: list[np.ndarray] = field(default_factory=list)  # VERTEX: per part
    skipped: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(p.count for p in self.parts)


def world_frame(matrix: np.ndarray, points: np.ndarray, normals: np.ndarray):
    """Points and unit normals in world space (normals by the inverse transpose)."""
    linear = matrix[:3, :3]
    world = points @ linear.T + matrix[:3, 3]
    turned = normals @ np.linalg.inv(linear)
    length = np.linalg.norm(turned, axis=1, keepdims=True)
    turned = np.divide(turned, length, out=np.zeros_like(turned), where=length > 0)
    return world, turned


def set_attribute(mesh: bpy.types.Mesh, name: str, field: str, values: np.ndarray) -> None:
    mesh.attributes[name].data.foreach_set(field, np.ascontiguousarray(values).ravel())


def get_attribute(mesh: bpy.types.Mesh, name: str, field: str, dtype, size: int) -> np.ndarray:
    values = np.empty(size, dtype)
    mesh.attributes[name].data.foreach_get(field, values)
    return values


def corner_vertices(mesh: bpy.types.Mesh) -> np.ndarray:
    return get_attribute(mesh, ".corner_vert", "value", np.int32, len(mesh.loops))


def mesh_samples(mesh: bpy.types.Mesh, domain: Domain | str) -> tuple[np.ndarray, np.ndarray]:
    """Local sample points and normals of a mesh, float64.

    FACE: one per face, at its centre (the mean of its corners, as Blender's
    ``polygon.center`` and Geometry Nodes' face Position), with the face
    normal. CORNER: one per corner, :data:`CORNER_INSET` of the way to the
    face centre, with the face normal. :data:`VERTEX`: one per vertex, with
    the vertex normal (Geometry Nodes' Normal on points).
    """
    co = get_attribute(mesh, "position", "vector", np.float32, len(mesh.vertices) * 3)
    co = co.reshape(-1, 3).astype(np.float64)
    if domain == VERTEX:
        normals = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertex_normals.foreach_get("vector", normals)
        return co, normals.reshape(-1, 3).astype(np.float64)
    faces = len(mesh.polygons)
    starts = np.empty(faces, np.int32)
    mesh.polygons.foreach_get("loop_start", starts)
    sizes = np.diff(np.append(starts, len(mesh.loops)))
    corners = co[corner_vertices(mesh)]
    centres = np.add.reduceat(corners, starts, axis=0) / sizes[:, None]
    normals = np.empty(faces * 3, np.float32)
    mesh.polygon_normals.foreach_get("vector", normals)
    normals = normals.reshape(-1, 3).astype(np.float64)
    if domain is Domain.FACE:
        return centres, normals
    corner_face = np.repeat(np.arange(faces), sizes)
    points = corners + CORNER_INSET * (centres[corner_face] - corners)
    return points, normals[corner_face]


def collect_samples(
    scene: bpy.types.Scene, objects: Sequence[bpy.types.Object], domain: Domain | str, depsgraph
) -> Samples:
    """World-space sample points and normals of ``objects`` that can take the values.

    Values are written to the original mesh, so an object is skipped (with a
    note) when its evaluated mesh has another topology, or when an earlier
    object shares its mesh. For :data:`VERTEX` each part also records its
    corners' vertices, to spread vertex values onto corners.
    """
    samples = Samples()
    for obj in objects:
        if obj.data.is_editmode:
            raise RayBakeError(f"{obj.name} is in Edit Mode; switch to Object Mode first.")
    apply = apply_modifiers(scene)
    seen: dict[int, str] = {}
    start = 0
    for obj, mesh in layer_meshes(scene, list(objects), depsgraph=depsgraph):
        pointer = obj.data.as_pointer()
        if pointer in seen:
            samples.skipped.append(
                f"{obj.name} shares its mesh with {seen[pointer]}; it has that object's values."
            )
            continue
        seen[pointer] = obj.name
        if apply and not same_topology(obj.data, mesh):
            samples.skipped.append(
                f"{obj.name}: its modifiers change the topology, so face values cannot be "
                "stored on its mesh. Use a texture layer, or apply the modifiers."
            )
            continue
        points, normals = mesh_samples(mesh, domain)
        if not len(points):
            continue
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        world, turned = world_frame(matrix, points, normals)
        samples.parts.append(Part(obj, start, len(points)))
        samples.points.append(world)
        samples.normals.append(turned)
        if domain == VERTEX:
            samples.corner_vertex.append(corner_vertices(mesh))
        start += len(points)
    return samples


def texel_layout(count: int) -> tuple[int, int]:
    width = max(1, math.ceil(math.sqrt(count)))
    return width, max(1, math.ceil(count / width))


def parking(low: np.ndarray, count: int, depth: float) -> np.ndarray:
    """Where parked sample triangles sit: a grid one unit apart, ``depth`` below ``low``.

    They must be in the scene for Cycles to bake them, so they sit far below
    whatever the rays can reach, close together.
    """
    width = max(1, math.ceil(math.sqrt(count)))
    index = np.arange(count)
    grid = np.stack(
        [index % width - width / 2.0, index // width - width / 2.0, np.zeros(count)], axis=1
    )
    return np.asarray(low, np.float64)[None, :] + grid + np.array([0.0, 0.0, -depth])


def build_sample_object(
    saved: _Saved,
    context: bpy.types.Context,
    points: np.ndarray,
    normals: np.ndarray,
    *,
    name: str = "GeoTools Ray Bake Samples",
    at: np.ndarray | None = None,
) -> tuple[bpy.types.Object, int, int]:
    """The temporary sample object: one tiny triangle per sample, each on its own texel.

    Returns the object and its texel grid (width, height): sample ``i`` is
    texel ``(i % width, i // width)``. The triangle's UVs surround that texel's
    centre (and no other); its 3D shape is a few float32 steps across around
    ``at`` (default: the sample's point), and the exact point and normal are
    face attributes. Cycles only bakes an object some ray type sees, so it is
    visible to camera rays, and the Raycast node sees it too: at the sample
    point, rays start 16 float32 steps off the surface, away from their own
    triangle, and another sample's is hit only by a ray passing within a few
    float32 steps of that sample's point; parked (``at``), no ray reaches it.
    """
    n = len(points)
    width, height = texel_layout(n)
    p32 = points.astype(np.float32)
    where = p32 if at is None else np.asarray(at).astype(np.float32)
    size = 4.0 * np.spacing(np.maximum(np.abs(where).max(axis=1), np.float32(1.0)))
    offsets = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [-1.0, -1.0, 0.0]])
    corners = (where[:, None, :] + size[:, None, None] * offsets[None, :, :]).astype(np.float32)

    mesh = bpy.data.meshes.new(name)
    saved.temp_meshes.append(mesh)
    mesh.vertices.add(3 * n)
    mesh.loops.add(3 * n)
    mesh.polygons.add(n)
    # The attribute arrays take numpy buffers directly: far faster than
    # vertices.co and loops.vertex_index, which go element by element.
    set_attribute(mesh, "position", "vector", corners)
    set_attribute(mesh, ".corner_vert", "value", np.arange(3 * n, dtype=np.int32))
    mesh.polygons.foreach_set("loop_start", np.arange(0, 3 * n, 3, dtype=np.int32))
    mesh.update(calc_edges=False)
    for attribute, values in ((POSITION_ATTRIBUTE, p32), (NORMAL_ATTRIBUTE, normals)):
        attr = mesh.attributes.new(attribute, "FLOAT_VECTOR", "FACE")
        attr.data.foreach_set("vector", np.ascontiguousarray(values, np.float32).ravel())

    index = np.arange(n)
    centre = np.stack([(index % width + 0.5) / width, (index // width + 0.5) / height], axis=1)
    angles = np.radians([90.0, 210.0, 330.0])
    # 0.9 texel from the centre: the triangle holds its own texel centre (and the
    # jitter around it), never a neighbour's, which is a full texel away.
    spread = 0.9 * np.stack([np.cos(angles) / width, np.sin(angles) / height], axis=1)
    uv = centre[:, None, :] + spread[None, :, :]
    mesh.uv_layers.new(name=PROBE_UV)
    set_attribute(mesh, PROBE_UV, "vector", uv.astype(np.float32))

    obj = bpy.data.objects.new(mesh.name, mesh)
    saved.temp_objects.append(obj)
    # Cycles bakes only an object some ray type can see, and the Raycast node
    # then sees it too (see above).
    for flag in (
        "visible_diffuse",
        "visible_glossy",
        "visible_transmission",
        "visible_volume_scatter",
        "visible_shadow",
    ):
        setattr(obj, flag, False)
    context.scene.collection.objects.link(obj)
    return obj, width, height


# --- running -------------------------------------------------------------------------------------


def setup_cycles(saved: _Saved, scene: bpy.types.Scene) -> None:
    """CPU, one sample, no denoising, EMIT: what every ray bake uses."""
    scene.render.engine = "CYCLES"
    saved.capture_cycles(scene)
    cycles = scene.cycles
    cycles.device = "CPU"
    cycles.samples = 1
    cycles.use_adaptive_sampling = False
    cycles.use_denoising = False
    if hasattr(cycles, "time_limit"):
        cycles.time_limit = 0.0
    cycles.bake_type = "EMIT"


@dataclass
class Run:
    """Temporary data of one run, removed afterwards (with ``_Saved``'s)."""

    groups: list = field(default_factory=list)
    images: list = field(default_factory=list)

    def clean(self) -> None:
        for image in self.images:
            bpy.data.images.remove(image)
        for group in self.groups:
            if group.name in bpy.data.node_groups:
                bpy.data.node_groups.remove(group)
        self.images.clear()
        self.groups.clear()


def show(saved: _Saved, objects: Sequence[bpy.types.Object]) -> None:
    """Make ``objects`` visible and renderable for the bake (restored afterwards)."""
    for obj in objects:
        _show(saved, obj)


def render_only(
    saved: _Saved, context: bpy.types.Context, keep: Sequence[bpy.types.Object]
) -> list[str]:
    """Render only ``keep`` (shown if hidden): every other object is hidden from renders.

    Cycles' Raycast node meets whatever the render holds, so this sets the
    occluders. Restored afterwards with the rest of ``saved``. Returns the
    names of ``keep`` objects outside the view layer (which cannot render).
    """
    kept = {obj.as_pointer() for obj in keep}
    for obj in context.view_layer.objects:
        if obj.as_pointer() in kept or obj.hide_render:
            continue
        saved.visibility.append((obj, obj.hide_get(), obj.hide_viewport, obj.hide_render))
        obj.hide_render = True
    inside = {obj.as_pointer() for obj in context.view_layer.objects}
    show(saved, [obj for obj in keep if obj.as_pointer() in inside])
    return [obj.name for obj in keep if obj.as_pointer() not in inside]


def bake(
    context: bpy.types.Context,
    objects: Sequence[bpy.types.Object],
    uv: str,
    *,
    margin: int = 0,
    error: type[Exception] = RayBakeError,
) -> None:
    """EMIT-bake ``objects`` (selected, the first active) on the UV map ``uv``."""
    view_layer = context.view_layer
    _deselect_all(view_layer)
    for obj in objects:
        obj.select_set(True)
    view_layer.objects.active = objects[0]
    try:
        _run_bake(
            type="EMIT",
            target="IMAGE_TEXTURES",
            margin=margin,
            margin_type="EXTEND",
            use_clear=True,
            use_selected_to_active=False,
            use_cage=False,
            use_split_materials=False,
            uv_layer=uv,
        )
    except RuntimeError as ex:
        raise error(f"The bake failed: {ex}") from None
