"""Rules about how geotools/ uses some APIs, checked on its source.

Each rule has one sanctioned way and an easy wrong one; the failure message
says which to use. Checked on the syntax tree, so comments and docstrings
never trigger them.
"""

from __future__ import annotations

import ast
import re
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


# --- geodetic numbers: binary64, never a single-precision FloatProperty ---------------------

# Offsets, origins, anchors and latitude/longitude need binary64 (a northing of 6.9M
# in float32 is only good to 0.5 m), so they are text fields or Python floats.
_GEODETIC_NAME = re.compile(
    r"(^|_)(offset_[ena]|origin_[en]|anchor|latitude|longitude|lat|lon|easting|northing)($|_)"
)


def _property_kind(node: ast.expr | None) -> str:
    if isinstance(node, ast.Call):
        return _dotted(node.func).rsplit(".", 1)[-1]
    return ""


def _geodetic_float_properties() -> list[str]:
    found = []
    for path in _modules():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, kind = node.target.id, _property_kind(node.annotation)
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                name, kind = _dotted(node.targets[0]).rsplit(".", 1)[-1], _property_kind(node.value)
            else:
                continue
            if kind in ("FloatProperty", "FloatVectorProperty") and _GEODETIC_NAME.search(name):
                found.append(f"{_rel(path)}:{node.lineno}: {name}")
    return found


def test_geodetic_numbers_are_never_float_properties() -> None:
    found = _geodetic_float_properties()
    assert not found, (
        "Blender's FloatProperty is single precision. Store offsets, origins, anchors and "
        "latitude/longitude as binary64 text (properties.write_number / read_number, or JSON "
        "of Python floats).\n" + "\n".join(found)
    )


@pytest.mark.parametrize(
    ("name", "caught"),
    [
        ("source_offset_e", True),
        ("origin_n", True),
        ("anchor_height", True),
        ("latitude", True),
        ("utc_offset", False),
        ("ray_offset", False),
        ("observer_height", False),
    ],
)
def test_the_geodetic_rule_names(name: str, caught: bool) -> None:
    assert bool(_GEODETIC_NAME.search(name)) is caught


# --- decoded tiles: one module calls rusty-tiles' list_tiles and decode_tile ----------------

# It normalises rusty-tiles' decoded views into TileEntry and DecodedTile, so an API
# change there is a change in one module.
DECODE_OWNER = "geotools/core/output_tiles.py"
_DECODE_NAMES = {"list_tiles", "decode_tile"}


def _decode_calls_elsewhere() -> list[str]:
    found = []
    for path in _modules():
        if _rel(path) == DECODE_OWNER:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr in _DECODE_NAMES:
                if _dotted(node.value).rsplit(".", 1)[-1] != "output_tiles":
                    found.append(f"{_rel(path)}:{node.lineno}: {ast.unparse(node)}")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("rusty_tiles"):
                names = {alias.name for alias in node.names} & _DECODE_NAMES
                if names:
                    found.append(f"{_rel(path)}:{node.lineno}: from rusty_tiles import {names}")
    return found


def test_only_output_tiles_decodes_tiles() -> None:
    found = _decode_calls_elsewhere()
    assert not found, (
        f"Read published tiles through {DECODE_OWNER} (output_tiles.list_tiles and "
        "decode_tile), which resolves rusty-tiles' feature IDs into per-face source IDs.\n"
        + "\n".join(found)
    )


# --- pyproj: imported only where reprojection and CRS facts live ----------------------------

PYPROJ_IMPORTERS = {
    "geotools/analysis/site.py",  # points on Earth, convergence and scale factor
    "geotools/core/crs_catalog.py",  # the EPSG picker's names and areas of use
    "geotools/core/crs_match.py",  # is a CRS geographic
    "geotools/core/deps.py",  # the probe: version, PROJ version and data dir
    "geotools/core/geoscene.py",  # GeoScene latitude/longitude
    "geotools/core/output_frame.py",  # placing decoded tiles back in the CRS
    "geotools/core/reproject.py",  # reprojecting imported vector points
    "geotools/core/vector/gpkg.py",  # GeoPackage spatial reference definitions
}


def _pyproj_imports() -> list[str]:
    found = []
    for path in _modules():
        rel = _rel(path)
        for node in ast.walk(_tree(path)):
            hit = (
                (
                    isinstance(node, ast.Import)
                    and any(a.name.split(".")[0] == "pyproj" for a in node.names)
                )
                or (
                    isinstance(node, ast.ImportFrom)
                    and (node.module or "").split(".")[0] == "pyproj"
                )
                or (
                    isinstance(node, ast.Call)
                    and _dotted(node.func).rsplit(".", 1)[-1] in ("import_module", "_import")
                    and any(
                        (isinstance(a, ast.Constant) and str(a.value).split(".")[0] == "pyproj")
                        or _dotted(a).endswith("PYPROJ_MODULE")
                        for a in node.args
                    )
                )
            )
            if hit and rel not in PYPROJ_IMPORTERS:
                found.append(f"{rel}:{node.lineno}: {ast.unparse(node)}")
    return found


def test_pyproj_is_imported_only_by_its_modules() -> None:
    found = _pyproj_imports()
    assert not found, (
        "rusty-tiles does all CRS maths at publish time; pyproj only reprojects imported vector "
        "points and answers CRS questions, in the modules of PYPROJ_IMPORTERS. Call one of them "
        "(for example core.reproject.reproject_points), or add the module there with a reason.\n"
        + "\n".join(found)
    )


def test_every_pyproj_importer_still_imports_it() -> None:
    importers = {line.split(":")[0] for line in _pyproj_imports_anywhere()}
    assert importers == PYPROJ_IMPORTERS


def _pyproj_imports_anywhere() -> list[str]:
    saved = set(PYPROJ_IMPORTERS)
    PYPROJ_IMPORTERS.clear()
    try:
        return _pyproj_imports()
    finally:
        PYPROJ_IMPORTERS.update(saved)


# --- UI words: Import, not "ingest" ---------------------------------------------------------

# Strings users read: poll messages, reports, RNA names and descriptions, and the
# docstrings of Blender classes (Blender shows them as tooltips). Module and function
# docstrings and comments are developer text. Issue #91 has the rest of the UI sweep.
_BANNED_UI_WORDS = re.compile(r"\bingest", re.IGNORECASE)
_IDNAME = re.compile(r"^geotools\.\w+$")


def _developer_docstrings(tree: ast.Module) -> set[int]:
    owners = [tree] + [
        n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return {
        id(owner.body[0].value)
        for owner in owners
        if owner.body
        and isinstance(owner.body[0], ast.Expr)
        and isinstance(owner.body[0].value, ast.Constant)
    }


def _banned_ui_words() -> list[str]:
    found = []
    for path in sorted((PACKAGE / "blender").rglob("*.py")):
        tree = _tree(path)
        skip = _developer_docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in skip
                and not _IDNAME.match(node.value)
                and _BANNED_UI_WORDS.search(node.value)
            ):
                found.append(f"{_rel(path)}:{node.lineno}: {node.value[:80]!r}")
    return found


def test_ui_strings_say_import_not_ingest() -> None:
    found = _banned_ui_words()
    assert not found, (
        "One word per thing in the UI: Import brings a file in (see the blender.panels "
        "docstring). The operator id geotools.ingest_source stays for scripts.\n" + "\n".join(found)
    )
