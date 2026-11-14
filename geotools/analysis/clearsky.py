"""Clear-sky irradiance: the Ineichen-Perez model with a fixed Linke turbidity.

bpy-free, numpy only. Used by the insolation tool (:mod:`.sun_hours`) to turn
the sun's position into direct normal (DNI) and diffuse horizontal (DHI)
irradiance in W/m² under a cloudless sky.

Model: P. Ineichen and R. Perez, "A new airmass independent formulation for
the Linke turbidity coefficient", Solar Energy 73(3), 151-157 (2002), as
implemented by pvlib-python's ``clearsky.ineichen`` (the default clear-sky
model there), with:

- relative air mass from Kasten and Young (1989), on the apparent zenith;
- absolute air mass: relative air mass x site pressure / 101325 Pa, with the
  pressure from the altitude (pvlib's ``alt2pres``, the standard atmosphere);
- extraterrestrial normal irradiance: a solar constant of 1366.1 W/m²
  divided by the square of the Earth-sun distance in AU (SPA gives it);
- the Linke turbidity TL (air mass 2) as one number for the whole period.

Assumptions and accuracy, stated plainly:

- **Cloudless sky.** The result is an upper bound. Real long-term totals with
  cloud are typically 50-80 % of clear-sky totals, depending on climate.
- **Linke turbidity is the dominant input.** Typical values: 2 (very clean,
  cold or high-altitude air), 3 (clean rural mid-latitude, the default), 4-5
  (humid summer or hazy), 6-7 (polluted). A TL error of 1 changes DNI by
  about 10 % at moderate air mass. It also varies by month; one fixed value
  for a year blurs that. Climatological monthly values (for example the
  SoDa/Meteotest maps) are better when known.
- Validation of the model against measured clear-sky data (Ineichen 2006,
  Solar Energy 80, 468-478) gives a global horizontal bias within about
  ±5 % and a scatter of a few percent, and larger errors for the beam
  component, given a good turbidity value.
- Below the horizon (apparent zenith of 90° or more) all three are 0.

Implemented from the equations; ``tests/core/test_sun_hours.py`` checks
the outputs against values computed with pvlib-python 0.16.1.
"""

from __future__ import annotations

import numpy as np

__all__ = [
    "DEFAULT_LINKE_TURBIDITY",
    "SOLAR_CONSTANT",
    "absolute_airmass",
    "altitude_pressure",
    "ineichen",
    "relative_airmass",
]

SOLAR_CONSTANT = 1366.1
"""Extraterrestrial normal irradiance at 1 AU, W/m² (pvlib's default)."""

DEFAULT_LINKE_TURBIDITY = 3.0
"""A clean rural mid-latitude atmosphere."""


def relative_airmass(zenith_deg: np.ndarray | float) -> np.ndarray:
    """Kasten and Young (1989) relative optical air mass; NaN at and below the horizon."""
    z = np.asarray(zenith_deg, dtype=np.float64)
    zc = np.where(z < 90.0, z, np.nan)
    with np.errstate(invalid="ignore"):
        return 1.0 / (np.cos(np.radians(zc)) + 0.50572 * (96.07995 - zc) ** -1.6364)


def altitude_pressure(altitude_m: float) -> float:
    """Standard-atmosphere pressure at an altitude, Pa."""
    return 100.0 * ((44331.514 - float(altitude_m)) / 11880.516) ** (1.0 / 0.1902632)


def absolute_airmass(relative: np.ndarray, pressure_pa: float = 101325.0) -> np.ndarray:
    """Pressure-corrected air mass."""
    return np.asarray(relative, dtype=np.float64) * float(pressure_pa) / 101325.0


def ineichen(
    apparent_zenith_deg: np.ndarray | float,
    *,
    linke_turbidity: float = DEFAULT_LINKE_TURBIDITY,
    altitude_m: float = 0.0,
    distance_au: np.ndarray | float = 1.0,
    solar_constant: float = SOLAR_CONSTANT,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Clear-sky (GHI, DNI, DHI) in W/m², each shaped like ``apparent_zenith_deg``.

    ``altitude_m`` is the site's height above sea level (an ellipsoidal height
    is close enough: tens of metres change nothing here). ``distance_au`` is
    the Earth-sun distance. Zero below the horizon.
    """
    zenith = np.asarray(apparent_zenith_deg, dtype=np.float64)
    tl = float(linke_turbidity)
    if not np.isfinite(tl) or tl <= 0:
        raise ValueError(f"The Linke turbidity must be positive, not {linke_turbidity!r}")
    altitude = float(altitude_m)
    dni_extra = float(solar_constant) / np.asarray(distance_au, dtype=np.float64) ** 2
    am = absolute_airmass(relative_airmass(zenith), altitude_pressure(altitude))
    up = np.isfinite(am)
    am = np.where(up, am, 0.0)
    cos_zenith = np.where(up, np.cos(np.radians(np.where(up, zenith, 0.0))), 0.0)

    fh1 = np.exp(-altitude / 8000.0)
    fh2 = np.exp(-altitude / 1250.0)
    cg1 = 5.09e-05 * altitude + 0.868
    cg2 = 3.92e-05 * altitude + 0.0387
    ghi = cg1 * dni_extra * cos_zenith * np.exp(-cg2 * am * (fh1 + fh2 * (tl - 1.0)))
    b = 0.664 + 0.163 / fh1
    beam = dni_extra * b * np.exp(-0.09 * am * (tl - 1.0))
    correction = 1.0 - (0.1 - 0.2 * np.exp(-tl)) / (0.1 + 0.882 / fh1)
    with np.errstate(divide="ignore", invalid="ignore"):
        beam_2 = np.where(up, ghi * np.clip(correction / cos_zenith, 0.0, 1e20), 0.0)
    dni = np.minimum(beam, beam_2)
    dhi = ghi - dni * cos_zenith
    zero = ~up
    return (
        np.where(zero, 0.0, ghi),
        np.where(zero, 0.0, dni),
        np.where(zero, 0.0, np.maximum(dhi, 0.0)),
    )
