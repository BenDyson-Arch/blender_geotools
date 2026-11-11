#!/usr/bin/env python3
"""Fetch the pinned rusty-tiles wheels into geotools/wheels/.

The pin and the target platforms come from ``[tool.geotools]`` in pyproject.toml.
Fetched wheels are never committed; scripts/build_extension.py bundles whatever
this script leaves in geotools/wheels/.

Usage:
    python scripts/fetch_wheels.py                      # download from the index (pip)
    python scripts/fetch_wheels.py --from-dir DIR       # copy from a local directory
    python scripts/fetch_wheels.py --platform linux-x64 # restrict to some platforms

Stdlib only (Python 3.11+). Default mode needs pip in the running interpreter;
pip honours PIP_INDEX_URL and friends if a different index is needed.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
WHEELS_DIR = REPO_ROOT / "geotools" / "wheels"

DIST_NAME = "rusty-tiles"
# Python of the oldest supported Blender (5.2 LTS ships CPython 3.13).
BLENDER_PYTHON = (3, 13)

# Blender platform -> wheel platform tag passed to `pip download --platform`.
# rusty-tiles builds its release wheels with maturin (cp310-abi3):
#   Linux:   manylinux 2_28 (x86_64 and aarch64)
#   macOS:   MACOSX_DEPLOYMENT_TARGET=15.0 (arm64 and x86_64)
#   Windows: x86_64-pc-windows-msvc -> win_amd64
# pip expands these to every compatible older tag (manylinux_2_17,
# manylinux2014, macosx_11_0, ...), so a wheel built for an older baseline
# is still found.
PIP_PLATFORM_TAGS = {
    "linux-x64": "manylinux_2_28_x86_64",
    "linux-arm64": "manylinux_2_28_aarch64",
    "macos-arm64": "macosx_15_0_arm64",
    "macos-x64": "macosx_15_0_x86_64",
    "windows-x64": "win_amd64",
}

# Mirrors blender_platforms_from_wheel_platform() in Blender's
# addons_core/bl_pkg/cli/blender_ext.py, so this script picks wheels for a
# platform exactly the way `blender --command extension build --split-platforms`
# will later filter them.
_SYSTEM_FROM_WHEEL = {
    "macosx": "macos",
    "manylinux": "linux",
    "musllinux": "linux",
    "win": "windows",
}
_MACHINE_FROM_WHEEL = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "aarch32": "arm32"}


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class WheelName:
    filename: str
    distribution: str
    version: str
    python_tag: str
    abi_tag: str
    platform_tag: str

    @classmethod
    def parse(cls, filename: str) -> WheelName:
        if not filename.endswith(".whl"):
            raise ValueError(f"not a wheel: {filename}")
        parts = filename[: -len(".whl")].split("-")
        if len(parts) not in (5, 6):
            raise ValueError(f"wheel filename does not follow the spec: {filename}")
        return cls(filename, parts[0], parts[1], parts[-3], parts[-2], parts[-1])

    @property
    def blender_platforms(self) -> set[str]:
        result: set[str] = set()
        for tag in self.platform_tag.split("."):
            result.update(blender_platforms_from_wheel_platform(tag))
        return result

    @property
    def is_portable_linux(self) -> bool:
        return not any(t.startswith("linux_") for t in self.platform_tag.split("."))


def blender_platforms_from_wheel_platform(tag: str) -> list[str]:
    if tag == "any":
        return ["any"]
    head, sep, tail = tag.partition("_")
    if not sep:
        return [tag]
    for src, dst in _SYSTEM_FROM_WHEEL.items():
        if head == src or (head.startswith(src) and head[len(src) :].isdigit()):
            head = dst
            break
    for src, dst in _MACHINE_FROM_WHEEL.items():
        if tail == src or tail.endswith("_" + src):
            tail = dst
            break
    else:
        tail = tail.rpartition("_")[2]
    tails = ["x64", "arm64"] if (head, tail) == ("macos", "universal2") else [tail]
    return [f"{head}-{t}" for t in tails]


def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def normalize_version(version: str) -> str:
    return version.strip().lower().replace("_", "-")


def python_compatible(wheel: WheelName, python: tuple[int, int] = BLENDER_PYTHON) -> bool:
    """True if Blender's CPython can load the wheel (cpXY-abi3 with XY <= ours, or cpXY-cpXY)."""
    ours = f"cp{python[0]}{python[1]}"
    for py in wheel.python_tag.split("."):
        m = re.fullmatch(r"cp(\d)(\d+)", py)
        if not m:
            continue
        version = (int(m.group(1)), int(m.group(2)))
        abis = wheel.abi_tag.split(".")
        if "abi3" in abis and version[0] == python[0] and version <= python:
            return True
        if py == ours and ours in abis:
            return True
    return False


