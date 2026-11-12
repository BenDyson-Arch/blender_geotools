"""Fast (bake) paths of the heaviest Geometry Nodes presets: the bpy-free part.

Sky-view factor, positive and negative openness, and the cumulative viewshed
take minutes on a 10M-triangle mesh in Geometry Nodes. Their fast path casts
the same rays with Cycles ``Raycast`` shader nodes in one EMIT bake (the sun
hours engine, ``geotools.blender.ray_bake``) and stores the result as an
ordinary layer (origin ANALYSIS). This module holds what does not need
Blender: which presets have a fast path, their inputs, the layer and its
provenance, how bake sums become values, and a numpy model of the horizon
bisection (:func:`bisect_horizon`) that both engines follow.

The computation is the preset's, ray for ray:

* **Horizon presets** (per vertex, stored on corners): for each of
  Directions azimuths from +Y, Steps rays bisect the horizon angle within
  Max Distance; the first ray is level, each later one at the midpoint of the
  bracket, and the estimate is the last bracket's midpoint. Rays start Ray
  Offset along the vertex normal (below the surface for negative openness).
  SVF = 1 − mean sin(max(γ, 0)); positive openness 90° − mean γ; negative
  openness 90° + mean γ′.
* **Cumulative viewshed** (per face): for each observer's eye, a face counts
  when the eye is within Max Distance in plan of its centre, its front faces
  the eye (or Target Height > 0) and nothing lies on the segment from its
  centre (moved Ray Offset along the normal and raised by Target Height) to
  the eye.

Differences from the Geometry Nodes presets, all documented in
docs/analysis-presets.md: values are computed once (not live), in scene space
rather than the object's own space (the same for unrotated, unscaled
objects), and the meshes the analysis is added to occlude each other.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np

from ..core.layers import DataType, LayerSpec, Origin, make_provenance
from . import presets as catalog

TOOL = "analysis_bake"
"""Provenance tool name of a fast-path layer (live preset layers use ``analysis_preset``)."""

ENGINE = "Cycles Raycast shader nodes in an EMIT bake (geotools.blender.ray_bake)"

HORIZON = frozenset({"sky_view_factor", "openness_positive", "openness_negative"})
VIEWSHED = frozenset({"viewshed_cumulative"})
FAST = HORIZON | VIEWSHED
"""Presets with a fast (bake) path."""

METHODS = {
    "sky_view_factor": (
        "Zakšek et al. (2011) SVF = 1 - mean sin(max(horizon, 0)); horizon by bisection, "
        "Steps rays per direction from a level first ray, as the GeoTools Sky-View Factor "
        "preset; rays cast by Cycles Raycast nodes in one EMIT bake"
    ),
    "openness_positive": (
        "Yokoyama et al. (2002) positive openness = 90 - mean horizon angle (-90..90); "
        "horizon by bisection as the GeoTools Positive Openness preset; rays cast by "
        "Cycles Raycast nodes in one EMIT bake"
    ),
    "openness_negative": (
        "Yokoyama et al. (2002) negative openness = 90 + mean lowest angle (-90..90), rays "
        "from Ray Offset below the surface; bisection as the GeoTools Negative Openness "
        "preset; rays cast by Cycles Raycast nodes in one EMIT bake"
    ),
    "viewshed_cumulative": (
        "Count of observers whose eye sees the face: in plan range, facing the eye (or "
        "Target Height > 0) and nothing on the segment from the offset face centre to the "
        "eye, as the GeoTools Cumulative Viewshed preset; one Cycles Raycast node per "
        "observer, direction and length computed in the shader, in one EMIT bake per "
        "1,024 observers"
    ),
}

OCCLUDERS = {
    "horizon": "the meshes the analysis is added to",
    "viewshed": "the meshes the analysis is added to and the Occluders collection",
}


def is_fast(preset_id: str) -> bool:
    return preset_id in FAST


def input_names(preset_id: str) -> tuple[str, ...]:
    """The preset inputs the fast path takes (object inputs aside: Observers, Occluders)."""
    preset = catalog.get(preset_id)
    return tuple(i.name for i in preset.inputs if i.kind not in ("OBJECT", "COLLECTION"))


def default_inputs(preset_id: str) -> dict[str, object]:
    preset = catalog.get(preset_id)
    return {i.name: i.default for i in preset.inputs if i.name in input_names(preset_id)}


def check_inputs(preset_id: str, inputs: Mapping[str, object]) -> dict[str, object]:
    """The inputs, complete and within the preset's limits; ``ValueError`` otherwise."""
    preset = catalog.get(preset_id)
    values = default_inputs(preset_id)
    unknown = set(inputs) - set(values)
    if unknown:
        raise ValueError(f"{preset.label} has no input {min(unknown)!r}")
    values.update(inputs)
    for spec in preset.inputs:
        if spec.name not in values:
            continue
        value = values[spec.name]
        if spec.kind == "INT":
            value = int(value)
        elif spec.kind == "FLOAT":
            value = float(value)
            if not math.isfinite(value):
                raise ValueError(f"{spec.name} must be a finite number")
        elif spec.kind == "BOOLEAN":
            value = bool(value)
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{spec.name} must be at least {spec.min}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{spec.name} must be at most {spec.max}")
        values[spec.name] = value
    return values


# --- the horizon search ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HorizonSearch:
    """The bisection of one horizon preset."""

    below: bool  # negative openness: rays from below, bracketing the lowest point
    lower: float  # lowest angle searched, degrees (0 for SVF: never below level)
    max_distance: float
    directions: int
    steps: int
    ray_offset: float

    @property
    def side(self) -> float:
        """Signed offset of the ray origins along the normal."""
        return -self.ray_offset if self.below else self.ray_offset


