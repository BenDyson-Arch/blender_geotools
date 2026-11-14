"""scripts/fetch_wheels.py keeps the existing wheels when a fetch fails."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("fetch_wheels", ROOT / "scripts/fetch_wheels.py")
fetch_wheels = importlib.util.module_from_spec(_spec)
sys.modules["fetch_wheels"] = fetch_wheels  # dataclasses look the module up
_spec.loader.exec_module(fetch_wheels)


def _existing(tmp_path: Path) -> tuple[Path, Path]:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    kept = wheels / "rusty_tiles-0.4.0-cp310-abi3-linux_x86_64.whl"
    kept.write_bytes(b"not really a wheel")
    return wheels, kept


def test_missing_source_dir_leaves_wheels(tmp_path: Path) -> None:
    wheels, kept = _existing(tmp_path)
    missing = tmp_path / "does-not-exist"
    argv = ["--from-dir", str(missing), "--platform", "linux-x64", "--wheels-dir", str(wheels)]
    assert fetch_wheels.main(argv) == 1
    assert kept.read_bytes() == b"not really a wheel"


def test_source_without_the_wheel_leaves_wheels(tmp_path: Path) -> None:
    wheels, kept = _existing(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    argv = ["--from-dir", str(empty), "--platform", "linux-x64", "--wheels-dir", str(wheels)]
    assert fetch_wheels.main(argv) == 1
    assert kept.exists()
