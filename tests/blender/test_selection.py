"""Selection-driven analysis and layers; the source collection only decides what is published.

Issue #155: the analysis tools act on the selected meshes wherever they are and
refuse with nothing selected; a layer, its range and Show in Viewport cover
every object that carries it; an unset source collection is set from the
object worked on, and says so; Run Checks flags a layer outside the source
collection and offers Add to Source.
"""

from __future__ import annotations

import _harness as h
import bpy
import numpy as np


def _layers():
    return h.submodule("blender.layers")


def _collection(name: str) -> bpy.types.Collection:
    collection = bpy.data.collections.new(name)
    bpy.context.scene.collection.children.link(collection)
    return collection


def _reset() -> None:
    for item in h.project().layers:
        item.presentation.show_in_viewport = False
    h.reset_scene()
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    for name in ("geotools_hydrology", "geotools_contours", "geotools_least_cost"):
        group = getattr(bpy.context.scene, name)
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"} and not prop.is_readonly:
                group.property_unset(prop.identifier)


def _grid(name: str, collection: bpy.types.Collection | None, n: int = 6) -> bpy.types.Object:
    """An n x n quad grid, 1 m cells sloping along x, in ``collection`` (the scene's own if None)."""
    axis = np.arange(n + 1, dtype=np.float32)
    xs, ys = np.meshgrid(axis, axis)
    co = np.stack([xs.ravel(), ys.ravel(), 0.2 * xs.ravel()], axis=1)
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
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def _select(*objects: bpy.types.Object) -> None:
    bpy.context.view_layer.update()
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj in objects)
    bpy.context.view_layer.objects.active = objects[0] if objects else None


class _Poll:
    """Stands in for an operator class so its poll message can be read back; anything
    else the poll reads comes from the class."""

    def __init__(self, cls: type) -> None:
        self.cls = cls
        self.messages: list[str] = []

    def poll_message_set(self, message: str) -> None:
        self.messages.append(message)

    def __getattr__(self, name: str) -> object:
        return getattr(self.cls, name)


def _poll_message(cls: type) -> str | None:
    """``cls.poll`` in the current context: None if it passes, else its poll message."""
    stub = _Poll(cls)
    if cls.poll.__func__(stub, bpy.context):
        return None
    assert stub.messages, f"{cls.__name__}.poll failed without a message"
    return stub.messages[-1]


def _add_analysis(preset: str = "slope", **props: object) -> h.ReportRecorder:
    """Run Add Analysis's execute, recording its reports."""
    recorder = h.ReportRecorder()
    recorder.preset, recorder.name, recorder.north, recorder.engine = preset, "", "GRID", "GEONODES"
    for key, value in props.items():
        setattr(recorder, key, value)
    cls = h.submodule("blender.analysis_presets").GEOTOOLS_OT_analysis_add
    assert cls.execute(recorder, bpy.context) == {"FINISHED"}, recorder.reports
    return recorder


def _show(item=None) -> tuple[set[str], h.ReportRecorder]:
    """Press Show in Viewport on the selected layer, recording its reports."""
    recorder = h.ReportRecorder()
    cls = h.submodule("blender.live_view").GEOTOOLS_OT_layer_show
    return cls.execute(recorder, bpy.context), recorder


def _messages(recorder: h.ReportRecorder, level: str = "INFO") -> str:
    return " | ".join(text for levels, text in recorder.reports if level in levels)


def _view_objects() -> list[str]:
    is_view = h.submodule("blender.export").is_view_modifier
    return sorted(o.name for o in bpy.data.objects if any(is_view(m) for m in o.modifiers))


# --- nothing selected --------------------------------------------------------------------


