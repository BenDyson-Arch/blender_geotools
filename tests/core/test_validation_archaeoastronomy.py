"""Validation of the archaeoastronomy maths against independent references.

The reference values are in tests/data/validation/archaeoastronomy/, made by
scripts/validation/archaeoastronomy/ (Skyfield 1.53 with JPL DE440s, JPL
DE441 through jplephem 2.23 and ERFA 2.0.1.5, astropy 7.1.0); see
docs/validation/archaeoastronomy.md for the cases, the tolerances and why.
The closed-form cases need no reference file. The Blender side (the horizon
bake) is tests/blender/test_validation_archaeoastronomy.py.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from functools import cache
from pathlib import Path

import numpy as np
import pytest

from geotools.analysis import archaeo, horizon, moon, sun

DATA = Path(__file__).resolve().parents[1] / "data" / "validation" / "archaeoastronomy"
ARCSEC = 1.0 / 3600.0


@cache
def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


def _wrap(a: np.ndarray | float) -> np.ndarray:
    return (np.asarray(a) + 180.0) % 360.0 - 180.0


# --- the Moon: Meeus ch. 47 against DE440 ---------------------------------------------------------


def test_moon_against_de440() -> None:
    """300 instants 1900-2100: Meeus' truncated ELP against DE440s (Skyfield).

    Meeus quotes 10" in longitude and 4" in latitude for his truncation, so
    declination and right ascension are allowed 6" and 10" at most.
    """
    ref = _load("moon_reference.json")["modern"]
    # The geocentric place needs only TT: give TT as the time and ΔT as 0.
    pos = moon.position(np.array(ref["jd_tt"]), delta_t_seconds=0.0)
    d_dec = (pos.geocentric_declination - np.array(ref["dec"])) / ARCSEC
    d_ra = _wrap(pos.right_ascension - np.array(ref["ra"])) * np.cos(
        np.radians(np.array(ref["dec"]))
    )
    d_ra = d_ra / ARCSEC
    assert np.abs(d_dec).max() < 6.0 and np.sqrt(np.mean(d_dec**2)) < 2.0
    assert np.abs(d_ra).max() < 10.0 and np.sqrt(np.mean(d_ra**2)) < 4.0
    assert np.abs(pos.distance_km - np.array(ref["distance_km"])).max() < 60.0


@pytest.mark.parametrize("site", ["north", "south"])
def test_topocentric_moon_against_de440(site) -> None:
    """Altitude and azimuth (airless, parallax included) at 51.18° N and 27.5° S."""
    ref = _load("moon_reference.json")["modern"]
    where = ref["topocentric"][site]
    alt, az = [], []
    for jd, delta_t in zip(ref["jd_ut1"], ref["delta_t"], strict=True):
        p = moon.position(jd, where["latitude"], where["longitude"], delta_t_seconds=delta_t)
        alt.append(p.elevation_airless[0])
        az.append(p.azimuth[0])
    alt = np.array(alt)
    d_alt = (alt - np.array(where["altitude"])) / ARCSEC
    d_az = _wrap(np.array(az) - np.array(where["azimuth"])) * np.cos(np.radians(alt)) / ARCSEC
    assert np.abs(d_alt).max() < 10.0 and np.sqrt(np.mean(d_alt**2)) < 3.0
    assert np.abs(d_az).max() < 12.0 and np.sqrt(np.mean(d_az**2)) < 4.0


def test_standstills_now_against_de440() -> None:
    """2006 and 2025 major, 2015 and 2034 minor: the extremes within 3"."""
    for ref in _load("moon_reference.json")["modern"]["standstills"]:
        got = archaeo.standstill(ref["north_jd_tt"], ref["kind"])
        assert got.north == pytest.approx(ref["north"], abs=3 * ARCSEC), ref["label"]
        assert got.south == pytest.approx(ref["south"], abs=3 * ARCSEC), ref["label"]
        # The same lunation: UT + ΔT against TT, to the hour.
        assert got.north_jd + 70 / 86400 == pytest.approx(ref["north_jd_tt"], abs=1 / 24)