def horizon_search(preset_id: str, inputs: Mapping[str, object]) -> HorizonSearch:
    values = check_inputs(preset_id, inputs)
    return HorizonSearch(
        below=preset_id == "openness_negative",
        lower=0.0 if preset_id == "sky_view_factor" else -90.0,
        max_distance=float(values["Max Distance"]),
        directions=int(values["Directions"]),
        steps=int(values["Steps"]),
        ray_offset=float(values["Ray Offset"]),
    )


def azimuth_vectors(directions: int) -> np.ndarray:
    """(N, 3) horizontal unit vectors (sin a, cos a, 0), a = i·360°/N from +Y."""
    azimuth = np.arange(directions, dtype=np.float64) * (2.0 * math.pi / directions)
    return np.stack([np.sin(azimuth), np.cos(azimuth), np.zeros(directions)], axis=1)


def bisect_horizon(
    search: HorizonSearch, hits: Callable[[int, np.ndarray], np.ndarray], count: int
) -> tuple[np.ndarray, np.ndarray]:
    """The bisection both engines run, for ``count`` samples: (angle sums, sine sums).

    ``hits(direction, theta)`` says, per sample, whether the ray at elevation
    ``theta`` (degrees, one per sample) in that direction hits the surface
    within Max Distance. Step 0 casts level; step k > 0 the bracket's midpoint;
    a hit raises the low end (lowers the high end when ``below``). The
    estimate is the final bracket's midpoint. Sums are binary64 (the shader's
    are float32).
    """
    angle_sum = np.zeros(count)
    sine_sum = np.zeros(count)
    for d in range(search.directions):
        lo = np.full(count, search.lower)
        hi = np.full(count, 90.0)
        for step in range(search.steps):
            theta = np.zeros(count) if step == 0 else 0.5 * (lo + hi)
            raise_low = np.asarray(hits(d, theta), bool) ^ search.below
            lo, hi = np.where(raise_low, theta, lo), np.where(raise_low, hi, theta)
        estimate = 0.5 * (lo + hi)
        angle_sum += estimate
        sine_sum += np.sin(np.radians(np.maximum(estimate, 0.0)))
    return angle_sum, sine_sum


def horizon_values(
    preset_id: str, angle_sum: np.ndarray, sine_sum: np.ndarray, directions: int
) -> np.ndarray:
    """The preset's value from the sums over its directions."""
    if preset_id == "sky_view_factor":
        return 1.0 - np.asarray(sine_sum) / directions
    if preset_id == "openness_negative":
        return 90.0 + np.asarray(angle_sum) / directions
    if preset_id == "openness_positive":
        return 90.0 - np.asarray(angle_sum) / directions
    raise ValueError(f"{preset_id} is not a horizon preset")


# --- the cumulative viewshed -----------------------------------------------------------------


def eyes(points: np.ndarray, observer_height: float) -> np.ndarray:
    """The observers' eyes: points (draped or not) raised by Observer Height, binary64."""
    eye = np.array(points, dtype=np.float64).reshape(-1, 3)
    eye[:, 2] += float(observer_height)
    return eye


def view_points(
    centres: np.ndarray, normals: np.ndarray, ray_offset: float, target_height: float
) -> np.ndarray:
    """Where each face's sight lines start: centre + Ray Offset x normal + Target Height up."""
    point = np.asarray(centres, np.float64) + float(ray_offset) * np.asarray(normals, np.float64)
    point[:, 2] += float(target_height)
    return point


# --- the layer ---------------------------------------------------------------------------------


def layer_spec(
    preset_id: str,
    name: str | None = None,
    *,
    inputs: Mapping[str, object] | None = None,
    objects: tuple[str, ...] = (),
    run: Mapping[str, object] | None = None,
    geotools_version: str = "",
) -> LayerSpec:
    """The FLOAT layer a fast path writes: the preset's layer, origin ANALYSIS.

    The provenance (tool ``analysis_bake``) names the preset and the version
    whose computation it reproduces, the method, the engine, the inputs, the
    objects and what occludes, plus ``run`` (samples, rays, bakes, ...).
    """
    preset = catalog.get(preset_id)
    if preset_id not in FAST:
        raise ValueError(f"{preset.label} has no fast path")
    kind = "horizon" if preset_id in HORIZON else "viewshed"
    parameters: dict[str, object] = {
        "preset": preset.id,
        "preset_version": preset.version,
        "node_group": preset.group,
        "engine": "bake",
        "method": METHODS[preset_id],
        "ray_engine": ENGINE,
        "inputs": dict(inputs or {}),
        "objects": list(objects),
        "occluders": OCCLUDERS[kind],
        "space": "scene (world) axes and units",
        "domain_sampled": "vertex, stored on its corners" if kind == "horizon" else "face",
        **dict(run or {}),
    }
    return LayerSpec(
        name=name or preset.layer,
        domain=preset.domain,
        data_type=DataType.FLOAT,
        units=preset.units,
        origin=Origin.ANALYSIS,
        aggregation=preset.aggregation,
        description=f"{preset.description} (computed once by a Cycles bake)",
        label=f"{preset.label} (Fast)",
        provenance=make_provenance(TOOL, parameters, geotools_version),
    )


def fast_preset_of(provenance: Mapping[str, object] | None) -> str | None:
    """The preset id a fast-path layer's provenance names, or None for other layers."""
    if not provenance or provenance.get("tool") != TOOL:
        return None
    parameters = provenance.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    preset = parameters.get("preset")
    return preset if isinstance(preset, str) else None
