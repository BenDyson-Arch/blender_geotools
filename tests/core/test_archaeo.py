"""Archaeoastronomy core: the Moon (Meeus ch. 47), obliquity, refraction, horizon and events."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from geotools.analysis import archaeo, horizon, moon, sun

# --- the Moon ------------------------------------------------------------------------------------


def test_meeus_example_47a() -> None:
    """Meeus, Astronomical Algorithms (2nd ed.), example 47.a: 1992 April 12, 0h TD."""
    jde = 2448724.5
    geo = moon.geocentric(jde)
    sigma_l, sigma_b, sigma_r = (float(v[0]) for v in geo.sums)
    assert round(sigma_l) == -1127527
    assert round(sigma_b) == -3229126
    assert round(sigma_r) == -16590875
    assert geo.longitude[0] == pytest.approx(133.162655, abs=1e-6)
    assert geo.latitude[0] == pytest.approx(-3.229126, abs=1e-6)
    assert geo.distance_km[0] == pytest.approx(368409.7, abs=0.05)
    assert geo.parallax[0] == pytest.approx(0.991990, abs=1e-6)
    # Apparent place: Meeus' ε 23.440636 and α 134.688470, δ 13.768368 (his nutation is
    # the same IAU 1980 series, so the difference is in the last digit).
    apparent = moon.position(jde, delta_t_seconds=0.0)
    assert apparent.obliquity[0] == pytest.approx(23.440636, abs=2e-6)
    assert apparent.longitude[0] == pytest.approx(133.167265, abs=2e-6)
    assert apparent.right_ascension[0] == pytest.approx(134.688470, abs=3e-6)
    assert apparent.geocentric_declination[0] == pytest.approx(13.768368, abs=3e-6)


def test_topocentric_parallax_lowers_the_moon_by_about_a_degree() -> None:
    jd = 2461041.5 + np.arange(0, 2, 1 / 1440)  # two days at one-minute steps
    topo = moon.position(jd, 51.0, -1.8)
    drop = topo.elevation_geocentric - topo.elevation_airless
    # sin p = rho sin(pi) cos(h'): at the horizon the drop is the parallax itself.
    near_horizon = np.abs(topo.elevation_airless) < 0.5
    assert near_horizon.any()
    assert np.abs(drop[near_horizon] - topo.parallax[near_horizon]).max() < 0.01
    assert drop[near_horizon].min() > 0.9
    # And it shrinks as cos(h') higher up.
    high = topo.elevation_airless > 20.0
    expected = np.degrees(
        np.arcsin(np.sin(np.radians(topo.parallax)) * np.cos(np.radians(topo.elevation_airless)))
    )
    assert np.abs(drop[high] - expected[high]).max() < 0.01


def test_mean_node_and_standstill_dates() -> None:
    # The major standstills of 2006 and 2024-25 and the minor one of 2015.
    last, nxt = moon.node_crossings(archaeo.julian_day_of(2016, 1, 1), 0.0)
    for jde in (last, nxt):
        assert float(moon.mean_node(jde)[0]) == pytest.approx(0.0, abs=1e-8) or float(
            moon.mean_node(jde)[0]
        ) == pytest.approx(360.0, abs=1e-8)
    assert archaeo.calendar_date(last)[:2] == (2006, 6)
    assert archaeo.calendar_date(nxt)[:2] == (2025, 1)
    assert nxt - last == pytest.approx(moon.NODE_PERIOD_DAYS, abs=20)
    last, _nxt = moon.node_crossings(archaeo.julian_day_of(2016, 1, 1), 180.0)
    assert archaeo.calendar_date(last)[:2] == (2015, 10)


def test_standstill_extremes() -> None:
    major = archaeo.standstill(archaeo.epoch_jd(2025), "major")
    assert archaeo.calendar_date(major.nearest)[0] == 2025
    assert major.mean_north == pytest.approx(major.obliquity + moon.INCLINATION)
    # The ephemeris reaches the mean geometry plus most of the inclination's wobble.
    assert 0.05 < major.north - major.mean_north < 0.2
    assert 0.05 < -major.south - major.mean_north < 0.2
    assert abs(major.north_jd - major.nearest) < 366
    minor = archaeo.standstill(archaeo.epoch_jd(2034), "minor")
    assert 0.05 < minor.mean_north - minor.north < 0.2
    assert 0.05 < minor.mean_north + minor.south < 0.2


# --- obliquity -------------------------------------------------------------------------------------


def _iau2006_obliquity(jde: float) -> float:
    """IAU 2006 (Capitaine et al. 2003) mean obliquity: an independent polynomial."""
    t = (jde - sun.J2000) / 36525.0
    arcsec = (
        84381.406
        - 46.836769 * t
        - 0.0001831 * t**2
        + 0.00200340 * t**3
        - 0.000000576 * t**4
        - 0.0000000434 * t**5
    )
    return arcsec / 3600.0


def test_obliquity_is_laskar() -> None:
    assert float(sun.mean_obliquity(sun.J2000)[0]) == pytest.approx(
        23 + 26 / 60 + 21.448 / 3600, abs=1e-12
    )
    # Laskar's polynomial (Meeus eq. 22.3) written out independently at 3000 BCE.
    jde = archaeo.julian_day_of(-2999, 7, 2.5)
    u = (jde - sun.J2000) / 3652500.0
    coefficients = (
        -4680.93,
        -1.55,
        1999.25,
        -51.38,
        -249.67,
        -39.05,
        7.12,
        27.87,
        5.79,
        2.45,
    )
    arcsec = 84381.448 + sum(c * u ** (i + 1) for i, c in enumerate(coefficients))
    ancient = float(sun.mean_obliquity(jde)[0])
    assert ancient == pytest.approx(arcsec / 3600.0, abs=1e-10)
    assert ancient == pytest.approx(24.0209, abs=5e-5)
    # The solstice sun then stood 0.58° further north than now.
    assert ancient - float(sun.mean_obliquity(sun.J2000)[0]) == pytest.approx(0.582, abs=0.001)
    # IAU 2006 agrees to 0.002° at 3000 BCE and to 0.5" over 1000 to 3000 CE.
    assert ancient == pytest.approx(_iau2006_obliquity(jde), abs=0.002)
    for year in range(1000, 3001, 250):
        jde = archaeo.julian_day_of(year, 1, 1.5)
        assert float(sun.mean_obliquity(jde)[0]) == pytest.approx(
            _iau2006_obliquity(jde), abs=0.5 / 3600
        )


def test_events_at_the_ends_of_the_epoch_range() -> None:
    """The standstill nearest an epoch at either end can lie just beyond Laskar's range."""
    for year in (
        archaeo.EPOCH_MIN,
        archaeo.EPOCH_MIN + 1,
        archaeo.EPOCH_MAX - 5,
        archaeo.EPOCH_MAX,
    ):
        events, summary = archaeo.epoch_events(year, 51.0)
        assert len(events) == 14, year
        assert all(math.isfinite(e.azimuth) for e in events), year
        assert summary["major"]["mean_north"] == pytest.approx(
            summary["obliquity"] + moon.INCLINATION, abs=0.01
        )


