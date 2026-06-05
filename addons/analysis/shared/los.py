"""
analysis/los.py — Line-of-sight computation.

Uses ``core.raycast.SceneRaycaster`` to determine whether a straight line
between an observer point and a target point is free of geometric obstructions.

All coordinates are Blender world-space (not scene CRS).  Unit is metres when
the scene is set up at real-world scale — the same assumption as dem/reader.py.

Usage::

    from ....core.raycast import SceneRaycaster
    from .los import check_los

    raycaster = SceneRaycaster(context, collection_names=["Terrain"])
    result = check_los(raycaster, observer_pt, target_pt)
    if result.visible:
        print("Clear LoS over", result.distance, "m")
    else:
        print("Blocked by", result.obstructing_object.name,
              "at", result.hit_location)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import mathutils

from ....core.raycast import SceneRaycaster

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class LOSResult:
    """Result of a single line-of-sight check."""

    visible: bool
    """True when the ray reaches the target without hitting any geometry."""

    observer: mathutils.Vector
    """Start point used for the check (world space)."""

    target: mathutils.Vector
    """End point used for the check (world space)."""

    distance: float = 0.0
    """Straight-line distance from observer to target (metres)."""

    hit_location: Optional[mathutils.Vector] = None
    """World-space point of first obstruction, or None when visible."""

    obstructing_object: Optional[object] = None
    """Blender object that caused the obstruction, or None when visible."""

    def __bool__(self) -> bool:
        return self.visible


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------


def check_los(
    raycaster: SceneRaycaster,
    observer: mathutils.Vector,
    target: mathutils.Vector,
    epsilon: float = 0.05,
) -> LOSResult:
    """Check whether the line of sight from *observer* to *target* is clear.

    A small *epsilon* nudge is applied to the ray origin along the cast
    direction to avoid self-intersection at the observer's surface.

    Parameters
    ----------
    raycaster:
        Pre-built :class:`~core.raycast.SceneRaycaster` for the blocking
        geometry.  Build it with ``exclude_names`` set to the observer and
        target object names so their own geometry does not self-obstruct.
    observer:
        Ray start point in world space.
    target:
        Ray end point in world space.
    epsilon:
        Metres to offset the ray origin from *observer* before casting.
        Prevents false hits on the surface the observer sits on.

    Returns
    -------
    LOSResult
        ``visible=True`` when nothing blocks the path to *target*;
        ``visible=False`` with ``hit_location`` and ``obstructing_object``
        populated when blocked.
    """
    observer = mathutils.Vector(observer)
    target = mathutils.Vector(target)

    direction = target - observer
    target_dist = direction.length

    # Identical or nearly identical points → always visible
    if target_dist < 1e-6:
        return LOSResult(
            visible=True,
            observer=observer,
            target=target,
            distance=0.0,
        )

    direction_norm = direction.normalized()

    # Nudge origin to avoid self-hit; shorten cast length correspondingly
    ray_origin = observer + direction_norm * epsilon
    cast_dist = target_dist - epsilon

    # If epsilon consumed the entire distance the path is trivially clear
    if cast_dist <= 0.0:
        return LOSResult(
            visible=True,
            observer=observer,
            target=target,
            distance=target_dist,
        )

    result = raycaster.cast(
        origin=ray_origin,
        direction=direction_norm,
        max_distance=cast_dist,
    )

    if result:
        return LOSResult(
            visible=False,
            observer=observer,
            target=target,
            distance=target_dist,
            hit_location=result.location,
            obstructing_object=result.object,
        )

    return LOSResult(
        visible=True,
        observer=observer,
        target=target,
        distance=target_dist,
    )
