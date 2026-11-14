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


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


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


def _calls(tree: ast.AST) -> Iterator[ast.Call]:
    return (node for node in ast.walk(tree) if isinstance(node, ast.Call))


def test_the_rules_see_the_package() -> None:
    assert len(_modules()) > 50


# --- rusty-tiles: the pinned API, no feature detection --------------------------------------

# The probe is the one place that looks for rusty-tiles' functions: it reports a
# wheel without REQUIRED_FUNCTIONS (0.4.0) as not available, so nothing else asks.
RUSTY_TILES_PROBE = "geotools/core/deps.py"


def _rusty_tiles_names(tree: ast.Module) -> set[str]:
    """Names bound to the rusty_tiles module or to anything got from it, module-wide."""
    names = {"rusty_tiles", "rt"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "rusty_tiles":
                    names.add(alias.asname or "rusty_tiles")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rusty_tiles"):
            names.update(alias.asname or alias.name for alias in node.names)
    while True:
        before = len(names)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value is not None and _from_rusty_tiles(value, names):
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
        if len(names) == before:
            return names


def _from_rusty_tiles(node: ast.expr, names: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Attribute):
        return _from_rusty_tiles(node.value, names)
    if isinstance(node, ast.Call):
        func = _dotted(node.func).rsplit(".", 1)[-1]
        if func == "_rusty_tiles":
            return True
        if func == "import_module":
            return any(
                (isinstance(a, ast.Constant) and "rusty_tiles" in str(a.value))
                or _dotted(a).endswith("MODULE_NAME")
                for a in node.args
            )
        return _from_rusty_tiles(node.func, names)
    return False


def _rusty_tiles_feature_checks() -> list[str]:
    found = []
    for path in _modules():
        if _rel(path) == RUSTY_TILES_PROBE:
            continue
        tree = _tree(path)
        names = _rusty_tiles_names(tree)
        for call in _calls(tree):
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in ("hasattr", "getattr")
                and call.args
                and _from_rusty_tiles(call.args[0], names)
            ):
                found.append(f"{_rel(path)}:{call.lineno}: {ast.unparse(call)}")
    return found


def test_no_rusty_tiles_feature_detection() -> None:
    found = _rusty_tiles_feature_checks()
    assert not found, (
        "GeoTools requires the pinned rusty-tiles ([tool.geotools.wheels] in pyproject.toml) "
        "and has no fallbacks for older wheels: use its attributes directly, and give test "
        "fakes the attributes the real API has. deps.REQUIRED_FUNCTIONS is the one check.\n"
        + "\n".join(found)
    )


@pytest.mark.parametrize(
    "source",
    [
        "import rusty_tiles\nhasattr(rusty_tiles, 'validate')",
        "rt = _rusty_tiles()\ngetattr(rt, 'validate', None)",
        "tiled = _rusty_tiles().mesh_to_tileset(a, b)\ngetattr(tiled, 'cleanup_diagnostics', ())",
        "m = importlib.import_module(deps.MODULE_NAME)\nhasattr(m, 'x')",
    ],
)
def test_the_rusty_tiles_rule_catches(source: str) -> None:
    tree = ast.parse(source)
    names = _rusty_tiles_names(tree)
    call = next(c for c in _calls(tree) if _dotted(c.func) in ("hasattr", "getattr"))
    assert _from_rusty_tiles(call.args[0], names)


def test_the_rusty_tiles_rule_ignores_others() -> None:
    tree = ast.parse("import pyproj\nmodule = pyproj\ngetattr(module, 'x', None)")
    names = _rusty_tiles_names(tree)
    call = next(_calls(tree))
    assert not _from_rusty_tiles(call.args[0], names)


# --- stored paths: read and written through blend_paths -------------------------------------


def _bypasses_blend_paths(call: ast.Call) -> bool:
    name = _dotted(call.func)
    if name == "bpy.path.abspath":
        # Add-on preferences live in Blender's user preferences, not the .blend.
        return not (call.args and _dotted(call.args[0]).startswith("prefs."))
    return name in ("bpy.path.relpath", "os.path.relpath")


def _path_calls() -> list[str]:
    return [
        f"{_rel(path)}:{call.lineno}: {ast.unparse(call)}"
        for path in _modules()
        for call in _calls(_tree(path))
        if _bypasses_blend_paths(call)
    ]


def test_stored_paths_go_through_blend_paths() -> None:
    found = _path_calls()
    assert not found, (
        "Read a path GeoTools stores with blend_paths.abspath (// with either separator; "
        "bpy.path.abspath keeps backslashes on Linux and macOS) and store one with "
        "blend_paths.portable (//sub/file with forward slashes under the .blend's folder, "
        "absolute otherwise). See geotools/core/paths.py.\n" + "\n".join(found)
    )


# --- true north: turned through the site's map, not one convergence ------------------------

# Owns the grid-to-ground map: SunSite.scene_azimuth, true_azimuth and rotation.
TRUE_NORTH_OWNER = "geotools/analysis/site.py"


def _convergence_uses() -> list[str]:
    found = []
    for path in _modules():
        if _rel(path) == TRUE_NORTH_OWNER:
            continue
        tree = _tree(path)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if node.attr == "meridian_convergence":
                found.append(f"{_rel(path)}:{node.lineno}: {ast.unparse(node)}")
            # Recorded or shown (a dict value) is fine; turning with it is not.
            elif node.attr == "convergence" and not isinstance(parents[node], ast.Dict):
                found.append(f"{_rel(path)}:{node.lineno}: {ast.unparse(parents[node])}")
    return found


def test_true_north_turns_through_the_site_map() -> None:
    found = _convergence_uses()
    assert not found, (
        "Turn azimuths between true and scene north with site.SunSite.scene_azimuth and "
        "true_azimuth, or take SunSite.rotation where one angle must do. One meridian "
        "convergence is exact only in a conformal grid (not in Albers or Web Mercator); "
        "outside geotools/analysis/site.py it may only be recorded or shown.\n" + "\n".join(found)
    )


# --- pyproj: x is easting or longitude ------------------------------------------------------


def _transformers_without_always_xy() -> list[str]:
    found = []
    for path in _modules():
        for call in _calls(_tree(path)):
            func = call.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in ("from_crs", "from_proj")
                and ast.unparse(func.value).endswith("Transformer")
            ):
                continue
            ok = any(
                k.arg == "always_xy" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in call.keywords
            )
            if not ok:
                found.append(f"{_rel(path)}:{call.lineno}: {ast.unparse(call)}")
    return found


def test_pyproj_transformers_take_x_first() -> None:
    found = _transformers_without_always_xy()
    assert not found, (
        "Pass always_xy=True to pyproj.Transformer.from_crs: GeoTools' coordinates are "
        "(easting, northing) and (longitude, latitude), and without it pyproj follows each "
        "CRS's axis order (latitude first for EPSG:4326). See geotools/core/reproject.py.\n"
        + "\n".join(found)
    )
