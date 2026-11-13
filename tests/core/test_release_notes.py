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


PAGE = """<!--
Maintainer note: not published.
-->

GeoTools {version} intro.

## Download

| Linux x64 | [`geotools-{version}-linux_x64.zip`](https://example.invalid/v{version}/geotools-{version}-linux_x64.zip) |
<!-- TODO: an inner note. -->
- A limitation.
"""


def test_without_a_page_the_notes_are_the_changelog_section(tmp_path) -> None:
    assert release_notes.notes("0.3.0", CHANGELOG, pages=tmp_path) == (
        "### Added\n\n- Publish to .3tz."
    )


def test_a_page_replaces_the_changelog_section(tmp_path) -> None:
    (tmp_path / "0.3.0.md").write_text(PAGE, encoding="utf-8")
    body = release_notes.notes("0.3.0", CHANGELOG, pages=tmp_path)
    assert body.startswith("GeoTools 0.3.0 intro.")
    assert "geotools-0.3.0-linux_x64.zip" in body
    assert "{version}" not in body
    assert "<!--" not in body and "Maintainer note" not in body and "TODO" not in body
    assert "|\n- A limitation." in body
    assert "Publish to .3tz." not in body
    assert body.endswith(
        "[CHANGELOG.md](https://github.com/BenDyson-Arch/blender_geotools/blob/v0.3.0/CHANGELOG.md)."
    )


def test_a_prerelease_uses_its_base_page_with_its_own_version(tmp_path) -> None:
    (tmp_path / "0.3.0.md").write_text(PAGE, encoding="utf-8")
    body = release_notes.notes("0.3.0-rc.1", CHANGELOG, pages=tmp_path)
    assert body.startswith("GeoTools 0.3.0-rc.1 intro.")
    assert "/v0.3.0-rc.1/geotools-0.3.0-rc.1-linux_x64.zip" in body


def test_a_page_still_needs_a_changelog_section(tmp_path) -> None:
    (tmp_path / "0.9.0.md").write_text(PAGE, encoding="utf-8")
    assert release_notes.notes("0.9.0", CHANGELOG, pages=tmp_path) is None


def test_the_030_page_has_what_a_first_time_user_needs() -> None:
    page = (ROOT / "docs/release-notes/0.3.0.md").read_text(encoding="utf-8")
    body = release_notes.render_page(page, "0.3.0")
    for heading in (
        "## Download",
        "### Requirements",
        "### Install",
        "### Check the download",
        "## What is new since 0.2",
        "## Known limitations",
        "## Upgrading from 0.2",
        "## Getting help",
    ):
        assert f"\n{heading}\n" in body, heading
    for platform in ("linux_x64", "windows_x64", "macos_arm64"):
        assert f"releases/download/v0.3.0/geotools-0.3.0-{platform}.zip" in body
    assert "releases/download/v0.3.0/geotools-demo-data-0.3.0.zip" in body
    assert "Source code (zip)" in body
    assert "SHA256SUMS" in body
    assert "{version}" not in body and "<!--" not in body
    # Relative links do not work in a release body.
    assert "](docs/" not in body and "](../" not in body
