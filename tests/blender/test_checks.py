"""Run Checks, Verify Placement and the build profiles."""

from __future__ import annotations

import _harness as h
import bpy
import numpy as np


def _source_with_cube(location: tuple[float, float, float] = (0.0, 0.0, 0.0)) -> bpy.types.Object:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    mesh = bpy.data.meshes.new("Cube")
    corners = np.array(
        [[x, y, z] for x in (-5, 5) for y in (-5, 5) for z in (0, 3)], dtype=np.float32
    )
    mesh.vertices.add(8)
    mesh.vertices.foreach_set("co", corners.ravel())
    mesh.loops.add(3)
    mesh.loops.foreach_set("vertex_index", np.array([0, 2, 6], dtype=np.int32))
    mesh.polygons.add(1)
    mesh.polygons.foreach_set("loop_start", np.array([0], dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new("Cube", mesh)
    obj.location = location
    collection.objects.link(obj)
    return obj


def _issue_codes() -> list[tuple[str, str]]:
    return [(i.severity, i.code) for i in h.project().issues]


# --- run checks --------------------------------------------------------------------


def test_run_checks_needs_a_source() -> None:
    h.reset_scene()
    assert not bpy.ops.geotools.run_checks.poll()
    assert not bpy.ops.geotools.verify_placement.poll()
    assert not bpy.ops.geotools.refresh_source_summary.poll()


def test_run_checks_clean_scene() -> None:
    h.reset_scene()
    _source_with_cube()
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    props = h.project()
    assert props.checks_run
    assert not [c for c in _issue_codes() if c[0] == "ERROR"], _issue_codes()
    labels = list(h.labels(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_checks)))
    if not len(props.issues):
        assert ("No issues found", "CHECKMARK") in labels, labels


def test_run_checks_flags_shape_keys() -> None:
    h.reset_scene()
    obj = _source_with_cube()
    obj.shape_key_add(name="Basis")
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    codes = _issue_codes()
    assert ("ERROR", "shape-keys") in codes, codes
    # Errors come first.
    assert codes[0][0] == "ERROR", codes
    item = next(i for i in h.project().issues if i.code == "shape-keys")
    assert item.subject == "Cube" and "shape keys" in item.message

    panels = h.submodule("blender.panels")
    calls = h.draw(panels.GEOTOOLS_PT_checks)
    assert ("template_list", "GEOTOOLS_UL_issues", "NONE") in calls, calls
    assert h.draw_header(panels.GEOTOOLS_PT_checks) == "CANCEL"
    row = h.draw_list_item(h.submodule("blender.checks").GEOTOOLS_UL_issues, h.project(), item)
    assert row == [("label", item.message, "CANCEL")], row


def test_run_checks_includes_placement() -> None:
    h.reset_scene()
    _source_with_cube()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"  # with no CRS
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    assert ("ERROR", "crs-missing") in _issue_codes(), _issue_codes()
    # With no CRS yet the offset is GeoTools' own text, so a typo is kept and reported
    # (a GeoScene key would refuse it).
    props.origin_e = "not a number"
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    codes = _issue_codes()
    assert ("ERROR", "invalid-number") in codes, codes
    assert ("INFO", "placement-not-checked") in codes, codes


def test_run_checks_flags_coordinate_range() -> None:
    h.reset_scene()
    _source_with_cube(location=(500000.0, 6961000.0, 0.0))
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    assert ("ERROR", "coordinate-range") in _issue_codes(), _issue_codes()


# --- verify placement ---------------------------------------------------------------


def _verify() -> str | None:
    _result, error = h.call_op(bpy.ops.geotools.verify_placement)
    return error


def test_verify_placement_needs_rusty_tiles() -> None:
    h.reset_scene()
    _source_with_cube()
    available = h.CONFIG.expect == "available"
    assert bpy.ops.geotools.verify_placement.poll() == available
    calls = h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project)
    assert ("operator", "geotools.verify_placement", "WORLD") in calls


def _direct_probe():
    """What core.probe says for the scene's placement and source bounds."""
    p = h.submodule("blender.properties")
    summarise = h.submodule("blender.summarise")
    probe = h.submodule("core.probe")
    summary = summarise.summarise_collection(h.project().source_collection, bpy.context)
    lo, hi = summary.bounds()
    return probe.probe_placement(p.placement_from_props(h.project()), lo, hi)


