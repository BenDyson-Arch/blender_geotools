"""The Analyses list (a mock-up, #157): every analysis result in one list.

``scene.geotools_analyses`` holds one item per analysis the user added with
the searchable **Add Analysis** field (:func:`search_entries`, the CRS
picker's pattern: a ``StringProperty`` with a search callback whose update
runs the add). Each item is one of:

* ``PRESET``: a Geometry Nodes preset (``analysis_presets.add_preset``). A
  layer preset's item names its layer and the meshes that carry the modifier;
  an object preset's item lists the new object of lines in ``outputs``. The
  sidebar draws the inputs of the modifier itself (``analyses_ui``), so
  nothing is copied: the modifier stays the single place the inputs live.
* ``HYDROLOGY``, ``SUN_HOURS``, ``LEAST_COST``, ``CONTOURS``, ``SECTIONS``:
  a Python tool. The item keeps its own copy of the tool's settings group
  (the same ``PropertyGroup`` type the classic panels use on the scene) and
  the meshes it runs on. **Compute** (:class:`GEOTOOLS_OT_analyses_compute`)
  copies the item's settings onto the scene's group, selects the item's
  meshes, runs the tool's existing operator, and copies the settings (with
  the result or error) back, so every result can be edited and run again.
* ``LAYER``: a plain layer (From Attribute, Texture Bake, or a layer that was
  already in the registry, listed by :func:`list_unlisted_layers`).

Selecting an item makes its layer the registry's active layer
(``scene.geotools.layers_index``), so the existing layer, bake and display
operators act on it.

**The list follows the selection**, as Blender's modifier stack follows the
active object. A result's meshes (:func:`result_meshes`) are the meshes it
applies to and the objects it made; a plain layer's are its carriers
(``layers.stored_carriers``). With **Selected** on (the default), the list
shows the results whose meshes include the active object or a selected one
(:func:`visible_flags`, cached per selection and depsgraph update); with
nothing selected it shows all. When the active object changes (a msgbus
subscription), the active row moves to that mesh's result of the same kind,
else its first (:func:`follow_active`).

**One result per analysis.** A preset result has one modifier on each of its
meshes; the sidebar draws the active mesh's (else the first's) inputs, and a
depsgraph handler copies them to the result's other modifiers
(:func:`sync_preset_inputs`). A tool result remembers a signature of its
settings and meshes at its last run, so the panel can say it is out of date.

This is a mock-up for the maintainer to try; the classic Analysis and
Layers panels stay, behind the ``Use Classic Analysis Panels`` preference.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..analysis import presets as catalog
from ..core.layers import DataType, Domain, LayerSpec, attribute_name
from . import analysis, analysis_presets, contours, hydrology, least_cost
from .common import redraw_ui
from .layers import (
    DOMAIN_ICONS,
    ensure_layer,
    find_layer,
    refresh_carriers,
    remove_layer_data,
    spec_from_props,
    stored_carriers,
)

log = logging.getLogger(__name__)

KIND_ITEMS = (
    ("PRESET", "Preset", "A Geometry Nodes analysis preset: live, edited on its modifier"),
    ("HYDROLOGY", "Hydrology", "Flow accumulation, ponding and depressions"),
    ("SUN_HOURS", "Sun Hours", "Hours of direct sun and clear-sky insolation"),
    ("LEAST_COST", "Least-Cost Routes", "Least-cost routes and travel times"),
    ("CONTOURS", "Contours", "Contour lines"),
    ("SECTIONS", "Sections", "Section lines"),
    ("LAYER", "Layer", "A plain layer: an attribute, a texture bake or values written by hand"),
)

TOOL_KINDS = frozenset({"HYDROLOGY", "SUN_HOURS", "LEAST_COST", "CONTOURS", "SECTIONS"})

KIND_ICONS = {
    "PRESET": "GEOMETRY_NODES",
    "HYDROLOGY": "MOD_FLUIDSIM",
    "SUN_HOURS": "LIGHT_SUN",
    "LEAST_COST": "TRACKING",
    "CONTOURS": "IPO_CONSTANT",
    "SECTIONS": "SNAP_EDGE",
}

SETTINGS_ATTR = {
    "HYDROLOGY": "hydrology",
    "SUN_HOURS": "sun_hours",
    "LEAST_COST": "least_cost",
    "CONTOURS": "contours",
    "SECTIONS": "contours",
}
"""The item's own settings group for each tool kind."""


class AnalysesError(RuntimeError):
    """The analysis could not be added or run; the message says why."""


# --- the Add Analysis catalog ----------------------------------------------------------


GROUP_SHORT = {
    catalog.Category.TERRAIN: "Terrain",
    catalog.Category.ARCHAEOLOGY: "Archaeology",
    catalog.Category.VISIBILITY: "Visibility",
    catalog.Category.CHANGE: "Change",
    catalog.Category.VOLUMES: "Volumes",
    catalog.Category.PATHS: "Movement",
}
"""Short group names for the picker, so an entry fits the search list."""

PRESET_GROUP = {"flow_path": "Water", "least_cost_path": "Movement"}
"""Presets filed under another group than their category's."""

GROUP_ORDER = (
    "Terrain",
    "Archaeology",
    "Visibility",
    "Change",
    "Volumes",
    "Water",
    "Sun",
    "Movement",
    "Lines",
    "Layers",
)
"""The order of the picker's groups."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One choice of the Add Analysis search field."""

    key: str  # "preset:<id>", "preset:<id>:true", "tool:<KIND>", "layer:ATTRIBUTE"
    group: str
    label: str
    description: str

    @property
    def text(self) -> str:
        """What the search list shows and the field stores until the add runs."""
        return f"{self.group} > {self.label}"


