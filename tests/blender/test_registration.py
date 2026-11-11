"""Preferences, operator and panel are registered and draw."""

from __future__ import annotations

import _harness as h
import bpy


def test_every_declared_class_registered() -> None:
    unregistered = [cls.__name__ for cls in h.extension_classes() if not cls.is_registered]
    assert not unregistered, f"not registered: {unregistered}"


def test_preferences_registered() -> None:
    cls = h.preferences_class()
    assert cls.is_registered
    assert cls.bl_idname == h.CONFIG.package
    addon = bpy.context.preferences.addons[h.CONFIG.package]
    assert isinstance(addon.preferences, cls), addon.preferences


def test_operator_registered() -> None:
    cls = h.operator_class()
    assert cls.is_registered
    assert getattr(bpy.types, cls.__name__, None) is cls
    assert h.operator_available(cls.bl_idname)
    assert bpy.ops.geotools.check_dependencies.poll()


def test_panel_registered() -> None:
    cls = h.panel_class()
    assert cls.is_registered
    assert getattr(bpy.types, cls.__name__, None) is cls
    assert (cls.bl_space_type, cls.bl_region_type, cls.bl_category) == (
        "VIEW_3D",
        "UI",
        "GeoTools",
    )


def test_panel_draws_version_and_status() -> None:
    calls = h.draw(h.panel_class())
    version = h.submodule("blender.common").extension_version()
    assert ("label", f"Version {version}", "NONE") in calls, calls
    assert ("operator", "geotools.check_dependencies", "FILE_REFRESH") in calls, calls


def test_preferences_draw_status() -> None:
    prefs = bpy.context.preferences.addons[h.CONFIG.package].preferences
    calls = h.draw(h.preferences_class(), data=prefs)
    assert ("prop", "rusty_tiles_cli", "NONE") in calls, calls
    assert ("prop", "cesium_dir", "NONE") in calls, calls
    assert any(text.startswith("rusty-tiles") for text, _icon in h.labels(calls)), calls
    assert ("operator", "geotools.check_dependencies", "FILE_REFRESH") in calls, calls
