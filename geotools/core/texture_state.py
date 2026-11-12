"""Which source textures changed since the last publish (bpy-free).

Each publish records, in build.json ``source.images``, one entry per source
image: its name, where its pixels come from, and the SHA-256 of those pixels'
encoded bytes (the image file on disk, or the packed file in the ``.blend``).
Publish saves every edited image first (``blender.texture_state``), so the
bytes hashed are the bytes published.

Comparing the scene with that record finds the textures changed since: an
image with unsaved edits, an image whose file or packed bytes hash differently,
an image the source did not use before, and one it no longer uses. The live
comparison avoids hashing: a file whose size and modification time match the
record is taken as unchanged, and a hash is only computed (and cached by path,
size and time) when they differ.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

_CHUNK = 1 << 22


class Origin(StrEnum):
    """Where an image's pixels come from."""

    FILE = "file"  # a file on disk, saved
    PACKED = "packed"  # packed into the .blend
    GENERATED = "generated"  # a generated image, never painted
    UNSAVED = "unsaved"  # edits not saved anywhere yet


class Reason(StrEnum):
    EDITED = "edited"  # unsaved edits
    CHANGED = "changed"  # saved, but the bytes differ from the published ones
    NEW = "new"  # not in the last publish
    REMOVED = "removed"  # in the last publish, no longer used


REASON_TEXT = {
    Reason.EDITED: "unsaved edits",
    Reason.CHANGED: "changed",
    Reason.NEW: "new",
    Reason.REMOVED: "no longer used",
}


@dataclass(frozen=True, slots=True)
class ImageState:
    """One source image as build.json records it."""

    name: str
    origin: Origin
    sha256: str | None = None  # None while the image has unsaved edits
    path: str = ""  # absolute file path, "" if none
    bytes: int | None = None  # size of the hashed bytes
    mtime_ns: int | None = None  # file modification time, for the cheap comparison
    width: int = 0
    height: int = 0

    def to_record(self) -> dict[str, object]:
        return {
            "name": self.name,
            "origin": Origin(self.origin).value,
            "sha256": self.sha256,
            "path": self.path,
            "bytes": self.bytes,
            "mtime_ns": self.mtime_ns,
            "width": self.width,
            "height": self.height,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> ImageState:
        origin = str(record.get("origin", Origin.FILE.value))
        try:
            origin_ = Origin(origin)
        except ValueError:
            origin_ = Origin.FILE
        return cls(
            name=str(record.get("name", "")),
            origin=origin_,
            sha256=_str_or_none(record.get("sha256")),
            path=str(record.get("path") or ""),
            bytes=_int_or_none(record.get("bytes")),
            mtime_ns=_int_or_none(record.get("mtime_ns")),
            width=_int_or_none(record.get("width")) or 0,
            height=_int_or_none(record.get("height")) or 0,
        )


@dataclass(frozen=True, slots=True)
class Change:
    name: str
    reason: Reason

    @property
    def text(self) -> str:
        return f"{self.name}: {REASON_TEXT[self.reason]}"


def recorded_images(record: Mapping[str, object] | None) -> list[ImageState] | None:
    """The images a build.json recorded, or None if it records none (older builds)."""
    if not isinstance(record, Mapping):
        return None
    source = record.get("source")
    if not isinstance(source, Mapping):
        return None
    images = source.get("images")
    if not isinstance(images, list):
        return None
    return [ImageState.from_record(i) for i in images if isinstance(i, Mapping)]


def compare(
    current: Iterable[ImageState], recorded: Sequence[ImageState] | None
) -> list[Change] | None:
    """The changes from ``recorded`` to ``current``, sorted by name; None if unknown.

    An image with unsaved edits always counts as changed. A saved one counts
    when its hash differs from the recorded one, or either hash is unknown.
    """
    if recorded is None:
        return None
    before = {r.name: r for r in recorded}
    changes: list[Change] = []
    seen: set[str] = set()
    for state in current:
        seen.add(state.name)
        old = before.get(state.name)
        if state.origin is Origin.UNSAVED:
            changes.append(Change(state.name, Reason.EDITED))
        elif old is None:
            changes.append(Change(state.name, Reason.NEW))
        elif state.sha256 is None or old.sha256 is None or state.sha256 != old.sha256:
            changes.append(Change(state.name, Reason.CHANGED))
    changes += [Change(name, Reason.REMOVED) for name in before if name not in seen]
    return sorted(changes, key=lambda c: c.name)


def same_file(state: ImageState, old: ImageState | None) -> bool:
    """Whether a file can be taken as unchanged without hashing it (path, size, time)."""
    return (
        old is not None
        and old.sha256 is not None
        and bool(state.path)
        and state.path == old.path
        and state.bytes is not None
        and state.bytes == old.bytes
        and state.mtime_ns is not None
        and state.mtime_ns == old.mtime_ns
    )


def sha256_bytes(data: bytes | bytearray | memoryview) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str | None:
    """Hex SHA-256 of a file, or None if it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


class HashCache:
    """SHA-256 of files and blobs, cached by a key that changes with the content.

    Files are keyed by ``(path, size, mtime_ns)``; blobs by a caller key. The
    cache keeps at most ``limit`` entries, dropping the oldest.
    """

    def __init__(self, limit: int = 256) -> None:
        self._limit = limit
        self._hashes: dict[tuple, str] = {}

    def file(self, path: str, size: int, mtime_ns: int) -> str | None:
        key = ("file", path, size, mtime_ns)
        found = self._hashes.get(key)
        if found is None:
            found = sha256_file(path)
            if found is not None:
                self._put(key, found)
        return found

    def blob(self, key: tuple, read) -> str:
        """The hash of ``read()``'s bytes, computed once per ``key``."""
        full = ("blob", *key)
        found = self._hashes.get(full)
        if found is None:
            found = sha256_bytes(read())
            self._put(full, found)
        return found

    def forget(self, predicate) -> None:
        """Drop the entries whose key satisfies ``predicate``."""
        for key in [k for k in self._hashes if predicate(k)]:
            del self._hashes[key]

    def clear(self) -> None:
        self._hashes.clear()

    def _put(self, key: tuple, value: str) -> None:
        self._hashes[key] = value
        while len(self._hashes) > self._limit:
            del self._hashes[next(iter(self._hashes))]


def summary_text(changes: Sequence[Change] | None) -> str:
    """The Publish panel line, e.g. "3 textures changed since last publish".

    Empty when nothing changed, or nothing is known.
    """
    if not changes:
        return ""
    n = len(changes)
    return f"{n} texture{'s' * (n != 1)} changed since last publish"


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None
