"""The texture-space bake: values, selected-to-active, gutter, UV overlap, restore, persistence."""

from __future__ import annotations

import contextlib
import json
import math
import tempfile
from collections.abc import Iterator
from pathlib import Path

import _harness as h
import bpy
import numpy as np

QUADS = 4
SIZE = 16  # image resolution: quad i covers texel columns 4i..4i+3, rows 0..7
MARGIN = 2
FACE_VALUES = np.array([-2.5, 0.123456789, 7.0, 1000.125], np.float32)


def _core():
    return h.submodule("core.layers")


def _layers():
    return h.submodule("blender.layers")


def _bake():
    return h.submodule("blender.bake_texture")


def _strip(name: str, overlap: bool = False) -> bpy.types.Mesh:
    """A strip of QUADS unit quads along X; quad i maps to u in [i/4, (i+1)/4], v in [0, 0.5].

    With ``overlap`` the last quad reuses the first quad's UVs.
    """
    verts = [(float(x), float(y), 0.0) for y in (0, 1) for x in range(QUADS + 1)]
    faces = [(i, i + 1, i + QUADS + 2, i + QUADS + 1) for i in range(QUADS)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    uv = mesh.uv_layers.new(name="UVMap")
    corners = []
    for i in range(QUADS):
        j = 0 if overlap and i == QUADS - 1 else i
        corners += [
            (j / QUADS, 0.0),
            ((j + 1) / QUADS, 0.0),
            ((j + 1) / QUADS, 0.5),
            (j / QUADS, 0.5),
        ]
    uv.data.foreach_set("uv", np.array(corners, np.float32).ravel())
    return mesh


def _source(overlap: bool = False) -> bpy.types.Object:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    obj = bpy.data.objects.new("Strip", _strip("Strip", overlap))
    collection.objects.link(obj)
    return obj


def _registry(obj: bpy.types.Object, *, nodata=None) -> None:
    """A FACE layer "slope" holding FACE_VALUES, and a TEXTURE layer "slope_tex"."""
    c, L = _core(), _layers()
    scene = bpy.context.scene
    slope = c.LayerSpec("slope", c.Domain.FACE, c.DataType.FLOAT, units="deg")
    L.ensure_layer(scene, slope)
    L.write_layer(obj.data, slope, FACE_VALUES)
    tex = c.LayerSpec("slope_tex", c.Domain.TEXTURE, c.DataType.FLOAT, nodata=nodata)
    item = L.ensure_layer(scene, tex)
    item.uv_map = "UVMap"
    item.resolution = SIZE
    item.margin = MARGIN
    h.project().layers_index = len(h.project().layers) - 1


@contextlib.contextmanager
def _saved_file() -> Iterator[Path]:
    """A clean scene saved in a temporary folder, so layer images can be saved next to it."""
    assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
    h.reset_scene()
    try:
        with tempfile.TemporaryDirectory(prefix="geotools-bake-") as tmp:
            path = Path(tmp) / "bake.blend"
            assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}
            yield path
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()


def _pixels(image: bpy.types.Image) -> np.ndarray:
    pixels = np.empty(len(image.pixels), np.float32)
    image.pixels.foreach_get(pixels)
    return pixels.reshape(image.size[1], image.size[0], 4)


def _bake_op(**kwargs):
    return h.call_op(bpy.ops.geotools.bake_texture_layer, **kwargs)


