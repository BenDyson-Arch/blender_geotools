"""Extract for Web (geotools.publish.web), without Blender."""

from __future__ import annotations

import json
import os
import re
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
    (dest / "stale.txt").write_text("old")
    # A newer archive makes the folder stale until it is extracted again.
    marker = dest / web.MARKER_NAME
    later = marker.stat().st_mtime_ns + 5_000_000_000
    os.utime(archive, ns=(later, later))
    assert web.is_stale(archive, dest)
    web.extract_for_web(archive)
    assert not (dest / "stale.txt").exists()
    assert (dest / "tileset.json").is_file()


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
