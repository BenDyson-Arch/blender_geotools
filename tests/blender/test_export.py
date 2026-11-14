"""Source extraction for publishing (``blender.export.extract_scene``)."""

from __future__ import annotations

import math
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _fixtures
import _harness as h
import bmesh
import bpy
import numpy as np

# --- helpers -----------------------------------------------------------------------


def _export():
    return h.submodule("blender.export")


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    return collection


def _object(
    name: str,
    verts: list[tuple[float, float, float]],
    faces: list[tuple[int, ...]],
    collection: bpy.types.Collection,
    uvs: dict[str, np.ndarray] | None = None,
) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    for uv_name, values in (uvs or {}).items():
        layer = mesh.uv_layers.new(name=uv_name)
        layer.uv.foreach_set("vector", np.asarray(values, dtype=np.float32).ravel())
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _corner_uvs(obj: bpy.types.Object, per_vertex: np.ndarray) -> np.ndarray:
    """Per-corner UVs that follow the vertices (no seams)."""
    loops = np.empty(len(obj.data.loops), dtype=np.int32)
    obj.data.loops.foreach_get("vertex_index", loops)
    return np.asarray(per_vertex, dtype=np.float32)[loops]


STRIP_VERTS = [(0, 0, 0), (1, 0, 0), (2, 0, 0), (0, 1, 0), (1, 1, 0), (2, 1, 0)]
STRIP_FACES = [(0, 1, 4, 3), (1, 2, 5, 4)]


@contextmanager
def _extracted(collection: bpy.types.Collection) -> Iterator[tuple[object, dict, Path]]:
    tmp = Path(tempfile.mkdtemp(prefix="geotools-export-"))
    try:
        bpy.context.view_layer.update()
        scene, topology = _export().extract_scene(bpy.context, collection, tmp / "images")
        yield scene, topology, tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _material(name: str) -> tuple[bpy.types.Material, bpy.types.NodeTree, bpy.types.Node]:
    material = bpy.data.materials.new(name)
    if material.node_tree is None:
        material.use_nodes = True
    tree = material.node_tree
    principled = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    return material, tree, principled


def _image_node(tree: bpy.types.NodeTree, image: bpy.types.Image) -> bpy.types.Node:
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    return node


def _generated(name: str, rgba=(0.2, 0.4, 0.6, 1.0), size: int = 4, **kwargs) -> bpy.types.Image:
    image = bpy.data.images.new(name, size, size, **kwargs)
    image.pixels.foreach_set(np.tile(np.array(rgba, dtype=np.float32), size * size))
    return image


def _corner_positions(prim) -> np.ndarray:
    return prim.positions[prim.indices.astype(np.int64)]


def _assert_scenes_equal(a, b) -> None:
    assert a.object_names == b.object_names
    assert a.materials == b.materials
    assert [(i.name, i.mime_type) for i in a.images] == [(i.name, i.mime_type) for i in b.images]
    for ia, ib in zip(a.images, b.images, strict=True):
        assert ia.path.read_bytes() == ib.path.read_bytes(), (ia, ib)
    assert len(a.primitives) == len(b.primitives)
    for pa, pb in zip(a.primitives, b.primitives, strict=True):
        assert pa.material == pb.material
        for name in ("positions", "indices", "normals", "tangents", "color"):
            x, y = getattr(pa, name), getattr(pb, name)
            assert (x is None) == (y is None), name
            if x is not None:
                assert x.dtype == y.dtype and np.array_equal(x, y), name
        assert len(pa.texcoords) == len(pb.texcoords)
        for x, y in zip(pa.texcoords, pb.texcoords, strict=True):
            assert np.array_equal(x, y)
        assert np.array_equal(pa.source_object, pb.source_object)
        assert np.array_equal(pa.source_polygon, pb.source_polygon)


def _writer_ready() -> bool:
    try:
        source = Path(h.submodule("core.gltf_writer").__file__).read_text(encoding="utf-8")
    except OSError:
        return False
    return "raise NotImplementedError" not in source


# --- objects and determinism -----------------------------------------------------------


