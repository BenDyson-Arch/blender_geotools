"""Operators."""

from __future__ import annotations

import bpy

from ..core import deps
from .common import extension_version, redraw_ui


class GEOTOOLS_OT_check_dependencies(bpy.types.Operator):
    """Re-check the bundled libraries: rusty-tiles and pyproj"""

    bl_idname = "geotools.check_dependencies"
    bl_label = "Check Dependencies"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        deps.reset_cache()
        for status in deps.probe_all():
            self.report({"INFO"} if status.available else {"WARNING"}, status.summary)
        if deps.check_for_update(extension_version()):
            self.report({"WARNING"}, deps.RESTART_MESSAGE)
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_copy_details(bpy.types.Operator):
    """Copy the GeoTools version, the library status and PROJ's own error text to the clipboard, for a bug report"""

    bl_idname = "geotools.copy_details"
    bl_label = "Copy Details"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        deps.probe_all()
        deps.check_for_update(extension_version())
        context.window_manager.clipboard = deps.details_text(extension_version())
        self.report({"INFO"}, "Copied to the clipboard.")
        return {"FINISHED"}


classes = (GEOTOOLS_OT_check_dependencies, GEOTOOLS_OT_copy_details)

register, unregister = bpy.utils.register_classes_factory(classes)
