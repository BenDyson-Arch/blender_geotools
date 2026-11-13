"""Extract for Web (geotools.publish.web), without Blender."""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path

import pytest

from geotools.publish import preview as pv
from geotools.publish import web

REPO = Path(__file__).resolve().parents[2]


def _archive(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


def _site(tmp_path: Path, name: str = "site.3tz") -> Path:
    return _archive(
        tmp_path / name,
        {"tileset.json": b"{}", "content/a.glb": b"glb", pv.INDEX_MEMBER: b"index"},
    )


def test_web_dir_for() -> None:
    assert web.web_dir_for(Path("/d/site.3tz")) == Path("/d/site.web")
    assert web.web_dir_for(Path("/d/site.slope.3tz")) == Path("/d/site.slope.web")
    with pytest.raises(ValueError):
        web.web_dir_for(Path("/d/site.glb"))


def test_extract_for_web(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    folder = web.extract_for_web(archive)
    dest = tmp_path / "site.web"
    assert folder == web.WebFolder(dest, True)
    assert (dest / "tileset.json").read_bytes() == b"{}"
    assert (dest / "content" / "a.glb").read_bytes() == b"glb"
    assert not (dest / pv.INDEX_MEMBER).exists()
    assert (dest / "index.html").read_text(encoding="utf-8") == web.viewer_html()
    marker = json.loads((dest / web.MARKER_NAME).read_text())
    assert marker["archive"] == "site.3tz" and marker["cesium"] == web.CESIUM_VERSION
    assert web.is_geotools_web_dir(dest)
    assert not web.is_stale(archive, dest)
    # Nothing else is left beside it (the hidden work folder is renamed into place).
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.3tz", "site.web"]


def test_extract_again_replaces_own_folder(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    dest = web.extract_for_web(archive).path
    marker = json.loads((dest / web.MARKER_NAME).read_text())
    assert set(marker["files"]) == {"tileset.json", "content/a.glb", "index.html"}
    # A newer archive makes the folder stale until it is extracted again.
    _archive(archive, {"tileset.json": b'{"v": 2}', "content/b.glb": b"glb2"})
    later = (dest / web.MARKER_NAME).stat().st_mtime_ns + 5_000_000_000
    os.utime(archive, ns=(later, later))
    assert web.is_stale(archive, dest)
    assert web.extract_for_web(archive) == web.WebFolder(dest, True)
    # Files GeoTools wrote last time and the new archive lacks are gone.
    assert not (dest / "content" / "a.glb").exists()
    assert (dest / "content" / "b.glb").read_bytes() == b"glb2"
    assert (dest / "tileset.json").read_bytes() == b'{"v": 2}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.3tz", "site.web"]


def test_extract_again_keeps_the_users_files(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    dest = web.extract_for_web(archive).path
    (dest / "index.html").write_text("MY CUSTOM PAGE")
    folder = web.extract_for_web(archive)
    assert folder == web.WebFolder(dest, False, kept_page=True)
    assert (dest / "index.html").read_text() == "MY CUSTOM PAGE"
    assert json.loads((dest / web.MARKER_NAME).read_text())["viewer"] is None
    # Still the user's page on the next extract: the marker does not claim it.
    assert web.extract_for_web(archive).kept_page
    assert (dest / "index.html").read_text() == "MY CUSTOM PAGE"

    # Any other file the user added or changed stops the extract, and nothing is lost.
    (dest / "notes.md").write_text("mine")
    (dest / "content" / "a.glb").write_bytes(b"edited")
    with pytest.raises(web.WebExtractError, match=r"content/a\.glb, notes\.md"):
        web.extract_for_web(archive)
    assert (dest / "notes.md").read_text() == "mine"
    assert (dest / "content" / "a.glb").read_bytes() == b"edited"
    assert (dest / "index.html").read_text() == "MY CUSTOM PAGE"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.3tz", "site.web"]

    # A user page next to an archive that brings its own index.html is not overwritten.
    (dest / "notes.md").unlink()
    (dest / "content" / "a.glb").write_bytes(b"glb")
    _archive(archive, {"tileset.json": b"{}", "index.html": b"theirs"})
    with pytest.raises(web.WebExtractError, match="index.html"):
        web.extract_for_web(archive)
    assert (dest / "index.html").read_text() == "MY CUSTOM PAGE"


def test_unreadable_marker_vouches_for_nothing(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    dest = web.extract_for_web(archive).path
    (dest / web.MARKER_NAME).write_text("not json")
    with pytest.raises(web.WebExtractError, match="did not write"):
        web.extract_for_web(archive)
    assert (dest / "tileset.json").is_file()


@pytest.mark.parametrize("step", ["aside", "into place"])
def test_failed_swap_keeps_the_previous_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    archive = _site(tmp_path)
    dest = web.extract_for_web(archive).path
    before = sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*"))
    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        moving_aside = self == dest
        moving_in = (
            Path(target) == dest and self.name.startswith(".site.web-") and "-old-" not in self.name
        )
        if (step == "aside" and moving_aside) or (step == "into place" and moving_in):
            raise PermissionError("folder is in use")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(PermissionError):
        web.extract_for_web(archive)
    monkeypatch.undo()
    assert web.is_geotools_web_dir(dest)
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.3tz", "site.web"]
    web.extract_for_web(archive)  # and the next extract works


def test_failed_cleanup_still_swaps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting the old folder comes last, after the new one is in place."""
    archive = _site(tmp_path)
    dest = web.extract_for_web(archive).path
    _archive(archive, {"tileset.json": b'{"v": 2}'})
    real_rmtree = shutil.rmtree

    def rmtree(path: Path, ignore_errors: bool = False) -> None:
        if "-old-" in Path(path).name:
            return  # as if a file in the old folder were held open
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(web.shutil, "rmtree", rmtree)
    web.extract_for_web(archive)
    assert web.is_geotools_web_dir(dest)
    assert (dest / "tileset.json").read_bytes() == b'{"v": 2}'
    (left,) = [p for p in tmp_path.iterdir() if "-old-" in p.name]
    assert left.name.startswith(".site.web-old-")


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no os.symlink")
def test_never_follows_a_symlinked_folder(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.txt").write_text("keep me")
    (elsewhere / web.MARKER_NAME).write_text("{}")  # even if it looks like GeoTools' own
    try:
        (tmp_path / "site.web").symlink_to(elsewhere, target_is_directory=True)
    except OSError as ex:  # Windows without the symlink privilege
        pytest.skip(f"cannot make a symlink: {ex}")
    with pytest.raises(web.WebExtractError, match="not a folder"):
        web.extract_for_web(archive)
    assert sorted(p.name for p in elsewhere.iterdir()) == [web.MARKER_NAME, "keep.txt"]
    assert (elsewhere / "keep.txt").read_text() == "keep me"
    assert (tmp_path / "site.web").is_symlink()


def test_never_replaces_a_folder_it_did_not_write(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    mine = tmp_path / "site.web"
    mine.mkdir()
    (mine / "notes.txt").write_text("keep me")
    with pytest.raises(web.WebExtractError, match="not written by Extract for Web"):
        web.extract_for_web(archive)
    assert (mine / "notes.txt").read_text() == "keep me"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.3tz", "site.web"]

    (tmp_path / "other.web").write_text("a file")
    with pytest.raises(web.WebExtractError, match="not a folder"):
        web.extract_for_web(_site(tmp_path, "other.3tz"))

    # An empty folder of the same name is fine to fill.
    (tmp_path / "empty.web").mkdir()
    assert web.extract_for_web(_site(tmp_path, "empty.3tz")).viewer


def test_failed_extract_keeps_the_previous_folder(tmp_path: Path) -> None:
    archive = _site(tmp_path)
    dest = web.extract_for_web(archive).path
    _archive(archive, {"other.json": b"{}"})  # republished without a tileset.json
    with pytest.raises(web.WebExtractError, match="tileset.json"):
        web.extract_for_web(archive)
    assert (dest / "tileset.json").is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site.3tz", "site.web"]

    archive.write_bytes(b"not a zip")
    with pytest.raises(web.WebExtractError, match="could not extract"):
        web.extract_for_web(archive)
    with pytest.raises(web.WebExtractError, match="publish first"):
        web.extract_for_web(tmp_path / "missing.3tz")


def test_archive_with_its_own_index_html(tmp_path: Path) -> None:
    archive = _archive(tmp_path / "s.3tz", {"tileset.json": b"{}", "index.html": b"theirs"})
    folder = web.extract_for_web(archive)
    assert not folder.viewer
    assert (folder.path / "index.html").read_bytes() == b"theirs"


def test_viewer_page() -> None:
    """The page loads the pinned CesiumJS and needs no Cesium ion token."""
    html = web.viewer_html()
    version = re.escape(web.CESIUM_VERSION)
    urls = re.findall(r'(?:src|href)="(https://[^"]+)"', html)
    assert len(urls) == 2, urls
    for url in urls:
        assert re.match(rf"https://cdn\.jsdelivr\.net/npm/cesium@{version}/Build/Cesium/", url)
    assert html.count('integrity="sha384-') == 2
    assert 'Cesium3DTileset.fromUrl("./tileset.json")' in html
    assert "NaturalEarthII" in html  # bundled imagery, not ion's
    assert "geocoder: false" in html and "baseLayerPicker: false" in html
    assert "defaultAccessToken" not in html and "ion.cesium.com" not in html


def test_guide_shows_the_same_page() -> None:
    """docs/guide/publish.md shows the page Extract for Web writes, word for word."""
    guide = (REPO / "docs" / "guide" / "publish.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```html\n(.*?)```", guide, flags=re.DOTALL)
    assert blocks == [web.viewer_html()]
