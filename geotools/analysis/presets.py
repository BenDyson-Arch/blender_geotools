"""The analysis preset library: what each bundled Geometry Nodes preset is.

A preset is an ordinary Geometry Nodes group of stock nodes, shipped in
``geotools/assets/analysis_presets.blend`` (built by
``scripts/make_analysis_presets.py``). "Add Analysis" appends the group, adds
it as a modifier and creates the M3 layer its Store Named Attribute writes.
This module is the catalog both sides read: the group's name and version, its
inputs and defaults, and the layer it produces (domain, units, aggregation,
display range). It is bpy-free; the formulas are in ``docs/analysis-presets.md``.

Most presets are **layer** presets: a modifier on the selected meshes whose
group has a string input ``Attribute`` (the attribute it stores, which the
operator sets to the layer's ``gt_<name>``). Groups that need north have a
``Convergence`` input, filled from ``analysis.site`` when true north is asked.

**Object** presets (intervisibility, line of sight, least-cost paths and flow
paths) make lines rather than a layer: Add Analysis creates a new mesh object
whose modifier builds them from its inputs (the surface, points or a line),
with fixed POINT and EDGE attributes (``Preset.attributes``) that vector
publish exports as columns.

Object inputs (``kind="OBJECT"``) say how Add Analysis fills them (``Fill``):
from the other selected objects, or a new Empty or one-vertex mesh at the 3D
cursor.
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
PROVENANCE_KEY = "geotools_provenance"
"""Custom property (JSON text) on an object preset's new object: its provenance."""


class Category(StrEnum):
    TERRAIN = "TERRAIN"
    ARCHAEOLOGY = "ARCHAEOLOGY"
    VISIBILITY = "VISIBILITY"
    CHANGE = "CHANGE"
    VOLUMES = "VOLUMES"
    PATHS = "PATHS"


CATEGORY_LABELS = {
    Category.TERRAIN: "Terrain Shape",
    Category.ARCHAEOLOGY: "Archaeological Visualisation",
    Category.VISIBILITY: "Visibility",
    Category.CHANGE: "Change and Distance",
    Category.VOLUMES: "Volumes",
    Category.PATHS: "Paths and Flow",
}


class Output(StrEnum):
    LAYER = "LAYER"  # a modifier on the selected meshes, storing a FACE or CORNER layer
    OBJECT = "OBJECT"  # a new mesh object of lines, made by the modifier from its inputs


class Fill(StrEnum):
    """How Add Analysis fills an OBJECT input."""

    NONE = "NONE"  # left empty: optional
    POINT = (
        "POINT"  # a selected Empty, camera or light (its origin), else a new Empty at the 3D cursor
    )
    POINTS = (
        "POINTS"  # a selected object (its vertices), else a new one-vertex mesh at the 3D cursor
    )
    SURFACE = "SURFACE"  # the active mesh: the surface an object preset works on
    MESH = "MESH"  # another selected mesh with faces (required)
    LINE = "LINE"  # a selected mesh with edges, or a curve (required)
    FEATURE = "FEATURE"  # any other selected mesh or curve: points, lines or polygons (required)


FILL_LABELS = {
    Fill.POINT: "a point (an Empty, or the 3D cursor)",
    Fill.POINTS: "points (a mesh whose vertices are the points, or the 3D cursor)",
    Fill.SURFACE: "the surface (the active mesh)",
    Fill.MESH: "a second surface (another selected mesh)",
    Fill.LINE: "a line (a selected mesh with edges, or a curve)",
    Fill.FEATURE: "a feature (another selected mesh or curve)",
}


@dataclass(frozen=True, slots=True)
class PresetInput:
    """One input of a preset's node group (besides Geometry and Attribute)."""

    name: str
    kind: str  # "FLOAT", "INT", "BOOLEAN", "STRING", "OBJECT" or "COLLECTION"
    default: float | int | bool | str | None
    description: str
    min: float | int | None = None
    max: float | int | None = None
    subtype: str = "NONE"  # "DISTANCE", "ANGLE" (stored in degrees, shown as a plain float)
    fill: Fill = Fill.NONE  # OBJECT inputs: how Add Analysis fills it


