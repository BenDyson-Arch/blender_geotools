"""Where the Moon is: Meeus' lunar theory (Astronomical Algorithms, 2nd ed., ch. 47).

bpy-free, numpy only, vectorised over time. The geocentric position is
Meeus' truncation of Chapront's ELP-2000/82: 60 terms in longitude and
distance and 60 in latitude (``_moon_tables``), good to about 10" in longitude
and 4" in latitude near the present. Nutation, the true obliquity and the
sidereal time are the SPA's (:mod:`.sun`), as in Meeus. The topocentric place
(parallax, up to about 1 degree near the horizon) follows Meeus ch. 40.

Far from J2000 the theory's mean elements (polynomials in time) drift: the
declination extremes that set standstill azimuths depend on the obliquity
(Laskar, :func:`.sun.mean_obliquity`) and the orbit's inclination: within a few
arc seconds of JPL's DE441 back to 3000 BCE and 0.015 degree at 8000 BCE (mostly
Laskar's obliquity, 42" from Vondrák 2011 there), but the timing of a given
lunation, and so the date of an extreme, becomes uncertain by days, and
ΔT (TT - UT) by hours, more than 2,000 years from now.

Conventions are those of :mod:`.sun`: degrees, azimuth clockwise from true
north, Julian days in UT with ΔT from :func:`.sun.delta_t` unless given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import _moon_tables as _t
from . import sun

__all__ = [
    "INCLINATION",
    "MEAN_PARALLAX",
    "NODE_PERIOD_DAYS",
    "MoonEcliptic",
    "MoonPosition",
    "geocentric",
    "mean_node",
    "node_crossings",
    "position",
]

INCLINATION = 5.145396
"""Mean inclination of the Moon's orbit to the ecliptic, degrees (Meeus ch. 47)."""
INCLINATION_WOBBLE = 0.145
"""Half the range of the inclination's 173-day oscillation, degrees (about 9')."""
MEAN_PARALLAX = 0.950724
"""The Moon's mean equatorial horizontal parallax, degrees (3422.6")."""
SEMI_DIAMETER_RATIO = 0.272481
"""The Moon's radius in Earth equatorial radii: semi-diameter = asin(k sin parallax)."""
NODE_PERIOD_DAYS = 6798.38
"""Period of the mean node, days (18.61 years)."""

_LR = np.array(_t.LR_TERMS, dtype=np.float64)
_B = np.array(_t.B_TERMS, dtype=np.float64)
_EARTH_RADIUS_KM = 6378.14


def _poly(t: np.ndarray, *coefficients: float) -> np.ndarray:
    """c0 + c1 t + c2 t^2 + ... (lowest power first)."""
    total = np.zeros_like(t)
    for c in reversed(coefficients):
        total = total * t + c
    return total


def _arguments(t: np.ndarray) -> tuple[np.ndarray, ...]:
    """L', D, M, M', F in degrees at Julian centuries ``t`` (TT) from J2000 (eqs. 47.1-47.5)."""
    lp = _poly(t, 218.3164477, 481267.88123421, -0.0015786, 1 / 538841.0, -1 / 65194000.0)
    d = _poly(t, 297.8501921, 445267.1114034, -0.0018819, 1 / 545868.0, -1 / 113065000.0)
    m = _poly(t, 357.5291092, 35999.0502909, -0.0001536, 1 / 24490000.0)
    mp = _poly(t, 134.9633964, 477198.8675055, 0.0087414, 1 / 69699.0, -1 / 14712000.0)
    f = _poly(t, 93.2720950, 483202.0175233, -0.0036539, -1 / 3526000.0, 1 / 863310000.0)
    return lp, d, m, mp, f


@dataclass(frozen=True, slots=True)
class MoonEcliptic:
    """The geocentric Moon (float64 arrays): mean ecliptic and equinox of date."""

    longitude: np.ndarray
    """Geometric longitude λ, degrees, [0, 360) (no nutation)."""
    latitude: np.ndarray
    """Latitude β, degrees."""
    distance_km: np.ndarray
    """Centre of the Earth to centre of the Moon, km."""
    sums: tuple[np.ndarray, np.ndarray, np.ndarray]
    """Σl, Σb (1e-6 degree) and Σr (1e-3 km), for checking against Meeus."""

    @property
    def parallax(self) -> np.ndarray:
        """Equatorial horizontal parallax, degrees."""
        return np.degrees(np.arcsin(_EARTH_RADIUS_KM / self.distance_km))


