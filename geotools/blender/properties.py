"""Scene project properties (``scene.geotools``) and the bridge to ``core.placement``.

Precision: every geodetic or offset number is stored as text in a
``StringProperty`` and parsed with :func:`float`, so it stays a binary64 Python
float end to end. Blender's ``FloatProperty`` is single precision: a northing of
6,961,000 m keeps only about 0.5 m, and a longitude about 1 m. Writes go through
:func:`write_number`, which stores ``repr(value)``, the shortest text that
parses back to exactly the same float.

:func:`placement_from_props` is the only place a ``Placement`` is built from
these properties.
"""

import math
from dataclasses import dataclass

import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..core import images as core_images
from ..core.images import ImageRemedy, RemedyKind
from ..core.issues import Issue, Severity
from ..core.layers import Aggregation, DataType, Domain, Origin
from ..core.placement import Placement, PlacementMode, Vec3
from ..core.presentation import Blend, Target
from ..core.ramps import DEFAULT_PALETTE, PALETTES

# --- binary64 number fields ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NumberField:
    attr: str
    label: str
    description: str


SOURCE_CRS_FIELDS: tuple[NumberField, ...] = (
    NumberField("source_offset_e", "Offset E", "Source offset easting, in CRS units"),
    NumberField("source_offset_n", "Offset N", "Source offset northing, in CRS units"),
    NumberField("source_offset_a", "Offset A", "Source offset altitude, in metres"),
    NumberField(
        "height_offset",
        "Height Offset",
        "Metres added to height plus offset A to get the ellipsoidal height",
    ),
)

ANCHOR_FIELDS: tuple[NumberField, ...] = (
    NumberField("anchor_lon", "Longitude", "Anchor longitude, WGS84 degrees"),
    NumberField("anchor_lat", "Latitude", "Anchor latitude, WGS84 degrees"),
    NumberField("anchor_height", "Height", "Anchor ellipsoidal height, metres"),
    NumberField(
        "heading", "Heading", "Rotation about local up, degrees counter-clockwise from east"
    ),
    NumberField("scene_offset_x", "Scene Offset E", "Scene offset east, metres"),
    NumberField("scene_offset_y", "Scene Offset N", "Scene offset north, metres"),
    NumberField("scene_offset_z", "Scene Offset U", "Scene offset up, metres"),
)

NUMBER_FIELDS: dict[str, NumberField] = {f.attr: f for f in SOURCE_CRS_FIELDS + ANCHOR_FIELDS}

FIELDS_BY_MODE: dict[PlacementMode, tuple[NumberField, ...]] = {
    PlacementMode.LOCAL: (),
    PlacementMode.ANCHOR: ANCHOR_FIELDS,
    PlacementMode.SOURCE_CRS: SOURCE_CRS_FIELDS,
}


class FieldError(ValueError):
    """A number field does not hold a finite number."""

    def __init__(self, field: NumberField, text: str, reason: str) -> None:
        super().__init__(
            f"{field.label} {reason}: {text!r}" if text.strip() else f"{field.label} {reason}"
        )
        self.field = field
        self.text = text


def parse_number(field: NumberField, text: str) -> float:
    """``float(text)``, refusing empty, non-numeric and non-finite text."""
    if not text.strip():
        raise FieldError(field, text, "is empty")
    try:
        value = float(text)
    except ValueError:
        raise FieldError(field, text, "is not a number") from None
    if not math.isfinite(value):
        raise FieldError(field, text, "must be finite")
    return value


def read_number(props: "GeoToolsProject", attr: str) -> float:
    """The binary64 value of a number field; raises :class:`FieldError`."""
    return parse_number(NUMBER_FIELDS[attr], getattr(props, attr))


def write_number(props: "GeoToolsProject", attr: str, value: float) -> None:
    """Store ``value`` exactly: ``repr`` of a float parses back to the same float."""
    if attr not in NUMBER_FIELDS:
        raise KeyError(attr)
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{NUMBER_FIELDS[attr].label} must be finite, got {value!r}")
    setattr(props, attr, repr(value))


def number_is_valid(props: "GeoToolsProject", attr: str) -> bool:
    try:
        read_number(props, attr)
    except FieldError:
        return False
    return True


def placement_mode(props: "GeoToolsProject") -> PlacementMode:
    return PlacementMode(props.placement_mode.lower())


