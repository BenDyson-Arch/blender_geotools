"""Operators."""

from __future__ import annotations

import bpy

from ..core import deps
from .common import redraw_ui


class GEOTOOLS_OT_check_dependencies(bpy.types.Operator):
    """Re-check the bundled rusty-tiles library"""

    bl_idname = "geotools.check_dependencies"
    bl_label = "Check Dependencies"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        deps.reset_cache()
        status = deps.probe_rusty_tiles()
        self.report({"INFO"} if status.available else {"WARNING"}, status.summary)
        redraw_ui(context)
        return {"FINISHED"}


classes = (GEOTOOLS_OT_check_dependencies,)

register, unregister = bpy.utils.register_classes_factory(classes)