def test_nothing_selected_is_refused_with_a_clear_message() -> None:
    _reset()
    site = _collection("Site")
    h.project().source_collection = site
    _grid("Ground", site)
    _select()
    surface = h.submodule("blender.surface")
    want = {
        h.submodule("blender.analysis_presets").GEOTOOLS_OT_analysis_add: surface.SELECT_MESHES,
        h.submodule("blender.hydrology").GEOTOOLS_OT_hydrology: surface.SELECT_MESHES,
        h.submodule("blender.contours").GEOTOOLS_OT_contours: surface.SELECT_MESHES,
        h.submodule("blender.contours").GEOTOOLS_OT_sections: (
            "Select the meshes to cut, besides the section line"
        ),
        h.submodule("blender.sun_hours").GEOTOOLS_OT_sun_hours: surface.SELECT_MESHES,
        h.submodule("blender.least_cost").GEOTOOLS_OT_least_cost: "Select the mesh to route over",
    }
    assert surface.SELECT_MESHES == "Select the meshes to analyse"
    for cls, message in want.items():
        assert _poll_message(cls) == message, cls.__name__
    # Run without the poll, the tools refuse with the same words.
    hydrology = h.submodule("blender.hydrology")
    try:
        hydrology.compute(bpy.context)
    except surface.SurfaceError as ex:
        assert str(ex) == "Select the meshes to analyse.", ex
    else:
        raise AssertionError("hydrology ran with nothing selected")
    assert not h.project().layers

    # The whole source collection stays a choice where whole-site runs make sense.
    bpy.context.scene.geotools_hydrology.surface = "SOURCE"
    assert _poll_message(hydrology.GEOTOOLS_OT_hydrology) is None
    h.project().source_collection = None
    assert _poll_message(hydrology.GEOTOOLS_OT_hydrology) == surface.NO_SOURCE


def test_surface_defaults_to_the_selection_and_keeps_old_files() -> None:
    _reset()
    scene = bpy.context.scene
    for group in (
        scene.geotools_hydrology,
        scene.geotools_contours,
        scene.geotools_least_cost,
        scene.geotools_analysis.sun_hours,
    ):
        assert group.surface == "SELECTED", group
        # Stored by number: a file saved with Source Collection (0) still reads it.
        items = group.bl_rna.properties["surface"].enum_items
        assert (items["SOURCE"].value, items["SELECTED"].value) == (0, 1)


# --- a selection outside the source ------------------------------------------------------


def test_analysis_on_a_selection_outside_the_source() -> None:
    _reset()
    site, other = _collection("Site"), _collection("Other")
    h.project().source_collection = site
    inside = _grid("Inside", site)
    outside = _grid("Outside", other)
    _select(outside)

    recorder = _add_analysis("slope")
    info = _messages(recorder)
    assert "on 1 object, layer slope" in info, recorder.reports
    assert "Not in the source collection, so not published: Outside" in info, info
    assert not inside.modifiers and outside.modifiers
    item = _layers().find_layer(bpy.context.scene, "slope")
    # The layer covers the object that carries it, so its range is read from it.
    assert _layers().stored_carriers(item) == [outside]
    assert float(item.value_min) > 10.0, item.value_min  # 0.2 rise per metre: 11.3 degrees
    codes = {(i.code, i.subject) for i in _layers().cached_status(bpy.context.scene, item)}
    assert ("layer-outside-source", "Outside") in codes, codes
    assert ("layer-attribute-missing", "Inside") in codes, codes

    # Hydrology runs on the selection too, wherever it is.
    hydrology = h.submodule("blender.hydrology")
    result = hydrology.compute(bpy.context)
    assert result["objects"] == 1
    assert "gt_flow_accumulation" not in inside.data.attributes
    assert "gt_flow_accumulation" in outside.data.attributes
    assert result["source_notes"] == ["Not in the source collection, so not published: Outside"], (
        result["source_notes"]
    )


# --- the source collection auto-default ------------------------------------------------------


def test_adding_an_analysis_sets_an_unset_source() -> None:
    _reset()
    collection = _collection("Collection")
    obj = _grid("Imported", collection)
    _select(obj)
    assert h.project().source_collection is None

    recorder = _add_analysis("slope")
    assert h.project().source_collection == collection
    info = _messages(recorder)
    assert "Source set to Collection" in info, recorder.reports
    assert "not published" not in info, info
    item = _layers().find_layer(bpy.context.scene, "slope")
    # The status was checked against the new source, not "no source".
    codes = {i.code for i in _layers().cached_status(bpy.context.scene, item)}
    assert not codes & {"layer-no-source", "layer-not-checked", "layer-outside-source"}, codes

    # Set once: a later run leaves the user's choice alone.
    other = _collection("Other")
    second = _grid("Second", other)
    _select(second)
    recorder = _add_analysis("aspect")
    assert h.project().source_collection == collection
    assert "Source set" not in _messages(recorder)


