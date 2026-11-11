"""Extension preferences."""

from __future__ import annotations

import bpy

from .common import ADDON_PACKAGE, draw_dependency_status


class GEOTOOLS_AddonPreferences(bpy.types.AddonPreferences):
    """GeoTools preferences; for now only the dependency status."""

    bl_idname = ADDON_PACKAGE

    def draw(self, context: bpy.types.Context) -> None:
        draw_dependency_status(self.layout)


classes = (GEOTOOLS_AddonPreferences,)

register, unregister = bpy.utils.register_classes_factory(classes)