def test_objects_sorted_recursively_and_deterministic() -> None:
    h.reset_scene()
    source = _source()
    child = bpy.data.collections.new("Child")
    source.children.link(child)
    _object("b", STRIP_VERTS, STRIP_FACES, source)
    _object("a", STRIP_VERTS, STRIP_FACES, child)
    _object("c", STRIP_VERTS, STRIP_FACES, child)
    source.objects.link(bpy.data.objects.new("empty", None))  # not a mesh

    with _extracted(source) as (first, topology, tmp):
        assert first.object_names == ("a", "b", "c"), first.object_names
        assert sorted(topology) == ["a", "b", "c"]
        assert first.generator == "GeoTools"
        with _extracted(source) as (second, topology2, _tmp):
            _assert_scenes_equal(first, second)
            assert topology == topology2
            if _writer_ready():
                write_glb = h.submodule("core.gltf_writer").write_glb
                one = write_glb(first, tmp / "one.glb")
                two = write_glb(second, tmp / "two.glb")
                assert one.sha256 == two.sha256
                assert (tmp / "one.glb").read_bytes() == (tmp / "two.glb").read_bytes()
        prim = first.primitives[0]
        assert prim.source_object.dtype == np.int32 and prim.source_polygon.dtype == np.int32
        assert prim.indices.dtype == np.uint32 and prim.positions.dtype == np.float32
        assert prim.source_object.tolist() == [0] * 4 + [1] * 4 + [2] * 4
        assert prim.source_polygon.tolist() == [0, 0, 1, 1] * 3


# --- transforms ----------------------------------------------------------------------


def _triangle_object(name: str, collection, location, rotation, scale) -> bpy.types.Object:
    obj = _object(name, [(0, 0, 0), (1, 0, 0), (0, 1, 0)], [(0, 1, 2)], collection)
    obj.location, obj.rotation_euler, obj.scale = location, rotation, scale
    return obj


def test_world_transform_and_winding() -> None:
    h.reset_scene()
    source = _source()
    cases = {
        "plain": ((10.0, 20.0, 30.0), (0.3, 0.5, 0.7), (2.0, 3.0, 0.5)),
        "mirror_x": ((1.0, 2.0, 3.0), (0.0, 0.0, 0.4), (-1.0, 2.0, 1.0)),
        "mirror_xyz": ((-5.0, 0.0, 7.0), (0.2, 0.0, 0.0), (-1.0, -2.0, -0.5)),
    }
    for name, (loc, rot, scale) in cases.items():
        _triangle_object(name, source, loc, rot, scale)
    bpy.context.view_layer.update()

    with _extracted(source) as (scene, _topology, _tmp):
        assert len(scene.primitives) == 1
        prim = scene.primitives[0]
        for index, name in enumerate(scene.object_names):
            obj = bpy.data.objects[name]
            matrix = np.array(obj.matrix_world, dtype=np.float64)
            mirrored = np.linalg.det(matrix[:3, :3]) < 0
            assert mirrored == name.startswith("mirror"), name
            rows = np.flatnonzero(prim.source_object == index)
            assert rows.size == 1
            tri = prim.indices[rows[0]].astype(np.int64)
            local = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0)], dtype=np.float64)
            world = (local @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32)
            # Mirrored triangles are written as (0, 2, 1) so they keep facing their normal.
            expected = world[[0, 2, 1]] if mirrored else world
            assert np.allclose(prim.positions[tri], expected, rtol=0, atol=1e-5), name

            normal = np.linalg.inv(matrix[:3, :3]).T @ np.array([0.0, 0.0, 1.0])
            normal /= np.linalg.norm(normal)
            assert np.allclose(prim.normals[tri], normal, atol=1e-5), name
            p = prim.positions[tri].astype(np.float64)
            geometric = np.cross(p[1] - p[0], p[2] - p[0])
            geometric /= np.linalg.norm(geometric)
            assert float(geometric @ normal) > 0.999, (name, geometric, normal)


# --- modifiers and topology ------------------------------------------------------------


