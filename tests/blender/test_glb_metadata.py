"""The metadata GLB (M4, ``blender.glb_export``), decoded and checked against Blender.

A scene of two grids with FACE, CORNER, CATEGORY and TEXTURE layers (one FACE
layer computed only by a Geometry Nodes modifier) is exported, and the GLB is
decoded with ``tests/glbcheck.py``: feature IDs, the object table, values per
face and per corner (matched to Blender faces and vertices by position), enums,
property texture values at known UVs, and the XMP placement record. Also the
Earth-placed matrix against the probe's root transform, the persistent object
id across a rename and a save/reopen, the face table option and its size cost,
a ``.glb`` build profile end to end with its build.json, the File > Export
entry, and Blender's own glTF importer opening the file.

The external validators run on the exported GLB when their environment
variables are set (see tests/core/test_metadata_glb.py).
"""

from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/: glbcheck
import glbcheck

JOIN_SECONDS = 120.0
N = 3  # grid size: N x N quads, face (c, r) at x in [c, c+1], y in [r, r+1]
C = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], float)  # Blender->glTF


def _layers():
    return h.submodule("blender.layers")


def _core_layers():
    return h.submodule("core.layers")


def _glb():
    return h.submodule("blender.glb_export")


def _gm():
    return h.submodule("core.gltf_metadata")


@contextmanager
def _tmpdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-glb-test-"))
    try:
        yield path
    finally:
        h.submodule("blender.publish").shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.get("Source")
    if collection is None:
        collection = bpy.data.collections.new("Source")
        bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _grid(name: str, offset: float = 0.0) -> bpy.types.Object:
    """An N x N grid of quads with a UV map (UV = position / (N + 1))."""
    xs, ys = np.meshgrid(np.arange(N + 1, dtype=np.float32), np.arange(N + 1, dtype=np.float32))
    co = np.stack([xs.ravel() + offset, ys.ravel(), np.zeros(xs.size, np.float32)], axis=1)
    i = np.arange(N)
    a = (i[None, :] + (N + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + N + 2, a + N + 1], axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    uv = mesh.uv_layers.new(name="UVMap")
    local = co.copy()
    local[:, 0] -= offset
    uv.data.foreach_set("uv", (local[quads.ravel(), :2] / (N + 1)).astype(np.float32).ravel())
    mesh.update()
    mesh.validate()
    material = bpy.data.materials.new(f"{name} grey")
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    _source().objects.link(obj)
    return obj


def _store_tree(attribute: str, subdivide: int = 0) -> bpy.types.NodeTree:
    """Store Named Attribute ``attribute`` = face index * 2, FACE FLOAT (after an
    optional Subdivide Mesh)."""
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
    math_node = nodes.new("ShaderNodeMath")
    math_node.operation = "MULTIPLY"
    math_node.inputs[1].default_value = 2.0
    links.new(nodes.new("GeometryNodeInputIndex").outputs[0], math_node.inputs[0])
    links.new(math_node.outputs[0], store.inputs["Value"])
    links.new(store.outputs["Geometry"], nodes.new("NodeGroupOutput").inputs[0])
    return tree


def _layer(**kwargs) -> str:
    core = _core_layers()
    spec = core.LayerSpec(**kwargs)
    _layers().ensure_layer(bpy.context.scene, spec)
    return spec.name


def _spec(name: str):
    return _layers().spec_from_props(_layers().find_layer(bpy.context.scene, name))


def _texture(name: str, objects, size: int = 4) -> dict[str, np.ndarray]:
    """A TEXTURE layer whose image on object k holds ``k + (row * size + col) / 16``."""
    item = _layers().find_layer(bpy.context.scene, name)
    item.resolution = size
    out = {}
    for k, obj in enumerate(objects):
        image = _layers().ensure_layer_image(_layers().find_layer(bpy.context.scene, name), obj)
        values = (k + np.arange(size * size, dtype=np.float32) / 16).astype(np.float32)
        image.pixels.foreach_set(np.repeat(values, 4))
        out[obj.name] = values.reshape(size, size)
    return out


def _mga56() -> None:
    props = h.project()
    preset = h.submodule("core.crs_presets").get("MGA2020_56")
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = preset.definition
    p = h.submodule("blender.properties")
    p.write_number(props, "source_offset_e", 502_000.25)
    p.write_number(props, "source_offset_n", 6_961_000.123456789)
    p.write_number(props, "source_offset_a", 12.5)
    p.write_number(props, "height_offset", 38.25)


def _scene():
    """Terrain and Hut with five layers; returns the objects and the texture values."""
    core = _core_layers()
    terrain = _grid("Terrain")
    hut = _grid("Hut", offset=10.0)
    faces = np.arange(N * N)
    loops = len(terrain.data.loops)
    corner_vertex = np.empty(loops, np.int32)
    terrain.data.loops.foreach_get("vertex_index", corner_vertex)
    corner_face = np.repeat(faces, 4)
    _layer(
        name="slope",
        domain=core.Domain.FACE,
        data_type=core.DataType.FLOAT,
        units="deg",
        nodata=-9999.0,
        label="Slope",
    )
    _layer(name="count", domain=core.Domain.CORNER, data_type=core.DataType.INT)
    _layer(
        name="landcover",
        domain=core.Domain.FACE,
        data_type=core.DataType.CATEGORY,
        categories=("bare", "grass", "water"),
        nodata=-1,
    )
    _layer(
        name="height",
        domain=core.Domain.FACE,
        data_type=core.DataType.FLOAT,
        source_attribute="height",
        nodata=-1.0,
    )
    _layer(name="shade", domain=core.Domain.TEXTURE, data_type=core.DataType.FLOAT)
    for k, obj in enumerate((terrain, hut)):
        _layers().write_layer(obj.data, _spec("slope"), (faces * 1.5 + 100 * k).astype(np.float32))
        _layers().write_layer(
            obj.data, _spec("count"), 100 * corner_face + corner_vertex + 1000 * k
        )
        cover = (faces % 3).astype(np.int32)
        cover[0] = -1
        _layers().write_layer(obj.data, _spec("landcover"), cover)
    # height exists only in Terrain's Geometry Nodes output.
    modifier = terrain.modifiers.new("Height", "NODES")
    modifier.node_group = _store_tree("height")
    textures = _texture("shade", (hut, terrain))
    _mga56()
    bpy.context.view_layer.update()
    return terrain, hut, textures


def _clear_trees() -> None:
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)


