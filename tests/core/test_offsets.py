"""Tests for geotools.core.offsets: the rusty-tiles offset-file rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from geotools.core.offsets import (
    OFFSET_FILE_MAX_BYTES,
    OffsetFileError,
    find_offset_file,
    parse_offset_text,
    read_offset_file,
)

# The two offset files from rusty-tiles' own tests, byte for byte.
MGAL = "E: 14812000\nN: -1384000\nA: 100\n"
POHNPEI = "E: 17620600\nN: 768700\nA: 70\n"


def _padded(size: int) -> str:
    """A valid file of exactly ``size`` bytes, padded with leading zeros."""
    tail = "1\nN: 2\nA: 3\n"
    return "E: " + "0" * (size - len("E: ") - len(tail)) + tail


VALID: list[tuple[str, tuple[float, float, float]]] = [
    (MGAL, (14_812_000.0, -1_384_000.0, 100.0)),
    (POHNPEI, (17_620_600.0, 768_700.0, 70.0)),
    ("E: 1.25\r\nN: -2.5e3\r\nA: 0", (1.25, -2500.0, 0.0)),
    ("E: 1.25\r\nN: -2.5e3\r\nA: 0\r\n", (1.25, -2500.0, 0.0)),
    ("E: 1\r\nN: 2\nA: 3\r\n", (1.0, 2.0, 3.0)),  # mixed endings
    ("E: 1\nN: 2\nA: 3\r", (1.0, 2.0, 3.0)),  # a lone final CR is a line's CR
    ("E: 1\nN: 2\nA: 3", (1.0, 2.0, 3.0)),
    ("E: +1.\nN: .5\nA: -0\n", (1.0, 0.5, -0.0)),
    ("E: 1E3\nN: 2e-3\nA: 3e+0\n", (1000.0, 0.002, 3.0)),
    (
        "E: 14812000.123456789012345\nN: -1384000.0000001\nA: 100.5\n",
        (
            14812000.123456789012345,
            -1384000.0000001,
            100.5,
        ),
    ),
    ("E: 1e-400\nN: 2\nA: 3\n", (0.0, 2.0, 3.0)),  # underflow is finite
    (_padded(OFFSET_FILE_MAX_BYTES), (1.0, 2.0, 3.0)),
]

INVALID: list[str] = [
    # rusty-tiles' own refusals
    "",
    "E: 1\nN: 2\n",
    "E: 1\nN: 2\nA: 3\n\n",
    "N: 2\nE: 1\nA: 3\n",
    "E 1\nN: 2\nA: 3\n",
    "E=1\nN=2\nA=3\n",
    "e: 1\nn: 2\na: 3\n",
    "E:  1\nN: 2\nA: 3\n",
    "E: 1 \nN: 2\nA: 3\n",
    "# shift\nE: 1\nN: 2\nA: 3\n",
    "E: 1\nN: 2\nA: 3\nH: 4\n",
    "E: nan\nN: 2\nA: 3\n",
    "E: inf\nN: 2\nA: 3\n",
    "E: 1m\nN: 2\nA: 3\n",
    # more
    "E: 1\r\nN: 2\r\nA: 3\r\n\r\n",  # CRLF with an extra line
    "E: 1\r\rN: 2\nA: 3\n",
    "E: 1\r\r\nN: 2\nA: 3\n",
    "\r\nE: 1\nN: 2\nA: 3\n",
    " E: 1\nN: 2\nA: 3\n",
    "E:1\nN: 2\nA: 3\n",
    "E:\t1\nN: 2\nA: 3\n",
    "E: \nN: 2\nA: 3\n",
    "E: 1_000\nN: 2\nA: 3\n",
    "E: 0x10\nN: 2\nA: 3\n",
    "E: 1.0.0\nN: 2\nA: 3\n",
    "E: 1e\nN: 2\nA: 3\n",
    "E: .\nN: 2\nA: 3\n",
    "E: e5\nN: 2\nA: 3\n",
    "E: 1e400\nN: 2\nA: 3\n",  # overflows to infinity
    "E: Infinity\nN: 2\nA: 3\n",
    "E: -NaN\nN: 2\nA: 3\n",
    "E: \u0661\nN: 2\nA: 3\n",  # Arabic-Indic one: Python float() takes it, Rust does not
    "E: 1\u00a0\nN: 2\nA: 3\n",
    "\ufeffE: 1\nN: 2\nA: 3\n",  # a UTF-8 BOM
    "E: 1\nA: 3\nN: 2\n",  # wrong order
    "E: 1\nN: 2\nA: 3\nA: 3\n",  # extra line
    _padded(OFFSET_FILE_MAX_BYTES + 1),  # over 4 KiB, otherwise valid
]


@pytest.mark.parametrize(("text", "expected"), VALID)
def test_parse_valid(text: str, expected) -> None:
    assert parse_offset_text(text) == expected


@pytest.mark.parametrize("text", INVALID)
def test_parse_invalid(text: str) -> None:
    with pytest.raises(OffsetFileError) as info:
        parse_offset_text(text)
    assert isinstance(info.value, ValueError)
    assert str(info.value).endswith(".")


def test_parse_returns_floats() -> None:
    assert all(type(v) is float for v in parse_offset_text(MGAL))


def test_size_limit_counts_utf8_bytes() -> None:
    # 2-byte characters: under 4096 characters but over 4096 bytes.
    text = "E: 1\nN: 2\nA: 3\n" + "é" * 2100
    with pytest.raises(OffsetFileError, match="4 KiB"):
        parse_offset_text(text)


def test_read_offset_file(tmp_path: Path) -> None:
    path = tmp_path / "site_offset.txt"
    path.write_bytes(b"E: 1.25\r\nN: -2.5e3\r\nA: 0\r\n")
    assert read_offset_file(path) == (1.25, -2500.0, 0.0)


def test_read_offset_file_refuses_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / "offset.txt"
    path.write_bytes(b"E: 1\xff\nN: 2\nA: 3\n")
    with pytest.raises(OffsetFileError, match="UTF-8"):
        read_offset_file(path)


def test_read_offset_file_refuses_large_file_without_reading_it_all(tmp_path: Path) -> None:
    path = tmp_path / "offset.txt"
    path.write_bytes(b"x" * (10 * 1024 * 1024))
    with pytest.raises(OffsetFileError, match="4 KiB"):
        read_offset_file(path)


def test_read_offset_file_missing_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_offset_file(tmp_path / "nope.txt")


# --- find_offset_file --------------------------------------------------------------


def test_find_offset_file_order(tmp_path: Path) -> None:
    source = tmp_path / "site.glb"
    source.write_bytes(b"")
    assert find_offset_file(source) is None

    (tmp_path / "offset.txt").write_text(MGAL)
    assert find_offset_file(source) == tmp_path / "offset.txt"

    (tmp_path / "site.offset.txt").write_text(MGAL)
    assert find_offset_file(source) == tmp_path / "site.offset.txt"

    (tmp_path / "site_offset.txt").write_text(MGAL)
    assert find_offset_file(source) == tmp_path / "site_offset.txt"


def test_find_offset_file_ignores_other_stems_and_directories(tmp_path: Path) -> None:
    source = tmp_path / "site.glb"
    (tmp_path / "other_offset.txt").write_text(MGAL)
    (tmp_path / "site_offset.txt").mkdir()
    assert find_offset_file(source) is None


def test_find_offset_file_accepts_str(tmp_path: Path) -> None:
    (tmp_path / "mesh_offset.txt").write_text(MGAL)
    assert find_offset_file(str(tmp_path / "mesh.obj")) == tmp_path / "mesh_offset.txt"  # type: ignore[arg-type]


# --- against the real wheel ------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_glb(tmp_path_factory) -> Path:
    from geotools.core.probe import write_probe_glb

    path = tmp_path_factory.mktemp("glb") / "tiny.glb"
    write_probe_glb(path, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    return path


def _run_with_offset_file(rusty_tiles, glb: Path, offset: Path, out: Path) -> dict:
    return rusty_tiles.mesh_to_tileset(
        glb, out, jobs=1, source_crs="EPSG:3857", source_offset_file=offset
    ).report


@pytest.mark.parametrize(("text", "expected"), VALID)
def test_rusty_tiles_reads_valid_files_the_same(tmp_path, tiny_glb, text: str, expected) -> None:
    rusty_tiles = pytest.importorskip("rusty_tiles")
    path = tmp_path / "offset.txt"
    path.write_bytes(text.encode("utf-8"))
    report = _run_with_offset_file(rusty_tiles, tiny_glb, path, tmp_path / "o.3tz")
    ours = read_offset_file(path)
    assert ours == expected
    assert tuple(report["source_crs"]["source_offset"]) == ours


@pytest.mark.parametrize("text", INVALID)
def test_rusty_tiles_refuses_invalid_files_too(tmp_path, tiny_glb, text: str) -> None:
    rusty_tiles = pytest.importorskip("rusty_tiles")
    path = tmp_path / "offset.txt"
    path.write_bytes(text.encode("utf-8"))
    with pytest.raises(OffsetFileError):
        read_offset_file(path)
    with pytest.raises(rusty_tiles.InvalidRequestError):
        _run_with_offset_file(rusty_tiles, tiny_glb, path, tmp_path / "o.3tz")


def test_rusty_tiles_refuses_non_utf8_too(tmp_path, tiny_glb) -> None:
    rusty_tiles = pytest.importorskip("rusty_tiles")
    path = tmp_path / "offset.txt"
    path.write_bytes(b"E: 1\xff\nN: 2\nA: 3\n")
    with pytest.raises(rusty_tiles.InvalidRequestError):
        _run_with_offset_file(rusty_tiles, tiny_glb, path, tmp_path / "o.3tz")