def test_modifiers_are_applied() -> None:
    h.reset_scene()
    source = _source()
    arrayed = _object("arrayed", STRIP_VERTS, STRIP_FACES, source)
    modifier = arrayed.modifiers.new("Array", "ARRAY")
    modifier.count = 3
    displaced = _object("displaced", STRIP_VERTS, STRIP_FACES, source)
    displace = displaced.modifiers.new("Displace", "DISPLACE")
    displace.strength = 2.0
    digest = h.submodule("core.facemap").topology_digest

    with _extracted(source) as (scene, topology, _tmp):
        prim = scene.primitives[0]
        arrayed_rows = prim.source_object == scene.object_names.index("arrayed")
        assert int(arrayed_rows.sum()) == 12  # 2 quads x 3 copies x 2 triangles
        assert sorted(set(prim.source_polygon[arrayed_rows].tolist())) == list(range(6))
        assert prim.positions[:, 0].max() >= 5.99  # the third copy

        # Array changes the topology: indices refer to evaluated polygons.
        assert topology["arrayed"].startswith("evaluated:"), topology
        # Displace keeps it: the plain digest of the ORIGINAL mesh.
        mesh = displaced.data
        loops = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loops)
        totals = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("loop_total", totals)
        assert topology["displaced"] == digest(totals, loops, len(mesh.vertices))
        displaced_rows = prim.source_object == scene.object_names.index("displaced")
        z = _corner_positions(prim)[displaced_rows][..., 2]
        assert np.allclose(z, 1.0), z  # (1 - midlevel 0.5) x strength 2 along +Z


def test_topology_digest_tracks_topology_not_positions() -> None:
    h.reset_scene()
    source = _source()
    obj = _object("mesh", STRIP_VERTS, STRIP_FACES, source)
    with _extracted(source) as (_scene, topology, _tmp):
        before = topology["mesh"]
    assert not before.startswith("evaluated:")

    obj.data.vertices[4].co.z = 3.0
    obj.data.update()
    with _extracted(source) as (_scene, topology, _tmp):
        assert topology["mesh"] == before

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[1]], context="FACES_ONLY")
    bm.to_mesh(obj.data)
    bm.free()
    with _extracted(source) as (_scene, topology, _tmp):
        assert topology["mesh"] != before

    # Same polygon count, different corners: still a topology change.
    obj2 = _object("rewired", STRIP_VERTS, STRIP_FACES, source)
    with _extracted(source) as (_scene, topology, _tmp):
        rewired = topology["rewired"]
    bm = bmesh.new()
    bm.from_mesh(obj2.data)
    bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
    bm.to_mesh(obj2.data)
    bm.free()
    assert len(obj2.data.polygons) == 2
    with _extracted(source) as (_scene, topology, _tmp):
        assert topology["rewired"] != rewired


def test_edit_mode_changes_are_exported() -> None:
    h.reset_scene()
    source = _source()
    obj = _object("mesh", STRIP_VERTS, STRIP_FACES, source)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        bm = bmesh.from_edit_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bm.verts[0].co.z = 5.0
        bmesh.update_edit_mesh(obj.data)
        with _extracted(source) as (scene, _topology, _tmp):
            assert scene.primitives[0].positions[:, 2].max() == 5.0
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


# --- triangles -------------------------------------------------------------------------


def test_ngon_triangulated_with_polygon_indices() -> None:
    h.reset_scene()
    source = _source()
    angles = np.linspace(0.0, 2.0 * math.pi, 7, endpoint=False)
    heptagon = [(math.cos(a), math.sin(a), 0.0) for a in angles]
    quad = [(3, 0, 0), (4, 0, 0), (4, 1, 0), (3, 1, 0)]
    tri = [(6, 0, 0), (7, 0, 0), (6, 1, 0)]
    verts = heptagon + quad + tri
    faces = [tuple(range(7)), (7, 8, 9, 10), (11, 12, 13)]
    _object("ngon", verts, faces, source)

    with _extracted(source) as (scene, _topology, _tmp):
        prim = scene.primitives[0]
        assert prim.indices.shape == (8, 3)
        assert np.bincount(prim.source_polygon).tolist() == [5, 2, 1]
        corners = _corner_positions(prim).astype(np.float64)
        polygon_verts = [np.array(verts[:7]), np.array(quad), np.array(tri)]
        for row, polygon in enumerate(prim.source_polygon.tolist()):
            for corner in corners[row]:
                assert np.abs(polygon_verts[polygon] - corner).sum(axis=1).min() < 1e-6
        area = 0.5 * np.linalg.norm(
            np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
        )
        heptagon_area = 0.5 * 7 * math.sin(2 * math.pi / 7)
        assert math.isclose(area[prim.source_polygon == 0].sum(), heptagon_area, rel_tol=1e-5)


