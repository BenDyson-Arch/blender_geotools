"""The live layer view: Display > Show in Viewport (``blender.live_view``).

The view's colours against ``core.ramps`` (ramps, log scale, a degenerate
range, no-data, categories, booleans, texture layers through a Cycles bake),
Display changes rebuilding it, topology-changing Geometry Nodes upstream, the
user's data left alone, Solid shading, one layer at a time, turning it off,
saving and reloading, undo, and publishes byte-identical with the view on.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import bpy
import numpy as np

JOIN_SECONDS = 180.0


def _view():
    return h.submodule("blender.live_view")


def _layers():
    return h.submodule("blender.layers")


def _core():
    return h.submodule("core.layers")


def _ramps():
    return h.submodule("core.ramps")


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.get("Source")
    if collection is None:
        collection = bpy.data.collections.new("Source")
        bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _grid(name: str = "Grid", n: int = 4, offset: float = 0.0) -> bpy.types.Object:
    """An n x n grid of quads with a UV map, in the source collection."""
    xs, ys = np.meshgrid(np.arange(n + 1, dtype=np.float32), np.arange(n + 1, dtype=np.float32))
    co = np.stack([xs.ravel() + offset, ys.ravel(), 0.05 * np.sin(xs.ravel())], axis=1)
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
    uv.data.foreach_set("uv", (co[quads.ravel(), :2] / n).astype(np.float32).ravel())
    mesh.update()
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    _source().objects.link(obj)
    return obj


def _material(name: str, rgb=(0.2, 0.4, 0.6)) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    if material.node_tree is None:
        material.use_nodes = True
    bsdf = next(n for n in material.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    return material


def _layer(name: str = "slope", domain: str = "FACE", data_type: str = "FLOAT", **kwargs):
    core = _core()
    spec_fields = {
        k: kwargs.pop(k) for k in ("nodata", "categories", "source_attribute") if k in kwargs
    }
    spec = core.LayerSpec(
        name=name, domain=core.Domain(domain), data_type=core.DataType(data_type), **spec_fields
    )
    item = _layers().ensure_layer(bpy.context.scene, spec)
    for key, value in kwargs.items():
        setattr(item.presentation, key, value)
    return _layers().find_layer(bpy.context.scene, name)


def _write(obj: bpy.types.Object, item, values: np.ndarray) -> None:
    _layers().write_layer(obj.data, _layers().spec_from_props(item), values)


def _evaluated(obj: bpy.types.Object) -> bpy.types.Mesh:
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data


def _view_bytes(obj: bpy.types.Object) -> np.ndarray:
    """The view's colours on the evaluated mesh, one 8-bit sRGB RGBA per corner."""
    mesh = _evaluated(obj)
    attr = mesh.attributes["gt_view"]
    assert (attr.domain, attr.data_type) == ("CORNER", "BYTE_COLOR")
    values = np.empty(len(mesh.loops) * 4, dtype=np.float32)
    attr.data.foreach_get("color_srgb", values)
    return np.round(values.reshape(-1, 4) * 255.0).astype(np.int64)


def _per_corner(mesh: bpy.types.Mesh, per_face: np.ndarray) -> np.ndarray:
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return np.repeat(per_face, totals, axis=0)


def _assert_close(got: np.ndarray, want: np.ndarray, what: str) -> None:
    diff = np.abs(got.astype(np.int64) - want.astype(np.int64))
    assert diff.max() <= 1, f"{what}: off by {diff.max()} (got {got[:4]}, want {want[:4]})"


def _show(item, on: bool = True) -> None:
    item.presentation.show_in_viewport = on


def _view_modifiers(obj: bpy.types.Object) -> list[bpy.types.Modifier]:
    return [m for m in obj.modifiers if h.submodule("blender.export").is_view_modifier(m)]


def _view_blocks() -> tuple[list[str], list[str]]:
    groups = sorted(g.name for g in bpy.data.node_groups if "geotools_view" in g)
    materials = sorted(m.name for m in bpy.data.materials if "geotools_view" in m)
    return groups, materials


def _clear() -> None:
    for item in h.project().layers:
        item.presentation.show_in_viewport = False
    h.reset_scene()
    for group in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(group)
    for screen in bpy.data.screens:
        if "geotools_view_shading" in screen:
            del screen["geotools_view_shading"]


