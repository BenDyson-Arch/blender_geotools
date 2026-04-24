"""
properties.py — Per-scene PropertyGroup for the 3D Tiles loader.
"""

import logging
from pathlib import Path

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
from bpy.types import PropertyGroup

log = logging.getLogger(__name__)


def _on_filepath_update(self, context):
    """Auto-scan the tileset when the filepath changes."""
    fp = Path(self.last_filepath).resolve()
    if not fp.is_file():
        self.total_content_nodes = 0
        self.error_min = 0.0
        self.error_max = 0.0
        return
    try:
        from . import traversal
        data = traversal.load_tileset(fp)
        min_e, max_e, total = traversal.scan_tileset(
            data["root"], base_path=fp.parent
        )
        self.error_min = min_e
        self.error_max = max_e
        self.total_content_nodes = total
        self.error_threshold = max_e
        log.info("Auto-scanned %s: %d nodes, ε %.4f–%.4f", fp.name, total, min_e, max_e)
    except Exception as exc:
        log.warning("Auto-scan failed for %s: %s", fp, exc)
        self.total_content_nodes = 0
        self.error_min = 0.0
        self.error_max = 0.0


class TILES3D_PG_scene(PropertyGroup):
    """Scene-level state written by ScanTileset, read by ImportTileset."""

    error_min: FloatProperty(
        name="Min geometricError",
        description="Lowest geometricError found in the tileset (finest detail)",
        default=0.0,
    )
    error_max: FloatProperty(
        name="Max geometricError",
        description="Highest geometricError found in the tileset (coarsest detail)",
        default=0.0,
    )
    total_content_nodes: IntProperty(
        name="Total Content Nodes",
        description="Total number of content-bearing nodes in the full tileset tree",
        default=0,
    )
    last_filepath: StringProperty(
        name="Last Tileset Path",
        description="Path to tileset.json — scans automatically on change",
        default="",
        subtype="FILE_PATH",
        update=_on_filepath_update,
    )
    error_threshold: FloatProperty(
        name="Threshold",
        description="Import tiles at or below this geometricError value",
        default=1.0,
        min=0.0,
        precision=4,
    )
    apply_transforms: BoolProperty(
        name="Apply Transforms",
        description="Apply tile world transforms (ENU decomposition + scene origin offset)",
        default=True,
    )
    join_mesh: BoolProperty(
        name="Join Mesh",
        description="Merge all tile objects into a single mesh after import",
        default=False,
    )


_classes = (TILES3D_PG_scene,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.tiles3d = bpy.props.PointerProperty(type=TILES3D_PG_scene)


def unregister():
    del bpy.types.Scene.tiles3d
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