def test_epoch_limits() -> None:
    with pytest.raises(ValueError, match="10,000 years"):
        archaeo.epoch_jd(-8000)
    with pytest.raises(ValueError, match="10,000 years"):
        sun.mean_obliquity(archaeo.julian_day_of(-8100, 1, 1))
    assert archaeo.format_year(-2999) == "3000 BCE"
    assert archaeo.format_year(0) == "1 BCE"
    assert archaeo.format_year(2026) == "2026 CE"
    notes = " ".join(archaeo.epoch_trust(-2999))
    assert "weeks" in notes and "ΔT" in notes


# --- calendar ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("date", "jd"),
    [
        ((2000, 1, 1.5), 2451545.0),
        ((1957, 10, 4.81), 2436116.31),
        ((333, 1, 27.5), 1842713.0),
        ((-584, 5, 28.63), 1507900.13),
        ((-1000, 7, 12.5), 1356001.0),
        ((-4712, 1, 1.5), 0.0),
    ],
)
def test_julian_day_meeus_examples(date, jd) -> None:
    assert archaeo.julian_day_of(*date) == pytest.approx(jd, abs=1e-9)
    year, month, day = archaeo.calendar_date(jd)
    assert (year, month) == date[:2]
    assert day == pytest.approx(date[2], abs=1e-6)


def test_format_date_notes_the_julian_calendar() -> None:
    assert archaeo.format_date(archaeo.julian_day_of(-2999, 6, 23.25), time=True) == (
        "23 Jun 3000 BCE 06:00 UT (Julian cal.)"
    )
    assert archaeo.format_date(2451545.0) == "1 Jan 2000 CE"


