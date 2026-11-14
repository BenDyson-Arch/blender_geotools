"""Reference sunrises, sunsets and moonrises from Skyfield and DE440s (not run in CI).

Writes tests/data/validation/archaeoastronomy/events_reference.json:

* **Dated sunrises and sunsets** at six synthetic sites (64° N to 60° S) on the
  2026 equinoxes and solstices and two other days, against three horizons:
  the astronomical one (apparent altitude 0), a raised one (2°) and a
  sinusoidal skyline, h(A) = 1.5 + sin(3A) degrees. The event is the upper
  limb's true (airless) topocentric altitude crossing h - R(h), with R
  Skyfield's Bennett refraction at 1010 hPa and 10 °C; Skyfield's
  ``find_discrete`` finds the crossing. The astropy azimuth of the Sun at
  the same instant (its own ERFA ephemeris, no JPL) is a third opinion.
* **Moonrises and moonsets** at 51° N and 27.5° S through March 2025 (the
  major standstill): the topocentric DE440s Moon, upper limb, astronomical
  horizon, with the geocentric declination, horizontal parallax and
  semi-diameter at the instant, for ``archaeo.rise_set_azimuths``.

Nothing here imports geotools. Run with the pinned environment (README.md):

    .scratch/refenv/bin/python scripts/validation/archaeoastronomy/events_reference.py \
        --eph .scratch/eph
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path

import numpy as np
from skyfield import searchlib
from skyfield.api import load, load_file, wgs84
from skyfield.earthlib import refraction

OUT = Path(__file__).resolve().parents[3] / "tests/data/validation/archaeoastronomy"
SUN_RADIUS_KM = 695700.0  # IAU 2015 nominal solar radius
MOON_K = 0.2725076  # Moon radius / Earth equatorial radius (IAU)
EARTH_EQ_KM = 6378.1366

SITES = [
    ("64N", 64.0, -21.0),
    ("51N", 51.0, -2.0),
    ("35N", 35.0, 135.0),
    ("EQ", 0.0, 30.0),
    ("34S", -34.0, 151.0),
    ("60S", -60.0, -60.0),
]
DAYS = [
    dt.date(2026, 3, 20),
    dt.date(2026, 6, 21),
    dt.date(2026, 9, 23),
    dt.date(2026, 12, 21),
    dt.date(2026, 2, 3),
    dt.date(2026, 10, 30),
]
HORIZONS = {
    "flat": lambda az: np.zeros_like(az),
    "raised": lambda az: np.full_like(az, 2.0),
    "sine": lambda az: 1.5 + np.sin(np.radians(3.0 * az)),
}


def bennett(h: np.ndarray) -> np.ndarray:
    return refraction(h, temperature_C=10.0, pressure_mbar=1010.0)


def threshold(name: str, az: np.ndarray) -> np.ndarray:
    h = HORIZONS[name](az)
    return h - bennett(h)


def sun_events(ts, eph) -> list[dict]:
    from astropy import units as u
    from astropy.coordinates import AltAz, EarthLocation, get_body
    from astropy.time import Time
    from astropy.utils import iers

    iers.conf.auto_download = False  # the bundled IERS tables (astropy-iers-data) suffice

    earth, sun = eph["earth"], eph["sun"]
    rows = []
    for label, lat, lon in SITES:
        observer = earth + wgs84.latlon(lat, lon)
        hours = round(lon / 15.0)
        tz = dt.timezone(dt.timedelta(hours=hours))
        location = EarthLocation.from_geodetic(lon * u.deg, lat * u.deg, 0 * u.m)
        for day in DAYS:
            start = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
            t0, t1 = ts.from_datetime(start), ts.from_datetime(start + dt.timedelta(days=1))
            for horizon in HORIZONS:

                def up(t, horizon=horizon, observer=observer):
                    alt, az, distance = observer.at(t).observe(sun).apparent().altaz()
                    semi = np.degrees(np.arcsin(SUN_RADIUS_KM / distance.km))
                    return alt.degrees + semi > threshold(horizon, az.degrees)

                up.step_days = 1.0 / 96.0
                times, values = searchlib.find_discrete(t0, t1, up, epsilon=1e-3 / 86400.0)
                rise = [t for t, v in zip(times, values, strict=True) if v]
                sets = [t for t, v in zip(times, values, strict=True) if not v]
                row = {
                    "site": label,
                    "latitude": lat,
                    "longitude": lon,
                    "tz_hours": hours,
                    "day": day.isoformat(),
                    "horizon": horizon,
                }
                for phase, found in (("rise", rise[:1]), ("set", sets[-1:])):
                    if not found:
                        row[phase] = None
                        continue
                    t = found[0]
                    alt, az, distance = observer.at(t).observe(sun).apparent().altaz()
                    _ra, dec, _gd = earth.at(t).observe(sun).apparent().radec(epoch="date")
                    when = Time(t.tt, format="jd", scale="tt")
                    frame = AltAz(obstime=when, location=location, pressure=0 * u.hPa)
                    other = get_body("sun", when, location).transform_to(frame)
                    row[phase] = {
                        "unix": t.utc_datetime().timestamp(),
                        "azimuth": float(az.degrees),
                        "altitude_airless": float(alt.degrees),
                        "declination": float(dec.degrees),
                        "semi_diameter": float(np.degrees(np.arcsin(SUN_RADIUS_KM / distance.km))),
                        "astropy_azimuth": float(other.az.deg),
                    }
                rows.append(row)
    return rows


def moon_events(ts, eph) -> list[dict]:
    earth, moon = eph["earth"], eph["moon"]
    rows = []
    for label, lat, lon in (("51N", 51.0, -2.0), ("27S", -27.5, 133.0)):
        observer = earth + wgs84.latlon(lat, lon)

        def up(t, observer=observer):
            alt, _az, distance = observer.at(t).observe(moon).apparent().altaz()
            semi = np.degrees(np.arcsin(MOON_K * EARTH_EQ_KM / distance.km))
            return alt.degrees + semi > -bennett(0.0)

        up.step_days = 1.0 / 96.0
        t0, t1 = ts.utc(2025, 3, 1), ts.utc(2025, 4, 1)
        times, values = searchlib.find_discrete(t0, t1, up, epsilon=1e-3 / 86400.0)
        for t, value in zip(times, values, strict=True):
            _alt, az, _d = observer.at(t).observe(moon).apparent().altaz()
            _ra, dec, distance = earth.at(t).observe(moon).apparent().radec(epoch="date")
            rows.append(
                {
                    "site": label,
                    "latitude": lat,
                    "longitude": lon,
                    "phase": "rise" if value else "set",
                    "unix": t.utc_datetime().timestamp(),
                    "azimuth": float(az.degrees),
                    "geocentric_declination": float(dec.degrees),
                    "parallax": float(np.degrees(np.arcsin(EARTH_EQ_KM / distance.km))),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eph", type=Path, default=Path(".scratch/eph"))
    args = parser.parse_args()
    import importlib.metadata as md

    ts = load.timescale(builtin=True)
    eph = load_file(str(args.eph / "de440s.bsp"))
    data = {
        "generated_by": "scripts/validation/archaeoastronomy/events_reference.py",
        "versions": {p: md.version(p) for p in ("skyfield", "astropy", "pyerfa", "numpy")},
        "ephemeris": "DE440s",
        "refraction": "Bennett (Skyfield earthlib.refraction), 1010 hPa, 10 C",
        "sun": sun_events(ts, eph),
        "moon": moon_events(ts, eph),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "events_reference.json"
    path.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(path, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
