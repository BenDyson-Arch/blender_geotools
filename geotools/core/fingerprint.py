"""File fingerprints for linked sources (``GeoToolsLink.fingerprint``).

A fingerprint is the file's ``mtime_ns`` and size, plus a SHA-256 of its bytes
when it is under :data:`HASH_LIMIT`. Checking is cheap: stat first, and hash
only if the stat changed (a touched but unchanged file is then not "changed").

A GeoPackage edited in QGIS is usually in SQLite's WAL mode: committed edits
sit in ``<file>-wal`` until a checkpoint, and the main file does not change.
The fingerprint therefore covers the ``-wal`` file too when there is one.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

HASH_LIMIT = 64 * 1024 * 1024
"""Files (with their WAL) at or above this many bytes are compared by stat only."""

_CHUNK = 1024 * 1024


class FileState(StrEnum):
    SAME = "same"  # stat and content unchanged
    TOUCHED = "touched"  # stat changed, content the same
    CHANGED = "changed"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Fingerprint:
    mtime_ns: int
    size: int
    sha256: str | None  # None for files at or over HASH_LIMIT

    def to_text(self) -> str:
        return json.dumps(
            {"mtime_ns": self.mtime_ns, "size": self.size, "sha256": self.sha256}, sort_keys=True
        )

    @classmethod
    def from_text(cls, text: str) -> Fingerprint | None:
        """Read :meth:`to_text` output back; None for empty or invalid text."""
        if not text or not text.strip():
            return None
        try:
            raw = json.loads(text)
            return cls(int(raw["mtime_ns"]), int(raw["size"]), raw.get("sha256") or None)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def same_stat(self, other: Fingerprint) -> bool:
        return self.mtime_ns == other.mtime_ns and self.size == other.size


def _parts(path: Path) -> list[Path]:
    wal = path.with_name(path.name + "-wal")
    return [path, wal] if wal.is_file() else [path]


def stat(path: str | os.PathLike[str]) -> Fingerprint:
    """The stat part only (no hash). Raises OSError if the file is missing."""
    parts = _parts(Path(path))
    stats = [p.stat() for p in parts]
    return Fingerprint(max(s.st_mtime_ns for s in stats), sum(s.st_size for s in stats), None)


def take(path: str | os.PathLike[str]) -> Fingerprint:
    """The full fingerprint of ``path``. Raises OSError if the file is missing."""
    quick = stat(path)
    if quick.size >= HASH_LIMIT:
        return quick
    digest = hashlib.sha256()
    for part in _parts(Path(path)):
        with part.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                digest.update(chunk)
    return Fingerprint(quick.mtime_ns, quick.size, digest.hexdigest())


def check(
    path: str | os.PathLike[str], stored: Fingerprint | None
) -> tuple[FileState, Fingerprint | None]:
    """Compare ``path`` with ``stored``; return the state and the current fingerprint.

    The current fingerprint is None when the file is missing, and is only
    hashed when its stat differs from ``stored``.
    """
    try:
        quick = stat(path)
    except OSError:
        return FileState.MISSING, None
    if stored is None:
        return FileState.CHANGED, take(path)
    if quick.same_stat(stored):
        return FileState.SAME, stored
    try:
        current = take(path)
    except OSError:
        return FileState.MISSING, None
    if current.sha256 is not None and current.sha256 == stored.sha256:
        return FileState.TOUCHED, current
    return FileState.CHANGED, current