def _view3d_shadings() -> list:
    return [
        area.spaces.active.shading
        for screen in bpy.data.screens
        for area in screen.areas
        if area.type == "VIEW_3D"
    ]


# --- registration and UI --------------------------------------------------------------------


def test_view_registered_and_drawn() -> None:
    _clear()
    _grid()
    item = _layer()
    assert "show_in_viewport" in item.presentation.bl_rna.properties
    assert not h.operator_available("geotools.layer_write_presentation")  # retired
    calls = h.draw(h.layer_panel("display"))
    assert ("operator", "geotools.layer_show", "HIDE_ON") in [c[:3] for c in calls], calls
    modules = [m.__name__.rpartition(".")[2] for m in h.submodule("blender").MODULES]
    assert modules.index("presentation") < modules.index("live_view")
    _show(item)
    calls = h.draw(h.layer_panel("display"))
    assert ("operator", "geotools.layer_show", "HIDE_OFF") in [c[:3] for c in calls], calls
    _show(item, False)


# --- colours ---------------------------------------------------------------------------------


def test_face_ramp_matches_the_published_colours() -> None:
    _clear()
    obj = _grid()
    flat = _material("Flat")
    obj.data.materials.append(flat)
    paint = obj.data.color_attributes.new("Paint", "BYTE_COLOR", "CORNER")
    obj.data.color_attributes.active_color = paint
    obj.data.color_attributes.render_color_index = 0
    item = _layer(range_min="0", range_max="15", palette="turbo", nodata=-9999.0)
    item.presentation.nodata_color = (1.0, 0.0, 1.0)
    values = np.arange(16, dtype=np.float32)
    values[3], values[5], values[6] = np.nan, -9999.0, np.inf
    values[7] = 40.0  # above the range: the high end
    _write(obj, item, values)
    modifiers_before = [m.name for m in obj.modifiers]

    _show(item)
    want = _ramps().apply_ramp(
        values, 0.0, 15.0, "turbo", nodata=-9999.0, nodata_rgba=(255, 0, 255, 255)
    )
    got = _view_bytes(obj)
    _assert_close(got, _per_corner(obj.data, want), "face ramp")
    assert got[_per_corner(obj.data, np.arange(16)) == 6][0].tolist() == [255, 0, 255, 255]

    # One modifier, last, viewport only; the shared material only in its output.
    (modifier,) = _view_modifiers(obj)
    assert modifier.name == "GeoTools View" and obj.modifiers[-1] == modifier
    assert modifier.show_viewport and not modifier.show_render
    assert [m.name for m in obj.modifiers] == [*modifiers_before, "GeoTools View"]
    assert [s.material for s in obj.material_slots] == [flat], "the user's slots changed"
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    view_material = bpy.data.materials["GeoTools View"]
    assert "GeoTools View" in [s.material.name for s in evaluated.material_slots]
    index = np.empty(len(evaluated.data.polygons), np.int32)
    evaluated.data.polygons.foreach_get("material_index", index)
    assert {evaluated.data.materials[i].original for i in index.tolist()} == {view_material}
    assert _view_blocks() == (["GeoTools View slope"], ["GeoTools View"])
    group = bpy.data.node_groups["GeoTools View slope"]
    assert group.bl_idname == "GeometryNodeTree" and group.get("geotools_view") == "slope"
    idnames = {n.bl_idname for n in group.nodes}
    assert {"ShaderNodeMapRange", "ShaderNodeValToRGB", "GeometryNodeSetMaterial"} <= idnames
    # Solid mode shows the active colour attribute: the view's, while it is on;
    # the render one (what the export reads) is untouched.
    colours = obj.data.color_attributes
    assert colours.active_color_name == "gt_view"
    assert colours.default_color_name == "Paint"
    assert [a.name for a in obj.data.attributes if a.name.startswith("gt_v")] == []
    for shading in _view3d_shadings():
        assert shading.color_type == "VERTEX"

    # Off: everything it added is gone and the user's settings are back.
    _show(item, False)
    assert _view_modifiers(obj) == [] and [m.name for m in obj.modifiers] == modifiers_before
    assert _view_blocks() == ([], [])
    assert colours.active_color_name == "Paint" and "geotools_view_active" not in obj.data
    for shading in _view3d_shadings():
        assert shading.color_type != "VERTEX"
    assert not any("geotools_view_shading" in s for s in bpy.data.screens)
    assert "gt_view" not in _evaluated(obj).attributes


