"""The analysis preset library: what each bundled Geometry Nodes preset is.

A preset is an ordinary Geometry Nodes group of stock nodes, shipped in
``geotools/assets/analysis_presets.blend`` (built by
``scripts/make_analysis_presets.py``). "Add Analysis" appends the group, adds
it as a modifier and creates the M3 layer its Store Named Attribute writes.
This module is the catalog both sides read: the group's name and version, its
inputs and defaults, and the layer it produces (domain, units, aggregation,
display range). It is bpy-free; the formulas are in ``docs/analysis-presets.md``.

Every group has a string input ``Attribute`` (the attribute it stores, which
the operator sets to the layer's ``gt_<name>``). Groups that need north have a
``Convergence`` input, filled from ``analysis.site`` when true north is asked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.layers import (
    Aggregation,
    DataType,
    Domain,
    LayerSpec,
    Origin,
    attribute_name,
    make_provenance,
)

LIBRARY_FILE = "analysis_presets.blend"
"""The bundled library, in ``geotools/assets/``."""

PRESET_KEY = "geotools_preset"
"""Custom property on each node group of the library: the preset (or helper) id."""
VERSION_KEY = "geotools_preset_version"
"""Custom property on each node group: the version of that group."""

ATTRIBUTE_INPUT = "Attribute"
CONVERGENCE_INPUT = "Convergence"
TOOL = "analysis_preset"
"""``provenance["tool"]`` of a layer made by Add Analysis."""


class Category(StrEnum):
    TERRAIN = "TERRAIN"
    ARCHAEOLOGY = "ARCHAEOLOGY"


CATEGORY_LABELS = {
    Category.TERRAIN: "Terrain Shape",
    Category.ARCHAEOLOGY: "Archaeological Visualisation",
}


@dataclass(frozen=True, slots=True)
class PresetInput:
    """One input of a preset's node group (besides Geometry and Attribute)."""

    name: str
    kind: str  # "FLOAT", "INT" or "BOOLEAN"
    default: float | int | bool
    description: str
    min: float | int | None = None
    max: float | int | None = None
    subtype: str = "NONE"  # "DISTANCE", "ANGLE" (stored in degrees, shown as a plain float)


@dataclass(frozen=True, slots=True)
class Preset:
    id: str
    version: int
    group: str  # node group name in the library
    category: Category
    label: str  # menu and layer label
    layer: str  # default layer name
    domain: Domain  # FACE or CORNER
    units: str
    aggregation: Aggregation
    description: str  # one sentence, for the layer and the group
    inputs: tuple[PresetInput, ...] = ()
    palette: str = "viridis"
    display_range: tuple[float, float] | None = None  # fixed ramp range, else automatic
    north: bool = False  # has a Convergence input
    cost: str = "light"  # "light", "medium", "heavy": a hint in the menu and the docs
    parameters: Mapping[str, object] = field(default_factory=dict)  # extra provenance

    @property
    def attribute(self) -> str:
        return attribute_name(self.layer)


# --- inputs shared by several presets ------------------------------------------------

_SEARCH_HEIGHT = PresetInput(
    "Search Height",
    "FLOAT",
    2.0,
    "Neighbour heights are read by vertical rays started this far above the point and "
    "running twice as far down: the topmost surface within that band (2.5D)",
    min=0.001,
    subtype="DISTANCE",
)
_SMOOTHING = PresetInput(
    "Smoothing",
    "INT",
    0,
    "Blur iterations of the face normals over neighbouring faces before measuring "
    "(0: each face's own normal)",
    min=0,
    max=1000,
)
_MAX_DISTANCE = PresetInput(
    "Max Distance",
    "FLOAT",
    2.0,
    "How far the horizon is searched along each direction",
    min=0.001,
    subtype="DISTANCE",
)
_DIRECTIONS = PresetInput(
    "Directions", "INT", 16, "Azimuths searched, evenly spaced from +Y", min=1, max=64
)
_RAY_OFFSET = PresetInput(
    "Ray Offset",
    "FLOAT",
    0.002,
    "Rays start this far from the surface along the vertex normal, so the surface does "
    "not hit itself",
    min=0.0,
    subtype="DISTANCE",
)


