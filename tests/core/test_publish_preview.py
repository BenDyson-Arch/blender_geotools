"""Preview helpers (geotools.publish.preview), without Blender.

The launch test needs a rusty-tiles CLI and a Cesium 1.146 runtime; set
GEOTOOLS_TEST_RUSTY_TILES_CLI and GEOTOOLS_TEST_CESIUM to run it.
"""

from __future__ import annotations

import os
import urllib.request
import zipfile
from pathlib import Path

import pytest

from geotools.publish import preview as pv


def _archive(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def test_extract_archive(tmp_path: Path) -> None:
    archive = _archive(
        tmp_path / "site.3tz",
        {"tileset.json": b"{}", "content/a.glb": b"glb", pv.INDEX_MEMBER: b"index"},
    )
    dest = tmp_path / "preview"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    assert pv.extract_archive(archive, dest) == dest
    assert (dest / "tileset.json").read_bytes() == b"{}"
    assert (dest / "content" / "a.glb").read_bytes() == b"glb"
    assert not (dest / pv.INDEX_MEMBER).exists()
    assert not (dest / "stale.txt").exists()


@pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "a/../../b.txt", "c:\\x"])
def test_extract_refuses_unsafe_names(tmp_path: Path, name: str) -> None:
    archive = _archive(tmp_path / "bad.3tz", {"tileset.json": b"{}", name: b"x"})
    with pytest.raises(pv.PreviewError, match="unsafe"):
        pv.extract_archive(archive, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_extract_needs_tileset(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "x.3tz", {"other.json": b"{}"})
    with pytest.raises(pv.PreviewError, match="tileset.json"):
        pv.extract_archive(archive, tmp_path / "out")
    with pytest.raises(pv.PreviewError, match="publish first"):
        pv.extract_archive(tmp_path / "missing.3tz", tmp_path / "out")


def test_problems(tmp_path: Path) -> None:
    assert "preferences" in pv.cli_problem("")
    assert "does not exist" in pv.cli_problem(str(tmp_path / "nope"))
    assert "preferences" in pv.cesium_problem("")
    assert "Cesium.js" in pv.cesium_problem(str(tmp_path))
    (tmp_path / "Cesium.js").write_text("")
    assert pv.cesium_problem(str(tmp_path)) is None


def test_command() -> None:
    cmd = pv.preview_command("rt", "/c", Path("/m"), 1234)
    assert cmd == ["rt", "preview", "--cesium", "/c", "--mesh", "/m", "--port", "1234", "--json"]


CLI = os.environ.get("GEOTOOLS_TEST_RUSTY_TILES_CLI", "")
CESIUM = os.environ.get("GEOTOOLS_TEST_CESIUM", "")


@pytest.mark.skipif(not (CLI and CESIUM), reason="needs a rusty-tiles CLI and Cesium runtime")
def test_preview_server(tmp_path: Path) -> None:
    mesh = tmp_path / "mesh"
    mesh.mkdir()
    (mesh / "tileset.json").write_text('{"asset": {"version": "1.1"}}')
    opened: list[str] = []
    server = pv.PreviewServer(
        CLI, CESIUM, mesh, tmp_path / "preview.log", open_browser=opened.append
    )
    try:
        assert server.started.wait(10)
        assert server.error is None, server.error
        assert server.url and opened == [server.url]
        assert server.running
        with urllib.request.urlopen(server.url + "config.json", timeout=10) as response:
            assert response.status == 200
    finally:
        server.stop()
    assert not server.running


@pytest.mark.skipif(not CLI, reason="needs a rusty-tiles CLI")
def test_preview_server_reports_startup_error(tmp_path: Path) -> None:
    mesh = tmp_path / "mesh"
    mesh.mkdir()
    server = pv.PreviewServer(CLI, str(tmp_path / "no-cesium"), mesh, tmp_path / "preview.log")
    try:
        assert server.started.wait(10)
        assert server.url is None
        assert "does not exist" in server.error, server.error
    finally:
        server.stop()
