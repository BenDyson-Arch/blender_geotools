"""File fingerprints for linked sources (core.fingerprint)."""

from __future__ import annotations

import os
from pathlib import Path

from geotools.core import fingerprint as fp
from geotools.core.fingerprint import FileState, Fingerprint


def _write(path: Path, data: bytes, mtime_ns: int) -> None:
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_take_and_text_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "a.geojson"
    _write(path, b"{}", 1_700_000_000_000_000_000)
    print_ = fp.take(path)
    assert print_.size == 2 and print_.mtime_ns == 1_700_000_000_000_000_000
    assert print_.sha256 is not None and len(print_.sha256) == 64
    assert Fingerprint.from_text(print_.to_text()) == print_
    assert Fingerprint.from_text("") is None
    assert Fingerprint.from_text("{") is None


def test_check_states(tmp_path: Path) -> None:
    path = tmp_path / "a.geojson"
    _write(path, b"one", 1_000)
    stored = fp.take(path)
    assert fp.check(path, stored) == (FileState.SAME, stored)

    _write(path, b"one", 2_000)  # touched, same bytes
    state, current = fp.check(path, stored)
    assert state is FileState.TOUCHED and current is not None and current.mtime_ns == 2_000

    _write(path, b"two", 2_000)  # same size and mtime: stat says same, so no hash
    assert fp.check(path, current)[0] is FileState.SAME

    _write(path, b"three", 3_000)
    assert fp.check(path, current)[0] is FileState.CHANGED

    path.unlink()
    assert fp.check(path, current) == (FileState.MISSING, None)
    assert fp.check(tmp_path / "new", None)[0] is FileState.MISSING


def test_wal_file_is_part_of_the_fingerprint(tmp_path: Path) -> None:
    path = tmp_path / "site.gpkg"
    _write(path, b"main", 1_000)
    stored = fp.take(path)
    wal = tmp_path / "site.gpkg-wal"
    _write(wal, b"committed edit", 5_000)
    state, current = fp.check(path, stored)
    assert state is FileState.CHANGED
    assert current is not None and current.size == 4 + len(b"committed edit")
    assert current.mtime_ns == 5_000


def test_large_files_compare_by_stat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fp, "HASH_LIMIT", 4)
    path = tmp_path / "big.gpkg"
    _write(path, b"12345", 1_000)
    stored = fp.take(path)
    assert stored.sha256 is None
    _write(path, b"12345", 2_000)  # touched: without a hash this counts as changed
    assert fp.check(path, stored)[0] is FileState.CHANGED
