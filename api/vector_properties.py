"""Shared Blender property definitions for vector-source addons."""

from __future__ import annotations

import bpy

_VECTOR_MODE_ITEMS = [
    (
        "PER_FEATURE",
        "One object per feature",
        "Each feature becomes a separate Blender object",
    ),
    (
        "BY_TYPE",
        "One object per geometry type",
        "All features of the same type are merged into one object",
    ),
    (
        "JOINED",
        "Single joined mesh",
        "All geometry is joined into one object",
    ),
]

_GPKG_MODE_ITEMS = [
    (
        "PER_FEATURE",
        "One object per feature",
        "Each feature becomes a separate Blender object (required for reload/write-back)",
    ),
    (
        "BY_TYPE",
        "One object per geometry type",
        "All features of the same type are merged (write-back not available)",
    ),
    (
        "JOINED",
        "Single joined mesh",
        "All geometry is joined into one object (write-back not available)",
    ),
]


def vector_mode_property(
    *, writeback_ready: bool = False, default: str = "PER_FEATURE"
):
    items = _GPKG_MODE_ITEMS if writeback_ready else _VECTOR_MODE_ITEMS
    return bpy.props.EnumProperty(name="Import Mode", items=items, default=default)


def offset_origin_property(default: bool = True):
    return bpy.props.BoolProperty(
        name="Offset to world origin",
        description="Subtract the dataset bounding box centroid from all coordinates",
        default=default,
    )


def store_custom_props_property(default: bool = False):
    return bpy.props.BoolProperty(
        name="Duplicate as custom properties",
        description="Also write numeric attributes as object custom properties (legacy)",
        default=default,
    )


def import_holes_property(default: bool = True):
    return bpy.props.BoolProperty(
        name="Import holes (interior rings)",
        description="Cut interior rings as holes in polygon faces",
        default=default,
    )
