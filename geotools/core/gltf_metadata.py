"""The metadata GLB (M4): object identity, layers and georeference as glTF extensions.

bpy-free. The Blender side extracts a ``SceneExport`` whose primitives carry
the feature ID and layer vertex attributes (``blender.export``), reads the
texture layers and the placement, and calls :func:`build_extension`; the
writer (``core.gltf_writer.write_glb(..., extension=...)``) then lays the
result out. See docs/design/m4-glb-metadata.md for the file layout.

Extensions written (all optional, so in ``extensionsUsed`` only):

* ``EXT_mesh_features``: feature ID set ``object`` (``_FEATURE_ID_0``, one id
  per Blender object, property table 0) and, with the face table option,
  ``face`` (``_FEATURE_ID_1``, one id per exported face, property table 1).
* ``EXT_structural_metadata``: the schema (``core.metadata_schema``), property
  tables (``object``, ``face``), one property attribute (class ``vertex``: the
  FACE and CORNER layers as vertex attributes) and one property texture per
  textured primitive (class ``texel``: the TEXTURE layers).
* ``KHR_xmp_json_ld``: one packet on ``asset`` with the georeference
  (:func:`xmp_packet`).

Texture layers are float32 values packed into lossless RGBA PNGs
(:func:`pack_texture`): a FLOAT32 or INT32 value is its four little-endian
bytes in R, G, B, A (``channels [0, 1, 2, 3]``); a UINT8 (boolean) or an 8-bit
enum uses R alone, a 16-bit enum R and G. No offset/scale quantisation: values
round-trip bit for bit, NaN included.
"""

from __future__ import annotations

import math
import struct
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np

from .gltf_writer import ExportError, GlbLayout, SceneExport
from .layers import DataType, LayerSpec, fill_value
from .metadata_schema import (
    FLOAT32_EXACT_INT,
    OBJECT_CLASS,
    PropertyType,
    Storage,
    attribute_semantic,
    build_schema,
    layer_class,
    object_class,
    property_id,
    property_type,
)

__all__ = [
    "DC_NAMESPACE",
    "EXT_MESH_FEATURES",
    "EXT_STRUCTURAL_METADATA",
    "FACE_CLASS",
    "FACE_FEATURE_ID",
    "GEOTOOLS_NAMESPACE",
    "KHR_XMP_JSON_LD",
    "OBJECT_FEATURE_ID",
    "TEXEL_CLASS",
    "VERTEX_CLASS",
    "XMP_CONTEXT",
    "Georeference",
    "MetadataExtension",
    "PackedTexture",
    "build_extension",
    "decode_png",
    "encode_png",
    "face_class",
    "layer_attribute_values",
    "pack_strings",
    "pack_texture",
    "pack_values",
    "unpack_texture",
    "xmp_packet",
]

EXT_MESH_FEATURES = "EXT_mesh_features"
EXT_STRUCTURAL_METADATA = "EXT_structural_metadata"
KHR_XMP_JSON_LD = "KHR_xmp_json_ld"

OBJECT_FEATURE_ID = "_FEATURE_ID_0"
FACE_FEATURE_ID = "_FEATURE_ID_1"
FACE_CLASS = "face"
VERTEX_CLASS = "vertex"
TEXEL_CLASS = "texel"

GEOTOOLS_NAMESPACE = (
    "https://github.com/BenDyson-Arch/blender_geotools/blob/main/docs/design/m4-glb-metadata.md#"
)
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"
XMP_CONTEXT = {"dc": DC_NAMESPACE, "geotools": GEOTOOLS_NAMESPACE}

# Property textures hold raw bytes: never filter between texels.
PROPERTY_TEXTURE_SAMPLER = {"magFilter": 9728, "minFilter": 9728, "wrapS": 10497, "wrapT": 10497}

_UINT32_MAX = 2**32 - 1


# --- binary packing ----------------------------------------------------------------------


def pack_values(values: np.ndarray, ptype: PropertyType) -> bytes:
    """Property table values, tightly packed little-endian (BOOLEAN as LSB-first bits)."""
    values = np.asarray(values)
    if ptype.value_type == "BOOLEAN":
        return np.packbits(values.astype(bool), bitorder="little").tobytes()
    return _cast(values, ptype).tobytes()