def _entries() -> tuple[Entry, ...]:
    found: list[Entry] = []
    for category in catalog.Category:
        for preset in catalog.by_category(category):
            group = PRESET_GROUP.get(preset.id, GROUP_SHORT[category])
            found.append(Entry(f"preset:{preset.id}", group, preset.label, preset.description))
            if preset.north:
                found.append(
                    Entry(
                        f"preset:{preset.id}:true",
                        group,
                        f"{preset.label} (True North)",
                        f"{preset.description}, from true north",
                    )
                )
    found += [
        Entry(
            "tool:HYDROLOGY",
            "Water",
            "Hydrology",
            "Flow accumulation, ponding and depressions on the 3D surface",
        ),
        Entry("tool:SUN_HOURS", "Sun", "Sun Hours", "Hours of direct sun over a day or a period"),
        Entry(
            "tool:INSOLATION",
            "Sun",
            "Insolation",
            "Clear-sky irradiation in kWh/m², with the sun hours",
        ),
        Entry(
            "tool:LEAST_COST",
            "Movement",
            "Least-Cost Routes",
            "Directed least-cost routes and travel times over one surface",
        ),
        Entry("tool:CONTOURS", "Lines", "Contours", "Contour lines of the surface"),
        Entry("tool:SECTIONS", "Lines", "Sections", "Section lines along a drawn line"),
        Entry(
            "layer:ATTRIBUTE",
            "Layers",
            "From Attribute",
            "A layer read from a mesh attribute (a Geometry Nodes output, an import)",
        ),
        Entry(
            "layer:TEXTURE",
            "Layers",
            "Texture Bake",
            "A texture layer filled by a Cycles bake",
        ),
    ]
    return tuple(sorted(found, key=lambda e: GROUP_ORDER.index(e.group)))


ENTRIES = _entries()
_BY_TEXT = {e.text: e for e in ENTRIES}
_BY_KEY = {e.key: e for e in ENTRIES}


def search_entries(query: str) -> list[Entry]:
    """The entries whose group, label or description holds every word of ``query``.

    Entries matched by their group and label come before those matched only
    through the description, each in catalog order.
    """
    words = query.lower().split()
    found = [e for e in ENTRIES if all(w in f"{e.text} {e.description}".lower() for w in words)]
    return sorted(found, key=lambda e: not all(w in e.text.lower() for w in words))


def _search(_self, _context, edit_text: str):
    try:
        query = "" if edit_text in _BY_TEXT else edit_text
        return [(e.text, e.description) for e in search_entries(query)]
    except Exception:  # a search callback must never raise into the UI
        log.exception("Add Analysis search failed")
        return []


def _picked(self, context: bpy.types.Context) -> None:
    """``add_search`` update: add the chosen entry, then empty the field."""
    text = self.add_search
    if not text:
        return
    self.add_search = ""  # calls this again with "", which returns above
    entry = _BY_TEXT.get(text)
    if entry is None:
        self.error = f"No analysis called {text!r}."
        return
    try:
        item = add_entry(context, entry.key)
    except (AnalysesError, analysis_presets.PresetError) as exc:
        self.error = str(exc)
        return
    self.error = ""
    self.note = f"Added {item.name}"
    redraw_ui(context)


# --- property groups -------------------------------------------------------------------


class GeoToolsAnalysisObject(bpy.types.PropertyGroup):
    """An object an analysis runs on or made."""

    object: PointerProperty(name="Object", description="The object", type=bpy.types.Object)


class GeoToolsAnalysisLayer(bpy.types.PropertyGroup):
    """A layer an analysis writes (``name`` is the layer's name)."""


def _index_changed(self, context: bpy.types.Context) -> None:
    item = active_item(context.scene)
    if item is None or not item.layer:
        return
    layers = context.scene.geotools.layers
    index = layers.find(item.layer)
    if index >= 0:
        context.scene.geotools.layers_index = index


class GeoToolsAnalysisItem(bpy.types.PropertyGroup):
    """One analysis of the Analyses list (``name`` is what the list shows)."""

    kind: EnumProperty(name="Kind", description="What made this result", items=KIND_ITEMS)
    preset: StringProperty(name="Preset", description="The preset's id (presets only)")
    north: StringProperty(
        name="North", description="TRUE when the preset's aspect is from true north"
    )
    layer: StringProperty(
        name="Layer", description="The layer shown in Display and published as a variant"
    )
    layers: CollectionProperty(type=GeoToolsAnalysisLayer)
    objects: CollectionProperty(type=GeoToolsAnalysisObject)
    outputs: CollectionProperty(type=GeoToolsAnalysisObject)
    computed: BoolProperty(
        name="Computed", description="Whether the tool has run since it was added"
    )
    signature: StringProperty(
        name="Signature",
        description="The tool's settings and meshes at its last run (out of date when they differ)",
    )
    hydrology: PointerProperty(type=hydrology.GeoToolsHydrologySettings)
    sun_hours: PointerProperty(type=analysis.GeoToolsSunHoursSettings)
    least_cost: PointerProperty(type=least_cost.GeoToolsLeastCostSettings)
    contours: PointerProperty(type=contours.GeoToolsContourSettings)


def _redraw(_self, context: bpy.types.Context) -> None:
    invalidate()
    redraw_ui(context)


class GeoToolsAnalyses(bpy.types.PropertyGroup):
    """The scene's Analyses list (``scene.geotools_analyses``)."""

    items: CollectionProperty(type=GeoToolsAnalysisItem)
    index: IntProperty(
        name="Active Analysis",
        description="The analysis whose inputs, display and outputs the panel shows",
        default=0,
        update=_index_changed,
    )
    add_search: StringProperty(
        name="Add Analysis",
        description="Search the analyses (presets, tools and layers) and add one to the "
        "selected meshes",
        search=_search,
        search_options=set(),
        update=_picked,
        options={"SKIP_SAVE"},
    )
    show: EnumProperty(
        name="Show",
        description="Which results the list shows",
        items=(
            (
                "SELECTED",
                "Selected",
                (
                    "The results on the active object or the selected meshes (all of them when "
                    "nothing is selected)"
                ),
            ),
            ("ALL", "All", "Every result in the file"),
        ),
        default="SELECTED",
        update=_redraw,
    )
    error: StringProperty(default="")
    note: StringProperty(default="")


