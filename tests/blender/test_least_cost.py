"""Least-cost routes in Blender (``blender.least_cost``): the operator, its outputs and errors.

Synthetic scenes only: a plane rising 10 % to the east (a quad mesh, so faces
are two triangles each), and a second object to refuse. The maths is checked
in ``tests/core/test_validation_least_cost.py``; here the operator reads the
surface, Start, End and the cost layer as a user sets them and writes the
route, the travel-time layer and the isochrones.
"""

from __future__ import annotations

import json
import math

import _harness as h
import bpy
import numpy as np

GRADE = 0.1


def _lc():
    return h.submodule("analysis.least_cost")


def _reset() -> None:
    h.reset_scene()
    props = bpy.context.scene.geotools_least_cost
    for prop in props.bl_rna.properties:
        if prop.identifier not in {"rna_type", "name"}:
            props.property_unset(prop.identifier)
    props.surface = "SOURCE"  # the whole site; Selected Objects (the default) is test_selection's


def _plane(name: str = "ramp", offset=(0.0, 0.0, 0.0), n: int = 40, size: float = 20.0):
    """A quad grid ``size`` m across at its centre, z = GRADE x, in a source collection."""
    xs = np.linspace(-size / 2, size / 2, n + 1)
    gx, gy = np.meshgrid(xs, xs)
    verts = np.stack([gx.ravel(), gy.ravel(), GRADE * gx.ravel()], axis=1) + offset
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1)
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts.tolist(), [], quads.tolist())
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection = bpy.data.collections.get("Source")
    if collection is None:
        collection = bpy.data.collections.new("Source")
        bpy.context.scene.collection.children.link(collection)
        h.project().source_collection = collection
    collection.objects.link(obj)
    return obj


def _empty(name: str, location) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _attr(obj, name: str, size: int, dtype=np.float32) -> np.ndarray:
    values = np.empty(size, dtype)
    obj.data.attributes[name].data.foreach_get("value", values)
    return values


def _ends(props, x0: float = -8.0, x1: float = 8.0) -> None:
    props.start = _empty("A", (x0, 0.0, GRADE * x0))
    props.end = _empty("B", (x1, 0.0, GRADE * x1))


def _run() -> dict:
    result, error = h.call_op(bpy.ops.geotools.least_cost)
    assert error is None and result == {"FINISHED"}, error
    return json.loads(bpy.context.scene.geotools_least_cost.result)


def test_least_cost_registered() -> None:
    h.ensure_enabled()
    assert h.operator_available("geotools.least_cost")
    props = bpy.context.scene.geotools_least_cost
    assert (props.model, props.steiner, props.time_layer) == ("TOBLER", 3, "travel_time")
    assert props.max_gradient == np.float32(100.0 / 12.0)


def test_route_and_travel_time_up_and_down() -> None:
    _reset()
    ramp = _plane()
    props = bpy.context.scene.geotools_least_cost
    _ends(props)
    out = _run()
    lc = _lc()
    up = float(lc.segment_time("TOBLER", 16.0, 16.0 * GRADE))
    assert out["faces"] == 1600 and out["triangles"] == 3200
    assert math.isclose(out["route"]["time"], up, rel_tol=1e-6)
    assert math.isclose(out["route"]["length"], 16.0 * math.hypot(1, GRADE), rel_tol=1e-6)

    route = bpy.data.objects["Least-Cost Route"]
    assert route.name in bpy.data.collections["GeoTools Analysis"].objects
    n = len(route.data.vertices)
    elapsed = _attr(route, "elapsed", n)
    edge_time = _attr(route, "time", n - 1)
    assert elapsed[0] == 0 and math.isclose(elapsed[-1], up, rel_tol=1e-6)
    assert (np.diff(elapsed) >= 0).all()
    assert math.isclose(float(edge_time.sum()), up, rel_tol=1e-5)
    assert json.loads(route["geotools_provenance"])["tool"] == "least_cost"

    item = h.project().layers["travel_time"]
    assert (item.domain, item.data_type, item.units, item.origin) == (
        "FACE",
        "FLOAT",
        "s",
        "ANALYSIS",
    )
    times = _attr(ramp, "gt_travel_time", 1600)
    assert np.isfinite(times).all() and out["reached"] == 1600
    # Start is on a vertex of four faces: the downhill ones are reached first.
    centres = np.empty(4800)
    ramp.data.polygons.foreach_get("center", centres)
    centres = centres.reshape(-1, 3)
    first = centres[times.argmin()]
    assert np.abs(first[:2] - (-8.0, 0.0)).max() < 0.5 and first[0] < -8.0

    # Back down is quicker: the route is directed.
    props.start, props.end = props.end, props.start
    down = float(lc.segment_time("TOBLER", 16.0, -16.0 * GRADE))
    back = _run()
    assert math.isclose(back["route"]["time"], down, rel_tol=1e-6) and down < 0.8 * up
    assert "Least-Cost Route.001" not in bpy.data.objects


