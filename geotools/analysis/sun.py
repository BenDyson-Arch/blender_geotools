"""Where the sun is: NREL's Solar Position Algorithm, vectorised over time.

bpy-free and numpy only, so it can be tested with plain pytest and reused by
the later M5 tools (sun hours, insolation, archaeoastronomy), which need many
time steps: a year at one-minute steps is about half a million positions and
takes well under a second.

Algorithm: Reda and Andreas, "Solar Position Algorithm for Solar Radiation
Applications", NREL/TP-560-34302 (2003, revised 2008). The paper states an
uncertainty of +/-0.0003 degrees from -2000 to 6000. Here the ephemeris
follows the paper step for step (its periodic-term tables are in
``_spa_tables``), and the result matches its worked example to better than
0.0001 degrees (see ``tests/core/test_sun.py``). In practice the error budget
is set by the inputs, not the algorithm:

- UT1 - UTC (at most 0.9 s) is taken as zero, so times are UTC: up to
  0.004 degrees of hour angle.
- Delta T (TT - UT) comes from the Espenak and Meeus (2006) polynomials unless
  given. A few seconds of error moves the sun by about 0.00005 degrees today;
  for ancient dates its uncertainty grows to minutes or hours of rotation, which
  matters for archaeoastronomy, so pass a better value there.
- Refraction uses the paper's formula with the given pressure and temperature,
  and is only applied above the horizon (as in the paper). Near the horizon
  the real refraction varies by a fraction of a degree with the weather.
  GeoTools passes the standard-atmosphere pressure at the site's height
  (:func:`standard_pressure`) and the paper's 12 °C.

Conventions: azimuth is measured clockwise (eastward) from true north, in
degrees in [0, 360). Elevation is above the horizon, with refraction when
``refraction`` is on. Latitudes and longitudes are geodetic degrees, east and
north positive. Times are seconds since the Unix epoch, UTC, or Julian days
(UT); both are calendar-free, so dates before 1582 come in through the
proleptic Gregorian calendar of Python's ``datetime``.

Time zones (:func:`zone`, :func:`to_utc`) use ``zoneinfo`` IANA names. Python
on Windows has no system time zone database, and Blender's bundled Python does
not ship the ``tzdata`` package, so a named zone may be unavailable there:
:func:`zone` then raises :class:`TimeZoneUnavailable` and callers fall back to a
fixed UTC offset (:func:`fixed_offset`), which has no daylight saving.

The direction helpers (:func:`direction`, :func:`light_euler`) work in a
scene frame whose +Y is "north" for the caller: true north, or grid north once
the caller subtracts the grid convergence from the azimuth (see
:mod:`geotools.analysis.site`).
"""

from __future__ import annotations

import datetime as _dt
import math
import zoneinfo
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from . import _spa_tables as _t

__all__ = [
    "SunPosition",
    "TimeZoneUnavailable",
    "apparent_longitude",
    "apparent_sidereal_time",
    "delta_t",
    "direction",
    "fixed_offset",
    "julian_day",
    "light_euler",
    "local_times",
    "mean_obliquity",
    "nutation",
    "solar_position",
    "solar_position_jd",
    "standard_pressure",
    "time_zone_database_available",
    "to_utc",
    "zone",
]

UNIX_EPOCH_JD = 2440587.5
"""Julian day of 1970-01-01 00:00 UTC."""
J2000 = 2451545.0

DEFAULT_PRESSURE_HPA = 1013.25
DEFAULT_TEMPERATURE_C = 12.0
DEFAULT_ATMOS_REFRACT = 0.5667
"""Refraction at sunrise and sunset in degrees (the paper's default)."""

SUN_RADIUS_ARCSEC = 959.63
"""The sun's apparent radius at 1 AU, arc seconds (IAU)."""

_CHUNK = 32_768  # time steps per block: (64 terms x 32k) doubles is 16 MB

_ISA_LAPSE = 0.0065 / 288.15
"""The ICAO standard atmosphere's lapse rate over its sea-level temperature, 1/m."""
_ISA_EXPONENT = 5.255877
"""g M / (R L) of the ICAO standard atmosphere."""


