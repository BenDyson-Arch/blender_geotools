"""Probe the bundled native libraries: rusty-tiles and pyproj.

The extension ships rusty-tiles and pyproj (with PROJ inside) as native wheels.
A missing or broken wheel must be reported to the user, never crash
registration, so :func:`probe_rusty_tiles` and :func:`probe_pyproj` turn every
load failure into a :class:`DependencyStatus`. Without rusty-tiles nothing can
be published; without pyproj imports cannot reproject (see
:mod:`geotools.core.reproject`).

Updating GeoTools in place, without restarting Blender, replaces these
libraries on disk while the running process keeps the ones it loaded: on
macOS PROJ then fails every CRS lookup with "SQLite error ... disk I/O
error", as its proj.db is gone. So the process records what it loaded
(:func:`record_session`, at register, and :func:`_import`, when a library is
first imported): the extension version and the identity (path, inode, mtime,
size) of each library's native files and PROJ's ``proj.db``. Only ``os.stat``,
never a hash. About, Check Dependencies and Run Checks compare them with the
files on disk (:func:`check_for_update`) and say :data:`RESTART_MESSAGE`; the
result is sticky, as only a restart clears it. The record lives in
``sys.modules[SESSION_KEY]``, outside the package, so it survives Blender
reloading the extension's modules.

:func:`proj_error_message` turns PROJ's database errors into one plain
sentence for the UI. The raw text goes to the log and to
:func:`details_text` (About's Copy Details), never into a panel.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

log = logging.getLogger(__name__)

MODULE_NAME = "rusty_tiles"
"""Import name of the rusty-tiles Python bindings."""

DIST_NAME = "rusty-tiles"
"""Distribution name of the rusty-tiles wheel."""

CRS_FUNCTIONS = ("supported_epsg_codes", "resolve_source_crs")
"""rusty-tiles functions GeoTools needs (0.4.1): the EPSG picker and the CRS check."""

DECODE_FUNCTIONS = ("list_tiles", "decode_tile")
"""rusty-tiles functions GeoTools needs (0.4.1): opening output tiles (M7, ``core.output_tiles``)."""

REQUIRED_FUNCTIONS = CRS_FUNCTIONS + DECODE_FUNCTIONS
"""Every rusty-tiles function GeoTools needs; a wheel without one is reported as not available."""

MINIMUM_RUSTY_TILES = "0.4.1"

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


# Probed statuses by distribution name ("rusty-tiles", "pyproj"); reset_cache() empties it.
_cache: dict[str, DependencyStatus] = {}


def probe_rusty_tiles() -> DependencyStatus:
    """Return the rusty-tiles status, probing once and caching the result."""
    status = _cache.get("rusty-tiles")
    if status is None:
        status = _cache["rusty-tiles"] = _probe()
        if status.available:
            log.info("%s", status.summary)
        else:
            log.warning("%s", status.summary)
    return status


def probe_pyproj() -> DependencyStatus:
    """Return the pyproj status, probing once and caching the result.

    Not called at registration: importing pyproj loads PROJ, which only the
    About panel, Check Dependencies and an import that reprojects need.
    """
    status = _cache.get("pyproj")
    if status is None:
        status = _cache["pyproj"] = _probe_pyproj()
        if status.available:
            log.info("%s, data %s", status.summary, status.data_dir)
        else:
            log.warning("%s", status.summary)
    return status


def probe_all() -> tuple[DependencyStatus, ...]:
    """Every bundled library, in display order."""
    return probe_rusty_tiles(), probe_pyproj()


def reset_cache() -> None:
    """Forget the cached statuses so the next probe imports again."""
    _cache.clear()
    importlib.invalidate_caches()
    from . import crs_catalog  # its EPSG list and names come from these libraries

    crs_catalog.reset_cache()


def _import(module_name: str) -> ModuleType | str:
    """The imported module, or why it could not be imported.

    Records the identity of the library's files (:func:`_record_library`): of
    the files just loaded when this call imports it, else (loaded earlier in
    this process) only when nothing recorded it yet.
    """
    fresh = module_name not in sys.modules
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return f"{module_name} is not installed"
        return f"{module_name} failed to import: {exc}"
    except ImportError as exc:
        return f"{module_name} failed to import: {exc}"
    except Exception as exc:  # a broken native wheel can raise anything
        log.debug("%s load failure", module_name, exc_info=True)
        return f"{module_name} failed to load: {type(exc).__name__}: {exc}"
    _record_library(module, fresh=fresh)
    return module


def _probe() -> DependencyStatus:
    """Import rusty-tiles and read its version, catching any load failure.

    A wheel without :data:`REQUIRED_FUNCTIONS` (rusty-tiles 0.4.0) is not available.
    """
    module = _import(MODULE_NAME)
    if isinstance(module, str):
        return DependencyStatus(False, error=module)
    version = _version(module)
    missing = [name for name in REQUIRED_FUNCTIONS if not callable(getattr(module, name, None))]
    if missing:
        names = missing[0] if len(missing) == 1 else f"{', '.join(missing[:-1])} or {missing[-1]}"
        return DependencyStatus(
            False,
            version=version,
            error=f"rusty-tiles {version or '(unknown version)'} has no "
            f"{names}; GeoTools needs rusty-tiles {MINIMUM_RUSTY_TILES} or later",
        )
    return DependencyStatus(True, version=version)


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


# --- updated while Blender is running ------------------------------------------------------

RESTART_MESSAGE = (
    "GeoTools was updated while Blender was running. Restart Blender to finish the update."
)
"""The one notice for a library or the extension changed on disk under the running process."""

UPDATE_ISSUE_CODE = "restart-after-update"
"""Run Checks' issue code for :data:`RESTART_MESSAGE`."""

