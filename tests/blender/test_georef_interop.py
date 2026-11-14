"""The scene georeference as BlenderGIS GeoScene keys (``blender.scene_georef``).

The keys are written here exactly as BlenderGIS writes them: ``scene["SRID"]`` a
string, ``scene["crs x"]``/``scene["crs y"]`` Python floats (ID property
doubles), ``latitude``/``longitude`` floats. BlenderGIS itself is not needed;
``test_blendergis_acceptance`` runs the real one when it is given.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import _geotiff as gw
import _harness as h
import bpy
import numpy as np

E0, N0 = 502_000.123456789, 6_961_000.987654321
KEYS = ("SRID", "crs x", "crs y", "latitude", "longitude")


def _available() -> bool:
    return h.CONFIG.expect == "available"


def _props_mod():
    return h.submodule("blender.properties")


def _placement_mod():
    return h.submodule("core.placement")


def _keys() -> dict[str, object]:
    scene = bpy.context.scene
    return {k: scene[k] for k in KEYS if k in scene}


def _blendergis_scene(srid: str = "EPSG:7856", x: float = E0, y: float = N0) -> None:
    """Keys as BlenderGIS's GeoScene writes them (no GeoTools settings at all)."""
    scene = bpy.context.scene
    scene["SRID"] = srid
    scene["crs x"] = x
    scene["crs y"] = y
    scene["latitude"] = -27.47
    scene["longitude"] = 153.02


def _expected_lon_lat(crs: str, e: float, n: float) -> tuple[float, float] | None:
    return h.submodule("core.geoscene").origin_lon_lat(crs, e, n)


def _grid_source() -> bpy.types.Object:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    mesh = bpy.data.meshes.new("Grid")
    co = np.array([[0, 0, 5], [10, 0, 5], [10, 10, 6], [0, 10, 6]], np.float32)
    mesh.vertices.add(4)
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(4)
    mesh.loops.foreach_set("vertex_index", np.arange(4, dtype=np.int32))
    mesh.polygons.add(1)
    mesh.polygons.foreach_set("loop_start", np.array([0], np.int32))
    mesh.update()
    obj = bpy.data.objects.new("Grid", mesh)
    collection.objects.link(obj)
    return obj


# --- a scene georeferenced by BlenderGIS ---------------------------------------------------


def test_blendergis_keys_are_the_crs_project() -> None:
    h.reset_scene()
    _blendergis_scene()
    before = _keys()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    assert _keys() == before  # read, not rewritten (lat/lon included)
    assert props.crs == "EPSG:7856"
    assert props.origin_e == repr(E0) and props.origin_n == repr(N0)
    Placement, Mode = _placement_mod().Placement, _placement_mod().PlacementMode
    placement = _props_mod().placement_from_props(props)
    assert placement == Placement(
        mode=Mode.SOURCE_CRS, source_crs="EPSG:7856", source_offset=(E0, N0, 0.0)
    ), placement
    assert placement.source_offset[0] == E0  # binary64 end to end
    assert props.georef_notice == ""
    # The panel draws the same fields; no interop warning at A = 0.
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    calls = h.draw(panel)
    drawn = h.props_drawn(calls)
    assert "crs" in drawn and "origin_e" in drawn and "origin_n" in drawn, drawn
    assert "GeoScene tools" not in h.label_text(calls)
    # BlenderGIS may store a whole number as an int ID property: still read.
    bpy.context.scene["crs x"] = 502000
    assert props.origin_e == "502000.0"
    assert _props_mod().placement_from_props(props).source_offset[0] == 502000.0


