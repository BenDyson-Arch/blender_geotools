"""Validation of the solar tools in Blender against independent references.

The references (``tests/data/validation/solar/``) come from Skyfield with JPL
DE440s and pyproj geodesics, computed by ``scripts/validation/solar/``; see
docs/validation/solar.md. These tests check what only Blender can: the Sun
light's orientation, its keyframes, the Shadow Study's days, and the Cycles
ray bake of sun hours on tilted surfaces and behind a wall.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from functools import cache
from pathlib import Path

import _harness as h
import bpy
import numpy as np

DATA = Path(__file__).resolve().parents[1] / "data" / "validation" / "solar"


@cache
def _load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


def _has_pyproj() -> bool:
    return h.CONFIG.expect == "available"


def _reset() -> None:
    h.reset_scene()
    analysis = bpy.context.scene.geotools_analysis
    for group in (analysis.sun, analysis.sun_hours, analysis.shadow_study):
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"}:
                group.property_unset(prop.identifier)
    for marker in list(bpy.context.scene.timeline_markers):
        bpy.context.scene.timeline_markers.remove(marker)
    bpy.context.scene.cursor.location = (0.0, 0.0, 0.0)
    bpy.context.scene.frame_set(1)


def _local_project(lon: float, lat: float) -> None:
    props = h.project()
    props.placement_mode = "LOCAL"
    props.anchor_lon, props.anchor_lat = repr(lon), repr(lat)


def _crs_project(crs: str, east: float, north: float) -> None:
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = crs
    props.origin_e = repr(east)
    props.origin_n = repr(north)
    props.source_offset_a = "0.0"


def _sun_date(day: str, offset: float) -> None:
    """The Sun panel's date, at a fixed UTC offset.

    Fixed offsets, not the references' IANA zones: Blender on Windows has no
    time zone database. None of the days used here has a daylight-saving
    change, so the offset gives the same local day as the zone.
    """
    sun_props = bpy.context.scene.geotools_analysis.sun
    date = dt.date.fromisoformat(day)
    sun_props.year, sun_props.month, sun_props.day = date.year, date.month, date.day
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = offset


def _source(name: str, verts, faces) -> bpy.types.Object:
    collection = bpy.data.collections.get("Source") or bpy.data.collections.new("Source")
    if collection.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(map(float, v)) for v in verts], [], faces)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _grid(x0: float, x1: float, y0: float, y1: float, size: float) -> tuple[np.ndarray, list]:
    nx, ny = round((x1 - x0) / size), round((y1 - y0) / size)
    xs = np.linspace(x0, x1, nx + 1)
    ys = np.linspace(y0, y1, ny + 1)
    gx, gy = np.meshgrid(xs, ys, indexing="xy")
    verts = np.stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)], axis=1)
    faces = []
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            faces.append((a, a + 1, a + nx + 2, a + nx + 1))
    return verts, faces


def _values(obj: bpy.types.Object, name: str) -> np.ndarray:
    attr = obj.data.attributes[f"gt_{name}"]
    values = np.empty(len(attr.data), np.float32)
    attr.data.foreach_get("value", values)
    return values


def _face_centres(obj: bpy.types.Object) -> np.ndarray:
    centres = np.empty(len(obj.data.polygons) * 3, np.float32)
    obj.data.polygons.foreach_get("center", centres)
    return centres.reshape(-1, 3)


def _sun_hours(**settings) -> dict:
    props = bpy.context.scene.geotools_analysis.sun_hours
    for key, value in settings.items():
        setattr(props, key, value)
    result, error = h.call_op(bpy.ops.geotools.sun_hours)
    assert result == {"FINISHED"}, error or props.error
    return json.loads(props.result)


def _towards_sun(obj: bpy.types.Object) -> np.ndarray:
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    return matrix[:3, 2] / np.linalg.norm(matrix[:3, 2])


# --- the Sun light ---------------------------------------------------------------------------


def test_light_points_at_the_geodesic_sun() -> None:
    """Place Sun at a zone edge: the light against Skyfield's sun and a geodesic grid bearing.

    MGA zone 56 at 150 E, 30 S: the convergence is 1.5 degrees. The reference
    azimuth is turned into a grid bearing by projecting a 1 m geodesic step
    along it, so neither PROJ's get_factors nor GeoTools' SPA is used. Within
    0.001 degrees: SPA against DE440 is 0.0003, UT1 - UTC (taken as 0) adds
    up to 0.0004 in 2026.
    """
    _reset()
    if not _has_pyproj():
        return
    ref = _load("sun_hours.json")["wall"]
    _crs_project(ref["crs"], ref["east"], ref["north"])
    instant = dt.datetime.fromisoformat(ref["instant"])
    sun_props = bpy.context.scene.geotools_analysis.sun
    _sun_date(instant.date().isoformat(), 0.0)
    sun_props.hour, sun_props.minute, sun_props.second = instant.hour, instant.minute, 0
    sun_props.refraction = False
    result, error = h.call_op(bpy.ops.geotools.sun_place)
    assert result == {"FINISHED"}, error
    towards = _towards_sun(bpy.data.objects["GeoTools Sun"])
    elevation = math.degrees(math.asin(towards[2]))
    bearing = math.degrees(math.atan2(towards[0], towards[1])) % 360.0
    assert abs(elevation - ref["elevation_airless"]) < 1e-3, (elevation, ref)
    assert abs(bearing - ref["grid_bearing"]) < 1e-3, (bearing, ref)
    # Without the convergence the light would be 1.5 degrees off.
    assert abs(bearing - ref["azimuth"]) > 1.4


def test_light_in_a_non_conformal_crs() -> None:
    """Place Sun in Australian Albers (EPSG:3577) at 150 E, 12 S, a June morning.

    The light's azimuth from +Y is the grid bearing of a 1 m geodesic step along
    the sun's true azimuth, projected (no get_factors), to 1e-6 degrees; turning
    by the convergence alone put it 1.1 degrees out there. The panel says why.
    """
    _reset()
    if not _has_pyproj():
        return
    import pyproj

    lon, lat = 150.0, -12.0
    east, north = pyproj.Transformer.from_crs("EPSG:4283", "EPSG:3577", always_xy=True).transform(
        lon, lat
    )
    _crs_project("EPSG:3577", east, north)
    sun_props = bpy.context.scene.geotools_analysis.sun
    _sun_date("2026-06-21", 10.0)
    sun_props.hour, sun_props.minute, sun_props.second = 8, 30, 0
    result, error = h.call_op(bpy.ops.geotools.sun_place)
    assert result == {"FINISHED"}, error
    stored = json.loads(sun_props.result)
    towards = _towards_sun(bpy.data.objects["GeoTools Sun"])
    bearing = math.degrees(math.atan2(towards[0], towards[1])) % 360.0
    geod = pyproj.Geod(ellps="GRS80")
    forward = pyproj.Transformer.from_crs("EPSG:4283", "EPSG:3577", always_xy=True)
    ends = [geod.fwd(lon, lat, stored["azimuth"], d)[:2] for d in (1.0, -1.0)]
    (e1, n1), (e0, n0) = (forward.transform(*end) for end in ends)
    grid = math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360.0
    assert abs(bearing - grid) < 1e-4, (bearing, grid)  # float32 light rotation
    assert abs(stored["scene_azimuth"] - grid) < 1e-6, (stored["scene_azimuth"], grid)
    turned = (stored["azimuth"] - stored["convergence"]) % 360.0
    assert abs(turned - grid) > 0.5, (turned, grid)
    assert any("not conformal" in note for note in stored["notes"]), stored["notes"]


def test_day_keys_switch_off_at_skyfield_sunrise_and_sunset() -> None:
    """Over a Day at 1-minute keys: the light is on exactly between Skyfield's rise and set.

    51.18 N on the June solstice, Europe/London (UTC+01:00 that day), refraction on (the centre's
    SPA-refracted elevation above 0, as GeoTools defines "up"). A key may
    disagree only within 2 s of an event (SPA against DE440 near the horizon,
    and UT1 - UTC).
    """
    _reset()
    ref = next(c for c in _load("sun_hours.json")["open"] if c["name"] == "51 N June solstice")
    _local_project(ref["longitude"], ref["latitude"])
    assert ref["zone"] == "Europe/London"
    _sun_date(ref["first"], 1.0)
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.start_hour, sun_props.end_hour, sun_props.step_minutes = 0.0, 24.0, 1
    sun_props.frame_start, sun_props.frames_per_step = 1, 1
    result, error = h.call_op(bpy.ops.geotools.sun_animate)
    assert result == {"FINISHED"}, error
    stored = json.loads(sun_props.result)
    assert stored["steps"] == 1441
    start = dt.datetime(2026, 6, 21, tzinfo=dt.timezone(dt.timedelta(hours=1))).timestamp()
    rise = next(t for t, kind in ref["events"] if kind == 1)
    sets = next(t for t, kind in ref["events"] if kind == -1)
    light = bpy.data.objects["GeoTools Sun"]
    scene = bpy.context.scene
    wrong, on = [], 0
    for i in range(1441):
        scene.frame_set(1 + i)
        when = start + 60.0 * i
        expected = rise < when < sets
        on += light.data.energy > 0.0
        if (light.data.energy > 0.0) != expected and min(abs(when - rise), abs(when - sets)) > 2:
            wrong.append((i, when))
    assert not wrong, wrong[:5]
    assert abs(on - ref["hours"] * 60.0) <= 1.0, on  # 16.5 h of keys at one a minute
    # The keys around sunrise: off, then on.
    first_on = math.ceil((rise - start) / 60.0)
    scene.frame_set(first_on)
    assert light.data.energy == 0.0
    scene.frame_set(first_on + 1)
    assert light.data.energy == sun_props.strength


def test_shadow_study_days_in_far_time_zones() -> None:
    """The equinox and solstice days, taken in UTC+14 and UTC-11, against Skyfield's moments.

    Kiritimati (UTC+14) and Pago Pago (UTC-11) as fixed offsets: neither has
    daylight saving, and Blender on Windows has no time zone database.
    """
    _reset()
    seasons = _load("seasons.json")
    names = seasons["seasons"]
    moments = {
        names[s]: u
        for y, s, u in zip(seasons["year"], seasons["season"], seasons["ut1"], strict=True)
        if y == 2026
    }
    props = h.project()
    props.placement_mode = "ANCHOR"
    props.anchor_lon, props.anchor_lat, props.anchor_height = "-157.4", "1.87", "0"
    study = bpy.context.scene.geotools_analysis.shadow_study
    study.march = study.june = study.september = study.december = True
    study.start_hour, study.end_hour, study.step_hours = 12.0, 12.0, 1.0
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.year = 2026
    for offset in (14.0, -11.0, 0.0):
        sun_props.tz_mode = "OFFSET"
        sun_props.utc_offset = offset
        zone = dt.timezone(dt.timedelta(hours=offset))
        result, error = h.call_op(bpy.ops.geotools.sun_shadow_study)
        assert result == {"FINISHED"}, error
        rows = json.loads(study.result)["moments"]
        assert [row["season"] for row in rows] == names
        for row in rows:
            moment = dt.datetime.fromtimestamp(moments[row["season"]], zone)
            assert row["local"] == f"{moment.date().isoformat()}T12:00:00", (zone, row, moment)


# --- sun hours: the Cycles bake ---------------------------------------------------------------


def test_tilted_planes_match_skyfield() -> None:
    """Sun hours on tilted, vertical and overhanging planes, baked by Cycles, at 1-minute steps.

    The surface is one quad turned by its object's rotation (sun hours reads
    world-space normals). Within 2.1 min, as in the core test: half a step at
    each of up to four edges (sunrise, sunset, the surface's own plane twice).
    """
    rows = _load("sun_hours.json")["surfaces"]
    cases = [  # with the reference zone's UTC offset on that day
        ("51 N December", "east 45", 0.0),
        ("51 N March", "north 60", 0.0),
        ("51 N June", "vertical north", 1.0),
        ("27.5 S June", "north 30", 10.0),
        ("80 N June", "overhang (faces down, 120)", 0.0),
    ]
    for site, surface, offset in cases:
        ref = next(r for r in rows if r["site"] == site and r["surface"] == surface)
        _reset()
        _local_project(ref["longitude"], ref["latitude"])
        _sun_date(ref["day"], offset)
        quad = _source("Quad", [(-1, -1, 0), (1, -1, 0), (1, 1, 0), (-1, 1, 0)], [(0, 1, 2, 3)])
        # Tilt about X (the normal leans towards -Y, south), then turn it to face the aspect.
        quad.rotation_mode = "XYZ"
        quad.rotation_euler = (
            math.radians(ref["tilt"]),
            0.0,
            math.radians(180.0 - ref["aspect"]),
        )
        bpy.context.view_layer.update()
        normal = np.array(quad.matrix_world.to_3x3() @ quad.data.polygons[0].normal)
        t, a = math.radians(ref["tilt"]), math.radians(ref["aspect"])
        want = [math.sin(t) * math.sin(a), math.sin(t) * math.cos(a), math.cos(t)]
        assert np.allclose(normal, want, atol=1e-6), (normal, want)
        _sun_hours(step_minutes=1, tolerance=0.0)
        hours = float(_values(quad, "sun_hours")[0])
        assert abs(hours - ref["hours"]) * 60.0 <= 2.1, (site, surface, hours, ref["hours"])


def test_wall_shadow_edge_at_a_zone_edge() -> None:
    """A 20 m east-west wall's shadow edge on the ground: H x cos(grid azimuth) / tan(elevation).

    One 12-hour step (local day 2026-03-20 at UTC+07:00 with a 720-minute step
    has one daytime midpoint, 2026-03-19 23:00 UTC), so every face is lit for
    12 h or shaded for 0 h. The sun is in the north-east at 36 degrees; the
    wall shades the ground south of it to y* = -0.05 - H cos(Ag) / tan(e),
    with the reference elevation from Skyfield and Ag the geodesic grid
    bearing. A 1.5 degree convergence error would move that edge by 0.6 m, so
    checking every 0.25 m face away from the edge proves the direction.
    """
    _reset()
    if not _has_pyproj():
        return
    ref = _load("sun_hours.json")["wall"]
    _crs_project(ref["crs"], ref["east"], ref["north"])
    sun_props = bpy.context.scene.geotools_analysis.sun
    sun_props.year, sun_props.month, sun_props.day = 2026, 3, 20
    sun_props.tz_mode = "OFFSET"
    sun_props.utc_offset = ref["utc_offset"]
    sun_props.refraction = False
    height, half = 20.0, 0.05
    verts, faces = _grid(-5.0, 5.0, -20.05, -half, 0.25)
    ground = _source("Ground", verts, faces)
    wall_mesh = bpy.data.meshes.new("Wall")
    x0, x1, y0, y1 = -300.0, 300.0, -half, half
    box = [(x, y, z) for z in (0.0, height) for y in (y0, y1) for x in (x0, x1)]
    sides = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    wall_mesh.from_pydata(box, [], sides)
    wall = bpy.data.objects.new("Wall", wall_mesh)
    bpy.context.scene.collection.objects.link(wall)

    result = _sun_hours(step_minutes=ref["step_minutes"], tolerance=0.0)
    assert result["stats"]["steps_sun_up"] == 1, result["stats"]
    hours = _values(ground, "sun_hours")
    y = _face_centres(ground)[:, 1]
    elevation = math.radians(ref["elevation_airless"])

    def edge(bearing: float) -> float:
        return -half - height * math.cos(math.radians(bearing)) / math.tan(elevation)

    y_star = edge(ref["grid_bearing"])
    assert abs(y_star - edge(ref["azimuth"])) > 0.5  # the convergence matters here
    clear = np.abs(y - y_star) > 0.125 + 0.02  # face half-width plus SPA and float slack
    lit = y < y_star
    assert np.all(np.abs(hours[clear & lit] - 12.0) < 1e-3), hours[clear & lit].min()
    assert np.all(hours[clear & ~lit] == 0.0), hours[clear & ~lit].max()
    assert (clear & lit).sum() > 100 and (clear & ~lit).sum() > 100