def _cast(values: np.ndarray, ptype: PropertyType) -> np.ndarray:
    """``values`` as the property's binary type; integers are rounded and range checked."""
    dtype = np.dtype(ptype.numpy_dtype)
    if dtype.kind == "f":
        return np.ascontiguousarray(values, dtype=dtype)
    if values.dtype.kind == "f":
        if not np.isfinite(values).all():
            raise ExportError("Integer layer values contain NaN or infinity.")
        values = np.rint(values)
    info = np.iinfo(dtype)
    if values.size and (values.min() < info.min or values.max() > info.max):
        raise ExportError(
            f"Values {values.min()}..{values.max()} do not fit {ptype.value_type} "
            f"({info.min}..{info.max})."
        )
    return np.ascontiguousarray(values, dtype=dtype)


def pack_strings(strings: Sequence[str]) -> tuple[bytes, bytes]:
    """UTF-8 string values and their UINT32 offsets (``len(strings) + 1`` of them)."""
    encoded = [s.encode("utf-8") for s in strings]
    offsets = np.zeros(len(encoded) + 1, dtype="<u4")
    if encoded:
        lengths = np.fromiter((len(b) for b in encoded), dtype=np.int64, count=len(encoded))
        total = int(lengths.sum())
        if total > _UINT32_MAX:
            raise ExportError("The object names are over 4 GiB of text.")
        offsets[1:] = np.cumsum(lengths)
    return b"".join(encoded), offsets.tobytes()


def layer_attribute_values(values: np.ndarray, spec: LayerSpec) -> np.ndarray:
    """A layer's values (any dtype) as its vertex attribute array (``Storage.ATTRIBUTE``).

    INT layers become float32 (checked to be exact), BOOLEAN layers uint8 and
    CATEGORY layers their enum value type.
    """
    ptype = property_type(spec, Storage.ATTRIBUTE)
    values = np.asarray(values)
    if DataType(spec.data_type) is DataType.BOOLEAN:
        return values.astype(np.uint8)
    if ptype.component_type == "FLOAT32":
        integers = values.dtype.kind in "iu" and values.size
        if integers and int(np.abs(values.astype(np.int64)).max()) > FLOAT32_EXACT_INT:
            raise ExportError(
                f"Layer {spec.name!r} holds integers beyond +-2^24, which a FLOAT32 vertex "
                "attribute cannot hold exactly; use the face table option."
            )
        return np.ascontiguousarray(values, dtype=np.float32)
    try:
        return _cast(values, ptype)
    except ExportError as ex:
        raise ExportError(f"Layer {spec.name!r}: {ex}") from None


# --- textures and PNG --------------------------------------------------------------------


def _channels(ptype: PropertyType) -> list[int]:
    return list(range(ptype.size))


def pack_texture(values: np.ndarray, spec: LayerSpec) -> tuple[np.ndarray, list[int]]:
    """A texture layer's values as an RGBA8 image for a property texture.

    ``values`` is ``(H, W)``, row 0 at the BOTTOM (Blender's pixel order). The
    result is ``(H, W, 4) uint8`` with row 0 at the TOP (PNG and glTF order, so
    glTF ``v = 1 - v_blender`` samples the same texel), and the channels holding
    the value's little-endian bytes. Unused channels are 0, and alpha 255 when
    unused. NaN in an integer layer becomes its no-data (fill) value.
    """
    ptype = property_type(spec, Storage.TEXTURE)
    values = np.asarray(values)
    if values.ndim != 2:
        raise ExportError(f"Layer {spec.name!r}: texture values must be (H, W).")
    data_type = DataType(spec.data_type)
    if data_type is DataType.BOOLEAN:
        typed = (np.nan_to_num(values, nan=0.0) != 0).astype(np.uint8)
    elif ptype.value_type == "FLOAT32":
        typed = np.ascontiguousarray(values, dtype="<f4")
    else:
        floats = values.astype(np.float64)
        floats = np.where(np.isfinite(floats), floats, float(fill_value(spec)))
        typed = _cast(floats, ptype)
    flipped = np.ascontiguousarray(typed[::-1])
    raw = flipped.view(np.uint8).reshape(values.shape[0], values.shape[1], ptype.size)
    rgba = np.zeros((values.shape[0], values.shape[1], 4), dtype=np.uint8)
    rgba[..., : ptype.size] = raw
    if ptype.size < 4:
        rgba[..., 3] = 255
    return rgba, _channels(ptype)


