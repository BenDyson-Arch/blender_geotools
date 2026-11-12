"""Fast (bake) paths of the horizon and cumulative viewshed presets, without Blender.

The bisection model (``bisect_horizon``) is what the Geometry Nodes preset
and the bake both run; here it is checked on analytic horizons: a level plane
(SVF 1, openness 90° within half the Steps resolution) and the bottom of a
V-trench with 30° walls (SVF 0.6714; 0.6733 at Steps 8).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from geotools.analysis import preset_bake as fast
from geotools.analysis import presets
from geotools.core.layers import Domain, Origin, validate

RESOLUTION = 90.0 / 2 ** (8 - 1)  # Steps 8


def test_fast_presets_and_their_inputs() -> None:
    assert fast.FAST == {
        "sky_view_factor",
        "openness_positive",
        "openness_negative",
        "viewshed_cumulative",
    }
    assert not fast.is_fast("slope") and fast.is_fast("viewshed_cumulative")
    for preset in fast.HORIZON:
        assert fast.input_names(preset) == ("Max Distance", "Directions", "Steps", "Ray Offset")
    assert fast.input_names("viewshed_cumulative") == (
        "Observer Height",
        "Target Height",
        "Max Distance",
        "Ray Offset",
        "Drape",
    )
    assert fast.default_inputs("sky_view_factor") == {
        "Max Distance": 2.0,
        "Directions": 16,
        "Steps": 8,
        "Ray Offset": 0.002,
    }


def test_inputs_are_checked() -> None:
    values = fast.check_inputs("openness_positive", {"Directions": 8.0})
    assert values["Directions"] == 8 and isinstance(values["Directions"], int)
    assert values["Max Distance"] == 2.0
    with pytest.raises(ValueError, match="at most 64"):
        fast.check_inputs("sky_view_factor", {"Directions": 65})
    with pytest.raises(ValueError, match="at least"):
        fast.check_inputs("sky_view_factor", {"Max Distance": 0.0})
    with pytest.raises(ValueError, match="finite"):
        fast.check_inputs("viewshed_cumulative", {"Observer Height": math.nan})
    with pytest.raises(ValueError, match="no input"):
        fast.check_inputs("sky_view_factor", {"Observers": 1})


def test_azimuths_start_at_plus_y_and_turn_clockwise() -> None:
    azimuths = fast.azimuth_vectors(16)
    np.testing.assert_allclose(azimuths[0], (0.0, 1.0, 0.0), atol=1e-15)
    np.testing.assert_allclose(azimuths[4], (1.0, 0.0, 0.0), atol=1e-15)  # 90°: east
    np.testing.assert_allclose(np.linalg.norm(azimuths, axis=1), 1.0)
    assert np.all(azimuths[:, 2] == 0.0)


def _search(preset: str, **inputs) -> fast.HorizonSearch:
    return fast.horizon_search(preset, inputs)


def test_searches_follow_the_presets() -> None:
    svf = _search("sky_view_factor")
    assert (svf.below, svf.lower, svf.side) == (False, 0.0, 0.002)
    negative = _search("openness_negative", **{"Ray Offset": 0.01})
    assert (negative.below, negative.lower, negative.side) == (True, -90.0, -0.01)
    assert _search("openness_positive").lower == -90.0


def _values(preset: str, horizon_deg: np.ndarray) -> np.ndarray:
    """The preset's value for one sample whose surface in direction d is at ``horizon_deg[d]``.

    Above the surface a ray at theta hits when theta < horizon; from just below
    it (negative openness) a ray crosses the surface when theta > horizon.
    """
    search = _search(preset)

    def hits(d: int, theta: np.ndarray) -> np.ndarray:
        if search.below:
            return theta > horizon_deg[d]
        return theta < horizon_deg[d]

    angle, sine = fast.bisect_horizon(search, hits, 1)
    return fast.horizon_values(preset, angle, sine, search.directions)


def test_a_level_plane() -> None:
    level = np.zeros(16)
    assert _values("sky_view_factor", level)[0] == 1.0
    # A level ray misses, so the bracket closes on [-0.70°, 0]: 90° + 0.35°.
    for preset in ("openness_positive", "openness_negative"):
        value = _values(preset, level)[0]
        assert value == pytest.approx(90.0 + RESOLUTION / 2), preset


def test_the_bottom_of_a_v_trench() -> None:
    azimuths = np.arange(16) * 2 * math.pi / 16
    horizon = np.degrees(np.arctan(np.abs(np.sin(azimuths)) * math.tan(math.radians(30.0))))
    exact = 1.0 - np.mean(np.sin(np.radians(horizon)))
    assert exact == pytest.approx(0.6714, abs=1e-4)
    svf = _values("sky_view_factor", horizon)[0]
    assert abs(svf - exact) < 0.006
    # 0.6733 at Steps 8; the meshes give 0.6737, their rays starting Ray Offset up.
    assert svf == pytest.approx(0.6733, abs=5e-5)
    positive = _values("openness_positive", horizon)[0]
    negative = _values("openness_negative", horizon)[0]
    mean = float(np.mean(horizon))
    assert abs(positive - (90.0 - mean)) < RESOLUTION
    assert abs(negative - (90.0 + mean)) < RESOLUTION
    assert positive == pytest.approx(70.71, abs=0.01)  # the meshes: 70.75


def test_values_from_sums() -> None:
    angle, sine = np.array([-8.0, 16.0]), np.array([0.0, 4.0])
    np.testing.assert_allclose(fast.horizon_values("sky_view_factor", angle, sine, 16), [1, 0.75])
    np.testing.assert_allclose(
        fast.horizon_values("openness_positive", angle, sine, 16), [90.5, 89]
    )
    np.testing.assert_allclose(
        fast.horizon_values("openness_negative", angle, sine, 16), [89.5, 91]
    )
    with pytest.raises(ValueError):
        fast.horizon_values("viewshed_cumulative", angle, sine, 16)


def test_eyes_and_view_points() -> None:
    points = np.array([[1.0, 2.0, 3.0], [500_000.25, 7_000_000.5, 10.0]])
    eyes = fast.eyes(points, 1.6)
    assert eyes.dtype == np.float64
    np.testing.assert_array_equal(eyes[:, :2], points[:, :2])
    np.testing.assert_allclose(eyes[:, 2], points[:, 2] + 1.6)
    assert points[0, 2] == 3.0  # not changed in place
    starts = fast.view_points(points, np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0]]), 0.01, 0.5)
    np.testing.assert_allclose(starts, [[1.0, 2.0, 3.51], [500_000.26, 7_000_000.5, 10.5]])


def test_the_layer_and_its_provenance() -> None:
    spec = fast.layer_spec(
        "sky_view_factor",
        inputs=fast.default_inputs("sky_view_factor"),
        objects=("Site",),
        run={"samples": 10, "rays_per_sample": 128, "bakes": 1},
        geotools_version="0.3.0-dev",
    )
    live = presets.layer_spec(presets.get("sky_view_factor"))
    assert spec.name == "sky_view_factor" and spec.domain is Domain.CORNER
    assert spec.origin is Origin.ANALYSIS and live.origin is Origin.GEONODES
    assert (spec.units, spec.aggregation, spec.data_type) == (
        live.units,
        live.aggregation,
        live.data_type,
    )
    assert spec.label == "Sky-View Factor (Fast)"
    record = spec.provenance
    assert record["tool"] == fast.TOOL == "analysis_bake"
    parameters = record["parameters"]
    assert parameters["preset"] == "sky_view_factor" and parameters["preset_version"] == 1
    assert parameters["engine"] == "bake" and "Cycles Raycast" in parameters["method"]
    assert "bisection" in parameters["method"]
    assert parameters["inputs"]["Steps"] == 8 and parameters["samples"] == 10
    assert not [i for i in validate(spec) if i.severity.name == "ERROR"]
    # A fast layer is not a live preset's, and the other way round.
    assert fast.fast_preset_of(record) == "sky_view_factor"
    assert presets.preset_of(record) is None
    assert fast.fast_preset_of(live.provenance) is None
    viewshed = fast.layer_spec("viewshed_cumulative", "seen_from_barrows")
    assert viewshed.domain is Domain.FACE and viewshed.units == "count"
    assert "segment" in viewshed.provenance["parameters"]["method"]
    assert "Occluders" in viewshed.provenance["parameters"]["occluders"]
    with pytest.raises(ValueError, match="no fast path"):
        fast.layer_spec("slope")