def test_standstills_long_ago_against_de441() -> None:
    """8000 BCE to 1 CE: DE441 and long-term precession (Vondrák et al. 2011).

    The difference follows the gap between Laskar's (1986) obliquity and
    Vondrák's (5" at 3000 BCE, 42" at 8000 BCE): within 0.002° back to
    2000 BCE, 0.002° at 3000 BCE and 0.015° at 8000 BCE.
    """
    ref = _load("moon_reference.json")
    for row in ref["ancient"]:
        got = archaeo.standstill(archaeo.epoch_jd(row["epoch_year"]), row["kind"])
        for which in ("north", "south"):
            ours, theirs = getattr(got, which), row[which]
            sign = 1.0 if which == "north" else -1.0
            eps_gap = _laskar(row[f"{which}_jd_tt"]) - row[f"vondrak_at_{which}"]
            # Corrected for the obliquity gap, the Moon itself agrees to 3" back to 3000 BCE
            # and 12" at 8000 BCE (Meeus' mean elements drift).
            moon_limit = (3 if row["epoch_year"] >= -2999 else 12) * ARCSEC
            assert ours - sign * eps_gap == pytest.approx(theirs, abs=moon_limit), row["label"]
            limit = 0.002 if row["epoch_year"] >= -2999 else 0.015
            assert ours == pytest.approx(theirs, abs=limit), row["label"]
        # The same lunation: GeoTools' UT plus its ΔT is DE441's TT, to a few hours.
        delta_t = float(sun.delta_t(row["epoch_year"] + 0.5)) / 86400.0
        assert got.north_jd + delta_t == pytest.approx(row["north_jd_tt"], abs=0.25)


def _laskar(jd_tt: float) -> float:
    """GeoTools' mean obliquity, unchecked (DE441 windows reach just past 8000 BCE)."""
    return float(sun._mean_obliquity_jce(np.array([(jd_tt - sun.J2000) / 36525.0]))[0])


def test_obliquity_against_long_term_series() -> None:
    """Laskar (1986) against Vondrák et al. (2011) and IAU 2006 over the epoch range.

    Within 1" of Vondrák 400 BCE to 6000 CE, 6" at 3000 BCE, and growing to
    42" at 8000 BCE and 25" at 11600 CE. IAU 2006 (a polynomial for a few
    centuries) leaves both after about 1000 years.
    """
    ref = _load("moon_reference.json")["obliquity"]
    years = np.array(ref["year"])
    jd = np.array(ref["jd_tt"])
    ours = sun._mean_obliquity_jce((jd - sun.J2000) / 36525.0)
    gap = (ours - np.array(ref["vondrak2011"])) / ARCSEC
    assert np.abs(gap[(years >= -400) & (years <= 6000)]).max() < 1.0
    assert np.abs(gap[(years >= -3000) & (years <= 8000)]).max() < 6.0
    assert np.abs(gap).max() < 45.0
    iau = (ours - np.array(ref["iau2006"])) / ARCSEC
    assert np.abs(iau[(years >= 1000) & (years <= 3000)]).max() < 0.5


def test_solstice_declination_long_ago_against_de441() -> None:
    """The June solstice sun's greatest declination (mean equator of date) is ε."""
    for row in _load("moon_reference.json")["ancient"]:
        eps = _laskar(row["sun_max_mean_jd_tt"])
        # DE441's own ecliptic: the sun's greatest declination is Vondrák's ε to 2"...
        assert row["sun_max_declination_mean"] == pytest.approx(
            row["vondrak_at_sun"], abs=2 * ARCSEC
        )
        # ... and Laskar's within the gap between the two series (42" at 8000 BCE).
        limit = 6 * ARCSEC if row["epoch_year"] >= -2999 else 45 * ARCSEC
        assert eps == pytest.approx(row["sun_max_declination_mean"], abs=limit), row["label"]


# --- rise and set: closed forms ----------------------------------------------------------------------


def _closed_form_azimuth(
    dec: float, lat: float, horizon_alt: float, limb: float, parallax: float
) -> float:
    """Rise azimuth where the limb's true topocentric altitude is h - R(h), in closed form."""
    h_true = horizon_alt - float(horizon.refraction(horizon_alt)) - limb  # the centre, topocentric
    h_geo = h_true + math.degrees(
        math.asin(math.sin(math.radians(parallax)) * math.cos(math.radians(h_true)))
    )
    phi, d, h = (math.radians(v) for v in (lat, dec, h_geo))
    cos_a = (math.sin(d) - math.sin(phi) * math.sin(h)) / (math.cos(phi) * math.cos(h))
    return math.degrees(math.acos(cos_a)) if abs(cos_a) <= 1.0 else math.nan  # never crosses


