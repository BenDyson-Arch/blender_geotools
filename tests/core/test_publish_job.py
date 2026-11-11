"""The publish job (geotools.publish.job), run without Blender.

Tests marked ``rusty`` need the rusty-tiles wheel (the scratch venv has it);
the others substitute a fake rusty-tiles module.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from geotools.core import crs_presets
from geotools.core.facemap import load_face_map
from geotools.core.gltf_writer import MaterialData, PrimitiveData, SceneExport
from geotools.core.layers import DataType, Domain, LayerSpec, from_record, to_record
from geotools.publish import job as job_mod
from geotools.publish import staging as st
from geotools.publish.job import (
    RECORD_SCHEMA,
    BuildJob,
    BuildRequest,
    JobState,
    ProgressEvent,
)

try:
    import rusty_tiles
except ImportError:  # pragma: no cover - depends on the environment
    rusty_tiles = None

rusty = pytest.mark.skipif(rusty_tiles is None, reason="needs the rusty-tiles wheel")

ANCHOR = {"anchor": (153.02, -27.47, 10.0)}


def grid_scene(n: int = 8) -> SceneExport:
    """An n x n grid of quads (2 n^2 triangles) in one primitive, Blender Z-up."""
    xs, ys = np.meshgrid(np.arange(n + 1, dtype=np.float32), np.arange(n + 1, dtype=np.float32))
    positions = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size, np.float32)], axis=1)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    b, c, d = a + 1, a + n + 1, a + n + 2
    tris = np.concatenate([np.stack([a, b, d], 1), np.stack([a, d, c], 1)]).astype(np.uint32)
    t = tris.shape[0]
    return SceneExport(
        object_names=("Grid",),
        materials=(MaterialData(name="Grey", base_color_factor=(0.5, 0.5, 0.5, 1.0)),),
        images=(),
        primitives=(
            PrimitiveData(
                material=0,
                positions=positions,
                indices=tris,
                source_object=np.zeros(t, np.int32),
                source_polygon=(np.arange(t) // 2).astype(np.int32),
            ),
        ),
    )


def request(output: Path, **kwargs: object) -> BuildRequest:
    base: dict[str, object] = {
        "output": output,
        "placement_kwargs": dict(ANCHOR),
        "topology": {"Grid": "0" * 64},
        "extra_record": {"geotools_version": "test", "build_profile": "Test"},
    }
    base.update(kwargs)
    return BuildRequest(**base)  # type: ignore[arg-type]


def run(job: BuildJob, timeout: float = 120.0) -> list[ProgressEvent]:
    job.start()
    assert job.join(timeout), "job did not finish"
    return job.poll_events()


def fake_rt(**overrides: object) -> SimpleNamespace:
    """A rusty-tiles stand-in whose mesh_to_tileset writes a dummy archive."""

    def mesh_to_tileset(input, output, *, callback=None, force=False, **_kwargs):
        assert Path(input).is_file()
        if callback:
            callback({"event": "progress", "phase": "capture", "done": 0, "total": None})
            callback({"event": "warning", "message": "something odd"})
            callback({"event": "log", "message": "phase capture done"})
        Path(output).write_bytes(b"new archive")
        report = {"triangles": 128, "tiles": 1, "leaves": 1, "max_depth": 0}
        return SimpleNamespace(report=report, diagnostics={"phase_seconds": {}}, output=output)

    def validate(path, **_kwargs):
        return {"ok": True, "tiles": 1, "checks": ["a", "b"], "payloads": [{"uri": "x"}]}

    namespace = SimpleNamespace(mesh_to_tileset=mesh_to_tileset, validate=validate)
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


# --- staging and lock ------------------------------------------------------------------


def test_staging_layout(tmp_path: Path) -> None:
    out = tmp_path / "site.3tz"
    assert job_mod.staging_dir_for(out) == tmp_path / "site.geotools"
    assert st.images_dir_for(out) == tmp_path / "site.geotools" / "images"
    with pytest.raises(ValueError):
        st.staging_dir_for(tmp_path / "site.glb")


def test_lock_is_exclusive(tmp_path: Path) -> None:
    out = tmp_path / "site.3tz"
    lock = st.StagingLock.acquire(out)
    try:
        with pytest.raises(st.StagingBusyError):
            st.StagingLock.acquire(out)
    finally:
        lock.release()
    lock.release()  # idempotent
    with st.StagingLock.acquire(out) as again:
        assert again.held
    assert not again.held


def test_busy_staging_fails_without_touching_it(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(job_mod, "_rusty_tiles", fake_rt)
    out = tmp_path / "site.3tz"
    lock = st.StagingLock.acquire(out)
    images = st.images_dir_for(out)
    images.mkdir()
    (images / "keep.png").write_bytes(b"x")
    try:
        job = BuildJob(grid_scene(), request(out))
        run(job)
        assert job.state is JobState.FAILED
        assert job.error.startswith("StagingBusyError:"), job.error
        assert (images / "keep.png").exists()
        assert not out.exists()
    finally:
        lock.release()


# --- events ------------------------------------------------------------------------------


def test_event_queue_coalesces_progress() -> None:
    q = job_mod._EventQueue()
    for done in range(1000):
        q.put(ProgressEvent(JobState.TILING, phase="leaf_build", done=done, total=1000))
    q.put(ProgressEvent(JobState.TILING, phase="leaf_build", message="a log line"))
    q.put(ProgressEvent(JobState.TILING, phase="archive", done=0, total=1))
    events = q.drain()
    assert [e.done for e in events] == [999, None, 0]
    assert q.drain() == []


def test_poll_events_does_not_block_while_running(tmp_path: Path, monkeypatch) -> None:
    release = threading.Event()

    def slow(input, output, *, callback=None, **_kwargs):
        callback({"event": "progress", "phase": "capture", "done": 0, "total": 1})
        release.wait(10)
        raise RuntimeError("stopped by the test")

    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(mesh_to_tileset=slow))
    job = BuildJob(grid_scene(), request(tmp_path / "site.3tz"))
    job.start()
    try:
        deadline = time.monotonic() + 10
        while job.state is not JobState.TILING and time.monotonic() < deadline:
            t = time.perf_counter()
            job.poll_events()
            assert time.perf_counter() - t < 0.05
            time.sleep(0.01)
        assert job.state is JobState.TILING
    finally:
        release.set()
        assert job.join(10)
    assert job.state is JobState.FAILED
    assert job.error == "RuntimeError: stopped by the test"


# --- with a fake rusty-tiles -----------------------------------------------------------


def test_failure_maps_to_failed_and_keeps_work(tmp_path: Path, monkeypatch) -> None:
    class DataError(Exception):
        pass

    def broken(input, output, **_kwargs):
        raise DataError("bad source")

    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(mesh_to_tileset=broken))
    out = tmp_path / "site.3tz"
    job = BuildJob(grid_scene(), request(out))
    events = run(job)
    assert job.state is JobState.FAILED
    assert job.error == "DataError: bad source"
    assert job.result is None
    assert events[-1].state is JobState.FAILED and events[-1].message == job.error
    staging = st.staging_dir_for(out)
    assert not out.exists()
    assert not (staging / "build.json").exists()
    # The failed attempt stays in work/ for debugging; scratch does not.
    assert (staging / "work" / "source.glb").is_file()
    assert "DataError: bad source" in (staging / "work" / "error.txt").read_text()
    assert not (staging / "work" / "scratch").exists()
    # The lock was released: another job can start.
    st.StagingLock.acquire(out).release()


def test_writer_exception_is_failed(tmp_path: Path, monkeypatch) -> None:
    def broken_writer(scene, path):
        raise ValueError("no triangles")

    monkeypatch.setattr(job_mod, "write_glb", broken_writer)
    monkeypatch.setattr(job_mod, "_rusty_tiles", fake_rt)
    job = BuildJob(grid_scene(), request(tmp_path / "site.3tz"))
    run(job)
    assert job.state is JobState.FAILED
    assert job.error == "ValueError: no triangles"


def test_cancel_during_writing(tmp_path: Path, monkeypatch) -> None:
    called = []
    real_writer = job_mod.write_glb
    holder: dict[str, BuildJob] = {}

    def cancelling_writer(scene, path):
        holder["job"].cancel()
        return real_writer(scene, path)

    def must_not_run(*_args, **_kwargs):
        called.append(True)

    monkeypatch.setattr(job_mod, "write_glb", cancelling_writer)
    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(mesh_to_tileset=must_not_run))
    out = tmp_path / "site.3tz"
    holder["job"] = job = BuildJob(grid_scene(), request(out))
    run(job)
    assert job.state is JobState.CANCELLED
    assert job.error is None
    assert not called
    assert not (st.staging_dir_for(out) / "work").exists()


def test_cancel_before_start(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(job_mod, "_rusty_tiles", fake_rt)
    job = BuildJob(grid_scene(), request(tmp_path / "site.3tz"))
    job.cancel()
    job.cancel()
    run(job)
    assert job.state is JobState.CANCELLED


def test_validation_failure_is_recorded_not_failed(tmp_path: Path, monkeypatch) -> None:
    class DataError(Exception):
        pass

    def bad_validate(path, **_kwargs):
        raise DataError("payload is broken")

    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: fake_rt(validate=bad_validate))
    out = tmp_path / "site.3tz"
    job = BuildJob(grid_scene(), request(out))
    events = run(job)
    assert job.state is JobState.DONE
    assert job.result.validation_ok is False
    assert "validation FAILED" in events[-1].message
    record = json.loads((st.staging_dir_for(out) / "build.json").read_text())
    assert record["validation"] == {
        "requested": True,
        "ok": False,
        "error": "DataError: payload is broken",
    }


def test_events_and_warnings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(job_mod, "_rusty_tiles", fake_rt)
    out = tmp_path / "site.3tz"
    job = BuildJob(grid_scene(), request(out))
    events = run(job)
    states = [e.state for e in events if e.message and e.phase is None]
    assert states[:3] == [JobState.WRITING, JobState.TILING, JobState.VALIDATING]
    assert events[-1].state is JobState.DONE
    messages = [e.message for e in events if e.message]
    assert "Warning: something odd" in messages
    assert "phase capture done" in messages
    record = json.loads((st.staging_dir_for(out) / "build.json").read_text())
    assert record["warnings"] == {"count": 1, "first": ["something odd"]}
    validation = json.loads((st.staging_dir_for(out) / "validation.json").read_text())
    assert "payloads" not in validation and validation["payload_rows"] == 1
    assert record["schema"] == RECORD_SCHEMA == 2
    assert record["layers"] == []


def test_layers_are_recorded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(job_mod, "_rusty_tiles", fake_rt)
    specs = [
        LayerSpec("slope", Domain.FACE, DataType.FLOAT, units="deg", nodata=-9999.0),
        LayerSpec("cover", Domain.CORNER, DataType.CATEGORY, categories=("a", "b"), nodata=-1),
        LayerSpec("lit", Domain.TEXTURE, DataType.BOOLEAN),
    ]
    out = tmp_path / "site.3tz"
    job = BuildJob(grid_scene(), request(out, layers=[to_record(s) for s in specs]))
    run(job)
    assert job.state is JobState.DONE, job.error
    record = json.loads((st.staging_dir_for(out) / "build.json").read_text())
    assert record["layers"] == [to_record(s) for s in specs]
    assert [from_record(r).name for r in record["layers"]] == ["slope", "cover", "lit"]
    assert record["layers"][0]["attribute"] == "gt_slope"


def test_source_hashes_and_crs_epsg(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(job_mod, "_rusty_tiles", fake_rt)
    blend = tmp_path / "site.blend"
    blend.write_bytes(b"BLENDER-v502" + bytes(range(256)) * 5000)
    preset = crs_presets.PRESETS[0]
    placement = {
        "source_crs": preset.definition,
        "source_axes": "y-up",
        "source_offset": (500000.0, 6961000.0, 10.0),
        "height_offset": 0.0,
    }
    out = tmp_path / "site.3tz"
    job = BuildJob(grid_scene(), request(out, blend_path=blend, placement_kwargs=placement))
    run(job)
    assert job.state is JobState.DONE, job.error
    staging = st.staging_dir_for(out)
    record = json.loads((staging / "build.json").read_text())
    glb = hashlib.sha256((staging / "source.glb").read_bytes()).hexdigest()
    assert record["source"] == {
        "glb_sha256": glb,
        "blend_sha256": hashlib.sha256(blend.read_bytes()).hexdigest(),
    }
    assert record["source"]["glb_sha256"] == job.result.glb_sha256
    assert record["placement"]["crs_epsg"] == preset.epsg
    assert record["placement"]["source_crs"] == preset.definition  # the fallback, unchanged
    assert "hash_blend" in record["phase_seconds"]

    # An unreadable .blend is recorded as null, not a failed build.
    out2 = tmp_path / "other.3tz"
    job = BuildJob(grid_scene(), request(out2, blend_path=tmp_path / "gone.blend"))
    run(job)
    assert job.state is JobState.DONE, job.error
    record = json.loads((st.staging_dir_for(out2) / "build.json").read_text())
    assert record["source"]["blend_sha256"] is None


# --- with rusty-tiles ----------------------------------------------------------------------


@rusty
def test_publish_success_and_record(tmp_path: Path) -> None:
    out = tmp_path / "site.3tz"
    images = st.images_dir_for(out)
    images.mkdir(parents=True)
    (images / "leftover.png").write_bytes(b"x")
    job = BuildJob(grid_scene(), request(out, profile="fast", jobs=2))
    events = run(job)
    assert job.state is JobState.DONE, job.error
    assert job.error is None
    result = job.result
    staging = st.staging_dir_for(out)
    assert result.output == out and result.staging_dir == staging
    assert out.is_file()
    assert result.triangles == 128
    assert result.validation_ok is True
    assert result.record_path == staging / "build.json"
    assert rusty_tiles.validate(out)["ok"]

    phases = {e.phase for e in events if e.phase}
    assert {"capture", "leaf_build"} <= phases, phases

    # Staging: promoted files, no work/ or images/ left.
    for name in ("source.glb", "facemap.npz", "report.json", "diagnostics.json", "build.json"):
        assert (staging / name).is_file(), name
    assert not (staging / "work").exists()
    assert not images.exists()

    record = json.loads(result.record_path.read_text())
    assert record["schema"] == 2
    assert record["output"] == str(out)
    assert record["output_bytes"] == out.stat().st_size
    assert record["source_glb"]["sha256"] == result.glb_sha256
    assert record["source_glb"]["triangles"] == 128
    assert record["settings"] == {
        "profile": "fast",
        "jobs": 2,
        "memory_budget_bytes": None,
        "force": False,
        "validate": True,
    }
    assert record["placement"] == {"anchor": [153.02, -27.47, 10.0], "crs_epsg": None}
    assert record["source"] == {"glb_sha256": result.glb_sha256, "blend_sha256": None}
    assert record["rusty_tiles_version"] == "0.4.0"
    assert record["geotools"] == {"geotools_version": "test", "build_profile": "Test"}
    assert record["started_utc"].endswith("Z") and record["finished_utc"].endswith("Z")
    assert set(record["phase_seconds"]) == {"write_glb", "face_map", "tiling", "validation"}
    assert "capture" in record["rusty_tiles_phase_seconds"]
    assert record["face_map"] == "facemap.npz"
    assert record["validation"]["ok"] is True
    assert record["validation"]["summary"]["ok"] is True

    summary = record["report_summary"]
    assert summary == result.report_summary
    for key in (
        "triangles",
        "leaves",
        "tiles",
        "max_depth",
        "root_geometric_error",
        "tileset_geometric_error",
        "lossless_fallback_pages",
        "viewer_mip_fallback_pages",
        "placement",
    ):
        assert key in summary, key
    assert summary["triangles"] == 128
    assert set(summary["texel_ownership"]) >= {"unique_leaves", "shared_leaves"}
    assert set(summary["escalated_pages"]) == {"uastc_level2", "uastc_level3"}
    assert summary["placement"]["kind"] == "wgs84"

    report = json.loads((staging / "report.json").read_text())
    assert report["triangles"] == 128 and "texel_ownership" in report
    face_map = load_face_map(staging / "facemap.npz")
    assert face_map.object_names == ("Grid",)
    assert face_map.lookup(0, 5) == ("Grid", 2)


@rusty
def test_source_crs_summary(tmp_path: Path) -> None:
    out = tmp_path / "site.3tz"
    placement = {
        "source_crs": "EPSG:32756",
        "source_axes": "y-up",
        "source_offset": (500000.0, 6961000.0, 10.0),
        "height_offset": 0.0,
    }
    job = BuildJob(grid_scene(2), request(out, placement_kwargs=placement, validate=False))
    run(job)
    assert job.state is JobState.DONE, job.error
    assert job.result.validation_ok is None
    staging = st.staging_dir_for(out)
    assert not (staging / "validation.json").exists()
    summary = job.result.report_summary
    assert summary["source_crs"]["definition"] == "EPSG:32756"
    assert summary["source_coordinates"] == "source-crs"
    record = json.loads((staging / "build.json").read_text())
    assert record["validation"] == {"requested": False, "ok": None}
    assert "validation" not in record["phase_seconds"]
    assert record["placement"]["source_crs"] == "EPSG:32756"
    assert record["placement"]["crs_epsg"] == "EPSG:32756"


@rusty
def test_cancel_inside_first_progress_callback(tmp_path: Path, monkeypatch) -> None:
    holder: dict[str, BuildJob] = {}
    calls = []

    def mesh_to_tileset(input, output, *, callback, **kwargs):
        def cancelling(event):
            if event.get("event") == "progress" and not calls:
                calls.append(event)
                holder["job"].cancel()
            callback(event)

        return rusty_tiles.mesh_to_tileset(input, output, callback=cancelling, **kwargs)

    monkeypatch.setattr(
        job_mod, "_rusty_tiles", lambda: SimpleNamespace(mesh_to_tileset=mesh_to_tileset)
    )
    out = tmp_path / "site.3tz"
    holder["job"] = job = BuildJob(grid_scene(), request(out))
    events = run(job)
    assert calls, "rusty-tiles sent no progress event"
    assert job.state is JobState.CANCELLED, job.error
    assert job.error is None and job.result is None
    assert events[-1].state is JobState.CANCELLED
    assert not out.exists()
    staging = st.staging_dir_for(out)
    assert not (staging / "work").exists()
    assert not (staging / "build.json").exists()


@rusty
def test_existing_output_needs_force(tmp_path: Path) -> None:
    out = tmp_path / "site.3tz"
    first = BuildJob(grid_scene(), request(out))
    run(first)
    assert first.state is JobState.DONE, first.error
    before = out.read_bytes()
    record_before = (st.staging_dir_for(out) / "build.json").read_bytes()

    second = BuildJob(grid_scene(4), request(out))
    run(second)
    assert second.state is JobState.FAILED
    assert second.error.startswith("OutputExistsError:"), second.error
    assert out.read_bytes() == before
    assert (st.staging_dir_for(out) / "build.json").read_bytes() == record_before

    forced = BuildJob(grid_scene(4), request(out, force=True))
    run(forced)
    assert forced.state is JobState.DONE, forced.error
    assert forced.result.triangles == 32
    record = json.loads((st.staging_dir_for(out) / "build.json").read_text())
    assert record["source_glb"]["triangles"] == 32 and record["settings"]["force"] is True


@rusty
def test_rusty_tiles_output_conflict_is_failed(tmp_path: Path, monkeypatch) -> None:
    """The output appears after the job's own check: rusty-tiles refuses it."""
    out = tmp_path / "site.3tz"

    def racing(input, output, **kwargs):
        Path(output).write_bytes(b"someone else")
        return rusty_tiles.mesh_to_tileset(input, output, **kwargs)

    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: SimpleNamespace(mesh_to_tileset=racing))
    job = BuildJob(grid_scene(), request(out))
    run(job)
    assert job.state is JobState.FAILED
    assert job.error.startswith("OutputExistsError:"), job.error
    assert out.read_bytes() == b"someone else"


@rusty
def test_failed_rebuild_keeps_previous_build(tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "site.3tz"
    first = BuildJob(grid_scene(), request(out))
    run(first)
    assert first.state is JobState.DONE, first.error
    staging = st.staging_dir_for(out)
    snapshot = {p.name: p.read_bytes() for p in staging.iterdir() if p.is_file()}
    archive = out.read_bytes()

    def broken(input, output, **_kwargs):
        raise rusty_tiles.DataError("simulated")

    monkeypatch.setattr(job_mod, "_rusty_tiles", lambda: SimpleNamespace(mesh_to_tileset=broken))
    second = BuildJob(grid_scene(4), request(out, force=True))
    run(second)
    assert second.state is JobState.FAILED
    assert out.read_bytes() == archive
    for name, data in snapshot.items():
        if name != ".lock":
            assert (staging / name).read_bytes() == data, name
