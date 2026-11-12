"""M5 analysis tools in Blender: the sun light, point readout and measurement."""

from __future__ import annotations

import datetime as dt
import json
import math

import _harness as h
import bpy
import numpy as np

# The NREL SPA example site and moment (2003-10-17 12:30:30 at UTC-7).
SPA_LON, SPA_LAT = -105.1786, 39.742476
SPA_AZIMUTH = 194.34024


def _analysis():
    return h.submodule("blender.analysis")


def _site():
    return h.submodule("analysis.site")


def _sun():
    return h.submodule("analysis.sun")


def _has_pyproj() -> bool:
    return h.CONFIG.expect == "available"


def _reset() -> None:
    h.reset_scene()
    props = bpy.context.scene.geotools_analysis
    for group in (props.sun, props.measure):
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"}:
                group.property_unset(prop.identifier)
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
    bpy.context.scene.frame_set(1)


def _spa_moment(sun_props) -> None:
    sun_props.year, sun_props.month, sun_props.day = 2003, 10, 17
    sun_props.hour, sun_props.minute, sun_props.second = 12, 30, 30
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = -7.0


def _utm13_project(a: float = 1830.14) -> tuple[float, float]:
    """A UTM zone 13N project whose offset is the SPA example site."""
    import pyproj

    east, north = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:32613", always_xy=True).transform(
        SPA_LON, SPA_LAT
    )
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:32613"
    props.origin_e = repr(east)
    props.origin_n = repr(north)
    props.source_offset_a = repr(a)
    return east, north


def _mga56_edge_project() -> tuple[float, float]:
    """An MGA zone 56 (EPSG:7856) project at the zone's west edge, 30 S."""
    import pyproj

    east, north = pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True).transform(
        150.0, -30.0
    )
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    props.origin_e = repr(east)
    props.origin_n = repr(north)
    props.source_offset_a = "250.0"
    return east, north


def _towards_sun(obj: bpy.types.Object) -> np.ndarray:
    """The light's +Z in world space: it shines along -Z."""
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    return matrix[:3, 2] / np.linalg.norm(matrix[:3, 2])


def _result() -> dict:
    return json.loads(bpy.context.scene.geotools_analysis.sun.result)


def test_analysis_registered() -> None:
    h.ensure_enabled()
    assert hasattr(bpy.context.scene, "geotools_analysis")
    for idname in (
        "geotools.sun_place",
        "geotools.sun_animate",
        "geotools.point_from_cursor",
        "geotools.point_pick",
        "geotools.measure_area",
        "geotools.measure_refresh",
        "geotools.measure_clear",
        "geotools.copy_reading",
    ):
        assert h.operator_available(idname), idname


# --- sun ---------------------------------------------------------------------------------


def test_place_sun_in_a_crs_project() -> None:
    _reset()
    sun_props = bpy.context.scene.geotools_analysis.sun
    _spa_moment(sun_props)
    if not _has_pyproj():
        props = h.project()
        props.placement_mode = "SOURCE_CRS"
        props.crs = "EPSG:32613"
        result, error = h.call_op(bpy.ops.geotools.sun_place)
        assert error is not None and "pyproj" in error, (result, error)
        assert "pyproj" in sun_props.error and sun_props.result == ""
        assert bpy.data.objects.get("GeoTools Sun") is None
        return
    _utm13_project()
    result, error = h.call_op(bpy.ops.geotools.sun_place)
    assert result == {"FINISHED"}, error
    obj = bpy.data.objects["GeoTools Sun"]
    assert obj.data.type == "SUN" and sun_props.light == obj
    stored = _result()
    # Refraction uses standard pressure here (the paper's 820 hPa changes only the
    # elevation), so the azimuth is the paper's.
    assert abs(stored["azimuth"] - SPA_AZIMUTH) < 1e-4, stored
    assert abs(stored["elevation"] - (90.0 - 50.11162)) < 0.01, stored
    assert stored["north"] == "grid north"
    assert -0.15 < stored["convergence"] < 0.0  # just west of zone 13's meridian
    assert stored["scene_azimuth"] == (stored["azimuth"] - stored["convergence"]) % 360.0
    expected = _sun().direction(stored["scene_azimuth"], stored["elevation"])[0]
    assert np.allclose(_towards_sun(obj), expected, atol=1e-6), (_towards_sun(obj), expected)
    assert obj.data.energy == sun_props.strength

    # Night: the light is placed but switched off, with a note.
    sun_props.hour = 23
    result, error = h.call_op(bpy.ops.geotools.sun_place)
    assert result == {"FINISHED"}, error
    assert obj.data.energy == 0.0
    assert any("below the horizon" in note for note in _result()["notes"])
    assert len([o for o in bpy.data.objects if o.name.startswith("GeoTools Sun")]) == 1


