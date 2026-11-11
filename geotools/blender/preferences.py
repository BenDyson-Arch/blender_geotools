"""Extension preferences: dependency status and the optional preview tools."""

from __future__ import annotations

import bpy
from bpy.props import StringProperty

from .common import ADDON_PACKAGE, draw_dependency_status


class GEOTOOLS_AddonPreferences(bpy.types.AddonPreferences):
    """GeoTools preferences."""

    bl_idname = ADDON_PACKAGE

    rusty_tiles_cli: StringProperty(
        name="rusty-tiles CLI",
        description="Optional rusty-tiles command-line executable, used only for Preview "
        "(the bundled Python library has no preview server)",
        subtype="FILE_PATH",
    )
    cesium_dir: StringProperty(
        name="Cesium Runtime",
        description="Optional Cesium 1.146 Build/Cesium folder (the one holding Cesium.js), "
        "served by rusty-tiles preview",
        subtype="DIR_PATH",
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        draw_dependency_status(layout)
        box = layout.box()
        box.label(text="Preview (optional)", icon="WORLD")
        box.prop(self, "rusty_tiles_cli")
        box.prop(self, "cesium_dir")
        box.label(text="Neither is downloaded or bundled; install them separately.")


classes = (GEOTOOLS_AddonPreferences,)

register, unregister = bpy.utils.register_classes_factory(classes)
