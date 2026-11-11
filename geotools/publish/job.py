"""A publish job: write the GLB, run rusty-tiles, validate, record.

bpy-free. The Blender side extracts a SceneExport on the main thread, then hands
it to a BuildJob, which does everything else on one worker thread. The main
thread drains ``poll_events()`` from a timer. rusty-tiles releases the GIL and
calls ``callback`` from Rust worker threads; the job only queues those events.

A ``.glb`` output (a build profile of output type ``.glb``, M4) is the
metadata GLB instead: the job writes it with the request's ``extension``
(``core.gltf_metadata``) into ``work/``, moves it onto the output, writes the
face map and build.json, and never runs rusty-tiles.

A vector build (a build profile of format Vectors, M8) has no SceneExport:
the request's ``vector`` (``publish.vector.VectorSource``) is written to
``work/vectors.gpkg`` and tiled with ``vector_to_3tz``, reusing the previous
archive when there is one (see ``publish.vector``).

Staging layout, next to the output ``<dir>/<name>.3tz`` (see ``publish.staging``
for the full list):
    <dir>/<name>.geotools/source.glb   the exact rusty-tiles input
    <dir>/<name>.geotools/facemap.npz  see core.facemap
    <dir>/<name>.geotools/build.json   the build record (schema 2)

Threads: the worker thread runs every step. rusty-tiles calls the callback on
its own worker threads; the callback checks the cancel flag, then appends to a
lock-protected event list. Nothing here touches Blender.

Cancel: :meth:`BuildJob.cancel` sets a :class:`threading.Event`. The worker
checks it between steps (the GLB writer has no cancel hook, so a cancel during
the write takes effect when the write returns), and the rusty-tiles callback
raises a private exception from the next event, which aborts the run before
publication. That ends the job CANCELLED, not FAILED. Once rusty-tiles has
installed the archive the output is published and cannot be withdrawn: a cancel
then only skips validation, and the job still records the build and ends DONE.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import shutil
import threading
import time
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import ModuleType
from typing import Any

from ..core import deps
from ..core.crs_presets import epsg_code
from ..core.facemap import build_face_map, save_face_map
from ..core.gltf_writer import SceneExport, write_glb
from . import staging as st
from . import vector as vec
from .staging import StagingLock, check_output, staging_dir_for

__all__ = [
    "RECORD_SCHEMA",
    "BuildJob",
    "BuildRequest",
    "BuildResult",
    "JobState",
    "OutputExistsError",
    "ProgressEvent",
    "staging_dir_for",
    "summarise_report",
]

log = logging.getLogger(__name__)

RECORD_SCHEMA = 2
"""``schema`` of build.json. 2 added ``layers`` (the M3 layer registry),
``source`` (GLB and ``.blend`` hashes), ``placement.crs_epsg`` and ``variant``
(the presented layer of a variant tileset, null for the plain one), and then
``output_type`` (``"3tz"`` or ``"glb"``) and, for a GLB, ``glb``: its file,
extensions used, options and face table cost (M4). Vector builds (M8) have
``output_type`` ``"vector"`` and ``vectors``: the GeoPackage tiled, its tables
and whether the previous archive was reused."""

MAX_RECORDED_WARNINGS = 50


class JobState(StrEnum):
    PENDING = "pending"
    WRITING = "writing"  # source.glb and face map
    TILING = "tiling"  # rusty_tiles.mesh_to_tileset
    VALIDATING = "validating"  # rusty_tiles.validate
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in _FINISHED


_FINISHED = frozenset((JobState.DONE, JobState.FAILED, JobState.CANCELLED))


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    state: JobState
    phase: str | None = None  # rusty-tiles phase name, e.g. "capture", "leaf_build"
    done: int | None = None
    total: int | None = None
    message: str | None = None  # log/warning text


@dataclass(frozen=True, slots=True)
class BuildRequest:
    output: Path  # must end in .3tz, or .glb for the metadata GLB
    placement_kwargs: dict[str, object]  # from Placement.mesh_to_tileset_kwargs()
    profile: str = "balanced"  # fast | balanced | quality | lossless
    jobs: int | None = None  # None = rusty-tiles "auto"
    memory_budget_bytes: int | None = None
    force: bool = False  # replace an existing output
    validate: bool = True
    topology: dict[str, str] = field(default_factory=dict)  # for the face map
    extra_record: dict[str, object] = field(
        default_factory=dict
    )  # e.g. geotools version, profile name
    layers: list[dict[str, object]] = field(
        default_factory=list
    )  # core.layers.to_record of each layer, for build.json
    # The saved .blend the scene came from, hashed on the worker thread; None if
    # unsaved or modified since saving (the file would not match the scene).
    blend_path: Path | None = None
    # A variant tileset's presentation (core.presentation.variant_record); None
    # for the plain tileset.
    variant: dict[str, object] | None = None
    # .glb outputs: the metadata extension (core.gltf_metadata.MetadataExtension)
    # and what build.json records about it (options, extensions, face table cost).
    extension: object | None = None
    glb_record: dict[str, object] = field(default_factory=dict)
    # Vector builds (M8): the features to write and tile (publish.vector.VectorSource).
    vector: vec.VectorSource | None = None

    @property
    def output_type(self) -> str:
        if self.vector is not None:
            return "vector"
        return "glb" if Path(self.output).suffix == ".glb" else "3tz"


@dataclass(frozen=True, slots=True)
class BuildResult:
    output: Path
    staging_dir: Path
    glb_sha256: str
    triangles: int
    report_summary: dict[str, object]  # selected TilerResult.report fields
    validation_ok: bool | None  # None if validation was not requested
    record_path: Path
    seconds: float


class OutputExistsError(FileExistsError):
    """The output exists and the request does not allow replacing it.

    Raised before any work, so a large GLB is not written for nothing.
    rusty-tiles raises its own ``OutputExistsError`` if the output appears
    later; both end the job FAILED with ``OutputExistsError: ...``.
    """


class _Cancelled(Exception):
    """Raised inside the job (and from the rusty-tiles callback) on cancel."""


def _rusty_tiles() -> ModuleType:
    """The rusty-tiles module. A function so tests can substitute it."""
    import rusty_tiles

    return rusty_tiles


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- event queue ---------------------------------------------------------------------


class _EventQueue:
    """Thread-safe event list. A progress event replaces the previous one when
    it is the last queued and has the same phase, so a main thread that drains
    rarely (or never, as in background tests) sees bounded growth."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[ProgressEvent] = []

    def put(self, event: ProgressEvent) -> None:
        with self._lock:
            if (
                event.message is None
                and self._events
                and (last := self._events[-1]).message is None
                and last.phase == event.phase
                and last.state == event.state
                and event.phase is not None
            ):
                self._events[-1] = event
            else:
                self._events.append(event)

    def drain(self) -> list[ProgressEvent]:
        with self._lock:
            events, self._events = self._events, []
        return events