@dataclass(frozen=True, slots=True)
class OutputAttribute:
    """An attribute an object preset's lines carry (exported as a vector column)."""

    name: str
    domain: str  # "POINT" or "EDGE"
    units: str
    description: str


@dataclass(frozen=True, slots=True)
class Preset:
    id: str
    version: int
    group: str  # node group name in the library
    category: Category
    label: str  # menu and layer label
    layer: str  # default layer name (an object preset: the new object's name)
    domain: Domain | None  # FACE or CORNER; None for an object preset
    units: str
    aggregation: Aggregation
    description: str  # one sentence, for the layer and the group
    inputs: tuple[PresetInput, ...] = ()
    palette: str = "viridis"
    display_range: tuple[float, float] | None = None  # fixed ramp range, else automatic
    north: bool = False  # has a Convergence input
    cost: str = "light"  # "light", "medium", "heavy": a hint in the menu and the docs
    parameters: Mapping[str, object] = field(default_factory=dict)  # extra provenance
    output: Output = Output.LAYER
    attributes: tuple[OutputAttribute, ...] = ()  # an object preset's attributes

    @property
    def attribute(self) -> str:
        """The attribute the preset stores: ``gt_<layer>``, or an object preset's first."""
        if self.output is Output.OBJECT:
            return self.attributes[0].name
        return attribute_name(self.layer)

    @property
    def object_inputs(self) -> tuple[PresetInput, ...]:
        return tuple(i for i in self.inputs if i.kind == "OBJECT")

    @property
    def active_only(self) -> bool:
        """Whether only the active object is the target (the other selected ones are inputs).

        True for object presets and for layer presets that take a mesh, line,
        feature or set of points from the selection; a preset whose only
        object input is a POINT (an Empty) goes on every selected mesh.
        """
        return self.output is Output.OBJECT or any(
            i.fill not in (Fill.NONE, Fill.POINT) for i in self.object_inputs
        )


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


# --- inputs of the second batch -----------------------------------------------------------

_OCCLUDERS = PresetInput(
    "Occluders",
    "COLLECTION",
    None,
    "Other objects that block sight as well as the surface (buildings, vegetation); optional",
)
_VIEW_RAY_OFFSET = PresetInput(
    "Ray Offset",
    "FLOAT",
    0.01,
    "Sight lines start this far from the surface, so it does not block itself",
    min=0.0,
    subtype="DISTANCE",
)
_OBSERVER_HEIGHT = PresetInput(
    "Observer Height",
    "FLOAT",
    1.6,
    "Eye height above each observer point",
    min=0.0,
    subtype="DISTANCE",
)
_TARGET_HEIGHT = PresetInput(
    "Target Height",
    "FLOAT",
    0.0,
    "Height above the surface that must be seen (0: the surface itself, seen from the front only)",
    min=0.0,
    subtype="DISTANCE",
)
_VIEW_DISTANCE = PresetInput(
    "Max Distance",
    "FLOAT",
    1000.0,
    "Horizontal reach of the view; faces further from every observer are not visible "
    "(viewshed: no data)",
    min=0.0,
    subtype="DISTANCE",
)
_DRAPE = PresetInput(
    "Drape",
    "BOOLEAN",
    False,
    "Drop the points vertically onto the topmost surface first (for 2D points, such as "
    "imported vector points at height 0)",
)


def _surface_input(description: str = "The surface the lines are traced on") -> PresetInput:
    return PresetInput("Surface", "OBJECT", None, description, fill=Fill.SURFACE)


