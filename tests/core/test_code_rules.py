"""Rules about how geotools/ uses some APIs, checked on its source.

Each rule has one sanctioned way and an easy wrong one; the failure message
says which to use. Checked on the syntax tree, so comments and docstrings
never trigger them.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "geotools"


def _modules() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dotted(node: ast.expr) -> str:
    """``a.b.c`` for a Name/Attribute chain; "" for anything else."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def test_the_rules_see_the_package() -> None:
    assert len(_modules()) > 50


# --- subnormals: Blender 5.3 runs Python with denormals-are-zero --------------------------


def _is_subnormal_constant(node: ast.AST) -> bool:
    """A subnormal float literal, ``nextafter(0, ...)`` or ``finfo(...).smallest_subnormal``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, float):
        return 0.0 < abs(node.value) < 2.2250738585072014e-308  # smallest normal binary64
    if isinstance(node, ast.Attribute):
        return node.attr == "smallest_subnormal"
    return (
        isinstance(node, ast.Call)
        and _dotted(node.func).split(".")[-1] == "nextafter"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 0
    )


def _subnormal_constants(tree: ast.AST) -> Iterator[ast.AST]:
    return (node for node in ast.walk(tree) if _is_subnormal_constant(node))


def test_no_subnormal_constants() -> None:
    found = [
        f"{_rel(path)}:{node.lineno}"
        for path in _modules()
        for node in _subnormal_constants(_tree(path))
    ]
    assert not found, (
        "Blender 5.3 runs Python with denormals-are-zero (and flush-to-zero), so a "
        "subnormal compares and computes as 0 in numpy there, but not in 5.2 or in "
        "tests/core. Compare with 0 and keep the sign some other way (see "
        "core.uv_overlap._edge).\n" + "\n".join(found)
    )


@pytest.mark.parametrize(
    "source",
    [
        "x = 5e-324",
        "x = -1e-310",
        "import numpy as np\nt = float(np.nextafter(0.0, 1.0))",
        "from numpy import nextafter\nt = nextafter(0, 1)",
        "import numpy as np\nt = np.finfo(np.float32).smallest_subnormal",
    ],
)
def test_the_subnormal_rule_catches(source: str) -> None:
    assert list(_subnormal_constants(ast.parse(source)))


def test_the_subnormal_rule_ignores_normal_numbers() -> None:
    source = "import numpy as np\na = 1e-300\nb = np.nextafter(1.0, 2.0)\nc = np.finfo(float).tiny"
    assert not list(_subnormal_constants(ast.parse(source)))
