"""geotools.core.paths: the portable path rule, with Windows and POSIX inputs on any OS."""

from __future__ import annotations

import ntpath
import os
import posixpath
import sys
from pathlib import Path

import pytest

from geotools.core import paths

WIN = {"pathmod": ntpath}
POSIX = {"pathmod": posixpath}


# --- reading ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "//data/roads.geojson",
        "//data\\roads.geojson",
        "//data\\\\roads.geojson",
        "//./data/roads.geojson",
    ],
)
def test_resolve_relative_either_separator_posix(text: str) -> None:
    assert paths.resolve(text, "/proj/site", **POSIX) == "/proj/site/data/roads.geojson"


@pytest.mark.parametrize("text", ["//data/roads.geojson", "//data\\roads.geojson"])
def test_resolve_relative_either_separator_windows(text: str) -> None:
    got = paths.resolve(text, "C:\\Users\\me\\site", **WIN)
    assert got == "C:\\Users\\me\\site\\data\\roads.geojson"


def test_resolve_relative_on_a_unc_share() -> None:
    got = paths.resolve("//out\\site.3tz", "\\\\server\\share\\proj", **WIN)
    assert got == "\\\\server\\share\\proj\\out\\site.3tz"


def test_resolve_collapses_parent_steps() -> None:
    assert paths.resolve("//../shared/dem.tif", "/proj/site", **POSIX) == "/proj/shared/dem.tif"
    assert paths.resolve("//..\\shared\\dem.tif", "D:\\proj\\site", **WIN) == (
        "D:\\proj\\shared\\dem.tif"
    )


def test_resolve_blend_folder_itself() -> None:
    assert paths.resolve("//", "/proj/site", **POSIX) == "/proj/site"


@pytest.mark.parametrize(
    "text",
    [
        "/home/me/data/roads.geojson",
        "C:\\data\\roads.geojson",
        "C:/data/roads.geojson",
        "\\\\server\\share\\roads.geojson",
        "relative/roads.geojson",
    ],
)
def test_resolve_leaves_other_paths_alone(text: str) -> None:
    for kwargs in (WIN, POSIX):
        assert paths.resolve(text, "/proj", **kwargs) == text
        assert paths.resolve(text, None, **kwargs) == text


def test_resolve_relative_needs_a_saved_blend() -> None:
    with pytest.raises(paths.NotSavedError):
        paths.resolve("//out/site.3tz", None)


def test_resolve_does_not_look_for_missing_files(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "roads.geojson").write_text("{}")
    got = paths.resolve("//data/roads.geojson", tmp_path)
    assert got == os.path.join(tmp_path, "data", "roads.geojson")
    assert not os.path.exists(got)


