"""Tests for geotools.core.deps."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.metadata
import sys
import types
from collections.abc import Iterator

import pytest

from geotools.core import deps


class _FailingFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Finds ``rusty_tiles`` and raises ``exc`` when it is executed, like a broken .so."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def find_spec(self, fullname, path=None, target=None):
        if fullname != deps.MODULE_NAME:
            return None
        return importlib.machinery.ModuleSpec(fullname, self)

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        self.calls += 1
        raise self.exc


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start each test with no cached status and no real rusty_tiles or pyproj."""
    monkeypatch.delitem(sys.modules, deps.MODULE_NAME, raising=False)
    monkeypatch.delitem(sys.modules, f"{deps.MODULE_NAME}.{deps.MODULE_NAME}", raising=False)
    monkeypatch.delitem(sys.modules, deps.PYPROJ_MODULE, raising=False)
    deps.reset_cache()
    yield
    deps.reset_cache()


def _no_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def version(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", version)


def _fake_module(monkeypatch: pytest.MonkeyPatch, version: str | None) -> types.ModuleType:
    module = types.ModuleType(deps.MODULE_NAME)
    if version is not None:
        module.__version__ = version
    monkeypatch.setitem(sys.modules, deps.MODULE_NAME, module)
    return module


def test_available_uses_metadata_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_module(monkeypatch, "9.9.9")
    seen: list[str] = []

    def version(name: str) -> str:
        seen.append(name)
        return "0.4.0"

    monkeypatch.setattr(importlib.metadata, "version", version)

    status = deps.probe_rusty_tiles()

    assert status == deps.DependencyStatus(True, version="0.4.0", error=None)
    assert seen == ["rusty-tiles"]
    assert status.summary == "rusty-tiles 0.4.0"


def test_available_falls_back_to_module_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_module(monkeypatch, "0.4.0")
    _no_metadata(monkeypatch)

    assert deps.probe_rusty_tiles() == deps.DependencyStatus(True, version="0.4.0")


def test_available_falls_back_to_native_submodule(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real package re-exports with `import *`; cover a build where that drops __version__.
    package = _fake_module(monkeypatch, None)
    package.rusty_tiles = types.SimpleNamespace(__version__="0.4.0")
    _no_metadata(monkeypatch)

    assert deps.probe_rusty_tiles().version == "0.4.0"


def test_available_without_any_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_module(monkeypatch, None)
    _no_metadata(monkeypatch)

    status = deps.probe_rusty_tiles()

    assert status.available and status.version is None
    assert status.summary == "rusty-tiles (unknown version)"


def test_broken_metadata_still_reports_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_module(monkeypatch, "0.4.0")

    def version(name: str) -> str:
        raise ValueError("corrupt METADATA")

    monkeypatch.setattr(importlib.metadata, "version", version)

    assert deps.probe_rusty_tiles() == deps.DependencyStatus(True, version="0.4.0")


def test_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, deps.MODULE_NAME, None)  # import raises ModuleNotFoundError

    status = deps.probe_rusty_tiles()

    assert not status.available
    assert status.version is None
    assert status.error == "rusty_tiles is not installed"
    assert status.summary == "rusty-tiles not available: rusty_tiles is not installed"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            ImportError("libfoo.so: cannot open shared object file"),
            "rusty_tiles failed to import: libfoo.so: cannot open shared object file",
        ),
        (
            ModuleNotFoundError("No module named 'numpy'", name="numpy"),
            "rusty_tiles failed to import: No module named 'numpy'",
        ),
        (
            OSError("invalid ELF header"),
            "rusty_tiles failed to load: OSError: invalid ELF header",
        ),
        (
            SystemError("PyInit failed"),
            "rusty_tiles failed to load: SystemError: PyInit failed",
        ),
    ],
)
def test_broken_import(monkeypatch: pytest.MonkeyPatch, exc: BaseException, expected: str) -> None:
    monkeypatch.setattr(sys, "meta_path", [_FailingFinder(exc), *sys.meta_path])

    status = deps.probe_rusty_tiles()

    assert status == deps.DependencyStatus(False, version=None, error=expected)


def test_probe_is_cached_until_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    finder = _FailingFinder(OSError("boom"))
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])

    first = deps.probe_rusty_tiles()
    assert deps.probe_rusty_tiles() is first
    assert finder.calls == 1

    # The wheel gets fixed; the cached failure persists until reset.
    monkeypatch.setattr(sys, "meta_path", [m for m in sys.meta_path if m is not finder])
    _fake_module(monkeypatch, "0.4.0")
    _no_metadata(monkeypatch)
    assert deps.probe_rusty_tiles() is first

    deps.reset_cache()
    assert deps.probe_rusty_tiles() == deps.DependencyStatus(True, version="0.4.0")


def test_status_is_frozen() -> None:
    status = deps.DependencyStatus(True, version="0.4.0")
    with pytest.raises(AttributeError):
        status.available = False  # type: ignore[misc]


# --- pyproj --------------------------------------------------------------------------


def _fake_pyproj(
    monkeypatch: pytest.MonkeyPatch, data_dir: str | Exception = "/x/proj_dir/share/proj"
) -> types.ModuleType:
    module = types.ModuleType(deps.PYPROJ_MODULE)
    module.__version__ = "3.8.0"
    module.proj_version_str = "9.8.1"

    def get_data_dir() -> str:
        if isinstance(data_dir, Exception):
            raise data_dir
        return data_dir

    module.datadir = types.SimpleNamespace(get_data_dir=get_data_dir)
    monkeypatch.setitem(sys.modules, deps.PYPROJ_MODULE, module)
    return module


def test_pyproj_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pyproj(monkeypatch)
    _no_metadata(monkeypatch)

    status = deps.probe_pyproj()

    assert status == deps.DependencyStatus(
        True,
        version="3.8.0",
        name="pyproj",
        proj_version="9.8.1",
        data_dir="/x/proj_dir/share/proj",
    )
    assert status.summary == "pyproj 3.8.0 (PROJ 9.8.1)"
    assert deps.probe_pyproj() is status  # cached


def test_pyproj_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, deps.PYPROJ_MODULE, None)

    status = deps.probe_pyproj()

    assert not status.available and status.name == "pyproj"
    assert status.summary == "pyproj not available: pyproj is not installed"


def test_pyproj_without_data_dir_is_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_pyproj(monkeypatch, data_dir=RuntimeError("Valid PROJ data directory not found."))
    _no_metadata(monkeypatch)

    status = deps.probe_pyproj()

    assert not status.available
    assert status.version == "3.8.0" and status.proj_version == "9.8.1"
    assert status.error == ("PROJ data directory not found: Valid PROJ data directory not found.")


def test_probe_all_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_module(monkeypatch, "0.4.0")
    monkeypatch.setitem(sys.modules, deps.PYPROJ_MODULE, None)
    _no_metadata(monkeypatch)

    rusty, pyproj = deps.probe_all()
    assert rusty.name == "rusty-tiles" and rusty.available
    assert pyproj.name == "pyproj" and not pyproj.available

    _fake_pyproj(monkeypatch)
    assert not deps.probe_pyproj().available  # still cached
    deps.reset_cache()
    assert deps.probe_pyproj().available
