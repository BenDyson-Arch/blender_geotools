"""Source ingest, offset files and the scene summary."""

from __future__ import annotations

import json
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


# --- an offset file that would move an existing origin ----------------------------------

E1, N1, A1 = "512000.25", "6961000.5", "10"
E2, N2, A2 = "512100.75", "6961050.125", "12.5"
GEOSCENE = ("SRID", "crs x", "crs y", "latitude", "longitude")


def _keys() -> dict[str, object]:
    scene = bpy.context.scene
    return {k: scene[k] for k in GEOSCENE if k in scene}


def _chunk(folder: Path, name: str, offset: tuple[str, str, str] | None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.obj"
    fx.write_obj(path, [fx.TRIANGLE])
    if offset is not None:
        fx.write_offset(folder / f"{name}_offset.txt", *offset)
    return path


def _import(path: Path) -> list[bpy.types.Object]:
    before = set(bpy.data.objects)
    assert bpy.ops.geotools.ingest_source(filepath=str(path)) == {"FINISHED"}
    return [obj for obj in bpy.data.objects if obj not in before]


def _origin() -> tuple[float, float, float]:
    p = h.submodule("blender.properties")
    props = h.project()
    return tuple(p.read_number(props, a) for a in ("origin_e", "origin_n", "source_offset_a"))


def _offer() -> dict | None:
    return h.submodule("blender.source").pending_offset_offer(bpy.context)


def _source_panel_calls() -> list:
    return h.draw(h.submodule("blender.panels").GEOTOOLS_PT_source)


def test_second_chunk_with_another_offset_is_offered_not_applied() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        first = _import(_chunk(tmp / "one", "chunk1", (E1, N1, A1)))
        props = h.project()
        assert props.placement_mode == "SOURCE_CRS"
        assert _origin() == (float(E1), float(N1), float(A1))
        props.crs = "EPSG:7856"
        keys = _keys()
        assert keys["crs x"] == float(E1)
        assert _offer() is None

        second = _import(_chunk(tmp / "two", "chunk2", (E2, N2, A2)))
        # The project origin, its keys and the file it came from are as they were.
        assert _origin() == (float(E1), float(N1), float(A1))
        assert _keys() == keys
        assert Path(props.offset_file).name == "chunk1_offset.txt"
        assert tuple(second[0].location) == (0.0, 0.0, 0.0)  # imported unshifted
        pending = _offer()
        assert pending is not None
        message = pending["message"]
        assert f"E {E2}, N {N2}, A {A2}" in message and f"E {E1}, N {N1}, A {A1}" in message
        shift = (float(E2) - float(E1), float(N2) - float(N1), float(A2) - float(A1))
        assert tuple(pending["shift"]) == shift
        calls = _source_panel_calls()
        assert ("operator", "geotools.use_offset_file_origin", "ORIENTATION_GLOBAL") in calls
        assert ("operator", "geotools.place_at_project_origin", "OBJECT_ORIGIN") in calls
        assert "imported unshifted" in h.label_text(calls)

        # Place the imported objects at the project origin: a float64 difference.
        assert pending["objects"] == [o.name for o in second]
        result = bpy.ops.geotools.place_at_project_origin(
            objects=json.dumps(pending["objects"]),
            shift=h.submodule("blender.properties").format_vec(shift),
        )
        assert result == {"FINISHED"}
        assert tuple(second[0].location) == tuple(float(np.float32(d)) for d in shift)
        assert tuple(first[0].location) == (0.0, 0.0, 0.0)
        assert _offer() is None
        assert _origin() == (float(E1), float(N1), float(A1))

        # Or use the second file as the project origin instead (EXEC: no question).
        third = _import(_chunk(tmp / "three", "chunk3", (E2, N2, A2)))
        assert third and _offer() is not None
        path = _offer()["path"]
        assert bpy.ops.geotools.use_offset_file_origin(filepath=path) == {"FINISHED"}
        assert _origin() == (float(E2), float(N2), float(A2))
        assert bpy.context.scene["crs x"] == float(E2) and props.crs == "EPSG:7856"
        assert Path(props.offset_file).name == "chunk3_offset.txt"
        assert _offer() is None


def test_the_same_offset_reimported_or_shared_is_silent() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        chunk1 = _chunk(tmp / "one", "chunk1", (E1, N1, A1))
        _import(chunk1)
        props = h.project()
        props.crs = "EPSG:7856"
        keys = _keys()
        _import(chunk1)  # imported again
        _import(_chunk(tmp / "two", "chunk2", (E1, N1, A1)))  # another chunk, the same offset
        assert _offer() is None
        assert _keys() == keys  # not rewritten, lat/lon included
        assert _origin() == (float(E1), float(N1), float(A1))
        level, message = h.submodule("blender.source").detect_offset(props, chunk1, bpy.context)
        assert level == {"INFO"} and "project's origin already" in message, (level, message)
        assert not any(
            kind == "operator" and "offset_file_origin" in text
            for kind, text, _ in _source_panel_calls()
        )


def test_blendergis_keys_are_not_re_origined_by_an_offset_file() -> None:
    h.reset_scene()
    scene = bpy.context.scene
    scene["SRID"], scene["crs x"], scene["crs y"] = "EPSG:7856", 502_000.0, 6_961_000.0
    scene["latitude"], scene["longitude"] = -27.47, 153.02
    keys = _keys()
    props = h.project()
    with _tmpdir() as tmp:
        objects = _import(_chunk(tmp, "site", (E1, N1, A1)))
        assert props.placement_mode == "LOCAL"  # not switched behind the user's back
        assert _keys() == keys
        pending = _offer()
        assert pending is not None and pending["shift"] is None  # a Local frame: no shift
        assert "BlenderGIS (GeoScene) georeference" in pending["message"]
        calls = _source_panel_calls()
        assert ("operator", "geotools.use_offset_file_origin", "ORIENTATION_GLOBAL") in calls
        assert not any(text == "geotools.place_at_project_origin" for _, text, _ in calls)
        assert objects
        # The user chooses the file: a CRS project at the file's origin, keys rewritten.
        assert bpy.ops.geotools.use_offset_file_origin(filepath=pending["path"]) == {"FINISHED"}
        assert props.placement_mode == "SOURCE_CRS"
        assert (scene["SRID"], scene["crs x"], scene["crs y"]) == (
            "EPSG:7856",
            float(E1),
            float(N1),
        )
        assert _origin() == (float(E1), float(N1), float(A1))
        assert props.georef_notice == ""  # nothing of GeoTools' own was overruled

    # Keys that are the file's E/N: the file applies, and only adds A.
    h.reset_scene()
    scene["SRID"], scene["crs x"], scene["crs y"] = "EPSG:7856", float(E1), float(N1)
    with _tmpdir() as tmp:
        _import(_chunk(tmp, "site", (E1, N1, A1)))
        assert _offer() is None
        assert h.project().placement_mode == "SOURCE_CRS"
        assert _origin() == (float(E1), float(N1), float(A1))


def test_an_anchor_project_gets_the_offer_not_a_switch() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "ANCHOR"
    with _tmpdir() as tmp:
        _import(_chunk(tmp, "site", (E1, N1, A1)))
        assert props.placement_mode == "ANCHOR"
        pending = _offer()
        assert pending is not None and "Anchor project" in pending["message"]
        assert bpy.ops.geotools.use_offset_file_origin(filepath=pending["path"]) == {"FINISHED"}
        assert props.placement_mode == "SOURCE_CRS"
        assert _origin() == (float(E1), float(N1), float(A1))
        # Dismissing an offer clears it.
        _import(_chunk(tmp / "b", "other", (E2, N2, A2)))
        assert _offer() is not None
        assert bpy.ops.geotools.dismiss_offset_offer() == {"FINISHED"}
        assert _offer() is None


def test_placing_far_objects_warns_about_single_precision() -> None:
    h.reset_scene()
    with _tmpdir() as tmp:
        objects = _import(_chunk(tmp, "site", None))
    source = h.submodule("blender.source")
    recorder = h.ReportRecorder()
    recorder.objects = json.dumps([o.name for o in objects])
    recorder.shift = "20000.123456, 5.0, 0.0"
    assert source.GEOTOOLS_OT_place_at_project_origin.execute(recorder, bpy.context) == {"FINISHED"}
    levels = {next(iter(level)): text for level, text in recorder.reports}
    assert "single precision" in levels["WARNING"], recorder.reports
    assert objects[0].location.x == float(np.float32(20000.123456))
    objects[0].location = (0.0, 0.0, 0.0)
    recorder = h.ReportRecorder()
    recorder.objects, recorder.shift = json.dumps([o.name for o in objects]), "1.0, 0.0, 0.0"
    source.GEOTOOLS_OT_place_at_project_origin.execute(recorder, bpy.context)
    assert all("WARNING" not in level for level, _ in recorder.reports)


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
    # A normalised path in the OS's own spelling (backslashes on Windows).
    filepath = s.images[0].filepath
    assert Path(filepath) == Path("/nonexistent/geotools/gone.png"), filepath
    assert not s.images[0].exists and not s.images[0].packed
    assert s.unsupported_nodes == ()
