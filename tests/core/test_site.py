"""Scene points on Earth: coordinates, convergence and scale (geotools.analysis.site)."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from geotools.analysis import site, sun
from geotools.core import deps
from geotools.core.crs_migration import PRESET_CODES
from geotools.core.placement import Placement, PlacementMode

pyproj = pytest.importorskip("pyproj")

MGA56_PRESET = next(text for text, code in PRESET_CODES.items() if code == "EPSG:7856")
# The west edge of MGA zone 56 (central meridian 153 E) at 30 S: the convergence is
# about 3 x sin(30) = 1.5 degrees, the largest MGA has at that latitude.
EDGE_LON, EDGE_LAT = 150.0, -30.0


def _grid(lon: float, lat: float) -> tuple[float, float]:
    return pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True).transform(lon, lat)


def _crs_placement(
    lon: float, lat: float, a: float = 0.0, height_offset: float = 0.0, crs: str | None = None
) -> Placement:
    east, north = _grid(lon, lat)
    return Placement(
        PlacementMode.SOURCE_CRS,
        source_crs=crs or "EPSG:7856",
        source_offset=(east, north, a),
        height_offset=height_offset,
    )


def test_crs_input_prefers_the_epsg_code() -> None:
    assert site.crs_input("EPSG:7856") == "EPSG:7856"
    assert site.crs_input(MGA56_PRESET) == "EPSG:7856"  # a saved preset, not migrated yet
    assert site.crs_input(" epsg:32756 ") == "EPSG:32756"
    proj = "+proj=utm +zone=56 +south +ellps=GRS80 +units=m +no_defs"
    assert site.crs_input(f" {proj} ") == proj


def test_convergence_at_an_mga_zone_edge() -> None:
    placement = _crs_placement(EDGE_LON, EDGE_LAT)
    reading = site.read_point(placement, (0.0, 0.0, 0.0))
    assert reading.longitude == pytest.approx(EDGE_LON, abs=1e-9)
    assert reading.latitude == pytest.approx(EDGE_LAT, abs=1e-9)
    assert reading.geodetic_crs == "GDA2020"
    # Magnitude and sign: west of the central meridian in the southern hemisphere,
    # grid north lies clockwise (east) of true north.
    assert 1.45 < reading.convergence < 1.55
    # Independently: the grid bearing of a step due (true) north is minus the convergence.
    to_grid = pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True)
    e0, n0 = to_grid.transform(EDGE_LON, EDGE_LAT)
    e1, n1 = to_grid.transform(EDGE_LON, EDGE_LAT + 1e-6)
    grid_bearing_of_north = math.degrees(math.atan2(e1 - e0, n1 - n0))
    assert reading.convergence == pytest.approx(-grid_bearing_of_north, abs=1e-6)
    # And it flips east of the central meridian.
    east_edge = site.read_point(_crs_placement(155.99, EDGE_LAT), (0.0, 0.0, 0.0))
    assert -1.55 < east_edge.convergence < -1.45


def test_sun_azimuth_in_grid_terms() -> None:
    """A direction at true azimuth A runs at grid bearing A - convergence."""
    placement = _crs_placement(EDGE_LON, EDGE_LAT)
    where = site.sun_site(placement)
    assert where.north == "grid north"
    assert where.convergence == pytest.approx(1.5, abs=0.05)
    geod = pyproj.Geod(ellps="GRS80")
    to_grid = pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True)
    e0, n0 = to_grid.transform(where.longitude, where.latitude)
    for true_azimuth in (30.0, 135.0, 260.0):
        lon, lat, _ = geod.fwd(where.longitude, where.latitude, true_azimuth, 10.0)
        e1, n1 = to_grid.transform(lon, lat)
        grid_bearing = math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360.0
        assert grid_bearing == pytest.approx(true_azimuth - where.convergence, abs=1e-4)


def test_scale_factor_and_ground_distance() -> None:
    height = 600.0
    placement = _crs_placement(EDGE_LON, EDGE_LAT, a=height)
    reading = site.read_point(placement, (0.0, 0.0, 0.0))
    factors = pyproj.Proj("EPSG:7856").get_factors(EDGE_LON, EDGE_LAT)
    assert reading.grid_scale == pytest.approx(factors.meridional_scale, rel=1e-12)
    assert 1.0006 < reading.grid_scale < 1.0009  # k0 0.9996 grows to about 1.0007 at 3 degrees
    assert reading.height == height
    assert reading.height_factor == pytest.approx(1.0 - height / 6.37e6, abs=2e-7)

    # A 1 km line at 600 m: compare the ground distance with the ellipsoid distance
    # (pyproj.Geod) raised to the line's height.
    a, b = (0.0, 0.0, 0.0), (700.0, -700.0, 5.0)
    m = site.measure(placement, a, b)
    assert m.grid_distance == pytest.approx(math.hypot(700.0, 700.0), abs=1e-9)
    inverse = pyproj.Transformer.from_crs("EPSG:7856", "EPSG:7844", always_xy=True)
    east, north, _ = placement.source_offset
    lon_a, lat_a = inverse.transform(east + a[0], north + a[1])
    lon_b, lat_b = inverse.transform(east + b[0], north + b[1])
    _, _, ellipsoid = pyproj.Geod(ellps="GRS80").inv(lon_a, lat_a, lon_b, lat_b)
    radius = site._gaussian_radius(6378137.0, 1 / 298.257222101, np.array([EDGE_LAT]))[0]
    expected_ground = ellipsoid * (radius + height + 2.5) / radius
    assert m.ground_distance == pytest.approx(expected_ground, rel=1e-7)  # 0.1 mm per km
    assert m.combined_scale == pytest.approx(m.grid_scale * m.height_factor, rel=1e-15)
    assert m.height_difference == 5.0
    assert m.slope_distance == pytest.approx(math.hypot(m.ground_distance, 5.0))
    assert m.grid_bearing == pytest.approx(135.0)
    middle = site.read_point(placement, (350.0, -350.0, 2.5))
    assert m.true_bearing == pytest.approx(135.0 + middle.convergence, abs=1e-9)


def test_reading_precision() -> None:
    east, north = _grid(153.0, -27.5)
    placement = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs="EPSG:7856",
        source_offset=(east, north, 10.0),
        height_offset=40.25,
    )
    local = (1234.567, -89.1, 3.3)
    reading = site.read_point(placement, local)
    held = np.asarray(local, dtype=np.float32).astype(np.float64)
    # The local position is its float32 value; the offsets add in binary64.
    assert reading.local == tuple(held)
    assert reading.east == east + held[0]
    assert reading.north == north + held[1]
    assert reading.level == 10.0 + held[2]
    assert reading.height == reading.level + 40.25
    assert reading.resolution == pytest.approx(1.22e-4, rel=0.01)  # float32 spacing at 1234 m
    assert site.float32_resolution((100_000.0, 0.0, 0.0)) == pytest.approx(0.0078125)
    text = reading.as_text()
    assert f"E\t{reading.east!r}" in text and "Latitude (GDA2020)" in text


def test_saved_preset_and_epsg_code_agree() -> None:
    by_preset = site.read_point(_crs_placement(152.1, -28.0, crs=MGA56_PRESET), (5.0, 6.0, 0.0))
    by_code = site.read_point(_crs_placement(152.1, -28.0, crs="EPSG:7856"), (5.0, 6.0, 0.0))
    assert by_preset == by_code


def test_face_areas() -> None:
    placement = _crs_placement(EDGE_LON, EDGE_LAT, a=100.0)
    square = np.array(
        [[[0, 0, 0], [10, 0, 0], [10, 10, 0]], [[0, 0, 0], [10, 10, 0], [0, 10, 0]]],
        dtype=np.float64,
    )
    flat = site.face_areas(placement, square)
    csf = site.read_point(placement, (5.0, 5.0, 0.0)).combined_scale
    assert flat.triangles == 2
    assert flat.grid_plan == pytest.approx(100.0)
    assert flat.grid_surface == pytest.approx(100.0)
    assert flat.ground_plan == pytest.approx(100.0 / csf**2, rel=1e-9)
    assert flat.ground_surface == pytest.approx(flat.ground_plan, rel=1e-12)
    wall = np.array([[[0, 0, 0], [10, 0, 0], [10, 0, 10]]], dtype=np.float64)
    vertical = site.face_areas(placement, wall)
    assert vertical.grid_plan == pytest.approx(0.0, abs=1e-12)
    wall_csf = site.read_point(placement, tuple(wall[0].mean(axis=0))).combined_scale
    assert vertical.ground_surface == pytest.approx(50.0 / wall_csf, rel=1e-6)  # only x is scaled
    assert site.face_areas(placement, np.empty((0, 3, 3))).triangles == 0


def test_anchor_frame() -> None:
    lon, lat, height = 153.02, -27.47, 25.0
    placement = Placement(
        PlacementMode.ANCHOR,
        anchor=(lon, lat, height),
        heading_degrees=90.0,
        scene_offset=(10.0, 20.0, 5.0),
    )
    # The offset moves the scene before the heading turns it: local (-10, -20, -5)
    # is the anchor itself, and local +X runs north.
    origin = site.read_point(placement, (-10.0, -20.0, -5.0))
    assert (origin.east, origin.north, origin.level) == (0.0, 0.0, 0.0)
    assert origin.longitude == pytest.approx(lon, abs=1e-10)
    assert origin.latitude == pytest.approx(lat, abs=1e-10)
    assert origin.height == pytest.approx(height, abs=1e-6)
    north = site.read_point(placement, (90.0, -20.0, -5.0))  # 100 m along +X
    assert north.north == pytest.approx(100.0) and north.east == pytest.approx(0.0, abs=1e-12)
    bearing, _, distance = pyproj.Geod(ellps="WGS84").inv(lon, lat, north.longitude, north.latitude)
    assert bearing == pytest.approx(0.0, abs=1e-6)
    assert distance == pytest.approx(100.0, abs=1e-3)
    assert north.convergence == -90.0
    assert north.combined_scale == 1.0

    m = site.measure(placement, (0.0, 0.0, 0.0), (0.0, 50.0, 0.0))  # along local +Y: west
    assert m.ground_distance == m.grid_distance == 50.0
    assert m.true_bearing == pytest.approx(270.0)

    where = site.sun_site(placement)
    assert (where.longitude, where.latitude, where.convergence) == (lon, lat, -90.0)


def test_local_frame() -> None:
    placement = Placement()
    reading = site.read_point(placement, (1.0, 2.0, 3.0))
    assert (reading.east, reading.north, reading.level) == (1.0, 2.0, 3.0)
    assert reading.latitude is None and reading.height is None
    assert site.measure(placement, (0.0, 0.0, 0.0), (3.0, 4.0, 0.0)).ground_distance == 5.0
    where = site.sun_site(placement, local_lon_lat=(-0.12, 51.5))
    assert (where.latitude, where.convergence, where.north) == (51.5, 0.0, "true north")
    with pytest.raises(site.SiteError):
        site.sun_site(placement)


def test_geographic_crs_cannot_measure() -> None:
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:4326", source_offset=(153.0, -27.5, 0.0)
    )
    where = site.sun_site(placement)
    assert (where.longitude, where.latitude, where.convergence) == (153.0, -27.5, 0.0)
    with pytest.raises(site.SiteError, match="geographic"):
        site.measure(placement, (0.0, 0.0, 0.0), (0.001, 0.0, 0.0))


def test_bad_crs_and_points() -> None:
    with pytest.raises(site.SiteError, match="does not know"):
        site.read_point(Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:999999"), (0, 0, 0))
    far = Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:7856", source_offset=(1e12, 0, 0))
    with pytest.raises(site.SiteError):
        site.read_point(far, (0.0, 0.0, 0.0))


def test_without_pyproj(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        deps, "probe_pyproj", lambda: deps.DependencyStatus(False, error="pyproj is not installed")
    )
    site._grid.cache_clear()
    site._ecef_to_wgs84.cache_clear()
    try:
        assert not site.available()
        with pytest.raises(site.GeodesyUnavailable, match="pyproj"):
            site.read_point(Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:7856"), (0, 0, 0))
        anchor = Placement(PlacementMode.ANCHOR, anchor=(153.0, -27.0, 0.0))
        with pytest.raises(site.GeodesyUnavailable):
            site.read_point(anchor, (0.0, 0.0, 0.0))
        # Without latitude and longitude an anchor point still reads, and the sun works.
        assert site.read_point(anchor, (1.0, 2.0, 3.0), geodetic=False).north == 2.0
        assert site.sun_site(anchor).latitude == -27.0
    finally:
        monkeypatch.undo()
        site._grid.cache_clear()
        site._ecef_to_wgs84.cache_clear()


def test_sun_through_the_site_end_to_end() -> None:
    """The NREL example site placed in a UTM project: grid azimuth = true - convergence."""
    lon, lat = -105.1786, 39.742476
    east, north = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32613", always_xy=True).transform(
        lon, lat
    )
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:32613", source_offset=(east, north, 1830.14)
    )
    where = site.sun_site(placement)
    moment = dt.datetime(2003, 10, 17, 19, 30, 30, tzinfo=dt.UTC).timestamp()
    position = sun.solar_position(
        moment,
        where.latitude,
        where.longitude,
        where.height,
        pressure_hpa=820,
        temperature_c=11,
        delta_t_seconds=67,
    )
    assert position.azimuth[0] == pytest.approx(194.34024, abs=1e-5)
    # Zone 13's meridian is 105 W, so the site is just west of it in the north: small, negative.
    assert -0.15 < where.convergence < 0.0
    scene = (position.azimuth[0] - where.convergence) % 360.0
    assert scene == pytest.approx(194.34024 - where.convergence, abs=1e-5)
