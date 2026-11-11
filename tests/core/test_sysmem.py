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
