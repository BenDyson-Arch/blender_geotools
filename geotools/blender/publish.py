"""Publish: the build operator, its timer, cancel, preview and the Publish panel.

The work happens in ``publish.job.BuildJob`` on a worker thread that never
touches ``bpy``. This module is the main-thread side:

1. ``geotools.publish`` checks the scene, extracts it with
   ``export.extract_scene`` (main thread) and starts one job per session;
2. a ``bpy.app.timers`` function (:func:`update_from_job`) drains the job's
   events into the window manager's ``geotools_publish`` status, redraws the
   sidebar and, when the job ends, stores the result on the build profile;
3. unregistering the extension or quitting Blender cancels the job and joins it
   briefly.

In background mode timers never fire; tests call :func:`update_from_job`.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
import textwrap
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty

from ..core import deps, sysmem
from ..core.issues import Severity
from ..publish import preview as pv
from ..publish import staging as st
from ..publish.job import BuildJob, BuildRequest, JobState
from .checks import count_issues, describe_counts, run_checks, store_issues
from .common import ADDON_PACKAGE, extension_version, redraw_ui
from .properties import FieldError, placement_from_props

log = logging.getLogger(__name__)

OPEN_BROWSER = True  # tests turn this off
TIMER_INTERVAL = 0.25  # seconds between event drains
SHUTDOWN_JOIN_SECONDS = 5.0  # how long unregister and quit wait for a cancelled job

STATE_ITEMS = (
    ("IDLE", "Idle", ""),
    ("PENDING", "Starting", ""),
    ("WRITING", "Writing", "Writing the source GLB and face map"),
    ("TILING", "Tiling", "rusty-tiles is building the archive"),
    ("VALIDATING", "Validating", "rusty-tiles is validating the archive"),
    ("DONE", "Done", ""),
    ("FAILED", "Failed", ""),
    ("CANCELLED", "Cancelled", ""),
)
RUNNING_STATES = frozenset(("PENDING", "WRITING", "TILING", "VALIDATING"))


class GeoToolsPublishStatus(bpy.types.PropertyGroup):
    """Live status of the publish job (window manager, not saved)."""

    state: EnumProperty(name="State", items=STATE_ITEMS, default="IDLE")
    phase: StringProperty(name="Phase")
    done: IntProperty(name="Done", default=0)
    total: IntProperty(name="Total", default=-1)  # -1: unknown
    message: StringProperty(name="Message")
    elapsed: FloatProperty(name="Elapsed", default=0.0)
    output: StringProperty(name="Output", subtype="FILE_PATH")
    profile_name: StringProperty(name="Build Profile")


# --- session state ---------------------------------------------------------------------


@dataclass
class _Session:
    job: BuildJob
    scene_name: str
    profile_name: str
    detached: bool = False  # another .blend was loaded since; don't write results


_session: _Session | None = None
_preview: pv.PreviewServer | None = None


def active_job() -> BuildJob | None:
    """The running (or not yet collected) publish job of this session."""
    return _session.job if _session is not None else None


def is_running() -> bool:
    return _session is not None


def status(context: bpy.types.Context | None = None) -> GeoToolsPublishStatus | None:
    wm = (context or bpy.context).window_manager
    if wm is None and bpy.data.window_managers:
        wm = bpy.data.window_managers[0]
    return getattr(wm, "geotools_publish", None) if wm is not None else None


def active_profile(scene: bpy.types.Scene) -> bpy.types.PropertyGroup | None:
    props = scene.geotools
    if 0 <= props.build_profile_index < len(props.build_profiles):
        return props.build_profiles[props.build_profile_index]
    return None


def _utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- output path ----------------------------------------------------------------------


class OutputPathError(ValueError):
    pass


def resolve_output(path_text: str) -> Path:
    """The absolute ``.3tz`` path of a build profile's output.

    ``//`` paths are relative to the .blend and need it saved; other relative
    paths are refused, since Blender's working directory is arbitrary.
    """
    text = path_text.strip()
    if not text:
        raise OutputPathError("Set an output path (.3tz) on the build profile")
    if text.startswith("//") and not bpy.data.is_saved:
        raise OutputPathError(
            "The output path is relative to the .blend (//); save the .blend first "
            "or use an absolute path"
        )
    resolved = bpy.path.abspath(text)
    if not os.path.isabs(resolved):
        raise OutputPathError(
            f"The output path {text!r} is relative; use an absolute path or // for the .blend's folder"
        )
    output = Path(os.path.normpath(resolved))
    if output.suffix != ".3tz":
        raise OutputPathError(f"The output must end in .3tz: {output}")
    return output


# --- timer -------------------------------------------------------------------------------


def _apply_events(status_: GeoToolsPublishStatus, job: BuildJob) -> None:
    for event in job.poll_events():
        if event.state.name != status_.state:
            # A new job step (e.g. validation) starts with no rusty-tiles phase.
            status_.phase = ""
            status_.done, status_.total = 0, -1
        status_.state = event.state.name
        if event.phase is not None and event.phase != status_.phase:
            status_.phase = event.phase
            status_.done, status_.total = 0, -1
        if event.done is not None:
            status_.done = event.done
        if event.done is not None or event.phase is not None:
            status_.total = event.total if event.total is not None else -1
        if event.message:
            status_.message = event.message[:1024]
    status_.elapsed = job.elapsed


def _store_result(session: _Session) -> None:
    """Write a DONE job's summary onto its build profile, if it still exists."""
    job = session.job
    result = job.result
    if result is None or session.detached:
        return
    scene = bpy.data.scenes.get(session.scene_name)
    if scene is None:
        return
    profile = scene.geotools.build_profiles.get(session.profile_name)
    if profile is None:
        return
    summary = result.report_summary
    profile.last_output = str(result.output)
    profile.last_record = str(result.record_path)
    profile.last_build_utc = _utc_now()
    profile.last_seconds = result.seconds
    profile.last_validation = {True: "OK", False: "FAILED", None: "NONE"}[result.validation_ok]
    profile.last_triangles = str(summary.get("triangles", result.triangles))
    profile.last_tiles = str(summary.get("tiles", ""))