def _gn(name: str, *, subdivide: int = 0, domain: str = "FACE") -> bpy.types.NodeTree:
    """Store "g": face index * 0.5 on faces, or position X on points (after subdividing)."""
    tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_out = nodes.new("NodeGroupOutput")
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type = "FLOAT"
    store.domain = domain
    store.inputs["Name"].default_value = "g"
    geometry = group_in.outputs[0]
    if subdivide:
        sub = nodes.new("GeometryNodeSubdivideMesh")
        sub.inputs["Level"].default_value = subdivide
        links.new(geometry, sub.inputs["Mesh"])
        geometry = sub.outputs[0]
    links.new(geometry, store.inputs["Geometry"])
    if domain == "FACE":
        index = nodes.new("GeometryNodeInputIndex")
        half = nodes.new("ShaderNodeMath")
        half.operation = "MULTIPLY"
        half.inputs[1].default_value = 0.5
        links.new(index.outputs[0], half.inputs[0])
        links.new(half.outputs[0], store.inputs["Value"])
    else:
        position = nodes.new("GeometryNodeInputPosition")
        split = nodes.new("ShaderNodeSeparateXYZ")
        links.new(position.outputs[0], split.inputs[0])
        links.new(split.outputs[0], store.inputs["Value"])
    links.new(store.outputs[0], group_out.inputs[0])
    return tree


def _provenance(item) -> dict:
    return json.loads(item.provenance)


def _no_temporaries() -> None:
    prefix = _bake().TEMP_PREFIX
    for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.materials):
        names = [block.name for block in collection if block.name.startswith(prefix)]
        assert not names, names


# --- registration and UI ------------------------------------------------------------------


def test_registered_and_panel_draws() -> None:
    assert h.operator_available("geotools.bake_texture_layer")
    assert h.operator_available("geotools.check_layer_uvs")
    panel = _bake().GEOTOOLS_PT_layers_bake_texture
    assert panel.is_registered and panel.bl_parent_id == "GEOTOOLS_PT_layers"
    h.reset_scene()
    obj = _source()
    _registry(obj)
    assert panel.poll(bpy.context)
    calls = h.draw(panel)
    assert ("operator", "geotools.bake_texture_layer", "RENDER_STILL") in calls
    assert ("label", "Not baked yet", "INFO") in calls
    h.project().layers_index = 0  # the FACE layer: no texture bake panel
    assert not panel.poll(bpy.context)
    h.reset_scene()


# --- values -------------------------------------------------------------------------------


def test_bake_face_layer_values_and_gutter() -> None:
    with _saved_file() as path:
        obj = _source()
        _registry(obj)
        result, error = _bake_op(source_layer="slope")
        assert result == {"FINISHED"}, error
        item = _layers().find_layer(bpy.context.scene, "slope_tex")
        image = _layers().layer_image(item, obj)
        assert image is not None and tuple(image.size) == (SIZE, SIZE)
        p = _pixels(image)
        for i, value in enumerate(FACE_VALUES):
            cols = slice(4 * i, 4 * i + 4)
            # Every texel of quad i holds its value exactly, in R, G and B.
            assert np.array_equal(p[0:8, cols, :3], np.full((8, 4, 3), value)), (i, p[0:8, cols, 0])
            # The gutter: the first row above the island copies the face below.
            assert np.array_equal(p[8, 4 * i + 1 : 4 * i + 3, 0], [value, value])
        assert np.isfinite(p[8 : 8 + MARGIN, :, 0]).all()  # MARGIN rows of gutter
        assert np.isnan(p[8 + MARGIN :, :, 0]).all()  # beyond it: no-data (NaN)
        assert (p[..., 3] == 1).all()
        # Saved as EXR next to the .blend; provenance and range set.
        exr = path.parent / "geotools_layers" / "slope_tex__Strip.exr"
        assert exr.is_file() and image.file_format == "OPEN_EXR"
        assert image.filepath == "//geotools_layers/slope_tex__Strip.exr"
        prov = _provenance(item)
        assert prov["tool"] == "geotools.bake_texture_layer"
        assert prov["parameters"]["attribute"] == "gt_slope"
        assert prov["parameters"]["methods"] == {"Strip": "DIRECT"}
        assert prov["parameters"]["margin"] == MARGIN
        assert (float(item.value_min), float(item.value_max)) == (-2.5, 1000.125)
        assert _layers().layer_status(bpy.context.scene, item) == []
        _no_temporaries()


