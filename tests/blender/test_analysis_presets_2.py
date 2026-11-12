"""M5 analysis presets, second batch: visibility, change, volumes, paths and flow.

Each preset is added with the operator (as the menu does), its object inputs
filled from the selection or the 3D cursor, and checked against an analytic
answer: a viewshed behind a wall against a segment-box test, the distance
between a plane and a copy 0.25 m above it, cut and fill of a Gaussian bump
(π m³), a closed cube (8 m³), a least-cost path on a plane (Tobler's flat
speed) and round a ridge too steep to cross, and flow paths on a cone.
"""

from __future__ import annotations

import json
import math

import _harness as h
import bpy
import numpy as np
from test_analysis_presets import _add, _ap, _catalog, _clear_trees, _grid, _layers, _reset, _values

EYE = 1.6  # the default Observer Height
OFFSET = 0.01  # the default Ray Offset of the visibility presets
WALL = ((2.0, -3.0, 0.0), (2.4, 3.0, 1.0))  # min and max corner: lower than the eye
FLAT_SPEED = 6.0 / 3.6 * math.exp(-3.5 * 0.05)  # Tobler on level ground, m/s


def _select(active: bpy.types.Object, *others: bpy.types.Object) -> None:
    bpy.context.view_layer.update()
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj is active or obj in others)
    bpy.context.view_layer.objects.active = active


def _object(name: str, co, edges=(), faces=(), *, source: bool = False) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(map(float, c)) for c in co], list(edges), list(faces))
    obj = bpy.data.objects.new(name, mesh)
    collection = h.project().source_collection if source else bpy.context.scene.collection
    collection.objects.link(obj)
    return obj


