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

* **Direct-visibility entries** (per face; bake only, no Geometry Nodes
  preset: :data:`BAKE_ONLY`): Sky Visibility, Sky View (Cosine) and Openness
  (3D). Each face casts Rays fixed directions (``sun_hours.sky_directions``,
  equal solid angle; Openness (3D) also their mirror images below the
  horizon) from its centre, moved Ray Offset along the face normal, and
  counts the rays in front of the face that miss within Max Distance. The
  model both the shader and the tests follow is :func:`visibility_sums`.

Why a second mode: the bisection assumes that a ray at elevation θ hits iff
the horizon is at least θ. That holds for a height field only: under an
overhang a level ray escapes and a higher one hits the lip, so the bisection
settles on the wrong bracket and reports open sky. Counting misses over a
fixed direction set assumes nothing about the surface; it costs more rays for
the same resolution on a height field.

Differences from the Geometry Nodes presets, all documented in
docs/analysis-presets.md: values are computed once (not live), in scene space
rather than the object's own space (the same for unrotated, unscaled
objects), and the meshes the analysis is added to occlude each other.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from ..core.layers import Aggregation, DataType, Domain, LayerSpec, Origin, make_provenance
from . import presets as catalog
from .sun_hours import sky_directions

TOOL = "analysis_bake"
"""Provenance tool name of a fast-path layer (live preset layers use ``analysis_preset``)."""

ENGINE = "Cycles Raycast shader nodes in an EMIT bake (geotools.blender.ray_bake)"

HORIZON = frozenset({"sky_view_factor", "openness_positive", "openness_negative"})
VIEWSHED = frozenset({"viewshed_cumulative"})
DIRECT = frozenset({"sky_visibility", "sky_view_cosine", "openness_3d"})
"""Direct-visibility entries: bake only (:data:`BAKE_ONLY`)."""
FAST = HORIZON | VIEWSHED | DIRECT
"""Presets with a fast (bake) path, and the bake-only entries."""

_RAYS = catalog.PresetInput(
    "Rays",
    "INT",
    256,
    "Directions cast from each face, spread evenly over the sky (equal solid angle); "
    "Openness (3D) casts as many again below the horizon. Only those in front of the face "
    "are cast",
    min=16,
    max=4096,
)
_REACH = catalog.PresetInput(
    "Max Distance",
    "FLOAT",
    2.0,
    "Length of each ray: surface further than this along the ray does not block it",
    min=0.001,
    subtype="DISTANCE",
)
_OFFSET = catalog.PresetInput(
    "Ray Offset",
    "FLOAT",
    0.002,
    "Rays start this far from the face centre along its normal, so the face does not hit itself",
    min=0.0,
    subtype="DISTANCE",
)
_DIRECT_INPUTS = (_REACH, _RAYS, _OFFSET)


def _bake_only(ident: str, label: str, description: str) -> catalog.Preset:
    return catalog.Preset(
        id=ident,
        version=1,
        group="",
        category=catalog.Category.ARCHAEOLOGY,
        label=label,
        layer=ident,
        domain=Domain.FACE,
        units="ratio",
        aggregation=Aggregation.AREA_MEAN,
        description=description,
        inputs=_DIRECT_INPUTS,
        palette="greys",
        display_range=(0.0, 1.0),
        cost="heavy",
    )


BAKE_ONLY: tuple[catalog.Preset, ...] = (
    _bake_only(
        "sky_visibility",
        "Sky Visibility",
        "Share of the sky hemisphere (equal solid angle) seen from each face by direct rays: "
        "the sky-view factor of true 3D meshes, overhangs included",
    ),
    _bake_only(
        "sky_view_cosine",
        "Sky View (Cosine)",
        "Share of the sky seen from each face weighted by the cosine to its normal (the "
        "radiative sky view factor): 1 for a level open face, 0.5 for an open wall",
    ),
    _bake_only(
        "openness_3d",
        "Openness (3D)",
        "Share of the face's own hemisphere unobstructed within Max Distance, by direct "
        "rays: 1 on an open face, 0 in a closed cavity, whichever way the face points",
    ),
)
"""Entries with no Geometry Nodes preset: Add Analysis offers them as Fast Bake only."""

_BAKE_ONLY = {p.id: p for p in BAKE_ONLY}

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
    "sky_visibility": (
        "Direct visibility: share of Rays fixed sky directions (Fibonacci spiral, equal solid "
        "angle) in front of the face that miss the surface within Max Distance; rays cast by "
        "Cycles Raycast nodes in EMIT bakes"
    ),
    "sky_view_cosine": (
        "Direct visibility: 2/Rays x the sum of cos(angle to the face normal) over the fixed "
        "sky directions (Fibonacci spiral) that miss the surface within Max Distance; rays "
        "cast by Cycles Raycast nodes in EMIT bakes"
    ),
    "openness_3d": (
        "Direct visibility: share of the directions in the face's own hemisphere (Rays sky "
        "directions and their mirror images below the horizon) that miss the surface within "
        "Max Distance; rays cast by Cycles Raycast nodes in EMIT bakes"
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
    "direct": "the meshes the analysis is added to",
    "viewshed": "the meshes the analysis is added to and the Occluders collection",
}


def is_fast(preset_id: str) -> bool:
    return preset_id in FAST


def is_bake_only(preset_id: str) -> bool:
    return preset_id in _BAKE_ONLY


def get(preset_id: str) -> catalog.Preset:
    """A catalog preset or a bake-only entry; ``KeyError`` if there is neither."""
    return _BAKE_ONLY.get(preset_id) or catalog.get(preset_id)