def test_corner_log_degenerate_and_auto_ranges() -> None:
    _clear()
    obj = _grid(n=2)
    item = _layer("flow", domain="CORNER", scale="LOG", range_min="1", range_max="1000")
    values = np.array(
        [
            0.0,
            -2.0,
            0.5,
            1.0,
            3.0,
            10.0,
            31.6,
            100.0,
            999.0,
            1000.0,
            5e4,
            2.0,
            7.0,
            70.0,
            700.0,
            1.5,
        ],
        dtype=np.float32,
    )
    _write(obj, item, values)
    _show(item)
    want = _ramps().apply_ramp(values, 1.0, 1000.0, "viridis", log=True)
    _assert_close(_view_bytes(obj), want, "log ramp")

    # A degenerate range: below gets the low end, above the high end, equal the middle.
    item.presentation.scale = "LINEAR"
    item.presentation.range_min = item.presentation.range_max = "10"
    want = _ramps().apply_ramp(values, 10.0, 10.0, "viridis")
    _assert_close(_view_bytes(obj), want, "degenerate range")

    # Automatic ends come from the values, worked out when the view is turned on.
    item.presentation.range_min = item.presentation.range_max = ""
    _show(item, False)
    item.auto_min = item.auto_max = ""
    _show(item)
    low, high = _ramps().auto_range(values)
    assert float(item.auto_min) == low and float(item.auto_max) == high
    _assert_close(_view_bytes(obj), _ramps().apply_ramp(values, low, high, "viridis"), "auto")
    # A log scale takes its automatic range again, from the positive values.
    item.presentation.scale = "LOG"
    low, high = _ramps().auto_range(values, log=True)
    assert float(item.auto_min) == low
    _assert_close(
        _view_bytes(obj), _ramps().apply_ramp(values, low, high, "viridis", log=True), "auto log"
    )

    # A range the Display panel flags shows every corner in the no-data colour.
    item.presentation.range_min, item.presentation.range_max = "5", "1"
    assert np.unique(_view_bytes(obj), axis=0).tolist() == [[128, 128, 128, 255]]
    _show(item, False)


def test_categories_booleans_and_integers() -> None:
    _clear()
    obj = _grid(n=3)
    cover = _layer("cover", data_type="CATEGORY", categories=("bare", "grass", "trees"), nodata=9.0)
    defaults = _ramps().default_category_colours(3)
    for rgba in defaults:
        cover.presentation.category_colors.add().color = tuple(float(c) / 255 for c in rgba[:3])
    values = np.array([0, 1, 2, 3, -1, 9, 2, 1, 0], dtype=np.int32)
    _write(obj, cover, values)
    _show(cover)
    want = _ramps().apply_categories(values, defaults, nodata=9.0, nodata_rgba=(128, 128, 128, 255))
    _assert_close(_view_bytes(obj), _per_corner(obj.data, want), "categories")
    # Editing a category colour shows at once.
    cover.presentation.category_colors[1].color = (1.0, 0.0, 0.0)
    got = _view_bytes(obj)[_per_corner(obj.data, values) == 1]
    assert np.unique(got, axis=0).tolist() == [[255, 0, 0, 255]]

    # Turning another layer on turns this one off: one layer at a time.
    flag = _layer("flag", data_type="BOOLEAN")
    _write(obj, flag, np.array([1, 0, 1, 0, 1, 0, 1, 0, 1], dtype=bool))
    _show(flag)
    assert not cover.presentation.show_in_viewport
    assert _view_blocks() == (["GeoTools View flag"], ["GeoTools View"])
    want = _ramps().apply_ramp(np.array([1, 0, 1, 0, 1, 0, 1, 0, 1]), 0.0, 1.0, "viridis")
    _assert_close(_view_bytes(obj), _per_corner(obj.data, want), "boolean")

    count = _layer("count", data_type="INT", nodata=-1.0, range_min="0", range_max="8")
    ints = np.array([0, 1, 2, -1, 4, 5, 6, 7, 8], dtype=np.int32)
    _write(obj, count, ints)
    _show(count)
    want = _ramps().apply_ramp(ints, 0.0, 8.0, "viridis", nodata=-1.0, nodata_rgba=(128,) * 3)
    _assert_close(_view_bytes(obj), _per_corner(obj.data, want), "integers")
    # A layer no object carries shows on none (the Display toggle refuses it, test_selection).
    missing = _layer("missing", range_min="0", range_max="1")
    _show(missing)
    assert not _view_modifiers(obj)
    _show(missing, False)


