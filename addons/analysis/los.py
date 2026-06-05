from __future__ import annotations

from bpy.props import EnumProperty, FloatProperty, StringProperty

from .common import register_analysis_addon, setting

TOOL_SPEC = register_analysis_addon(
    tool_id="los",
    label="Line of Sight",
    description="Check visibility from an observer to selected target objects",
    icon="HIDE_OFF",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_los",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_compute_los",
    ),
    settings=(
        setting(
            "los_observer",
            EnumProperty(
                name="Observer",
                description="Source of the observer position",
                items=[
                    ("ACTIVE", "Active Object", "Use the active object's location"),
                    ("CURSOR", "3D Cursor", "Use the 3D cursor position"),
                ],
                default="ACTIVE",
            ),
        ),
        setting(
            "los_observer_height",
            FloatProperty(
                name="Observer Height",
                description="Vertical offset above the observer origin (metres)",
                default=1.7,
                min=0.0,
                unit="LENGTH",
            ),
        ),
        setting(
            "los_target_height",
            FloatProperty(
                name="Target Height",
                description="Vertical offset above each target object's origin (metres)",
                default=0.0,
                min=0.0,
                unit="LENGTH",
            ),
        ),
        setting(
            "los_terrain_collection",
            StringProperty(
                name="Terrain Collection",
                description=(
                    "Name of the collection that contains blocking geometry. "
                    "Leave blank to use all visible meshes (observer and targets excluded)"
                ),
                default="",
            ),
        ),
    ),
    order=40,
)
