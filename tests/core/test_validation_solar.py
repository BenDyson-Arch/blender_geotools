"""Validation of the solar tools against independent references (docs/validation/solar.md).

The reference values in ``tests/data/validation/solar/`` were computed with
pvlib-python, Skyfield and JPL DE440s, pyproj's geodesics and GRASS GIS r.sun
by ``scripts/validation/solar/`` (which never import GeoTools). This file
only compares GeoTools against them; the tolerances are justified in the
validation document and next to each assertion.
"""

from __future__ import annotations

import datetime as dt
import json
from functools import cache
from pathlib import Path

import numpy as np
import pytest

from geotools.analysis import sun
from geotools.analysis import sun_hours as sh

DATA = Path(__file__).resolve().parents[1] / "data" / "validation" / "solar"
J2000_UNIX = 946_728_000.0
EARTH_ROTATION = 360.0 / 86164.0905  # degrees of hour angle per second of UT1


@cache
def load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


def separation(az1, el1, az2, el2) -> np.ndarray:
    """Great-circle angle (degrees) between two sky directions."""
    a = sun.direction(az1, el1)
    b = sun.direction(az2, el2)
    return np.degrees(np.arccos(np.clip(np.einsum("ij,ij->i", a, b), -1.0, 1.0)))


def wrap(angle) -> np.ndarray:
    return (np.asarray(angle) + 180.0) % 360.0 - 180.0


NO_TZDB = not sun.time_zone_database_available()  # Python on Windows without tzdata


def needs_database(row: dict) -> bool:
    """True for a named zone this Python cannot load (those rows are skipped)."""
    return NO_TZDB and not row["zone"].startswith("UTC")


def time_zone(name: str):
    if name.startswith("UTC") and len(name) > 3:  # "UTC+05:45"
        hours, minutes = name[4:].split(":")
        sign = 1.0 if name[3] == "+" else -1.0
        return sun.fixed_offset(sign * (int(hours) + int(minutes) / 60.0))
    return sun.zone(name)


def normal(tilt: float, aspect: float) -> np.ndarray:
    """Unit normal of a surface tilted ``tilt`` degrees, facing true azimuth ``aspect``."""
    t, a = np.radians(tilt), np.radians(aspect)
    return np.array([np.sin(t) * np.sin(a), np.sin(t) * np.cos(a), np.cos(t)])


def sun_hours(
    row: dict,
    step: float,
    *,
    first: str | None = None,
    last: str | None = None,
    surface=(0.0, 0.0, 1.0),
    horizon=None,
    tolerance: float = 0.0,
) -> tuple[float, float]:
    """GeoTools' hours of sun on an open surface (no occluder), and the period's hours."""
    first = first or row.get("first") or row["day"]
    last = last or row.get("last") or row["day"]
    steps = sh.period_steps(
        dt.date.fromisoformat(first), dt.date.fromisoformat(last), time_zone(row["zone"]), step
    )
    up = sh.sun_steps(
        steps,
        row["latitude"],
        row["longitude"],
        refraction=row.get("refraction", True),
        horizon=horizon,
    )
    rays = sh.build_rays(up, insolation=False, tolerance_deg=tolerance)
    hours, _ = sh.accumulate(np.ones((1, len(rays)), bool), np.array([surface], float), rays)
    return float(hours[0]), steps.hours


# --- sun position ------------------------------------------------------------------------------


@pytest.mark.parametrize("group", ["spa", "horizon"])
def test_spa_matches_pvlib_spa(group: str) -> None:
    """The same algorithm (pvlib.spa, numpy) from -1999 to 5999, refraction included.

    Both follow Reda and Andreas step by step in binary64 with the same Delta T,
    pressure and temperature, so they must agree to round-off: 1e-6 degrees
    (observed 2e-9). The ``horizon`` group sits at -1.5 to 5 degrees, where the
    refraction formula and its switch act.
    """
    ref = load("sun_position.json")[group]
    n = len(ref["unix"])
    for i in range(n):
        got = sun.solar_position(
            ref["unix"][i],
            ref["latitude"][i],
            ref["longitude"][i],
            ref.get("height", [0.0] * n)[i],
            pressure_hpa=ref.get("pressure_hpa", [1013.25] * n)[i],
            temperature_c=ref.get("temperature_c", [12.0] * n)[i],
            delta_t_seconds=ref["delta_t"][i],
        )
        assert got.elevation[0] == pytest.approx(ref["elevation"][i], abs=1e-6), i
        assert got.elevation_airless[0] == pytest.approx(ref["elevation_airless"][i], abs=1e-6), i
        cos_el = np.cos(np.radians(ref["elevation_airless"][i]))
        assert abs(wrap(got.azimuth[0] - ref["azimuth"][i])) * cos_el < 1e-6, i


