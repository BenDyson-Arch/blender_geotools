"""GeoJSON and GeoPackage import, linked refresh, write-back and export (M8 stream 1)."""

from __future__ import annotations

import json
import os
import sqlite3
import struct
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import _vector_fixtures as vf
import bpy
import numpy as np

E0, N0, A0 = vf.E0, vf.N0, 10.0
COLUMNS = {"context": "TEXT", "depth": "REAL", "phase": "INTEGER", "sealed": "BOOLEAN"}


@contextmanager
def _tmpdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="geotools-vectors-") as tmp:
        yield Path(tmp)


def _vectors():
    return h.submodule("blender.vectors")


def _links():
    return h.submodule("blender.links")


def _project(mode: str = "SOURCE_CRS") -> None:
    h.reset_scene()
    props = h.project()
    if mode == "SOURCE_CRS":
        preset = h.submodule("core.crs_presets").get("MGA2020_56")
        props.placement_mode = "SOURCE_CRS"
        props.source_crs = preset.definition
        props.source_offset_e = repr(E0)
        props.source_offset_n = repr(N0)
        props.source_offset_a = repr(A0)
    else:
        props.placement_mode = mode


def _gpkg_rows(features: list[dict]) -> list[tuple]:
    return [(f["id"], f["geometry"], f["properties"]) for f in features]


def _mixed_features() -> list[dict]:
    """Polygons (some with holes, one 3D), a line and two points, in MGA zone 56."""
    features = vf.contexts(2)
    features.append(
        vf.feature(
            {
                "type": "LineString",
                "coordinates": [[E0, N0 - 5, 1.5], [E0 + 30, N0 - 5, 2.5], [E0 + 30, N0 - 9, 3]],
            },
            {"context": "wall", "depth": 0.5, "phase": 2, "sealed": False},
            10,
        )
    )
    features.append(
        vf.feature(
            {"type": "MultiPoint", "coordinates": [[E0 + 0.125, N0 + 0.375], [E0 + 1, N0 + 2]]},
            {"context": "find", "depth": 0.1, "phase": 1, "sealed": True},
            11,
        )
    )
    return features


def _attr(mesh: bpy.types.Mesh, name: str) -> np.ndarray:
    attr = mesh.attributes[name]
    dtype = {"FLOAT": np.float32, "INT": np.int32, "BOOLEAN": bool}[attr.data_type]
    out = np.empty(len(attr.data), dtype=dtype)
    attr.data.foreach_get("value", out)
    return out


def _positions(mesh: bpy.types.Mesh) -> np.ndarray:
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def _face_area(mesh: bpy.types.Mesh) -> float:
    areas = np.empty(len(mesh.polygons), dtype=np.float32)
    mesh.polygons.foreach_get("area", areas)
    return float(areas.astype(np.float64).sum())


def _import_gpkg(path: Path, **kwargs) -> tuple[set[str], str | None]:
    return h.call_op(bpy.ops.geotools.import_gpkg, filepath=str(path), **kwargs)


def _bump(path: Path) -> None:
    """Make sure the next write gets a new mtime, however coarse the clock."""
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - 10_000_000_000))


# --- registration and Blender behaviour ---------------------------------------------------


def test_vector_operators_registered() -> None:
    for idname in (
        "geotools.import_geojson",
        "geotools.import_gpkg",
        "geotools.export_vector",
        "geotools.link_refresh",
        "geotools.link_check",
        "geotools.link_unlink",
        "geotools.link_filters",
        "geotools.link_write_back",
    ):
        assert h.operator_available(idname), idname
    assert hasattr(bpy.types.Object, "geotools_link")
    links = _links()
    assert {"GEOJSON", "GPKG"} <= set(links.REFRESHERS)
    assert links._on_load_post in bpy.app.handlers.load_post
    assert bpy.app.timers.is_registered(links._timer)
    # The interface stream 2 (GeoTIFF) codes against.
    fields = set(links.GeoToolsLink.bl_rna.properties.keys())
    assert {
        "kind",
        "filepath",
        "layer",
        "fid_field",
        "crs",
        "options",
        "fingerprint",
        "last_sync",
        "auto_refresh",
        "status",
    } <= fields
    kinds = links.GeoToolsLink.bl_rna.properties["kind"].enum_items.keys()
    assert {"GEOJSON", "GPKG", "GEOTIFF"} <= set(kinds)