def field_issues(props: "GeoToolsProject") -> list[Issue]:
    """One error per number field of the active mode that does not parse."""
    issues = []
    for field in FIELDS_BY_MODE[placement_mode(props)]:
        try:
            read_number(props, field.attr)
        except FieldError as ex:
            issues.append(Issue(Severity.ERROR, "invalid-number", f"{ex}.", field.label))
    return issues


def placement_from_props(props: "GeoToolsProject") -> Placement:
    """Build the ``core.placement.Placement`` these properties describe.

    Only the active mode's fields are read; the others keep their defaults.
    Raises :class:`FieldError` (a ``ValueError``) if one of them is not a number.
    """
    mode = placement_mode(props)
    if mode is PlacementMode.ANCHOR:
        n = {f.attr: read_number(props, f.attr) for f in ANCHOR_FIELDS}
        return Placement(
            mode=mode,
            anchor=(n["anchor_lon"], n["anchor_lat"], n["anchor_height"]),
            heading_degrees=n["heading"],
            scene_offset=(n["scene_offset_x"], n["scene_offset_y"], n["scene_offset_z"]),
        )
    if mode is PlacementMode.SOURCE_CRS:
        n = {f.attr: read_number(props, f.attr) for f in SOURCE_CRS_FIELDS}
        return Placement(
            mode=mode,
            source_crs=props.source_crs.strip(),
            source_offset=(n["source_offset_e"], n["source_offset_n"], n["source_offset_a"]),
            height_offset=n["height_offset"],
        )
    return Placement(mode=PlacementMode.LOCAL)


def placement_from_scene(scene: bpy.types.Scene) -> Placement:
    return placement_from_props(scene.geotools)


def apply_source_offset(props: "GeoToolsProject", offset: Vec3, path: str = "") -> None:
    """Switch to SOURCE_CRS and store an offset, e.g. from an offset file.

    The CRS is left as it is: an offset file does not say which CRS it is in.
    """
    props.placement_mode = "SOURCE_CRS"
    write_number(props, "source_offset_e", offset[0])
    write_number(props, "source_offset_n", offset[1])
    write_number(props, "source_offset_a", offset[2])
    props.offset_file = path


def format_vec(v: Vec3) -> str:
    """Exact text for a vector, read back by :func:`parse_vec`."""
    return ", ".join(repr(float(c)) for c in v)


def parse_vec(text: str) -> Vec3 | None:
    if not text:
        return None
    x, y, z = (float(part) for part in text.split(","))
    return (x, y, z)


# --- property groups ---------------------------------------------------------------


def _number(field: NumberField) -> StringProperty:
    return StringProperty(
        name=field.label,
        description=f"{field.description}. Stored as text so the full double precision is kept",
        default="0",
    )


SEVERITY_ITEMS = (
    ("ERROR", "Error", "Publishing would fail or be wrong"),
    ("WARNING", "Warning", "Publishing works but something is dropped or degraded"),
    ("INFO", "Info", "For information"),
)

SEVERITY_ICONS = {"ERROR": "CANCEL", "WARNING": "ERROR", "INFO": "INFO"}


class GeoToolsIssue(bpy.types.PropertyGroup):
    """One problem found by Run Checks."""

    severity: EnumProperty(name="Severity", items=SEVERITY_ITEMS, default="INFO")
    code: StringProperty(name="Code")
    message: StringProperty(name="Message")
    subject: StringProperty(name="Subject")


PROFILE_ITEMS = (
    ("FAST", "Fast", "Quickest build, lossy"),
    ("BALANCED", "Balanced", "Balance between build time and quality"),
    ("QUALITY", "Quality", "Best lossy quality, slower"),
    ("LOSSLESS", "Lossless", "No lossy compression"),
)


