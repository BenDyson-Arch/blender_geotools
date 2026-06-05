"""
analysis/terrain.py — Slope, aspect, and roughness as face attributes.

Works on any mesh object — terrain from DEM import, 3D Tiles geometry,
building roofs, or arbitrary imported meshes.  Nothing here is DEM-specific.

Attributes written (all FLOAT, FACE domain):

  slope_deg    Angle between face normal and world Z-up, in degrees.
               0 = perfectly flat, 90 = vertical wall.

  aspect_deg   Compass bearing of the downslope direction, in degrees.
               0 / 360 = North, 90 = East, 180 = South, 270 = West.
               -1 written for faces with slope < 0.1° (effectively flat —
               aspect is undefined there).

  roughness    Mean absolute deviation of vertex Z values within each face
               from that face's centroid Z, normalised by the face's
               inscribed-circle radius so the metric is scale-independent.
               0 = geometrically flat face.  Higher values indicate more
               local relief relative to face size.

All three are computed from the mesh as it sits in world space, so object
scale and rotation are correctly accounted for.  Existing attributes of the
same names are overwritten.
"""

from __future__ import annotations

import logging
import math
from typing import List, Tuple

import bpy
import mathutils

log = logging.getLogger(__name__)

_FLAT_THRESHOLD_DEG = 0.1   # faces flatter than this get aspect_deg = -1
_TWO_PI = 2.0 * math.pi


# ---------------------------------------------------------------------------
# Per-face calculations
# ---------------------------------------------------------------------------

def _world_normal(face: bpy.types.MeshPolygon,
                  world_mat: mathutils.Matrix) -> mathutils.Vector:
    """Return the face normal transformed into world space and normalised."""
    # MeshPolygon.normal is in object local space; apply the rotation part only.
    rot   = world_mat.to_3x3().normalized()
    world = rot @ face.normal
    return world.normalized()


def _slope_deg(normal: mathutils.Vector) -> float:
    """Angle between *normal* and the world Z-up axis, in degrees."""
    z_up = mathutils.Vector((0.0, 0.0, 1.0))
    cos_a = max(-1.0, min(1.0, normal.dot(z_up)))
    return math.degrees(math.acos(cos_a))


def _aspect_deg(normal: mathutils.Vector, slope: float) -> float:
    """Compass bearing of the downslope direction, in degrees (0 = North).

    Returns -1 for faces flatter than ``_FLAT_THRESHOLD_DEG``.
    """
    if slope < _FLAT_THRESHOLD_DEG:
        return -1.0

    # Project the normal onto the horizontal plane (XY) to get the
    # horizontal component of the outward-facing direction.
    horiz = mathutils.Vector((normal.x, normal.y, 0.0))
    if horiz.length < 1e-9:
        return -1.0
    horiz.normalize()

    # atan2(x, y) gives the bearing angle with 0 = North (+Y), 90 = East (+X)
    bearing = math.degrees(math.atan2(horiz.x, horiz.y))
    return bearing % 360.0


def _roughness(face: bpy.types.MeshPolygon,
               verts_world: List[mathutils.Vector]) -> float:
    """Scale-independent roughness for a single face.

    Computes the mean absolute deviation of vertex Z from the face centroid Z,
    divided by the face's inscribed-circle radius (area / half-perimeter).
    This normalisation makes the metric comparable across faces of different
    sizes.

    A planar face (all verts at the same Z) returns exactly 0.0.
    """
    face_verts = [verts_world[i] for i in face.vertices]
    n = len(face_verts)
    if n == 0:
        return 0.0

    centroid_z = sum(v.z for v in face_verts) / n
    mad_z = sum(abs(v.z - centroid_z) for v in face_verts) / n

    # Inscribed-circle radius = area / semi-perimeter
    area = face.area   # object-space area; acceptable for normalisation
    if area < 1e-12:
        return 0.0
    perimeter = sum(
        (face_verts[(i + 1) % n] - face_verts[i]).length
        for i in range(n)
    )
    semi_perimeter = perimeter / 2.0
    if semi_perimeter < 1e-9:
        return 0.0

    inscribed_radius = area / semi_perimeter
    return mad_z / inscribed_radius


# ---------------------------------------------------------------------------
# Attribute writers
# ---------------------------------------------------------------------------

def _ensure_face_attr(mesh: bpy.types.Mesh, name: str) -> bpy.types.Attribute:
    """Return a fresh FLOAT FACE attribute, removing any existing one first."""
    if name in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[name])
    return mesh.attributes.new(name=name, type="FLOAT", domain="FACE")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_terrain_attributes(
    obj: bpy.types.Object,
    compute_slope: bool = True,
    compute_aspect: bool = True,
    compute_roughness: bool = True,
) -> Tuple[float, float, float, float, float, float]:
    """Compute and write terrain attributes to *obj*'s mesh in-place.

    All three attributes are written as FLOAT face attributes so they are
    immediately consumable by Geometry Nodes and the attribute visualisation
    operator.

    Parameters
    ----------
    obj:
        A mesh object.  Must be ``obj.type == "MESH"``.
    compute_slope / compute_aspect / compute_roughness:
        Toggle individual metrics.  Disabled metrics are not written
        (existing attributes of that name are left unchanged).

    Returns
    -------
    (slope_min, slope_max, aspect_min, aspect_max, roughness_min, roughness_max)
        Ranges of computed values, useful for operator reporting.
        Ranges for disabled metrics return (0.0, 0.0).
    """
    if obj.type != "MESH":
        raise TypeError(f"Expected a MESH object, got {obj.type!r} ({obj.name!r})")

    mesh      = obj.data
    world_mat = obj.matrix_world
    faces     = mesh.polygons

    if len(faces) == 0:
        log.debug("compute_terrain_attributes: %s has no faces, skipping", obj.name)
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    # Pre-transform all vertices to world space once; reused for every face
    verts_world: List[mathutils.Vector] = [
        world_mat @ v.co for v in mesh.vertices
    ]

    # Prepare attribute storage
    slope_attr     = _ensure_face_attr(mesh, "slope_deg")     if compute_slope     else None
    aspect_attr    = _ensure_face_attr(mesh, "aspect_deg")    if compute_aspect    else None
    roughness_attr = _ensure_face_attr(mesh, "roughness")     if compute_roughness else None

    slopes     = []
    aspects    = []
    roughnesses = []

    for i, face in enumerate(faces):
        normal  = _world_normal(face, world_mat)
        slope   = _slope_deg(normal)
        aspect  = _aspect_deg(normal, slope)
        rough   = _roughness(face, verts_world)

        if slope_attr is not None:
            slope_attr.data[i].value = slope
            slopes.append(slope)

        if aspect_attr is not None:
            aspect_attr.data[i].value = aspect
            # Exclude the -1 sentinel from the reported range
            if aspect >= 0.0:
                aspects.append(aspect)

        if roughness_attr is not None:
            roughness_attr.data[i].value = rough
            roughnesses.append(rough)

    mesh.update()

    slope_range     = (min(slopes),      max(slopes))      if slopes      else (0.0, 0.0)
    aspect_range    = (min(aspects),     max(aspects))     if aspects     else (0.0, 0.0)
    roughness_range = (min(roughnesses), max(roughnesses)) if roughnesses else (0.0, 0.0)

    return (*slope_range, *aspect_range, *roughness_range)