def settings(scene: bpy.types.Scene) -> GeoToolsAnalyses:
    return scene.geotools_analyses


def active_item(scene: bpy.types.Scene | None) -> GeoToolsAnalysisItem | None:
    if scene is None:
        return None
    props = scene.geotools_analyses
    if 0 <= props.index < len(props.items):
        return props.items[props.index]
    return None


def item_objects(item: GeoToolsAnalysisItem) -> list[bpy.types.Object]:
    return [o.object for o in item.objects if o.object is not None]


def output_objects(item: GeoToolsAnalysisItem) -> list[bpy.types.Object]:
    return [o.object for o in item.outputs if o.object is not None]


def item_layer(scene: bpy.types.Scene, item: GeoToolsAnalysisItem | None):
    """The item's display layer in the registry, if it exists."""
    if item is None or not item.layer:
        return None
    return find_layer(scene, item.layer)


def tool_settings(item: GeoToolsAnalysisItem):
    attr = SETTINGS_ATTR.get(item.kind)
    return getattr(item, attr) if attr else None


# --- following the selection -----------------------------------------------------------


def applies_to(scene: bpy.types.Scene, item: GeoToolsAnalysisItem) -> list[bpy.types.Object]:
    """The meshes the result applies to: its own, and a plain layer's carriers too."""
    own = item_objects(item)
    if item.kind != "LAYER":
        return own
    layer = item_layer(scene, item)
    known = {o.as_pointer() for o in own}
    carriers = stored_carriers(layer) if layer is not None else []
    return own + [o for o in carriers if o.as_pointer() not in known]


def result_meshes(scene: bpy.types.Scene, item: GeoToolsAnalysisItem) -> list[bpy.types.Object]:
    """The objects that show the result in the list: its meshes and the objects it made."""
    return applies_to(scene, item) + output_objects(item)


def selection_pointers(context: bpy.types.Context) -> frozenset[int]:
    """The active object and the selected objects, as pointers (empty: nothing selected)."""
    view_layer = getattr(context, "view_layer", None)
    if view_layer is None:
        return frozenset()
    found = {o.as_pointer() for o in view_layer.objects if o.select_get()}
    if view_layer.objects.active is not None:
        found.add(view_layer.objects.active.as_pointer())
    return frozenset(found)


_cache: dict[str, object] = {"key": None, "flags": None}
_generation = [0]


def invalidate() -> None:
    """Forget the cached list filter (results or their meshes changed)."""
    _generation[0] += 1


def visible_flags(context: bpy.types.Context) -> list[bool]:
    """Which items the list shows, cached per selection, list and depsgraph update.

    Cheap enough for ``draw()``: pointers only, no mesh is read. With nothing
    selected, or Show set to All, every item shows.
    """
    scene = context.scene
    props = scene.geotools_analyses
    selection = selection_pointers(context)
    key = (scene.as_pointer(), props.show, len(props.items), selection, _generation[0])
    if _cache["key"] == key:
        return _cache["flags"]  # type: ignore[return-value]
    if props.show == "ALL" or not selection:
        flags = [True] * len(props.items)
    else:
        flags = [
            any(o.as_pointer() in selection for o in result_meshes(scene, item))
            for item in props.items
        ]
    _cache["key"], _cache["flags"] = key, flags
    return flags


def shown_item(context: bpy.types.Context) -> GeoToolsAnalysisItem | None:
    """The active item when the list shows it (the panels below the list draw it)."""
    props = context.scene.geotools_analyses
    item = active_item(context.scene)
    if item is None:
        return None
    flags = visible_flags(context)
    return item if props.index < len(flags) and flags[props.index] else None


def _kind_key(item: GeoToolsAnalysisItem | None) -> tuple[str, str]:
    return ("", "") if item is None else (item.kind, item.preset)


def follow_active(context: bpy.types.Context) -> None:
    """Keep the active row on the active object's result of the same kind, else its first."""
    scene = context.scene
    view_layer = getattr(context, "view_layer", None)
    if scene is None or view_layer is None or not hasattr(scene, "geotools_analyses"):
        return
    active = view_layer.objects.active
    if active is None:
        return
    props = scene.geotools_analyses
    pointer = active.as_pointer()
    mine = [
        i
        for i, item in enumerate(props.items)
        if any(o.as_pointer() == pointer for o in result_meshes(scene, item))
    ]
    if not mine or props.index in mine:
        return
    key = _kind_key(active_item(scene))
    same = [i for i in mine if _kind_key(props.items[i]) == key]
    props.index = (same or mine)[0]


_OWNER = object()
"""The msgbus subscription owner of the active-object watch."""


def _on_active_changed(*_args: object) -> None:
    try:
        follow_active(bpy.context)
    except Exception:  # a msgbus callback must never raise
        log.exception("Following the active object failed")


def _subscribe() -> None:
    bpy.msgbus.clear_by_owner(_OWNER)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.LayerObjects, "active"),
        owner=_OWNER,
        args=(),
        notify=_on_active_changed,
    )


# --- keeping a preset's modifiers in step ----------------------------------------------


SYNC_SKIP = frozenset({catalog.ATTRIBUTE_INPUT, catalog.CONVERGENCE_INPUT})
"""Inputs that differ per mesh by design (the attribute name, true north's angle)."""