def test_spa_matches_skyfield_de440() -> None:
    """Airless topocentric sun against Skyfield with JPL DE440s, 1850-2149, same UT1 and Delta T.

    The SPA paper states +/-0.0003 degrees for its truncated VSOP87 series;
    observed max 0.00017 degrees, median 0.00006.
    """
    ref = load("sun_position.json")["skyfield"]
    got = [
        sun.solar_position(
            ref["unix"][i],
            ref["latitude"][i],
            ref["longitude"][i],
            ref["height"][i],
            delta_t_seconds=ref["delta_t"][i],
            refraction=False,
        )
        for i in range(len(ref["unix"]))
    ]
    az = np.array([p.azimuth[0] for p in got])
    el = np.array([p.elevation[0] for p in got])
    error = separation(az, el, ref["azimuth"], ref["elevation_airless"])
    assert error.max() < 3e-4
    assert np.median(error) < 1e-4


def test_end_to_end_against_skyfield_in_utc() -> None:
    """UTC moments 1973-2025: GeoTools takes UT1 = UTC and its Delta T polynomial.

    Skyfield uses IERS UT1 - UTC and Delta T. UT1 - UTC turns the sky by
    0.0042 degrees a second, so each case is held to the SPA's 0.0003 degrees
    plus 0.0001 for Delta T (up to 6 s off today, about 0.00007 degrees) plus
    that rotation.
    """
    ref = load("sun_position.json")["skyfield_utc"]
    got = [
        sun.solar_position(
            ref["unix"][i], ref["latitude"][i], ref["longitude"][i], refraction=False
        )
        for i in range(len(ref["unix"]))
    ]
    az = np.array([p.azimuth[0] for p in got])
    el = np.array([p.elevation[0] for p in got])
    error = separation(az, el, ref["azimuth"], ref["elevation_airless"])
    bound = 4e-4 + EARTH_ROTATION * np.abs(ref["ut1_minus_utc"])
    assert (error < bound).all(), np.max(error - bound)
    assert error.max() < 0.004  # |UT1 - UTC| < 0.9 s


def test_delta_t_matches_pvlib() -> None:
    """Espenak and Meeus (2006), against pvlib.spa.calculate_deltat (an independent coding)."""
    ref = load("sun_position.json")["delta_t"]
    got = sun.delta_t(np.array(ref["decimal_year"]))
    assert np.allclose(got, ref["delta_t"], rtol=0.0, atol=1e-6)


