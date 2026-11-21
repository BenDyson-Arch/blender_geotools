"""Extension preferences: dependency status, the bake device and the optional preview tools."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty

from .bake_device import DEVICE_ITEMS
from .common import ADDON_PACKAGE, draw_dependency_status, redraw_ui


def _classic_changed(_self, context: bpy.types.Context) -> None:
    from . import analyses  # it imports the modules this one is registered before

    analyses.list_all_scenes()  # layers the classic panels added since
    redraw_ui(context)


class GEOTOOLS_AddonPreferences(bpy.types.AddonPreferences):
    """GeoTools preferences."""

    bl_idname = ADDON_PACKAGE

    bake_device: EnumProperty(
        name="Bake Device",
        description="Device for GeoTools' Cycles bakes (texture bakes, sun hours, the horizon "
        "and the presets' fast paths); your render device is restored afterwards",
        items=DEVICE_ITEMS,
        default="AUTO",
    )
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

    use_classic_analysis_panels: BoolProperty(
        name="Use Classic Analysis Panels",
        description="Temporary, while the Analyses list is a mock-up: show the old Analysis "
        "and Layers panels instead of Analyses and Tools, to compare the two",
        default=False,
        update=_classic_changed,
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        draw_dependency_status(layout, show_paths=True)
        box = layout.box()
        box.label(text="Bakes", icon="RENDER_STILL")
        box.prop(self, "bake_device")
        box.label(text="Auto uses the GPU set in System > Cycles Render Devices.")
        box = layout.box()
        box.label(text="Preview (optional)", icon="WORLD")
        box.prop(self, "rusty_tiles_cli")
        box.prop(self, "cesium_dir")
        box.label(text="Neither is downloaded or bundled; install them separately.")
        box = layout.box()
        box.label(text="Developer (temporary)", icon="EXPERIMENTAL")
        box.prop(self, "use_classic_analysis_panels")


def classic_panels(context: bpy.types.Context | None) -> bool:
    """Whether the classic Analysis and Layers panels are shown instead of Analyses and Tools."""
    prefs = getattr(context, "preferences", None) if context is not None else None
    addon = prefs.addons.get(ADDON_PACKAGE) if prefs is not None else None
    return bool(addon is not None and addon.preferences.use_classic_analysis_panels)


classes = (GEOTOOLS_AddonPreferences,)

register, unregister = bpy.utils.register_classes_factory(classes)