def _empty(name: str, location) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _box(lo, hi):
    """Vertices and outward quads of an axis-aligned box."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    co = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    faces = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return co, faces


def _walled_ground(n: int = 40, size: float = 20.0) -> bpy.types.Object:
    """A level n x n grid with the WALL box as a second part of the same mesh, in the source."""
    axis = np.linspace(-size / 2, size / 2, n + 1)
    xs, ys = np.meshgrid(axis, axis)
    co = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)], axis=1).tolist()
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    faces = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).tolist()
    box_co, box_faces = _box(*WALL)
    start = len(co)
    co += box_co
    faces += [[start + k for k in f] for f in box_faces]
    return _object("Ground", co, faces=faces, source=True)


def _blocked(start: np.ndarray, end: np.ndarray, lo, hi) -> np.ndarray:
    """Whether each segment start -> end meets the box [lo, hi] (slab test, binary64)."""
    d = end - start
    with np.errstate(divide="ignore", invalid="ignore"):
        t0 = (np.asarray(lo) - start) / d
        t1 = (np.asarray(hi) - start) / d
    near = np.where(np.isnan(t0), -np.inf, np.minimum(t0, t1))
    far = np.where(np.isnan(t1), np.inf, np.maximum(t0, t1))
    inside = (start >= lo) & (start <= hi)
    near = np.where((d == 0) & inside, -np.inf, near)
    far = np.where((d == 0) & inside, np.inf, far)
    far = np.where((d == 0) & ~inside, -np.inf, far)
    t_in, t_out = near.max(axis=1), far.min(axis=1)
    return (t_in <= t_out) & (t_out >= 0) & (t_in <= 1)


def _shadow(points: np.ndarray, eye) -> tuple[np.ndarray, np.ndarray]:
    """(hidden, unsure): segments to ``eye`` meeting the wall, and those within 1 mm of it."""
    eye = np.broadcast_to(np.asarray(eye, np.float64), points.shape)
    lo, hi = np.asarray(WALL[0]), np.asarray(WALL[1])
    hidden = _blocked(points, eye, lo, hi)
    grown = _blocked(points, eye, lo - 1e-3, hi + 1e-3)
    shrunk = _blocked(points, eye, lo + 1e-3, hi - 1e-3)
    return hidden, grown != shrunk


def _evaluated(obj: bpy.types.Object) -> bpy.types.Mesh:
    return obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data


def _attr(mesh: bpy.types.Mesh, name: str) -> np.ndarray:
    data = mesh.attributes[name].data
    values = np.empty(len(data), np.float64 if mesh.attributes[name].data_type == "FLOAT" else int)
    data.foreach_get("value", values)
    return values


def _co(mesh: bpy.types.Mesh) -> np.ndarray:
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3).astype(np.float64)


def _edges(mesh: bpy.types.Mesh) -> np.ndarray:
    ends = np.empty(len(mesh.edges) * 2, np.int32)
    mesh.edges.foreach_get("vertices", ends)
    return ends.reshape(-1, 2)


def _new_object(preset: str) -> bpy.types.Object:
    obj = bpy.context.view_layer.objects.active
    record = json.loads(obj[_catalog().PROVENANCE_KEY])
    assert record["parameters"]["preset"] == preset
    assert obj.users_collection[0].name == _ap().ANALYSIS_COLLECTION
    return obj


def _warnings(obj: bpy.types.Object) -> list[str]:
    bpy.context.view_layer.update()
    _evaluated(obj)
    return [w.message for m in obj.modifiers for w in m.node_warnings]


# --- visibility ---------------------------------------------------------------------------


def test_viewshed_behind_a_wall() -> None:
    _reset()
    ground = _walled_ground()
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
    _select(ground)
    assert _add("viewshed") == {"FINISHED"}
    # The observer is a new Empty at the 3D cursor, outside the source collection.
    modifier = ground.modifiers[0]
    observer = _ap().get_input(modifier, "Observer")
    assert observer.type == "EMPTY" and observer.users_collection[0].name == "GeoTools Analysis"
    spec = _layers().spec_from_props(_layers().find_layer(bpy.context.scene, "viewshed"))
    assert spec.provenance["parameters"]["inputs"]["Observer"] == observer.name
    assert (spec.domain, spec.units) == ("FACE", "ratio")

    values, where = _values(ground, "viewshed")
    ground_faces = np.abs(where[:, 2]) < 1e-6
    ground_faces &= ~((np.abs(where[:, 0] - 2.2) < 0.21) & (np.abs(where[:, 1]) < 3.01))
    points = where[ground_faces].astype(np.float64) + (0.0, 0.0, OFFSET)
    hidden, unsure = _shadow(points, (0.0, 0.0, EYE))
    assert not unsure.any()  # no face centre lies on a shadow edge
    assert 50 < hidden.sum() < 400, hidden.sum()
    np.testing.assert_array_equal(values[ground_faces] == 0.0, hidden)
    assert set(np.unique(values[ground_faces])) == {0.0, 1.0}
    # The wall: its front and top seen, its back facing away.
    wall = ~ground_faces & (where[:, 2] > -1e-6)
    front = wall & (np.abs(where[:, 0] - 2.0) < 1e-6)
    back = wall & (np.abs(where[:, 0] - 2.4) < 1e-6)
    top = wall & (np.abs(where[:, 2] - 1.0) < 1e-6)
    assert values[front] == 1.0 and values[back] == 0.0 and values[top] == 1.0

    # Max Distance: no data beyond it (horizontally); moving the observer recomputes.
    _ap().set_input(modifier, "Max Distance", 5.0)
    observer.location = (-1.0, 0.0, 0.0)
    values, where = _values(ground, "viewshed")
    beyond = np.hypot(where[:, 0] + 1.0, where[:, 1]) > 5.0
    assert np.isnan(values[beyond]).all() and not np.isnan(values[~beyond]).any()
    _clear_trees()


def test_cumulative_viewshed_counts_observers() -> None:
    _reset()
    ground = _walled_ground()
    where_observers = [(0.0, 0.0, 0.0), (5.0, 1.0, 0.0), (-4.0, -6.0, 0.0)]
    observers = _object("Observers", where_observers)
    _select(ground, observers)
    assert _add("viewshed_cumulative") == {"FINISHED"}
    assert _ap().get_input(ground.modifiers[0], "Observers") == observers
    values, where = _values(ground, "viewshed_cumulative")
    ground_faces = (np.abs(where[:, 2]) < 1e-6) & ~(
        (np.abs(where[:, 0] - 2.2) < 0.21) & (np.abs(where[:, 1]) < 3.01)
    )
    points = where[ground_faces].astype(np.float64) + (0.0, 0.0, OFFSET)
    expected = np.zeros(len(points))
    for x, y, z in where_observers:
        hidden, unsure = _shadow(points, (x, y, z + EYE))
        assert not unsure.any()
        expected += ~hidden
    np.testing.assert_array_equal(values[ground_faces], expected)
    assert set(np.unique(expected)) == {1.0, 2.0, 3.0}

    # Selected with nothing else, the observers are a new one-vertex mesh at the cursor.
    other = _grid("Other", 4, 4.0, lambda x, y: 0.0 * x)
    _select(other)
    assert _add("viewshed_cumulative") == {"FINISHED"}
    made = _ap().get_input(other.modifiers[0], "Observers")
    assert made.type == "MESH" and len(made.data.vertices) == 1
    _clear_trees()


def test_intervisibility_between_points() -> None:
    _reset()
    ground = _walled_ground()
    co = np.array([(0.0, 0.0, 0.0), (5.0, 0.0, 0.0), (0.0, 7.0, 0.0), (6.0, -2.0, 0.0)])
    points = _object("Points", co)
    _select(ground, points)
    assert _add("intervisibility") == {"FINISHED"}
    lines = _new_object("intervisibility")
    _ap().set_input(lines.modifiers[0], "Height", 0.5)  # below the top of the wall
    mesh = _evaluated(lines)
    assert len(mesh.edges) == 6 and len(mesh.polygons) == 0
    ends = _co(mesh)[_edges(mesh)]
    visible, distance = _attr(mesh, "visible"), _attr(mesh, "distance")
    hidden, unsure = _shadow(ends[:, 0] + (0, 0, OFFSET), ends[:, 1])
    assert not unsure.any()
    np.testing.assert_array_equal(visible == 0.0, hidden)
    assert 0 < hidden.sum() < 6
    np.testing.assert_allclose(distance, np.linalg.norm(ends[:, 1] - ends[:, 0], axis=1), 1e-6)
    np.testing.assert_allclose(ends[:, :, 2], 0.5, atol=1e-6)  # eye to eye
    lines.select_set(True)
    message = _ap().totals(bpy.context, lines)
    assert message == [f"Intervisibility: {6 - hidden.sum()} of 6 pairs see each other"]
    _clear_trees()


def test_line_of_sight_finds_the_obstruction() -> None:
    _reset()
    ground = _walled_ground()
    line = _object("Line", [(-1.0, 0.0, 0.0), (3.0, 0.0, 0.0), (8.0, 0.0, 0.0)], [(0, 1), (1, 2)])
    _select(ground, line)
    assert _add("line_of_sight") == {"FINISHED"}
    obj = _new_object("line_of_sight")
    mesh = _evaluated(obj)
    co, ends = _co(mesh), _edges(mesh)
    part, visible = _attr(mesh, "part"), _attr(mesh, "visible")
    # The sight line from (-1, 0, 1.6) to (8, 0, 0) passes the wall's top (z = 1) at x = 2.375.
    (obstruction,) = co[_attr(mesh, "obstruction") > 0]
    np.testing.assert_allclose(obstruction, (2.375, 0.0, 1.0), atol=1e-4)
    sight = ends[part == 1]
    assert len(sight) == 2 and visible[part == 1].tolist() == [1.0, 0.0]
    np.testing.assert_allclose(co[sight[0, 0]], (-1.0, 0.0, EYE), atol=1e-5)
    # The profile: seen in front of the wall and on its top, hidden behind it (to x = 8.0).
    profile = ends[part == 0]
    middle = co[profile].mean(axis=1)
    seen = visible[part == 0]
    front = middle[:, 0] < 1.9
    behind = (middle[:, 0] > 2.6) & (middle[:, 0] < 7.9)
    assert (seen[front] == 1.0).all() and (seen[behind] == 0.0).all()
    assert front.sum() > 4 and behind.sum() > 8
    np.testing.assert_allclose(co[profile][..., 1], 0.0, atol=1e-6)
    (note,) = _ap().totals(bpy.context, obj)
    assert "blocked at (2.375, 0.000, 1.000)" in note
    _clear_trees()


# --- change and distance ----------------------------------------------------------------------


def test_distance_between_two_surveys() -> None:
    _reset()
    later = _grid("Later", 20, 10.0, lambda x, y: 0.0 * x)
    earlier = _grid("Earlier", 20, 10.0, lambda x, y: 0.0 * x, select=False)
    earlier.location.z = -0.25  # the later survey is 0.25 m above
    _select(later, earlier)
    assert _add("surface_distance") == {"FINISHED"}
    assert len(earlier.modifiers) == 0  # only the active mesh is measured
    values, _ = _values(later, "surface_distance")
    np.testing.assert_allclose(values, 0.25, atol=1e-6)
    earlier.location.z = 0.25  # now below it
    np.testing.assert_allclose(_values(later, "surface_distance")[0], -0.25, atol=1e-6)
    earlier.location.z = 2.0  # beyond Max Distance (1 m): no data
    assert np.isnan(_values(later, "surface_distance")[0]).all()
    spec = _layers().spec_from_props(_layers().find_layer(bpy.context.scene, "surface_distance"))
    assert spec.provenance["parameters"]["inputs"]["Reference"] == "Earlier"
    assert spec.units == "m" and spec.domain == "CORNER"

    # Without a second mesh selected nothing is added.
    _select(later)
    result, error = h.call_op(bpy.ops.geotools.analysis_add, preset="surface_distance", name="d2")
    assert error is not None and "second surface" in error, (result, error)
    assert len(later.modifiers) == 1
    _clear_trees()


def test_distance_to_a_feature() -> None:
    _reset()
    site = _grid("Site", 20, 10.0, lambda x, y: 3.0 + 0.0 * x)
    line = _object("Feature", [(0.0, -10.0, 0.0), (0.0, 10.0, 0.0)], [(0, 1)])
    _select(site, line)
    assert _add("feature_distance") == {"FINISHED"}
    values, where = _values(site, "feature_distance")
    np.testing.assert_allclose(values, np.abs(where[:, 0]), atol=1e-6)  # in plan
    _ap().set_input(site.modifiers[0], "Horizontal", False)
    values, where = _values(site, "feature_distance")
    np.testing.assert_allclose(values, np.hypot(where[:, 0], 3.0), atol=1e-5)  # in 3D
    # Points and polygons too: inside a polygon the distance is 0.
    line.data = bpy.data.meshes.new("Square")
    line.data.from_pydata([(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [], [(0, 1, 2, 3)])
    _ap().set_input(site.modifiers[0], "Horizontal", True)
    values, where = _values(site, "feature_distance")
    outside = np.hypot(
        np.maximum(np.abs(where[:, 0]) - 1, 0), np.maximum(np.abs(where[:, 1]) - 1, 0)
    )
    np.testing.assert_allclose(values, outside, atol=1e-6)
    _clear_trees()


# --- volumes --------------------------------------------------------------------------------------


def test_cut_and_fill_of_a_bump_and_a_pit() -> None:
    _reset()
    # A Gaussian bump of height 1 and a pit of depth 0.5, each exp(-r²): volumes π and π/2.
    site = _grid(
        "Site",
        320,
        16.0,
        lambda x, y: np.exp(-((x + 4) ** 2 + y**2)) - 0.5 * np.exp(-((x - 4) ** 2 + y**2)),
    )
    reference = _grid("Ground", 4, 12.0, lambda x, y: 0.0 * x, select=False)
    _select(site, reference)
    assert _add("cut_fill") == {"FINISHED"}
    values, where = _values(site, "cut_fill")
    outside = (np.abs(where[:, 0]) > 6) | (np.abs(where[:, 1]) > 6)
    assert np.isnan(values[outside]).all() and not np.isnan(values[~outside]).any()
    (line,) = _ap().totals(bpy.context, site)
    fill, cut = (float(s.split()[1].replace(",", "")) for s in line.split(": ")[1].split(", ")[:2])
    # Each Gaussian's tail beyond the reference's edge, 2 m from its centre, is left out.
    inside = 1.0 - math.erfc(2.0) / 2.0
    assert abs(fill - math.pi * inside) < 1e-3 * math.pi, fill
    assert abs(cut - math.pi / 2 * inside) < 1e-3 * math.pi, cut
    assert abs(fill - math.pi) < 0.01 * math.pi  # and within 1% of the whole bump
    assert "over 144.00 m² (112.00 m² outside the reference)" in line, line
    # The modifier shows its own (float32) totals.
    (shown,) = _warnings(site)
    words = shown.split()
    assert words[0] == "Fill" and words[3] == "cut", shown
    assert abs(float(words[1]) - fill) < 2e-3 and abs(float(words[4]) - cut) < 2e-3, shown
    _clear_trees()


def test_volume_of_closed_parts() -> None:
    _reset()
    # A cube of side 2 far from the origin, a triangulated cube of side 1, and an open box.
    cube, faces = _box((1000.0, 2000.0, 50.0), (1002.0, 2002.0, 52.0))
    small, small_faces = _box((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    tris = [t for q in small_faces for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))]
    box, box_faces = _box((5.0, 0.0, 0.0), (6.0, 1.0, 1.0))
    co = cube + small + box
    faces = (
        faces
        + [tuple(i + 8 for i in t) for t in tris]
        + [tuple(i + 16 for i in f) for f in box_faces[1:]]  # no bottom: open
    )
    obj = _object("Contexts", co, faces=faces, source=True)
    _select(obj)
    assert _add("closed_volume") == {"FINISHED"}
    values, _ = _values(obj, "closed_volume")
    np.testing.assert_array_equal(values[:6], 8.0)  # exact
    np.testing.assert_allclose(values[6:18], 1.0, rtol=1e-6)
    assert np.isnan(values[18:]).all()
    (line,) = _ap().totals(bpy.context, obj)
    assert line == "Closed Volume: volume 9.000 m³ (5 faces on open parts)", line
    (shown,) = _warnings(obj)
    assert shown == "Volume 9.000 m³ in 2 closed parts (1 open parts have no volume)", shown
    spec = _layers().spec_from_props(_layers().find_layer(bpy.context.scene, "closed_volume"))
    assert (spec.units, spec.aggregation) == ("m3", "NONE")
    _clear_trees()


# --- least-cost paths and flow -------------------------------------------------------------------


def _tobler(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    run = np.hypot(*(b - a)[:, :2].T)
    speed = 6.0 / 3.6 * np.exp(-3.5 * np.abs((b - a)[:, 2] / run + 0.05))
    return run / speed


def _path(obj: bpy.types.Object) -> tuple[np.ndarray, np.ndarray]:
    """The path's vertices in order from the start, and their elapsed times."""
    mesh = _evaluated(obj)
    elapsed = _attr(mesh, "elapsed")
    order = np.argsort(elapsed)
    return _co(mesh)[order], elapsed[order]


