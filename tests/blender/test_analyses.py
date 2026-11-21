"""The Analyses list: one result per mesh, listed for the active mesh like its modifiers.

Adding from the search field makes one result per selected mesh, each with
its own inputs; the list and the panels below it show the active mesh's
results; tool and Fast Bake results run on their own mesh (or their Surface)
and say when they are out of date; plain layers get a row on each mesh that
carries them; removing a result takes its modifier, values and objects with
it; and a file saved before the list (built here with the operators develop
used, then saved and opened again) has its analyses listed.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import _harness as h
import bpy
import numpy as np


def _an():
    return h.submodule("blender.analyses")


def _ui():
    return h.submodule("blender.analyses_ui")


def _ap():
    return h.submodule("blender.analysis_presets")


def _props():
    return bpy.context.scene.geotools_analyses


def _reset() -> None:
    h.reset_scene()
    props = _props()
    props.items.clear()
    props.index = 0
    props.error = props.note = ""
    props.version = _an().MIGRATED
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    collection = bpy.data.collections.new("Site")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection


def _bowl(name: str = "Bowl", n: int = 12, x: float = 0.0) -> bpy.types.Object:
    """A small quad grid with a dip in the middle, in the source collection."""
    axis = np.linspace(-10.0, 10.0, n + 1)
    xs, ys = np.meshgrid(axis, axis)
    zs = 0.02 * (xs**2 + ys**2) + 0.1 * xs
    co = np.stack([xs.ravel() + x, ys.ravel(), zs.ravel()], axis=1)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.astype(np.float32).ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    h.project().source_collection.objects.link(obj)
    return obj


def _only(*objects: bpy.types.Object) -> None:
    """Select exactly ``objects``, the first active (a click in the viewport)."""
    bpy.context.view_layer.update()
    for obj in bpy.context.view_layer.objects:
        if obj is not None:
            obj.select_set(obj in objects)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


def _add(text: str) -> list:
    """Choose ``text`` in the Add Analysis field; the results it added."""
    props = _props()
    before = len(props.items)
    props.add_search = text
    assert props.add_search == ""
    assert not props.error, props.error
    return list(props.items)[before:]


def _rows() -> list[str]:
    """The names the list shows for the active mesh (through the UIList's filter)."""
    props = _props()
    fake = type("L", (), {"filter_name": "", "bitflag_filter_item": 1 << 30})()
    flags, _order = _an().GEOTOOLS_UL_analyses.filter_items(fake, bpy.context, props, "items")
    return [item.name for item, flag in zip(props.items, flags, strict=True) if flag]


def _value(modifier, name: str):
    return _ap().get_input(modifier, name)


# --- the picker ------------------------------------------------------------------------


def test_the_picker_lists_presets_fast_bakes_tools_and_layers() -> None:
    an = _an()
    catalog = h.submodule("analysis.presets")
    keys = {e.key for e in an.ENTRIES}
    assert {f"preset:{p.id}" for p in catalog.PRESETS} <= keys
    for key in (
        "fast:sky_visibility",
        "fast:sky_view_cosine",
        "fast:openness_3d",
        "fast:sky_view_factor",
        "fast:viewshed_cumulative",
        "tool:HYDROLOGY",
        "tool:SUN_HOURS",
        "tool:LEAST_COST",
        "tool:CONTOURS",
        "tool:SECTIONS",
        "layer:ATTRIBUTE",
        "layer:TEXTURE",
    ):
        assert key in keys, key
    texts = [e.text for e in an.search_entries("sky")]
    assert "Relief and Sky > Sky Visibility" in texts, texts
    assert next(e.key for e in an.search_entries("water hydro")) == "tool:HYDROLOGY"
    assert not an.search_entries("no such analysis")
    assert len({e.text for e in an.ENTRIES}) == len(an.ENTRIES)  # the field stores the text
    groups = {e.key: e.group for e in an.ENTRIES}
    assert groups["preset:flow_path"] == "Water"
    assert groups["preset:least_cost_path"] == "Movement"
    order = [an.GROUP_ORDER.index(e.group) for e in an.ENTRIES]
    assert order == sorted(order)


def test_the_classic_panels_are_gone() -> None:
    for name in (
        "GEOTOOLS_PT_layers",
        "GEOTOOLS_PT_analysis_presets",
        "GEOTOOLS_PT_analysis_hydrology",
        "GEOTOOLS_PT_analysis_sun_hours",
        "GEOTOOLS_MT_analysis_presets",
        "GEOTOOLS_UL_layers",
    ):
        assert not hasattr(bpy.types, name), name
    prefs = h.preferences_class()
    assert "use_classic_analysis_panels" not in prefs.bl_rna.properties
    assert bpy.types.GEOTOOLS_PT_analysis.bl_label == "Tools"


# --- one result per mesh ---------------------------------------------------------------


def test_each_selected_mesh_gets_its_own_result() -> None:
    _reset()
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills, other)
    added = _add("Terrain > Aspect")
    assert [(i.object.name, i.layer) for i in added] == [("hills", "aspect"), ("other", "aspect")]
    first, second = (_an().item_modifier(i) for i in added)
    assert first is not None and second is not None and first != second
    _ap().set_input(first, "Flat Slope", 2.5)  # an edit on hills' result
    assert _value(second, "Flat Slope") != 2.5  # does not touch other's
    assert _props().items[_props().index].object == hills  # the active mesh's row

    labels = h.label_text(h.draw(_ui().GEOTOOLS_PT_analyses_inputs))
    assert "Applies to" not in labels and "Edits apply" not in labels, labels
    assert "value" in h.props_drawn(h.draw(_ui().GEOTOOLS_PT_analyses_inputs))

    # Adding it again on hills makes a second result with a layer of its own.
    _only(hills)
    (again,) = _add("Terrain > Aspect")
    assert again.layer == "aspect_2"