def test_blender_string_attributes() -> None:
    """Why text columns are STRING attributes set one element at a time (see the design)."""
    mesh = bpy.data.meshes.new("strings")
    try:
        mesh.vertices.add(4)
        mesh.edges.add(1)
        mesh.edges.foreach_set("vertices", [0, 1])
        mesh.loops.add(3)
        mesh.loops.foreach_set("vertex_index", [1, 2, 3])
        mesh.polygons.add(1)
        mesh.polygons.foreach_set("loop_start", [0])
        mesh.update(calc_edges=True)
        for domain in ("POINT", "EDGE", "FACE", "CORNER"):
            attr = mesh.attributes.new(f"s_{domain}", "STRING", domain)
            assert attr.data_type == "STRING" and len(attr.data) > 0
            attr.data[0].value = "café".encode()
            assert attr.data[0].value.decode() == "café"
        try:
            mesh.attributes["s_FACE"].data.foreach_set("value", [b"x"])
        except (RuntimeError, TypeError):
            pass
        else:
            raise AssertionError("foreach_set now takes STRING attributes: vectorise the writes")
    finally:
        bpy.data.meshes.remove(mesh)


# --- import ------------------------------------------------------------------------------


def test_import_geojson_kinds_domains_and_placement() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_geojson(tmp / "dig.geojson", _mixed_features())
        result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(path))
        assert result == {"FINISHED"}, error
        names = sorted(o.name for o in bpy.data.objects)
        assert names == ["dig", "dig.lines", "dig.points"], names

        polys = bpy.data.objects["dig"].data
        assert len(polys.polygons) > 4  # holes are tessellated
        # Every attribute on FACE, with the feature's value repeated on its triangles.
        for name, kind in (("fid", "INT"), ("depth", "FLOAT"), ("phase", "INT")):
            attr = polys.attributes[name]
            assert (attr.domain, attr.data_type) == ("FACE", kind), name
        assert polys.attributes["sealed"].data_type == "BOOLEAN"
        assert polys.attributes["context"].data_type == "STRING"
        fid = _attr(polys, "fid")
        assert sorted(set(fid.tolist())) == [1, 2, 3, 4]
        depth = _attr(polys, "depth")
        np.testing.assert_array_equal(depth, (0.25 * fid).astype(np.float32))
        contexts = {
            f: polys.attributes["context"].data[i].value.decode() for i, f in enumerate(fid)
        }
        assert contexts == {1: "C001", 2: "C002", 3: "C003", 4: "C004"}
        # Two 10 m squares with a 4 m hole and two without.
        assert abs(_face_area(polys) - (4 * 100 - 2 * 16)) < 1e-3
        normals = np.empty(len(polys.polygons) * 3, np.float32)
        polys.polygons.foreach_get("normal", normals)
        assert (normals.reshape(-1, 3)[:, 2] > 0.99).all()  # all faces point up
        # Project frame: E - offset E; 2D data gets z = 0.
        pos = _positions(polys)
        assert pos[:, 0].min() == 0.0 and pos[:, 1].min() == 0.0 and (pos[:, 2] == 0).all()

        lines = bpy.data.objects["dig.lines"].data
        assert len(lines.edges) == 2 and len(lines.polygons) == 0
        assert lines.attributes["fid"].domain == "EDGE"
        assert lines.attributes["context"].domain == "EDGE"
        np.testing.assert_array_equal(_positions(lines)[:, 2], [1.5 - A0, 2.5 - A0, 3 - A0])

        points = bpy.data.objects["dig.points"].data
        assert len(points.vertices) == 2 and len(points.edges) == 0
        assert points.attributes["phase"].domain == "POINT"
        # Shifted in binary64, then rounded: the fractions survive at 6.96 million m N.
        assert _positions(points)[0].tolist() == [0.125, 0.375, 0.0]

        link = bpy.data.objects["dig.lines"].geotools_link
        assert (link.kind, link.layer, link.fid_field) == ("GEOJSON", "dig", "id")
        assert Path(link.filepath) == path and link.status == "OK" and link.last_sync
        options = json.loads(link.options)
        assert options["kind"] == "lines" and options["columns"]["context"] == "context"
        assert json.loads(link.crs)["epsg"] == "EPSG:7856"
        assert json.loads(link.fingerprint)["sha256"]


