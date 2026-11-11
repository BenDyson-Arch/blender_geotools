"""The package root and core must import without Blender."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Runs in a fresh interpreter so nothing earlier in the session hides an import.
# A meta-path hook makes any bpy import fail loudly, in case bpy is importable.
_SCRIPT = textwrap.dedent(
    """
    import importlib, pkgutil, sys

    class BlockBpy:
        def find_spec(self, name, path=None, target=None):
            if name == "bpy" or name.startswith(("bpy.", "bpy_", "mathutils", "bmesh")):
                raise ImportError(f"{name} imported outside Blender")
            return None

    sys.meta_path.insert(0, BlockBpy())
    sys.path.insert(0, sys.argv[1])

    import geotools
    import geotools.core
    for info in pkgutil.walk_packages(geotools.core.__path__, "geotools.core."):
        importlib.import_module(info.name)
    geotools.core.deps.probe_rusty_tiles()

    bad = sorted(m for m in sys.modules if m == "bpy" or m.startswith("bpy."))
    assert not bad, bad
    assert "geotools.blender" not in sys.modules, "geotools imported its Blender glue eagerly"
    print("ok")
    """
)


def test_geotools_and_core_never_import_bpy() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_package_has_no_bl_info() -> None:
    import geotools

    assert not hasattr(geotools, "bl_info")
    assert callable(geotools.register) and callable(geotools.unregister)