def test_verify_placement_matches_core_probe() -> None:
    if h.CONFIG.expect != "available":
        return
    h.reset_scene()
    _source_with_cube()
    p = h.submodule("blender.properties")
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:32756"
    p.write_number(props, "origin_e", 500000.0)
    p.write_number(props, "origin_n", 6961000.123456)
    p.write_number(props, "source_offset_a", 10.0)
    assert _verify() is None
    assert props.probe_status == "OK", props.probe_error
    direct = _direct_probe()
    assert direct.ok, direct
    got = (float(props.probe_lon), float(props.probe_lat), float(props.probe_height))
    assert got == direct.origin_lon_lat_height, (got, direct)
    assert props.probe_operation == (direct.operation or "")
    print(f"    origin: {got}, operation {props.probe_operation!r}")
    # Somewhere near Brisbane (UTM 56S), as a sanity check.
    assert 152.0 < got[0] < 154.0 and -28.0 < got[1] < -27.0, got
    labels = [t for t, _i in h.labels(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project))]
    assert f"{got[0]:.7f}°" in labels, labels
    assert "Placement changed; verify again" not in labels
    p.write_number(props, "origin_n", 6961001.0)
    labels = [t for t, _i in h.labels(h.draw(h.submodule("blender.panels").GEOTOOLS_PT_project))]
    assert "Placement changed; verify again" in labels, labels


def test_verify_placement_anchor_matches_core_probe() -> None:
    if h.CONFIG.expect != "available":
        return
    h.reset_scene()
    _source_with_cube()
    p = h.submodule("blender.properties")
    props = h.project()
    props.placement_mode = "ANCHOR"
    p.write_number(props, "anchor_lon", 174.763336)
    p.write_number(props, "anchor_lat", -36.848461)
    p.write_number(props, "anchor_height", 30.0)
    p.write_number(props, "heading", 30.0)
    assert _verify() is None
    assert props.probe_status == "OK", props.probe_error
    direct = _direct_probe()
    got = (float(props.probe_lon), float(props.probe_lat), float(props.probe_height))
    assert got == direct.origin_lon_lat_height, (got, direct)


def test_verify_placement_reports_unsupported_crs() -> None:
    if h.CONFIG.expect != "available":
        return
    h.reset_scene()
    _source_with_cube()
    p = h.submodule("blender.properties")
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:27700"  # British National Grid: needs a grid, refused
    p.write_number(props, "origin_e", 530000.0)
    p.write_number(props, "origin_n", 180000.0)
    error = _verify()
    assert error and "Placement failed" in error, error
    assert props.probe_status == "FAILED"
    assert props.probe_error and props.probe_lon == "", props.probe_error
    direct = _direct_probe()
    assert not direct.ok and direct.error == props.probe_error, (direct, props.probe_error)
    panel = h.submodule("blender.panels").GEOTOOLS_PT_project
    calls = h.draw(panel)
    assert any(icon == "ERROR" for _t, icon in h.labels(calls)), calls
    assert h.draw_header(panel) == "CANCEL"  # the probe failed for this placement
    print(f"    EPSG:27700 error: {props.probe_error}")


