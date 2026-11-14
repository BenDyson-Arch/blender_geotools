"""The extension is installed, enabled at startup and loaded from the profile."""

from __future__ import annotations

import sys
from pathlib import Path

import _harness as h
import bpy


def test_enabled_at_startup() -> None:
    # `extension install-file -e` saved the enabled state in the isolated
    # profile, and Blender enabled the extension when it loaded that profile.
    assert h.STARTUP.in_preferences, f"{h.CONFIG.package} is not in the saved preferences"
    assert h.STARTUP.loaded, f"{h.CONFIG.package} was not enabled at startup"


def test_loaded_from_installed_extension() -> None:
    pkg = h.package()
    expected_dir = h.extensions_root() / h.CONFIG.repo / h.CONFIG.extension_id
    assert Path(pkg.__file__).resolve().parent == expected_dir, (
        f"{h.CONFIG.package} loaded from {pkg.__file__}, expected {expected_dir}"
    )
    # Nothing may come from a source checkout under the bare name.
    assert "geotools" not in sys.modules, sys.modules["geotools"]
    assert h.submodule("blender.common").ADDON_PACKAGE == h.CONFIG.package


def test_no_bl_info() -> None:
    assert not hasattr(h.package(), "bl_info")


def test_reports_installed_manifest_version() -> None:
    version = h.submodule("blender.common").extension_version()
    assert version != "unknown"
    if h.CONFIG.extension_version is not None:
        assert version == h.CONFIG.extension_version, version


def test_blender_version_supported() -> None:
    minimum = h.submodule("core.manifest").read_manifest().blender_version_min
    wanted = tuple(int(part) for part in minimum.split("."))
    assert bpy.app.version >= wanted, f"Blender {bpy.app.version_string} < {minimum}"
