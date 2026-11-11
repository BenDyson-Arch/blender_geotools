"""scripts/release_notes.py: the changelog section a release publishes."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Next thing.

## [0.3.0] - 2026-11-20

### Added

- Publish to .3tz.

## [0.3.0-rc.1] - 2026-11-18

- Candidate.

## [0.2.0] - 2026-06-05

[Unreleased]: https://example.invalid/compare/v0.3.0...HEAD
[0.3.0]: https://example.invalid/compare/v0.2.0...v0.3.0
"""


def test_section_is_the_body_between_headings() -> None:
    assert release_notes.section(CHANGELOG, "0.3.0") == "### Added\n\n- Publish to .3tz."


def test_prerelease_section_is_matched_exactly() -> None:
    assert release_notes.section(CHANGELOG, "0.3.0-rc.1") == "- Candidate."


def test_missing_or_empty_section_is_none() -> None:
    assert release_notes.section(CHANGELOG, "0.9.0") is None
    assert release_notes.section(CHANGELOG, "0.2.0") is None


def test_main_reads_the_repository_changelog(capsys) -> None:
    assert release_notes.main(["v0.2.0"]) == 0
    assert "### Added" in capsys.readouterr().out
    assert release_notes.main(["0.0.9"]) == 1