def test_convergence_matches_geodesics() -> None:
    """Grid convergence and sun directions from site.sun_site against geodesic steps, projected.

    The reference does not use PROJ's get_factors. For conformal projections
    the grid bearing of any true azimuth is that azimuth minus the convergence,
    to 1e-6 degrees (observed 1e-7). Two allowed CRSs are not conformal, so one
    rotation cannot map every azimuth: Web Mercator (EPSG:3857, spherical
    formulas on ellipsoidal coordinates) and Australian Albers (EPSG:3577,
    equal-area). There the convergence alone is 0.09 and 0.24 degrees out at
    these points (the turn before 0.3.0); ``scene_azimuth`` maps every
    direction through the grid-to-ground map and is within 1e-6 everywhere.
    """
    pytest.importorskip("pyproj")
    from geotools.analysis import site
    from geotools.core.placement import Placement, PlacementMode

    non_conformal = {"EPSG:3857": 0.1, "EPSG:3577": 0.3}
    for point in load("sun_position.json")["convergence"]["points"]:
        placement = Placement(
            mode=PlacementMode.SOURCE_CRS,
            source_crs=point["crs"],
            source_offset=(point["east"], point["north"], 0.0),
        )
        where = site.sun_site(placement)
        what = point["what"]
        assert where.convergence == pytest.approx(point["convergence"], abs=1e-6), what
        assert where.latitude == pytest.approx(point["latitude"], abs=1e-9), what
        assert where.longitude == pytest.approx(point["longitude"], abs=1e-9), what
        azimuths = np.array(point["azimuths"])
        bearings = np.array(point["grid_bearings"])
        mapped = np.abs(wrap(where.scene_azimuth(azimuths) - bearings)).max()
        assert mapped < 1e-6, (what, mapped)
        back = np.abs(wrap(where.true_azimuth(bearings) - azimuths)).max()
        assert back < 1e-6, (what, back)
        turned = np.abs(wrap((azimuths - where.convergence) % 360.0 - bearings)).max()
        assert where.conformal == (point["crs"] not in non_conformal), what
        if point["crs"] in non_conformal:
            # The convergence alone: the documented limit before 0.3.0, still the
            # one rotation Aspect (True North) can take (site.SunSite.rotation).
            assert 0.01 < turned < non_conformal[point["crs"]], (what, turned)
            assert where.rotation == pytest.approx(where.convergence, abs=1e-6), what
        else:
            assert turned < 1e-6 and where.rotation == where.convergence, (what, turned)


# --- shadow study: equinoxes and solstices -------------------------------------------------------


def test_season_moments_against_skyfield() -> None:
    """Equinoxes and solstices 1850-2149 against Skyfield's almanac.seasons with DE440s.

    In TT (the ephemeris alone) within 30 s: the SPA's 0.0003 degrees of solar
    longitude is 26 s of the Sun's motion (observed max 26 s). In UT1 the Delta
    T models differ too: within a minute up to 2067, as the design doc says;
    beyond that Espenak and Meeus' extrapolation runs ahead of the IERS-based
    prediction (by 1.8 min in 2100 and 3.3 min by 2149). The Shadow Study only
    takes the local date from these moments.
    """
    ref = load("seasons.json")
    years = np.array(ref["year"])
    # Every fifth year and every year 2020-2030 keeps the test to a few seconds.
    keep = (years % 5 == 0) | ((years >= 2020) & (years <= 2030)) | (years == 2067)
    years = years[keep]
    seasons = np.array(ref["season"])[keep]
    ut1 = np.array(ref["ut1"])[keep]
    tt = np.array(ref["tt"])[keep]
    moments = {int(year): sh.season_moments(int(year)) for year in np.unique(years)}
    got = np.array(
        [
            moments[int(year)][ref["seasons"][season]].timestamp()
            for year, season in zip(years, seasons, strict=True)
        ]
    )
    delta_t = sun.delta_t(2000.0 + (got - J2000_UNIX) / (365.25 * 86400.0))
    assert np.abs(got + delta_t - tt).max() < 30.0
    near = years <= 2067
    assert np.abs(got - ut1)[near].max() < 60.0
    assert np.abs(got - ut1).max() < 240.0
    assert len(got) > 250


# --- sun hours -----------------------------------------------------------------------------------


def _open_cases() -> list[dict]:
    return load("sun_hours.json")["open"]


@pytest.mark.parametrize(
    "case", [c for c in _open_cases() if c["first"] == c["last"]], ids=lambda c: c["name"]
)
def test_day_length_on_open_ground(case: dict) -> None:
    """Hours of sun on open ground over one local day, against Skyfield's risings and settings.

    The midpoint rule is off by at most half a step at each sunrise and sunset,
    so a day is within one step (plus 0.1 min for the two ephemerides and UT1).
    The local day's length (23, 24, 24.5 or 25 h) must be exact.
    """
    if needs_database(case):
        pytest.skip("no time zone database")
    for step in (1, 5, 15):
        hours, period = sun_hours(case, step, horizon=case["horizon"] or None)
        assert period == pytest.approx(case["period_hours"], abs=1e-9)
        assert abs(hours - case["hours"]) * 60.0 <= step + 0.1, (step, hours, case["hours"])
        if case["hours"] in (0.0, 24.0):
            assert hours == pytest.approx(case["hours"], abs=1e-9)  # polar night and day


