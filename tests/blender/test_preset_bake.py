"""M5 fast (bake) paths of the sky-view factor, openness and cumulative viewshed presets.

Add Analysis with engine "Fast (bake)" computes the preset's rays with Cycles
Raycast shader nodes in one EMIT bake (``blender.ray_bake``) and stores a
layer. Checked against the live Geometry Nodes preset and against exact
answers: a level plane (SVF 1, openness 90°), a V-trench with 30° walls (SVF
0.6714 at the bottom, and every corner off the mesh boundary against an exact
ray-surface intersection run through the same bisection), and a cumulative
viewshed behind a wall against batch 2's segment-box test.
"""

from __future__ import annotations

import math

import _harness as h
import bpy
import numpy as np
from test_analysis_presets import (
    _add,
    _clear_trees,
    _grid,
    _inside,
    _layers,
    _reset,
    _select,
    _values,
)
from test_analysis_presets_2 import EYE, OFFSET, WALL, _blocked, _box, _object, _walled_ground

TAN30 = math.tan(math.radians(30.0))
RESOLUTION = 90.0 / 2 ** (8 - 1)  # Steps 8
HORIZON = ("sky_view_factor", "openness_positive", "openness_negative")


def _fast():
    return h.submodule("blender.preset_bake")


def _core():
    return h.submodule("analysis.preset_bake")


def _spec(name: str):
    L = _layers()
    return L.spec_from_props(L.find_layer(bpy.context.scene, name))


def _no_leftovers() -> None:
    prefix = "GeoTools Fast"
    for collection in (
        bpy.data.node_groups,
        bpy.data.materials,
        bpy.data.objects,
        bpy.data.meshes,
        bpy.data.images,
    ):
        assert not [d.name for d in collection if d.name.startswith(prefix)], collection


def _corners(obj: bpy.types.Object) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Vertex positions, vertex normals (float64) and each corner's vertex."""
    mesh = obj.data
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    normals = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertex_normals.foreach_get("vector", normals)
    loops = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", loops)
    return co.reshape(-1, 3).astype(np.float64), normals.reshape(-1, 3).astype(np.float64), loops


def _exact_trench(preset: str, obj: bpy.types.Object, half: float) -> np.ndarray:
    """The preset's bisection with exact ray-trench intersections, per corner (binary64)."""
    core = _core()
    co, normals, loops = _corners(obj)
    search = core.horizon_search(preset, {})
    origin = co + search.side * normals
    azimuths = core.azimuth_vectors(search.directions)

    def hits(d: int, theta: np.ndarray) -> np.ndarray:
        rad = np.radians(theta)
        dirs = azimuths[d][None, :] * np.cos(rad)[:, None]
        dirs[:, 2] = np.sin(rad)
        hit = np.zeros(len(theta), bool)
        for side in (1.0, -1.0):  # the wall z = side * x * tan 30°, where side * x >= 0
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (side * TAN30 * origin[:, 0] - origin[:, 2]) / (
                    dirs[:, 2] - side * TAN30 * dirs[:, 0]
                )
            p = origin + t[:, None] * dirs
            hit |= (
                (t > 0)
                & (t <= search.max_distance)
                & (side * p[:, 0] >= -1e-12)
                & (np.abs(p[:, 0]) <= half)
                & (np.abs(p[:, 1]) <= half)
            )
        return hit

    angle, sine = core.bisect_horizon(search, hits, len(co))
    return core.horizon_values(preset, angle, sine, search.directions)[loops]


# --- sky-view factor and openness ------------------------------------------------------------


def test_fast_horizon_on_a_plane() -> None:
    _reset()
    flat = _grid("Flat", 40, 8.0, lambda x, y: 0.0 * x)
    other = _grid("Elsewhere", 2, 1.0, lambda x, y: 0.0 * x + 0.5, select=False)
    engine = bpy.context.scene.render.engine
    for preset in HORIZON:
        assert _add(preset, engine="BAKE") == {"FINISHED"}
    assert not flat.modifiers  # computed once: no modifier
    spec = _spec("sky_view_factor")
    assert (spec.domain, spec.origin, spec.units) == ("CORNER", "ANALYSIS", "ratio")
    record = spec.provenance
    assert record["tool"] == "analysis_bake" and record["parameters"]["engine"] == "bake"
    assert "Cycles Raycast" in record["parameters"]["method"]
    assert record["parameters"]["inputs"] == _core().default_inputs("sky_view_factor")
    assert record["parameters"]["samples"] == 41 * 41
    assert record["parameters"]["rays_per_sample"] == 16 * 8
    assert record["parameters"]["objects"] == ["Flat"]
    item = _layers().find_layer(bpy.context.scene, "sky_view_factor")
    assert item.presentation.palette == "greys" and item.presentation.range_min == "0.6"

    svf, where = _values(flat, "sky_view_factor")
    np.testing.assert_array_equal(svf, 1.0)
    inside = _inside(where, 1.9)
    for layer in ("openness_positive", "openness_negative"):
        values, _ = _values(flat, layer)
        np.testing.assert_allclose(values[inside], 90.0 + RESOLUTION / 2, atol=1e-4)
        assert values.max() > 120.0  # the edge: nothing within reach outwards
    _no_leftovers()
    assert bpy.context.scene.render.engine == engine
    assert not other.hide_render  # hidden from the bake only

    # The live presets give the same values away from the edge.
    for preset in HORIZON:
        assert _add(preset, name=f"{preset}_live") == {"FINISHED"}
        live, _ = _values(flat, f"{preset}_live")
        fast, _ = _values(flat, preset)
        np.testing.assert_allclose(fast[inside], live[inside], rtol=0, atol=1e-5)
    _clear_trees()