def test_resolve_native(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    target = tmp_path / "data" / "roads.geojson"
    target.write_text("{}")
    for text in ("//data/roads.geojson", "//data\\roads.geojson"):
        got = paths.resolve(text, tmp_path)
        assert os.path.isfile(got) and paths.same_file(got, target)


# --- writing ---------------------------------------------------------------------------


def test_portable_under_blend_folder_posix() -> None:
    got = paths.portable("/proj/site/data/roads.geojson", "/proj/site", **POSIX)
    assert got == "//data/roads.geojson"
    assert paths.portable("/proj/site/a/../b/x.tif", "/proj/site/", **POSIX) == "//b/x.tif"


def test_portable_under_blend_folder_windows() -> None:
    got = paths.portable("C:\\Users\\Me\\Site\\out\\site.3tz", "c:\\users\\me\\site", **WIN)
    assert got == "//out/site.3tz"
    got = paths.portable("C:/Users/Me/Site/out/site.3tz", "C:\\Users\\Me\\Site", **WIN)
    assert got == "//out/site.3tz"
    got = paths.portable("\\\\nas\\gis\\proj\\dem.tif", "\\\\NAS\\gis\\proj", **WIN)
    assert got == "//dem.tif"


@pytest.mark.parametrize(
    ("path", "blend_dir", "kwargs"),
    [
        ("/proj/other/roads.geojson", "/proj/site", POSIX),
        ("/proj/site-2/roads.geojson", "/proj/site", POSIX),  # a prefix, not a parent
        ("/proj/site", "/proj/site", POSIX),
        ("D:\\data\\roads.geojson", "C:\\proj", WIN),  # another drive
        ("\\\\server\\share\\roads.geojson", "C:\\proj", WIN),  # UNC
        ("C:\\proj\\roads.geojson", "\\\\server\\share\\proj", WIN),
        ("C:\\projects\\roads.geojson", "C:\\proj", WIN),
    ],
)
def test_portable_keeps_paths_outside_absolute(path: str, blend_dir: str, kwargs: dict) -> None:
    assert paths.portable(path, blend_dir, **kwargs) == path


def test_portable_without_saved_blend_keeps_absolute() -> None:
    assert paths.portable("/proj/site/x.tif", None, **POSIX) == "/proj/site/x.tif"
    assert paths.portable("C:\\proj\\x.tif", None, **WIN) == "C:\\proj\\x.tif"


def test_portable_normalises_relative_separators() -> None:
    for kwargs in (WIN, POSIX):
        assert paths.portable("//out\\site.3tz", "/x", **kwargs) == "//out/site.3tz"
        assert paths.portable("//out\\sub//site.3tz", None, **kwargs) == "//out/sub/site.3tz"
        assert paths.portable("//..\\shared\\a.tif", None, **kwargs) == "//../shared/a.tif"


def test_portable_leaves_plain_relative_and_empty() -> None:
    assert paths.portable("", "/proj") == ""
    assert paths.portable("site.3tz", "/proj", **POSIX) == "site.3tz"


def test_portable_round_trip_windows_to_posix() -> None:
    """Stored on Windows, read on Linux or macOS: the same file under the folder."""
    stored = paths.portable("C:\\work\\site\\data\\dem.tif", "C:\\work\\site", **WIN)
    assert paths.resolve(stored, "/Users/me/site", **POSIX) == "/Users/me/site/data/dem.tif"
    stored = paths.portable("/home/me/site/data/dem.tif", "/home/me/site", **POSIX)
    assert paths.resolve(stored, "E:\\site", **WIN) == "E:\\site\\data\\dem.tif"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_portable_follows_symlinked_blend_folder(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "data").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    # The .blend opened through the link, the file chosen through the real folder.
    assert paths.portable(real / "data" / "a.tif", link) == "//data/a.tif"


# --- names and comparisons ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("//out\\site.3tz", "site.3tz"),
        ("//out/site.3tz", "site.3tz"),
        ("C:\\data\\offset.txt", "offset.txt"),
        ("/home/me/offset.txt", "offset.txt"),
        ("", ""),
    ],
)
def test_name(text: str, expected: str) -> None:
    assert paths.name(text) == expected


def test_relative_to() -> None:
    assert paths.relative_to("/a/b/c", "/a", **POSIX) == ["b", "c"]
    assert paths.relative_to("/a", "/a", **POSIX) is None
    assert paths.relative_to("C:\\A\\b", "c:\\a", **WIN) == ["b"]
    assert paths.relative_to("C:\\x", "C:\\", **WIN) == ["x"]
    assert paths.relative_to("/x", "/", **POSIX) == ["x"]
    assert paths.relative_to("relative/x", "/", **POSIX) is None


def test_same_file(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    a.write_text("a")
    assert paths.same_file(a, tmp_path / "sub" / ".." / "a.txt")
    assert not paths.same_file(a, tmp_path / "b.txt")
    # Missing files compare by their resolved spelling.
    assert paths.same_file(tmp_path / "gone.txt", str(tmp_path) + os.sep + "gone.txt")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_same_file_through_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    (tmp_path / "real" / "a.txt").write_text("a")
    assert paths.same_file(tmp_path / "link" / "a.txt", tmp_path / "real" / "a.txt")
    # Missing: still resolved through the link.
    assert paths.same_file(tmp_path / "link" / "gone", tmp_path / "real" / "gone")