def test_triangle_fast_path_matches_loop_triangles() -> None:
    h.reset_scene()
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=2)
    obj = bpy.context.object
    mesh = obj.data
    count = len(mesh.loop_triangles)
    loops = np.empty(count * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", loops)
    polygons = np.empty(count, dtype=np.int32)
    mesh.loop_triangles.foreach_get("polygon_index", polygons)
    tri_loops, tri_poly = _export()._triangles(mesh)
    assert np.array_equal(tri_loops, loops.reshape(-1, 3))
    assert np.array_equal(tri_poly, polygons)


# --- vertices and attributes --------------------------------------------------------------


def test_weld_keeps_uv_seams_split() -> None:
    h.reset_scene()
    source = _source()
    obj = _object("strip", STRIP_VERTS, STRIP_FACES, source)
    smooth = _corner_uvs(obj, np.array(STRIP_VERTS)[:, :2] / 2.0)
    obj.data.uv_layers.new(name="UVMap").uv.foreach_set("vector", smooth.ravel())

    with _extracted(source) as (scene, _topology, _tmp):
        prim = scene.primitives[0]
        assert prim.positions.shape[0] == 6, prim.positions.shape
        assert prim.indices.shape == (4, 3)

    # A seam on the shared edge: the second quad's UVs move away.
    seamed = smooth.copy()
    seamed[4:] += (5.0, 0.0)
    obj.data.uv_layers["UVMap"].uv.foreach_set("vector", seamed.ravel())
    with _extracted(source) as (scene, _topology, _tmp):
        prim = scene.primitives[0]
        assert prim.positions.shape[0] == 8, prim.positions.shape
        uv = prim.texcoords[0][prim.indices.astype(np.int64)]
        # Triangles 0-1 are the first quad, 2-3 the second.
        assert uv[:2, :, 0].max() <= 1.0 and uv[2:, :, 0].min() >= 5.0
        positions = _corner_positions(prim)
        assert np.allclose(positions[:, :, 0].min(axis=1), [0, 0, 1, 1])


def test_weld_splits_sharp_normals() -> None:
    h.reset_scene()
    source = _source()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.object
    for collection in cube.users_collection:
        collection.objects.unlink(cube)
    source.objects.link(cube)
    with _extracted(source) as (scene, _topology, _tmp):
        prim = scene.primitives[0]
        # Flat shading: every face has its own four corners.
        assert prim.positions.shape[0] == 24
        assert np.allclose(np.abs(prim.normals).max(axis=1), 1.0)


def test_two_uv_maps_render_map_first() -> None:
    h.reset_scene()
    source = _source()
    uvs = {
        "A": np.full((6, 2), 0.1),
        "B": np.full((6, 2), 0.2),
        "C": np.full((6, 2), 0.3),
    }
    obj = _object(
        "tri2", [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)], [(0, 1, 2), (1, 3, 2)], source, uvs
    )
    obj.data.uv_layers["C"].active_render = True
    obj.data.uv_layers.active = obj.data.uv_layers["B"]  # editing B must not matter

    with _extracted(source) as (scene, _topology, _tmp):
        texcoords = scene.primitives[0].texcoords
        assert len(texcoords) == 2
        assert np.allclose(texcoords[0], 0.3) and np.allclose(texcoords[1], 0.1)

    # A material that samples B through a UV Map node puts B first.
    material, tree, principled = _material("UsesB")
    tex = _image_node(tree, _generated("TexB"))
    uv_node = tree.nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = "B"
    tree.links.new(uv_node.outputs["UV"], tex.inputs["Vector"])
    tree.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
    obj.data.materials.append(material)
    with _extracted(source) as (scene, _topology, _tmp):
        texcoords = scene.primitives[0].texcoords
        assert np.allclose(texcoords[0], 0.2) and np.allclose(texcoords[1], 0.3)
        assert scene.materials[0].base_color_texture.texcoord == 0


def test_color_attribute_srgb_to_linear() -> None:
    h.reset_scene()
    source = _source()
    obj = _object("coloured", STRIP_VERTS, STRIP_FACES, source)
    mesh = obj.data
    byte = mesh.color_attributes.new("Byte", "BYTE_COLOR", "CORNER")
    byte.data.foreach_set(
        "color_srgb", np.tile([0.5, 1.0, 0.0, 1.0], len(mesh.loops)).astype(np.float32)
    )
    floats = mesh.color_attributes.new("Float", "FLOAT_COLOR", "POINT")
    floats.data.foreach_set(
        "color", np.tile([2.0, -1.0, 0.5, 1.0], len(mesh.vertices)).astype(np.float32)
    )

    mesh.color_attributes.render_color_index = 0  # Byte renders
    mesh.color_attributes.active_color_index = 1  # Float is being edited
    with _extracted(source) as (scene, _topology, _tmp):
        color = scene.primitives[0].color
        assert color is not None and color.dtype == np.float32
        srgb = round(0.5 * 255) / 255
        linear = ((srgb + 0.055) / 1.055) ** 2.4
        assert np.allclose(color, [linear, 1.0, 0.0, 1.0], atol=1e-4), color[0]

    mesh.color_attributes.render_color_index = 1
    with _extracted(source) as (scene, _topology, _tmp):
        color = scene.primitives[0].color
        assert np.allclose(color, [1.0, 0.0, 0.5, 1.0]), color[0]  # clamped to [0, 1]