def test_verify_placement_without_mesh() -> None:
    if h.CONFIG.expect != "available":
        return
    h.reset_scene()
    collection = bpy.data.collections.new("Empty Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    error = _verify()
    assert error and "no meshes" in error, error
    assert h.project().probe_status == "FAILED"


# --- build profiles -------------------------------------------------------------------


def test_build_profiles_add_remove_move() -> None:
    h.reset_scene()
    props = h.project()
    assert not bpy.ops.geotools.build_profile_remove.poll()
    for _ in range(3):
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    names = [p.name for p in props.build_profiles]
    assert names == ["Profile 1", "Profile 2", "Profile 3"], names
    assert props.build_profile_index == 2
    first = props.build_profiles[0]
    assert (first.profile, first.jobs, first.memory_budget_gib, first.output_path) == (
        "BALANCED",
        0,
        0.0,
        "",
    )
    props.build_profiles[2].profile = "LOSSLESS"
    props.build_profiles[2].jobs = 8
    props.build_profiles[2].memory_budget_gib = 12.5

    assert bpy.ops.geotools.build_profile_move(direction="UP") == {"FINISHED"}
    assert [p.name for p in props.build_profiles] == ["Profile 1", "Profile 3", "Profile 2"]
    assert props.build_profile_index == 1
    moved = props.build_profiles[1]
    assert (moved.profile, moved.jobs, moved.memory_budget_gib) == ("LOSSLESS", 8, 12.5)

    props.build_profile_index = 0
    assert bpy.ops.geotools.build_profile_move(direction="UP") == {"CANCELLED"}

    props.build_profile_index = 1
    assert bpy.ops.geotools.build_profile_remove() == {"FINISHED"}
    assert [p.name for p in props.build_profiles] == ["Profile 1", "Profile 2"]
    assert props.build_profile_index == 1
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    assert props.build_profiles[-1].name == "Profile 3"

    panels = h.submodule("blender.panels")
    calls = h.draw(panels.GEOTOOLS_PT_publish)
    assert ("template_list", "GEOTOOLS_UL_build_profiles", "NONE") in calls
    assert {"output_path", "variant_layer", "overwrite", "validate"} <= set(h.props_drawn(calls))
    assert panels.GEOTOOLS_PT_publish_settings.poll(bpy.context)
    calls = h.draw(panels.GEOTOOLS_PT_publish_settings)
    assert {"profile", "jobs", "memory_budget_gib"} <= set(h.props_drawn(calls))
    assert any(t.startswith("Auto memory: ") for t, _i in h.labels(calls)), calls
    ul = h.submodule("blender.profiles").GEOTOOLS_UL_build_profiles
    row = h.draw_list_item(ul, props, props.build_profiles[0])
    assert ("label", "Balanced", "NONE") in row, row

    while len(props.build_profiles):
        assert bpy.ops.geotools.build_profile_remove() == {"FINISHED"}
    assert props.build_profile_index == 0
    assert not bpy.ops.geotools.build_profile_remove.poll()


# --- panels -----------------------------------------------------------------------


def test_panels_order_and_draw() -> None:
    h.reset_scene()
    panels = h.submodule("blender.panels")
    top = [c for c in panels.classes if not getattr(c, "bl_parent_id", "")]
    assert [c.bl_label for c in top] == [
        "Project",
        "Source",
        "Linked Data",
        "Layers",
        "Textures",
        "Checks",
        "Publish",
        "Inspect Output",
        "Analysis",
        "About",
    ], top
    children: dict[str, list[str]] = {}
    for c in panels.classes:
        if getattr(c, "bl_parent_id", ""):
            children.setdefault(c.bl_parent_id, []).append(c.bl_label)
    assert children == {
        "GEOTOOLS_PT_project": ["CRS Details"],
        "GEOTOOLS_PT_layers": ["Bake", "Display", "Details"],
        "GEOTOOLS_PT_textures": ["Paint", "Cleanup", "Balance"],
        "GEOTOOLS_PT_checks": ["Images"],
        "GEOTOOLS_PT_publish": ["Build Settings", "Last Build"],
        "GEOTOOLS_PT_inspect": ["Trace to Source", "Quality"],
        "GEOTOOLS_PT_analysis": [
            "Sun",
            "Sun Hours",
            "Shadow Study",
            "Archaeoastronomy",
            "Coordinates",
            "Presets",
            "Hydrology",
            "Contours and Sections",
        ],
    }, children
    for cls in panels.classes:
        assert cls.is_registered, cls
        assert (cls.bl_space_type, cls.bl_region_type, cls.bl_category) == (
            "VIEW_3D",
            "UI",
            "GeoTools",
        )
        h.draw(cls)
        if hasattr(cls, "draw_header"):
            h.draw_header(cls)
    # An empty project: what is left to do shows in the headers.
    assert h.draw_header(panels.GEOTOOLS_PT_source) == "RADIOBUT_OFF"
    assert h.draw_header(panels.GEOTOOLS_PT_checks) == "RADIOBUT_OFF"
    assert h.draw_header(panels.GEOTOOLS_PT_publish) == "RADIOBUT_OFF"
    assert h.draw_header(panels.GEOTOOLS_PT_layers) is None  # layers are optional
    labels = h.label_text(h.draw(panels.GEOTOOLS_PT_source))
    assert "Choose a source collection" in labels, labels