class GeoToolsBuildProfile(bpy.types.PropertyGroup):
    """Settings for one build, and a summary of its last publish."""

    # ``name`` is the built-in PropertyGroup name, shown in the list.
    profile: EnumProperty(name="Profile", items=PROFILE_ITEMS, default="BALANCED")
    jobs: IntProperty(
        name="Jobs", description="Worker threads, 0 for automatic", default=0, min=0, soft_max=64
    )
    memory_budget_gib: FloatProperty(
        name="Memory Budget",
        description="Memory budget for rusty-tiles in GiB. 0 for Auto: 60% of the memory "
        "available when publishing starts, minus 4 GiB for Blender",
        default=0.0,
        min=0.0,
        soft_max=256.0,
        precision=1,
        unit="NONE",
    )
    output_path: StringProperty(
        name="Output", description="Where the .3tz archive is written", subtype="FILE_PATH"
    )
    overwrite: BoolProperty(
        name="Overwrite",
        description="Replace an existing archive at the output path. The old one is kept "
        "if the build fails or is cancelled",
        default=False,
    )
    validate: BoolProperty(
        name="Validate",
        description="Validate the archive with rusty-tiles after building",
        default=True,
    )
    variant_layer: StringProperty(
        name="Variant Layer",
        description="Publish a variant tileset with this layer presented, as "
        "<output>.<layer>.3tz. Empty for the plain tileset",
        default="",
    )

    # Last successful publish, written by geotools.publish (never while drawing).
    last_output: StringProperty(name="Last Output", subtype="FILE_PATH")
    last_record: StringProperty(name="Last Build Record", subtype="FILE_PATH")
    last_build_utc: StringProperty(name="Last Build Time")
    last_seconds: FloatProperty(name="Last Build Seconds", default=0.0)
    last_validation: EnumProperty(
        name="Last Validation",
        items=(
            ("NONE", "Not Run", "Validation was off or skipped"),
            ("OK", "Passed", "rusty-tiles validated the archive"),
            ("FAILED", "Failed", "rusty-tiles validation failed; see the build record"),
        ),
        default="NONE",
    )
    # Text, not IntProperty: counts can pass 2**31 on very large sources.
    last_triangles: StringProperty(name="Last Triangles")
    last_tiles: StringProperty(name="Last Tiles")


# --- image remedies -------------------------------------------------------------------

REMEDY_ITEMS = (
    ("NONE", "None", "Publish the image as it is"),
    (
        "JPEG",
        "JPEG",
        (
            "Publish a JPEG copy at the chosen quality. Lossy: small next to the fast, "
            "balanced and quality profiles' own compression, but it defeats the lossless "
            "profile"
        ),
    ),
    (
        "DOWNSCALE",
        "Downscale",
        (
            "Publish a copy scaled down to the max edge, as PNG, or as JPEG at the chosen "
            "quality if the PNG is still over 32 MiB"
        ),
    ),
    ("CONVERT_8BIT", "8-bit", "Publish an 8-bit PNG copy of a 16-bit image"),
)


class GeoToolsImageRemedy(bpy.types.PropertyGroup):
    """How GeoTools makes an image publishable (``image.geotools_remedy``).

    Stored on the image so it follows renames, save and reload, and appending
    into another file. It is only read when exporting: the image itself, its
    file and its pixels are never changed.
    """

    kind: EnumProperty(name="Remedy", items=REMEDY_ITEMS, default="NONE")
    quality: IntProperty(
        name="JPEG Quality",
        description="JPEG quality of the published copy (and of the downscale JPEG fallback)",
        default=core_images.JPEG_QUALITY_DEFAULT,
        min=core_images.JPEG_QUALITY_MIN,
        max=core_images.JPEG_QUALITY_MAX,
    )
    max_edge: IntProperty(
        name="Max Edge",
        description="Longest side of the downscaled copy, in pixels",
        default=core_images.DOWNSCALE_FALLBACK_EDGE,
        min=core_images.DOWNSCALE_MIN_EDGE,
        max=16_384,
        subtype="PIXEL",
    )


def image_remedy(image: bpy.types.Image) -> ImageRemedy:
    """The remedy stored on ``image``, or none."""
    props = getattr(image, "geotools_remedy", None)
    if props is None or props.kind == "NONE":
        return core_images.NO_REMEDY
    return ImageRemedy(RemedyKind(props.kind), quality=props.quality, max_edge=props.max_edge)


class GeoToolsImageInfo(bpy.types.PropertyGroup):
    """One source image as the last summary saw it, for the Images panel."""

    # ``name`` is the image's name.
    width: IntProperty()
    height: IntProperty()
    file_format: StringProperty()
    file_bytes: FloatProperty(default=-1.0)  # the file used as is, -1 if none
    bit_depth: IntProperty(default=0)  # 0 if not known


# --- layers ---------------------------------------------------------------------------
# The registry of docs/design/m3-layers.md. ``blender.layers.spec_from_props`` is
# the only place a ``core.layers.LayerSpec`` is built from these properties.