def test_the_list_shows_the_active_mesh_only() -> None:
    _reset()
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills)
    _add("Terrain > Slope")
    _add("Water > Hydrology")
    _only(other)
    _add("Terrain > Slope")
    _only(hills)
    assert _rows() == ["Slope", "Hydrology"]
    _only(other)
    assert _rows() == ["Slope"]

    _only()  # no active mesh: one muted line, and nothing below the list
    calls = h.draw(_ui().GEOTOOLS_PT_analyses)
    assert "Select a mesh to see its analyses" in h.label_text(calls)
    assert not any(kind == "template_list" for kind, _t, _i in calls)
    assert _rows() == []
    assert not _ui().GEOTOOLS_PT_analyses_inputs.poll(bpy.context)
    assert "Selected" not in h.label_text(calls) and " of " not in h.label_text(calls)


def test_the_active_row_follows_the_active_mesh() -> None:
    _reset()
    an = _an()
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills)
    _add("Terrain > Slope")
    _add("Terrain > Aspect")
    _only(other)
    _add("Terrain > Aspect")
    _add("Terrain > Slope")
    props = _props()
    names = [(i.object.name, i.name) for i in props.items]
    assert names == [
        ("hills", "Slope"),
        ("hills", "Aspect"),
        ("other", "Aspect"),
        ("other", "Slope"),
    ]
    _only(hills)
    props.index = 0  # hills' Slope
    _only(other)
    an.follow_active(bpy.context)
    assert props.index == 3  # other's Slope, not its first row
    _only(hills)
    props.index = 1  # hills' Aspect
    _only(hills)
    an.follow_active(bpy.context)
    assert props.index == 1  # unchanged while it is the mesh's


def test_a_preset_needs_a_selection() -> None:
    _reset()
    _only()
    props = _props()
    props.add_search = "Terrain > Slope"
    assert "Select" in props.error, props.error
    assert not len(props.items)


# --- tools and Fast Bakes --------------------------------------------------------------


