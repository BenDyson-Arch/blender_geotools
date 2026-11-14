"""Scene project properties: binary64 precision and the Placement bridge."""

from __future__ import annotations

import tempfile
from pathlib import Path

import _harness as h
import bpy
import numpy as np

NORTHING = 6961000.123456
EASTING = 512345.987654321
LONGITUDE = 174.123456789012
LATITUDE = -36.987654321098


def _props():
    return h.submodule("blender.properties")


def _placement():
    return h.submodule("core.placement")


def test_scene_has_project() -> None:
    h.reset_scene()
    props = h.project()
    assert isinstance(props, h.submodule("blender.properties").GeoToolsProject)
    assert props.placement_mode == "LOCAL"
    assert _props().placement_from_props(props) == _placement().Placement()


def test_float_property_would_lose_the_value() -> None:
    # Why the numbers are text: single precision cannot hold them.
    assert float(np.float32(NORTHING)) != NORTHING
    assert abs(float(np.float32(NORTHING)) - NORTHING) > 0.1


def test_offsets_round_trip_exactly() -> None:
    h.reset_scene()
    p = _props()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:32760"
    # Typed by a user as text...
    props.origin_n = "6961000.123456"
    # ...and written by code through the helper.
    p.write_number(props, "origin_e", EASTING)
    p.write_number(props, "source_offset_a", -12.000000001)
    p.write_number(props, "height_offset", 0.1 + 0.2)

    assert props.origin_n == "6961000.123456"
    assert p.read_number(props, "origin_n") == NORTHING
    assert p.read_number(props, "origin_e") == EASTING
    assert p.read_number(props, "height_offset") == 0.1 + 0.2

    placement = p.placement_from_props(props)
    Placement, Mode = _placement().Placement, _placement().PlacementMode
    assert placement == Placement(
        mode=Mode.SOURCE_CRS,
        source_crs="EPSG:32760",
        source_offset=(EASTING, NORTHING, -12.000000001),
        height_offset=0.1 + 0.2,
    ), placement
    assert placement.source_offset[1] == 6961000.123456
    kwargs = placement.mesh_to_tileset_kwargs()
    assert kwargs["source_offset"] == (EASTING, NORTHING, -12.000000001), kwargs
    assert kwargs["source_axes"] == "y-up"


def test_anchor_round_trips_exactly() -> None:
    h.reset_scene()
    p = _props()
    props = h.project()
    props.placement_mode = "ANCHOR"
    p.write_number(props, "anchor_lon", LONGITUDE)
    p.write_number(props, "anchor_lat", LATITUDE)
    p.write_number(props, "anchor_height", 35.123456789)
    p.write_number(props, "heading", 12.5)
    p.write_number(props, "scene_offset_x", 1000.000001)
    p.write_number(props, "scene_offset_y", -2000.000002)
    p.write_number(props, "scene_offset_z", 3.25)
    placement = p.placement_from_props(props)
    assert placement.mode == _placement().PlacementMode.ANCHOR
    assert placement.anchor == (LONGITUDE, LATITUDE, 35.123456789)
    assert placement.heading_degrees == 12.5
    assert placement.scene_offset == (1000.000001, -2000.000002, 3.25)
    # Fields of the other mode are not read.
    assert placement.source_offset == (0.0, 0.0, 0.0)


def test_values_survive_save_and_reload() -> None:
    h.reset_scene()
    p = _props()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:32760"
    p.write_number(props, "origin_n", NORTHING)
    p.write_number(props, "origin_e", EASTING)
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        path = Path(tmp) / "precision.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True) == {"FINISHED"}
        h.reset_scene()
        assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}
        props = h.project()
        placement = p.placement_from_props(props)
        assert placement.source_offset[:2] == (EASTING, NORTHING), placement
        assert placement.source_crs == "EPSG:32760"
    h.reset_scene()