def test_models_isochrones_and_a_cost_layer() -> None:
    _reset()
    ramp = _plane()
    props = bpy.context.scene.geotools_least_cost
    _ends(props)
    lc = _lc()
    for model in ("LANGMUIR", "NAISMITH"):
        props.model = model
        out = _run()
        expected = float(lc.segment_time(model, 16.0, 16.0 * GRADE))
        assert math.isclose(out["route"]["time"], expected, rel_tol=1e-6), model
    # The wheelchair: 10 % is over 1:12, and with the limit raised a traverse up the slope
    # beats going straight up (its speed falls fast with gradient, above about 8.9 %).
    props.model = "WHEELCHAIR"
    route = bpy.data.objects["Least-Cost Route"]
    assert _run()["route"]["length"] > 16.0 * 1.15  # a traverse, every step within 1:12
    co = np.empty(3 * len(route.data.vertices), np.float32)
    route.data.vertices.foreach_get("co", co)
    step = np.diff(co.reshape(-1, 3).astype(np.float64), axis=0)
    assert (np.abs(step[:, 2]) <= np.hypot(step[:, 0], step[:, 1]) / 12.0 + 1e-5).all()
    props.max_gradient = 12.0
    straight = float(lc.segment_time("WHEELCHAIR", 16.0, 1.6, max_gradient=0.12))
    assert _run()["route"]["time"] < straight
    props.model = "TOBLER"
    once = _run()["route"]["time"]

    # A cost layer of 2 everywhere doubles every time.
    core = h.submodule("core.layers")
    layers = h.submodule("blender.layers")
    spec = core.LayerSpec("scrub", core.Domain.FACE, core.DataType.FLOAT)
    layers.ensure_layer(bpy.context.scene, spec)
    layers.write_layer(ramp.data, spec, np.full(1600, 2.0, np.float32))
    props.cost_layer = "scrub"
    assert math.isclose(_run()["route"]["time"], 2 * once, rel_tol=1e-6)
    # No data across the middle, with a gap: the route goes round through the gap.
    values = np.full(1600, 1.0, np.float32)
    centres = np.empty(4800)
    ramp.data.polygons.foreach_get("center", centres)
    centres = centres.reshape(-1, 3)
    values[(np.abs(centres[:, 0]) < 0.5) & (centres[:, 1] < 5.0)] = np.nan
    layers.write_layer(ramp.data, spec, values)
    out = _run()
    route = bpy.data.objects["Least-Cost Route"]
    co = np.empty(3 * len(route.data.vertices), np.float32)
    route.data.vertices.foreach_get("co", co)
    # Round the wall's end at y = 5: sqrt(8² + 5²) x 2 = 18.9 m, not 16.
    assert co.reshape(-1, 3)[:, 1].max() >= 5.0 - 1e-5 and out["route"]["time"] > 1.1 * once
    props.cost_layer = ""

    # Isochrones in 0.1 min bands.
    props.band_minutes = 0.1
    _run()
    item = h.project().layers["isochrones"]
    assert (item.domain, item.data_type) == ("FACE", "CATEGORY")
    labels = item.categories.splitlines()
    assert labels[:3] == ["Unreached", "0–0.1 min", "0.1–0.2 min"]
    bands = _attr(ramp, "gt_isochrones", 1600, np.int32)
    times = _attr(ramp, "gt_travel_time", 1600)
    expected = np.floor(times / 6.0).astype(np.int32) + 1
    on_edge = np.abs(times / 6.0 - np.round(times / 6.0)) < 1e-5  # float32 storage
    assert (bands == expected)[~on_edge].all() and bands.max() >= 3

    # Route only: no layer written, and a route-only search may stop early.
    props.make_layer = False
    props.time_layer = "not_written"
    out = _run()
    assert out["layers"] == [] and "not_written" not in h.project().layers