# --- materials ----------------------------------------------------------------------------


def test_materials_split_into_primitives_and_merge_across_objects() -> None:
    h.reset_scene()
    source = _source()
    m1, _t, p1 = _material("M1")
    m2, _t, p2 = _material("M2")
    p1.inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
    p2.inputs["Base Color"].default_value = (0.0, 1.0, 0.0, 1.0)
    a = _object("A", STRIP_VERTS, STRIP_FACES, source)
    a.data.materials.append(m1)
    a.data.materials.append(m2)
    a.data.polygons[0].material_index = 1
    a.data.polygons[1].material_index = 0
    b = _object("B", STRIP_VERTS, STRIP_FACES, source)
    b.data.materials.append(m1)
    b.data.polygons[1].material_index = 7  # out of range: Blender clamps to the last slot
    _object("C", STRIP_VERTS, STRIP_FACES, source)  # no material

    with _extracted(source) as (scene, _topology, _tmp):
        # A's slot 0 (M1) registers before slot 1 (M2).
        assert [m.name for m in scene.materials] == ["M1", "M2"]
        assert scene.materials[0].base_color_factor == (1.0, 0.0, 0.0, 1.0)
        assert [p.material for p in scene.primitives] == [0, 1, None]
        m1_prim, m2_prim, none_prim = scene.primitives
        assert m1_prim.source_object.tolist() == [0, 0, 1, 1, 1, 1]
        assert m1_prim.source_polygon.tolist() == [1, 1, 0, 0, 1, 1]
        assert m2_prim.source_object.tolist() == [0, 0]
        assert m2_prim.source_polygon.tolist() == [0, 0]
        assert none_prim.source_object.tolist() == [2, 2, 2, 2]
        # Merged vertex buffers: B's indices are offset past A's vertices.
        assert int(m1_prim.indices[:2].max()) < int(m1_prim.indices[2:].min())


