"""An ``EXT_structural_metadata`` schema from GeoTools layers (M4; reused by M9).

bpy-free and independent of any GLB writer: it turns
:class:`core.layers.LayerSpec` descriptions into the JSON of a 3D Metadata
schema (the ``schema`` object of ``EXT_structural_metadata``, the same format
3D Tiles 1.1 uses). See docs/design/m4-glb-metadata.md.

Where the values live decides the types, so every class is built for one
:class:`Storage`:

=========  ===========  ======================  ====================  =================
layer      TABLE        ATTRIBUTE               TEXTURE               notes
=========  ===========  ======================  ====================  =================
FLOAT      FLOAT32      FLOAT32                 FLOAT32 (4 channels)
INT        INT32        FLOAT32 (exact <= 2^24) INT32 (4 channels)    glTF vertex attributes
                                                                      have no 32-bit integer
BOOLEAN    BOOLEAN      UINT8 (0 or 1)          UINT8 (1 channel)     attributes and textures
                                                                      cannot hold BOOLEAN
CATEGORY   ENUM         ENUM                    ENUM                  enum value type: the
                                                                      smallest that fits
=========  ===========  ======================  ====================  =================

Ids are stable and derived from layer names (``[a-z][a-z0-9_]*`` already
matches the schema id rule): the property id is the layer name, the enum id
``<name>_category``, and the vertex attribute ``_<NAME>``.

Every property carries ``name`` (the layer's display name) and ``description``
(its description, units and what it is), ``noData`` (the layer's no-data,
except for BOOLEAN), and ``min``/``max`` from the layer's ``value_min`` and
``value_max``. CATEGORY layers become schema ``enums`` whose value names are the
category labels; a no-data value becomes one more enum value, named
``"no data"``, which the property's ``noData`` names (enum no-data is a name).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .layers import DataType, Domain, LayerSpec, display_name

__all__ = [
    "FLOAT32_EXACT_INT",
    "NODATA_ENUM_NAME",
    "OBJECT_CLASS",
    "SCHEMA_ID",
    "PropertyType",
    "SchemaError",
    "Storage",
    "attribute_semantic",
    "build_schema",
    "enum_definition",
    "enum_id",
    "enum_value_type",
    "layer_class",
    "object_class",
    "property_definition",
    "property_id",
    "property_type",
]

SCHEMA_ID = "geotools"
OBJECT_CLASS = "object"
NODATA_ENUM_NAME = "no data"
FLOAT32_EXACT_INT = 1 << 24
"""Integers up to this magnitude are exact in float32."""

_INT_RANGES = {
    "UINT8": (0, 255),
    "INT8": (-128, 127),
    "UINT16": (0, 65_535),
    "INT16": (-32_768, 32_767),
    "INT32": (-(2**31), 2**31 - 1),
}
_SIZES = {
    "INT8": 1,
    "UINT8": 1,
    "INT16": 2,
    "UINT16": 2,
    "INT32": 4,
    "UINT32": 4,
    "FLOAT32": 4,
}
_NUMPY = {
    "INT8": "<i1",
    "UINT8": "<u1",
    "INT16": "<i2",
    "UINT16": "<u2",
    "INT32": "<i4",
    "UINT32": "<u4",
    "FLOAT32": "<f4",
}


class Storage(StrEnum):
    """Where a class's values are stored, which limits their types."""

    TABLE = "TABLE"  # property tables: any type
    ATTRIBUTE = "ATTRIBUTE"  # property attributes: glTF vertex attribute types
    TEXTURE = "TEXTURE"  # property textures: 1 to 4 8-bit channels


class SchemaError(ValueError):
    """A layer cannot be represented in the requested storage."""


@dataclass(frozen=True, slots=True)
class PropertyType:
    """How one layer is typed in one storage."""

    type: str  # "SCALAR", "BOOLEAN" or "ENUM"
    component_type: str | None  # SCALAR: FLOAT32, INT32, UINT8, ...
    enum_type: str | None  # ENUM: the enum id
    value_type: str  # the binary component type (an ENUM's value type; "BOOLEAN" bits)

    @property
    def numpy_dtype(self) -> str:
        """The little-endian numpy dtype of one value (not for BOOLEAN)."""
        return _NUMPY[self.value_type]

    @property
    def size(self) -> int:
        """Bytes per value (BOOLEAN: 0, they are bits)."""
        return _SIZES.get(self.value_type, 0)


# --- ids ------------------------------------------------------------------------------


