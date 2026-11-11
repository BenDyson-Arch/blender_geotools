"""The rusty-tiles status matches what the build bundled."""

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
    assert recorder.reports == [(frozenset({level}), _expected_summary())], recorder.reports


def test_panel_shows_status() -> None:
    labels = list(h.labels(h.draw(h.panel_class())))
    if h.CONFIG.expect == "available":
        assert (f"rusty-tiles {h.CONFIG.expect_version}", "CHECKMARK") in labels, labels
    else:
        assert ("rusty-tiles not available", "ERROR") in labels, labels
        assert ("rusty_tiles is not installed", "NONE") in labels, labels