PROJ_DATABASE_MESSAGE = (
    "PROJ's database could not be read. Restart Blender; if that does not help, reinstall GeoTools."
)
"""The plain message for a PROJ database error when no update was detected."""

PROJ_DATABASE_MARKERS = (
    "sqlite error",
    "disk i/o error",
    "no database context",
    "unable to open database",
    "database disk image is malformed",
)
"""Lower-case text that marks a PROJ error as a failure to read proj.db.

A bare ``proj_create`` is not one: PROJ prefixes ordinary refusals with it too
("proj_create: crs not found", "proj_create: unrecognized format"), and those
must still name the CRS the user typed. The real database failure reads
"proj_create: SQLite error [ code = 10, msg = disk I/O error ]".
"""

SESSION_KEY = "_geotools_session"
"""The ``sys.modules`` entry holding what this Blender process loaded."""

LIBRARY_DISTS = {MODULE_NAME: DIST_NAME, PYPROJ_MODULE: PYPROJ_DIST}
"""The bundled libraries whose files are recorded, by import name."""

_NATIVE_SUFFIXES = (".so", ".pyd", ".dylib", ".dll")
_KEPT_PROJ_ERRORS = 5

Identity = tuple[str, int, int, int]
"""``(path, inode, mtime_ns, size)``; plain tuples, so a reloaded module compares them."""


def _session() -> dict[str, Any]:
    """The process-wide record: ``version``, ``files`` (name -> identities),
    ``changed`` (why a restart is needed) and ``proj_errors`` (raw texts)."""
    holder = sys.modules.get(SESSION_KEY)
    state = getattr(holder, "state", None)
    if not isinstance(state, dict):
        holder = ModuleType(
            SESSION_KEY, "What GeoTools loaded in this process (geotools.core.deps)."
        )
        state = {"version": None, "files": {}, "changed": [], "proj_errors": []}
        setattr(holder, "state", state)  # noqa: B010 (a plain module has no declared attribute)
        sys.modules[SESSION_KEY] = holder
    return state


def reset_session() -> None:
    """Forget the process record (tests only: a real process keeps it until Blender exits)."""
    sys.modules.pop(SESSION_KEY, None)


def _identity(path: str) -> Identity | None:
    try:
        info = Path(path).stat()
    except OSError:
        return None
    return (path, info.st_ino, info.st_mtime_ns, info.st_size)


def _native_files(folder: Path) -> list[str]:
    try:
        names = sorted(p.name for p in folder.iterdir() if p.is_file())
    except OSError:
        return []
    return [str(folder / n) for n in names if any(s in n for s in _NATIVE_SUFFIXES)]


def library_files(module: ModuleType) -> list[str]:
    """The files a running process holds for a library, found without hashing anything.

    The package's ``__init__``, its native extension modules, the shared
    libraries a wheel repair tool put beside it (``<name>.libs``, macOS
    ``.dylibs``) and, for pyproj, PROJ's ``proj.db``. Empty for a module with
    no file (a test double).
    """
    init = getattr(module, "__file__", None)
    if not isinstance(init, str):
        return []
    package = Path(init).parent
    files = [init]
    for folder in (package, package / ".dylibs", package.parent / f"{package.name}.libs"):
        files += _native_files(folder)
    if module.__name__ == PYPROJ_MODULE:
        try:
            files.append(str(Path(module.datadir.get_data_dir()) / "proj.db"))
        except Exception:  # no data directory: probe_pyproj reports it
            log.debug("no PROJ data directory to record", exc_info=True)
    return files