def bake_only(category: catalog.Category) -> tuple[catalog.Preset, ...]:
    return tuple(p for p in BAKE_ONLY if p.category is category)


def kind(preset_id: str) -> str:
    """The fast path's kind: horizon, direct or viewshed (``ValueError`` if it has none)."""
    if preset_id in HORIZON:
        return "horizon"
    if preset_id in DIRECT:
        return "direct"
    if preset_id in VIEWSHED:
        return "viewshed"
    raise ValueError(f"{preset_id} has no fast path")


def input_names(preset_id: str) -> tuple[str, ...]:
    """The preset inputs the fast path takes (object inputs aside: Observers, Occluders)."""
    preset = get(preset_id)
    return tuple(i.name for i in preset.inputs if i.kind not in ("OBJECT", "COLLECTION"))


def default_inputs(preset_id: str) -> dict[str, object]:
    preset = get(preset_id)
    return {i.name: i.default for i in preset.inputs if i.name in input_names(preset_id)}


def check_inputs(preset_id: str, inputs: Mapping[str, object]) -> dict[str, Any]:
    """The inputs, complete and within the preset's limits; ``ValueError`` otherwise."""
    preset = get(preset_id)
    values: dict[str, Any] = default_inputs(preset_id)
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


# --- direct visibility ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VisibilitySearch:
    """The rays of one direct-visibility entry."""

    rays: int
    max_distance: float
    ray_offset: float
    cosine: bool  # weight each miss by its cosine to the normal (Sky View (Cosine))
    sphere: bool  # also the mirror images below the horizon (Openness (3D))

    @property
    def directions(self) -> np.ndarray:
        """(D, 3) unit vectors: Rays sky directions, then (sphere) their mirror images."""
        sky = sky_directions(self.rays)
        return np.concatenate([sky, sky * (1.0, 1.0, -1.0)]) if self.sphere else sky


def visibility_search(preset_id: str, inputs: Mapping[str, object]) -> VisibilitySearch:
    if preset_id not in DIRECT:
        raise ValueError(f"{preset_id} is not a direct-visibility entry")
    values = check_inputs(preset_id, inputs)
    return VisibilitySearch(
        rays=int(values["Rays"]),
        max_distance=float(values["Max Distance"]),
        ray_offset=float(values["Ray Offset"]),
        cosine=preset_id == "sky_view_cosine",
        sphere=preset_id == "openness_3d",
    )


def visibility_sums(
    search: VisibilitySearch,
    normals: np.ndarray,
    hits: Callable[[int], np.ndarray],
) -> tuple[np.ndarray, np.ndarray]:
    """What the shader sums per sample, in binary64: (gains, rays in front).

    For each direction d (``search.directions``), a sample whose normal has
    d·n > 0 casts the ray; ``hits(d)`` says, per sample, whether it hits the
    surface within Max Distance (only read where the ray is cast). A miss
    gains 1, or d·n for the cosine entry. Rays behind the face are not cast:
    the surface itself hides those directions.
    """
    normals = np.asarray(normals, np.float64).reshape(-1, 3)
    gains = np.zeros(len(normals))
    front = np.zeros(len(normals))
    for d, direction in enumerate(search.directions):
        dot = normals @ direction
        cast = dot > 0.0
        miss = cast & ~np.asarray(hits(d), bool)
        gains += np.where(miss, dot if search.cosine else 1.0, 0.0)
        front += cast
    return gains, front


def visibility_values(
    preset_id: str, gains: np.ndarray, front: np.ndarray, rays: int
) -> np.ndarray:
    """The entry's value from the sums over its directions (``rays``: the Rays input).

    Sky Visibility: misses / Rays (each sky direction stands for 2π / Rays sr,
    so this is the visible share of the sky's solid angle). Sky View (Cosine):
    2 / Rays x Σ cos (the midpoint rule for (1/π) ∫ cos dω over the visible
    sky). Openness (3D): misses / rays in front.
    """
    gains = np.asarray(gains, np.float64)
    if preset_id == "sky_visibility":
        return gains / rays
    if preset_id == "sky_view_cosine":
        return 2.0 * gains / rays
    if preset_id == "openness_3d":
        front = np.asarray(front, np.float64)
        return np.divide(gains, front, out=np.zeros_like(gains), where=front > 0)
    raise ValueError(f"{preset_id} is not a direct-visibility entry")


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
    whose computation it reproduces (a bake-only entry: its own, with no node
    group), the method, the engine, the inputs, the
    objects and what occludes, plus ``run`` (samples, rays, bakes, ...).
    """
    preset = get(preset_id)
    if preset_id not in FAST or preset.domain is None:
        raise ValueError(f"{preset.label} has no fast path")
    path = kind(preset_id)
    parameters: dict[str, object] = {"preset": preset.id, "preset_version": preset.version}
    if preset.group:
        parameters["node_group"] = preset.group
    parameters.update(
        {
            "engine": "bake",
            "method": METHODS[preset_id],
            "ray_engine": ENGINE,
            "inputs": dict(inputs or {}),
            "objects": list(objects),
            "occluders": OCCLUDERS[path],
            "space": "scene (world) axes and units",
            "domain_sampled": "vertex, stored on its corners" if path == "horizon" else "face",
            **dict(run or {}),
        }
    )
    return LayerSpec(
        name=name or preset.layer,
        domain=preset.domain,
        data_type=DataType.FLOAT,
        units=preset.units,
        origin=Origin.ANALYSIS,
        aggregation=preset.aggregation,
        description=f"{preset.description} (computed once by a Cycles bake)",
        label=preset.label if preset.id in _BAKE_ONLY else f"{preset.label} (Fast)",
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