def geocentric(jde: np.ndarray | float) -> MoonEcliptic:
    """The Moon's geocentric ecliptic place at Julian ephemeris days ``jde`` (TT)."""
    jde = np.atleast_1d(np.asarray(jde, dtype=np.float64))
    t = (jde - sun.J2000) / 36525.0
    lp, d, m, mp, f = _arguments(t)
    a1 = np.radians(119.75 + 131.849 * t)
    a2 = np.radians(53.09 + 479264.290 * t)
    a3 = np.radians(313.45 + 481266.484 * t)
    e = 1.0 - 0.002516 * t - 0.0000074 * t * t
    rad = np.radians
    args = np.stack([rad(d), rad(m), rad(mp), rad(f)])  # (4, n)

    def series(table: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        angle = table[:, :4] @ args  # (terms, n)
        # Terms in M are multiplied by E per power of |M| (the Earth's eccentricity).
        factor = e[None, :] ** np.abs(table[:, 1:2])
        return angle, factor

    angle, factor = series(_LR)
    sigma_l = ((_LR[:, 4:5] * factor) * np.sin(angle)).sum(axis=0)
    sigma_r = ((_LR[:, 5:6] * factor) * np.cos(angle)).sum(axis=0)
    angle, factor = series(_B)
    sigma_b = ((_B[:, 4:5] * factor) * np.sin(angle)).sum(axis=0)

    lp_r, f_r, mp_r = rad(lp), rad(f), rad(mp)
    sigma_l = sigma_l + 3958.0 * np.sin(a1) + 1962.0 * np.sin(lp_r - f_r) + 318.0 * np.sin(a2)
    sigma_b = (
        sigma_b
        - 2235.0 * np.sin(lp_r)
        + 382.0 * np.sin(a3)
        + 175.0 * np.sin(a1 - f_r)
        + 175.0 * np.sin(a1 + f_r)
        + 127.0 * np.sin(lp_r - mp_r)
        - 115.0 * np.sin(lp_r + mp_r)
    )
    return MoonEcliptic(
        longitude=(lp + sigma_l / 1e6) % 360.0,
        latitude=sigma_b / 1e6,
        distance_km=385000.56 + sigma_r / 1000.0,
        sums=(sigma_l, sigma_b, sigma_r),
    )


def mean_node(jde: np.ndarray | float) -> np.ndarray:
    """Longitude of the Moon's mean ascending node, degrees in [0, 360) (Meeus eq. 47.7).

    It moves backwards round the ecliptic once in 18.61 years. At 0 (the
    ascending node at the March equinox) the orbit is tilted furthest from the
    equator: the major standstill, declinations reaching ±(ε + i). At 180 it
    is the minor standstill, ±(ε - i).
    """
    jde = np.atleast_1d(np.asarray(jde, dtype=np.float64))
    t = (jde - sun.J2000) / 36525.0
    return _node_unwrapped(t) % 360.0


def _node_unwrapped(t: np.ndarray) -> np.ndarray:
    return _poly(t, 125.0445479, -1934.1362891, 0.0020754, 1 / 467441.0, -1 / 60616000.0)


def node_crossings(jde: float, target: float) -> tuple[float, float]:
    """The last and next Julian ephemeris days, around ``jde``, when the mean node is at ``target``.

    ``target`` is in degrees (0 for a major standstill, 180 for a minor one).
    Newton's method on Meeus' polynomial; the last is at or before ``jde``.
    """
    rate = 1934.1362891 / 36525.0  # degrees per day, decreasing

    def solve(value: float) -> float:
        t = (float(jde) - sun.J2000) / 36525.0
        t -= (value - float(_node_unwrapped(np.array([t]))[0])) / rate / 36525.0
        for _ in range(30):
            f = float(_node_unwrapped(np.array([t]))[0]) - value
            slope = -1934.1362891 + 2 * 0.0020754 * t + 3 * t * t / 467441.0 - 4 * t**3 / 60616000.0
            step = f / slope
            t -= step
            if abs(step) < 1e-13:
                break
        return sun.J2000 + t * 36525.0

    now = float(_node_unwrapped(np.array([(float(jde) - sun.J2000) / 36525.0]))[0])
    # The node decreases, so the last crossing had the smallest target + 360 k >= now.
    last_value = target + 360.0 * math.ceil((now - target) / 360.0)
    return solve(last_value), solve(last_value - 360.0)


@dataclass(frozen=True, slots=True)
class MoonPosition:
    """The Moon seen from a site, one value per time (float64 arrays)."""

    azimuth: np.ndarray
    """Topocentric azimuth, degrees clockwise from true north, [0, 360)."""
    elevation_airless: np.ndarray
    """Topocentric elevation of the centre, degrees, without refraction."""
    elevation_geocentric: np.ndarray
    """Elevation of the centre as seen from the Earth's centre (no parallax), degrees."""
    declination: np.ndarray
    """Topocentric declination, degrees."""
    geocentric_declination: np.ndarray
    """Apparent geocentric declination, degrees."""
    right_ascension: np.ndarray
    """Apparent geocentric right ascension, degrees."""
    longitude: np.ndarray
    """Apparent geocentric ecliptic longitude (with nutation), degrees."""
    latitude: np.ndarray
    distance_km: np.ndarray
    parallax: np.ndarray
    """Equatorial horizontal parallax, degrees."""
    obliquity: np.ndarray
    """True obliquity of the ecliptic, degrees."""

    @property
    def semi_diameter(self) -> np.ndarray:
        """Geocentric semi-diameter, degrees (the topocentric one is larger by < 0.3')."""
        return np.degrees(np.arcsin(SEMI_DIAMETER_RATIO * np.sin(np.radians(self.parallax))))


def _delta_t(jd: np.ndarray, given: float | None) -> np.ndarray:
    if given is not None:
        return np.full(jd.shape, float(given))
    return sun.delta_t(2000.0 + (jd - sun.J2000) / 365.25)


def position(
    jd: np.ndarray | float,
    latitude: float | None = None,
    longitude: float | None = None,
    height: float = 0.0,
    *,
    delta_t_seconds: float | None = None,
) -> MoonPosition:
    """The Moon at Julian days ``jd`` (UT): apparent geocentric, and topocentric from a site.

    Without ``latitude`` and ``longitude`` the topocentric fields equal the
    geocentric ones and the azimuth and elevation are NaN.
    """
    jd = np.atleast_1d(np.asarray(jd, dtype=np.float64))
    jde = jd + _delta_t(jd, delta_t_seconds) / 86400.0
    t = (jde - sun.J2000) / 36525.0
    geo = geocentric(jde)
    delta_psi, delta_eps = sun.nutation(t)
    eps = sun._mean_obliquity_jce(t) + delta_eps
    lam = geo.longitude + delta_psi
    lam_r, beta_r, eps_r = np.radians(lam), np.radians(geo.latitude), np.radians(eps)
    alpha = np.arctan2(
        np.sin(lam_r) * np.cos(eps_r) - np.tan(beta_r) * np.sin(eps_r), np.cos(lam_r)
    )
    delta = np.arcsin(
        np.sin(beta_r) * np.cos(eps_r) + np.cos(beta_r) * np.sin(eps_r) * np.sin(lam_r)
    )
    parallax = geo.parallax
    nan = np.full_like(jd, np.nan)
    if latitude is None or longitude is None:
        return MoonPosition(
            nan,
            nan,
            nan,
            np.degrees(delta),
            np.degrees(delta),
            np.degrees(alpha) % 360.0,
            lam % 360.0,
            geo.latitude,
            geo.distance_km,
            parallax,
            eps,
        )
    lat_r = math.radians(float(latitude))
    theta = sun.apparent_sidereal_time(jd, delta_psi, eps)
    h = np.radians(theta + float(longitude)) - alpha
    # Meeus ch. 40 (as the SPA's 3.12): rho sin(phi'), rho cos(phi').
    u = math.atan(0.99664719 * math.tan(lat_r))
    rho_sin = 0.99664719 * math.sin(u) + height / 6378140.0 * math.sin(lat_r)
    rho_cos = math.cos(u) + height / 6378140.0 * math.cos(lat_r)
    sin_pi = np.sin(np.radians(parallax))
    denominator = np.cos(delta) - rho_cos * sin_pi * np.cos(h)
    delta_alpha = np.arctan2(-rho_cos * sin_pi * np.sin(h), denominator)
    delta_topo = np.arctan2((np.sin(delta) - rho_sin * sin_pi) * np.cos(delta_alpha), denominator)
    h_topo = h - delta_alpha
    geocentric_elevation = np.degrees(
        np.arcsin(math.sin(lat_r) * np.sin(delta) + math.cos(lat_r) * np.cos(delta) * np.cos(h))
    )
    elevation = np.degrees(
        np.arcsin(
            math.sin(lat_r) * np.sin(delta_topo)
            + math.cos(lat_r) * np.cos(delta_topo) * np.cos(h_topo)
        )
    )
    gamma = np.degrees(
        np.arctan2(
            np.sin(h_topo),
            np.cos(h_topo) * math.sin(lat_r) - np.tan(delta_topo) * math.cos(lat_r),
        )
    )
    return MoonPosition(
        azimuth=(gamma + 180.0) % 360.0,
        elevation_airless=elevation,
        elevation_geocentric=geocentric_elevation,
        declination=np.degrees(delta_topo),
        geocentric_declination=np.degrees(delta),
        right_ascension=np.degrees(alpha) % 360.0,
        longitude=lam % 360.0,
        latitude=geo.latitude,
        distance_km=geo.distance_km,
        parallax=parallax,
        obliquity=eps,
    )
