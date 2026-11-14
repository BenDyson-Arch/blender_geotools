"""Reference hours of direct sun for the solar validation, from Skyfield and DE440s.

Writes ``tests/data/validation/solar/sun_hours.json``.

GeoTools' definition (docs/design/m5-analysis.md, "Sun hours"): over the
local days of the period, the time the sun's centre is above the horizon
(apparent elevation > 0 with the SPA refraction formula at 1013.25 hPa and
12 C, or airless elevation > 0 with refraction off), above the far horizon
when one is set, and in front of the surface (cos(incidence) > 0), and with
no occluder in the way. The references here implement that definition with
an independent sun (Skyfield, JPL DE440s, IERS UT1) and independent time-zone
arithmetic (pandas):

- ``open``: open horizontal ground: the time between Skyfield's risings and
  settings of the sun's centre against the airless elevation at which the
  SPA-refracted elevation is 0 (or the far horizon), from
  ``almanac.find_risings`` / ``find_settings`` (single days also list those
  events, as Unix seconds with +1 for a rising and -1 for a setting). Days, date ranges and years,
  in named zones (with daylight-saving days), fixed offsets, the equator,
  polar day and night.
- ``surfaces``: tilted and vertical surfaces: the sun sampled every 2 s
  (bin centres), apparent elevation from Skyfield's airless elevation plus
  the SPA refraction formula, counted while above 0 and in front of the
  surface.
- ``profiles``: a far horizon that varies with azimuth, interpolated linearly
  and periodically between given (true azimuth, elevation) pairs.
- ``wall``: a single instant for the Blender shadow test: Skyfield's airless
  sun at the site of a project in MGA zone 56 near its west edge, with the
  grid bearing of its azimuth measured geodesically (``Geod.fwd`` then the
  projection).

Run:

    .scratch/refenv/bin/python -I scripts/validation/solar/sun_hours.py
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c

UNIX_EPOCH_JD = 2440587.5


def _sky():
    from skyfield.api import Loader, load_file

    eph = load_file(str(c.EPHEMERIS))
    ts = Loader(str(c.EPHEMERIS.parent)).timescale()
    return ts, eph["earth"], eph["sun"]


TS, EARTH, SUN = _sky()


def local_day_bounds(first: str, last: str, zone: str) -> tuple[float, float]:
    """Unix seconds of local midnight starting ``first`` and ending ``last`` (pandas, tz database)."""
    import pandas as pd

    tz = _pandas_tz(zone)
    start = pd.Timestamp(first).tz_localize(tz)
    end = (pd.Timestamp(last) + pd.Timedelta(days=1)).tz_localize(tz)
    return start.timestamp(), end.timestamp()


def _pandas_tz(zone: str):
    if zone.startswith("UTC") and len(zone) > 3:  # "UTC+05:45"
        sign = 1 if zone[3] == "+" else -1
        hours, minutes = zone[4:].split(":")
        return dt.timezone(sign * dt.timedelta(hours=int(hours), minutes=int(minutes)))
    return zone


def _utc(unix: float):
    whole = float(np.floor(unix))
    t = dt.datetime.fromtimestamp(whole, dt.UTC)
    return TS.utc(t.year, t.month, t.day, t.hour, t.minute, t.second + (unix - whole))


def _utc_many(unix: np.ndarray):
    """Skyfield times for many Unix seconds (UTC), vectorised through the seconds field."""
    unix = np.asarray(unix, dtype=np.float64)
    base = float(np.floor(unix.min() / 86400.0) * 86400.0)
    t = dt.datetime.fromtimestamp(base, dt.UTC)
    return TS.utc(t.year, t.month, t.day, 0, 0, unix - base)


def threshold(horizon: float, refraction: bool) -> float:
    """The airless elevation at which the apparent (SPA-refracted) elevation is ``horizon``."""
    if not refraction:
        return float(horizon)
    lo, hi = horizon - 1.0, horizon
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if mid + c.spa_refraction(mid) > horizon:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def open_hours(lat, lon, t0, t1, horizon=0.0, refraction=True) -> tuple[float, list]:
    """Hours the sun's centre is above the threshold between Unix ``t0`` and ``t1``.

    Also the risings (+1) and settings (-1) inside the period, as (Unix, kind).
    """
    from skyfield import almanac
    from skyfield.api import wgs84

    observer = EARTH + wgs84.latlon(lat, lon)
    h = threshold(horizon, refraction)
    start, end = _utc(t0), _utc(t1)
    rise_t, rise_ok = almanac.find_risings(observer, SUN, start, end, horizon_degrees=h)
    set_t, set_ok = almanac.find_settings(observer, SUN, start, end, horizon_degrees=h)
    events = [(float(x), +1) for x, ok in zip(_unix(rise_t), rise_ok, strict=True) if ok]
    events += [(float(x), -1) for x, ok in zip(_unix(set_t), set_ok, strict=True) if ok]
    events = sorted(e for e in events if t0 < e[0] < t1)
    alt, _az, _ = observer.at(start).observe(SUN).apparent().altaz()
    up = alt.degrees > h
    total, last = 0.0, t0
    for when, kind in events:
        if up and kind == -1:
            total += when - last
        up = kind == +1
        last = when
    if up:
        total += t1 - last
    return total / 3600.0, events


def _unix(t) -> np.ndarray:
    # Unix seconds on the UTC scale (what GeoTools' times are).
    return np.array([x.timestamp() for x in np.atleast_1d(t.utc_datetime())])


def sampled(lat, lon, t0, t1, step=2.0):
    """Airless azimuth and elevation at the centres of ``step``-second bins over [t0, t1)."""
    from skyfield.api import wgs84

    observer = EARTH + wgs84.latlon(lat, lon)
    n = round((t1 - t0) / step)
    centres = t0 + step * (np.arange(n) + 0.5)
    alt, az, _ = observer.at(_utc_many(centres)).observe(SUN).apparent().altaz()
    return az.degrees, alt.degrees, step


def surface_hours(lat, lon, t0, t1, tilt, aspect, refraction=True, profile=None) -> float:
    az, e0, step = sampled(lat, lon, t0, t1)
    elevation = (
        e0 + np.where(e0 >= -(0.26667 + 0.5667), c.spa_refraction(e0), 0.0) if refraction else e0
    )
    a, el = np.radians(az), np.radians(elevation)
    direction = np.stack([np.sin(a) * np.cos(el), np.cos(a) * np.cos(el), np.sin(el)])
    t, s = np.radians(tilt), np.radians(aspect)
    normal = np.array([np.sin(t) * np.sin(s), np.sin(t) * np.cos(s), np.cos(t)])
    lit = (elevation > 0.0) & (normal @ direction > 0.0)
    if profile is not None:
        azimuths, elevations = profile
        lit &= elevation > np.interp(az % 360.0, azimuths, elevations, period=360.0)
    return float(lit.sum() * step / 3600.0)


OPEN_CASES = [
    # (name, lat, lon, first, last, zone, refraction, horizon)
    ("equator equinox, airless", 0.0, 0.0, "2026-03-20", "2026-03-20", "UTC", False, 0.0),
    ("equator equinox", 0.0, 0.0, "2026-03-20", "2026-03-20", "UTC", True, 0.0),
    ("51 N June solstice", 51.18, -1.83, "2026-06-21", "2026-06-21", "Europe/London", True, 0.0),
    (
        "51 N December solstice",
        51.18,
        -1.83,
        "2026-12-21",
        "2026-12-21",
        "Europe/London",
        True,
        0.0,
    ),
    (
        "51 N clocks go forward (23 h)",
        51.18,
        -1.83,
        "2026-03-29",
        "2026-03-29",
        "Europe/London",
        True,
        0.0,
    ),
    (
        "51 N clocks go back (25 h)",
        51.18,
        -1.83,
        "2026-10-25",
        "2026-10-25",
        "Europe/London",
        True,
        0.0,
    ),
    (
        "51 N June, horizon 5 deg",
        51.18,
        -1.83,
        "2026-06-21",
        "2026-06-21",
        "Europe/London",
        True,
        5.0,
    ),
    (
        "51 N June, horizon 5 deg, airless",
        51.18,
        -1.83,
        "2026-06-21",
        "2026-06-21",
        "Europe/London",
        False,
        5.0,
    ),
    ("27.5 S June", -27.5, 153.0, "2026-06-21", "2026-06-21", "Australia/Brisbane", True, 0.0),
    ("27.5 S December", -27.5, 153.0, "2026-12-21", "2026-12-21", "Australia/Brisbane", True, 0.0),
    (
        "Sydney DST ends (25 h)",
        -33.87,
        151.21,
        "2026-04-05",
        "2026-04-05",
        "Australia/Sydney",
        True,
        0.0,
    ),
    (
        "Sydney DST starts (23 h)",
        -33.87,
        151.21,
        "2026-10-04",
        "2026-10-04",
        "Australia/Sydney",
        True,
        0.0,
    ),
    (
        "Lord Howe DST ends (24.5 h)",
        -31.55,
        159.08,
        "2026-04-05",
        "2026-04-05",
        "Australia/Lord_Howe",
        True,
        0.0,
    ),
    ("Kathmandu UTC+05:45", 27.7, 85.3, "2026-12-21", "2026-12-21", "Asia/Kathmandu", True, 0.0),
    ("Kathmandu fixed offset", 27.7, 85.3, "2026-12-21", "2026-12-21", "UTC+05:45", True, 0.0),
    (
        "Kiritimati UTC+14",
        1.87,
        -157.4,
        "2026-06-21",
        "2026-06-21",
        "Pacific/Kiritimati",
        True,
        0.0,
    ),
    (
        "Tromso near midnight sun",
        69.65,
        18.96,
        "2026-05-19",
        "2026-05-19",
        "Europe/Oslo",
        True,
        0.0,
    ),
    ("Tromso first polar day", 69.65, 18.96, "2026-05-22", "2026-05-22", "Europe/Oslo", True, 0.0),
    ("Tromso near polar night", 69.65, 18.96, "2026-11-25", "2026-11-25", "Europe/Oslo", True, 0.0),
    ("Tromso polar night", 69.65, 18.96, "2026-12-21", "2026-12-21", "Europe/Oslo", True, 0.0),
    ("80 N polar day", 80.0, 15.0, "2026-06-21", "2026-06-21", "UTC", True, 0.0),
    ("80 N polar night", 80.0, 15.0, "2026-12-21", "2026-12-21", "UTC", True, 0.0),
    ("89 N March equinox", 89.0, 0.0, "2026-03-20", "2026-03-20", "UTC", True, 0.0),
    (
        "78 S December (polar day)",
        -78.0,
        166.0,
        "2026-12-21",
        "2026-12-21",
        "Antarctica/McMurdo",
        True,
        0.0,
    ),
    (
        "78 S June (polar night)",
        -78.0,
        166.0,
        "2026-06-21",
        "2026-06-21",
        "Antarctica/McMurdo",
        True,
        0.0,
    ),
    ("64 N June", 64.1, -21.9, "2026-06-21", "2026-06-21", "Atlantic/Reykjavik", True, 0.0),
    (
        "27.5 S winter season",
        -27.5,
        153.0,
        "2026-06-01",
        "2026-08-31",
        "Australia/Brisbane",
        True,
        0.0,
    ),
    ("51 N year", 51.18, -1.83, "2026-01-01", "2026-12-31", "Europe/London", True, 0.0),
    ("78 N year (Svalbard)", 78.2, 15.6, "2026-01-01", "2026-12-31", "UTC+01:00", True, 0.0),
    ("equator year", 0.0, 30.0, "2026-01-01", "2026-12-31", "UTC+02:00", True, 0.0),
]

SURFACES = [
    # (name, tilt, aspect: true azimuth the surface faces)
    ("horizontal", 0.0, 0.0),
    ("south 30", 30.0, 180.0),
    ("north 30", 30.0, 0.0),
    ("north 60", 60.0, 0.0),
    ("east 45", 45.0, 90.0),
    ("west 45", 45.0, 270.0),
    ("vertical south", 90.0, 180.0),
    ("vertical north", 90.0, 0.0),
    ("vertical east", 90.0, 90.0),
    ("overhang (faces down, 120)", 120.0, 180.0),
]

SURFACE_SITES = [
    ("51 N June", 51.18, -1.83, "2026-06-21", "Europe/London"),
    ("51 N December", 51.18, -1.83, "2026-12-21", "Europe/London"),
    ("51 N March", 51.18, -1.83, "2026-03-20", "Europe/London"),
    ("27.5 S June", -27.5, 153.0, "2026-06-21", "Australia/Brisbane"),
    ("27.5 S December", -27.5, 153.0, "2026-12-21", "Australia/Brisbane"),
    ("equator equinox", 0.0, 0.0, "2026-03-20", "UTC"),
    ("80 N June", 80.0, 15.0, "2026-06-21", "UTC"),
]

PROFILE = ([0.0, 90.0, 180.0, 270.0], [2.0, 12.0, 4.0, 8.0])

PROFILE_SITES = [
    ("51 N June", 51.18, -1.83, "2026-06-21", "Europe/London"),
    ("51 N December", 51.18, -1.83, "2026-12-21", "Europe/London"),
    ("27.5 S June", -27.5, 153.0, "2026-06-21", "Australia/Brisbane"),
]

WALL = {
    "crs": "EPSG:7856",
    "east": 211_000.0,
    "north": 6_680_000.0,
    # One 12-hour step: local day 2026-03-20 at UTC+07:00 runs from 2026-03-19
    # 17:00 UTC; with a 720-minute step its midpoints are 23:00 UTC (09:00 at
    # 150 E, the sun in the north-east) and 11:00 UTC (night).
    "date": "2026-03-20",
    "utc_offset": 7.0,
    "step_minutes": 720,
    "instant": "2026-03-19T23:00:00+00:00",
}


def wall_reference() -> dict:
    from pyproj import CRS, Geod, Transformer
    from skyfield.api import wgs84

    crs = CRS.from_user_input(WALL["crs"])
    geodetic = crs.geodetic_crs
    to_geo = Transformer.from_crs(crs, geodetic, always_xy=True)
    to_grid = Transformer.from_crs(geodetic, crs, always_xy=True)
    ell = geodetic.ellipsoid
    geod = Geod(a=ell.semi_major_metre, rf=ell.inverse_flattening)
    lon, lat = to_geo.transform(WALL["east"], WALL["north"])
    unix = dt.datetime.fromisoformat(WALL["instant"]).timestamp()
    observer = EARTH + wgs84.latlon(lat, lon)
    alt, az, _ = observer.at(_utc(unix)).observe(SUN).apparent().altaz()

    def grid_bearing(azimuth: float) -> float:
        lon1, lat1, _ = geod.fwd(lon, lat, azimuth + 180.0, 0.5)
        lon2, lat2, _ = geod.fwd(lon, lat, azimuth, 0.5)
        e1, n1 = to_grid.transform(lon1, lat1)
        e2, n2 = to_grid.transform(lon2, lat2)
        return float(np.degrees(np.arctan2(e2 - e1, n2 - n1)) % 360.0)

    return {
        **WALL,
        "unix": unix,
        "longitude": lon,
        "latitude": lat,
        "azimuth": float(az.degrees),
        "elevation_airless": float(alt.degrees),
        "grid_bearing": grid_bearing(float(az.degrees)),
        "convergence": -((grid_bearing(0.0) + 180.0) % 360.0 - 180.0),
    }


def main() -> None:
    open_rows = []
    for name, lat, lon, first, last, zone, refraction, horizon in OPEN_CASES:
        t0, t1 = local_day_bounds(first, last, zone)
        hours, events = open_hours(lat, lon, t0, t1, horizon, refraction)
        print(f"{name:40s} {hours:10.5f} h of {(t1 - t0) / 3600:.2f}")
        open_rows.append(
            {
                "name": name,
                "latitude": lat,
                "longitude": lon,
                "first": first,
                "last": last,
                "zone": zone,
                "refraction": refraction,
                "horizon": horizon,
                "period_hours": (t1 - t0) / 3600.0,
                "hours": hours,
                "events": [[round(when, 3), kind] for when, kind in events]
                if first == last
                else [],
            }
        )
    surface_rows = []
    for site, lat, lon, day, zone in SURFACE_SITES:
        t0, t1 = local_day_bounds(day, day, zone)
        for name, tilt, aspect in SURFACES:
            hours = surface_hours(lat, lon, t0, t1, tilt, aspect)
            surface_rows.append(
                {
                    "site": site,
                    "surface": name,
                    "latitude": lat,
                    "longitude": lon,
                    "day": day,
                    "zone": zone,
                    "tilt": tilt,
                    "aspect": aspect,
                    "hours": hours,
                }
            )
        print(site, [round(r["hours"], 3) for r in surface_rows[-len(SURFACES) :]])
    profile_rows = []
    for site, lat, lon, day, zone in PROFILE_SITES:
        t0, t1 = local_day_bounds(day, day, zone)
        for name, tilt, aspect in SURFACES[:3]:
            hours = surface_hours(lat, lon, t0, t1, tilt, aspect, profile=PROFILE)
            profile_rows.append(
                {
                    "site": site,
                    "surface": name,
                    "latitude": lat,
                    "longitude": lon,
                    "day": day,
                    "zone": zone,
                    "tilt": tilt,
                    "aspect": aspect,
                    "hours": hours,
                }
            )
    wall = wall_reference()
    print("wall", wall)
    c.write(
        "sun_hours.json",
        {
            "about": __doc__.split("\n\n")[0],
            "versions": c.versions(),
            "ephemeris": c.EPHEMERIS.name,
            "open": open_rows,
            "surfaces": surface_rows,
            "profile": {"azimuths": PROFILE[0], "elevations": PROFILE[1], "cases": profile_rows},
            "wall": wall,
        },
    )


if __name__ == "__main__":
    main()
