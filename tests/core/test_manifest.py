"""Tests for geotools.core.manifest."""

from __future__ import annotations

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