def test_least_cost_path_on_a_plane() -> None:
    _reset()
    ground = _grid("Plain", 40, 20.0, lambda x, y: 0.0 * x)
    start, end = _empty("A Start", (-8.0, 2.0, 0.3)), _empty("B End", (7.0, 2.0, -0.2))
    _select(ground, start, end)
    assert _add("least_cost_path") == {"FINISHED"}
    obj = _new_object("least_cost_path")
    modifier = obj.modifiers[0]
    assert _ap().get_input(modifier, "Start") == start and _ap().get_input(modifier, "End") == end
    co, elapsed = _path(obj)
    np.testing.assert_allclose(co[[0, -1]], [(-8.0, 2.0, 0.0), (7.0, 2.0, 0.0)], atol=1e-6)
    np.testing.assert_allclose(co[:, 1], 2.0, atol=1e-6)  # straight along the row
    assert abs(elapsed[-1] - 15.0 / FLAT_SPEED) < 1e-4 * elapsed[-1], elapsed[-1]
    time = _attr(_evaluated(obj), "time")
    assert abs(time.sum() - elapsed[-1]) < 1e-3
    (line,) = _ap().totals(bpy.context, obj)
    assert line == f"Least-Cost Path: walking time {15.0 / FLAT_SPEED / 60:.1f} min over 15.0 m"
    (shown,) = _warnings(obj)
    assert shown.startswith("Walking time 0.2 min over 15.0 m"), shown
    _clear_trees()