def test_bake_maps_nan_and_nodata() -> None:
    with _saved_file():
        obj = _source()
        _registry(obj, nodata=-9999.0)
        L = _layers()
        slope = L.spec_from_props(L.find_layer(bpy.context.scene, "slope"))
        values = FACE_VALUES.copy()
        values[1] = np.nan
        values[2] = np.inf
        L.write_layer(obj.data, slope, values)
        result, error = _bake_op(source_layer="slope")
        assert result == {"FINISHED"}, error
        item = L.find_layer(bpy.context.scene, "slope_tex")
        p = _pixels(L.layer_image(item, obj))
        assert (p[0:8, 0:4, 0] == -2.5).all()
        assert (p[0:8, 4:12, 0] == -9999.0).all()  # NaN and inf become the layer's no-data
        assert (p[0:8, 12:16, 0] == 1000.125).all()
        assert (p[8 + MARGIN :, :, 0] == -9999.0).all()
        assert (float(item.value_min), float(item.value_max)) == (-2.5, 1000.125)


def test_bake_geometry_nodes_attribute_directly() -> None:
    with _saved_file():
        obj = _source()
        _registry(obj)
        modifier = obj.modifiers.new("GN", "NODES")
        modifier.node_group = _gn("store")
        assert "g" not in obj.data.attributes
        result, error = _bake_op(attribute="g")
        assert result == {"FINISHED"}, error
        item = _layers().find_layer(bpy.context.scene, "slope_tex")
        p = _pixels(_layers().layer_image(item, obj))
        for i in range(QUADS):
            assert (p[0:8, 4 * i : 4 * i + 4, 0] == 0.5 * i).all(), (i, p[0:8, 4 * i, 0])
        prov = _provenance(item)
        assert prov["parameters"]["methods"] == {"Strip": "DIRECT"}
        assert item.origin == "GEONODES"
        assert modifier.show_viewport and modifier.show_render


def test_bake_selected_to_active_from_subdivided_geometry_nodes() -> None:
    with _saved_file():
        obj = _source()
        _registry(obj)
        modifier = obj.modifiers.new("GN", "NODES")
        modifier.node_group = _gn("subdivide", subdivide=2, domain="POINT")
        depsgraph = bpy.context.evaluated_depsgraph_get()
        assert len(obj.evaluated_get(depsgraph).data.polygons) == QUADS * 16
        result, error = _bake_op(attribute="g")
        assert result == {"FINISHED"}, error
        item = _layers().find_layer(bpy.context.scene, "slope_tex")
        assert _provenance(item)["parameters"]["methods"] == {"Strip": "SELECTED_TO_ACTIVE"}
        p = _pixels(_layers().layer_image(item, obj))
        # g is position X, and X = 4 u on the strip: texel column c is at X = (c + 0.5) / 4.
        expected = (np.arange(SIZE) + 0.5) / 4
        assert np.allclose(p[0:8, :, 0], expected[None, :], atol=1e-2), p[4, :, 0]
        assert np.isnan(p[8 + MARGIN :, :, 0]).all()
        # The modifier is back on, and the temporary source is gone.
        assert modifier.show_viewport and modifier.show_render
        _no_temporaries()
        assert [o.name for o in bpy.context.scene.objects] == ["Strip"]


# --- UV overlap ---------------------------------------------------------------------------


