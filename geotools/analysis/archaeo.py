"""Archaeoastronomy: rise and set events, lunar standstills and alignments. bpy-free.

Two kinds of event:

* **Epoch geometry** (:func:`epoch_events`): where a body of a given
  declination rises and sets, for the solstices (δ = ±ε), the equinoxes
  (δ = 0) and the lunar standstills (δ = ±(ε + i) major, ±(ε - i) minor, or
  the extremes the ephemeris reaches), with ε the mean obliquity at the
  chosen epoch (Laskar 1986, :func:`.sun.mean_obliquity`). This works for any
  year within 10,000 years of 2000, which dated sun positions cannot. The
  solstice declination is 24.02° in 3000 BCE against 23.44° now.
* **Dated sun events** (:func:`sun_rise_set`): sunrise and sunset times and
  azimuths on a calendar day, from NREL SPA (``sun``).

Both are found against the astronomical horizon (apparent altitude 0) and,
when given, a real horizon profile (:class:`.horizon.HorizonProfile`). A body
is on the horizon when the true altitude of its chosen limb equals the
horizon's apparent altitude h less the refraction there, h - R(h) (Bennett,
:func:`.horizon.refraction`). Rise is the first moment it comes above the
horizon and set the last moment it goes below, so a jagged skyline that
hides a body for a while gives the outer pair.

The Moon's topocentric parallax (about 1° at the horizon) lowers it by
asin(sin π cos h); it is included in both the geometry (mean parallax, or
the ephemeris' at the extreme) and the inversion (:func:`declination`).

Calendar dates use Meeus' algorithm (ch. 7): the Julian calendar before
1582 October 15 and the Gregorian after, with astronomical year numbers
(0 is 1 BCE, -2999 is 3000 BCE), as archaeoastronomy quotes them.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import asdict, dataclass

import numpy as np

from . import horizon, moon, sun

__all__ = [
    "Alignment",
    "Event",
    "Standstill",
    "alignments",
    "calendar_date",
    "dated_sun_events",
    "declination",
    "epoch_events",
    "epoch_jd",
    "epoch_trust",
    "format_date",
    "format_year",
    "julian_day_of",
    "rise_set_azimuths",
    "standstill",
    "sun_rise_set",
]

LIMBS = {"UPPER": 1.0, "CENTRE": 0.0, "LOWER": -1.0}
"""Which part of the disc touches the horizon: + semi-diameter above the centre for UPPER."""
SUN_SEMI_DIAMETER = sun.SUN_RADIUS_ARCSEC / 3600.0
"""At 1 AU, degrees; the dated events use the day's distance."""
HOUR_ANGLE_STEP = 0.01
"""Degrees of hour angle between points of a diurnal circle (:func:`rise_set_azimuths`)."""
DAY_STEP_SECONDS = 20.0
"""Seconds between sun positions when finding a dated sunrise or sunset."""
EPOCH_MIN, EPOCH_MAX = -7999, 11999
"""Years (astronomical) within reach of Laskar's obliquity."""


# --- calendar ------------------------------------------------------------------------------------