LAYER_DOMAIN_ITEMS = (
    (Domain.FACE.value, "Face", "One value per face, in the gt_<name> attribute"),
    (Domain.CORNER.value, "Corner", "One value per face corner, in the gt_<name> attribute"),
    (Domain.TEXTURE.value, "Texture", "One value per texel, in a float image per object"),
)

LAYER_DATA_TYPE_ITEMS = (
    (DataType.FLOAT.value, "Float", "A real number (32-bit float)"),
    (DataType.INT.value, "Integer", "A whole number (32-bit integer)"),
    (DataType.BOOLEAN.value, "Boolean", "True or false"),
    (DataType.CATEGORY.value, "Category", "An integer index into the layer's category labels"),
)

LAYER_ORIGIN_ITEMS = (
    (Origin.GEONODES.value, "Geometry Nodes", "Baked from a Geometry Nodes output"),
    (Origin.ANALYSIS.value, "Analysis", "Written by a GeoTools analysis tool"),
    (Origin.MANUAL.value, "Manual", "Made or edited by hand"),
)

LAYER_AGGREGATION_DEFAULT = "DEFAULT"
"""The ``aggregation`` item that means ``None``: the default for the data type."""

LAYER_AGGREGATION_ITEMS = (
    (
        LAYER_AGGREGATION_DEFAULT,
        "Default",
        "Area-weighted mean for float and integer layers, mode for boolean and category layers",
    ),
    (Aggregation.AREA_MEAN.value, "Area Mean", "Coarse levels take the area-weighted mean"),
    (Aggregation.MIN.value, "Minimum", "Coarse levels take the minimum (for booleans: all)"),
    (Aggregation.MAX.value, "Maximum", "Coarse levels take the maximum (for booleans: any)"),
    (Aggregation.MODE.value, "Mode", "Coarse levels take the most common value"),
    (Aggregation.NONE.value, "None", "Coarse levels carry no value"),
)


class GeoToolsLayerImage(bpy.types.PropertyGroup):
    """The image that holds a TEXTURE layer on one object."""

    object: PointerProperty(name="Object", type=bpy.types.Object)
    image: PointerProperty(name="Image", type=bpy.types.Image)


PRESENT_TARGET_ITEMS = (
    (
        Target.COLOR_0.value,
        "Vertex Colour",
        "Face and corner layers: the ramp goes into COLOR_0 (the gt_present attribute)",
    ),
    (
        Target.BASE_COLOR.value,
        "Base Colour",
        "Texture layers: the ramp becomes the base-colour texture",
    ),
    (
        Target.EMISSIVE.value,
        "Emission",
        "Texture layers: the ramp becomes the emissive texture",
    ),
)

PRESENT_BLEND_ITEMS = (
    (
        Blend.REPLACE.value,
        "Replace",
        "Show the ramp alone: the base colour becomes white (black for emission) with no texture",
    ),
    (
        Blend.MULTIPLY.value,
        "Multiply",
        "Keep the material: the ramp multiplies its base colour (emission adds on top)",
    ),
)

PALETTE_LABELS = {
    "viridis": "Viridis",
    "magma": "Magma",
    "cividis": "Cividis",
    "turbo": "Turbo",
    "diverging_rdbu": "Diverging Red-Blue",
    "greys": "Greys",
}

PALETTE_ITEMS = tuple(
    (name, PALETTE_LABELS.get(name, name.title()), f"The {PALETTE_LABELS.get(name, name)} ramp")
    for name in PALETTES
)


class GeoToolsCategoryColor(bpy.types.PropertyGroup):
    """The presentation colour of one category value (item i is value i)."""

    color: FloatVectorProperty(
        name="Colour", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0, default=(0.5, 0.5, 0.5)
    )


