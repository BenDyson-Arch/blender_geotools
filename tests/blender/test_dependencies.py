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


# --- updated while Blender is running (issue 134) ---------------------------------------


def test_register_recorded_what_this_blender_loaded() -> None:
    deps = h.submodule("core.deps")
    state = deps._session()
    assert state["version"] == h.submodule("blender.common").extension_version(), state
    manifest = str(h.submodule("core.manifest").MANIFEST_PATH)
    assert [i[0] for i in state["files"]["geotools"]] == [manifest], state["files"]
    if h.CONFIG.expect == "available":
        native = [i[0] for i in state["files"]["rusty_tiles"]]
        assert any("rusty_tiles" in p and h.is_within(p, h.extensions_root()) for p in native)
    # Nothing changed on disk in this run: no restart notice.
    assert not deps.check_for_update(h.submodule("blender.common").extension_version())
    assert deps.RESTART_MESSAGE.split()[0] not in h.label_text(h.draw(h.panel_class()))


def test_a_changed_library_shows_the_restart_notice() -> None:
    """Fake a library whose file on disk is not the one loaded: every place says restart."""
    import copy

    deps = h.submodule("core.deps")
    state = deps._session()
    saved = copy.deepcopy(state)
    manifest = str(h.submodule("core.manifest").MANIFEST_PATH)
    try:
        state["files"]["fakelib"] = [(manifest, -1, 0, 0)]  # another inode than the file's
        assert not deps.update_pending()  # compared only by About, Check Dependencies, Checks

        calls = h.draw(h.panel_class())  # About compares
        text = h.label_text(calls)
        assert deps.RESTART_MESSAGE in text, text
        first = next(t for t, i in h.labels(calls) if t.startswith("GeoTools was updated"))
        assert (first, "ERROR") in h.labels(calls), calls
        assert h.operator_text("geotools.copy_details") is not None
        if h.CONFIG.expect == "available":
            assert (
                h.draw_header(h.panel_class()) == h.submodule("blender.ui").STATUS_ICONS["WARNING"]
            )

        recorder = h.ReportRecorder()
        assert h.operator_class().execute(recorder, bpy.context) == {"FINISHED"}
        assert (frozenset({"WARNING"}), deps.RESTART_MESSAGE) in recorder.reports

        checks = h.submodule("blender.checks").update_issues()
        assert [(i.severity, i.code, i.message) for i in checks] == [
            ("warning", "restart-after-update", deps.RESTART_MESSAGE)
        ]

        # PROJ's database errors now say restart, with PROJ's text only in Copy Details.
        raw = "proj_create: SQLite error [ code = 10, msg = disk I/O error ] on SELECT"
        assert deps.proj_error_message(raw) == deps.RESTART_MESSAGE
        assert bpy.ops.geotools.copy_details() == {"FINISHED"}
        details = deps.details_text(h.submodule("blender.common").extension_version())
        assert f"Restart needed: fakelib: {manifest} changed on disk" in details, details
        assert f"PROJ error: {raw}" in details, details
    finally:
        state.clear()
        state.update(saved)
    assert not deps.update_pending()
    assert deps.RESTART_MESSAGE not in h.label_text(h.draw(h.panel_class()))
