"""Read the extension's own ``blender_manifest.toml``."""

from __future__ import annotations

import functools
import tomllib
from dataclasses import dataclass
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "blender_manifest.toml"


@dataclass(frozen=True, slots=True)
class ExtensionInfo:
    """The manifest fields GeoTools reports."""

    id: str
    name: str
    version: str
    blender_version_min: str


@functools.cache
def read_manifest(path: Path = MANIFEST_PATH) -> ExtensionInfo:
    """Parse the manifest at ``path`` (cached).

    Raises ``OSError``, ``tomllib.TOMLDecodeError`` or ``KeyError`` if it is
    missing or incomplete.
    """
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return ExtensionInfo(
        id=data["id"],
        name=data["name"],
        version=data["version"],
        blender_version_min=data["blender_version_min"],
    )