def update_from_job() -> float | None:
    """Timer: drain events into the status, finish up when the job ends.

    Returns the next interval, or None (stop) once there is no job.
    """
    global _session
    session = _session
    if session is None:
        return None
    job = session.job
    finished = job.state.finished  # read before draining, so no event is missed
    status_ = status()
    if status_ is not None:
        _apply_events(status_, job)
    else:
        job.poll_events()
    if finished:
        if job.state is JobState.DONE:
            _store_result(session)
        elif job.state is JobState.FAILED and status_ is not None:
            status_.message = job.error or "Publish failed"
        _session = None
    try:
        redraw_ui(bpy.context)
    except Exception:  # no windows (background) or a context without screens
        log.debug("publish: redraw failed", exc_info=True)
    return None if finished else TIMER_INTERVAL


def _start_timer() -> None:
    if not bpy.app.timers.is_registered(update_from_job):
        # Persistent: a non-persistent timer is dropped when a file is loaded,
        # which would leave the session marked as running for good.
        bpy.app.timers.register(update_from_job, first_interval=TIMER_INTERVAL, persistent=True)


def shutdown_job(timeout: float = SHUTDOWN_JOIN_SECONDS) -> None:
    """Cancel the job and wait briefly (unregister, quit, tests)."""
    global _session
    session, _session = _session, None
    if session is None:
        return
    session.job.cancel()
    if not session.job.join(timeout):
        log.warning(
            "the publish job did not stop within %.0f s; it stops at its next progress event",
            timeout,
        )


def stop_preview() -> None:
    global _preview
    server, _preview = _preview, None
    if server is not None:
        server.stop()


