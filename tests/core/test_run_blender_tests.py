"""scripts/run_blender_tests.py refuses a zip whose wheels Blender's CPython cannot load."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "run_blender_tests", ROOT / "scripts/run_blender_tests.py"
)
run_blender_tests = importlib.util.module_from_spec(_spec)
sys.modules["run_blender_tests"] = run_blender_tests
_spec.loader.exec_module(run_blender_tests)

WHEELS = [
    "./wheels/rusty_tiles-0.4.1-cp310-abi3-manylinux_2_28_x86_64.whl",
    "./wheels/pyproj-3.8.0-cp313-cp313-manylinux_2_28_x86_64.whl",
    "./wheels/certifi-2026.10.23-py3-none-any.whl",
]


def test_accepts_wheels_for_blenders_python() -> None:
    run_blender_tests.check_wheels_python({"wheels": WHEELS}, (3, 13))


def test_accepts_a_zip_without_wheels() -> None:
    run_blender_tests.check_wheels_python({}, (3, 14))


def test_names_the_wheel_and_the_python_to_build_for() -> None:
    with pytest.raises(run_blender_tests.RunError) as err:
        run_blender_tests.check_wheels_python({"wheels": WHEELS}, (3, 14))
    message = str(err.value)
    assert "CPython 3.14" in message
    assert "pyproj-3.8.0-cp313-cp313-manylinux_2_28_x86_64.whl" in message
    assert "rusty_tiles" not in message  # abi3 loads in 3.14
    assert "--python 3.14" in message


def test_bad_wheel_name_is_a_run_error() -> None:
    with pytest.raises(run_blender_tests.RunError):
        run_blender_tests.check_wheels_python({"wheels": ["./wheels/not-a-wheel.zip"]}, (3, 13))