def preset_modifiers_of(item: GeoToolsAnalysisItem) -> list[tuple[bpy.types.Object, object]]:
    """``(mesh, modifier)`` of a layer preset result on each of its meshes."""
    try:
        preset = catalog.get(item.preset)
    except KeyError:
        return []
    group = analysis_presets.find_group(preset.id, preset.version)
    if group is None or preset.output is not catalog.Output.LAYER:
        return []
    attribute = attribute_name(item.layer)
    found = []
    for obj in item_objects(item):
        modifier = analysis_presets.find_modifier(obj, group, attribute)
        if modifier is not None:
            found.append((obj, modifier))
    return found


def sync_inputs(source, targets) -> int:
    """Copy ``source``'s group inputs (but :data:`SYNC_SKIP`) to ``targets``; return changes."""
    sockets = [
        s
        for s in source.node_group.interface.items_tree
        if s.item_type == "SOCKET"
        and s.in_out == "INPUT"
        and s.socket_type != "NodeSocketGeometry"
        and s.name not in SYNC_SKIP
    ]
    changed = 0
    for target in targets:
        touched = False
        for socket in sockets:
            want = getattr(source.properties.inputs, socket.identifier, None)
            have = getattr(target.properties.inputs, socket.identifier, None)
            if want is None or have is None or have.value == want.value:
                continue
            have.value = want.value
            touched = True
            changed += 1
        if touched:
            target.id_data.update_tag()
    return changed


def sync_preset_inputs(
    scene: bpy.types.Scene, active: bpy.types.Object | None, updated: frozenset[int] | None
) -> int:
    """Bring every preset result's modifiers in step with the one the sidebar edits.

    That is the active mesh's modifier, else the first mesh's. ``updated``
    limits the work to results with an updated mesh (None: all of them).
    """
    changed = 0
    for item in scene.geotools_analyses.items:
        if item.kind != "PRESET" or len(item.objects) < 2:
            continue
        if updated is not None and not any(o.as_pointer() in updated for o in item_objects(item)):
            continue
        pairs = preset_modifiers_of(item)
        if len(pairs) < 2:
            continue
        source = next((m for o, m in pairs if o == active), pairs[0][1])
        changed += sync_inputs(source, [m for _o, m in pairs if m != source])
    return changed


@persistent
def _on_depsgraph_update(scene: bpy.types.Scene, depsgraph: bpy.types.Depsgraph) -> None:
    invalidate()
    if not hasattr(scene, "geotools_analyses") or not len(scene.geotools_analyses.items):
        return
    try:
        updated = frozenset(
            u.id.original.as_pointer()
            for u in depsgraph.updates
            if isinstance(u.id, bpy.types.Object)
        )
        if updated:
            sync_preset_inputs(scene, depsgraph.view_layer.objects.active, updated)
    except Exception:  # never break the depsgraph
        log.exception("Keeping the preset inputs in step failed")


# --- out of date -----------------------------------------------------------------------


_NOT_SETTINGS = frozenset(
    {"result", "error", "contour_result", "contour_error", "section_result", "section_error"}
)


def tool_signature(item: GeoToolsAnalysisItem) -> str:
    """The tool's settings and meshes as text (cheap: a few RNA reads, no mesh)."""
    group = tool_settings(item)
    if group is None:
        return ""
    parts = []
    for prop in group.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"} or name in _NOT_SETTINGS or prop.type == "COLLECTION":
            continue
        value = getattr(group, name)
        parts.append(f"{name}={getattr(value, 'name', value)!r}")
    parts.append("objects=" + ",".join(o.name for o in item_objects(item)))
    return ";".join(parts)


def out_of_date(item: GeoToolsAnalysisItem) -> bool:
    """Whether a computed tool's settings or meshes changed since its last run."""
    return item.kind in TOOL_KINDS and item.computed and item.signature != tool_signature(item)


# --- naming ----------------------------------------------------------------------------


def _claimed(scene: bpy.types.Scene) -> set[str]:
    names = {layer.name for layer in scene.geotools.layers}
    for item in scene.geotools_analyses.items:
        names.add(item.layer)
        names.update(entry.name for entry in item.layers)
        group = tool_settings(item)
        if group is not None:
            names.update(_planned_layers(item.kind, group))
    names.discard("")
    return names