def property_id(spec: LayerSpec) -> str:
    """The property id of a layer: its name."""
    return spec.name


def enum_id(spec: LayerSpec) -> str:
    """The enum id of a CATEGORY layer: ``<name>_category``."""
    return f"{spec.name}_category"


def attribute_semantic(spec: LayerSpec) -> str:
    """The vertex attribute of a layer: ``_<NAME>`` (``_SLOPE`` for ``slope``).

    A name that would collide with an ``EXT_mesh_features`` feature ID attribute
    (``_FEATURE_ID_<n>``) gets ``_GT`` in front: ``_GT_FEATURE_ID_0``.
    """
    semantic = "_" + spec.name.upper()
    rest = semantic.removeprefix("_FEATURE_ID_")
    if rest != semantic and rest.isdigit():
        semantic = "_GT" + semantic
    return semantic


# --- types ------------------------------------------------------------------------------


def _nodata_int(spec: LayerSpec) -> int | None:
    if spec.nodata is None or not math.isfinite(float(spec.nodata)):
        return None
    return int(spec.nodata)


def enum_value_type(spec: LayerSpec) -> str:
    """The smallest enum value type holding the category values and the no-data."""
    values = [0, max(len(spec.categories) - 1, 0)]
    nodata = _nodata_int(spec)
    if nodata is not None:
        values.append(nodata)
    lo, hi = min(values), max(values)
    for name in ("UINT8", "INT8", "UINT16", "INT16", "INT32"):
        low, high = _INT_RANGES[name]
        if low <= lo and hi <= high:
            return name
    raise SchemaError(f"Layer {spec.name!r}: its no-data {nodata} does not fit a 32-bit integer.")


def property_type(spec: LayerSpec, storage: Storage) -> PropertyType:
    """The type of layer ``spec`` in ``storage`` (see the module docstring)."""
    data_type = DataType(spec.data_type)
    storage = Storage(storage)
    if data_type is DataType.CATEGORY:
        value_type = enum_value_type(spec)
        if storage is Storage.ATTRIBUTE and _SIZES[value_type] > 2:
            raise SchemaError(
                f"Layer {spec.name!r}: a vertex attribute holds enum values of at most 16 "
                f"bits, but its values need {value_type}."
            )
        return PropertyType("ENUM", None, enum_id(spec), value_type)
    if data_type is DataType.BOOLEAN:
        if storage is Storage.TABLE:
            return PropertyType("BOOLEAN", None, None, "BOOLEAN")
        return PropertyType("SCALAR", "UINT8", None, "UINT8")
    if data_type is DataType.FLOAT or storage is Storage.ATTRIBUTE:
        _check_float32_ints(spec, storage)
        return PropertyType("SCALAR", "FLOAT32", None, "FLOAT32")
    return PropertyType("SCALAR", "INT32", None, "INT32")


def _check_float32_ints(spec: LayerSpec, storage: Storage) -> None:
    """An INT layer stored as FLOAT32 must stay exact."""
    if DataType(spec.data_type) is not DataType.INT:
        return
    for value in (spec.value_min, spec.value_max, spec.nodata):
        finite = value is not None and math.isfinite(float(value))
        if finite and abs(float(value)) > FLOAT32_EXACT_INT:
            raise SchemaError(
                f"Layer {spec.name!r}: integer {value:g} is beyond +-2^24, which a "
                f"FLOAT32 {storage.value.lower()} cannot hold exactly (glTF vertex "
                "attributes have no 32-bit integer type)."
            )


# --- definitions ---------------------------------------------------------------------


def _description(spec: LayerSpec, ptype: PropertyType, storage: Storage) -> str:
    parts = []
    text = spec.description.strip()
    if text:
        parts.append(text if text.endswith((".", "!", "?")) else text + ".")
    units = spec.units.strip()
    if units:
        parts.append(f"Units: {units}.")
    domain = Domain(spec.domain).value.lower()
    data_type = DataType(spec.data_type)
    parts.append(f"GeoTools layer {spec.name} ({domain} {data_type.value.lower()}).")
    if data_type is DataType.BOOLEAN and ptype.type != "BOOLEAN":
        parts.append("1 is true, 0 is false.")
    if data_type is DataType.INT and ptype.component_type == "FLOAT32":
        parts.append("Whole numbers stored as FLOAT32: glTF vertex attributes have no 32-bit int.")
    return " ".join(parts)


