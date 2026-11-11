"""Tests for geotools.core.reproject.

Most tests need pyproj and are skipped without it (CI's core job installs the
pinned version from [tool.geotools.wheels] in pyproject.toml).

Reference values are computed independently of PROJ, with the 6th-order Krüger
series for the transverse Mercator projection on the GRS80 ellipsoid
(:func:`_mga`), the definition MGA2020 uses (k0 = 0.9996, false easting 500 km,
false northing 10 000 km). It agrees with PROJ's own algorithm to well under a
micrometre inside a zone:

- Brisbane, 153.0251 E, 27.4698 S (WGS84, taken as GDA2020 with EPSG's null
  shift "GDA2020 to WGS 84 (2)") -> MGA2020 zone 56:
  E 502479.8690 m, N 6961528.0930 m.
- 150.5 E, 27.4698 S in zone 55: E 845922.8959, N 6956650.6765;
  in zone 56: E 252955.7549, N 6959041.1638.
- GDA94 -> GDA2020 has no closed-form reference here; EPSG:8048 "GDA94 to
  GDA2020 (1)" (a 7-parameter Helmert, 0.01 m) moves points in south-east
  Queensland about 1.5 m north-east, which the test checks along with the
  operation and its accuracy, and a regression value to the millimetre.
"""

from __future__ import annotations

import importlib.util
import math
import time

import numpy as np
import pytest

from geotools.core import deps, reproject

HAS_PYPROJ = importlib.util.find_spec("pyproj") is not None
needs_pyproj = pytest.mark.skipif(not HAS_PYPROJ, reason="pyproj is not installed")

BRISBANE = (153.0251, -27.4698)


def _mga(lon: float, lat: float, zone: int) -> tuple[float, float]:
    """Krüger 6th-order transverse Mercator, GRS80, MGA zone ``zone`` (southern)."""
    a, f = 6378137.0, 1 / 298.257222101
    k0, fe, fn = 0.9996, 500000.0, 10000000.0
    lon0 = 6 * zone - 183
    n = f / (2 - f)
    e = 2 * math.sqrt(n) / (1 + n)
    big_a = a / (1 + n) * (1 + n**2 / 4 + n**4 / 64 + n**6 / 256)
    alpha = (
        n / 2 - 2 * n**2 / 3 + 5 * n**3 / 16 + 41 * n**4 / 180 - 127 * n**5 / 288
        + 7891 * n**6 / 37800,
        13 * n**2 / 48 - 3 * n**3 / 5 + 557 * n**4 / 1440 + 281 * n**5 / 630
        - 1983433 * n**6 / 1935360,
        61 * n**3 / 240 - 103 * n**4 / 140 + 15061 * n**5 / 26880 + 167603 * n**6 / 181440,
        49561 * n**4 / 161280 - 179 * n**5 / 168 + 6601661 * n**6 / 7257600,
        34729 * n**5 / 80640 - 3418889 * n**6 / 1995840,
        212378941 * n**6 / 319334400,
    )  # fmt: skip
    phi = math.radians(lat)
    lam = math.radians(lon - lon0)
    t = math.sinh(math.atanh(math.sin(phi)) - e * math.atanh(e * math.sin(phi)))
    xi1 = math.atan2(t, math.cos(lam))
    eta1 = math.atanh(math.sin(lam) / math.sqrt(1 + t * t))
    xi = xi1 + sum(
        al * math.sin(2 * j * xi1) * math.cosh(2 * j * eta1) for j, al in enumerate(alpha, 1)
    )
    eta = eta1 + sum(
        al * math.cos(2 * j * xi1) * math.sinh(2 * j * eta1) for j, al in enumerate(alpha, 1)
    )
    return fe + k0 * big_a * eta, fn + k0 * big_a * xi


@pytest.fixture(autouse=True)
def _fresh_probe():
    deps.reset_cache()
    yield
    deps.reset_cache()


# --- without pyproj ----------------------------------------------------------------


def test_unavailable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    missing = deps.DependencyStatus(False, error="pyproj is not installed", name="pyproj")
    monkeypatch.setattr(deps, "probe_pyproj", lambda: missing)

    assert reproject.available() is False
    with pytest.raises(reproject.ReprojectionUnavailable, match="pyproj is not installed"):
        reproject.reproject_points(np.zeros((1, 3)), "EPSG:4326", "EPSG:7856")


def test_format_accuracy() -> None:
    assert reproject.format_accuracy(0.01) == "0.01 m"
    assert reproject.format_accuracy(None) == "unknown"


# --- with pyproj --------------------------------------------------------------------


