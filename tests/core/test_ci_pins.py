"""The Blender versions CI tests match the range the manifest allows.

The ``blender-range`` job in .github/workflows/ci.yml runs the Blender suites on
the oldest Blender the extension accepts and on the newest official release.
Parsed as text: the core job has no YAML library.
"""

from __future__ import annotations

import re
from pathlib import Path

from geotools.core.manifest import read_manifest

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"

_ENTRY = re.compile(r'^ +- version: "(\d+\.\d+\.\d+)"\n +sha256: "([0-9a-f]{64})"$', re.MULTILINE)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _range_entries() -> list[tuple[str, str]]:
    text = CI.read_text(encoding="utf-8")
    _, found, job = text.partition("\n  blender-range:\n")
    assert found, "ci.yml has no blender-range job"
    job = re.split(r"\n  [\w-]+:\n", job, maxsplit=1)[0]  # up to the next job
    return _ENTRY.findall(job)


def _pinned_blender() -> str:
    match = re.search(
        r'^  BLENDER_VERSION: "(\d+\.\d+\.\d+)"$', CI.read_text("utf-8"), re.MULTILINE
    )
    assert match, "ci.yml has no BLENDER_VERSION"
    return match.group(1)


def test_range_starts_at_blender_version_min() -> None:
    entries = _range_entries()
    assert len(entries) >= 2, entries
    assert entries[0][0] == read_manifest().blender_version_min


def test_range_is_ascending_and_ends_at_or_past_the_pinned_blender() -> None:
    versions = [_version(v) for v, _ in _range_entries()]
    assert versions == sorted(set(versions))
    assert versions[-1] >= _version(_pinned_blender())