def standard_pressure(height_m: float, sea_level_hpa: float = DEFAULT_PRESSURE_HPA) -> float:
    """Air pressure at ``height_m`` metres, hPa: ``sea_level_hpa`` scaled by the ISA.

    The barometric formula of the ICAO standard atmosphere's troposphere,
    P = P0 (1 - L h / T0) ^ (g M / R L), so exactly ``sea_level_hpa`` at 0 m
    (results at sea level do not change), 0.887 of it at 1,000 m and 0.608 at
    4,000 m. Heights are held to -500 m to 11,000 m, the troposphere. The
    height is the site's ellipsoidal one: the geoid's separation (up to about
    100 m) changes the pressure by about 1 % at most, and refraction with it.
    """
    h = float(height_m)
    if not math.isfinite(h):
        raise ValueError(f"height must be finite, not {height_m!r}")
    h = min(max(h, -500.0), 11_000.0)
    return float(sea_level_hpa) * (1.0 - _ISA_LAPSE * h) ** _ISA_EXPONENT


@dataclass(frozen=True, slots=True)
class SunPosition:
    """The sun seen from one site, one value per time step (float64 arrays)."""

    azimuth: np.ndarray
    """Topocentric azimuth, degrees clockwise from true north, in [0, 360)."""
    elevation: np.ndarray
    """Topocentric elevation, degrees, with refraction if requested."""
    elevation_airless: np.ndarray
    """Topocentric elevation, degrees, without refraction."""
    declination: np.ndarray
    """Topocentric declination, degrees."""
    hour_angle: np.ndarray
    """Topocentric local hour angle, degrees, westward from the meridian, in [-180, 180)."""
    distance_au: np.ndarray
    """Earth-sun distance, astronomical units."""

    @property
    def zenith(self) -> np.ndarray:
        """Topocentric zenith angle, degrees (``90 - elevation``)."""
        return 90.0 - self.elevation

    @property
    def angular_diameter(self) -> np.ndarray:
        """The sun's apparent diameter, degrees."""
        return 2.0 * SUN_RADIUS_ARCSEC / 3600.0 / self.distance_au

    def __len__(self) -> int:
        return int(self.azimuth.shape[0])


# --- time ------------------------------------------------------------------------------


_UNIX_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)


def utc_datetime(unix_seconds: float) -> _dt.datetime:
    """The aware UTC datetime of ``unix_seconds``, before 1970 too.

    ``datetime.fromtimestamp`` raises OSError for negative timestamps on
    Windows, so ancient epochs and pre-1970 dates count from the epoch instead.
    """
    return _UNIX_EPOCH + _dt.timedelta(seconds=float(unix_seconds))


def julian_day(unix_seconds: np.ndarray | float) -> np.ndarray:
    """Julian day (UT) of Unix seconds (UTC)."""
    return np.asarray(unix_seconds, dtype=np.float64) / 86400.0 + UNIX_EPOCH_JD


def delta_t(decimal_year: np.ndarray | float) -> np.ndarray:
    """TT - UT in seconds: the Espenak and Meeus (2006) polynomials (NASA eclipse site).

    Valid -1999 to 3000; outside that the long-term parabola is used. Accurate
    to about a second for 1900-2005 and a few seconds to 2050, and much less
    certain for ancient dates.
    """
    y = np.asarray(decimal_year, dtype=np.float64)
    u = (y - 1820.0) / 100.0
    long_term = -20.0 + 32.0 * u * u
    t = y / 100.0
    c1 = (y - 1000.0) / 100.0
    a = y - 1600.0
    b = y - 1700.0
    c = y - 1800.0
    d = y - 1860.0
    e = y - 1900.0
    f = y - 1920.0
    g = y - 1950.0
    h = y - 1975.0
    k = y - 2000.0
    conditions = [
        y < -500,
        y < 500,
        y < 1600,
        y < 1700,
        y < 1800,
        y < 1860,
        y < 1900,
        y < 1920,
        y < 1941,
        y < 1961,
        y < 1986,
        y < 2005,
        y < 2050,
        y < 2150,
    ]
    choices = [
        long_term,
        10583.6
        - 1014.41 * t
        + 33.78311 * t**2
        - 5.952053 * t**3
        - 0.1798452 * t**4
        + 0.022174192 * t**5
        + 0.0090316521 * t**6,
        1574.2
        - 556.01 * c1
        + 71.23472 * c1**2
        + 0.319781 * c1**3
        - 0.8503463 * c1**4
        - 0.005050998 * c1**5
        + 0.0083572073 * c1**6,
        120.0 - 0.9808 * a - 0.01532 * a**2 + a**3 / 7129.0,
        8.83 + 0.1603 * b - 0.0059285 * b**2 + 0.00013336 * b**3 - b**4 / 1174000.0,
        13.72
        - 0.332447 * c
        + 0.0068612 * c**2
        + 0.0041116 * c**3
        - 0.00037436 * c**4
        + 0.0000121272 * c**5
        - 0.0000001699 * c**6
        + 0.000000000875 * c**7,
        7.62
        + 0.5737 * d
        - 0.251754 * d**2
        + 0.01680668 * d**3
        - 0.0004473624 * d**4
        + d**5 / 233174.0,
        -2.79 + 1.494119 * e - 0.0598939 * e**2 + 0.0061966 * e**3 - 0.000197 * e**4,
        21.20 + 0.84493 * f - 0.076100 * f**2 + 0.0020936 * f**3,
        29.07 + 0.407 * g - g**2 / 233.0 + g**3 / 2547.0,
        45.45 + 1.067 * h - h**2 / 260.0 - h**3 / 718.0,
        63.86
        + 0.3345 * k
        - 0.060374 * k**2
        + 0.0017275 * k**3
        + 0.000651814 * k**4
        + 0.00002373599 * k**5,
        62.92 + 0.32217 * k + 0.005589 * k**2,
        long_term - 0.5628 * (2150.0 - y),
    ]
    return np.select(conditions, choices, default=long_term)