def test_fast_horizon_of_a_v_trench() -> None:
    _reset()
    half = 3.0
    trench = _grid("Trench", 60, 2 * half, lambda x, y: np.abs(x) * TAN30)
    azimuths = np.arange(16) * 2 * math.pi / 16
    horizon = np.arctan(np.abs(np.sin(azimuths)) * TAN30)
    for preset in HORIZON:
        assert _add(preset, engine="BAKE") == {"FINISHED"}
        assert _add(preset, name=f"{preset}_live") == {"FINISHED"}

    svf, where = _values(trench, "sky_view_factor")
    bottom = (np.abs(where[:, 0]) < 1e-4) & (np.abs(where[:, 1]) < 0.9)
    expected = 1.0 - np.mean(np.sin(horizon))  # 0.6714
    assert np.all(np.abs(svf[bottom] - expected) < 0.006), (svf[bottom].min(), expected)
    positive, _ = _values(trench, "openness_positive")
    negative, _ = _values(trench, "openness_negative")
    mean_horizon = math.degrees(np.mean(horizon))
    assert np.all(np.abs(positive[bottom] - (90.0 - mean_horizon)) < RESOLUTION)
    assert np.all(np.abs(negative[bottom] - (90.0 + mean_horizon)) < RESOLUTION)

    # Away from the edge (more than Max Distance in y, and the walls' feet) the
    # live preset gives exactly the same values.
    middle = (np.abs(where[:, 0]) < 1.0) & (np.abs(where[:, 1]) < 0.9)
    edge = np.minimum(half - np.abs(where[:, 0]), half - np.abs(where[:, 1]))
    for preset in HORIZON:
        fast, _ = _values(trench, preset)
        live, _ = _values(trench, f"{preset}_live")
        # (The two sum their directions in float32 in different orders.)
        np.testing.assert_allclose(fast[middle], live[middle], rtol=0, atol=1e-5)
        # Every corner off the boundary matches an exact intersection, also where
        # rays leave the mesh; a ray from a boundary vertex along the boundary
        # grazes it, which either engine may call a hit.
        exact = _exact_trench(preset, trench, half)
        off = edge > 1e-4
        np.testing.assert_allclose(fast[off], exact[off], atol=2e-4, err_msg=preset)
    _no_leftovers()
    _clear_trees()


# --- the cumulative viewshed ---------------------------------------------------------------------


def _expected_counts(points: np.ndarray, observers, boxes) -> np.ndarray:
    expected = np.zeros(len(points))
    for x, y, z in observers:
        eye = np.broadcast_to(np.array([x, y, z + EYE]), points.shape)
        hidden = np.zeros(len(points), bool)
        for lo, hi in boxes:
            hidden |= _blocked(points, eye, np.asarray(lo), np.asarray(hi))
            grown = _blocked(points, eye, np.asarray(lo) - 1e-3, np.asarray(hi) + 1e-3)
            shrunk = _blocked(points, eye, np.asarray(lo) + 1e-3, np.asarray(hi) - 1e-3)
            assert not (grown != shrunk).any()  # no face centre on a shadow edge
        expected += ~hidden
    return expected