def test_blendergis_keys_publish() -> None:
    h.reset_scene()
    _grid_source()
    _blendergis_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    tmp = Path(tempfile.mkdtemp(prefix="geotools-georef-test-"))
    publish = h.submodule("blender.publish")
    try:
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        profile = props.build_profiles[props.build_profile_index]
        profile.output_path = str(tmp / "site.3tz")
        profile.profile = "FAST"
        if not _available():
            result, error = h.call_op(bpy.ops.geotools.publish)
            assert result == {"ERROR_REPORTED"} or error, (result, error)
            return
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        job = publish.active_job()
        assert job is not None and job.join(180.0)
        publish.update_from_job()
        status = bpy.context.window_manager.geotools_publish
        assert status.state == "DONE", (status.state, status.message, job.error)
        record = json.loads((tmp / "site.geotools" / "build.json").read_text())
        placement = record["placement"]
        assert placement["source_crs"] == "EPSG:7856"
        assert placement["source_offset"] == [E0, N0, 0.0], placement
        assert placement["height_offset"] == 0.0
    finally:
        publish.shutdown_job()
        shutil.rmtree(tmp, ignore_errors=True)


def test_an_srid_blendergis_cannot_parse_is_read_not_rewritten() -> None:
    h.reset_scene()
    proj = "+proj=utm +zone=56 +south +ellps=GRS80 +units=m +no_defs"
    _blendergis_scene(srid=proj)
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    assert props.crs == proj
    bpy.context.scene["SRID"] = "7856"  # a bare code, as BlenderGIS also accepts
    assert props.crs == "EPSG:7856"
    props.origin_e = "502001.0"
    assert bpy.context.scene["SRID"] == "7856"  # never rewritten by an origin edit


# --- GeoTools edits update the keys -------------------------------------------------------


def test_geotools_edits_write_the_keys_with_consistent_lat_lon() -> None:
    h.reset_scene()
    props = h.project()
    p = _props_mod()
    props.placement_mode = "SOURCE_CRS"
    assert _keys() == {}  # no CRS yet: nothing to write
    p.write_number(props, "origin_e", E0)
    p.write_number(props, "origin_n", N0)
    assert _keys() == {}  # an origin without a CRS is not a GeoScene
    props.crs = "EPSG:7856 — GDA2020 / MGA zone 56"  # a picker entry
    scene = bpy.context.scene
    assert scene["SRID"] == "EPSG:7856" and isinstance(scene["SRID"], str)
    assert scene["crs x"] == E0 and isinstance(scene["crs x"], float)
    assert scene["crs y"] == N0
    assert not props.is_property_set("source_crs")  # no second copy
    expected = _expected_lon_lat("EPSG:7856", E0, N0)
    if expected is None:  # no pyproj: no lat/lon rather than wrong ones
        assert "latitude" not in scene and "longitude" not in scene
    else:
        assert (scene["longitude"], scene["latitude"]) == expected
        assert abs(scene["latitude"] - -27.4746) < 1e-3

    p.write_number(props, "origin_e", 503_000.0)
    assert scene["crs x"] == 503_000.0
    expected = _expected_lon_lat("EPSG:7856", 503_000.0, N0)
    if expected is not None:
        assert (scene["longitude"], scene["latitude"]) == expected
    else:
        scene["latitude"], scene["longitude"] = 1.0, 2.0  # stale values a tool left
        p.write_number(props, "origin_n", N0 + 1.0)
        assert "latitude" not in scene and "longitude" not in scene  # deleted, as BlenderGIS does

    props.crs = "EPSG:28356"  # the numbers stay; lat/lon follow
    assert (scene["SRID"], scene["crs x"]) == ("EPSG:28356", 503_000.0)
    # A and the height offset stay GeoTools settings: no key for them.
    p.write_number(props, "source_offset_a", 3.0)
    assert set(_keys()) <= set(KEYS) and "crs z" not in scene


def test_a_crs_geoscene_cannot_hold_is_never_written() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    props.origin_e, props.origin_n = "502000.0", "6961000.0"
    assert bpy.context.scene["SRID"] == "EPSG:7856"
    props.crs = "EPSG:7856+5711"  # compound: BlenderGIS's parser would raise
    assert _keys() == {}
    assert props.crs == "EPSG:7856+5711"
    assert (props.origin_e, props.origin_n) == ("502000.0", "6961000.0")  # kept
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    assert "BlenderGIS cannot hold this CRS" in h.label_text(h.draw(panel))
    props.crs = "EPSG:7856"  # back: the keys return
    assert _keys()["crs x"] == 502000.0


