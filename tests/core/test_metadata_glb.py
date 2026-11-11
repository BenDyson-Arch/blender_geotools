"""The metadata GLB (M4): schema from layers, packing, XMP, the writer hook, georeference.

The external validators run when their paths are set (skipped otherwise):

- ``GEOTOOLS_TEST_3D_TILES_VALIDATOR``: the 3d-tiles-validator CLI
  (``npx 3d-tiles-validator`` installed locally, or its ``build/main``), run as
  ``<cli> --tileContentFile FILE --reportFile REPORT``;
- ``GEOTOOLS_TEST_GLTF_VALIDATOR``: the Khronos glTF-Validator CLI, run as
  ``<cli> -o FILE`` (the JSON report on stdout).
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
from pathlib import Path

import glbcheck
import numpy as np
import pytest

from geotools.core import georef
from geotools.core import gltf_metadata as gm
from geotools.core import metadata_schema as ms
from geotools.core.gltf_writer import ExportError, PrimitiveData, SceneExport, write_glb
from geotools.core.layers import DataType, Domain, LayerSpec
from geotools.core.placement import Placement, PlacementMode
from geotools.core.probe import PlacementProbe

SLOPE = LayerSpec(
    "slope",
    Domain.FACE,
    DataType.FLOAT,
    units="deg",
    nodata=-9999.0,
    value_min=0.0,
    value_max=3.0,
    label="Slope",
    description="Steepness of the face",
)
LAND = LayerSpec(
    "landcover", Domain.FACE, DataType.CATEGORY, categories=("bare", "grass"), nodata=-1
)
WET = LayerSpec("wet", Domain.CORNER, DataType.BOOLEAN)
COUNT = LayerSpec("count", Domain.CORNER, DataType.INT, value_min=-3, value_max=7)
SHADE = LayerSpec("shade", Domain.TEXTURE, DataType.FLOAT, value_min=0.0, value_max=1.0)
ZONE = LayerSpec("zone", Domain.TEXTURE, DataType.CATEGORY, categories=("a", "b", "c"))


# --- schema ----------------------------------------------------------------------------


def test_types_per_storage() -> None:
    def kind(spec, storage):
        t = ms.property_type(spec, storage)
        return (t.type, t.component_type or t.enum_type, t.value_type)

    assert kind(SLOPE, ms.Storage.TABLE) == ("SCALAR", "FLOAT32", "FLOAT32")
    assert kind(SLOPE, ms.Storage.ATTRIBUTE) == ("SCALAR", "FLOAT32", "FLOAT32")
    assert kind(SLOPE, ms.Storage.TEXTURE) == ("SCALAR", "FLOAT32", "FLOAT32")
    assert kind(COUNT, ms.Storage.TABLE) == ("SCALAR", "INT32", "INT32")
    assert kind(COUNT, ms.Storage.ATTRIBUTE) == ("SCALAR", "FLOAT32", "FLOAT32")
    assert kind(COUNT, ms.Storage.TEXTURE) == ("SCALAR", "INT32", "INT32")
    assert kind(WET, ms.Storage.TABLE) == ("BOOLEAN", None, "BOOLEAN")
    assert kind(WET, ms.Storage.ATTRIBUTE) == ("SCALAR", "UINT8", "UINT8")
    assert kind(WET, ms.Storage.TEXTURE) == ("SCALAR", "UINT8", "UINT8")
    for storage in ms.Storage:
        assert kind(LAND, storage) == ("ENUM", "landcover_category", "INT8")
    assert ms.property_type(SHADE, ms.Storage.TEXTURE).size == 4


def test_enum_value_type_is_the_smallest_that_fits() -> None:
    def vt(n, nodata=None):
        spec = LayerSpec(
            "c", Domain.FACE, DataType.CATEGORY, categories=tuple(map(str, range(n))), nodata=nodata
        )
        return ms.enum_value_type(spec)

    assert vt(2) == "UINT8"
    assert vt(256) == "UINT8"
    assert vt(257) == "UINT16"
    assert vt(2, nodata=-1) == "INT8"
    assert vt(200, nodata=-1) == "INT16"
    assert vt(2, nodata=1000) == "UINT16"
    assert vt(2, nodata=-100_000) == "INT32"
    big = LayerSpec("c", Domain.FACE, DataType.CATEGORY, categories=("a",), nodata=-100_000)
    with pytest.raises(ms.SchemaError, match="16 bits"):
        ms.property_type(big, ms.Storage.ATTRIBUTE)
    assert ms.property_type(big, ms.Storage.TABLE).value_type == "INT32"


def test_property_definition_carries_label_units_nodata_and_range() -> None:
    prop = ms.property_definition(SLOPE, ms.Storage.ATTRIBUTE)
    assert list(prop) == ["name", "description", "type", "componentType", "noData", "min", "max"]
    assert prop["name"] == "Slope"
    assert prop["description"].startswith("Steepness of the face. Units: deg.")
    assert "GeoTools layer slope (face float)" in prop["description"]
    assert (prop["noData"], prop["min"], prop["max"]) == (-9999.0, 0.0, 3.0)
    count = ms.property_definition(COUNT, ms.Storage.TABLE)
    assert (count["min"], count["max"]) == (-3, 7) and isinstance(count["min"], int)
    assert "noData" not in count  # none set
    attr = ms.property_definition(COUNT, ms.Storage.ATTRIBUTE)
    assert attr["componentType"] == "FLOAT32" and isinstance(attr["min"], float)
    assert "FLOAT32" in attr["description"]
    wet = ms.property_definition(
        LayerSpec("wet", Domain.FACE, DataType.BOOLEAN, value_min=0, value_max=1),
        ms.Storage.TABLE,
    )
    assert "noData" not in wet and "min" not in wet and wet["type"] == "BOOLEAN"
    land = ms.property_definition(LAND, ms.Storage.TABLE)
    assert land["noData"] == ms.NODATA_ENUM_NAME and "min" not in land
    assert land["name"] == "landcover"  # no label: the name


def test_int_layer_beyond_float32_refused_as_attribute() -> None:
    spec = LayerSpec("big", Domain.FACE, DataType.INT, value_max=float(2**25))
    with pytest.raises(ms.SchemaError, match="2\\^24"):
        ms.property_type(spec, ms.Storage.ATTRIBUTE)
    assert ms.property_type(spec, ms.Storage.TABLE).component_type == "INT32"


def test_enums_and_stable_ids() -> None:
    labels = LayerSpec(
        "kind", Domain.FACE, DataType.CATEGORY, categories=("a", "a", "no data"), nodata=-1
    )
    enum = ms.enum_definition(labels)
    assert enum["valueType"] == "INT8"
    assert [v["name"] for v in enum["values"]] == [
        "a",
        "a (value 1)",
        "no data (value 2)",
        "no data",
    ]
    assert [v["value"] for v in enum["values"]] == [0, 1, 2, -1]
    assert ms.enum_id(labels) == "kind_category" and ms.property_id(labels) == "kind"
    assert ms.attribute_semantic(SLOPE) == "_SLOPE"
    clash = LayerSpec("feature_id_0", Domain.FACE, DataType.FLOAT)
    assert ms.attribute_semantic(clash) == "_GT_FEATURE_ID_0"
    assert ms.attribute_semantic(LayerSpec("feature_id_x", Domain.FACE, DataType.FLOAT)) == (
        "_FEATURE_ID_X"
    )
    schema = ms.build_schema(
        {"vertex": ms.layer_class([SLOPE, LAND], ms.Storage.ATTRIBUTE, name="V", description="")},
        [SLOPE, LAND],
        version="1.2.3",
    )
    assert list(schema) == ["id", "name", "description", "version", "classes", "enums"]
    assert list(schema["enums"]) == ["landcover_category"]
    assert list(schema["classes"]["vertex"]["properties"]) == ["slope", "landcover"]
    # The same layers give the same schema.
    again = ms.build_schema(
        {"vertex": ms.layer_class([SLOPE, LAND], ms.Storage.ATTRIBUTE, name="V", description="")},
        [SLOPE, LAND],
        version="1.2.3",
    )
    assert json.dumps(schema) == json.dumps(again)
    with pytest.raises(ms.SchemaError, match="Two layers"):
        ms.layer_class([SLOPE, SLOPE], ms.Storage.TABLE, name="x", description="")


# --- packing ----------------------------------------------------------------------------


def test_pack_strings_and_values() -> None:
    data, offsets = gm.pack_strings(["A", "Bé", ""])
    assert data == b"AB\xc3\xa9"
    assert np.frombuffer(offsets, "<u4").tolist() == [0, 1, 4, 4]
    assert gm.pack_strings([]) == (b"", b"\0\0\0\0")
    bools = ms.property_type(WET, ms.Storage.TABLE)
    bits = gm.pack_values(np.array([1, 0, 1, 1, 0, 0, 0, 0, 1], bool), bools)
    assert bits == bytes([0b00001101, 0b00000001])
    ints = ms.property_type(COUNT, ms.Storage.TABLE)
    assert np.frombuffer(gm.pack_values(np.array([1.0, -2.0]), ints), "<i4").tolist() == [1, -2]
    with pytest.raises(ExportError, match="NaN"):
        gm.pack_values(np.array([np.nan]), ints)
    enum = ms.property_type(LAND, ms.Storage.TABLE)
    with pytest.raises(ExportError, match="fit"):
        gm.pack_values(np.array([300]), enum)


def test_layer_attribute_values() -> None:
    assert gm.layer_attribute_values(np.array([True, False]), WET).dtype == np.uint8
    counted = gm.layer_attribute_values(np.array([1, -3], np.int32), COUNT)
    assert counted.dtype == np.float32 and counted.tolist() == [1.0, -3.0]
    land = gm.layer_attribute_values(np.array([0, 1, -1], np.int32), LAND)
    assert land.dtype == np.int8
    with pytest.raises(ExportError, match="2\\^24"):
        gm.layer_attribute_values(np.array([2**25], np.int32), COUNT)


@pytest.mark.parametrize(
    ("spec", "values"),
    [
        (SHADE, np.array([[0.0, 0.25, np.nan], [1.0, -0.0, 3.5e-41]], np.float32)),
        (
            LayerSpec("n", Domain.TEXTURE, DataType.INT),
            np.array([[1.0, -2.0, 2.0**24], [0.0, 7.0, -5.0]]),
        ),
        (ZONE, np.array([[0.0, 1.0, 2.0], [2.0, 1.0, 0.0]], np.float32)),
        (LayerSpec("b", Domain.TEXTURE, DataType.BOOLEAN), np.array([[0, 1, 1], [1, 0, 0]], float)),
    ],
)
def test_texture_packing_round_trip(spec: LayerSpec, values: np.ndarray) -> None:
    rgba, channels = gm.pack_texture(values, spec)
    assert rgba.shape == (2, 3, 4) and rgba.dtype == np.uint8
    assert channels == list(range(ms.property_type(spec, ms.Storage.TEXTURE).size))
    png = gm.encode_png(rgba)
    decoded = gm.decode_png(png)
    np.testing.assert_array_equal(decoded, rgba)
    back = gm.unpack_texture(decoded, channels, spec)
    if spec.data_type is DataType.FLOAT:
        assert back.dtype == np.float32
        np.testing.assert_array_equal(back.view(np.uint32), values.view(np.uint32))  # bits
    else:
        np.testing.assert_array_equal(back.astype(np.float64), values)
    # Row 0 of the PNG is the top: Blender's last row.
    np.testing.assert_array_equal(rgba[0], gm.pack_texture(values[::-1], spec)[0][-1])


def test_texture_float_bytes_are_little_endian_rgba() -> None:
    rgba, channels = gm.pack_texture(np.array([[1.0]], np.float32), SHADE)
    assert channels == [0, 1, 2, 3]
    assert rgba[0, 0].tobytes() == np.float32(1.0).tobytes()  # 00 00 80 3f
    rgba, channels = gm.pack_texture(np.array([[2.0]]), ZONE)
    assert channels == [0] and rgba[0, 0].tolist() == [2, 0, 0, 255]
    nodata = LayerSpec("z", Domain.TEXTURE, DataType.CATEGORY, categories=("a",), nodata=-1)
    rgba, _ = gm.pack_texture(np.array([[np.nan]]), nodata)
    assert rgba[0, 0, 0] == 0xFF  # -1 as INT8


def test_png_is_deterministic_and_decodes_rgb() -> None:
    rgba = np.arange(2 * 3 * 4, dtype=np.uint8).reshape(2, 3, 4)
    assert gm.encode_png(rgba) == gm.encode_png(rgba.copy())
    with pytest.raises(ValueError):
        gm.encode_png(np.zeros((0, 3, 4), np.uint8))


# --- XMP ---------------------------------------------------------------------------------


def test_xmp_packet() -> None:
    geo = gm.Georeference(
        mode="source_crs",
        geotools_version="0.3.0-dev",
        crs_epsg="EPSG:7856",
        crs_definition="+proj=utm +zone=56 +south",
        source_offset=(500000.0, 6960000.123456789, 12.5),
        height_offset=-0.1,
        origin_lon_lat_height=(153.01, -27.47, 12.4),
        origin_model=(1.0, 2.0, -3.0),
        heading_degrees=-0.93,
        grid_scale=0.9996,
        model_to_ecef=tuple(float(v) for v in range(16)),
        crs_operation="portable",
        source="site.blend",
    )
    packet = gm.xmp_packet(geo)
    assert packet["@context"] == {
        "dc": "http://purl.org/dc/elements/1.1/",
        "geotools": gm.GEOTOOLS_NAMESPACE,
    }
    assert packet["@id"] == ""
    assert packet["dc:format"] == "model/gltf-binary" and packet["dc:source"] == "site.blend"
    assert packet["geotools:crs"] == "EPSG:7856"
    assert packet["geotools:sourceOffset"] == {"@list": [500000.0, 6960000.123456789, 12.5]}
    assert packet["geotools:heightOffset"] == -0.1
    assert packet["geotools:originLatitude"] == -27.47
    assert packet["geotools:heading"] == -0.93
    assert packet["geotools:earthPlaced"] is False
    assert len(packet["geotools:modelToEcef"]["@list"]) == 16
    # binary64 survives JSON exactly.
    text = json.dumps(packet)
    assert json.loads(text)["geotools:sourceOffset"]["@list"][1] == 6960000.123456789
    for key in packet:
        assert key.startswith("@") or key.partition(":")[0] in packet["@context"]
    local = gm.xmp_packet(gm.Georeference(mode="local"))
    assert local["geotools:placement"] == "local" and "geotools:crs" not in local
    with pytest.raises(ExportError, match="finite"):
        gm.xmp_packet(gm.Georeference(mode="anchor", heading_degrees=math.inf))


# --- the extension and the writer ------------------------------------------------------------

_POS = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], np.float32)
_IDX = np.array([[0, 1, 2], [0, 2, 3]], np.uint32)
_UV = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], np.float32)


def _primitive(obj: int, face_table: bool = True, **extra) -> PrimitiveData:
    attrs = [("_FEATURE_ID_0", np.full(4, obj, np.float32))]
    if face_table:
        attrs.append(("_FEATURE_ID_1", np.array([0, 0, 0, 1], np.float32) + 2 * obj))
    attrs += [
        ("_SLOPE", np.array([0, 0, 0, 1] if obj == 0 else [2, 2, 2, -9999], np.float32)),
        ("_LANDCOVER", np.array([0, 0, 0, 1] if obj == 0 else [1, 1, 1, -1], np.int8)),
        ("_WET", np.array([0, 1, 1, 0], np.uint8)),
        ("_COUNT", np.array([-3, 0, 7, 1], np.float32)),
    ]
    return PrimitiveData(
        material=None,
        positions=_POS + np.float32(2 * obj),
        indices=_IDX,
        normals=np.tile(np.float32([0, 0, 1]), (4, 1)),
        texcoords=(_UV,),
        source_object=np.full(2, obj, np.int32),
        source_polygon=np.array([0, 1], np.int32),
        custom_attributes=tuple(attrs),
        **extra,
    )


def _scene(face_table: bool = True) -> SceneExport:
    return SceneExport(("A", "Bé"), (), (), (_primitive(0, face_table), _primitive(1, face_table)))


_SHADE_VALUES = np.linspace(0, 1, 16, dtype=np.float32).reshape(4, 4)
_ZONE_VALUES = np.array([[0, 1], [2, 1]], np.float32)


def _extension(scene: SceneExport, *, face_table: bool = True, matrix=None):
    rgba, ch = gm.pack_texture(_SHADE_VALUES, SHADE)
    zone, zch = gm.pack_texture(_ZONE_VALUES, ZONE)
    textures = [
        (
            gm.PackedTexture("shade", "shade.A", gm.encode_png(rgba), tuple(ch), 0),
            gm.PackedTexture("zone", "zone.A", gm.encode_png(zone), tuple(zch), 0),
        ),
        (),
    ]
    geo = gm.Georeference(
        mode="anchor", anchor=(153.0, -27.5, 10.0), heading_degrees=30.0, earth_placed=True
    )
    kwargs = {}
    if face_table:
        kwargs = {
            "face_layers": [SLOPE, LAND],
            "face_values": {
                "slope": [np.array([0.0, 1.0]), None],
                "landcover": [np.array([0, 1]), np.array([1, -1])],
            },
            "face_counts": [2, 2],
        }
    return gm.build_extension(
        scene,
        object_ids=["id-a", "id-b"],
        vertex_layers=[SLOPE, LAND, WET, COUNT],
        texture_layers=[SHADE, ZONE],
        textures=textures,
        georeference=geo,
        root_matrix=matrix,
        version="0.3.0-dev",
        **kwargs,
    )


def test_metadata_glb_structure(tmp_path: Path) -> None:
    scene = _scene()
    matrix = tuple(float(v) for v in (np.eye(4) * 2).T.ravel())
    path = tmp_path / "site.glb"
    result = write_glb(scene, path, extension=_extension(scene, matrix=matrix))
    glb = glbcheck.check(path)
    doc = glb.doc
    assert doc["extensionsUsed"] == [
        "EXT_mesh_features",
        "EXT_structural_metadata",
        "KHR_xmp_json_ld",
    ]
    # Object identity: one row per object, names with multi-byte UTF-8.
    assert glb.column("object", "name") == ["A", "Bé"]
    assert glb.column("object", "object_id") == ["id-a", "id-b"]
    f0 = glb.primitives[0]["extensions"]["EXT_mesh_features"]["featureIds"]
    assert f0 == [
        {"featureCount": 1, "attribute": 0, "propertyTable": 0, "label": "object"},
        {"featureCount": 2, "attribute": 1, "propertyTable": 1, "label": "face"},
    ]
    assert glb.attribute(1, "_FEATURE_ID_0").tolist() == [1, 1, 1, 1]
    # Face table: object, polygon, then the layers (None filled with no-data).
    assert glb.column("face", "object").tolist() == [0, 0, 1, 1]
    assert glb.column("face", "polygon").tolist() == [0, 1, 0, 1]
    assert glb.column("face", "slope").tolist() == [0.0, 1.0, -9999.0, -9999.0]
    assert glb.column("face", "landcover") == ["bare", "grass", "grass", "no data"]
    # Property attributes: one per layer, the right component types.
    attribute = glb.metadata["propertyAttributes"][0]
    assert attribute["class"] == "vertex"
    assert attribute["properties"]["wet"] == {"attribute": "_WET"}
    acc = doc["accessors"][glb.primitives[0]["attributes"]["_LANDCOVER"]]
    assert acc["componentType"] == 5120  # INT8
    assert doc["bufferViews"][acc["bufferView"]]["byteStride"] == 4
    assert glb.attribute(1, "_LANDCOVER").tolist() == [1, 1, 1, -1]
    # Property textures: on primitive 0 only, NEAREST sampler, values round-trip.
    assert glb.primitives[0]["extensions"]["EXT_structural_metadata"]["propertyTextures"] == [0]
    assert "propertyTextures" not in glb.primitives[1]["extensions"]["EXT_structural_metadata"]
    shade = glb.metadata["propertyTextures"][0]["properties"]["shade"]
    assert shade["channels"] == [0, 1, 2, 3] and shade["texCoord"] == 0
    rgba = gm.decode_png(glb.image(shade["index"]))
    np.testing.assert_array_equal(gm.unpack_texture(rgba, shade["channels"], SHADE), _SHADE_VALUES)
    # XMP on the asset, and the root node.
    assert doc["asset"]["extensions"] == {"KHR_xmp_json_ld": {"packet": 0}}
    packet = doc["extensions"]["KHR_xmp_json_ld"]["packets"][0]
    assert packet["geotools:anchor"] == {"@list": [153.0, -27.5, 10.0]}
    assert doc["nodes"][0]["matrix"] == list(matrix) and doc["nodes"][0]["children"] == [1]
    assert doc["nodes"][1]["mesh"] == 0 and doc["scenes"] == [{"nodes": [0]}]
    assert result.primitives == 2


def test_metadata_glb_is_deterministic(tmp_path: Path) -> None:
    scene = _scene()
    a = write_glb(scene, tmp_path / "a.glb", extension=_extension(scene))
    b = write_glb(scene, tmp_path / "b.glb", extension=_extension(scene))
    assert a.sha256 == b.sha256
    assert (tmp_path / "a.glb").read_bytes() == (tmp_path / "b.glb").read_bytes()


def test_without_face_table(tmp_path: Path) -> None:
    scene = _scene(face_table=False)
    write_glb(scene, tmp_path / "s.glb", extension=_extension(scene, face_table=False))
    glb = glbcheck.check(tmp_path / "s.glb")
    assert [t["class"] for t in glb.metadata["propertyTables"]] == ["object"]
    assert "face" not in glb.schema["classes"]
    sets = glb.primitives[0]["extensions"]["EXT_mesh_features"]["featureIds"]
    assert [s["label"] for s in sets] == ["object"]
    assert glb.doc["nodes"] == [{"mesh": 0}]


def test_plain_writer_refuses_custom_attributes(tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="rusty-tiles refuses"):
        write_glb(_scene(), tmp_path / "plain.glb")
    assert not (tmp_path / "plain.glb").exists()


def test_build_extension_checks_its_inputs() -> None:
    scene = _scene()
    with pytest.raises(ExportError, match="unique"):
        gm.build_extension(scene, object_ids=["x", "x"])
    with pytest.raises(ExportError, match="One object id"):
        gm.build_extension(scene, object_ids=["x"])
    with pytest.raises(ExportError, match="_MISSING"):
        gm.build_extension(
            scene,
            object_ids=["a", "b"],
            vertex_layers=[LayerSpec("missing", Domain.FACE, DataType.FLOAT)],
        )
    with pytest.raises(ExportError, match="has 3 values"):
        gm.build_extension(
            scene,
            object_ids=["a", "b"],
            face_layers=[SLOPE],
            face_values={"slope": [np.zeros(3), None]},
            face_counts=[2, 2],
        )
    reserved = LayerSpec("polygon", Domain.FACE, DataType.FLOAT)
    with pytest.raises(ExportError, match="face table uses itself"):
        gm.build_extension(scene, object_ids=["a", "b"], face_layers=[reserved], face_counts=[2, 2])


def test_custom_attribute_validation(tmp_path: Path) -> None:
    ext = _extension(_scene())

    def write(attrs):
        prim = PrimitiveData(material=None, positions=_POS, indices=_IDX, custom_attributes=attrs)
        write_glb(SceneExport(("A",), (), (), (prim,)), tmp_path / "x.glb", extension=ext)

    with pytest.raises(ExportError, match="must start with '_'"):
        write((("SLOPE", np.zeros(4, np.float32)),))
    with pytest.raises(ExportError, match="twice"):
        write((("_A", np.zeros(4, np.float32)), ("_A", np.zeros(4, np.float32))))
    with pytest.raises(ExportError, match=r"\(4,\) array"):
        write((("_A", np.zeros(3, np.float32)),))
    with pytest.raises(ExportError, match="int32"):
        write((("_A", np.zeros(4, np.int32)),))


# --- georeference -----------------------------------------------------------------------------


def _probe(lon, lat, h, matrix: np.ndarray | None = None) -> PlacementProbe:
    if matrix is None:
        matrix = np.eye(4)
        matrix[:3, 3] = georef.ecef_from_lon_lat_height(lon, lat, h)
    return PlacementProbe(
        ok=True,
        origin_lon_lat_height=(lon, lat, h),
        root_transform=tuple(float(v) for v in matrix.T.ravel()),
        operation="portable",
    )


def _enu(lon: float, lat: float, h: float) -> np.ndarray:
    lo, la = math.radians(lon), math.radians(lat)
    m = np.eye(4)
    m[:3, 0] = (-math.sin(lo), math.cos(lo), 0.0)
    m[:3, 1] = (-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la))
    m[:3, 2] = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    m[:3, 3] = georef.ecef_from_lon_lat_height(lon, lat, h)
    return m


def test_anchor_frame_is_the_root_transform() -> None:
    root = _enu(153.0, -27.5, 10.0)
    frame = georef.frame_from_probes(
        PlacementMode.ANCHOR, (5.0, 5.0, 0.0), _probe(153.0, -27.5, 10.0, root)
    )
    np.testing.assert_array_equal(frame.matrix, root)
    assert frame.origin_local == pytest.approx((0.0, 0.0, 0.0), abs=1e-6)
    assert frame.heading_degrees == pytest.approx(0.0, abs=1e-9)
    node = np.array(georef.gltf_node_matrix(frame)).reshape(4, 4).T
    c = np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, 0], [0, 0, 0, 1]], float)
    np.testing.assert_allclose(np.linalg.inv(c) @ node @ c, root)
    # A glTF point: the viewer's Y-up to Z-up turn after the node gives ECEF.
    p_blender = np.array([3.0, 4.0, 5.0, 1.0])
    p_gltf = c @ p_blender
    np.testing.assert_allclose(np.linalg.inv(c) @ node @ p_gltf, root @ p_blender)


def test_source_crs_frame_carries_convergence() -> None:
    lon, lat = 151.0, -27.5
    root = _enu(lon, lat, 0.0)
    # Grid east lands 1 deg anticlockwise of true east, grid north likewise; scale 0.9996.
    angle, k, d = math.radians(1.0), 0.9996, 50.0
    east_enu = k * d * np.array([math.cos(angle), math.sin(angle), 0.0])
    north_enu = k * d * np.array([-math.sin(angle), math.cos(angle), 0.0])

    def probe_at(enu: np.ndarray) -> PlacementProbe:
        ecef = root[:3, :3] @ enu + root[:3, 3]
        # Invert ECEF to geodetic by iteration (test helper).
        x, y, z = ecef
        lo = math.atan2(y, x)
        p = math.hypot(x, y)
        la = math.atan2(z, p)
        for _ in range(10):
            n = 6378137.0 / math.sqrt(1 - georef._WGS84_E2 * math.sin(la) ** 2)
            h = p / math.cos(la) - n
            la = math.atan2(z, p * (1 - georef._WGS84_E2 * n / (n + h)))
        return _probe(math.degrees(lo), math.degrees(la), h)

    centre = (100.0, 200.0, 0.0)
    frame = georef.frame_from_probes(
        PlacementMode.SOURCE_CRS,
        centre,
        _probe(lon, lat, 0.0, root),
        probe_at(east_enu),
        probe_at(north_enu),
        (d, d),
    )
    assert frame.heading_degrees == pytest.approx(1.0, abs=1e-6)
    assert frame.scale == pytest.approx(k, abs=1e-9)
    assert frame.origin_local == pytest.approx(centre, abs=1e-6)
    corner = frame.matrix @ np.array([100.0 + d, 200.0, 0.0, 1.0])
    np.testing.assert_allclose(corner[:3], root[:3, :3] @ east_enu + root[:3, 3], atol=1e-6)


def test_local_placement_has_no_frame() -> None:
    with pytest.raises(georef.GeorefError):
        georef.measure_frame(Placement(), (0, 0, 0), (1, 1, 1))


@pytest.fixture
def rusty_tiles():
    return pytest.importorskip("rusty_tiles")


def test_measure_frame_matches_rusty_tiles(rusty_tiles) -> None:
    from geotools.core import probe

    anchor = Placement(PlacementMode.ANCHOR, anchor=(153.0, -27.5, 10.0), heading_degrees=30.0)
    frame = georef.measure_frame(anchor, (100.0, 200.0, 5.0), (110.0, 230.0, 9.0))
    result = probe.probe_placement(anchor, (100.0, 200.0, 5.0), (110.0, 230.0, 9.0))
    assert frame.model_to_ecef == result.root_transform
    assert frame.heading_degrees == pytest.approx(30.0, abs=1e-9)

    # 200 km west of the UTM 56 central meridian: about 0.9 degrees of convergence.
    crs = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs="EPSG:32756",
        source_offset=(300000.0, 6960000.0, 0.0),
    )
    lo, hi = (-100.0, -100.0, 0.0), (100.0, 100.0, 20.0)
    frame = georef.measure_frame(crs, lo, hi)
    assert -1.0 < frame.heading_degrees < -0.8
    # A corner of the site lands where rusty-tiles puts it, to the millimetre.
    corner = np.array([100.0, -100.0, 20.0])
    tiny = probe.probe_placement(crs, tuple(corner - 0.5), tuple(corner + 0.5))
    expected = georef.ecef_from_lon_lat_height(*tiny.origin_lon_lat_height)
    got = frame.matrix @ np.append(corner, 1.0)
    assert np.linalg.norm(got[:3] - expected) < 0.01
    # Copying the root transform would be metres off at that corner.
    root = np.array(probe.probe_placement(crs, lo, hi).root_transform).reshape(4, 4).T
    naive = root @ np.append(corner - np.array([0.0, 0.0, 10.0]), 1.0)
    assert np.linalg.norm(naive[:3] - expected) > 1.0


# --- external validators (optional) ---------------------------------------------------------


def _write_sample(tmp_path: Path) -> Path:
    scene = _scene()
    path = tmp_path / "sample.glb"
    write_glb(scene, path, extension=_extension(scene, matrix=tuple(np.eye(4).T.ravel() * 1.0)))
    return path


TILES_VALIDATOR = os.environ.get("GEOTOOLS_TEST_3D_TILES_VALIDATOR", "")
GLTF_VALIDATOR = os.environ.get("GEOTOOLS_TEST_GLTF_VALIDATOR", "")


@pytest.mark.skipif(not TILES_VALIDATOR, reason="set GEOTOOLS_TEST_3D_TILES_VALIDATOR")
def test_3d_tiles_validator_accepts_the_glb(tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    report = tmp_path / "report.json"
    subprocess.run(
        [
            *shlex.split(TILES_VALIDATOR),
            "--tileContentFile",
            str(path),
            "--reportFile",
            str(report),
        ],
        check=False,
        capture_output=True,
        timeout=300,
    )
    result = json.loads(report.read_text())
    assert result["numErrors"] == 0 and result["numWarnings"] == 0, json.dumps(result, indent=1)


@pytest.mark.skipif(not GLTF_VALIDATOR, reason="set GEOTOOLS_TEST_GLTF_VALIDATOR")
def test_gltf_validator_accepts_the_glb(tmp_path: Path) -> None:
    path = _write_sample(tmp_path)
    done = subprocess.run(
        [*shlex.split(GLTF_VALIDATOR), "-o", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=300,
    )
    report = json.loads(done.stdout)
    issues = report["issues"]
    assert issues["numErrors"] == 0 and issues["numWarnings"] == 0, done.stdout