def _export(path: Path, **options) -> object:
    """``prepare_glb`` and write; returns the PreparedGlb."""
    glb = _glb()
    bpy.context.view_layer.update()
    image_dir = Path(tempfile.mkdtemp(prefix="geotools-glb-images-"))
    try:
        prepared = glb.prepare_glb(bpy.context, image_dir, glb.GlbOptions(**options))
        glb.write_prepared(prepared, path)
    finally:
        shutil.rmtree(image_dir, ignore_errors=True)
    return prepared


def _blender(points: np.ndarray) -> np.ndarray:
    """glTF (x, y, z) -> Blender (x, -z, y)."""
    return np.stack([points[:, 0], -points[:, 2], points[:, 1]], axis=1)


def _triangles(glb, p: int) -> tuple[np.ndarray, np.ndarray]:
    """Primitive p's triangles (T, 3) and its vertex positions in Blender axes."""
    prim = glb.primitives[p]
    indices = glb.accessor(prim["indices"]).reshape(-1, 3)
    return indices, _blender(glb.attribute(p, "POSITION").astype(np.float64))


def _face_of(points: np.ndarray, offset: float) -> np.ndarray:
    """Grid face index of points inside faces (Blender axes)."""
    c = np.floor(points[:, 0] - offset).astype(int)
    r = np.floor(points[:, 1]).astype(int)
    return r * N + c


def _object_names(glb) -> list[str]:
    return glb.column("object", "name")


def _primitive_of(glb, name: str) -> int:
    index = _object_names(glb).index(name)
    for p in range(len(glb.primitives)):
        if (glb.attribute(p, "_FEATURE_ID_0") == index).all():
            return p
    raise AssertionError(f"no primitive for {name}")


def _available() -> bool:
    return h.CONFIG.expect == "available"


# --- registration ---------------------------------------------------------------------------