def _steps(default: int) -> PresetInput:
    return PresetInput(
        "Steps",
        "INT",
        default,
        "Rays per direction that bisect the horizon angle; it is resolved to 90° / 2^(steps - 1)",
        min=1,
        max=16,
    )


def _window(default: float) -> PresetInput:
    return PresetInput(
        "Window",
        "FLOAT",
        default,
        "Spacing of the 3 x 3 window of heights around each point (the window is twice this wide)",
        min=0.0001,
        subtype="DISTANCE",
    )


_CURVATURE_INPUTS = (_window(0.1), _SEARCH_HEIGHT)
_HORIZON_INPUTS = (_MAX_DISTANCE, _DIRECTIONS, _steps(8), _RAY_OFFSET)


PRESETS: tuple[Preset, ...] = (
    # --- terrain shape -----------------------------------------------------------------
    Preset(
        id="slope",
        version=1,
        group="GeoTools Slope",
        category=Category.TERRAIN,
        label="Slope",
        layer="slope",
        domain=Domain.FACE,
        units="deg",
        aggregation=Aggregation.AREA_MEAN,
        description="Angle between each face's normal and +Z, in degrees: 0 level, 90 "
        "vertical, above 90 overhanging",
        inputs=(_SMOOTHING,),
        palette="viridis",
        display_range=(0.0, 90.0),
    ),
    Preset(
        id="slope_percent",
        version=1,
        group="GeoTools Slope Percent",
        category=Category.TERRAIN,
        label="Slope (Percent)",
        layer="slope_percent",
        domain=Domain.FACE,
        units="%",
        aggregation=Aggregation.AREA_MEAN,
        description="Rise over run of each face, in percent (100 is 45 degrees); no data "
        "on vertical and overhanging faces",
        inputs=(_SMOOTHING,),
        palette="viridis",
        display_range=(0.0, 100.0),
    ),
    Preset(
        id="aspect",
        version=1,
        group="GeoTools Aspect",
        category=Category.TERRAIN,
        label="Aspect",
        layer="aspect",
        domain=Domain.FACE,
        units="deg",
        aggregation=Aggregation.NONE,
        description="Compass direction each face faces (its downslope direction), "
        "clockwise from north in degrees; no data on level faces",
        inputs=(
            PresetInput(
                CONVERGENCE_INPUT,
                "FLOAT",
                0.0,
                "Degrees from true north to the scene's +Y, clockwise: 0 measures from +Y "
                "(grid north); Add Analysis fills it for true north",
                min=-180.0,
                max=180.0,
            ),
            PresetInput(
                "Flat Slope",
                "FLOAT",
                0.5,
                "Faces flatter than this many degrees have no aspect (no data)",
                min=0.0,
                max=90.0,
            ),
            _SMOOTHING,
        ),
        palette="turbo",
        display_range=(0.0, 360.0),
        north=True,
    ),
    Preset(
        id="curvature_profile",
        version=1,
        group="GeoTools Profile Curvature",
        category=Category.TERRAIN,
        label="Profile Curvature",
        layer="curvature_profile",
        domain=Domain.CORNER,
        units="1/m",
        aggregation=Aggregation.AREA_MEAN,
        description="Normal curvature along the slope line (vertical curvature), 1/m: "
        "positive convex (crests, breaks of slope), negative concave",
        inputs=_CURVATURE_INPUTS,
        palette="diverging_rdbu",
        cost="medium",
    ),
    Preset(
        id="curvature_plan",
        version=1,
        group="GeoTools Plan Curvature",
        category=Category.TERRAIN,
        label="Plan Curvature",
        layer="curvature_plan",
        domain=Domain.CORNER,
        units="1/m",
        aggregation=Aggregation.AREA_MEAN,
        description="Normal curvature along the contour (horizontal or tangential "
        "curvature), 1/m: positive convex (spurs, ridges), negative concave (hollows)",
        inputs=_CURVATURE_INPUTS,
        palette="diverging_rdbu",
        cost="medium",
    ),
    Preset(
        id="curvature_mean",
        version=1,
        group="GeoTools Mean Curvature",
        category=Category.TERRAIN,
        label="Mean Curvature",
        layer="curvature_mean",
        domain=Domain.CORNER,
        units="1/m",
        aggregation=Aggregation.AREA_MEAN,
        description="Mean of the principal curvatures, 1/m: positive convex, negative concave",
        inputs=_CURVATURE_INPUTS,
        palette="diverging_rdbu",
        cost="medium",
    ),
    Preset(
        id="rugosity",
        version=1,
        group="GeoTools Rugosity",
        category=Category.TERRAIN,
        label="Rugosity",
        layer="rugosity",
        domain=Domain.FACE,
        units="m2/m2",
        aggregation=Aggregation.AREA_MEAN,
        description="Surface area over planimetric (plan) area in a neighbourhood of "
        "each face: 1 on level ground, higher where the surface is rough or steep",
        inputs=(
            PresetInput(
                "Radius",
                "FLOAT",
                0.1,
                "Neighbourhood radius: the face areas are blurred over neighbouring faces "
                "(a Gaussian of about half this radius)",
                min=0.0,
                subtype="DISTANCE",
            ),
            PresetInput(
                "Max Iterations",
                "INT",
                1000,
                "Upper limit on the blur iterations the radius asks for",
                min=0,
                max=100000,
            ),
        ),
        palette="magma",
        display_range=(1.0, 2.0),
    ),
    Preset(
        id="tpi",
        version=1,
        group="GeoTools TPI",
        category=Category.TERRAIN,
        label="Topographic Position (TPI)",
        layer="tpi",
        domain=Domain.CORNER,
        units="m",
        aggregation=Aggregation.AREA_MEAN,
        description="Height above the mean height of a ring (annulus) around each point, "
        "in metres: positive on rises and crests, negative in hollows",
        inputs=(
            PresetInput(
                "Inner Radius",
                "FLOAT",
                0.0,
                "Inner radius of the annulus (0: a full disc)",
                min=0.0,
                subtype="DISTANCE",
            ),
            PresetInput(
                "Outer Radius",
                "FLOAT",
                0.5,
                "Outer radius of the annulus",
                min=0.0001,
                subtype="DISTANCE",
            ),
            PresetInput(
                "Samples",
                "INT",
                24,
                "Heights sampled in the annulus: point-symmetric pairs on a sunflower pattern of even "
                "density (an odd number is rounded up)",
                min=1,
                max=1024,
            ),
            _SEARCH_HEIGHT,
        ),
        palette="diverging_rdbu",
        cost="medium",
    ),
    Preset(
        id="tri",
        version=1,
        group="GeoTools TRI",
        category=Category.TERRAIN,
        label="Ruggedness (TRI)",
        layer="tri",
        domain=Domain.CORNER,
        units="m",
        aggregation=Aggregation.AREA_MEAN,
        description="Terrain Ruggedness Index (Riley): the root of the summed squared "
        "height differences from each point to the eight points of a 3 x 3 window, in "
        "metres",
        inputs=(_window(0.05), _SEARCH_HEIGHT),
        palette="magma",
        cost="medium",
    ),
    # --- archaeological visualisation --------------------------------------------------
    Preset(
        id="local_relief",
        version=1,
        group="GeoTools Local Relief",
        category=Category.ARCHAEOLOGY,
        label="Local Relief Model",
        layer="local_relief",
        domain=Domain.CORNER,
        units="m",
        aggregation=Aggregation.AREA_MEAN,
        description="Height above the surface smoothed over a disc (the trend removal of "
        "a local relief model), in metres: subtle banks positive, ditches negative",
        inputs=(
            PresetInput(
                "Radius",
                "FLOAT",
                2.0,
                "Radius of the disc the surface is smoothed over: larger than the "
                "features to bring out",
                min=0.0001,
                subtype="DISTANCE",
            ),
            PresetInput(
                "Samples",
                "INT",
                48,
                "Heights sampled in the disc: point-symmetric pairs on a sunflower pattern of even "
                "density (an odd number is rounded up)",
                min=1,
                max=1024,
            ),
            PresetInput(
                "Search Height",
                "FLOAT",
                5.0,
                _SEARCH_HEIGHT.description,
                min=0.001,
                subtype="DISTANCE",
            ),
        ),
        palette="diverging_rdbu",
        cost="medium",
    ),
    Preset(
        id="sky_view_factor",
        version=1,
        group="GeoTools Sky-View Factor",
        category=Category.ARCHAEOLOGY,
        label="Sky-View Factor",
        layer="sky_view_factor",
        domain=Domain.CORNER,
        units="ratio",
        aggregation=Aggregation.AREA_MEAN,
        description="Share of the sky hemisphere seen from each point (Zakšek et al. "
        "2011): 1 in the open, lower in ditches and against walls",
        inputs=(_MAX_DISTANCE, _DIRECTIONS, _steps(8), _RAY_OFFSET),
        palette="greys",
        display_range=(0.6, 1.0),
        cost="heavy",
    ),
    Preset(
        id="openness_positive",
        version=1,
        group="GeoTools Positive Openness",
        category=Category.ARCHAEOLOGY,
        label="Positive Openness",
        layer="openness_positive",
        domain=Domain.CORNER,
        units="deg",
        aggregation=Aggregation.AREA_MEAN,
        description="Mean zenith angle of the horizon above each point (Yokoyama et al. "
        "2002), degrees: 90 on a plane, more on crests, less in hollows",
        inputs=_HORIZON_INPUTS,
        palette="greys",
        display_range=(60.0, 120.0),
        cost="heavy",
    ),
    Preset(
        id="openness_negative",
        version=1,
        group="GeoTools Negative Openness",
        category=Category.ARCHAEOLOGY,
        label="Negative Openness",
        layer="openness_negative",
        domain=Domain.CORNER,
        units="deg",
        aggregation=Aggregation.AREA_MEAN,
        description="Mean nadir angle of the horizon below each point (Yokoyama et al. "
        "2002), degrees: 90 on a plane, more in hollows, less on crests",
        inputs=_HORIZON_INPUTS,
        palette="greys",
        display_range=(60.0, 120.0),
        cost="heavy",
    ),
)