@needs_pyproj
def test_available() -> None:
    assert reproject.available() is True


@needs_pyproj
def test_wgs84_to_mga2020_zone56_brisbane() -> None:
    points = np.array([[*BRISBANE, 10.0]])
    before = points.copy()

    out, report = reproject.reproject_points(points, "EPSG:4326", "EPSG:7856")

    ref_e, ref_n = _mga(*BRISBANE, zone=56)
    assert ref_e == pytest.approx(502479.8690, abs=1e-4)
    assert ref_n == pytest.approx(6961528.0930, abs=1e-4)
    np.testing.assert_allclose(out[0], [ref_e, ref_n, 10.0], rtol=0, atol=1e-6)
    np.testing.assert_array_equal(points, before)  # the input is not changed
    assert out.dtype == np.float64 and out.shape == (1, 3)

    assert report.source == "EPSG:4326" and report.target == "EPSG:7856"
    assert "GDA2020 to WGS 84 (2)" in report.operation  # EPSG's null shift, 3 m
    assert "Map Grid of Australia zone 56" in report.operation
    assert report.accuracy_m == 3.0
    assert report.network is False
    assert any("No vertical transform" in note for note in report.notes)


@needs_pyproj
def test_mga_zone55_to_zone56() -> None:
    e55, n55 = _mga(150.5, -27.4698, zone=55)
    e56, n56 = _mga(150.5, -27.4698, zone=56)

    out, report = reproject.reproject_points(np.array([[e55, n55, 42.0]]), "EPSG:7855", "EPSG:7856")

    np.testing.assert_allclose(out[0], [e56, n56, 42.0], rtol=0, atol=1e-6)
    assert report.operation == (
        "Inverse of Map Grid of Australia zone 55 + Map Grid of Australia zone 56"
    )
    assert report.accuracy_m == 0.0  # a conversion: exact


@needs_pyproj
def test_gda94_to_gda2020_uses_the_direct_operation() -> None:
    points = np.array([[502000.0, 6961000.0, 5.0]])

    out, report = reproject.reproject_points(points, "EPSG:28356", "EPSG:7856")

    assert "GDA94 to GDA2020 (1)" in report.operation
    assert "WGS 84" not in report.operation  # direct, not through WGS84
    assert report.accuracy_m == pytest.approx(0.01)
    de, dn = out[0, 0] - points[0, 0], out[0, 1] - points[0, 1]
    assert de > 0 and dn > 0 and 1.4 < math.hypot(de, dn) < 1.7  # ~1.5 m north-east
    np.testing.assert_allclose(out[0], [502000.5964, 6961001.3906, 5.0], rtol=0, atol=1e-3)


@needs_pyproj
def test_network_is_off_by_default_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    import pyproj
    import pyproj.network

    seen: list[bool] = []
    original = pyproj.Transformer.from_crs

    def recording(*args, **kwargs):
        seen.append(pyproj.network.is_network_enabled())
        return original(*args, **kwargs)

    monkeypatch.setattr(pyproj.Transformer, "from_crs", staticmethod(recording))
    previous = pyproj.network.is_network_enabled()
    try:
        pyproj.network.set_network_enabled(active=True)
        _, report = reproject.reproject_points(np.array([BRISBANE]), "EPSG:4326", "EPSG:7856")
        assert seen and not any(seen)
        assert report.network is False
        assert pyproj.network.is_network_enabled() is True

        seen.clear()
        pyproj.network.set_network_enabled(active=False)
        _, report = reproject.reproject_points(
            np.array([BRISBANE]), "EPSG:4326", "EPSG:7856", allow_network=True
        )
        assert seen[0] is True
        assert report.network is True
        assert pyproj.network.is_network_enabled() is False

        # Restored after a failure inside the transform as well.
        pyproj.network.set_network_enabled(active=True)
        with pytest.raises(reproject.ReprojectionError):
            reproject.reproject_points(np.array([BRISBANE[::-1]]), "EPSG:4326", "EPSG:7856")
        assert pyproj.network.is_network_enabled() is True
    finally:
        pyproj.network.set_network_enabled(active=previous)


@needs_pyproj
def test_ballpark_is_detected() -> None:
    # PROJ knows no NAD27 -> GDA2020 shift: it falls back to a ballpark offset.
    _, report = reproject.reproject_points(np.array([[*BRISBANE, 3.0]]), "EPSG:4267", "EPSG:7856")

    assert "ballpark" in report.operation.lower()
    assert report.accuracy_m is None
    assert any(note.startswith("Ballpark transformation") for note in report.notes)


