"""
analysis/properties.py — Dynamic PropertyGroup for analysis panel state.
"""

import bpy
from bpy.props import EnumProperty
from bpy.types import PropertyGroup

from .registry import apply_tool_settings, tool_items


class AnalysisSettings(PropertyGroup):
    """Per-scene analysis panel state."""

    active_tool: EnumProperty(
        name="Tool",
        description="Analysis tool to configure and run",
        items=tool_items,
        default=0,
    )


apply_tool_settings(AnalysisSettings)

_classes = (AnalysisSettings,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.geotools_analysis = bpy.props.PointerProperty(type=AnalysisSettings)


def unregister():
    del bpy.types.Scene.geotools_analysis
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