def test_glb_export_registered() -> None:
    assert h.operator_available("geotools.export_glb")
    drawn = [f.__name__ for f in bpy.types.TOPBAR_MT_file_export._dyn_ui_initialize()]
    assert "menu_export" in drawn, drawn
    assert "geotools_id" in bpy.types.Object.bl_rna.properties
    profile = bpy.types.PropertyGroup.bl_rna_get_subclass_py("GeoToolsBuildProfile")
    for prop in ("output_type", "glb_face_table", "glb_earth_placed"):
        assert prop in profile.bl_rna.properties, prop
    earth = profile.bl_rna.properties["glb_earth_placed"]
    assert "6,400 km" in earth.description and earth.default is False
    assert profile.bl_rna.properties["output_type"].default == "3TZ"
    modules = [m.__name__.rpartition(".")[2] for m in h.submodule("blender").MODULES]
    assert modules.index("presentation") < modules.index("glb_export") < modules.index("publish")


# --- the export, decoded -----------------------------------------------------------------------


def test_metadata_glb_values() -> None:
    h.reset_scene()
    terrain, hut, textures = _scene()
    try:
        with _tmpdir() as tmp:
            path = tmp / "site.glb"
            prepared = _export(path)
            glb = glbcheck.check(path)
            doc = glb.doc
            assert doc["extensionsUsed"] == [
                "EXT_mesh_features",
                "EXT_structural_metadata",
                "KHR_xmp_json_ld",
            ]
            # Objects: names and persistent ids, in name order.
            assert _object_names(glb) == ["Hut", "Terrain"]
            ids = glb.column("object", "object_id")
            assert ids == [hut.geotools_id, terrain.geotools_id] and all(ids)
            assert len(set(ids)) == 2

            schema = glb.schema
            vertex = schema["classes"]["vertex"]["properties"]
            assert list(vertex) == ["slope", "count", "landcover", "height"]
            assert (
                vertex["slope"]["name"] == "Slope"
                and "Units: deg." in (vertex["slope"]["description"])
            )
            assert (vertex["slope"]["min"], vertex["slope"]["max"]) == (0.0, 112.0)
            assert vertex["slope"]["noData"] == -9999.0
            assert vertex["count"]["componentType"] == "FLOAT32"
            assert vertex["landcover"] == {
                "name": "landcover",
                "description": vertex["landcover"]["description"],
                "type": "ENUM",
                "enumType": "landcover_category",
                "noData": "no data",
            }
            enum = schema["enums"]["landcover_category"]
            assert [(v["name"], v["value"]) for v in enum["values"]] == [
                ("bare", 0),
                ("grass", 1),
                ("water", 2),
                ("no data", -1),
            ]
            assert enum["valueType"] == "INT8"
            texel = schema["classes"]["texel"]["properties"]["shade"]
            assert texel["componentType"] == "FLOAT32"

            for name, offset, k in (("Terrain", 0.0, 1), ("Hut", 10.0, 0)):
                p = _primitive_of(glb, name)
                indices, points = _triangles(glb, p)
                centroids = points[indices].mean(axis=1)
                face = _face_of(centroids, offset)
                obj_k = 0 if name == "Terrain" else 1

                def per_triangle(semantic: str, p=p, indices=indices) -> np.ndarray:
                    values = glb.attribute(p, semantic)[indices]
                    assert (values == values[:, :1]).all(), f"{semantic} varies inside a face"
                    return values[:, 0]

                np.testing.assert_array_equal(per_triangle("_SLOPE"), face * 1.5 + 100 * obj_k)
                cover = face % 3
                cover[face == 0] = -1
                np.testing.assert_array_equal(per_triangle("_LANDCOVER"), cover)
                # The Geometry Nodes layer: Terrain's evaluated values, Hut has none.
                expected = face * 2.0 if name == "Terrain" else np.full(face.shape, -1.0)
                np.testing.assert_array_equal(per_triangle("_HEIGHT"), expected)
                # Corner values: 100 * face + vertex index, per corner.
                vertex_index = np.rint(points[:, 0] - offset).astype(int) + (N + 1) * np.rint(
                    points[:, 1]
                ).astype(int)
                corner = glb.attribute(p, "_COUNT")[indices]
                want = 100 * face[:, None] + vertex_index[indices] + 1000 * obj_k
                np.testing.assert_array_equal(corner, want)

                # Property texture: the image texel at each triangle's UV centroid.
                ext = glb.primitives[p]["extensions"]["EXT_structural_metadata"]
                texture = glb.metadata["propertyTextures"][ext["propertyTextures"][0]]
                assert texture["class"] == "texel"
                shade = texture["properties"]["shade"]
                uv = glb.attribute(p, f"TEXCOORD_{shade['texCoord']}")[indices].mean(axis=1)
                rgba = _gm().decode_png(glb.image(shade["index"]))
                height, width = rgba.shape[:2]
                col = np.minimum((uv[:, 0] * width).astype(int), width - 1)
                row_top = np.minimum((uv[:, 1] * height).astype(int), height - 1)
                raw = np.ascontiguousarray(rgba[row_top, col][:, shade["channels"]])
                got = raw.view("<f4").ravel()
                row_blender = np.minimum(((1.0 - uv[:, 1]) * height).astype(int), height - 1)
                np.testing.assert_array_equal(got, textures[name][row_blender, col])

            # Feature IDs per primitive.
            for p, prim in enumerate(glb.primitives):
                sets = prim["extensions"]["EXT_mesh_features"]["featureIds"]
                assert sets == [
                    {"featureCount": 1, "attribute": 0, "propertyTable": 0, "label": "object"}
                ]
            assert prepared.record["options"]["face_table"] is False

            # XMP placement record.
            packet = doc["extensions"]["KHR_xmp_json_ld"]["packets"][0]
            assert doc["asset"]["extensions"] == {"KHR_xmp_json_ld": {"packet": 0}}
            assert packet["geotools:placement"] == "source_crs"
            assert packet["geotools:crs"] == "EPSG:7856"
            assert packet["geotools:crsDefinition"] == h.project().source_crs
            assert packet["geotools:sourceOffset"] == {
                "@list": [502000.25, 6961000.123456789, 12.5]
            }
            assert packet["geotools:heightOffset"] == 38.25
            assert packet["geotools:earthPlaced"] is False
            assert packet["geotools:version"] == h.submodule("blender.common").extension_version()
            assert "matrix" not in doc["nodes"][0]
            if _available():
                probe = h.submodule("core.probe")
                placement = h.submodule("blender.properties").placement_from_props(h.project())
                lo = (0.0, 0.0, 0.0)
                hi = (10.0 + N, float(N), 0.0)
                result = probe.probe_placement(placement, lo, hi)
                lon, lat, height = result.origin_lon_lat_height
                assert packet["geotools:originLongitude"] == lon
                assert packet["geotools:originLatitude"] == lat
                assert packet["geotools:originHeight"] == height
                assert abs(packet["geotools:heading"]) < 1.0  # grid convergence, near the CM
                assert len(packet["geotools:modelToEcef"]["@list"]) == 16
            else:
                assert "geotools:originLongitude" not in packet
                assert "rusty-tiles" in packet["geotools:originUnavailable"]
            _external_validators(path)
    finally:
        _clear_trees()


