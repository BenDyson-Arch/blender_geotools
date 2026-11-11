"""Vector publish (M8): a build profile of format Vectors, end to end.

A small synthetic GeoJSON layer in GDA2020 / MGA zone 56 (the project CRS is
EPSG:7856, which rusty-tiles 0.4.1's allow-list resolves) is imported as a
linked object, raised by a stock
Geometry Nodes modifier, published with ``vector_to_3tz``, validated, and
republished with ``reuse_tileset`` after an edit and a refresh.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import _vector_fixtures as vf
import bpy
import numpy as np

E0, N0, A0 = vf.E0, vf.N0, 10.0
LIFT = 2.5  # metres the Geometry Nodes modifier raises the features
JOIN_SECONDS = 180.0


def _available() -> bool:
    return h.CONFIG.expect == "available"


@contextmanager
def _tmpdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-vector-publish-"))
    try:
        yield path
    finally:
        h.submodule("blender.publish").shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


def _project(mode: str = "SOURCE_CRS") -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = mode
    if mode == "SOURCE_CRS":
        props.source_crs = "EPSG:7856"
        props.source_offset_e = repr(E0)
        props.source_offset_n = repr(N0)
        props.source_offset_a = repr(A0)
    elif mode == "ANCHOR":
        props.anchor_lon, props.anchor_lat, props.anchor_height = "153.02", "-27.47", "10.0"


def _layer(tmp: Path, depth: float = 0.5) -> tuple[Path, bpy.types.Object]:
    """Four squares (two with holes) at Z 12 m, a line and a point; imported and linked."""
    features = vf.contexts(2, z=12.0)
    for f in features:
        f["properties"]["depth"] = depth
    features.append(
        vf.feature(
            {"type": "LineString", "coordinates": [[E0, N0 - 5, 12], [E0 + 30, N0 - 5, 13]]},
            {"context": "wall", "depth": 0.0, "phase": 2, "sealed": False},
            10,
        )
    )
    path = vf.write_geojson(tmp / "dig.geojson", features)
    collection = bpy.data.collections.new("Vectors")
    bpy.context.scene.collection.children.link(collection)
    layer = bpy.context.view_layer.layer_collection.children[collection.name]
    bpy.context.view_layer.active_layer_collection = layer
    result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(path))
    assert error is None and result == {"FINISHED"}, error
    polygons = bpy.data.objects["dig"]
    assert {o.name for o in collection.objects} == {"dig", "dig.lines"}
    _lift(polygons)
    return path, polygons


def _lift(obj: bpy.types.Object) -> bpy.types.NodeTree:
    """A stock-node modifier: Set Position with an offset of LIFT metres up."""
    tree = bpy.data.node_groups.new("Lift", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    gi, go = tree.nodes.new("NodeGroupInput"), tree.nodes.new("NodeGroupOutput")
    up = tree.nodes.new("ShaderNodeCombineXYZ")
    up.inputs["Z"].default_value = LIFT
    move = tree.nodes.new("GeometryNodeSetPosition")
    tree.links.new(gi.outputs[0], move.inputs["Geometry"])
    tree.links.new(up.outputs[0], move.inputs["Offset"])
    tree.links.new(move.outputs[0], go.inputs[0])
    obj.modifiers.new("Lift", "NODES").node_group = tree
    return tree


def _profile(output: Path, **settings: object) -> bpy.types.PropertyGroup:
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    props = h.project()
    profile = props.build_profiles[props.build_profile_index]
    profile.output_type = "VECTOR"
    profile.output_path = str(output)
    profile.vector_collection = bpy.data.collections.get("Vectors")
    profile.overwrite = True
    for key, value in settings.items():
        setattr(profile, key, value)
    return profile


def _finish() -> object:
    publish = h.submodule("blender.publish")
    job = publish.active_job()
    assert job is not None, "no publish job is running"
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    assert publish.update_from_job() is None
    assert not publish.is_running()
    return job


def _record(output: Path) -> dict:
    return json.loads((output.with_suffix(".geotools") / "build.json").read_text())


def _published_gpkg(output: Path):
    gpkg = h.submodule("core.vector.gpkg")
    return output.with_suffix(".geotools") / "vectors.gpkg", gpkg


# --- registration, drawing and refusals ------------------------------------------------


def test_vector_format_registered_and_drawn() -> None:
    _project()
    with _tmpdir() as tmp:
        _layer(tmp)
        profile = _profile(tmp / "dig.3tz")
        assert "VECTOR" in profile.bl_rna.properties["output_type"].enum_items
        assert profile.vector_reuse is True and profile.vector_skip_invalid is False
        publish = h.submodule("blender.publish")
        assert publish.profile_suffix(profile) == ".3tz"
        assert publish.output_name(bpy.context.scene, profile) == "dig.3tz"
        panels = h.submodule("blender.panels")
        drawn = [(kind, text) for kind, text, _icon in h.draw(panels.GEOTOOLS_PT_publish)]
        assert ("prop", "vector_collection") in drawn, drawn
        assert ("prop", "variant_layer") not in drawn  # prop_search draws as "prop"
        assert h.operator_text("geotools.publish") == "Publish dig.3tz"
        settings = [(k, t) for k, t, _i in h.draw(panels.GEOTOOLS_PT_publish_settings)]
        for name in ("apply_modifiers", "vector_reuse", "vector_skip_invalid", "jobs"):
            assert ("prop", name) in settings, settings
        assert ("prop", "memory_budget_gib") not in settings
        profiles = h.submodule("blender.profiles")
        item = h.draw_list_item(profiles.GEOTOOLS_UL_build_profiles, h.project(), profile)
        assert ("label", "Vectors") in [(k, t) for k, t, _i in item]
        text = publish.GEOTOOLS_OT_publish.description(bpy.context, type("P", (), {"layer": ""}))
        assert "vector features" in text and "Vectors" in text, text


def test_vector_publish_refusals() -> None:
    _project()
    with _tmpdir() as tmp:
        _layer(tmp)
        profile = _profile(tmp / "dig.3tz", vector_collection=None)
        _result, error = h.call_op(bpy.ops.geotools.publish)
        assert error and "collection of vector objects" in error, error

        empty = bpy.data.collections.new("Empty")
        bpy.context.scene.collection.children.link(empty)
        profile.vector_collection = empty
        _result, error = h.call_op(bpy.ops.geotools.publish)
        assert error and "has no mesh objects" in error, error

        profile.vector_collection = bpy.data.collections["Vectors"]
        profile.variant_layer = "slope"
        _result, error = h.call_op(bpy.ops.geotools.publish)
        assert error and "variants" in error, error
        profile.variant_layer = ""

        # Vectors cannot be anchored: rusty-tiles places them by their CRS.
        h.project().placement_mode = "ANCHOR"
        _result, error = h.call_op(bpy.ops.geotools.publish)
        assert error and "cannot anchor" in error, error
        h.project().placement_mode = "SOURCE_CRS"

        if not _available():
            _result, error = h.call_op(bpy.ops.geotools.publish)
            assert error and "rusty-tiles" in error.lower(), error
        assert not h.submodule("blender.publish").is_running()
        # No source collection is needed for vectors (the mesh publish needs one).
        assert h.project().source_collection is None


# --- end to end -------------------------------------------------------------------------


def test_vector_publish_end_to_end_with_reuse() -> None:
    if not _available():
        return
    _project()
    with _tmpdir() as tmp:
        path, polygons = _layer(tmp)
        output = tmp / "dig.3tz"
        profile = _profile(output)
        result, error = h.call_op(bpy.ops.geotools.publish)
        assert error is None and result == {"FINISHED"}, error
        job = _finish()
        assert job.state.name == "DONE", job.error

        record = _record(output)
        assert record["output_type"] == "vector"
        # The plain EPSG code, passed explicitly, and its datum operation (rusty-tiles 0.4.1).
        operation = record["placement"].pop("crs_operation")
        assert record["placement"] == {
            "source_crs": "EPSG:7856",
            "height_offset": 0.0,
            "crs_epsg": "EPSG:7856",
        }
        assert operation["operationId"] == "EPSG:8450" and operation["accuracyMetres"] == 3.0
        assert record["validation"]["ok"] is True
        assert record["geotools"]["vector_collection"] == "Vectors"
        assert record["geotools"]["apply_modifiers"] is True
        tables = {t["table"]: t for t in record["vectors"]["tables"]}
        assert tables["dig"]["features"] == 4 and tables["dig"]["kind"] == "polygons"
        assert tables["dig_lines"]["features"] == 1
        reuse = record["vectors"]["reuse"]
        assert (reuse["requested"], reuse["used"], reuse["reason"]) == (
            True,
            False,
            "no previous archive",
        )
        for layer in record["report_summary"]["layers"]:
            assert layer["sourceCrs"] == "EPSG:7856" and layer["crsOperation"] == operation
        assert profile.last_features == "5" and profile.last_validation == "OK"
        assert profile.last_triangles == "" and profile.last_tiles

        # The GeoPackage tiled: project CRS, offsets restored in binary64, the
        # modifier's lift applied (Z = 12 + LIFT, absolute, not local).
        gpkg_path, gpkg = _published_gpkg(output)
        data = gpkg.read_layer(gpkg_path, "dig")
        coords = data.geometries[h.submodule("core.vector.model").Kind.POLYGONS].coords
        assert np.allclose(coords[:, 2], 12.0 + LIFT, atol=1e-4)
        assert coords[:, 0].min() == E0  # float32 local 0.0 plus the offset, exactly
        assert np.all(np.abs(coords[:, :2] - [E0 + 15, N0 + 15]) < 20)
        assert sorted(data.fids.tolist()) == [1, 2, 3, 4]

        # Last Build shows the features.
        panels = h.submodule("blender.panels")
        labels = [t for k, t, _i in h.draw(panels.GEOTOOLS_PT_publish_last) if k == "label"]
        assert "Features" in labels and "Triangles" not in labels, labels

        # An edit in the source, a refresh, a republish: reuse_tileset is used.
        features = json.loads(path.read_text(encoding="utf-8"))
        features["features"][0]["properties"]["depth"] = 0.9
        path.write_text(json.dumps(features, indent=2), encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        refreshed = h.submodule("blender.links").refresh_link(polygons)
        assert refreshed.changed == 1, refreshed
        assert polygons.modifiers["Lift"].node_group is not None  # the modifier survives
        assert h.call_op(bpy.ops.geotools.publish) == ({"FINISHED"}, None)
        job = _finish()
        assert job.state.name == "DONE", job.error
        record = _record(output)
        reuse = record["vectors"]["reuse"]
        assert reuse["used"] is True, reuse
        assert reuse["report"]["previousTileset"] is True
        assert record["validation"]["ok"] is True
        depth = gpkg.read_layer(gpkg_path, "dig").columns["depth"].values
        assert abs(depth.max() - 0.9) < 1e-6  # a FLOAT attribute is float32

        # Apply Modifiers off: the original mesh data, without the lift.
        profile.apply_modifiers = False
        profile.vector_reuse = False
        assert h.call_op(bpy.ops.geotools.publish) == ({"FINISHED"}, None)
        job = _finish()
        assert job.state.name == "DONE", job.error
        record = _record(output)
        assert record["geotools"]["apply_modifiers"] is False
        reuse = record["vectors"]["reuse"]
        assert (reuse["requested"], reuse["used"]) == (False, False), reuse
        assert reuse["report"]["requestedPreviousTileset"] is False
        data = gpkg.read_layer(gpkg_path, "dig")
        coords = data.geometries[h.submodule("core.vector.model").Kind.POLYGONS].coords
        assert np.allclose(coords[:, 2], 12.0, atol=1e-4)


def test_vector_publish_local_project() -> None:
    """A LOCAL project publishes local metres (source_crs "local")."""
    if not _available():
        return
    _project("LOCAL")
    with _tmpdir() as tmp:
        collection = bpy.data.collections.new("Vectors")
        bpy.context.scene.collection.children.link(collection)
        mesh = bpy.data.meshes.new("plot")
        mesh.from_pydata([(0, 0, 1), (4, 0, 1), (4, 3, 1.5), (0, 3, 1.2)], [], [(0, 1, 2, 3)])
        obj = bpy.data.objects.new("plot", mesh)
        collection.objects.link(obj)
        output = tmp / "plot.3tz"
        _profile(output)
        result, error = h.call_op(bpy.ops.geotools.publish)
        assert error is None and result == {"FINISHED"}, error
        job = _finish()
        assert job.state.name == "DONE", job.error
        record = _record(output)
        assert record["placement"]["source_crs"] == "local"
        assert record["vectors"]["crs"] is None
        assert record["validation"]["ok"] is True