def test_invalid_number_is_reported() -> None:
    h.reset_scene()
    p = _props()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:32760"
    props.origin_e = "502000.5"
    scene = bpy.context.scene
    # The offset is the GeoScene key 'crs x' (a double): text that is not a finite
    # number is refused and the field keeps its value.
    for bad in ("", "abc", "nan", "inf", "1,5"):
        props.origin_e = bad
        assert props.origin_e == "502000.5" and scene["crs x"] == 502000.5
    assert p.field_issues(props) == []
    # A project without a CRS (or with one GeoScene cannot hold) keeps GeoTools' own
    # text, which Check reports when it is not a number.
    props.crs = ""
    assert "SRID" not in scene and "crs x" not in scene
    assert props.origin_e == "502000.5"
    for bad in ("", "abc", "nan", "inf", "1,5"):
        props.origin_e = bad
        try:
            p.placement_from_props(props)
        except p.FieldError as ex:
            assert ex.field.attr == "origin_e"
            assert "Offset E" in str(ex)
        else:
            raise AssertionError(f"{bad!r} was accepted")
        issues = p.field_issues(props)
        assert [i.code for i in issues] == ["invalid-number"], issues
    try:
        p.write_number(props, "origin_e", float("nan"))
    except ValueError:
        pass
    else:
        raise AssertionError("write_number accepted nan")
    # An invalid field of the inactive mode does not matter.
    props.placement_mode = "ANCHOR"
    assert p.field_issues(props) == []
    p.placement_from_props(props)


def test_project_panel_shows_only_active_mode_fields() -> None:
    h.reset_scene()
    p = _props()
    props = h.project()
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    anchor = {f.attr for f in p.ANCHOR_FIELDS}
    source = {f.attr for f in p.SOURCE_CRS_FIELDS} | {"crs"}

    drawn = set(h.props_drawn(h.draw(panel)))
    assert "placement_mode" in drawn
    assert not drawn & (anchor | source), drawn

    props.placement_mode = "ANCHOR"
    drawn = set(h.props_drawn(h.draw(panel)))
    assert anchor <= drawn and not drawn & source, drawn

    props.placement_mode = "SOURCE_CRS"
    calls = h.draw(panel)
    drawn = set(h.props_drawn(calls))
    assert source <= drawn and not drawn & anchor, drawn
    assert ("operator", "geotools.load_offset_file", "FILEBROWSER") in calls
    assert "crs_near_site" in h.props_drawn(calls)
    if _available():
        assert ("Search for an EPSG code, or type WKT or PROJ", "ERROR") in list(h.labels(calls))
    else:  # the no-wheels build: the CRS row says rusty-tiles is missing
        text = h.label_text(calls)
        assert "No EPSG list or CRS check" in text and "rusty_tiles is not installed" in text
    assert h.draw_header(panel) == "CANCEL"  # no CRS yet

    # An EPSG code shows its name and rusty-tiles' verdict; CRS Details has the rest.
    details = h.submodule("blender.panels").GEOTOOLS_PT_project_crs
    assert not details.poll(bpy.context)
    props.crs = "EPSG:7856"
    calls = h.draw(panel)
    assert "crs" in h.props_drawn(calls)
    assert h.draw_header(panel) == "CHECKMARK"
    assert details.poll(bpy.context)
    details_text = h.label_text(h.draw(details))
    if _available():
        text = h.label_text(calls)
        assert "GDA2020 / MGA zone 56" in text, text
        assert "GDA2020 to WGS 84 (2) (EPSG:8450), EPSG-stated accuracy 3 m" in text, text
        assert any(
            t.startswith("EPSG:7856: GDA2020") and i == "CHECKMARK" for t, i in h.labels(calls)
        )
        assert "Area of use" in details_text and "GDAL-verified" in details_text, details_text
        assert "towgs84" in details_text  # the definition rusty-tiles uses

        # A CRS rusty-tiles refuses: the refusal, in red, before any bake.
        props.crs = "EPSG:27700"
        alerts: list[str] = []
        calls = h.draw(panel, alerts)
        assert "crs" in alerts, alerts
        assert "EPSG:27700 is not in the standard build" in h.label_text(calls)
        assert "native-geospatial" not in h.label_text(calls)
        assert h.draw_header(panel) == "CANCEL"

    props.placement_mode = "LOCAL"
    assert h.draw_header(panel) == "CHECKMARK"


def test_project_panel_flags_invalid_numbers() -> None:
    h.reset_scene()
    props = h.project()
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    props.placement_mode = "SOURCE_CRS"
    props.origin_n = "6961000.5x"  # no CRS yet: GeoTools' own text, kept as typed
    alerts: list[str] = []
    h.draw(panel, alerts)
    assert alerts == ["crs", "crs_near_site", "origin_n"], alerts  # the CRS row too: no CRS
    assert h.draw_header(panel) == "CANCEL"


def _available() -> bool:
    return h.CONFIG.expect == "available"


def _preset_mga56() -> str:
    codes = h.submodule("core.crs_migration").PRESET_CODES
    return next(text for text, code in codes.items() if code == "EPSG:7856")