class TimeZoneUnavailable(LookupError):
    """A named time zone cannot be used: no time zone database, or an unknown name."""

    def __init__(self, message: str, *, no_database: bool) -> None:
        super().__init__(message)
        self.no_database = no_database


@lru_cache(maxsize=1)
def time_zone_database_available() -> bool:
    """True if ``zoneinfo`` can load named zones (the system database or ``tzdata``)."""
    try:
        zoneinfo.ZoneInfo("Etc/GMT-1")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def zone(name: str) -> _dt.tzinfo:
    """The IANA time zone ``name``, e.g. ``"Australia/Brisbane"``; ``"UTC"`` always works.

    Raises :class:`TimeZoneUnavailable` for an unknown name, or for any name
    other than UTC when this Python has no time zone database (Python on
    Windows without the ``tzdata`` package, as in Blender).
    """
    text = (name or "").strip()
    if text.upper() in {"UTC", "Z", "ETC/UTC", "GMT"}:
        return _dt.UTC
    if not text:
        raise TimeZoneUnavailable("No time zone given.", no_database=False)
    try:
        return zoneinfo.ZoneInfo(text)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        pass
    if not time_zone_database_available():
        raise TimeZoneUnavailable(
            f'Time zone "{text}" cannot be used: this Python has no time zone database '
            "(usual for Blender on Windows). Use a fixed UTC offset instead.",
            no_database=True,
        )
    raise TimeZoneUnavailable(
        f'Unknown time zone "{text}". Use an IANA name such as "Australia/Brisbane".',
        no_database=False,
    )


def fixed_offset(hours: float) -> _dt.timezone:
    """A fixed offset from UTC (no daylight saving), rounded to the minute."""
    minutes = round(float(hours) * 60.0)
    if not -24 * 60 < minutes < 24 * 60:
        raise ValueError(f"A UTC offset must be within +/-24 hours, not {hours!r}.")
    return _dt.timezone(_dt.timedelta(minutes=minutes))


def to_utc(local: _dt.datetime, tz: _dt.tzinfo) -> tuple[_dt.datetime, tuple[str, ...]]:
    """The UTC moment of wall-clock time ``local`` (naive) in ``tz``, with notes.

    A time that does not exist (skipped by a daylight-saving change) or is
    ambiguous (repeated) is resolved as ``zoneinfo`` does with ``fold=0`` (the
    offset before the change), and a note says so.
    """
    if local.tzinfo is not None:
        raise ValueError("to_utc takes a naive local time")
    notes: list[str] = []
    first = local.replace(tzinfo=tz, fold=0)
    second = local.replace(tzinfo=tz, fold=1)
    utc = first.astimezone(_dt.UTC)
    if first.utcoffset() != second.utcoffset():
        if utc.astimezone(tz).replace(tzinfo=None) != local:
            notes.append(
                f"{local:%Y-%m-%d %H:%M} does not exist in this time zone (a daylight-saving "
                "change skips it); it was read with the offset before the change."
            )
        else:
            notes.append(
                f"{local:%Y-%m-%d %H:%M} happens twice in this time zone (a daylight-saving "
                "change repeats it); the first was used."
            )
    return utc, tuple(notes)


