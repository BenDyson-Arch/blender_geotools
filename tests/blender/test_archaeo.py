"""Archaeoastronomy in Blender (``blender.archaeo``): horizon, events, sightlines, alignments.

The terrain is synthetic and chosen so the horizon is known in closed form.
An east-west ridge with a flat top at height H whose near face runs along
y = D, seen from an eye at height e above the origin, has its skyline at the
face's top edge, so at azimuth A (|A| < 90°, the ridge being long enough)
the horizon's altitude is

    atan((H - e) cos A / D)

before curvature. A far ridge at 20 km checks the curvature drop,
(1 - k) d / 2R, with k 0.13 when refraction is on and 0 when it is off.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
from pathlib import Path

import _harness as h
import bpy
import numpy as np

RIDGE_D, RIDGE_H, EYE = 100.0, 10.0, 1.6
FAR_D, FAR_H = 20000.0, 500.0


def _blender():
    return h.submodule("blender.archaeo")


def _core():
    return h.submodule("analysis.archaeo")


def _horizon():
    return h.submodule("analysis.horizon")


def _reset(lat: str = "0.0") -> None:
    h.reset_scene()
    analysis = bpy.context.scene.geotools_analysis
    for group in (analysis.sun, analysis.sun_hours, analysis.archaeo):
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"}:
                group.property_unset(prop.identifier)
    props = h.project()
    props.placement_mode = "LOCAL"
    props.anchor_lon, props.anchor_lat = "0.0", lat
    sun_props = analysis.sun
    sun_props.tz_mode, sun_props.tz_name = "ZONE", "UTC"


def _box(name: str, x0, x1, y0, y1, z0, z1, collection=None) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    verts = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    mesh.from_pydata(verts, [], faces)
    obj = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def _terrain(far: bool = False) -> None:
    """A 400 m ground slab (the source) and the ridge to the north; optionally the far ridge."""
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    _box("Ground", -200, 200, -200, 200, -1.0, 0.0, collection)
    _box("Ridge", -5000, 5000, RIDGE_D, RIDGE_D + 10, 0.0, RIDGE_H)
    if far:
        bpy.data.objects["Ridge"].location.z = -100.0  # out of the way
        _box("Far", -40000, 40000, FAR_D, FAR_D + 100, 0.0, FAR_H)


def _observer() -> None:
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
    assert bpy.ops.geotools.archaeo_observer_cursor() == {"FINISHED"}


def _profile(**settings):
    props = bpy.context.scene.geotools_analysis.archaeo
    for key, value in settings.items():
        setattr(props, key, value)
    result, error = h.call_op(bpy.ops.geotools.archaeo_horizon)
    assert result == {"FINISHED"}, error or props.profile_error
    return _horizon().HorizonProfile.from_json(props.profile)


def _ridge(azimuth: np.ndarray, scale: float = 1.0) -> np.ndarray:
    a = np.radians(azimuth)
    return np.degrees(np.arctan((RIDGE_H - EYE) * np.cos(a) / (RIDGE_D / scale)))


# --- tests ------------------------------------------------------------------------------------


def test_archaeo_registered() -> None:
    for idname in (
        "archaeo_observer_cursor",
        "archaeo_observer_pick",
        "archaeo_horizon",
        "archaeo_clear_horizon",
        "archaeo_events",
        "archaeo_sightlines",
        "archaeo_alignment",
        "archaeo_copy",
    ):
        assert h.operator_available(f"geotools.{idname}"), idname
    props = bpy.context.scene.geotools_analysis.archaeo
    assert props.epoch_year == 2026
    # Geodetic results are text (binary64), never FloatProperty.
    for name in ("observer", "profile", "events", "alignment"):
        assert props.bl_rna.properties[name].type == "STRING", name


def test_ridge_horizon() -> None:
    """The profile matches the ridge's closed form, and the open and edge azimuths are flagged."""
    _reset()
    _terrain()
    _observer()
    profile = _profile(curvature=False, refraction=False, azimuth_step=1.0, eye_height=EYE)
    assert len(profile.azimuth) == 360
    north = (profile.azimuth <= 75.0) | (profile.azimuth >= 285.0)
    error = profile.elevation[north] - _ridge(profile.azimuth[north])
    assert np.abs(error).max() < 0.003, (
        np.abs(error).max(),
        profile.azimuth[north][np.argmax(np.abs(error))],
    )
    # At azimuth 0 the skyline is the ridge's top edge, RIDGE_D away.
    i = profile.nearest(0.0)
    reach = profile.distance[i] * math.cos(math.radians(profile.geometric[i]))
    assert abs(reach - RIDGE_D) < 0.5, reach
    # To the south only the ground slab: its edge, slightly below level, flagged.
    j = profile.nearest(180.0)
    assert profile.edge[j] and not profile.edge[i]
    expected = -math.degrees(math.atan(EYE / 200.0))
    assert abs(profile.elevation[j] - expected) < 0.003, profile.elevation[j]
    assert profile.info["rays"] > 360 * 700
    assert profile.info["seconds"] < 60.0


