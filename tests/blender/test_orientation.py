"""Normal orientation in Run Checks and before Hydrology, Sun Hours and the sky presets.

Synthetic meshes only: boxes (outward, flipped, one face reversed, mirrored by
a negative scale) and a small terrain, outward or flipped.
"""

from __future__ import annotations

import json
import types

import _harness as h
import bpy
import numpy as np

BOX = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
"""A unit box's quads, counter-clockwise seen from outside."""


def _orient():
    return h.submodule("blender.orientation")


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _object(name, verts, faces, collection, location=(0.0, 0.0, 0.0)) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(map(float, v)) for v in verts], [], [tuple(f) for f in faces])
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def _box(name, collection, x: float, reversed_faces=None) -> bpy.types.Object:
    verts = [(i, j, k) for k in (0, 1) for i, j in ((0, 0), (1, 0), (1, 1), (0, 1))]
    which = range(6) if reversed_faces is None else reversed_faces
    faces = [f[::-1] if i in which else f for i, f in enumerate(BOX)]
    return _object(name, verts, faces, collection, (x, 0.0, 0.0))


def _terrain(name, collection, *, flipped: bool, n: int = 12) -> bpy.types.Object:
    """An n x n quad terrain, gently curved, normals up (or down when ``flipped``)."""
    u = np.linspace(-5.0, 5.0, n + 1)
    x, y = np.meshgrid(u, u, indexing="ij")
    verts = np.stack([x.ravel(), y.ravel(), 0.05 * (x**2 + y**2).ravel()], axis=1)
    i, j = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
    a = (i * (n + 1) + j).ravel()
    quads = np.stack([a, a + n + 1, a + n + 2, a + 1], axis=1)
    if flipped:
        quads = quads[:, ::-1]
    return _object(name, verts, quads, collection)


def _normal_issues() -> dict[str, list[tuple[str, str]]]:
    found: dict[str, list[tuple[str, str]]] = {}
    for item in h.project().issues:
        if item.code.startswith("normals-"):
            found.setdefault(item.subject, []).append((item.severity, item.code))
    return found


# --- Run Checks ---------------------------------------------------------------------------


def test_run_checks_reports_inward_and_inconsistent_normals() -> None:
    h.reset_scene()
    source = _source()
    _box("Outward", source, 0.0, reversed_faces=())
    _box("Flipped", source, 3.0)
    _box("Mixed", source, 6.0, reversed_faces={1})
    mirrored = _box("Mirrored", source, 10.0, reversed_faces=())
    mirrored.scale = (-1.0, 1.0, 1.0)  # Blender still shows its normals outward
    _terrain("Ground", source, flipped=False).location = (0.0, 20.0, 0.0)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    assert _normal_issues() == {
        "Flipped": [("WARNING", "normals-inward")],
        "Mixed": [("WARNING", "normals-inconsistent")],
    }
    item = next(i for i in h.project().issues if i.code == "normals-inconsistent")
    assert (
        "Recalculate Outside" in item.message
        and "4 edges" in item.message
        and item.message.startswith("Mixed: 4 edges")
    )
    item = next(i for i in h.project().issues if i.code == "normals-inward")
    assert "Flip" in item.message
    # The rays' temporary objects are gone.
    assert not [o.name for o in bpy.data.objects if o.name.startswith("GeoTools Raycast")]
    assert not [g.name for g in bpy.data.node_groups if g.name.startswith("GeoTools Raycast")]


def test_run_checks_finds_a_flipped_terrain_even_under_another_object() -> None:
    h.reset_scene()
    source = _source()
    _terrain("Ground", source, flipped=True)
    # A roof over part of it, from another object: it shades some rays, not all.
    _object("Roof", [(-5, -5, 4), (0, -5, 4), (0, 5, 4), (-5, 5, 4)], [(0, 1, 2, 3)], source)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    found = _normal_issues()
    assert found["Ground"] == [("WARNING", "normals-inward")], found