def test_principled_material_mapping() -> None:
    h.reset_scene()
    source = _source()
    material, tree, principled = _material("Full")
    links = tree.links
    base_image = _generated("Base", (1.0, 1.0, 1.0, 0.25))
    base = _image_node(tree, base_image)
    uv_node = tree.nodes.new("ShaderNodeUVMap")
    uv_node.uv_map = "Second"
    links.new(uv_node.outputs["UV"], base.inputs["Vector"])
    links.new(base.outputs["Color"], principled.inputs["Base Color"])
    clip = tree.nodes.new("ShaderNodeMath")
    clip.operation = "GREATER_THAN"
    clip.inputs[1].default_value = 0.3
    links.new(base.outputs["Alpha"], clip.inputs[0])
    links.new(clip.outputs[0], principled.inputs["Alpha"])

    normal_image = _generated("Normal", (0.5, 0.5, 1.0, 1.0), is_data=True)
    normal_tex = _image_node(tree, normal_image)
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.inputs["Strength"].default_value = 0.7
    links.new(normal_tex.outputs["Color"], normal_map.inputs["Color"])
    links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])

    mr_image = _generated("MR", (0.0, 0.6, 0.9, 1.0), is_data=True)
    mr_tex = _image_node(tree, mr_image)
    separate = tree.nodes.new("ShaderNodeSeparateColor")
    links.new(mr_tex.outputs["Color"], separate.inputs["Color"])
    links.new(separate.outputs["Green"], principled.inputs["Roughness"])
    links.new(separate.outputs["Blue"], principled.inputs["Metallic"])

    principled.inputs["Emission Color"].default_value = (1.0, 0.5, 0.25, 1.0)
    principled.inputs["Emission Strength"].default_value = 2.0
    material.use_backface_culling = False

    plain, _t, plain_principled = _material("Plain")
    plain_principled.inputs["Metallic"].default_value = 0.25
    plain_principled.inputs["Roughness"].default_value = 0.75
    plain.use_backface_culling = True

    quad = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    obj = _object("textured", quad, [(0, 1, 2, 3)], source)
    obj.data.uv_layers.new(name="UVMap").uv.foreach_set(
        "vector", np.array([(0, 0), (1, 0), (1, 1), (0, 1)], dtype=np.float32).ravel()
    )
    obj.data.uv_layers.new(name="Second")
    obj.data.materials.append(material)
    plain_obj = _object("plain", quad, [(0, 1, 2, 3)], source)
    plain_obj.data.materials.append(plain)

    with _extracted(source) as (scene, _topology, _tmp):
        full = scene.materials[[m.name for m in scene.materials].index("Full")]
        images = [i.name for i in scene.images]
        assert full.base_color_texture is not None
        assert images[full.base_color_texture.image] == "Base"
        # Base samples "Second" (slot 0); MR and normal the render map (slot 1).
        assert full.base_color_texture.texcoord == 0
        assert images[full.metallic_roughness_texture.image] == "MR"
        assert full.metallic_roughness_texture.texcoord == 1
        assert (full.metallic_factor, full.roughness_factor) == (1.0, 1.0)
        assert images[full.normal_texture.image] == "Normal"
        assert full.normal_texture.texcoord == 1
        assert math.isclose(full.normal_scale, 0.7, rel_tol=1e-6)
        assert full.base_color_factor == (1.0, 1.0, 1.0, 1.0)
        assert full.alpha_mode == "MASK" and math.isclose(full.alpha_cutoff, 0.3, rel_tol=1e-6)
        assert full.emissive_factor == (1.0, 1.0, 0.5)  # (2, 1, 0.5) clamped
        assert full.double_sided is True

        flat = scene.materials[[m.name for m in scene.materials].index("Plain")]
        assert flat.base_color_texture is None and flat.normal_texture is None
        assert math.isclose(flat.metallic_factor, 0.25) and math.isclose(
            flat.roughness_factor, 0.75
        )
        assert flat.alpha_mode == "OPAQUE" and flat.double_sided is False
        assert flat.emissive_factor == (0.0, 0.0, 0.0)

        prim = next(p for p in scene.primitives if scene.materials[p.material].name == "Full")
        assert len(prim.texcoords) == 2
        assert prim.tangents is not None and prim.tangents.shape == (prim.positions.shape[0], 4)
        assert np.allclose(np.linalg.norm(prim.tangents[:, :3], axis=1), 1.0, atol=1e-5)
        assert set(np.unique(prim.tangents[:, 3]).tolist()) <= {-1.0, 1.0}
        # UVMap runs u along +X: tangents follow it.
        assert np.allclose(prim.tangents[:, :3], (1.0, 0.0, 0.0), atol=1e-5)
        plain_prim = next(
            p for p in scene.primitives if scene.materials[p.material].name == "Plain"
        )
        assert plain_prim.tangents is None


def _normal_mapped_quad(name: str, collection, material, scale) -> bpy.types.Object:
    quad = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    faces = [(0, 1, 2, 3)]
    if name.startswith("ngon"):
        quad.append((0.5, -0.5, 0))
        faces = [(0, 4, 1, 2, 3)]
    obj = _object(name, quad, faces, collection)
    obj.data.uv_layers.new(name="UVMap").uv.foreach_set(
        "vector", _corner_uvs(obj, np.array(quad)[:, :2]).ravel()
    )
    obj.data.materials.append(material)
    obj.scale = scale
    return obj


def test_tangent_handedness_survives_mirroring() -> None:
    h.reset_scene()
    source = _source()
    material, tree, principled = _material("Bumpy")
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    tex = _image_node(tree, _generated("N", (0.5, 0.5, 1.0, 1.0), is_data=True))
    tree.links.new(tex.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], principled.inputs["Normal"])
    for name, scale in (("normal", (1, 1, 1)), ("mirrored", (-1, 1, 1)), ("ngon", (1, 1, 1))):
        _normal_mapped_quad(name, source, material, scale)
    bpy.context.view_layer.update()

    with _extracted(source) as (scene, _topology, _tmp):
        prim = scene.primitives[0]
        corners = prim.indices.astype(np.int64)
        for index, name in enumerate(scene.object_names):
            matrix = np.array(bpy.data.objects[name].matrix_world, dtype=np.float64)[:3, :3]
            v = corners[prim.source_object == index].ravel()
            n, t = prim.normals[v].astype(np.float64), prim.tangents[v].astype(np.float64)
            bitangent = np.cross(n, t[:, :3]) * t[:, 3:]
            # +U and +V of the UV map, carried into world space.
            assert np.allclose(
                t[:, :3], matrix @ (1, 0, 0) / np.linalg.norm(matrix @ (1, 0, 0)), atol=1e-4
            ), name
            assert np.allclose(bitangent, matrix @ (0, 1, 0), atol=1e-4), (name, bitangent)


