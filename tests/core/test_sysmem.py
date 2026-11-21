from __future__ import annotations

from geotools.core import sysmem
from geotools.core.sysmem import GIB, auto_memory_budget


def test_auto_budget_on_a_workstation() -> None:
    # 42 GiB available -> 0.6 * 42 - 4 = 21.2 GiB, rounded down to 256 MiB = 21.0 GiB.
    assert auto_memory_budget(42 * GIB) == 21 * GIB


def test_auto_budget_floor_cap_and_unknown() -> None:
    assert auto_memory_budget(4 * GIB) == 2 * GIB  # floor
    assert auto_memory_budget(42 * GIB, cap_bytes=8 * GIB) == 8 * GIB
    assert auto_memory_budget(None) is None
    assert auto_memory_budget(0) is None


def test_available_memory_is_plausible() -> None:
    available = sysmem.available_memory_bytes()
    assert available is None or available > 64 << 20


def test_reading_expires_after_max_age() -> None:
    reading = sysmem.MemoryReading(8 * GIB, taken_at=100.0)
    assert not reading.expired(now=100.0)
    assert not reading.expired(now=100.0 + sysmem.READING_MAX_AGE_SECONDS - 0.01)
    assert reading.expired(now=100.0 + sysmem.READING_MAX_AGE_SECONDS)
    assert reading.expired(now=101.0, max_age=1.0)


def test_last_reading_never_reads_and_fresh_reads_always_do(monkeypatch) -> None:
    reads: list[int] = []

    def fake_read() -> int:
        reads.append(1)
        return 12 * GIB + len(reads)

    monkeypatch.setattr(sysmem, "_read_available", fake_read)
    sysmem.forget_reading()
    assert sysmem.last_reading() is None
    assert reads == []
    assert sysmem.available_memory_bytes() == 12 * GIB + 1
    reading = sysmem.last_reading()
    assert reading is not None and reading.available == 12 * GIB + 1
    assert not reading.expired()
    assert sysmem.last_reading() is reading and len(reads) == 1
    # A publish asks for the memory now, however recent the last reading.
    assert sysmem.available_memory_bytes() == 12 * GIB + 2
    assert len(reads) == 2
    sysmem.forget_reading()