def test_least_cost_path_goes_round_a_steep_ridge() -> None:
    _reset()
    # A ridge along y = 0 from the west edge to x = 4, its sides 35° steep.
    rise = 0.5 * math.tan(math.radians(35.0))
    ground = _grid(
        "Ridge",
        40,
        20.0,
        lambda x, y: np.where(x < 3.9, rise * np.clip(1 - np.abs(y) / 0.5, 0, 1), 0.0),
    )
    start = _empty("A Start", (-6.0, -6.0, 0.0))
    end = _empty("B End", (-6.0, 6.0, 0.0))
    _select(ground, start, end)
    assert _add("least_cost_path") == {"FINISHED"}
    obj = _new_object("least_cost_path")
    co, elapsed = _path(obj)
    assert co[:, 0].max() >= 4.0  # round the east end of the ridge
    steps = np.diff(co, axis=0)
    slope = np.degrees(np.arctan2(np.abs(steps[:, 2]), np.hypot(steps[:, 0], steps[:, 1])))
    assert slope.max() <= 30.0
    # The time is Tobler's, walked from the start, along the path found.
    np.testing.assert_allclose(elapsed[1:], np.cumsum(_tobler(co[:-1], co[1:])), rtol=1e-5)
    # With Max Slope 40 the ridge can be crossed, and the path is quicker for it.
    _ap().set_input(obj.modifiers[0], "Max Slope", 40.0)
    over, elapsed_over = _path(obj)
    assert over[:, 0].max() < 4.0 and elapsed_over[-1] < elapsed[-1]
    # A cost layer of 0 (impassable) on the east: no path at all.
    _ap().set_input(obj.modifiers[0], "Max Slope", 30.0)
    cost = ground.data.attributes.new("gt_cost", "FLOAT", "FACE")
    centres = np.empty(len(ground.data.polygons) * 3, np.float32)
    ground.data.polygons.foreach_get("center", centres)
    cost.data.foreach_set("value", np.where(centres[0::3] > 2.0, 0.0, 1.0).astype(np.float32))
    _ap().set_input(obj.modifiers[0], "Cost Attribute", "gt_cost")
    ground.data.update()
    assert len(_evaluated(obj).edges) == 0
    shown = _warnings(obj)
    assert shown == ["No walkable path from Start to End (Max Slope, or impassable faces)"], shown
    assert _ap().totals(bpy.context, obj) == ["Least-Cost Path: no walkable path from Start to End"]
    _clear_trees()


