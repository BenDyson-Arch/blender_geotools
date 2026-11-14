"""Sun hours and insolation maths (``geotools.analysis.sun_hours`` and ``.clearsky``)."""

from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from geotools.analysis import clearsky, sun
from geotools.analysis import sun_hours as sh

UTC = sun.zone("UTC")

# pvlib-python 0.16.1, clearsky.ineichen(zenith, airmass_absolute, tl, altitude, dni_extra=1366.1)
# with Kasten-Young relative air mass and alt2pres for the absolute air mass.
ZENITHS = np.array([0.0, 30.0, 60.0, 80.0, 85.0, 89.0])
KASTEN_YOUNG = [
    0.9997119918558381,
    1.1539922333636758,
    1.9942928525292494,
    5.5860358798512,
    10.305791327930304,
    26.310555068385266,
]
PVLIB = {
    (0.0, 2.0): (
        [1097.482251, 939.165435, 508.083718, 133.629162, 46.545244, 2.7005],
        [1015.972464, 1003.91259, 940.696885, 683.359747, 446.859153, 105.827957],
        [81.509787, 69.751629, 37.735276, 14.964987, 7.598902, 0.853548],
    ),
    (0.0, 3.0): (
        [1055.8328, 898.145558, 470.345298, 107.650399, 31.236607, 0.975523],
        [943.707724, 917.861089, 789.020222, 413.343189, 176.747515, 9.913176],
        [112.125077, 103.254538, 75.835187, 35.874107, 15.832046, 0.802514],
    ),
    (0.0, 5.0): (
        [977.215706, 821.402421, 403.069433, 69.862551, 14.068278, 0.127299],
        [788.291817, 745.703047, 551.046524, 151.228474, 27.651496, 0.086984],
        [188.923889, 175.604638, 127.546171, 43.602002, 11.658291, 0.125781],
    ),
    (1000.0, 3.0): (
        [1109.983514, 943.193809, 491.050972, 109.60984, 30.77558, 0.859641],
        [988.369301, 964.32095, 843.24923, 475.234429, 223.688947, 17.373582],
        [121.614212, 108.067369, 69.426357, 27.086247, 11.279803, 0.55643],
    ),
}


# --- clear sky ---------------------------------------------------------------------------


def test_kasten_young_air_mass() -> None:
    assert np.allclose(clearsky.relative_airmass(ZENITHS), KASTEN_YOUNG, rtol=1e-12)
    # The published value at the horizon (Kasten and Young 1989): 37.92.
    assert clearsky.relative_airmass(np.array([89.999999]))[0] == pytest.approx(37.92, abs=0.005)
    assert np.isnan(clearsky.relative_airmass(np.array([90.0, 120.0]))).all()


def test_sun_steps_refract_at_the_site_height() -> None:
    """Refraction at the standard-atmosphere pressure of the site: unchanged at sea level,
    a June day at 51 N 4.0 minutes shorter at 4,000 m (616 hPa) than with sea-level
    pressure."""
    steps = sh.period_steps(dt.date(2026, 6, 21), dt.date(2026, 6, 21), dt.UTC, 0.25)
    sea = sh.sun_steps(steps, 51.18, -1.83, 0.0)
    standard = sun.solar_position(steps.times, 51.18, -1.83, 0.0)  # 1013.25 hPa
    assert np.array_equal(sea.elevation, standard.elevation[standard.elevation > 0.0])
    high = sh.sun_steps(steps, 51.18, -1.83, 4000.0)
    at_site = sun.solar_position(steps.times, 51.18, -1.83, 4000.0, pressure_hpa=616.4023)
    assert len(high.hours) == int(np.count_nonzero(at_site.elevation > 0.0))
    shorter = (sea.hours.sum() - high.hours.sum()) * 60.0
    assert shorter == pytest.approx(4.0, abs=0.25)  # was 0: sea-level pressure everywhere


def test_standard_pressure() -> None:
    assert clearsky.altitude_pressure(0.0) == pytest.approx(101325.0, rel=1e-4)
    assert clearsky.altitude_pressure(1000.0) == pytest.approx(89874.75046856777, rel=1e-12)


@pytest.mark.parametrize(("altitude", "turbidity"), sorted(PVLIB))
def test_ineichen_matches_pvlib(altitude: float, turbidity: float) -> None:
    ghi, dni, dhi = clearsky.ineichen(ZENITHS, linke_turbidity=turbidity, altitude_m=altitude)
    want_ghi, want_dni, want_dhi = (np.array(v) for v in PVLIB[(altitude, turbidity)])
    assert np.allclose(ghi, want_ghi, rtol=1e-6, atol=1e-5)
    assert np.allclose(dni, want_dni, rtol=1e-6, atol=1e-5)
    assert np.allclose(dhi, want_dhi, rtol=1e-6, atol=1e-5)
    # GHI = DNI cos z + DHI, by construction.
    assert np.allclose(ghi, dni * np.cos(np.radians(ZENITHS)) + dhi)