@needs_pyproj
def test_points_outside_the_area_of_use_are_noted() -> None:
    # Northing 11 000 km in zone 56 is about 9 N, outside Australia.
    points = np.array([[502000.0, 6961000.0], [502000.0, 11000000.0]])

    _, report = reproject.reproject_points(points, "EPSG:28356", "EPSG:7856")

    assert any("1 of 2 points lie outside the area of use" in n for n in report.notes)


@needs_pyproj
@pytest.mark.parametrize(
    ("source", "target", "match"),
    [
        ("EPSG:999999", "EPSG:7856", "unknown source CRS 'EPSG:999999'"),
        ("EPSG:4326", "+proj=nonsense", "unknown target CRS"),
        ("", "EPSG:7856", "no source CRS"),
    ],
)
def test_bad_crs(source: str, target: str, match: str) -> None:
    with pytest.raises(reproject.ReprojectionError, match=match):
        reproject.reproject_points(np.zeros((1, 2)), source, target)


@needs_pyproj
def test_bad_input() -> None:
    with pytest.raises(reproject.ReprojectionError, match=r"shape \(N, 2\) or \(N, 3\)"):
        reproject.reproject_points(np.zeros((3, 4)), "EPSG:4326", "EPSG:7856")
    with pytest.raises(reproject.ReprojectionError, match="1 of 2 input points are not finite"):
        reproject.reproject_points(np.array([[0.0, 0.0], [np.nan, 0.0]]), "EPSG:4326", "EPSG:7856")


@needs_pyproj
def test_non_finite_output() -> None:
    # Latitude and longitude swapped: latitude 153 has no position.
    with pytest.raises(
        reproject.ReprojectionError,
        match=r"1 of 2 points could not be transformed .* first is point 1 .* in the source CRS",
    ):
        reproject.reproject_points(np.array([BRISBANE, BRISBANE[::-1]]), "EPSG:4326", "EPSG:7856")


@needs_pyproj
def test_2d_input() -> None:
    out, report = reproject.reproject_points(np.array([BRISBANE]), "EPSG:4326", "EPSG:7856")

    assert out.shape == (1, 2)
    np.testing.assert_allclose(out[0], _mga(*BRISBANE, zone=56), rtol=0, atol=1e-6)
    assert not any("vertical" in note.lower() for note in report.notes)


@needs_pyproj
def test_z_is_transformed_when_both_crs_are_3d() -> None:
    # WGS84 3D (lon, lat, ellipsoidal height) -> geocentric XYZ.
    out, report = reproject.reproject_points(
        np.array([[0.0, 0.0, 100.0]]), "EPSG:4979", "EPSG:4978"
    )

    np.testing.assert_allclose(out[0], [6378237.0, 0.0, 0.0], rtol=0, atol=1e-6)
    assert not any("No vertical transform" in note for note in report.notes)


@needs_pyproj
def test_wkt_and_proj_strings_are_accepted() -> None:
    import pyproj

    wkt = pyproj.CRS.from_epsg(7856).to_wkt()

    out, _ = reproject.reproject_points(np.array([BRISBANE]), "EPSG:7844", wkt)

    np.testing.assert_allclose(out[0], _mga(*BRISBANE, zone=56), rtol=0, atol=1e-6)


@needs_pyproj
def test_one_million_points_are_vectorised(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    import pyproj

    calls: list[int] = []
    original = pyproj.Transformer.transform

    def counting(self, *args, **kwargs):
        calls.append(np.size(args[0]))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pyproj.Transformer, "transform", counting)
    rng = np.random.default_rng(7)
    n = 1_000_000
    points = np.column_stack(
        [rng.uniform(152.9, 153.1, n), rng.uniform(-27.6, -27.4, n), rng.uniform(0, 50, n)]
    )

    started = time.perf_counter()
    out, report = reproject.reproject_points(points, "EPSG:4326", "EPSG:7856")
    elapsed = time.perf_counter() - started

    # One bulk call, one for the centre point, one bulk call for the area check.
    assert len(calls) <= 3, calls
    assert sum(1 for size in calls if size == n) <= 2
    assert out.shape == (n, 3) and np.isfinite(out).all()
    np.testing.assert_array_equal(out[:, 2], points[:, 2])
    i = 12345
    np.testing.assert_allclose(out[i, :2], _mga(*points[i, :2], zone=56), rtol=0, atol=1e-6)
    assert "GDA2020 to WGS 84 (2)" in report.operation
    assert elapsed < 10.0
    with capsys.disabled():
        print(f"\n    reproject_points: {n:,} points EPSG:4326 -> EPSG:7856 in {elapsed:.2f} s")