def test_display_changes_rebuild_the_view() -> None:
    _clear()
    obj = _grid()
    item = _layer(range_min="0", range_max="15")
    values = np.arange(16, dtype=np.float32)
    _write(obj, item, values)
    _show(item)
    group = bpy.data.node_groups["GeoTools View slope"]
    first_nodes = len(group.nodes)
    for palette in ("magma", "greys", "diverging_rdbu"):
        item.presentation.palette = palette
        want = _ramps().apply_ramp(values, 0.0, 15.0, palette)
        _assert_close(_view_bytes(obj), _per_corner(obj.data, want), palette)
    item.presentation.range_max = "30"
    want = _ramps().apply_ramp(values, 0.0, 30.0, "diverging_rdbu")
    _assert_close(_view_bytes(obj), _per_corner(obj.data, want), "range")
    assert bpy.data.node_groups["GeoTools View slope"] == group and len(group.nodes) == first_nodes
    # An unchanged setting does not rebuild the tree.
    marker = group.nodes.new("NodeFrame")
    item.presentation.range_max = "30"
    assert marker.name in group.nodes
    # Renaming the layer moves the view to a group of the new name.
    assert bpy.ops.geotools.layer_rename("EXEC_DEFAULT", new_name="steep") == {"FINISHED"}
    assert _view_blocks()[0] == ["GeoTools View steep"]
    (modifier,) = _view_modifiers(obj)
    assert modifier.node_group.name == "GeoTools View steep"
    _assert_close(_view_bytes(obj), _per_corner(obj.data, want), "renamed")
    # A source object added later gets the view on the next update.
    other = _grid("Other", n=1, offset=6.0)
    _write(other, item, np.array([15.0], dtype=np.float32))
    item.presentation.palette = "viridis"
    assert len(_view_modifiers(other)) == 1
    _show(item, False)
    assert _view_modifiers(other) == []
    assert _view_blocks() == ([], []), _view_blocks()


def test_view_follows_geometry_nodes_that_change_topology() -> None:
    """A layer stored by Geometry Nodes after a Subdivide Mesh: the view is
    evaluated last, so it colours the subdivided faces."""
    _clear()
    obj = _grid(n=2)
    tree = bpy.data.node_groups.new("Store slope", "GeometryNodeTree")
    tree.is_modifier = True
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    sub = nodes.new("GeometryNodeSubdivideMesh")
    links.new(nodes.new("NodeGroupInput").outputs[0], sub.inputs["Mesh"])
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT", "FACE"
    store.inputs["Name"].default_value = "slope"
    links.new(sub.outputs["Mesh"], store.inputs["Geometry"])
    links.new(nodes.new("GeometryNodeInputIndex").outputs[0], store.inputs["Value"])
    links.new(store.outputs["Geometry"], nodes.new("NodeGroupOutput").inputs[0])
    user = obj.modifiers.new("Slope", "NODES")
    user.node_group = tree
    item = _layer(source_attribute="slope", range_min="0", range_max="15")
    _show(item)
    mesh = _evaluated(obj)
    assert len(mesh.polygons) == 16
    want = _ramps().apply_ramp(np.arange(16, dtype=np.float32), 0.0, 15.0, "viridis")
    _assert_close(_view_bytes(obj), _per_corner(mesh, want), "subdivided")
    # The view is pinned last: a modifier added later goes before it.
    obj.modifiers.new("Weld", "WELD")
    assert obj.modifiers[-1].name == "GeoTools View"
    # The user's group is never edited.
    assert len(tree.nodes) == 5 and "geotools_view" not in tree
    _show(item, False)
    assert [m.name for m in obj.modifiers] == ["Slope", "Weld"]
    assert "Store slope" in bpy.data.node_groups


