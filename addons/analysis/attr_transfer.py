from __future__ import annotations

from bpy.props import EnumProperty, FloatProperty, StringProperty

from .common import register_analysis_addon, setting

TOOL_SPEC = register_analysis_addon(
    tool_id="attr_transfer",
    label="Attribute Transfer",
    description="Copy a face attribute from one mesh to others via ray or nearest query",
    icon="COPYDOWN",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_attr_transfer",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_transfer_attributes",
    ),
    settings=(
        setting(
            "xfer_method",
            EnumProperty(
                name="Method",
                description="Spatial query used to match source and target faces",
                items=[
                    (
                        "RAYCAST",
                        "Vertical Ray",
                        "Cast a vertical ray from each target face centroid to hit the source",
                    ),
                    (
                        "NEAREST",
                        "Nearest Face",
                        "Find the closest point on the source mesh to each target face centroid",
                    ),
                ],
                default="RAYCAST",
            ),
        ),
        setting(
            "xfer_ray_direction",
            EnumProperty(
                name="Ray Direction",
                description="Direction of the vertical ray cast from each target face centroid",
                items=[
                    (
                        "UP",
                        "Up",
                        "Cast upward (+Z) — use when source is above the target",
                    ),
                    (
                        "DOWN",
                        "Down",
                        "Cast downward (−Z) — use when source is below the target",
                    ),
                ],
                default="UP",
            ),
        ),
        setting(
            "xfer_source_attr",
            StringProperty(
                name="Source Attribute",
                description="Name of the FACE attribute on the source (active) object to copy",
                default="",
            ),
        ),
        setting(
            "xfer_dest_attr",
            StringProperty(
                name="Destination Attribute",
                description="Name of the FACE attribute to write on the target objects (blank = same as source)",
                default="",
            ),
        ),
        setting(
            "xfer_max_distance",
            FloatProperty(
                name="Max Distance",
                description=(
                    "Maximum search distance in scene units. "
                    "For Raycast: maximum ray length. "
                    "For Nearest: faces beyond this distance are skipped."
                ),
                default=1000.0,
                min=0.001,
                unit="LENGTH",
            ),
        ),
    ),
    order=60,
)
