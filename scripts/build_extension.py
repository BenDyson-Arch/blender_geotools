#!/usr/bin/env python3
"""Build the GeoTools Blender extension zip(s).

Copies geotools/ to a staging directory under .build/, injects ``platforms`` and
``wheels`` into the staged blender_manifest.toml from the wheels in
geotools/wheels/ (see scripts/fetch_wheels.py), then runs Blender's
``extension validate`` and ``extension build --split-platforms``.
The repository manifest is never modified.

Usage:
    python scripts/build_extension.py [--blender PATH] [--out DIR] [--no-wheels]

Stdlib only (Python 3.11+).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_wheels import (
    REPO_ROOT,
    WHEELS_DIR,
    FetchError,
    check_wheel,
    read_config,
)

SOURCE_DIR = REPO_ROOT / "geotools"
MANIFEST_NAME = "blender_manifest.toml"
BUILD_DIR = REPO_ROOT / ".build"
DEFAULT_OUT = REPO_ROOT / "dist"


class BuildError(Exception):
    pass


def toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def toml_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def stage_source(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SOURCE_DIR,
        staging,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", "wheels"),
    )


def bundled_wheels(pin: str, configured: list[str]) -> tuple[list[Path], list[str]]:
    """Return the wheels in geotools/wheels and the Blender platforms they cover."""
    wheels = sorted(WHEELS_DIR.glob("*.whl")) if WHEELS_DIR.is_dir() else []
    if not wheels:
        raise BuildError(
            f"no wheels in {WHEELS_DIR}; run scripts/fetch_wheels.py first, "
            "or pass --no-wheels for a build without rusty-tiles"
        )
    covered: set[str] = set()
    for path in wheels:
        try:
            wheel = check_wheel(path, pin)
        except (FetchError, ValueError) as ex:
            raise BuildError(f"stale or invalid wheel: {ex}; re-run scripts/fetch_wheels.py")
        covered |= wheel.blender_platforms
    covered.discard("any")
    if not covered:
        raise BuildError(
            f"the wheels in {WHEELS_DIR} are all platform independent; nothing to split"
        )
    unexpected = sorted(covered - set(configured))
    if unexpected:
        raise BuildError(
            f"wheels cover platform(s) {', '.join(unexpected)} that are not in "
            "[tool.geotools] platforms; re-run scripts/fetch_wheels.py"
        )
    platforms = [p for p in configured if p in covered]
    missing = [p for p in configured if p not in covered]
    if missing:
        print(
            f"note: no wheels for {', '.join(missing)}; building for {', '.join(platforms)} only",
            file=sys.stderr,
        )
    return wheels, platforms


def inject_manifest(manifest: Path, platforms: list[str], wheel_names: list[str]) -> None:
    """Insert top-level ``platforms`` and ``wheels`` keys before the first table header."""
    text = manifest.read_text(encoding="utf-8")
    original = tomllib.loads(text)
    for key in ("platforms", "wheels"):
        if key in original:
            raise BuildError(
                f"{SOURCE_DIR / MANIFEST_NAME} already sets {key!r}; "
                "it must be left to scripts/build_extension.py"
            )
    wheel_paths = [f"./wheels/{name}" for name in wheel_names]
    block = (
        "# BEGIN injected by scripts/build_extension.py\n"
        f"platforms = {toml_array(platforms)}\n"
        f"wheels = {toml_array(wheel_paths)}\n"
        "# END injected by scripts/build_extension.py\n\n"
    )
    lines = text.splitlines(keepends=True)
    index = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    if index == len(lines) and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.insert(index, block)
    new_text = "".join(lines)

    staged = tomllib.loads(new_text)
    expected = dict(original, platforms=platforms, wheels=wheel_paths)
    if staged != expected:
        raise BuildError("internal error: manifest injection changed other keys")
    manifest.write_text(new_text, encoding="utf-8", newline="\n")


def find_blender(blender: str) -> str:
    resolved = shutil.which(blender)
    if resolved is None:
        raise BuildError(f"Blender not found: {blender!r} (pass --blender PATH)")
    return resolved


def run_blender(blender: str, args: list[str]) -> None:
    cmd = [blender, "--factory-startup", "--command", "extension", *args]
    print("$ " + " ".join(cmd), flush=True)
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if output:
        print("\n".join("  " + line for line in output.splitlines()), flush=True)
    # `extension validate` reports manifest errors as status lines; treat any
    # error line as a failure even if the exit code does not say so.
    errored = any(
        line.lstrip().lower().startswith(("error", "fatal")) for line in output.splitlines()
    )
    if result.returncode != 0 or errored:
        raise BuildError(f"blender extension {args[0]} failed (exit code {result.returncode})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--blender", default="blender", help="Blender executable (default: blender on PATH)"
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="output directory (default: dist/)"
    )
    parser.add_argument(
        "--no-wheels",
        action="store_true",
        help="build without rusty-tiles wheels and without a platforms restriction",
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    try:
        blender = find_blender(args.blender)
        if not (SOURCE_DIR / MANIFEST_NAME).is_file():
            raise BuildError(f"{SOURCE_DIR / MANIFEST_NAME} not found")

        with (SOURCE_DIR / MANIFEST_NAME).open("rb") as fh:
            manifest = tomllib.load(fh)
        ext_id, version = manifest["id"], manifest["version"]

        staging = BUILD_DIR / "staging" / ext_id
        stage_source(staging)

        if args.no_wheels:
            platforms: list[str] = []
            print("building without wheels (no platforms restriction)")
        else:
            pin, configured = read_config()
            wheels, platforms = bundled_wheels(pin, configured)
            (staging / "wheels").mkdir()
            for path in wheels:
                shutil.copy2(path, staging / "wheels" / path.name)
            inject_manifest(staging / MANIFEST_NAME, platforms, [w.name for w in wheels])
            print(f"rusty-tiles {pin} wheels for {', '.join(platforms)}:")
            for path in wheels:
                print(f"  {path.name}")

        out_dir = args.out.expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        if platforms:
            names = [f"{ext_id}-{version}-{p.replace('-', '_')}.zip" for p in platforms]
        else:
            names = [f"{ext_id}-{version}.zip"]
        for name in names:
            (out_dir / name).unlink(missing_ok=True)

        run_blender(blender, ["validate", str(staging)])
        build_args = ["build", "--source-dir", str(staging), "--output-dir", str(out_dir)]
        if platforms:
            build_args.append("--split-platforms")
        run_blender(blender, build_args)

        produced = [out_dir / name for name in names]
        absent = [p for p in produced if not p.is_file()]
        if absent:
            raise BuildError("Blender did not produce: " + ", ".join(str(p) for p in absent))
        for path in produced:
            run_blender(blender, ["validate", str(path)])
    except (BuildError, FetchError, OSError, KeyError) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1

    print("built:")
    for path in produced:
        print(f"  {os.path.relpath(path, Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