# --- texture layers ---------------------------------------------------------------------------


def _bake_emission(obj: bpy.types.Object, size: int) -> np.ndarray:
    """Cycles-bake ``obj``'s emission into a float image: linear RGBA per texel.

    A bake renders, so the view modifier is let into the render meanwhile, and
    every material on the object gets the target image (Cycles asks for one)."""
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 1
    target = bpy.data.images.new("bake", size, size, float_buffer=True)
    materials = [s.material for s in obj.material_slots if s.material is not None]
    materials.append(bpy.data.materials[f"GeoTools View shade {obj.name}"])
    added = []
    for material in materials:
        node = material.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = target
        material.node_tree.nodes.active = node
        added.append((material, node))
    (modifier,) = _view_modifiers(obj)
    modifier.show_render = True
    bpy.context.view_layer.update()
    for other in bpy.context.scene.objects:
        other.select_set(other == obj)
    bpy.context.view_layer.objects.active = obj
    try:
        result, error = h.call_op(bpy.ops.object.bake, type="EMIT", margin=0)
        assert result == {"FINISHED"}, (result, error)
        pixels = np.empty(size * size * 4, np.float32)
        target.pixels.foreach_get(pixels)
    finally:
        modifier.show_render = False
        for material, node in added:
            material.node_tree.nodes.remove(node)
        bpy.data.images.remove(target)
    return pixels.reshape(-1, 4)


def test_texture_layer_view_samples_the_image_through_the_ramp() -> None:
    _clear()
    a = _grid("A", n=1)
    b = _grid("B", n=1, offset=3.0)
    a.data.materials.append(_material("Mine"))
    item = _layer(
        "shade", domain="TEXTURE", nodata=-1.0, target="EMISSIVE", range_min="0", range_max="1"
    )
    item.resolution = 4
    values = np.linspace(-0.2, 1.2, 16).astype(np.float32)
    values[5] = -1.0  # Cycles reads NaN texels as 0, so the bake checks a no-data value
    for obj in (a, b):
        image = _layers().ensure_layer_image(item, obj)
        image.pixels.foreach_set(np.repeat(values, 4))
    _show(item)
    groups, materials = _view_blocks()
    assert groups == ["GeoTools View shade", "GeoTools View shade Ramp"], groups
    assert materials == ["GeoTools View shade A", "GeoTools View shade B"], materials
    for shading in _view3d_shadings():  # a texture layer shows in Material Preview
        assert shading.color_type != "VERTEX"
    assert "geotools_view_active" not in a.data
    assert [s.material.name for s in a.material_slots] == ["Mine"]
    # Each object's view material samples its own image.
    for obj in (a, b):
        material = bpy.data.materials[f"GeoTools View shade {obj.name}"]
        image = next(
            n.image for n in material.node_tree.nodes if n.bl_idname == "ShaderNodeTexImage"
        )
        assert image == _layers().layer_image(item, obj)
    baked = _bake_emission(a, 4)
    want_bytes = _ramps().apply_ramp(
        values, 0.0, 1.0, "viridis", nodata=-1.0, nodata_rgba=(128, 128, 128, 255)
    )
    srgb = np.where(
        baked[:, :3] <= 0.0031308, baked[:, :3] * 12.92, 1.055 * baked[:, :3] ** (1 / 2.4) - 0.055
    )
    _assert_close(np.round(srgb * 255.0), want_bytes[:, :3], "texture ramp")
    _show(item, False)
    assert _view_blocks() == ([], [])
    assert [s.material.name for s in a.material_slots] == ["Mine"]


# --- saving, undo and publishing ----------------------------------------------------------------


@contextmanager
def _tmpdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-view-test-"))
    try:
        yield path
    finally:
        h.submodule("blender.publish").shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


