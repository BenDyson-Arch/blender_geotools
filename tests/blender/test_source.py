"""Source ingest, offset files and the scene summary."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _fixtures as fx
import _harness as h
import bpy
import numpy as np


@contextmanager
def _tmpdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        yield Path(tmp)


def _world_vertices(objects: list[bpy.types.Object]) -> np.ndarray:
    """World positions of every vertex, read with foreach_get, sorted by row."""
    parts = []
    for obj in objects:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        m = np.array(obj.matrix_world, dtype=np.float64)
        parts.append(co.reshape(-1, 3).astype(np.float64) @ m[:3, :3].T + m[:3, 3])
    points = np.concatenate(parts)
    return points[np.lexsort(points.T[::-1])]


def _sorted(points: list[tuple[float, float, float]]) -> np.ndarray:
    a = np.array(points, dtype=np.float64)
    return a[np.lexsort(a.T[::-1])]


def _y_up_to_blender(v: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = v
    return (x, -z, y)


def _source_objects() -> list[bpy.types.Object]:
    collection = h.project().source_collection
    assert collection is not None
    return list(collection.all_objects)


def _assert_only_in_source(objects: list[bpy.types.Object]) -> None:
    collection = h.project().source_collection
    for obj in objects:
        assert list(obj.users_collection) == [collection], (obj.name, obj.users_collection[:])


def test_ingest_glb_converts_y_up() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        path = tmp / "site.glb"
        fx.write_glb(path, [fx.TRIANGLE])
        result = bpy.ops.geotools.ingest_source(filepath=str(path))
    assert result == {"FINISHED"}
    props = h.project()
    collection = props.source_collection
    assert collection is not None and collection.name == "GeoTools Source"
    assert collection in bpy.context.scene.collection.children_recursive
    objects = _source_objects()
    assert [o.type for o in objects] == ["MESH"], objects
    _assert_only_in_source(objects)
    # glTF (x, y, z) Y-up arrives as Blender (x, -z, y) Z-up.
    expected = _sorted([_y_up_to_blender(v) for v in fx.TRIANGLE])
    got = _world_vertices(objects)
    assert np.allclose(got, expected, atol=1e-6), (got, expected)
    assert props.placement_mode == "LOCAL"  # no offset file
    assert props.summary_valid and props.summary_faces == 1 and props.summary_vertices == 3


def test_ingest_obj_z_up_is_identity() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        path = tmp / "model.obj"
        fx.write_obj(path, [fx.TRIANGLE])
        assert bpy.ops.geotools.ingest_source(filepath=str(path)) == {"FINISHED"}
    objects = _source_objects()
    _assert_only_in_source(objects)
    got = _world_vertices(objects)
    assert np.allclose(got, _sorted(list(fx.TRIANGLE)), atol=1e-6), got


def test_ingest_obj_y_up() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        path = tmp / "model.obj"
        fx.write_obj(path, [fx.TRIANGLE])
        assert bpy.ops.geotools.ingest_source(filepath=str(path), up_axis="Y") == {"FINISHED"}
    got = _world_vertices(_source_objects())
    expected = _sorted([_y_up_to_blender(v) for v in fx.TRIANGLE])
    assert np.allclose(got, expected, atol=1e-6), got


def test_ingest_ply_z_up_is_identity() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        path = tmp / "cloud.ply"
        fx.write_ply(path, [fx.TRIANGLE])
        assert bpy.ops.geotools.ingest_source(filepath=str(path)) == {"FINISHED"}
    objects = _source_objects()
    _assert_only_in_source(objects)
    got = _world_vertices(objects)
    assert np.allclose(got, _sorted(list(fx.TRIANGLE)), atol=1e-6), got


def test_ingest_into_existing_collection_twice() -> None:
    h.reset_scene()
    existing = bpy.data.collections.new("My Source")
    bpy.context.scene.collection.children.link(existing)
    h.project().source_collection = existing
    with _tmpdir() as tmp:
        a, b = tmp / "a.obj", tmp / "b.glb"
        fx.write_obj(a, [fx.TRIANGLE])
        fx.write_glb(b, [fx.TRIANGLE])
        assert bpy.ops.geotools.ingest_source(filepath=str(a)) == {"FINISHED"}
        assert bpy.ops.geotools.ingest_source(filepath=str(b)) == {"FINISHED"}
    assert h.project().source_collection == existing
    assert len(existing.all_objects) == 2
    _assert_only_in_source(list(existing.all_objects))
    assert "GeoTools Source" not in bpy.data.collections


def test_ingest_refuses_bad_paths() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        missing = tmp / "nothing.obj"
        result, error = h.call_op(bpy.ops.geotools.ingest_source, filepath=str(missing))
        assert error and "File not found" in error, (result, error)
        wrong = tmp / "notes.txt"
        wrong.write_text("x")
        result, error = h.call_op(bpy.ops.geotools.ingest_source, filepath=str(wrong))
        assert error and ".glb, .gltf, .obj or .ply" in error, (result, error)
    assert not bpy.data.objects


def test_ingest_detects_offset_file() -> None:
    h.reset_scene()
    p = h.submodule("blender.properties")
    with _tmpdir() as tmp:
        path = tmp / "site.obj"
        fx.write_obj(path, [fx.TRIANGLE])
        fx.write_offset(tmp / "site_offset.txt", "512345.987654321", "6961000.123456", "12.5")
        assert bpy.ops.geotools.ingest_source(filepath=str(path)) == {"FINISHED"}
        props = h.project()
        assert props.placement_mode == "SOURCE_CRS"
        assert props.crs == ""  # left for the user
        assert Path(props.offset_file) == tmp / "site_offset.txt"
        assert p.read_number(props, "origin_e") == 512345.987654321
        assert p.read_number(props, "origin_n") == 6961000.123456
        assert p.read_number(props, "source_offset_a") == 12.5
        # The CRS is still missing, and the checks say so.
        issues = p.placement_from_props(props).validate()
        assert [i.code for i in issues] == ["crs-missing"], issues


def test_ingest_offset_report_asks_for_crs() -> None:
    h.reset_scene()
    source = h.submodule("blender.source")
    with _tmpdir() as tmp:
        path = tmp / "site.obj"
        fx.write_obj(path, [fx.TRIANGLE])
        fx.write_offset(tmp / "offset.txt", "1", "2", "3")
        level, message = source.detect_offset(h.project(), path)
        assert level == {"WARNING"} and "Set the source CRS" in message, (level, message)
        h.project().crs = "EPSG:32760"
        level, message = source.detect_offset(h.project(), path)
        assert level == {"INFO"} and "Set the source CRS" not in message, (level, message)
        # A broken offset file is reported and leaves the placement alone.
        h.reset_scene()
        (tmp / "offset.txt").write_text("E: 1\nN: 2\n")
        level, message = source.detect_offset(h.project(), path)
        assert level == {"WARNING"} and "could not read" in message, message
        assert h.project().placement_mode == "LOCAL"


def test_load_offset_file_operator() -> None:
    h.reset_scene()
    p = h.submodule("blender.properties")
    with _tmpdir() as tmp:
        good = tmp / "offset.txt"
        fx.write_offset(good, "300000.25", "6961000.123456", "-4")
        bad = tmp / "bad.txt"
        bad.write_text("E 1\n")
        result, error = h.call_op(bpy.ops.geotools.load_offset_file, filepath=str(bad))
        assert error and "not a valid offset file" in error, (result, error)
        assert h.project().placement_mode == "LOCAL"
        result, error = h.call_op(bpy.ops.geotools.load_offset_file, filepath=str(tmp / "no.txt"))
        assert error and "Cannot read" in error, error
        assert bpy.ops.geotools.load_offset_file(filepath=str(good)) == {"FINISHED"}
    props = h.project()
    assert props.placement_mode == "SOURCE_CRS"
    assert Path(props.offset_file).name == "offset.txt"
    placement = p.placement_from_props(props)
    assert placement.source_offset == (300000.25, 6961000.123456, -4.0)
    assert placement.source_crs == ""  # the file says nothing about the CRS


# --- summary ---------------------------------------------------------------------------


def _cube(name: str, collection: bpy.types.Collection) -> bpy.types.Object:
    """A [-1, 1] cube built with foreach_set."""
    corners = np.array(
        [[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], dtype=np.float32
    )
    faces = np.array(
        [
            [0, 1, 3, 2],
            [4, 6, 7, 5],
            [0, 4, 5, 1],
            [2, 3, 7, 6],
            [0, 2, 6, 4],
            [1, 5, 7, 3],
        ],
        dtype=np.int32,
    )
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(8)
    mesh.vertices.foreach_set("co", corners.ravel())
    mesh.loops.add(24)
    mesh.loops.foreach_set("vertex_index", faces.ravel())
    mesh.polygons.add(6)
    mesh.polygons.foreach_set("loop_start", np.arange(0, 24, 4, dtype=np.int32))
    mesh.update()
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _source_collection() -> bpy.types.Collection:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def test_summary_bounds_are_exact_for_rotated_mesh() -> None:
    """A rotated mesh's bounds come from its vertices, not its loose bound_box."""
    h.reset_scene()
    summarise = h.submodule("blender.summarise")
    collection = _source_collection()
    mesh = bpy.data.meshes.new("Tri")
    mesh.from_pydata([(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 1.0, 0.0)], [], [(0, 1, 2)])
    obj = bpy.data.objects.new("Tri", mesh)
    collection.objects.link(obj)
    obj.rotation_euler = (0.0, 0.0, math.pi / 4)
    bpy.context.view_layer.update()

    lo, hi = summarise.world_bounds(obj)
    s = math.sqrt(0.5)
    # Vertices map to (0, 0), (2s, 2s) and (-s, s); the bound_box corner (2, 1)
    # would map to (s, 3s) and widen the box.
    assert np.allclose(lo, (-s, 0.0, 0.0), atol=1e-6), lo
    assert np.allclose(hi, (2 * s, 2 * s, 0.0), atol=1e-6), hi


