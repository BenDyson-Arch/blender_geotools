"""An in-place update without a restart (issue 134): the notice, and PROJ's errors kept out of the UI.

The record of loaded files (``deps.record_session``, ``deps._record_library``)
is compared with the disk by ``deps.check_for_update``. PROJ's database errors
become one plain sentence in reprojection, site and the CRS catalogue, while
the raw text goes to the log and ``deps.details_text``.
"""

from __future__ import annotations

import importlib.metadata
import logging
import types
from pathlib import Path

import numpy as np
import pytest

from geotools.core import crs_catalog, deps

RAW = (
    "Invalid projection: EPSG:7853: (Internal Proj Error: proj_create: SQLite error "
    "[ code = 10, msg = disk I/O error ] on SELECT versioned_auth_name, auth_name, code "
    "FROM other_transformation)"
)
"""The error macOS Blender gave after 0.3.0 was installed over an older GeoTools."""


# --- the error mapping ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        RAW,
        "proj_create_from_database: no database context specified",
        "query_crs_info: SQLite error on SELECT name FROM crs_view",
        "proj_create: Open of /x/proj.db failed: unable to open database file",
    ],
)
def test_database_errors_map_to_the_plain_message(raw: str) -> None:
    assert deps.proj_error_message(raw) == deps.PROJ_DATABASE_MESSAGE
    assert deps.proj_error_message(RuntimeError(raw)) == deps.PROJ_DATABASE_MESSAGE


@pytest.mark.parametrize(
    "raw",
    [
        "Invalid projection: EPSG:999999: (Internal Proj Error: proj_create: crs not found)",
        "Invalid projection: garbage: (Internal Proj Error: proj_create: unrecognized format)",
        "x, y, z, and time must be same size",
    ],
)
def test_other_proj_errors_are_left_alone(raw: str) -> None:
    """A CRS the user typed wrong still says which; only database failures are mapped."""
    assert deps.proj_error_message(raw) is None
    assert deps.PROJ_DATABASE_MESSAGE not in deps.details_text("0.4.0")


def test_raw_text_goes_to_the_log_and_copy_details(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=deps.__name__):
        message = deps.proj_error_message(RAW)
        deps.proj_error_message(RAW)  # logged and kept once
    assert message is not None and "SQLite" not in message
    assert [r.getMessage().count(RAW) for r in caplog.records] == [1]
    details = deps.details_text("0.4.0")
    assert details.count(RAW) == 1 and details.startswith("GeoTools 0.4.0")


def test_an_update_makes_it_the_restart_message(tmp_path: Path) -> None:
    manifest = tmp_path / "blender_manifest.toml"
    manifest.write_text("version = '0.4.0'\n")
    deps.record_session("0.4.0", (str(manifest),))
    assert deps.proj_error_message(RAW) == deps.PROJ_DATABASE_MESSAGE
    assert deps.check_for_update("0.4.1")
    assert deps.proj_error_message(RAW) == deps.RESTART_MESSAGE
    assert deps.is_plain_proj_message(deps.RESTART_MESSAGE)
    assert deps.is_plain_proj_message(deps.PROJ_DATABASE_MESSAGE)
    assert not deps.is_plain_proj_message(RAW)


# --- the record of what this process loaded -----------------------------------------------


def _library(tmp_path: Path, name: str = "fakelib") -> types.ModuleType:
    """A package with a native module and a repaired wheel's ``<name>.libs`` folder."""
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "_native.cpython-313-x86_64-linux-gnu.so").write_bytes(b"\0" * 64)
    (package / "helpers.py").write_text("")
    libs = tmp_path / f"{name}.libs"
    libs.mkdir()
    (libs / "libproj-0123abcd.so.25.9.8.1").write_bytes(b"\1" * 64)
    module = types.ModuleType(name)
    module.__file__ = str(package / "__init__.py")
    return module


def _replace(path: Path, data: bytes) -> None:
    """Replace a file as an installer does: a new file (new inode) renamed over the old."""
    new = path.with_name(path.name + ".new")
    new.write_bytes(data)
    new.replace(path)


def test_library_files_are_the_native_ones(tmp_path: Path) -> None:
    files = deps.library_files(_library(tmp_path))
    names = [Path(f).name for f in files]
    assert names == [
        "__init__.py",
        "_native.cpython-313-x86_64-linux-gnu.so",
        "libproj-0123abcd.so.25.9.8.1",
    ]
    assert deps.library_files(types.ModuleType("double")) == []  # no file: nothing recorded


def test_unchanged_files_need_no_restart(tmp_path: Path) -> None:
    deps._record_library(_library(tmp_path), fresh=True)
    deps.record_session("0.4.0")
    assert not deps.check_for_update("0.4.0")
    assert not deps.update_pending()


@pytest.mark.parametrize(
    "which", ["fakelib.libs/libproj-0123abcd.so.25.9.8.1", "fakelib/__init__.py"]
)
def test_a_replaced_library_needs_a_restart(tmp_path: Path, which: str) -> None:
    deps._record_library(_library(tmp_path), fresh=True)
    _replace(tmp_path / which, b"new")
    assert not deps.update_pending()  # nothing compared yet: no disk access
    assert deps.check_for_update("0.4.0")
    assert deps.update_pending()
    assert "Restart needed: fakelib:" in deps.details_text("0.4.0")