def test_ineichen_below_horizon_and_distance() -> None:
    ghi, dni, dhi = clearsky.ineichen(np.array([90.0, 95.0, 180.0]))
    assert not ghi.any() and not dni.any() and not dhi.any()
    near, _, _ = clearsky.ineichen(np.array([30.0]), distance_au=0.983)
    far, _, _ = clearsky.ineichen(np.array([30.0]), distance_au=1.017)
    assert near[0] / far[0] == pytest.approx((1.017 / 0.983) ** 2)
    with pytest.raises(ValueError):
        clearsky.ineichen(np.array([30.0]), linke_turbidity=0.0)


# --- time steps --------------------------------------------------------------------------


def test_period_steps_cover_local_days() -> None:
    tz = sun.fixed_offset(10.0)
    steps = sh.period_steps(dt.date(2026, 6, 21), dt.date(2026, 6, 21), tz, 15)
    assert len(steps.times) == 96
    assert steps.start_utc == dt.datetime(2026, 6, 20, 14, tzinfo=dt.UTC)
    assert steps.hours == 24.0
    assert np.all(steps.durations == 900.0)
    assert steps.times[0] == steps.start_utc.timestamp() + 450.0
    # A step that does not divide the day: the last step is shortened.
    steps = sh.period_steps(dt.date(2026, 1, 1), dt.date(2026, 1, 2), UTC, 7)
    assert steps.durations.sum() == 2 * 86400.0
    assert steps.durations[-1] < 420.0
    assert steps.times[-1] == pytest.approx(steps.end_utc.timestamp() - steps.durations[-1] / 2)
    with pytest.raises(ValueError):
        sh.period_steps(dt.date(2026, 1, 2), dt.date(2026, 1, 1), UTC, 10)
    with pytest.raises(ValueError):
        sh.period_steps(dt.date(2026, 1, 1), dt.date(2026, 1, 1), UTC, 0)


@pytest.mark.skipif(not sun.time_zone_database_available(), reason="no time zone database")
def test_period_steps_follow_daylight_saving() -> None:
    sydney = sun.zone("Australia/Sydney")
    # 2026-10-04: clocks go forward, so the local day is 23 hours long.
    steps = sh.period_steps(dt.date(2026, 10, 4), dt.date(2026, 10, 4), sydney, 60)
    assert steps.hours == 23.0 and len(steps.times) == 23


def test_sun_steps_keep_daylight_only() -> None:
    steps = sh.period_steps(dt.date(2026, 3, 20), dt.date(2026, 3, 20), UTC, 1)
    up = sh.sun_steps(steps, 0.0, 0.0, refraction=False)
    # Equator, equinox: 12 hours of sun, within the declination's drift.
    assert up.hours.sum() == pytest.approx(12.0, abs=0.05)
    assert (up.elevation > 0).all() and up.total_steps == 1440
    assert np.allclose(np.linalg.norm(up.directions, axis=1), 1.0)
    # Refraction lifts the sun at both ends: a few minutes more.
    refracted = sh.sun_steps(steps, 0.0, 0.0, refraction=True)
    assert 0.05 < refracted.hours.sum() - up.hours.sum() < 0.2
    # A 10 degree horizon removes 40 minutes at each end at the equator.
    walled = sh.sun_steps(steps, 0.0, 0.0, refraction=False, horizon=10.0)
    assert up.hours.sum() - walled.hours.sum() == pytest.approx(80 / 60, abs=0.05)
    assert walled.below_profile == pytest.approx(80, abs=3)


def test_convergence_turns_the_directions() -> None:
    steps = sh.period_steps(dt.date(2026, 6, 21), dt.date(2026, 6, 21), UTC, 30)
    true = sh.sun_steps(steps, -27.5, 153.0)
    grid = sh.sun_steps(steps, -27.5, 153.0, convergence=1.5)
    # +Y is 1.5 degrees east of true north, so scene azimuths are 1.5 less.
    true_az = np.degrees(np.arctan2(true.directions[:, 0], true.directions[:, 1]))
    grid_az = np.degrees(np.arctan2(grid.directions[:, 0], grid.directions[:, 1]))
    assert np.allclose((true_az - grid_az + 180) % 360 - 180, 1.5)
    assert np.allclose(true.directions[:, 2], grid.directions[:, 2])