class GeoToolsLayerPresentation(bpy.types.PropertyGroup):
    """How a layer is made visible in a variant tileset (``GeoToolsLayer.presentation``).

    Read with ``blender.presentation.presentation_settings``. ``range_min`` and
    ``range_max`` are binary64 text, empty for automatic (``core.ramps.auto_range``).
    """

    target: EnumProperty(name="Target", items=PRESENT_TARGET_ITEMS, default=Target.COLOR_0.value)
    blend: EnumProperty(name="Blend", items=PRESENT_BLEND_ITEMS, default=Blend.REPLACE.value)
    palette: EnumProperty(name="Palette", items=PALETTE_ITEMS, default=DEFAULT_PALETTE)
    range_min: StringProperty(
        name="Ramp Minimum",
        description="Value at the low end of the ramp, stored as text so the full double "
        "precision is kept. Empty for automatic (2nd percentile of the values)",
        default="",
    )
    range_max: StringProperty(
        name="Ramp Maximum",
        description="Value at the high end of the ramp, stored as text so the full double "
        "precision is kept. Empty for automatic (98th percentile of the values)",
        default="",
    )
    nodata_color: FloatVectorProperty(
        name="No-Data Colour",
        description="Colour of faces, corners or texels without a value",
        subtype="COLOR_GAMMA",
        size=3,
        min=0.0,
        max=1.0,
        default=(0.5, 0.5, 0.5),
    )
    category_colors: CollectionProperty(type=GeoToolsCategoryColor)
    # The 8-bit images of a texture presentation, one per object, written by
    # blender.presentation.write_presentation (regenerated on every publish).
    images: CollectionProperty(type=GeoToolsLayerImage)


class GeoToolsLayer(bpy.types.PropertyGroup):
    """One layer of the registry; mirrors ``core.layers.LayerSpec``.

    ``name`` is the built-in PropertyGroup name: the layer's identifier. Rename
    a layer with ``geotools.layer_rename``, which renames its attributes too.
    Later work streams add their settings here (e.g. a ``presentation``
    PointerProperty); register their PropertyGroups before this class.
    """

    label: StringProperty(name="Label", description="Display name; empty to show the layer name")
    domain: EnumProperty(name="Domain", items=LAYER_DOMAIN_ITEMS, default=Domain.FACE.value)
    data_type: EnumProperty(name="Type", items=LAYER_DATA_TYPE_ITEMS, default=DataType.FLOAT.value)
    units: StringProperty(name="Units", description='Units of the values, e.g. "deg", "m", "h"')
    nodata: StringProperty(
        name="No-Data",
        description="Value that means no data, stored as text so the full double precision "
        "is kept. Empty for none (float layers then use NaN)",
        default="",
    )
    origin: EnumProperty(name="Origin", items=LAYER_ORIGIN_ITEMS, default=Origin.MANUAL.value)
    aggregation: EnumProperty(
        name="Aggregation",
        description="How coarse levels reduce the layer (recorded for queryable tiles)",
        items=LAYER_AGGREGATION_ITEMS,
        default=LAYER_AGGREGATION_DEFAULT,
    )
    description: StringProperty(name="Description")
    categories: StringProperty(
        name="Categories",
        description="Category labels, one per line: line i is the label of value i",
        default="",
    )
    # The range of the values present (binary64 text, empty if not known), set by
    # whoever writes the values or by blender.layers.update_value_range.
    value_min: StringProperty(
        name="Minimum",
        description="Smallest value present, ignoring no-data. Stored as text so the full "
        "double precision is kept; empty if not known",
        default="",
    )
    value_max: StringProperty(
        name="Maximum",
        description="Largest value present, ignoring no-data. Stored as text so the full "
        "double precision is kept; empty if not known",
        default="",
    )
    provenance: StringProperty(
        name="Provenance",
        description='What made the layer, as JSON: {"tool", "parameters", '
        '"geotools_version"}. Empty for a manual layer',
        default="",
    )

    # TEXTURE layers ------------------------------------------------------------
    images: CollectionProperty(type=GeoToolsLayerImage)
    uv_map: StringProperty(
        name="UV Map", description="UV map of the images; empty for the active render UV map"
    )
    resolution: IntProperty(
        name="Resolution",
        description="Width and height of each object's image, in pixels",
        default=1024,
        min=1,
        soft_min=64,
        max=16_384,
        subtype="PIXEL",
    )
    margin: IntProperty(
        name="Margin",
        description="Gutter around UV islands, in pixels",
        default=8,
        min=0,
        soft_max=64,
        subtype="PIXEL",
    )

    # Presentation (blender.presentation) ---------------------------------------
    presentation: PointerProperty(type=GeoToolsLayerPresentation)


PLACEMENT_MODE_ITEMS = (
    ("LOCAL", "Local", "Local metres, not placed on Earth"),
    ("ANCHOR", "Anchor", "Local metres placed rigidly at a WGS84 point"),
    ("SOURCE_CRS", "Source CRS", "Coordinates in a declared horizontal CRS plus an offset"),
)

