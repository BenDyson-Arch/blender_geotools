"""File fingerprints for linked sources (``GeoToolsLink.fingerprint``).

A fingerprint is ``mtime_ns``, ``size`` and, for files under 64 MiB, the
SHA-256 of the contents, stored as JSON text. Checking is cheap: stat first,
and hash only when the stat changed.
"""

from __future__ import annotations

import hashlib
import json
import os

HASH_LIMIT = 64 * 1024 * 1024


def fingerprint(path: str | os.PathLike[str]) -> dict[str, object]:
    """``{"mtime_ns", "size", "sha256"}`` of ``path`` (sha256 None for large files)."""
    st = os.stat(path)
    digest = None
    if st.st_size < HASH_LIMIT:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        digest = h.hexdigest()
    return {"mtime_ns": st.st_mtime_ns, "size": st.st_size, "sha256": digest}


def to_text(print_: dict[str, object]) -> str:
    return json.dumps(print_, sort_keys=True)


def from_text(text: str) -> dict[str, object] | None:
    try:
        value = json.loads(text) if text else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def changed(path: str | os.PathLike[str], stored: str) -> bool | None:
    """True if the file differs from the stored fingerprint, None if it is missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    old = from_text(stored)
    if old is None:
        return True
    if st.st_mtime_ns == old.get("mtime_ns") and st.st_size == old.get("size"):
        return False
    if old.get("sha256") is None or st.st_size >= HASH_LIMIT:
        return True
    return fingerprint(path)["sha256"] != old.get("sha256")