# --- report summary --------------------------------------------------------------------


def _pick(mapping: object, *keys: str) -> dict[str, object]:
    if not isinstance(mapping, Mapping):
        return {}
    return {k: mapping[k] for k in keys if k in mapping}


def summarise_report(report: Mapping[str, Any]) -> dict[str, object]:
    """The TilerResult.report fields that matter to a GeoTools user."""
    summary: dict[str, object] = {
        k: report[k]
        for k in (
            "profile",
            "triangles",
            "leaves",
            "tiles",
            "max_depth",
            "root_geometric_error",
            "tileset_geometric_error",
            "maximum_leaf_geometry_error",
            "lossless_fallback_pages",
            "viewer_mip_fallback_pages",
            "source_coordinates",
            "coordinates",
            "placement",
        )
        if k in report
    }
    summary["texel_ownership"] = _pick(
        report.get("texel_ownership"),
        "unique_leaves",
        "shared_leaves",
        "shared_triangles",
        "unique_proxies",
        "shared_omitted",
    )
    summary["escalated_pages"] = _pick(
        report.get("escalated_pages"), "uastc_level2", "uastc_level3"
    )
    source_crs = report.get("source_crs")
    if isinstance(source_crs, Mapping):
        summary["source_crs"] = _pick(
            source_crs,
            "definition",
            "axes",
            "source_offset",
            "height_offset_metres",
            "operation",
            "origin_degrees_metres",
        )
    return summary


