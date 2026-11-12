"""Publish: the build operator, its timer, cancel, preview and the Publish panel.

The work happens in ``publish.job.BuildJob`` on a worker thread that never
touches ``bpy``. This module is the main-thread side:

1. ``geotools.publish`` checks the scene (Run Checks, which also refreshes
   every layer's range and status from the evaluated meshes), builds a
   variant's presentation, extracts the scene with ``export.extract_scene``
   (main thread; the evaluated meshes unless the build profile's Apply
   Modifiers is off) and starts one job per session. A Vectors profile (M8)
   instead reads its vector collection (``vector_publish.prepare``) and the
   job tiles it with ``vector_to_3tz``;
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
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty

from ..core import deps, sysmem
from ..core.display import format_count, format_seconds
from ..core.issues import Issue, Severity
from ..core.presentation import variant_output
from ..publish import preview as pv
from ..publish import staging as st
from ..publish.job import BuildJob, BuildRequest, JobState
from . import texture_state
from .checks import count_issues, describe_counts, run_checks, store_issues
from .common import ADDON_PACKAGE, extension_version, redraw_ui
from .layers import layer_records
from .presentation import build_presentation, variant_issues
from .properties import OUTPUT_SUFFIXES, FieldError, placement_from_props
from .ui import info_row, wrapped
from .vector_publish import is_vector

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


def profile_suffix(profile: bpy.types.PropertyGroup) -> str:
    """``.3tz`` or ``.glb``: what the profile's format writes."""
    return OUTPUT_SUFFIXES.get(getattr(profile, "output_type", "3TZ"), ".3tz")


def is_glb(profile: bpy.types.PropertyGroup | None) -> bool:
    return profile is not None and getattr(profile, "output_type", "3TZ") == "GLB"


def resolve_output(path_text: str, suffix: str = ".3tz") -> Path:
    """The absolute ``suffix`` (``.3tz`` or ``.glb``) path of a build profile's output.

    ``//`` paths are relative to the .blend and need it saved; other relative
    paths are refused, since Blender's working directory is arbitrary.
    """
    text = path_text.strip()
    if not text:
        raise OutputPathError(f"Set an output path ({suffix}) on the build profile")
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
    if output.suffix != suffix:
        raise OutputPathError(f"The output must end in {suffix}: {output}")
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
    if result.output.suffix == ".glb":
        summary = {"triangles": result.triangles, "tiles": ""}
    vector = "features" in summary and not result.triangles
    profile.last_features = str(summary.get("features", "")) if vector else ""
    if vector:
        summary = {**summary, "triangles": ""}
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
            texture_state.invalidate()
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


def _extract_scene(
    context,
    collection,
    image_dir: Path,
    remedies: list[dict],
    presentation=None,
    apply_modifiers: bool = True,
):
    """``export.extract_scene``; imported late so a missing exporter only fails here."""
    from . import export

    return export.extract_scene(
        context,
        collection,
        image_dir,
        remedies=remedies,
        presentation=presentation,
        apply_modifiers=apply_modifiers,
    )


def _push_undo(message: str) -> None:
    """An undo step, so a later undo keeps what was just stored (the object ids)."""
    if bpy.app.background:
        return
    try:
        bpy.ops.ed.undo_push(message=message)
    except RuntimeError:
        log.debug("publish: undo push failed", exc_info=True)


def blend_path_for_record(filepath: str | None = None, is_dirty: bool | None = None) -> Path | None:
    """The saved ``.blend`` whose hash build.json records, or None.

    None when the file was never saved or has changed since (``is_dirty``): the
    file on disk would then not match the scene that was published. The
    arguments default to ``bpy.data.filepath`` and ``bpy.data.is_dirty``.
    """
    filepath = bpy.data.filepath if filepath is None else filepath
    is_dirty = bpy.data.is_dirty if is_dirty is None else is_dirty
    if not filepath or is_dirty:
        return None
    return Path(filepath)


def _extra_record(
    context: bpy.types.Context, profile_name: str, collection_name: str, apply_modifiers: bool
) -> dict:
    return {
        "geotools_version": extension_version(),
        "blender_version": bpy.app.version_string,
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "build_profile": profile_name,
        "source_collection": collection_name,
        "apply_modifiers": apply_modifiers,
    }