def local_times(start_utc: _dt.datetime, count: int, step_seconds: float) -> np.ndarray:
    """``count`` Unix times from ``start_utc`` at ``step_seconds`` apart (float64)."""
    if start_utc.tzinfo is None:
        raise ValueError("start_utc must be time zone aware")
    return start_utc.timestamp() + float(step_seconds) * np.arange(int(count), dtype=np.float64)


# --- SPA -------------------------------------------------------------------------------

_L = tuple(np.array(rows, dtype=np.float64) for rows in _t.L_TERMS)
_B = tuple(np.array(rows, dtype=np.float64) for rows in _t.B_TERMS)
_R = tuple(np.array(rows, dtype=np.float64) for rows in _t.R_TERMS)
_NUT_Y = np.array(_t.NUTATION_Y, dtype=np.float64)
_NUT_ABCD = np.array(_t.NUTATION_ABCD, dtype=np.float64)
# Mean obliquity of the ecliptic in arc seconds, a polynomial in jme / 10 (eq. 24),
# highest power first.
_OBLIQUITY_ARCSEC = (
    2.45,
    5.79,
    27.87,
    7.12,
    -39.05,
    -249.67,
    -51.38,
    1999.25,
    -1.55,
    -4680.93,
    84381.448,
)


def _earth(tables: tuple[np.ndarray, ...], jme: np.ndarray) -> np.ndarray:
    """Sum_i (Sum_rows A cos(B + C jme)) jme^i / 1e8: the paper's eqs. 9-11."""
    total = np.zeros_like(jme)
    power = np.ones_like(jme)
    for rows in tables:
        series = rows[:, 0] @ np.cos(rows[:, 1:2] + rows[:, 2:3] * jme[None, :])
        total += series * power
        power = power * jme
    return total / 1e8


def _poly(x: np.ndarray, c0: float, c1: float, c2: float, c3_divisor: float) -> np.ndarray:
    return c0 + c1 * x + c2 * x * x + x * x * x / c3_divisor


EPHEMERIS_STEP_DAYS = 1.0 / 24.0
"""Node spacing when the ephemeris is interpolated (see :func:`solar_position_jd`)."""


def solar_position(
    unix_seconds: np.ndarray | float,
    latitude: float,
    longitude: float,
    height: float = 0.0,
    *,
    pressure_hpa: float = DEFAULT_PRESSURE_HPA,
    temperature_c: float = DEFAULT_TEMPERATURE_C,
    delta_t_seconds: float | None = None,
    refraction: bool = True,
    atmos_refract: float = DEFAULT_ATMOS_REFRACT,
) -> SunPosition:
    """The sun at Unix times (UTC) from a site; see :func:`solar_position_jd`."""
    return solar_position_jd(
        julian_day(np.atleast_1d(np.asarray(unix_seconds, dtype=np.float64))),
        latitude,
        longitude,
        height,
        pressure_hpa=pressure_hpa,
        temperature_c=temperature_c,
        delta_t_seconds=delta_t_seconds,
        refraction=refraction,
        atmos_refract=atmos_refract,
    )


