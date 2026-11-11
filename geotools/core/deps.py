"""Probe the bundled rusty-tiles library.

The extension ships rusty-tiles as a native wheel. A missing or broken wheel must
be reported to the user, never crash registration, so :func:`probe_rusty_tiles`
turns every load failure into a :class:`DependencyStatus`.
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


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    """Result of probing one optional dependency."""

    available: bool
    version: str | None = None
    error: str | None = None

    @property
    def summary(self) -> str:
        """One-line, human-readable status."""
        if self.available:
            return f"{DIST_NAME} {self.version or '(unknown version)'}"
        return f"{DIST_NAME} not available: {self.error or 'unknown error'}"


_cached: DependencyStatus | None = None


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


def reset_cache() -> None:
    """Forget the cached status so the next probe imports again."""
    global _cached
    _cached = None
    importlib.invalidate_caches()


def _probe() -> DependencyStatus:
    """Import rusty-tiles and read its version, catching any load failure."""
    try:
        module = importlib.import_module(MODULE_NAME)
    except ModuleNotFoundError as exc:
        if exc.name == MODULE_NAME:
            return DependencyStatus(False, error=f"{MODULE_NAME} is not installed")
        return DependencyStatus(False, error=f"{MODULE_NAME} failed to import: {exc}")
    except ImportError as exc:
        return DependencyStatus(False, error=f"{MODULE_NAME} failed to import: {exc}")
    except Exception as exc:  # a broken native wheel can raise anything
        log.debug("rusty-tiles load failure", exc_info=True)
        return DependencyStatus(
            False, error=f"{MODULE_NAME} failed to load: {type(exc).__name__}: {exc}"
        )
    return DependencyStatus(True, version=_version(module))


def _version(module: ModuleType) -> str | None:
    """Version from the wheel metadata, else from ``__version__`` on the module."""
    try:
        return importlib.metadata.version(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception:  # corrupt metadata must not hide the module
        log.debug("could not read %s metadata", DIST_NAME, exc_info=True)
    # The pyo3 module sets __version__; the package re-exports it via `import *`,
    # which depends on the native __all__, so also look on the native submodule.
    for candidate in (module, getattr(module, MODULE_NAME, None)):
        version = getattr(candidate, "__version__", None)
        if isinstance(version, str):
            return version
    return None
