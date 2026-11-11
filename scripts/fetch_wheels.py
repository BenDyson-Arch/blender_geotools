#!/usr/bin/env python3
"""Fetch the pinned wheels (rusty-tiles, pyproj, certifi) into geotools/wheels/.

The pins and the target platforms come from ``[tool.geotools]`` and
``[tool.geotools.wheels]`` in pyproject.toml. Fetched wheels are never
committed; scripts/build_extension.py bundles whatever this script leaves in
geotools/wheels/.

Usage:
    python scripts/fetch_wheels.py                        # every pin, every platform, from the index
    python scripts/fetch_wheels.py --platform linux-x64   # restrict to some platforms
    python scripts/fetch_wheels.py --from-dir DIR         # every pin from a local directory
    python scripts/fetch_wheels.py --from-dir rusty-tiles=DIR   # one pin locally, the rest from the index
    python scripts/fetch_wheels.py --platform linux-x64 --python 3.14   # local Blender on CPython 3.14

Stdlib only (Python 3.11+). Downloads use pip from the running interpreter, or
``uvx pip`` when that interpreter has no pip; pip honours PIP_INDEX_URL and
friends if a different index is needed.
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

RUSTY_TILES = "rusty-tiles"
"""The one pin every build needs; the others are read from the same table."""

# Python of the oldest supported Blender (5.2 LTS ships CPython 3.13). Release
# builds target exactly this; other versions are for local builds only.
BLENDER_PYTHON = (3, 13)

# Blender platform -> wheel platform tag passed to `pip download --platform`.
# pip expands each tag to every compatible older one (manylinux_2_17,
# manylinux2014, macosx_11_0, ...), so a wheel built for an older baseline is
# still found. The newest baselines among the pins:
#   rusty-tiles (maturin, cp310-abi3): manylinux_2_28; macOS 15.0 deployment
#     target for arm64 and x86_64; win_amd64.
#   pyproj 3.8.0 (cp313): manylinux_2_28_x86_64, macosx_14_0_arm64,
#     macosx_15_0_x86_64, win_amd64.
# Blender ignores the macOS version in a wheel tag (it only matches the
# architecture), so these tags set the real minimum macOS of the zip.
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
    def name(self) -> str:
        """Normalised distribution name, as used for the pins."""
        return normalize_name(self.distribution)

    @property
    def blender_platforms(self) -> set[str]:
        result: set[str] = set()
        for tag in self.platform_tag.split("."):
            result.update(blender_platforms_from_wheel_platform(tag))
        return result

    @property
    def is_pure(self) -> bool:
        """Platform independent (``any``): one wheel serves every platform."""
        return self.blender_platforms == {"any"}

    @property
    def is_portable_linux(self) -> bool:
        return not any(t.startswith("linux_") for t in self.platform_tag.split("."))

    def covers(self, platform: str) -> bool:
        return self.is_pure or platform in self.blender_platforms


@dataclass(frozen=True)
class Config:
    """``[tool.geotools]``: the pins (normalised name -> version) and the platforms."""

    pins: dict[str, str]
    platforms: list[str]

    @property
    def rusty_tiles(self) -> str:
        return self.pins[RUSTY_TILES]


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


def parse_python(text: str) -> tuple[int, int]:
    """``"3.14"`` -> ``(3, 14)``."""
    match = re.fullmatch(r"\s*(\d+)\.(\d+)\s*", text)
    if not match or match.group(1) != "3":
        raise argparse.ArgumentTypeError(f"expected a CPython version like 3.13, got {text!r}")
    return int(match.group(1)), int(match.group(2))


def python_label(python: tuple[int, int]) -> str:
    return f"{python[0]}.{python[1]}"


def python_compatible(wheel: WheelName, python: tuple[int, int] = BLENDER_PYTHON) -> bool:
    """True if this CPython can load the wheel.

    Accepts ``cpXY-cpXY`` for exactly our version, ``cpXY-abi3`` with XY <= ours,
    and pure ``pyX[Y]-none`` wheels. Rejects free-threaded (``cp313t``) builds.
    Matches the Python check of Blender's own wheel filter.
    """
    ours = f"cp{python[0]}{python[1]}"
    abis = wheel.abi_tag.split(".")
    for py in wheel.python_tag.split("."):
        if m := re.fullmatch(r"py(\d)(\d*)", py):
            major, minor = int(m.group(1)), m.group(2)
            if "none" in abis and major == python[0] and (not minor or int(minor) <= python[1]):
                return True
            continue
        m = re.fullmatch(r"cp(\d)(\d+)", py)
        if not m:
            continue
        version = (int(m.group(1)), int(m.group(2)))
        if "abi3" in abis and version[0] == python[0] and version <= python:
            return True
        if py == ours and ours in abis:
            return True
    return False


def read_config(pyproject: Path = PYPROJECT) -> Config:
    try:
        with pyproject.open("rb") as fh:
            config = tomllib.load(fh)["tool"]["geotools"]
        table = config["wheels"]
        platforms = [str(p) for p in config["platforms"]]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as ex:
        raise FetchError(
            f"cannot read [tool.geotools] platforms / [tool.geotools.wheels] from {pyproject}: "
            f"{ex!r}"
        )
    if not isinstance(table, dict) or not table:
        raise FetchError(f"[tool.geotools.wheels] in {pyproject} must be a non-empty table")
    pins: dict[str, str] = {}
    for name, version in table.items():
        if not isinstance(version, str) or not version.strip():
            raise FetchError(f"[tool.geotools.wheels] {name} must be a version string")
        key = normalize_name(name)
        if key in pins:
            raise FetchError(f"[tool.geotools.wheels] pins {key} twice")
        pins[key] = version.strip()
    if RUSTY_TILES not in pins:
        raise FetchError(f"[tool.geotools.wheels] in {pyproject} has no {RUSTY_TILES} pin")
    if not platforms:
        raise FetchError(f"[tool.geotools] platforms in {pyproject} is empty")
    return Config(pins, platforms)


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


def check_wheel(
    path: Path, pins: dict[str, str], python: tuple[int, int] = BLENDER_PYTHON
) -> WheelName:
    """Verify a wheel is a pinned distribution at its pin and loadable by this CPython."""
    wheel = WheelName.parse(path.name)
    pin = pins.get(wheel.name)
    if pin is None:
        raise FetchError(
            f"{path.name}: {wheel.name} is not pinned in [tool.geotools.wheels] ({', '.join(pins)})"
        )
    if normalize_version(wheel.version) != normalize_version(pin):
        raise FetchError(f"{path.name}: version {wheel.version} does not match the pin {pin}")
    name, version = wheel_metadata(path)
    if normalize_name(name) != wheel.name or normalize_version(version) != normalize_version(pin):
        raise FetchError(
            f"{path.name}: METADATA says {name} {version}, expected {wheel.name} {pin}"
        )
    if not python_compatible(wheel, python):
        raise FetchError(
            f"{path.name}: tags {wheel.python_tag}-{wheel.abi_tag} cannot load in "
            f"CPython {python_label(python)}"
        )
    return wheel


def pick_wheel(candidates: list[WheelName], platform: str) -> WheelName | None:
    """Choose one wheel for a Blender platform.

    Prefers a platform wheel over a pure one, and portable (manylinux) tags over
    linux_* tags.
    """
    matching = [w for w in candidates if w.covers(platform)]
    if not matching:
        return None
    matching.sort(key=lambda w: (w.is_pure, not w.is_portable_linux, w.filename))
    return matching[0]


def collect_from_dir(
    source: Path,
    dist: str,
    pin: str,
    platforms: list[str],
    dest: Path,
    python: tuple[int, int],
) -> None:
    if not source.is_dir():
        raise FetchError(f"--from-dir {source} is not a directory")
    candidates: list[WheelName] = []
    skipped: list[str] = []
    for path in sorted(source.glob("*.whl")):
        try:
            wheel = WheelName.parse(path.name)
        except ValueError:
            continue
        if wheel.name != dist:
            continue
        if normalize_version(wheel.version) != normalize_version(pin):
            skipped.append(f"{path.name} (version {wheel.version}, pin is {pin})")
            continue
        if not python_compatible(wheel, python):
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
        msg = [f"no {dist} {pin} wheel in {source} for: {', '.join(missing)}"]
        if skipped:
            msg.append("ignored wheels: " + "; ".join(skipped))
        raise FetchError("\n".join(msg))

    for platform, wheel in chosen.items():
        target = dest / wheel.filename
        if not target.exists():
            shutil.copy2(source / wheel.filename, target)
        if not wheel.is_portable_linux:
            print(
                f"warning: {wheel.filename} is tagged {wheel.platform_tag}, not manylinux; "
                "fine for local builds, do not ship it",
                file=sys.stderr,
            )
        print(f"  {platform}: {wheel.filename} (from {source})")


def pip_command() -> list[str]:
    """pip of the running interpreter, else ``uvx pip``."""
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "--version"], check=False, capture_output=True, text=True
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", "pip"]
    uvx = shutil.which("uvx")
    if uvx is not None:
        print(f"note: no pip in {sys.executable}; using uvx pip", file=sys.stderr)
        return [uvx, "pip"]
    raise FetchError(
        f"pip is not available in {sys.executable} and uvx is not on PATH; run this script "
        "with a Python that has pip, or use --from-dir"
    )


def download(
    pip: list[str],
    dist: str,
    pin: str,
    platforms: list[str],
    dest: Path,
    python: tuple[int, int],
) -> None:
    unknown = [p for p in platforms if p not in PIP_PLATFORM_TAGS]
    if unknown:
        raise FetchError(
            f"no wheel tag known for platform(s) {', '.join(unknown)}; "
            f"known: {', '.join(PIP_PLATFORM_TAGS)}"
        )
    cp = f"cp{python[0]}{python[1]}"
    failures: list[str] = []
    fetched: list[WheelName] = []
    for platform in platforms:
        if pick_wheel(fetched, platform) is not None:
            continue  # a pure wheel fetched for an earlier platform covers this one
        with tempfile.TemporaryDirectory(prefix="geotools-wheel-") as tmp:
            cmd = [
                *pip,
                "download",
                "--only-binary=:all:",
                "--no-deps",
                "--python-version",
                python_label(python),
                "--implementation",
                "cp",
                # Native (cp313-cp313), stable ABI (cp310-abi3) and pure (py3-none) wheels.
                "--abi",
                cp,
                "--abi",
                "abi3",
                "--abi",
                "none",
                "--platform",
                PIP_PLATFORM_TAGS[platform],
                "--disable-pip-version-check",
                "--no-cache-dir",
                "-d",
                tmp,
                f"{dist}=={pin}",
            ]
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
            wheel = pick_wheel([WheelName.parse(f.name) for f in files], platform)
            if wheel is None:
                failures.append(
                    f"{platform}: pip downloaded {[f.name for f in files]}, none for this platform"
                )
                continue
            check_wheel(Path(tmp) / wheel.filename, {dist: pin}, python)
            target = dest / wheel.filename
            if not target.exists():
                shutil.copy2(Path(tmp) / wheel.filename, target)
            fetched.append(wheel)
            covered = "every platform" if wheel.is_pure else platform
            print(f"  {covered}: {wheel.filename}")
    if failures:
        raise FetchError(
            f"could not download {dist}=={pin} for every platform "
            "(is that version published? for local wheels use --from-dir [NAME=]DIR):\n"
            + "\n".join(failures)
        )


def parse_sources(values: list[str] | None, pins: dict[str, str]) -> dict[str, Path | None]:
    """Map each pinned distribution to a local directory, or None for the index.

    ``DIR`` applies to every pin; ``NAME=DIR`` to one and wins over ``DIR``.
    """
    default: Path | None = None
    named: dict[str, Path] = {}
    for value in values or []:
        name, sep, path = value.partition("=")
        if sep and normalize_name(name) in pins:
            named[normalize_name(name)] = Path(path).expanduser().resolve()
        elif sep and not Path(value).exists():
            raise FetchError(
                f"--from-dir {value}: {name} is not pinned in [tool.geotools.wheels] "
                f"({', '.join(pins)})"
            )
        else:
            if default is not None:
                raise FetchError("--from-dir DIR (without NAME=) can be given only once")
            default = Path(value).expanduser().resolve()
    return {dist: named.get(dist, default) for dist in pins}


def clear_wheels_dir(wheels_dir: Path) -> None:
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir)
    wheels_dir.mkdir(parents=True)


def platform_sizes(wheels: list[Path], platforms: list[str]) -> dict[str, int]:
    """Total wheel bytes each platform zip will carry."""
    sizes = dict.fromkeys(platforms, 0)
    for path in wheels:
        wheel = WheelName.parse(path.name)
        for platform in platforms:
            if wheel.covers(platform):
                sizes[platform] += path.stat().st_size
    return sizes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--from-dir",
        action="append",
        metavar="[NAME=]DIR",
        help="copy wheels from a local directory instead of downloading: DIR for every "
        "pin, NAME=DIR for one (repeatable); pins without a directory are downloaded",
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
        "--python",
        type=parse_python,
        default=BLENDER_PYTHON,
        metavar="X.Y",
        help=f"CPython the wheels must load in (default {python_label(BLENDER_PYTHON)}, "
        "Blender 5.2 LTS); another version is for a local Blender only, never a release",
    )
    parser.add_argument(
        "--wheels-dir",
        type=Path,
        default=WHEELS_DIR,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    python: tuple[int, int] = args.python

    try:
        config = read_config()
        configured = config.platforms
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
        sources = parse_sources(args.from_dir, config.pins)

        if python != BLENDER_PYTHON:
            print(
                f"warning: fetching wheels for CPython {python_label(python)}, not "
                f"{python_label(BLENDER_PYTHON)}: the zip only installs in a Blender with that "
                "Python; use it for local testing, never for a release",
                file=sys.stderr,
            )

        wheels_dir: Path = args.wheels_dir
        clear_wheels_dir(wheels_dir)
        pins = ", ".join(f"{d} {v}" for d, v in config.pins.items())
        print(f"{pins} -> {wheels_dir} for {', '.join(platforms)} (CPython {python_label(python)})")

        # Gather into a temporary directory so a failure never leaves a partial set behind.
        pip: list[str] | None = None
        with tempfile.TemporaryDirectory(prefix="geotools-wheels-") as tmp:
            staging = Path(tmp)
            for dist, pin in config.pins.items():
                print(f"{dist} {pin}:")
                source = sources[dist]
                if source is not None:
                    collect_from_dir(source, dist, pin, platforms, staging, python)
                else:
                    pip = pip or pip_command()
                    download(pip, dist, pin, platforms, staging, python)
            for path in sorted(staging.glob("*.whl")):
                check_wheel(path, config.pins, python)
                shutil.copy2(path, wheels_dir / path.name)
    except FetchError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1

    wheels = sorted(wheels_dir.glob("*.whl"))
    print(f"wheels in {wheels_dir}:")
    for path in wheels:
        print(f"  {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
    for platform, size in platform_sizes(wheels, platforms).items():
        print(f"  {platform}: {size / 1e6:.1f} MB of wheels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
