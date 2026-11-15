"""How the pyright check is pinned and silenced.

pyright runs in CI's core job on the packages in ``[tool.pyright] include``
(pyproject.toml), with mypy-style ``# type: ignore`` comments switched off. A
suppression is ``# pyright: ignore[reportRuleName]``, with the reason beside
it, so it silences one rule on one line.
"""

from __future__ import annotations

import io
import re
import tokenize
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _checked_modules() -> list[Path]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        include = tomllib.load(fh)["tool"]["pyright"]["include"]
    return sorted(path for folder in include for path in (REPO_ROOT / folder).rglob("*.py"))


def _bad_suppressions(source: str) -> list[tuple[int, str]]:
    """``(line, comment)`` of each ``type: ignore`` and each pyright ignore with no rule."""
    found = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT:
            continue
        text = token.string
        if re.search(r"type:\s*ignore", text) or re.search(r"pyright:\s*ignore(?!\[)", text):
            found.append((token.start[0], text))
    return found


def test_pyright_checks_some_modules() -> None:
    assert len(_checked_modules()) > 40


def test_suppressions_name_a_pyright_rule() -> None:
    found = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {comment}"
        for path in _checked_modules()
        for line, comment in _bad_suppressions(path.read_text(encoding="utf-8"))
    ]
    assert not found, (
        "pyright ignores `# type: ignore` here (enableTypeIgnoreComments = false). Fix the "
        "type, or silence one rule with `# pyright: ignore[reportRuleName]` and say why.\n"
        + "\n".join(found)
    )


@pytest.mark.parametrize(
    ("comment", "bad"),
    [
        ("x = f()  # type: ignore[arg-type]", True),
        ("x = f()  # type:ignore", True),
        ("x = f()  # pyright: ignore", True),
        ("x = f()  # pyright: ignore[reportArgumentType]", False),
        ('x = "# type: ignore"', False),
    ],
)
def test_the_suppression_rule_catches(comment: str, bad: bool) -> None:
    assert bool(_bad_suppressions(comment + "\n")) is bad


def test_agents_md_runs_the_ci_pyright() -> None:
    pin = re.search(r'PYRIGHT_VERSION: "([^"]+)"', CI.read_text(encoding="utf-8"))
    assert pin, "ci.yml sets PYRIGHT_VERSION"
    agents = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"uvx pyright@{pin.group(1)}" in agents