def test_import_gpkg_layers_and_filter() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_gpkg(tmp / "site.gpkg", "contexts", _gpkg_rows(vf.contexts(3)), COLUMNS)
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE walls (fid INTEGER PRIMARY KEY, geom LINESTRING, name TEXT)")
        con.execute(
            "INSERT INTO gpkg_contents (table_name, data_type, srs_id) VALUES (?, ?, ?)",
            ("walls", "features", 7856),
        )
        con.execute(
            "INSERT INTO gpkg_geometry_columns VALUES ('walls', 'geom', 'LINESTRING', 7856, 0, 0)"
        )
        line = {"type": "LineString", "coordinates": [[E0, N0], [E0 + 5, N0]]}
        con.execute("INSERT INTO walls VALUES (1, ?, 'north')", (vf.gpkg_geometry(line, 7856),))
        con.commit()
        con.close()
        result, error = _import_gpkg(path)
        assert result == {"FINISHED"}, error
        assert sorted(o.name for o in bpy.data.objects) == ["contexts", "walls"]
        walls = bpy.data.objects["walls"]
        assert walls.geotools_link.layer == "walls" and len(walls.data.edges) == 1

        h.reset_scene()
        _project()
        result, error = _import_gpkg(path, layer="contexts", where="phase = 1")
        assert result == {"FINISHED"}, error
        obj = bpy.data.objects["contexts"]
        assert sorted(set(_attr(obj.data, "fid").tolist())) == [1, 4, 7]
        assert json.loads(obj.geotools_link.options)["where"] == "phase = 1"
        result, error = _import_gpkg(path, layer="contexts", where="1; DROP TABLE contexts")
        assert error and "';'" in error, error
        assert len(bpy.data.objects) == 1


def test_import_crs_mismatch_is_refused() -> None:
    _project()
    with _tmpdir() as tmp:
        wgs84 = vf.write_geojson(
            tmp / "wgs.geojson",
            [vf.feature({"type": "Point", "coordinates": [153.0, -27.5]}, {}, 1)],
            crs=None,
        )
        result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(wgs84))
        assert result == {"ERROR_REPORTED"}
        assert "EPSG:4326" in error and "EPSG:7856" in error and "QGIS" in error, error
        assert not bpy.data.objects and not bpy.data.meshes

        unknown = vf.write_gpkg(
            tmp / "unknown.gpkg",
            "t",
            _gpkg_rows(vf.contexts(1)),
            COLUMNS,
            srs_id=-1,
            organization="NONE",
            definition="undefined",
        )
        result, error = _import_gpkg(unknown)
        assert error and "Data Is in Project CRS" in error, error
        result, error = _import_gpkg(unknown, assume_project_crs=True)
        assert result == {"FINISHED"}, error
        crs = json.loads(bpy.data.objects["t"].geotools_link.crs)
        assert crs["source"] == "user" and crs["epsg"] == "EPSG:7856"


def test_import_in_a_local_project_warns() -> None:
    _project("LOCAL")
    with _tmpdir() as tmp:
        path = vf.write_geojson(
            tmp / "local.geojson",
            [vf.feature({"type": "Point", "coordinates": [3.0, 4.0, 5.0]}, {}, 1)],
            crs=None,
        )
        bpy.ops.geotools.import_geojson(filepath=str(path))
        assert _positions(bpy.data.objects["local"].data).tolist() == [[3.0, 4.0, 5.0]]


def test_degenerate_parts_are_skipped() -> None:
    _project()
    with _tmpdir() as tmp:
        bad = [[E0, N0], [E0 + 1, N0], [E0, N0], [E0 + 1, N0]]  # two distinct points
        geom = {"type": "MultiPolygon", "coordinates": [[bad], [vf.square(E0, N0, 2.0)]]}
        path = vf.write_gpkg(tmp / "d.gpkg", "d", [(1, geom, {})], {})
        result, error = _import_gpkg(path)
        assert result == {"FINISHED"}, error
        assert len(bpy.data.objects["d"].data.polygons) == 1


# --- refresh -----------------------------------------------------------------------------