class GEOTOOLS_OT_publish(bpy.types.Operator):
    """Run the checks, write the source GLB and build a .3tz with rusty-tiles, or write the metadata GLB for a .glb build profile"""

    bl_idname = "geotools.publish"
    bl_label = "Publish"
    bl_options = {"REGISTER"}

    layer: StringProperty(
        name="Variant Layer",
        description="Publish a variant tileset with this layer presented "
        "(<output>.<layer>.3tz). Empty to use the build profile's variant layer",
        options={"SKIP_SAVE"},
    )

    @classmethod
    def description(cls, context: bpy.types.Context, properties) -> str:
        if properties.layer:
            return (
                f"Publish the variant tileset of layer {properties.layer} with the active "
                "build profile: the same source, placement and checks, with the layer's "
                "display colours, as <output>.<layer>.3tz"
            )
        profile = active_profile(context.scene) if context.scene is not None else None
        name = output_name(context.scene, profile) if profile is not None else None
        if name and is_vector(profile):
            collection = profile.vector_collection
            what = f"the objects of {collection.name}" if collection else "the vector objects"
            return (
                f"Write {name} with build profile {profile.name}: {what} as vector features "
                "(polygons, lines, points with their attributes), tiled by rusty-tiles"
            )
        if name and is_glb(profile):
            return (
                f"Write {name} with build profile {profile.name}: run the checks and write a "
                "standard GLB with the objects, layers and georeference as glTF metadata"
            )
        if name:
            variant = profile.variant_layer.strip()
            what = f"the variant of layer {variant}" if variant else "the plain tileset"
            return (
                f"Write {name}, {what}, with build profile {profile.name}: run the checks, "
                "write the source GLB and build the archive with rusty-tiles"
            )
        return cls.__doc__

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
        # Before anything below writes to the scene (store_issues does).
        blend_path = blend_path_for_record()
        props = context.scene.geotools
        profile = active_profile(context.scene)
        if props.source_collection is None and not is_vector(profile):
            self.report({"ERROR"}, "Cannot publish: set or ingest a source collection first")
            return {"CANCELLED"}
        if profile is None:
            self.report(
                {"ERROR"},
                "Cannot publish: add a build profile first (Publish panel, + button)",
            )
            return {"CANCELLED"}
        glb = is_glb(profile)
        try:
            output = resolve_output(profile.output_path, profile_suffix(profile))
        except OutputPathError as ex:
            self.report({"ERROR"}, f"Cannot publish with {profile.name!r}: {ex}")
            return {"CANCELLED"}
        if is_vector(profile):
            return self._execute_vector(context, profile, output, blend_path)
        variant = self.layer.strip() or profile.variant_layer.strip()
        if variant:
            try:
                output = variant_output(output, variant)
            except ValueError as ex:
                self.report({"ERROR"}, f"Cannot publish the variant: {ex}")
                return {"CANCELLED"}

        options = None
        issues = run_checks(context)
        if variant:
            issues += variant_issues(context.scene, variant)
        if glb:
            from .glb_export import GlbOptions, glb_issues

            options = GlbOptions(
                face_table=profile.glb_face_table,
                earth_placed=profile.glb_earth_placed,
                apply_modifiers=bool(profile.apply_modifiers),
                variant=variant,
            )
            issues += glb_issues(context.scene, options)
            if not options.earth_placed:  # the CRS is only recorded, never applied
                issues = [
                    Issue(Severity.WARNING, i.code, i.message, i.subject)
                    if i.code == "crs-refused"
                    else i
                    for i in issues
                ]
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
        if not dep.available and not glb:
            self.report({"ERROR"}, f"Cannot publish: {dep.summary}")
            return {"CANCELLED"}
        # M6: painted or cleaned textures are saved before the export reads them.
        try:
            saved = texture_state.save_edited_images(texture_state.source_images(context.scene))
        except texture_state.SaveError as ex:
            self.report({"ERROR"}, f"Cannot publish: {ex}")
            return {"CANCELLED"}
        if saved.count:
            store_issues(props, [i for i in issues if i.code != "image-unsaved"])
        if saved.blend_changed:
            blend_path = None  # the saved .blend no longer holds these images' pixels
        image_records = texture_state.image_records(context.scene)

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
            apply = bool(profile.apply_modifiers)
            extra_record = _extra_record(context, profile.name, props.source_collection.name, apply)
            layer_records_, invalid_layers = layer_records(context.scene)
            if glb:
                from .glb_export import prepare_glb

                prepared = prepare_glb(context, image_dir, options)
                if prepared.ids_made:
                    blend_path = None  # the saved .blend does not have the new object ids
                    _push_undo("GeoTools object ids")
                remedies = prepared.remedies
                scene_export = prepared.scene
                if remedies:
                    extra_record["image_remedies"] = remedies
                request = BuildRequest(
                    output=output,
                    placement_kwargs=placement_kwargs,
                    force=profile.overwrite,
                    validate=False,
                    topology=dict(prepared.topology),
                    extra_record=extra_record,
                    layers=layer_records_,
                    blend_path=blend_path,
                    variant=prepared.variant,
                    images=image_records,
                    extension=prepared.extension,
                    glb_record=prepared.record,
                )
            else:
                present, variant_record = None, None
                if variant:
                    present, variant_record = build_presentation(context.scene, variant)
                scene_export, topology = _extract_scene(
                    context, props.source_collection, image_dir, remedies, present, apply
                )
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
                    layers=layer_records_,
                    blend_path=blend_path,
                    variant=variant_record,
                    images=image_records,
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
        if saved.count:
            notes.append(f"saved {saved.count} edited image{'s' * (saved.count != 1)}")
        if invalid_layers:
            notes.append(
                f"layer{'s' * (len(invalid_layers) != 1)} {', '.join(invalid_layers)} not "
                "recorded: fix them in the Layers panel"
            )
        note = f" ({', '.join(notes)})" if notes else ""
        verb = "Exporting" if glb else "Publishing"
        self.report({"WARNING" if invalid_layers else "INFO"}, f"{verb} {output.name}{note}")
        redraw_ui(context)
        return {"FINISHED"}

    def _execute_vector(
        self,
        context: bpy.types.Context,
        profile: bpy.types.PropertyGroup,
        output: Path,
        blend_path: Path | None,
    ) -> set[str]:
        """A Vectors profile: read the vector collection, then tile it on the worker."""
        global _session
        from . import vector_publish

        if self.layer.strip() or profile.variant_layer.strip():
            self.report({"ERROR"}, "Cannot publish: variants are for .3tz mesh profiles")
            return {"CANCELLED"}
        issues = vector_publish.vector_issues(context, profile)
        if issues:
            self.report({"ERROR"}, f"Cannot publish: {issues[0].message}")
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
            prepared = vector_publish.prepare(context, profile)
            apply = bool(profile.apply_modifiers)
            collection = profile.vector_collection
            extra_record = _extra_record(context, profile.name, "", apply)
            del extra_record["source_collection"]
            extra_record["vector_collection"] = collection.name if collection else None
            extra_record["vector_objects"] = prepared.objects
            request = BuildRequest(
                output=output,
                placement_kwargs=dict(prepared.placement_kwargs),
                force=profile.overwrite,
                validate=profile.validate,
                extra_record=extra_record,
                blend_path=blend_path,
                vector=prepared.source,
            )
            job = BuildJob(None, request, lock=lock)
        except vector_publish.VectorPublishError as ex:
            lock.release()
            self.report({"ERROR"}, f"Cannot publish: {ex}")
            return {"CANCELLED"}
        except Exception as ex:
            lock.release()
            log.exception("publish: vector extraction failed")
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
        _session = _Session(job=job, scene_name=context.scene.name, profile_name=profile.name)
        job.start()
        _start_timer()
        features = prepared.source.features
        text = f"Publishing {output.name} ({features:,} feature{'s' * (features != 1)})"
        if prepared.warnings:
            self.report({"WARNING"}, " ".join([text + ".", *prepared.warnings]))
        else:
            self.report({"INFO"}, text)
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


