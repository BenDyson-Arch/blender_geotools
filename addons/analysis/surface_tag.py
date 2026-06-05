from __future__ import annotations

from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .common import register_analysis_addon, setting

TOOL_SPEC = register_analysis_addon(
    tool_id="surface_tag",
    label="Surface Tag",
    description="Write a boolean face attribute by slope range or face selection",
    icon="BRUSH_DATA",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_surface_tag",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_tag_surface",
    ),
    settings=(
        setting(
            "tag_mode",
            EnumProperty(
                name="Mode",
                description="How to select which faces get tagged",
                items=[
                    (
                        "SLOPE",
                        "Slope Range",
                        "Tag faces whose slope_deg falls within Min–Max",
                    ),
                    (
                        "SELECTION",
                        "Edit Mode Selection",
                        "Tag faces selected the last time in Edit Mode",
                    ),
                ],
                default="SLOPE",
            ),
        ),
        setting(
            "tag_attribute_name",
            StringProperty(
                name="Attribute Name",
                description="Name of the boolean face attribute to write (e.g. 'walkable')",
                default="walkable",
            ),
        ),
        setting(
            "tag_slope_min",
            FloatProperty(
                name="Min Slope",
                description="Lower bound of the tagged slope range in degrees (inclusive)",
                default=0.0,
                min=0.0,
                max=90.0,
                soft_max=45.0,
                precision=1,
            ),
        ),
        setting(
            "tag_slope_max",
            FloatProperty(
                name="Max Slope",
                description="Upper bound of the tagged slope range in degrees (inclusive)",
                default=15.0,
                min=0.0,
                max=90.0,
                soft_max=45.0,
                precision=1,
            ),
        ),
        setting(
            "tag_auto_slope",
            BoolProperty(
                name="Auto-compute Slope",
                description=(
                    "Automatically run Slope computation if slope_deg is absent. "
                    "Disable to require a prior Slope pass."
                ),
                default=True,
            ),
        ),
    ),
    order=50,
)