def _drape_modifier(obj: bpy.types.Object, slope: float = 0.1, base: float = 5.0) -> None:
    """A Geometry Nodes 'drape': z = base + slope * x, as a draping recipe would produce."""
    group = bpy.data.node_groups.new("Test Drape", "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links
    gin, gout = nodes.new("NodeGroupInput"), nodes.new("NodeGroupOutput")
    pos = nodes.new("GeometryNodeInputPosition")
    sep = nodes.new("ShaderNodeSeparateXYZ")
    mul = nodes.new("ShaderNodeMath")
    mul.operation = "MULTIPLY_ADD"
    mul.inputs[1].default_value = slope
    mul.inputs[2].default_value = base
    comb = nodes.new("ShaderNodeCombineXYZ")
    setp = nodes.new("GeometryNodeSetPosition")
    links.new(gin.outputs[0], setp.inputs["Geometry"])
    links.new(pos.outputs[0], sep.inputs[0])
    links.new(sep.outputs[0], mul.inputs[0])
    links.new(sep.outputs[0], comb.inputs[0])
    links.new(sep.outputs[1], comb.inputs[1])
    links.new(mul.outputs[0], comb.inputs[2])
    links.new(comb.outputs[0], setp.inputs["Position"])
    links.new(setp.outputs[0], gout.inputs[0])
    obj.modifiers.new("Drape", "NODES").node_group = group


def _edit_gpkg(path: Path) -> None:
    """Move fid 2, relabel fid 3, delete fid 4 and add fid 50."""
    con = sqlite3.connect(path)
    moved = {"type": "Polygon", "coordinates": [vf.square(E0 + 100, N0, 10.0)]}
    con.execute("UPDATE contexts SET geom = ? WHERE fid = 2", (vf.gpkg_geometry(moved, 7856),))
    con.execute("UPDATE contexts SET context = 'C003-renamed' WHERE fid = 3")
    con.execute("DELETE FROM contexts WHERE fid = 4")
    new = {"type": "Polygon", "coordinates": [vf.square(E0 + 200, N0, 5.0)]}
    con.execute(
        "INSERT INTO contexts (fid, geom, context, depth, phase, sealed) VALUES (50, ?, 'C050', 1, 1, 0)",
        (vf.gpkg_geometry(new, 7856),),
    )
    con.commit()
    con.close()


def test_refresh_keeps_modifiers_material_and_transform() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_gpkg(tmp / "site.gpkg", "contexts", _gpkg_rows(vf.contexts(2)), COLUMNS)
        _bump(path)
        _import_gpkg(path)
        obj = bpy.data.objects["contexts"]
        _drape_modifier(obj)
        material = bpy.data.materials.new("Context Colour")
        obj.data.materials.append(material)
        obj.location = (1.0, 2.0, 3.0)
        parent = bpy.data.objects.new("Site Root", None)
        bpy.context.scene.collection.objects.link(parent)
        obj.parent = parent
        old_mesh_name = obj.data.name

        _edit_gpkg(path)
        assert _links().check_link(obj) == "CHANGED"
        result, error = h.call_op(bpy.ops.geotools.link_refresh, name=obj.name)
        assert result == {"FINISHED"}, error

        assert [m.name for m in obj.modifiers] == ["Drape"]
        assert obj.modifiers["Drape"].node_group.name == "Test Drape"
        assert list(obj.data.materials) == [material]
        assert tuple(obj.location) == (1.0, 2.0, 3.0) and obj.parent == parent
        assert obj.data.name == old_mesh_name
        assert len([m for m in bpy.data.meshes if m.name.startswith("contexts")]) == 1
        assert sorted(set(_attr(obj.data, "fid").tolist())) == [1, 2, 3, 50]
        assert obj.geotools_link.status == "OK"
        # Draped heights come back after the refresh.
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
        pos = _positions(evaluated)
        np.testing.assert_allclose(pos[:, 2], 5.0 + 0.1 * pos[:, 0], atol=1e-4)
        obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh_clear()

        # The counts by fid, from the operator report.
        links = _links()
        summary = links.RefreshResult(1, 1, 2).summary("x")
        assert "1 added, 1 removed, 2 changed" in summary
        _bump(path)
        _edit_gpkg_again(path)
        out = links.refresh_link(obj)
        assert (out.added, out.removed, out.changed) == (0, 0, 1), out


def _edit_gpkg_again(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("UPDATE contexts SET depth = 9 WHERE fid = 50")
    con.commit()
    con.close()


def test_refresh_counts_and_failure_keeps_mesh() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_geojson(tmp / "dig.geojson", vf.contexts(2))
        _bump(path)
        bpy.ops.geotools.import_geojson(filepath=str(path))
        obj = bpy.data.objects["dig"]
        features = vf.contexts(2)
        features[0]["properties"]["depth"] = 7.0  # changed attribute
        features[1]["geometry"]["coordinates"][0][0][0] += 0.5  # changed geometry
        features[1]["geometry"]["coordinates"][0][-1][0] += 0.5
        del features[3]  # removed
        features.append(
            vf.feature({"type": "Polygon", "coordinates": [vf.square(E0, N0, 1)]}, {}, 99)
        )
        vf.write_geojson(path, features)
        result = _links().refresh_link(obj)
        assert (result.added, result.removed, result.changed) == (1, 1, 2), result

        mesh = obj.data
        _bump(path)
        path.write_text("{ not json")
        result, error = h.call_op(bpy.ops.geotools.link_refresh, name=obj.name)
        assert result == {"ERROR_REPORTED"} and "Cannot read" in error, error
        assert obj.data == mesh and obj.geotools_link.status == "ERROR"


def test_stale_detection_on_load_and_on_demand() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_geojson(tmp / "dig.geojson", vf.contexts(1))
        _bump(path)
        bpy.ops.geotools.import_geojson(filepath=str(path))
        obj = bpy.data.objects["dig"]
        links = _links()
        assert links.check_link(obj) == "OK"
        os.utime(path)  # touched, same bytes
        assert links.check_link(obj) == "OK"
        assert json.loads(obj.geotools_link.fingerprint)["mtime_ns"] == path.stat().st_mtime_ns

        blend = tmp / "site.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(blend), copy=True) == {"FINISHED"}
        vf.write_geojson(path, vf.contexts(2))
        assert bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False) == {"FINISHED"}
        obj = bpy.data.objects["dig"]
        assert obj.geotools_link.status == "CHANGED"  # set by the load_post handler

        result, error = h.call_op(bpy.ops.geotools.link_check)
        assert result == {"FINISHED"}, error
        path.unlink()
        bpy.ops.geotools.link_check()
        assert obj.geotools_link.status == "MISSING"
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}


