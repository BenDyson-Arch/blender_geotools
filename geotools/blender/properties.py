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
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..core.issues import Issue, Severity
from ..core.placement import Placement, PlacementMode, Vec3

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
    """Settings for one build. Stored only; publishing (M2) uses them."""

    # ``name`` is the built-in PropertyGroup name, shown in the list.
    profile: EnumProperty(name="Profile", items=PROFILE_ITEMS, default="BALANCED")
    jobs: IntProperty(
        name="Jobs", description="Worker threads, 0 for automatic", default=0, min=0, soft_max=64
    )
    memory_budget_gib: FloatProperty(
        name="Memory Budget",
        description="Memory budget in GiB, 0 for the rusty-tiles default",
        default=0.0,
        min=0.0,
        soft_max=256.0,
        precision=1,
        unit="NONE",
    )
    output_path: StringProperty(
        name="Output", description="Where the .3tz archive is written", subtype="FILE_PATH"
    )


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


classes = (GeoToolsIssue, GeoToolsBuildProfile, GeoToolsProject)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools = PointerProperty(type=GeoToolsProject)


def unregister() -> None:
    if hasattr(bpy.types.Scene, "geotools"):
        del bpy.types.Scene.geotools
    _unregister_classes()
