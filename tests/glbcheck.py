"""A small structural validator and decoder for GeoTools metadata GLBs (tests only).

Shared by the core tests (pytest) and the Blender suite (its own runner), so it
needs only numpy and the standard library. It checks what the external
validators would most likely catch, without them:

- the GLB container (header, chunk lengths, 8-byte BIN alignment);
- accessors fit their buffer views, with valid component types and strides;
- every primitive's attributes have POSITION's count;
- ``extensionsUsed`` lists what is used, ``extensionsRequired`` is absent;
- ``EXT_mesh_features``: feature ID attributes exist, are SCALAR, not
  normalized, whole numbers, fewer distinct values than ``featureCount`` and
  within the property table's rows;
- ``EXT_structural_metadata``: the schema's ids, enum values and references;
  property tables (value lengths, string offsets including object names,
  booleans, enums); property attributes (attribute present, component type
  matching the class, enum values and min/max respected); property textures
  (texture, texCoord, channel count, NEAREST sampler);
- ``KHR_xmp_json_ld``: packets with a ``@context`` dictionary, the asset's
  packet reference.

``check(data)`` raises ``AssertionError`` with a message and returns a
:class:`Glb` for decoding values.
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ID_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
COMPONENTS = {
    5120: ("i1", 1),
    5121: ("u1", 1),
    5122: ("<i2", 2),
    5123: ("<u2", 2),
    5125: ("<u4", 4),
    5126: ("<f4", 4),
}
WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}
METADATA_TYPES = {
    "INT8": "i1",
    "UINT8": "u1",
    "INT16": "<i2",
    "UINT16": "<u2",
    "INT32": "<i4",
    "UINT32": "<u4",
    "INT64": "<i8",
    "UINT64": "<u8",
    "FLOAT32": "<f4",
    "FLOAT64": "<f8",
}
GLTF_FOR_METADATA = {"INT8": 5120, "UINT8": 5121, "INT16": 5122, "UINT16": 5123, "FLOAT32": 5126}
EXT_MF = "EXT_mesh_features"
EXT_SM = "EXT_structural_metadata"
XMP = "KHR_xmp_json_ld"


@dataclass
class Glb:
    doc: dict
    bin: bytes

    # --- raw access -------------------------------------------------------------

    def view(self, index: int) -> bytes:
        view = self.doc["bufferViews"][index]
        start = view.get("byteOffset", 0)
        return self.bin[start : start + view["byteLength"]]

    def accessor(self, index: int) -> np.ndarray:
        acc = self.doc["accessors"][index]
        dtype, size = COMPONENTS[acc["componentType"]]
        width = WIDTHS[acc["type"]]
        view = self.doc["bufferViews"][acc["bufferView"]]
        stride = view.get("byteStride", size * width)
        data = self.view(acc["bufferView"])[acc.get("byteOffset", 0) :]
        count = acc["count"]
        rows = np.frombuffer(data[: stride * (count - 1) + size * width], dtype=np.uint8)
        rows = np.lib.stride_tricks.as_strided(rows, (count, size * width), (stride, 1))
        values = np.ascontiguousarray(rows).view(dtype).reshape(count, width)
        return values[:, 0] if width == 1 else values

    def attribute(self, primitive: int, name: str) -> np.ndarray:
        prim = self.doc["meshes"][0]["primitives"][primitive]
        return self.accessor(prim["attributes"][name])

    @property
    def primitives(self) -> list[dict]:
        return self.doc["meshes"][0]["primitives"]

    @property
    def metadata(self) -> dict:
        return self.doc["extensions"][EXT_SM]

    @property
    def schema(self) -> dict:
        return self.metadata["schema"]

    def image(self, texture: int) -> bytes:
        source = self.doc["textures"][texture]["source"]
        return self.view(self.doc["images"][source]["bufferView"])

    # --- property tables --------------------------------------------------------

    def table(self, index_or_class: int | str) -> dict:
        tables = self.metadata["propertyTables"]
        if isinstance(index_or_class, int):
            return tables[index_or_class]
        return next(t for t in tables if t["class"] == index_or_class)

    def column(self, index_or_class: int | str, prop: str) -> list | np.ndarray:
        """A property table column: a list of str, an array of numbers or bools, or
        enum names (a list of str)."""
        table = self.table(index_or_class)
        definition = self.schema["classes"][table["class"]]["properties"][prop]
        entry = table["properties"][prop]
        count = table["count"]
        data = self.view(entry["values"])
        kind = definition["type"]
        if kind == "STRING":
            offsets = np.frombuffer(self.view(entry["stringOffsets"]), "<u4")
            return [data[offsets[i] : offsets[i + 1]].decode("utf-8") for i in range(count)]
        if kind == "BOOLEAN":
            bits = np.unpackbits(np.frombuffer(data, np.uint8), bitorder="little")
            return bits[:count].astype(bool)
        if kind == "ENUM":
            enum = self.schema["enums"][definition["enumType"]]
            values = np.frombuffer(data, METADATA_TYPES[enum.get("valueType", "UINT16")])
            names = {v["value"]: v["name"] for v in enum["values"]}
            return [names[int(v)] for v in values[:count]]
        return np.frombuffer(data, METADATA_TYPES[definition["componentType"]])[:count]


def read(data: bytes | Path, *, metadata: bool = True) -> Glb:
    """Parse the GLB container; ``metadata`` also asks for 8-byte BIN alignment."""
    if isinstance(data, Path):
        data = data.read_bytes()
    magic, version, length = struct.unpack_from("<III", data, 0)
    assert magic == 0x46546C67 and version == 2, "not a glTF 2.0 GLB"
    assert length == len(data), f"header length {length} != file length {len(data)}"
    json_length, json_kind = struct.unpack_from("<II", data, 12)
    assert json_kind == 0x4E4F534A and json_length % 4 == 0, "bad JSON chunk"
    doc = json.loads(data[20 : 20 + json_length])
    bin_header = 20 + json_length
    bin_length, bin_kind = struct.unpack_from("<II", data, bin_header)
    assert bin_kind == 0x004E4942, "bad BIN chunk"
    start = bin_header + 8
    assert start + bin_length == len(data), "BIN chunk length"
    if metadata:
        assert start % 8 == 0, f"BIN data starts at {start}, not 8-byte aligned"
    return Glb(doc, data[start:])


# --- checks ------------------------------------------------------------------------


def _check_accessors(glb: Glb, align: int) -> None:
    doc = glb.doc
    assert len(doc["buffers"]) == 1 and doc["buffers"][0]["byteLength"] <= len(glb.bin)
    for i, view in enumerate(doc["bufferViews"]):
        start = view.get("byteOffset", 0)
        assert start % align == 0, f"bufferView {i} offset {start} not {align}-aligned"
        assert start + view["byteLength"] <= doc["buffers"][0]["byteLength"], f"bufferView {i}"
    for i, acc in enumerate(doc["accessors"]):
        assert acc["componentType"] in COMPONENTS, f"accessor {i} componentType"
        assert acc["type"] in WIDTHS, f"accessor {i} type"
        assert acc["count"] >= 1, f"accessor {i} count"
        view = doc["bufferViews"][acc["bufferView"]]
        _dtype, size = COMPONENTS[acc["componentType"]]
        element = size * WIDTHS[acc["type"]]
        stride = view.get("byteStride", element)
        assert stride >= element and stride % 4 == 0 or "byteStride" not in view, f"stride {i}"
        need = acc.get("byteOffset", 0) + stride * (acc["count"] - 1) + element
        assert need <= view["byteLength"], f"accessor {i} needs {need} bytes of its view"


def _check_extensions_used(doc: dict) -> set[str]:
    used = set(doc.get("extensionsUsed", ()))
    assert "extensionsRequired" not in doc, "metadata extensions are optional"
    found = set(doc.get("extensions", {}))
    for prim in doc["meshes"][0]["primitives"]:
        found |= set(prim.get("extensions", {}))
    if "extensions" in doc.get("asset", {}):
        found |= set(doc["asset"]["extensions"])
    assert found <= used, f"extensions {found - used} used but not in extensionsUsed"
    assert used <= found, f"extensionsUsed lists {used - found}, which nothing uses"
    return used


def _check_schema(schema: dict) -> None:
    assert ID_RE.match(schema["id"]), schema["id"]
    enums = schema.get("enums", {})
    for eid, enum in enums.items():
        assert ID_RE.match(eid), eid
        values = enum["values"]
        names = [v["name"] for v in values]
        numbers = [v["value"] for v in values]
        assert len(set(names)) == len(names), f"enum {eid} repeats a name"
        assert len(set(numbers)) == len(numbers), f"enum {eid} repeats a value"
        lo, hi = _int_range(enum.get("valueType", "UINT16"))
        assert all(lo <= n <= hi for n in numbers), f"enum {eid} value out of range"
    for cid, cls in schema["classes"].items():
        assert ID_RE.match(cid), cid
        for pid, prop in cls["properties"].items():
            assert ID_RE.match(pid), pid
            kind = prop["type"]
            assert kind in ("SCALAR", "STRING", "BOOLEAN", "ENUM"), (cid, pid, kind)
            if kind == "SCALAR":
                assert prop["componentType"] in METADATA_TYPES, (cid, pid)
            if kind == "ENUM":
                assert prop["enumType"] in enums, f"{cid}.{pid} names a missing enum"
                if "noData" in prop:
                    names = [v["name"] for v in enums[prop["enumType"]]["values"]]
                    assert prop["noData"] in names, f"{cid}.{pid} noData is not an enum name"
            if kind == "BOOLEAN":
                assert "noData" not in prop, f"{cid}.{pid}: BOOLEAN has no noData"
            if prop.get("required"):
                assert "noData" not in prop, f"{cid}.{pid}: required with noData"
            if "min" in prop and "max" in prop:
                assert prop["min"] <= prop["max"], f"{cid}.{pid} min > max"


def _int_range(name: str) -> tuple[int, int]:
    info = np.iinfo(np.dtype(METADATA_TYPES[name]))
    return int(info.min), int(info.max)


def _value_type(schema: dict, prop: dict) -> str:
    if prop["type"] == "ENUM":
        return schema["enums"][prop["enumType"]].get("valueType", "UINT16")
    return prop["componentType"]


def _check_range(name: str, prop: dict, values: np.ndarray) -> None:
    values = np.asarray(values, dtype=np.float64)
    if "noData" in prop and not isinstance(prop["noData"], str):
        values = values[values != float(prop["noData"])]
    values = values[np.isfinite(values)]
    if values.size == 0:
        return
    if "min" in prop:
        assert values.min() >= prop["min"], f"{name}: {values.min()} below min {prop['min']}"
    if "max" in prop:
        assert values.max() <= prop["max"], f"{name}: {values.max()} above max {prop['max']}"


def _check_tables(glb: Glb) -> None:
    schema = glb.schema
    for t, table in enumerate(glb.metadata.get("propertyTables", ())):
        cls = schema["classes"][table["class"]]
        count = table["count"]
        assert count >= 1, f"table {t} count"
        for pid, prop in cls["properties"].items():
            if prop.get("required"):
                assert pid in table["properties"], f"table {t} lacks required {pid}"
        for pid, entry in table["properties"].items():
            prop = cls["properties"][pid]
            data = glb.view(entry["values"])
            where = f"table {t} ({table['class']}).{pid}"
            if prop["type"] == "STRING":
                assert entry.get("stringOffsetType", "UINT32") == "UINT32", where
                offsets = np.frombuffer(glb.view(entry["stringOffsets"]), "<u4")
                assert offsets.size == count + 1, f"{where}: {offsets.size} string offsets"
                assert offsets[0] == 0 and (np.diff(offsets.astype(np.int64)) >= 0).all(), where
                assert offsets[-1] == len(data), f"{where}: last offset {offsets[-1]}"
                for value in glb.column(t, pid):
                    assert isinstance(value, str)
            elif prop["type"] == "BOOLEAN":
                assert len(data) == (count + 7) // 8, f"{where}: {len(data)} bytes of bits"
            else:
                kind = _value_type(schema, prop)
                size = np.dtype(METADATA_TYPES[kind]).itemsize
                assert len(data) == count * size, f"{where}: {len(data)} bytes for {count} rows"
                view_offset = glb.doc["bufferViews"][entry["values"]].get("byteOffset", 0)
                assert view_offset % size == 0, f"{where}: misaligned"
                values = np.frombuffer(data, METADATA_TYPES[kind])
                if prop["type"] == "ENUM":
                    allowed = {v["value"] for v in schema["enums"][prop["enumType"]]["values"]}
                    assert set(np.unique(values).tolist()) <= allowed, f"{where}: bad enum value"
                else:
                    _check_range(where, prop, values)


def _check_primitive(glb: Glb, p: int, prim: dict) -> None:
    doc = glb.doc
    count = doc["accessors"][prim["attributes"]["POSITION"]]["count"]
    for name, index in prim["attributes"].items():
        assert doc["accessors"][index]["count"] == count, f"primitive {p} {name} count"
    if "indices" in prim:
        indices = glb.accessor(prim["indices"])
        assert indices.max() < count, f"primitive {p} index out of range"
    extensions = prim.get("extensions", {})
    tables = glb.metadata.get("propertyTables", []) if EXT_SM in doc.get("extensions", {}) else []
    for f, feature in enumerate(extensions.get(EXT_MF, {}).get("featureIds", ())):
        assert feature["featureCount"] >= 1
        if "label" in feature:
            assert ID_RE.match(feature["label"]), feature["label"]
        name = f"_FEATURE_ID_{feature['attribute']}"
        assert name in prim["attributes"], f"primitive {p} lacks {name}"
        acc = doc["accessors"][prim["attributes"][name]]
        assert acc["type"] == "SCALAR" and not acc.get("normalized", False), name
        values = glb.attribute(p, name).astype(np.float64)
        assert (values == np.round(values)).all() and (values >= 0).all(), f"{name} not ids"
        distinct = np.unique(values)
        assert distinct.size <= feature["featureCount"], (
            f"primitive {p} feature set {f}: {distinct.size} ids > featureCount"
        )
        if "propertyTable" in feature:
            rows = tables[feature["propertyTable"]]["count"]
            assert distinct.max() < rows, f"primitive {p} {name}: id beyond {rows} rows"
    metadata = extensions.get(EXT_SM, {})
    schema = glb.schema if metadata else None
    for a in metadata.get("propertyAttributes", ()):
        attribute = glb.metadata["propertyAttributes"][a]
        cls = schema["classes"][attribute["class"]]
        for pid, entry in attribute["properties"].items():
            prop = cls["properties"][pid]
            assert prop["type"] in ("SCALAR", "ENUM"), f"{pid}: {prop['type']} as attribute"
            semantic = entry["attribute"]
            assert semantic.startswith("_"), semantic
            assert semantic in prim["attributes"], f"primitive {p} lacks {semantic}"
            acc = doc["accessors"][prim["attributes"][semantic]]
            kind = _value_type(schema, prop)
            assert acc["componentType"] == GLTF_FOR_METADATA[kind], f"{semantic} componentType"
            values = glb.attribute(p, semantic)
            if prop["type"] == "ENUM":
                allowed = {v["value"] for v in schema["enums"][prop["enumType"]]["values"]}
                assert set(np.unique(values).tolist()) <= allowed, f"{semantic}: bad enum value"
            else:
                _check_range(semantic, prop, values)
    for t in metadata.get("propertyTextures", ()):
        texture = glb.metadata["propertyTextures"][t]
        cls = schema["classes"][texture["class"]]
        for pid, entry in texture["properties"].items():
            prop = cls["properties"][pid]
            kind = _value_type(schema, prop)
            size = np.dtype(METADATA_TYPES[kind]).itemsize
            assert len(entry["channels"]) == size, f"{pid}: channels for {kind}"
            assert f"TEXCOORD_{entry.get('texCoord', 0)}" in prim["attributes"], pid
            gltf_texture = doc["textures"][entry["index"]]
            sampler = doc["samplers"][gltf_texture["sampler"]]
            assert sampler.get("magFilter", 9728) == 9728, f"{pid}: property texture filter"
            assert sampler.get("minFilter", 9728) == 9728, f"{pid}: property texture filter"
            image = doc["images"][gltf_texture["source"]]
            assert image["mimeType"] == "image/png", f"{pid}: property texture not PNG"


def _check_xmp(doc: dict) -> None:
    packets = doc["extensions"][XMP]["packets"]
    assert packets, "no XMP packets"
    for packet in packets:
        assert isinstance(packet.get("@context"), dict), "@context must be a dictionary"
        for key in packet:
            if key.startswith("@"):
                continue
            prefix = key.partition(":")[0]
            assert prefix in packet["@context"], f"{key}: prefix not in @context"
    reference = doc["asset"]["extensions"][XMP]["packet"]
    assert 0 <= reference < len(packets)


def check(data: bytes | Path) -> Glb:
    """Validate a metadata GLB's structure; returns it for decoding."""
    glb = read(data, metadata=True)
    doc = glb.doc
    assert doc["asset"]["version"] == "2.0"
    _check_accessors(glb, align=8)
    used = _check_extensions_used(doc)
    if EXT_SM in used:
        _check_schema(glb.schema)
        _check_tables(glb)
    for p, prim in enumerate(glb.primitives):
        _check_primitive(glb, p, prim)
    if XMP in used:
        _check_xmp(doc)
    for node in doc["nodes"]:
        if "matrix" in node:
            assert len(node["matrix"]) == 16
    return glb
