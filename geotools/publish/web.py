"""Extract for Web: a published archive as a folder a web server can serve.

bpy-free. Web viewers such as CesiumJS read a tileset from a server, not from a
``.3tz``. For ``<dir>/<name>.3tz`` this writes ``<dir>/<name>.web/``:

    tileset.json        and the tiles, as in the archive (without its index)
    index.html          an optional CesiumJS page that shows ./tileset.json; not
                        part of the tileset (``viewer.html`` in this package)
    .geotools-web.json  marks the folder as GeoTools' own, so it can be replaced

The folder is only ever replaced when it carries the marker (or is empty), so a
folder of the user's that happens to have the same name is never deleted. The
new copy is extracted into a hidden sibling first and swapped in once complete.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import preview as pv

WEB_SUFFIX = ".web"
MARKER_NAME = ".geotools-web.json"
VIEWER_NAME = "index.html"
CESIUM_VERSION = "1.146.0"  # the CesiumJS that viewer.html loads (and was checked with)

_TEMPLATE = Path(__file__).with_name("viewer.html")


class WebExtractError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebFolder:
    path: Path
    viewer: bool  # index.html was written (False if the archive has its own)


def web_dir_for(output: Path) -> Path:
    """``<dir>/<name>.web`` for ``<dir>/<name>.3tz``."""
    output = Path(output)
    if output.suffix != ".3tz" or not output.stem:
        raise ValueError(f"Extract for Web needs a .3tz, got {str(output)!r}")
    return output.with_name(output.stem + WEB_SUFFIX)


def viewer_html() -> str:
    """The CesiumJS page written as ``index.html``."""
    return _TEMPLATE.read_text(encoding="utf-8")


def is_geotools_web_dir(path: Path) -> bool:
    return (Path(path) / MARKER_NAME).is_file()


def replace_problem(dest: Path) -> str | None:
    """Why ``dest`` must not be replaced, or None."""
    dest = Path(dest)
    if not dest.exists() and not dest.is_symlink():
        return None
    if dest.is_symlink() or not dest.is_dir():
        return f"{dest.name} exists and is not a folder; move it away first"
    if is_geotools_web_dir(dest) or not any(dest.iterdir()):
        return None
    return f"{dest.name} exists and was not written by Extract for Web; move or rename it first"


def is_stale(archive: Path, dest: Path) -> bool:
    """True if ``dest`` was extracted from an older copy of ``archive``."""
    marker = Path(dest) / MARKER_NAME
    try:
        return Path(archive).stat().st_mtime_ns > marker.stat().st_mtime_ns
    except OSError:
        return False


def extract_for_web(archive: Path, dest: Path | None = None) -> WebFolder:
    """Extract ``archive`` into ``dest`` (default :func:`web_dir_for`) with a viewer page."""
    archive = Path(archive)
    dest = Path(dest) if dest is not None else web_dir_for(archive)
    if not archive.is_file():
        raise WebExtractError(f"{archive} does not exist; publish first")
    problem = replace_problem(dest)
    if problem:
        raise WebExtractError(problem)
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{dest.name}-", dir=dest.parent))
    try:
        try:
            pv.extract_archive(archive, work)
        except (pv.PreviewError, zipfile.BadZipFile) as ex:
            raise WebExtractError(f"could not extract {archive.name}: {ex}") from ex
        viewer = not (work / VIEWER_NAME).exists()
        if viewer:
            (work / VIEWER_NAME).write_text(viewer_html(), encoding="utf-8")
        marker = {
            "written_by": "GeoTools Extract for Web",
            "archive": archive.name,
            "viewer": VIEWER_NAME if viewer else None,
            "cesium": CESIUM_VERSION if viewer else None,
        }
        # Written last: its time stamp is what is_stale() compares.
        (work / MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        if dest.exists():
            shutil.rmtree(dest)
        work.rename(dest)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return WebFolder(dest, viewer)