PLANNED: tuple[tuple[str, str], ...] = (
    ("Visibility", "viewshed, cumulative viewshed, intervisibility and line of sight"),
    ("Surface reading", "ambient occlusion and curvature bakes for worn surfaces"),
    ("Change and distance", "distance between two surveys, distance to a feature"),
    ("Volumes", "cut and fill between two surfaces, closed-mesh volumes"),
    ("Least-cost paths", "Shortest Edge Paths with a Tobler walking cost"),
    ("Flow paths", "quick-look flow tracing in a Repeat zone"),
)
"""The next batch of presets (docs only)."""


_BY_ID = {p.id: p for p in PRESETS}


def get(preset_id: str) -> Preset:
    """The preset with this id; ``KeyError`` if there is none."""
    return _BY_ID[preset_id]


def by_category(category: Category) -> tuple[Preset, ...]:
    return tuple(p for p in PRESETS if p.category is category)


def layer_spec(
    preset: Preset,
    name: str | None = None,
    *,
    inputs: Mapping[str, object] | None = None,
    objects: tuple[str, ...] = (),
    north: str | None = None,
    geotools_version: str = "",
) -> LayerSpec:
    """The FLOAT layer a preset writes, named ``name`` (default: the preset's layer).

    The provenance names the preset, its version and node group, the input
    values set on the modifiers, the objects and, for a north-aware preset,
    which north the values are measured from.
    """
    parameters: dict[str, object] = {
        "preset": preset.id,
        "preset_version": preset.version,
        "node_group": preset.group,
        "library": LIBRARY_FILE,
        "inputs": dict(inputs or {}),
        "objects": list(objects),
        **preset.parameters,
    }
    if preset.north:
        parameters["north"] = north or "grid"
    return LayerSpec(
        name=name or preset.layer,
        domain=preset.domain,
        data_type=DataType.FLOAT,
        units=preset.units,
        origin=Origin.GEONODES,
        aggregation=preset.aggregation,
        description=preset.description,
        label=preset.label,
        provenance=make_provenance(TOOL, parameters, geotools_version),
    )


def preset_of(provenance: Mapping[str, object] | None) -> str | None:
    """The preset id a layer's provenance names, or None for other layers."""
    if not provenance or provenance.get("tool") != TOOL:
        return None
    parameters = provenance.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    preset = parameters.get("preset")
    return preset if isinstance(preset, str) else None