# --- refraction, curvature, the profile ---------------------------------------------------------------


def test_bennett_refraction() -> None:
    # Meeus ch. 16: 34.5' at the horizon (Bennett), about 9.9' at 5°, under 1' above 45°.
    assert float(horizon.refraction(0.0)) * 60 == pytest.approx(34.5, abs=0.1)
    assert float(horizon.refraction(5.0)) * 60 == pytest.approx(9.9, abs=0.15)
    assert float(horizon.refraction(45.0)) * 60 == pytest.approx(0.99, abs=0.02)
    # Held below -1°, scaled by pressure and temperature.
    assert float(horizon.refraction(-3.0)) == float(horizon.refraction(-1.0))
    cold = float(horizon.refraction(0.0, pressure_hpa=1030.0, temperature_c=-10.0))
    assert cold > float(horizon.refraction(0.0))
    assert float(horizon.true_altitude(0.0, False)) == 0.0


def test_curvature_drop() -> None:
    # 10 km: d / 2R is 0.044966°; with k = 0.13 it is 87 % of that.
    assert float(horizon.curvature_drop(10_000.0, k=0.0)) == pytest.approx(0.044966, abs=1e-6)
    assert float(horizon.curvature_drop(10_000.0)) == pytest.approx(0.87 * 0.044966, abs=1e-6)


def test_best_hits_and_profile_round_trip() -> None:
    assert len(horizon.azimuths(0.5)) == 720 and horizon.azimuths(30.0)[1] == 30.0
    with pytest.raises(ValueError):
        horizon.azimuths(45.0)
    azimuths = np.array([0.0, 90.0, 180.0, 270.0])
    elevations = horizon.coarse_elevations(-1.0, 3.0)
    n_az, n_el = len(azimuths), len(elevations)
    hit = np.zeros((n_az, n_el), dtype=bool)
    distance = np.full((n_az, n_el), np.nan)
    # Azimuth 0: a wall 100 m away and 2 m up hits rays up to atan(2/100) = 1.146°.
    wall = elevations <= math.degrees(math.atan(0.02))
    hit[0, wall] = True
    distance[0, wall] = 100.0 / np.cos(np.radians(elevations[wall]))
    # Azimuth 90: a far hill 20 km away hits rays up to 1°; azimuth 180 open.
    far = elevations <= 1.0
    hit[1, far] = True
    distance[1, far] = 20_000.0 / np.cos(np.radians(elevations[far]))
    hits = horizon.best_hits(elevations, hit, distance, curvature=True, k=0.0)
    assert hits.geometric[0] == pytest.approx(1.1, abs=1e-9)
    assert hits.apparent[1] == pytest.approx(1.0 - 20_000 / (2 * 6371000) * 180 / math.pi, 1e-9)
    assert not np.isfinite(hits.apparent[2])
    profile = horizon.make_profile(azimuths, hits, open_elevation=0.25, info={"x": 1})
    assert profile.open.tolist() == [False, False, True, True]
    assert profile.elevation[2] == 0.25
    again = horizon.HorizonProfile.from_json(profile.to_json())
    assert np.array_equal(again.elevation, profile.elevation)
    assert np.array_equal(again.open, profile.open) and again.info == {"x": 1}
    # Periodic interpolation across north.
    assert float(again.at(315.0)) == pytest.approx((0.25 + profile.elevation[0]) / 2)
    assert "open" in again.as_text()


# --- rise and set ------------------------------------------------------------------------------------