def test_view_survives_save_and_reload() -> None:
    _clear()
    obj = _grid()
    item = _layer(range_min="0", range_max="15")
    _write(obj, item, np.arange(16, dtype=np.float32))
    _show(item)
    want = _view_bytes(obj)
    with _tmpdir() as tmp:
        path = tmp / "view.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True)
        bpy.ops.wm.open_mainfile(filepath=str(path))
        try:
            obj = bpy.data.objects["Grid"]
            item = _layers().find_layer(bpy.context.scene, "slope")
            assert item.presentation.show_in_viewport
            assert len(_view_modifiers(obj)) == 1
            np.testing.assert_array_equal(_view_bytes(obj), want)
            _show(item, False)
            assert _view_modifiers(obj) == []
            assert _view_blocks() == ([], []), [
                (b.name, b.users) for b in (*bpy.data.node_groups, *bpy.data.materials)
            ]
            assert obj.data.color_attributes.active_color_name == ""
        finally:
            bpy.ops.wm.read_homefile(app_template="")
            h.ensure_enabled()


def test_undo_takes_the_view_off_again() -> None:
    _clear()
    obj = _grid()
    item = _layer(range_min="0", range_max="15")
    _write(obj, item, np.arange(16, dtype=np.float32))
    shadings = _view3d_shadings()
    before = [s.color_type for s in shadings]
    if not bpy.ops.ed.undo_push.poll() or not bpy.ops.ed.undo.poll():
        print("undo: background Blender has no undo stack; checking the handler")
        # Background Blender has no undo stack: check the handler alone.
        _show(item)
        for shading in shadings:
            shading.color_type = "VERTEX"
        item.presentation["show_in_viewport"] = False  # what undo restores, no update
        _view()._on_undo()
        assert [s.color_type for s in shadings] == before
        _show(item, False)
        return
    bpy.ops.ed.undo_push(message="before")
    _show(item)
    bpy.ops.ed.undo_push(message="view on")
    assert bpy.ops.ed.undo() == {"FINISHED"}
    obj = bpy.data.objects["Grid"]
    assert _view_modifiers(obj) == []
    assert [s.color_type for s in _view3d_shadings()] == before


def _publish_bytes(tmp: Path, name: str, output_type: str = "3TZ") -> dict[str, bytes]:
    publish = h.submodule("blender.publish")
    props = h.project()
    profile = props.build_profiles[0]
    suffix = ".glb" if output_type == "GLB" else ".3tz"
    profile.output_type = output_type
    profile.output_path = str(tmp / name / f"site{suffix}")
    assert bpy.ops.geotools.publish() == {"FINISHED"}
    job = publish.active_job()
    assert job is not None and job.join(JOIN_SECONDS), "the publish did not finish"
    publish.update_from_job()
    assert job.state.name == "DONE", job.error
    output = Path(profile.output_path)
    return {output.name: output.read_bytes()}


def test_publish_is_byte_identical_with_the_view_on() -> None:
    if h.CONFIG.expect != "available":
        return
    _clear()
    obj = _grid()
    obj.data.materials.append(_material("Flat"))
    obj.data.color_attributes.new("Paint", "BYTE_COLOR", "CORNER")
    props = h.project()
    props.placement_mode = "ANCHOR"
    p = h.submodule("blender.properties")
    p.write_number(props, "anchor_lon", 153.02)
    p.write_number(props, "anchor_lat", -27.47)
    item = _layer(range_min="0", range_max="15")
    _write(obj, item, np.arange(16, dtype=np.float32))
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    props.build_profiles[0].profile = "LOSSLESS"
    with _tmpdir() as tmp:
        off = _publish_bytes(tmp, "off")
        off_glb = _publish_bytes(tmp, "off-glb", "GLB")
        _show(item)
        modifiers = [m.name for m in obj.modifiers]
        time.sleep(1.1)  # a different build time must not matter
        on = _publish_bytes(tmp, "on")
        on_glb = _publish_bytes(tmp, "on-glb", "GLB")
        assert on == off, "the view changed the published tileset"
        assert on_glb == off_glb, "the view changed the published GLB"
        # The view is back after the publish, unchanged.
        assert [m.name for m in obj.modifiers] == modifiers
        assert all(m.show_viewport for m in _view_modifiers(obj))
        assert obj.data.color_attributes.active_color_name == "gt_view"
        record = json.loads((tmp / "on" / "site.geotools" / "build.json").read_text())
        assert record["validation"]["ok"] is True
    _show(item, False)