@pytest.mark.parametrize("lat", [-60.0, -34.0, 0.0, 30.0, 51.18, 64.0])
@pytest.mark.parametrize(
    ("dec", "limb", "parallax"),
    [
        (23.44, archaeo.SUN_SEMI_DIAMETER, 0.0),
        (0.0, archaeo.SUN_SEMI_DIAMETER, 0.0),
        (-24.02, -archaeo.SUN_SEMI_DIAMETER, 0.0),
        (28.72, 0.259, moon.MEAN_PARALLAX),
        (-18.29, 0.0, moon.MEAN_PARALLAX),
    ],
)
@pytest.mark.parametrize("horizon_alt", [0.0, 0.7, 3.5])
def test_rise_azimuth_closed_form(lat, dec, limb, parallax, horizon_alt) -> None:
    """The diurnal-circle trace against the closed form, refraction, limb and parallax on.

    The trace steps 0.01° of hour angle and interpolates linearly, so it is
    allowed 2×10⁻⁴° (a chord's sag over the step is far smaller).
    """
    expected = _closed_form_azimuth(dec, lat, horizon_alt, limb, parallax)
    level = horizon.make_profile(
        horizon.azimuths(1.0),
        horizon.Hits(np.full(360, horizon_alt), np.full(360, horizon_alt), np.ones(360)),
    )
    got = archaeo.rise_set_azimuths(dec, lat, level, limb=limb, parallax=parallax)[0]
    if math.isnan(expected):
        assert math.isnan(got.rise_azimuth)
        return
    assert got.rise_azimuth == pytest.approx(expected, abs=2e-4)
    assert got.set_azimuth == pytest.approx(360.0 - expected, abs=2e-4)


@pytest.mark.parametrize("lat", [0.0, 20.0, 45.0, 60.0])
def test_equinox_sunrise_is_north_of_east(lat) -> None:
    """At δ = 0 refraction and the upper limb put the first gleam north of east.

    cos A = -tan φ tan h₀ with h₀ = -(R(0) + s) = -0.84°: due east on the
    equator, 0.84° north of east at 45° and 1.45° at 60°.
    """
    h0 = -(float(horizon.refraction(0.0)) + archaeo.SUN_SEMI_DIAMETER)
    expected = math.degrees(
        math.acos(
            -math.sin(math.radians(lat))
            * math.sin(math.radians(h0))
            / (math.cos(math.radians(lat)) * math.cos(math.radians(h0)))
        )
    )
    got = archaeo.rise_set_azimuths(0.0, lat, limb=archaeo.SUN_SEMI_DIAMETER)[0]
    assert got.rise_azimuth == pytest.approx(expected, abs=2e-4)
    assert 90.0 - got.rise_azimuth == pytest.approx(
        0.0
        if lat == 0
        else math.degrees(math.atan(math.tan(math.radians(lat)) * math.radians(-h0))),
        abs=0.005,
    )


def test_solstice_amplitude_grows_with_latitude() -> None:
    """sin(amplitude) = sin ε / cos φ on an airless flat horizon, to the pole circle."""
    eps = float(sun.mean_obliquity(sun.J2000)[0])
    for lat in np.arange(-66.0, 66.5, 3.0):
        got = archaeo.rise_set_azimuths(eps, float(lat), refract=False)[0]
        amplitude = math.degrees(
            math.asin(math.sin(math.radians(eps)) / math.cos(math.radians(lat)))
        )
        assert 90.0 - got.rise_azimuth == pytest.approx(amplitude, abs=1e-4), lat
    # Beyond the polar circle the June sun neither rises nor sets.
    assert math.isnan(archaeo.rise_set_azimuths(eps, 67.0, refract=False)[0].rise_azimuth)


# --- rise and set: Skyfield and DE440 --------------------------------------------------------------


def _sine_profile(name: str) -> horizon.HorizonProfile | None:
    if name == "flat":
        return None
    az = horizon.azimuths(0.1)
    h = np.full_like(az, 2.0) if name == "raised" else 1.5 + np.sin(np.radians(3.0 * az))
    return horizon.make_profile(az, horizon.Hits(h, h, np.ones_like(az)))


