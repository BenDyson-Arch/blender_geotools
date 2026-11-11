#!/usr/bin/env python3
"""Build, install and test the GeoTools extension in a headless Blender.

Steps, all inside a throwaway Blender profile (BLENDER_USER_RESOURCES), so the
real user configuration is never touched:

1. build the extension zip with scripts/build_extension.py (or take --zip);
2. ``blender --command extension install-file -r user_default -e ZIP``;
3. start Blender with that profile and run tests/blender/run_suite.py against
   the installed ``bl_ext.user_default.<id>`` package;
4. delete the profile (unless --keep).

Usage:
    python scripts/run_blender_tests.py --expect missing      # wheel-less build
    python scripts/run_blender_tests.py --expect available    # bundles geotools/wheels/
    python scripts/run_blender_tests.py --zip dist/geotools-0.3.0-dev.zip --expect missing

With --expect available the build bundles whatever scripts/fetch_wheels.py put
in geotools/wheels/ and the expected rusty-tiles version defaults to the pin in
pyproject.toml. With --expect missing it builds with --no-wheels.

Stdlib only (Python 3.12+). Works on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_wheels import REPO_ROOT, FetchError, read_config

SUITE = REPO_ROOT / "tests" / "blender" / "run_suite.py"
BUILD_SCRIPT = REPO_ROOT / "scripts" / "build_extension.py"
EXTENSION_REPO = "user_default"
BLENDER_TIMEOUT = 900  # seconds, per Blender invocation

# Environment variables that would override BLENDER_USER_RESOURCES for one
# kind of user path and so leak the real profile into the test run.
_USER_PATH_OVERRIDES = (
    "BLENDER_USER_CONFIG",
    "BLENDER_USER_SCRIPTS",
    "BLENDER_USER_EXTENSIONS",
    "BLENDER_USER_DATAFILES",
)


class RunError(Exception):
    pass


def host_platform() -> str:
    """This machine as a Blender platform name (linux-x64, macos-arm64, ...)."""
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system is None or arch is None:
        raise RunError(f"unsupported host platform {platform.system()} {platform.machine()}")
    return f"{system}-{arch}"


def find_blender(blender: str) -> str:
    resolved = shutil.which(blender)
    if resolved is None:
        raise RunError(f"Blender not found: {blender!r} (pass --blender PATH or set BLENDER)")
    return resolved


def zip_manifest(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.rstrip("/").endswith("blender_manifest.toml")]
            names.sort(key=lambda n: n.count("/"))
            if not names:
                raise RunError(f"{path} has no blender_manifest.toml")
            return tomllib.loads(zf.read(names[0]).decode("utf-8"))
    except (zipfile.BadZipFile, tomllib.TOMLDecodeError, OSError) as ex:
        raise RunError(f"cannot read the manifest in {path}: {ex}")


def build(blender: str, out_dir: Path, with_wheels: bool, env: dict[str, str]) -> Path:
    cmd = [sys.executable, str(BUILD_SCRIPT), "--blender", blender, "--out", str(out_dir)]
    if not with_wheels:
        cmd.append("--no-wheels")
    print("$ " + " ".join(cmd), flush=True)
    if subprocess.run(cmd, env=env, check=False).returncode != 0:
        raise RunError("scripts/build_extension.py failed")
    zips = sorted(out_dir.glob("*.zip"))
    if len(zips) == 1:
        return zips[0]
    # Split-platform build: pick this machine's zip.
    suffix = "_" + host_platform().replace("-", "_") + ".zip"
    matching = [z for z in zips if z.name.endswith(suffix)]
    if len(matching) != 1:
        raise RunError(
            f"cannot pick a zip for {host_platform()} from: {', '.join(z.name for z in zips)}"
        )
    return matching[0]


def isolated_env(user_resources: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _USER_PATH_OVERRIDES}
    env["BLENDER_USER_RESOURCES"] = str(user_resources)
    return env


def run_blender(cmd: list[str], env: dict[str, str]) -> int:
    print("$ " + " ".join(cmd), flush=True)
    try:
        return subprocess.run(cmd, env=env, check=False, timeout=BLENDER_TIMEOUT).returncode
    except subprocess.TimeoutExpired:
        raise RunError(f"Blender did not finish within {BLENDER_TIMEOUT}s")


def install(blender: str, zip_path: Path, ext_id: str, env: dict[str, str], root: Path) -> None:
    code = run_blender(
        [
            blender,
            "--factory-startup",
            "--command",
            "extension",
            "install-file",
            "-r",
            EXTENSION_REPO,
            "-e",
            str(zip_path),
        ],
        env,
    )
    if code != 0:
        raise RunError(f"extension install-file failed (exit code {code})")
    installed = root / "extensions" / EXTENSION_REPO / ext_id / "blender_manifest.toml"
    if not installed.is_file():
        raise RunError(f"install-file exited 0 but {installed} does not exist")
    if not (root / "config" / "userpref.blend").is_file():
        raise RunError("install-file -e did not save the preferences that enable the extension")


def run_suite(
    blender: str,
    env: dict[str, str],
    ext_id: str,
    ext_version: str,
    expect: str,
    expect_version: str | None,
    keyword: str | None,
    results: Path,
) -> dict:
    # No --factory-startup: the suite checks that the preferences saved by
    # install-file enable the extension when Blender starts.
    cmd = [
        blender,
        "--background",
        "--python-exit-code",
        "1",
        "--python",
        str(SUITE),
        "--",
        "--repo",
        EXTENSION_REPO,
        "--extension-id",
        ext_id,
        "--extension-version",
        ext_version,
        "--expect",
        expect,
        "--results",
        str(results),
    ]
    if expect_version:
        cmd += ["--expect-version", expect_version]
    if keyword:
        cmd += ["-k", keyword]
    code = run_blender(cmd, env)
    # Require the report as well as the exit code, so a suite that never ran
    # (or a Blender that ignores --python-exit-code) cannot pass.
    try:
        report = json.loads(results.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise RunError(f"the in-Blender suite wrote no report (exit code {code}): {ex}")
    if code != 0 or report.get("failed") != 0 or not report.get("passed"):
        failed = [t["name"] for t in report.get("tests", []) if t.get("outcome") != "pass"]
        detail = f"; failed: {', '.join(failed)}" if failed else ""
        raise RunError(f"in-Blender suite failed (exit code {code}){detail}")
    return report


def _remove_readonly(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, onexc=_remove_readonly)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--blender",
        default=os.environ.get("BLENDER", "blender"),
        help="Blender executable (default: $BLENDER, else blender on PATH)",
    )
    parser.add_argument(
        "--expect",
        choices=("available", "missing"),
        required=True,
        help="expected rusty-tiles status in the installed extension",
    )
    parser.add_argument(
        "--expect-version",
        metavar="VERSION",
        help="expected rusty-tiles version with --expect available "
        "(default: the pin in pyproject.toml)",
    )
    parser.add_argument(
        "--zip",
        type=Path,
        help="install this extension zip instead of building one "
        "(default: build with wheels for --expect available, --no-wheels for missing)",
    )
    parser.add_argument("-k", dest="keyword", help="only run tests whose name contains this")
    parser.add_argument(
        "--keep", action="store_true", help="keep the temporary profile and build for debugging"
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    if args.expect == "missing" and args.expect_version:
        parser.error("--expect-version only makes sense with --expect available")

    tmp = Path(tempfile.mkdtemp(prefix="geotools-blender-tests-")).resolve()
    user_resources = tmp / "user"
    user_resources.mkdir()
    ok = False
    try:
        blender = find_blender(args.blender)
        expect_version = None
        if args.expect == "available":
            expect_version = args.expect_version or read_config()[0]

        env = isolated_env(user_resources)
        if args.zip is not None:
            zip_path = args.zip.expanduser().resolve()
            if not zip_path.is_file():
                raise RunError(f"--zip {zip_path} does not exist")
        else:
            zip_path = build(blender, tmp / "dist", args.expect == "available", env)
        manifest = zip_manifest(zip_path)
        ext_id, ext_version = str(manifest["id"]), str(manifest["version"])
        print(f"testing {zip_path.name} ({ext_id} {ext_version}) in {user_resources}")

        install(blender, zip_path, ext_id, env, user_resources)
        report = run_suite(
            blender,
            env,
            ext_id,
            ext_version,
            args.expect,
            expect_version,
            args.keyword,
            tmp / "results.json",
        )
        print(
            f"OK: {report['passed']} passed in Blender {report['blender']} "
            f"(Python {report['python']}), rusty-tiles {args.expect}"
            + (f" {expect_version}" if expect_version else "")
        )
        ok = True
    except (RunError, FetchError, KeyError) as ex:
        print(f"error: {ex}", file=sys.stderr)
    finally:
        if args.keep:
            print(f"kept {tmp}")
        else:
            remove_tree(tmp)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