def summarise_validation(report: Mapping[str, Any]) -> dict[str, object]:
    summary = _pick(report, "ok", "tiles", "contentReferences", "entries", "storedBytes")
    checks = report.get("checks")
    if isinstance(checks, list):
        summary["checks"] = len(checks)
    if "notInspected" in report:
        summary["not_inspected"] = list(report["notInspected"])
    return summary


# --- JSON ------------------------------------------------------------------------------


def _jsonable(value: object) -> object:
    """Plain JSON data: paths become strings, non-finite floats ``None``."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    fields = {k: getattr(value, k) for k in ("path", "kind", "message") if hasattr(value, k)}
    if fields:  # e.g. rusty_tiles.CleanupDiagnostic
        return _jsonable(fields)
    try:
        return [_jsonable(v) for v in value]  # type: ignore[attr-defined]
    except TypeError:
        return str(value)


def write_json(path: Path, data: object) -> None:
    """Write JSON atomically (temp file, then rename)."""
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps(_jsonable(data), indent=2, sort_keys=True, allow_nan=False)
    tmp.write_text(text + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --- the job ---------------------------------------------------------------------------


class BuildJob:
    def __init__(
        self,
        scene: SceneExport | None,
        request: BuildRequest,
        *,
        lock: StagingLock | None = None,
    ) -> None:
        """``lock``: a held :class:`StagingLock` for the output, if the caller took
        it before extracting the scene (the Blender side does, so two sessions
        cannot overwrite each other's images). The job releases it when it ends.
        Without one the job takes the lock itself on the worker thread.

        ``scene`` is None for a vector build, whose request carries ``vector``."""
        if scene is None and request.vector is None:
            raise ValueError("a mesh or GLB build needs the scene")
        if request.vector is not None and Path(request.output).suffix != ".3tz":
            raise ValueError("a vector build writes a .3tz archive")
        self._scene = scene
        self._request = request
        self._output = check_output(request.output)
        self._staging = staging_dir_for(self._output)
        if lock is not None and lock.staging_dir != self._staging:
            raise ValueError(f"the lock is for {lock.staging_dir}, not {self._staging}")
        self._lock_handle = lock
        self._cancel = threading.Event()
        self._events = _EventQueue()
        self._state_lock = threading.Lock()
        self._state = JobState.PENDING
        self._phase: str | None = None
        self._result: BuildResult | None = None
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._warnings: list[str] = []
        self._warning_count = 0
        self._started_monotonic: float | None = None
        self._published = False  # the archive is installed (worker thread only)
        self._work_ready = False  # work/ was cleared for this attempt (worker thread only)

    # --- public API -----------------------------------------------------------

    @property
    def output(self) -> Path:
        return self._output

    @property
    def staging_dir(self) -> Path:
        return self._staging

    def start(self) -> None:
        """Start the worker thread. Call once."""
        if self._thread is not None:
            raise RuntimeError("BuildJob.start() was already called")
        self._started_monotonic = time.monotonic()
        # Daemon: a build can take an hour, and Blender must be able to quit.
        # The Blender side cancels and joins briefly on unregister and at exit.
        self._thread = threading.Thread(
            target=self._run, name=f"geotools-publish:{self._output.name}", daemon=True
        )
        self._thread.start()

    def cancel(self) -> None:
        """Ask the job to stop; rusty-tiles aborts at its next callback and
        publishes nothing. Safe to call from any thread, any number of times."""
        self._cancel.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def poll_events(self) -> list[ProgressEvent]:
        """Drain queued events (main thread). Never blocks."""
        return self._events.drain()

    @property
    def state(self) -> JobState:
        with self._state_lock:
            return self._state

    @property
    def phase(self) -> str | None:
        """The last rusty-tiles phase seen, if any."""
        return self._phase

    @property
    def result(self) -> BuildResult | None:
        with self._state_lock:
            return self._result

    @property
    def error(self) -> str | None:
        """'ClassName: message' when FAILED."""
        with self._state_lock:
            return self._error

    @property
    def elapsed(self) -> float:
        """Seconds since start(), 0 before."""
        if self._started_monotonic is None:
            return 0.0
        return time.monotonic() - self._started_monotonic

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker (tests, shutdown). True if it finished."""
        if self._thread is None:
            return False
        self._thread.join(timeout)
        return not self._thread.is_alive()

    # --- worker ---------------------------------------------------------------

    def _set_state(self, state: JobState, message: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._events.put(ProgressEvent(state=state, message=message))

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise _Cancelled("cancelled")

    def _callback(self, event: dict[str, object]) -> None:
        """rusty-tiles callback. Runs on Rust worker threads, GIL held."""
        if self._cancel.is_set():
            raise _Cancelled("cancelled")
        try:
            self._queue_rusty_event(event)
        except Exception:  # a malformed event must never fail the build
            log.debug("ignored a rusty-tiles event: %r", event, exc_info=True)

    def _queue_rusty_event(self, event: Mapping[str, object]) -> None:
        state = self._state
        kind = event.get("event")
        if kind == "progress":
            phase = event.get("phase")
            phase = str(phase) if phase is not None else None
            self._phase = phase
            done, total = event.get("done"), event.get("total")
            self._events.put(
                ProgressEvent(
                    state=state,
                    phase=phase,
                    done=int(done) if isinstance(done, int) else None,
                    total=int(total) if isinstance(total, int) else None,
                )
            )
        elif kind == "warning":
            detail = event.get("message")
            if not isinstance(detail, str):
                detail = ", ".join(f"{k}={v}" for k, v in event.items() if k != "event")
            text = f"Warning: {detail}"
            with self._state_lock:
                self._warning_count += 1
                if len(self._warnings) < MAX_RECORDED_WARNINGS:
                    self._warnings.append(detail)
            self._events.put(ProgressEvent(state=state, phase=self._phase, message=text))
        elif kind == "log":
            message = event.get("message")
            if isinstance(message, str):
                self._events.put(ProgressEvent(state=state, phase=self._phase, message=message))

    def _run(self) -> None:
        work = self._staging / st.WORK_DIR
        final = JobState.FAILED
        result: BuildResult | None = None
        error: str | None = None
        message: str | None = None
        try:
            result, message = self._build(work)
            final = JobState.DONE
        except _Cancelled:
            final, message = JobState.CANCELLED, "Cancelled; nothing was published"
        except BaseException as ex:  # noqa: BLE001 - every failure ends the job FAILED
            if self._cancel.is_set() and _is_cancel_error(ex):
                final, message = JobState.CANCELLED, "Cancelled; nothing was published"
            else:
                error = f"{type(ex).__name__}: {ex}"
                if self._published:
                    error += " (the archive was installed, but its build record was not written)"
                message = error
                if self._work_ready:
                    try:
                        (work / st.ERROR_NAME).write_text(
                            error + "\n\n" + traceback.format_exc(), encoding="utf-8"
                        )
                    except OSError:
                        pass
        finally:
            # Touch the staging directory only while holding its lock: if taking
            # the lock failed, another job owns everything in it.
            lock = self._lock_handle
            if lock is not None and lock.held:
                if final is not JobState.FAILED:
                    _rmtree(work)
                elif self._work_ready:
                    _rmtree(work / "scratch")  # keep the GLB and face map for debugging
                _rmtree(self._staging / st.IMAGES_DIR)  # they are inside the GLB now
                lock.release()
        with self._state_lock:
            self._result = result
            self._error = error
            self._events.put(ProgressEvent(state=final, phase=self._phase, message=message))
            self._state = final

    def _build(self, work: Path) -> tuple[BuildResult, str]:
        request = self._request
        output = self._output
        started_utc = _utc_now()
        t_start = time.monotonic()
        phase_seconds: dict[str, float] = {}

        if self._lock_handle is None:
            self._lock_handle = StagingLock.acquire(output)
        self._check_cancel()
        if output.exists() and not request.force:
            raise OutputExistsError(
                f"{output} already exists; turn on Overwrite (force) to replace it"
            )

        _rmtree(work)
        work.mkdir(parents=True)
        self._work_ready = True

        # 0. The .blend hash, read before anything else so a save during the
        # build cannot change it.
        blend_sha256 = None
        if request.blend_path is not None:
            t = time.monotonic()
            blend_sha256 = _sha256_file(request.blend_path)
            phase_seconds["hash_blend"] = time.monotonic() - t
            self._check_cancel()

        if request.output_type == "glb":
            return self._build_glb(work, started_utc, t_start, phase_seconds, blend_sha256)
        if request.output_type == "vector":
            return self._build_vector(work, started_utc, t_start, phase_seconds, blend_sha256)

        # 1. GLB and face map. The writer has no cancel hook; check around it.
        self._set_state(JobState.WRITING, "Writing source GLB")
        t = time.monotonic()
        glb = write_glb(self._scene, work / st.GLB_NAME)
        phase_seconds["write_glb"] = time.monotonic() - t
        self._check_cancel()
        t = time.monotonic()
        face_map = build_face_map(self._scene, dict(request.topology))
        save_face_map(face_map, work / st.FACEMAP_NAME)
        phase_seconds["face_map"] = time.monotonic() - t
        self._check_cancel()
        _rmtree(self._staging / st.IMAGES_DIR)

        # 2. Tiling.
        rt = _rusty_tiles()
        self._set_state(JobState.TILING, f"Tiling {glb.triangles:,} triangles")
        scratch = work / "scratch"
        scratch.mkdir()
        t = time.monotonic()
        tiled = rt.mesh_to_tileset(
            work / st.GLB_NAME,
            output,
            profile=request.profile,
            jobs=request.jobs,
            scratch_dir=scratch,
            memory_budget_bytes=request.memory_budget_bytes,
            force=request.force,
            callback=self._callback,
            **request.placement_kwargs,
        )
        phase_seconds["tiling"] = time.monotonic() - t
        self._published = True
        report = dict(tiled.report)
        diagnostics = dict(tiled.diagnostics)
        write_json(work / st.REPORT_NAME, report)
        write_json(work / st.DIAGNOSTICS_NAME, diagnostics)
        summary = summarise_report(report)

        # 3. Validation. The archive is installed, so a validation failure or a
        # cancel here is recorded, not turned into a failed job.
        validation: dict[str, object] = {"requested": request.validate, "ok": None}
        if request.validate and self._cancel.is_set():
            validation["skipped"] = "cancelled"
        elif request.validate:
            self._set_state(JobState.VALIDATING, "Validating the archive")
            t = time.monotonic()
            validation.update(self._validate(rt, output, work))
            phase_seconds["validation"] = time.monotonic() - t
        validation_ok = validation["ok"]

        # 4. Record: promote work/ into the staging directory, build.json last.
        finished_utc = _utc_now()
        seconds = time.monotonic() - t_start
        staging = self._staging
        record_path = staging / st.RECORD_NAME
        record = {
            "schema": RECORD_SCHEMA,
            "output_type": "3tz",
            "output": str(output),
            "output_bytes": _size(output),
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "seconds": seconds,
            "phase_seconds": phase_seconds,
            "rusty_tiles_phase_seconds": diagnostics.get("phase_seconds", {}),
            "rusty_tiles_version": _rusty_tiles_version(rt),
            "geotools": dict(request.extra_record),
            "settings": {
                "profile": request.profile,
                "jobs": request.jobs,
                "memory_budget_bytes": request.memory_budget_bytes,
                "force": request.force,
                "validate": request.validate,
            },
            "placement": {
                **request.placement_kwargs,
                "crs_epsg": epsg_code(request.placement_kwargs.get("source_crs")),  # type: ignore[arg-type]
            },
            "source": {"glb_sha256": glb.sha256, "blend_sha256": blend_sha256},
            "layers": [dict(record) for record in request.layers],
            "variant": None if request.variant is None else dict(request.variant),
            "source_glb": {
                "path": st.GLB_NAME,
                "sha256": glb.sha256,
                "bytes": glb.bytes,
                "primitives": glb.primitives,
                "triangles": glb.triangles,
            },
            "face_map": st.FACEMAP_NAME,
            "report": st.REPORT_NAME,
            "diagnostics": st.DIAGNOSTICS_NAME,
            "report_summary": summary,
            "validation": validation,
            "warnings": {"count": self._warning_count, "first": list(self._warnings)},
            "cleanup_diagnostics": list(getattr(tiled, "cleanup_diagnostics", ()) or ()),
        }
        record_path.unlink(missing_ok=True)  # no record is better than a stale one
        for name in st.PROMOTED_FILES:
            src = work / name
            if src.exists():
                os.replace(src, staging / name)
            else:
                (staging / name).unlink(missing_ok=True)
        write_json(record_path, record)

        result = BuildResult(
            output=output,
            staging_dir=staging,
            glb_sha256=glb.sha256,
            triangles=int(summary.get("triangles", glb.triangles)),  # type: ignore[arg-type]
            report_summary=summary,
            validation_ok=validation_ok,  # type: ignore[arg-type]
            record_path=record_path,
            seconds=seconds,
        )
        tiles = summary.get("tiles")
        message = f"Published {output.name}: {result.triangles:,} triangles, {tiles} tiles"
        if validation_ok is False:
            message += "; validation FAILED"
        elif validation.get("skipped"):
            message += "; validation skipped (cancelled after publishing)"
        elif validation_ok:
            message += ", validated"
        return result, message

    def _build_glb(
        self,
        work: Path,
        started_utc: str,
        t_start: float,
        phase_seconds: dict[str, float],
        blend_sha256: str | None,
    ) -> tuple[BuildResult, str]:
        """The metadata GLB: write, move onto the output, face map, record."""
        request = self._request
        output = self._output
        if request.extension is None:
            raise ValueError("a .glb build needs the metadata extension")
        self._set_state(JobState.WRITING, f"Writing {output.name}")
        t = time.monotonic()
        glb = write_glb(self._scene, work / output.name, extension=request.extension)  # type: ignore[arg-type]
        phase_seconds["write_glb"] = time.monotonic() - t
        self._check_cancel()
        t = time.monotonic()
        face_map = build_face_map(self._scene, dict(request.topology))
        save_face_map(face_map, work / st.FACEMAP_NAME)
        phase_seconds["face_map"] = time.monotonic() - t
        self._check_cancel()
        _rmtree(self._staging / st.IMAGES_DIR)

        # Publish: the GLB replaces the output in one rename (same directory tree).
        if output.exists() and not request.force:
            raise OutputExistsError(
                f"{output} already exists; turn on Overwrite (force) to replace it"
            )
        os.replace(work / output.name, output)
        self._published = True

        staging = self._staging
        record_path = staging / st.RECORD_NAME
        seconds = time.monotonic() - t_start
        record = {
            "schema": RECORD_SCHEMA,
            "output_type": "glb",
            "output": str(output),
            "output_bytes": _size(output),
            "started_utc": started_utc,
            "finished_utc": _utc_now(),
            "seconds": seconds,
            "phase_seconds": phase_seconds,
            "rusty_tiles_phase_seconds": {},
            "rusty_tiles_version": None,
            "geotools": dict(request.extra_record),
            "settings": {"force": request.force},
            "placement": {
                **request.placement_kwargs,
                "crs_epsg": epsg_code(request.placement_kwargs.get("source_crs")),  # type: ignore[arg-type]
            },
            "source": {"glb_sha256": glb.sha256, "blend_sha256": blend_sha256},
            "layers": [dict(record) for record in request.layers],
            "variant": None if request.variant is None else dict(request.variant),
            "glb": {
                "path": output.name,
                "sha256": glb.sha256,
                "bytes": glb.bytes,
                "primitives": glb.primitives,
                "triangles": glb.triangles,
                **dict(request.glb_record),
            },
            "face_map": st.FACEMAP_NAME,
            "report_summary": {},
            "validation": {"requested": False, "ok": None},
            "warnings": {"count": 0, "first": []},
        }
        record_path.unlink(missing_ok=True)
        os.replace(work / st.FACEMAP_NAME, staging / st.FACEMAP_NAME)
        write_json(record_path, record)
        result = BuildResult(
            output=output,
            staging_dir=staging,
            glb_sha256=glb.sha256,
            triangles=glb.triangles,
            report_summary={"triangles": glb.triangles, "bytes": glb.bytes},
            validation_ok=None,
            record_path=record_path,
            seconds=seconds,
        )
        return result, f"Exported {output.name}: {glb.triangles:,} triangles, {glb.bytes:,} bytes"

    def _build_vector(
        self,
        work: Path,
        started_utc: str,
        t_start: float,
        phase_seconds: dict[str, float],
        blend_sha256: str | None,
    ) -> tuple[BuildResult, str]:
        """Vectors: write the GeoPackage, ``vector_to_3tz`` (reusing the previous
        archive), validate, record."""
        request = self._request
        source = request.vector
        assert source is not None
        output = self._output

        # 1. The GeoPackage, through the vector exporter (sqlite3; no cancel hook).
        self._set_state(JobState.WRITING, f"Writing {source.features:,} vector features")
        t = time.monotonic()
        gpkg_path = work / st.VECTORS_NAME
        tables = vec.write_source(source, gpkg_path)
        gpkg_sha256 = _sha256_file(gpkg_path)
        phase_seconds["write_gpkg"] = time.monotonic() - t
        self._check_cancel()

        # 2. The reuse baseline: rusty-tiles refuses the output itself as
        # reuse_tileset, so a copy of it goes into work/.
        previous: Path | None = None
        reuse: dict[str, object] = {"requested": source.reuse, "used": False}
        if source.reuse and output.is_file():
            t = time.monotonic()
            previous = work / st.PREVIOUS_NAME
            shutil.copyfile(output, previous)
            phase_seconds["copy_previous"] = time.monotonic() - t
            self._check_cancel()
        elif source.reuse:
            reuse["reason"] = "no previous archive"

        # 3. Tiling.
        rt = _rusty_tiles()
        options = dict(source.options)
        self._set_state(JobState.TILING, f"Tiling {source.features:,} vector features")
        t = time.monotonic()
        try:
            try:
                tiled = self._vector_to_3tz(rt, gpkg_path, output, tables, options, previous)
                reuse["used"] = previous is not None
            except Exception as ex:
                if previous is None or self._cancel.is_set() or not vec.is_reuse_refusal(ex):
                    raise
                # rusty-tiles cannot reuse this archive (another encoder or
                # settings): say why, and build afresh.
                reuse["refused"] = f"{type(ex).__name__}: {ex}"
                self._events.put(
                    ProgressEvent(
                        state=JobState.TILING,
                        message=f"The previous archive cannot be reused ({ex}); building afresh",
                    )
                )
                tiled = self._vector_to_3tz(rt, gpkg_path, output, tables, options, None)
        finally:
            if previous is not None:  # a copy of the output; never kept, even on failure
                previous.unlink(missing_ok=True)
        phase_seconds["tiling"] = time.monotonic() - t
        self._published = True
        report = dict(tiled.report)
        write_json(work / st.REPORT_NAME, report)
        summary = vec.summarise_vector_report(report)
        if isinstance(summary.get("reuse"), Mapping):
            reuse["report"] = summary["reuse"]

        # 4. Validation (the archive is installed: a failure is recorded).
        validation: dict[str, object] = {"requested": request.validate, "ok": None}
        if request.validate and self._cancel.is_set():
            validation["skipped"] = "cancelled"
        elif request.validate:
            self._set_state(JobState.VALIDATING, "Validating the archive")
            t = time.monotonic()
            validation.update(self._validate(rt, output, work))
            phase_seconds["validation"] = time.monotonic() - t
        validation_ok = validation["ok"]

        # 5. Record.
        staging = self._staging
        record_path = staging / st.RECORD_NAME
        seconds = time.monotonic() - t_start
        features = int(summary.get("features", source.features))  # type: ignore[arg-type]
        placement = dict(request.placement_kwargs)
        record = {
            "schema": RECORD_SCHEMA,
            "output_type": "vector",
            "output": str(output),
            "output_bytes": _size(output),
            "started_utc": started_utc,
            "finished_utc": _utc_now(),
            "seconds": seconds,
            "phase_seconds": phase_seconds,
            "rusty_tiles_phase_seconds": {},
            "rusty_tiles_version": _rusty_tiles_version(rt),
            "geotools": dict(request.extra_record),
            "settings": {
                "force": request.force,
                "validate": request.validate,
                **{k: v for k, v in options.items() if k not in ("source_crs", "height_offset")},
            },
            "placement": {
                **placement,
                "crs_epsg": epsg_code(placement.get("source_crs")),  # type: ignore[arg-type]
            },
            "source": {"gpkg_sha256": gpkg_sha256, "blend_sha256": blend_sha256},
            "layers": [dict(record) for record in request.layers],
            "variant": None,
            "vectors": {
                "path": st.VECTORS_NAME,
                "sha256": gpkg_sha256,
                "bytes": _size(gpkg_path),
                "crs": None if source.crs is None else source.crs.label(),
                "tables": tables,
                "features": source.features,
                "reuse": reuse,
            },
            "report": st.REPORT_NAME,
            "report_summary": summary,
            "validation": validation,
            "warnings": {"count": self._warning_count, "first": list(self._warnings)},
            "cleanup_diagnostics": list(getattr(tiled, "cleanup_diagnostics", ()) or ()),
        }
        record_path.unlink(missing_ok=True)
        for name in st.PROMOTED_FILES:
            src = work / name
            if src.exists():
                os.replace(src, staging / name)
            else:
                (staging / name).unlink(missing_ok=True)
        write_json(record_path, record)

        result = BuildResult(
            output=output,
            staging_dir=staging,
            glb_sha256="",
            triangles=0,
            report_summary={**summary, "features": features},
            validation_ok=validation_ok,  # type: ignore[arg-type]
            record_path=record_path,
            seconds=seconds,
        )
        tiles = summary.get("tiles")
        message = f"Published {output.name}: {features:,} features"
        if tiles is not None:
            message += f", {tiles} tiles"
        if reuse.get("used"):
            message += " (incremental)"
        if validation_ok is False:
            message += "; validation FAILED"
        elif validation.get("skipped"):
            message += "; validation skipped (cancelled after publishing)"
        elif validation_ok:
            message += ", validated"
        return result, message

    def _vector_to_3tz(
        self,
        rt: ModuleType,
        gpkg_path: Path,
        output: Path,
        tables: list[dict[str, object]],
        options: dict[str, object],
        previous: Path | None,
    ) -> Any:
        return rt.vector_to_3tz(
            gpkg_path,
            output,
            force=self._request.force,
            layers=[str(t["table"]) for t in tables],
            reuse_tileset=previous,
            callback=self._callback,
            **options,
        )

    def _validate(self, rt: ModuleType, output: Path, work: Path) -> dict[str, object]:
        """Run rusty_tiles.validate; never raises except for internal bugs."""

        def callback(event: dict[str, object]) -> None:
            if self._cancel.is_set():
                raise _Cancelled("cancelled")
            try:
                self._queue_rusty_event(event)
            except Exception:
                log.debug("ignored a rusty-tiles event: %r", event, exc_info=True)

        try:
            report = rt.validate(output, lazy_payloads=True, callback=callback)
        except _Cancelled:
            return {"ok": None, "skipped": "cancelled"}
        except Exception as ex:  # a failed check raises (DataError, ...)
            log.info("validation of %s failed", output, exc_info=True)
            if self._cancel.is_set() and _is_cancel_error(ex):
                return {"ok": None, "skipped": "cancelled"}
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        report = dict(report)
        payloads = report.pop("payloads", None)
        try:
            report["payload_rows"] = len(payloads)  # type: ignore[arg-type]
        except TypeError:
            pass
        write_json(work / st.VALIDATION_NAME, report)
        return {"ok": bool(report.get("ok")), "summary": summarise_validation(report)}


def _is_cancel_error(ex: BaseException) -> bool:
    """rusty-tiles' own cancellation, or an observer failure caused by our raise."""
    return type(ex).__name__ in {"CancelledError", "ObserverError"} or isinstance(ex, _Cancelled)


def _sha256_file(path: Path) -> str | None:
    """Hex SHA-256 of a file, or None if it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                digest.update(chunk)
    except OSError:
        log.warning("could not hash %s", path, exc_info=True)
        return None
    return digest.hexdigest()


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _rusty_tiles_version(rt: ModuleType) -> str | None:
    status = deps.probe_rusty_tiles()
    if status.available and status.version:
        return status.version
    version = getattr(rt, "__version__", None)
    return version if isinstance(version, str) else None