def test_summary_bounds_match_known_geometry() -> None:
    h.reset_scene()
    summarise = h.submodule("blender.summarise")
    collection = _source_collection()
    a = _cube("A", collection)
    a.location = (10.0, 20.0, 30.0)
    a.scale = (2.0, 3.0, 4.0)
    a.rotation_euler = (0.0, 0.0, math.pi / 2)  # x extent 3, y extent 2
    child = bpy.data.collections.new("Child")
    collection.children.link(child)
    b = _cube("B", child)
    b.location = (-5.0, 0.0, 0.0)
    empty = bpy.data.objects.new("Empty", None)
    empty.location = (1000.0, 1000.0, 1000.0)
    collection.objects.link(empty)

    summary = summarise.summarise_collection(collection, bpy.context)
    by_name = {o.name: o for o in summary.objects}
    assert set(by_name) == {"A", "B", "Empty"}
    assert by_name["A"].faces == 6 and by_name["A"].vertices == 8
    assert np.allclose(by_name["A"].bounds_min, (7.0, 18.0, 26.0), atol=1e-5), by_name["A"]
    assert np.allclose(by_name["A"].bounds_max, (13.0, 22.0, 34.0), atol=1e-5), by_name["A"]
    assert np.allclose(by_name["B"].bounds_min, (-6.0, -1.0, -1.0), atol=1e-5)
    assert by_name["Empty"].type == "EMPTY"
    lo, hi = summary.bounds()
    assert np.allclose(lo, (-6.0, -1.0, -1.0), atol=1e-5), lo
    assert np.allclose(hi, (13.0, 22.0, 34.0), atol=1e-5), hi
    assert summary.unit_scale == 1.0

    # The cached copy the panel draws.
    assert bpy.ops.geotools.refresh_source_summary() == {"FINISHED"}
    props = h.project()
    assert (props.summary_objects, props.summary_meshes) == (3, 2)
    assert (props.summary_faces, props.summary_vertices) == (12, 16)
    p = h.submodule("blender.properties")
    assert np.allclose(p.parse_vec(props.summary_bounds_max), (13.0, 22.0, 34.0), atol=1e-5)
    panel = h.submodule("blender.panels").GEOTOOLS_PT_source
    labels = list(h.labels(h.draw(panel)))
    for row in (("Faces", "NONE"), ("12", "NONE"), ("3 (2 meshes)", "NONE")):
        assert row in labels, labels
    assert ("19 \u00d7 23 \u00d7 35 m", "NONE") in labels, labels  # the bounds' size
    assert h.draw_header(panel) == "CHECKMARK"


