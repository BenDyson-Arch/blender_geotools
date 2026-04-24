"""
analysis/properties.py — PropertyGroups for analysis panel state.
"""

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup

# Registry of available analysis tools.
# Each entry: (identifier, label, description, icon)
# Add new tools here and implement their panel draw in panel.py.
ANALYSIS_TOOLS = [
    ("slope",     "Slope",
     "Angle between face normal and world Z (0=flat, 90=vertical)", "RNDCURVE"),
    ("aspect",    "Aspect",
     "Compass bearing of the downslope direction (0/360=N, 90=E)",  "ORIENTATION_GLOBAL"),
    ("roughness", "Roughness",
     "Scale-independent local relief relative to face size",        "MESH_GRID"),
    ("los",          "Line of Sight",
     "Check visibility from an observer to selected target objects",  "HIDE_OFF"),
    ("surface_tag",    "Surface Tag",
     "Write a boolean face attribute by slope range or face selection", "BRUSH_DATA"),
    ("attr_transfer",  "Attribute Transfer",
     "Copy a face attribute from one mesh to others via ray or nearest query", "COPYDOWN"),
    ("attr_vis",       "Attribute Visualise",
     "Map a numeric face attribute to a colour attribute for viewport display", "RESTRICT_COLOR_OFF"),
    ("shadow",         "Shadow Analysis",
     "Compute per-face shadow cast by terrain and obstacles from a given sun position", "LIGHT_SUN"),
]


def _tool_items(_self, _context):
    return [(id_, label, desc, icon, i)
            for i, (id_, label, desc, icon) in enumerate(ANALYSIS_TOOLS)]


