"""The layer registry: storage, attribute helpers, operators, panel and persistence."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

import _harness as h
import bpy
import numpy as np


def _core():
    return h.submodule("core.layers")


def _layers():
    return h.submodule("blender.layers")


def _spec(name: str = "slope", domain: str = "FACE", data_type: str = "FLOAT", **kwargs):
    c = _core()
    return c.LayerSpec(name, c.Domain(domain), c.DataType(data_type), **kwargs)


def _mesh(name: str, offset: float = 0.0) -> bpy.types.Mesh:
    """Two quads and a triangle: 3 faces, 11 corners, with a UV map."""
    verts = [(x + offset, y, 0.0) for y in (0, 1) for x in (0, 1, 2)] + [(offset + 3, 0, 0)]
    faces = [(0, 1, 4, 3), (1, 2, 5, 4), (2, 6, 5)]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.uv_layers.new(name="UVMap")
    return mesh


def _source(count: int = 2) -> list[bpy.types.Object]:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    objects = []
    for i in range(count):
        name = f"Grid_{'AB'[i]}"
        obj = bpy.data.objects.new(name, _mesh(name, offset=4.0 * i))
        collection.objects.link(obj)
        objects.append(obj)
    return objects


def _codes(issues) -> list[str]:
    return [i.code for i in issues]


# --- registration -------------------------------------------------------------------------


def test_layers_registered() -> None:
    for idname in (
        "geotools.layer_add",
        "geotools.layer_remove",
        "geotools.layer_rename",
        "geotools.layer_create_attributes",
        "geotools.layer_set_categories",
        "geotools.layer_update_range",
    ):
        assert h.operator_available(idname), idname
    assert hasattr(bpy.types, "GEOTOOLS_UL_layers")
    panels = [cls.__name__ for cls in h.submodule("blender.panels").classes]
    # Source, then Linked Data (M8), then Layers.
    assert panels.index("GEOTOOLS_PT_layers") == panels.index("GEOTOOLS_PT_source") + 2, panels
    props = h.project()
    assert "layers" in props.bl_rna.properties and "layers_index" in props.bl_rna.properties


# --- registry -------------------------------------------------------------------------------


def test_registry_create_update_find() -> None:
    h.reset_scene()
    c, L = _core(), _layers()
    scene = bpy.context.scene
    spec = _spec(units="deg", nodata=-9999.0, origin=c.Origin.ANALYSIS, description="Slope")
    item = L.ensure_layer(scene, spec)
    assert len(h.project().layers) == 1
    assert L.find_layer(scene, "slope") == item
    assert L.find_layer(scene, "nope") is None
    assert L.spec_from_props(item) == spec
    assert item.nodata == "-9999.0" and item.aggregation == "DEFAULT"

    # Update in place; aggregation and binary64 no-data round-trip exactly.
    updated = _spec(nodata=0.1 + 0.2, aggregation=c.Aggregation.MAX, units="%")
    item = L.ensure_layer(scene, updated)
    assert len(h.project().layers) == 1
    assert L.spec_from_props(item) == updated
    assert L.spec_from_props(item).nodata == 0.1 + 0.2

    # A TEXTURE layer keeps its texture settings when its spec is updated.
    tex = L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    tex.uv_map, tex.resolution, tex.margin = "UVMap", 256, 2
    tex = L.ensure_layer(scene, _spec("shade", "TEXTURE", units="h"))
    assert (tex.uv_map, tex.resolution, tex.margin, tex.units) == ("UVMap", 256, 2, "h")

    cats = _spec("cover", data_type="CATEGORY", categories=("ground", "trees"), nodata=-1.0)
    item = L.ensure_layer(scene, cats)
    assert item.categories == "ground\ntrees"
    assert L.spec_from_props(item) == cats

    # Label, value range (binary64 text) and provenance (JSON text).
    full = _spec(
        "sun",
        label="Sun hours",
        value_min=0.1 + 0.2,
        value_max=11.75,
        provenance=c.make_provenance("sun_hours", {"steps": 24, "date": "2026-06-21"}, "0.3.0"),
    )
    item = L.ensure_layer(scene, full)
    assert item.label == "Sun hours"
    assert item.value_min == repr(0.1 + 0.2) and item.value_max == "11.75"
    assert '"tool": "sun_hours"' in item.provenance
    assert L.spec_from_props(item) == full
    item.provenance = "{broken"
    assert "layer-provenance-invalid" in _codes(L.layer_status(scene, item))
    item.provenance = ""
    assert L.spec_from_props(item).provenance is None
    item.value_min = "20"
    assert "layer-range-invalid" in _codes(L.layer_status(scene, item))


def test_ensure_layer_refuses_invalid_spec() -> None:
    h.reset_scene()
    c, L = _core(), _layers()
    scene = bpy.context.scene
    for bad in (
        _spec("Bad"),
        _spec(data_type="CATEGORY"),
        _spec(data_type="CATEGORY", categories=("a",), aggregation=c.Aggregation.AREA_MEAN),
        _spec(nodata=math.inf),
    ):
        try:
            L.ensure_layer(scene, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    assert len(h.project().layers) == 0


def test_validation_of_stored_text() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    item = L.ensure_layer(scene, _spec())
    item.nodata = "not a number"
    spec = L.spec_from_props(item)
    assert math.isnan(spec.nodata)
    assert "layer-nodata-invalid" in _codes(L.layer_status(scene, item))
    item.nodata = ""
    assert L.spec_from_props(item).nodata is None
    # A duplicate made by hand is reported on both entries.
    other = h.project().layers.add()
    other.name = "slope"
    assert other.name == "slope"
    assert "layer-name-duplicate" in _codes(L.layer_status(scene, item))
    assert "layer-name-duplicate" in _codes(L.layer_status(scene, other))


def test_add_operator() -> None:
    h.reset_scene()
    props = h.project()
    assert bpy.ops.geotools.layer_add() == {"FINISHED"}
    assert [layer.name for layer in props.layers] == ["layer_1"]
    assert bpy.ops.geotools.layer_add(name="cover", domain="CORNER", data_type="CATEGORY") == {
        "FINISHED"
    }
    assert props.layers_index == 1
    item = props.layers["cover"]
    assert (item.domain, item.data_type, item.origin) == ("CORNER", "CATEGORY", "MANUAL")
    assert bpy.ops.geotools.layer_add() == {"FINISHED"}
    assert props.layers[-1].name == "layer_2"
    for bad in ("cover", "Cover", "2x", "present"):
        result, error = h.call_op(bpy.ops.geotools.layer_add, name=bad)
        assert result == {"ERROR_REPORTED"} and error, (bad, result)
    assert len(props.layers) == 3


def test_set_categories_operator() -> None:
    h.reset_scene()
    assert bpy.ops.geotools.layer_add(name="cover", data_type="CATEGORY") == {"FINISHED"}
    item = h.project().layers["cover"]
    assert bpy.ops.geotools.layer_set_categories(labels="ground, grass ,trees") == {"FINISHED"}
    assert item.categories == "ground\ngrass\ntrees"
    assert _layers().spec_from_props(item).categories == ("ground", "grass", "trees")
    assert bpy.ops.geotools.layer_set_categories(labels=" ") == {"FINISHED"}
    assert item.categories == ""


# --- attributes -----------------------------------------------------------------------------


def test_write_and_read_every_type() -> None:
    h.reset_scene()
    L = _layers()
    (obj, _other) = _source()
    mesh = obj.data
    rng = np.random.default_rng(3)
    for domain, count in (("FACE", 3), ("CORNER", 11)):
        for data_type, blender_type, values in (
            ("FLOAT", "FLOAT", rng.random(count).astype(np.float32) * 90),
            ("INT", "INT", rng.integers(-5, 5, count).astype(np.int32)),
            ("BOOLEAN", "BOOLEAN", rng.random(count) > 0.5),
            ("CATEGORY", "INT", rng.integers(0, 3, count).astype(np.int32)),
        ):
            kwargs = {"categories": ("a", "b", "c")} if data_type == "CATEGORY" else {}
            spec = _spec(f"{domain}_{data_type}".lower(), domain, data_type, **kwargs)
            assert L.domain_size(mesh, spec.domain) == count
            L.write_layer(mesh, spec, values)
            attr = mesh.attributes[f"gt_{spec.name}"]
            assert (attr.domain, attr.data_type) == (domain, blender_type), spec
            back = L.read_layer(mesh, spec)
            assert back.dtype == _core().numpy_dtype(spec.data_type), back.dtype
            assert back.shape == (count,)
            assert np.array_equal(back, values), (spec, back, values)
    # Lists and other dtypes are converted.
    spec = _spec("listed", "FACE", "INT")
    L.write_layer(mesh, spec, [1.0, 2.0, 3.0])
    assert L.read_layer(mesh, spec).tolist() == [1, 2, 3]


def test_new_attribute_is_filled_with_nodata() -> None:
    h.reset_scene()
    L = _layers()
    (obj, _other) = _source()
    mesh = obj.data
    attr = L.ensure_attribute(mesh, _spec())
    assert (attr.name, attr.domain, attr.data_type) == ("gt_slope", "FACE", "FLOAT")
    assert np.isnan(L.read_layer(mesh, _spec())).all()
    spec = _spec("count", "CORNER", "INT", nodata=-1.0)
    L.ensure_attribute(mesh, spec)
    assert (L.read_layer(mesh, spec) == -1).all()
    spec = _spec("flag", "FACE", "BOOLEAN")
    L.ensure_attribute(mesh, spec)
    assert not L.read_layer(mesh, spec).any()
    # Existing values are kept by a second ensure_attribute.
    L.write_layer(mesh, _spec(), np.array([1, 2, 3], np.float32))
    L.ensure_attribute(mesh, _spec())
    assert L.read_layer(mesh, _spec()).tolist() == [1.0, 2.0, 3.0]


def test_write_checks_length_and_values() -> None:
    h.reset_scene()
    L = _layers()
    (obj, _other) = _source()
    mesh = obj.data
    for spec, values in (
        (_spec(), np.zeros(4, np.float32)),
        (_spec(domain="CORNER"), np.zeros(3, np.float32)),
        (_spec(), np.zeros((3, 1), np.float32)),
        (_spec(data_type="INT"), np.array([1.0, math.nan, 2.0])),
    ):
        try:
            L.write_layer(mesh, spec, values)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {values.shape} for {spec}")
        assert "gt_slope" not in mesh.attributes, "a refused write changed the mesh"
    try:
        L.write_layer(mesh, _spec(domain="TEXTURE"), np.zeros(3))
    except ValueError:
        pass
    else:
        raise AssertionError("wrote a TEXTURE layer to an attribute")


def test_read_errors() -> None:
    h.reset_scene()
    L = _layers()
    (obj, _other) = _source()
    mesh = obj.data
    try:
        L.read_layer(mesh, _spec())
    except KeyError:
        pass
    else:
        raise AssertionError("read a missing attribute")
    mesh.attributes.new("gt_slope", "INT", "FACE")
    try:
        L.read_layer(mesh, _spec())
    except ValueError as ex:
        assert "FACE INT" in str(ex), ex
    else:
        raise AssertionError("read an INT attribute as FLOAT")


def test_wrong_domain_or_type_is_replaced() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    (obj, other) = _source()
    item = L.ensure_layer(scene, _spec())
    obj.data.attributes.new("gt_slope", "FLOAT", "CORNER")
    other.data.attributes.new("gt_slope", "INT", "FACE")
    status = L.layer_status(scene, item)
    assert _codes(status) == ["layer-attribute-mismatch"] * 2, status
    assert "CORNER FLOAT" in status[0].message

    issues: list = []
    attr = L.ensure_attribute(obj.data, _spec(), issues=issues)
    assert (attr.domain, attr.data_type) == ("FACE", "FLOAT")
    assert _codes(issues) == ["layer-attribute-replaced"], issues
    assert issues[0].subject == obj.data.name
    assert len([a for a in obj.data.attributes if a.name.startswith("gt_slope")]) == 1

    # write_layer replaces too, and the operator replaces the rest.
    L.write_layer(obj.data, _spec(), np.array([1, 2, 3], np.float32))
    assert L.read_layer(obj.data, _spec()).tolist() == [1, 2, 3]
    assert bpy.ops.geotools.layer_create_attributes() == {"FINISHED"}
    assert other.data.attributes["gt_slope"].data_type == "FLOAT"
    assert L.layer_status(scene, item) == []
    assert L.read_layer(obj.data, _spec()).tolist() == [1, 2, 3], "a good attribute was replaced"


# --- status -----------------------------------------------------------------------------------


def test_status_of_attribute_layers() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    item = L.ensure_layer(scene, _spec(domain="CORNER"))
    assert _codes(L.layer_status(scene, item)) == ["layer-no-source"]
    (obj, other) = _source()
    status = L.layer_status(scene, item)
    assert _codes(status) == ["layer-attribute-missing"] * 2, status
    assert [i.subject for i in status] == [obj.name, other.name]
    L.ensure_attribute(obj.data, L.spec_from_props(item))
    assert [i.subject for i in L.layer_status(scene, item)] == [other.name]
    assert bpy.ops.geotools.layer_create_attributes() == {"FINISHED"}
    assert L.layer_status(scene, item) == []
    # Changing the registry's type makes the stored attributes wrong.
    item.data_type = "BOOLEAN"
    assert _codes(L.layer_status(scene, item)) == ["layer-attribute-mismatch"] * 2
    # A shared mesh is reported once.
    other.data = obj.data
    assert _codes(L.layer_status(scene, item)) == ["layer-attribute-mismatch"]


def test_status_of_texture_layers() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    (obj, other) = _source()
    item = L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    item.resolution = 8
    status = L.layer_status(scene, item)
    assert _codes(status) == ["layer-image-missing"] * 2, status
    assert not bpy.ops.geotools.layer_create_attributes.poll()

    image = L.ensure_layer_image(item, obj)
    assert L.ensure_layer_image(item, obj) == image
    assert L.layer_image(item, obj) == image and L.layer_image(item, other) is None
    assert (image.size[0], image.size[1], image.is_float) == (8, 8, True)
    assert image.colorspace_settings.name == "Non-Color"
    pixels = np.empty(8 * 8 * 4, np.float32)
    image.pixels.foreach_get(pixels)
    assert np.isnan(pixels.reshape(-1, 4)[:, :3]).all(), "a new image is not no-data"
    status = L.layer_status(scene, item)
    assert _codes(status) == ["layer-image-unsaved", "layer-image-missing"], status

    item.uv_map = "Missing"
    assert _codes(L.layer_status(scene, item)).count("layer-uv-map-missing") == 2
    item.uv_map = ""
    other.data.uv_layers.remove(other.data.uv_layers[0])
    assert _codes(L.layer_status(scene, item)).count("layer-uv-map-missing") == 1

    try:
        L.save_layer_image(item, obj)
    except RuntimeError as ex:
        assert "Save the .blend" in str(ex)
    else:
        raise AssertionError("saved a layer image next to an unsaved .blend")


# --- value range --------------------------------------------------------------------------


def test_update_value_range() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    (obj, other) = _source()
    slope = L.ensure_layer(scene, _spec(nodata=-9999.0))
    assert L.update_value_range(scene, slope) == (None, None)
    assert (slope.value_min, slope.value_max) == ("", "")
    L.write_layer(obj.data, _spec(nodata=-9999.0), np.array([-9999.0, 0.5, math.nan], np.float32))
    L.write_layer(other.data, _spec(nodata=-9999.0), np.array([12.25, -3.0, -9999.0], np.float32))
    assert L.update_value_range(scene, slope) == (-3.0, 12.25)
    assert (slope.value_min, slope.value_max) == ("-3.0", "12.25")
    assert L.spec_from_props(slope).value_max == 12.25
    # The automatic ramp range is cached too, for the Display panel.
    ramps = h.submodule("core.ramps")
    auto = ramps.auto_range(np.array([0.5, 12.25, -3.0], np.float64))
    assert (float(slope.auto_min), float(slope.auto_max)) == auto, (slope.auto_min, auto)

    # No-data that is not exact in float32 is still recognised.
    odd = _spec("odd", nodata=0.1)
    item = L.ensure_layer(scene, odd)
    L.write_layer(obj.data, odd, np.array([0.1, 0.2, 0.3], np.float32))
    low, high = L.update_value_range(scene, item)
    assert (low, high) == (float(np.float32(0.2)), float(np.float32(0.3))), (low, high)

    count = _spec("count", "CORNER", "INT", nodata=-1.0)
    item = L.ensure_layer(scene, count)
    L.write_layer(obj.data, count, np.arange(11, dtype=np.int32) - 1)
    assert L.update_value_range(scene, item) == (0.0, 9.0)

    flag = _spec("flag", data_type="BOOLEAN")
    item = L.ensure_layer(scene, flag)
    L.write_layer(obj.data, flag, np.array([True, True, True]))
    assert L.update_value_range(scene, item) == (1.0, 1.0)
    assert (item.auto_min, item.auto_max) == ("", "")  # booleans always ramp 0 to 1

    tex = L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    tex.resolution = 2
    image = L.ensure_layer_image(tex, obj)
    pixels = np.array([[4, 99, 99, 1], [math.nan, 0, 0, 1], [-2, 50, 50, 1], [1, -50, 0, 1]])
    image.pixels.foreach_set(pixels.astype(np.float32).ravel())
    assert L.update_value_range(scene, tex) == (-2.0, 4.0), "read a channel other than R"

    slope = L.find_layer(scene, "slope")  # adding layers may move the old entries
    slope.value_min = slope.value_max = ""
    h.project().layers_index = 0
    assert bpy.ops.geotools.layer_update_range() == {"FINISHED"}
    assert (slope.value_min, slope.value_max) == ("-3.0", "12.25")


# --- remove and rename ---------------------------------------------------------------------


def _layer_with_data(scene) -> tuple:
    L = _layers()
    objects = _source()
    L.ensure_layer(scene, _spec())
    tex = L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    tex.resolution = 4
    for obj in objects:
        L.ensure_attribute(obj.data, _spec())
        L.ensure_layer_image(tex, obj)
    return objects, tex


def test_remove_keeps_data_by_default() -> None:
    h.reset_scene()
    scene = bpy.context.scene
    props = h.project()
    objects, _tex = _layer_with_data(scene)
    images = len(bpy.data.images)
    props.layers_index = 0
    assert bpy.ops.geotools.layer_remove() == {"FINISHED"}
    assert [layer.name for layer in props.layers] == ["shade"]
    assert props.layers_index == 0
    assert all("gt_slope" in obj.data.attributes for obj in objects)
    assert bpy.ops.geotools.layer_remove() == {"FINISHED"}
    assert len(props.layers) == 0
    assert len(bpy.data.images) == images
    assert not bpy.ops.geotools.layer_remove.poll()


def test_remove_can_delete_data() -> None:
    h.reset_scene()
    scene = bpy.context.scene
    props = h.project()
    objects, _tex = _layer_with_data(scene)
    images = len(bpy.data.images)
    props.layers_index = 1
    assert bpy.ops.geotools.layer_remove(delete_data=True) == {"FINISHED"}
    assert len(bpy.data.images) == images - 2
    assert all("gt_slope" in obj.data.attributes for obj in objects)
    props.layers_index = 0
    assert bpy.ops.geotools.layer_remove(delete_data=True) == {"FINISHED"}
    assert not any("gt_slope" in obj.data.attributes for obj in objects)
    assert len(props.layers) == 0


def test_rename_renames_attributes() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    props = h.project()
    objects = _source()
    L.ensure_layer(scene, _spec())
    L.ensure_layer(scene, _spec("aspect"))
    L.write_layer(objects[0].data, _spec(), np.array([1, 2, 3], np.float32))
    props.layers_index = 0
    for bad in ("aspect", "Bad", ""):
        result, error = h.call_op(bpy.ops.geotools.layer_rename, new_name=bad)
        assert result == {"ERROR_REPORTED"} and error, (bad, result)
    objects[1].data.attributes.new("gt_taken", "FLOAT", "FACE")
    result, error = h.call_op(bpy.ops.geotools.layer_rename, new_name="taken")
    assert error and "gt_taken" in error, error
    assert bpy.ops.geotools.layer_rename(new_name="gradient") == {"FINISHED"}
    assert L.find_layer(scene, "gradient") is not None and L.find_layer(scene, "slope") is None
    assert "gt_slope" not in objects[0].data.attributes
    assert L.read_layer(objects[0].data, _spec("gradient")).tolist() == [1, 2, 3]


def test_custom_source_attribute() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    props = h.project()
    objects = _source()
    spec = _spec(source_attribute="Slope Deg")
    item = L.ensure_layer(scene, spec)
    assert item.source_attribute == "Slope Deg"
    assert L.spec_from_props(item) == spec

    # Reads, writes, the status and the range all use the source attribute.
    status = L.refresh_layer(scene, item)
    assert _codes(status) == ["layer-attribute-missing"] * 2, status
    assert "has no Slope Deg attribute" in status[0].message
    objects[0].data.attributes.new("Slope Deg", "FLOAT", "CORNER")
    assert _codes(L.layer_status(scene, item)) == [
        "layer-attribute-mismatch",
        "layer-attribute-missing",
    ]
    L.write_layer(objects[0].data, spec, np.array([1.0, 2.0, 3.0], np.float32))
    assert "gt_slope" not in objects[0].data.attributes
    attr = objects[0].data.attributes["Slope Deg"]
    assert (attr.domain, attr.data_type) == ("FACE", "FLOAT")
    props.layers_index = 0
    assert bpy.ops.geotools.layer_create_attributes() == {"FINISHED"}
    assert "Slope Deg" in objects[1].data.attributes
    assert L.cached_status(scene, item) == []  # the operator refreshed it
    assert L.read_layer(objects[0].data, spec).tolist() == [1.0, 2.0, 3.0]
    assert L.update_value_range(scene, item) == (1.0, 3.0)
    assert L.layer_records(scene)[0][0]["attribute"] == "Slope Deg"

    # Changing the attribute makes the cached status stale until the next refresh.
    item.source_attribute = "other"
    assert _codes(L.cached_status(scene, item)) == ["layer-not-checked"]
    item.source_attribute = "gt_present"
    assert "layer-attribute-invalid" in _codes(L.cached_status(scene, item))
    alerts: list[str] = []
    h.draw(_panel(), alerts)
    assert "source_attribute" in alerts, alerts
    item.source_attribute = "Slope Deg"

    # Renaming the layer keeps its source attribute; removing its data deletes it.
    assert bpy.ops.geotools.layer_rename(new_name="gradient") == {"FINISHED"}
    item = L.find_layer(scene, "gradient")
    assert item.source_attribute == "Slope Deg"
    assert "Slope Deg" in objects[0].data.attributes
    assert L.cached_status(scene, item) == []
    assert bpy.ops.geotools.layer_remove(delete_data=True) == {"FINISHED"}
    assert not any("Slope Deg" in obj.data.attributes for obj in objects)


# --- publish record ---------------------------------------------------------------------------


def test_layer_records_skip_invalid_layers() -> None:
    h.reset_scene()
    c, L = _core(), _layers()
    scene = bpy.context.scene
    slope = _spec(units="deg")
    L.ensure_layer(scene, slope)
    bad = L.ensure_layer(scene, _spec("bad"))
    bad.nodata = "x"
    records, invalid = L.layer_records(scene)
    assert records == [c.to_record(slope)]
    assert invalid == ["bad"]


# --- panel ----------------------------------------------------------------------------------


def _panel(name: str = "GEOTOOLS_PT_layers"):
    return getattr(h.submodule("blender.panels"), name)


def test_panel_draws_without_layers() -> None:
    h.reset_scene()
    calls = h.draw(_panel())
    assert ("template_list", "GEOTOOLS_UL_layers", "NONE") in calls, calls
    assert ("operator", "geotools.layer_add", "ADD") in calls
    assert "No layers yet" in h.label_text(calls), calls
    for name in (
        "GEOTOOLS_PT_layers_bake",
        "GEOTOOLS_PT_layers_display",
        "GEOTOOLS_PT_layers_details",
    ):
        assert not _panel(name).poll(bpy.context), name


def test_panel_draws_layers() -> None:
    h.reset_scene()
    L = _layers()
    scene = bpy.context.scene
    props = h.project()
    _source()
    L.ensure_layer(scene, _spec(units="deg"))
    # The selected layer at a glance; the rest is in Details.
    calls = h.draw(_panel())
    drawn = h.props_drawn(calls)
    assert {"label", "units", "source_attribute"} <= set(drawn), drawn
    assert not {"domain", "data_type", "nodata", "value_min", "value_max"} & set(drawn), drawn
    assert ("operator", "geotools.layer_update_range", "FILE_REFRESH") in calls
    assert ("operator", "geotools.layer_rename", "GREASEPENCIL") in calls
    assert ("Not known", "NONE") in list(h.labels(calls)), calls
    # Drawing reads no mesh: the status is what the last refresh found.
    assert "Not checked yet" in h.label_text(calls), calls
    assert h.draw_header(_panel()) == "RADIOBUT_OFF"
    L.refresh_layer(scene, props.layers[0])
    labels = h.label_text(h.draw(_panel()))
    assert "has no gt_slope attribute" in labels and "Store Named Attribute" in labels, labels
    assert h.draw_header(_panel()) == "RADIOBUT_OFF"  # no values yet
    # Face and corner layers need no bake: the Bake sub-panel is for texture layers.
    assert not _panel("GEOTOOLS_PT_layers_bake").poll(bpy.context)

    details = _panel("GEOTOOLS_PT_layers_details")
    assert details.poll(bpy.context)
    calls = h.draw(details)
    drawn = h.props_drawn(calls)
    for prop in ("domain", "data_type", "nodata", "origin", "aggregation", "description"):
        assert prop in drawn, (prop, drawn)
    assert "provenance" not in drawn and "value_min" not in drawn
    assert ("operator", "geotools.layer_create_attributes", "NONE") in calls
    assert ("Area Mean", "INFO") in list(h.labels(calls)), calls

    assert bpy.ops.geotools.layer_create_attributes() == {"FINISHED"}
    labels = h.label_text(h.draw(_panel()))
    assert "Stored on every source object" in labels, labels
    assert h.draw_header(_panel()) == "CHECKMARK"

    # Values are formatted for people; binary64 stays in storage.
    props.layers[0].value_min = repr(0.46055179834365845)
    props.layers[0].value_max = repr(39.88410568237305)
    labels = list(h.labels(h.draw(_panel())))
    assert ("0.461 \u2013 39.9 deg", "NONE") in labels, labels

    # Invalid no-data and range are drawn as alerts in Details.
    props.layers[0].nodata = "abc"
    props.layers[0].value_min = "inf"
    alerts: list[str] = []
    h.draw(details, alerts)
    assert "nodata" in alerts and "value_min" in alerts, alerts
    assert h.draw_header(_panel()) == "CANCEL"
    props.layers[0].provenance = _core().format_provenance(
        _core().make_provenance("slope", {}, "0.3.0")
    )
    labels = h.label_text(h.draw(details))
    assert "Made by slope (GeoTools 0.3.0)" in labels, labels

    L.ensure_layer(scene, _spec("cover", data_type="CATEGORY", categories=("ground", "trees")))
    props.layers_index = 1
    calls = h.draw(_panel())
    assert ("operator", "geotools.layer_set_categories", "GREASEPENCIL") in calls
    assert ("2", "NONE") in list(h.labels(calls)), calls
    calls = h.draw(_panel("GEOTOOLS_PT_layers_display"))
    assert ("label", "1  trees", "NONE") in calls, calls

    L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    props.layers_index = 2
    assert "source_attribute" not in h.props_drawn(h.draw(_panel()))
    bake = _panel("GEOTOOLS_PT_layers_bake")
    assert bake.poll(bpy.context)
    calls = h.draw(bake)
    drawn = h.props_drawn(calls)
    assert {"uv_map", "resolution", "margin"} <= set(drawn), drawn
    assert ("label", "Not baked yet", "INFO") in calls, calls
    L.refresh_layer(scene, props.layers[2])
    assert h.draw_header(bake) == "RADIOBUT_OFF"
    calls = h.draw(details)
    assert ("operator", "geotools.layer_create_attributes", "NONE") not in calls

    ul = bpy.types.GEOTOOLS_UL_layers
    for item in props.layers:
        rows = h.draw_list_item(ul, props, item)
        assert ("label", item.name, L.DOMAIN_ICONS[item.domain]) in rows, rows
    props.layers[0].label = "Slope (degrees)"
    rows = h.draw_list_item(ul, props, props.layers[0])
    assert ("label", "Slope (degrees)", "FACESEL") in rows, rows


# --- persistence ------------------------------------------------------------------------------


def test_layers_survive_save_and_reopen() -> None:
    h.reset_scene()
    c, L = _core(), _layers()
    scene = bpy.context.scene
    objects = _source()
    slope = _spec(
        units="deg",
        nodata=-9999.0,
        origin=c.Origin.GEONODES,
        label="Slope",
        value_min=-2.25,
        value_max=1.5,
        provenance=c.make_provenance("geonodes", {"modifier": "Slope"}, "0.3.0-dev"),
    )
    count = _spec("count", "CORNER", "INT", aggregation=c.Aggregation.MAX)
    flag = _spec("flag", "FACE", "BOOLEAN")
    shade = _spec("shade", "TEXTURE", units="h", nodata=0.1 + 0.2)
    face_values = np.array([1.5, -2.25, 1e-7], np.float32)
    corner_values = np.arange(11, dtype=np.int32) - 5
    flags = np.array([True, False, True])
    for spec in (slope, count, flag, shade):
        L.ensure_layer(scene, spec)
    for obj in objects:
        L.write_layer(obj.data, slope, face_values)
        L.write_layer(obj.data, count, corner_values)
        L.write_layer(obj.data, flag, flags)
    tex = L.find_layer(scene, "shade")
    tex.uv_map, tex.resolution, tex.margin = "UVMap", 4, 3
    pixels = np.random.default_rng(5).random((16, 4)).astype(np.float32)
    pixels[:, 3] = 1.0
    image = L.ensure_layer_image(tex, objects[0])
    image.pixels.foreach_set(pixels.ravel())
    expected = [c.to_record(s) for s in (slope, count, flag, shade)]

    try:
        with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
            path = Path(tmp) / "layers.blend"
            # Save once so "//" means tmp, then save the image next to it.
            assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}
            exr = L.save_layer_image(tex, objects[0])
            assert exr == Path(tmp) / "geotools_layers" / "shade__Grid_A.exr", exr
            assert exr.is_file()
            assert image.filepath == "//geotools_layers/shade__Grid_A.exr", image.filepath
            assert _codes(L.layer_status(scene, tex)) == ["layer-image-missing"]
            assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}

            h.reset_scene()
            assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
            assert len(h.project().layers) == 0
            assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}

            scene = bpy.context.scene
            props = h.project()
            assert [layer.name for layer in props.layers] == ["slope", "count", "flag", "shade"]
            records, invalid = L.layer_records(scene)
            assert records == expected and invalid == []
            assert L.spec_from_props(props.layers["shade"]).nodata == 0.1 + 0.2
            objects = L.source_objects(scene)
            assert [o.name for o in objects] == ["Grid_A", "Grid_B"]
            for obj in objects:
                mesh = obj.data
                assert np.array_equal(L.read_layer(mesh, slope), face_values)
                assert np.array_equal(L.read_layer(mesh, count), corner_values)
                assert np.array_equal(L.read_layer(mesh, flag), flags)
            for name in ("slope", "count", "flag"):
                assert L.layer_status(scene, props.layers[name]) == [], name
            assert L.spec_from_props(props.layers["slope"]) == slope
            assert L.update_value_range(scene, props.layers["slope"]) == (-2.25, 1.5)

            tex = props.layers["shade"]
            assert (tex.uv_map, tex.resolution, tex.margin) == ("UVMap", 4, 3)
            image = L.layer_image(tex, objects[0])
            assert image is not None and L.layer_image(tex, objects[1]) is None
            assert image.filepath == "//geotools_layers/shade__Grid_A.exr"
            assert Path(bpy.path.abspath(image.filepath)) == exr
            back = np.empty(16 * 4, np.float32)
            image.pixels.foreach_get(back)
            back = back.reshape(-1, 4)
            assert np.array_equal(back[:, :3], pixels[:, :3]), np.abs(back - pixels).max()
            status = L.layer_status(scene, tex)
            assert _codes(status) == ["layer-image-missing"], status
            assert status[0].subject == "Grid_B"
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()


# --- build record ---------------------------------------------------------------------------


def test_blend_path_for_record() -> None:
    publish = h.submodule("blender.publish")
    try:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()
        assert publish.blend_path_for_record() is None, "an unsaved file was hashed"
        with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
            path = Path(tmp) / "hash.blend"
            assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}
            assert not bpy.data.is_dirty
            assert publish.blend_path_for_record() == path
            # Background Blender never marks the file dirty, so pass the flag.
            assert publish.blend_path_for_record(str(path), is_dirty=True) is None
            assert publish.blend_path_for_record("", is_dirty=False) is None
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()