def test_convergence_turns_the_sun_at_a_zone_edge() -> None:
    _reset()
    if not _has_pyproj():
        return
    _mga56_edge_project()
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.year, sun_props.month, sun_props.day = 2026, 12, 21
    sun_props.hour = 9
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = 10.0
    result, error = h.call_op(bpy.ops.geotools.sun_place)
    assert result == {"FINISHED"}, error
    stored = _result()
    assert 1.45 < stored["convergence"] < 1.55, stored
    # The light is turned by the convergence: its azimuth from +Y is the true azimuth
    # minus about 1.5 degrees.
    towards = _towards_sun(bpy.data.objects["GeoTools Sun"])
    scene_azimuth = math.degrees(math.atan2(towards[0], towards[1])) % 360.0
    assert abs(scene_azimuth - (stored["azimuth"] - stored["convergence"])) < 1e-4
    assert abs(stored["latitude"] + 30.0) < 1e-9


def test_animate_a_day() -> None:
    _reset()
    sun_props = bpy.context.scene.geotools_analysis.sun
    # An ANCHOR project needs no pyproj for the sun.
    props = h.project()
    props.placement_mode = "ANCHOR"
    props.anchor_lon, props.anchor_lat, props.anchor_height = "153.02", "-27.47", "10"
    props.heading = "30"
    sun_props.year, sun_props.month, sun_props.day = 2026, 6, 21
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = 10.0
    sun_props.start_hour, sun_props.end_hour, sun_props.step_minutes = 6.0, 18.0, 30
    sun_props.frame_start, sun_props.frames_per_step = 10, 2
    sun_props.strength = 3.0
    result, error = h.call_op(bpy.ops.geotools.sun_animate)
    assert result == {"FINISHED"}, error
    stored = _result()
    assert stored["steps"] == 25 and stored["frames"] == [10, 58], stored
    assert stored["north"] == "anchor heading" and stored["convergence"] == -30.0
    obj = bpy.data.objects["GeoTools Sun"]

    # Frame 34 is step 12: 12:00 local, 02:00 UTC.
    scene = bpy.context.scene
    scene.frame_set(34)
    utc = dt.datetime(2026, 6, 21, 2, tzinfo=dt.UTC).timestamp()
    position = _sun().solar_position(utc, -27.47, 153.02, 10.0)
    expected = _sun().direction((position.azimuth + 30.0) % 360.0, position.elevation)[0]
    assert np.allclose(_towards_sun(obj), expected, atol=1e-5)
    assert obj.data.energy == 3.0
    scene.frame_set(10)  # 06:00, before the winter sunrise in Brisbane
    assert obj.data.energy == 0.0

    # Animating again replaces the keyframes; placing removes them.
    assert h.call_op(bpy.ops.geotools.sun_animate)[0] == {"FINISHED"}
    actions = [a for a in bpy.data.actions if a.name.startswith("GeoTools Sun")]
    assert len(actions) == 2, [a.name for a in actions]
    assert h.call_op(bpy.ops.geotools.sun_place)[0] == {"FINISHED"}
    assert obj.animation_data is None or obj.animation_data.action is None
    assert not [a for a in bpy.data.actions if a.name.startswith("GeoTools Sun")]