def test_start_and_end_from_the_cursor() -> None:
    _reset()
    _plane()
    props = bpy.context.scene.geotools_least_cost
    bpy.context.scene.cursor.location = (-5.0, 2.0, -0.5)
    _result, error = h.call_op(bpy.ops.geotools.least_cost)
    assert error is not None and "same place" in error
    start, end = props.start, props.end
    assert start.name == "Route Start" and end.name == "Route End"
    assert start.name in bpy.data.collections["GeoTools Analysis"].objects
    end.location = (5.0, -2.0, 0.5)
    out = _run()
    assert out["route"]["time"] > 0 and out["start_snap"] < 1e-5
    # Travel time alone needs no End.
    _reset()
    _plane()
    props.make_route = False
    out = _run()
    assert props.end is None and out["route"] is None and out["reached"] == 1600


def test_least_cost_errors() -> None:
    _reset()
    _plane()
    props = bpy.context.scene.geotools_least_cost
    _ends(props)
    props.make_route = props.make_layer = False
    assert "Choose an output" in h.call_op(bpy.ops.geotools.least_cost)[1]
    props.make_route = props.make_layer = True
    props.cost_layer = "missing"
    assert "no layer missing" in h.call_op(bpy.ops.geotools.least_cost)[1]
    core = h.submodule("core.layers")
    h.submodule("blender.layers").ensure_layer(
        bpy.context.scene, core.LayerSpec("corner", core.Domain.CORNER, core.DataType.FLOAT)
    )
    props.cost_layer = "corner"
    assert "face layer of numbers" in h.call_op(bpy.ops.geotools.least_cost)[1]
    props.cost_layer = ""
    props.time_layer = "corner"
    assert "choose another name" in h.call_op(bpy.ops.geotools.least_cost)[1]
    props.time_layer = "travel_time"
    # Two objects: one connected surface only, for now.
    _plane("upper", offset=(0.0, 0.0, 3.0))
    error = h.call_op(bpy.ops.geotools.least_cost)[1]
    assert "one connected surface" in error and "#109" in error
    assert props.error and not props.result
    # Unreachable End (route only): a clear error, nothing made.
    _reset()
    _plane()
    _ends(props)
    props.max_slope = 5.0  # the 5.7° ramp cannot be walked at all
    error = h.call_op(bpy.ops.geotools.least_cost)[1]
    assert "No face can be crossed" in error


def test_least_cost_panel_draws() -> None:
    _reset()
    ui = h.submodule("blender.surface_ui")
    calls = h.draw(ui.GEOTOOLS_PT_analysis_least_cost)
    assert "3D cursor" in h.label_text(calls)
    _plane()
    _ends(bpy.context.scene.geotools_least_cost)
    _run()
    text = h.label_text(h.draw(ui.GEOTOOLS_PT_analysis_least_cost))
    assert "Route" in text and "Furthest" in text
