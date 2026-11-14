"""Reference sun positions, Delta T and meridian convergence for the solar validation.

Writes ``tests/data/validation/solar/sun_position.json`` with:

- ``spa``: random sites and moments from -1999 to 5999 through pvlib's own
  NREL SPA (``pvlib.spa.solar_position``, numpy), with random pressure,
  temperature and height and a given Delta T, refraction included. GeoTools
  must reproduce the same algorithm to its round-off.
- ``horizon``: the same near sunrise and sunset (airless elevation -1.5 to 5
  degrees), where the refraction formula and its switch matter.
- ``skyfield``: random sites and moments 1850-2149 through Skyfield with the
  JPL DE440s ephemeris (geometric, airless), with the same UT1 and Delta T
  given to both, so only the ephemerides and reduction models differ.
- ``skyfield_utc``: moments 1973-2025 in UTC, Skyfield with its own IERS
  UT1 and Delta T; GeoTools takes UT1 = UTC and its Delta T polynomial. This
  is the end-to-end error a user sees.
- ``delta_t``: pvlib's independent implementation of the Espenak and Meeus
  (2006) polynomials, at decimal years ``year + (month - 0.5) / 12``.
- ``convergence``: points in several projected CRSs with the convergence
  measured geodesically: the grid bearing of a 1 m geodesic step due north
  (pyproj ``Geod.fwd`` then the projection), which does not use PROJ's
  ``get_factors``. ``directions`` holds, per point, true azimuths and the
  grid bearing of a 1 m geodesic step along each.

Run (see docs/validation/solar.md for the environment):

    .scratch/refenv/bin/python -I scripts/validation/solar/sun_position.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c

RNG = np.random.default_rng(20261114)
UNIX_EPOCH_JD = 2440587.5


def unix_of_year(year: np.ndarray) -> np.ndarray:
    """Whole Unix seconds of a decimal year (Julian years from J2000).

    Whole seconds, and the other inputs rounded below, so the JSON holds the
    inputs exactly (it keeps 12 significant digits).
    """
    seconds = (np.asarray(year, dtype=np.float64) - 2000.0) * 365.25 * 86400.0 + 946_728_000.0
    return np.round(seconds)


def angles(values: np.ndarray) -> np.ndarray:
    return np.round(values, 7)


def pvlib_spa(unix, lat, lon, height, pressure, temp, delta_t, atmos_refract=0.5667):
    from pvlib import spa

    out = spa.solar_position(
        np.atleast_1d(unix).astype(np.float64),
        lat,
        lon,
        height,
        pressure,
        temp,
        np.atleast_1d(delta_t).astype(np.float64),
        atmos_refract,
        numthreads=1,
        sst=False,
        esd=False,
    )
    _app_zen, _zen, app_el, el, az, _eot = out
    return az, el, app_el


def decimal_month(unix: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    year = 2000.0 + (np.asarray(unix) - 946_728_000.0) / (365.25 * 86400.0)
    whole = np.floor(year)
    month = np.floor((year - whole) * 12.0) + 1.0
    return whole, month


def pvlib_delta_t(unix: np.ndarray) -> np.ndarray:
    from pvlib import spa

    years, months = decimal_month(unix)
    values = [spa.calculate_deltat(y, m) for y, m in zip(years, months, strict=True)]
    return np.round(np.array(values, dtype=np.float64), 4)


def spa_cases(n: int) -> dict:
    year = RNG.uniform(-1999.0, 5999.0, n)
    unix = unix_of_year(year)
    lat = angles(RNG.uniform(-89.9, 89.9, n))
    lat[:6] = [-89.9, -66.56, 0.0, 23.44, 66.56, 89.9]
    lon = angles(RNG.uniform(-180.0, 180.0, n))
    lon[6:9] = [-180.0, 180.0, 0.0]
    height = np.round(RNG.uniform(0.0, 4000.0, n), 2)
    pressure = np.round(RNG.uniform(600.0, 1050.0, n), 2)
    temp = np.round(RNG.uniform(-30.0, 40.0, n), 2)
    dt = pvlib_delta_t(unix)
    az, el, app = [], [], []
    for i in range(n):
        a, e, p = pvlib_spa(unix[i], lat[i], lon[i], height[i], pressure[i], temp[i], dt[i])
        az.append(a[0])
        el.append(e[0])
        app.append(p[0])
    return {
        "unix": unix,
        "latitude": lat,
        "longitude": lon,
        "height": height,
        "pressure_hpa": pressure,
        "temperature_c": temp,
        "delta_t": dt,
        "azimuth": az,
        "elevation_airless": el,
        "elevation": app,
    }


def horizon_cases(n: int) -> dict:
    """Moments with the airless sun between -1.5 and 5 degrees (standard air)."""
    keep: dict[str, list] = {k: [] for k in ("unix", "latitude", "longitude", "delta_t")}
    while len(keep["unix"]) < n:
        m = 4000
        unix = unix_of_year(RNG.uniform(1900.0, 2100.0, m))
        lat = angles(RNG.uniform(-70.0, 70.0, m))
        lon = angles(RNG.uniform(-180.0, 180.0, m))
        for i in range(m):
            a, e, p = pvlib_spa(
                unix[i], lat[i], lon[i], 0.0, c.SPA_PRESSURE, c.SPA_TEMPERATURE, 69.0
            )
            if -1.5 < e[0] < 5.0:
                keep["unix"].append(unix[i])
                keep["latitude"].append(lat[i])
                keep["longitude"].append(lon[i])
                if len(keep["unix"]) >= n:
                    break
    unix = np.array(keep["unix"])
    dt = pvlib_delta_t(unix)
    az, el, app = [], [], []
    for i in range(n):
        a, e, p = pvlib_spa(
            unix[i],
            keep["latitude"][i],
            keep["longitude"][i],
            0.0,
            c.SPA_PRESSURE,
            c.SPA_TEMPERATURE,
            dt[i],
        )
        az.append(a[0])
        el.append(e[0])
        app.append(p[0])
    return {
        "unix": unix,
        "latitude": keep["latitude"],
        "longitude": keep["longitude"],
        "delta_t": dt,
        "azimuth": az,
        "elevation_airless": el,
        "elevation": app,
    }


def _skyfield():
    from skyfield.api import Loader, load_file

    eph = load_file(str(c.EPHEMERIS))
    return Loader(str(c.EPHEMERIS.parent)), eph


def skyfield_cases(n: int) -> dict:
    """Airless topocentric sun, DE440s, with the UT1 and Delta T given (pvlib's Delta T)."""
    from skyfield.api import wgs84

    loader, eph = _skyfield()
    earth, sun = eph["earth"], eph["sun"]
    unix = unix_of_year(RNG.uniform(1850.0, 2149.0, n))
    lat = angles(RNG.uniform(-89.9, 89.9, n))
    lon = angles(RNG.uniform(-180.0, 180.0, n))
    height = np.round(RNG.uniform(0.0, 3000.0, n), 2)
    dt = pvlib_delta_t(unix)
    az, el = [], []
    for i in range(n):
        ts = loader.timescale(delta_t=float(dt[i]))
        t = ts.ut1_jd(unix[i] / 86400.0 + UNIX_EPOCH_JD)
        where = earth + wgs84.latlon(lat[i], lon[i], elevation_m=height[i])
        alt, azm, _ = where.at(t).observe(sun).apparent().altaz()
        el.append(alt.degrees)
        az.append(azm.degrees)
    return {
        "unix": unix,
        "latitude": lat,
        "longitude": lon,
        "height": height,
        "delta_t": dt,
        "azimuth": az,
        "elevation_airless": el,
    }


def skyfield_utc_cases(n: int) -> dict:
    """Airless sun at UTC moments 1973-2025 with Skyfield's own UT1 - UTC and Delta T."""
    from skyfield.api import wgs84

    loader, eph = _skyfield()
    earth, sun = eph["earth"], eph["sun"]
    ts = loader.timescale()
    unix = unix_of_year(RNG.uniform(1973.0, 2025.9, n))
    lat = angles(RNG.uniform(-89.9, 89.9, n))
    lon = angles(RNG.uniform(-180.0, 180.0, n))
    import datetime as dt

    t = ts.from_datetimes([dt.datetime.fromtimestamp(u, dt.UTC) for u in unix])
    az, el = [], []
    for i in range(n):
        where = earth + wgs84.latlon(lat[i], lon[i])
        alt, azm, _ = where.at(t[i]).observe(sun).apparent().altaz()
        el.append(alt.degrees)
        az.append(azm.degrees)
    return {
        "unix": unix,
        "latitude": lat,
        "longitude": lon,
        "skyfield_delta_t": t.delta_t,
        "ut1_minus_utc": t.dut1,
        "azimuth": az,
        "elevation_airless": el,
    }


def delta_t_cases() -> dict:
    from pvlib import spa

    years = list(range(-1999, 3001, 37)) + [
        -500,
        -499,
        499,
        500,
        1599,
        1600,
        1699,
        1700,
        1799,
        1800,
        1859,
        1860,
        1899,
        1900,
        1919,
        1920,
        1940,
        1941,
        1960,
        1961,
        1985,
        1986,
        2004,
        2005,
        2026,
        2049,
        2050,
        2149,
        2150,
        2500,
    ]
    rows = []
    for y in sorted(set(years)):
        for m in (1, 7):
            rows.append((y + (m - 0.5) / 12.0, float(spa.calculate_deltat(y, m))))
    return {"decimal_year": [r[0] for r in rows], "delta_t": [r[1] for r in rows]}


CONVERGENCE_POINTS = [
    # (CRS, easting, northing, what)
    ("EPSG:7856", 211_000.0, 6_680_000.0, "MGA zone 56, near its west edge (150 E, 30 S)"),
    ("EPSG:7856", 500_000.0, 6_960_000.0, "MGA zone 56, central meridian (153 E)"),
    ("EPSG:7856", 790_000.0, 6_680_000.0, "MGA zone 56, east edge"),
    ("EPSG:7855", 300_000.0, 5_800_000.0, "MGA zone 55, 38 S"),
    ("EPSG:32633", 250_000.0, 6_600_000.0, "UTM 33N, 59 N, west of the meridian"),
    ("EPSG:32633", 720_000.0, 5_400_000.0, "UTM 33N, 48 N, east of the meridian"),
    ("EPSG:32733", 720_000.0, 7_000_000.0, "UTM 33S, east of the meridian"),
    ("EPSG:32633", 450_000.0, 8_800_000.0, "UTM 33N, 79 N (Svalbard)"),
    ("EPSG:2193", 1_600_000.0, 5_100_000.0, "NZTM2000, 44 S, wide single zone"),
    ("EPSG:2193", 2_050_000.0, 5_950_000.0, "NZTM2000, far east"),
    ("EPSG:27700", 150_000.0, 50_000.0, "British National Grid, far south-west"),
    ("EPSG:27700", 650_000.0, 300_000.0, "British National Grid, east"),
    ("EPSG:3031", 1_000_000.0, 1_500_000.0, "Antarctic polar stereographic"),
    ("EPSG:3413", -1_500_000.0, -800_000.0, "Arctic polar stereographic (NSIDC)"),
    ("EPSG:3857", 1_000_000.0, 6_000_000.0, "Web Mercator (no convergence)"),
    ("EPSG:3112", -1_500_000.0, -3_500_000.0, "GDA94 / Geoscience Australia Lambert"),
    ("EPSG:3577", -1_800_000.0, -3_900_000.0, "Australian Albers (not conformal)"),
]

AZIMUTHS = [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]


def convergence_cases() -> dict:
    from pyproj import CRS, Geod, Transformer

    rows = []
    for code, east, north, what in CONVERGENCE_POINTS:
        crs = CRS.from_user_input(code)
        geodetic = crs.geodetic_crs
        to_geo = Transformer.from_crs(crs, geodetic, always_xy=True)
        to_grid = Transformer.from_crs(geodetic, crs, always_xy=True)
        ellipsoid = geodetic.ellipsoid
        geod = Geod(a=ellipsoid.semi_major_metre, rf=ellipsoid.inverse_flattening)
        lon, lat = to_geo.transform(east, north)
        bearings = []
        for azimuth in [0.0, *AZIMUTHS]:
            # Central difference: 0.5 m back and 0.5 m forward along the geodesic.
            lon1, lat1, _ = geod.fwd(lon, lat, azimuth + 180.0, 0.5)
            lon2, lat2, _ = geod.fwd(lon, lat, azimuth, 0.5)
            e1, n1 = to_grid.transform(lon1, lat1)
            e2, n2 = to_grid.transform(lon2, lat2)
            bearings.append(float(np.degrees(np.arctan2(e2 - e1, n2 - n1))))
        convergence = -bearings[0]
        rows.append(
            {
                "crs": code,
                "what": what,
                "east": east,
                "north": north,
                "longitude": lon,
                "latitude": lat,
                "convergence": (convergence + 180.0) % 360.0 - 180.0,
                "azimuths": AZIMUTHS,
                "grid_bearings": [b % 360.0 for b in bearings[1:]],
            }
        )
    return {"points": rows}


def main() -> None:
    data = {
        "about": __doc__.split("\n\n")[0],
        "versions": c.versions(),
        "ephemeris": c.EPHEMERIS.name,
        "spa": spa_cases(240),
        "horizon": horizon_cases(60),
        "skyfield": skyfield_cases(240),
        "skyfield_utc": skyfield_utc_cases(120),
        "delta_t": delta_t_cases(),
        "convergence": convergence_cases(),
    }
    c.write("sun_position.json", data)


if __name__ == "__main__":
    main()