def test_blend_material_is_refused() -> None:
    h.reset_scene()
    source = _source()
    material, _tree, principled = _material("Glass")
    principled.inputs["Alpha"].default_value = 0.5
    obj = _object("glass", STRIP_VERTS, STRIP_FACES, source)
    obj.data.materials.append(material)
    error = h.submodule("core.gltf_writer").ExportError
    try:
        with _extracted(source):
            pass
    except error as ex:
        assert "Glass" in str(ex)
    else:
        raise AssertionError("a blended material was exported")


# --- images ---------------------------------------------------------------------------------


def _image_state(image: bpy.types.Image) -> tuple:
    return (
        image.name,
        image.filepath,
        image.filepath_raw,
        image.source,
        image.file_format,
        image.packed_file is not None,
        image.is_dirty,
        image.colorspace_settings.name,
    )


def _png_pixels(path: Path) -> np.ndarray:
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        pixels = np.empty(len(image.pixels), dtype=np.float32)
        image.pixels.foreach_get(pixels)
        return pixels.reshape(-1, 4)
    finally:
        bpy.data.images.remove(image)


def _textured(name: str, collection, image: bpy.types.Image, slot: str = "Base Color"):
    material, tree, principled = _material(name)
    tex = _image_node(tree, image)
    tree.links.new(tex.outputs["Color"], principled.inputs[slot])
    if slot == "Emission Color":
        principled.inputs["Emission Strength"].default_value = 1.0
    quad = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    obj = _object(name, quad, [(0, 1, 2, 3)], collection)
    obj.data.uv_layers.new(name="UVMap")
    obj.data.materials.append(material)
    return obj


def test_images_referenced_in_place_or_saved_as_png() -> None:
    h.reset_scene()
    source = _source()
    files = Path(tempfile.mkdtemp(prefix="geotools-images-"))
    try:
        on_disk = files / "on disk.png"
        on_disk.write_bytes(_fixtures.png_rgba(alpha=255))
        disk_image = bpy.data.images.load(str(on_disk))
        packed_file = files / "packed.png"
        packed_bytes = _fixtures.png_rgba(width=3, height=3, alpha=255)
        packed_file.write_bytes(packed_bytes)
        packed_image = bpy.data.images.load(str(packed_file))
        packed_image.pack()
        packed_file.unlink()
        generated = _generated("Generated/with:odd name", (0.25, 0.5, 0.75, 1.0))
        linear = _generated("Linear", (0.2140, 0.2140, 0.2140, 1.0), float_buffer=True)
        exr_path = files / "texture.exr"
        exr_src = _generated("exr-src", (0.2140, 0.2140, 0.2140, 1.0), float_buffer=True)
        exr_src.filepath_raw = str(exr_path)
        exr_src.file_format = "OPEN_EXR"
        exr_src.save()
        bpy.data.images.remove(exr_src)
        exr = bpy.data.images.load(str(exr_path))

        images = [disk_image, packed_image, generated, linear, exr]
        for i, image in enumerate(images):
            _textured(f"T{i}", source, image)
        _textured("Again", source, disk_image, "Emission Color")  # deduplicated
        before = [_image_state(i) for i in images]
        count = len(bpy.data.images)

        with _extracted(source) as (scene, _topology, tmp):
            by_name = {i.name: i for i in scene.images}
            assert len(scene.images) == len(images), [i.name for i in scene.images]
            image_dir = (tmp / "images").resolve()

            ref = by_name[disk_image.name]
            # Referenced in place, as the user's path spells it; compare resolved paths,
            # since a temp dir can sit behind a symlink (macOS /var -> /private/var).
            assert ref.path.resolve() == on_disk.resolve() and ref.mime_type == "image/png"

            ref = by_name[packed_image.name]
            assert ref.path.parent == image_dir and ref.path.suffix == ".png"
            assert ref.path.read_bytes() == packed_bytes

            for image in (generated, linear, exr):
                ref = by_name[image.name]
                assert ref.mime_type == "image/png" and ref.path.parent == image_dir, ref
                assert ref.path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
                assert "/" not in ref.path.name and ":" not in ref.path.name
            gen = _png_pixels(by_name[generated.name].path)
            assert np.allclose(gen[0], (0.25, 0.5, 0.75, 1.0), atol=1 / 255)
            # Float colour buffers are linear: written to PNG as sRGB.
            for image in (linear, exr):
                pixels = _png_pixels(by_name[image.name].path)
                assert abs(pixels[0, 0] * 255 - 128) <= 1, pixels[0]

            # The same scene gives the same file names.
            with _extracted(source) as (again, _t, _tmp2):
                assert [i.path.name for i in again.images] == [i.path.name for i in scene.images]

        assert [_image_state(i) for i in images] == before
        assert len(bpy.data.images) == count
    finally:
        shutil.rmtree(files, ignore_errors=True)