def test_flow_paths_run_down_a_cone() -> None:
    _reset()
    cone = _grid("Cone", 100, 20.0, lambda x, y: 5.0 - np.hypot(x, y))
    angles = np.arange(8) * math.pi / 4 + 0.1
    seeds = _object("Seeds", [(math.cos(a), math.sin(a), 0.0) for a in angles])
    _select(cone, seeds)
    assert _add("flow_path") == {"FINISHED"}
    obj = _new_object("flow_path")
    mesh = _evaluated(obj)
    co, step = _co(mesh), _attr(mesh, "step")
    seed = np.zeros(len(co), int)
    seed[_edges(mesh)[:, 0]] = _attr(mesh, "seed")
    seed[_edges(mesh)[:, 1]] = _attr(mesh, "seed")
    assert set(seed) == set(range(8))
    for index, angle in enumerate(angles):
        mine = seed == index
        order = np.argsort(step[mine])
        path = co[mine][order]
        radius = np.hypot(path[:, 0], path[:, 1])
        assert (np.diff(radius) > 0).all() and (np.diff(path[:, 2]) < 0).all()
        assert radius[-1] > 9.5  # to the edge of the mesh
        drift = np.angle(np.exp(1j * (np.arctan2(path[:, 1], path[:, 0]) - angle)))
        assert np.degrees(np.abs(drift)).max() < 3.0, (index, np.degrees(drift).max())
    _clear_trees()