def test_summary_layers_and_deformers() -> None:
    h.reset_scene()
    summarise = h.submodule("blender.summarise")
    collection = _source_collection()
    obj = _cube("Cube", collection)
    obj.data.uv_layers.new(name="UV0")
    obj.data.uv_layers.new(name="UV1")
    obj.data.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
    obj.shape_key_add(name="Basis")
    obj.modifiers.new("Armature", "ARMATURE")
    obj.keyframe_insert("location", frame=1)
    summary = summarise.summarise_collection(collection, bpy.context).objects[0]
    assert summary.uv_layers == 2 and summary.color_attributes == 1
    assert summary.has_shape_keys and summary.has_armature and summary.is_animated

    plain = _cube("Plain", collection)
    s = summarise.summarise_collection(collection, bpy.context)
    plain_summary = next(o for o in s.objects if o.name == plain.name)
    assert not (plain_summary.has_shape_keys or plain_summary.has_armature)
    assert not plain_summary.is_animated
    assert plain_summary.materials == ()


def test_summary_maps_imported_alpha_modes() -> None:
    """Materials made by Blender's glTF importer map back to their alpha mode."""
    h.reset_scene()
    tris = [
        fx.TRIANGLE,
        tuple((x + 10.0, y, z) for x, y, z in fx.TRIANGLE),
        tuple((x + 20.0, y, z) for x, y, z in fx.TRIANGLE),
    ]
    with _tmpdir() as tmp:
        path = tmp / "alpha.glb"
        fx.write_glb(path, tris, alpha_modes=["OPAQUE", "MASK", "BLEND"])
        assert bpy.ops.geotools.ingest_source(filepath=str(path)) == {"FINISHED"}
    summarise = h.submodule("blender.summarise")
    summary = summarise.summarise_collection(h.project().source_collection, bpy.context)
    materials = {m.name: m for o in summary.objects for m in o.materials}
    assert set(materials) == {"MatOpaque", "MatMask", "MatBlend"}, materials
    assert materials["MatOpaque"].blend_mode == "OPAQUE"
    assert materials["MatMask"].blend_mode == "CLIP"
    assert materials["MatBlend"].blend_mode == "BLEND"
    assert bpy.data.materials["MatBlend"].surface_render_method == "BLENDED"
    for m in materials.values():
        assert m.uses_principled, m
        # The importer's own nodes, alpha clip included, are all expressible.
        assert m.unsupported_nodes == (), m
        assert len(m.images) == 1, m
        image = m.images[0]
        assert image.packed and image.exists and image.file_format == "PNG", image