PROBE_STATUS_ITEMS = (
    ("NONE", "Not Run", ""),
    ("OK", "OK", ""),
    ("FAILED", "Failed", ""),
)


class GeoToolsProject(bpy.types.PropertyGroup):
    """The GeoTools project of a scene."""

    # Placement ------------------------------------------------------------------
    placement_mode: EnumProperty(name="Placement", items=PLACEMENT_MODE_ITEMS, default="LOCAL")
    source_crs: StringProperty(
        name="CRS",
        description='Horizontal CRS of the source coordinates, e.g. "EPSG:32756", WKT or PROJ',
    )
    offset_file: StringProperty(
        name="Offset File", description="Offset file the source offset was read from"
    )
    # Number fields (binary64 text): see NUMBER_FIELDS.
    source_offset_e: _number(NUMBER_FIELDS["source_offset_e"])
    source_offset_n: _number(NUMBER_FIELDS["source_offset_n"])
    source_offset_a: _number(NUMBER_FIELDS["source_offset_a"])
    height_offset: _number(NUMBER_FIELDS["height_offset"])
    anchor_lon: _number(NUMBER_FIELDS["anchor_lon"])
    anchor_lat: _number(NUMBER_FIELDS["anchor_lat"])
    anchor_height: _number(NUMBER_FIELDS["anchor_height"])
    heading: _number(NUMBER_FIELDS["heading"])
    scene_offset_x: _number(NUMBER_FIELDS["scene_offset_x"])
    scene_offset_y: _number(NUMBER_FIELDS["scene_offset_y"])
    scene_offset_z: _number(NUMBER_FIELDS["scene_offset_z"])

    # Source ---------------------------------------------------------------------
    source_collection: PointerProperty(
        name="Source",
        type=bpy.types.Collection,
        description="Collection holding the full-detail source mesh",
    )
    source_filepath: StringProperty(
        name="Source File",
        description="File the source was last ingested from",
        subtype="FILE_PATH",
    )
    # Cached summary, refreshed by operators (never computed while drawing).
    summary_valid: BoolProperty(default=False)
    summary_objects: IntProperty(default=0)
    summary_meshes: IntProperty(default=0)
    summary_faces: IntProperty(default=0)
    summary_vertices: IntProperty(default=0)
    summary_bounds_min: StringProperty(default="")  # format_vec text, "" if no mesh
    summary_bounds_max: StringProperty(default="")
    summary_images: CollectionProperty(type=GeoToolsImageInfo)
    summary_images_index: IntProperty(default=0)

    # Checks ---------------------------------------------------------------------
    issues: CollectionProperty(type=GeoToolsIssue)
    issues_index: IntProperty(default=0)
    checks_run: BoolProperty(default=False)

    # Placement probe ------------------------------------------------------------
    probe_status: EnumProperty(items=PROBE_STATUS_ITEMS, default="NONE")
    probe_lon: StringProperty(default="")  # repr of a binary64
    probe_lat: StringProperty(default="")
    probe_height: StringProperty(default="")
    probe_operation: StringProperty(default="")
    probe_error: StringProperty(default="")
    probe_key: StringProperty(default="")  # repr of the Placement probed

    # Build profiles -------------------------------------------------------------
    build_profiles: CollectionProperty(type=GeoToolsBuildProfile)
    build_profile_index: IntProperty(name="Active Build Profile", default=0)

    # Layers (see blender.layers) ------------------------------------------------
    layers: CollectionProperty(type=GeoToolsLayer)
    layers_index: IntProperty(name="Active Layer", default=0)


classes = (
    GeoToolsIssue,
    GeoToolsBuildProfile,
    GeoToolsImageRemedy,
    GeoToolsImageInfo,
    GeoToolsLayerImage,
    GeoToolsCategoryColor,
    GeoToolsLayerPresentation,
    GeoToolsLayer,
    GeoToolsProject,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools = PointerProperty(type=GeoToolsProject)
    bpy.types.Image.geotools_remedy = PointerProperty(type=GeoToolsImageRemedy)


def unregister() -> None:
    if hasattr(bpy.types.Image, "geotools_remedy"):
        del bpy.types.Image.geotools_remedy
    if hasattr(bpy.types.Scene, "geotools"):
        del bpy.types.Scene.geotools
    _unregister_classes()