def solar_position_jd(
    jd: np.ndarray | float,
    latitude: float,
    longitude: float,
    height: float = 0.0,
    *,
    pressure_hpa: float = DEFAULT_PRESSURE_HPA,
    temperature_c: float = DEFAULT_TEMPERATURE_C,
    delta_t_seconds: float | None = None,
    refraction: bool = True,
    atmos_refract: float = DEFAULT_ATMOS_REFRACT,
) -> SunPosition:
    """NREL SPA for Julian days ``jd`` (UT, a scalar or 1-D array).

    ``latitude`` and ``longitude`` are geodetic degrees (east positive);
    ``height`` is the site's height in metres (the paper's elevation, used only
    for the tiny parallax term, so ellipsoidal or above sea level does not
    matter). ``delta_t_seconds`` (TT - UT, one value) defaults to
    :func:`delta_t` at each time. ``pressure_hpa`` and ``temperature_c`` set
    the refraction; ``refraction`` False gives the airless elevation.

    The geocentric part of the algorithm (the Earth's heliocentric position
    and the nutation, about 260 periodic terms) is the costly one but changes
    slowly: its fastest term has a period of 13.6 days. With many closely
    spaced times it is evaluated exactly on an hourly grid of nodes and
    linearly interpolated, which is within about 1e-7 degrees of evaluating
    it at every time (tested to 5e-7), while the Earth's rotation is computed at every time.
    Sparse times are evaluated directly.
    """
    jd = np.atleast_1d(np.asarray(jd, dtype=np.float64))
    if jd.ndim != 1:
        raise ValueError("jd must be a scalar or a 1-D array")
    lat = float(latitude)
    lon = float(longitude)
    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
        raise ValueError(f"latitude must be within -90 to 90 degrees, not {latitude!r}")
    if not (math.isfinite(lon) and -180.0 <= lon <= 180.0):
        raise ValueError(f"longitude must be within -180 to 180 degrees, not {longitude!r}")
    if not np.isfinite(jd).all():
        raise ValueError("times must be finite")
    if delta_t_seconds is not None and not math.isfinite(float(delta_t_seconds)):
        raise ValueError("delta_t_seconds must be finite")
    if len(jd) == 0:
        empty = np.empty(0)
        return SunPosition(empty, empty, empty, empty, empty, empty)

    start, stop = float(jd.min()), float(jd.max())
    nodes = math.ceil((stop - start) / EPHEMERIS_STEP_DAYS) + 2
    if nodes * 4 < len(jd):
        grid = start + EPHEMERIS_STEP_DAYS * np.arange(nodes, dtype=np.float64)
        at_nodes = _ephemeris(grid, _delta_t_at(grid, delta_t_seconds))
        geo = tuple(np.interp(jd, grid, values) for values in at_nodes)
    else:
        geo = _ephemeris(jd, _delta_t_at(jd, delta_t_seconds))
    return _topocentric(
        jd,
        *geo,
        lat,
        lon,
        float(height),
        float(pressure_hpa),
        float(temperature_c),
        bool(refraction),
        float(atmos_refract),
    )


def apparent_longitude(jd: np.ndarray | float, delta_t_seconds: float | None = None) -> np.ndarray:
    """The sun's geocentric apparent longitude (degrees, in [0, 360)) at Julian days ``jd`` (UT).

    0, 90, 180 and 270 are the March equinox, June solstice, September
    equinox and December solstice.
    """
    jd = np.atleast_1d(np.asarray(jd, dtype=np.float64))
    lam = _ephemeris(jd, _delta_t_at(jd, delta_t_seconds))[0]
    return lam % 360.0


def _delta_t_at(jd: np.ndarray, given: float | None) -> np.ndarray:
    if given is not None:
        return np.full(jd.shape, float(given))
    return delta_t(2000.0 + (jd - J2000) / 365.25)


def _ephemeris(jd: np.ndarray, dt: np.ndarray) -> tuple[np.ndarray, ...]:
    """The slowly varying, geocentric part of SPA (paper sections 3.1 to 3.7).

    Returns ``(lambda, beta, R, delta_psi, epsilon)``: the apparent sun
    longitude (degrees, continuous rather than wrapped, so it interpolates),
    geocentric latitude (degrees), Earth radius vector (AU), nutation in
    longitude (degrees) and true obliquity of the ecliptic (degrees).
    """
    out = [np.empty_like(jd) for _ in range(5)]
    for i in range(0, len(jd), _CHUNK):
        block = slice(i, i + _CHUNK)
        for target, values in zip(out, _ephemeris_block(jd[block], dt[block]), strict=True):
            target[block] = values
    return tuple(out)


def _ephemeris_block(jd: np.ndarray, dt: np.ndarray) -> tuple[np.ndarray, ...]:
    jde = jd + dt / 86400.0
    jce = (jde - J2000) / 36525.0
    jme = jce / 10.0

    # Earth heliocentric longitude, latitude and radius (3.2); geocentric (3.3).
    theta = np.degrees(_earth(_L, jme)) + 180.0  # not wrapped
    beta = -np.degrees(_earth(_B, jme))
    big_r = _earth(_R, jme)

    # Nutation in longitude and obliquity (3.4); true obliquity (3.5).
    delta_psi, delta_eps = nutation(jce)
    eps = _mean_obliquity_jce(jce) + delta_eps

    # Aberration and the apparent sun longitude (3.6, 3.7).
    lam = theta + delta_psi - 20.4898 / (3600.0 * big_r)
    return lam, beta, big_r, delta_psi, eps


