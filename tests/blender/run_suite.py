"""Run the GeoTools tests inside Blender against the installed extension.

Started by scripts/run_blender_tests.py, which builds and installs the
extension into a throwaway profile first. To run it by hand against a profile
where the extension is already installed and enabled:

    BLENDER_USER_RESOURCES=DIR blender -b --python-exit-code 1 \\
        --python tests/blender/run_suite.py -- --expect missing

A deliberately tiny runner rather than pytest: Blender's Python has no pytest,
and this keeps the suite free of network installs and of pip/pytest version
drift against whatever CPython a Blender build bundles. It discovers
``test_*`` functions in ``test_*.py`` next to this file, runs them in
definition order, prints a pass/fail line each, optionally writes a JSON
report, and exits non-zero if anything failed or nothing ran.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Modules run first in this order (startup state before anything toggles the
# add-on); any other test_*.py follows alphabetically.
PREFERRED_ORDER = ("test_install", "test_registration", "test_dependencies", "test_lifecycle")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_suite.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", default="user_default", help="extension repository id")
    parser.add_argument("--extension-id", default="geotools", help="extension id")
    parser.add_argument("--expect", choices=("available", "missing"), required=True)
    parser.add_argument("--expect-version", help="expected rusty-tiles version (with available)")
    parser.add_argument("--expect-pyproj-version", help="expected pyproj version (with available)")
    parser.add_argument("--extension-version", help="expected extension (manifest) version")
    parser.add_argument("-k", dest="keyword", help="only run tests whose name contains this")
    parser.add_argument("--results", type=Path, help="write a JSON report here")
    args = parser.parse_args(argv)
    if args.expect == "available" and not (args.expect_version and args.expect_pyproj_version):
        parser.error("--expect available needs --expect-version and --expect-pyproj-version")
    return args


def discover(keyword: str | None) -> list[tuple[str, Callable[[], None]]]:
    stems = sorted(p.stem for p in HERE.glob("test_*.py"))
    stems.sort(key=lambda s: PREFERRED_ORDER.index(s) if s in PREFERRED_ORDER else 99)
    tests: list[tuple[str, Callable[[], None]]] = []
    for stem in stems:
        module = importlib.import_module(stem)
        functions = [
            fn
            for name, fn in vars(module).items()
            if name.startswith("test_")
            and inspect.isfunction(fn)
            and fn.__module__ == module.__name__
        ]
        functions.sort(key=lambda fn: fn.__code__.co_firstlineno)
        for fn in functions:
            name = f"{stem}::{fn.__name__}"
            if keyword is None or keyword in name:
                tests.append((name, fn))
    return tests


def main() -> int:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    # Only this directory goes on sys.path: the extension must come from the
    # installed copy, never from the source checkout.
    sys.path.insert(0, str(HERE))
    import _harness as h
    import bpy

    package = f"bl_ext.{args.repo}.{args.extension_id}"
    h.STARTUP = h.snapshot_startup(package)
    h.CONFIG = h.Config(
        package=package,
        repo=args.repo,
        expect=args.expect,
        expect_version=args.expect_version if args.expect == "available" else None,
        expect_pyproj_version=args.expect_pyproj_version if args.expect == "available" else None,
        extension_version=args.extension_version,
    )

    print(f"Blender {bpy.app.version_string}, Python {sys.version.split()[0]}")
    print(
        f"extension: {package}, expect rusty-tiles {args.expect} {args.expect_version or ''}, "
        f"pyproj {args.expect} {args.expect_pyproj_version or ''}"
    )
    print(f"startup state: {h.STARTUP}")

    results: list[dict[str, object]] = []
    tests = discover(args.keyword)
    for name, fn in tests:
        started = time.perf_counter()
        error = None
        try:
            # Every test starts with the extension enabled; lifecycle tests
            # that disable it are not allowed to leak that into the next one.
            if name.split("::")[0] != "test_install":
                h.ensure_enabled()
            fn()
        except KeyboardInterrupt:
            raise
        except BaseException:  # noqa: BLE001 - a test failure of any kind is reported
            error = traceback.format_exc()
        elapsed = time.perf_counter() - started
        outcome = "FAIL" if error else "PASS"
        print(f"{outcome} {name} ({elapsed:.2f}s)")
        if error:
            print("    " + error.rstrip().replace("\n", "\n    "))
        results.append({"name": name, "outcome": outcome.lower(), "error": error})

    passed = sum(r["outcome"] == "pass" for r in results)
    failed = len(results) - passed
    print(f"\n{passed} passed, {failed} failed")
    if args.results:
        args.results.write_text(
            json.dumps(
                {
                    "blender": bpy.app.version_string,
                    "python": sys.version.split()[0],
                    "package": package,
                    "expect": args.expect,
                    "expect_version": h.CONFIG.expect_version,
                    "expect_pyproj_version": h.CONFIG.expect_pyproj_version,
                    "passed": passed,
                    "failed": failed,
                    "tests": results,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if not results:
        print("error: no tests collected", file=sys.stderr)
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    if code:
        # Exit straight away with the code; returning normally would let
        # Blender quit with 0.
        sys.exit(code)
