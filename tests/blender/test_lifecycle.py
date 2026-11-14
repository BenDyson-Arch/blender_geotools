"""Disabling unregisters everything; enabling again restores it."""

from __future__ import annotations

import _harness as h
import bpy


def _assert_fully_unregistered(classes: list[type]) -> None:
    assert not h.is_enabled()
    assert h.CONFIG.package not in bpy.context.preferences.addons
    still = [cls.__name__ for cls in classes if cls.is_registered]
    assert not still, f"still registered after disable: {still}"
    leaked = [cls.__name__ for cls in classes if hasattr(bpy.types, cls.__name__)]
    assert not leaked, f"still on bpy.types after disable: {leaked}"
    assert not h.operator_available("geotools.check_dependencies")
    assert not h.operator_available("geotools.ingest_source")
    assert not hasattr(bpy.types.Scene, "geotools"), "Scene.geotools left behind"
    assert not hasattr(bpy.types.Image, "geotools_remedy"), "Image.geotools_remedy left behind"
    assert not hasattr(bpy.types.Image, "geotools_texture_file"), (
        "Image.geotools_texture_file left behind"
    )
    package = h.CONFIG.package
    for name in dir(bpy.app.handlers):
        handlers = getattr(bpy.app.handlers, name)
        if not isinstance(handlers, list):
            continue
        ours = [f.__name__ for f in handlers if getattr(f, "__module__", "").startswith(package)]
        assert not ours, f"bpy.app.handlers.{name} still has {ours} after disable"


def _assert_fully_registered(classes: list[type]) -> None:
    assert h.is_enabled()
    missing = [cls.__name__ for cls in classes if not cls.is_registered]
    assert not missing, f"not registered after enable: {missing}"
    assert h.operator_available("geotools.check_dependencies")
    assert bpy.ops.geotools.check_dependencies() == {"FINISHED"}
    assert h.operator_available("geotools.ingest_source")
    assert bpy.context.scene.geotools.placement_mode == "LOCAL"


def test_disable_unregisters_cleanly() -> None:
    classes = h.extension_classes()
    h.disable()
    _assert_fully_unregistered(classes)


def test_enable_after_disable() -> None:
    h.ensure_enabled()
    classes = h.extension_classes()
    h.disable()
    h.enable()
    _assert_fully_registered(classes)
    prefs = bpy.context.preferences.addons[h.CONFIG.package].preferences
    assert isinstance(prefs, h.preferences_class())


def test_repeated_enable_disable() -> None:
    h.ensure_enabled()
    classes = h.extension_classes()
    for _ in range(3):
        h.disable()
        _assert_fully_unregistered(classes)
        h.enable()
        _assert_fully_registered(classes)
    status = h.submodule("core.deps").probe_rusty_tiles()
    assert status.available == (h.CONFIG.expect == "available"), status


def test_failed_register_rolls_back() -> None:
    h.ensure_enabled()
    classes = h.extension_classes()
    h.disable()
    pkg = h.package()
    panels = h.submodule("blender.panels")
    original = panels.register

    def broken() -> None:
        raise RuntimeError("simulated panel registration failure")

    panels.register = broken
    try:
        try:
            pkg.register()
        except RuntimeError as ex:
            assert "simulated" in str(ex)
        else:
            raise AssertionError("register() swallowed the panel failure")
    finally:
        panels.register = original
    still = [cls.__name__ for cls in classes if cls.is_registered]
    assert not still, f"rollback left registered: {still}"
    h.enable()
    _assert_fully_registered(classes)
