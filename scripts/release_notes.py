"""Print the CHANGELOG.md section for one version, for the GitHub release notes.

    python scripts/release_notes.py 0.3.0

Exits non-zero if the changelog has no ``## [<version>]`` section or the
section is empty, so a release cannot go out without notes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


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


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: release_notes.py VERSION", file=sys.stderr)
        return 2
    version = args[0].removeprefix("v")
    body = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if body is None:
        print(f"error: CHANGELOG.md has no non-empty section for [{version}]", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