def test_curvature_and_terrestrial_refraction() -> None:
    _reset()
    _terrain(far=True)
    _observer()
    flat = math.degrees(math.atan((FAR_H - EYE) / FAR_D))
    radius = _horizon().EARTH_RADIUS
    for curvature, refraction, k in ((False, False, 0.0), (True, False, 0.0), (True, True, 0.13)):
        profile = _profile(
            curvature=curvature, refraction=refraction, azimuth_step=10.0, eye_height=EYE
        )
        drop = math.degrees((1 - k) * FAR_D / (2 * radius)) if curvature else 0.0
        got = profile.at(0.0)
        assert abs(got - (flat - drop)) < 0.003, (curvature, refraction, got, flat - drop)


def test_ignore_within_drops_the_near_ridge() -> None:
    _reset()
    _terrain()
    _observer()
    near = _profile(curvature=False, refraction=False, azimuth_step=5.0, eye_height=EYE)
    assert near.at(0.0) > 4.0
    far = _profile(
        curvature=False, refraction=False, azimuth_step=5.0, eye_height=EYE, ignore_within=150.0
    )
    # The ridge (100 m) is left out; only the slab's edge (200 m) is left, below level.
    i = far.nearest(0.0)
    assert far.elevation[i] < 0.0 and far.edge[i], far.elevation[i]


def test_events_on_the_profile() -> None:
    """At 51 N the June sunrise is behind the ridge; inverting it gives back ε."""
    _reset(lat="51.0")
    _terrain()
    _observer()
    profile = _profile(curvature=False, refraction=True, azimuth_step=0.5, eye_height=EYE)
    props = bpy.context.scene.geotools_analysis.archaeo
    props.epoch_year = -2999
    result, error = h.call_op(bpy.ops.geotools.archaeo_events)
    assert result == {"FINISHED"}, error or props.events_error
    events, summary = _blender().load_events(bpy.context)
    assert abs(summary["obliquity"] - 24.0209) < 0.001
    assert summary["epoch"] == "3000 BCE" and summary["trust"]
    june = next(e for e in events if e.name == "June solstice sunrise")
    assert abs(june.declination - summary["obliquity"]) < 1e-9
    # Behind the ridge it rises later, and further south: its azimuth grows as it climbs.
    assert 2.0 < june.horizon_azimuth - june.azimuth < 8.0, (june.azimuth, june.horizon_azimuth)
    assert abs(june.horizon_altitude - profile.at(june.horizon_azimuth)) < 1e-9
    assert june.horizon_altitude > 1.0
    semi = _core().SUN_SEMI_DIAMETER
    back = float(_core().declination(june.horizon_azimuth, june.horizon_altitude, 51.0, limb=semi))
    assert abs(back - june.declination) < 0.003, back
    moon = [e for e in events if e.body == "moon"]
    assert len(moon) == 8 and "major" in summary and "minor" in summary
    dated = [e for e in events if e.kind == "dated"]
    assert len(dated) >= 8 and all(e.time for e in dated)
    # Copy has every event at full precision.
    text = _blender().copy_text(bpy.context, "EVENTS")
    assert "June solstice sunrise" in text and repr(june.azimuth) in text