# --- drawing (the panels are in panels.py) ---------------------------------------------------


_END_ICONS = {"DONE": "CHECKMARK", "FAILED": "ERROR", "CANCELLED": "CANCEL"}


def output_name(scene: bpy.types.Scene, profile: bpy.types.PropertyGroup) -> str | None:
    """File name the profile publishes (its variant's, if it has one), or None if it is
    unset or does not end in the format's suffix."""
    text = profile.output_path.strip()
    if not text:
        return None
    path = Path(text.replace("//", "", 1))
    if path.suffix != profile_suffix(profile):
        return None
    variant = profile.variant_layer.strip()
    if variant:
        try:
            path = variant_output(path, variant)
        except ValueError:
            return None
    return path.name


def publish_state(context: bpy.types.Context) -> str:
    """The Publish panel's header state: BUSY, ERROR, DONE or TODO (ui states)."""
    if is_running():
        return "BUSY"
    profile = active_profile(context.scene)
    if profile is None or not profile.output_path.strip():
        return "TODO"
    status_ = status(context)
    if status_ is not None and status_.state == "FAILED":
        return "ERROR"
    return "DONE" if profile.last_output else "TODO"


def draw_progress(
    layout: bpy.types.UILayout, context: bpy.types.Context, status_: GeoToolsPublishStatus
) -> None:
    box = layout.box()
    label = {k: n for k, n, _d in STATE_ITEMS}.get(status_.state, status_.state)
    if status_.total > 0:
        factor = min(max(status_.done / status_.total, 0.0), 1.0)
        text = f"{status_.phase} {status_.done:,}/{status_.total:,}"
    else:
        factor = 0.0
        text = status_.phase or label
    box.label(text=f"{label} ({format_seconds(status_.elapsed)})", icon="SORTTIME")
    box.progress(factor=factor, type="BAR", text=text)
    if status_.message:
        wrapped(box, context, status_.message)
    box.operator("geotools.cancel_publish", text="Cancel", icon="CANCEL")


