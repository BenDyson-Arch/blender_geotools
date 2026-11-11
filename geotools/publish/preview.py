"""Preview a published archive with ``rusty-tiles preview`` (the CLI).

bpy-free. CesiumJS cannot open a ``.3tz`` directly, so the archive is extracted
into the staging directory's ``preview/`` folder and served from there:

    rusty-tiles preview --cesium DIR --mesh EXTRACTED --port N --json

The Python wheel has no ``preview``; this needs a separately installed CLI and a
Cesium 1.146 ``Build/Cesium`` directory. Nothing is downloaded.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
import zipfile
from collections.abc import Callable
from pathlib import Path, PurePosixPath

log = logging.getLogger(__name__)

INDEX_MEMBER = "@3dtilesIndex1@"


class PreviewError(RuntimeError):
    pass


def cli_problem(cli: str) -> str | None:
    """Why ``cli`` cannot be used as the rusty-tiles CLI, or None."""
    if not cli.strip():
        return "Set the rusty-tiles CLI in the GeoTools preferences"
    path = Path(cli)
    if not path.is_file():
        return f"The rusty-tiles CLI {cli} does not exist"
    if os.name != "nt" and not os.access(path, os.X_OK):
        return f"The rusty-tiles CLI {cli} is not executable"
    return None


def cesium_problem(cesium_dir: str) -> str | None:
    """Why ``cesium_dir`` is not a Cesium ``Build/Cesium`` directory, or None."""
    if not cesium_dir.strip():
        return "Set the Cesium runtime folder in the GeoTools preferences"
    if not (Path(cesium_dir) / "Cesium.js").is_file():
        return f"No Cesium.js in {cesium_dir}; pick a Cesium Build/Cesium folder"
    return None


def extract_archive(archive: Path, dest: Path) -> Path:
    """Extract a ``.3tz`` into ``dest`` (emptied first); return ``dest``.

    Refuses absolute or parent-relative member names. The archive index member
    is skipped: the extracted directory is a plain tileset.
    """
    archive = Path(archive)
    if not archive.is_file():
        raise PreviewError(f"{archive} does not exist; publish first")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename
            parts = PurePosixPath(name).parts
            if name.startswith(("/", "\\")) or ".." in parts or ":" in name or "\\" in name:
                raise PreviewError(f"unsafe member name in {archive.name}: {name!r}")
            if name == INDEX_MEMBER or info.is_dir():
                continue
            target = root.joinpath(*parts)
            if not target.resolve().is_relative_to(root):
                raise PreviewError(f"unsafe member name in {archive.name}: {name!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
    if not (dest / "tileset.json").is_file():
        raise PreviewError(f"{archive.name} has no tileset.json")
    return dest


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def preview_command(cli: str, cesium_dir: str, mesh_dir: Path, port: int) -> list[str]:
    return [
        str(cli),
        "preview",
        "--cesium",
        str(cesium_dir),
        "--mesh",
        str(mesh_dir),
        "--port",
        str(port),
        "--json",
    ]


class PreviewServer:
    """One running ``rusty-tiles preview`` process.

    A reader thread waits for the CLI's startup JSON line, then opens the URL
    in the browser (``open_browser``). It never touches Blender.
    """

    def __init__(
        self,
        cli: str,
        cesium_dir: str,
        mesh_dir: Path,
        log_path: Path,
        *,
        open_browser: Callable[[str], object] | None = webbrowser.open,
    ) -> None:
        self.port = free_port()
        self.url: str | None = None
        self.error: str | None = None
        self.started = threading.Event()  # set once the URL or an error is known
        self._open_browser = open_browser
        self._log = log_path.open("wb")
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        try:
            self.process = subprocess.Popen(
                preview_command(cli, cesium_dir, mesh_dir, self.port),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._log,
                **kwargs,  # type: ignore[arg-type]
            )
        except OSError as ex:
            self._log.close()
            raise PreviewError(f"could not start {cli}: {ex}") from ex
        self._reader = threading.Thread(
            target=self._read_startup, name="geotools-preview", daemon=True
        )
        self._reader.start()

    def _read_startup(self) -> None:
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        try:
            data = json.loads(line) if line.strip() else None
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("url"), str):
            self.url = data["url"]
            if self._open_browser is not None:
                try:
                    self._open_browser(self.url)
                except Exception as ex:  # no browser is not fatal
                    log.warning("could not open %s", self.url, exc_info=True)
                    self.error = f"could not open a browser: {ex}"
        else:
            code = self.process.wait()
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                detail = data["error"].get("message") or json.dumps(data["error"])
            else:
                detail = f"exit code {code}"
            self.error = f"rusty-tiles preview stopped: {detail} (see {self._log.name})"
        self.started.set()
        # Drain anything else so the pipe never fills.
        for _ in self.process.stdout:
            pass

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    def stop(self, timeout: float = 3.0) -> None:
        """Terminate the server and wait for it. Safe to call more than once."""
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout)
        self._reader.join(timeout)
        self._log.close()