def test_tool_results_run_on_their_own_mesh() -> None:
    _reset()
    an = _an()
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills, other)
    one, two = _add("Water > Hydrology")
    assert (one.object, two.object) == (hills, other)
    assert one.layer == two.layer == "flow_accumulation"  # one layer, values per mesh
    assert not one.computed and two.kind == "HYDROLOGY"
    one.hydrology.min_depth = 0.05
    h.draw(_ui().GEOTOOLS_PT_analyses_inputs)
    _only(other)  # Compute uses the result's mesh, not the selection
    _props().index = 0
    _only(hills)
    assert bpy.ops.geotools.analyses_compute() == {"FINISHED"}, one.hydrology.error
    assert one.computed and not two.computed
    assert abs(one.hydrology.min_depth - 0.05) < 1e-6  # copied back, not reset by the run
    assert "gt_flow_accumulation" in hills.data.attributes
    assert "gt_flow_accumulation" not in other.data.attributes
    assert not an.out_of_date(one)
    one.hydrology.min_depth = 0.2
    assert an.out_of_date(one) and not an.out_of_date(two)
    assert "out of date" in h.label_text(h.draw(_ui().GEOTOOLS_PT_analyses_inputs))
    labels = h.label_text(h.draw(_ui().GEOTOOLS_PT_analyses_inputs))
    assert "Depressions" in labels, labels

    # Source Collection: one run over both meshes, owned by hills.
    one.extent = "SOURCE"
    assert bpy.ops.geotools.analyses_compute() == {"FINISHED"}, one.hydrology.error
    assert "gt_flow_accumulation" in other.data.attributes
    assert one.object == hills and not an.out_of_date(one)


def test_sun_hours_keeps_its_own_date() -> None:
    _reset()
    sun = bpy.context.scene.geotools_analysis.sun
    sun.year, sun.month, sun.day = 2026, 3, 20
    _only(_bowl())
    (item,) = _add("Sun > Sun Hours")
    assert (item.sun_year, item.sun_month, item.sun_day) == (2026, 3, 20)  # from Sun
    item.sun_month, item.sun_day = 12, 21
    an = _an()
    with an._sun_date(bpy.context, item):
        assert (sun.month, sun.day) == (12, 21)
    assert (sun.month, sun.day) == (3, 20)  # put back
    h.draw(_ui().GEOTOOLS_PT_analyses_inputs)


def test_a_fast_bake_result_computes_with_its_own_inputs() -> None:
    _reset()
    an = _an()
    _only(_bowl("hills", n=6))
    (item,) = _add("Relief and Sky > Sky Visibility")
    assert (item.kind, item.preset, item.layer) == ("FAST", "sky_visibility", "sky_visibility")
    assert not item.computed
    item.fast.rays = 32
    h.draw(_ui().GEOTOOLS_PT_analyses_inputs)
    assert bpy.ops.geotools.analyses_compute() == {"FINISHED"}, item.fast.error
    assert item.computed and item.fast.result
    assert h.project().layers["sky_visibility"]
    assert not an.out_of_date(item)
    item.fast.rays = 64
    assert an.out_of_date(item)

    (live,) = _add("Relief and Sky > Sky-View Factor (Fast Bake)")
    assert live.layer == "sky_view_factor_fast"


# --- plain layers ----------------------------------------------------------------------


def test_a_plain_layer_gets_a_row_on_each_mesh_that_carries_it() -> None:
    _reset()
    an = _an()
    layers = h.submodule("blender.layers")
    core = h.submodule("core.layers")
    hills, other, bare = _bowl("hills"), _bowl("other", x=30.0), _bowl("bare", x=60.0)
    spec = core.LayerSpec("cover", core.Domain.FACE, core.DataType.FLOAT)
    item = layers.ensure_layer(bpy.context.scene, spec)
    for obj in (hills, other):
        values = np.ones(len(obj.data.polygons), np.float32)
        layers.write_layer(obj.data, spec, values)
    layers.refresh_carriers(bpy.context.scene, item, sync_view=False)
    an.sync(bpy.context.scene)
    _only(hills)
    assert _rows() == ["cover"]
    _only(other)
    assert _rows() == ["cover"]
    _only(bare)
    assert _rows() == []
    _only(other)
    an.follow_active(bpy.context)
    h.draw(_ui().GEOTOOLS_PT_analyses_inputs)
    h.draw(_ui().GEOTOOLS_PT_analyses_details)
    assert bpy.ops.geotools.analyses_remove() == {"FINISHED"}
    assert "gt_cover" not in other.data.attributes  # its values on this mesh only
    assert "gt_cover" in hills.data.attributes and "cover" in h.project().layers


