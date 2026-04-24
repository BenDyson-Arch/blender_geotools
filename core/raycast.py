"""
core/raycast.py — BVHTree-based raycast and nearest-point primitives.

Wraps mathutils.bvhtree so analysis operators share a single, consistent
interface without each reimplementing BVH construction and scene traversal.

All public functions and classes expect Blender world-space coordinates
(mathutils.Vector or plain 3-tuples).  Coordinates are *not* in the scene
CRS — callers are responsible for converting via GeoScene if needed.

No bpy.ops calls are made here.  All BVH work is done through the direct
mathutils API so these functions are safe to call from non-operator contexts
(e.g. modal timers, background jobs).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Set

import bpy
import mathutils

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class RaycastResult:
    """Result of a single ray cast."""

    hit: bool
    """True when the ray intersected geometry."""

    location: Optional[mathutils.Vector] = None
    """World-space hit point, or None when hit=False."""

    normal: Optional[mathutils.Vector] = None
    """World-space face normal at the hit point, or None when hit=False."""

    distance: Optional[float] = None
    """Distance from the ray origin to the hit point, or None when hit=False."""

    face_index: Optional[int] = None
    """Index of the hit face in obj.data.polygons, or None when hit=False.

    Use this with obj.data.attributes to read face attributes at the hit
    location without an additional spatial query.
    """

    object: Optional[bpy.types.Object] = None
    """The Blender object that was hit, or None when hit=False."""

    @property
    def miss(self) -> bool:
        return not self.hit

    # Convenience — bool(result) is True on hit
    def __bool__(self) -> bool:
        return self.hit


@dataclass
class NearestResult:
    """Result of a nearest-point query."""

    found: bool
    """True when a point was found within the search distance."""

    location: Optional[mathutils.Vector] = None
    """Closest point on the mesh surface."""

    normal: Optional[mathutils.Vector] = None
    """Face normal at the closest point."""

    distance: Optional[float] = None
    """Distance from the query point to the closest surface point."""

    face_index: Optional[int] = None
    """Index of the nearest face in obj.data.polygons."""

    object: Optional[bpy.types.Object] = None
    """The object that owns the nearest point."""

    def __bool__(self) -> bool:
        return self.found


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _objects_from_collections(
    context: bpy.types.Context,
    collection_names: Optional[Sequence[str]],
) -> List[bpy.types.Object]:
    """Return mesh objects visible in the viewport, optionally filtered by collection.

    When *collection_names* is None or empty every visible mesh object in
    the scene is returned.
    """
    if collection_names:
        name_set: Set[str] = set(collection_names)
        targets = []
        for col in bpy.data.collections:
            if col.name in name_set:
                targets.extend(col.objects)
    else:
        targets = list(context.scene.objects)

    return [
        obj for obj in targets
        if obj.type == "MESH" and obj.visible_get()
    ]


def _world_bvh(obj: bpy.types.Object) -> mathutils.bvhtree.BVHTree:
    """Build a BVHTree from *obj* in world space.

    Applies the object's world matrix so all queries work directly in
    world-space coordinates.  A fresh tree is built on every call — no
    caching so there are no stale-state issues when geometry changes.
    """
    mesh = obj.data
    world_mat = obj.matrix_world
    verts = [world_mat @ v.co for v in mesh.vertices]
    polys = [p.vertices[:] for p in mesh.polygons]
    return mathutils.bvhtree.BVHTree.FromPolygons(verts, polys)


def _face_attr_value(obj: bpy.types.Object, face_index: int, attr_name: str):
    """Read a single face attribute value from *obj*.

    Returns the value (float, int, bool, etc.) or None if the attribute
    does not exist or the face_index is out of range.
    """
    attrs = obj.data.attributes
    if attr_name not in attrs:
        return None
    attr = attrs[attr_name]
    if attr.domain != "FACE":
        return None
    try:
        element = attr.data[face_index]
    except IndexError:
        return None
    # Different attribute types expose their data through different field names
    for field_name in ("value", "color", "vector", "boolean", "integer"):
        if hasattr(element, field_name):
            return getattr(element, field_name)
    return None


# ---------------------------------------------------------------------------
# SceneRaycaster
# ---------------------------------------------------------------------------

class SceneRaycaster:
    """Performs ray casts and nearest-point queries against scene geometry.

    A single ``SceneRaycaster`` instance operates against a fixed set of
    objects determined at construction time.  Rebuild it when the relevant
    scene geometry changes (typically once per operator execution).

    Parameters
    ----------
    context:
        Active Blender context.
    collection_names:
        Optional list of collection names to limit the query target.
        When None or empty, all visible mesh objects in the scene are used.

    Example
    -------
    ::

        rc = SceneRaycaster(context, collection_names=["Terrain"])
        result = rc.cast(
            origin=mathutils.Vector((0, 0, 100)),
            direction=mathutils.Vector((0, 0, -1)),
        )
        if result:
            print(result.location, result.face_index)

        nearest = rc.nearest(mathutils.Vector((500, 300, 0)))
        if nearest:
            height = nearest.location.z
    """

    def __init__(
        self,
        context: bpy.types.Context,
        collection_names: Optional[Sequence[str]] = None,
        exclude_names: Optional[Sequence[str]] = None,
        include_names: Optional[Sequence[str]] = None,
    ) -> None:
        self._objects = _objects_from_collections(context, collection_names)
        if include_names:
            _inc: Set[str] = set(include_names)
            self._objects = [o for o in self._objects if o.name in _inc]
        if exclude_names:
            _exc: Set[str] = set(exclude_names)
            self._objects = [o for o in self._objects if o.name not in _exc]
        # Build one BVHTree per object, keyed by object name for diagnostics
        self._trees: dict[str, tuple[mathutils.bvhtree.BVHTree, bpy.types.Object]] = {}
        for obj in self._objects:
            try:
                tree = _world_bvh(obj)
                self._trees[obj.name] = (tree, obj)
            except Exception:
                log.debug("Could not build BVH for %s", obj.name, exc_info=True)

        log.debug(
            "SceneRaycaster built with %d object(s) from %s",
            len(self._trees),
            collection_names or "all collections",
        )

    @property
    def object_count(self) -> int:
        return len(self._trees)

    # ------------------------------------------------------------------
    # Ray cast

    def cast(
        self,
        origin: mathutils.Vector,
        direction: mathutils.Vector,
        max_distance: float = 1e9,
    ) -> RaycastResult:
        """Cast a ray and return the closest hit across all target objects.

        Parameters
        ----------
        origin:
            Ray start point in world space.
        direction:
            Ray direction vector (does not need to be normalised).
        max_distance:
            Discard hits further than this distance from *origin*.

        Returns
        -------
        RaycastResult — ``hit=True`` on intersection; ``hit=False`` on miss.
        The returned result is always the *closest* hit when multiple objects
        are intersected.
        """
        origin = mathutils.Vector(origin)
        direction = mathutils.Vector(direction).normalized()

        best: Optional[RaycastResult] = None

        for tree, obj in self._trees.values():
            loc, normal, face_idx, dist = tree.ray_cast(origin, direction, max_distance)
            if loc is None:
                continue
            if best is None or dist < best.distance:
                best = RaycastResult(
                    hit=True,
                    location=loc.copy(),
                    normal=normal.copy(),
                    distance=dist,
                    face_index=face_idx,
                    object=obj,
                )

        if best is not None:
            return best

        return RaycastResult(hit=False)

    def cast_vertical(
        self,
        xy: mathutils.Vector,
        z_origin: float = 1e6,
        direction: str = "DOWN",
    ) -> RaycastResult:
        """Cast a vertical ray from a world-XY position.

        Convenience wrapper around :meth:`cast` for the common case of
        dropping a ray straight down (or up) from an XY coordinate.

        Parameters
        ----------
        xy:
            2D or 3D vector; only X and Y are used.
        z_origin:
            Z coordinate of the ray start point.  Defaults to 1,000,000 m
            above sea level — high enough to be above any realistic geometry.
        direction:
            ``"DOWN"`` (negative Z) or ``"UP"`` (positive Z).
        """
        origin = mathutils.Vector((xy[0], xy[1], z_origin))
        d = mathutils.Vector((0.0, 0.0, -1.0 if direction.upper() == "DOWN" else 1.0))
        return self.cast(origin, d, max_distance=abs(z_origin) + 1e6)

    # ------------------------------------------------------------------
    # Nearest point

    def nearest(
        self,
        point: mathutils.Vector,
        max_distance: float = 1e9,
    ) -> NearestResult:
        """Find the closest point on any target surface to *point*.

        Uses ``BVHTree.find_nearest`` — no directionality, purely spatial
        proximity.  Suitable for distance queries and surface projection.

        Parameters
        ----------
        point:
            Query point in world space.
        max_distance:
            Discard results further than this distance.

        Returns
        -------
        NearestResult — ``found=True`` when a point within *max_distance*
        was found.
        """
        point = mathutils.Vector(point)
        best: Optional[NearestResult] = None

        for tree, obj in self._trees.values():
            loc, normal, face_idx, dist = tree.find_nearest(point, max_distance)
            if loc is None:
                continue
            if best is None or dist < best.distance:
                best = NearestResult(
                    found=True,
                    location=loc.copy(),
                    normal=normal.copy(),
                    distance=dist,
                    face_index=face_idx,
                    object=obj,
                )

        if best is not None:
            return best

        return NearestResult(found=False)

    def nearest_with_attr(
        self,
        point: mathutils.Vector,
        attr_name: str,
        attr_value,
        max_distance: float = 1e9,
        max_candidates: int = 64,
    ) -> NearestResult:
        """Find the nearest surface point where a face attribute matches *attr_value*.

        Walks outward from *point*, testing successive nearest-face candidates
        until a matching face is found or *max_candidates* is exhausted.

        This is an approximation — ``BVHTree.find_nearest`` returns only the
        single closest point per call, so this method calls it iteratively
        with an expanding radius.  Suitable for surfaces where tagged faces
        are not vanishingly sparse.

        Parameters
        ----------
        point:
            Query point in world space.
        attr_name:
            Name of a BOOLEAN or FLOAT face attribute to filter by.
        attr_value:
            Required value.  For BOOLEAN attributes use ``True``/``False``.
            For FLOAT use a float.
        max_distance:
            Hard cap on search radius in metres.
        max_candidates:
            Maximum number of candidate faces to test before giving up.
        """
        point = mathutils.Vector(point)

        # Collect (distance, NearestResult) from every tree in one pass,
        # sorted by distance.  We then walk the list checking the attribute.
        candidates: list[tuple[float, NearestResult]] = []

        # Expand search in steps to find more candidates beyond the closest
        step = max_distance / max(max_candidates, 1)
        radius = step
        seen_faces: set[tuple[str, int]] = set()

        while len(candidates) < max_candidates and radius <= max_distance:
            for tree, obj in self._trees.values():
                loc, normal, face_idx, dist = tree.find_nearest(point, radius)
                if loc is None:
                    continue
                key = (obj.name, face_idx)
                if key in seen_faces:
                    continue
                seen_faces.add(key)
                candidates.append((dist, NearestResult(
                    found=True,
                    location=loc.copy(),
                    normal=normal.copy(),
                    distance=dist,
                    face_index=face_idx,
                    object=obj,
                )))
            radius += step

        candidates.sort(key=lambda t: t[0])

        for _dist, result in candidates:
            val = _face_attr_value(result.object, result.face_index, attr_name)
            if val == attr_value:
                return result

        return NearestResult(found=False)
