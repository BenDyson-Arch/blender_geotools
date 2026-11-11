"""The vertex-space bake: Geometry Nodes attributes frozen into layer attributes."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import _harness as h
import bpy
import numpy as np

OP = "geotools.bake_geonodes_layer"


def _core():
    return h.submodule("core.layers")


def _layers():
    return h.submodule("blender.layers")


def _bake():
    return h.submodule("blender.bake_vertex")


def _clean() -> None:
    h.reset_scene()
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)


def _spec(name: str = "height", domain: str = "FACE", data_type: str = "FLOAT", **kwargs):
    c = _core()
    return c.LayerSpec(name, c.Domain(domain), c.DataType(data_type), **kwargs)


def _plane_z(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """The analytic field of the tilted test plane."""
    return 0.5 * x + 0.25 * y


def _grid(name: str, n: int, tilt: bool = True) -> bpy.types.Mesh:
    """An ``n`` x ``n`` quad grid on [-1, 1]^2, built with foreach_set.

    With ``tilt`` the vertices lie on z = 0.5x + 0.25y, else on z = 0.
    """
    xs = np.linspace(-1.0, 1.0, n + 1)
    x, y = np.meshgrid(xs, xs, indexing="xy")
    x, y = x.ravel(), y.ravel()
    z = _plane_z(x, y) if tilt else np.zeros_like(x)
    co = np.column_stack([x, y, z]).astype(np.float32)
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="xy")
    v0 = (j * (n + 1) + i).ravel()
    quads = np.column_stack([v0, v0 + 1, v0 + n + 2, v0 + n + 1]).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(co.shape[0])
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(quads.shape[0])
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    mesh.update(calc_edges=True)
    return mesh


def _source(*meshes: bpy.types.Mesh) -> list[bpy.types.Object]:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    objects = []
    for mesh in meshes:
        obj = bpy.data.objects.new(mesh.name, mesh)
        collection.objects.link(obj)
        objects.append(obj)
    return objects


def _tree(
    domain: str = "FACE",
    field: str = "z",
    *,
    data_type: str = "FLOAT",
    name: str = "value",
    subdivide: int = 0,
    offset_z: float = 0.0,
    delete_x_below: float | None = None,
    nodata_x_below: float | None = None,
    nodata: float = -9999.0,
) -> bpy.types.GeometryNodeTree:
    """A Geometry Nodes tree that stores ``field`` as attribute ``name`` on ``domain``.

    ``field``: ``z`` (position z), ``x``, ``region`` (1 where x > 0, else 0) or
    ``index``. Optionally subdivides first, and after storing deletes faces with
    centre x below a value or moves the surface up.
    """
    tree = bpy.data.node_groups.new("Test GN", "GeometryNodeTree")
    tree.is_modifier = True
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = tree.nodes, tree.links
    geometry = nodes.new("NodeGroupInput").outputs[0]
    if subdivide:
        sub = nodes.new("GeometryNodeSubdivideMesh")
        sub.inputs["Level"].default_value = subdivide
        links.new(geometry, sub.inputs["Mesh"])
        geometry = sub.outputs[0]
    position = nodes.new("GeometryNodeInputPosition").outputs[0]
    xyz = nodes.new("ShaderNodeSeparateXYZ")
    links.new(position, xyz.inputs[0])
    if field == "z":
        value = xyz.outputs["Z"]
    elif field == "x":
        value = xyz.outputs["X"]
    elif field == "region":
        compare = nodes.new("FunctionNodeCompare")
        compare.data_type, compare.operation = "FLOAT", "GREATER_THAN"
        links.new(xyz.outputs["X"], compare.inputs["A"])
        compare.inputs["B"].default_value = 0.0
        value = compare.outputs[0]
    else:
        value = nodes.new("GeometryNodeInputIndex").outputs[0]
    if nodata_x_below is not None:
        below = nodes.new("FunctionNodeCompare")
        below.data_type, below.operation = "FLOAT", "LESS_THAN"
        links.new(xyz.outputs["X"], below.inputs["A"])
        below.inputs["B"].default_value = nodata_x_below
        switch = nodes.new("GeometryNodeSwitch")
        switch.input_type = "FLOAT"
        links.new(below.outputs[0], switch.inputs["Switch"])
        links.new(value, switch.inputs["False"])
        switch.inputs["True"].default_value = nodata
        value = switch.outputs[0]
    store = nodes.new("GeometryNodeStoreNamedAttribute")
    store.data_type, store.domain = data_type, domain
    links.new(geometry, store.inputs["Geometry"])
    store.inputs["Name"].default_value = name
    links.new(value, store.inputs["Value"])
    geometry = store.outputs[0]
    if delete_x_below is not None:
        below = nodes.new("FunctionNodeCompare")
        below.data_type, below.operation = "FLOAT", "LESS_THAN"
        links.new(xyz.outputs["X"], below.inputs["A"])
        below.inputs["B"].default_value = delete_x_below
        delete = nodes.new("GeometryNodeDeleteGeometry")
        delete.domain = "FACE"
        links.new(geometry, delete.inputs["Geometry"])
        links.new(below.outputs[0], delete.inputs["Selection"])
        geometry = delete.outputs[0]
    if offset_z:
        move = nodes.new("GeometryNodeSetPosition")
        move.inputs["Offset"].default_value = (0.0, 0.0, offset_z)
        links.new(geometry, move.inputs["Geometry"])
        geometry = move.outputs[0]
    links.new(geometry, nodes.new("NodeGroupOutput").inputs[0])
    return tree


def _add_modifier(obj: bpy.types.Object, tree: bpy.types.NodeTree, name: str = "GN"):
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = tree
    return modifier


def _arrays(mesh: bpy.types.Mesh):
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3).astype(np.float64), _bake().mesh_arrays(mesh)


def _face_centres(mesh: bpy.types.Mesh) -> np.ndarray:
    co, a = _arrays(mesh)
    corners = co[a.corner_vert]
    return np.add.reduceat(corners, a.loop_start, axis=0) / a.loop_total[:, None]


def _corner_samples(mesh: bpy.types.Mesh) -> np.ndarray:
    """Where the bake samples a CORNER layer: corners moved towards the face centre."""
    co, a = _arrays(mesh)
    centres = np.repeat(_face_centres(mesh), a.loop_total, axis=0)
    corners = co[a.corner_vert]
    return corners + _bake().CORNER_INSET * (centres - corners)


def _run(layer: str = "height", **kwargs) -> tuple[set[str], str | None]:
    kwargs.setdefault("modifier", "GN")
    kwargs.setdefault("attribute", "value")
    return h.call_op(bpy.ops.geotools.bake_geonodes_layer, layer=layer, **kwargs)


def _provenance(layer: str = "height") -> dict:
    return _layers().spec_from_props(_layers().find_layer(bpy.context.scene, layer)).provenance


def _datablocks() -> tuple[int, ...]:
    return tuple(
        len(c)
        for c in (
            bpy.data.objects,
            bpy.data.meshes,
            bpy.data.node_groups,
            bpy.data.collections,
            bpy.data.materials,
        )
    )


# --- registration -------------------------------------------------------------------------


def test_bake_registered() -> None:
    assert h.operator_available(OP)
    assert hasattr(bpy.types, "GEOTOOLS_PT_layers_bake")
    layer = h.submodule("blender.properties").GeoToolsLayer
    assert "vertex_bake" in layer.bl_rna.properties


# --- same topology -------------------------------------------------------------------------


def test_same_topology_face_attribute_is_copied() -> None:
    _clean()
    L, c = _layers(), _core()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 6))
    _add_modifier(obj, _tree("FACE", "index", data_type="INT"))
    L.ensure_layer(scene, _spec(units="m", label="Height"))
    result, error = _run()
    assert result == {"FINISHED"}, error
    item = L.find_layer(scene, "height")
    spec = L.spec_from_props(item)
    values = L.read_layer(obj.data, spec)
    assert np.array_equal(values, np.arange(36, dtype=np.float32))
    assert spec.origin is c.Origin.GEONODES
    assert (spec.units, spec.label) == ("m", "Height")
    assert (spec.value_min, spec.value_max) == (0.0, 35.0)
    prov = spec.provenance
    assert prov["tool"] == "bake_geonodes"
    assert prov["geotools_version"] == h.submodule("blender.common").extension_version()
    params = prov["parameters"]
    assert params["modifier"] == "GN" and params["node_group"] == "Test GN"
    assert params["attribute"] == "value" and params["method"] == "NEAREST"
    assert params["source_domains"] == ["FACE"] and params["same_topology"] == [True]
    assert params["counts"]["copied"] == 36 and params["counts"]["nodata"] == 0
    assert "max_distance" not in params
    settings = item.vertex_bake
    assert (settings.modifier, settings.attribute) == ("GN", "value")
    assert settings.last_report.startswith("Baked height on 1 object: 36 faces: 36 copied")


def test_same_topology_from_point_and_corner_domains() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    co, a = _arrays(obj.data)
    corner_z = _plane_z(co[:, 0], co[:, 1])[a.corner_vert]
    face_z = _plane_z(*_face_centres(obj.data)[:, :2].T)
    for source_domain in ("POINT", "CORNER"):
        obj.modifiers.clear()
        _add_modifier(obj, _tree(source_domain, "z"))
        for target, expected in (("FACE", face_z), ("CORNER", corner_z)):
            L.ensure_layer(scene, _spec(domain=target))
            result, error = _run()
            assert result == {"FINISHED"}, (source_domain, target, error)
            spec = L.spec_from_props(L.find_layer(scene, "height"))
            values = L.read_layer(obj.data, spec)
            np.testing.assert_allclose(values, expected, atol=1e-6, err_msg=source_domain)
            assert _provenance()["parameters"]["same_topology"] == [True]
    # A face attribute on a corner layer: every corner gets its face's value.
    obj.modifiers.clear()
    _add_modifier(obj, _tree("FACE", "z"))
    result, error = _run(domain="CORNER")
    assert result == {"FINISHED"}, error
    spec = L.spec_from_props(L.find_layer(scene, "height"))
    assert spec.domain.value == "CORNER"
    np.testing.assert_allclose(L.read_layer(obj.data, spec), np.repeat(face_z, 4), atol=1e-6)


def test_later_modifiers_are_left_out_and_restored() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    _add_modifier(obj, _tree("FACE", "index", data_type="INT"))
    tri = obj.modifiers.new("Triangulate", "TRIANGULATE")
    L.ensure_layer(scene, _spec())
    result, error = _run()
    assert result == {"FINISHED"}, error
    # The triangulation after the bake's modifier did not change its topology.
    assert _provenance()["parameters"]["counts"]["copied"] == 16
    assert tri.show_viewport and tri.show_render
    assert obj.modifiers["GN"].show_viewport


# --- changed topology ------------------------------------------------------------------------


def test_changed_topology_each_method_on_an_analytic_field() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 6))
    face_z = _plane_z(*_face_centres(obj.data)[:, :2].T)
    corner_z = _plane_z(*_corner_samples(obj.data)[:, :2].T)
    for source_domain in ("POINT", "CORNER", "FACE"):
        obj.modifiers.clear()
        _add_modifier(obj, _tree(source_domain, "z", subdivide=2))
        for method in ("NEAREST", "RAYCAST", "AREA_MEAN"):
            for target in ("FACE", "CORNER"):
                L.ensure_layer(scene, _spec(domain=target))
                result, error = _run(method=method, max_distance=0.5)
                where = (source_domain, method, target)
                assert result == {"FINISHED"}, (where, error)
                spec = L.spec_from_props(L.find_layer(scene, "height"))
                values = L.read_layer(obj.data, spec)
                params = _provenance()["parameters"]
                assert params["same_topology"] == [False], where
                counts = params["counts"]
                path = {"NEAREST": "nearest", "RAYCAST": "raycast", "AREA_MEAN": "area_mean"}
                assert counts[path[method]] == values.size, (where, counts)
                assert counts["nodata"] == 0, (where, counts)
                if target == "FACE":
                    expected = face_z
                elif method == "AREA_MEAN":
                    expected = np.repeat(face_z, 4)  # face values on every corner
                else:
                    expected = corner_z
                # A FACE attribute is constant per evaluated face (a quarter of a
                # source face's sub-face): only the area mean recovers the centre.
                atol = 1e-5
                if source_domain == "FACE" and method != "AREA_MEAN":
                    atol = 0.5 * 2 / 6 / 4 + 1e-5
                np.testing.assert_allclose(values, expected, atol=atol, err_msg=str(where))
                if method == "RAYCAST":
                    assert params["max_distance"] == 0.5


def test_changed_topology_category_regions() -> None:
    _clean()
    L, c = _layers(), _core()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 8))
    expected = (_face_centres(obj.data)[:, 0] > 0).astype(np.int32)
    for source_domain in ("FACE", "POINT"):
        obj.modifiers.clear()
        _add_modifier(obj, _tree(source_domain, "region", data_type="INT", subdivide=1))
        for method in ("NEAREST", "RAYCAST", "AREA_MEAN"):
            L.ensure_layer(scene, _spec("cover", data_type="CATEGORY", categories=("west", "east")))
            result, error = _run("cover", method=method)
            assert result == {"FINISHED"}, (source_domain, method, error)
            spec = L.spec_from_props(L.find_layer(scene, "cover"))
            values = L.read_layer(obj.data, spec)
            if source_domain == "FACE":
                assert np.array_equal(values, expected), (method, values)
            else:
                # Point values are 0/1 per vertex; the mode never invents a 0.5.
                assert set(np.unique(values).tolist()) <= {0, 1}
                interior = np.abs(_face_centres(obj.data)[:, 0]) > 0.3
                assert np.array_equal(values[interior], expected[interior]), method
            assert spec.data_type is c.DataType.CATEGORY


def test_raycast_hits_within_distance_and_falls_back_otherwise() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4, tilt=False))
    _add_modifier(obj, _tree("POINT", "x", subdivide=1, offset_z=0.3))
    L.ensure_layer(scene, _spec())
    expected = _face_centres(obj.data)[:, 0]
    for distance, path in ((1.0, "raycast"), (0.1, "fallback")):
        result, error = _run(method="RAYCAST", max_distance=distance)
        assert result == {"FINISHED"}, error
        counts = _provenance()["parameters"]["counts"]
        assert counts[path] == 16 and counts["nodata"] == 0, (distance, counts)
        spec = L.spec_from_props(L.find_layer(scene, "height"))
        np.testing.assert_allclose(L.read_layer(obj.data, spec), expected, atol=1e-5)


def test_area_mean_falls_back_where_nothing_arrives() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    # Faces left of x = -0.5 are deleted: their source faces receive nothing.
    _add_modifier(obj, _tree("POINT", "z", subdivide=1, delete_x_below=-0.5))
    L.ensure_layer(scene, _spec())
    result, error = _run(method="AREA_MEAN")
    assert result == {"FINISHED"}, error
    counts = _provenance()["parameters"]["counts"]
    assert counts["fallback"] == 4 and counts["area_mean"] == 12, counts
    spec = L.spec_from_props(L.find_layer(scene, "height"))
    values = L.read_layer(obj.data, spec)
    centres = _face_centres(obj.data)
    kept = centres[:, 0] > -0.5
    np.testing.assert_allclose(values[kept], _plane_z(*centres[kept, :2].T), atol=1e-5)
    # The fallback takes the nearest remaining surface, at x = -0.5.
    assert np.isfinite(values[~kept]).all()


def test_nodata_is_kept_and_counted() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    centres = _face_centres(obj.data)
    west = centres[:, 0] < 0
    # Same topology, face values: no-data west of x = 0.
    _add_modifier(obj, _tree("FACE", "z", nodata_x_below=0.0))
    L.ensure_layer(scene, _spec(nodata=-9999.0))
    result, error = _run()
    assert result == {"FINISHED"}, error
    counts = _provenance()["parameters"]["counts"]
    assert counts["copied"] == 8 and counts["nodata"] == 8, counts
    spec = L.spec_from_props(L.find_layer(scene, "height"))
    values = L.read_layer(obj.data, spec)
    assert (values[west] == -9999.0).all()
    assert spec.value_min > -9999.0

    # Changed topology, point values: interpolation never mixes in the no-data.
    obj.modifiers.clear()
    _add_modifier(obj, _tree("POINT", "z", subdivide=1, nodata_x_below=-0.01))
    for method in ("NEAREST", "RAYCAST", "AREA_MEAN"):
        result, error = _run(method=method)
        assert result == {"FINISHED"}, error
        values = L.read_layer(obj.data, spec)
        good = values != -9999.0
        assert (values[good] > -2.0).all(), (method, values)
        np.testing.assert_allclose(
            values[~west], _plane_z(*centres[~west, :2].T), atol=1e-5, err_msg=method
        )
        counts = _provenance()["parameters"]["counts"]
        assert counts["nodata"] == np.count_nonzero(~good), (method, counts)

    # A float layer without no-data stores NaN.
    L.ensure_layer(scene, _spec())
    obj.modifiers.clear()
    _add_modifier(obj, _tree("FACE", "z", nodata_x_below=0.0, nodata=float("nan")))
    result, error = _run()
    assert result == {"FINISHED"}, error
    values = L.read_layer(obj.data, L.spec_from_props(L.find_layer(scene, "height")))
    assert np.isnan(values[west]).all() and np.isfinite(values[~west]).all()


# --- options, state and errors ----------------------------------------------------------------


def test_modifier_off_option() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    modifier = _add_modifier(obj, _tree("POINT", "z", subdivide=1))
    L.ensure_layer(scene, _spec())
    result, error = _run(disable_modifier=False)
    assert result == {"FINISHED"}, error
    assert modifier.show_viewport and modifier.show_render
    assert _provenance()["parameters"]["modifier_disabled"] is False
    result, error = _run(disable_modifier=True)
    assert result == {"FINISHED"}, error
    assert not modifier.show_viewport and not modifier.show_render
    assert _provenance()["parameters"]["modifier_disabled"] is True
    # The export now sees the source topology with the baked values.
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.to_mesh()
    try:
        assert len(mesh.polygons) == 16 and "gt_height" in mesh.attributes
    finally:
        evaluated.to_mesh_clear()
    # Baking again turns it on for the evaluation only.
    result, error = _run(method="AREA_MEAN", disable_modifier=True)
    assert result == {"FINISHED"}, error
    assert _provenance()["parameters"]["counts"]["area_mean"] == 16
    assert not modifier.show_viewport and not modifier.show_render


def test_no_temporary_datablocks_are_left() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    objects = _source(_grid("Grid_A", 4), _grid("Grid_B", 3))
    for obj in objects:
        _add_modifier(obj, _tree("POINT", "z", subdivide=1))
    L.ensure_layer(scene, _spec())
    before = _datablocks()
    for method in ("NEAREST", "RAYCAST", "AREA_MEAN"):
        result, error = _run(method=method)
        assert result == {"FINISHED"}, error
        assert _datablocks() == before, (method, before, _datablocks())
    assert not any(o.name.startswith(_bake().TEMP_NAME) for o in bpy.data.objects)

    # A failure on the second object leaves no temporary data and writes nothing.
    L.ensure_layer(scene, _spec("other"))
    objects[1].modifiers["GN"].node_group = _tree("POINT", "z", name="elsewhere", subdivide=1)
    before = _datablocks()
    result, error = _run("other", method="AREA_MEAN")
    assert result == {"ERROR_REPORTED"} and "no attribute 'value'" in error, error
    assert _datablocks() == before
    assert all("gt_other" not in o.data.attributes for o in objects)


def test_restores_mode_selection_and_active_object() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    obj, other = _source(_grid("Grid_A", 4), _grid("Grid_B", 2))
    _add_modifier(obj, _tree("FACE", "index", data_type="INT"))
    L.ensure_layer(scene, _spec())
    view_layer = bpy.context.view_layer
    for o in bpy.data.objects:
        o.select_set(False)
    other.select_set(True)
    obj.select_set(True)
    view_layer.objects.active = obj
    assert bpy.ops.object.mode_set(mode="EDIT") == {"FINISHED"}
    try:
        result, error = _run()
        assert result == {"FINISHED"}, error
        assert obj.mode == "EDIT"
        assert view_layer.objects.active == obj
        assert {o.name for o in view_layer.objects if o.select_get()} == {"Grid_A", "Grid_B"}
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")
    spec = L.spec_from_props(L.find_layer(scene, "height"))
    assert np.array_equal(L.read_layer(obj.data, spec), np.arange(16, dtype=np.float32))
    # Grid_B has no modifier: it is skipped and reported.
    assert "1 without the modifier" in L.find_layer(scene, "height").vertex_bake.last_report


def test_operator_report_and_errors() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    _add_modifier(obj, _tree("POINT", "z", subdivide=1))
    L.ensure_layer(scene, _spec())
    cls = bpy.types.GEOTOOLS_OT_bake_geonodes_layer
    recorder = h.ReportRecorder()
    for key, value in {
        "layer": "",  # the selected layer
        "modifier": "GN",
        "attribute": "value",
        "domain": "LAYER",
        "method": "RAYCAST",
        "max_distance": 1.0,
        "disable_modifier": False,
    }.items():
        setattr(recorder, key, value)
    assert cls.execute(recorder, bpy.context) == {"FINISHED"}
    ((levels, message),) = recorder.reports
    assert levels == {"INFO"}, recorder.reports
    assert message == "Baked height on 1 object: 16 faces: 16 raycast, 0 no-data", message

    L.ensure_layer(scene, _spec("fresh"))
    for kwargs, text in (
        ({"modifier": "Nope"}, "no source object has a Geometry Nodes modifier 'Nope'"),
        ({"attribute": "missing"}, "Grid: the evaluated mesh has no attribute 'missing'"),
        ({"attribute": ""}, "name the attribute"),
        ({"layer": "nothing"}, "No layer 'nothing'"),
    ):
        result, error = _run(**{"layer": "fresh", **kwargs})
        assert result == {"ERROR_REPORTED"} and text in error, (kwargs, error)
    assert "gt_fresh" not in obj.data.attributes
    assert L.find_layer(scene, "fresh").provenance == ""

    # A vector attribute is refused.
    obj.modifiers.clear()
    _add_modifier(obj, _tree("POINT", "z", data_type="FLOAT_VECTOR"))
    result, error = _run()
    assert result == {"ERROR_REPORTED"} and "FLOAT_VECTOR" in error, error

    # A texture layer needs a texture bake, unless a domain is chosen.
    obj.modifiers.clear()
    _add_modifier(obj, _tree("FACE", "z"))
    L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    result, error = _run("shade")
    assert result == {"ERROR_REPORTED"} and "texture layer" in error, error
    result, error = _run("shade", domain="FACE")
    assert result == {"FINISHED"}, error
    assert L.find_layer(scene, "shade").domain == "FACE"


def test_panel_draws() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    panel = bpy.types.GEOTOOLS_PT_layers_bake
    assert not panel.poll(bpy.context)
    (obj,) = _source(_grid("Grid", 2))
    L.ensure_layer(scene, _spec())
    assert panel.poll(bpy.context)
    calls = h.draw(panel)
    drawn = h.props_drawn(calls)
    assert {"modifier", "attribute", "method", "disable_modifier"} <= set(drawn), drawn
    assert "max_distance" not in drawn
    assert ("operator", OP, "NODETREE") in calls

    _add_modifier(obj, _tree("FACE", "z"))
    settings = L.find_layer(scene, "height").vertex_bake
    settings.modifier, settings.attribute, settings.method = "GN", "value", "RAYCAST"
    calls = h.draw(panel)
    assert "max_distance" in h.props_drawn(calls)
    assert ("label", "On 1 of 1 source objects", "INFO") in calls, calls
    assert bpy.ops.geotools.bake_geonodes_layer(
        layer="height", modifier="GN", attribute="value"
    ) == {"FINISHED"}
    labels = h.label_text(h.draw(panel))
    assert "Baked height on 1 object" in labels, labels
    assert h.draw_header(panel) == "CHECKMARK"
    settings.modifier = "Missing"
    assert ("label", "No source object has Missing", "ERROR") in h.draw(panel)

    L.ensure_layer(scene, _spec("shade", "TEXTURE"))
    h.project().layers_index = 1
    assert ("operator", OP, "NODETREE") not in h.draw(panel)


# --- persistence ------------------------------------------------------------------------------


def test_baked_layer_survives_save_and_reopen() -> None:
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Grid", 4))
    _add_modifier(obj, _tree("POINT", "z", subdivide=1))
    L.ensure_layer(scene, _spec(nodata=-9999.0))
    result, error = _run(method="AREA_MEAN", disable_modifier=True)
    assert result == {"FINISHED"}, error
    spec = L.spec_from_props(L.find_layer(scene, "height"))
    values = L.read_layer(obj.data, spec)
    try:
        with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
            path = Path(tmp) / "bake.blend"
            assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}
            _clean()
            assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
            assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}
            scene = bpy.context.scene
            item = L.find_layer(scene, "height")
            assert L.spec_from_props(item) == spec
            assert item.vertex_bake.modifier == "GN"
            assert item.vertex_bake.method == "AREA_MEAN"
            assert item.vertex_bake.last_report.startswith("Baked height")
            (obj,) = L.source_objects(scene)
            assert np.array_equal(L.read_layer(obj.data, spec), values)
            assert not obj.modifiers["GN"].show_viewport
            assert L.layer_status(scene, item) == []
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        _clean()


# --- performance ---------------------------------------------------------------------------------


def test_performance() -> None:
    """A 1M-face same-topology bake, and a changed-topology bake per method."""
    _clean()
    L = _layers()
    scene = bpy.context.scene
    (obj,) = _source(_grid("Big", 1000))
    _add_modifier(obj, _tree("FACE", "z"))
    L.ensure_layer(scene, _spec())
    bpy.context.evaluated_depsgraph_get()
    started = time.perf_counter()
    result, error = _run()
    elapsed = time.perf_counter() - started
    assert result == {"FINISHED"}, error
    assert _provenance()["parameters"]["counts"]["copied"] == 1_000_000
    print(f"    same topology, 1,000,000 faces: {elapsed:.2f}s")
    timings = [elapsed]

    _clean()
    (obj,) = _source(_grid("Medium", 100))
    _add_modifier(obj, _tree("POINT", "z", subdivide=2))
    L.ensure_layer(scene, _spec())
    for method in ("NEAREST", "RAYCAST", "AREA_MEAN"):
        started = time.perf_counter()
        result, error = _run(method=method)
        elapsed = time.perf_counter() - started
        assert result == {"FINISHED"}, error
        print(f"    changed topology, 10,000 -> 160,000 faces, {method}: {elapsed:.2f}s")
        timings.append(elapsed)
    # Generous bounds: a Python loop over the faces would take far longer.
    assert timings[0] < 30.0 and max(timings[1:]) < 30.0, timings
    _clean()