def test_overlap_refused_then_baked_anyway() -> None:
    with _saved_file():
        obj = _source(overlap=True)
        _registry(obj)
        scene = bpy.context.scene
        L = _layers()
        result, error = _bake_op(source_layer="slope")
        assert result == {"ERROR_REPORTED"}
        assert "Refused" in error and "Bake Anyway" in error, error
        assert "Strip (UV map UVMap)" in error and f"at {SIZE}x{SIZE}" in error, error
        item = L.find_layer(scene, "slope_tex")
        assert L.layer_image(item, obj) is None and not len(item.images)
        assert not item.provenance
        _no_temporaries()

        # The Checks panel reports it as a warning.
        issues = h.submodule("blender.checks").run_checks(bpy.context)
        overlap = [i for i in issues if i.code == "uv-overlap"]
        assert len(overlap) == 1 and overlap[0].subject == "Strip", issues
        assert overlap[0].severity.name == "WARNING"
        assert "32 texels" in overlap[0].message, overlap[0].message  # quad 0: 4 x 8 texels
        assert "4 of 8 triangles" in overlap[0].message
        assert "faces 0, 3" in overlap[0].message
        result, error = h.call_op(bpy.ops.geotools.check_layer_uvs)
        assert result == {"FINISHED"}

        result, error = _bake_op(source_layer="slope", bake_anyway=True)
        assert result == {"FINISHED"}, error
        item = L.find_layer(scene, "slope_tex")
        image = L.layer_image(item, obj)
        assert image is not None
        p = _pixels(image)
        assert set(np.unique(p[0:8, 0:4, 0])) <= {FACE_VALUES[0], FACE_VALUES[3]}
        # Nothing maps to quad 3's old place now: beyond quad 2's gutter it is no-data.
        assert np.isnan(p[0:8, 12 + MARGIN :, 0]).all()
        assert (p[0:8, 12 : 12 + MARGIN, 0] == FACE_VALUES[2]).all()
        assert _provenance(item)["parameters"]["bake_anyway"] is True


def test_no_overlap_check_without_texture_layers() -> None:
    h.reset_scene()
    _source(overlap=True)
    issues = h.submodule("blender.checks").run_checks(bpy.context)
    assert "uv-overlap" not in [i.code for i in issues]
    h.reset_scene()


# --- restore ------------------------------------------------------------------------------


def _scene_state(obj: bpy.types.Object) -> dict[str, object]:
    scene = bpy.context.scene
    bake = scene.render.bake
    state = {name: getattr(bake, name) for name in _bake()._BAKE_SETTINGS}
    state.update(
        {f"cycles.{name}": getattr(scene.cycles, name) for name in _bake()._CYCLES_SETTINGS}
    )
    state["engine"] = scene.render.engine
    state["selected"] = sorted(o.name for o in bpy.context.view_layer.objects.selected)
    state["active"] = bpy.context.view_layer.objects.active.name
    state["slots"] = [(s.link, s.material.name if s.material else None) for s in obj.material_slots]
    state["materials"] = sorted(m.name for m in bpy.data.materials)
    state["modifiers"] = [(m.show_viewport, m.show_render) for m in obj.modifiers]
    state["hidden"] = (obj.hide_get(), obj.hide_render)
    return state


def _odd_settings(obj: bpy.types.Object) -> bpy.types.Object:
    """Settings unlike the bake's; returns another object, selected and active."""
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.cycles.samples = 77
    scene.cycles.use_adaptive_sampling = True
    scene.cycles.use_denoising = True
    scene.cycles.bake_type = "DIFFUSE"
    bake = scene.render.bake
    bake.margin = 5
    bake.margin_type = "ADJACENT_FACES"
    bake.use_clear = True
    bake.use_selected_to_active = False
    bake.cage_extrusion = 0.25
    bake.max_ray_distance = 0.5
    material = bpy.data.materials.new("Ground")
    obj.data.materials.append(material)  # a DATA-linked slot
    obj.hide_render = True
    other = bpy.data.objects.new("Other", bpy.data.meshes.new("Other"))
    bpy.context.scene.collection.objects.link(other)
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    other.select_set(True)
    bpy.context.view_layer.objects.active = other
    return other