def test_auto_refresh_timer() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_geojson(tmp / "dig.geojson", vf.contexts(1))
        _bump(path)
        bpy.ops.geotools.import_geojson(filepath=str(path))
        obj = bpy.data.objects["dig"]
        links = _links()
        assert links.auto_refresh_tick() == links.AUTO_REFRESH_SECONDS
        vf.write_geojson(path, vf.contexts(2))
        links.auto_refresh_tick()  # off: nothing happens
        assert len(set(_attr(obj.data, "fid").tolist())) == 1

        obj.geotools_link.auto_refresh = True
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            links.auto_refresh_tick()  # Edit Mode: skipped
            assert obj.mode == "EDIT"
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")
        assert len(set(_attr(obj.data, "fid").tolist())) == 1
        links.auto_refresh_tick()
        assert len(set(_attr(obj.data, "fid").tolist())) == 4
        assert obj.geotools_link.status == "OK"

        # A broken file fails once and is not retried until it changes again.
        _bump(path)
        path.write_text("{")
        links.auto_refresh_tick()
        assert obj.geotools_link.status == "ERROR"
        calls = []
        original = links.REFRESHERS["GEOJSON"]
        links.REFRESHERS["GEOJSON"] = lambda o: calls.append(o) or original(o)
        try:
            links.auto_refresh_tick()
            assert not calls
            _bump(path)
            vf.write_geojson(path, vf.contexts(1))
            links.auto_refresh_tick()
            assert calls and obj.geotools_link.status == "OK"
        finally:
            links.REFRESHERS["GEOJSON"] = original


# --- filters ---------------------------------------------------------------------------


def _site(extent: float) -> None:
    """A source collection with a flat mesh from (0, 0) to (extent, extent) local."""
    collection = bpy.data.collections.new("Site")
    bpy.context.scene.collection.children.link(collection)
    mesh = bpy.data.meshes.new("Ground")
    mesh.from_pydata(
        [(0, 0, 0), (extent, 0, 0), (extent, extent, 0), (0, extent, 0)], [], [(0, 1, 2, 3)]
    )
    obj = bpy.data.objects.new("Ground", mesh)
    collection.objects.link(obj)
    h.project().source_collection = collection