def julian_day_of(year: int, month: int, day: float) -> float:
    """Julian day (at 0h of ``day`` plus its fraction) of a calendar date (Meeus eq. 7.1).

    Julian calendar before 1582 October 15, Gregorian from it.
    """
    y, m = int(year), int(month)
    if m <= 2:
        y -= 1
        m += 12
    gregorian = (int(year), int(month), float(day)) >= (1582, 10, 15)
    b = 0
    if gregorian:
        a = math.floor(y / 100)
        b = 2 - a + math.floor(a / 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + float(day) + b - 1524.5


def calendar_date(jd: float) -> tuple[int, int, float]:
    """(year, month, day with fraction) of a Julian day (Meeus ch. 7), Julian before 1582 Oct 15."""
    z = math.floor(jd + 0.5)
    f = jd + 0.5 - z
    if z < 2299161:
        a = z
    else:
        alpha = math.floor((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - math.floor(alpha / 4)
    b = a + 1524
    c = math.floor((b - 122.1) / 365.25)
    d = math.floor(365.25 * c)
    e = math.floor((b - d) / 30.6001)
    day = b - d - math.floor(30.6001 * e) + f
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715
    return int(year), int(month), float(day)


def format_year(year: int) -> str:
    """Astronomical year as people say it: 2026 -> "2026 CE", -2999 -> "3000 BCE"."""
    y = int(year)
    return f"{y} CE" if y > 0 else f"{1 - y} BCE"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_date(jd: float, *, time: bool = False) -> str:
    """A Julian day (UT) as a readable date, noting the Julian calendar before 1582."""
    year, month, day = calendar_date(jd)
    whole = math.floor(day)
    text = f"{whole} {_MONTHS[month - 1]} {format_year(year)}"
    if time:
        minutes = round((day - whole) * 1440.0)
        if minutes == 1440:
            return format_date(jd + 1.0 / 2880.0, time=True)
        text += f" {minutes // 60:02d}:{minutes % 60:02d} UT"
    if jd < 2299160.5:
        text += " (Julian cal.)"
    return text


def epoch_jd(year: int) -> float:
    """The middle of an (astronomical) year, as a Julian day: the date an epoch stands for."""
    y = int(year)
    if not EPOCH_MIN <= y <= EPOCH_MAX:
        raise ValueError(
            f"The epoch must be within 10,000 years of 2000 ({format_year(EPOCH_MIN)} to "
            f"{format_year(EPOCH_MAX)}), not {format_year(y)}: Laskar's obliquity is not "
            "valid beyond that."
        )
    return julian_day_of(y, 7, 2.5)


def _jde(jd: float) -> float:
    return jd + float(sun.delta_t(2000.0 + (jd - sun.J2000) / 365.25)) / 86400.0


def epoch_trust(year: int) -> list[str]:
    """Plain statements of how far the results for ``year`` can be trusted."""
    years = abs(int(year) - 2000)
    notes = [
        (
            "Declinations: the obliquity (Laskar 1986) is good to arc seconds at this epoch; "
            "lunar extremes to about 0.01°. Refraction near the horizon varies by several arc "
            "minutes with the weather, which outweighs both."
        )
    ]
    if years > 4000:
        notes.append(
            f"{format_year(year)} is {years:,} years from 2000: the Moon's mean elements "
            "drift, so standstill dates are good only to weeks; the declinations still hold."
        )
    elif years > 2000:
        notes.append(
            "More than 2,000 years from now the standstill dates are good to days, not hours."
        )
    if int(year) < 500:
        notes.append(
            "ΔT (Earth rotation) is uncertain by minutes around 1 CE and by tens of minutes "
            "to hours before 2000 BCE: it moves times of day, not azimuths."
        )
    return notes


# --- rise and set geometry -----------------------------------------------------------------------


def _horizon_threshold(
    azimuth: np.ndarray, profile: horizon.HorizonProfile | None, refract: bool
) -> np.ndarray:
    """True altitude of a point on the horizon at ``azimuth``: h - R(h), h the apparent altitude."""
    h = np.zeros_like(azimuth) if profile is None else profile.at(azimuth)
    return horizon.true_altitude(h, refract)


def _parallax_drop(true_geocentric: np.ndarray, parallax_deg: float | np.ndarray) -> np.ndarray:
    """Altitude lost to parallax: p with sin p = sin π cos h' (h' topocentric), iterated once."""
    sin_pi = np.sin(np.radians(parallax_deg))
    p = np.degrees(np.arcsin(sin_pi * np.cos(np.radians(true_geocentric))))
    return np.degrees(np.arcsin(sin_pi * np.cos(np.radians(true_geocentric - p))))


@dataclass(frozen=True, slots=True)
class RiseSet:
    """Where a body of fixed declination rises and sets (degrees; NaN when it does not)."""

    rise_azimuth: float
    set_azimuth: float
    rise_altitude: float
    """Apparent horizon altitude where it rises."""
    set_altitude: float


def rise_set_azimuths(
    dec: np.ndarray | float,
    latitude: float,
    profile: horizon.HorizonProfile | None = None,
    *,
    refract: bool = True,
    limb: float = 0.0,
    parallax: float | np.ndarray = 0.0,
) -> list[RiseSet]:
    """Rise and set azimuths of bodies at declinations ``dec`` from ``latitude``.

    Traces each diurnal circle in steps of :data:`HOUR_ANGLE_STEP` and finds
    where the limb's true altitude crosses the horizon's (``profile``, or
    the astronomical horizon). ``limb`` is the limb's offset from the centre
    in degrees (+ semi-diameter for the upper limb); ``parallax`` the
    horizontal parallax (the Moon's). Rise is the first crossing upwards
    after lower culmination and set the last one downwards before the next.
    A body that never rises, or never sets, gives NaN.
    """
    decs = np.atleast_1d(np.asarray(dec, dtype=np.float64))
    pars = np.broadcast_to(np.asarray(parallax, dtype=np.float64), decs.shape)
    limbs = np.broadcast_to(np.asarray(limb, dtype=np.float64), decs.shape)
    lat = math.radians(float(latitude))
    count = round(360.0 / HOUR_ANGLE_STEP)
    h_angle = np.radians(-180.0 + HOUR_ANGLE_STEP * np.arange(count + 1, dtype=np.float64))
    d = np.radians(decs)[:, None]
    sin_alt = math.sin(lat) * np.sin(d) + math.cos(lat) * np.cos(d) * np.cos(h_angle)[None, :]
    altitude = np.degrees(np.arcsin(np.clip(sin_alt, -1.0, 1.0)))
    azimuth = (
        np.degrees(
            np.arctan2(
                np.sin(h_angle)[None, :],
                np.cos(h_angle)[None, :] * math.sin(lat) - np.tan(d) * math.cos(lat),
            )
        )
        + 180.0
    ) % 360.0
    topocentric = altitude - _parallax_drop(altitude, pars[:, None])
    g = topocentric + limbs[:, None] - _horizon_threshold(azimuth, profile, refract)
    out = []
    for i in range(len(decs)):
        out.append(_crossings(g[i], azimuth[i], profile))
    return out


def _interp_azimuth(a0: float, a1: float, f: float) -> float:
    delta = (a1 - a0 + 180.0) % 360.0 - 180.0
    return (a0 + f * delta) % 360.0


def _altitude_at(profile: horizon.HorizonProfile | None, az: float) -> float:
    return 0.0 if profile is None or not math.isfinite(az) else float(profile.at(az))


def _crossings(g: np.ndarray, azimuth: np.ndarray, profile) -> RiseSet:
    above = g > 0.0
    up = np.flatnonzero(~above[:-1] & above[1:])
    down = np.flatnonzero(above[:-1] & ~above[1:])
    rise = set_ = math.nan
    if len(up):
        i = int(up[0])
        f = g[i] / (g[i] - g[i + 1])
        rise = _interp_azimuth(float(azimuth[i]), float(azimuth[i + 1]), float(f))
    if len(down):
        i = int(down[-1])
        f = g[i] / (g[i] - g[i + 1])
        set_ = _interp_azimuth(float(azimuth[i]), float(azimuth[i + 1]), float(f))
    return RiseSet(rise, set_, _altitude_at(profile, rise), _altitude_at(profile, set_))


def declination(
    azimuth: np.ndarray | float,
    altitude: np.ndarray | float,
    latitude: np.ndarray | float,
    *,
    refract: bool = True,
    limb: float = 0.0,
    parallax: float = 0.0,
) -> np.ndarray:
    """The declination a body has when its limb is on the horizon at ``azimuth``, ``altitude``.

    The standard inversion: ``altitude`` is the horizon's apparent altitude;
    its true altitude is h - R(h) (with ``refract``), the centre is ``limb``
    below that, and parallax (the Moon's, ``parallax`` degrees) is added
    back to give the geocentric altitude h_g. Then
    sin δ = sin φ sin h_g + cos φ cos h_g cos A. Arrays broadcast.
    """
    az = np.radians(np.asarray(azimuth, dtype=np.float64))
    h = horizon.true_altitude(altitude, refract) - float(limb)
    if parallax:
        sin_pi = math.sin(math.radians(parallax))
        h = h + np.degrees(np.arcsin(sin_pi * np.cos(np.radians(h))))
    h_r = np.radians(h)
    lat = np.radians(np.asarray(latitude, dtype=np.float64))
    return np.degrees(np.arcsin(np.sin(lat) * np.sin(h_r) + np.cos(lat) * np.cos(h_r) * np.cos(az)))


# --- dated sun events ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SunRiseSet:
    """Sunrise and sunset on one local day (UTC times as Unix seconds; NaN when none)."""

    rise_time: float
    rise_azimuth: float
    rise_altitude: float
    set_time: float
    set_azimuth: float
    set_altitude: float
    declination: float
    """The sun's topocentric declination at local noon, degrees."""


def sun_rise_set(
    day: _dt.date,
    tz: _dt.tzinfo,
    latitude: float,
    longitude: float,
    height: float = 0.0,
    profile: horizon.HorizonProfile | None = None,
    *,
    refract: bool = True,
    limb: float = 1.0,
) -> SunRiseSet:
    """Sunrise and sunset on local ``day`` in ``tz`` against ``profile`` (or the astronomical horizon).

    SPA's airless topocentric sun at :data:`DAY_STEP_SECONDS` steps from local
    midnight to the next, the limb (``limb`` times the day's semi-diameter)
    against the horizon, crossings interpolated linearly.
    """
    start, _notes = sun.to_utc(_dt.datetime(day.year, day.month, day.day), tz)
    end_day = day + _dt.timedelta(days=1)
    end, _notes = sun.to_utc(_dt.datetime(end_day.year, end_day.month, end_day.day), tz)
    t0, t1 = start.timestamp(), end.timestamp()
    count = math.ceil((t1 - t0) / DAY_STEP_SECONDS)
    times = t0 + (t1 - t0) * np.arange(count + 1, dtype=np.float64) / count
    position = sun.solar_position(times, latitude, longitude, height, refraction=False)
    semi = position.angular_diameter / 2.0
    g = (
        position.elevation_airless
        + float(limb) * semi
        - _horizon_threshold(position.azimuth, profile, refract)
    )
    above = g > 0.0
    up = np.flatnonzero(~above[:-1] & above[1:])
    down = np.flatnonzero(above[:-1] & ~above[1:])
    nan = math.nan
    rise = (nan, nan, nan)
    set_ = (nan, nan, nan)
    for indices, first in ((up, True), (down, False)):
        if not len(indices):
            continue
        i = int(indices[0] if first else indices[-1])
        f = float(g[i] / (g[i] - g[i + 1]))
        when = float(times[i] + f * (times[i + 1] - times[i]))
        az = _interp_azimuth(float(position.azimuth[i]), float(position.azimuth[i + 1]), f)
        value = (when, az, _altitude_at(profile, az))
        if first:
            rise = value
        else:
            set_ = value
    noon = int(np.argmax(position.elevation_airless))
    return SunRiseSet(*rise, *set_, float(position.declination[noon]))


# --- lunar standstills ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Standstill:
    """A lunar standstill near an epoch (Julian days in UT; degrees)."""

    kind: str
    """"major" or "minor"."""
    node_last: float
    """The last time, at or before the epoch, the mean node was at 0° (major) or 180° (minor)."""
    node_next: float
    nearest: float
    """Whichever of the two is nearer the epoch: the standstill used."""
    obliquity: float
    """Mean obliquity then (Laskar)."""
    mean_north: float
    """+(ε + i) for a major standstill, +(ε - i) for a minor one: the node-cycle geometry."""
    mean_south: float
    north: float
    """The ephemeris' extreme of the monthly northern limits within a year of ``nearest``."""
    north_jd: float
    south: float
    south_jd: float
    north_parallax: float
    south_parallax: float


def _peaks(values: np.ndarray, maxima: bool) -> np.ndarray:
    v = values if maxima else -values
    return np.flatnonzero((v[1:-1] > v[:-2]) & (v[1:-1] >= v[2:])) + 1


def standstill(epoch_julian_day: float, kind: str) -> Standstill:
    """The major or minor standstill nearest ``epoch_julian_day``, from the mean node and Meeus.

    The node-cycle geometry gives ±(ε + i) (major) or ±(ε - i) (minor). The
    ephemeris (:func:`.moon.position`, hourly, geocentric apparent
    declination) gives what the Moon actually reaches: within a year of the
    node crossing, the most extreme monthly limit for a major standstill and
    the least extreme for a minor one, each refined by a parabola. These
    include the 173-day wobble of the inclination (about ±0.15°).
    """
    if kind not in {"major", "minor"}:
        raise ValueError("kind is 'major' or 'minor'")
    jde = _jde(float(epoch_julian_day))
    last, nxt = moon.node_crossings(jde, 0.0 if kind == "major" else 180.0)
    dt_days = jde - float(epoch_julian_day)
    last, nxt = last - dt_days, nxt - dt_days  # back to UT, near enough for a date
    nearest = last if abs(float(epoch_julian_day) - last) <= abs(nxt - epoch_julian_day) else nxt
    eps = float(sun.mean_obliquity(_jde(nearest))[0])
    i = moon.INCLINATION
    mean = eps + i if kind == "major" else eps - i
    hours = np.arange(-366 * 24, 366 * 24 + 1, dtype=np.float64)
    jd = nearest + hours / 24.0
    position = moon.position(jd)
    dec = position.geocentric_declination
    found = {}
    for which, maxima in (("north", True), ("south", False)):
        peaks = _peaks(dec, maxima)
        values = dec[peaks]
        if kind == "major":
            pick = int(np.argmax(values) if maxima else np.argmin(values))
        else:
            pick = int(np.argmin(values) if maxima else np.argmax(values))
        k = int(peaks[pick])
        y0, y1, y2 = (float(v) for v in dec[k - 1 : k + 2])
        denominator = y0 - 2.0 * y1 + y2
        shift = 0.5 * (y0 - y2) / denominator if denominator else 0.0
        found[which] = (
            y1 - 0.25 * (y0 - y2) * shift,
            float(jd[k] + shift / 24.0),
            float(position.parallax[k]),
        )
    return Standstill(
        kind=kind,
        node_last=last,
        node_next=nxt,
        nearest=nearest,
        obliquity=eps,
        mean_north=mean,
        mean_south=-mean,
        north=found["north"][0],
        north_jd=found["north"][1],
        south=found["south"][0],
        south_jd=found["south"][1],
        north_parallax=found["north"][2],
        south_parallax=found["south"][2],
    )


# --- the events table -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """One rise or set: where it is on the astronomical horizon and on the real one."""

    name: str
    """E.g. "June solstice sunrise"."""
    body: str
    """"sun" or "moon"."""
    phase: str
    """"rise" or "set"."""
    when: str
    """The epoch or date it is for, as text."""
    declination: float
    azimuth: float
    """On the astronomical horizon (apparent altitude 0), true degrees; NaN if none."""
    horizon_azimuth: float
    """On the horizon profile, true degrees; NaN without a profile or if none."""
    horizon_altitude: float
    """The profile's apparent altitude at ``horizon_azimuth``."""
    time: str = ""
    """Local time on the astronomical horizon (dated events only)."""
    horizon_time: str = ""
    """Local time on the horizon profile (dated events only)."""
    kind: str = "epoch"
    """"epoch" (declination geometry) or "dated" (a calendar day's sun)."""

    @property
    def best_azimuth(self) -> float:
        """The real-horizon azimuth when there is one, else the astronomical one."""
        return self.horizon_azimuth if math.isfinite(self.horizon_azimuth) else self.azimuth

    @property
    def best_altitude(self) -> float:
        return self.horizon_altitude if math.isfinite(self.horizon_azimuth) else 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def _rows(
    names: list[tuple[str, str, float, float, float]],
    when: str,
    latitude: float,
    profile: horizon.HorizonProfile | None,
    refract: bool,
) -> list[Event]:
    """Events for (name, body, declination, limb, parallax) rows; rise then set each."""
    decs = np.array([n[2] for n in names])
    limbs = np.array([n[3] for n in names])
    pars = np.array([n[4] for n in names])
    flat = rise_set_azimuths(decs, latitude, None, refract=refract, limb=limbs, parallax=pars)
    real = (
        rise_set_azimuths(decs, latitude, profile, refract=refract, limb=limbs, parallax=pars)
        if profile is not None
        else [None] * len(names)
    )
    events = []
    for (name, body, dec, _limb, _par), a, b in zip(names, flat, real, strict=True):
        word = {"sun": ("sunrise", "sunset"), "moon": ("moonrise", "moonset")}[body]
        for phase, label, az, hz, alt in (
            (
                "rise",
                word[0],
                a.rise_azimuth,
                b.rise_azimuth if b else math.nan,
                b.rise_altitude if b else math.nan,
            ),
            (
                "set",
                word[1],
                a.set_azimuth,
                b.set_azimuth if b else math.nan,
                b.set_altitude if b else math.nan,
            ),
        ):
            events.append(Event(f"{name} {label}", body, phase, when, dec, az, hz, alt))
    return events


def epoch_events(
    year: int,
    latitude: float,
    profile: horizon.HorizonProfile | None = None,
    *,
    refract: bool = True,
    limb: str = "UPPER",
    lunar: str = "MEAN",
    moon_events: bool = True,
) -> tuple[list[Event], dict]:
    """The solstice, equinox and lunar standstill rises and sets at an epoch.

    ``lunar`` is "MEAN" (±(ε ± i), the node-cycle geometry, with the mean
    parallax) or "EXTREME" (the declinations the ephemeris reaches at the
    standstills nearest the epoch, with the parallax then). Returns the
    events and a summary (obliquity, standstill dates and declinations).
    """
    jd = epoch_jd(year)
    eps = float(sun.mean_obliquity(_jde(jd))[0])
    side = LIMBS[limb]
    s_sun = side * SUN_SEMI_DIAMETER
    when = format_year(year)
    rows = [
        ("June solstice", "sun", eps, s_sun, 0.0),
        ("Equinox", "sun", 0.0, s_sun, 0.0),
        ("December solstice", "sun", -eps, s_sun, 0.0),
    ]
    summary: dict = {
        "year": int(year),
        "epoch": when,
        "obliquity": eps,
        "inclination": moon.INCLINATION,
        "trust": epoch_trust(year),
    }
    if moon_events:
        stands = {k: standstill(jd, k) for k in ("major", "minor")}
        for kind, st in stands.items():
            summary[kind] = {
                "node_last": st.node_last,
                "node_next": st.node_next,
                "node_last_text": format_date(st.node_last),
                "node_next_text": format_date(st.node_next),
                "nearest_text": format_date(st.nearest),
                "mean_north": st.mean_north,
                "north": st.north,
                "north_text": format_date(st.north_jd, time=True),
                "south": st.south,
                "south_text": format_date(st.south_jd, time=True),
            }
            for which in ("north", "south"):
                if lunar == "EXTREME":
                    dec = getattr(st, which)
                    par = getattr(st, f"{which}_parallax")
                else:
                    dec = st.mean_north if which == "north" else st.mean_south
                    par = moon.MEAN_PARALLAX
                semi = math.degrees(
                    math.asin(moon.SEMI_DIAMETER_RATIO * math.sin(math.radians(par)))
                )
                label = f"{kind.capitalize()} standstill {which}"
                rows.append((label, "moon", dec, side * semi, par))
    return _rows(rows, when, latitude, profile, refract), summary


def _local_time(seconds: float, tz: _dt.tzinfo) -> str:
    if not math.isfinite(seconds):
        return ""
    return _dt.datetime.fromtimestamp(seconds, _dt.UTC).astimezone(tz).strftime("%H:%M:%S")


def dated_sun_events(
    days: list[tuple[str, _dt.date]],
    tz: _dt.tzinfo,
    latitude: float,
    longitude: float,
    height: float = 0.0,
    profile: horizon.HorizonProfile | None = None,
    *,
    refract: bool = True,
    limb: str = "UPPER",
) -> list[Event]:
    """Sunrise and sunset on each (label, local day), against 0° and the profile."""
    side = LIMBS[limb]
    events = []
    for label, day in days:
        flat = sun_rise_set(day, tz, latitude, longitude, height, None, refract=refract, limb=side)
        real = (
            sun_rise_set(day, tz, latitude, longitude, height, profile, refract=refract, limb=side)
            if profile is not None
            else None
        )
        for phase, word in (("rise", "sunrise"), ("set", "sunset")):
            az = getattr(flat, f"{phase}_azimuth")
            events.append(
                Event(
                    name=f"{label} {word}",
                    body="sun",
                    phase=phase,
                    when=day.isoformat(),
                    declination=flat.declination,
                    azimuth=az,
                    horizon_azimuth=getattr(real, f"{phase}_azimuth") if real else math.nan,
                    horizon_altitude=getattr(real, f"{phase}_altitude") if real else math.nan,
                    time=_local_time(getattr(flat, f"{phase}_time"), tz),
                    horizon_time=_local_time(getattr(real, f"{phase}_time"), tz) if real else "",
                    kind="dated",
                )
            )
    return events


# --- alignments ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Alignment:
    """One line feature against the events, in both directions along it."""

    index: int
    """The line's index (edge index in its mesh)."""
    azimuth: float
    """True azimuth from its first vertex to its second, degrees."""
    back_azimuth: float
    nearest: tuple[tuple[str, float, float], ...]
    """Up to three (event name, event azimuth, signed difference line - event), nearest first."""
    declination: float
    """The declination (no parallax) on the horizon along ``azimuth``."""
    back_declination: float
    altitude: float
    """Horizon apparent altitude used along ``azimuth`` (0 without a profile)."""
    back_altitude: float


def _signed(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a - b + 180.0) % 360.0 - 180.0


def alignments(
    azimuth: np.ndarray,
    latitude: np.ndarray | float,
    events: list[Event],
    profile: horizon.HorizonProfile | None = None,
    *,
    refract: bool = True,
    count: int = 3,
    indices: np.ndarray | None = None,
) -> list[Alignment]:
    """Each line's azimuth (and back azimuth) against every event's best azimuth.

    ``azimuth`` is true degrees per line; ``latitude`` one value or one per
    line. A line matches an event in either direction along it; the signed
    difference is the line's direction minus the event's azimuth. The
    declination is the standard inversion (:func:`declination`) at the
    horizon altitude along each direction, for the centre of a body without
    parallax (the sun or a star).
    """
    az = np.asarray(azimuth, dtype=np.float64).reshape(-1) % 360.0
    back = (az + 180.0) % 360.0
    lat = np.broadcast_to(np.asarray(latitude, dtype=np.float64), az.shape)
    alt = profile.at(az) if profile is not None else np.zeros_like(az)
    back_alt = profile.at(back) if profile is not None else np.zeros_like(az)
    dec = declination(az, alt, lat, refract=refract)
    back_dec = declination(back, back_alt, lat, refract=refract)
    names = [e.name for e in events]
    targets = np.array([e.best_azimuth for e in events], dtype=np.float64).reshape(1, -1)
    valid = np.isfinite(targets)
    forward = _signed(az[:, None], targets)
    backward = _signed(back[:, None], targets)
    diff = np.where(np.abs(forward) <= np.abs(backward), forward, backward)
    diff = np.where(valid, diff, np.inf)
    order = np.argsort(np.abs(diff), axis=1, kind="stable")[:, :count]
    ids = np.arange(len(az)) if indices is None else np.asarray(indices)
    out = []
    for k in range(len(az)):
        nearest = tuple(
            (names[j], float(targets[0, j]), float(diff[k, j]))
            for j in order[k].tolist()
            if math.isfinite(diff[k, j])
        )
        out.append(
            Alignment(
                index=int(ids[k]),
                azimuth=float(az[k]),
                back_azimuth=float(back[k]),
                nearest=nearest,
                declination=float(dec[k]),
                back_declination=float(back_dec[k]),
                altitude=float(alt[k]),
                back_altitude=float(back_alt[k]),
            )
        )
    return out