# --- the operator ---------------------------------------------------------------------------------


def test_object_inputs_and_readding() -> None:
    _reset()
    ground = _grid("Ground", 10, 10.0, lambda x, y: 0.0 * x)
    observer = _empty("Tower", (1.0, 2.0, 3.0))
    _select(ground, observer)
    assert _add("viewshed") == {"FINISHED"}
    assert _ap().get_input(ground.modifiers[0], "Observer") == observer
    empties = len([o for o in bpy.data.objects if o.type == "EMPTY"])
    # Adding again keeps the modifier and makes nothing new.
    _select(ground)
    assert _add("viewshed") == {"FINISHED"}
    assert len(ground.modifiers) == 1
    assert len([o for o in bpy.data.objects if o.type == "EMPTY"]) == empties
    # A line preset needs a line: nothing is made without one.
    objects = len(bpy.data.objects)
    result, error = h.call_op(bpy.ops.geotools.analysis_add, preset="line_of_sight")
    assert error is not None and "a line" in error, (result, error)
    assert len(bpy.data.objects) == objects
    # The panel lists an object preset's lines and offers totals.
    path = _empty("End", (3.0, 3.0, 0.0))
    _select(ground, path)
    assert _add("least_cost_path", name="Walk") == {"FINISHED"}
    walk = bpy.context.view_layer.objects.active
    assert walk.name == "Walk"
    text = h.label_text(h.draw(bpy.types.GEOTOOLS_PT_analysis_presets))
    assert "Least-Cost Path: lines (elapsed, time)" in text
    assert bpy.ops.geotools.analysis_totals() == {"FINISHED"}
    calls = h.draw(bpy.types.GEOTOOLS_MT_analysis_presets)
    assert "Paths and Flow" in h.label_text(calls) and "Visibility" in h.label_text(calls)
    _clear_trees()