# --- removing and the scene ------------------------------------------------------------


def test_removing_a_result_takes_its_modifier_and_objects() -> None:
    _reset()
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills, other)
    _add("Terrain > Slope")
    _only(hills)
    _an().follow_active(bpy.context)
    plan = _an().removal_plan(bpy.context.scene, _an().shown_item(bpy.context))
    assert plan == ["Its modifier on hills"], plan
    assert bpy.ops.geotools.analyses_remove() == {"FINISHED"}
    assert not _ap().preset_modifiers(hills) and _ap().preset_modifiers(other)
    assert "slope" in h.project().layers  # other's result still stores it
    _only(other)
    _an().follow_active(bpy.context)
    assert bpy.ops.geotools.analyses_remove() == {"FINISHED"}
    assert "slope" not in h.project().layers  # no result, no carrier: gone

    _only(hills)
    (contours,) = _add("Lines > Contours")
    assert bpy.ops.geotools.analyses_compute() == {"FINISHED"}, contours.contours.contour_error
    made = [o.object.name for o in contours.outputs]
    assert made, "contours made no object"
    assert any(made[0] in line for line in _an().removal_plan(bpy.context.scene, contours))
    assert bpy.ops.geotools.analyses_remove() == {"FINISHED"}
    assert not any(name in bpy.data.objects for name in made)


def test_the_list_follows_the_modifier_stack() -> None:
    _reset()
    an = _an()
    hills = _bowl("hills")
    _only(hills)
    _add("Terrain > Slope")
    ((modifier, _preset),) = _ap().preset_modifiers(hills)
    hills.modifiers.remove(modifier)  # deleted in the modifier stack
    bpy.context.view_layer.update()  # the depsgraph handler syncs
    an.sync(bpy.context.scene)
    assert _rows() == [], _rows()
    # A preset a script adds (the old operator) is listed.
    assert bpy.ops.geotools.analysis_add(preset="slope") == {"FINISHED"}
    an.sync(bpy.context.scene)
    assert _rows() == ["Slope"]


def test_publish_picks_its_variant_from_the_results() -> None:
    _reset()
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills, other)
    _add("Terrain > Slope")
    choices = dict(_an().variant_choices(bpy.context.scene, ""))
    assert "Slope on hills" in choices["slope"] and "Slope on other" in choices["slope"]
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    _only(hills)
    _an().follow_active(bpy.context)
    assert bpy.ops.geotools.analyses_publish_variant() == {"FINISHED"}
    assert h.project().build_profiles[0].variant_layer == "slope"


def test_the_sync_stays_cheap() -> None:
    """Hundreds of results on tens of meshes: the key check is well under a frame."""
    _reset()
    an = _an()
    meshes = [_bowl(f"m{i}", n=2, x=30.0 * i) for i in range(30)]
    props = _props()
    for i in range(300):
        item = props.items.add()
        item.kind, item.name, item.object = "HYDROLOGY", f"r{i}", meshes[i % 30]
    scene = bpy.context.scene
    an.sync_if_changed(scene)
    start = time.perf_counter()
    an.sync_if_changed(scene)  # nothing changed: the key only
    key_seconds = time.perf_counter() - start
    start = time.perf_counter()
    an.sync(scene)
    sync_seconds = time.perf_counter() - start
    print(f"VALIDATION analyses_sync {{'key': {key_seconds:.4f}, 'sync': {sync_seconds:.4f}}}")
    assert key_seconds < 0.01, key_seconds
    assert sync_seconds < 0.25, sync_seconds
    _only(meshes[0])
    assert len(_rows()) == 10


# --- a file from before the list ---------------------------------------------------------