@persistent
def _on_exit(*_args: object) -> None:
    shutdown_job()
    stop_preview()


@persistent
def _on_load_pre(*_args: object) -> None:
    # The job keeps going (its output does not depend on Blender), but its
    # result must not be written into a different file's profile.
    if _session is not None:
        _session.detached = True


# --- operators ---------------------------------------------------------------------------


def _extract_scene(context, collection, image_dir: Path, remedies: list[dict]):
    """``export.extract_scene``; imported late so a missing exporter only fails here."""
    from . import export

    return export.extract_scene(context, collection, image_dir, remedies=remedies)


def _extra_record(context: bpy.types.Context, profile_name: str, collection_name: str) -> dict:
    return {
        "geotools_version": extension_version(),
        "blender_version": bpy.app.version_string,
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "build_profile": profile_name,
        "source_collection": collection_name,
    }


class GEOTOOLS_OT_publish(bpy.types.Operator):
    """Run the checks, write the source GLB and build a .3tz with rusty-tiles"""

    bl_idname = "geotools.publish"
    bl_label = "Publish"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        if is_running():
            cls.poll_message_set("A publish is already running")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _session
        if is_running():
            self.report({"ERROR"}, "A publish is already running; cancel it or wait")
            return {"CANCELLED"}
        props = context.scene.geotools
        if props.source_collection is None:
            self.report({"ERROR"}, "Cannot publish: set or ingest a source collection first")
            return {"CANCELLED"}
        profile = active_profile(context.scene)
        if profile is None:
            self.report(
                {"ERROR"},
                "Cannot publish: add a build profile first (Build Profiles panel, + button)",
            )
            return {"CANCELLED"}
        try:
            output = resolve_output(profile.output_path)
        except OutputPathError as ex:
            self.report({"ERROR"}, f"Cannot publish with {profile.name!r}: {ex}")
            return {"CANCELLED"}

        issues = run_checks(context)
        store_issues(props, issues)
        counts = count_issues(issues)
        if counts[Severity.ERROR]:
            redraw_ui(context)
            self.report(
                {"ERROR"},
                f"Cannot publish: checks found {describe_counts(counts)}. "
                "Open the Checks panel and fix the errors first",
            )
            return {"CANCELLED"}
        try:
            placement_kwargs = placement_from_props(props).mesh_to_tileset_kwargs()
        except (FieldError, ValueError) as ex:
            self.report({"ERROR"}, f"Cannot publish: {ex}")
            return {"CANCELLED"}
        dep = deps.probe_rusty_tiles()
        if not dep.available:
            self.report({"ERROR"}, f"Cannot publish: {dep.summary}")
            return {"CANCELLED"}

        try:
            lock = st.StagingLock.acquire(output)
        except (st.StagingBusyError, OSError) as ex:
            self.report({"ERROR"}, f"Cannot publish: {ex}")
            return {"CANCELLED"}
        try:
            image_dir = st.images_dir_for(output)
            shutil.rmtree(image_dir, ignore_errors=True)
            image_dir.mkdir(parents=True)
            remedies: list[dict] = []
            scene_export, topology = _extract_scene(
                context, props.source_collection, image_dir, remedies
            )
            extra_record = _extra_record(context, profile.name, props.source_collection.name)
            if remedies:
                # Which file each remedy wrote (a downscale may fall back to JPEG).
                extra_record["image_remedies"] = remedies
            request = BuildRequest(
                output=output,
                placement_kwargs=placement_kwargs,
                profile=profile.profile.lower(),
                jobs=profile.jobs or None,
                memory_budget_bytes=(
                    int(profile.memory_budget_gib * 2**30)
                    if profile.memory_budget_gib > 0
                    else sysmem.auto_memory_budget(sysmem.available_memory_bytes())
                ),
                force=profile.overwrite,
                validate=profile.validate,
                topology=dict(topology),
                extra_record=extra_record,
            )
            job = BuildJob(scene_export, request, lock=lock)
        except Exception as ex:
            shutil.rmtree(st.images_dir_for(output), ignore_errors=True)
            lock.release()
            log.exception("publish: extraction failed")
            self.report({"ERROR"}, f"Cannot publish: {type(ex).__name__}: {ex}")
            return {"CANCELLED"}

        status_ = status(context)
        if status_ is not None:
            status_.state = "PENDING"
            status_.phase = ""
            status_.done, status_.total = 0, -1
            status_.message = "Starting"
            status_.elapsed = 0.0
            status_.output = str(output)
            status_.profile_name = profile.name
        _session = _Session(
            job=job,
            scene_name=context.scene.name,
            profile_name=profile.name,
        )
        job.start()
        _start_timer()
        warnings = counts[Severity.WARNING]
        notes = [f"{warnings} check warning{'s' * (warnings != 1)}"] if warnings else []
        if remedies:
            notes.append(f"{len(remedies)} image{'s' * (len(remedies) != 1)} remedied")
        note = f" ({', '.join(notes)})" if notes else ""
        self.report({"INFO"}, f"Publishing {output.name}{note}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_cancel_publish(bpy.types.Operator):
    """Stop the running publish; nothing is published and an existing archive is kept"""

    bl_idname = "geotools.cancel_publish"
    bl_label = "Cancel Publish"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not is_running():
            cls.poll_message_set("No publish is running")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        job = active_job()
        if job is None:
            return {"CANCELLED"}
        job.cancel()
        status_ = status(context)
        if status_ is not None:
            if status_.state == "WRITING" or status_.phase in {"", "conversion", "capture"}:
                # rusty-tiles 0.4.0 checks for cancel only at progress events, and
                # reading the source sends none (rusty-tiles #335).
                status_.message = (
                    "Cancelling: rusty-tiles stops once it has read the source, "
                    "which can take about a minute on large sources"
                )
            else:
                status_.message = "Cancelling..."
        self.report({"INFO"}, "Cancelling the publish")
        redraw_ui(context)
        return {"FINISHED"}


def _last_output(context: bpy.types.Context) -> Path | None:
    profile = active_profile(context.scene) if context.scene is not None else None
    if profile is None or not profile.last_output:
        return None
    return Path(profile.last_output)


class GEOTOOLS_OT_open_output_folder(bpy.types.Operator):
    """Open the folder holding the last published archive"""

    bl_idname = "geotools.open_output_folder"
    bl_label = "Open Output Folder"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        output = _last_output(context)
        if output is None or not output.parent.is_dir():
            cls.poll_message_set("Nothing published with this build profile yet")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        return bpy.ops.wm.path_open(filepath=str(_last_output(context).parent))


class GEOTOOLS_OT_open_build_record(bpy.types.Operator):
    """Open build.json, the record of the last publish"""

    bl_idname = "geotools.open_build_record"
    bl_label = "Open Build Record"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        profile = active_profile(context.scene) if context.scene is not None else None
        if profile is None or not profile.last_record or not Path(profile.last_record).is_file():
            cls.poll_message_set("No build record for this build profile yet")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        return bpy.ops.wm.path_open(filepath=active_profile(context.scene).last_record)


# --- preview --------------------------------------------------------------------------------


def addon_preferences(
    context: bpy.types.Context | None = None,
) -> bpy.types.AddonPreferences | None:
    addon = (context or bpy.context).preferences.addons.get(ADDON_PACKAGE)
    return addon.preferences if addon is not None else None


def preview_problem(context: bpy.types.Context) -> str | None:
    """Why preview cannot run now, or None."""
    prefs = addon_preferences(context)
    if prefs is None:
        return "GeoTools preferences are not available"
    problem = pv.cli_problem(bpy.path.abspath(prefs.rusty_tiles_cli)) or pv.cesium_problem(
        bpy.path.abspath(prefs.cesium_dir)
    )
    if problem:
        return problem
    output = _last_output(context)
    if output is None or not output.is_file():
        return "Publish with this build profile first"
    return None


def preview_url() -> str | None:
    server = _preview
    if server is None or not server.running:
        return None
    return server.url


class GEOTOOLS_OT_preview_output(bpy.types.Operator):
    """Serve an extracted copy of the last published archive with rusty-tiles preview and open it in the browser"""

    bl_idname = "geotools.preview_output"
    bl_label = "Preview in Browser"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        problem = preview_problem(context)
        if problem:
            cls.poll_message_set(problem)
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        global _preview
        prefs = addon_preferences(context)
        output = _last_output(context)
        stop_preview()
        try:
            mesh_dir = pv.extract_archive(output, st.preview_dir_for(output))
            _preview = pv.PreviewServer(
                bpy.path.abspath(prefs.rusty_tiles_cli),
                bpy.path.abspath(prefs.cesium_dir),
                mesh_dir,
                st.staging_dir_for(output) / "preview.log",
                open_browser=webbrowser.open if OPEN_BROWSER else None,
            )
        except (pv.PreviewError, OSError, ValueError) as ex:
            self.report({"ERROR"}, f"Preview failed: {ex}")
            return {"CANCELLED"}
        server = _preview
        server.started.wait(5.0)
        if server.error and not server.url:
            stop_preview()
            self.report({"ERROR"}, server.error)
            return {"CANCELLED"}
        self.report({"INFO"}, f"Preview at {server.url or f'http://127.0.0.1:{server.port}/'}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_stop_preview(bpy.types.Operator):
    """Stop the rusty-tiles preview server"""

    bl_idname = "geotools.stop_preview"
    bl_label = "Stop Preview"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _preview is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        stop_preview()
        redraw_ui(context)
        return {"FINISHED"}


# --- panel ----------------------------------------------------------------------------------


def _format_seconds(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"


def _wrapped(layout: bpy.types.UILayout, text: str, icon: str = "NONE", width: int = 44) -> None:
    col = layout.column(align=True)
    for i, line in enumerate(textwrap.wrap(text, width)[:8] or [""]):
        col.label(text=line, icon=icon if i == 0 else "NONE")


_END_ICONS = {"DONE": "CHECKMARK", "FAILED": "ERROR", "CANCELLED": "CANCEL"}


def _draw_progress(layout: bpy.types.UILayout, status_: GeoToolsPublishStatus) -> None:
    box = layout.box()
    label = {k: n for k, n, _d in STATE_ITEMS}.get(status_.state, status_.state)
    if status_.total > 0:
        factor = min(max(status_.done / status_.total, 0.0), 1.0)
        text = f"{status_.phase} {status_.done:,}/{status_.total:,}"
    else:
        factor = 0.0
        text = status_.phase or label
    box.label(text=f"{label} ({_format_seconds(status_.elapsed)})", icon="SORTTIME")
    box.progress(factor=factor, type="BAR", text=text)
    if status_.message:
        _wrapped(box, status_.message)
    box.operator("geotools.cancel_publish", text="Cancel", icon="CANCEL")


def _draw_last_build(layout: bpy.types.UILayout, profile: bpy.types.PropertyGroup) -> None:
    box = layout.box()
    col = box.column(align=True)
    col.label(text="Last Build", icon="FILE_3D")
    col.label(text=Path(profile.last_output).name)
    if profile.last_build_utc:
        col.label(
            text=f"{profile.last_build_utc.replace('T', ' ')} ({_format_seconds(profile.last_seconds)})"
        )
    if profile.last_triangles:
        col.label(text=f"Triangles  {int(profile.last_triangles):,}")
    if profile.last_tiles:
        col.label(text=f"Tiles  {profile.last_tiles}")
    validation = {
        "OK": ("Validation passed", "CHECKMARK"),
        "FAILED": ("Validation FAILED, see the build record", "ERROR"),
        "NONE": ("Not validated", "INFO"),
    }[profile.last_validation]
    row = col.row()
    row.alert = profile.last_validation == "FAILED"
    row.label(text=validation[0], icon=validation[1])
    row = box.row(align=True)
    row.operator("geotools.open_output_folder", text="Folder", icon="FILE_FOLDER")
    row.operator("geotools.open_build_record", text="Record", icon="TEXT")


def _draw_preview(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    url = preview_url()
    if url:
        box = layout.box()
        box.label(text=url, icon="URL")
        box.operator("geotools.stop_preview", icon="CANCEL")
        return
    layout.operator("geotools.preview_output", icon="WORLD")
    problem = preview_problem(context)
    if problem:
        _wrapped(layout, problem, icon="INFO")


class GEOTOOLS_PT_publish(bpy.types.Panel):
    """Publish the source as a .3tz with the active build profile."""

    bl_idname = "GEOTOOLS_PT_publish"
    bl_label = "Publish"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "GeoTools"

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        profile = active_profile(scene)
        status_ = status(context)
        running = is_running()

        if profile is None:
            layout.label(text="No build profile", icon="ERROR")
            layout.operator("geotools.build_profile_add", text="Add Build Profile", icon="ADD")
        else:
            col = layout.column(align=True)
            col.label(text=f"Profile: {profile.name}", icon="PRESET")
            row = col.row(align=True)
            row.alert = not profile.output_path.strip()
            row.prop(profile, "output_path", text="")
            row = layout.row(align=True)
            row.prop(profile, "overwrite")
            row.prop(profile, "validate")
            if profile.output_path.startswith("//") and not bpy.data.is_saved:
                layout.label(text="Save the .blend first (// path)", icon="ERROR")

        if not deps.probe_rusty_tiles().available:
            layout.label(text="Needs rusty-tiles (see About)", icon="INFO")

        if running and status_ is not None:
            _draw_progress(layout, status_)
        else:
            layout.operator("geotools.publish", icon="EXPORT")
            if status_ is not None and status_.state in _END_ICONS and status_.message:
                box = layout.box()
                box.alert = status_.state == "FAILED"
                _wrapped(box, status_.message, icon=_END_ICONS[status_.state])

        if profile is not None and profile.last_output:
            _draw_last_build(layout, profile)
        _draw_preview(layout, context)


# --- registration ----------------------------------------------------------------------------

# The panel is registered by panels.py so it sits in the sidebar order there.
classes = (
    GeoToolsPublishStatus,
    GEOTOOLS_OT_publish,
    GEOTOOLS_OT_cancel_publish,
    GEOTOOLS_OT_open_output_folder,
    GEOTOOLS_OT_open_build_record,
    GEOTOOLS_OT_preview_output,
    GEOTOOLS_OT_stop_preview,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.WindowManager.geotools_publish = bpy.props.PointerProperty(type=GeoToolsPublishStatus)
    if _on_exit not in bpy.app.handlers.exit_pre:
        bpy.app.handlers.exit_pre.append(_on_exit)
    if _on_load_pre not in bpy.app.handlers.load_pre:
        bpy.app.handlers.load_pre.append(_on_load_pre)


def unregister() -> None:
    shutdown_job()
    stop_preview()
    if bpy.app.timers.is_registered(update_from_job):
        bpy.app.timers.unregister(update_from_job)
    for handlers, fn in (
        (bpy.app.handlers.exit_pre, _on_exit),
        (bpy.app.handlers.load_pre, _on_load_pre),
    ):
        while fn in handlers:
            handlers.remove(fn)
    if hasattr(bpy.types.WindowManager, "geotools_publish"):
        del bpy.types.WindowManager.geotools_publish
    _unregister_classes()
