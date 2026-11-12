"""Shared by hydrology and contours: surfaces as numpy arrays in, line and point objects out.

* :func:`read_surface` reads the evaluated meshes of a set of objects (the
  source collection, or the selected objects) into one world-space mesh of
  numpy arrays, with each object's place in it and whether face values can be
  written back to its original mesh.
* :func:`cast_vertical` casts vertical rays with a Geometry Nodes Raycast (a
  temporary target and query object), for hydrology's drops.
* :func:`write_lines` and :func:`write_points` make mesh objects of loose edges
  or vertices with attributes, in the ``GeoTools Analysis`` collection, which
  M8's Export Vectors writes as GeoPackage or GeoJSON lines and points with Z,
  and the vector publish tiles.

All reads and writes are ``foreach_get``/``foreach_set``; no loops over mesh
elements. See docs/design/m5-analysis.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import bpy
import numpy as np

from ..core.placement import PlacementMode
from .export import same_topology
from .layers import apply_modifiers, layer_meshes, source_objects
from .properties import placement_from_props

log = logging.getLogger(__name__)

OUTPUT_COLLECTION = "GeoTools Analysis"
OUTPUT_TOOL_PROP = "geotools_output"
TEMP_PREFIX = "GeoTools Raycast"

SURFACE_ITEMS = (
    ("SOURCE", "Source Collection", "The meshes of the project's source collection"),
    ("SELECTED", "Selected Objects", "The selected mesh objects"),
)


class SurfaceError(RuntimeError):
    """The surface cannot be read (nothing to read, an object in Edit Mode)."""


def _get(mesh: bpy.types.Mesh, name: str, field: str, dtype, size: int) -> np.ndarray:
    values = np.empty(size, dtype)
    if size:
        mesh.attributes[name].data.foreach_get(field, values)
    return values


def _set(mesh: bpy.types.Mesh, name: str, field: str, values: np.ndarray) -> None:
    mesh.attributes[name].data.foreach_set(field, np.ascontiguousarray(values).ravel())


@dataclass
class Part:
    """One object's slice of the combined surface."""

    obj: bpy.types.Object
    face_start: int
    face_count: int
    writable: bool


@dataclass
class SurfaceArrays:
    """Several evaluated meshes as one, in world space (binary64 positions)."""

    positions: np.ndarray
    face_starts: np.ndarray
    corner_verts: np.ndarray
    corner_edges: np.ndarray
    n_edges: int
    flip: np.ndarray
    triangles: np.ndarray | None
    parts: list[Part] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def n_faces(self) -> int:
        return len(self.face_starts) - 1


def surface_objects(context: bpy.types.Context, which: str, exclude=()) -> list[bpy.types.Object]:
    """The source collection's meshes, or the selected mesh objects (sorted by name)."""
    if which == "SELECTED":
        objects = [o for o in context.selected_objects if o.type == "MESH" and o not in exclude]
        return sorted(objects, key=lambda o: o.name)
    return [o for o in source_objects(context.scene) if o not in exclude]


