"""Terrain validation, the pure-Python half (docs/validation/terrain.md).

The Blender half (``tests/blender/test_validation_terrain.py``) runs the
presets. Here: the closed forms the validation relies on, the conventions of
the committed reference outputs (GDAL 3.12, WhiteboxTools 2.4, rvt-py 2.2.1),
the numpy model of the horizon bisection both preset engines follow, and the
sign of the convergence that Aspect (True North) adds.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from geotools.analysis import preset_bake, site
from geotools.core.placement import Placement, PlacementMode

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "blender"))
import _terrain_surfaces as ts  # shared with the Blender suite

REFERENCES = REPO / "tests" / "data" / "validation" / "terrain" / "references.npz"


@pytest.fixture(scope="module")
def refs() -> dict[str, np.ndarray]:
    with np.load(REFERENCES) as data:
        out = {k: data[k] for k in data.files}
    out["meta"] = json.loads(str(out["meta"]))
    return out


# --- the closed forms ----------------------------------------------------------------------------


def test_waves_derivatives_match_finite_differences() -> None:
    rng = np.random.default_rng(1)
    x, y = rng.uniform(-6, 6, 200), rng.uniform(-6, 6, 200)
    d = ts.waves_derivatives(x, y)
    e = 1e-4
    f = ts.waves
    np.testing.assert_allclose(d["p"], (f(x + e, y) - f(x - e, y)) / (2 * e), atol=1e-6)
    np.testing.assert_allclose(d["q"], (f(x, y + e) - f(x, y - e)) / (2 * e), atol=1e-6)
    e = 1e-3
    np.testing.assert_allclose(d["r"], (f(x + e, y) - 2 * f(x, y) + f(x - e, y)) / e**2, atol=1e-4)
    np.testing.assert_allclose(d["t"], (f(x, y + e) - 2 * f(x, y) + f(x, y - e)) / e**2, atol=1e-4)
    s = (f(x + e, y + e) - f(x + e, y - e) - f(x - e, y + e) + f(x - e, y - e)) / (4 * e * e)
    np.testing.assert_allclose(d["s"], s, atol=1e-4)


def test_disc_mean_matches_quadrature() -> None:
    # Polar quadrature over the annulus (Gauss-Legendre in r, uniform in angle).
    nodes, weights = np.polynomial.legendre.leggauss(40)
    for inner, outer in ((0.0, 0.5), (0.3, 0.8), (0.0, 2.0)):
        r = inner + (outer - inner) * (nodes + 1) / 2
        w = weights * (outer - inner) / 2 * r
        a = np.linspace(0, 2 * math.pi, 256, endpoint=False)
        px, py = 0.37, -1.2
        z = ts.waves(px + r[:, None] * np.sin(a), py + r[:, None] * np.cos(a))
        mean = (w[:, None] * z).sum() * (2 * math.pi / 256) / (math.pi * (outer**2 - inner**2))
        assert abs(ts.waves_disc_mean(px, py, inner, outer) - mean) < 1e-9


def test_curvature_formulas_on_a_sphere_and_a_paraboloid() -> None:
    radius = 5.0
    x, y = np.array([0.3, -1.1, 2.0]), np.array([0.4, 0.9, -1.5])
    z = np.sqrt(radius**2 - x**2 - y**2)
    p, q = -x / z, -y / z
    r = -(radius**2 - y**2) / z**3
    t = -(radius**2 - x**2) / z**3
    s = -x * y / z**3
    for kind, value in ts.curvatures(p, q, r, s, t).items():
        np.testing.assert_allclose(value, 1 / radius, rtol=1e-12, err_msg=kind)
    # z = c rho^2: profile -2c / (1 + g)^1.5 and plan -2c / (1 + g)^0.5 (surface of revolution).
    c = 0.25
    k = ts.curvatures(2 * c * x, 2 * c * y, 2 * c, 0.0, 2 * c)
    g = 4 * c * c * (x**2 + y**2)
    np.testing.assert_allclose(k["profile"], -2 * c / (1 + g) ** 1.5, rtol=1e-12)
    np.testing.assert_allclose(k["plan"], -2 * c / np.sqrt(1 + g), rtol=1e-12)
    np.testing.assert_allclose(k["mean"], (k["profile"] + k["plan"]) / 2, rtol=1e-12)


def test_sunflower_is_the_presets_pattern() -> None:
    dx, dy = ts.sunflower(24, 0.0, 0.5)
    assert len(dx) == 24
    np.testing.assert_allclose(dx[:12], -dx[12:])  # point-symmetric pairs
    # Even areal density: the mean squared radius of a disc's samples is R^2 / 2.
    assert abs(np.mean(dx**2 + dy**2) - 0.5**2 / 2) < 1e-12
    assert len(ts.sunflower(25, 0.0, 1.0)[0]) == 26  # an odd count is rounded up


# --- the reference outputs and their conventions -----------------------------------------------------


def _window_mean(z: np.ndarray, radius: int) -> np.ndarray:
    """Mean over a (2m+1)^2 square, edge-replicated (rvt-py's mean filter)."""
    pad = np.pad(z, radius, mode="edge")
    total = np.zeros_like(z)
    size = 2 * radius + 1
    for di in range(size):
        for dj in range(size):
            total += pad[di : di + z.shape[0], dj : dj + z.shape[1]]
    return total / size**2


def test_reference_conventions(refs) -> None:
    """What each committed reference computes, recomputed here from the same DEM."""
    meta = refs["meta"]
    assert (meta["n"], meta["h"]) == (ts.N, ts.H)
    assert meta["versions"]["rvt_py"] == "2.2.1"
    assert "3.12.0" in meta["versions"]["gdal"] and "v2.4.0" in meta["versions"]["whitebox_tools"]
    z = ts.waves(*ts.grid_xy()).astype(np.float32).astype(np.float64)
    a, b, _ = meta["local_crop"]
    c = (slice(a, b),) * 2
    inner = (slice(3, -3),) * 2
    diffs = np.stack(
        [
            np.roll(z, (-di, -dj), (0, 1)) - z
            for di in (-1, 0, 1)
            for dj in (-1, 0, 1)
            if (di, dj) != (0, 0)
        ]
    )
    riley = np.sqrt((diffs**2).sum(0))[c][inner]
    np.testing.assert_allclose(refs["gdal_tri_riley"][inner], riley, atol=1e-6)
    np.testing.assert_allclose(
        refs["gdal_tri_wilson"][inner], np.abs(diffs).mean(0)[c][inner], atol=1e-6
    )
    # WhiteboxTools' RuggednessIndex is the root mean square: Riley / sqrt(8).
    np.testing.assert_allclose(refs["wbt_ruggedness_index"][inner], riley / math.sqrt(8), atol=1e-6)
    np.testing.assert_allclose(refs["gdal_tpi"][inner], -diffs.mean(0)[c][inner], atol=1e-6)
    # rvt-py's simple LRM: z - mean of a (2r+1)^2 square.
    r = meta["slrm_radius_cells"]
    hs, he, step = meta["horizon_crop"]
    slrm = (z - _window_mean(z, r))[hs:he:step, hs:he:step]
    np.testing.assert_allclose(refs["rvt_slrm"], slrm, atol=1e-5)

    # Curvature: WhiteboxTools' profile, tangential and mean curvature share
    # GeoTools' sign (+ convex) and unit (1/m); its plan curvature is the
    # contour curvature (GeoTools' plan curvature / sin(slope)).
    d = {k: v[c] for k, v in ts.waves_derivatives(*ts.grid_xy()).items()}
    truth = ts.curvatures(**d)
    g = d["p"] ** 2 + d["q"] ** 2
    contour = truth["plan"] * np.sqrt(1 + g) / np.sqrt(g)
    for key, value in (
        ("wbt_profile_curvature", truth["profile"]),
        ("wbt_tangential_curvature", truth["plan"]),
        ("wbt_mean_curvature", truth["mean"]),
    ):
        error = (refs[key] - value)[inner]
        assert np.sqrt(np.mean(error**2)) < 0.15 * np.sqrt(np.mean(value[inner] ** 2)), key
    # (The contour curvature diverges where the slope vanishes, hence medians.)
    error = np.median(np.abs(refs["wbt_plan_curvature"] - contour)[inner])
    flipped = np.median(np.abs(refs["wbt_plan_curvature"] + contour)[inner])
    assert error < 0.15 * np.median(np.abs(contour[inner])) < flipped, (error, flipped)
    # Slope and aspect: degrees; aspect clockwise from north, the downslope direction.
    slope, aspect = ts.slope_aspect(d["p"], d["q"])
    for key in ("gdal_slope_horn", "gdal_slope_zt", "wbt_slope"):
        assert np.median(np.abs(refs[key] - slope)[inner]) < 0.6, key
    for key in ("gdal_aspect_horn", "gdal_aspect_zt", "wbt_aspect"):
        error = ts.angle_difference(refs[key], aspect)[inner][slope[inner] > 5]
        assert np.median(np.abs(error)) < 1.0, key


# --- the horizon bisection ---------------------------------------------------------------------------


@pytest.mark.parametrize("preset", ["sky_view_factor", "openness_positive", "openness_negative"])
def test_bisection_brackets_the_horizon(preset: str) -> None:
    """Both engines' bisection: within half a bracket of the true horizon, per direction."""
    search = preset_bake.horizon_search(preset, {})
    rng = np.random.default_rng(7)
    count = 2000
    lowest = 0.0 if preset == "sky_view_factor" else -89.9
    true = rng.uniform(lowest, 89.9, (search.directions, count))
    if preset == "sky_view_factor":
        true[:, :200] = -rng.uniform(0, 89, (search.directions, 200))  # below level: counts 0

    def hits(d: int, theta: np.ndarray) -> np.ndarray:
        # From above, a ray hits iff the horizon is at least its angle; from
        # below (negative openness) iff the lowest point is at most its angle.
        return true[d] <= theta if search.below else true[d] >= theta

    angle_sum, sine_sum = preset_bake.bisect_horizon(search, hits, count)
    half = (90.0 - search.lower) / 2 ** (search.steps - (0 if search.lower < 0 else 1)) / 2
    clipped = np.maximum(true, 0.0) if preset == "sky_view_factor" else true
    mean_angle = angle_sum / search.directions
    assert np.all(np.abs(mean_angle - clipped.mean(0)) <= half + 1e-9)
    values = preset_bake.horizon_values(preset, angle_sum, sine_sum, search.directions)
    exact = {
        "sky_view_factor": 1 - np.mean(np.sin(np.radians(clipped)), 0),
        "openness_positive": 90 - clipped.mean(0),
        "openness_negative": 90 + clipped.mean(0),
    }[preset]
    tolerance = math.sin(math.radians(half)) if preset == "sky_view_factor" else half
    assert np.all(np.abs(values - exact) <= tolerance + 1e-9)
    if preset == "sky_view_factor":
        # A horizon below level: the level ray misses and the estimate is exactly 0.
        level = np.where(true.max(0) < 0)[0]
        assert len(level) and np.all(values[level] == 1.0)


def test_bisection_on_a_level_plane() -> None:
    """The documented level-plane values: SVF 1, openness 90 deg plus half a bracket."""
    for preset, expected in (
        ("sky_view_factor", 1.0),
        ("openness_positive", 90.0 + 90.0 / 2**8),
        ("openness_negative", 90.0 + 90.0 / 2**8),
    ):
        search = preset_bake.horizon_search(preset, {})
        below = search.below

        def hits(_d: int, theta: np.ndarray, below: bool = below) -> np.ndarray:
            # The level ray, Ray Offset above (below) the plane, misses it.
            return (theta > 0) if below else (theta < 0)

        angle_sum, sine_sum = preset_bake.bisect_horizon(search, hits, 1)
        value = preset_bake.horizon_values(preset, angle_sum, sine_sum, search.directions)[0]
        assert value == pytest.approx(expected, abs=1e-12), preset


# --- true north --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crs", "lon", "lat"),
    [
        ("EPSG:32633", 17.0, 60.0),  # UTM 33N, east of the central meridian (15 E)
        ("EPSG:32633", 12.5, 45.0),  # west of it
        ("EPSG:7856", 150.0, -30.0),  # MGA zone 56 (153 E), southern hemisphere, west
        ("EPSG:7856", 155.5, -36.0),  # east
        ("EPSG:3031", 60.0, -75.0),  # Antarctic polar stereographic
        ("EPSG:3413", -45.0 + 30.0, 80.0),  # NSIDC north polar stereographic (central -45)
    ],
)
def test_convergence_turns_grid_aspect_to_true(crs: str, lon: float, lat: float) -> None:
    """true = grid + convergence, checked against the direction of the meridian itself.

    Project a point and one a little north of it along its meridian; true north
    has grid bearing atan2(dE, dN), and a face whose true aspect is A has grid
    aspect A - convergence, so convergence = -(grid bearing of true north).
    """
    pyproj = pytest.importorskip("pyproj")
    forward = pyproj.Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    e0, n0 = forward.transform(lon, lat)
    e1, n1 = forward.transform(lon, lat + 1e-6)
    expected = -math.degrees(math.atan2(e1 - e0, n1 - n0))
    placement = Placement(PlacementMode.SOURCE_CRS, source_crs=crs, source_offset=(e0, n0, 0.0))
    where = site.sun_site(placement)
    assert where.convergence == pytest.approx(expected, abs=1e-4), (crs, where.convergence)
    # The preset's arithmetic: a face facing grid azimuth A faces true A + convergence.
    grid_aspect = (0.0 - where.convergence) % 360  # a face facing true north
    assert ts.angle_difference(grid_aspect + where.convergence, 0.0) == pytest.approx(0, abs=1e-9)