def draw_end_status(
    layout: bpy.types.UILayout, context: bpy.types.Context, status_: GeoToolsPublishStatus | None
) -> None:
    """Why the last publish this session failed or was cancelled (a success shows in
    Last Build)."""
    if status_ is None or status_.state not in {"FAILED", "CANCELLED"} or not status_.message:
        return
    box = layout.box()
    box.alert = status_.state == "FAILED"
    wrapped(box, context, status_.message, icon=_END_ICONS[status_.state])


def draw_last_build(
    layout: bpy.types.UILayout, context: bpy.types.Context, profile: bpy.types.PropertyGroup
) -> None:
    col = layout.column(align=True)
    col.label(text=Path(profile.last_output).name, icon="FILE_3D")
    if profile.last_build_utc:
        when = profile.last_build_utc.replace("T", " ").removesuffix("Z")
        info_row(col, "Built", f"{when[:16]} UTC")
        info_row(col, "Took", format_seconds(profile.last_seconds))
    if profile.last_triangles:
        info_row(col, "Triangles", format_count(int(profile.last_triangles)))
    if getattr(profile, "last_features", ""):
        info_row(col, "Features", format_count(int(profile.last_features)))
    if profile.last_tiles:
        info_row(col, "Tiles", format_count(int(profile.last_tiles)))
    glb = Path(profile.last_output).suffix == ".glb"
    row = layout.row(align=True)
    row.operator("geotools.open_output_folder", text="Folder", icon="FILE_FOLDER")
    row.operator("geotools.open_build_record", text="Record", icon="TEXT")
    if glb:
        return
    validation = {
        "OK": ("Passed", "CHECKMARK"),
        "FAILED": ("FAILED", "ERROR"),
        "NONE": ("Not run", "NONE"),
    }[profile.last_validation]
    row = col.row()
    row.alert = profile.last_validation == "FAILED"
    info_row(row, "Validation", validation[0], icon=validation[1])


def draw_preview(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    url = preview_url()
    if url:
        box = layout.box()
        box.label(text=url, icon="URL")
        box.operator("geotools.stop_preview", icon="CANCEL")
        return
    row = layout.row(align=True)
    row.operator("geotools.preview_output", icon="WORLD")
    prefs = addon_preferences(context)
    configured = prefs is not None and not (
        pv.cli_problem(bpy.path.abspath(prefs.rusty_tiles_cli))
        or pv.cesium_problem(bpy.path.abspath(prefs.cesium_dir))
    )
    if not configured:
        op = row.operator("preferences.addon_show", text="", icon="PREFERENCES")
        op.module = ADDON_PACKAGE
        layout.label(text="Preview needs setup (see gear)", icon="INFO")


# --- registration ----------------------------------------------------------------------------

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
