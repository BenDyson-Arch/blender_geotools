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
    props.source_crs = "EPSG:32760"
    # Typed by a user as text...
    props.source_offset_n = "6961000.123456"
    # ...and written by code through the helper.
    p.write_number(props, "source_offset_e", EASTING)
    p.write_number(props, "source_offset_a", -12.000000001)
    p.write_number(props, "height_offset", 0.1 + 0.2)

    assert props.source_offset_n == "6961000.123456"
    assert p.read_number(props, "source_offset_n") == NORTHING
    assert p.read_number(props, "source_offset_e") == EASTING
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
    props.source_crs = "EPSG:32760"
    p.write_number(props, "source_offset_n", NORTHING)
    p.write_number(props, "source_offset_e", EASTING)
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
    props.source_crs = "EPSG:32760"
    for bad in ("", "abc", "nan", "inf", "1,5"):
        props.source_offset_e = bad
        try:
            p.placement_from_props(props)
        except p.FieldError as ex:
            assert ex.field.attr == "source_offset_e"
            assert "Offset E" in str(ex)
        else:
            raise AssertionError(f"{bad!r} was accepted")
        issues = p.field_issues(props)
        assert [i.code for i in issues] == ["invalid-number"], issues
    try:
        p.write_number(props, "source_offset_e", float("nan"))
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
    source = {f.attr for f in p.SOURCE_CRS_FIELDS} | {"source_crs"}

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
    assert ("Set the source CRS, e.g. EPSG:32756", "ERROR") in list(h.labels(calls))


def test_project_panel_flags_invalid_numbers() -> None:
    h.reset_scene()
    props = h.project()
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = "EPSG:32760"
    props.source_offset_n = "6961000.5x"
    alerts: list[str] = []
    h.draw(panel, alerts)
    assert alerts == ["source_offset_n"], alerts
