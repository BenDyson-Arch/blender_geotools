"""The staging directory beside a published archive, and its lock.

bpy-free. For an output ``<dir>/<name>.3tz`` the staging directory is
``<dir>/<name>.geotools/``; for a metadata GLB ``<dir>/<name>.glb`` (a ``.glb``
build profile, M4) it is ``<dir>/<name>.glb.geotools/``, so a ``.3tz`` and a
``.glb`` of the same name keep separate records. A ``.glb`` staging directory
holds only ``facemap.npz`` and ``build.json``; the GLB is written in ``work/``
and moved onto the output once it is complete.

    .lock              held by the one job building this output (see StagingLock)
    source.glb         the exact rusty-tiles input of the last successful build
    facemap.npz        its face map (core.facemap)
    report.json        rusty-tiles' full report (TilerResult.report)
    diagnostics.json   rusty-tiles' runtime diagnostics (TilerResult.diagnostics)
    validation.json    rusty-tiles' validation report, without payload rows
    build.json         the build record (schema 1); written last
    images/            transient: images the Blender side extracted for this
                       attempt; deleted when the job ends
    work/              transient: the current attempt. Promoted into the files
                       above only after the archive is installed; kept after a
                       failure for debugging (with error.txt) until the next
                       attempt; deleted after a cancel
    preview/           an extracted copy of the archive for ``rusty-tiles preview``

The files of the last successful build are only replaced once a new archive is
installed, so the record always describes the archive beside it.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Self

STAGING_SUFFIX = ".geotools"
LOCK_NAME = ".lock"
GLB_NAME = "source.glb"
FACEMAP_NAME = "facemap.npz"
REPORT_NAME = "report.json"
DIAGNOSTICS_NAME = "diagnostics.json"
VALIDATION_NAME = "validation.json"
RECORD_NAME = "build.json"
ERROR_NAME = "error.txt"
IMAGES_DIR = "images"
WORK_DIR = "work"
PREVIEW_DIR = "preview"

# Promoted from work/ into the staging directory after a successful build, in
# this order; build.json is written after them.
PROMOTED_FILES = (GLB_NAME, FACEMAP_NAME, REPORT_NAME, DIAGNOSTICS_NAME, VALIDATION_NAME)


OUTPUT_SUFFIXES = (".3tz", ".glb")


def check_output(output: Path) -> Path:
    """The absolute output path; ValueError unless it names a ``.3tz`` or ``.glb`` file."""
    output = Path(output)
    if output.suffix not in OUTPUT_SUFFIXES or not output.stem:
        raise ValueError(f"the output must be a .3tz or .glb file, got {str(output)!r}")
    return Path(os.path.abspath(output))


def staging_dir_for(output: Path) -> Path:
    """``<dir>/<name>.geotools`` for ``<dir>/<name>.3tz``, ``<name>.glb.geotools`` for a GLB."""
    output = check_output(output)
    if output.suffix == ".glb":
        return output.with_name(output.name + STAGING_SUFFIX)
    return output.with_name(output.stem + STAGING_SUFFIX)


def images_dir_for(output: Path) -> Path:
    """Where the Blender side writes the images of one publish attempt."""
    return staging_dir_for(output) / IMAGES_DIR


def record_path_for(output: Path) -> Path:
    return staging_dir_for(output) / RECORD_NAME


def preview_dir_for(output: Path) -> Path:
    return staging_dir_for(output) / PREVIEW_DIR


class StagingBusyError(RuntimeError):
    """Another job, in this process or another, is building the same output."""


# Staging directories locked by this process. OS file locks alone are enough on
# POSIX (flock is per open file) and Windows, but this makes the in-process case
# independent of platform details.
_held: set[str] = set()
_held_lock = threading.Lock()


class StagingLock:
    """An exclusive lock on one staging directory.

    An OS lock (``flock`` on POSIX, ``msvcrt.locking`` on Windows) on
    ``.lock``, so a crashed Blender never leaves a stale lock: the OS drops it
    with the process. The file itself is left in place.
    """

    def __init__(self, staging_dir: Path, fd: int) -> None:
        self.staging_dir = staging_dir
        self._fd: int | None = fd

    @classmethod
    def acquire(cls, output: Path) -> StagingLock:
        """Lock the staging directory of ``output``, creating it if needed.

        Raises :class:`StagingBusyError` if it is already locked.
        """
        staging = staging_dir_for(output)
        key = os.path.normcase(str(staging))
        with _held_lock:
            if key in _held:
                raise StagingBusyError(f"{staging} is in use by another publish in this session")
            staging.mkdir(parents=True, exist_ok=True)
            fd = os.open(staging / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _os_lock(fd)
            except OSError:
                os.close(fd)
                raise StagingBusyError(
                    f"{staging} is in use by another publish (another Blender?)"
                ) from None
            try:
                os.ftruncate(fd, 0)
                os.write(fd, f"pid {os.getpid()} since {time.time():.0f}\n".encode())
            except OSError:
                pass  # informational only
            _held.add(key)
        return cls(staging, fd)

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """Release the lock. Safe to call more than once."""
        with _held_lock:
            fd, self._fd = self._fd, None
            if fd is None:
                return
            try:
                _os_unlock(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
                _held.discard(os.path.normcase(str(self.staging_dir)))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _os_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _os_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
