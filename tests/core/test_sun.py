"""NREL SPA sun positions, time zones and light directions (geotools.analysis.sun)."""

from __future__ import annotations

import datetime as dt
import time
import zoneinfo

import numpy as np
import pytest

from geotools.analysis import sun

# The worked example of Reda and Andreas (NREL/TP-560-34302, Table A5.1 and A5.2).
SPA_MOMENT = dt.datetime(2003, 10, 17, 12, 30, 30, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
SPA_SITE = {"latitude": 39.742476, "longitude": -105.1786, "height": 1830.14}
SPA_ATMOSPHERE = {"pressure_hpa": 820.0, "temperature_c": 11.0, "delta_t_seconds": 67.0}


def _spa_example() -> sun.SunPosition:
    return sun.solar_position(SPA_MOMENT.timestamp(), **SPA_SITE, **SPA_ATMOSPHERE)


def test_spa_paper_example() -> None:
    position = _spa_example()
    # The paper's results, to its printed precision (5 decimals): within 1e-5 degrees.
    assert position.zenith[0] == pytest.approx(50.11162, abs=1e-5)
    assert position.azimuth[0] == pytest.approx(194.34024, abs=1e-5)
    assert position.elevation_airless[0] == pytest.approx(39.872046, abs=1e-6)
    assert position.declination[0] == pytest.approx(-9.316179, abs=1e-6)


def test_spa_paper_intermediate_values() -> None:
    jd = np.atleast_1d(sun.julian_day(SPA_MOMENT.timestamp()))
    assert jd[0] == pytest.approx(2452930.312847, abs=1e-6)
    lam, beta, radius, delta_psi, eps = sun._ephemeris(jd, np.array([67.0]))
    assert lam[0] % 360.0 == pytest.approx(204.0085519281, abs=1e-9)
    assert beta[0] == pytest.approx(0.0001011219, abs=1e-10)
    assert radius[0] == pytest.approx(0.9965422974, abs=1e-10)
    assert delta_psi[0] == pytest.approx(-0.00399840, abs=1e-8)
    assert eps[0] == pytest.approx(23.440465, abs=1e-6)


def test_vectorised_matches_one_at_a_time() -> None:
    rng = np.random.default_rng(5)
    times = dt.datetime(2026, 1, 1, tzinfo=dt.UTC).timestamp() + rng.uniform(0, 3.15e7, 20)
    together = sun.solar_position(times, -27.47, 153.02, 30.0)
    for i, t in enumerate(times):
        alone = sun.solar_position(float(t), -27.47, 153.02, 30.0)
        assert alone.azimuth[0] == pytest.approx(together.azimuth[i], abs=1e-9)
        assert alone.elevation[0] == pytest.approx(together.elevation[i], abs=1e-9)


def test_interpolated_ephemeris_matches_direct() -> None:
    # A day at one-minute steps takes the interpolated path (1441 times, 26 nodes).
    start = dt.datetime(2026, 3, 20, tzinfo=dt.UTC).timestamp()
    times = start + 60.0 * np.arange(1441)
    jd = sun.julian_day(times)
    dt_seconds = sun.delta_t(np.full(1, 2026.2))[0]
    fast = sun.solar_position_jd(jd, 51.5, -0.12, delta_t_seconds=dt_seconds)
    direct = sun._topocentric(
        jd,
        *sun._ephemeris(jd, np.full(len(jd), dt_seconds)),
        51.5,
        -0.12,
        0.0,
        sun.DEFAULT_PRESSURE_HPA,
        sun.DEFAULT_TEMPERATURE_C,
        True,
        sun.DEFAULT_ATMOS_REFRACT,
    )
    d_az = (fast.azimuth - direct.azimuth + 180.0) % 360.0 - 180.0
    assert np.abs(d_az).max() < 5e-7
    assert np.abs(fast.elevation - direct.elevation).max() < 5e-7


def test_a_year_of_minutes_is_cheap() -> None:
    times = dt.datetime(2026, 1, 1, tzinfo=dt.UTC).timestamp() + 60.0 * np.arange(525_600)
    started = time.perf_counter()
    position = sun.solar_position(times, -27.47, 153.02)
    elapsed = time.perf_counter() - started
    assert len(position) == 525_600
    assert elapsed < 10.0, elapsed  # about 0.5 s on a desktop; generous for CI
    # Brisbane, 27.5 S: the noon sun is highest near the December solstice.
    assert 85.0 < position.elevation.max() < 87.0


def test_noon_elevation_and_hemisphere() -> None:
    # Equinox: the sun crosses the meridian at 90 - |latitude| (to refraction and
    # the equinox's hour), due north from the south and due south from the north.
    day = dt.datetime(2026, 3, 20, tzinfo=dt.UTC).timestamp()
    times = day + 60.0 * np.arange(1440)
    for lat, lon, bearing in ((-33.87, 151.21, 0.0), (51.48, 0.0, 180.0)):
        position = sun.solar_position(times, lat, lon, refraction=False)
        noon = int(np.argmax(position.elevation))
        assert position.elevation[noon] == pytest.approx(90.0 - abs(lat), abs=0.6)
        offset = (position.azimuth[noon] - bearing + 180.0) % 360.0 - 180.0
        assert abs(offset) < 2.0


def test_refraction_only_lifts_the_sun_near_and_above_the_horizon() -> None:
    times = dt.datetime(2026, 6, 21, tzinfo=dt.UTC).timestamp() + 600.0 * np.arange(144)
    seen = sun.solar_position(times, 40.0, -105.0)
    airless = sun.solar_position(times, 40.0, -105.0, refraction=False)
    lift = seen.elevation - airless.elevation
    assert (lift >= 0).all()
    assert lift[airless.elevation < -1.0].max() == 0.0
    assert 0.4 < lift[np.argmin(np.abs(airless.elevation))] < 0.6  # about 0.5 at the horizon


def test_standard_pressure_at_the_site_height() -> None:
    """The ICAO standard atmosphere: exactly the sea-level pressure at 0 m."""
    from geotools.analysis import clearsky

    assert sun.standard_pressure(0.0) == sun.DEFAULT_PRESSURE_HPA  # unchanged at sea level
    assert sun.standard_pressure(0.0, 1010.0) == 1010.0
    # The ICAO table: 898.76 hPa at 1,000 m, 616.40 hPa at 4,000 m.
    assert sun.standard_pressure(1000.0) == pytest.approx(898.76, abs=0.02)
    assert sun.standard_pressure(4000.0) == pytest.approx(616.40, abs=0.02)
    assert sun.standard_pressure(-400.0) > sun.DEFAULT_PRESSURE_HPA
    # pvlib's alt2pres (which clear-sky air mass uses) agrees to 1e-5.
    for h in (0.0, 1500.0, 4000.0):
        ratio = clearsky.altitude_pressure(h) / clearsky.altitude_pressure(0.0)
        assert sun.standard_pressure(h) / sun.DEFAULT_PRESSURE_HPA == pytest.approx(ratio, rel=1e-5)
    assert sun.standard_pressure(30_000.0) == sun.standard_pressure(11_000.0)  # troposphere
    with pytest.raises(ValueError):
        sun.standard_pressure(float("nan"))


def test_delta_t() -> None:
    assert sun.delta_t(2000.0) == pytest.approx(63.86, abs=0.01)
    # Continuous across the 2005 and 2050 joins, to a fraction of a second.
    for year in (2005.0, 2050.0):
        below, above = sun.delta_t(np.array([year - 1e-6, year]))
        assert abs(above - below) < 1.0
    assert 60.0 < sun.delta_t(2026.5) < 80.0


def test_input_checks() -> None:
    with pytest.raises(ValueError, match="latitude"):
        sun.solar_position(0.0, 91.0, 0.0)
    with pytest.raises(ValueError, match="longitude"):
        sun.solar_position(0.0, 0.0, 200.0)
    with pytest.raises(ValueError, match="finite"):
        sun.solar_position(np.nan, 0.0, 0.0)
    assert len(sun.solar_position(np.empty(0), 0.0, 0.0)) == 0


# --- time zones ---------------------------------------------------------------------


def test_utc_and_fixed_offsets_need_no_database() -> None:
    assert sun.zone("UTC") is dt.UTC
    assert sun.zone(" utc ") is dt.UTC
    assert sun.fixed_offset(-7).utcoffset(None) == dt.timedelta(hours=-7)
    assert sun.fixed_offset(5.75).utcoffset(None) == dt.timedelta(hours=5, minutes=45)
    with pytest.raises(ValueError):
        sun.fixed_offset(24.0)
    utc, notes = sun.to_utc(dt.datetime(2003, 10, 17, 12, 30, 30), sun.fixed_offset(-7))
    assert utc == SPA_MOMENT and notes == ()


def test_missing_database_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_database(_name: str) -> None:
        raise zoneinfo.ZoneInfoNotFoundError("No time zone found")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", no_database)
    sun.time_zone_database_available.cache_clear()
    try:
        assert not sun.time_zone_database_available()
        with pytest.raises(sun.TimeZoneUnavailable, match="UTC offset") as raised:
            sun.zone("Australia/Brisbane")
        assert raised.value.no_database
        assert sun.zone("UTC") is dt.UTC
    finally:
        monkeypatch.undo()
        sun.time_zone_database_available.cache_clear()


needs_tzdb = pytest.mark.skipif(
    not sun.time_zone_database_available(), reason="no time zone database"
)


@needs_tzdb
def test_named_zone_and_daylight_saving() -> None:
    denver = sun.zone("America/Denver")
    utc, notes = sun.to_utc(dt.datetime(2003, 10, 17, 12, 30, 30), denver)
    assert notes == ()
    # Mid-October Denver is on MDT (UTC-6), so this is an hour before the paper's moment.
    assert utc == SPA_MOMENT - dt.timedelta(hours=1)
    _, gap = sun.to_utc(dt.datetime(2026, 3, 8, 2, 30), denver)
    assert "does not exist" in gap[0]
    _, fold = sun.to_utc(dt.datetime(2026, 11, 1, 1, 30), denver)
    assert "happens twice" in fold[0]
    with pytest.raises(sun.TimeZoneUnavailable, match="Unknown time zone") as raised:
        sun.zone("Mars/Olympus_Mons")
    assert not raised.value.no_database


def test_local_times() -> None:
    start = dt.datetime(2026, 6, 21, 5, tzinfo=dt.UTC)
    times = sun.local_times(start, 3, 900.0)
    assert times.tolist() == [start.timestamp() + 900.0 * i for i in range(3)]
    with pytest.raises(ValueError):
        sun.local_times(start.replace(tzinfo=None), 3, 900.0)


# --- directions -----------------------------------------------------------------------


def _euler_matrix(x: float, y: float, z: float) -> np.ndarray:
    """Blender's XYZ Euler: Rz @ Ry @ Rx."""
    cx, sx, cy, sy, cz, sz = np.cos(x), np.sin(x), np.cos(y), np.sin(y), np.cos(z), np.sin(z)
    rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return rz @ ry @ rx


def test_direction_axes() -> None:
    assert np.allclose(sun.direction(0.0, 0.0), [[0, 1, 0]])  # north on the horizon
    assert np.allclose(sun.direction(90.0, 0.0), [[1, 0, 0]])  # east
    assert np.allclose(sun.direction(180.0, 45.0), [[0, -np.sqrt(0.5), np.sqrt(0.5)]])
    assert np.allclose(sun.direction(123.0, 90.0), [[0, 0, 1]])


def test_light_points_away_from_the_sun() -> None:
    azimuth = np.array([10.0, 95.0, 181.0, 300.0, 359.0])
    elevation = np.array([5.0, 30.0, 60.0, -10.0, 89.0])
    euler = sun.light_euler(azimuth, elevation)
    expected = sun.direction(azimuth, elevation)
    for angles, towards_sun in zip(euler, expected, strict=True):
        local_z = _euler_matrix(*angles) @ np.array([0.0, 0.0, 1.0])
        assert np.allclose(local_z, towards_sun, atol=1e-12)  # the light shines along -Z


def test_light_euler_is_continuous_through_north() -> None:
    azimuth = np.array([350.0, 355.0, 0.0, 5.0, 10.0])  # the sun passing north
    z = sun.light_euler(azimuth, np.full(5, 40.0))[:, 2]
    assert np.allclose(np.diff(z), np.radians(-5.0))


def test_utc_datetime_before_1970():
    """Negative timestamps work on every platform (fromtimestamp fails on Windows)."""
    import datetime as dt

    from geotools.analysis import sun as _sun

    assert _sun.utc_datetime(0.0) == dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    when = dt.datetime(1850, 3, 20, 12, 30, tzinfo=dt.UTC)
    assert _sun.utc_datetime(when.timestamp()) == when
    ancient = dt.datetime(1, 1, 1, tzinfo=dt.UTC)
    assert _sun.utc_datetime(ancient.timestamp()) == ancient