# --- migration ----------------------------------------------------------------------------


def _save_and_reload(path: Path) -> None:
    assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True) == {"FINISHED"}
    h.reset_scene()
    assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}


def test_a_scene_from_an_earlier_geotools_is_migrated_on_load() -> None:
    h.reset_scene()
    _grid_source()
    props = h.project()
    p = _props_mod()
    props.placement_mode = "SOURCE_CRS"
    # What an earlier GeoTools saved: its own text, no keys.
    props.source_crs = "EPSG:7856"
    props.source_offset_e, props.source_offset_n = repr(E0), repr(N0)
    p.write_number(props, "source_offset_a", 12.5)
    p.write_number(props, "height_offset", 0.25)
    assert _keys() == {}
    before = p.placement_from_props(props)
    kwargs_before = before.mesh_to_tileset_kwargs()
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        _save_and_reload(Path(tmp) / "old.blend")
        props = h.project()
        scene = bpy.context.scene
        assert (scene["SRID"], scene["crs x"], scene["crs y"]) == ("EPSG:7856", E0, N0)
        assert not props.is_property_set("source_crs")
        assert p.read_number(props, "source_offset_a") == 12.5  # A kept
        after = p.placement_from_props(props)
        assert after == before, (after, before)
        assert after.mesh_to_tileset_kwargs() == kwargs_before
        notice = props.georef_notice
        assert "EPSG:7856" in notice and "GeoTools' project settings" in notice, notice
        assert "Offset A" in notice
        # A = 12.5 is not 0: the panel and Check warn about the height shift.
        panel = h.submodule("blender.panels").GEOTOOLS_PT_project
        assert "12.5 m lower" in h.label_text(h.draw(panel))
        assert ("operator", "geotools.dismiss_georef_notice", "X") in h.draw(panel)
        assert bpy.ops.geotools.dismiss_georef_notice() == {"FINISHED"}
        assert props.georef_notice == ""
        # Saved again and reloaded: nothing more to migrate, no notice.
        _save_and_reload(Path(tmp) / "again.blend")
        assert h.project().georef_notice == ""
        assert p.placement_from_props(h.project()) == before
    h.reset_scene()


def test_migration_when_keys_and_geotools_differ_the_keys_win() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = "EPSG:7856"
    props.source_offset_e, props.source_offset_n = "1.0", repr(N0)
    _blendergis_scene(srid="EPSG:28356", x=E0, y=N0)
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        _save_and_reload(Path(tmp) / "both.blend")
        props = h.project()
        assert props.crs == "EPSG:28356" and props.origin_e == repr(E0)
        notice = props.georef_notice
        assert "win" in notice and "EPSG:28356" in notice and "EPSG:7856" in notice, notice
        assert "1.0" in notice
        assert not props.is_property_set("source_offset_e")
    h.reset_scene()


def _publish_tileset(path: Path) -> tuple[dict, list]:
    """Publish the scene; return build.json's placement and the root transform."""
    publish = h.submodule("blender.publish")
    props = h.project()
    if not len(props.build_profiles):
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    profile = props.build_profiles[0]
    profile.output_path = str(path)
    profile.profile = "FAST"
    profile.overwrite = True
    assert bpy.ops.geotools.publish() == {"FINISHED"}
    job = publish.active_job()
    assert job is not None and job.join(180.0)
    publish.update_from_job()
    status = bpy.context.window_manager.geotools_publish
    assert status.state == "DONE", (status.state, status.message, job.error)
    record = json.loads((path.parent / f"{path.stem}.geotools" / "build.json").read_text())
    with zipfile.ZipFile(path) as archive:
        tileset = json.loads(archive.read("tileset.json"))
    return record["placement"], tileset["root"].get("transform")