def test_local_project_uses_the_anchor_latitude() -> None:
    _reset()
    props = h.project()
    props.anchor_lon, props.anchor_lat = "-0.1276", "51.5072"
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.month, sun_props.day, sun_props.hour = 6, 21, 13  # 13:00 BST = 12:00 UTC
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = 1.0
    result, error = h.call_op(bpy.ops.geotools.sun_place)
    assert result == {"FINISHED"}, error
    stored = _result()
    assert stored["north"] == "true north" and stored["convergence"] == 0.0
    assert 61.0 < stored["elevation"] < 62.5  # 90 - 51.5 + 23.44, near midsummer noon


def test_time_zone_errors() -> None:
    _reset()
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.tz_name = "Nowhere/Atlantis"
    _, error = h.call_op(bpy.ops.geotools.sun_place)
    assert error is not None and ("Unknown time zone" in error or "UTC offset" in error)
    sun_props.tz_name = "UTC"
    sun_props.month, sun_props.day = 2, 30
    _, error = h.call_op(bpy.ops.geotools.sun_place)
    assert error is not None and "Not a date" in error


# --- coordinates and measurement ---------------------------------------------------------------


def test_point_readout_and_distance() -> None:
    _reset()
    measure = bpy.context.scene.geotools_analysis.measure
    if not _has_pyproj():
        props = h.project()
        props.placement_mode = "SOURCE_CRS"
        props.crs = "EPSG:7856"
        result, error = h.call_op(bpy.ops.geotools.point_from_cursor, point="A")
        assert error is not None and "pyproj" in error
        assert "pyproj" in measure.error and measure.reading_a == ""
        return
    east, north = _mga56_edge_project()
    scene = bpy.context.scene
    scene.cursor.location = (1234.567, -89.1, 3.3)
    result, error = h.call_op(bpy.ops.geotools.point_from_cursor, point="A")
    assert result == {"FINISHED"}, error
    reading = _analysis().load_reading(measure.reading_a)
    held = np.asarray((1234.567, -89.1, 3.3), dtype=np.float32).astype(np.float64)
    assert reading.east == east + held[0]  # binary64 offset + float32 local, exactly
    assert reading.north == north + held[1]
    assert reading.level == 250.0 + held[2]
    assert reading.geodetic_crs == "GDA2020"
    assert 1.45 < reading.convergence < 1.55
    assert abs(reading.resolution - 1.22e-4) < 1e-6
    assert measure.distance == ""  # only A so far

    scene.cursor.location = (1234.567 + 300.0, -89.1 + 400.0, 3.3)
    assert h.call_op(bpy.ops.geotools.point_from_cursor, point="B")[0] == {"FINISHED"}
    measurement = _analysis().load_measurement(measure.distance)
    assert abs(measurement.grid_distance - 500.0) < 1e-3
    ground = measurement.grid_distance / measurement.combined_scale
    assert abs(measurement.ground_distance - ground) < 1e-9
    assert measurement.combined_scale > 1.0  # k > 1 at the zone edge beats 250 m of height

    text = _analysis().reading_text(bpy.context, "DISTANCE")
    assert f"Ground distance (m)\t{measurement.ground_distance!r}" in text
    assert h.call_op(bpy.ops.geotools.copy_reading, what="A")[0] == {"FINISHED"}

    # A changed project marks the readings stale until they are read again.
    from_panel = h.draw(h.submodule("blender.analysis_ui").GEOTOOLS_PT_analysis_coordinates)
    assert "Project changed" not in h.label_text(from_panel)
    h.project().source_offset_a = "260.0"
    from_panel = h.draw(h.submodule("blender.analysis_ui").GEOTOOLS_PT_analysis_coordinates)
    assert "Project changed" in h.label_text(from_panel)
    assert h.call_op(bpy.ops.geotools.measure_refresh)[0] == {"FINISHED"}
    assert _analysis().load_reading(measure.reading_a).level == 260.0 + held[2]
    assert h.call_op(bpy.ops.geotools.measure_clear)[0] == {"FINISHED"}
    assert measure.reading_a == measure.distance == ""