def _noaa_sunrise_azimuth(day: dt.date, latitude: float, longitude: float) -> float:
    # Minutes are UTC from 0h of ``day`` (negative for a sunrise on the UTC day before,
    # as east of about 90° E): the morning of local ``day`` at the site's zone.
    """Sunrise azimuth from the NOAA Solar Calculator spreadsheet's formulas.

    Sunrise is the centre at a zenith of 90.833° (34' refraction and a 16'
    semi-diameter). The declination is taken at the spreadsheet's sunrise
    time, iterated.
    """

    def declination_and_eot(jd: float) -> tuple[float, float]:
        t = (jd - 2451545.0) / 36525.0
        l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0
        m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
        e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
        mr = math.radians(m)
        c = (
            math.sin(mr) * (1.914602 - t * (0.004817 + 0.000014 * t))
            + math.sin(2 * mr) * (0.019993 - 0.000101 * t)
            + math.sin(3 * mr) * 0.000289
        )
        omega = math.radians(125.04 - 1934.136 * t)
        app = l0 + c - 0.00569 - 0.00478 * math.sin(omega)
        eps0 = 23 + (26 + (21.448 - t * (46.815 + t * (0.00059 - t * 0.001813))) / 60) / 60
        eps = eps0 + 0.00256 * math.cos(omega)
        dec = math.degrees(math.asin(math.sin(math.radians(eps)) * math.sin(math.radians(app))))
        y = math.tan(math.radians(eps / 2)) ** 2
        l0r = math.radians(l0)
        eot = 4 * math.degrees(
            y * math.sin(2 * l0r)
            - 2 * e * math.sin(mr)
            + 4 * e * y * math.sin(mr) * math.cos(2 * l0r)
            - 0.5 * y * y * math.sin(4 * l0r)
            - 1.25 * e * e * math.sin(2 * mr)
        )
        return dec, eot

    noon = archaeo.julian_day_of(day.year, day.month, day.day) + 0.5
    jd = noon
    phi = math.radians(latitude)
    for _ in range(4):
        dec, eot = declination_and_eot(jd)
        d = math.radians(dec)
        cos_ha = math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(d)) - math.tan(
            phi
        ) * math.tan(d)
        ha = math.degrees(math.acos(cos_ha))
        minutes = 720 - 4 * longitude - eot - 4 * ha  # UTC minutes of sunrise
        jd = archaeo.julian_day_of(day.year, day.month, day.day) + minutes / 1440.0
    z = math.radians(90.833)
    return math.degrees(
        math.acos((math.sin(d) - math.sin(phi) * math.cos(z)) / (math.cos(phi) * math.sin(z)))
    )


@pytest.mark.parametrize(
    ("day", "lat", "lon"),
    [
        (dt.date(2026, 6, 21), 51.1789, -1.8262),  # a chalk downland site, June solstice
        (dt.date(2026, 12, 21), 51.1789, -1.8262),
        (dt.date(2026, 3, 20), -33.86, 151.21),
        (dt.date(2026, 12, 22), -33.86, 151.21),
        (dt.date(2026, 9, 23), 0.0, 30.0),
        (dt.date(2026, 6, 21), 64.0, -21.0),
    ],
)
def test_sunrise_azimuth_against_noaa(day, lat, lon) -> None:
    tz = dt.timezone(dt.timedelta(hours=round(lon / 15.0)))  # the site's own local day
    ours = archaeo.sun_rise_set(day, tz, lat, lon, 0.0, None, refract=True, limb=1.0)
    reference = _noaa_sunrise_azimuth(day, lat, lon)
    assert ours.rise_azimuth == pytest.approx(reference, abs=0.1)
    # Sunset mirrors it about the meridian to within the day's change of declination.
    assert (360.0 - ours.set_azimuth) == pytest.approx(ours.rise_azimuth, abs=1.0)


def test_sunrise_at_the_equinox_on_the_equator_is_east() -> None:
    moments = archaeo.sun_rise_set(dt.date(2026, 3, 20), dt.UTC, 0.0, 0.0, refract=False, limb=0.0)
    # On the equator a body rises at 90° - δ and sets at 270° + δ.
    at = sun.solar_position([moments.rise_time, moments.set_time], 0.0, 0.0, refraction=False)
    assert abs(at.elevation_airless).max() < 1e-3
    assert moments.rise_azimuth == pytest.approx(90.0 - at.declination[0], abs=0.002)
    assert moments.set_azimuth == pytest.approx(270.0 + at.declination[1], abs=0.002)
    # Twelve hours apart, to within the day's motion.
    assert (moments.set_time - moments.rise_time) / 3600 == pytest.approx(12.0, abs=0.01)


def _flat_profile(altitude: float) -> horizon.HorizonProfile:
    az = horizon.azimuths(1.0)
    hits = horizon.Hits(np.full_like(az, altitude), np.full_like(az, altitude), np.ones_like(az))
    return horizon.make_profile(az, hits)


