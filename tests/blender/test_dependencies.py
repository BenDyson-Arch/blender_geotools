"""The rusty-tiles and pyproj status matches what the build bundled."""

from __future__ import annotations

import importlib.util
import sys

import _harness as h
import bpy


def _expected_summary() -> str:
    if h.CONFIG.expect == "available":
        return f"rusty-tiles {h.CONFIG.expect_version}"
    return "rusty-tiles not available: rusty_tiles is not installed"


def test_status_matches_expectation() -> None:
    deps = h.submodule("core.deps")
    deps.reset_cache()
    status = deps.probe_rusty_tiles()
    print(f"    status: {status.summary}")
    if h.CONFIG.expect == "available":
        assert status.available, status
        assert status.version == h.CONFIG.expect_version, (
            f"bundled rusty-tiles is {status.version}, expected {h.CONFIG.expect_version}"
        )
        assert status.error is None
    else:
        assert not status.available, status
        assert status.version is None
        assert status.error == "rusty_tiles is not installed", status.error
    assert status.summary == _expected_summary()


def test_rusty_tiles_comes_from_the_extension() -> None:
    spec = importlib.util.find_spec("rusty_tiles")
    if h.CONFIG.expect == "missing":
        assert spec is None, f"rusty_tiles is importable from {spec.origin}"
        return
    assert spec is not None and spec.origin
    # Blender installs bundled wheels into <extensions>/.local/lib/pythonX.Y/site-packages.
    assert h.is_within(spec.origin, h.extensions_root()), (
        f"rusty_tiles loaded from {spec.origin}, not from the installed extension wheels"
    )
    module = sys.modules.get("rusty_tiles") or importlib.import_module("rusty_tiles")
    assert module.__name__ == "rusty_tiles"


def test_operator_runs() -> None:
    assert bpy.ops.geotools.check_dependencies() == {"FINISHED"}
    status = h.submodule("core.deps").probe_rusty_tiles()
    assert status.available == (h.CONFIG.expect == "available"), status


def test_operator_reports_status() -> None:
    recorder = h.ReportRecorder()
    result = h.operator_class().execute(recorder, bpy.context)
    assert result == {"FINISHED"}
    level = "INFO" if h.CONFIG.expect == "available" else "WARNING"
    assert recorder.reports == [
        (frozenset({level}), _expected_summary()),
        (frozenset({level}), _expected_pyproj_summary()),
    ], recorder.reports


def test_panel_shows_status() -> None:
    labels = list(h.labels(h.draw(h.panel_class())))
    if h.CONFIG.expect == "available":
        assert (f"rusty-tiles {h.CONFIG.expect_version}", "CHECKMARK") in labels, labels
        assert (_expected_pyproj_summary(), "CHECKMARK") in labels, labels
        assert h.draw_header(h.panel_class()) == "CHECKMARK"
    else:
        assert ("rusty-tiles not available", "ERROR") in labels, labels
        assert ("rusty_tiles is not installed", "NONE") in labels, labels
        assert ("pyproj not available", "ERROR") in labels, labels
        assert ("pyproj is not installed", "NONE") in labels, labels
        assert ("Imports cannot reproject", "NONE") in labels, labels
        assert h.draw_header(h.panel_class()) == "CANCEL"


# --- pyproj ---------------------------------------------------------------------------


def _expected_pyproj_summary() -> str:
    if h.CONFIG.expect == "available":
        status = h.submodule("core.deps").probe_pyproj()
        return f"pyproj {h.CONFIG.expect_pyproj_version} (PROJ {status.proj_version})"
    return "pyproj not available: pyproj is not installed"


def test_pyproj_status_matches_expectation() -> None:
    deps = h.submodule("core.deps")
    deps.reset_cache()
    status = deps.probe_pyproj()
    print(f"    status: {status.summary}, data {status.data_dir}")
    if h.CONFIG.expect == "available":
        assert status.available, status
        assert status.version == h.CONFIG.expect_pyproj_version, (
            f"bundled pyproj is {status.version}, expected {h.CONFIG.expect_pyproj_version}"
        )
        assert status.proj_version and status.data_dir
        # The PROJ data (proj.db) inside the bundled wheel, not a system PROJ.
        assert h.is_within(status.data_dir, h.extensions_root()), status.data_dir
    else:
        assert not status.available, status
        assert status.error == "pyproj is not installed", status.error
    assert status.summary == _expected_pyproj_summary()


def test_pyproj_comes_from_the_extension() -> None:
    spec = importlib.util.find_spec("pyproj")
    if h.CONFIG.expect == "missing":
        assert spec is None, f"pyproj is importable from {spec.origin}"
        return
    assert spec is not None and spec.origin
    assert h.is_within(spec.origin, h.extensions_root()), (
        f"pyproj loaded from {spec.origin}, not from the installed extension wheels"
    )
    import pyproj

    assert pyproj.__version__ == h.CONFIG.expect_pyproj_version


def test_reprojection_in_blender() -> None:
    import numpy as np

    reproject = h.submodule("core.reproject")
    points = np.array([[153.0251, -27.4698, 10.0]])
    if h.CONFIG.expect == "missing":
        assert reproject.available() is False
        try:
            reproject.reproject_points(points, "EPSG:4326", "EPSG:7856")
        except reproject.ReprojectionUnavailable as exc:
            assert "pyproj is not installed" in str(exc), exc
        else:
            raise AssertionError("reproject_points worked without pyproj")
        return
    assert reproject.available() is True
    out, report = reproject.reproject_points(points, "EPSG:4326", "EPSG:7856")
    # Reference: Krüger series on GRS80 (see tests/core/test_reproject.py).
    assert abs(out[0, 0] - 502479.8690) < 1e-3 and abs(out[0, 1] - 6961528.0930) < 1e-3, out
    assert out[0, 2] == 10.0
    assert "Map Grid of Australia zone 56" in report.operation, report
    assert report.network is False