def test_the_auto_default_never_picks_the_scene_or_output_collections() -> None:
    _reset()
    source = h.submodule("blender.source")
    scene = bpy.context.scene
    loose = _grid("Loose", None)  # only in the scene's own collection
    assert source.default_source(scene, [loose]) is None
    analysis = _collection("GeoTools Analysis")
    result = _grid("Result", analysis)
    assert source.default_source(scene, [result]) is None
    assert h.project().source_collection is None
    _select(loose)
    recorder = _add_analysis("slope")
    assert h.project().source_collection is None
    assert "No source collection, so nothing is published" in _messages(recorder)
    # The active object's collection wins.
    first, second = _collection("First"), _collection("Second")
    a, b = _grid("A", first), _grid("B", second)
    _select(b, a)
    assert source.analysis_notes(bpy.context, [a, b])[0] == "Source set to Second"


def test_showing_a_layer_sets_an_unset_source() -> None:
    _reset()
    collection = _collection("Collection")
    obj = _grid("Imported", collection)
    _select(obj)
    _add_analysis("slope")
    h.project().source_collection = None
    result, recorder = _show()
    assert result == {"FINISHED"}, recorder.reports
    assert h.project().source_collection == collection
    assert "Source set to Collection" in _messages(recorder), recorder.reports
    assert "shown on 1 object" in _messages(recorder), recorder.reports
    _show()  # off again
    assert not _view_objects()


# --- Show in Viewport --------------------------------------------------------------------


def test_show_in_viewport_covers_objects_outside_the_source() -> None:
    _reset()
    site, other = _collection("Site"), _collection("Other")
    h.project().source_collection = site
    inside = _grid("Inside", site)
    outside = _grid("Outside", other)
    _select(outside)
    _add_analysis("slope")
    result, recorder = _show()
    assert result == {"FINISHED"}, recorder.reports
    item = _layers().find_layer(bpy.context.scene, "slope")
    assert item.presentation.show_in_viewport
    assert _view_objects() == ["Outside"], _view_objects()
    assert "Not in the source collection, so not published: Outside" in _messages(recorder)

    # A later analysis that puts the layer on another object extends the view.
    _select(inside)
    _add_analysis("slope", name="slope")
    assert _view_objects() == ["Inside", "Outside"], _view_objects()
    _show()
    assert not item.presentation.show_in_viewport and not _view_objects()


def test_show_in_viewport_with_no_carrier_reports_and_stays_off() -> None:
    _reset()
    site = _collection("Site")
    h.project().source_collection = site
    _grid("Ground", site)
    assert bpy.ops.geotools.layer_add(name="empty") == {"FINISHED"}
    result, recorder = _show()
    assert result == {"CANCELLED"}, recorder.reports
    item = _layers().find_layer(bpy.context.scene, "empty")
    assert not item.presentation.show_in_viewport
    assert not _view_objects()
    warning = _messages(recorder, "WARNING")
    assert "Layer empty not shown" in warning and "gt_empty" in warning, recorder.reports

    # The Display panel draws the toggle as that operator, pressed in while shown.
    h.draw(h.layer_panel("display"))
    assert h.operator_text("geotools.layer_show") == "Show in Viewport"


# --- Checks ----------------------------------------------------------------------------------


def test_checks_flag_a_layer_outside_the_source_and_add_it() -> None:
    _reset()
    site, other = _collection("Site"), _collection("Other")
    h.project().source_collection = site
    _grid("Inside", site)
    outside = _grid("Outside", other)
    _select(outside)
    _add_analysis("slope")
    _add_analysis("aspect")

    assert bpy.ops.geotools.run_checks() == {"FINISHED"}
    props = h.project()
    found = [i for i in props.issues if i.code == "layer-outside-source"]
    assert len(found) == 1, [(i.code, i.message) for i in props.issues]
    issue = found[0]
    assert issue.severity == "WARNING" and issue.subject == "Outside"
    assert "layers slope, aspect" in issue.message and "not published" in issue.message
    assert "source collection Site" in issue.message, issue.message

    # The Checks panel offers the fix for that issue.
    props.issues_index = list(props.issues).index(issue)
    h.draw(h.submodule("blender.panels").GEOTOOLS_PT_checks)
    assert h.operator_text("geotools.add_to_source") is not None

    assert bpy.ops.geotools.add_to_source(object="Outside") == {"FINISHED"}
    assert site in outside.users_collection and other in outside.users_collection
    assert not [i for i in props.issues if i.code == "layer-outside-source"]
    item = _layers().find_layer(bpy.context.scene, "slope")
    codes = {i.code for i in _layers().cached_status(bpy.context.scene, item)}
    assert "layer-outside-source" not in codes, codes