def _free(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _free_object_name(base: str) -> str:
    if base not in bpy.data.objects:
        return base
    n = 2
    while f"{base} {n}" in bpy.data.objects:
        n += 1
    return f"{base} {n}"


def _planned_layers(kind: str, group) -> list[str]:
    """The layers a tool writes with these settings, the display layer first."""
    if kind == "HYDROLOGY":
        return [group.accumulation_layer, group.depth_layer, group.depression_layer]
    if kind == "SUN_HOURS":
        hours = [group.hours_layer]
        return [group.insolation_layer, *hours] if group.insolation else hours
    if kind == "LEAST_COST":
        if not group.make_layer:
            return []
        bands = [group.band_layer] if group.band_minutes > 0 else []
        return [group.time_layer, *bands]
    return []


# --- adding ----------------------------------------------------------------------------


def _selected_meshes(context: bpy.types.Context) -> list[bpy.types.Object]:
    return analysis_presets.target_objects(context)


def _new_item(scene: bpy.types.Scene, kind: str, label: str) -> GeoToolsAnalysisItem:
    props = scene.geotools_analyses
    item = props.items.add()
    item.kind, item.name = kind, label
    return item


def _select(scene: bpy.types.Scene, item: GeoToolsAnalysisItem) -> None:
    invalidate()
    props = scene.geotools_analyses
    props.index = next(i for i, other in enumerate(props.items) if other == item)


def _set_objects(collection, objects) -> None:
    collection.clear()
    for obj in objects:
        collection.add().object = obj


def _find_preset_item(scene, preset_id: str, layer: str) -> GeoToolsAnalysisItem | None:
    for item in scene.geotools_analyses.items:
        if item.kind == "PRESET" and item.preset == preset_id and item.layer == layer:
            return item
    return None


def add_entry(context: bpy.types.Context, key: str) -> GeoToolsAnalysisItem:
    """Add the picker entry ``key``; returns the (new or existing) item, made active."""
    scene = context.scene
    kind, _, rest = key.partition(":")
    if kind == "preset":
        preset_id, _, north = rest.partition(":")
        item = _add_preset(context, catalog.get(preset_id), "TRUE" if north else "GRID")
    elif kind == "tool":
        item = _add_tool(context, rest)
    elif kind == "layer":
        item = _add_layer(scene, rest)
        _set_objects(item.objects, _selected_meshes(context))
    else:
        raise AnalysesError(f"Unknown analysis {key!r}")
    _select(scene, item)
    return item


def _add_preset(context, preset: catalog.Preset, north: str) -> GeoToolsAnalysisItem:
    scene = context.scene
    objects = analysis_presets.preset_targets(context, preset)
    if not objects:
        raise AnalysesError("Select the meshes to analyse.")
    before = {o.as_pointer() for o in bpy.data.objects}
    name, _notes = analysis_presets.add_preset(context, preset, objects, north=north)
    if preset.output is catalog.Output.OBJECT:
        item = _new_item(scene, "PRESET", preset.label)
        item.preset = preset.id
        _set_objects(item.objects, objects)
        made = [o for o in bpy.data.objects if o.as_pointer() not in before]
        _set_objects(item.outputs, [o for o in made if o.name == name] or made)
        item.computed = True
        return item
    item = _find_preset_item(scene, preset.id, name)
    if item is None:
        item = _new_item(scene, "PRESET", preset.label)
        item.preset, item.layer, item.north = preset.id, name, north
        item.layers.add().name = name
    _extend(item, objects)
    item.computed = True
    return item


def _extend(item: GeoToolsAnalysisItem, objects: list[bpy.types.Object]) -> None:
    """Add ``objects`` to a preset result; new modifiers take the result's current inputs."""
    known = {o.as_pointer() for o in item_objects(item)}
    for obj in objects:
        if obj.as_pointer() not in known:
            item.objects.add().object = obj
    pairs = preset_modifiers_of(item)
    source = next((m for o, m in pairs if o.as_pointer() in known), None)
    if source is not None:
        sync_inputs(source, [m for o, m in pairs if o.as_pointer() not in known])
    invalidate()


_TOOL_LABELS = {
    "HYDROLOGY": "Hydrology",
    "SUN_HOURS": "Sun Hours",
    "INSOLATION": "Insolation",
    "LEAST_COST": "Least-Cost Routes",
    "CONTOURS": "Contours",
    "SECTIONS": "Sections",
}


def _add_tool(context, which: str) -> GeoToolsAnalysisItem:
    scene = context.scene
    kind = "SUN_HOURS" if which == "INSOLATION" else which
    taken = _claimed(scene)
    objects = _selected_meshes(context)
    item = _new_item(scene, kind, _TOOL_LABELS[which])
    group = tool_settings(item)
    if kind == "HYDROLOGY":
        group.surface = "SELECTED"
        for attr in ("accumulation_layer", "depth_layer", "depression_layer"):
            setattr(group, attr, _free(getattr(group, attr), taken))
    elif kind == "SUN_HOURS":
        group.surface = "SELECTED"
        group.insolation = which == "INSOLATION"
        group.hours_layer = _free(group.hours_layer, taken)
        group.insolation_layer = _free(group.insolation_layer, taken)
    elif kind == "LEAST_COST":
        group.surface = "SELECTED"
        group.time_layer = _free(group.time_layer, taken)
        group.band_layer = _free(group.band_layer, taken)
        objects = objects[:1]
    elif kind == "CONTOURS":
        group.surface = "SELECTED"
        group.contour_name = _free_object_name(group.contour_name)
    elif kind == "SECTIONS":
        group.surface = "SELECTED"
        group.section_name = _free_object_name(group.section_name)
        active = context.active_object
        if active is not None and active.type == "MESH" and len(objects) > 1:
            group.line = active
            objects = [o for o in objects if o != active]
    _set_objects(item.objects, objects)
    planned = _planned_layers(kind, group)
    item.layer = planned[0] if planned else ""
    for name in planned:
        item.layers.add().name = name
    return item


def _add_layer(scene: bpy.types.Scene, which: str) -> GeoToolsAnalysisItem:
    taken = _claimed(scene)
    if which == "TEXTURE":
        spec = LayerSpec(_free("bake", taken), Domain.TEXTURE, DataType.FLOAT, label="Bake")
        label = "Texture Bake"
    else:
        spec = LayerSpec(_free("layer", taken), Domain.FACE, DataType.FLOAT)
        label = "From Attribute"
    ensure_layer(scene, spec)
    item = _new_item(scene, "LAYER", label)
    item.layer = spec.name
    item.layers.add().name = spec.name
    item.computed = True
    return item


# --- the registry's other layers -------------------------------------------------------


def unlisted_layers(scene: bpy.types.Scene) -> list[str]:
    """Registry layers no item names (cheap enough for ``draw()``: names only)."""
    listed = set()
    for item in scene.geotools_analyses.items:
        listed.add(item.layer)
        listed.update(entry.name for entry in item.layers)
    return [layer.name for layer in scene.geotools.layers if layer.name not in listed]


def _carriers(preset: catalog.Preset, layer: str) -> list[bpy.types.Object]:
    group = analysis_presets.find_group(preset.id, preset.version)
    if group is None:
        return []
    attribute = attribute_name(layer)
    return [
        o
        for o in bpy.data.objects
        if o.type == "MESH" and analysis_presets.find_modifier(o, group, attribute) is not None
    ]


def list_unlisted_layers(scene: bpy.types.Scene) -> int:
    """Add an item for every unlisted registry layer; return how many were added.

    A preset's layer becomes a PRESET item with the meshes carrying its
    modifier; any other layer (a tool's too, for now) a plain LAYER item.
    """
    added = 0
    for name in unlisted_layers(scene):
        layer = find_layer(scene, name)
        provenance = spec_from_props(layer).provenance
        preset_id = catalog.preset_of(provenance)
        try:
            preset = catalog.get(preset_id) if preset_id else None
        except KeyError:
            preset = None
        label = layer.label.strip() or name
        if preset is not None:
            item = _new_item(scene, "PRESET", label)
            item.preset = preset.id
            north = (provenance or {}).get("parameters", {}).get("north")
            item.north = "TRUE" if north == "true" else "GRID"
            _set_objects(item.objects, _carriers(preset, name))
        else:
            item = _new_item(scene, "LAYER", label)
            try:
                refresh_carriers(scene, layer, sync_view=False)
            except (RuntimeError, ValueError, KeyError):
                log.exception("could not find the carriers of layer %s", name)
        item.layer = name
        item.layers.add().name = name
        item.computed = True
        added += 1
    if added:
        invalidate()
    return added


def list_all_scenes() -> None:
    for scene in bpy.data.scenes:
        try:
            if getattr(scene, "geotools_analyses", None) is not None:
                list_unlisted_layers(scene)
        except Exception:  # never break file loading
            log.exception("Listing the layers of scene %s failed", scene.name)


@persistent
def _on_load_post(*_args: object) -> None:
    invalidate()
    _subscribe()  # a file load drops msgbus subscriptions
    list_all_scenes()


def _after_register() -> None:
    list_all_scenes()


# --- running a tool --------------------------------------------------------------------


def copy_settings(source: bpy.types.PropertyGroup, target: bpy.types.PropertyGroup) -> None:
    """Copy every writable property of one settings group onto another of the same type."""
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"} or prop.is_readonly or prop.type == "COLLECTION":
            continue
        setattr(target, name, getattr(source, name))


_SCENE_SETTINGS = {
    "HYDROLOGY": hydrology.settings,
    "SUN_HOURS": lambda context: analysis.settings(context).sun_hours,
    "LEAST_COST": least_cost.settings,
    "CONTOURS": contours.settings,
    "SECTIONS": contours.settings,
}

_OPERATORS = {
    "HYDROLOGY": "hydrology",
    "SUN_HOURS": "sun_hours",
    "LEAST_COST": "least_cost",
    "CONTOURS": "contours",
    "SECTIONS": "sections",
}
"""The ``geotools.*`` operator each tool kind runs."""


@contextmanager
def _selected(context: bpy.types.Context, objects: list[bpy.types.Object]) -> Iterator[None]:
    """Select exactly ``objects`` (the first active) for the run, then put things back."""
    view_layer = context.view_layer
    before = [o for o in view_layer.objects if o.select_get()]
    active = view_layer.objects.active
    try:
        for obj in view_layer.objects:
            obj.select_set(obj in objects)
        if objects:
            view_layer.objects.active = objects[0]
        yield
    finally:
        for obj in view_layer.objects:
            obj.select_set(obj in before)
        view_layer.objects.active = active


def tool_error(item: GeoToolsAnalysisItem) -> str:
    group = tool_settings(item)
    if group is None:
        return ""
    if item.kind == "CONTOURS":
        return group.contour_error
    if item.kind == "SECTIONS":
        return group.section_error
    return group.error


def compute(context: bpy.types.Context, item: GeoToolsAnalysisItem) -> str:
    """Run the item's tool with its settings on its meshes; return the error, if any."""
    if item.kind not in TOOL_KINDS:
        raise AnalysesError(f"{item.name} has nothing to compute: it updates by itself.")
    objects = [o for o in item_objects(item) if o.name in context.view_layer.objects]
    if not objects:
        raise AnalysesError("It applies to no mesh: select the meshes, then Add Selected.")
    group = tool_settings(item)
    scene_group = _SCENE_SETTINGS[item.kind](context)
    name = _OPERATORS[item.kind]
    before = {o.as_pointer() for o in bpy.data.objects}
    copy_settings(group, scene_group)
    failed = ""
    try:
        with _selected(context, objects):
            getattr(bpy.ops.geotools, name)()
    except RuntimeError as exc:  # an error report (also in the settings) or a failed poll
        failed = str(exc).removeprefix("Error: ").strip()
    finally:
        copy_settings(scene_group, group)
    item.computed = True
    planned = _planned_layers(item.kind, group)
    item.layers.clear()
    for layer in planned:
        if find_layer(context.scene, layer) is not None:
            item.layers.add().name = layer
    item.layer = planned[0] if planned else ""
    keep = [o for o in output_objects(item) if o.name in bpy.data.objects]
    made = [o for o in bpy.data.objects if o.as_pointer() not in before]
    _set_objects(item.outputs, keep + [o for o in made if o not in keep])
    item.signature = tool_signature(item)
    invalidate()
    _index_changed(context.scene.geotools_analyses, context)
    return tool_error(item) or failed


# --- operators -------------------------------------------------------------------------


def _active_poll(cls, context: bpy.types.Context) -> bool:
    if active_item(context.scene) is None:
        cls.poll_message_set("Select an analysis in the list")
        return False
    return True


class GEOTOOLS_OT_analyses_compute(bpy.types.Operator):
    """Run the selected analysis again with its inputs, on its meshes"""

    bl_idname = "geotools.analyses_compute"
    bl_label = "Compute"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _active_poll(cls, context):
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        return active_item(context.scene).kind in TOOL_KINDS

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = active_item(context.scene)
        try:
            error = compute(context, item)
        except AnalysesError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        redraw_ui(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        self.report({"INFO"}, f"{item.name}: computed")
        return {"FINISHED"}


def _selection_poll(cls, context: bpy.types.Context) -> bool:
    if not _active_poll(cls, context):
        return False
    item = active_item(context.scene)
    if item.kind == "LAYER":
        cls.poll_message_set("A plain layer applies to every mesh that carries its attribute")
        return False
    if item.kind == "PRESET" and catalog.get(item.preset).output is catalog.Output.OBJECT:
        cls.poll_message_set("A preset that makes lines works on one surface: add it again")
        return False
    if not _selected_meshes(context):
        cls.poll_message_set("Select the meshes first")
        return False
    return True


class GEOTOOLS_OT_analyses_add_selected(bpy.types.Operator):
    """Apply the selected result to the selected meshes as well (a preset: its modifier, with the result's inputs)"""

    bl_idname = "geotools.analyses_add_selected"
    bl_label = "Add Selected"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _selection_poll(cls, context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = active_item(context.scene)
        objects = _selected_meshes(context)
        if item.kind == "PRESET":
            try:
                analysis_presets.add_preset(
                    context,
                    catalog.get(item.preset),
                    objects,
                    name=item.layer,
                    north=item.north or "GRID",
                )
            except analysis_presets.PresetError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            _extend(item, objects)
        elif item.kind == "LEAST_COST":  # one surface
            _set_objects(item.objects, objects[:1])
        else:
            known = {o.as_pointer() for o in item_objects(item)}
            for obj in objects:
                if obj.as_pointer() not in known:
                    item.objects.add().object = obj
        invalidate()
        self.report({"INFO"}, f"{item.name} applies to {_summary(item_objects(item))}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_remove_selected(bpy.types.Operator):
    """Stop applying the selected result to the selected meshes (a preset: remove its modifier from them)"""

    bl_idname = "geotools.analyses_remove_selected"
    bl_label = "Remove Selected"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _selection_poll(cls, context):
            return False
        mine = {o.as_pointer() for o in item_objects(active_item(context.scene))}
        if not any(o.as_pointer() in mine for o in _selected_meshes(context)):
            cls.poll_message_set("None of the selected meshes has this result")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = active_item(context.scene)
        gone = {o.as_pointer() for o in _selected_meshes(context)}
        if item.kind == "PRESET":
            for obj, modifier in preset_modifiers_of(item):
                if obj.as_pointer() in gone:
                    obj.modifiers.remove(modifier)
        _set_objects(item.objects, [o for o in item_objects(item) if o.as_pointer() not in gone])
        invalidate()
        self.report({"INFO"}, f"{item.name} applies to {_summary(item_objects(item))}")
        redraw_ui(context)
        return {"FINISHED"}


def _summary(objects: list[bpy.types.Object]) -> str:
    """``hills (+2)``, or ``no mesh``."""
    if not objects:
        return "no mesh"
    more = f" (+{len(objects) - 1})" if len(objects) > 1 else ""
    return f"{objects[0].name}{more}"


class GEOTOOLS_OT_analyses_remove(bpy.types.Operator):
    """Remove the selected analysis: its list entry, its modifiers and its layers"""

    bl_idname = "geotools.analyses_remove"
    bl_label = "Remove Analysis"
    bl_options = {"REGISTER", "UNDO"}

    delete_data: BoolProperty(
        name="Delete Attributes and Images",
        description="Also delete the layers' attributes and images (attributes made by "
        "modifiers go with the modifier)",
        default=False,
        options={"SKIP_SAVE"},
    )
    delete_objects: BoolProperty(
        name="Delete These Objects",
        description="Also delete the objects the analysis made (lines, routes, points), "
        "listed above",
        default=True,
        options={"SKIP_SAVE"},
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        return _active_poll(cls, context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        return context.window_manager.invoke_props_dialog(self, confirm_text="Remove")

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = active_item(context.scene)
        if item is None:
            return
        layout.label(text=f"Remove {item.name}?")
        meshes = applies_to(context.scene, item)
        if item.kind == "PRESET" and meshes:
            layout.label(text=f"Its modifier on {_summary(meshes)}", icon="MODIFIER")
        for entry in item.layers:
            layout.label(text=f"Layer {entry.name}", icon="RNA")
        made = output_objects(item)
        if made:
            col = layout.column(align=True)
            col.label(text="It made these objects:")
            for obj in made[:8]:
                col.label(text=obj.name, icon="OUTLINER_OB_MESH")
            if len(made) > 8:
                col.label(text=f"... and {len(made) - 8} more")
            layout.prop(self, "delete_objects")
        layout.prop(self, "delete_data")

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        props = scene.geotools_analyses
        item = active_item(scene)
        name = item.name
        if item.kind == "PRESET" and item.layer:
            preset = catalog.get(item.preset)
            group = analysis_presets.find_group(preset.id, preset.version)
            attribute = attribute_name(item.layer)
            for obj in item_objects(item):
                modifier = analysis_presets.find_modifier(obj, group, attribute) if group else None
                if modifier is not None:
                    obj.modifiers.remove(modifier)
        for entry in item.layers:
            layer = find_layer(scene, entry.name)
            if layer is None:
                continue
            if self.delete_data:
                try:
                    remove_layer_data(scene, layer)
                except RuntimeError as ex:
                    self.report({"ERROR"}, f"Cannot delete the data of {entry.name}: {ex}")
                    return {"CANCELLED"}
            layer.presentation.show_in_viewport = False
            scene.geotools.layers.remove(scene.geotools.layers.find(entry.name))
        if self.delete_objects:
            for obj in output_objects(item):
                bpy.data.objects.remove(obj)
        index = props.index
        props.items.remove(index)
        invalidate()
        props.index = min(index, len(props.items) - 1) if index else 0
        scene.geotools.layers_index = min(
            scene.geotools.layers_index, max(len(scene.geotools.layers) - 1, 0)
        )
        self.report({"INFO"}, f"Removed {name}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_list_layers(bpy.types.Operator):
    """Add the registry's layers that are not in the Analyses list to it"""

    bl_idname = "geotools.analyses_list_layers"
    bl_label = "List Them"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        count = list_unlisted_layers(context.scene)
        self.report({"INFO"}, f"Listed {count} layer{'s' * (count != 1)}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_show_layer(bpy.types.Operator):
    """Show this layer in Display and as the analysis' variant"""

    bl_idname = "geotools.analyses_show_layer"
    bl_label = "Display This Layer"
    bl_options = {"REGISTER", "UNDO"}

    layer: StringProperty(name="Layer", description="The layer to display")

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = active_item(context.scene)
        if item is None or find_layer(context.scene, self.layer) is None:
            return {"CANCELLED"}
        item.layer = self.layer
        _index_changed(context.scene.geotools_analyses, context)
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_show(bpy.types.Operator):
    """Show this layer in the viewport in its Display colours, or take the view off (Show in Viewport)"""

    bl_idname = "geotools.analyses_show"
    bl_label = "Show in Viewport"
    bl_options = {"REGISTER", "UNDO"}

    layer: StringProperty(name="Layer", description="The layer to show or hide")

    def execute(self, context: bpy.types.Context) -> set[str]:
        layers = context.scene.geotools.layers
        index = layers.find(self.layer)
        if index < 0:
            return {"CANCELLED"}
        context.scene.geotools.layers_index = index
        try:
            return bpy.ops.geotools.layer_show()
        except RuntimeError as exc:  # its report, raised by bpy.ops from Python
            self.report({"WARNING"}, str(exc).removeprefix("Error: ").strip())
            return {"CANCELLED"}


class GEOTOOLS_OT_analyses_select_object(bpy.types.Operator):
    """Select this object and make it active"""

    bl_idname = "geotools.analyses_select_object"
    bl_label = "Select Object"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Object", description="The object to select")

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = bpy.data.objects.get(self.name)
        if obj is None or obj.name not in context.view_layer.objects:
            return {"CANCELLED"}
        for other in context.view_layer.objects:
            other.select_set(other == obj)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_publish_variant(bpy.types.Operator):
    """Publish the selected analysis' layer as the active build profile's variant"""

    bl_idname = "geotools.analyses_publish_variant"
    bl_label = "Publish as Variant"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _active_poll(cls, context):
            return False
        props = context.scene.geotools
        if not 0 <= props.build_profile_index < len(props.build_profiles):
            cls.poll_message_set("Add a build profile in Publish first")
            return False
        if item_layer(context.scene, active_item(context.scene)) is None:
            cls.poll_message_set("This analysis has no layer")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        profile = props.build_profiles[props.build_profile_index]
        item = active_item(context.scene)
        profile.variant_layer = item.layer
        self.report({"INFO"}, f"{profile.name}: variant {item.layer}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_UL_analyses(bpy.types.UIList):
    """Analyses: kind icon, name, units (or Not Run) and Show in Viewport."""

    bl_idname = "GEOTOOLS_UL_analyses"

    def draw_item(
        self,
        context: bpy.types.Context,
        layout: bpy.types.UILayout,
        data: bpy.types.AnyType,
        item: GeoToolsAnalysisItem,
        icon: int,
        active_data: bpy.types.AnyType,
        active_property: str,
        index: int = 0,
        flt_flag: int = 0,
    ) -> None:
        layer = item_layer(context.scene, item)
        icon = KIND_ICONS.get(item.kind)
        if icon is None:
            icon = DOMAIN_ICONS.get(layer.domain, "NONE") if layer is not None else "NONE"
        row = layout.row(align=True)
        row.label(text=item.name, icon=icon)
        right = row.row(align=True)
        right.alignment = "RIGHT"
        if item.kind in TOOL_KINDS and not item.computed:
            sub = right.row()
            sub.active = False
            sub.label(text="Not run")
        elif out_of_date(item):
            right.label(text="", icon="FILE_REFRESH")
        elif layer is not None and layer.units.strip():
            right.label(text=layer.units.strip())
        if layer is not None:
            shown = layer.presentation.show_in_viewport
            right.operator(
                "geotools.analyses_show",
                text="",
                icon="HIDE_OFF" if shown else "HIDE_ON",
                emboss=False,
            ).layer = layer.name
        else:
            right.label(text="", icon="BLANK1")

    def filter_items(self, context: bpy.types.Context, data, propname: str):
        """Show the results on the selection (:func:`visible_flags`), then the name filter."""
        items = getattr(data, propname)
        flags = visible_flags(context)
        if self.filter_name:
            named = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items, "name"
            )
        else:
            named = [self.bitflag_filter_item] * len(items)
        shown = [
            self.bitflag_filter_item if flags[i] and named[i] else 0 for i in range(len(items))
        ]
        return shown, []


classes = (
    GeoToolsAnalysisObject,
    GeoToolsAnalysisLayer,
    GeoToolsAnalysisItem,
    GeoToolsAnalyses,
    GEOTOOLS_OT_analyses_compute,
    GEOTOOLS_OT_analyses_add_selected,
    GEOTOOLS_OT_analyses_remove_selected,
    GEOTOOLS_OT_analyses_remove,
    GEOTOOLS_OT_analyses_list_layers,
    GEOTOOLS_OT_analyses_show_layer,
    GEOTOOLS_OT_analyses_show,
    GEOTOOLS_OT_analyses_select_object,
    GEOTOOLS_OT_analyses_publish_variant,
    GEOTOOLS_UL_analyses,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools_analyses = PointerProperty(type=GeoToolsAnalyses)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    _subscribe()
    if not bpy.app.timers.is_registered(_after_register):
        bpy.app.timers.register(_after_register, first_interval=0.0)


def unregister() -> None:
    if bpy.app.timers.is_registered(_after_register):
        bpy.app.timers.unregister(_after_register)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    bpy.msgbus.clear_by_owner(_OWNER)
    if hasattr(bpy.types.Scene, "geotools_analyses"):
        del bpy.types.Scene.geotools_analyses
    _unregister_classes()