def read_surface(
    context: bpy.types.Context,
    objects: list[bpy.types.Object],
    *,
    topology: bool = True,
    triangles: bool = False,
) -> SurfaceArrays:
    """The evaluated meshes of ``objects`` as one mesh in world space.

    ``topology`` reads faces with their corner vertices and edges (hydrology);
    ``triangles`` reads Blender's loop triangles (contours and sections). An
    object's values can be written back (``Part.writable``) when its evaluated
    mesh has its original topology and no earlier object shares its mesh.
    """
    if not objects:
        raise SurfaceError("There is no mesh to analyse.")
    for obj in objects:
        if obj.data.is_editmode:
            raise SurfaceError(f"{obj.name} is in Edit Mode; switch to Object Mode first.")
    scene = context.scene
    apply = apply_modifiers(scene)
    depsgraph = context.evaluated_depsgraph_get()
    positions, starts, corner_verts, corner_edges, flips, tris = [], [], [], [], [], []
    parts: list[Part] = []
    notes: list[str] = []
    seen: dict[int, str] = {}
    v_base = l_base = e_base = f_base = 0
    for obj, mesh in layer_meshes(scene, objects, apply=apply, depsgraph=depsgraph):
        n_v, n_f, n_l = len(mesh.vertices), len(mesh.polygons), len(mesh.loops)
        if not n_f:
            continue
        co = _get(mesh, "position", "vector", np.float32, 3 * n_v).reshape(-1, 3)
        matrix = np.array(obj.matrix_world, dtype=np.float64)
        positions.append(co.astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3])
        mirrored = np.linalg.det(matrix[:3, :3]) < 0
        if topology:
            loop_start = np.empty(n_f, np.int32)
            mesh.polygons.foreach_get("loop_start", loop_start)
            starts.append(loop_start.astype(np.int64) + l_base)
            corner_verts.append(_get(mesh, ".corner_vert", "value", np.int32, n_l) + v_base)
            corner_edges.append(_get(mesh, ".corner_edge", "value", np.int32, n_l) + e_base)
            flips.append(np.full(n_f, mirrored))
        if triangles:
            n_t = len(mesh.loop_triangles)
            t = np.empty(3 * n_t, np.int32)
            mesh.loop_triangles.foreach_get("vertices", t)
            tris.append(t.reshape(-1, 3).astype(np.int64) + v_base)
        pointer = obj.data.as_pointer()
        writable = True
        if pointer in seen:
            writable = False
            notes.append(
                f"{obj.name} shares its mesh with {seen[pointer]}; it has that object's values."
            )
        elif apply and not same_topology(obj.data, mesh):
            writable = False
            notes.append(
                f"{obj.name}: its modifiers change the topology, so it takes part but its "
                "face values cannot be stored on its mesh (apply the modifiers to keep them)."
            )
        seen.setdefault(pointer, obj.name)
        parts.append(Part(obj, f_base, n_f, writable))
        v_base += n_v
        l_base += n_l
        e_base += len(mesh.edges)
        f_base += n_f
    if not parts:
        raise SurfaceError("The meshes have no faces.")
    cat = np.concatenate
    return SurfaceArrays(
        positions=cat(positions),
        face_starts=np.append(cat(starts), l_base) if topology else np.zeros(1, np.int64),
        corner_verts=cat(corner_verts).astype(np.int64) if topology else np.zeros(0, np.int64),
        corner_edges=cat(corner_edges).astype(np.int64) if topology else np.zeros(0, np.int64),
        n_edges=e_base,
        flip=cat(flips) if topology else np.zeros(0, bool),
        triangles=cat(tris) if triangles else None,
        parts=parts,
        notes=notes,
    )


def datum(scene: bpy.types.Scene) -> tuple[float, str]:
    """The height added to a local z to give a real-world height, and what that height is.

    A SOURCE_CRS project's offset A (binary64), giving RL in the source's
    vertical datum; otherwise 0 and local heights.
    """
    placement = placement_from_props(scene.geotools)
    if PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS:
        return float(placement.source_offset[2]), "RL"
    return 0.0, "local z"


# --- vertical rays ---------------------------------------------------------------------------