def test_image_in_place_through_a_symlink_is_resolved() -> None:
    # On macOS the temporary folder is under /var, a symlink to /private/var.
    h.reset_scene()
    source = _source()
    files = Path(tempfile.mkdtemp(prefix="geotools-images-"))
    try:
        real = files / "real"
        real.mkdir()
        (real / "photo.png").write_bytes(_fixtures.png_rgba(alpha=255))
        link = files / "link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            print("    skipped: cannot create a symlink here")
            return
        image = bpy.data.images.load(str(link / "photo.png"))
        _textured("Linked", source, image)

        with _extracted(source) as (scene, _topology, _tmp):
            ref = scene.images[0]
            assert ref.path == (real / "photo.png").resolve(), ref.path
    finally:
        shutil.rmtree(files, ignore_errors=True)


def test_dirty_image_exports_current_pixels() -> None:
    h.reset_scene()
    source = _source()
    files = Path(tempfile.mkdtemp(prefix="geotools-images-"))
    try:
        path = files / "photo.png"
        original = _fixtures.png_rgba(width=2, height=2, alpha=255)
        path.write_bytes(original)
        image = bpy.data.images.load(str(path))
        painted = np.tile(np.array([0.0, 1.0, 0.0, 1.0], dtype=np.float32), 4)
        image.pixels.foreach_set(painted)  # as a texture paint stroke leaves it
        assert image.is_dirty
        _textured("Painted", source, image)

        with _extracted(source) as (scene, _topology, _tmp):
            ref = scene.images[0]
            assert ref.path != path.resolve()
            assert np.allclose(_png_pixels(ref.path), painted.reshape(-1, 4), atol=1 / 255)
        assert path.read_bytes() == original
        assert image.is_dirty and image.filepath == str(path)
        pixels = np.empty(16, dtype=np.float32)
        image.pixels.foreach_get(pixels)
        assert np.allclose(pixels, painted)
    finally:
        shutil.rmtree(files, ignore_errors=True)


# --- end to end ------------------------------------------------------------------------------


def test_extract_write_and_tile_end_to_end() -> None:
    if h.CONFIG.expect != "available" or not _writer_ready():
        return
    import rusty_tiles

    h.reset_scene()
    source = _source()
    _textured("Textured", source, _generated("Tex", (0.8, 0.4, 0.2, 1.0), size=16))
    bpy.ops.mesh.primitive_uv_sphere_add(segments=32, ring_count=16, location=(3, 0, 0))
    sphere = bpy.context.object
    for collection in sphere.users_collection:
        collection.objects.unlink(sphere)
    source.objects.link(sphere)
    sphere.modifiers.new("Subdivision", "SUBSURF").levels = 1

    with _extracted(source) as (scene, topology, tmp):
        assert topology["Sphere"].startswith("evaluated:")
        triangles = sum(p.indices.shape[0] for p in scene.primitives)
        result = h.submodule("core.gltf_writer").write_glb(scene, tmp / "source.glb")
        assert result.triangles == triangles
        scratch = tmp / "scratch"
        scratch.mkdir()
        tiled = rusty_tiles.mesh_to_tileset(
            tmp / "source.glb", tmp / "out.3tz", profile="fast", scratch_dir=scratch, force=True
        )
        report = dict(tiled.report)
        assert report["triangles"] == triangles, (report.get("triangles"), triangles)
        validation = dict(rusty_tiles.validate(tmp / "out.3tz"))
        assert validation.get("ok") is True, {
            k: v for k, v in validation.items() if k != "payloads"
        }
