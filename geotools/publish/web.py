"""Extract for Web: a published archive as a folder a web server can serve.

bpy-free. Web viewers such as CesiumJS read a tileset from a server, not from a
``.3tz``. For ``<dir>/<name>.3tz`` this writes ``<dir>/<name>.web/``:

    tileset.json        and the tiles, as in the archive (without its index)
    index.html          an optional CesiumJS page that shows ./tileset.json; not
                        part of the tileset (``viewer.html`` in this package)
    .geotools-web.json  marks the folder as GeoTools' own and lists the files it
                        wrote, with their SHA-256, so it can be replaced

The folder is only ever replaced when it carries the marker (or is empty), so a
folder of the user's that happens to have the same name is never deleted. The
marker only covers the files it lists: an ``index.html`` the user edited or
replaced is carried over to the new folder, and any other file the user added or
changed stops the extract until they move it. The new copy is extracted into a
hidden sibling first; the old folder is renamed aside, the new one renamed into
place, and only then is the old one deleted, so a failure at any step leaves
the old folder (and its marker) whole.
"""

from __future__ import annotations

import hashlib
import json
import secrets
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
    viewer: bool  # GeoTools' index.html was written
    kept_page: bool = False  # the user's own index.html was carried over instead


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> dict[str, Path]:
    """Every file under ``root`` (symlinks included, not followed), by POSIX relative path."""
    return {
        p.relative_to(root).as_posix(): p
        for p in sorted(root.rglob("*"))
        if (p.is_file() or p.is_symlink()) and p.name != MARKER_NAME
    }


def _untracked(dest: Path) -> list[str]:
    """Files in GeoTools' ``dest`` that its marker does not list with their current hash."""
    try:
        marker = json.loads((dest / MARKER_NAME).read_text(encoding="utf-8"))
        recorded = marker["files"]
        if not isinstance(recorded, dict):
            raise TypeError("files is not an object")
    except (OSError, ValueError, KeyError, TypeError):
        recorded = {}  # an unreadable marker vouches for nothing
    return [
        name
        for name, path in _files(dest).items()
        if path.is_symlink() or recorded.get(name) != _sha256(path)
    ]


def _swap(work: Path, dest: Path) -> None:
    """Put ``work`` at ``dest``; on failure the old ``dest`` is left whole."""
    if not dest.exists():
        work.rename(dest)
        return
    backup = dest.with_name(f".{dest.name}-old-{secrets.token_hex(4)}")
    dest.rename(backup)  # fails cleanly if something holds the folder open
    try:
        work.rename(dest)
    except BaseException:
        backup.rename(dest)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def extract_for_web(archive: Path, dest: Path | None = None) -> WebFolder:
    """Extract ``archive`` into ``dest`` (default :func:`web_dir_for`) with a viewer page."""
    archive = Path(archive)
    dest = Path(dest) if dest is not None else web_dir_for(archive)
    if not archive.is_file():
        raise WebExtractError(f"{archive} does not exist; publish first")
    problem = replace_problem(dest)
    if problem:
        raise WebExtractError(problem)
    untracked = _untracked(dest) if is_geotools_web_dir(dest) else []
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f".{dest.name}-", dir=dest.parent))
    try:
        try:
            pv.extract_archive(archive, work)
        except (pv.PreviewError, zipfile.BadZipFile) as ex:
            raise WebExtractError(f"could not extract {archive.name}: {ex}") from ex
        written = _files(work)
        # The user's own page is kept, unless the archive brings an index.html.
        kept_page = VIEWER_NAME in untracked and VIEWER_NAME not in written
        others = [name for name in untracked if name != VIEWER_NAME or not kept_page]
        if others:
            shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
            raise WebExtractError(
                f"{dest.name} has files Extract for Web did not write or that were "
                f"changed since ({shown}); move them out of the folder first"
            )
        viewer = VIEWER_NAME not in written and not kept_page
        if kept_page:
            shutil.copy2(dest / VIEWER_NAME, work / VIEWER_NAME)
        elif viewer:
            (work / VIEWER_NAME).write_text(viewer_html(), encoding="utf-8")
            written[VIEWER_NAME] = work / VIEWER_NAME
        marker = {
            "written_by": "GeoTools Extract for Web",
            "archive": archive.name,
            "viewer": VIEWER_NAME if viewer else None,
            "cesium": CESIUM_VERSION if viewer else None,
            # What GeoTools may replace next time; a kept index.html is not listed.
            "files": {name: _sha256(path) for name, path in written.items()},
        }
        # Written last: its time stamp is what is_stale() compares.
        (work / MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        _swap(work, dest)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return WebFolder(dest, viewer, kept_page)