def unpack_texture(rgba: np.ndarray, channels: Sequence[int], spec: LayerSpec) -> np.ndarray:
    """The values :func:`pack_texture` packed, ``(H, W)`` with row 0 at the bottom."""
    ptype = property_type(spec, Storage.TEXTURE)
    raw = np.ascontiguousarray(rgba[..., list(channels)])
    values = raw.view(np.dtype(ptype.numpy_dtype)).reshape(rgba.shape[0], rgba.shape[1])
    return values[::-1]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))


def encode_png(rgba: np.ndarray, level: int = 6) -> bytes:
    """A lossless 8-bit RGBA PNG of ``(H, W, 4) uint8`` (row 0 at the top).

    Deterministic: filter "None" on every row, zlib at ``level``."""
    rgba = np.ascontiguousarray(rgba, dtype=np.uint8)
    if rgba.ndim != 3 or rgba.shape[2] != 4 or 0 in rgba.shape:
        raise ValueError(f"expected a non-empty (H, W, 4) array, got {rgba.shape}")
    height, width = rgba.shape[:2]
    rows = np.zeros((height, 1 + 4 * width), dtype=np.uint8)
    rows[:, 1:] = rgba.reshape(height, 4 * width)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"IDAT", zlib.compress(rows.tobytes(), level)),
            _png_chunk(b"IEND", b""),
        )
    )