def nutation(jce: np.ndarray | float) -> tuple[np.ndarray, np.ndarray]:
    """Nutation in longitude and in obliquity (degrees) at Julian ephemeris centuries ``jce``.

    The SPA paper's 63-term series (IAU 1980, its section 3.4), with ``jce``
    counted from J2000 in TT.
    """
    jce = np.atleast_1d(np.asarray(jce, dtype=np.float64))
    x = np.stack(
        [
            _poly(jce, 297.85036, 445267.111480, -0.0019142, 189474.0),
            _poly(jce, 357.52772, 35999.050340, -0.0001603, -300000.0),
            _poly(jce, 134.96298, 477198.867398, 0.0086972, 56250.0),
            _poly(jce, 93.27191, 483202.017538, -0.0036825, 327270.0),
            _poly(jce, 125.04452, -1934.136261, 0.0020708, 450000.0),
        ]
    )
    arg = np.radians(_NUT_Y @ x)  # (terms, n)
    a, b, c, d = (_NUT_ABCD[:, i : i + 1] for i in range(4))
    delta_psi = ((a + b * jce) * np.sin(arg)).sum(axis=0) / 36_000_000.0
    delta_eps = ((c + d * jce) * np.cos(arg)).sum(axis=0) / 36_000_000.0
    return delta_psi, delta_eps


LASKAR_LIMIT_YEARS = 10_000.0
"""Laskar's obliquity polynomial is fitted for 10,000 years either side of J2000."""


def _mean_obliquity_jce(jce: np.ndarray) -> np.ndarray:
    u = jce / 100.0  # units of 10,000 Julian years
    eps0 = np.zeros_like(u)
    for coefficient in _OBLIQUITY_ARCSEC:  # Horner, highest power first
        eps0 = eps0 * u + coefficient
    return eps0 / 3600.0


def mean_obliquity(jde: np.ndarray | float) -> np.ndarray:
    """Mean obliquity of the ecliptic (degrees) at Julian ephemeris days ``jde`` (TT).

    Laskar (1986), "Secular terms of classical planetary theories using the
    results of general relativity", A&A 157, 59 (the SPA paper's eq. 24 and
    Meeus' eq. 22.3). Fitted for 10,000 years either side of J2000: 0.01"
    over 1,000 years. Against the modern long-term series (Vondrák, Capitaine
    and Wallace 2011) it is within 1" from 1000 BCE to 6000 CE, 5.5" at
    3000 BCE, 17.5" at 5000 BCE and 42" at 8000 BCE (0.012°, about 0.02° in a
    solstice sunrise azimuth at 51° N), and 26" at 12000 CE
    (docs/validation/archaeoastronomy.md). Raises ValueError beyond the
    10,000 years.
    """
    jde = np.atleast_1d(np.asarray(jde, dtype=np.float64))
    jce = (jde - J2000) / 36525.0
    if np.any(np.abs(jce) > LASKAR_LIMIT_YEARS / 100.0):
        raise ValueError(
            "Laskar's obliquity is only valid within 10,000 years of 2000 (8000 BCE to 12000 CE)."
        )
    return _mean_obliquity_jce(jce)


def apparent_sidereal_time(
    jd: np.ndarray | float, delta_psi: np.ndarray | float, eps: np.ndarray | float
) -> np.ndarray:
    """Apparent sidereal time at Greenwich (degrees, [0, 360)) at Julian days ``jd`` (UT).

    ``delta_psi`` and ``eps`` are the nutation in longitude and the true
    obliquity in degrees (SPA section 3.8, Meeus eq. 12.4).
    """
    jd = np.atleast_1d(np.asarray(jd, dtype=np.float64))
    jc = (jd - J2000) / 36525.0
    nu0 = (
        280.46061837
        + 360.98564736629 * (jd - J2000)
        + 0.000387933 * jc * jc
        - jc * jc * jc / 38_710_000.0
    ) % 360.0
    return (nu0 + np.asarray(delta_psi) * np.cos(np.radians(eps))) % 360.0


