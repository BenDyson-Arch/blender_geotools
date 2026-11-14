"""Layer presentation and variant tilesets (``blender.presentation``, design section 4).

Ramps into COLOR_0 colours and the ``gt_present`` preview, no-data and category
colours, texture presentations, what the exporter writes for each target and
blend mode (decoded from the GLB), the user's data left alone, layers computed
by Geometry Nodes (also changing the topology), Apply Modifiers off, and the
variant publish: naming, staging, the build.json record and, with
rusty-tiles, end-to-end variants whose leaf COLOR_0 shows the ramp.
"""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import _harness as h
import bpy
import numpy as np

JOIN_SECONDS = 180.0


# --- helpers -----------------------------------------------------------------------------


def _present():
    return h.submodule("blender.presentation")


def _layers():
    return h.submodule("blender.layers")


def _core_layers():
    return h.submodule("core.layers")


def _ramps():
    return h.submodule("core.ramps")


def _export():
    return h.submodule("blender.export")


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.get("Source")
    if collection is None:
        collection = bpy.data.collections.new("Source")
        bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _grid(name: str = "Grid", n: int = 3, offset: float = 0.0) -> bpy.types.Object:
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
    uv.data.foreach_set("uv", (co[quads.ravel(), :2] / (n + offset + 1)).astype(np.float32).ravel())
    mesh.update()
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    _source().objects.link(obj)
    return obj


def _material(name: str, rgb=(0.2, 0.4, 0.6), image: bpy.types.Image | None = None):
    material = bpy.data.materials.new(name)
    if material.node_tree is None:
        material.use_nodes = True
    tree = material.node_tree
    bsdf = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    if image is not None:
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return material


def _material_state(material: bpy.types.Material) -> tuple:
    tree = material.node_tree
    bsdf = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    return (
        material.name,
        len(tree.nodes),
        sorted((link.from_node.name, link.to_socket.identifier) for link in tree.links),
        tuple(round(v, 6) for v in bsdf.inputs["Base Color"].default_value),
        tuple(round(v, 6) for v in bsdf.inputs["Emission Color"].default_value),
    )


def _image(name: str, rgba=(0.9, 0.1, 0.1, 1.0), size: int = 4) -> bpy.types.Image:
    image = bpy.data.images.new(name, size, size)
    image.pixels.foreach_set(np.tile(np.array(rgba, np.float32), size * size))
    image.pack()
    return image


def _layer(spec_kwargs: dict, **presentation: object):
    """Register a layer and set its presentation; returns its name."""
    core = _core_layers()
    spec = core.LayerSpec(**spec_kwargs)
    _layers().ensure_layer(bpy.context.scene, spec)
    item = _layers().find_layer(bpy.context.scene, spec.name)
    for key, value in presentation.items():
        setattr(item.presentation, key, value)
    return spec.name


def _spec(name: str):
    return _layers().spec_from_props(_layers().find_layer(bpy.context.scene, name))


def _loop_totals(mesh: bpy.types.Mesh) -> np.ndarray:
    totals = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", totals)
    return totals


def _present_bytes(mesh: bpy.types.Mesh) -> np.ndarray:
    attr = mesh.attributes["gt_present"]
    assert (attr.domain, attr.data_type) == ("CORNER", "BYTE_COLOR")
    values = np.empty(len(mesh.loops) * 4, dtype=np.float32)
    attr.data.foreach_get("color_srgb", values)
    return np.round(values.reshape(-1, 4) * 255.0).astype(np.uint8)


def _image_bytes(image: bpy.types.Image) -> np.ndarray:
    values = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(values)
    return np.round(values.reshape(-1, 4) * 255.0).astype(np.uint8)


def _png_bytes(path: Path) -> np.ndarray:
    image = bpy.data.images.load(str(path), check_existing=False)
    try:
        return _image_bytes(image)
    finally:
        bpy.data.images.remove(image)


def _srgb_to_linear(rgba: np.ndarray) -> np.ndarray:
    c = rgba[..., :3].astype(np.float64) / 255.0
    rgb = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return np.concatenate([rgb, rgba[..., 3:].astype(np.float64) / 255.0], axis=-1)


def _linear_to_srgb_bytes(rgba: np.ndarray) -> np.ndarray:
    c = np.clip(rgba[..., :3].astype(np.float64), 0.0, 1.0)
    srgb = np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1.0 / 2.4) - 0.055)
    return np.round(srgb * 255.0).astype(np.int64)


@contextmanager
def _tmpdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-present-test-"))
    try:
        yield path
    finally:
        h.submodule("blender.publish").shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


def _extract(presentation=None, image_dir: Path | None = None):
    bpy.context.view_layer.update()
    return _export().extract_scene(
        bpy.context, _source(), image_dir or Path(tempfile.mkdtemp()), presentation=presentation
    )


def _read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    json_length = struct.unpack_from("<I", data, 12)[0]
    document = json.loads(data[20 : 20 + json_length])
    return document, data[20 + json_length + 8 :]


def _accessor(document: dict, binary: bytes, index: int) -> np.ndarray:
    acc = document["accessors"][index]
    view = document["bufferViews"][acc["bufferView"]]
    width = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}[acc["type"]]
    dtype = {5126: "<f4", 5125: "<u4"}[acc["componentType"]]
    values = np.frombuffer(
        binary, dtype, count=acc["count"] * width, offset=view.get("byteOffset", 0)
    )
    return values.reshape(acc["count"], width) if width > 1 else values


# --- registration and UI ------------------------------------------------------------------


def test_presentation_registered() -> None:
    for idname in (
        "geotools.layer_write_presentation",
        "geotools.layer_presentation_reset_colors",
    ):
        assert h.operator_available(idname), idname
    assert "layer" in bpy.ops.geotools.publish.get_rna_type().properties
    layer_props = bpy.types.PropertyGroup.bl_rna_get_subclass_py("GeoToolsLayer")
    assert layer_props is not None and "presentation" in layer_props.bl_rna.properties
    panels = h.submodule("blender.panels")
    display = panels.GEOTOOLS_PT_layers_display
    assert display in panels.classes
    # Bake, then display: the workflow order.
    order = panels.classes.index
    assert order(panels.GEOTOOLS_PT_layers) < order(panels.GEOTOOLS_PT_layers_bake)
    assert order(display) == order(panels.GEOTOOLS_PT_layers_bake) + 1
    assert display.bl_parent_id == "GEOTOOLS_PT_layers"
    modules = [m.__name__.rpartition(".")[2] for m in h.submodule("blender").MODULES]
    assert modules.index("layers") < modules.index("presentation") < modules.index("publish")