_VISIBLE_EDGE = OutputAttribute(
    "visible", "EDGE", "ratio", "1 where the sight line is clear, 0 where it is blocked"
)


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
    # --- visibility ---------------------------------------------------------------------
    Preset(
        id="viewshed",
        version=1,
        group="GeoTools Viewshed",
        category=Category.VISIBILITY,
        label="Viewshed",
        layer="viewshed",
        domain=Domain.FACE,
        units="ratio",
        aggregation=Aggregation.AREA_MEAN,
        description="Whether each face can be seen from the observer: 1 visible, 0 hidden "
        "(behind something, or facing away), no data beyond Max Distance",
        inputs=(
            PresetInput(
                "Observer",
                "OBJECT",
                None,
                "The observer: this object's origin (Add Analysis uses a selected Empty, or "
                "adds one at the 3D cursor)",
                fill=Fill.POINT,
            ),
            _OBSERVER_HEIGHT,
            _TARGET_HEIGHT,
            _VIEW_DISTANCE,
            _VIEW_RAY_OFFSET,
            _OCCLUDERS,
        ),
        palette="greys",
        display_range=(0.0, 1.0),
    ),
    Preset(
        id="viewshed_cumulative",
        version=1,
        group="GeoTools Cumulative Viewshed",
        category=Category.VISIBILITY,
        label="Cumulative Viewshed",
        layer="viewshed_cumulative",
        domain=Domain.FACE,
        units="count",
        aggregation=Aggregation.AREA_MEAN,
        description="How many of the observer points see each face (0 where none does)",
        inputs=(
            PresetInput(
                "Observers",
                "OBJECT",
                None,
                "The observers: this object's vertices (or its origin if it has none)",
                fill=Fill.POINTS,
            ),
            _OBSERVER_HEIGHT,
            _TARGET_HEIGHT,
            _VIEW_DISTANCE,
            _VIEW_RAY_OFFSET,
            _DRAPE,
            _OCCLUDERS,
        ),
        palette="magma",
        cost="medium",
    ),
    Preset(
        id="intervisibility",
        version=1,
        group="GeoTools Intervisibility",
        category=Category.VISIBILITY,
        label="Intervisibility",
        layer="intervisibility",
        domain=None,
        units="ratio",
        aggregation=Aggregation.NONE,
        description="A line between every pair of points, eye to eye, marked visible (1) "
        "or blocked (0) by the surface",
        inputs=(
            PresetInput(
                "Points",
                "OBJECT",
                None,
                "The points: this object's vertices (or its origin if it has none)",
                fill=Fill.POINTS,
            ),
            _surface_input("The surface that blocks sight"),
            PresetInput(
                "Height",
                "FLOAT",
                1.6,
                "Eye height above each point, at both ends of each line",
                min=0.0,
                subtype="DISTANCE",
            ),
            _VIEW_RAY_OFFSET,
            _DRAPE,
            _OCCLUDERS,
        ),
        palette="greys",
        output=Output.OBJECT,
        attributes=(
            _VISIBLE_EDGE,
            OutputAttribute("distance", "EDGE", "m", "Length of the sight line, eye to eye"),
        ),
    ),
    Preset(
        id="line_of_sight",
        version=1,
        group="GeoTools Line of Sight",
        category=Category.VISIBILITY,
        label="Line of Sight",
        layer="line_of_sight",
        domain=None,
        units="ratio",
        aggregation=Aggregation.NONE,
        description="The ground profile along a line, marked visible or hidden from an "
        "observer at its first vertex, and the sight line to its last vertex, split at "
        "the first obstruction",
        inputs=(
            PresetInput(
                "Line",
                "OBJECT",
                None,
                "The line: observer at its first vertex, target at its last",
                fill=Fill.LINE,
            ),
            _surface_input("The surface the profile is taken on, and that blocks sight"),
            _OBSERVER_HEIGHT,
            PresetInput(
                "Target Height",
                "FLOAT",
                0.0,
                "Height above the ground that must be seen, along the profile and at the target",
                min=0.0,
                subtype="DISTANCE",
            ),
            PresetInput(
                "Spacing",
                "FLOAT",
                0.5,
                "Distance between profile points along the line",
                min=0.001,
                subtype="DISTANCE",
            ),
            _VIEW_RAY_OFFSET,
            _OCCLUDERS,
        ),
        palette="greys",
        output=Output.OBJECT,
        attributes=(
            OutputAttribute(
                "visible",
                "POINT",
                "ratio",
                "1 where the point is seen from the observer's eye, 0 where it is hidden "
                "(on edges: the mean of the two ends)",
            ),
            OutputAttribute(
                "obstruction",
                "POINT",
                "ratio",
                "1 at the first point where the sight line to the target is blocked",
            ),
            OutputAttribute("part", "POINT", "", "0 on the ground profile, 1 on the sight line"),
        ),
    ),
    # --- change and distance ------------------------------------------------------------
    Preset(
        id="surface_distance",
        version=1,
        group="GeoTools Surface Distance",
        category=Category.CHANGE,
        label="Distance to Surface",
        layer="surface_distance",
        domain=Domain.CORNER,
        units="m",
        aggregation=Aggregation.AREA_MEAN,
        description="Signed distance from each vertex to the nearest point of the "
        "reference surface, in metres: positive on the side its normals face (above), "
        "negative behind (below)",
        inputs=(
            PresetInput(
                "Reference",
                "OBJECT",
                None,
                "The surface measured to (for change: the earlier survey)",
                fill=Fill.MESH,
            ),
            PresetInput(
                "Max Distance",
                "FLOAT",
                1.0,
                "Farther than this from the reference is no data (beyond its edge, or no overlap)",
                min=0.0,
                subtype="DISTANCE",
            ),
        ),
        palette="diverging_rdbu",
    ),
    Preset(
        id="feature_distance",
        version=1,
        group="GeoTools Feature Distance",
        category=Category.CHANGE,
        label="Distance to Feature",
        layer="feature_distance",
        domain=Domain.CORNER,
        units="m",
        aggregation=Aggregation.AREA_MEAN,
        description="Distance from each vertex to the nearest point, line or polygon of "
        "a feature object (such as imported vectors), in metres",
        inputs=(
            PresetInput(
                "Feature",
                "OBJECT",
                None,
                "The feature: its vertices, edges and faces (curves too)",
                fill=Fill.FEATURE,
            ),
            PresetInput(
                "Horizontal",
                "BOOLEAN",
                True,
                "Measure in plan (ignore heights), as a GIS buffer does; off: in 3D",
            ),
        ),
        palette="viridis",
    ),
    # --- volumes -------------------------------------------------------------------------
    Preset(
        id="cut_fill",
        version=1,
        group="GeoTools Cut and Fill",
        category=Category.VOLUMES,
        label="Cut and Fill",
        layer="cut_fill",
        domain=Domain.FACE,
        units="m",
        aggregation=Aggregation.AREA_MEAN,
        description="Height of each face above the reference surface vertically below or "
        "above it, in metres: positive fill, negative cut; no data outside the reference",
        inputs=(
            PresetInput(
                "Reference",
                "OBJECT",
                None,
                "The surface compared with (for earthworks: the existing ground)",
                fill=Fill.MESH,
            ),
        ),
        palette="diverging_rdbu",
    ),
    Preset(
        id="closed_volume",
        version=1,
        group="GeoTools Closed Volume",
        category=Category.VOLUMES,
        label="Closed Volume",
        layer="closed_volume",
        domain=Domain.FACE,
        units="m3",
        aggregation=Aggregation.NONE,
        description="Volume enclosed by each closed part of the mesh, in cubic metres, on "
        "every face of that part; no data on parts that are not closed",
        palette="viridis",
    ),
    # --- paths and flow -----------------------------------------------------------------
    Preset(
        id="least_cost_path",
        version=1,
        group="GeoTools Least-Cost Path",
        category=Category.PATHS,
        label="Least-Cost Path",
        layer="least_cost_path",
        domain=None,
        units="s",
        aggregation=Aggregation.NONE,
        description="The quickest walking route along the surface's edges from Start to "
        "End (Tobler's hiking function), with its travel time",
        inputs=(
            _surface_input("The surface walked on"),
            PresetInput(
                "Start",
                "OBJECT",
                None,
                "Where the path starts: the surface vertex nearest this object's origin",
                fill=Fill.POINT,
            ),
            PresetInput(
                "End",
                "OBJECT",
                None,
                "Where the path ends: the surface vertex nearest this object's origin",
                fill=Fill.POINT,
            ),
            PresetInput(
                "Max Slope",
                "FLOAT",
                30.0,
                "Edges steeper than this many degrees cannot be walked",
                min=0.0,
                max=90.0,
            ),
            PresetInput(
                "Cost Attribute",
                "STRING",
                "",
                "A FACE attribute (a layer's gt_<name>) that multiplies the walking time "
                "of edges around each face; no data or 0 is impassable; empty for none",
            ),
        ),
        palette="viridis",
        cost="medium",
        output=Output.OBJECT,
        attributes=(
            OutputAttribute("elapsed", "POINT", "s", "Walking time from the start to this point"),
            OutputAttribute("time", "EDGE", "s", "Walking time along the edge"),
        ),
    ),
    Preset(
        id="flow_path",
        version=1,
        group="GeoTools Flow Path",
        category=Category.PATHS,
        label="Flow Path (Quick Look)",
        layer="flow_path",
        domain=None,
        units="",
        aggregation=Aggregation.NONE,
        description="A quick look at where water would run: from each seed, steps of "
        "fixed length down the steepest slope until a pit, a flat or the edge. Not "
        "hydrology",
        inputs=(
            _surface_input("The surface the water runs on"),
            PresetInput(
                "Seeds",
                "OBJECT",
                None,
                "Where the paths start: this object's vertices (or its origin)",
                fill=Fill.POINTS,
            ),
            PresetInput(
                "Step",
                "FLOAT",
                0.1,
                "Horizontal length of each step downhill",
                min=0.0001,
                subtype="DISTANCE",
            ),
            PresetInput(
                "Max Steps", "INT", 1000, "Most steps taken from each seed", min=1, max=100000
            ),
            PresetInput(
                "Flat Slope",
                "FLOAT",
                0.5,
                "A path stops where the surface is flatter than this many degrees",
                min=0.0,
                max=90.0,
            ),
        ),
        palette="viridis",
        cost="medium",
        output=Output.OBJECT,
        attributes=(
            OutputAttribute("step", "POINT", "", "Steps from the seed (0 at the seed)"),
            OutputAttribute("seed", "POINT", "", "Index of the seed the path starts from"),
        ),
    ),
)