def test_filters() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_gpkg(tmp / "site.gpkg", "contexts", _gpkg_rows(vf.contexts(3)), COLUMNS)
        _site(25.0)
        result, error = _import_gpkg(path, within_site=True)
        assert result == {"FINISHED"}, error
        obj = bpy.data.objects["contexts"]
        assert sorted(set(_attr(obj.data, "fid").tolist())) == [1, 2, 4, 5]
        bbox = json.loads(obj.geotools_link.options)["site_bbox"]
        assert bbox == [E0, N0, E0 + 25.0, N0 + 25.0]

        result, error = h.call_op(
            bpy.ops.geotools.link_filters, name=obj.name, where="sealed = 1", within_site=True
        )
        assert result == {"FINISHED"}, error
        assert sorted(set(_attr(obj.data, "fid").tolist())) == [2, 4]
        result, error = h.call_op(
            bpy.ops.geotools.link_filters, name=obj.name, where="x IN (SELECT 1 FROM gpkg_contents)"
        )
        assert result == {"ERROR_REPORTED"} and "refused" in error, error
        assert json.loads(obj.geotools_link.options)["where"] == "sealed = 1"
        result, error = h.call_op(bpy.ops.geotools.link_filters, name=obj.name, where="")
        assert result == {"FINISHED"}, error
        assert len(set(_attr(obj.data, "fid").tolist())) == 9


# --- write-back --------------------------------------------------------------------------


def _set_face_values(
    obj: bpy.types.Object, name: str, kind: str, per_fid: dict[int, float]
) -> None:
    mesh = obj.data
    if name not in mesh.attributes:
        mesh.attributes.new(name, kind, "FACE")
    fid = _attr(mesh, "fid")
    values = _attr(mesh, name)
    for f, v in per_fid.items():
        values[fid == f] = v
    mesh.attributes[name].data.foreach_set("value", values)


def test_write_back_gpkg() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_gpkg(tmp / "site.gpkg", "contexts", _gpkg_rows(vf.contexts(2)), COLUMNS)
        _bump(path)
        _import_gpkg(path)
        obj = bpy.data.objects["contexts"]
        geom_before = vf.rows(path, "SELECT fid, geom FROM contexts ORDER BY fid")
        _set_face_values(obj, "depth", "FLOAT", {1: 1.75, 3: 2.5})
        _set_face_values(obj, "level", "FLOAT", {1: 12.5, 2: 13.0, 3: 14.0, 4: 15.5})
        result, error = h.call_op(
            bpy.ops.geotools.link_write_back, name=obj.name, columns="depth, level"
        )
        assert result == {"FINISHED"}, error
        got = vf.rows(path, "SELECT fid, depth, level FROM contexts ORDER BY fid")
        assert got == [(1, 1.75, 12.5), (2, 0.5, 13.0), (3, 2.5, 14.0), (4, 1.0, 15.5)]
        assert vf.rows(path, "SELECT fid, geom FROM contexts ORDER BY fid") == geom_before
        assert _links().check_link(obj) == "OK"  # our own write is not "changed on disk"
        # A second write-back needs no refresh in between.
        _set_face_values(obj, "level", "FLOAT", {2: 99.0})
        result, error = h.call_op(bpy.ops.geotools.link_write_back, name=obj.name, columns="level")
        assert result == {"FINISHED"}, error
        assert vf.rows(path, "SELECT level FROM contexts WHERE fid = 2") == [(99.0,)]
        result, error = h.call_op(bpy.ops.geotools.link_write_back, name=obj.name, columns="fid")
        assert result == {"ERROR_REPORTED"}, error


def test_write_back_refused_after_a_change_on_disk() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_gpkg(tmp / "site.gpkg", "contexts", _gpkg_rows(vf.contexts(2)), COLUMNS)
        _bump(path)
        _import_gpkg(path)
        obj = bpy.data.objects["contexts"]
        _set_face_values(obj, "depth", "FLOAT", {1: 42.0})
        con = sqlite3.connect(path)
        con.execute("UPDATE contexts SET context = 'edited in QGIS' WHERE fid = 2")
        con.commit()
        con.close()
        result, error = h.call_op(bpy.ops.geotools.link_write_back, name=obj.name, columns="depth")
        assert result == {"ERROR_REPORTED"} and "Refresh first" in error, error
        assert vf.rows(path, "SELECT depth FROM contexts WHERE fid = 1") == [(0.25,)]
        assert obj.geotools_link.status == "CHANGED"