def _display():
    return h.submodule("blender.panels").GEOTOOLS_PT_layers_display


def test_presentation_panel_draws() -> None:
    h.reset_scene()
    _grid()
    core = _core_layers()
    _layer({"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT})
    calls = h.draw(_display())
    props = h.props_drawn(calls)
    assert {"blend", "palette", "range_min", "range_max", "nodata_color"} <= set(props)
    assert "target" not in props  # a face layer has one target, so it is not shown
    assert ("operator", "geotools.layer_write_presentation", "BRUSH_DATA") in calls
    assert ("operator", "geotools.publish", "EXPORT") not in calls  # variants: Publish only
    assert ("label", "Publish: choose it as Variant", "INFO") in calls
    assert h.draw_header(_display()) is None

    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    profile = h.project().build_profiles[0]
    profile.output_path = "/tmp/site.3tz"
    profile.variant_layer = "slope"
    calls = h.draw(_display())
    assert ("label", "Variant: site.slope.3tz", "FILE_3D") in calls, calls
    profile.variant_layer = ""

    # A target that does not suit the domain is shown and flagged.
    alerts: list[str] = []
    item = _layers().find_layer(bpy.context.scene, "slope")
    item.presentation.target = "BASE_COLOR"
    calls = h.draw(_display(), alerts)
    assert "target" in alerts
    assert ("label", "Use Vertex Colour for this layer", "ERROR") in calls
    assert h.draw_header(_display()) == "CANCEL"
    item.presentation.target = "COLOR_0"

    # Categories list their colours instead of a palette.
    _layer(
        {
            "name": "cover",
            "domain": core.Domain.FACE,
            "data_type": core.DataType.CATEGORY,
            "categories": ("bare", "grass"),
        }
    )
    h.project().layers_index = 1
    assert bpy.ops.geotools.layer_presentation_reset_colors() == {"FINISHED"}
    calls = h.draw(_display())
    assert "palette" not in h.props_drawn(calls)
    assert h.props_drawn(calls).count("color") == 2
    assert ("label", "1  grass", "NONE") in calls

    # The variant is chosen in one place: the Publish panel, on the build profile.
    publish_panel = h.submodule("blender.panels").GEOTOOLS_PT_publish
    profile.variant_layer = "cover"
    alerts = []
    calls = h.draw(publish_panel, alerts)
    assert "variant_layer" in h.props_drawn(calls) and "variant_layer" not in alerts
    assert h.operator_text("geotools.publish") == "Publish site.cover.3tz"
    profile.variant_layer = "nope"
    h.draw(publish_panel, alerts)
    assert "variant_layer" in alerts
    profile.variant_layer = ""
    h.draw(publish_panel)
    assert h.operator_text("geotools.publish") == "Publish site.3tz"


def test_display_shows_auto_range() -> None:
    h.reset_scene()
    _grid()
    core = _core_layers()
    _layer({"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT})
    present = _present()
    item = _layers().find_layer(bpy.context.scene, "slope")
    assert present._auto_text(item, "min") == "Auto"
    item.auto_min, item.auto_max = repr(4.914712905883789), repr(36.55620574951172)
    assert present._auto_text(item, "min") == "Auto: 4.91"
    assert present._auto_text(item, "max") == "Auto: 36.6"
    item.data_type = "BOOLEAN"
    assert present._auto_text(item, "max") == "Auto: 1"


# --- COLOR_0: gt_present -----------------------------------------------------------------------


def test_face_ramp_to_gt_present() -> None:
    h.reset_scene()
    obj = _grid(n=3)
    core = _core_layers()
    name = _layer(
        {"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT},
        palette="magma",
        range_min="0",
        range_max="8.0",
    )
    values = np.arange(9, dtype=np.float32)
    _layers().write_layer(obj.data, _spec(name), values)
    presentation, record = _present().build_presentation(bpy.context.scene, name)
    assert presentation.target.value == "COLOR_0" and presentation.blend.value == "REPLACE"

    # The exporter gets one colour per face, in memory; the mesh is not changed.
    expected = _ramps().apply_ramp(values, 0.0, 8.0, "magma")
    assert presentation.domain == "FACE" and set(presentation.colors) == {"Grid"}
    np.testing.assert_array_equal(presentation.colors["Grid"], expected)
    assert "gt_present" not in obj.data.attributes
    assert record["layer"] == "slope" and record["palette"] == "magma"
    assert record["range"] == {"min": 0.0, "max": 8.0, "auto_min": False, "auto_max": False}
    assert record["ramp_stops"] == _ramps().ramp_stops("magma", 0.0, 8.0, 11)

    # The preview paints the same colours into gt_present, per corner.
    preview_record, skipped = _present().write_preview(bpy.context.scene, name)
    assert preview_record == record and skipped == []
    got = _present_bytes(obj.data)
    np.testing.assert_array_equal(got, np.repeat(expected, _loop_totals(obj.data), axis=0))

    # The operator does the same for the selected layer.
    obj.data.attributes.remove(obj.data.attributes["gt_present"])
    assert bpy.ops.geotools.layer_write_presentation() == {"FINISHED"}
    np.testing.assert_array_equal(_present_bytes(obj.data)[::4], expected)


def test_corner_ramp_nodata_and_auto_range() -> None:
    h.reset_scene()
    obj = _grid("Grid", n=2)
    bare = _grid("Bare", n=1, offset=5.0)  # no layer attribute: no-data colour
    core = _core_layers()
    name = _layer(
        {
            "name": "height",
            "domain": core.Domain.CORNER,
            "data_type": core.DataType.FLOAT,
            "nodata": -9999.0,
        },
        palette="greys",
        nodata_color=(1.0, 0.0, 0.0),
    )
    corners = len(obj.data.loops)
    values = np.linspace(0.0, 100.0, corners).astype(np.float32)
    values[0] = -9999.0
    values[1] = np.nan
    _layers().write_layer(obj.data, _spec(name), values)

    issues = _present().variant_issues(bpy.context.scene, name)
    assert [(i.severity, i.code) for i in issues] == [
        (h.submodule("core.issues").Severity.WARNING, "variant-data-missing")
    ], issues

    presentation, record = _present().build_presentation(bpy.context.scene, name)
    low, high = _ramps().auto_range(values, nodata=-9999.0)
    assert record["range"] == {"min": low, "max": high, "auto_min": True, "auto_max": True}
    assert record["nodata_rgba"] == [255, 0, 0, 255]
    # Bare has no values: the exporter paints it with the no-data colour.
    assert presentation.domain == "CORNER" and set(presentation.colors) == {"Grid"}
    assert presentation.nodata_rgba == (255, 0, 0, 255)
    _preview_record, skipped = _present().write_preview(bpy.context.scene, name)
    assert skipped == []
    got = _present_bytes(obj.data)
    np.testing.assert_array_equal(presentation.colors["Grid"], got)
    np.testing.assert_array_equal(got[:2], [[255, 0, 0, 255]] * 2)
    np.testing.assert_array_equal(got[2:], _ramps().apply_ramp(values[2:], low, high, "greys"))
    np.testing.assert_array_equal(
        _present_bytes(bare.data), np.tile([255, 0, 0, 255], (len(bare.data.loops), 1))
    )

    # One automatic end: the other is the user's.
    item = _layers().find_layer(bpy.context.scene, name)
    item.presentation.range_max = "200"
    _presentation, record = _present().build_presentation(bpy.context.scene, name)
    assert record["range"] == {"min": low, "max": 200.0, "auto_min": True, "auto_max": False}


def test_category_colours() -> None:
    h.reset_scene()
    obj = _grid(n=2)
    core = _core_layers()
    name = _layer(
        {
            "name": "cover",
            "domain": core.Domain.FACE,
            "data_type": core.DataType.CATEGORY,
            "categories": ("bare", "grass", "water"),
            "nodata": -1,
        },
        nodata_color=(0.0, 0.0, 0.0),
    )
    item = _layers().find_layer(bpy.context.scene, name)
    item.presentation.category_colors.add().color = (1.0, 0.0, 0.0)  # value 0
    item.presentation.category_colors.add().color = (0.0, 1.0, 0.0)  # value 1; 2 is default
    _layers().write_layer(obj.data, _spec(name), np.array([0, 1, 2, -1], dtype=np.int32))
    presentation, record = _present().build_presentation(bpy.context.scene, name)
    default = _ramps().default_category_colours(3)[2]
    per_face = presentation.colors["Grid"]
    np.testing.assert_array_equal(
        per_face, [[255, 0, 0, 255], [0, 255, 0, 255], default, [0, 0, 0, 255]]
    )
    assert record["palette"] is None and record["range"] is None
    assert [c["label"] for c in record["categories"]] == ["bare", "grass", "water"]
    assert record["categories"][2]["rgba"] == default.tolist()

    # Reset gives every category its default colour.
    h.project().layers_index = 0
    assert bpy.ops.geotools.layer_presentation_reset_colors() == {"FINISHED"}
    assert len(item.presentation.category_colors) == 3
    _presentation, record = _present().build_presentation(bpy.context.scene, name)
    expected = _ramps().default_category_colours(3)
    assert [c["rgba"] for c in record["categories"]] == expected.tolist()


def test_user_colours_and_materials_unchanged() -> None:
    h.reset_scene()
    painted = _grid("Painted", n=2)
    plain = _grid("Plain", n=1, offset=4.0)
    mesh = painted.data
    paint = mesh.color_attributes.new("Paint", "BYTE_COLOR", "CORNER")
    paint.data.foreach_set("color_srgb", np.tile([0.2, 0.4, 0.6, 1.0], len(mesh.loops)))
    mesh.color_attributes.new("Edit", "FLOAT_COLOR", "POINT")
    mesh.color_attributes.render_color_index = 0  # Paint renders
    mesh.color_attributes.active_color_index = 1  # Edit is being edited
    material = _material("Mat", image=_image("Albedo"))
    mesh.materials.append(material)
    before = (_material_state(material), tuple(m.name for m in bpy.data.materials))
    paint_before = np.empty(len(mesh.loops) * 4, np.float32)
    paint.data.foreach_get("color", paint_before)

    core = _core_layers()
    name = _layer(
        {"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT},
        range_min="0",
        range_max="1",
    )
    _layers().write_layer(mesh, _spec(name), np.linspace(0, 1, len(mesh.polygons)))
    _present().build_presentation(bpy.context.scene, name)
    _present().write_preview(bpy.context.scene, name)
    _present().write_preview(bpy.context.scene, name)  # again: replaces in place

    attrs = mesh.color_attributes
    assert (attrs.default_color_name, attrs.active_color_name) == ("Paint", "Edit")
    assert plain.data.color_attributes.default_color_name == ""
    assert plain.data.color_attributes.active_color_name == ""
    assert [a.name for a in attrs] == ["Paint", "Edit", "gt_present"]
    after = np.empty(len(mesh.loops) * 4, np.float32)
    paint.data.foreach_get("color", after)
    np.testing.assert_array_equal(after, paint_before)
    assert (_material_state(material), tuple(m.name for m in bpy.data.materials)) == before

    # The plain export never publishes gt_present.
    scene, _topology = _extract()
    by_material = {p.material: p for p in scene.primitives}
    painted_prim = by_material[0]
    np.testing.assert_allclose(painted_prim.color[0], paint_before[:4], atol=1e-6)
    plain_prim = by_material[None]
    assert plain_prim.color is None, "gt_present leaked into the plain tileset"
    attrs.render_color_index = 2  # even when the user renders it
    attrs.active_color_index = 2
    scene, _topology = _extract()
    np.testing.assert_allclose(
        {p.material: p for p in scene.primitives}[0].color[0], paint_before[:4], atol=1e-6
    )


def test_replace_and_multiply_in_the_glb() -> None:
    h.reset_scene()
    obj = _grid(n=3)
    material = _material("Flat", rgb=(0.2, 0.4, 0.6))
    obj.data.materials.append(material)
    textured = _grid("Textured", n=2, offset=5.0)
    textured.data.materials.append(_material("Tex", image=_image("Albedo")))
    core = _core_layers()
    name = _layer(
        {"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT},
        palette="viridis",
        range_min="0",
        range_max="8",
    )
    for o in (obj, textured):
        count = len(o.data.polygons)
        _layers().write_layer(o.data, _spec(name), np.arange(count, dtype=np.float32))
    presentation, _record = _present().build_presentation(bpy.context.scene, name)
    assert "gt_present" not in obj.data.attributes  # the export does not need it
    expected_faces = _ramps().apply_ramp(np.arange(9, dtype=np.float32), 0.0, 8.0, "viridis")

    writer = h.submodule("core.gltf_writer")
    Presentation = _export().Presentation
    for blend in ("REPLACE", "MULTIPLY"):
        with _tmpdir() as tmp:
            present = Presentation(
                "COLOR_0", blend, colors=presentation.colors, domain=presentation.domain
            )
            scene, _topology = _extract(present, tmp / "images")
            writer.write_glb(scene, tmp / "source.glb")
            document, binary = _read_glb(tmp / "source.glb")
            materials = {m["name"]: m for m in document["materials"]}
            flat = materials["Flat"]["pbrMetallicRoughness"]
            tex = materials["Tex"]["pbrMetallicRoughness"]
            if blend == "REPLACE":
                assert flat["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]
                assert tex["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]
                assert "baseColorTexture" not in tex
                assert "images" not in document
            else:
                np.testing.assert_allclose(flat["baseColorFactor"], [0.2, 0.4, 0.6, 1.0], atol=1e-6)
                assert "baseColorTexture" in tex
            for p, prim in enumerate(scene.primitives):
                gltf_prim = document["meshes"][0]["primitives"][p]
                color = _accessor(document, binary, gltf_prim["attributes"]["COLOR_0"])
                indices = _accessor(document, binary, gltf_prim["indices"]).reshape(-1, 3)
                face = prim.source_polygon  # one value per face: value = polygon index
                want = _srgb_to_linear(expected_faces[face])
                for corner in range(3):
                    np.testing.assert_allclose(color[indices[:, corner]], want, atol=2e-3)
                srgb = _linear_to_srgb_bytes(color[indices[:, 0]])
                np.testing.assert_array_equal(srgb, expected_faces[face][:, :3])
    assert presentation.images == {}


def _remove_attribute_tree(name: str, subdivide: bool = False) -> bpy.types.NodeTree:
    """A Geometry Nodes tree that removes attribute ``name``, then optionally subdivides."""
    tree = bpy.data.node_groups.new(f"Remove {name}", "GeometryNodeTree")
    tree.is_modifier = True
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    group_in = tree.nodes.new("NodeGroupInput")
    group_out = tree.nodes.new("NodeGroupOutput")
    remove = tree.nodes.new("GeometryNodeRemoveAttribute")
    remove.inputs["Name"].default_value = name
    tree.links.new(group_in.outputs[0], remove.inputs["Geometry"])
    last = remove.outputs["Geometry"]
    if subdivide:
        sub = tree.nodes.new("GeometryNodeSubdivideMesh")
        tree.links.new(last, sub.inputs["Mesh"])
        last = sub.outputs["Mesh"]
    tree.links.new(last, group_out.inputs[0])
    return tree


def test_modifiers_and_presentation() -> None:
    h.reset_scene()
    obj = _grid(n=2)
    core = _core_layers()
    name = _layer(
        {"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT},
        range_min="0",
        range_max="3",
    )
    _layers().write_layer(obj.data, _spec(name), np.arange(4, dtype=np.float32))
    presentation, _record = _present().build_presentation(bpy.context.scene, name)
    plain, _topology = _extract(presentation)
    reference = plain.primitives[0].color
    palette = _ramps().DEFAULT_PALETTE
    ramp = _srgb_to_linear(_ramps().apply_ramp(np.arange(4, dtype=np.float32), 0.0, 3.0, palette))

    # A Subdivision modifier carries gt_slope to the evaluated faces, each child
    # face keeping its parent's value; the presentation reads it there.
    sub = obj.modifiers.new("Subdivision", "SUBSURF")
    sub.levels = 1
    bpy.context.view_layer.update()
    try:
        _extract(presentation)
    except h.submodule("core.gltf_writer").ExportError as ex:
        assert "the mesh changed" in str(ex), ex  # built for 4 faces, 16 exported
    else:
        raise AssertionError("expected an ExportError for a stale presentation")
    presentation, _record = _present().build_presentation(bpy.context.scene, name)
    assert presentation.colors["Grid"].shape == (16, 4)
    scene, topology = _extract(presentation)
    assert topology["Grid"].startswith("evaluated:")
    prim = scene.primitives[0]
    assert prim.color is not None and prim.color.shape[0] > reference.shape[0]
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
    values = _layers().read_layer(evaluated, _spec(name))
    np.testing.assert_array_equal(np.sort(values), np.repeat(np.arange(4, dtype=np.float32), 4))
    # A preview cannot be painted on the original corners: skipped, not wrong.
    _preview, skipped = _present().write_preview(bpy.context.scene, name)
    assert skipped == ["Grid"] and "gt_present" not in obj.data.attributes
    obj.modifiers.remove(sub)

    # The export never reads gt_present: a stale or dropped preview changes nothing.
    presentation, _record = _present().build_presentation(bpy.context.scene, name)
    _present().write_preview(bpy.context.scene, name)
    stale = obj.data.attributes["gt_present"]
    stale.data.foreach_set("color_srgb", np.zeros(4 * len(obj.data.loops), np.float32))
    gn = obj.modifiers.new("Drop", "NODES")
    gn.node_group = _remove_attribute_tree("gt_present")
    scene, topology = _extract(presentation)
    assert not topology["Grid"].startswith("evaluated:")
    np.testing.assert_array_equal(scene.primitives[0].color, reference)
    np.testing.assert_allclose(np.unique(reference, axis=0), np.unique(ramp, axis=0), atol=2e-3)

    # A modifier that drops the layer itself: no values, so no variant.
    gn.node_group = _remove_attribute_tree("gt_slope", subdivide=True)
    bpy.context.view_layer.update()
    codes = [i.code for i in _present().variant_issues(bpy.context.scene, name)]
    assert codes == ["variant-data-missing", "variant-no-data"], codes
    try:
        _present().build_presentation(bpy.context.scene, name)
    except _present().PresentationError as ex:
        assert "gt_slope" in str(ex), ex
    else:
        raise AssertionError("expected a PresentationError")
    obj.modifiers.remove(gn)
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)


# --- textures ---------------------------------------------------------------------------------


def _texture_layer(objects: list[bpy.types.Object], **presentation) -> tuple[str, np.ndarray]:
    core = _core_layers()
    name = _layer(
        {"name": "shade", "domain": core.Domain.TEXTURE, "data_type": core.DataType.FLOAT},
        **presentation,
    )
    item = _layers().find_layer(bpy.context.scene, name)
    item.resolution = 4
    values = np.linspace(0.0, 1.0, 16).astype(np.float32)
    for obj in objects:
        image = _layers().ensure_layer_image(_layers().find_layer(bpy.context.scene, name), obj)
        image.pixels.foreach_set(np.repeat(values, 4))
    return name, values


def test_texture_layer_to_base_color_and_emissive() -> None:
    h.reset_scene()
    a = _grid("A", n=1)
    b = _grid("B", n=1, offset=3.0)
    albedo = _image("Albedo")
    shared = _material("Shared", image=albedo)
    flat = _material("Flat", rgb=(0.2, 0.4, 0.6))
    a.data.materials.append(shared)
    b.data.materials.append(shared)
    b.data.materials.append(flat)
    b.data.polygons[0].material_index = 1  # b's only face uses Flat
    paint = a.data.color_attributes.new("Paint", "BYTE_COLOR", "CORNER")
    paint.data.foreach_set("color_srgb", np.tile([0.5, 0.5, 0.5, 1.0], len(a.data.loops)))
    states = [_material_state(m) for m in (shared, flat)]
    albedo_pixels = _image_bytes(albedo)

    name, values = _texture_layer(
        [a, b], target="BASE_COLOR", palette="greys", range_min="0", range_max="1"
    )
    presentation, record = _present().build_presentation(bpy.context.scene, name)
    assert set(presentation.images) == {"A", "B"}
    expected = _ramps().apply_ramp(values, 0.0, 1.0, "greys")
    for image in presentation.images.values():
        assert not image.is_float and tuple(image.size) == (4, 4)
        assert image.colorspace_settings.name == "sRGB"
        np.testing.assert_array_equal(_image_bytes(image), expected)
    assert record["target"] == "BASE_COLOR"
    item = _layers().find_layer(bpy.context.scene, name)
    assert len(item.presentation.images) == 2

    Presentation = _export().Presentation
    writer = h.submodule("core.gltf_writer")
    with _tmpdir() as tmp:
        scene, _topology = _extract(presentation, tmp / "images")
        names = sorted(m.name for m in scene.materials)
        assert names == ["Flat [B]", "Shared [A]"], names  # one per object
        for material in scene.materials:
            assert material.base_color_factor == (1.0, 1.0, 1.0, 1.0)
            ref = material.base_color_texture
            assert ref is not None and ref.texcoord == 0
            np.testing.assert_array_equal(_png_bytes(scene.images[ref.image].path), expected)
        assert all(p.color is None for p in scene.primitives)  # REPLACE drops COLOR_0
        writer.write_glb(scene, tmp / "source.glb")
        document, _binary = _read_glb(tmp / "source.glb")
        for m in document["materials"]:
            assert m["pbrMetallicRoughness"]["baseColorFactor"] == [1.0, 1.0, 1.0, 1.0]
            assert "baseColorTexture" in m["pbrMetallicRoughness"]

        # MULTIPLY keeps the factor and COLOR_0.
        multiply = Presentation("BASE_COLOR", "MULTIPLY", images=presentation.images)
        scene, _topology = _extract(multiply, tmp / "images2")
        by_name = {m.name: m for m in scene.materials}
        np.testing.assert_allclose(
            by_name["Flat [B]"].base_color_factor, (0.2, 0.4, 0.6, 1.0), atol=1e-6
        )
        assert by_name["Flat [B]"].base_color_texture is not None
        assert any(p.color is not None for p in scene.primitives)

        # EMISSIVE REPLACE: the ramp shows unlit on black.
        emissive = Presentation("EMISSIVE", "REPLACE", images=presentation.images)
        scene, _topology = _extract(emissive, tmp / "images3")
        for material in scene.materials:
            assert material.emissive_factor == (1.0, 1.0, 1.0)
            assert material.base_color_factor == (0.0, 0.0, 0.0, 1.0)
            assert material.base_color_texture is None and material.metallic_factor == 0.0
            ref = material.emissive_texture
            np.testing.assert_array_equal(_png_bytes(scene.images[ref.image].path), expected)
        assert all(p.color is None for p in scene.primitives)
        writer.write_glb(scene, tmp / "emissive.glb")
        document, _binary = _read_glb(tmp / "emissive.glb")
        for m in document["materials"]:
            assert m["emissiveFactor"] == [1.0, 1.0, 1.0] and "emissiveTexture" in m

        # EMISSIVE MULTIPLY: the material stays, the ramp glows on top.
        glow = Presentation("EMISSIVE", "MULTIPLY", images=presentation.images)
        scene, _topology = _extract(glow, tmp / "images4")
        shared_a = {m.name: m for m in scene.materials}["Shared [A]"]
        assert shared_a.base_color_texture is not None and shared_a.emissive_texture is not None
        assert shared_a.base_color_texture.image != shared_a.emissive_texture.image
        np.testing.assert_array_equal(
            _png_bytes(scene.images[shared_a.base_color_texture.image].path)[:, :3],
            albedo_pixels[:, :3],
        )

    # The user's materials and images are untouched.
    assert [_material_state(m) for m in (shared, flat)] == states
    np.testing.assert_array_equal(_image_bytes(albedo), albedo_pixels)
    assert a.data.color_attributes.default_color_name == "Paint"

    # A texture layer cannot go into COLOR_0, nor a face layer into a texture.
    item.presentation.target = "COLOR_0"
    codes = [i.code for i in _present().variant_issues(bpy.context.scene, name)]
    assert "variant-target-invalid" in codes, codes


def test_variant_checks() -> None:
    h.reset_scene()
    obj = _grid(n=1)
    present = _present()
    codes = [i.code for i in present.variant_issues(bpy.context.scene, "nope")]
    assert codes == ["variant-layer-missing"]
    core = _core_layers()
    name = _layer({"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT})
    codes = [i.code for i in present.variant_issues(bpy.context.scene, name)]
    assert codes == ["variant-data-missing", "variant-no-data"], codes
    _layers().write_layer(obj.data, _spec(name), np.zeros(1, np.float32))
    assert present.variant_issues(bpy.context.scene, name) == []
    item = _layers().find_layer(bpy.context.scene, name)
    item.presentation.range_min = "abc"
    codes = [i.code for i in present.variant_issues(bpy.context.scene, name)]
    assert codes == ["variant-range-invalid"], codes
    item.presentation.range_min = ""
    # All values are no-data: no automatic range.
    _layers().write_layer(obj.data, _spec(name), np.full(1, np.nan, np.float32))
    try:
        present.build_presentation(bpy.context.scene, name)
    except present.PresentationError as ex:
        assert "automatic ramp range" in str(ex)
    else:
        raise AssertionError("expected a PresentationError")

    tex = _layer(
        {"name": "shade", "domain": core.Domain.TEXTURE, "data_type": core.DataType.FLOAT},
        target="EMISSIVE",
    )
    codes = [i.code for i in present.variant_issues(bpy.context.scene, tex)]
    assert codes == ["variant-image-missing"], codes


# --- variant publish ------------------------------------------------------------------------


@contextmanager
def _fake_rusty_tiles() -> Iterator[None]:
    """rusty-tiles stand-in, so naming and the record are tested without the wheel."""
    deps = h.submodule("core.deps")
    job_mod = h.submodule("publish.job")
    cached, original = deps._cached, job_mod._rusty_tiles

    def mesh_to_tileset(input, output, *, callback=None, **_kwargs):
        assert Path(input).is_file()
        Path(output).write_bytes(b"fake archive")
        report = {"triangles": 8, "tiles": 1, "leaves": 1}
        return SimpleNamespace(report=report, diagnostics={}, output=output)

    deps._cached = deps.DependencyStatus(True, version="fake")
    job_mod._rusty_tiles = lambda: SimpleNamespace(
        mesh_to_tileset=mesh_to_tileset, validate=lambda *_a, **_k: {"ok": True}
    )
    try:
        yield
    finally:
        job_mod._rusty_tiles = original
        deps._cached = cached


def _finish():
    publish = h.submodule("blender.publish")
    job = publish.active_job()
    assert job is not None, "no publish job is running"
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    assert publish.update_from_job() is None
    return job


def _variant_scene(n: int = 2) -> tuple[bpy.types.Object, str]:
    obj = _grid(n=n)
    obj.data.materials.append(_material("Flat", rgb=(0.3, 0.3, 0.3)))
    props = h.project()
    props.placement_mode = "ANCHOR"
    p = h.submodule("blender.properties")
    p.write_number(props, "anchor_lon", 153.02)
    p.write_number(props, "anchor_lat", -27.47)
    p.write_number(props, "anchor_height", 10.0)
    core = _core_layers()
    name = _layer(
        {"name": "slope", "domain": core.Domain.FACE, "data_type": core.DataType.FLOAT},
        palette="viridis",
        range_min="0",
        range_max=repr(float(n * n - 1)),
    )
    _layers().write_layer(obj.data, _spec(name), np.arange(n * n, dtype=np.float32))
    return obj, name


def test_variant_output_staging_and_record() -> None:
    h.reset_scene()
    _obj, name = _variant_scene()
    with _tmpdir() as tmp, _fake_rusty_tiles():
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        profile = h.project().build_profiles[0]
        profile.output_path = str(tmp / "site.3tz")
        profile.profile = "FAST"

        # From the Layers panel: the operator's layer.
        assert bpy.ops.geotools.publish(layer=name) == {"FINISHED"}
        job = _finish()
        assert job.state.name == "DONE", job.error
        output = tmp / "site.slope.3tz"
        assert job.output == output and output.is_file()
        assert not (tmp / "site.3tz").exists()
        staging = tmp / "site.slope.geotools"
        assert job.staging_dir == staging
        record = json.loads((staging / "build.json").read_text())
        assert record["schema"] == 2
        assert record["output"] == str(output)
        variant = record["variant"]
        assert variant["layer"] == "slope" and variant["target"] == "COLOR_0"
        assert variant["blend"] == "REPLACE" and variant["palette"] == "viridis"
        assert variant["range"] == {"min": 0.0, "max": 3.0, "auto_min": False, "auto_max": False}
        assert variant["ramp_stops"] == _ramps().ramp_stops("viridis", 0.0, 3.0, 11)
        assert [r["name"] for r in record["layers"]] == ["slope"]
        assert record["placement"]["anchor"] == [153.02, -27.47, 10.0]
        assert h.project().build_profiles[0].last_output == str(output)

        # From the build profile's variant layer; the plain publish is separate.
        profile = h.project().build_profiles[0]
        profile.variant_layer = name
        profile.overwrite = True
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        assert _finish().output == output
        profile.variant_layer = ""
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        job = _finish()
        assert job.output == tmp / "site.3tz" and job.state.name == "DONE", job.error
        plain = json.loads((tmp / "site.geotools" / "build.json").read_text())
        assert plain["variant"] is None

        # A variant layer that is not presentable fails the checks.
        _result, error = h.call_op(bpy.ops.geotools.publish, layer="nope")
        assert error and "Checks panel" in error, error
        assert ("ERROR", "variant-layer-missing") in [
            (i.severity, i.code) for i in h.project().issues
        ]
        assert not (tmp / "site.nope.geotools").exists()


# --- end to end (needs the wheel) ---------------------------------------------------------------


def _meshopt_bytes(data: bytes, pos: int, count: int) -> tuple[bytearray, int]:
    groups = count // 16
    header = data[pos : pos + (groups + 3) // 4]
    pos += (groups + 3) // 4
    out = bytearray(count)
    for g in range(groups):
        bits = (header[g // 4] >> ((g % 4) * 2)) & 3
        if bits == 0:
            continue
        if bits == 3:
            out[g * 16 : g * 16 + 16] = data[pos : pos + 16]
            pos += 16
            continue
        width = 2 if bits == 1 else 4
        extra = pos + 2 * width
        values = [
            (byte >> shift) & ((1 << width) - 1)
            for byte in data[pos : pos + 2 * width]
            for shift in range(8 - width, -1, -width)
        ]
        for i, enc in enumerate(values):
            if enc == (1 << width) - 1:
                enc = data[extra]
                extra += 1
            out[g * 16 + i] = enc
        pos = extra
    return out, pos


def _meshopt_decode_attributes(data: bytes, count: int, stride: int) -> bytes:
    """meshoptimizer's vertex codec, version 0 (as in tests/core/test_gltf_writer.py)."""
    assert data[0] == 0xA0, "only meshopt vertex codec v0 is supported here"
    last = bytearray(data[len(data) - stride :])
    block = min((8192 // stride) & ~15, 256)
    out = bytearray(count * stride)
    pos, done = 1, 0
    while done < count:
        n = min(block, count - done)
        for k in range(stride):
            deltas, pos = _meshopt_bytes(data, pos, (n + 15) & ~15)
            p = last[k]
            for i in range(n):
                v = deltas[i]
                p = (p + ((v >> 1) ^ (-(v & 1) & 0xFF))) & 0xFF
                out[(done + i) * stride + k] = p
        last = out[(done + n - 1) * stride : (done + n) * stride]
        done += n
    return bytes(out)


def _leaf_colors(glb: bytes) -> np.ndarray | None:
    """COLOR_0 of a leaf's primitives (float VEC4, meshopt ATTRIBUTES), or None."""
    json_length = struct.unpack_from("<I", glb, 12)[0]
    document = json.loads(glb[20 : 20 + json_length])
    binary = glb[20 + json_length + 8 :]
    rows = []
    for mesh in document["meshes"]:
        for prim in mesh["primitives"]:
            index = prim["attributes"].get("COLOR_0")
            if index is None:
                continue
            acc = document["accessors"][index]
            assert (acc["componentType"], acc["type"]) == (5126, "VEC4"), acc
            view = document["bufferViews"][acc["bufferView"]]
            meshopt = view.get("extensions", {}).get("EXT_meshopt_compression")
            if meshopt is None:
                start = view.get("byteOffset", 0)
                raw = binary[start : start + view["byteLength"]]
            else:
                assert meshopt["mode"] == "ATTRIBUTES"
                assert meshopt.get("filter", "NONE") == "NONE"
                start = meshopt.get("byteOffset", 0)
                raw = _meshopt_decode_attributes(
                    binary[start : start + meshopt["byteLength"]],
                    meshopt["count"],
                    meshopt["byteStride"],
                )
            stride = view.get("byteStride", 16)
            data = np.frombuffer(raw, np.uint8).reshape(acc["count"], stride)[:, :16]
            rows.append(np.ascontiguousarray(data).view("<f4").reshape(acc["count"], 4))
    return np.concatenate(rows) if rows else None


def _assert_leaf_ramp(output: Path, count: int) -> None:
    """Every leaf COLOR_0 of a REPLACE variant is a colour of the ramp of 0 to
    ``count - 1``, and all ``count`` colours reached the tiles."""
    ramp = _ramps().apply_ramp(np.arange(count, dtype=np.float32), 0.0, count - 1.0, "viridis")
    palette = {tuple(c) for c in ramp[:, :3].tolist()}
    seen: set[tuple[int, ...]] = set()
    with zipfile.ZipFile(output) as archive:
        for leaf in (n for n in archive.namelist() if n.endswith(".glb")):
            glb = archive.read(leaf)
            colors = _leaf_colors(glb)
            if colors is None:
                continue
            document = json.loads(glb[20 : 20 + struct.unpack_from("<I", glb, 12)[0]])
            for material in document["materials"]:
                factor = material["pbrMetallicRoughness"]["baseColorFactor"]
                assert factor == [1.0, 1.0, 1.0, 1.0], factor  # REPLACE
            srgb = _linear_to_srgb_bytes(colors)
            seen |= {tuple(c) for c in srgb.tolist()}
    assert seen, "no leaf carries COLOR_0"
    assert seen <= palette, sorted(seen - palette)[:5]
    assert len(seen) == count, len(seen)  # every face's ramp colour reached the tiles


def test_variant_publish_end_to_end() -> None:
    if h.CONFIG.expect != "available":
        return
    import rusty_tiles

    h.reset_scene()
    _obj, name = _variant_scene(n=4)
    with _tmpdir() as tmp:
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        profile = h.project().build_profiles[0]
        profile.output_path = str(tmp / "site.3tz")
        profile.profile = "LOSSLESS"  # COLOR_0 stays float32, as written
        assert bpy.ops.geotools.publish(layer=name) == {"FINISHED"}
        job = _finish()
        assert job.state.name == "DONE", job.error
        output = tmp / f"site.{name}.3tz"
        assert output.is_file() and not (tmp / "site.3tz").exists()
        assert rusty_tiles.validate(output)["ok"]
        record = json.loads((tmp / f"site.{name}.geotools" / "build.json").read_text())
        assert record["validation"]["ok"] is True
        assert record["variant"]["layer"] == name

        _assert_leaf_ramp(output, 16)


# --- layers computed by Geometry Nodes ---------------------------------------------------------


def _store_tree(attribute: str = "slope", subdivide: int = 1) -> bpy.types.NodeTree:
    """A Geometry Nodes tree: Subdivide Mesh (changes the topology), then Store
    Named Attribute ``attribute`` = the face index, as a FACE FLOAT."""
    tree = bpy.data.node_groups.new(f"Store {attribute}", "GeometryNodeTree")
    tree.is_modifier = True
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    geometry = nodes.new("NodeGroupInput").outputs[0]
    if subdivide:
        sub = nodes.new("GeometryNodeSubdivideMesh")
        sub.inputs["Level"].default_value = subdivide
        links.new(geometry, sub.inputs["Mesh"])
        geometry = sub.outputs["Mesh"]
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = "FLOAT", "FACE"
    store.inputs["Name"].default_value = attribute
    links.new(geometry, store.inputs["Geometry"])
    links.new(nodes.new("GeometryNodeInputIndex").outputs[0], store.inputs["Value"])
    links.new(store.outputs["Geometry"], nodes.new("NodeGroupOutput").inputs[0])
    return tree


def _geonodes_scene() -> tuple[bpy.types.Object, str]:
    """A 2 x 2 grid whose layer ``slope`` exists only in its Geometry Nodes
    modifier's output: 16 faces after Subdivide Mesh, valued 0 to 15."""
    obj = _grid(n=2)
    obj.data.materials.append(_material("Flat", rgb=(0.3, 0.3, 0.3)))
    core = _core_layers()
    name = _layer(
        {
            "name": "slope",
            "domain": core.Domain.FACE,
            "data_type": core.DataType.FLOAT,
            "source_attribute": "slope",
        },
        palette="viridis",
        range_min="0",
        range_max="15",
    )
    modifier = obj.modifiers.new("Slope", "NODES")
    modifier.node_group = _store_tree("slope")
    bpy.context.view_layer.update()
    return obj, name


def _clear_trees() -> None:
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)


def test_geometry_nodes_layer() -> None:
    h.reset_scene()
    _clear_trees()
    L = _layers()
    scene = bpy.context.scene
    obj, name = _geonodes_scene()
    item = L.find_layer(scene, name)
    panel = h.submodule("blender.panels").GEOTOOLS_PT_layers

    # Nothing is checked while drawing; Update Range reads the evaluated mesh.
    assert [i.code for i in L.cached_status(scene, item)] == ["layer-not-checked"]
    assert h.draw_header(panel) == "RADIOBUT_OFF"
    assert bpy.ops.geotools.layer_update_range() == {"FINISHED"}
    item = L.find_layer(scene, name)
    assert (item.value_min, item.value_max) == ("0.0", "15.0")
    assert (float(item.auto_min), float(item.auto_max)) == _ramps().auto_range(
        np.arange(16, dtype=np.float32)
    )
    assert L.layer_status(scene, item) == [] and L.cached_status(scene, item) == []
    assert h.draw_header(panel) == "CHECKMARK"
    assert "Stored on every source object" in h.label_text(h.draw(panel))
    # Nothing was baked into the mesh.
    assert "slope" not in obj.data.attributes and "gt_slope" not in obj.data.attributes

    # Without the modifier the status says how to make the attribute.
    obj.modifiers["Slope"].show_viewport = False
    bpy.context.view_layer.update()
    status = L.refresh_layer(scene, item)
    assert [i.code for i in status] == ["layer-attribute-missing"], status
    assert "Store Named Attribute" in status[0].message and "slope" in status[0].message
    assert h.draw_header(panel) == "RADIOBUT_OFF"
    obj.modifiers["Slope"].show_viewport = True
    bpy.context.view_layer.update()
    assert L.refresh_layer(scene, item) == []

    # COLOR_0: one ramp colour per evaluated face, in the exported GLB.
    presentation, record = _present().build_presentation(scene, name)
    assert record["range"]["min"] == 0.0 and record["range"]["max"] == 15.0
    expected = _ramps().apply_ramp(np.arange(16, dtype=np.float32), 0.0, 15.0, "viridis")
    np.testing.assert_array_equal(presentation.colors["Grid"], expected)
    writer = h.submodule("core.gltf_writer")
    with _tmpdir() as tmp:
        export, topology = _extract(presentation, tmp / "images")
        assert topology["Grid"].startswith("evaluated:")
        writer.write_glb(export, tmp / "source.glb")
        document, binary = _read_glb(tmp / "source.glb")
        faces = 0
        for p, prim in enumerate(export.primitives):
            gltf_prim = document["meshes"][0]["primitives"][p]
            color = _accessor(document, binary, gltf_prim["attributes"]["COLOR_0"])
            indices = _accessor(document, binary, gltf_prim["indices"]).reshape(-1, 3)
            face = prim.source_polygon  # evaluated polygon index = its stored value
            faces = max(faces, int(face.max()) + 1)
            for corner in range(3):
                np.testing.assert_array_equal(
                    _linear_to_srgb_bytes(color[indices[:, corner]]), expected[face][:, :3]
                )
        assert faces == 16
    # The preview cannot be painted on the 4 original faces; the export did not need it.
    assert bpy.ops.geotools.layer_write_presentation() == {"FINISHED"}
    assert "gt_present" not in obj.data.attributes

    # A variant publish of it validates, with every ramp colour in the tiles.
    if h.CONFIG.expect == "available":
        import rusty_tiles

        props = h.project()
        props.placement_mode = "ANCHOR"
        p_ = h.submodule("blender.properties")
        p_.write_number(props, "anchor_lon", 153.02)
        p_.write_number(props, "anchor_lat", -27.47)
        p_.write_number(props, "anchor_height", 10.0)
        with _tmpdir() as tmp:
            assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
            profile = props.build_profiles[0]
            profile.output_path = str(tmp / "site.3tz")
            profile.profile = "LOSSLESS"  # COLOR_0 stays float32, as written
            assert bpy.ops.geotools.publish(layer=name) == {"FINISHED"}
            job = _finish()
            assert job.state.name == "DONE", job.error
            output = tmp / f"site.{name}.3tz"
            assert rusty_tiles.validate(output)["ok"]
            build = json.loads((tmp / f"site.{name}.geotools" / "build.json").read_text())
            assert build["validation"]["ok"] is True
            assert build["geotools"]["apply_modifiers"] is True
            (layer,) = build["layers"]
            assert layer["attribute"] == "slope"
            assert (layer["value_min"], layer["value_max"]) == (0.0, 15.0)
            assert build["source_glb"]["triangles"] == 32
            _assert_leaf_ramp(output, 16)
    _clear_trees()


def test_apply_modifiers_off() -> None:
    h.reset_scene()
    _clear_trees()
    L = _layers()
    scene = bpy.context.scene
    obj, name = _geonodes_scene()
    export_mod = _export()
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    profile = h.project().build_profiles[0]
    assert profile.apply_modifiers  # the default, as in Blender's glTF exporter
    calls = h.draw(h.submodule("blender.panels").GEOTOOLS_PT_publish_settings)
    assert "apply_modifiers" in h.props_drawn(calls)

    with _tmpdir() as tmp:
        applied, topology = _extract(None, tmp / "a")
        assert sum(p.indices.shape[0] for p in applied.primitives) == 32
        assert topology["Grid"].startswith("evaluated:")

        # Off: the original mesh is exported, its topology entry is the plain digest.
        original, topology = export_mod.extract_scene(
            bpy.context, _source(), tmp / "b", apply_modifiers=False
        )
        assert sum(p.indices.shape[0] for p in original.primitives) == 8
        assert topology["Grid"] == export_mod.mesh_topology_digest(obj.data, obj.data)
        positions = np.concatenate([p.positions for p in original.primitives])
        assert positions.shape[0] == len(obj.data.vertices)
        polygons = np.concatenate([p.source_polygon for p in original.primitives])
        assert sorted(set(polygons.tolist())) == [0, 1, 2, 3]

    # Layer reads follow the profile: the original mesh has no slope.
    item = L.find_layer(scene, name)
    profile.apply_modifiers = False
    assert [i.code for i in L.cached_status(scene, item)] == ["layer-not-checked"]
    status = L.refresh_layer(scene, item)
    assert [i.code for i in status] == ["layer-attribute-missing"], status
    assert "Apply Modifiers is off" in status[0].message
    # Values written by hand go to the original mesh, under the source attribute.
    L.write_layer(obj.data, _spec(name), np.array([5.0, 6.0, 7.0, 8.0], np.float32))
    assert "slope" in obj.data.attributes and "gt_slope" not in obj.data.attributes
    assert L.refresh_layer(scene, item) == []
    assert L.update_value_range(scene, item) == (5.0, 8.0)
    presentation, _record = _present().build_presentation(scene, name)
    assert presentation.colors["Grid"].shape == (4, 4)
    _preview, skipped = _present().write_preview(scene, name)
    assert skipped == [] and "gt_present" in obj.data.attributes

    # On again: the modifier's values replace them in what is read.
    profile.apply_modifiers = True
    assert [i.code for i in L.cached_status(scene, item)] == ["layer-not-checked"]
    assert L.update_value_range(scene, item) == (0.0, 15.0)

    # The publish records the choice, and exports the original without it.
    profile.apply_modifiers = False
    with _tmpdir() as tmp, _fake_rusty_tiles():
        profile.output_path = str(tmp / "site.3tz")
        assert bpy.ops.geotools.publish(layer=name) == {"FINISHED"}
        job = _finish()
        assert job.state.name == "DONE", job.error
        build = json.loads((tmp / f"site.{name}.geotools" / "build.json").read_text())
        assert build["geotools"]["apply_modifiers"] is False
        assert build["source_glb"]["triangles"] == 8
        assert (build["layers"][0]["value_min"], build["layers"][0]["value_max"]) == (5.0, 8.0)
    _clear_trees()