def test_sightlines_export_and_alignment() -> None:
    _reset(lat="51.0")
    _terrain()
    _observer()
    _profile(curvature=False, refraction=True, azimuth_step=1.0, eye_height=EYE)
    props = bpy.context.scene.geotools_analysis.archaeo
    props.dated_events = False
    result, error = h.call_op(bpy.ops.geotools.archaeo_events)
    assert result == {"FINISHED"}, error or props.events_error
    events, _summary = _blender().load_events(bpy.context)
    result, error = h.call_op(bpy.ops.geotools.archaeo_sightlines)
    assert result == {"FINISHED"}, error or props.events_error
    obj = bpy.data.objects["GeoTools Sightlines"]
    mesh = obj.data
    finite = [e for e in events if math.isfinite(e.best_azimuth)]
    assert len(mesh.edges) == len(finite) and len(mesh.polygons) == 0
    names = [item.value.decode() for item in mesh.attributes["event"].data]
    assert names == [e.name for e in finite]
    azimuth = np.empty(len(mesh.edges), np.float32)
    mesh.attributes["azimuth"].data.foreach_get("value", azimuth)
    assert np.allclose(azimuth, [e.best_azimuth for e in finite], atol=1e-4)
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 2, 3)
    # Northern events end on the ridge's top edge (y = RIDGE_D, z = RIDGE_H).
    k = names.index("Major standstill north moonrise")
    end = co[k, 1]
    assert abs(end[1] - RIDGE_D) < 0.5 and abs(end[2] - RIDGE_H) < 0.1, end
    assert np.allclose(co[:, 0], (0.0, 0.0, EYE), atol=1e-5)

    # Export as GeoJSON lines through the M8 exporter.
    tmp = Path(tempfile.mkdtemp(prefix="geotools-archaeo-"))
    try:
        path = tmp / "sightlines.geojson"
        result, error = h.call_op(
            bpy.ops.geotools.export_vector,
            filepath=str(path),
            file_format="GEOJSON",
            object_name=obj.name,
        )
        assert result == {"FINISHED"}, error
        data = json.loads(path.read_text())
        assert len(data["features"]) == len(finite)
        feature = data["features"][k]
        assert feature["geometry"]["type"] == "LineString"
        assert feature["properties"]["event"] == "Major standstill north moonrise"
        assert abs(feature["properties"]["azimuth"] - finite[k].best_azimuth) < 1e-3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Alignment: a wall along the June sunrise azimuth, and one due north-south.
    june = next(e for e in finite if e.name == "June solstice sunrise")
    a = math.radians(june.best_azimuth)
    wall = bpy.data.meshes.new("Walls")
    wall.from_pydata(
        [(10, 10, 0), (10 + 20 * math.sin(a), 10 + 20 * math.cos(a), 0), (0, -30, 0), (0, -10, 0)],
        [(0, 1), (2, 3)],
        [],
    )
    walls = bpy.data.objects.new("Walls", wall)
    bpy.context.scene.collection.objects.link(walls)
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    walls.select_set(True)
    bpy.context.view_layer.objects.active = walls
    result, error = h.call_op(bpy.ops.geotools.archaeo_alignment)
    assert result == {"FINISHED"}, error or props.alignment_error
    rows = json.loads(props.alignment)["rows"]
    assert len(rows) == 2  # nothing selected: every edge
    first, second = rows
    assert abs(first["azimuth"] - june.best_azimuth) < 1e-3
    assert first["nearest"][0][0] == "June solstice sunrise"
    assert abs(first["nearest"][0][2]) < 1e-3
    # The inversion along it gives the sun's centre on that horizon: ε less the limb.
    eps = json.loads(props.events)["summary"]["obliquity"]
    assert abs(first["declination"] - eps) < 0.4
    assert abs(second["azimuth"]) < 1e-6 or abs(second["azimuth"] - 360) < 1e-6
    assert second["altitude"] > 4.0  # the ridge, due north
    assert "June solstice sunrise" in _blender().copy_text(bpy.context, "ALIGNMENT")


