"""
attributes.py — Feature attribute I/O via Blender mesh attributes.

Mesh attributes (POINT domain) are the single source of truth for feature
data.  They are natively accessible in Geometry Nodes, the Spreadsheet
editor, and material shaders via ``Named Attribute``.

Object custom properties are used **only** for provenance metadata
(geo_source, geo_fid, …).
"""

import json


# ---------------------------------------------------------------------------
# Supported Blender attribute types
# ---------------------------------------------------------------------------
#   Python type      →  Blender attr type / read accessor / write accessor
#
#   int              →  INT           .value
#   float            →  FLOAT         .value
#   bool             →  BOOLEAN       .value  (Blender ≥ 4.0)
#   str              →  STRING        .value  (Blender ≥ 4.1)
#   bytes            →  STRING        .value  (hex-encoded)
#   list[number,2-4] →  FLOAT_VECTOR  .vector
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def write_mesh_attributes(mesh, properties: dict, domain: str = "POINT"):
    """Write feature *properties* as mesh attributes on *mesh*.

    Only types that map to a native Blender attribute type are written.
    Strings and JSON-serializable container values are stored as STRING
    attributes. Lists that are not short numeric vectors are JSON-encoded.

    Returns the set of property keys that were written as mesh attributes.
    """
    written = set()
    for key, value in properties.items():
        if value is None:
            continue

        if isinstance(value, bool):
            _write_uniform(mesh, key, value, "BOOLEAN", domain)
            written.add(key)
        elif isinstance(value, int):
            _write_uniform(mesh, key, value, "INT", domain)
            written.add(key)
        elif isinstance(value, float):
            _write_uniform(mesh, key, value, "FLOAT", domain)
            written.add(key)
        elif isinstance(value, str):
            _write_uniform(mesh, key, value, "STRING", domain)
            written.add(key)
        elif isinstance(value, bytes):
            _write_uniform(mesh, key, value.hex(), "STRING", domain)
            written.add(key)
        elif (isinstance(value, list)
              and 2 <= len(value) <= 4
              and all(isinstance(v, (int, float)) for v in value)):
            _write_vector(mesh, key, value, domain)
            written.add(key)
        elif isinstance(value, (dict, list)):
            _write_uniform(mesh, key, json.dumps(value), "STRING", domain)
            written.add(key)

    return written


def write_non_mesh_properties(obj, properties: dict, skip: set | None = None):
    """Write properties not yet handled as mesh attributes as custom props.

    With STRING mesh attribute support, virtually all types are now mesh
    attributes, so this only catches edge cases (None values, etc.).
    Keys in *skip* (those already written as mesh attributes) are excluded.
    """
    skip = skip or set()
    for key, value in properties.items():
        if key in skip or value is None:
            continue
        # Anything not already a mesh attribute → custom prop
        if isinstance(value, bytes):
            obj[key] = value.hex()
        elif isinstance(value, (dict, list)):
            obj[key] = json.dumps(value)
        elif isinstance(value, str):
            obj[key] = value
        else:
            obj[key] = value


# ---------------------------------------------------------------------------
# Read helpers (for write-back)
# ---------------------------------------------------------------------------

def read_mesh_attributes(mesh) -> dict:
    """Read all user mesh attributes from *mesh*, returning ``{name: value}``.

    Since feature attributes are uniform (same value for all elements),
    we read just the first element.  Built-in Blender attributes (names
    starting with ``'.'``) and internal attributes are skipped.
    """
    result = {}
    for attr in mesh.attributes:
        name = attr.name
        # Skip Blender internal attributes
        if name.startswith(".") or name in ("position", "shade_smooth",
                                            "sharp_face", "sharp_edge",
                                            "crease_edge", "crease_vert",
                                            "material_index"):
            continue
        if len(attr.data) == 0:
            continue

        if attr.data_type == "FLOAT":
            result[name] = attr.data[0].value
        elif attr.data_type == "INT":
            result[name] = attr.data[0].value
        elif attr.data_type == "BOOLEAN":
            result[name] = bool(attr.data[0].value)
        elif attr.data_type == "STRING":
            raw = attr.data[0].value
            result[name] = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        elif attr.data_type == "FLOAT_VECTOR":
            result[name] = list(attr.data[0].vector)
        # Other types (BYTE_COLOR, etc.) are analysis attrs — skip

    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _write_uniform(mesh, name: str, value, attr_type: str, domain: str):
    """Write a uniform scalar attribute (every element gets the same value)."""
    if name in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[name])
    attr = mesh.attributes.new(name=name, type=attr_type, domain=domain)
    if attr_type == "STRING":
        encoded = _coerce_string_attribute_value(value)
        for element in attr.data:
            element.value = encoded
    else:
        for element in attr.data:
            element.value = value


def _coerce_string_attribute_value(value):
    """Return a Blender-compatible byte string for STRING attributes."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    return str(value).encode("utf-8")


def _write_vector(mesh, name: str, vec, domain: str):
    """Write a uniform FLOAT_VECTOR attribute."""
    padded = [float(v) for v in vec]
    while len(padded) < 3:
        padded.append(0.0)
    padded = padded[:3]
    if name in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[name])
    attr = mesh.attributes.new(name=name, type="FLOAT_VECTOR", domain=domain)
    for element in attr.data:
        element.vector = padded


# ---------------------------------------------------------------------------
# Legacy wrappers (kept for GeoJSON operator compatibility)
# ---------------------------------------------------------------------------

def write_scalar_attribute(mesh, name, value, domain="POINT"):
    """Write a uniform FLOAT attribute named ``name`` on ``mesh``."""
    _write_uniform(mesh, name, float(value), "FLOAT", domain)


def write_vector_attribute(mesh, name, vec, domain="POINT"):
    """Write a uniform FLOAT_VECTOR attribute."""
    _write_vector(mesh, name, vec, domain)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def write_feature_properties(obj, mesh, properties,
                              store_custom_props=True,
                              write_attributes=True,
                              auto_normal=True,
                              normal_prop="normal"):
    """Write feature *properties* to mesh attributes and (optionally) custom props.

    When ``write_attributes`` is true, supported values are written as mesh
    attributes, including STRING attributes for text and JSON-encoded values.

    ``store_custom_props`` — if True, also writes **all** values as custom
    properties (legacy behaviour for callers that still need it).

    ``auto_normal`` / ``normal_prop`` — if True, the named property is
    written as a FLOAT_VECTOR mesh attribute.
    """
    mesh_keys = write_mesh_attributes(mesh, properties) if write_attributes else set()

    # Handle auto_normal explicitly if not already handled
    if write_attributes and auto_normal and normal_prop and normal_prop in properties:
        value = properties[normal_prop]
        if (isinstance(value, list)
                and 2 <= len(value) <= 4
                and all(isinstance(v, (int, float)) for v in value)):
            if normal_prop not in mesh_keys:
                _write_vector(mesh, normal_prop, value, "POINT")
                mesh_keys.add(normal_prop)

    # Values not written as mesh attributes remain available as custom props.
    write_non_mesh_properties(obj, properties, skip=mesh_keys)

    # Legacy: also write all props as custom properties if requested
    if store_custom_props:
        for key, value in properties.items():
            if value is None:
                continue
            if key in mesh_keys:
                # Already a mesh attr — also write as custom prop for legacy compat
                obj[key] = value
            # Non-mesh types already written by write_non_mesh_properties