def test_the_checks_panel_never_computes_orientation() -> None:
    h.reset_scene()
    source = _source()
    _box("Flipped", source, 0.0)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    module = _orient()
    saved = module.orientations

    def refuse(*_args, **_kwargs):
        raise AssertionError("a panel computed the normal orientation")

    module.orientations = refuse
    try:
        panels = h.submodule("blender.panels")
        calls = h.draw(panels.GEOTOOLS_PT_checks)
        h.draw_header(panels.GEOTOOLS_PT_checks)
    finally:
        module.orientations = saved
    assert ("template_list", "GEOTOOLS_UL_issues", "NONE") in calls, calls


def test_objects_in_edit_mode_are_skipped() -> None:
    h.reset_scene()
    source = _source()
    obj = _box("Flipped", source, 0.0)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    try:
        assert _orient().orientations(bpy.context, [obj], sky=True) == []
    finally:
        bpy.ops.object.mode_set(mode="OBJECT")


# --- before the analyses --------------------------------------------------------------------


def test_hydrology_warns_about_a_flipped_terrain_and_still_runs() -> None:
    h.reset_scene()
    source = _source()
    _terrain("Ground", source, flipped=True)
    props = bpy.context.scene.geotools_hydrology
    props.surface = "SOURCE"
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None and result == {"FINISHED"}, error
    notes = json.loads(props.result)["notes"]
    assert any("Ground: 100% of the area faces down" in n for n in notes), notes
    timings = json.loads(props.result)["timings"]
    assert "normals" in timings, timings

    h.reset_scene()
    _terrain("Ground", _source(), flipped=False)
    props.surface = "SOURCE"
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None and result == {"FINISHED"}, error
    assert not [n for n in json.loads(props.result)["notes"] if "faces down" in n]


def test_sun_hours_warns_about_inconsistent_normals() -> None:
    h.reset_scene()
    source = _source()
    _box("Mixed", source, 0.0, reversed_faces={1})
    props = h.project()
    props.placement_mode = "LOCAL"
    props.anchor_lon, props.anchor_lat = "0.0", "0.0"
    analysis = bpy.context.scene.geotools_analysis
    analysis.sun.year, analysis.sun.month, analysis.sun.day = 2026, 3, 20
    analysis.sun_hours.surface = "SOURCE"
    analysis.sun_hours.step_minutes = 60
    result, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert result == {"FINISHED"}, error or analysis.sun_hours.error
    notes = json.loads(analysis.sun_hours.result)["notes"]
    assert any(n.startswith("Mixed: ") and "Recalculate Outside" in n for n in notes), notes


def test_sky_presets_warn_and_other_presets_do_not() -> None:
    h.reset_scene()
    source = _source()
    ground = _terrain("Ground", source, flipped=True)
    for obj in bpy.data.objects:
        obj.select_set(obj == ground)
    bpy.context.view_layer.objects.active = ground
    cls = h.submodule("blender.analysis_presets").GEOTOOLS_OT_analysis_add

    def run(preset: str, engine: str) -> list[str]:
        recorder = h.ReportRecorder()
        recorder.preset, recorder.name, recorder.north, recorder.engine = preset, "", "GRID", engine
        recorder.occluders = ""
        defaults = h.submodule("blender.analysis_presets")._default
        for prop, name in (
            ("max_distance", "Max Distance"),
            ("directions", "Directions"),
            ("steps", "Steps"),
            ("ray_offset", "Ray Offset"),
        ):
            setattr(recorder, prop, defaults("sky_view_factor", name))
        recorder.fast_inputs = types.MethodType(cls.fast_inputs, recorder)
        recorder._execute_fast = types.MethodType(cls._execute_fast, recorder)
        assert cls.execute(recorder, bpy.context) == {"FINISHED"}, recorder.reports
        return [m for levels, m in recorder.reports if "WARNING" in levels]

    assert any("faces down" in w for w in run("sky_view_factor", "GEONODES"))
    assert any("faces down" in w for w in run("openness_positive", "BAKE"))
    assert run("slope", "GEONODES") == []
