from __future__ import annotations

from bpy.props import BoolProperty, EnumProperty, FloatProperty, StringProperty

from .common import register_analysis_addon, setting

TOOL_SPEC = register_analysis_addon(
    tool_id="attr_vis",
    label="Attribute Visualise",
    description="Map a numeric face attribute to a colour attribute for viewport display",
    icon="RESTRICT_COLOR_OFF",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_attr_vis",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_visualise_attribute",
    ),
    settings=(
        setting(
            "vis_source",
            EnumProperty(
                name="Source",
                description="Where to read the value from",
                items=[
                    (
                        "FACE_ATTR",
                        "Face Attribute",
                        "Read a per-face mesh attribute (slope_deg, aspect_deg, roughness …)",
                    ),
                    (
                        "OBJECT_PROP",
                        "Object Property",
                        "Read a custom object property (e.g. los_visible set by the LoS tool)",
                    ),
                ],
                default="FACE_ATTR",
            ),
        ),
        setting(
            "vis_source_attr",
            StringProperty(
                name="Property / Attribute",
                description="Name of the face attribute or custom object property to visualise",
                default="",
            ),
        ),
        setting(
            "vis_output_attr",
            StringProperty(
                name="Output Attribute",
                description="Name for the output BYTE_COLOR attribute (blank = <source>_color)",
                default="",
            ),
        ),
        setting(
            "vis_mode",
            EnumProperty(
                name="Mode",
                description="How to map values to colours",
                items=[
                    (
                        "AUTO",
                        "Auto",
                        "FLOAT/BOOLEAN → Ramp; INT with ≤16 distinct values → Categorical",
                    ),
                    (
                        "RAMP",
                        "Colour Ramp",
                        "Normalise value range and sample a continuous colour ramp",
                    ),
                    (
                        "CATEGORICAL",
                        "Categorical",
                        "Assign a distinct hue to each unique value (good for class codes)",
                    ),
                ],
                default="AUTO",
            ),
        ),
        setting(
            "vis_ramp",
            EnumProperty(
                name="Ramp",
                description="Colour ramp to apply in Ramp mode",
                items=[
                    (
                        "VIRIDIS",
                        "Viridis",
                        "Perceptually uniform dark-purple to yellow",
                    ),
                    ("PLASMA", "Plasma", "Purple to yellow via magenta"),
                    ("INFERNO", "Inferno", "Black to yellow via red"),
                    ("COOL_WARM", "Cool – Warm", "Diverging blue to red via grey"),
                    ("GREYSCALE", "Greyscale", "Black to white"),
                ],
                default="VIRIDIS",
            ),
        ),
        setting(
            "vis_invert",
            BoolProperty(
                name="Invert Ramp",
                description="Flip the ramp so high values map to the dark/cool end",
                default=False,
            ),
        ),
        setting(
            "vis_auto_range",
            BoolProperty(
                name="Auto Range",
                description="Automatically detect min/max from the attribute data",
                default=True,
            ),
        ),
        setting(
            "vis_range_min",
            FloatProperty(
                name="Min Value",
                description="Minimum value mapped to the start of the colour ramp",
                default=0.0,
            ),
        ),
        setting(
            "vis_range_max",
            FloatProperty(
                name="Max Value",
                description="Maximum value mapped to the end of the colour ramp",
                default=1.0,
            ),
        ),
    ),
    order=70,
)