class AnalysisSettings(PropertyGroup):
    """Per-scene analysis panel state."""

    active_tool: EnumProperty(
        name="Tool",
        description="Analysis tool to configure and run",
        items=_tool_items,
        default=0,
    )

    # ── Line-of-Sight settings ─────────────────────────────────────────
    los_observer: EnumProperty(
        name="Observer",
        description="Source of the observer position",
        items=[
            ("ACTIVE", "Active Object", "Use the active object's location"),
            ("CURSOR", "3D Cursor",     "Use the 3D cursor position"),
        ],
        default="ACTIVE",
    )
    los_observer_height: FloatProperty(
        name="Observer Height",
        description="Vertical offset above the observer origin (metres)",
        default=1.7,
        min=0.0,
        unit="LENGTH",
    )
    los_target_height: FloatProperty(
        name="Target Height",
        description="Vertical offset above each target object's origin (metres)",
        default=0.0,
        min=0.0,
        unit="LENGTH",
    )
    los_terrain_collection: StringProperty(
        name="Terrain Collection",
        description=(
            "Name of the collection that contains blocking geometry. "
            "Leave blank to use all visible meshes (observer and targets excluded)"
        ),
        default="",
    )

    # ── Surface Tag settings ────────────────────────────────────
    tag_mode: EnumProperty(
        name="Mode",
        description="How to select which faces get tagged",
        items=[
            ("SLOPE",     "Slope Range",       "Tag faces whose slope_deg falls within Min–Max"),
            ("SELECTION", "Edit Mode Selection", "Tag faces selected the last time in Edit Mode"),
        ],
        default="SLOPE",
    )
    tag_attribute_name: StringProperty(
        name="Attribute Name",
        description="Name of the boolean face attribute to write (e.g. 'walkable')",
        default="walkable",
    )
    tag_slope_min: FloatProperty(
        name="Min Slope",
        description="Lower bound of the tagged slope range in degrees (inclusive)",
        default=0.0,
        min=0.0,
        max=90.0,
        soft_max=45.0,
        precision=1,
    )
    tag_slope_max: FloatProperty(
        name="Max Slope",
        description="Upper bound of the tagged slope range in degrees (inclusive)",
        default=15.0,
        min=0.0,
        max=90.0,
        soft_max=45.0,
        precision=1,
    )
    tag_auto_slope: BoolProperty(
        name="Auto-compute Slope",
        description=(
            "Automatically run Slope computation if slope_deg is absent. "
            "Disable to require a prior Slope pass."
        ),
        default=True,
    )

    # ── Attribute Transfer settings ─────────────────────────────
    xfer_method: EnumProperty(
        name="Method",
        description="Spatial query used to match source and target faces",
        items=[
            ("RAYCAST", "Vertical Ray",
             "Cast a vertical ray from each target face centroid to hit the source"),
            ("NEAREST", "Nearest Face",
             "Find the closest point on the source mesh to each target face centroid"),
        ],
        default="RAYCAST",
    )
    xfer_ray_direction: EnumProperty(
        name="Ray Direction",
        description="Direction of the vertical ray cast from each target face centroid",
        items=[
            ("UP",   "Up",   "Cast upward (+Z) — use when source is above the target"),
            ("DOWN", "Down", "Cast downward (−Z) — use when source is below the target"),
        ],
        default="UP",
    )
    xfer_source_attr: StringProperty(
        name="Source Attribute",
        description="Name of the FACE attribute on the source (active) object to copy",
        default="",
    )
    xfer_dest_attr: StringProperty(
        name="Destination Attribute",
        description="Name of the FACE attribute to write on the target objects (blank = same as source)",
        default="",
    )
    xfer_max_distance: FloatProperty(
        name="Max Distance",
        description=(
            "Maximum search distance in scene units. "
            "For Raycast: maximum ray length. "
            "For Nearest: faces beyond this distance are skipped."
        ),
        default=1000.0,
        min=0.001,
        unit="LENGTH",
    )

    # ── Attribute Visualise settings ────────────────────────────
    vis_source: EnumProperty(
        name="Source",
        description="Where to read the value from",
        items=[
            ("FACE_ATTR",   "Face Attribute",
             "Read a per-face mesh attribute (slope_deg, aspect_deg, roughness …)"),
            ("OBJECT_PROP", "Object Property",
             "Read a custom object property (e.g. los_visible set by the LoS tool)"),
        ],
        default="FACE_ATTR",
    )
    vis_source_attr: StringProperty(
        name="Property / Attribute",
        description="Name of the face attribute or custom object property to visualise",
        default="",
    )
    vis_output_attr: StringProperty(
        name="Output Attribute",
        description="Name for the output BYTE_COLOR attribute (blank = <source>_color)",
        default="",
    )
    vis_mode: EnumProperty(
        name="Mode",
        description="How to map values to colours",
        items=[
            ("AUTO",        "Auto",
             "FLOAT/BOOLEAN → Ramp; INT with ≤16 distinct values → Categorical"),
            ("RAMP",        "Colour Ramp",
             "Normalise value range and sample a continuous colour ramp"),
            ("CATEGORICAL", "Categorical",
             "Assign a distinct hue to each unique value (good for class codes)"),
        ],
        default="AUTO",
    )
    vis_ramp: EnumProperty(
        name="Ramp",
        description="Colour ramp to apply in Ramp mode",
        items=[
            ("VIRIDIS",   "Viridis",    "Perceptually uniform dark-purple to yellow"),
            ("PLASMA",    "Plasma",     "Purple to yellow via magenta"),
            ("INFERNO",   "Inferno",    "Black to yellow via red"),
            ("COOL_WARM", "Cool – Warm", "Diverging blue to red via grey"),
            ("GREYSCALE", "Greyscale",  "Black to white"),
        ],
        default="VIRIDIS",
    )
    vis_invert: BoolProperty(
        name="Invert Ramp",
        description="Flip the ramp so high values map to the dark/cool end",
        default=False,
    )
    vis_auto_range: BoolProperty(
        name="Auto Range",
        description="Automatically detect min/max from the attribute data",
        default=True,
    )
    vis_range_min: FloatProperty(
        name="Min Value",
        description="Minimum value mapped to the start of the colour ramp",
        default=0.0,
    )
    vis_range_max: FloatProperty(
        name="Max Value",
        description="Maximum value mapped to the end of the colour ramp",
        default=1.0,
    )

    # ── Shadow Analysis settings ────────────────────────────────
    shadow_mode: EnumProperty(
        name="Mode",
        description="Instant shadow for a single moment, or daily shadow fraction",
        items=[
            ("INSTANT", "Instant Shadow",
             "Write a boolean in_shadow face attribute for a single sun position"),
            ("DAILY",   "Daily Shadow Fraction",
             "Sample solar positions across a time window; write shadow_fraction (0=sunny, 1=always shadowed)"),
        ],
        default="INSTANT",
    )
    shadow_source: EnumProperty(
        name="Sun Source",
        description="How to determine the sun direction (Instant mode only)",
        items=[
            ("MANUAL",   "Manual",   "Enter azimuth and elevation directly"),
            ("SUN_LAMP", "Sun Lamp", "Derive direction from a Blender Sun light object"),
            ("SOLAR",    "Solar",    "Compute from geographic coordinates, date and UTC hour"),
        ],
        default="MANUAL",
    )
    shadow_azimuth: FloatProperty(
        name="Azimuth",
        description="Sun azimuth in degrees clockwise from North (0=N, 90=E, 180=S, 270=W)",
        default=180.0,
        min=0.0,
        max=360.0,
        precision=1,
        subtype="ANGLE" if False else "NONE",  # keep as plain degrees
    )
    shadow_elevation: FloatProperty(
        name="Elevation",
        description="Sun elevation above the horizon in degrees (0=horizon, 90=zenith)",
        default=45.0,
        min=0.1,
        max=90.0,
        precision=1,
    )
    shadow_sun_object: StringProperty(
        name="Sun Lamp",
        description="Name of the Sun light object to read direction from",
        default="",
    )
    shadow_terrain_collection: StringProperty(
        name="Terrain Collection",
        description=(
            "Name of the collection containing shadow-casting geometry. "
            "Leave blank to use all visible meshes"
        ),
        default="",
    )
    shadow_attr_instant: StringProperty(
        name="Output Attribute",
        description="Name of the boolean FACE attribute to write (Instant mode)",
        default="in_shadow",
    )
    shadow_attr_daily: StringProperty(
        name="Output Attribute",
        description="Name of the float FACE attribute to write (Daily mode; 0=sunny, 1=always shadowed)",
        default="shadow_fraction",
    )
    # Solar / Daily parameters
    shadow_date: StringProperty(
        name="Date",
        description="Calendar date in YYYY-MM-DD format used for solar position computation",
        default="2026-06-21",
    )
    shadow_utc_offset: FloatProperty(
        name="UTC Offset",
        description=(
            "Hours ahead of UTC for your local time zone "
            "(e.g. +10 for AEST, +1 for CET, -5 for EST). "
            "All hour fields below are in local time"
        ),
        default=0.0,
        min=-12.0,
        max=14.0,
        precision=1,
    )
    shadow_hour_utc: FloatProperty(
        name="Local Hour",
        description="Local time of day as a decimal hour (Instant-Solar mode)",
        default=12.0,
        min=0.0,
        max=23.99,
        precision=2,
    )
    shadow_hour_start: FloatProperty(
        name="Start Hour",
        description="Begin of the daily sampling window in local decimal hours",
        default=6.0,
        min=0.0,
        max=23.99,
        precision=2,
    )
    shadow_hour_end: FloatProperty(
        name="End Hour",
        description="End of the daily sampling window in local decimal hours",
        default=18.0,
        min=0.0,
        max=23.99,
        precision=2,
    )
    shadow_num_steps: IntProperty(
        name="Steps",
        description="Number of equally-spaced time steps to sample between Start and End hour",
        default=12,
        min=2,
        max=96,
    )
    shadow_use_geoscene: BoolProperty(
        name="Use GeoScene Lat/Lon",
        description="Read geographic coordinates from the scene's GeoScene origin instead of the fields below",
        default=True,
    )
    shadow_lat: FloatProperty(
        name="Latitude",
        description="Geographic latitude in decimal degrees (positive = North)",
        default=51.5,
        min=-90.0,
        max=90.0,
        precision=4,
    )
    shadow_lon: FloatProperty(
        name="Longitude",
        description="Geographic longitude in decimal degrees (positive = East)",
        default=-0.1,
        min=-180.0,
        max=180.0,
        precision=4,
    )


_classes = (AnalysisSettings,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.geotools_analysis = bpy.props.PointerProperty(type=AnalysisSettings)


def unregister():
    del bpy.types.Scene.geotools_analysis
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