def _record_library(module: ModuleType, *, fresh: bool) -> None:
    """Record a library's files; ``fresh`` when this process has just loaded them.

    A library loaded before anything recorded it (by a GeoTools from before
    this record) is compared by version instead: its ``__version__`` in memory
    against the wheel metadata on disk.
    """
    name = module.__name__
    files = [i for i in map(_identity, library_files(module)) if i is not None]
    if not files:
        return
    recorded = _session()["files"]
    if not fresh and name in recorded:
        return
    recorded[name] = files
    if fresh:
        return
    loaded = getattr(module, "__version__", None)
    try:
        installed = importlib.metadata.version(LIBRARY_DISTS.get(name, name))
    except Exception:  # no or corrupt metadata: nothing to compare
        log.debug("no %s metadata to compare", name, exc_info=True)
        return
    if isinstance(loaded, str) and loaded != installed:
        _changed(f"{name} {loaded} is loaded but {installed} is installed")


def _changed(reason: str) -> None:
    changed = _session()["changed"]
    if reason not in changed:
        log.warning("restart needed: %s", reason)
        changed.append(reason)


def record_session(version: str, own_files: tuple[str, ...] = ()) -> None:
    """Record, at register, the extension version and the files this process holds.

    The first register in the process wins: a later one, after an in-place
    update, keeps the old record so :func:`check_for_update` sees the change.
    ``own_files`` are the extension's own (its manifest). Libraries already
    imported are recorded here; the rest when :func:`_import` loads them. Only
    ``os.stat``: cheap enough for register.
    """
    state = _session()
    if state["version"] is None and version and version != "unknown":
        state["version"] = version
    files = [i for i in map(_identity, own_files) if i is not None]
    if files:
        state["files"].setdefault("geotools", files)
    for module_name in LIBRARY_DISTS:
        module = sys.modules.get(module_name)
        if module is not None:
            _record_library(module, fresh=False)


def check_for_update(version: str | None = None) -> bool:
    """Compare the record with what is on disk now; True when Blender must restart.

    ``version`` is the extension version installed now. A handful of
    ``os.stat`` calls: About, Check Dependencies and Run Checks call it, never
    a panel's header or another panel's ``draw``.
    """
    state = _session()
    if state["changed"]:
        return True
    first = state["version"]
    if first and version and version != "unknown" and version != first:
        _changed(f"GeoTools {first} was loaded but {version} is installed")
    for name, files in state["files"].items():
        for identity in files:
            if _identity(identity[0]) != identity:
                _changed(f"{name}: {identity[0]} changed on disk")
                break
    return bool(state["changed"])


def update_pending() -> bool:
    """The last :func:`check_for_update` verdict, without touching the disk."""
    return bool(_session()["changed"])


def is_proj_database_error(error: BaseException | str) -> bool:
    """True when PROJ failed to read its database (:data:`PROJ_DATABASE_MARKERS`)."""
    text = str(error).lower()
    return any(marker in text for marker in PROJ_DATABASE_MARKERS)


def proj_error_message(error: BaseException | str) -> str | None:
    """The plain UI message for a PROJ database error; None for any other error.

    :data:`RESTART_MESSAGE` once an update was detected, else
    :data:`PROJ_DATABASE_MESSAGE`. The raw text is logged and kept for
    :func:`details_text`.
    """
    if not is_proj_database_error(error):
        return None
    text = str(error)
    errors = _session()["proj_errors"]
    if text not in errors:
        log.warning("PROJ could not read its database: %s", text)
        errors.append(text)
        del errors[:-_KEPT_PROJ_ERRORS]
    return RESTART_MESSAGE if update_pending() else PROJ_DATABASE_MESSAGE


def is_plain_proj_message(text: str) -> bool:
    """True for a message :func:`proj_error_message` gave."""
    return text in (RESTART_MESSAGE, PROJ_DATABASE_MESSAGE)


def details_text(version: str) -> str:
    """Copy Details: the version, each library's status, any restart reason, raw PROJ errors."""
    lines = [f"GeoTools {version}"]
    for status in _cache.values():
        lines.append(status.summary)
        if status.data_dir:
            lines.append(f"PROJ data: {status.data_dir}")
    state = _session()
    if state["version"] and state["version"] != version:
        lines.append(f"Loaded at start: GeoTools {state['version']}")
    lines += [f"Restart needed: {reason}" for reason in state["changed"]]
    lines += [f"PROJ error: {text}" for text in state["proj_errors"]]
    return "\n".join(lines)