def _ray_tree(target: bpy.types.Object) -> bpy.types.GeometryNodeTree:
    """Raycast down and up from each point, storing the hit face (``gt_face`` of the
    target, -1 for a miss) and the hit position."""
    tree = bpy.data.node_groups.new(f"{TEMP_PREFIX} Tree", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    info = nodes.new("GeometryNodeObjectInfo")
    info.inputs["Object"].default_value = target
    info.transform_space = "ORIGINAL"
    face = nodes.new("GeometryNodeInputNamedAttribute")
    face.data_type = "INT"
    face.inputs["Name"].default_value = "gt_face"
    position = nodes.new("GeometryNodeInputPosition")
    geometry = group_in.outputs[0]
    for name, direction in (("down", -1.0), ("up", 1.0)):
        ray = nodes.new("GeometryNodeRaycast")
        ray.data_type = "INT"
        ray.inputs["Interpolation"].default_value = "Nearest"
        links.new(info.outputs["Geometry"], ray.inputs["Target Geometry"])
        links.new(face.outputs["Attribute"], ray.inputs["Attribute"])
        links.new(position.outputs[0], ray.inputs["Source Position"])
        ray.inputs["Ray Direction"].default_value = (0.0, 0.0, direction)
        ray.inputs["Ray Length"].default_value = 1.0e7
        switch = nodes.new("GeometryNodeSwitch")
        switch.input_type = "INT"
        switch.inputs["False"].default_value = -1
        links.new(ray.outputs["Is Hit"], switch.inputs["Switch"])
        links.new(ray.outputs["Attribute"], switch.inputs["True"])
        for attr, socket, kind in (
            (f"gt_{name}_face", switch.outputs[0], "INT"),
            (f"gt_{name}_point", ray.outputs["Hit Position"], "FLOAT_VECTOR"),
        ):
            store = nodes.new("GeometryNodeStoreNamedAttribute")
            store.data_type = kind
            store.domain = "POINT"
            store.inputs["Name"].default_value = attr
            links.new(geometry, store.inputs["Geometry"])
            links.new(socket, store.inputs["Value"])
            geometry = store.outputs[0]
    links.new(geometry, group_out.inputs[0])
    return tree


def cast_vertical(
    context: bpy.types.Context,
    positions: np.ndarray,
    face_starts: np.ndarray,
    corner_verts: np.ndarray,
    faces: np.ndarray,
    origins: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Vertical rays from ``origins`` against the faces ``faces`` of the mesh.

    Returns ``(down_face, down_point, up_face, up_point)``: the first face hit
    straight below and above each origin (an index into the mesh's faces, -1
    for none) and where. Blender's Geometry Nodes Raycast casts them: the
    faces become a temporary target object (float32 world positions, with
    their face ids as ``gt_face``) and the origins a point object with a
    Raycast modifier, evaluated once.
    """
    q = len(origins)
    none = np.full(q, -1, dtype=np.int64)
    if q == 0 or not len(faces):
        return none, np.zeros((q, 3)), none.copy(), np.zeros((q, 3))
    sizes = np.diff(face_starts)[faces]
    corners = _ranges(face_starts[faces], sizes)
    used, local = np.unique(corner_verts[corners], return_inverse=True)
    temp_meshes: list[bpy.types.Mesh] = []
    temp_objects: list[bpy.types.Object] = []
    tree = None
    scene = context.scene
    try:
        target_mesh = bpy.data.meshes.new(f"{TEMP_PREFIX} Target")
        temp_meshes.append(target_mesh)
        target_mesh.vertices.add(len(used))
        target_mesh.loops.add(len(corners))
        target_mesh.polygons.add(len(faces))
        _set(target_mesh, "position", "vector", positions[used].astype(np.float32))
        _set(target_mesh, ".corner_vert", "value", local.astype(np.int32))
        loop_start = np.concatenate([[0], np.cumsum(sizes)[:-1]]).astype(np.int32)
        target_mesh.polygons.foreach_set("loop_start", loop_start)
        target_mesh.update(calc_edges=False)
        attr = target_mesh.attributes.new("gt_face", "INT", "FACE")
        attr.data.foreach_set("value", np.asarray(faces, dtype=np.int32))
        target = bpy.data.objects.new(target_mesh.name, target_mesh)
        temp_objects.append(target)

        query_mesh = bpy.data.meshes.new(f"{TEMP_PREFIX} Origins")
        temp_meshes.append(query_mesh)
        query_mesh.vertices.add(q)
        _set(query_mesh, "position", "vector", np.asarray(origins, dtype=np.float32))
        query_mesh.update()
        query = bpy.data.objects.new(query_mesh.name, query_mesh)
        temp_objects.append(query)
        for obj in temp_objects:
            scene.collection.objects.link(obj)
            obj.hide_render = True
        tree = _ray_tree(target)
        modifier = query.modifiers.new("Raycast", "NODES")
        modifier.node_group = tree
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        evaluated = query.evaluated_get(depsgraph).data
        out = []
        for name in ("down", "up"):
            hit = _get(evaluated, f"gt_{name}_face", "value", np.int32, q).astype(np.int64)
            point = _get(evaluated, f"gt_{name}_point", "vector", np.float32, 3 * q)
            out += [hit, point.reshape(-1, 3).astype(np.float64)]
        return tuple(out)  # type: ignore[return-value]
    finally:
        for obj in temp_objects:
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in temp_meshes:
            bpy.data.meshes.remove(mesh)
        if tree is not None:
            bpy.data.node_groups.remove(tree)


def _ranges(starts: np.ndarray, counts: np.ndarray) -> np.ndarray:
    total = int(counts.sum())
    first = np.cumsum(counts) - counts
    return np.repeat(np.asarray(starts) - first, counts) + np.arange(total)


# --- output objects --------------------------------------------------------------------------


def output_collection(scene: bpy.types.Scene) -> bpy.types.Collection:
    """The ``GeoTools Analysis`` collection under the scene's, created if missing."""
    collection = bpy.data.collections.get(OUTPUT_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(OUTPUT_COLLECTION)
    if collection.name not in scene.collection.children and not any(
        collection.name in c.children for c in scene.collection.children_recursive
    ):
        scene.collection.children.link(collection)
    return collection


def _output_object(
    scene: bpy.types.Scene, name: str, tool: str, mesh: bpy.types.Mesh
) -> bpy.types.Object:
    """An object named ``name`` showing ``mesh``: the one a previous run made (its
    mesh replaced, so its modifiers, materials and collections stay), else a new one."""
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.type == "MESH" and obj.get(OUTPUT_TOOL_PROP) == tool:
        old = obj.data
        obj.data = mesh
        if old.users == 0:
            bpy.data.meshes.remove(old)
        mesh.name = name
        return obj
    obj = bpy.data.objects.new(name, mesh)
    obj[OUTPUT_TOOL_PROP] = tool
    output_collection(scene).objects.link(obj)
    return obj


def _attribute(mesh: bpy.types.Mesh, name: str, domain: str, values: np.ndarray) -> None:
    values = np.asarray(values)
    if values.dtype == np.bool_:
        kind, field, data = "BOOLEAN", "value", values
    elif values.dtype.kind in "iu":
        kind, field, data = "INT", "value", values.astype(np.int32)
    else:
        kind, field, data = "FLOAT", "value", values.astype(np.float32)
    attr = mesh.attributes.new(name, kind, domain)
    attr.data.foreach_set(field, np.ascontiguousarray(data))


def write_lines(
    scene: bpy.types.Scene,
    name: str,
    tool: str,
    positions: np.ndarray,
    edges: np.ndarray,
    edge_attrs: dict[str, np.ndarray],
    point_attrs: dict[str, np.ndarray] | None = None,
) -> bpy.types.Object:
    """A mesh object of loose edges (world positions) with EDGE and POINT attributes.

    ``edges`` are the polyline steps in order, so Export Vectors joins each
    line's edges end to end; ``edge_attrs`` should include ``fid`` (one per line).
    """
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(positions))
    _set(mesh, "position", "vector", np.asarray(positions, dtype=np.float32))
    mesh.edges.add(len(edges))
    mesh.edges.foreach_set("vertices", np.ascontiguousarray(edges, dtype=np.int32).ravel())
    mesh.update()
    for attr, values in edge_attrs.items():
        _attribute(mesh, attr, "EDGE", values)
    for attr, values in (point_attrs or {}).items():
        _attribute(mesh, attr, "POINT", values)
    return _output_object(scene, name, tool, mesh)


def write_points(
    scene: bpy.types.Scene,
    name: str,
    tool: str,
    positions: np.ndarray,
    point_attrs: dict[str, np.ndarray],
) -> bpy.types.Object:
    """A mesh object of loose vertices (world positions) with POINT attributes."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(positions))
    _set(mesh, "position", "vector", np.asarray(positions, dtype=np.float32))
    mesh.update()
    for attr, values in point_attrs.items():
        _attribute(mesh, attr, "POINT", values)
    return _output_object(scene, name, tool, mesh)


def remove_output(name: str, tool: str) -> None:
    """Remove the object a previous run made, if it is still there and still ours."""
    obj = bpy.data.objects.get(name)
    if obj is not None and obj.get(OUTPUT_TOOL_PROP) == tool:
        mesh = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if mesh is not None and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