def test_dated_sun_events_against_skyfield() -> None:
    """216 sunrises and sunsets, 64° N to 60° S, flat, raised and sinusoidal skylines.

    Upper limb, Bennett refraction at h. GeoTools traces SPA every 20 s;
    Skyfield (DE440s) root-finds to 1 ms. Allowed 0.005° and 2 s.
    """
    ref = _load("events_reference.json")
    profiles = {name: _sine_profile(name) for name in ("flat", "raised", "sine")}
    d_az, d_time, d_astropy = [], [], []
    for row in ref["sun"]:
        tz = dt.timezone(dt.timedelta(hours=row["tz_hours"]))
        got = archaeo.sun_rise_set(
            dt.date.fromisoformat(row["day"]),
            tz,
            row["latitude"],
            row["longitude"],
            0.0,
            profiles[row["horizon"]],
        )
        for phase in ("rise", "set"):
            theirs = row[phase]
            ours = getattr(got, f"{phase}_azimuth")
            if theirs is None:
                assert math.isnan(ours), (row["site"], row["day"], phase)
                continue
            d_az.append(float(_wrap(ours - theirs["azimuth"])))
            d_time.append(getattr(got, f"{phase}_time") - theirs["unix"])
            d_astropy.append(float(_wrap(theirs["astropy_azimuth"] - theirs["azimuth"])))
    assert len(d_az) > 200
    assert np.abs(d_az).max() < 0.005
    assert np.abs(d_time).max() < 2.0
    # astropy (ERFA's own solar ephemeris) agrees with Skyfield to a few arc seconds.
    assert np.abs(d_astropy).max() < 5 * ARCSEC


def test_moonrise_azimuth_against_skyfield() -> None:
    """119 moonrises and moonsets in March 2025 (major standstill) at 51° N and 27.5° S.

    GeoTools' geometry with the declination and parallax at the instant
    against DE440s' topocentric Moon: within 0.01°. Without the parallax the
    azimuths would be up to 1.9° out.
    """
    errors = []
    for row in _load("events_reference.json")["moon"]:
        par = row["parallax"]
        semi = math.degrees(math.asin(moon.SEMI_DIAMETER_RATIO * math.sin(math.radians(par))))
        got = archaeo.rise_set_azimuths(
            row["geocentric_declination"], row["latitude"], limb=semi, parallax=par
        )[0]
        ours = got.rise_azimuth if row["phase"] == "rise" else got.set_azimuth
        errors.append(float(_wrap(ours - row["azimuth"])))
    assert len(errors) > 100
    assert np.abs(errors).max() < 0.01


def test_declination_inversion_against_skyfield() -> None:
    """Check Alignment's declination from each Skyfield sunrise's azimuth and horizon.

    With the sun's parallax (8.8") it gives DE440's declination to 3";
    GeoTools leaves that parallax out for the sun, so 12".
    """
    with_parallax, without = [], []
    for row in _load("events_reference.json")["sun"]:
        for phase in ("rise", "set"):
            e = row[phase]
            if e is None:
                continue
            if row["horizon"] == "flat":
                alt = 0.0
            elif row["horizon"] == "raised":
                alt = 2.0
            else:
                alt = 1.5 + math.sin(math.radians(3.0 * e["azimuth"]))
            args = (e["azimuth"], alt, row["latitude"])
            limb = e["semi_diameter"]
            with_parallax.append(
                float(archaeo.declination(*args, limb=limb, parallax=8.794 * ARCSEC))
                - e["declination"]
            )
            without.append(float(archaeo.declination(*args, limb=limb)) - e["declination"])
    assert np.abs(with_parallax).max() < 3 * ARCSEC
    assert np.abs(without).max() < 12 * ARCSEC


# --- refraction and the horizon --------------------------------------------------------------------


def test_bennett_against_saemundsson() -> None:
    """Bennett (apparent to true) and Saemundsson (true to apparent) agree to 4" above 0°."""
    h = np.linspace(0.0, 30.0, 601)
    r_b = horizon.refraction(h)
    h_true = h - r_b
    r_s = 1.02 / np.tan(np.radians(h_true + 10.3 / (h_true + 5.11))) / 60.0
    assert np.abs(r_s - r_b).max() < 4 * ARCSEC
    assert float(horizon.refraction(0.0)) * 60 == pytest.approx(34.478, abs=0.001)