def test_a_file_from_before_the_list_is_migrated_on_open() -> None:
    """Build develop's layout with the operators develop used, save it with no list,
    open it again: its preset modifiers, tool layers and lines, and plain layers show."""
    _reset()
    layers = h.submodule("blender.layers")
    core = h.submodule("core.layers")
    hills, other = _bowl("hills"), _bowl("other", x=30.0)
    _only(hills, other)
    assert bpy.ops.geotools.analysis_add(preset="slope") == {"FINISHED"}  # both meshes
    _only(hills)
    assert bpy.ops.geotools.hydrology() == {"FINISHED"}
    assert bpy.ops.geotools.contours() == {"FINISHED"}
    spec = core.LayerSpec("cover", core.Domain.FACE, core.DataType.FLOAT)
    cover = layers.ensure_layer(bpy.context.scene, spec)
    layers.write_layer(other.data, spec, np.ones(len(other.data.polygons), np.float32))
    layers.refresh_carriers(bpy.context.scene, cover, sync_view=False)
    for layer in h.project().layers:
        layer.carriers.clear()  # develop stored carriers only for the live view
    props = _props()
    props.items.clear()  # develop had no list
    props.version = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "develop_layout.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}
    props = _props()
    assert props.version == _an().MIGRATED
    rows = sorted((i.object.name, i.kind, i.name) for i in props.items)
    assert ("hills", "PRESET", "Slope") in rows and ("other", "PRESET", "Slope") in rows, rows
    assert ("hills", "HYDROLOGY", "Hydrology") in rows, rows
    assert ("other", "LAYER", "cover") in rows, rows
    hydro = next(i for i in props.items if i.kind == "HYDROLOGY")
    assert {e.name for e in hydro.layers} == {"flow_accumulation", "ponded_depth", "depression"}
    assert hydro.computed and not _an().out_of_date(hydro)
    contours = next(i for i in props.items if i.kind == "CONTOURS")
    assert contours.outputs and contours.computed
    assert not any(i.kind == "LAYER" and i.layer == "slope" for i in props.items), rows
    h.reset_scene()


def test_a_file_with_the_old_group_and_panel_names_still_loads() -> None:
    """Relief and Sky was Archaeology (Archaeological Visualisation) and Horizon and Sky
    Events was Archaeoastronomy. Only labels changed: the category member, preset ids,
    operators and stored properties keep their names, so a file saved with a preset of
    that group and the panel's settings opens with both, under the new names."""
    _reset()
    catalog = h.submodule("analysis.presets")
    hills = _bowl("hills")
    _only(hills)
    assert bpy.ops.geotools.analysis_add(preset="sky_view_factor") == {"FINISHED"}
    bpy.context.scene.geotools_analysis.archaeo.eye_height = 1.75
    props = _props()
    props.items.clear()  # saved before the list
    props.version = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "old_names.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True) == {"FINISHED"}
        assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}
    item = next(i for i in _props().items if i.preset == "sky_view_factor")
    assert item.object.name == "hills" and item.kind == "PRESET"
    preset = _an().preset_of(item)
    assert preset.category is catalog.Category.ARCHAEOLOGY  # the stored member
    assert catalog.CATEGORY_LABELS[preset.category] == "Relief and Sky"
    assert "Relief and Sky > Sky-View Factor" in {e.text for e in _an().ENTRIES}
    archaeo = bpy.context.scene.geotools_analysis.archaeo
    assert abs(archaeo.eye_height - 1.75) < 1e-6
    panel = h.submodule("blender.archaeo_ui").GEOTOOLS_PT_analysis_archaeo
    assert panel.bl_idname == "GEOTOOLS_PT_analysis_archaeo"
    assert (panel.bl_label, panel.bl_parent_id) == (
        "Horizon and Sky Events",
        "GEOTOOLS_PT_analysis",
    )
    assert "eye_height" in h.props_drawn(h.draw(panel))
    for name in ("archaeo_horizon", "archaeo_events", "archaeo_sightlines"):
        assert h.operator_available(f"geotools.{name}"), name
    h.reset_scene()
