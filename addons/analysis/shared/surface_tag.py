"""
analysis/surface_tag.py — Face classification by terrain criteria.

Writes a BOOLEAN face attribute to classify mesh faces:

  SLOPE mode     — tags a face ``True`` when its ``slope_deg`` value is
                   within a user-defined range (min..max).  ``slope_deg``
                   is auto-computed when absent unless opted out.

  SELECTION mode — tags each face ``True`` / ``False`` based on whether it
                   was selected the last time the object was in Edit Mode.
                   (``MeshPolygon.select`` reflects the last Edit Mode state
                   once you return to Object Mode.)

The output attribute name is configurable so multiple tag layers can coexist
on the same mesh, e.g. ``walkable``, ``roof_surface``, ``road_surface``.

Typical usage::

    from .surface_tag import tag_faces_by_slope, tag_faces_by_selection

    tagged, total = tag_faces_by_slope(
        obj, min_slope_deg=0.0, max_slope_deg=15.0, attr_name="walkable"
    )
    print(f"{tagged}/{total} faces tagged walkable")
"""

from __future__ import annotations

import logging
from typing import Tuple

import bpy

log = logging.getLogger(__name__)

_SLOPE_ATTR = "slope_deg"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_bool_attr(mesh: bpy.types.Mesh, name: str) -> bpy.types.Attribute:
    """Return a BOOLEAN FACE attribute, recreating it if the type differs."""
    if name in mesh.attributes:
        existing = mesh.attributes[name]
        if existing.domain == "FACE" and existing.data_type == "BOOLEAN":
            return existing
        mesh.attributes.remove(existing)
    return mesh.attributes.new(name=name, type="BOOLEAN", domain="FACE")


def _get_slope_attr(obj: bpy.types.Object, auto_compute: bool) -> bpy.types.Attribute:
    """Return the slope_deg FLOAT FACE attribute, computing it first if needed.

    Raises ``ValueError`` when the attribute is absent and *auto_compute* is
    False, so the operator can surface that as a user-facing message.
    """
    mesh = obj.data
    if (
        _SLOPE_ATTR in mesh.attributes
        and mesh.attributes[_SLOPE_ATTR].domain == "FACE"
        and mesh.attributes[_SLOPE_ATTR].data_type == "FLOAT"
    ):
        return mesh.attributes[_SLOPE_ATTR]

    if not auto_compute:
        raise ValueError(
            f"'{obj.name}' has no '{_SLOPE_ATTR}' attribute — "
            "run the Slope tool first or enable 'Auto-compute Slope'."
        )

    # Import here to avoid a circular import at module level
    from . import terrain as _terrain
    _terrain.compute_terrain_attributes(obj, compute_slope=True)
    return mesh.attributes[_SLOPE_ATTR]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def tag_faces_by_slope(
    obj: bpy.types.Object,
    min_slope_deg: float = 0.0,
    max_slope_deg: float = 15.0,
    attr_name: str = "walkable",
    auto_compute: bool = True,
) -> Tuple[int, int]:
    """Write *attr_name* (BOOLEAN, FACE) — True when slope is within range.

    Parameters
    ----------
    obj:
        Target mesh object.
    min_slope_deg:
        Lower bound of the walkable slope range (inclusive, default 0°).
    max_slope_deg:
        Upper bound of the walkable slope range (inclusive, default 15°).
    attr_name:
        Name of the output boolean attribute.
    auto_compute:
        When True, run slope computation automatically if ``slope_deg`` is
        absent.  When False, raise ``ValueError`` instead.

    Returns
    -------
    (tagged_count, total_faces)
        Number of faces tagged ``True`` and total face count.
    """
    slope_attr = _get_slope_attr(obj, auto_compute)
    tag_attr = _ensure_bool_attr(obj.data, attr_name)

    tagged = 0
    for i in range(len(obj.data.polygons)):
        val = slope_attr.data[i].value
        result = min_slope_deg <= val <= max_slope_deg
        tag_attr.data[i].value = result
        if result:
            tagged += 1

    return tagged, len(obj.data.polygons)


def tag_faces_by_selection(
    obj: bpy.types.Object,
    attr_name: str = "walkable",
) -> Tuple[int, int]:
    """Write *attr_name* (BOOLEAN, FACE) mirroring the Edit Mode face selection.

    ``MeshPolygon.select`` reflects the face selection state from the last
    Edit Mode session once the object returns to Object Mode.  If the object
    is still in Edit Mode the mesh data may not be up-to-date; the operator
    warns the caller to exit Edit Mode first.

    Parameters
    ----------
    obj:
        Target mesh object (must be in Object Mode for reliable results).
    attr_name:
        Name of the output boolean attribute.

    Returns
    -------
    (tagged_count, total_faces)
    """
    tag_attr = _ensure_bool_attr(obj.data, attr_name)

    tagged = 0
    for i, poly in enumerate(obj.data.polygons):
        tag_attr.data[i].value = poly.select
        if poly.select:
            tagged += 1

    return tagged, len(obj.data.polygons)
