"""Shared paths and helpers for the solar validation reference scripts.

These scripts regenerate the committed reference values in
``tests/data/validation/solar/`` with independent tools. They never import
GeoTools. They are not run in CI; see ``docs/validation/solar.md`` for the
exact environment and commands.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "tests" / "data" / "validation" / "solar"
EPHEMERIS = Path(os.environ.get("GEOTOOLS_DE440S", REPO / ".scratch" / "ephem" / "de440s.bsp"))

SPA_PRESSURE = 1013.25
SPA_TEMPERATURE = 12.0


def write(name: str, data: dict) -> Path:
    """Write ``data`` as compact JSON (floats to 12 significant digits) and print its size."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name

    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(v) for v in value]
        if isinstance(value, np.ndarray):
            return clean(value.tolist())
        if isinstance(value, (np.floating, float)):
            v = float(value)
            return float(f"{v:.12g}") if math.isfinite(v) else None
        if isinstance(value, np.integer):
            return int(value)
        return value

    path.write_text(json.dumps(clean(data), separators=(",", ":")) + "\n")
    print(f"wrote {path.relative_to(REPO)} ({path.stat().st_size / 1024:.1f} KiB)")
    return path


def versions() -> dict[str, str]:
    """Versions of the reference packages in this environment."""
    import importlib.metadata as md

    out = {}
    for name in ("numpy", "pvlib", "skyfield", "jplephem", "pyproj", "pandas"):
        try:
            out[name] = md.version(name)
        except md.PackageNotFoundError:
            pass
    return out


def spa_refraction(e0: np.ndarray, pressure: float = SPA_PRESSURE, temp: float = SPA_TEMPERATURE):
    """The SPA paper's refraction correction (eq. 42), degrees, for airless elevation ``e0``."""
    e0 = np.asarray(e0, dtype=np.float64)
    return (
        (pressure / 1010.0)
        * (283.0 / (273.0 + temp))
        * 1.02
        / (60.0 * np.tan(np.radians(e0 + 10.3 / (e0 + 5.11))))
    )


def apparent_horizon_airless() -> float:
    """The airless elevation whose SPA-refracted elevation is exactly 0 (standard air).

    GeoTools counts the sun as up when its refracted centre is above 0. With
    the SPA formula at 1013.25 hPa and 12 C that is an airless elevation of
    about -0.48 degrees. Bisection, independent of GeoTools.
    """
    lo, hi = -1.0, 0.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if mid + spa_refraction(mid) > 0.0:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
