"""
analysis/attr_transfer.py — Copy face attributes between meshes using spatial queries.

Typical use-case: transfer a feature-type or height attribute from a 3D Tiles
building mesh onto flat GeoJSON polygon footprints that share the same
ground plane.

Two transfer methods are supported:

  RAYCAST  — For each face centroid of the *target* mesh, cast a vertical ray
             (UP or DOWN, user choice) and read the attribute of whichever face
             on the *source* mesh is first hit.  Fast and accurate when the
             source and target are vertically separated (e.g. building walls
             above footprint polygons).

  NEAREST  — For each face centroid of the *target* mesh, find the nearest
             point on the *source* mesh and use that face's attribute value.
             More robust when there is no clean vertical relationship between
             source and target.

Both functions accept a ``SceneRaycaster`` pre-scoped to the source object(s)
so callers control exactly which geometry is queried.

Attribute type matching: the destination attribute is created (or recreated)
with the same ``data_type`` as the source attribute.  Supported types:
FLOAT, INT, INT8, BOOLEAN, FLOAT_VECTOR, FLOAT2, FLOAT_COLOR, BYTE_COLOR.

Faces where the query returns no result are left at the attribute default
(zero / False / black).  The return value reports how many were filled.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Tuple

import bpy
import mathutils

from ....core.raycast import SceneRaycaster

log = logging.getLogger(__name__)

# Attribute data_type → element field name
_ATTR_FIELD: dict[str, str] = {
    "FLOAT": "value",
    "INT": "value",
    "INT8": "value",
    "BOOLEAN": "value",
    "FLOAT_VECTOR": "vector",
    "FLOAT2": "vector",
    "FLOAT_COLOR": "color",
    "BYTE_COLOR": "color",
    "QUATERNION": "value",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _face_centroid_world(
    face: bpy.types.MeshPolygon,
    mesh: bpy.types.Mesh,
    world_mat: mathutils.Matrix,
) -> mathutils.Vector:
    """Return the world-space centroid of *face*."""
    local = sum(
        (mesh.vertices[v].co for v in face.vertices),
        mathutils.Vector((0.0, 0.0, 0.0)),
    ) / len(face.vertices)
    return world_mat @ local


def _ensure_face_attr(
    mesh: bpy.types.Mesh,
    name: str,
    data_type: str,
) -> bpy.types.Attribute:
    """Return a FACE attribute with the given name and data_type, recreating if needed."""
    if name in mesh.attributes:
        existing = mesh.attributes[name]
        if existing.domain == "FACE" and existing.data_type == data_type:
            return existing
        mesh.attributes.remove(existing)
    return mesh.attributes.new(name=name, type=data_type, domain="FACE")


def _read_value(attr: bpy.types.Attribute, face_index: int):
    """Read a face attribute element value using the appropriate field name."""
    field = _ATTR_FIELD.get(attr.data_type)
    if field is None:
        return None
    try:
        element = attr.data[face_index]
    except IndexError:
        return None
    val = getattr(element, field, None)
    # Return a plain copy for mutable types (vectors, colors)
    if hasattr(val, "__iter__") and not isinstance(val, str):
        return type(val)(val)
    return val


def _write_value(attr: bpy.types.Attribute, face_index: int, value) -> None:
    """Write a value to a face attribute element."""
    if value is None:
        return
    field = _ATTR_FIELD.get(attr.data_type)
    if field is None:
        return
    try:
        setattr(attr.data[face_index], field, value)
    except (IndexError, TypeError, AttributeError):
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def transfer_by_raycast(
    raycaster: SceneRaycaster,
    src_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    src_attr_name: str,
    dst_attr_name: str,
    cast_direction: str = "UP",
    max_distance: float = 1e6,
) -> Tuple[int, int]:
    """Copy a face attribute from *src_obj* to *target_obj* via vertical raycasting.

    For each face centroid of *target_obj*, a vertical ray is cast in
    *cast_direction* (``"UP"`` or ``"DOWN"``) and the source face hit by that
    ray donates its attribute value to the corresponding target face.

    Parameters
    ----------
    raycaster:
        Pre-built raycaster scoped to *src_obj*.  Build with
        ``SceneRaycaster(context, include_names=[src_obj.name])``.
    src_obj:
        Source mesh object that owns the attribute to copy.
    target_obj:
        Target mesh object that will receive the attribute.
    src_attr_name:
        Name of the FACE attribute on *src_obj* to read.
    dst_attr_name:
        Name of the FACE attribute to write on *target_obj*.
    cast_direction:
        ``"UP"`` (positive Z) or ``"DOWN"`` (negative Z).
    max_distance:
        Maximum ray length in scene units (metres at real-world scale).

    Returns
    -------
    (transferred_faces, total_faces)
        Counts of faces that received a value and total target faces.
    """
    src_mesh = src_obj.data
    if src_attr_name not in src_mesh.attributes:
        raise ValueError(
            f"Source object '{src_obj.name}' has no attribute '{src_attr_name}'"
        )
    src_attr = src_mesh.attributes[src_attr_name]
    if src_attr.domain != "FACE":
        raise ValueError(f"Attribute '{src_attr_name}' is not a FACE-domain attribute")

    dst_attr = _ensure_face_attr(target_obj.data, dst_attr_name, src_attr.data_type)
    world_mat = target_obj.matrix_world

    direction = mathutils.Vector(
        (0.0, 0.0, 1.0 if cast_direction.upper() == "UP" else -1.0)
    )
    transferred = 0
    total = len(target_obj.data.polygons)

    for i, face in enumerate(target_obj.data.polygons):
        centroid = _face_centroid_world(face, target_obj.data, world_mat)
        result = raycaster.cast(centroid, direction, max_distance=max_distance)
        if result:
            value = _read_value(src_attr, result.face_index)
            if value is not None:
                _write_value(dst_attr, i, value)
                transferred += 1

    return transferred, total


def transfer_by_nearest(
    raycaster: SceneRaycaster,
    src_obj: bpy.types.Object,
    target_obj: bpy.types.Object,
    src_attr_name: str,
    dst_attr_name: str,
    max_distance: float = 100.0,
) -> Tuple[int, int]:
    """Copy a face attribute from *src_obj* to *target_obj* using nearest-point queries.

    For each face centroid of *target_obj*, the nearest point on *src_obj*
    within *max_distance* donates its face attribute value.

    Parameters
    ----------
    raycaster:
        Pre-built raycaster scoped to *src_obj*.
    src_obj:
        Source mesh object.
    target_obj:
        Target mesh object.
    src_attr_name:
        FACE attribute on *src_obj* to read.
    dst_attr_name:
        FACE attribute to write on *target_obj*.
    max_distance:
        Search radius in scene units.  Faces with no source point within this
        range are left at the default attribute value.

    Returns
    -------
    (transferred_faces, total_faces)
    """
    src_mesh = src_obj.data
    if src_attr_name not in src_mesh.attributes:
        raise ValueError(
            f"Source object '{src_obj.name}' has no attribute '{src_attr_name}'"
        )
    src_attr = src_mesh.attributes[src_attr_name]
    if src_attr.domain != "FACE":
        raise ValueError(f"Attribute '{src_attr_name}' is not a FACE-domain attribute")

    dst_attr = _ensure_face_attr(target_obj.data, dst_attr_name, src_attr.data_type)
    world_mat = target_obj.matrix_world

    transferred = 0
    total = len(target_obj.data.polygons)

    for i, face in enumerate(target_obj.data.polygons):
        centroid = _face_centroid_world(face, target_obj.data, world_mat)
        result = raycaster.nearest(centroid, max_distance=max_distance)
        if result:
            value = _read_value(src_attr, result.face_index)
            if value is not None:
                _write_value(dst_attr, i, value)
                transferred += 1

    return transferred, total
