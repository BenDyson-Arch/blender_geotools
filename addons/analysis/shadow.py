from __future__ import annotations

from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    StringProperty,
)

from .common import register_analysis_addon, setting

TOOL_SPEC = register_analysis_addon(
    tool_id="shadow",
    label="Shadow Analysis",
    description="Compute per-face shadow cast by terrain and obstacles from a given sun position",
    icon="LIGHT_SUN",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_shadow",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_compute_shadow",
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_place_sun_lamp",
    ),
    settings=(
        setting(
            "shadow_mode",
            EnumProperty(
                name="Mode",
                description="Instant shadow for a single moment, or daily shadow fraction",
                items=[
                    (
                        "INSTANT",
                        "Instant Shadow",
                        "Write a boolean in_shadow face attribute for a single sun position",
                    ),
                    (
                        "DAILY",
                        "Daily Shadow Fraction",
                        "Sample solar positions across a time window; write shadow_fraction (0=sunny, 1=always shadowed)",
                    ),
                ],
                default="INSTANT",
            ),
        ),
        setting(
            "shadow_source",
            EnumProperty(
                name="Sun Source",
                description="How to determine the sun direction (Instant mode only)",
                items=[
                    ("MANUAL", "Manual", "Enter azimuth and elevation directly"),
                    (
                        "SUN_LAMP",
                        "Sun Lamp",
                        "Derive direction from a Blender Sun light object",
                    ),
                    (
                        "SOLAR",
                        "Solar",
                        "Compute from geographic coordinates, date and UTC hour",
                    ),
                ],
                default="MANUAL",
            ),
        ),
        setting(
            "shadow_azimuth",
            FloatProperty(
                name="Azimuth",
                description="Sun azimuth in degrees clockwise from North (0=N, 90=E, 180=S, 270=W)",
                default=180.0,
                min=0.0,
                max=360.0,
                precision=1,
                subtype="NONE",
            ),
        ),
        setting(
            "shadow_elevation",
            FloatProperty(
                name="Elevation",
                description="Sun elevation above the horizon in degrees (0=horizon, 90=zenith)",
                default=45.0,
                min=0.1,
                max=90.0,
                precision=1,
            ),
        ),
        setting(
            "shadow_sun_object",
            StringProperty(
                name="Sun Lamp",
                description="Name of the Sun light object to read direction from",
                default="",
            ),
        ),
        setting(
            "shadow_terrain_collection",
            StringProperty(
                name="Terrain Collection",
                description=(
                    "Name of the collection containing shadow-casting geometry. "
                    "Leave blank to use all visible meshes"
                ),
                default="",
            ),
        ),
        setting(
            "shadow_attr_instant",
            StringProperty(
                name="Output Attribute",
                description="Name of the boolean FACE attribute to write (Instant mode)",
                default="in_shadow",
            ),
        ),
        setting(
            "shadow_attr_daily",
            StringProperty(
                name="Output Attribute",
                description="Name of the float FACE attribute to write (Daily mode; 0=sunny, 1=always shadowed)",
                default="shadow_fraction",
            ),
        ),
        setting(
            "shadow_date",
            StringProperty(
                name="Date",
                description="Calendar date in YYYY-MM-DD format used for solar position computation",
                default="2026-06-21",
            ),
        ),
        setting(
            "shadow_utc_offset",
            FloatProperty(
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
            ),
        ),
        setting(
            "shadow_hour_utc",
            FloatProperty(
                name="Local Hour",
                description="Local time of day as a decimal hour (Instant-Solar mode)",
                default=12.0,
                min=0.0,
                max=23.99,
                precision=2,
            ),
        ),
        setting(
            "shadow_hour_start",
            FloatProperty(
                name="Start Hour",
                description="Begin of the daily sampling window in local decimal hours",
                default=6.0,
                min=0.0,
                max=23.99,
                precision=2,
            ),
        ),
        setting(
            "shadow_hour_end",
            FloatProperty(
                name="End Hour",
                description="End of the daily sampling window in local decimal hours",
                default=18.0,
                min=0.0,
                max=23.99,
                precision=2,
            ),
        ),
        setting(
            "shadow_num_steps",
            IntProperty(
                name="Steps",
                description="Number of equally-spaced time steps to sample between Start and End hour",
                default=12,
                min=2,
                max=96,
            ),
        ),
        setting(
            "shadow_use_geoscene",
            BoolProperty(
                name="Use GeoScene Lat/Lon",
                description="Read geographic coordinates from the scene's GeoScene origin instead of the fields below",
                default=True,
            ),
        ),
        setting(
            "shadow_lat",
            FloatProperty(
                name="Latitude",
                description="Geographic latitude in decimal degrees (positive = North)",
                default=51.5,
                min=-90.0,
                max=90.0,
                precision=4,
            ),
        ),
        setting(
            "shadow_lon",
            FloatProperty(
                name="Longitude",
                description="Geographic longitude in decimal degrees (positive = East)",
                default=-0.1,
                min=-180.0,
                max=180.0,
                precision=4,
            ),
        ),
    ),
    order=80,
)