def _external_validators(path: Path) -> None:
    tiles = os.environ.get("GEOTOOLS_TEST_3D_TILES_VALIDATOR", "")
    if tiles:
        report = path.with_suffix(".report.json")
        subprocess.run(
            [*shlex.split(tiles), "--tileContentFile", str(path), "--reportFile", str(report)],
            check=False,
            capture_output=True,
            timeout=300,
        )
        result = json.loads(report.read_text())
        assert result["numErrors"] == 0 and result["numWarnings"] == 0, json.dumps(result)
        print("    3d-tiles-validator: 0 errors, 0 warnings")
    gltf = os.environ.get("GEOTOOLS_TEST_GLTF_VALIDATOR", "")
    if gltf:
        done = subprocess.run(
            [*shlex.split(gltf), "-o", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        issues = json.loads(done.stdout)["issues"]
        assert issues["numErrors"] == 0 and issues["numWarnings"] == 0, done.stdout
        print("    glTF-Validator: 0 errors, 0 warnings")


def test_earth_placed_matrix_is_the_root_transform() -> None:
    h.reset_scene()
    _grid("Terrain")
    props = h.project()
    p = h.submodule("blender.properties")
    props.placement_mode = "ANCHOR"
    p.write_number(props, "anchor_lon", 153.02)
    p.write_number(props, "anchor_lat", -27.47)
    p.write_number(props, "anchor_height", 10.0)
    p.write_number(props, "heading", 30.0)
    with _tmpdir() as tmp:
        if not _available():
            options = _glb().GlbOptions(earth_placed=True)
            codes = [i.code for i in _glb().glb_issues(bpy.context.scene, options)]
            assert "glb-earth-placed-rusty-tiles" in codes, codes
            try:
                _export(tmp / "earth.glb", earth_placed=True)
            except h.submodule("core.gltf_writer").ExportError as ex:
                assert "rusty-tiles" in str(ex), ex
            else:
                raise AssertionError("expected an ExportError without rusty-tiles")
            return
        _export(tmp / "earth.glb", earth_placed=True)
        glb = glbcheck.check(tmp / "earth.glb")
        nodes = glb.doc["nodes"]
        assert nodes[0]["name"] == "GeoTools ECEF" and nodes[0]["children"] == [1]
        assert nodes[1]["mesh"] == 0 and glb.doc["scenes"] == [{"nodes": [0]}]
        node = np.array(nodes[0]["matrix"]).reshape(4, 4).T
        placement = p.placement_from_props(props)
        root = h.submodule("core.probe").probe_placement(
            placement, (0.0, 0.0, 0.0), (float(N), float(N), 0.0)
        )
        root = np.array(root.root_transform).reshape(4, 4).T
        # Undo the glTF Y-up convention: the node maps glTF to Y-up ECEF.
        np.testing.assert_allclose(np.linalg.inv(C) @ node @ C, root, rtol=0, atol=1e-6)
        packet = glb.doc["extensions"]["KHR_xmp_json_ld"]["packets"][0]
        assert packet["geotools:earthPlaced"] is True
        assert packet["geotools:heading"] == 30.0 or math.isclose(
            packet["geotools:heading"], 30.0, abs_tol=1e-9
        )
        assert packet["geotools:modelToEcef"]["@list"] == nodes[0]["matrix"]
        # Local placement cannot be Earth-placed.
        props.placement_mode = "LOCAL"
        codes = [
            i.code
            for i in _glb().glb_issues(bpy.context.scene, _glb().GlbOptions(earth_placed=True))
        ]
        assert "glb-earth-placed-local" in codes


def test_object_id_survives_rename_and_reopen() -> None:
    assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
    h.reset_scene()
    terrain = _grid("Terrain")
    _grid("Hut", offset=10.0)
    try:
        with _tmpdir() as tmp:
            assert terrain.geotools_id == ""
            _export(tmp / "a.glb")
            first = terrain.geotools_id
            assert first and len(first) == 36
            terrain.name = "Ground"
            blend = tmp / "site.blend"
            assert bpy.ops.wm.save_as_mainfile(filepath=str(blend)) == {"FINISHED"}
            assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
            assert bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False) == {"FINISHED"}
            ground = bpy.data.objects["Ground"]
            assert ground.geotools_id == first
            _export(tmp / "b.glb")
            glb = glbcheck.check(tmp / "b.glb")
            assert _object_names(glb) == ["Ground", "Hut"]
            assert glb.column("object", "object_id")[0] == first
            # A duplicate copies the property; the copy gets a new id.
            copy = ground.copy()
            copy.name = "Ground.copy"
            h.project().source_collection.objects.link(copy)
            _export(tmp / "c.glb")
            ids = glbcheck.check(tmp / "c.glb").column("object", "object_id")
            assert ids[0] == first and len(set(ids)) == 3
            assert copy.geotools_id == ids[1] != first
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()


def test_face_table_option_and_its_cost() -> None:
    h.reset_scene()
    terrain = _grid("Terrain")
    _grid("Hut", offset=10.0)
    core = _core_layers()
    _layer(
        name="height",
        domain=core.Domain.FACE,
        data_type=core.DataType.FLOAT,
        source_attribute="height",
        nodata=-1.0,
    )
    _layer(
        name="landcover",
        domain=core.Domain.FACE,
        data_type=core.DataType.CATEGORY,
        categories=("bare", "grass"),
    )
    _layer(name="wet", domain=core.Domain.FACE, data_type=core.DataType.BOOLEAN)
    _layer(name="count", domain=core.Domain.CORNER, data_type=core.DataType.INT)
    for obj in bpy.data.objects:
        faces = len(obj.data.polygons)
        _layers().write_layer(obj.data, _spec("landcover"), np.arange(faces) % 2)
        _layers().write_layer(obj.data, _spec("wet"), np.arange(faces) % 3 == 0)
    # Subdivide: the face table holds the 4 x 9 evaluated faces of Terrain.
    terrain.modifiers.new("Height", "NODES").node_group = _store_tree("height", subdivide=1)
    try:
        with _tmpdir() as tmp:
            plain = _export(tmp / "plain.glb")
            table = _export(tmp / "table.glb", face_table=True)
            glb = glbcheck.check(tmp / "table.glb")
            sizes = ((tmp / "plain.glb").stat().st_size, (tmp / "table.glb").stat().st_size)
            cost = table.record["face_table_cost"]
            print(
                f"    face table cost: {cost['vertices_without_face_table']} -> "
                f"{cost['vertices']} vertices ({cost['vertex_ratio']}x), GLB "
                f"{sizes[0]:,} -> {sizes[1]:,} bytes"
            )
            assert cost["vertices"] > cost["vertices_without_face_table"]
            # The plain export's face layers split vertices where faces differ, too.
            assert plain.record["vertices"] >= cost["vertices_without_face_table"]
            names = _object_names(glb)
            assert names == ["Hut", "Terrain"]
            faces = {"Hut": N * N, "Terrain": 4 * N * N}
            face_table = glb.table("face")
            assert face_table["count"] == faces["Hut"] + faces["Terrain"]
            obj_col = glb.column("face", "object")
            np.testing.assert_array_equal(obj_col, np.repeat([0, 1], [faces["Hut"], 36]))
            polygon = glb.column("face", "polygon")
            np.testing.assert_array_equal(polygon[faces["Hut"] :], np.arange(36))
            height = glb.column("face", "height")
            np.testing.assert_array_equal(height[: faces["Hut"]], -1.0)
            np.testing.assert_array_equal(height[faces["Hut"] :], np.arange(36) * 2.0)
            assert glb.column("face", "wet")[:4].tolist() == [True, False, False, True]
            assert glb.column("face", "landcover")[:3] == ["bare", "grass", "bare"]
            props = glb.schema["classes"]["face"]["properties"]
            assert list(props) == ["object", "polygon", "height", "landcover", "wet"]
            assert props["wet"]["type"] == "BOOLEAN"
            # Face layers are in the table, not vertex attributes; corner layers stay.
            assert list(glb.schema["classes"]["vertex"]["properties"]) == ["count"]
            for p, prim in enumerate(glb.primitives):
                assert "_HEIGHT" not in prim["attributes"]
                sets = prim["extensions"]["EXT_mesh_features"]["featureIds"]
                assert [s["label"] for s in sets] == ["object", "face"]
                indices = glb.accessor(prim["indices"]).reshape(-1, 3)
                face_ids = glb.attribute(p, "_FEATURE_ID_1")[indices]
                assert (face_ids == face_ids[:, :1]).all()
                rows = face_ids[:, 0].astype(int)
                assert sets[1]["featureCount"] == np.unique(rows).size
                objects = glb.attribute(p, "_FEATURE_ID_0")[indices][:, 0].astype(int)
                np.testing.assert_array_equal(obj_col[rows], objects)
                # Vertices are shared only inside a face.
                for v in np.unique(indices):
                    assert np.unique(face_ids[indices == v]).size == 1
    finally:
        _clear_trees()


# --- Blender's importer ---------------------------------------------------------------------------


def test_blender_gltf_importer_opens_it() -> None:
    h.reset_scene()
    _scene()
    try:
        with _tmpdir() as tmp:
            path = tmp / "site.glb"
            _export(path, face_table=True)
            glb = glbcheck.check(path)
            triangles = sum(
                glb.doc["accessors"][p["indices"]]["count"] // 3 for p in glb.primitives
            )
            before = set(bpy.data.objects)
            assert bpy.ops.import_scene.gltf(filepath=str(path)) == {"FINISHED"}
            imported = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
            assert imported, "the importer made no mesh"
            got = 0
            for obj in imported:
                obj.data.calc_loop_triangles()
                got += len(obj.data.loop_triangles)
            assert got == triangles, (got, triangles)
            # Geometry intact: the imported vertices span the exported bounds.
            co = np.concatenate(
                [
                    np.array([tuple(o.matrix_world @ v.co) for v in o.data.vertices])
                    for o in imported
                ]
            )
            np.testing.assert_allclose(co.min(axis=0), (0.0, 0.0, 0.0), atol=1e-5)
            np.testing.assert_allclose(co.max(axis=0), (10.0 + N, N, 0.0), atol=1e-5)
    finally:
        _clear_trees()


# --- build profile and File > Export -----------------------------------------------------------------


def _finish():
    publish = h.submodule("blender.publish")
    job = publish.active_job()
    assert job is not None, "no publish job is running"
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    assert publish.update_from_job() is None
    return job


def test_glb_build_profile_end_to_end() -> None:
    h.reset_scene()
    _scene()
    panels = h.submodule("blender.panels")
    try:
        with _tmpdir() as tmp:
            assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
            profile = h.project().build_profiles[0]
            profile.output_path = str(tmp / "site.3tz")
            profile.output_type = "GLB"
            assert profile.output_path == str(tmp / "site.glb")  # the suffix follows
            profile.glb_face_table = True
            h.draw(panels.GEOTOOLS_PT_publish)
            assert h.operator_text("geotools.publish") == "Export site.glb"
            calls = h.draw(panels.GEOTOOLS_PT_publish)
            drawn = h.props_drawn(calls)
            assert "glb_face_table" in drawn and "glb_earth_placed" in drawn
            assert "validate" not in drawn
            settings = h.props_drawn(h.draw(panels.GEOTOOLS_PT_publish_settings))
            assert settings == ["apply_modifiers"]

            assert bpy.ops.geotools.publish() == {"FINISHED"}
            job = _finish()
            assert job.state.name == "DONE", job.error
            output = tmp / "site.glb"
            assert job.output == output and output.is_file()
            glbcheck.check(output)
            staging = tmp / "site.glb.geotools"
            record = json.loads((staging / "build.json").read_text())
            assert record["schema"] == 2 and record["output_type"] == "glb"
            assert record["output"] == str(output)
            glb = record["glb"]
            assert glb["extensions_used"] == [
                "EXT_mesh_features",
                "EXT_structural_metadata",
                "KHR_xmp_json_ld",
            ]
            assert glb["options"] == {
                "face_table": True,
                "earth_placed": False,
                "apply_modifiers": True,
                "variant": None,
            }
            assert glb["face_layers"] == ["slope", "landcover", "height"]
            assert glb["vertex_layers"] == ["count"] and glb["texture_layers"] == ["shade"]
            # Corner values unique per face corner already split every vertex here.
            assert glb["face_table_cost"]["vertex_ratio"] == 1.0
            assert glb["bytes"] == output.stat().st_size
            assert record["placement"]["crs_epsg"] == "EPSG:7856"
            assert [r["name"] for r in record["layers"]] == [
                "slope",
                "count",
                "landcover",
                "height",
                "shade",
            ]
            assert record["geotools"]["build_profile"] == profile.name
            assert sorted(p.name for p in staging.iterdir()) == [
                ".lock",
                "build.json",
                "facemap.npz",
            ]
            profile = h.project().build_profiles[0]
            assert profile.last_output == str(output) and profile.last_tiles == ""
            item = h.draw_list_item(
                h.submodule("blender.profiles").GEOTOOLS_UL_build_profiles, h.project(), profile
            )
            assert ".glb" in [text for text, _icon in h.labels(item)]
            last = h.draw(panels.GEOTOOLS_PT_publish_last)
            assert not any(
                kind == "operator" and n == "geotools.preview_output" for kind, n, _i in last
            )

            # A plain .3tz of the same name keeps its own staging directory.
            profile.output_type = "3TZ"
            assert profile.output_path == str(tmp / "site.3tz")
            # A path whose suffix does not match the format is refused.
            profile.output_path = str(tmp / "site.glb")
            _result, error = h.call_op(bpy.ops.geotools.publish)
            assert error and ".3tz" in error, error
    finally:
        _clear_trees()


def test_file_export_entry() -> None:
    h.reset_scene()
    _scene()
    try:
        with _tmpdir() as tmp:
            path = tmp / "oneoff.glb"
            assert bpy.ops.geotools.export_glb(filepath=str(path), face_table=False) == {"FINISHED"}
            glb = glbcheck.check(path)
            assert _object_names(glb) == ["Hut", "Terrain"]
            # Refused with errors from the checks: a BLEND material.
            material = bpy.data.objects["Hut"].data.materials[0]
            material.use_nodes = True
            bsdf = next(
                n for n in material.node_tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled"
            )
            bsdf.inputs["Alpha"].default_value = 0.5
            _result, error = h.call_op(bpy.ops.geotools.export_glb, filepath=str(tmp / "x.glb"))
            assert error and "Cannot export" in error, error
            assert not (tmp / "x.glb").exists()
    finally:
        _clear_trees()