@pytest.mark.parametrize(
    "case", [c for c in _open_cases() if c["first"] != c["last"]], ids=lambda c: c["name"]
)
def test_seasons_and_years_on_open_ground(case: dict) -> None:
    """A season or a year of open-ground sun hours at 1-minute steps.

    The per-day errors partly cancel: within 1e-4 of the total (observed 5e-5).
    """
    if needs_database(case):
        pytest.skip("no time zone database")
    hours, period = sun_hours(case, 1)
    assert period == pytest.approx(case["period_hours"], abs=1e-9)
    assert abs(hours / case["hours"] - 1.0) < 1e-4


def test_slopes_and_walls() -> None:
    """Tilted, vertical and overhanging surfaces, 7 sites and days, against Skyfield sampled at 2 s.

    A step can be wrong at sunrise, sunset and each crossing of the surface's
    own plane (cos(incidence) = 0): at most 4 such edges a day, each half a
    step, plus 0.1 min.
    """
    for row in load("sun_hours.json")["surfaces"]:
        if needs_database(row):
            continue
        surface = normal(row["tilt"], row["aspect"])
        for step in (1, 15):
            hours, _ = sun_hours(row, step, surface=surface)
            assert abs(hours - row["hours"]) * 60.0 <= 2.0 * step + 0.1, (row, step, hours)


def test_horizon_profile_by_azimuth() -> None:
    """A far horizon varying with true azimuth, linear and periodic between four points."""
    data = load("sun_hours.json")["profile"]
    profile = (np.array(data["azimuths"]), np.array(data["elevations"]))
    for row in data["cases"]:
        if needs_database(row):
            continue
        surface = normal(row["tilt"], row["aspect"])
        for step in (1, 15):
            hours, _ = sun_hours(row, step, surface=surface, horizon=profile)
            assert abs(hours - row["hours"]) * 60.0 <= 2.0 * step + 0.1, (row, step, hours)


def test_merging_never_changes_open_ground() -> None:
    """Merge Within keeps every hour on open horizontal ground (all merged rays stay up)."""
    case = next(c for c in _open_cases() if c["name"] == "78 N year (Svalbard)")
    exact, _ = sun_hours(case, 30)
    merged, _ = sun_hours(case, 30, tolerance=2.0)
    assert merged == pytest.approx(exact, rel=1e-12)


def test_wall_instant_reference_is_consistent() -> None:
    """The Blender shadow test's instant: GeoTools' SPA and convergence against Skyfield + geodesics."""
    pytest.importorskip("pyproj")
    from geotools.analysis import site
    from geotools.core.placement import Placement, PlacementMode

    ref = load("sun_hours.json")["wall"]
    placement = Placement(
        mode=PlacementMode.SOURCE_CRS,
        source_crs=ref["crs"],
        source_offset=(ref["east"], ref["north"], 0.0),
    )
    where = site.sun_site(placement)
    position = sun.solar_position(ref["unix"], where.latitude, where.longitude, refraction=False)
    assert position.elevation[0] == pytest.approx(ref["elevation_airless"], abs=1e-3)
    grid = (position.azimuth[0] - where.convergence) % 360.0
    assert grid == pytest.approx(ref["grid_bearing"], abs=1e-3)


# --- clear-sky insolation ----------------------------------------------------------------------


def _insolation(row: dict, rays_on_sky: int):
    steps = sh.period_steps(
        dt.date.fromisoformat(row["day"]),
        dt.date.fromisoformat(row["day"]),
        time_zone(row["zone"]),
        1,
    )
    up = sh.sun_steps(steps, row["latitude"], row["longitude"], row["altitude"])
    common = {"linke_turbidity": row["linke_turbidity"], "altitude_m": row["altitude"]}
    direct = sh.build_rays(up, insolation=True, diffuse_rays=0, **common)
    total = sh.build_rays(up, insolation=True, diffuse_rays=rays_on_sky, **common)
    return direct, total


