"""The Analyses list mock-up (#157): the Add Analysis search, items, Compute and panels.

Cheap checks only, while the design is a mock-up: the picker lists every
preset and tool, adding a preset makes a list item whose Inputs are the
modifier's own inputs, a tool item keeps its own settings and runs its
operator on its meshes, other layers can be listed, and the classic panels
are hidden by default.
"""

from __future__ import annotations

import _harness as h
import bpy
import numpy as np


def _an():
    return h.submodule("blender.analyses")


def _ui():
    return h.submodule("blender.analyses_ui")


def _reset() -> None:
    h.reset_scene()
    props = bpy.context.scene.geotools_analyses
    props.items.clear()
    props.index = 0
    props.error = props.note = ""
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    collection = bpy.data.collections.new("Site")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection


def _bowl(name: str = "Bowl", n: int = 12) -> bpy.types.Object:
    """A small quad grid with a dip in the middle, selected and active."""
    axis = np.linspace(-10.0, 10.0, n + 1)
    xs, ys = np.meshgrid(axis, axis)
    zs = 0.02 * (xs**2 + ys**2) + 0.1 * xs
    co = np.stack([xs.ravel(), ys.ravel(), zs.ravel()], axis=1)
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
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def test_the_picker_lists_every_preset_and_tool() -> None:
    an = _an()
    catalog = h.submodule("analysis.presets")
    keys = {e.key for e in an.ENTRIES}
    assert {f"preset:{p.id}" for p in catalog.PRESETS} <= keys
    for key in ("tool:HYDROLOGY", "tool:SUN_HOURS", "tool:LEAST_COST", "tool:CONTOURS"):
        assert key in keys, key
    assert "layer:ATTRIBUTE" in keys
    texts = [e.text for e in an.search_entries("slope")]
    assert "Terrain > Slope" in texts, texts
    assert next(e.key for e in an.search_entries("water hydro")) == "tool:HYDROLOGY"
    assert not an.search_entries("no such analysis")
    assert len({e.text for e in an.ENTRIES}) == len(an.ENTRIES)  # the field stores the text


def test_classic_panels_are_hidden_by_default() -> None:
    panels = h.submodule("blender.panels")
    assert not panels.GEOTOOLS_PT_layers.poll(bpy.context)
    assert not h.submodule("blender.analysis_ui").GEOTOOLS_PT_analysis.poll(bpy.context)
    assert _ui().GEOTOOLS_PT_analyses.poll(bpy.context)
    assert _ui().GEOTOOLS_PT_tools.poll(bpy.context)


def test_adding_a_preset_lists_it_with_the_modifier_inputs() -> None:
    _reset()
    obj = _bowl()
    props = bpy.context.scene.geotools_analyses
    props.add_search = "Terrain > Slope"  # what choosing the entry stores
    assert props.add_search == ""
    assert not props.error, props.error
    assert len(props.items) == 1
    item = props.items[0]
    assert (item.kind, item.preset, item.layer) == ("PRESET", "slope", "slope")
    assert [o.object for o in item.objects] == [obj]
    assert h.project().layers["slope"]
    modifier, carrier = _ui().preset_modifier(bpy.context, item)
    assert carrier == obj and modifier is not None
    calls = h.draw(_ui().GEOTOOLS_PT_analyses_inputs)
    assert "value" in h.props_drawn(calls)  # the modifier's own inputs
    h.draw(_ui().GEOTOOLS_PT_analyses)
    h.draw(_ui().GEOTOOLS_PT_analyses_display)
    h.draw(_ui().GEOTOOLS_PT_analyses_outputs)
    h.draw_list_item(_an().GEOTOOLS_UL_analyses, props, item)
    # The list's eye goes through Show in Viewport (#155: it refuses a layer nobody carries).
    assert bpy.ops.geotools.analyses_show(layer="slope") == {"FINISHED"}
    assert h.project().layers["slope"].presentation.show_in_viewport
    assert bpy.ops.geotools.analyses_show(layer="slope") == {"FINISHED"}
    assert not h.project().layers["slope"].presentation.show_in_viewport
    # Adding it again on the same mesh reuses the item.
    props.add_search = "Terrain > Slope"
    assert len(props.items) == 1


def test_a_preset_needs_a_selection() -> None:
    _reset()
    props = bpy.context.scene.geotools_analyses
    props.add_search = "Terrain > Slope"
    assert "Select" in props.error, props.error
    assert not len(props.items)


def test_a_tool_item_keeps_its_settings_and_computes() -> None:
    _reset()
    obj = _bowl()
    props = bpy.context.scene.geotools_analyses
    props.add_search = "Water > Hydrology"
    item = props.items[props.index]
    assert item.kind == "HYDROLOGY" and not item.computed
    assert item.hydrology.surface == "SELECTED"
    assert [o.object for o in item.objects] == [obj]
    item.hydrology.min_depth = 0.05
    h.draw(_ui().GEOTOOLS_PT_analyses_inputs)
    obj.select_set(False)  # Compute uses the item's meshes, not the selection
    assert bpy.ops.geotools.analyses_compute() == {"FINISHED"}, item.hydrology.error
    assert item.computed and item.hydrology.result
    assert abs(item.hydrology.min_depth - 0.05) < 1e-6  # copied back, not reset by the run
    assert item.layer == "flow_accumulation"
    assert {e.name for e in item.layers} == {"flow_accumulation", "ponded_depth", "depression"}
    assert not obj.select_get()  # the selection is put back
    labels = h.label_text(h.draw(_ui().GEOTOOLS_PT_analyses_inputs))
    assert "Depressions" in labels, labels
    # A second Hydrology item gets layer names of its own.
    obj.select_set(True)
    props.add_search = "Water > Hydrology"
    second = props.items[props.index]
    assert second.hydrology.accumulation_layer == "flow_accumulation_2"


def test_unlisted_layers_are_listed_and_removed() -> None:
    _reset()
    _bowl()
    layers = h.submodule("blender.layers")
    core = h.submodule("core.layers")
    layers.ensure_layer(
        bpy.context.scene, core.LayerSpec("cover", core.Domain.FACE, core.DataType.FLOAT)
    )
    an = _an()
    assert an.unlisted_layers(bpy.context.scene) == ["cover"]
    assert an.list_unlisted_layers(bpy.context.scene) == 1
    props = bpy.context.scene.geotools_analyses
    assert props.items[0].kind == "LAYER"
    assert an.unlisted_layers(bpy.context.scene) == []
    props.index = 0
    h.draw(_ui().GEOTOOLS_PT_analyses_inputs)
    assert bpy.ops.geotools.analyses_remove() == {"FINISHED"}
    assert not len(props.items) and "cover" not in h.project().layers