def test_write_back_geojson_keeps_unknown_members() -> None:
    _project()
    with _tmpdir() as tmp:
        features = vf.contexts(2)
        features[0]["x-note"] = "keep me"
        path = vf.write_geojson(tmp / "dig.geojson", features, extra={"x-survey": {"day": 3}})
        _bump(path)
        bpy.ops.geotools.import_geojson(filepath=str(path))
        obj = bpy.data.objects["dig"]
        _set_face_values(obj, "level", "FLOAT", {1: 1.5, 2: 2.5, 3: 3.5, 4: 4.5})
        result, error = h.call_op(bpy.ops.geotools.link_write_back, name=obj.name, columns="level")
        assert result == {"FINISHED"}, error
        data = json.loads(path.read_text())
        assert data["x-survey"] == {"day": 3} and data["features"][0]["x-note"] == "keep me"
        assert [f["properties"]["level"] for f in data["features"]] == [1.5, 2.5, 3.5, 4.5]
        assert [f["id"] for f in data["features"]] == [1, 2, 3, 4]
        assert data["features"][1]["geometry"] == features[1]["geometry"]


# --- export ------------------------------------------------------------------------------


def _read_gpkg_xyz(path: Path, table: str) -> dict[int, list[np.ndarray]]:
    """fid -> rings (n, 3) from the written blobs, parsed here by hand (little-endian ISO WKB)."""
    out = {}
    for fid, blob in vf.rows(path, f'SELECT fid, geom FROM "{table}" ORDER BY fid'):
        flags = blob[3]
        env = {0: 0, 1: 32, 2: 48}[(flags >> 1) & 7]
        off = 8 + env
        order, code = struct.unpack_from("<BI", blob, off)
        assert order == 1 and code in (1003, 1006), code
        off += 5
        polys = 1
        if code == 1006:
            (polys,) = struct.unpack_from("<I", blob, off)
            off += 4
        rings = []
        for _ in range(polys):
            if code == 1006:
                off += 5
            (n_rings,) = struct.unpack_from("<I", blob, off)
            off += 4
            for _ in range(n_rings):
                (n,) = struct.unpack_from("<I", blob, off)
                off += 4
                rings.append(np.frombuffer(blob, "<f8", n * 3, off).reshape(n, 3))
                off += n * 24
        out[fid] = rings
    return out


