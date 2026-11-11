"""Probe the bundled native libraries: rusty-tiles and pyproj.

The extension ships rusty-tiles and pyproj (with PROJ inside) as native wheels.
A missing or broken wheel must be reported to the user, never crash
registration, so :func:`probe_rusty_tiles` and :func:`probe_pyproj` turn every
load failure into a :class:`DependencyStatus`. Without rusty-tiles nothing can
be published; without pyproj imports cannot reproject (see
:mod:`geotools.core.reproject`).
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from dataclasses import dataclass
from types import ModuleType

log = logging.getLogger(__name__)

MODULE_NAME = "rusty_tiles"
"""Import name of the rusty-tiles Python bindings."""

DIST_NAME = "rusty-tiles"
"""Distribution name of the rusty-tiles wheel."""

PYPROJ_MODULE = "pyproj"
"""Import name of pyproj."""

PYPROJ_DIST = "pyproj"
"""Distribution name of the pyproj wheel."""


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """Result of probing one optional dependency."""

    available: bool
    version: str | None = None
    error: str | None = None
    name: str = DIST_NAME
    """Distribution name, as shown to the user."""
    proj_version: str | None = None
    """PROJ library version (pyproj only)."""
    data_dir: str | None = None
    """PROJ data directory in use, holding proj.db (pyproj only)."""

    @property
    def summary(self) -> str:
        """One-line, human-readable status."""
        if self.available:
            text = f"{self.name} {self.version or '(unknown version)'}"
            if self.proj_version:
                text += f" (PROJ {self.proj_version})"
            return text
        return f"{self.name} not available: {self.error or 'unknown error'}"


_cached: DependencyStatus | None = None
_cached_pyproj: DependencyStatus | None = None


def probe_rusty_tiles() -> DependencyStatus:
    """Return the rusty-tiles status, probing once and caching the result."""
    global _cached
    if _cached is None:
        _cached = _probe()
        if _cached.available:
            log.info("%s", _cached.summary)
        else:
            log.warning("%s", _cached.summary)
    return _cached


def probe_pyproj() -> DependencyStatus:
    """Return the pyproj status, probing once and caching the result.

    Not called at registration: importing pyproj loads PROJ, which only the
    About panel, Check Dependencies and an import that reprojects need.
    """
    global _cached_pyproj
    if _cached_pyproj is None:
        _cached_pyproj = _probe_pyproj()
        if _cached_pyproj.available:
            log.info("%s, data %s", _cached_pyproj.summary, _cached_pyproj.data_dir)
        else:
            log.warning("%s", _cached_pyproj.summary)
    return _cached_pyproj


def probe_all() -> tuple[DependencyStatus, ...]:
    """Every bundled library, in display order."""
    return probe_rusty_tiles(), probe_pyproj()


def reset_cache() -> None:
    """Forget the cached statuses so the next probe imports again."""
    global _cached, _cached_pyproj
    _cached = None
    _cached_pyproj = None
    importlib.invalidate_caches()


def _import(module_name: str) -> ModuleType | str:
    """The imported module, or why it could not be imported."""
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return f"{module_name} is not installed"
        return f"{module_name} failed to import: {exc}"
    except ImportError as exc:
        return f"{module_name} failed to import: {exc}"
    except Exception as exc:  # a broken native wheel can raise anything
        log.debug("%s load failure", module_name, exc_info=True)
        return f"{module_name} failed to load: {type(exc).__name__}: {exc}"


def _probe() -> DependencyStatus:
    """Import rusty-tiles and read its version, catching any load failure."""
    module = _import(MODULE_NAME)
    if isinstance(module, str):
        return DependencyStatus(False, error=module)
    return DependencyStatus(True, version=_version(module))


def _probe_pyproj() -> DependencyStatus:
    """Import pyproj; read its version, the PROJ version and the PROJ data directory.

    pyproj without a data directory (proj.db) cannot resolve any CRS, so that
    counts as not available.
    """
    module = _import(PYPROJ_MODULE)
    if isinstance(module, str):
        return DependencyStatus(False, error=module, name=PYPROJ_DIST)
    version = _version(module, PYPROJ_DIST)
    proj_version = getattr(module, "proj_version_str", None)
    proj_version = proj_version if isinstance(proj_version, str) else None
    try:
        data_dir = str(module.datadir.get_data_dir())
    except Exception as exc:  # pyproj.exceptions.DataDirError, or a broken install
        log.debug("pyproj data directory lookup failed", exc_info=True)
        return DependencyStatus(
            False,
            version=version,
            error=f"PROJ data directory not found: {exc}",
            name=PYPROJ_DIST,
            proj_version=proj_version,
        )
    return DependencyStatus(
        True,
        version=version,
        name=PYPROJ_DIST,
        proj_version=proj_version,
        data_dir=data_dir,
    )


def _version(module: ModuleType, dist_name: str = DIST_NAME) -> str | None:
    """Version from the wheel metadata, else from ``__version__`` on the module."""
    try:
        return importlib.metadata.version(dist_name)
    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception:  # corrupt metadata must not hide the module
        log.debug("could not read %s metadata", dist_name, exc_info=True)
    # The pyo3 module sets __version__; the package re-exports it via `import *`,
    # which depends on the native __all__, so also look on the native submodule.
    for candidate in (module, getattr(module, module.__name__.rpartition(".")[2], None)):
        version = getattr(candidate, "__version__", None)
        if isinstance(version, str):
            return version
    return None