def test_summary_flags_unsupported_nodes() -> None:
    h.reset_scene()
    summarise = h.submodule("blender.summarise")
    material = bpy.data.materials.new("Odd")
    tree = material.node_tree
    principled = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    noise = tree.nodes.new("ShaderNodeTexNoise")
    tree.links.new(noise.outputs["Color"], principled.inputs["Base Color"])
    tree.nodes.new("ShaderNodeTexVoronoi")  # not connected, so ignored
    s = summarise.summarise_material(material)
    assert s.uses_principled
    assert s.unsupported_nodes == ("ShaderNodeTexNoise",), s
    assert s.blend_mode == "OPAQUE"
    principled.inputs["Alpha"].default_value = 0.5
    assert summarise.summarise_material(material).blend_mode == "BLEND"

    emission = bpy.data.materials.new("Glow")
    tree = emission.node_tree
    output = tree.get_output_node("ALL")
    node = tree.nodes.new("ShaderNodeEmission")
    tree.links.new(node.outputs[0], output.inputs["Surface"])
    s = summarise.summarise_material(emission)
    assert not s.uses_principled
    assert "ShaderNodeEmission" in s.unsupported_nodes


def test_summary_missing_image() -> None:
    h.reset_scene()
    summarise = h.submodule("blender.summarise")
    material = bpy.data.materials.new("Textured")
    tree = material.node_tree
    principled = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    tex = tree.nodes.new("ShaderNodeTexImage")
    image = bpy.data.images.new("Gone", 4, 4)
    image.source = "FILE"
    image.filepath = "/nonexistent/geotools/gone.png"
    tex.image = image
    tree.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
    s = summarise.summarise_material(material)
    assert [i.name for i in s.images] == ["Gone"]
    assert s.images[0].filepath == "/nonexistent/geotools/gone.png"
    assert not s.images[0].exists and not s.images[0].packed
    assert s.unsupported_nodes == ()
