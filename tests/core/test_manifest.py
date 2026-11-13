"""Tests for geotools.core.manifest."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from geotools.core.manifest import MANIFEST_PATH, ExtensionInfo, read_manifest


def test_reads_bundled_manifest() -> None:
    info = read_manifest()

    assert MANIFEST_PATH.name == "blender_manifest.toml"
    assert info.id == "geotools"
    assert info.name == "GeoTools"
    assert info.version  # whatever the manifest declares
    assert info.blender_version_min == "5.2.0"


def test_declares_the_permissions_it_uses() -> None:
    """Files, PROJ grid downloads and clipboard copies, each with a reason Blender accepts."""
    with MANIFEST_PATH.open("rb") as fh:
        permissions = tomllib.load(fh)["permissions"]

    assert set(permissions) == {"files", "network", "clipboard"}
    for key, reason in permissions.items():
        # Blender's rule for permission reasons: at most 64 characters, ending
        # in a letter, a digit or a closing bracket.
        assert isinstance(reason, str) and reason.strip() == reason, key
        assert 0 < len(reason) <= 64, (key, len(reason))
        assert reason[-1].isalnum() or reason[-1] in ")]}", key


def test_reads_given_path(tmp_path: Path) -> None:
    path = tmp_path / "blender_manifest.toml"
    path.write_text(
        'id = "x"\nname = "X"\nversion = "1.2.3"\nblender_version_min = "5.2.0"\n',
        encoding="utf-8",
    )

    assert read_manifest(path) == ExtensionInfo("x", "X", "1.2.3", "5.2.0")


def test_missing_key_raises(tmp_path: Path) -> None:
    path = tmp_path / "blender_manifest.toml"
    path.write_text('id = "x"\n', encoding="utf-8")

    with pytest.raises(KeyError):
        read_manifest(path)
