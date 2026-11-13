"""Print the GitHub release notes for one version.

    python scripts/release_notes.py 0.3.0

The notes are the release page ``.github/release-notes/<version>.md`` when there
is one (for a prerelease such as ``0.3.0-rc.1``, the page of ``0.3.0`` is used
if it has none of its own), with ``{version}`` replaced by the version and a
link to the full changelog added. Without a page, they are the CHANGELOG.md
section of the version.

Exits non-zero if the changelog has no ``## [<version>]`` section or the
section is empty, so a release cannot go out without a changelog entry, and if
the notes link to a file at the tag (``.../blob/v<version>/<path>``) that this
checkout does not have, so a release body cannot go out with dead links.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
PAGES = ROOT / ".github" / "release-notes"
REPOSITORY = "https://github.com/BenDyson-Arch/blender_geotools"

PLACEHOLDER = "{version}"
_COMMENT = re.compile(r"[ \t]*<!--.*?-->[ \t]*\n?", re.DOTALL)
"""An HTML comment: notes for maintainers, never part of the release body."""


def section(text: str, version: str) -> str | None:
    """The body of ``## [version]`` (without the heading), or None."""
    heading = re.compile(rf"^## \[{re.escape(version)}\](?:\s|$)", re.MULTILINE)
    match = heading.search(text)
    if match is None:
        return None
    rest = text[match.end() :]
    end = re.search(r"^## \[|^\[[^\]]+\]: ", rest, re.MULTILINE)
    body = rest[: end.start()] if end else rest
    # Drop the rest of the heading line (the date).
    body = body.split("\n", 1)[1] if "\n" in body else ""
    return body.strip() or None


def page_path(version: str, pages: Path = PAGES) -> Path | None:
    """The release page for ``version``, or for its base version if a prerelease has none."""
    for name in dict.fromkeys((version, version.split("-", 1)[0])):
        path = pages / f"{name}.md"
        if path.is_file():
            return path
    return None


def render_page(text: str, version: str) -> str:
    """The release page as a release body: comments dropped, the version filled in."""
    body = _COMMENT.sub("", text).strip()
    body = body.replace(PLACEHOLDER, version)
    return (
        f"{body}\n\n---\n\nThe full list of changes is in "
        f"[CHANGELOG.md]({REPOSITORY}/blob/v{version}/CHANGELOG.md)."
    )


def notes(version: str, changelog: str, pages: Path = PAGES) -> str | None:
    """The release notes for ``version``, or None if the changelog has no section for it."""
    body = section(changelog, version)
    if body is None:
        return None
    page = page_path(version, pages)
    if page is None:
        return body
    return render_page(page.read_text(encoding="utf-8"), version)


def missing_files(body: str, version: str, root: Path = ROOT) -> list[str]:
    """The repository paths ``body`` links to at tag ``v<version>`` that are not files under ``root``."""
    link = re.compile(rf"{re.escape(REPOSITORY)}/blob/v{re.escape(version)}/([^)#\s]+)")
    paths = dict.fromkeys(link.findall(body))
    return [path for path in paths if not (root / path).is_file()]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: release_notes.py VERSION", file=sys.stderr)
        return 2
    version = args[0].removeprefix("v")
    body = notes(version, CHANGELOG.read_text(encoding="utf-8"), pages=PAGES)
    if body is None:
        print(f"error: CHANGELOG.md has no non-empty section for [{version}]", file=sys.stderr)
        return 1
    missing = missing_files(body, version)
    if missing:
        for path in missing:
            print(
                f"error: the release notes link to {path}, which is not in the tag", file=sys.stderr
            )
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
