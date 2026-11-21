"""Available memory and the automatic rusty-tiles memory budget.

rusty-tiles' own default budget is deliberately conservative: half the detected
headroom, capped at 4 GiB. On a workstation that leaves its coarse-level stage
running one job at a time, so GeoTools passes an explicit budget derived from
the memory available when a publish starts, leaving room for Blender itself.

Reading the memory is not free everywhere: on macOS it runs ``vm_stat`` in a
subprocess. :func:`available_memory_bytes` always reads afresh (a publish's
budget and the DEM size check need the value now) and records each result;
:func:`last_reading` hands back that record without reading, which is what a
panel's ``draw()`` uses. ``MemoryReading.expired`` tells the caller when to
read again, off the draw path (``blender.publish.refresh_memory_reading``).
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

GIB = 1 << 30

AUTO_FRACTION = 0.6  # of available memory
AUTO_RESERVE_BYTES = 4 * GIB  # kept back for Blender and the rest of the system
AUTO_FLOOR_BYTES = 2 * GIB  # never ask for less than rusty-tiles' undetected default
AUTO_STEP_BYTES = 256 << 20  # round down so the value reads cleanly in the UI
READING_MAX_AGE_SECONDS = 5.0  # how long a panel may show a reading before asking for another


@dataclass(frozen=True)
class MemoryReading:
    """One :func:`available_memory_bytes` result and when it was taken."""

    available: int | None
    taken_at: float  # time.monotonic()

    def expired(self, now: float | None = None, max_age: float = READING_MAX_AGE_SECONDS) -> bool:
        """True once the reading is ``max_age`` seconds old (``now`` defaults to the clock)."""
        return (time.monotonic() if now is None else now) - self.taken_at >= max_age


class _Readings:
    last: MemoryReading | None = None


_readings = _Readings()


def available_memory_bytes() -> int | None:
    """Memory available to new allocations now, or None if it can't be read.

    Always reads (a subprocess on macOS), so never call it from ``draw()``;
    the result is recorded for :func:`last_reading`.
    """
    available = _read_available()
    _readings.last = MemoryReading(available, time.monotonic())
    return available


def last_reading() -> MemoryReading | None:
    """The last :func:`available_memory_bytes` result, or None; never reads."""
    return _readings.last


def forget_reading() -> None:
    """Drop the recorded reading, so the next :func:`last_reading` is None (tests)."""
    _readings.last = None


def _read_available() -> int | None:
    try:
        if sys.platform.startswith("linux"):
            return _linux_available()
        if sys.platform == "darwin":
            return _macos_available()
        if sys.platform == "win32":
            return _windows_available()
    except (OSError, ValueError, IndexError, subprocess.SubprocessError, AttributeError):
        # Any probe failure means "unknown", never a crash.
        return None
    return None


def auto_memory_budget(available: int | None, cap_bytes: int | None = None) -> int | None:
    """The budget GeoTools passes when the profile says Auto.

    ``AUTO_FRACTION`` of ``available`` minus ``AUTO_RESERVE_BYTES``, at least
    ``AUTO_FLOOR_BYTES``, at most ``cap_bytes`` if given, rounded down to
    ``AUTO_STEP_BYTES``. None (rusty-tiles' own default) if ``available`` is None.
    """
    if available is None or available <= 0:
        return None
    budget = max(int(available * AUTO_FRACTION) - AUTO_RESERVE_BYTES, AUTO_FLOOR_BYTES)
    if cap_bytes is not None and cap_bytes > 0:
        budget = min(budget, cap_bytes)
    return max(budget // AUTO_STEP_BYTES * AUTO_STEP_BYTES, AUTO_STEP_BYTES)


def _linux_available() -> int | None:
    with Path("/proc/meminfo").open(encoding="ascii") as meminfo:
        for line in meminfo:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


def _macos_available() -> int | None:
    # Free plus inactive plus purgeable pages, as Activity Monitor counts them.
    out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5, check=True)
    lines = out.stdout.splitlines()
    page = int(lines[0].split("page size of")[1].split()[0])
    counts: dict[str, int] = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        value = value.strip().rstrip(".")
        if value.isdigit():
            counts[key.strip()] = int(value)
    pages = sum(counts.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages purgeable"))
    return pages * page if pages else None


def _windows_available() -> int | None:
    if sys.platform != "win32":  # ctypes.windll exists on Windows only
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullAvailPhys)