def test_curvature_against_spherical_geometry() -> None:
    """The small-angle model against exact geometry on a sphere of radius R / (1 - k).

    Horizontal distances are arcs at the observer's height (the scene's
    ground metres). Within 0.0015° for skylines up to 5° and 4 km above
    the eye out to 200 km, 0.003° to 10°.
    """
    for k in (0.0, horizon.TERRESTRIAL_K):
        radius = horizon.EARTH_RADIUS / (1.0 - k)
        worst = {5.0: 0.0, 10.0: 0.0}
        for d in np.geomspace(100.0, 200_000.0, 40):
            for rise in np.linspace(-500.0, 4000.0, 46):
                for h0 in (0.0, 1000.0, 3000.0):
                    el = math.degrees(math.atan2(rise, d))
                    theta = d / (radius + h0)
                    far = radius + h0 + rise
                    exact = math.degrees(
                        math.atan2(far * math.cos(theta) - (radius + h0), far * math.sin(theta))
                    )
                    got = horizon.best_hits(
                        np.array([el]), np.array([[True]]), np.array([[math.hypot(d, rise)]]), k=k
                    ).apparent[0]
                    for limit, value in worst.items():
                        if abs(el) <= limit:
                            worst[limit] = max(value, abs(float(got) - exact))
        assert worst[5.0] < 0.0015 and worst[10.0] < 0.003, worst


@pytest.mark.parametrize("eye", [1.6, 20.0, 200.0])
def test_dip_of_the_horizon_on_a_plain(eye) -> None:
    """A level plain to 60 km: the two-pass search finds the dip, √(2 e (1 - k) / R).

    The first hits are in closed form (as Cycles would give them); the dip is
    the classical formula for the sea horizon, which the model reproduces as
    the largest of -atan(e / d) - (1 - k) d / 2R.
    """

    def cast(elevation: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t = np.tan(np.radians(elevation))
        reach = np.where(t < 0, -eye / np.where(t < 0, t, -1.0), np.inf)
        hit = reach <= 60_000.0
        return hit, np.where(hit, reach / np.cos(np.radians(elevation)), np.nan)

    coarse = horizon.coarse_elevations()
    hit, distance = cast(coarse)
    hits = horizon.best_hits(coarse, hit[None], distance[None])
    rows, base = horizon.fine_brackets(coarse, hit[None], distance[None])
    fine = horizon.fine_elevations(base)
    f_hit, f_distance = cast(fine)
    hits = horizon.merge_rows(hits, rows, horizon.best_hits(fine, f_hit, f_distance))
    k, radius = horizon.TERRESTRIAL_K, horizon.EARTH_RADIUS
    dip = math.degrees(math.sqrt(2.0 * eye * (1.0 - k) / radius))
    reach = math.sqrt(2.0 * eye * radius / (1.0 - k))
    if reach > 60_000.0:  # beyond the plain: its edge is the skyline
        dip = math.degrees(math.atan(eye / 60_000.0)) + float(horizon.curvature_drop(60_000.0))
    assert -float(hits.apparent[0]) == pytest.approx(dip, abs=0.002)


# --- alignments: true azimuth of a line ------------------------------------------------------------


def test_line_bearing_against_geodesic_azimuth() -> None:
    """Check Alignment's true azimuth of an edge in a projected CRS against the geodesic.

    Lines 100 m to 30 km long at the west edge of MGA zone 56 (convergence
    1.5°), every 15°: the grid bearing plus the convergence at the middle is
    the geodesic's azimuth there (pyproj.Geod, GeographicLib's algorithm) to
    0.2" — the arc-to-chord correction GeoTools leaves out is that small.
    """
    pyproj = pytest.importorskip("pyproj")
    from geotools.core.placement import Placement, PlacementMode

    geod = pyproj.Geod(ellps="GRS80")
    to_grid = pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True)
    lon, lat = 150.0, -30.0
    e0, n0 = to_grid.transform(lon, lat)
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:7856", source_offset=(e0, n0, 0.0)
    )
    from geotools.analysis import site

    for length in (100.0, 5000.0, 30_000.0):
        for azimuth in np.arange(0.0, 360.0, 15.0):
            lon2, lat2, _ = geod.fwd(lon, lat, azimuth, length)
            e1, n1 = to_grid.transform(lon2, lat2)
            bearing, _lat = site.line_bearings(placement, [(0, 0, 0)], [(e1 - e0, n1 - n0, 0)])
            mid_lon, mid_lat, _ = geod.fwd(lon, lat, azimuth, length / 2)
            geodesic = geod.inv(mid_lon, mid_lat, lon2, lat2)[0]
            assert abs(float(_wrap(bearing[0] - geodesic))) < 0.2 * ARCSEC, (length, azimuth)