def test_horizon_profile() -> None:
    profile = (np.array([0.0, 90.0, 180.0, 270.0]), np.array([0.0, 10.0, 0.0, 20.0]))
    got = sh.horizon_elevation(np.array([45.0, 90.0, 315.0, 360.0, 720.0 + 135.0]), profile)
    assert np.allclose(got, [5.0, 10.0, 10.0, 0.0, 5.0])
    assert np.allclose(sh.horizon_elevation(np.array([1.0, 2.0]), 4.0), 4.0)
    assert not sh.horizon_elevation(np.array([1.0]), None).any()
    with pytest.raises(ValueError):
        sh.horizon_elevation(np.array([1.0]), (np.array([0.0]), np.array([1.0, 2.0])))


# --- rays, grouping and accumulation -----------------------------------------------------


def _year(step: float = 30.0) -> sh.SunSteps:
    steps = sh.period_steps(dt.date(2026, 1, 1), dt.date(2026, 12, 31), sun.fixed_offset(10), step)
    return sh.sun_steps(steps, -27.47, 153.02, 30.0, 0.8)


def _wall_visibility(directions: np.ndarray, distances: np.ndarray) -> np.ndarray:
    """Unblocked rays from ground points ``distances`` south of an east-west wall 1 high."""
    dy, dz = directions[:, 1], directions[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        height = np.where(dy[None, :] > 0, distances[:, None] * dz[None, :] / dy[None, :], np.inf)
    return height > 1.0


def test_group_directions_keeps_totals() -> None:
    up = _year()
    weights = np.stack([up.hours, up.hours * 2.0], axis=1)
    for tolerance in (0.5, 2.0, 5.0):
        dirs, merged, group, deviation = sh.group_directions(up.directions, weights, tolerance)
        assert np.allclose(merged.sum(axis=0), weights.sum(axis=0), rtol=1e-12)
        assert np.allclose(np.linalg.norm(dirs, axis=1), 1.0)
        assert len(dirs) < len(up.directions)
        assert group.shape == (len(up.directions),) and group.max() == len(dirs) - 1
        # A cell is tolerance x tolerance on the sky, so no member is further
        # than its diagonal from the cell's mean.
        assert 0 < deviation <= tolerance * np.sqrt(2)
    same, weights_out, _, deviation = sh.group_directions(up.directions, up.hours, 0.0)
    assert np.array_equal(same, up.directions) and deviation == 0.0
    assert np.array_equal(weights_out, up.hours)


def test_grouping_error_against_an_analytic_wall() -> None:
    """Measured error of merged directions (documented in the design note)."""
    up = _year()
    distances = np.linspace(0.05, 5.0, 200)
    exact = _wall_visibility(up.directions, distances) @ up.hours
    previous = 0
    for tolerance, rays_at_most, mean_error, max_error in (
        (1.0, 2600, 0.001, 0.004),
        (2.0, 1300, 0.002, 0.008),
        (5.0, 400, 0.005, 0.02),
    ):
        rays = sh.build_rays(up, tolerance_deg=tolerance, insolation=False)
        approx = _wall_visibility(rays.directions, distances) @ rays.hours
        relative = np.abs(approx - exact) / exact.max()
        assert len(rays) <= rays_at_most
        assert relative.mean() < mean_error and relative.max() < max_error, (
            tolerance,
            relative.mean(),
            relative.max(),
        )
        assert previous == 0 or len(rays) < previous
        previous = len(rays)


def test_time_step_error() -> None:
    """The midpoint rule's error over a year against 1-minute steps (in the design note).

    Measured at 27.5 S: the largest error of a year's hours (about 4,400) is
    0.7 h at 5-minute steps, 2.7 h at 10, 15 h at 30 and 28 h at 60.
    """
    fine = _year(1.0)
    distances = np.linspace(0.05, 5.0, 50)
    reference = _wall_visibility(fine.directions, distances) @ fine.hours
    for step, max_hours in ((5.0, 1.5), (10.0, 4.0), (30.0, 20.0), (60.0, 35.0)):
        coarse = _year(step)
        hours = _wall_visibility(coarse.directions, distances) @ coarse.hours
        assert np.abs(hours - reference).max() < max_hours, step
        # Day length over the year: within a few hours of the 1-minute sum.
        assert abs(coarse.hours.sum() - fine.hours.sum()) < max_hours


def test_sky_directions_integrate_the_hemisphere() -> None:
    sky = sh.sky_directions(64)
    assert sky.shape == (64, 3) and (sky[:, 2] > 0).all()
    assert np.allclose(np.linalg.norm(sky, axis=1), 1.0)
    # Sum of cos(zenith) x 2/M is exactly 1: an unobstructed horizontal
    # surface sees the whole sky.
    assert (sky[:, 2] * 2 / 64).sum() == pytest.approx(1.0, abs=1e-12)
    # A vertical surface sees half of it, cosine-weighted: 1/2.
    facing = sky @ np.array([1.0, 0.0, 0.0])
    assert (np.where(facing > 0, facing, 0) * 2 / 64).sum() == pytest.approx(0.5, abs=0.02)
    with pytest.raises(ValueError):
        sh.sky_directions(0)


def test_build_rays_weights() -> None:
    steps = sh.period_steps(dt.date(2026, 3, 21), dt.date(2026, 3, 21), UTC, 10)
    up = sh.sun_steps(steps, 0.0, 0.0, refraction=False)
    rays = sh.build_rays(up, insolation=True, diffuse_rays=32)
    assert rays.sun_rays == len(up.hours) and len(rays) == len(up.hours) + 32
    assert np.array_equal(rays.hours[: rays.sun_rays], up.hours)
    assert not rays.hours[rays.sun_rays :].any()
    _ghi, dni, dhi = clearsky.ineichen(90.0 - up.elevation, distance_au=up.distance_au)
    assert np.allclose(rays.energy[: rays.sun_rays], dni * up.hours)
    assert rays.energy[rays.sun_rays :].sum() == pytest.approx(
        float(np.sum(dhi * up.hours)) * 2.0, rel=1e-12
    )
    stats = rays.stats
    assert stats["sky_rays"] == 32 and stats["sun_rays"] == rays.sun_rays
    assert stats["diffuse_horizontal_kwh_m2"] == pytest.approx(np.sum(dhi * up.hours) / 1000)
    # Without insolation, energies are zero and there are no sky rays.
    plain = sh.build_rays(up, insolation=False, diffuse_rays=32)
    assert len(plain) == len(up.hours) and not plain.energy.any()
    # A horizon profile removes the sky below it too.
    walled = sh.build_rays(up, insolation=True, diffuse_rays=32, horizon=45.0)
    assert walled.stats["sky_rays"] < 32


def test_accumulate_open_ground_is_ghi() -> None:
    steps = sh.period_steps(dt.date(2026, 3, 21), dt.date(2026, 3, 21), UTC, 10)
    up = sh.sun_steps(steps, 0.0, 0.0, refraction=False)
    rays = sh.build_rays(up, insolation=True, diffuse_rays=64)
    normals = np.array([[0.0, 0.0, 1.0], [0.0, 0.0, -1.0], [1.0, 0.0, 0.0]])
    visible = np.ones((3, len(rays)), bool)
    hours, energy = sh.accumulate(visible, normals, rays)
    ghi, _dni, _dhi = clearsky.ineichen(90.0 - up.elevation, distance_au=up.distance_au)
    assert hours[0] == pytest.approx(up.hours.sum())
    assert energy[0] == pytest.approx(np.sum(ghi * up.hours) / 1000.0, rel=1e-9)
    # Facing down: no sun, no sky.
    assert hours[1] == 0.0 and energy[1] == 0.0
    # Facing east: the morning only.
    assert hours[2] == pytest.approx(up.hours.sum() / 2, abs=0.2)
    # Blocked rays add nothing.
    hours, energy = sh.accumulate(np.zeros((3, len(rays)), bool), normals, rays)
    assert not hours.any() and not energy.any()


def test_ray_offset() -> None:
    assert sh.ray_offset(np.zeros((1, 3)), override=0.25) == 0.25
    near = sh.ray_offset(np.array([[0.0, 0.0, 0.0], [1000.0, 1000.0, 1.0]]))
    assert near == pytest.approx(1e-6 * np.sqrt(2_000_001.0))  # the extent dominates
    far = sh.ray_offset(np.array([[20000.0, 0.0, 0.0], [20001.0, 0.0, 0.0]]))
    assert far == 16 * float(np.spacing(np.float32(20001.0)))  # float32 steps dominate


# --- seasons -------------------------------------------------------------------------------


def test_season_moments_match_published_times() -> None:
    # USNO / IMCCE: 2024-03-20 03:06, 06-20 20:51, 09-22 12:44, 12-21 09:21 UTC.
    published = {
        "march": dt.datetime(2024, 3, 20, 3, 6, tzinfo=dt.UTC),
        "june": dt.datetime(2024, 6, 20, 20, 51, tzinfo=dt.UTC),
        "september": dt.datetime(2024, 9, 22, 12, 44, tzinfo=dt.UTC),
        "december": dt.datetime(2024, 12, 21, 9, 21, tzinfo=dt.UTC),
    }
    got = sh.season_moments(2024)
    assert list(got) == list(sh.SEASONS)
    for name, when in published.items():
        assert abs((got[name] - when).total_seconds()) < 90, (name, got[name])