def _topocentric(
    jd: np.ndarray,
    lam: np.ndarray,
    beta: np.ndarray,
    big_r: np.ndarray,
    delta_psi: np.ndarray,
    eps: np.ndarray,
    lat: float,
    lon: float,
    height: float,
    pressure: float,
    temperature: float,
    refraction: bool,
    atmos_refract: float,
) -> SunPosition:
    """The fast part of SPA, at every time (paper sections 3.8 to 3.15)."""
    rad = np.radians
    deg = np.degrees

    # Apparent sidereal time at Greenwich (3.8).
    eps_r = rad(eps)
    nu = apparent_sidereal_time(jd, delta_psi, eps)

    # Geocentric right ascension and declination (3.9, 3.10).
    lam_r, beta_r = rad(lam % 360.0), rad(beta)
    alpha = deg(
        np.arctan2(np.sin(lam_r) * np.cos(eps_r) - np.tan(beta_r) * np.sin(eps_r), np.cos(lam_r))
    )
    delta = np.arcsin(
        np.sin(beta_r) * np.cos(eps_r) + np.cos(beta_r) * np.sin(eps_r) * np.sin(lam_r)
    )

    # Observer local hour angle (3.11).
    h_r = rad((nu + lon - alpha) % 360.0)

    # Topocentric right ascension and declination (3.12).
    xi = rad(8.794 / (3600.0 * big_r))
    lat_r = math.radians(lat)
    u_term = math.atan(0.99664719 * math.tan(lat_r))
    x_term = math.cos(u_term) + height / 6378140.0 * math.cos(lat_r)
    y_term = 0.99664719 * math.sin(u_term) + height / 6378140.0 * math.sin(lat_r)
    denominator = np.cos(delta) - x_term * np.sin(xi) * np.cos(h_r)
    delta_alpha = np.arctan2(-x_term * np.sin(xi) * np.sin(h_r), denominator)
    delta_prime = np.arctan2(
        (np.sin(delta) - y_term * np.sin(xi)) * np.cos(delta_alpha), denominator
    )
    # Topocentric local hour angle (3.13).
    h_prime = h_r - delta_alpha

    # Topocentric elevation, with refraction (3.14).
    e0 = deg(
        np.arcsin(
            math.sin(lat_r) * np.sin(delta_prime)
            + math.cos(lat_r) * np.cos(delta_prime) * np.cos(h_prime)
        )
    )
    if refraction:
        above = e0 >= -(0.26667 + atmos_refract)  # the paper's switch
        with np.errstate(divide="ignore", invalid="ignore"):
            delta_e = (
                (pressure / 1010.0)
                * (283.0 / (273.0 + temperature))
                * 1.02
                / (60.0 * np.tan(rad(e0 + 10.3 / (e0 + 5.11))))
            )
        e = e0 + np.where(above, delta_e, 0.0)
    else:
        e = e0

    # Topocentric azimuth, eastward from north (3.15).
    gamma = deg(
        np.arctan2(
            np.sin(h_prime),
            np.cos(h_prime) * math.sin(lat_r) - np.tan(delta_prime) * math.cos(lat_r),
        )
    )
    return SunPosition(
        azimuth=(gamma + 180.0) % 360.0,
        elevation=e,
        elevation_airless=e0,
        declination=deg(delta_prime),
        hour_angle=(deg(h_prime) + 180.0) % 360.0 - 180.0,
        distance_au=big_r,
    )


# --- direction in a scene --------------------------------------------------------------


def direction(azimuth: np.ndarray | float, elevation: np.ndarray | float) -> np.ndarray:
    """Unit vectors towards the sun, (N, 3): +X east, +Y north, +Z up.

    ``azimuth`` is clockwise from the frame's +Y in degrees. Pass the true
    azimuth minus the grid convergence to get grid axes.
    """
    az = np.radians(np.atleast_1d(np.asarray(azimuth, dtype=np.float64)))
    el = np.radians(np.atleast_1d(np.asarray(elevation, dtype=np.float64)))
    cos_el = np.cos(el)
    return np.stack([np.sin(az) * cos_el, np.cos(az) * cos_el, np.sin(el)], axis=-1)


def light_euler(azimuth: np.ndarray | float, elevation: np.ndarray | float) -> np.ndarray:
    """XYZ Euler angles (radians, (N, 3)) turning a light's -Z towards the ground.

    A Blender sun light shines along its local -Z, so its +Z must point at the
    sun: rotating (0, 0, 1) about X by 90 - elevation and then about Z by
    180 - azimuth gives :func:`direction`. The Z angle is unwrapped, so
    keyframes between time steps turn the short way round.
    """
    az = np.atleast_1d(np.asarray(azimuth, dtype=np.float64))
    el = np.atleast_1d(np.asarray(elevation, dtype=np.float64))
    x = np.radians(90.0 - el)
    z = np.unwrap(np.radians(180.0 - az))
    return np.stack([x, np.zeros_like(x), z], axis=-1)