def test_migration_leaves_the_published_placement_unchanged() -> None:
    """A scene from an earlier GeoTools publishes the same tiles once migrated."""
    if not _available():
        return
    h.reset_scene()
    _grid_source()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = "EPSG:7856"
    props.source_offset_e, props.source_offset_n = repr(E0), repr(N0)
    _props_mod().write_number(props, "source_offset_a", 3.0)
    publish = h.submodule("blender.publish")
    tmp = Path(tempfile.mkdtemp(prefix="geotools-georef-test-"))
    try:
        assert _keys() == {}
        # Saved first, so the build record can hash the .blend it came from.
        assert bpy.ops.wm.save_as_mainfile(filepath=str(tmp / "start.blend")) == {"FINISHED"}
        before = _publish_tileset(tmp / "before.3tz")
        _save_and_reload(tmp / "old.blend")
        assert bpy.context.scene["SRID"] == "EPSG:7856"
        after = _publish_tileset(tmp / "after.3tz")
        assert after == before, (after, before)
    finally:
        publish.shutdown_job()
        shutil.rmtree(tmp, ignore_errors=True)
        h.reset_scene()


# --- LOCAL and ANCHOR ----------------------------------------------------------------------


def test_local_and_anchor_write_no_keys_and_warn_about_existing_ones() -> None:
    panels = h.submodule("blender.panels")
    for mode in ("LOCAL", "ANCHOR"):
        h.reset_scene()
        props = h.project()
        props.placement_mode = mode
        props.crs = "EPSG:7856"
        props.origin_e = "502000"
        assert _keys() == {}, mode  # GeoScene cannot express these projects
        calls = h.draw(panels.GEOTOOLS_PT_project)
        assert "BlenderGIS" not in h.label_text(calls)

        h.reset_scene()
        _blendergis_scene()
        props = h.project()
        props.placement_mode = mode
        calls = h.draw(panels.GEOTOOLS_PT_project)
        text = h.label_text(calls)
        assert "BlenderGIS (GeoScene) georeference" in text and "EPSG:7856" in text, text
        assert ("operator", "geotools.use_blendergis_georef", "ORIENTATION_GLOBAL") in calls
        issues = h.submodule("blender.scene_georef").issues(bpy.context.scene)
        assert [i.code for i in issues] == ["georef-keys-ignored"]
        assert _props_mod().placement_from_props(props).mode.value == mode.lower()
        before = _keys()

        p = _props_mod()
        p.write_number(props, "source_offset_a", 7.0)  # unused here: reset by the switch
        assert bpy.ops.geotools.use_blendergis_georef() == {"FINISHED"}
        assert props.placement_mode == "SOURCE_CRS"
        assert _keys() == before
        assert p.read_number(props, "source_offset_a") == 0.0
        placement = p.placement_from_props(props)
        assert placement.source_crs == "EPSG:7856" and placement.source_offset == (E0, N0, 0.0)
    h.reset_scene()
    assert not bpy.ops.geotools.use_blendergis_georef.poll()


def test_checks_warn_about_ignored_keys_and_a_not_zero() -> None:
    h.reset_scene()
    _grid_source()
    _blendergis_scene()
    props = h.project()
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    codes = [(i.severity, i.code) for i in props.issues]
    assert ("WARNING", "georef-keys-ignored") in codes, codes
    props.placement_mode = "SOURCE_CRS"
    _props_mod().write_number(props, "source_offset_a", -4.0)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    issues = {i.code: i.message for i in props.issues}
    assert "georef-keys-ignored" not in issues
    assert "4 m higher" in issues["georef-height-shift"], issues
    _props_mod().write_number(props, "source_offset_a", 0.0)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    assert "georef-height-shift" not in {i.code for i in props.issues}


def test_new_crs_projects_start_at_a_zero() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    assert _props_mod().read_number(props, "source_offset_a") == 0.0


# --- changing the origin never silently desynchronises objects from the keys ---------------