def test_insolation_matches_pvlib_chain() -> None:
    """Daily clear-sky irradiation against pvlib's own chain (SPA, Ineichen, isotropic transposition).

    GHI, DNI, DHI and the direct beam on each surface: the same model, so
    within 1e-5 relative or 1e-3 kWh/m² (one minute's step flipping at
    sunrise or at a surface's own plane). Diffuse: GeoTools integrates the
    isotropic sky with 64 Fibonacci rays, a midpoint rule whose error is up to
    2.4 % of (1 + cos tilt) / 2 at tilts up to 90 degrees (1/M convergence:
    0.15 % with 1024 rays).
    """
    for row in load("clearsky.json")["days"]:
        if needs_database(row):
            continue
        direct, total = _insolation(row, 64)
        stats = direct.stats
        assert stats["direct_normal_kwh_m2"] == pytest.approx(row["dni"], rel=1e-5, abs=1e-3)
        assert stats["diffuse_horizontal_kwh_m2"] == pytest.approx(row["dhi"], rel=1e-5, abs=1e-3)
        for surface in row["surfaces"]:
            n = normal(surface["tilt"], surface["aspect"])[None, :]
            _, beam = sh.accumulate(np.ones((1, len(direct)), bool), n, direct)
            _, both = sh.accumulate(np.ones((1, len(total)), bool), n, total)
            what = (row["site"], surface["surface"])
            assert beam[0] == pytest.approx(surface["direct"], rel=1e-5, abs=1e-3), what
            diffuse = both[0] - beam[0]
            assert diffuse == pytest.approx(surface["diffuse"], rel=0.025), what
            if surface["tilt"] == 0.0:
                assert both[0] == pytest.approx(row["ghi"], rel=1e-5, abs=1e-3), what


def test_sky_rays_converge_to_the_isotropic_view() -> None:
    """The sky rays' cosine sum against (1 + cos tilt) / 2, for every aspect."""
    view = load("clearsky.json")["sky_view"]
    for count, bound in ((64, 0.013), (256, 0.0035), (1024, 0.001)):
        sky = sh.sky_directions(count)
        for tilt, exact in zip(view["tilt"], view["isotropic"], strict=True):
            for aspect in range(0, 360, 15):
                cos = sky @ normal(tilt, aspect)
                got = np.sum(np.where(cos > 0.0, cos, 0.0)) * 2.0 / count
                assert abs(got - exact) < bound, (count, tilt, aspect, got, exact)


def test_insolation_against_grass_rsun_is_a_model_difference() -> None:
    """GRASS r.sun (ESRA, Muneer diffuse) on the same planes: a different model, bounded.

    Pinned as the validation document reports them, so a change in either
    shows: global irradiation on open ground 4 to 14 % below ESRA, beam on
    every surface with more than 0.5 kWh/m² between -40 % and +2 %, and the
    hours of direct sun within 0.6 h except where r.sun's one declination a
    day decides (a vertical wall facing the equator's equinox sun).
    """
    rows = load("rsun.json")["cases"]
    days = {row["site"]: row for row in load("clearsky.json")["days"]}
    for row in rows:
        day = {**days[row["site"]], "day": row["day"]}
        if needs_database(day):
            continue
        direct, _ = _insolation(day, 0)
        n = normal(row["tilt"], row["aspect"])[None, :]
        hours, beam = sh.accumulate(np.ones((1, len(direct)), bool), n, direct)
        what = (row["site"], row["surface"])
        if row["beam"] > 0.5:
            assert -0.40 < beam[0] / row["beam"] - 1.0 < 0.02, what
        if row["tilt"] == 0.0:
            diffuse = direct.stats["diffuse_horizontal_kwh_m2"]
            assert -0.14 < (beam[0] + diffuse) / row["global"] - 1.0 < -0.04, what
        if not (row["site"] == "equator equinox" and row["tilt"] == 90.0):
            assert abs(hours[0] - row["insolation_hours"]) < 0.6, what