def decode_png(data: bytes) -> np.ndarray:
    """A PNG written by :func:`encode_png` (8-bit RGB or RGBA, filter "None" on every
    row, no interlace) as ``(H, W, 4) uint8``. For tests and round trips; not a
    general decoder: other filters raise ``ValueError``."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat = 8, []
    width = height = color = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, color, _c, _f, interlace = struct.unpack(">IIBBBBB", body)
            if depth != 8 or color not in (2, 6) or interlace:
                raise ValueError("only 8-bit RGB/RGBA PNGs without interlace")
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    bpp = 4 if color == 6 else 3
    raw = np.frombuffer(zlib.decompress(b"".join(idat)), dtype=np.uint8)
    raw = raw.reshape(height, 1 + bpp * width)
    if raw[:, 0].any():
        raise ValueError("only PNGs with filter None on every row")
    pixels = raw[:, 1:].reshape(height, width, bpp)
    if bpp == 4:
        return pixels.copy()
    rgba = np.full((height, width, 4), 255, dtype=np.uint8)
    rgba[..., :3] = pixels
    return rgba


# --- georeference -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Georeference:
    """What the XMP packet records about the placement (all binary64)."""

    mode: str  # "local", "anchor" or "source_crs"
    geotools_version: str = ""
    crs_epsg: str | None = None
    crs_definition: str | None = None
    source_offset: tuple[float, float, float] | None = None  # E, N, A
    height_offset: float | None = None
    anchor: tuple[float, float, float] | None = None  # lon, lat, height
    heading_degrees: float | None = None  # anchor heading, or the measured one
    scene_offset: tuple[float, float, float] | None = None  # E, N, U
    origin_lon_lat_height: tuple[float, float, float] | None = None
    origin_model: tuple[float, float, float] | None = None  # glTF (Y-up) model coordinates
    model_to_ecef: tuple[float, ...] | None = None  # glTF node matrix, column-major
    ground_metres_per_model_metre: float | None = None
    """Metres on the ground (the ellipsoid at the origin's height) per model metre along
    +X: about 1 / the combined scale factor. Source CRS only."""
    crs_operation: str | None = None
    earth_placed: bool = False
    source: str = ""  # e.g. the .blend file name
    probe_error: str | None = None  # why the origin is missing


def _list(values) -> dict[str, object]:
    return {"@list": [float(v) for v in values]}


def xmp_packet(geo: Georeference) -> dict[str, object]:
    """The ``KHR_xmp_json_ld`` packet describing ``geo``.

    Keys are ``dc:`` terms and ``geotools:`` terms (namespace
    :data:`GEOTOOLS_NAMESPACE`, documented in docs/design/m4-glb-metadata.md).
    Numbers are JSON numbers (binary64, written exactly); arrays are ``@list``.
    """
    packet: dict[str, object] = {"@context": dict(XMP_CONTEXT), "@id": ""}
    packet["dc:format"] = "model/gltf-binary"
    if geo.source:
        packet["dc:source"] = geo.source
    packet["dc:description"] = (
        "Positions are local metres, glTF Y-up; geotools: terms say where they are on Earth."
        if not geo.earth_placed
        else "Positions are local metres, glTF Y-up; the root node places them in ECEF."
    )
    if geo.geotools_version:
        packet["geotools:version"] = geo.geotools_version
    packet["geotools:placement"] = geo.mode
    packet["geotools:axes"] = "y-up"
    packet["geotools:units"] = "metre"
    if geo.crs_epsg:
        packet["geotools:crs"] = geo.crs_epsg
    if geo.crs_definition:
        packet["geotools:crsDefinition"] = geo.crs_definition
    if geo.source_offset is not None:
        packet["geotools:sourceOffset"] = _list(geo.source_offset)
    if geo.height_offset is not None:
        packet["geotools:heightOffset"] = float(geo.height_offset)
    if geo.anchor is not None:
        packet["geotools:anchor"] = _list(geo.anchor)
    if geo.scene_offset is not None:
        packet["geotools:sceneOffset"] = _list(geo.scene_offset)
    if geo.origin_lon_lat_height is not None:
        lon, lat, height = geo.origin_lon_lat_height
        packet["geotools:originLongitude"] = float(lon)
        packet["geotools:originLatitude"] = float(lat)
        packet["geotools:originHeight"] = float(height)
    if geo.origin_model is not None:
        packet["geotools:originModel"] = _list(geo.origin_model)
    if geo.heading_degrees is not None:
        packet["geotools:heading"] = float(geo.heading_degrees)
    if geo.ground_metres_per_model_metre is not None:
        packet["geotools:groundMetresPerModelMetre"] = float(geo.ground_metres_per_model_metre)
    if geo.crs_operation:
        packet["geotools:crsOperation"] = geo.crs_operation
    if geo.model_to_ecef is not None:
        packet["geotools:modelToEcef"] = _list(geo.model_to_ecef)
    if geo.probe_error:
        packet["geotools:originUnavailable"] = geo.probe_error
    packet["geotools:earthPlaced"] = bool(geo.earth_placed)
    for key, value in packet.items():
        if isinstance(value, float) and not math.isfinite(value):
            raise ExportError(f"The georeference {key} is not finite.")
    return packet


# --- the extension -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackedTexture:
    """One texture layer of one object, packed (:func:`pack_texture`, :func:`encode_png`)."""

    layer: str
    name: str  # image name in the glTF
    png: bytes
    channels: tuple[int, ...]
    texcoord: int


@dataclass(frozen=True, slots=True)
class _Column:
    ptype: PropertyType | None  # None: STRING
    data: bytes
    string_offsets: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Table:
    class_id: str
    count: int
    columns: dict[str, _Column]


@dataclass(frozen=True, slots=True, eq=False)
class MetadataExtension:
    """Everything :func:`build_extension` worked out; the writer calls :meth:`apply`."""

    schema: dict
    tables: tuple[_Table, ...]
    feature_ids: tuple[tuple[dict, ...], ...]  # per primitive
    property_attribute: dict | None
    textures: tuple[tuple[PackedTexture, ...], ...]  # per primitive
    packet: dict | None
    root_matrix: tuple[float, ...] | None
    summary: dict = field(default_factory=dict)

    @property
    def extensions_used(self) -> list[str]:
        used = []
        if any(self.feature_ids):
            used.append(EXT_MESH_FEATURES)
        used.append(EXT_STRUCTURAL_METADATA)
        if self.packet is not None:
            used.append(KHR_XMP_JSON_LD)
        return used

    def apply(self, layout: GlbLayout) -> None:
        for name in self.extensions_used:
            layout.use(name)
        tables = []
        for table in self.tables:
            properties: dict[str, object] = {}
            for pid, column in table.columns.items():
                entry: dict[str, object] = {
                    "values": layout.add_view(f"table {table.class_id}.{pid}", column.data)
                }
                if column.string_offsets is not None:
                    entry["stringOffsets"] = layout.add_view(
                        f"table {table.class_id}.{pid} offsets", column.string_offsets
                    )
                    entry["stringOffsetType"] = "UINT32"
                properties[pid] = entry
            tables.append({"class": table.class_id, "count": table.count, "properties": properties})

        property_textures = []
        for p, (primitive, textures) in enumerate(
            zip(layout.primitives, self.textures, strict=True)
        ):
            extensions: dict[str, object] = {}
            if self.feature_ids[p]:
                extensions[EXT_MESH_FEATURES] = {
                    "featureIds": [dict(f) for f in self.feature_ids[p]]
                }
            metadata: dict[str, object] = {}
            if textures:
                properties = {}
                for texture in textures:
                    image = layout.add_image(texture.name, texture.png)
                    index = layout.add_texture(image, PROPERTY_TEXTURE_SAMPLER)
                    properties[texture.layer] = {
                        "index": index,
                        "texCoord": texture.texcoord,
                        "channels": list(texture.channels),
                    }
                property_textures.append({"class": TEXEL_CLASS, "properties": properties})
                metadata["propertyTextures"] = [len(property_textures) - 1]
            if self.property_attribute is not None:
                metadata["propertyAttributes"] = [0]
            if metadata:
                extensions[EXT_STRUCTURAL_METADATA] = metadata
            if extensions:
                primitive["extensions"] = extensions

        root: dict[str, object] = {"schema": self.schema}
        if tables:
            root["propertyTables"] = tables
        if property_textures:
            root["propertyTextures"] = property_textures
        if self.property_attribute is not None:
            root["propertyAttributes"] = [self.property_attribute]
        document = layout.document
        top: dict[str, object] = {EXT_STRUCTURAL_METADATA: root}
        if self.packet is not None:
            top[KHR_XMP_JSON_LD] = {"packets": [self.packet]}
            document["asset"]["extensions"] = {KHR_XMP_JSON_LD: {"packet": 0}}
        document["extensions"] = top
        if self.root_matrix is not None:
            document["nodes"] = [
                {"name": "GeoTools ECEF", "matrix": list(self.root_matrix), "children": [1]},
                {"name": "GeoTools source", "mesh": 0},
            ]


def face_class(specs: Sequence[LayerSpec]) -> dict[str, object]:
    """The ``face`` class: the object row and Blender face index, then the FACE layers."""
    cls = layer_class(
        specs,
        Storage.TABLE,
        name="Face",
        description="One face of an exported mesh (after its modifiers, as published).",
    )
    properties = {
        "object": {
            "name": "Object",
            "description": "Row of the object property table.",
            "type": "SCALAR",
            "componentType": "UINT32",
            "required": True,
        },
        "polygon": {
            "name": "Blender face",
            "description": "Index of the face in the exported (evaluated) Blender mesh.",
            "type": "SCALAR",
            "componentType": "UINT32",
            "required": True,
        },
    }
    for key in properties:
        if key in cls["properties"]:
            raise ExportError(
                f"A FACE layer is named {key!r}, which the face table uses itself; rename it."
            )
    properties.update(cls["properties"])  # type: ignore[arg-type]
    cls["properties"] = properties
    return cls


def _feature_count(values: np.ndarray) -> int:
    return max(int(np.unique(values).size), 1)


def _attribute(primitive, name: str, p: int) -> np.ndarray:
    for key, values in primitive.custom_attributes:
        if key == name:
            return values
    raise ExportError(f"Primitive {p} has no {name} attribute.")


def build_extension(
    scene: SceneExport,
    *,
    object_ids: Sequence[str],
    vertex_layers: Sequence[LayerSpec] = (),
    face_layers: Sequence[LayerSpec] = (),
    face_values: Mapping[str, Sequence[np.ndarray | None]] | None = None,
    face_counts: Sequence[int] | None = None,
    texture_layers: Sequence[LayerSpec] = (),
    textures: Sequence[Sequence[PackedTexture]] | None = None,
    georeference: Georeference | None = None,
    root_matrix: tuple[float, ...] | None = None,
    version: str | None = None,
) -> MetadataExtension:
    """The extension for ``scene``.

    Every primitive must carry ``_FEATURE_ID_0`` (the object row), the
    ``attribute_semantic`` of each of ``vertex_layers`` and, when
    ``face_layers`` or ``face_counts`` are given (the face table), also
    ``_FEATURE_ID_1``: the face's row, objects in ``object_names`` order and
    ``face_counts[i]`` faces each. ``face_values[layer][i]`` holds object
    ``i``'s values, one per face (None: the layer's fill value).
    ``textures[p]`` are primitive ``p``'s packed texture layers.
    """
    names = scene.object_names
    if len(object_ids) != len(names):
        raise ExportError("One object id per object name is needed.")
    if len(set(object_ids)) != len(object_ids):
        raise ExportError("Object ids must be unique.")
    face_table = face_counts is not None
    primitives = scene.primitives

    classes: dict[str, dict] = {OBJECT_CLASS: object_class()}
    name_data, name_offsets = pack_strings(list(names))
    id_data, id_offsets = pack_strings(list(object_ids))
    tables = [
        _Table(
            OBJECT_CLASS,
            len(names),
            {
                "name": _Column(None, name_data, name_offsets),
                "object_id": _Column(None, id_data, id_offsets),
            },
        )
    ]

    if face_table:
        counts = [int(c) for c in face_counts]  # type: ignore[union-attr]
        if len(counts) != len(names):
            raise ExportError("One face count per object is needed.")
        total = sum(counts)
        if total > FLOAT32_EXACT_INT:
            raise ExportError(
                f"The face table would have {total:,} rows; feature IDs are FLOAT32 vertex "
                f"attributes, exact up to {FLOAT32_EXACT_INT:,}. Turn the face table off."
            )
        if total == 0:
            raise ExportError("The face table has no faces.")
        classes[FACE_CLASS] = face_class(face_layers)
        uint32 = PropertyType("SCALAR", "UINT32", None, "UINT32")
        columns = {
            "object": _Column(
                uint32, np.repeat(np.arange(len(counts), dtype="<u4"), counts).tobytes()
            ),
            "polygon": _Column(
                uint32,
                np.concatenate([np.arange(c, dtype="<u4") for c in counts] or [np.zeros(0, "<u4")])
                .astype("<u4")
                .tobytes(),
            ),
        }
        for spec in face_layers:
            ptype = property_type(spec, Storage.TABLE)
            per_object = (face_values or {}).get(spec.name, [None] * len(counts))
            parts = []
            for i, count in enumerate(counts):
                values = per_object[i] if i < len(per_object) else None
                if values is None:
                    values = np.full(count, fill_value(spec))
                elif len(values) != count:
                    raise ExportError(
                        f"Layer {spec.name!r} has {len(values)} values for {names[i]!r}, "
                        f"which has {count} faces."
                    )
                parts.append(np.asarray(values))
            column = np.concatenate(parts) if parts else np.zeros(0)
            try:
                columns[property_id(spec)] = _Column(ptype, pack_values(column, ptype))
            except ExportError as ex:
                raise ExportError(f"Layer {spec.name!r}: {ex}") from None
        tables.append(_Table(FACE_CLASS, total, columns))

    property_attribute = None
    if vertex_layers:
        classes[VERTEX_CLASS] = layer_class(
            vertex_layers,
            Storage.ATTRIBUTE,
            name="Vertex",
            description="Face and corner layers, one value per vertex (a face's value "
            "repeats on its corners).",
        )
        property_attribute = {
            "class": VERTEX_CLASS,
            "properties": {
                property_id(s): {"attribute": attribute_semantic(s)} for s in vertex_layers
            },
        }
    if texture_layers:
        classes[TEXEL_CLASS] = layer_class(
            texture_layers,
            Storage.TEXTURE,
            name="Texel",
            description="Texture layers, one value per texel of each object's layer image.",
        )

    textures = list(textures) if textures is not None else [()] * len(primitives)
    if len(textures) != len(primitives):
        raise ExportError("One texture list per primitive is needed.")
    feature_ids = []
    for p, prim in enumerate(primitives):
        sets = [
            {
                "featureCount": _feature_count(_attribute(prim, OBJECT_FEATURE_ID, p)),
                "attribute": 0,
                "propertyTable": 0,
                "label": "object",
            }
        ]
        if face_table:
            sets.append(
                {
                    "featureCount": _feature_count(_attribute(prim, FACE_FEATURE_ID, p)),
                    "attribute": 1,
                    "propertyTable": 1,
                    "label": "face",
                }
            )
        for spec in vertex_layers:
            _attribute(prim, attribute_semantic(spec), p)
        feature_ids.append(tuple(sets))

    all_layers = [*vertex_layers, *face_layers, *texture_layers]
    schema = build_schema(classes, all_layers, version=version)
    packet = xmp_packet(georeference) if georeference is not None else None
    summary = {
        "extensions_used": [],
        "objects": len(names),
        "vertex_layers": [s.name for s in vertex_layers],
        "face_layers": [s.name for s in face_layers],
        "texture_layers": [s.name for s in texture_layers],
        "face_table_rows": sum(face_counts) if face_table else None,  # type: ignore[arg-type]
        "property_textures": sum(1 for t in textures if t),
    }
    extension = MetadataExtension(
        schema=schema,
        tables=tuple(tables),
        feature_ids=tuple(feature_ids),
        property_attribute=property_attribute,
        textures=tuple(tuple(t) for t in textures),
        packet=packet,
        root_matrix=root_matrix,
        summary=summary,
    )
    summary["extensions_used"] = extension.extensions_used
    return extension