def test_anchor_readout_without_pyproj_still_reads() -> None:
    _reset()
    props = h.project()
    props.placement_mode = "ANCHOR"
    props.anchor_lon, props.anchor_lat = "153.02", "-27.47"
    bpy.context.scene.cursor.location = (3.0, 4.0, 0.0)
    result, error = h.call_op(bpy.ops.geotools.point_from_cursor, point="A")
    assert result == {"FINISHED"}, error
    measure = bpy.context.scene.geotools_analysis.measure
    reading = _analysis().load_reading(measure.reading_a)
    assert (reading.east, reading.north) == (3.0, 4.0)
    if _has_pyproj():
        assert reading.latitude is not None and measure.note == ""
    else:
        assert reading.latitude is None and "pyproj" in measure.note


def test_area_of_selected_faces() -> None:
    _reset()
    bpy.ops.mesh.primitive_grid_add(x_subdivisions=11, y_subdivisions=11, size=10.0)
    obj = bpy.context.active_object
    selected = np.zeros(len(obj.data.polygons), dtype=bool)
    selected[: len(selected) // 2] = True
    face_area = np.empty(len(selected))
    obj.data.polygons.foreach_get("area", face_area)
    plan = float(face_area[selected].sum())
    obj.data.polygons.foreach_set("select", selected)
    # A local project needs no pyproj: ground and grid are the same.
    result, error = h.call_op(bpy.ops.geotools.measure_area)
    assert result == {"FINISHED"}, error
    local = _analysis().load_area(bpy.context.scene.geotools_analysis.measure.area)
    assert abs(local.ground_plan - plan) < 1e-4 and local.ground_plan == local.grid_plan
    if not _has_pyproj():
        props = h.project()
        props.placement_mode = "SOURCE_CRS"
        props.crs = "EPSG:7856"
        result, error = h.call_op(bpy.ops.geotools.measure_area)
        assert error is not None and "pyproj" in error
        return
    _mga56_edge_project()
    result, error = h.call_op(bpy.ops.geotools.measure_area)
    assert result == {"FINISHED"}, error
    area = _analysis().load_area(bpy.context.scene.geotools_analysis.measure.area)
    assert area.triangles == 2 * int(selected.sum())
    assert abs(area.grid_plan - plan) < 1e-4
    csf = _site().read_point(_analysis()._placement(bpy.context), (0.0, 0.0, 0.0)).combined_scale
    assert abs(area.ground_plan - plan / csf**2) < 1e-4

    obj.data.polygons.foreach_set("select", np.zeros(len(selected), dtype=bool))
    result, error = h.call_op(bpy.ops.geotools.measure_area)
    assert result == {"FINISHED"}, error
    assert _analysis().load_area(bpy.context.scene.geotools_analysis.measure.area).triangles == 0


# --- panels ------------------------------------------------------------------------------------


def test_panels_draw() -> None:
    _reset()
    ui = h.submodule("blender.analysis_ui")
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:32613"
    calls = h.draw(ui.GEOTOOLS_PT_analysis)
    assert ("pyproj" in h.label_text(calls)) is (not _has_pyproj())
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun)
    assert "tz_name" in h.props_drawn(calls) and "site" in h.props_drawn(calls)
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.tz_mode = "OFFSET"
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun)
    assert "utc_offset" in h.props_drawn(calls)
    assert "no daylight saving" in h.label_text(calls)
    h.draw(ui.GEOTOOLS_PT_analysis_coordinates)

    # LOCAL: the sun panel offers the latitude and longitude it uses.
    props.placement_mode = "LOCAL"
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun)
    assert {"anchor_lon", "anchor_lat"} <= set(h.props_drawn(calls))
    assert h.call_op(bpy.ops.geotools.sun_place)[0] == {"FINISHED"}
    calls = h.draw(ui.GEOTOOLS_PT_analysis_sun)
    assert "+Y is taken as true north" in h.label_text(calls)

    bpy.context.scene.cursor.location = (1.0, 2.0, 3.0)
    assert h.call_op(bpy.ops.geotools.point_from_cursor, point="A")[0] == {"FINISHED"}
    assert h.call_op(bpy.ops.geotools.point_from_cursor, point="B")[0] == {"FINISHED"}
    calls = h.draw(ui.GEOTOOLS_PT_analysis_coordinates)
    assert "A to B" in h.label_text(calls)