def test_fast_cumulative_viewshed_matches_the_segment_box_test() -> None:
    _reset()
    ground = _walled_ground()
    on_ground = [(0.0, 0.0, 0.0), (5.0, 1.0, 0.0), (-4.0, -6.0, 0.0)]
    # Draped: the points start 5 m up and drop onto the ground.
    observers = _object("Observers", [(x, y, z + 5.0) for x, y, z in on_ground])
    # Not an occluder: only the surface and the Occluders collection block sight.
    co, faces = _box((-1.0, -1.0, 0.0), (1.0, 1.0, 1.5))
    _object("Bystander", co, faces=faces)
    _select(ground, observers)
    assert _add("viewshed_cumulative", engine="BAKE", drape=True) == {"FINISHED"}
    assert not ground.modifiers
    spec = _spec("viewshed_cumulative")
    assert (spec.domain, spec.origin, spec.units) == ("FACE", "ANALYSIS", "count")
    parameters = spec.provenance["parameters"]
    assert parameters["inputs"]["Observers"] == "Observers" and parameters["inputs"]["Drape"]
    assert parameters["rays_per_sample"] == 3 and parameters["bakes"] == 1

    values, where = _values(ground, "viewshed_cumulative")
    ground_faces = (np.abs(where[:, 2]) < 1e-6) & ~(
        (np.abs(where[:, 0] - 2.2) < 0.21) & (np.abs(where[:, 1]) < 3.01)
    )
    points = where[ground_faces].astype(np.float64) + (0.0, 0.0, OFFSET)
    expected = _expected_counts(points, on_ground, [WALL])
    np.testing.assert_array_equal(values[ground_faces], expected)
    assert set(np.unique(expected)) == {1.0, 2.0, 3.0}

    # The live preset (Drape on) agrees on every face, the wall's too.
    _select(ground, observers)
    assert _add("viewshed_cumulative", name="viewshed_live") == {"FINISHED"}
    modifier = ground.modifiers[0]
    h.submodule("blender.analysis_presets").set_input(modifier, "Drape", True)
    live, _ = _values(ground, "viewshed_live")
    np.testing.assert_array_equal(values, live)
    ground.modifiers.remove(modifier)

    # More observers than a bake holds: the bakes add up to the same counts.
    fast = _fast()
    saved = fast.RAYS_PER_BAKE
    fast.RAYS_PER_BAKE = 2
    try:
        result = fast.compute(
            bpy.context,
            "viewshed_cumulative",
            [ground],
            name="viewshed_batches",
            inputs={"Drape": True},
            observers=observers,
        )
    finally:
        fast.RAYS_PER_BAKE = saved
    assert result["bakes"] == 2 and result["observers"] == 3
    batches, _ = _values(ground, "viewshed_batches")
    np.testing.assert_array_equal(batches, values)

    # An Occluders collection blocks sight as well.
    shed = ((-6.0, 3.0, 0.0), (-5.0, 4.0, 2.5))
    occluders = bpy.data.collections.new("Sheds")
    bpy.context.scene.collection.children.link(occluders)
    co, faces = _box(*shed)
    box = _object("Shed", co, faces=faces)
    bpy.context.scene.collection.objects.unlink(box)
    occluders.objects.link(box)
    fast.compute(
        bpy.context,
        "viewshed_cumulative",
        [ground],
        name="viewshed_sheds",
        inputs={"Drape": True},
        observers=observers,
        occluders=occluders,
    )
    values, where = _values(ground, "viewshed_sheds")
    under = (np.abs(where[:, 0] + 5.5) < 0.51) & (np.abs(where[:, 1] - 3.5) < 0.51)
    keep = ground_faces & ~under
    points = where[keep].astype(np.float64) + (0.0, 0.0, OFFSET)
    expected = _expected_counts(points, on_ground, [WALL, shed])
    np.testing.assert_array_equal(values[keep], expected)
    assert (expected < _expected_counts(points, on_ground, [WALL])).any()
    _no_leftovers()
    _clear_trees()


# --- names, failures and the menu -----------------------------------------------------------------


def test_fast_and_live_layers_keep_apart() -> None:
    _reset()
    flat = _grid("Flat", 8, 4.0, lambda x, y: 0.0 * x)
    assert _add("sky_view_factor") == {"FINISHED"}  # live, layer sky_view_factor
    _, error = h.call_op(bpy.ops.geotools.analysis_add, preset="sky_view_factor", engine="BAKE")
    assert error is not None and "not a fast" in error, error
    assert _add("sky_view_factor", engine="BAKE", name="svf_fast") == {"FINISHED"}
    _, error = h.call_op(bpy.ops.geotools.analysis_add, preset="sky_view_factor", name="svf_fast")
    assert error is not None and "not a Sky-View Factor layer" in error, error
    # Running it again replaces the values and the recorded inputs.
    assert _add("sky_view_factor", engine="BAKE", name="svf_fast", directions=8) == {"FINISHED"}
    assert _spec("svf_fast").provenance["parameters"]["inputs"]["Directions"] == 8
    assert len(flat.modifiers) == 1

    # A failed bake leaves no new layer, and puts the scene back.
    ray_bake = h.submodule("blender.ray_bake")
    run_bake = ray_bake._run_bake

    def fail(**_kwargs):
        raise RuntimeError("no bake today")

    ray_bake._run_bake = fail
    try:
        _, error = h.call_op(
            bpy.ops.geotools.analysis_add, preset="openness_positive", engine="BAKE"
        )
    finally:
        ray_bake._run_bake = run_bake
    assert error is not None and "no bake today" in error, error
    assert _layers().find_layer(bpy.context.scene, "openness_positive") is None
    _no_leftovers()

    calls = h.draw(bpy.types.GEOTOOLS_MT_analysis_presets)
    operators = [c for c in calls if c[0] == "operator"]
    catalog = h.submodule("analysis.presets")
    assert len(operators) == len(catalog.PRESETS) + 1 + len(_core().FAST)
    _clear_trees()