def test_sun_hours_uses_the_profile() -> None:
    _reset(lat="0.0")
    _terrain()
    sun_hours = h.submodule("blender.sun_hours")
    analysis = bpy.context.scene.geotools_analysis
    analysis.sun.year, analysis.sun.month, analysis.sun.day = 2026, 3, 20
    analysis.sun_hours.horizon_profile = True
    try:
        sun_hours.plan(bpy.context)
    except RuntimeError as exc:
        assert "no profile" in str(exc)
    else:
        raise AssertionError("a missing profile must be refused")
    _observer()
    _profile(curvature=False, refraction=False, azimuth_step=1.0, eye_height=EYE)
    # A wall due east (sunrise at the equinox on the equator) raises the horizon there.
    the_plan = sun_hours.plan(bpy.context)
    assert the_plan.parameters["horizon_profile"]["azimuths"] == 360
    _box("East", 50, 60, -500, 500, 0.0, 40.0)
    _profile(curvature=False, refraction=False, azimuth_step=1.0, eye_height=EYE)
    blocked = sun_hours.plan(bpy.context)
    assert (
        blocked.parameters["steps_below_horizon_profile"]
        > the_plan.parameters["steps_below_horizon_profile"]
    )
    assert blocked.parameters["sun_hours_possible"] < the_plan.parameters["sun_hours_possible"]


def test_projected_crs_turns_grid_to_true() -> None:
    """In MGA zone 56 the ridge to grid north is at true azimuth = convergence."""
    if not h.submodule("analysis.site").available():
        return
    import pyproj

    _reset()
    east, north = pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True).transform(
        150.5, -27.5
    )
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    props.origin_e = repr(east)
    props.origin_n = repr(north)
    props.source_offset_a = "100.0"
    _terrain()
    _observer()
    profile = _profile(curvature=False, refraction=False, azimuth_step=0.5, eye_height=EYE)
    convergence = profile.info["convergence"]
    assert convergence > 0.5  # west of the central meridian, southern hemisphere
    scale = profile.info["combined_scale"]
    true = np.array([300.0, 330.0, 30.0, 60.0])
    expected = _ridge(true - convergence, scale)
    got = profile.at(true)
    assert np.abs(got - expected).max() < 0.004, (got, expected)


def test_panel_draws() -> None:
    _reset()
    panel = h.submodule("blender.archaeo_ui").GEOTOOLS_PT_analysis_archaeo
    h.draw(panel)
    _terrain()
    _observer()
    _profile(curvature=False, refraction=False, azimuth_step=5.0, eye_height=EYE)
    result, error = h.call_op(bpy.ops.geotools.archaeo_events)
    assert result == {"FINISHED"}, error
    labels = h.label_text(h.draw(panel))
    assert "ε" in labels and "June sol. sunrise" in labels, labels


def test_sightlines_publish_as_vectors() -> None:
    """The sightlines' collection publishes through the vector publish (LOCAL: local metres)."""
    if h.CONFIG.expect != "available":
        return
    _reset(lat="51.0")
    _terrain()
    _observer()
    props = bpy.context.scene.geotools_analysis.archaeo
    props.dated_events = False
    props.moon_events = False
    assert h.call_op(bpy.ops.geotools.archaeo_events)[0] == {"FINISHED"}
    assert h.call_op(bpy.ops.geotools.archaeo_sightlines)[0] == {"FINISHED"}
    publish = h.submodule("blender.publish")
    tmp = Path(tempfile.mkdtemp(prefix="geotools-archaeo-publish-"))
    try:
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        project = h.project()
        profile = project.build_profiles[project.build_profile_index]
        profile.output_type = "VECTOR"
        profile.output_path = str(tmp / "sightlines.3tz")
        profile.vector_collection = bpy.data.collections["GeoTools Sightlines"]
        profile.overwrite = True
        result, error = h.call_op(bpy.ops.geotools.publish)
        assert error is None and result == {"FINISHED"}, error
        job = publish.active_job()
        assert job is not None and job.join(180.0)
        publish.update_from_job()
        assert job.state.name == "DONE", job.error
        record = json.loads((tmp / "sightlines.geotools" / "build.json").read_text())
        tables = {t["table"]: t for t in record["vectors"]["tables"]}
        (table,) = tables.values()
        assert table["kind"] == "lines" and table["features"] == 6, tables
        assert record["validation"]["ok"] is True
    finally:
        publish.shutdown_job()
        shutil.rmtree(tmp, ignore_errors=True)