PLANNED: tuple[tuple[str, str], ...] = (
    ("Surface reading", "ambient occlusion and curvature bakes for worn surfaces"),
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
    if preset.output is not Output.LAYER or preset.domain is None:
        raise ValueError(f"{preset.id} makes an object, not a layer")
    parameters = _parameters(preset, inputs, objects)
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


def object_provenance(
    preset: Preset,
    *,
    inputs: Mapping[str, object] | None = None,
    surface: str = "",
    geotools_version: str = "",
) -> dict[str, object]:
    """The provenance an object preset's new object records (``PROVENANCE_KEY``)."""
    parameters = _parameters(preset, inputs, (surface,) if surface else ())
    parameters["attributes"] = {
        a.name: {"domain": a.domain, "units": a.units, "description": a.description}
        for a in preset.attributes
    }
    return make_provenance(TOOL, parameters, geotools_version)


def _parameters(
    preset: Preset, inputs: Mapping[str, object] | None, objects: tuple[str, ...]
) -> dict[str, object]:
    return {
        "preset": preset.id,
        "preset_version": preset.version,
        "node_group": preset.group,
        "library": LIBRARY_FILE,
        "inputs": dict(inputs or {}),
        "objects": list(objects),
        **preset.parameters,
    }


def preset_of(provenance: Mapping[str, object] | None) -> str | None:
    """The preset id a layer's provenance names, or None for other layers."""
    if not provenance or provenance.get("tool") != TOOL:
        return None
    parameters = provenance.get("parameters")
    if not isinstance(parameters, Mapping):
        return None
    preset = parameters.get("preset")
    return preset if isinstance(preset, str) else None