def test_use_data_origin_adopts_an_existing_geoscene_instead_of_moving_it() -> None:
    h.reset_scene()
    _blendergis_scene(x=512_000.0, y=6_961_000.0)
    before = _keys()
    data_origin = h.submodule("blender.data_origin")
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        spec = gw.GeoTiffSpec(origin=(512_345.0, 6_961_789.0), pixel_size=(2.0, 2.0), epsg=7856)
        path = gw.write_geotiff(Path(tmp) / "far.tif", gw.dem(4, 4), spec)
        result, error = h.call_op(bpy.ops.geotools.import_geotiff, filepath=str(path))
        assert result == {"ERROR_REPORTED"}, (result, error)  # LOCAL refuses far data
        pending = data_origin.pending_offer(bpy.context)
        assert pending is not None
        assert data_origin.scene_georef_held(bpy.context.scene)  # the offer adopts it
        result, error = h.call_op(bpy.ops.geotools.use_data_origin, **pending["request"])
        assert result == {"FINISHED"}, error
        assert _keys()["crs x"] == before["crs x"] and _keys()["crs y"] == before["crs y"]
        obj = bpy.data.objects["far"]
        co = np.empty(len(obj.data.vertices) * 3, np.float32)
        obj.data.vertices.foreach_get("co", co)
        # Against the scene's origin (512000, 6961000), not the data's centre.
        assert abs(co.reshape(-1, 3)[:, 0].min() - (512_345.0 + 1.0 - 512_000.0)) < 1e-3


def test_a_failed_import_restores_the_keys_exactly() -> None:
    h.reset_scene()
    scene_georef = h.submodule("blender.scene_georef")
    props = h.project()
    _blendergis_scene()
    scene = bpy.context.scene
    scene["scale"] = 2000
    saved = scene_georef.snapshot(scene)
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:28356"
    props.origin_e = "1.0"
    scene_georef.restore(scene, saved)
    assert props.placement_mode == "LOCAL"
    assert scene["SRID"] == "EPSG:7856" and scene["crs x"] == E0
    assert scene["latitude"] == -27.47 and scene["scale"] == 2000


# --- an origin from latitude/longitude ------------------------------------------------------

LAT0, LON0 = -27.4745684, 153.0202439  # near (502000, 6961000) in EPSG:7856


def _geographic_scene(srid: str | None = "EPSG:7856") -> None:
    """Keys as a tool that sets only a geographic origin leaves them (blender_ogc3dtiles)."""
    scene = bpy.context.scene
    if srid is not None:
        scene["SRID"] = srid
    scene["latitude"] = LAT0
    scene["longitude"] = LON0


def _offers(calls: list) -> bool:
    return ("operator", "geotools.origin_from_lat_lon", "WORLD_DATA") in calls