def property_definition(spec: LayerSpec, storage: Storage) -> dict[str, object]:
    """The class property of layer ``spec`` in ``storage``.

    Keys in a fixed order: ``name``, ``description``, ``type``,
    ``componentType`` or ``enumType``, ``noData``, ``min``, ``max``.
    """
    storage = Storage(storage)
    ptype = property_type(spec, storage)
    out: dict[str, object] = {
        "name": display_name(spec),
        "description": _description(spec, ptype, storage),
        "type": ptype.type,
    }
    if ptype.component_type is not None:
        out["componentType"] = ptype.component_type
    if ptype.enum_type is not None:
        out["enumType"] = ptype.enum_type
    data_type = DataType(spec.data_type)
    nodata = spec.nodata if spec.nodata is not None and math.isfinite(float(spec.nodata)) else None
    if nodata is not None and data_type is not DataType.BOOLEAN:
        out["noData"] = NODATA_ENUM_NAME if ptype.type == "ENUM" else _as_type(nodata, ptype)
    if ptype.type == "SCALAR":
        for key, value in (("min", spec.value_min), ("max", spec.value_max)):
            if value is not None and math.isfinite(float(value)):
                out[key] = _as_type(value, ptype)
    return out


def _as_type(value: float, ptype: PropertyType) -> float | int:
    if ptype.component_type == "FLOAT32":
        return float(value)
    return int(value)


def _unique_names(labels: Sequence[str]) -> list[str]:
    """Labels made unique (enum value names must be): a repeat gets " (value i)"."""
    seen: set[str] = set()
    names = []
    for i, label in enumerate(labels):
        name = label.strip() or f"value {i}"
        if name in seen or name == NODATA_ENUM_NAME:
            name = f"{name} (value {i})"
        seen.add(name)
        names.append(name)
    return names


def enum_definition(spec: LayerSpec) -> dict[str, object]:
    """The enum of a CATEGORY layer: value i is named by category label i.

    A no-data value adds a ``"no data"`` value, which the property's ``noData``
    names.
    """
    if DataType(spec.data_type) is not DataType.CATEGORY:
        raise SchemaError(f"Layer {spec.name!r} is not a CATEGORY layer.")
    values: list[dict[str, object]] = [
        {"name": name, "value": i} for i, name in enumerate(_unique_names(spec.categories))
    ]
    nodata = _nodata_int(spec)
    if nodata is not None:
        values.append(
            {"name": NODATA_ENUM_NAME, "value": nodata, "description": "No data at this place."}
        )
    return {
        "name": display_name(spec),
        "description": f"Categories of GeoTools layer {spec.name}.",
        "valueType": enum_value_type(spec),
        "values": values,
    }


def layer_class(
    specs: Iterable[LayerSpec],
    storage: Storage,
    *,
    name: str,
    description: str,
) -> dict[str, object]:
    """A class whose properties are ``specs`` (in order) stored in ``storage``."""
    properties: dict[str, object] = {}
    for spec in specs:
        pid = property_id(spec)
        if pid in properties:
            raise SchemaError(f"Two layers are named {pid!r}.")
        properties[pid] = property_definition(spec, storage)
    return {"name": name, "description": description, "properties": properties}


def object_class() -> dict[str, object]:
    """The class of the per-object property table: name and persistent id."""
    return {
        "name": "Object",
        "description": "A Blender object of the GeoTools source collection.",
        "properties": {
            "name": {
                "name": "Name",
                "description": "The Blender object name when the file was written.",
                "type": "STRING",
                "required": True,
            },
            "object_id": {
                "name": "Object id",
                "description": "A persistent UUID stored on the Blender object; it survives "
                "renames and saves of the .blend.",
                "type": "STRING",
                "required": True,
            },
        },
    }


def build_schema(
    classes: Mapping[str, Mapping[str, object]],
    layers: Iterable[LayerSpec] = (),
    *,
    name: str = "GeoTools",
    description: str = "Objects and layers published by GeoTools.",
    version: str | None = None,
    schema_id: str = SCHEMA_ID,
) -> dict[str, object]:
    """The schema JSON: ``classes`` as given, and an enum for each CATEGORY layer in
    ``layers`` (in order). Empty ``enums`` is left out."""
    out: dict[str, object] = {"id": schema_id, "name": name, "description": description}
    if version:
        out["version"] = version
    out["classes"] = {cid: dict(c) for cid, c in classes.items()}
    enums: dict[str, object] = {}
    for spec in layers:
        if DataType(spec.data_type) is DataType.CATEGORY:
            enums[enum_id(spec)] = enum_definition(spec)
    if enums:
        out["enums"] = enums
    return out