def read_config(pyproject: Path = PYPROJECT) -> tuple[str, list[str]]:
    try:
        with pyproject.open("rb") as fh:
            config = tomllib.load(fh)["tool"]["geotools"]
        pin = str(config[DIST_NAME])
        platforms = [str(p) for p in config["platforms"]]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as ex:
        raise FetchError(
            f"cannot read [tool.geotools] {DIST_NAME} / platforms from {pyproject}: {ex}"
        )
    if not platforms:
        raise FetchError(f"[tool.geotools] platforms in {pyproject} is empty")
    return pin, platforms


def wheel_metadata(path: Path) -> tuple[str, str]:
    """Return (Name, Version) from the wheel's .dist-info/METADATA."""
    try:
        with zipfile.ZipFile(path) as zf:
            meta = [n for n in zf.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
            if len(meta) != 1:
                raise FetchError(
                    f"{path.name}: expected one .dist-info/METADATA, found {len(meta)}"
                )
            text = zf.read(meta[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile as ex:
        raise FetchError(f"{path.name}: not a valid wheel archive ({ex})")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break  # end of headers
        key, sep, value = line.partition(":")
        if sep and key in ("Name", "Version"):
            fields.setdefault(key, value.strip())
    return fields.get("Name", ""), fields.get("Version", "")


def check_wheel(path: Path, pin: str) -> WheelName:
    """Verify a wheel is rusty-tiles at the pinned version and loadable by Blender's Python."""
    wheel = WheelName.parse(path.name)
    if normalize_name(wheel.distribution) != DIST_NAME:
        raise FetchError(f"{path.name}: not a {DIST_NAME} wheel")
    if normalize_version(wheel.version) != normalize_version(pin):
        raise FetchError(f"{path.name}: version {wheel.version} does not match the pin {pin}")
    name, version = wheel_metadata(path)
    if normalize_name(name) != DIST_NAME or normalize_version(version) != normalize_version(pin):
        raise FetchError(f"{path.name}: METADATA says {name} {version}, expected {DIST_NAME} {pin}")
    if not python_compatible(wheel):
        raise FetchError(
            f"{path.name}: tags {wheel.python_tag}-{wheel.abi_tag} cannot load in "
            f"Blender's CPython {BLENDER_PYTHON[0]}.{BLENDER_PYTHON[1]}"
        )
    return wheel


def pick_wheel(candidates: list[WheelName], platform: str) -> WheelName | None:
    """Choose one wheel for a Blender platform, preferring portable (manylinux) over linux_* tags."""
    matching = [w for w in candidates if platform in w.blender_platforms]
    if not matching:
        return None
    matching.sort(key=lambda w: (not w.is_portable_linux, w.filename))
    return matching[0]


def collect_from_dir(source: Path, pin: str, platforms: list[str], dest: Path) -> list[Path]:
    if not source.is_dir():
        raise FetchError(f"--from-dir {source} is not a directory")
    candidates: list[WheelName] = []
    skipped: list[str] = []
    for path in sorted(source.glob("*.whl")):
        try:
            wheel = WheelName.parse(path.name)
        except ValueError:
            continue
        if normalize_name(wheel.distribution) != DIST_NAME:
            continue
        if normalize_version(wheel.version) != normalize_version(pin):
            skipped.append(f"{path.name} (version {wheel.version}, pin is {pin})")
            continue
        if not python_compatible(wheel):
            skipped.append(f"{path.name} (python tag {wheel.python_tag}-{wheel.abi_tag})")
            continue
        candidates.append(wheel)

    chosen: dict[str, WheelName] = {}
    missing: list[str] = []
    for platform in platforms:
        wheel = pick_wheel(candidates, platform)
        if wheel is None:
            missing.append(platform)
        else:
            chosen[platform] = wheel
    if missing:
        msg = [f"no {DIST_NAME} {pin} wheel in {source} for: {', '.join(missing)}"]
        if skipped:
            msg.append("ignored wheels: " + "; ".join(skipped))
        raise FetchError("\n".join(msg))

    copied: list[Path] = []
    for platform, wheel in chosen.items():
        target = dest / wheel.filename
        if not target.exists():
            shutil.copy2(source / wheel.filename, target)
            copied.append(target)
        if not wheel.is_portable_linux:
            print(
                f"warning: {wheel.filename} is tagged {wheel.platform_tag}, not manylinux; "
                "fine for local builds, do not ship it",
                file=sys.stderr,
            )
        print(f"{platform}: {wheel.filename}")
    return copied


def download(pin: str, platforms: list[str], dest: Path) -> None:
    unknown = [p for p in platforms if p not in PIP_PLATFORM_TAGS]
    if unknown:
        raise FetchError(
            f"no wheel tag known for platform(s) {', '.join(unknown)}; "
            f"known: {', '.join(PIP_PLATFORM_TAGS)}"
        )
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], check=False, capture_output=True, text=True
    )
    if probe.returncode != 0:
        raise FetchError(
            f"pip is not available in {sys.executable}; run this script with a Python "
            "that has pip, or use --from-dir"
        )

    failures: list[str] = []
    for platform in platforms:
        with tempfile.TemporaryDirectory(prefix="geotools-wheel-") as tmp:
            cmd = [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--only-binary=:all:",
                "--no-deps",
                "--python-version",
                f"{BLENDER_PYTHON[0]}.{BLENDER_PYTHON[1]}",
                "--implementation",
                "cp",
                "--abi",
                "abi3",
                "--platform",
                PIP_PLATFORM_TAGS[platform],
                "--disable-pip-version-check",
                "--no-cache-dir",
                "-d",
                tmp,
                f"{DIST_NAME}=={pin}",
            ]
            print(
                f"{platform}: pip download --platform {PIP_PLATFORM_TAGS[platform]} {DIST_NAME}=={pin}"
            )
            result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if result.returncode != 0:
                tail = "\n".join(
                    "    " + line
                    for line in (result.stderr or result.stdout).strip().splitlines()[-6:]
                )
                failures.append(
                    f"{platform} ({PIP_PLATFORM_TAGS[platform]}): pip download failed\n{tail}"
                )
                continue
            files = list(Path(tmp).glob("*.whl"))
            wheels = [WheelName.parse(f.name) for f in files]
            wheel = pick_wheel(wheels, platform)
            if wheel is None:
                failures.append(
                    f"{platform}: pip downloaded {[f.name for f in files]}, none for this platform"
                )
                continue
            check_wheel(Path(tmp) / wheel.filename, pin)
            target = dest / wheel.filename
            if not target.exists():
                shutil.copy2(Path(tmp) / wheel.filename, target)
            print(f"{platform}: {wheel.filename}")
    if failures:
        raise FetchError(
            f"could not download {DIST_NAME}=={pin} for every platform "
            "(is that version published? for local wheels use --from-dir DIR):\n"
            + "\n".join(failures)
        )


def clear_wheels_dir(wheels_dir: Path) -> None:
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir)
    wheels_dir.mkdir(parents=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--from-dir",
        type=Path,
        metavar="DIR",
        help="copy matching wheels from a local directory instead of downloading",
    )
    parser.add_argument(
        "--platform",
        action="append",
        metavar="NAME",
        dest="platforms",
        help="Blender platform to fetch (repeatable, or comma separated); default: all "
        "platforms in pyproject.toml",
    )
    parser.add_argument(
        "--wheels-dir",
        type=Path,
        default=WHEELS_DIR,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)

    try:
        pin, configured = read_config()
        if args.platforms:
            platforms = [p.strip() for arg in args.platforms for p in arg.split(",") if p.strip()]
            extra = [p for p in platforms if p not in configured]
            if extra:
                raise FetchError(
                    f"platform(s) {', '.join(extra)} are not in [tool.geotools] platforms "
                    f"({', '.join(configured)})"
                )
            platforms = list(dict.fromkeys(platforms))
        else:
            platforms = configured

        wheels_dir: Path = args.wheels_dir
        clear_wheels_dir(wheels_dir)
        print(f"{DIST_NAME} {pin} -> {wheels_dir} for {', '.join(platforms)}")

        # Gather into a temporary directory so a failure never leaves a partial set behind.
        with tempfile.TemporaryDirectory(prefix="geotools-wheels-") as tmp:
            staging = Path(tmp)
            if args.from_dir is not None:
                collect_from_dir(args.from_dir.expanduser().resolve(), pin, platforms, staging)
            else:
                download(pin, platforms, staging)
            for path in sorted(staging.glob("*.whl")):
                check_wheel(path, pin)
                shutil.copy2(path, wheels_dir / path.name)
    except FetchError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1

    print(f"wheels in {wheels_dir}:")
    for path in sorted(wheels_dir.glob("*.whl")):
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