def test_origin_from_lat_lon_sets_e_n_from_the_scene() -> None:
    h.reset_scene()
    _grid_source()
    _geographic_scene()
    props = h.project()
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    calls = h.draw(panel)
    assert not _offers(calls)  # a Local project leaves the keys alone
    assert not bpy.ops.geotools.origin_from_lat_lon.poll()
    props.placement_mode = "SOURCE_CRS"
    assert _keys() == {"SRID": "EPSG:7856", "latitude": LAT0, "longitude": LON0}  # untouched
    assert props.origin_e == "" and props.origin_n == ""
    calls = h.draw(panel)
    assert _offers(calls), calls
    assert "no projected origin" in h.label_text(calls)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    found = {i.code: i for i in props.issues}
    assert found["georef-origin-lat-lon-only"].severity == "WARNING", found
    # Checks offers the same remedy for the selected issue.
    props.issues_index = [i.code for i in props.issues].index("georef-origin-lat-lon-only")
    assert _offers(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_checks))

    result, error = h.call_op(bpy.ops.geotools.origin_from_lat_lon)
    if not _available():  # no pyproj: refused cleanly, nothing changed
        assert result == {"ERROR_REPORTED"} and "pyproj" in error, (result, error)
        assert _keys() == {"SRID": "EPSG:7856", "latitude": LAT0, "longitude": LON0}
        return
    assert result == {"FINISHED"}, error
    expected = h.submodule("core.geoscene").lon_lat_to_origin("EPSG:7856", LON0, LAT0)
    scene = bpy.context.scene
    assert (scene["crs x"], scene["crs y"]) == expected  # doubles, unrounded
    assert abs(scene["crs x"] - 502_000.0) < 1.0 and abs(scene["crs y"] - 6_961_000.0) < 1.0
    assert props.origin_e == repr(expected[0])
    assert (scene["longitude"], scene["latitude"]) == _expected_lon_lat("EPSG:7856", *expected)
    assert abs(scene["latitude"] - LAT0) < 1e-10 and abs(scene["longitude"] - LON0) < 1e-10
    placement = _props_mod().placement_from_props(props)
    assert placement.source_offset[:2] == expected
    assert not _offers(h.draw(panel))  # done: nothing left to offer
    # Nothing to do now that they agree, but running it again changes nothing either.
    assert bpy.ops.geotools.origin_from_lat_lon() == {"FINISHED"}
    assert (scene["crs x"], scene["crs y"]) == expected


def test_choosing_a_crs_keeps_a_geographic_only_origin() -> None:
    h.reset_scene()
    _geographic_scene(srid=None)
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    # Earlier GeoTools wrote crs x/y = 0 here and overwrote latitude/longitude from them.
    assert _keys() == {"SRID": "EPSG:7856", "latitude": LAT0, "longitude": LON0}
    assert h.submodule("blender.scene_georef").lat_lon_origin(bpy.context.scene).missing
    assert _offers(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project))
    if _available():
        assert bpy.ops.geotools.origin_from_lat_lon() == {"FINISHED"}
        assert abs(bpy.context.scene["crs x"] - 502_000.0) < 1.0


def test_lat_lon_that_disagree_with_e_n_are_reported_not_changed() -> None:
    h.reset_scene()
    _grid_source()
    _blendergis_scene(x=502_000.0, y=6_961_000.0)
    scene = bpy.context.scene
    scene["latitude"], scene["longitude"] = LAT0 + 1e-3, LON0  # about 111 m north
    before = _keys()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    calls = h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project)
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    codes = {i.code for i in props.issues}
    assert _keys() == before  # drawing and checking change nothing
    if not _available():  # without pyproj GeoTools cannot tell how far apart they are
        assert not _offers(calls) and "georef-origin-lat-lon-differ" not in codes
        result, error = h.call_op(bpy.ops.geotools.origin_from_lat_lon)
        assert result == {"ERROR_REPORTED"} and "pyproj" in error, (result, error)
        assert _keys() == before
        return
    assert _offers(calls), calls
    text = h.label_text(calls)
    assert "111" in text and " m from its projected origin" in text, text
    assert "georef-origin-lat-lon-differ" in codes
    # The user chooses: E/N from latitude/longitude.
    assert bpy.ops.geotools.origin_from_lat_lon() == {"FINISHED"}
    assert abs(scene["crs y"] - (6_961_000.0 + 111.0)) < 2.0, scene["crs y"]
    assert abs(scene["latitude"] - (LAT0 + 1e-3)) < 1e-10
    assert not _offers(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project))


def test_origin_from_lat_lon_leaves_local_and_anchor_keys_alone() -> None:
    for mode in ("LOCAL", "ANCHOR"):
        h.reset_scene()
        _geographic_scene()
        props = h.project()
        props.placement_mode = mode
        before = _keys()
        assert not _offers(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project))
        assert not bpy.ops.geotools.origin_from_lat_lon.poll()
        assert "georef-origin-lat-lon-only" not in {
            i.code for i in h.submodule("blender.scene_georef").issues(bpy.context.scene)
        }
        assert _keys() == before
    h.reset_scene()