def test_export_draped_mesh_with_z() -> None:
    _project()
    with _tmpdir() as tmp:
        path = vf.write_geojson(tmp / "dig.geojson", vf.contexts(2))
        bpy.ops.geotools.import_geojson(filepath=str(path))
        obj = bpy.data.objects["dig"]
        _drape_modifier(obj, slope=0.1, base=5.0)
        out = tmp / "draped.gpkg"
        result, error = h.call_op(
            bpy.ops.geotools.export_vector,
            filepath=str(out),
            file_format="GPKG",
            object_name=obj.name,
            layer="draped",
        )
        assert result == {"FINISHED"}, error
        (geometry_type, z) = vf.rows(
            out,
            "SELECT geometry_type_name, z FROM gpkg_geometry_columns WHERE table_name = 'draped'",
        )[0]
        assert geometry_type == "POLYGON" and z == 1
        assert vf.rows(
            out, "SELECT organization_coordsys_id FROM gpkg_spatial_ref_sys WHERE srs_id = 7856"
        )
        rings = _read_gpkg_xyz(out, "draped")
        assert sorted(rings) == [1, 2, 3, 4]
        assert [len(r) for r in rings.values()] == [2, 1, 1, 2]  # the holes come back
        for fid_rings in rings.values():
            for ring in fid_rings:
                assert (ring[0] == ring[-1]).all()
                local_x = ring[:, 0] - E0
                np.testing.assert_allclose(ring[:, 2], A0 + 5.0 + 0.1 * local_x, atol=1e-4)
        ext = rings[1][0]
        assert ext[:, 0].min() == E0 and ext[:, 1].min() == N0  # exact in binary64
        attrs = vf.rows(out, "SELECT fid, context, depth, phase, sealed FROM draped ORDER BY fid")
        assert attrs[0] == (1, "C001", 0.25, 1, 0), attrs
        # Read back by the importer, in the same project.
        h.reset_scene()
        _project()
        _import_gpkg(out)
        back = bpy.data.objects["draped"].data
        pos = _positions(back)
        np.testing.assert_allclose(pos[:, 2], 5.0 + 0.1 * pos[:, 0], atol=1e-4)

        # GeoJSON: with the legacy crs member; lines and points too.
        h.reset_scene()
        _project()
        mixed = vf.write_geojson(tmp / "mixed.geojson", _mixed_features())
        bpy.ops.geotools.import_geojson(filepath=str(mixed))
        for o in bpy.data.objects:
            o.select_set(True)
        gj = tmp / "all.geojson"
        result, error = h.call_op(
            bpy.ops.geotools.export_vector, filepath=str(gj), file_format="GEOJSON"
        )
        assert result == {"FINISHED"}, error
        data = json.loads(gj.read_text())
        assert data["crs"]["properties"]["name"] == "urn:ogc:def:crs:EPSG::7856"
        types = sorted(f["geometry"]["type"] for f in data["features"])
        assert types == ["LineString", "MultiPoint", "Polygon", "Polygon", "Polygon", "Polygon"]
        line = next(f for f in data["features"] if f["geometry"]["type"] == "LineString")
        assert line["geometry"]["coordinates"][0] == [E0, N0 - 5, 1.5]
        assert line["properties"]["context"] == "wall"


# --- panel -------------------------------------------------------------------------------


def test_linked_data_panel() -> None:
    _project()
    ui = h.submodule("blender.links_ui")
    panel = ui.GEOTOOLS_PT_linked_data
    panels = [cls.__name__ for cls in h.submodule("blender.panels").classes]
    assert panels.index("GEOTOOLS_PT_linked_data") == panels.index("GEOTOOLS_PT_source") + 1
    assert h.draw_header(panel) is None
    calls = h.draw(panel)
    assert ("template_list", "GEOTOOLS_UL_links", "NONE") in calls
    assert h.operator_text("geotools.import_geojson") == "GeoJSON"
    assert h.operator_text("geotools.import_gpkg") == "GeoPackage"
    assert h.operator_text("geotools.import_geotiff") is None or ui.geotiff_available()
    assert "No linked data yet" in h.label_text(calls)
    with _tmpdir() as tmp:
        path = vf.write_gpkg(tmp / "site.gpkg", "contexts", _gpkg_rows(vf.contexts(1)), COLUMNS)
        _bump(path)
        _import_gpkg(path, where="phase = 1")
        obj = bpy.data.objects["contexts"]
        assert bpy.context.scene.geotools_link_index == bpy.data.objects.find(obj.name)
        calls = h.draw(panel)
        for idname in (
            "geotools.link_refresh",
            "geotools.link_filters",
            "geotools.link_write_back",
            "geotools.export_vector",
            "geotools.link_check",
        ):
            assert any(c[:2] == ("operator", idname) for c in calls), idname
        assert "auto_refresh" in h.props_drawn(calls)
        assert "Filters: phase = 1" in h.label_text(calls)
        assert h.draw_header(panel) == "CHECKMARK"
        item = h.draw_list_item(_links().GEOTOOLS_UL_links, bpy.data, obj)
        assert ("label", obj.name, "FILE_CACHE") in item and ("label", "", "CHECKMARK") in item
        flags, _order = _links().GEOTOOLS_UL_links.filter_items(
            type("L", (), {"bitflag_filter_item": 1, "filter_name": ""})(),
            bpy.context,
            bpy.data,
            "objects",
        )
        assert sum(flags) == 1

        con = sqlite3.connect(path)
        con.execute("UPDATE contexts SET depth = 9 WHERE fid = 1")
        con.commit()
        con.close()
        _links().check_link(obj)
        assert h.draw_header(panel) == "ERROR"  # the warning icon
        assert "changed on disk" in h.label_text(h.draw(panel))
        path.unlink()
        _links().check_link(obj)
        assert h.draw_header(panel) == "CANCEL"
