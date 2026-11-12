"""Output inspection (M7): open published tiles, place them, trace them, check quality.

The exit test of M7: a small synthetic site is published with GeoTools, a leaf
and a coarse tile of it are opened, and each traces back to its source faces.
Every end-to-end test needs the rusty-tiles wheel (``--expect available``).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import bpy
import numpy as np

JOIN_SECONDS = 180.0


def _available() -> bool:
    return h.CONFIG.expect == "available"


def _inspect():
    return h.submodule("blender.inspect")


@contextmanager
def _tmpdir():
    path = Path(tempfile.mkdtemp(prefix="geotools-inspect-test-"))
    try:
        yield path
    finally:
        h.submodule("blender.publish").shutdown_job()
        if bpy.context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        _inspect().remove_tiles(bpy.context.scene)
        shutil.rmtree(path, ignore_errors=True)


def _grid(name: str, n: int, collection, origin=(0.0, 0.0), textured: bool = True):
    """An n x n grid of 1 m quads, gently rolling, with UVs and (optionally) a
    256 x 256 texture."""
    xs, ys = np.meshgrid(np.arange(n + 1, dtype=np.float64), np.arange(n + 1, dtype=np.float64))
    x, y = xs.ravel() + origin[0], ys.ravel() + origin[1]
    co = np.stack([x, y, 0.4 * np.sin(x / 5.0) + 0.3 * np.cos(y / 7.0)], axis=1)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.astype(np.float32).ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    uv = mesh.uv_layers.new(name="UVMap")
    local = np.stack([xs.ravel(), ys.ravel()], axis=1) / n
    uv.data.foreach_set("uv", local[quads.ravel()].astype(np.float32).ravel())
    mesh.update()
    if textured:
        image = bpy.data.images.new(f"{name} texture", 256, 256, alpha=False)
        px = np.indices((256, 256)).sum(axis=0) % 2
        rgba = np.ones((256, 256, 4), np.float32)
        rgba[..., 0] = px
        rgba[..., 1] = np.linspace(0, 1, 256)[None, :]
        image.pixels.foreach_set(rgba.ravel())
        image.pack()
        material = bpy.data.materials.new(f"{name} material")
        material.use_nodes = True
        tree = material.node_tree
        bsdf = next(nd for nd in tree.nodes if nd.type == "BSDF_PRINCIPLED")
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _site(n: int = 70) -> tuple[bpy.types.Object, bpy.types.Object]:
    """Two source objects: a textured terrain and an untextured patch beside it."""
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    terrain = _grid("Terrain", n, collection)
    patch = _grid("Patch", 6, collection, origin=(n + 2.0, 0.0), textured=False)
    return terrain, patch


def _place(mode: str) -> None:
    props = h.project()
    p = h.submodule("blender.properties")
    props.placement_mode = mode
    if mode == "ANCHOR":
        p.write_number(props, "anchor_lon", 153.02)
        p.write_number(props, "anchor_lat", -27.47)
        p.write_number(props, "anchor_height", 10.0)
        p.write_number(props, "heading", 25.0)
        p.write_number(props, "scene_offset_x", 3.0)
    elif mode == "SOURCE_CRS":
        props.source_crs = "EPSG:7856"
        p.write_number(props, "source_offset_e", 502000.0)
        p.write_number(props, "source_offset_n", 6960000.0)
        p.write_number(props, "source_offset_a", 20.0)
        p.write_number(props, "height_offset", 40.0)


def _publish(output: Path, profile_name: str = "FAST") -> dict:
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    profile = h.project().build_profiles[h.project().build_profile_index]
    profile.output_path = str(output)
    profile.profile = profile_name
    profile.validate = False
    assert bpy.ops.geotools.publish() == {"FINISHED"}
    publish = h.submodule("blender.publish")
    job = publish.active_job()
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    assert publish.update_from_job() is None
    assert job.result is not None, job.error
    return json.loads(job.result.record_path.read_text())


def _list() -> None:
    props = _inspect().settings(bpy.context)
    props.source = "LAST"
    assert bpy.ops.geotools.inspect_list() == {"FINISHED"}, props.result


def _open(index: int) -> bpy.types.Object:
    props = _inspect().settings(bpy.context)
    props.scope = "SELECTED"
    props.tiles_index = index
    assert bpy.ops.geotools.inspect_open() == {"FINISHED"}, props.result
    obj = bpy.context.view_layer.objects.active
    assert _inspect().tile_info(obj)["uri"] == props.tiles[index].uri
    return obj


def _face_ids(obj) -> tuple[np.ndarray, np.ndarray | None]:
    mod = _inspect()
    output_tiles = h.submodule("core.output_tiles")
    return (
        mod.face_attribute(obj.data, output_tiles.SOURCE_PRIMITIVE),
        mod.face_attribute(obj.data, output_tiles.SOURCE_TRIANGLE),
    )


def _face_map(record: dict):
    staging = Path(record["output"]).with_suffix(".geotools")
    return h.submodule("core.facemap").load_face_map(staging / "facemap.npz")


def _positions(mesh) -> np.ndarray:
    co = np.empty(3 * len(mesh.vertices), np.float32)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3).astype(np.float64)


def _corners(mesh, width: int) -> np.ndarray:
    verts = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", verts)
    return verts.reshape(-1, width)


def _leaf_error(tile, record: dict) -> float:
    """Largest distance from a leaf triangle's vertex to the nearest corner of the
    source quad its IDs name (metres)."""
    face_map = _face_map(record)
    primitive, triangle = _face_ids(tile)
    objects, polygons = face_map.lookup_many(primitive, triangle)
    tri = _positions(tile.data)[_corners(tile.data, 3)]  # (F, 3, 3)
    error = 0.0
    for index in np.unique(objects):
        source = bpy.data.objects[face_map.object_names[int(index)]].data
        quads = _positions(source)[_corners(source, 4)]  # every source polygon is a quad
        mine = objects == index
        quad = quads[polygons[mine]]  # (f, 4, 3)
        gaps = np.linalg.norm(tri[mine][:, :, None, :] - quad[:, None, :, :], axis=3)
        error = max(error, float(gaps.min(axis=2).max()))
    return error


def _select_faces(obj, mask) -> None:
    _inspect().write_face_selection(obj.data, np.asarray(mask, dtype=bool))


# --- registration and refusals ---------------------------------------------------------------


def test_inspect_registered() -> None:
    for idname in (
        "geotools.inspect_list",
        "geotools.inspect_open",
        "geotools.inspect_remove",
        "geotools.inspect_select_source",
        "geotools.inspect_select_output",
        "geotools.inspect_distance",
        "geotools.inspect_texel_density",
    ):
        assert h.operator_available(idname), idname
    assert hasattr(bpy.types.Scene, "geotools_inspect")
    h.reset_scene()
    ui = h.submodule("blender.inspect_ui")
    labels = h.label_text(h.draw(ui.GEOTOOLS_PT_inspect))
    assert "nothing published yet" in labels, labels
    assert not bpy.ops.geotools.inspect_open.poll()
    assert not bpy.ops.geotools.inspect_select_source.poll()
    assert not bpy.ops.geotools.inspect_remove.poll()


def test_inspect_list_refusals() -> None:
    h.reset_scene()
    props = _inspect().settings(bpy.context)
    props.source = "LAST"
    _result, error = h.call_op(bpy.ops.geotools.inspect_list)
    assert error and "Publish a .3tz first" in error, error
    props.source = "FILE"
    props.filepath = ""
    _result, error = h.call_op(bpy.ops.geotools.inspect_list)
    assert error and "Choose a .3tz" in error, error
    with _tmpdir() as tmp:
        props.filepath = str(tmp / "absent.3tz")
        _result, error = h.call_op(bpy.ops.geotools.inspect_list)
        assert error and "does not exist" in error, error
        (tmp / "empty.3tz").write_bytes(b"")
        props.filepath = str(tmp / "empty.3tz")
        _result, error = h.call_op(bpy.ops.geotools.inspect_list)
        if _available():
            assert error and "Cannot list empty.3tz" in error, error
        else:
            # No fallback: without rusty-tiles 0.4.1 nothing is read.
            assert error and "rusty-tiles not available" in error, error
            assert "0.4.1" in error or "not installed" in error, error
    props.property_unset("filepath")
    props.property_unset("source")


# --- the exit test ---------------------------------------------------------------------------


def test_exit_open_leaf_and_coarse_and_trace() -> None:
    """M7 exit: open any tile of a published archive and trace it to its source faces."""
    if not _available():
        return
    h.reset_scene()
    terrain, patch = _site(70)
    _place("ANCHOR")
    mod = _inspect()
    with _tmpdir() as tmp:
        record = _publish(tmp / "site.3tz")
        _list()
        props = mod.settings(bpy.context)
        tiles = list(props.tiles)
        assert props.archive == str(tmp / "site.3tz")
        assert tiles[0].depth == 0 and tiles[0].role == "coarse", [t.role for t in tiles]
        leaves = [i for i, t in enumerate(tiles) if t.role == "leaf"]
        assert leaves and all(t.triangles > 0 for t in tiles), [t.triangles for t in tiles]
        total = sum(tiles[i].triangles for i in leaves)
        assert total == record["source_glb"]["triangles"], (total, record["source_glb"])
        assert not props.notes, props.notes
        ui = h.submodule("blender.inspect_ui")
        for panel in ui.classes:
            h.draw(panel)
        row = h.label_text(h.draw_list_item(mod.GEOTOOLS_UL_output_tiles, props, tiles[1]))
        assert "leaf" in row and "tris" in row, row

        # A leaf: read-only, with face IDs, a texture page and material, in place.
        leaf = _open(leaves[0])
        assert leaf.users_collection[0].name == mod.OUTPUT_COLLECTION
        assert tuple(leaf.lock_location) == (True, True, True)
        primitive, triangle = _face_ids(leaf)
        assert triangle is not None and (primitive >= 0).all() and (triangle >= 0).all()
        assert leaf.data.uv_layers and leaf.data.materials
        textured = [
            m
            for m in leaf.data.materials
            if m.node_tree
            and any(n.type == "TEX_IMAGE" and n.image is not None for n in m.node_tree.nodes)
        ]
        assert textured, [m.name for m in leaf.data.materials]
        error = _leaf_error(leaf, record)
        tolerance = record["report_summary"]["maximum_leaf_geometry_error"] + 1e-5
        assert error <= tolerance, (error, tolerance)

        # Leaf -> source: one output face selects exactly its source polygon.
        face_map = _face_map(record)
        mask = np.zeros(len(leaf.data.polygons), bool)
        mask[0] = True
        _select_faces(leaf, mask)
        bpy.context.view_layer.objects.active = leaf
        assert bpy.ops.geotools.inspect_select_source(edit=False) == {"FINISHED"}
        name, polygon = face_map.lookup(int(primitive[0]), int(triangle[0]))
        source = bpy.data.objects[name]
        selected = np.flatnonzero(mod.face_selection(source.data))
        assert selected.tolist() == [polygon], (selected, polygon)
        assert "Selected 1 source face" in props.result, props.result

        # Source -> leaf: that polygon selects the leaf's faces made from it (two
        # triangles of the quad, both in this leaf unless a leaf border splits it).
        _select_faces(leaf, np.zeros(len(leaf.data.polygons), bool))
        assert bpy.ops.geotools.inspect_select_output(edit=False) == {"FINISHED"}
        chosen = mod.face_selection(leaf.data)
        objects, polygons = face_map.lookup_many(primitive, triangle)
        expected = (np.asarray(face_map.object_names)[objects] == name) & (polygons == polygon)
        assert np.array_equal(chosen, expected), (np.flatnonzero(chosen), np.flatnonzero(expected))
        assert 1 <= chosen.sum() <= 2

        # The coarse root: representative source primitives only.
        coarse = _open(0)
        primitive_c, triangle_c = _face_ids(coarse)
        assert triangle_c is None and (primitive_c >= 0).all()
        assert coarse.data.attributes.get("source_triangle") is None
        mask = np.zeros(len(coarse.data.polygons), bool)
        mask[0] = True
        _select_faces(coarse, mask)
        bpy.context.view_layer.objects.active = coarse
        for obj in (terrain, patch):
            _select_faces(obj, np.zeros(len(obj.data.polygons), bool))
        assert bpy.ops.geotools.inspect_select_source(edit=False) == {"FINISHED"}
        assert "Coarse tile" in props.result and "representative" in props.result, props.result
        p = int(primitive_c[0])
        rows = slice(face_map.primitive_offsets[p], face_map.primitive_offsets[p + 1])
        want = {}
        for o, poly in zip(
            face_map.source_object[rows], face_map.source_polygon[rows], strict=True
        ):
            want.setdefault(face_map.object_names[int(o)], set()).add(int(poly))
        for obj in (terrain, patch):
            got = set(np.flatnonzero(mod.face_selection(obj.data)).tolist())
            assert got == want.get(obj.name, set()), (
                obj.name,
                len(got),
                len(want.get(obj.name, ())),
            )

        # Source -> coarse: the coarse faces of that primitive light up, and say so.
        assert bpy.ops.geotools.inspect_select_output(edit=False) == {"FINISHED"}
        assert "representative" in props.result, props.result
        coarse_mask = mod.face_selection(coarse.data)
        assert np.array_equal(coarse_mask, primitive_c == p)

        # Quality: distance to source on the coarse tile, texel density on the leaf.
        bpy.context.view_layer.objects.active = coarse
        assert bpy.ops.geotools.inspect_distance() == {"FINISHED"}, props.result
        assert "RMS" in props.result, props.result
        values = np.empty(len(coarse.data.polygons), np.float32)
        coarse.data.attributes[mod.DISTANCE_ATTRIBUTE].data.foreach_get("value", values)
        finite = values[np.isfinite(values)]
        assert finite.size > 0.9 * values.size, (finite.size, values.size)
        # A flat coarse face over a rolling source sits off it, but within the
        # tile's geometric error.
        geometric_error = tiles[0].geometric_error
        assert 0.0 < np.abs(finite).max() <= geometric_error + 1e-6, (finite, geometric_error)
        assert coarse.data.color_attributes.active_color_name == mod.DISTANCE_COLOURS
        assert not coarse.modifiers

        bpy.context.view_layer.objects.active = leaf
        assert bpy.ops.geotools.inspect_texel_density() == {"FINISHED"}, props.result
        density = np.empty(len(leaf.data.polygons), np.float32)
        leaf.data.attributes[mod.DENSITY_ATTRIBUTE].data.foreach_get("value", density)
        on_terrain = np.isfinite(density)
        assert on_terrain.any()
        # Leaves keep the source texel density: 256 x 256 texels over 70 m x 70 m.
        source_density = 256 * 256 / 70.0**2
        median = float(np.median(density[on_terrain]))
        assert 0.5 * source_density <= median <= 4.0 * source_density, (median, source_density)
        assert "texels/m²" in props.result, props.result

        # Levels: one depth in place, then side by side.
        props.view = "DEPTH"
        props.view_depth = 0
        assert not coarse.hide_get() and leaf.hide_get()
        props.view = "SIDE"
        assert not leaf.hide_get()
        assert coarse.location.x == 0.0 and leaf.location.x > 70.0, leaf.location
        props.view = "ALL"
        assert leaf.location.x == 0.0

        # A face map that no longer fits the scene is refused.
        bpy.context.view_layer.objects.active = leaf
        _select_faces(leaf, np.arange(len(leaf.data.polygons)) == 0)
        terrain.data.vertices.add(1)
        terrain.data.update()
        _result, error = h.call_op(bpy.ops.geotools.inspect_select_source, edit=False)
        assert error and "no longer fits" in error and "Terrain" in error, error

        assert bpy.ops.geotools.inspect_remove() == {"FINISHED"}
        assert not mod.opened_tiles(bpy.context.scene)
        assert not [m for m in bpy.data.materials if m.get(mod.TAG)]
        assert not [i for i in bpy.data.images if i.get(mod.TAG)]


def test_open_follows_the_record_not_the_scene() -> None:
    """Tiles are placed with the recorded placement, and a changed scene is noted."""
    if not _available():
        return
    h.reset_scene()
    _site(12)
    _place("ANCHOR")
    with _tmpdir() as tmp:
        record = _publish(tmp / "site.3tz", "QUALITY")
        p = h.submodule("blender.properties")
        p.write_number(h.project(), "anchor_lon", 153.5)
        _list()
        props = _inspect().settings(bpy.context)
        assert "placement changed" in props.notes and "anchor" in props.notes, props.notes
        leaf = _open(next(i for i, t in enumerate(props.tiles) if t.role == "leaf"))
        assert _leaf_error(leaf, record) <= 1e-5


def test_open_placement_modes() -> None:
    """Leaves overlay the source in LOCAL and SOURCE_CRS projects too (pyproj inverse)."""
    if not _available():
        return
    for mode in ("LOCAL", "SOURCE_CRS"):
        h.reset_scene()
        _site(12)
        _place(mode)
        with _tmpdir() as tmp:
            record = _publish(tmp / "site.3tz", "QUALITY")
            _list()
            props = _inspect().settings(bpy.context)
            assert not props.notes, (mode, props.notes)
            props.scope = "LEAVES"
            assert bpy.ops.geotools.inspect_open() == {"FINISHED"}, props.result
            tiles = _inspect().opened_tiles(bpy.context.scene)
            assert tiles
            worst = max(_leaf_error(tile, record) for tile in tiles)
            # float32 in the tile's east-north-up frame, through pyproj and back: 0.005 mm.
            assert worst <= 2e-5, (mode, worst)


def test_open_respects_the_limit() -> None:
    if not _available():
        return
    h.reset_scene()
    _site(70)
    _place("LOCAL")
    with _tmpdir() as tmp:
        _publish(tmp / "site.3tz")
        _list()
        props = _inspect().settings(bpy.context)
        props.scope = "LEAVES"
        props.limit = 1
        _result, error = h.call_op(bpy.ops.geotools.inspect_open)
        assert error and "over the limit of 1" in error, error
        props.scope = "DEPTH"
        props.open_depth = 0
        assert bpy.ops.geotools.inspect_open() == {"FINISHED"}, props.result
        assert len(_inspect().opened_tiles(bpy.context.scene)) == 1
        assert bpy.ops.geotools.inspect_open() == {"FINISHED"}
        assert "already open" in props.result, props.result


def test_open_a_chosen_archive_without_a_record() -> None:
    """A .3tz with no GeoTools staging beside it opens with the scene's placement, and
    says so; tracing needs the face map, so it is refused."""
    if not _available():
        return
    h.reset_scene()
    _site(12)
    _place("ANCHOR")
    with _tmpdir() as tmp:
        record = _publish(tmp / "site.3tz", "QUALITY")
        elsewhere = tmp / "copy"
        elsewhere.mkdir()
        shutil.copy(tmp / "site.3tz", elsewhere / "site.3tz")
        props = _inspect().settings(bpy.context)
        props.source = "FILE"
        props.filepath = str(elsewhere / "site.3tz")
        assert bpy.ops.geotools.inspect_list() == {"FINISHED"}, props.result
        assert "No build record" in props.notes, props.notes
        leaf = _open(next(i for i, t in enumerate(props.tiles) if t.role == "leaf"))
        assert _leaf_error(leaf, record) <= 1e-5
        _select_faces(leaf, np.arange(len(leaf.data.polygons)) == 0)
        _result, error = h.call_op(bpy.ops.geotools.inspect_select_source, edit=False)
        assert error and "no face map" in error, error
        props.property_unset("source")
        props.property_unset("filepath")