def test_choosing_an_entry_stores_the_plain_code() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856 — GDA2020 / MGA zone 56"
    assert props.crs == "EPSG:7856"
    props.crs = " epsg:2193 "
    assert props.crs == "EPSG:2193"
    proj = "+proj=tmerc +lon_0=151 +ellps=GRS80 +towgs84=0,0,0 +units=m"
    props.crs = proj
    assert props.crs == proj  # free text is kept as typed
    assert _props().placement_from_props(props).source_crs == proj


def test_search_lists_codes_near_the_site() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    picker = h.submodule("blender.crs_picker")
    found = picker.search_crs(props, bpy.context, "mga 56")
    if not _available():
        assert found == []  # nothing to list without rusty-tiles
        return
    labels = [label for label, _area in found]
    assert "EPSG:7856 — GDA2020 / MGA zone 56" in labels, labels[:10]
    assert "EPSG:28356 — GDA94 / MGA zone 56" in labels, labels[:10]
    assert "EPSG:7855 — GDA2020 / MGA zone 55" not in labels
    assert all(area for _label, area in found)  # the area of use is the hint

    # No location yet: every code, in code order.
    everything = [label for label, _area in picker.search_crs(props, bpy.context, "")]
    assert len(everything) > 3000 and everything[0].startswith("EPSG:2")
    # In Brisbane (a verified placement): only codes whose area of use holds it.
    props.probe_status = "OK"
    props.probe_lon, props.probe_lat = "153.03", "-27.47"
    near = [label for label, _area in picker.search_crs(props, bpy.context, "")]
    assert "EPSG:7856 — GDA2020 / MGA zone 56" in near
    assert not any("MGA zone 50" in label for label in near)
    assert len(near) < len(everything)
    props.crs_near_site = False
    near_first = [label for label, _area in picker.search_crs(props, bpy.context, "")]
    assert len(near_first) == len(everything) and near_first[: len(near)] == near
    # The field's own value lists everything, not just itself.
    props.crs = "EPSG:7856"
    assert len(picker.search_crs(props, bpy.context, "EPSG:7856")) == len(everything)


def test_a_saved_preset_is_migrated_to_its_epsg_code() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    preset = _preset_mga56()
    props.source_crs = preset  # GeoTools' own text, as an earlier version saved it
    picker = h.submodule("blender.crs_picker")
    placement = _props().placement_from_props(props)
    if not _available():  # rusty-tiles can't confirm the definition: kept as saved
        assert placement.source_crs == preset
        assert picker.migrate_scene(bpy.context.scene) is None
        assert props.crs == preset
        return
    assert placement.source_crs == "EPSG:7856"  # read as the code before it is written
    assert picker.migrate_scene(bpy.context.scene) == "EPSG:7856"
    assert props.crs == "EPSG:7856" and props.crs_migrated_from == preset
    assert picker.migrate_scene(bpy.context.scene) is None  # done once
    # A SRID key is never rewritten by the preset migration, whatever it holds.
    bpy.context.scene["SRID"] = preset
    assert picker.migrate_scene(bpy.context.scene) is None
    assert bpy.context.scene["SRID"] == preset
    del bpy.context.scene["SRID"]
    details = h.submodule("blender.panels").GEOTOOLS_PT_project_crs
    assert "Migrated from an old GeoTools CRS preset" in h.label_text(h.draw(details))
    props.crs = "EPSG:7855"  # another CRS: the note goes
    assert props.crs_migrated_from == ""


def test_a_saved_preset_is_migrated_on_file_load() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = _preset_mga56()  # GeoTools' own text, as an earlier version saved it
    expected = "EPSG:7856" if _available() else _preset_mga56()
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        path = Path(tmp) / "preset.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True) == {"FINISHED"}
        h.reset_scene()
        assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}
        assert h.project().crs == expected
        # Migrated first, then moved into the GeoScene keys (a PROJ string BlenderGIS
        # reads, when rusty-tiles cannot confirm the code).
        assert bpy.context.scene["SRID"] == expected
    h.reset_scene()


def test_run_checks_reports_the_crs_verdict() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.origin_e, props.origin_n = "502000", "6961000"
    checks = h.submodule("blender.checks")
    placement = h.submodule("core.placement")
    ok = placement.Placement(mode="source_crs", source_crs="EPSG:7856")
    refused = placement.Placement(mode="source_crs", source_crs="EPSG:27700")
    if not _available():
        assert checks.crs_issues(ok, []) == [] and checks.crs_issues(refused, []) == []
        return
    (note,) = checks.crs_issues(ok, [])
    assert note.code == "crs-operation" and "accuracy 3 m" in note.message
    (error,) = checks.crs_issues(refused, [])
    assert error.code == "crs-refused" and error.severity == "error"