def test_every_setting_restored() -> None:
    with _saved_file():
        obj = _source()
        _registry(obj)
        modifier = obj.modifiers.new("GN", "NODES")
        modifier.node_group = _gn("subdivide", subdivide=1, domain="POINT")
        _odd_settings(obj)
        before = _scene_state(obj)
        for kwargs in ({"source_layer": "slope"}, {"attribute": "g"}):  # direct, S2A
            result, error = _bake_op(**kwargs)
            assert result == {"FINISHED"}, (kwargs, error)
            assert _scene_state(obj) == before, kwargs
            _no_temporaries()
        assert _provenance(_layers().find_layer(bpy.context.scene, "slope_tex"))["parameters"][
            "methods"
        ] == {"Strip": "SELECTED_TO_ACTIVE"}


def test_every_setting_restored_when_the_bake_raises() -> None:
    bake = _bake()
    real = bake._run_bake
    with _saved_file():
        obj = _source()
        _registry(obj)
        modifier = obj.modifiers.new("GN", "NODES")
        modifier.node_group = _gn("subdivide", subdivide=1, domain="POINT")
        _odd_settings(obj)
        before = _scene_state(obj)
        L = _layers()
        scene = bpy.context.scene
        try:
            for error_type in (RuntimeError, KeyError):  # reported, and unexpected

                def fail(**_kwargs: object) -> set[str]:
                    raise error_type("bake exploded")  # noqa: B023

                bake._run_bake = fail
                for kwargs in ({"source_layer": "slope"}, {"attribute": "g"}):
                    result, error = _bake_op(**kwargs)
                    assert result == {"ERROR_REPORTED"}, (error_type, kwargs)
                    assert "bake exploded" in error, error
                    assert _scene_state(obj) == before, (error_type, kwargs)
                    _no_temporaries()
                    item = L.find_layer(scene, "slope_tex")
                    assert not len(item.images), "a failed bake left a new image"
                    assert not item.provenance
            # An image from an earlier bake is kept, reloaded from its file.
            bake._run_bake = real
            assert _bake_op(source_layer="slope")[0] == {"FINISHED"}
            image = L.layer_image(L.find_layer(scene, "slope_tex"), obj)
            baked = _pixels(image).copy()
            bake._run_bake = fail
            assert _bake_op(source_layer="slope")[0] == {"ERROR_REPORTED"}
            item = L.find_layer(scene, "slope_tex")
            assert L.layer_image(item, obj) == image
            assert np.array_equal(_pixels(image), baked, equal_nan=True)
            assert _scene_state(obj) == before
        finally:
            bake._run_bake = real


def test_needs_a_saved_blend_and_a_source() -> None:
    assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
    h.reset_scene()
    try:
        obj = _source()
        _registry(obj)
        result, error = _bake_op(source_layer="slope")
        assert result == {"ERROR_REPORTED"} and "Save the .blend" in error, error
        assert not len(_layers().find_layer(bpy.context.scene, "slope_tex").images)
        result, error = _bake_op(layer="slope", source_layer="slope")
        assert result == {"ERROR_REPORTED"} and "not a texture layer" in error, error
        result, error = _bake_op()
        assert result == {"ERROR_REPORTED"}, error  # unsaved, and no source chosen
    finally:
        h.reset_scene()


# --- persistence --------------------------------------------------------------------------


def test_baked_images_survive_save_and_reopen() -> None:
    with _saved_file() as path:
        obj = _source()
        _registry(obj)
        assert _bake_op(source_layer="slope")[0] == {"FINISHED"}
        L = _layers()
        item = L.find_layer(bpy.context.scene, "slope_tex")
        baked = _pixels(L.layer_image(item, obj)).copy()
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}

        scene = bpy.context.scene
        item = L.find_layer(scene, "slope_tex")
        obj = scene.objects["Strip"]
        image = L.layer_image(item, obj)
        assert image is not None and image.file_format == "OPEN_EXR"
        assert Path(bpy.path.abspath(image.filepath)).is_file()
        back = _pixels(image)
        assert np.array_equal(back, baked, equal_nan=True), np.nanmax(np.abs(back - baked))
        assert L.layer_status(scene, item) == []
        assert _provenance(item)["tool"] == "geotools.bake_texture_layer"
        assert not math.isnan(float(item.value_min))
