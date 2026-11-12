"""Publish: refusals, an end-to-end publish, cancel, overwrite, preview.

Background Blender runs no timers, so the tests wait with ``job.join()`` and
then call ``publish.update_from_job()`` themselves, as the timer would.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import _harness as h
import bpy
import numpy as np

JOIN_SECONDS = 180.0


def _available() -> bool:
    return h.CONFIG.expect == "available"


def _publish_mod():
    return h.submodule("blender.publish")


@contextmanager
def _tmpdir():
    path = Path(tempfile.mkdtemp(prefix="geotools-publish-test-"))
    try:
        yield path
    finally:
        _publish_mod().shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


def _textured_grid(n: int = 6) -> bpy.types.Object:
    """An n x n grid of quads with UVs and an image-textured material, in a source
    collection, placed with an anchor."""
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection

    xs, ys = np.meshgrid(np.arange(n + 1, dtype=np.float32), np.arange(n + 1, dtype=np.float32))
    co = np.stack([xs.ravel(), ys.ravel(), 0.1 * np.sin(xs.ravel())], axis=1).astype(np.float32)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)

    mesh = bpy.data.meshes.new("Grid")
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    uv = mesh.uv_layers.new(name="UVMap")
    uv.data.foreach_set("uv", (co[quads.ravel(), :2] / n).astype(np.float32).ravel())
    mesh.update()
    mesh.validate()

    image = bpy.data.images.new("Checker", 16, 16, alpha=False)
    px = np.indices((16, 16)).sum(axis=0) % 2
    rgba = np.ones((16, 16, 4), np.float32)
    rgba[..., 0] = px
    rgba[..., 1] = 0.5
    image.pixels.foreach_set(rgba.ravel())
    # Checks refuse an image that is neither on disk nor packed.
    image.pack()
    material = bpy.data.materials.new("Textured")
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next(nd for nd in tree.nodes if nd.type == "BSDF_PRINCIPLED")
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    mesh.materials.append(material)

    obj = bpy.data.objects.new("Grid", mesh)
    collection.objects.link(obj)

    props = h.project()
    props.placement_mode = "ANCHOR"
    p = h.submodule("blender.properties")
    p.write_number(props, "anchor_lon", 153.02)
    p.write_number(props, "anchor_lat", -27.47)
    p.write_number(props, "anchor_height", 10.0)
    return obj


def _add_profile(output: str, **settings: object) -> bpy.types.PropertyGroup:
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    profile = h.project().build_profiles[h.project().build_profile_index]
    profile.output_path = output
    profile.profile = "FAST"
    for key, value in settings.items():
        setattr(profile, key, value)
    return profile


def _status():
    return bpy.context.window_manager.geotools_publish


def _finish():
    """Wait for the job, then run the timer function as Blender would."""
    publish = _publish_mod()
    job = publish.active_job()
    assert job is not None, "no publish job is running"
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    assert publish.update_from_job() is None
    assert not publish.is_running()
    assert not bpy.ops.geotools.cancel_publish.poll()
    return job


def _panel_labels() -> list[str]:
    """Labels of the Publish panel and, if shown, its Last Build sub-panel."""
    panels = h.submodule("blender.panels")
    calls = h.draw(panels.GEOTOOLS_PT_publish)
    if panels.GEOTOOLS_PT_publish_last.poll(bpy.context):
        calls += h.draw(panels.GEOTOOLS_PT_publish_last)
    return [text for kind, text, _icon in calls if kind in {"label", "progress"}]


# --- registration and drawing ----------------------------------------------------------


def test_publish_registered() -> None:
    for idname in (
        "geotools.publish",
        "geotools.cancel_publish",
        "geotools.open_output_folder",
        "geotools.open_build_record",
        "geotools.preview_output",
        "geotools.stop_preview",
    ):
        assert h.operator_available(idname), idname
    assert hasattr(bpy.types.WindowManager, "geotools_publish")
    panels = h.submodule("blender.panels")
    assert panels.GEOTOOLS_PT_publish in panels.classes
    assert panels.classes[-1] is panels.GEOTOOLS_PT_about


def test_panel_without_profile() -> None:
    h.reset_scene()
    panels = h.submodule("blender.panels")
    calls = h.draw(panels.GEOTOOLS_PT_publish)
    assert "Add a build profile" in h.label_text(calls), calls
    assert ("operator", "geotools.build_profile_add", "ADD") in calls
    assert ("operator", "geotools.publish", "EXPORT") not in calls
    assert h.draw_header(panels.GEOTOOLS_PT_publish) == "RADIOBUT_OFF"
    # Settings and Last Build (with Preview) wait for a profile and a build.
    assert not panels.GEOTOOLS_PT_publish_settings.poll(bpy.context)
    assert not panels.GEOTOOLS_PT_publish_last.poll(bpy.context)
    assert not bpy.ops.geotools.preview_output.poll()


# --- refusals ----------------------------------------------------------------------------


def test_publish_refuses_without_profile() -> None:
    h.reset_scene()
    _textured_grid(2)
    _result, error = h.call_op(bpy.ops.geotools.publish)
    assert error and "add a build profile" in error, error
    assert not _publish_mod().is_running()


def test_publish_refuses_without_source() -> None:
    h.reset_scene()
    _add_profile("/tmp/never.3tz")
    _result, error = h.call_op(bpy.ops.geotools.publish)
    assert error and "source collection" in error, error


def test_publish_refuses_with_check_errors() -> None:
    h.reset_scene()
    obj = _textured_grid(2)
    obj.shape_key_add(name="Basis")
    with _tmpdir() as tmp:
        _add_profile(str(tmp / "site.3tz"))
        _result, error = h.call_op(bpy.ops.geotools.publish)
        assert error and "Checks panel" in error, error
        assert ("ERROR", "shape-keys") in [(i.severity, i.code) for i in h.project().issues]
        assert not _publish_mod().is_running()
        assert not (tmp / "site.geotools").exists()


def test_publish_refuses_relative_output_on_unsaved_blend() -> None:
    h.reset_scene()
    if bpy.data.is_saved:  # an earlier test saved the file; start an unsaved one
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()
    assert not bpy.data.is_saved
    _textured_grid(2)
    _add_profile("//site.3tz")
    _result, error = h.call_op(bpy.ops.geotools.publish)
    assert error and "save the .blend" in error, error
    labels = _panel_labels()
    assert "Save the .blend first (// path)" in labels, labels


def test_publish_refuses_bad_output_suffix() -> None:
    h.reset_scene()
    _textured_grid(2)
    with _tmpdir() as tmp:
        _add_profile(str(tmp / "site.glb"))
        _result, error = h.call_op(bpy.ops.geotools.publish)
        assert error and ".3tz" in error, error


def test_publish_needs_rusty_tiles() -> None:
    if _available():
        return
    h.reset_scene()
    _textured_grid(2)
    with _tmpdir() as tmp:
        _add_profile(str(tmp / "site.3tz"))
        _result, error = h.call_op(bpy.ops.geotools.publish)
        issues = [(i.severity, i.code, i.message) for i in h.project().issues]
        assert error and "rusty-tiles not available" in error, (error, issues)


# --- end to end (needs the wheel) ----------------------------------------------------------


def test_publish_end_to_end() -> None:
    if not _available():
        return
    h.reset_scene()
    _textured_grid(6)
    layers = h.submodule("core.layers")
    slope = layers.LayerSpec("slope", layers.Domain.FACE, layers.DataType.FLOAT, units="deg")
    h.submodule("blender.layers").ensure_layer(bpy.context.scene, slope)
    with _tmpdir() as tmp:
        output = tmp / "site.3tz"
        profile = _add_profile(str(output))
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        publish = _publish_mod()
        assert publish.is_running()
        assert not bpy.ops.geotools.publish.poll(), "a second publish must wait"
        assert bpy.ops.geotools.cancel_publish.poll()
        labels = _panel_labels()
        assert any(t.startswith(("Starting", "Writing", "Tiling", "Validating")) for t in labels)

        job = _finish()
        status = _status()
        assert status.state == "DONE", (status.state, status.message, job.error)
        assert output.is_file()
        rusty_tiles = __import__("rusty_tiles")
        assert rusty_tiles.validate(output)["ok"]

        staging = tmp / "site.geotools"
        record = json.loads((staging / "build.json").read_text())
        assert record["schema"] == 2
        assert record["layers"] == [layers.to_record(slope)]
        assert record["source"] == {
            "glb_sha256": record["source_glb"]["sha256"],
            "blend_sha256": None,  # never saved
            "images": record["source"]["images"],
        }
        # M6: the packed checker texture, hashed by its packed bytes.
        assert [(i["name"], i["origin"]) for i in record["source"]["images"]] == [
            ("Checker", "packed")
        ]
        assert len(record["source"]["images"][0]["sha256"]) == 64
        assert record["placement"]["crs_epsg"] is None
        assert record["output"] == str(output)
        assert record["settings"]["profile"] == "fast"
        assert record["placement"]["anchor"] == [153.02, -27.47, 10.0]
        assert record["validation"]["ok"] is True
        assert record["source_glb"]["triangles"] == 72
        assert record["report_summary"]["triangles"] == 72
        assert record["geotools"]["build_profile"] == profile.name
        assert record["geotools"]["source_collection"] == "Source"
        assert record["rusty_tiles_version"] == h.CONFIG.expect_version
        assert not (staging / "images").exists(), "extracted images were left behind"
        assert not (staging / "work").exists()

        facemap = h.submodule("core.facemap")
        face_map = facemap.load_face_map(staging / "facemap.npz")
        assert face_map.object_names == ("Grid",)
        assert set(face_map.topology) == {"Grid"}
        obj_index, polygon = face_map.lookup_many(np.array([0, 0]), np.array([0, 71]))
        assert set(obj_index.tolist()) == {0}
        assert 0 <= polygon.min() and polygon.max() < 36

        profile = h.project().build_profiles[profile.name]
        assert profile.last_output == str(output)
        assert profile.last_record == str(staging / "build.json")
        assert profile.last_validation == "OK"
        assert profile.last_triangles == "72"
        assert profile.last_tiles == str(record["report_summary"]["tiles"])
        assert profile.last_build_utc.endswith("Z")
        assert bpy.ops.geotools.open_build_record.poll()
        assert bpy.ops.geotools.open_output_folder.poll()

        labels = _panel_labels()
        assert "Passed" in labels and "Validation" in labels, labels
        assert "Triangles" in labels and "72" in labels, labels
        panels = h.submodule("blender.panels")
        assert h.draw_header(panels.GEOTOOLS_PT_publish) == "CHECKMARK"
        calls = h.draw(panels.GEOTOOLS_PT_publish_last)
        # Preview sits with the last build; unconfigured, it offers the preferences.
        assert ("operator", "geotools.preview_output", "WORLD") in calls
        assert ("operator", "preferences.addon_show", "PREFERENCES") in calls


@contextmanager
def _gated_tiling():
    """Hold rusty-tiles at its first progress event until the test opens the gate."""
    job_mod = h.submodule("publish.job")
    original = job_mod._rusty_tiles
    real = original()
    reached, gate = threading.Event(), threading.Event()

    def mesh_to_tileset(input, output, *, callback, **kwargs):
        job = _publish_mod().active_job()

        def gated(event):
            if event.get("event") == "progress" and not gate.is_set():
                reached.set()
                while not gate.wait(0.05):
                    if job is None or job.cancel_requested:
                        break
            callback(event)

        return real.mesh_to_tileset(input, output, callback=gated, **kwargs)

    job_mod._rusty_tiles = lambda: SimpleNamespace(
        mesh_to_tileset=mesh_to_tileset, validate=real.validate
    )
    try:
        yield reached, gate
    finally:
        gate.set()
        job_mod._rusty_tiles = original


def test_publish_cancel() -> None:
    if not _available():
        return
    h.reset_scene()
    _textured_grid(4)
    with _tmpdir() as tmp, _gated_tiling() as (reached, gate):
        output = tmp / "site.3tz"
        profile = _add_profile(str(output))
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        assert reached.wait(60), "tiling never started"
        publish = _publish_mod()
        publish.update_from_job()
        assert _status().state == "TILING", _status().state
        labels = _panel_labels()
        assert any(t.startswith("Tiling") for t in labels), labels
        assert bpy.ops.geotools.cancel_publish() == {"FINISHED"}
        gate.set()
        job = _finish()
        assert job.state.name == "CANCELLED", (job.state, job.error)
        assert _status().state == "CANCELLED"
        assert not output.exists()
        assert not (tmp / "site.geotools" / "build.json").exists()
        assert not h.project().build_profiles[profile.name].last_output


def test_publish_overwrite() -> None:
    if not _available():
        return
    h.reset_scene()
    _textured_grid(3)
    with _tmpdir() as tmp:
        output = tmp / "site.3tz"
        profile = _add_profile(str(output))
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        assert _finish().state.name == "DONE"
        archive = output.read_bytes()
        record = (tmp / "site.geotools" / "build.json").read_bytes()
        first_time = h.project().build_profiles[profile.name].last_build_utc

        # Overwrite off: fails cleanly, old archive and record untouched.
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        job = _finish()
        assert job.state.name == "FAILED"
        assert job.error.startswith("OutputExistsError:"), job.error
        assert _status().state == "FAILED"
        assert _status().message.startswith("OutputExistsError:")
        assert output.read_bytes() == archive
        assert (tmp / "site.geotools" / "build.json").read_bytes() == record
        assert any(t.startswith("OutputExistsError:") for t in _panel_labels())

        # Overwrite on: succeeds.
        h.project().build_profiles[profile.name].overwrite = True
        time.sleep(1.1)  # last_build_utc has one-second resolution
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        job = _finish()
        assert job.state.name == "DONE", job.error
        assert json.loads((tmp / "site.geotools" / "build.json").read_text())["settings"]["force"]
        assert h.project().build_profiles[profile.name].last_build_utc != first_time


def test_loading_a_file_detaches_the_job() -> None:
    """The job finishes after another file is loaded, but writes no results into it."""
    if not _available():
        return
    h.reset_scene()
    _textured_grid(3)
    with _tmpdir() as tmp, _gated_tiling() as (reached, gate):
        output = tmp / "site.3tz"
        _add_profile(str(output))
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        assert reached.wait(60)
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        publish = _publish_mod()
        assert publish.is_running()
        assert bpy.app.timers.is_registered(publish.update_from_job), "timer lost on load"
        _add_profile(str(output))  # same name and path in the new file
        gate.set()
        job = _finish()
        assert job.state.name == "DONE", job.error
        assert output.is_file()
        assert not h.project().build_profiles[0].last_output


def test_disable_cancels_running_publish() -> None:
    if not _available():
        return
    h.reset_scene()
    _textured_grid(3)
    with _tmpdir() as tmp, _gated_tiling() as (reached, _gate):
        output = tmp / "site.3tz"
        _add_profile(str(output))
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        assert reached.wait(60)
        publish = _publish_mod()
        job = publish.active_job()
        update = publish.update_from_job
        h.disable()
        assert job.join(0), "unregister did not wait for the cancelled job"
        assert job.state.name == "CANCELLED", (job.state, job.error)
        assert not bpy.app.timers.is_registered(update)
        assert not output.exists()
    h.enable()


# --- preview -----------------------------------------------------------------------------


def _preview_tools() -> tuple[str, str] | None:
    cli = os.environ.get("GEOTOOLS_TEST_RUSTY_TILES_CLI", "")
    cesium = os.environ.get("GEOTOOLS_TEST_CESIUM", "")
    if cli and cesium:
        return cli, cesium
    return None


def test_preview_hint_without_tools() -> None:
    h.reset_scene()
    prefs = _publish_mod().addon_preferences()
    prefs.rusty_tiles_cli = ""
    prefs.cesium_dir = ""
    problem = _publish_mod().preview_problem(bpy.context)
    assert problem and "rusty-tiles CLI" in problem, problem
    assert not bpy.ops.geotools.preview_output.poll()


def test_preview_launch() -> None:
    tools = _preview_tools()
    if not _available() or tools is None:
        print("    skipped: set GEOTOOLS_TEST_RUSTY_TILES_CLI and GEOTOOLS_TEST_CESIUM")
        return
    import urllib.request

    h.reset_scene()
    _textured_grid(3)
    publish = _publish_mod()
    prefs = publish.addon_preferences()
    prefs.rusty_tiles_cli, prefs.cesium_dir = tools
    publish.OPEN_BROWSER = False
    try:
        with _tmpdir() as tmp:
            _add_profile(str(tmp / "site.3tz"))
            assert bpy.ops.geotools.publish() == {"FINISHED"}
            assert _finish().state.name == "DONE"
            assert bpy.ops.geotools.preview_output.poll()
            assert bpy.ops.geotools.preview_output() == {"FINISHED"}
            url = publish.preview_url()
            assert url, publish._preview.error
            print(f"    preview at {url}")
            with urllib.request.urlopen(url, timeout=10) as response:
                assert response.status == 200
            assert (tmp / "site.geotools" / "preview" / "tileset.json").is_file()
            server = publish._preview
            assert any(t == url for t in _panel_labels())
            h.disable()  # unregister stops the server
            assert not server.running
            h.enable()
    finally:
        publish = _publish_mod()
        publish.OPEN_BROWSER = True
        publish.stop_preview()
        prefs = publish.addon_preferences()
        prefs.rusty_tiles_cli = prefs.cesium_dir = ""