def test_dated_sunrise_behind_a_raised_horizon() -> None:
    day = dt.date(2026, 6, 21)
    flat = archaeo.sun_rise_set(day, dt.UTC, 51.0, 0.0)
    raised = archaeo.sun_rise_set(day, dt.UTC, 51.0, 0.0, profile=_flat_profile(2.0))
    assert raised.rise_time > flat.rise_time + 600
    assert raised.rise_azimuth > flat.rise_azimuth
    assert raised.rise_altitude == pytest.approx(2.0)
    # It agrees with the declination geometry at the day's declination.
    geometry = archaeo.rise_set_azimuths(
        flat.declination, 51.0, _flat_profile(2.0), limb=archaeo.SUN_SEMI_DIAMETER
    )[0]
    assert raised.rise_azimuth == pytest.approx(geometry.rise_azimuth, abs=0.05)


@pytest.mark.parametrize("parallax", [0.0, moon.MEAN_PARALLAX])
@pytest.mark.parametrize("limb", [-0.27, 0.0, 0.27])
def test_declination_inversion_round_trip(parallax, limb) -> None:
    rng = np.random.default_rng(5)
    for _ in range(25):
        lat = float(rng.uniform(-60, 60))
        dec = float(rng.uniform(-28, 28))
        altitude = float(rng.uniform(-0.5, 6.0))
        profile = _flat_profile(altitude)
        result = archaeo.rise_set_azimuths(dec, lat, profile, limb=limb, parallax=parallax)[0]
        if not math.isfinite(result.rise_azimuth):
            continue
        for az in (result.rise_azimuth, result.set_azimuth):
            back = float(archaeo.declination(az, altitude, lat, limb=limb, parallax=parallax))
            assert back == pytest.approx(dec, abs=0.002), (lat, dec, altitude, az)


def test_rise_set_on_the_astronomical_horizon() -> None:
    # Without refraction, limb or parallax: cos A = sin δ / cos φ.
    for lat, dec in ((51.0, 23.44), (-33.0, -28.6), (0.0, 18.0)):
        result = archaeo.rise_set_azimuths(dec, lat, refract=False)[0]
        expected = math.degrees(
            math.acos(math.sin(math.radians(dec)) / math.cos(math.radians(lat)))
        )
        assert result.rise_azimuth == pytest.approx(expected, abs=1e-4)
        assert result.set_azimuth == pytest.approx(360.0 - expected, abs=1e-4)
    # A body that never rises, or never sets.
    never = archaeo.rise_set_azimuths(-50.0, 60.0)[0]
    assert math.isnan(never.rise_azimuth) and math.isnan(never.set_azimuth)
    circumpolar = archaeo.rise_set_azimuths(50.0, 60.0)[0]
    assert math.isnan(circumpolar.rise_azimuth)


def test_epoch_events_and_alignments() -> None:
    events, summary = archaeo.epoch_events(-2999, 51.18, _flat_profile(1.0), limb="UPPER")
    names = [e.name for e in events]
    assert names[:2] == ["June solstice sunrise", "June solstice sunset"]
    assert len(events) == 14 and summary["epoch"] == "3000 BCE"
    june = events[0]
    assert june.declination == pytest.approx(summary["obliquity"])
    assert june.horizon_altitude == pytest.approx(1.0)
    major = next(e for e in events if e.name == "Major standstill north moonrise")
    assert major.declination == pytest.approx(summary["major"]["mean_north"])
    assert major.declination == pytest.approx(summary["obliquity"] + moon.INCLINATION, abs=1e-3)
    # The ephemeris option uses the extremes it finds.
    extreme, summary2 = archaeo.epoch_events(-2999, 51.18, lunar="EXTREME")
    k = names.index("Major standstill north moonrise")
    assert extreme[k].declination == pytest.approx(summary2["major"]["north"])

    lines = np.array([june.best_azimuth + 0.3, (june.best_azimuth + 180.0) % 360.0, 10.0])
    rows = archaeo.alignments(lines, 51.18, events, _flat_profile(1.0))
    assert rows[0].nearest[0][0] == "June solstice sunrise"
    assert rows[0].nearest[0][2] == pytest.approx(0.3, abs=1e-9)
    # Either direction along a line counts.
    assert rows[1].nearest[0][0] == "June solstice sunrise"
    assert rows[1].nearest[0][2] == pytest.approx(0.0, abs=1e-9)
    # Along the June sunrise line the declination is for the sun's centre on that horizon,
    # higher than ε by about the semi-diameter x sin(φ) / cos(δ) (the event was for the
    # upper limb, the centre then a semi-diameter lower).
    assert rows[1].back_declination == pytest.approx(june.declination + 0.23, abs=0.03)
    assert len(rows[2].nearest) == 3
