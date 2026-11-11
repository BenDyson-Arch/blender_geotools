"""View3D sidebar panels."""

from __future__ import annotations

import bpy

from .common import draw_dependency_status, extension_version


class GEOTOOLS_PT_about(bpy.types.Panel):
    """Extension version and dependency status."""

    bl_idname = "GEOTOOLS_PT_about"
    bl_label = "GeoTools"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.label(text=f"Version {extension_version()}")
        draw_dependency_status(layout)


classes = (GEOTOOLS_PT_about,)

register, unregister = bpy.utils.register_classes_factory(classes)