def test_a_removed_library_needs_a_restart(tmp_path: Path) -> None:
    deps._record_library(_library(tmp_path), fresh=True)
    (tmp_path / "fakelib.libs" / "libproj-0123abcd.so.25.9.8.1").unlink()
    assert deps.check_for_update()


def test_the_verdict_is_sticky(tmp_path: Path) -> None:
    """Only a restart clears it, so putting the old file back does not."""
    library = _library(tmp_path)
    deps._record_library(library, fresh=True)
    init = tmp_path / "fakelib" / "__init__.py"
    before = init.read_bytes()
    _replace(init, b"new")
    assert deps.check_for_update()
    _replace(init, before)
    assert deps.check_for_update()


def test_a_new_extension_version_needs_a_restart() -> None:
    deps.record_session("0.4.0")
    deps.record_session("0.4.1")  # the updated extension registers again: the first stays
    assert deps.check_for_update("0.4.1")
    assert "GeoTools 0.4.0 was loaded but 0.4.1 is installed" in deps.details_text("0.4.1")


def test_the_first_register_keeps_its_files(tmp_path: Path) -> None:
    manifest = tmp_path / "blender_manifest.toml"
    manifest.write_text("old")
    deps.record_session("0.4.0", (str(manifest),))
    _replace(manifest, b"new")
    deps.record_session("0.4.0", (str(manifest),))  # re-enabled: records nothing new
    assert deps.check_for_update("0.4.0")


def test_a_fresh_import_records_the_files_it_loaded(tmp_path: Path) -> None:
    """A library first imported after an update loads the new files: no restart needed."""
    library = _library(tmp_path)
    deps._record_library(library, fresh=False)
    _replace(tmp_path / "fakelib" / "__init__.py", b"new")
    deps._record_library(library, fresh=True)
    assert not deps.check_for_update()


def test_a_library_loaded_before_any_record_is_compared_by_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older GeoTools loaded it and recorded nothing: its version in memory tells."""
    library = _library(tmp_path)
    library.__version__ = "3.7.1"
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "3.8.0")
    deps._record_library(library, fresh=False)
    assert deps.check_for_update()
    assert "fakelib 3.7.1 is loaded but 3.8.0 is installed" in deps.details_text("0.4.0")


def test_a_library_loaded_before_any_record_at_the_same_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _library(tmp_path)
    library.__version__ = "3.8.0"
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "3.8.0")
    deps._record_library(library, fresh=False)
    assert not deps.check_for_update()


def test_the_record_survives_a_reload_of_deps() -> None:
    """Blender reloads the extension's modules on an update; the record is outside them."""
    import importlib

    deps.record_session("0.4.0")
    reloaded = importlib.reload(deps)
    reloaded.record_session("0.4.1")
    assert reloaded.check_for_update("0.4.1")


# --- PROJ's errors in reprojection, site and the CRS catalogue ------------------------------


def _broken_database(monkeypatch: pytest.MonkeyPatch):
    """pyproj whose database reads fail as on macOS after the update."""
    pyproj = pytest.importorskip("pyproj")
    database = pytest.importorskip("pyproj.database")

    def fail(*_args, **_kwargs):
        raise pyproj.exceptions.CRSError(RAW)

    monkeypatch.setattr(pyproj.CRS, "from_user_input", fail)
    monkeypatch.setattr(database, "query_crs_info", fail)
    return pyproj


def test_reprojection_says_the_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from geotools.core import reproject

    _broken_database(monkeypatch)
    with pytest.raises(reproject.ReprojectionError) as caught:
        reproject.reproject_points(np.zeros((1, 3)), "EPSG:4326", "EPSG:7853")
    assert str(caught.value) == deps.PROJ_DATABASE_MESSAGE
    assert RAW in deps.details_text("0.4.0")


def test_site_says_the_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from geotools.analysis import site

    _broken_database(monkeypatch)
    site._grid.cache_clear()
    try:
        with pytest.raises(site.SiteError) as caught:
            site._grid("EPSG:7853")
    finally:
        site._grid.cache_clear()
    assert str(caught.value) == deps.PROJ_DATABASE_MESSAGE


def test_site_ecef_transform_says_the_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from geotools.analysis import site

    pyproj = pytest.importorskip("pyproj")

    def fail(*_args, **_kwargs):
        raise pyproj.exceptions.ProjError(RAW)

    monkeypatch.setattr(pyproj.Transformer, "from_crs", fail)
    site._ecef_to_wgs84.cache_clear()
    try:
        with pytest.raises(site.SiteError) as caught:
            site._ecef_to_wgs84()
    finally:
        site._ecef_to_wgs84.cache_clear()
    assert str(caught.value) == deps.PROJ_DATABASE_MESSAGE


def test_crs_names_say_the_plain_message(monkeypatch: pytest.MonkeyPatch, allow_list) -> None:
    _broken_database(monkeypatch)
    assert crs_catalog.name("EPSG:7856") == ""
    assert crs_catalog.names_error() == deps.PROJ_DATABASE_MESSAGE
    crs_catalog.reset_cache()
    assert crs_catalog.names_error() == ""
