"""Sun hours, insolation and shadow studies in Blender (``blender.sun_hours``).

The geometry is chosen so the answer is known in closed form. At the equator
on an equinox, with refraction off, the sun rises due east, passes the zenith
and sets due west, its elevation rising 15 degrees an hour. A north-south wall
of height H then shades a point at distance d on its west side every morning
until the sun clears atan(H / d), so the point sees the sun for

    6 h (afternoon) + (90 - atan(H / d)) / 15 h (morning),

and the same on the east side with morning and afternoon swapped.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path

import _harness as h
import bpy
import numpy as np

WALL_HEIGHT = 5.0
HALF = 10  # the plane spans -HALF..HALF in x and y, one metre quads


def _blender():
    return h.submodule("blender.sun_hours")


def _core():
    return h.submodule("analysis.sun_hours")


def _reset() -> None:
    h.reset_scene()
    analysis = bpy.context.scene.geotools_analysis
    for group in (analysis.sun, analysis.sun_hours, analysis.shadow_study):
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"}:
                group.property_unset(prop.identifier)
    for marker in list(bpy.context.scene.timeline_markers):
        bpy.context.scene.timeline_markers.remove(marker)
    bpy.context.scene.frame_set(1)


def _equator_equinox(day: int = 20) -> None:
    """A LOCAL project at 0 N 0 E on the March 2026 equinox, UTC, no refraction."""
    props = h.project()
    props.placement_mode = "LOCAL"
    props.anchor_lon, props.anchor_lat = "0.0", "0.0"
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.year, sun_props.month, sun_props.day = 2026, 3, day
    sun_props.tz_mode = "ZONE"
    sun_props.tz_name = "UTC"
    sun_props.refraction = False


def _plane(name: str = "Ground", size: int = HALF) -> bpy.types.Object:
    """A ground plane of one-metre quads in a new source collection, with a UV map."""
    collection = bpy.data.collections.get("Source") or bpy.data.collections.new("Source")
    if collection.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    n = 2 * size
    xs = np.arange(-size, size + 1, dtype=np.float64)
    gx, gy = np.meshgrid(xs, xs, indexing="xy")
    verts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1)
    faces = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            faces.append((a, a + 1, a + n + 2, a + n + 1))
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts.tolist(), [], faces)
    uv = mesh.uv_layers.new(name="UVMap")
    co = verts[np.array(faces).ravel(), :2]
    uv.data.foreach_set("uv", ((co + size) / n).astype(np.float32).ravel())
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _wall() -> bpy.types.Object:
    """A north-south wall along x = 0, WALL_HEIGHT tall, 0.1 thick, 2 km long; not a source."""
    mesh = bpy.data.meshes.new("Wall")
    x0, x1, y0, y1 = -0.05, 0.05, -1000.0, 1000.0
    verts = [(x, y, z) for z in (0.0, WALL_HEIGHT) for y in (y0, y1) for x in (x0, x1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new("Wall", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _expected_hours(x: np.ndarray) -> np.ndarray:
    distance = np.abs(x) - 0.05
    return 6.0 + (90.0 - np.degrees(np.arctan2(WALL_HEIGHT, distance))) / 15.0


def _face_centres(obj: bpy.types.Object) -> np.ndarray:
    mesh = obj.data
    centres = np.empty(len(mesh.polygons) * 3, np.float32)
    mesh.polygons.foreach_get("center", centres)
    return centres.reshape(-1, 3)


def _values(obj: bpy.types.Object, name: str) -> np.ndarray:
    attr = obj.data.attributes[f"gt_{name}"]
    values = np.empty(len(attr.data), np.float32)
    attr.data.foreach_get("value", values)
    return values


def _run(**settings) -> dict:
    props = bpy.context.scene.geotools_analysis.sun_hours
    for key, value in settings.items():
        setattr(props, key, value)
    result, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert result == {"FINISHED"}, error or props.error
    return json.loads(props.result)


@contextlib.contextmanager
def _saved_blend() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="geotools-sun-") as tmp:
        path = Path(tmp) / "sun.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path)) == {"FINISHED"}
        yield path
        bpy.ops.wm.read_homefile(use_empty=True)


# --- tests -----------------------------------------------------------------------------------


def test_sun_hours_registered() -> None:
    assert h.operator_available("geotools.sun_hours")
    assert h.operator_available("geotools.sun_shadow_study")
    analysis = bpy.context.scene.geotools_analysis
    assert analysis.sun_hours.hours_layer == "sun_hours"
    assert analysis.shadow_study.june


def test_wall_shades_half_the_plane() -> None:
    _reset()
    _equator_equinox()
    plane = _plane()
    _wall()
    result = _run(step_minutes=5, tolerance=0.0)
    hours = _values(plane, "sun_hours")
    x = _face_centres(plane)[:, 0]
    expected = _expected_hours(x)
    error = np.abs(hours - expected)
    # The midpoint rule at 5-minute steps puts each sunrise, sunset and shadow
    # edge within half a step; the equinox sun drifts 0.3 degrees off the
    # east-west plane over the day.
    assert error.max() < 0.1, (error.max(), x[np.argmax(error)])
    # Symmetry: the west half gets what the east half gets.
    west = hours[x < 0].reshape(2 * HALF, HALF)
    east = hours[x > 0].reshape(2 * HALF, HALF)[:, ::-1]
    assert np.abs(west - east).max() < 0.1
    assert abs(result["stats"]["sun_hours_possible"] - 12.0) < 0.1
    assert result["sun_rays"] == result["stats"]["steps_sun_up"]  # nothing merged
    # The wall itself is not a source, so it got no attribute.
    assert "gt_sun_hours" not in bpy.data.objects["Wall"].data.attributes


def test_layer_metadata_and_variant() -> None:
    _reset()
    _equator_equinox()
    plane = _plane(size=4)
    _wall()
    result = _run(step_minutes=30)
    item = h.project().layers.get("sun_hours")
    assert item is not None
    assert (item.domain, item.data_type, item.units) == ("FACE", "FLOAT", "h")
    assert item.origin == "ANALYSIS" and item.aggregation == "AREA_MEAN"
    values = _values(plane, "sun_hours")
    assert float(item.value_min) == float(values.min())
    assert float(item.value_max) == float(values.max())
    provenance = json.loads(item.provenance)
    assert provenance["tool"] == "sun_hours"
    parameters = provenance["parameters"]
    for key in (
        "first_day",
        "last_day",
        "time_zone",
        "step_minutes",
        "site",
        "algorithm",
        "convergence_deg",
        "sun_rays",
        "ray_offset",
        "max_group_deviation_deg",
    ):
        assert key in parameters, key
    assert parameters["first_day"] == parameters["last_day"] == "2026-03-20"
    assert parameters["site"]["latitude"] == 0.0 and parameters["step_minutes"] == 30
    assert result["layers"] == ["sun_hours"]
    issues = h.submodule("blender.layers").cached_status(bpy.context.scene, item)
    errors = [i for i in issues if i.severity.value == "error"]
    assert not errors, errors

    # It presents and publishes like any FACE layer.
    present = h.submodule("blender.presentation")
    codes = [i.code for i in present.variant_issues(bpy.context.scene, "sun_hours")]
    assert not codes, codes
    presentation, record = present.build_presentation(bpy.context.scene, "sun_hours")
    assert record["layer"] == "sun_hours"
    assert presentation.colors[plane.name].shape == (len(plane.data.polygons), 4)

    # A second run updates the same layer in place.
    _run(step_minutes=15)
    assert len([i for i in h.project().layers if i.name == "sun_hours"]) == 1
    assert (
        json.loads(h.project().layers["sun_hours"].provenance)["parameters"]["step_minutes"] == 15
    )


def test_insolation_on_open_ground_is_global_irradiation() -> None:
    # Unobstructed and horizontal: the beam term plus the whole sky's diffuse
    # (the sky rays' cosines sum to exactly 1) is the clear-sky GHI.
    _reset()
    _equator_equinox(day=21)
    plane = _plane(size=1)
    result = _run(step_minutes=10, insolation=True, diffuse=True, diffuse_rays=64, tolerance=0.0)
    item = h.project().layers.get("insolation")
    assert item is not None and item.units == "kWh/m²"
    energy = _values(plane, "insolation")
    hours = _values(plane, "sun_hours")

    core = _core()
    clearsky = h.submodule("analysis.clearsky")
    steps = core.period_steps(
        dt.date(2026, 3, 21),
        dt.date(2026, 3, 21),
        h.submodule("analysis.sun").zone("UTC"),
        10,
    )
    sun_steps = core.sun_steps(steps, 0.0, 0.0, 0.0, 0.0, refraction=False)
    ghi, _dni, _dhi = clearsky.ineichen(
        90.0 - sun_steps.elevation, distance_au=sun_steps.distance_au
    )
    expected = float(np.sum(ghi * sun_steps.hours)) / 1000.0
    assert np.allclose(energy, expected, rtol=2e-3), (energy, expected)
    assert np.allclose(hours, result["stats"]["sun_hours_possible"], atol=1e-3)
    assert 5.0 < expected < 9.0  # an equatorial clear-sky day
    assert result["stats"]["sky_rays"] == 64


def test_merged_directions_stay_close() -> None:
    _reset()
    _equator_equinox()
    plane = _plane(size=6)
    _wall()
    _run(step_minutes=2, tolerance=0.0)
    exact = _values(plane, "sun_hours").copy()
    result = _run(step_minutes=2, tolerance=2.0)
    merged = _values(plane, "sun_hours")
    assert result["sun_rays"] < result["stats"]["steps_sun_up"] / 2
    # Each merged ray is within 2 degrees of its steps; a shadow edge moves by
    # at most a few minutes of sun.
    assert np.abs(merged - exact).max() < 0.25
    assert abs(float(merged.mean() - exact.mean())) < 0.05


def test_batches_add_up() -> None:
    # Many bakes of a few rays each give what one bake of them all gives: the
    # batches, the blocks of the probe and a short last batch.
    _reset()
    _equator_equinox()
    plane = _plane(size=3)
    _wall()
    module = _blender()
    engine = bpy.context.scene.render.engine
    _run(step_minutes=5, tolerance=0.0)
    single = _values(plane, "sun_hours").copy()
    saved = module.RAYS_PER_BAKE
    module.RAYS_PER_BAKE = 40  # 143 rays: four bakes, the last of 23
    try:
        result = _run(step_minutes=5, tolerance=0.0)
    finally:
        module.RAYS_PER_BAKE = saved
    assert result["bakes"] == 4 and result["rays"] == 143, result
    assert np.allclose(_values(plane, "sun_hours"), single, atol=1e-4)
    # No temporary data is left behind.
    assert not [g for g in bpy.data.node_groups if g.name.startswith("GeoTools Sun Hours")]
    assert not [m for m in bpy.data.materials if m.name.startswith("GeoTools Sun Hours")]
    assert not [o for o in bpy.data.objects if o.name.startswith("GeoTools Sun Hours")]
    assert not [i for i in bpy.data.images if i.name.startswith("GeoTools Sun Hours")]
    assert bpy.context.scene.render.engine == engine


def test_corner_and_texture_values() -> None:
    _reset()
    _equator_equinox()
    plane = _plane(size=4)
    _wall()
    _run(step_minutes=5, domain="CORNER", hours_layer="corner_hours", tolerance=0.0)
    mesh = plane.data
    corner_vertex = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", corner_vertex)
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    corners = co.reshape(-1, 3)[corner_vertex]
    sizes = np.full(len(mesh.polygons), 4)
    centres = np.repeat(_face_centres(plane), sizes, axis=0)
    inset = _blender().CORNER_INSET
    sample_x = (corners + inset * (centres - corners))[:, 0]
    values = _values(plane, "corner_hours")
    assert len(values) == len(mesh.loops)
    # Corners of the faces along x = 0 sit on the wall's own faces; leave them out.
    keep = np.abs(sample_x) > 0.2
    error = np.abs(values - _expected_hours(sample_x))[keep]
    assert error.max() < 0.1, error.max()

    with _saved_blend():
        _run(
            step_minutes=10,
            domain="TEXTURE",
            hours_layer="texture_hours",
            resolution=64,
            tolerance=0.0,
        )
        item = h.project().layers["texture_hours"]
        assert item.domain == "TEXTURE" and item.resolution == 64
        image = h.submodule("blender.layers").layer_image(item, plane)
        assert image is not None and tuple(image.size) == (64, 64)
        assert Path(bpy.path.abspath(image.filepath)).is_file()
        pixels = np.empty(64 * 64 * 4, np.float32)
        image.pixels.foreach_get(pixels)
        texel = pixels[0::4].reshape(64, 64)
        # Texel (i, j) sits at x = (i + 0.5) / 64 * 8 - 4.
        x = (np.arange(64) + 0.5) / 64 * 8 - 4
        expected = _expected_hours(x)
        row = texel[32]
        keep = np.abs(x) > 0.3  # texels under or against the wall
        assert np.abs(row[keep] - expected[keep]).max() < 0.2, np.abs(row - expected).max()


def test_errors_are_reported() -> None:
    _reset()
    _equator_equinox()
    props = bpy.context.scene.geotools_analysis.sun_hours
    result, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert result != {"FINISHED"}
    _plane(size=1)
    props.period = "RANGE"
    props.end_year, props.end_month, props.end_day = 2026, 1, 1
    _, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert error is not None and "before the start" in error
    props.period = "DAY"
    props.insolation = True
    props.insolation_layer = props.hours_layer
    _, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert error is not None and "different layer names" in error
    props.insolation = False
    props.domain = "TEXTURE"
    _, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert error is not None and "Save the .blend" in error
    # A modifier that changes the topology: face values cannot be stored, and
    # the failed run leaves no empty layer behind.
    props.domain = "FACE"
    plane = bpy.data.objects["Ground"]
    plane.modifiers.new("Subdivide", "SUBSURF").levels = 1
    _, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert error is not None and "No source object" in error, error
    assert "change the topology" in props.error, props.error
    assert "sun_hours" not in h.project().layers


def test_shadow_study_keys_the_sun() -> None:
    _reset()
    _equator_equinox()
    sun_props = bpy.context.scene.geotools_analysis.sun
    h.project().anchor_lon = "150.0"
    sun_props.year = 2024
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = 10.0
    sun_props.frame_start = 5
    result, error = h.call_op(bpy.ops.geotools.sun_shadow_study)
    assert result == {"FINISHED"}, error
    stored = json.loads(bpy.context.scene.geotools_analysis.shadow_study.result)
    # March, June and December by default, at 9:00, 12:00 and 15:00.
    assert stored["frames"] == [5, 13]
    days = [row["local"][:10] for row in stored["moments"]]
    # 2024: 20 Mar 03:06, 20 Jun 20:51 and 21 Dec 09:20 UTC, so local (UTC+10) dates:
    assert days == ["2024-03-20"] * 3 + ["2024-06-21"] * 3 + ["2024-12-21"] * 3, days
    times = [row["local"][11:16] for row in stored["moments"]]
    assert times == ["09:00", "12:00", "15:00"] * 3
    scene = bpy.context.scene
    assert (scene.frame_start, scene.frame_end) == (5, 13)
    assert [m.name for m in sorted(scene.timeline_markers, key=lambda m: m.frame)][:2] == [
        "20 Mar 09:00",
        "20 Mar 12:00",
    ]
    light = bpy.data.objects["GeoTools Sun"]
    sun = h.submodule("analysis.sun")
    for row in stored["moments"]:
        scene.frame_set(row["frame"])
        matrix = np.array(light.matrix_world, dtype=np.float64)
        towards = matrix[:3, 2] / np.linalg.norm(matrix[:3, 2])
        utc = dt.datetime.fromisoformat(row["utc"])
        position = sun.solar_position(utc.timestamp(), 0.0, 150.0, refraction=False)
        expected = sun.direction(position.azimuth, position.elevation)[0]
        assert np.allclose(towards, expected, atol=1e-5), row
        assert position.elevation[0] > 0 and light.data.energy == 1.0, row
    # Constant keys: between two frames the light stays where the first put it.
    scene.frame_set(stored["moments"][0]["frame"])
    first = np.array(light.matrix_world)
    scene.frame_set(stored["moments"][0]["frame"], subframe=0.5)
    assert np.allclose(np.array(light.matrix_world), first)

    # Running again replaces the markers rather than adding more.
    assert h.call_op(bpy.ops.geotools.sun_shadow_study)[0] == {"FINISHED"}
    assert len(scene.timeline_markers) == 9


def test_panels_draw() -> None:
    _reset()
    _equator_equinox()
    ui = h.submodule("blender.analysis_ui")
    props = bpy.context.scene.geotools_analysis.sun_hours
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun_hours)
    drawn = h.props_drawn(calls)
    assert {"period", "step_minutes", "tolerance", "domain", "hours_layer"} <= set(drawn)
    assert "linke_turbidity" not in drawn
    props.insolation = True
    props.period = "RANGE"
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun_hours)
    assert {"linke_turbidity", "diffuse_rays", "end_day"} <= set(h.props_drawn(calls))
    _plane(size=1)
    props.insolation = False
    props.period = "DAY"
    _run(step_minutes=60)
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun_hours)
    assert "2026-03-20" in h.label_text(calls)
    calls = h.draw(ui.GEOTOOLS_PT_analysis_shadow_study)
    assert {"march", "june", "september", "december", "step_hours"} <= set(h.props_drawn(calls))
    assert h.call_op(bpy.ops.geotools.sun_shadow_study)[0] == {"FINISHED"}
    calls = h.draw(ui.GEOTOOLS_PT_analysis_shadow_study)
    assert "render them" in h.label_text(calls)
