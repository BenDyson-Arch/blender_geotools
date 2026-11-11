"""Metashape offset files. CONTRACT (M1).

The rule is rusty-tiles' own ``--source-offset-file`` rule (``parse_offset_file`` in
its ``mesh_large/source_crs.rs``), reproduced exactly so a file GeoTools accepts is
one rusty-tiles accepts, with the same numbers.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

from .placement import Vec3

OFFSET_FILE_MAX_BYTES = 4096
"""rusty-tiles refuses larger offset files before parsing them."""

_KEYS = ("E: ", "N: ", "A: ")
_BAD_LINES = (
    'The offset file must be exactly the lines "E: <number>", "N: <number>" and "A: <number>".'
)
# Rust's f64::from_str grammar for finite numbers: optional sign, digits with an
# optional point (or a point and digits), optional exponent. No spaces, no
# underscores, no hex. inf/nan parse in Rust but are then refused as non-finite.
_NUMBER = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


class OffsetFileError(ValueError):
    """The file is not a valid Metashape offset file."""


def parse_offset_text(text: str) -> Vec3:
    """Parse exactly ``E: <n>``, ``N: <n>``, ``A: <n>`` lines, in that order.

    Same rule as rusty-tiles ``--source-offset-file``: optional final LF or CRLF,
    at most 4 KiB, anything else raises OffsetFileError.
    """
    if len(text.encode("utf-8", "surrogatepass")) > OFFSET_FILE_MAX_BYTES:
        raise OffsetFileError("The offset file must be at most 4 KiB.")
    # One final CRLF or LF is optional; every other line may end in CR.
    if text.endswith("\r\n"):
        body = text[:-2]
    elif text.endswith("\n"):
        body = text[:-1]
    else:
        body = text
    lines = body.split("\n")
    if len(lines) != 3:
        raise OffsetFileError(_BAD_LINES)
    values = []
    for raw, key in zip(lines, _KEYS, strict=True):
        line = raw.removesuffix("\r")  # one CR at most, as rusty-tiles strips
        if not line.startswith(key):
            raise OffsetFileError(_BAD_LINES)
        number = line[len(key) :]
        if not _NUMBER.fullmatch(number):
            raise OffsetFileError(_BAD_LINES)
        value = float(number)
        if not math.isfinite(value):  # e.g. 1e400
            raise OffsetFileError(_BAD_LINES)
        values.append(value)
    return (values[0], values[1], values[2])


def read_offset_file(path: Path) -> Vec3:
    """Read and parse an offset file.

    Raises OffsetFileError if it is over 4 KiB, not UTF-8 or malformed, and
    OSError if it cannot be read.
    """
    with Path(path).open("rb") as handle:
        data = handle.read(OFFSET_FILE_MAX_BYTES + 1)
    if len(data) > OFFSET_FILE_MAX_BYTES:
        raise OffsetFileError("The offset file must be at most 4 KiB.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OffsetFileError("The offset file must be UTF-8 text.") from None
    return parse_offset_text(text)


def find_offset_file(source: Path) -> Path | None:
    """An offset file next to ``source``: ``<stem>_offset.txt``, ``<stem>.offset.txt``
    or ``offset.txt``, first match in that order."""
    source = Path(source)
    for name in (f"{source.stem}_offset.txt", f"{source.stem}.offset.txt", "offset.txt"):
        candidate = source.with_name(name)
        if candidate.is_file():
            return candidate
    return None
