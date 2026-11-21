"""The Analyses list: every analysis result and layer of a mesh, like its modifier stack.

``scene.geotools_analyses.items`` holds one **result** per mesh: each item
belongs to exactly one mesh object (``item.object``), as a modifier belongs to
one object. The Analyses panel (``analyses_ui``) lists the active object's
results only, and the panels below the list draw the active row. Kinds:

* ``PRESET``: a Geometry Nodes preset (``analysis_presets.add_preset``). A
  layer preset is one modifier on the mesh, storing the layer ``item.layer``;
  the sidebar draws that modifier's own inputs, so they live in one place.
  Two meshes with Slope have two results and two modifiers, each with its own
  inputs, storing the same layer. An object preset (lines) belongs to the
  surface it was added to; the new object is in ``outputs``.
* ``FAST``: a Fast Bake (``preset_bake.compute``), including the bake-only
  Sky Visibility, Sky View (Cosine) and Openness (3D). Its inputs are the
  item's own (:class:`GeoToolsFastSettings`).
* ``HYDROLOGY``, ``SUN_HOURS``, ``LEAST_COST``, ``CONTOURS``, ``SECTIONS``: a
  Python tool, with its own copy of the tool's settings group (the type the
  tool's operator reads from the scene). **Compute** (:func:`compute`) copies
  the item's settings onto the scene's group, runs the tool's operator and
  copies them back with the result.
* ``LAYER``: a plain layer (From Attribute, Texture Bake, a GeoTIFF, values
  written by a script): one row per mesh that carries it.

FAST and tool results run on their own mesh unless their **Surface**
(``item.extent``) says Selected Objects or Source Collection: hydrology and
least-cost routes need a connected surface, and a sky bake's other meshes
occlude. The result still belongs to its mesh; the values the run writes on
the other meshes show there as plain layer rows. Each such result remembers
a signature of its inputs at its last run, so the panel can say it is out of
date (:func:`out_of_date`). Sun Hours keeps its own start date; the time zone
and site stay the project's (Tools > Sun), as they describe the place.

:func:`sync` keeps the list in step with the scene: it drops results whose
mesh, modifier or layer is gone, lists a preset modifier or object added by
a script, and adds a plain layer row for each mesh a layer's stored carriers
(``layers.stored_carriers``) name. A depsgraph handler runs it when a cheap
key of the scene changes (:func:`scene_key`). Files from before the list are
migrated once on load (:func:`migrate`): their Fast Bake and tool layers
(with the scene's last tool settings) and their tool line objects become
results; :func:`sync` lists the rest.

When the active object changes (a msgbus subscription), the active row moves
to that mesh's result of the same kind, else to its first (:func:`follow_active`).
"""

from __future__ import annotations

import json
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
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..analysis import preset_bake as fast
from ..analysis import presets as catalog
from ..core.layers import DataType, Domain, LayerSpec, attribute_name
from . import analysis, analysis_presets, contours, hydrology, least_cost
from .common import redraw_ui
from .layers import (
    DOMAIN_ICONS,
    ensure_layer,
    find_layer,
    refresh_carriers,
    remove_attribute,
    source_objects,
    spec_from_props,
    stored_carriers,
)
from .surface import OUTPUT_TOOL_PROP

log = logging.getLogger(__name__)

KIND_ITEMS = (
    ("PRESET", "Preset", "A Geometry Nodes analysis preset: live, edited on its modifier"),
    ("FAST", "Fast Bake", "Values computed once by a Cycles bake into a layer"),
    ("HYDROLOGY", "Hydrology", "Flow accumulation, ponding and depressions"),
    ("SUN_HOURS", "Sun Hours", "Hours of direct sun and clear-sky insolation"),
    ("LEAST_COST", "Least-Cost Routes", "Least-cost routes and travel times"),
    ("CONTOURS", "Contours", "Contour lines"),
    ("SECTIONS", "Sections", "Section lines"),
    ("LAYER", "Layer", "A plain layer: an attribute, a texture bake or imported values"),
)

TOOL_KINDS = frozenset({"HYDROLOGY", "SUN_HOURS", "LEAST_COST", "CONTOURS", "SECTIONS"})
RUN_KINDS = TOOL_KINDS | {"FAST"}
"""Kinds that run when asked (Compute), rather than updating by themselves."""

KIND_ICONS = {
    "PRESET": "GEOMETRY_NODES",
    "FAST": "RENDER_STILL",
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
    "FAST": "fast",
}
"""The item's own settings group for each kind that runs."""

TOOL_PROVENANCE = {
    hydrology.TOOL: "HYDROLOGY",
    "sun_hours": "SUN_HOURS",
    least_cost.TOOL: "LEAST_COST",
}
"""Provenance tools whose layers :func:`migrate` turns into tool results."""

OUTPUT_KINDS = {
    contours.CONTOUR_TOOL: "CONTOURS",
    contours.SECTION_TOOL: "SECTIONS",
    hydrology.TOOL: "HYDROLOGY",
    least_cost.TOOL: "LEAST_COST",
}
"""``surface.OUTPUT_TOOL_PROP`` values of the tools' line and point objects."""

EXTENT_ITEMS = (
    ("MESH", "This Mesh", "Run on this result's mesh only"),
    (
        "SELECTED",
        "Selected Objects",
        (
            "Run on this mesh and the meshes selected when you compute, as one surface (they "
            "get the values too); the result stays this mesh's"
        ),
    ),
    (
        "SOURCE",
        "Source Collection",
        (
            "Run on every mesh of the source collection, as one surface (the whole site); the "
            "result stays this mesh's"
        ),
    ),
)

MIGRATED = 1
"""``GeoToolsAnalyses.version`` once a file's older layers have been migrated."""


class AnalysesError(RuntimeError):
    """The analysis could not be added, run or removed; the message says why."""


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

    key: str  # "preset:<id>[:true]", "fast:<id>", "tool:<KIND>", "layer:ATTRIBUTE|TEXTURE"
    group: str
    label: str
    description: str

    @property
    def text(self) -> str:
        """What the search list shows and the field stores until the add runs."""
        return f"{self.group} > {self.label}"


def _group(preset: catalog.Preset) -> str:
    return PRESET_GROUP.get(preset.id, GROUP_SHORT[preset.category])


def _entries() -> tuple[Entry, ...]:
    found: list[Entry] = []
    for preset in catalog.PRESETS:
        group = _group(preset)
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
        if fast.is_fast(preset.id):
            found.append(
                Entry(
                    f"fast:{preset.id}",
                    group,
                    f"{preset.label} (Fast Bake)",
                    f"{preset.description}, computed once by a Cycles bake",
                )
            )
    for preset in fast.BAKE_ONLY:
        found.append(Entry(f"fast:{preset.id}", _group(preset), preset.label, preset.description))
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
        Entry("layer:TEXTURE", "Layers", "Texture Bake", "A texture layer filled by a Cycles bake"),
    ]
    return tuple(sorted(found, key=lambda e: GROUP_ORDER.index(e.group)))


ENTRIES = _entries()
_BY_TEXT = {e.text: e for e in ENTRIES}


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
        added = add_entry(context, entry.key)
    except (AnalysesError, analysis_presets.PresetError) as exc:
        self.error = str(exc)
        return
    self.error = ""
    count = len(added)
    self.note = f"Added {added[0].name} to {count} mesh{'es' * (count != 1)}"
    redraw_ui(context)


# --- property groups -------------------------------------------------------------------


class GeoToolsAnalysisObject(bpy.types.PropertyGroup):
    """An object an analysis made."""

    object: PointerProperty(name="Object", description="The object", type=bpy.types.Object)


class GeoToolsAnalysisLayer(bpy.types.PropertyGroup):
    """A layer an analysis writes (``name`` is the layer's name)."""


class GeoToolsFastSettings(bpy.types.PropertyGroup):
    """A Fast Bake result's inputs (the preset's own, by ``FAST_FIELDS``)."""

    max_distance: FloatProperty(
        name="Max Distance",
        description="How far the horizon is searched along each direction (direct visibility: "
        "the length of each ray; the viewshed: the horizontal reach of the view)",
        min=0.0,
        subtype="DISTANCE",
    )
    directions: IntProperty(
        name="Directions", description="Azimuths searched, evenly spaced from +Y", min=1, max=64
    )
    steps: IntProperty(
        name="Steps", description="Rays per direction that bisect the horizon angle", min=1, max=16
    )
    ray_offset: FloatProperty(
        name="Ray Offset",
        description="Rays start this far from the surface",
        min=0.0,
        subtype="DISTANCE",
        precision=4,
    )
    rays: IntProperty(
        name="Rays",
        description="Sky directions cast from each face, evenly spread (equal solid angle)",
        min=16,
        max=4096,
    )
    observer_height: FloatProperty(
        name="Observer Height",
        description="Eye height above each observer point",
        min=0.0,
        subtype="DISTANCE",
    )
    target_height: FloatProperty(
        name="Target Height",
        description="Height above the surface that must be seen (0: the surface itself, seen "
        "from the front only)",
        min=0.0,
        subtype="DISTANCE",
    )
    drape: BoolProperty(
        name="Drape",
        description="Drop the observer points vertically onto the topmost surface first",
    )
    observers: PointerProperty(
        name="Observers",
        description="The object whose vertices (or origin) are the observer points",
        type=bpy.types.Object,
    )
    occluders: PointerProperty(
        name="Occluders",
        description="A collection of other objects that block sight as well (optional)",
        type=bpy.types.Collection,
    )
    result: StringProperty(default="")
    error: StringProperty(default="")


FAST_FIELDS = {
    "Max Distance": "max_distance",
    "Directions": "directions",
    "Steps": "steps",
    "Ray Offset": "ray_offset",
    "Rays": "rays",
    "Observer Height": "observer_height",
    "Target Height": "target_height",
    "Drape": "drape",
}
"""The fast path's input names and the :class:`GeoToolsFastSettings` property of each."""


def _index_changed(self, context: bpy.types.Context) -> None:
    item = active_item(context.scene)
    if item is None or not item.layer:
        return
    index = context.scene.geotools.layers.find(item.layer)
    if index >= 0:
        context.scene.geotools.layers_index = index


class GeoToolsAnalysisItem(bpy.types.PropertyGroup):
    """One result of the Analyses list (``name`` is what the list shows)."""

    object: PointerProperty(
        name="Mesh", description="The mesh this result belongs to", type=bpy.types.Object
    )
    kind: EnumProperty(name="Kind", description="What made this result", items=KIND_ITEMS)
    preset: StringProperty(name="Preset", description="The preset's id (presets, Fast Bake)")
    north: StringProperty(
        name="North", description="TRUE when the preset's aspect is from true north"
    )
    layer: StringProperty(
        name="Layer", description="The layer shown in Display and published as a variant"
    )
    layers: CollectionProperty(type=GeoToolsAnalysisLayer)
    outputs: CollectionProperty(type=GeoToolsAnalysisObject)
    auto: BoolProperty(
        name="Listed Automatically",
        description="A plain layer row listed because the mesh carries the layer",
    )
    extent: EnumProperty(
        name="Surface",
        description="The meshes the run covers",
        items=EXTENT_ITEMS,
        default="MESH",
    )
    computed: BoolProperty(
        name="Computed", description="Whether the result has run since it was added"
    )
    signature: StringProperty(
        name="Signature",
        description="The inputs at the last run (out of date when they differ)",
    )
    sun_year: IntProperty(
        name="Year", description="Year of the first day", default=2026, min=1, max=9999
    )
    sun_month: IntProperty(
        name="Month", description="Month of the first day", default=6, min=1, max=12
    )
    sun_day: IntProperty(name="Day", description="The first day", default=21, min=1, max=31)
    hydrology: PointerProperty(type=hydrology.GeoToolsHydrologySettings)
    sun_hours: PointerProperty(type=analysis.GeoToolsSunHoursSettings)
    least_cost: PointerProperty(type=least_cost.GeoToolsLeastCostSettings)
    contours: PointerProperty(type=contours.GeoToolsContourSettings)
    fast: PointerProperty(type=GeoToolsFastSettings)


class GeoToolsAnalyses(bpy.types.PropertyGroup):
    """The scene's Analyses list (``scene.geotools_analyses``)."""

    items: CollectionProperty(type=GeoToolsAnalysisItem)
    index: IntProperty(
        name="Active Analysis",
        description="The result whose inputs, display and outputs the panel shows",
        default=0,
        update=_index_changed,
    )
    add_search: StringProperty(
        name="Add Analysis",
        description="Search the analyses (presets, Fast Bakes, tools and layers) and add one "
        "to each selected mesh",
        search=_search,
        search_options=set(),
        update=_picked,
        options={"SKIP_SAVE"},
    )
    version: IntProperty(default=0)
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


def active_mesh(context: bpy.types.Context) -> bpy.types.Object | None:
    """The active object when it is a mesh: whose results the list shows."""
    obj = getattr(context, "active_object", None)
    return obj if obj is not None and obj.type == "MESH" else None


def shown_item(context: bpy.types.Context) -> GeoToolsAnalysisItem | None:
    """The active row when it belongs to the active mesh (what Inputs, Display, Outputs draw)."""
    item = active_item(context.scene)
    mesh = active_mesh(context)
    if item is None or mesh is None or item.object != mesh:
        return None
    return item


def results_of(scene: bpy.types.Scene, obj: bpy.types.Object | None) -> list[int]:
    """The indices of ``obj``'s results, in list order."""
    if obj is None:
        return []
    return [i for i, item in enumerate(scene.geotools_analyses.items) if item.object == obj]


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


def preset_of(item: GeoToolsAnalysisItem) -> catalog.Preset | None:
    try:
        return fast.get(item.preset) if item.preset else None
    except KeyError:
        return None


def item_modifier(item: GeoToolsAnalysisItem) -> bpy.types.Modifier | None:
    """A preset result's modifier: on its mesh (a layer preset) or its new object."""
    preset = preset_of(item)
    if preset is None or item.kind != "PRESET":
        return None
    group = analysis_presets.find_group(preset.id, preset.version)
    if group is None:
        return None
    if preset.output is catalog.Output.OBJECT:
        for obj in output_objects(item):
            for modifier in obj.modifiers:
                if modifier.type == "NODES" and modifier.node_group == group:
                    return modifier
        return None
    if item.object is None:
        return None
    return analysis_presets.find_modifier(item.object, group, attribute_name(item.layer))


# --- following the active object -------------------------------------------------------


def _kind_key(item: GeoToolsAnalysisItem | None) -> tuple[str, str]:
    if item is None:
        return ("", "")
    return (item.kind, item.layer if item.kind == "LAYER" else item.preset)


def follow_active(context: bpy.types.Context) -> None:
    """Move the active row to the active mesh's result of the same kind, else its first."""
    scene = getattr(context, "scene", None)
    if scene is None or not hasattr(scene, "geotools_analyses"):
        return
    mine = results_of(scene, active_mesh(context))
    props = scene.geotools_analyses
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


# --- naming ----------------------------------------------------------------------------


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


def _layers_of(scene: bpy.types.Scene, obj: bpy.types.Object, kind: str = "") -> set[str]:
    """The layers ``obj``'s results write (of one kind, or of all)."""
    names: set[str] = set()
    for i in results_of(scene, obj):
        item = scene.geotools_analyses.items[i]
        if kind and item.kind != kind:
            continue
        names.add(item.layer)
        names.update(entry.name for entry in item.layers)
        group = tool_settings(item)
        if group is not None and item.kind in TOOL_KINDS:
            names.update(planned_layers(item.kind, group))
    names.discard("")
    return names


def planned_layers(kind: str, group) -> list[str]:
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


def _new_item(scene, obj, kind: str, label: str) -> GeoToolsAnalysisItem:
    item = scene.geotools_analyses.items.add()
    item.object, item.kind, item.name = obj, kind, label
    return item


def _set_layers(item: GeoToolsAnalysisItem, names: list[str]) -> None:
    item.layers.clear()
    for name in names:
        item.layers.add().name = name
    item.layer = names[0] if names else ""


def _set_outputs(item: GeoToolsAnalysisItem, objects) -> None:
    item.outputs.clear()
    for obj in objects:
        item.outputs.add().object = obj


def _select(scene: bpy.types.Scene, item: GeoToolsAnalysisItem) -> None:
    props = scene.geotools_analyses
    props.index = next(i for i, other in enumerate(props.items) if other == item)


def add_entry(context: bpy.types.Context, key: str) -> list[GeoToolsAnalysisItem]:
    """Add the picker entry ``key``: one result per selected mesh. Returns them; the active
    mesh's (else the first) becomes the active row."""
    scene = context.scene
    kind, _, rest = key.partition(":")
    with _quiet(scene):
        if kind == "preset":
            preset_id, _, north = rest.partition(":")
            added = _add_preset(context, catalog.get(preset_id), "TRUE" if north else "GRID")
        elif kind == "fast":
            added = _add_fast(context, fast.get(rest))
        elif kind == "tool":
            added = _add_tool(context, rest)
        elif kind == "layer":
            added = _add_layer(context, rest)
        else:
            raise AnalysesError(f"Unknown analysis {key!r}")
    mesh = active_mesh(context)
    _select(scene, next((i for i in added if i.object == mesh), added[0]))
    return added


def _targets(context: bpy.types.Context) -> list[bpy.types.Object]:
    objects = _selected_meshes(context)
    if not objects:
        raise AnalysesError("Select the meshes to analyse.")
    return objects


def _preset_layer(scene, obj, preset: catalog.Preset) -> str:
    """The layer a new preset result on ``obj`` stores: the preset's, or ``_2``... when the
    mesh already has it or the name belongs to another kind of layer."""
    mine = _layers_of(scene, obj)
    n, name = 1, preset.layer
    while True:
        layer = find_layer(scene, name)
        foreign = (
            layer is not None and catalog.preset_of(spec_from_props(layer).provenance) != preset.id
        )
        if name not in mine and not foreign:
            return name
        n += 1
        name = f"{preset.layer}_{n}"


def _add_preset(context, preset: catalog.Preset, north: str) -> list[GeoToolsAnalysisItem]:
    scene = context.scene
    objects = analysis_presets.preset_targets(context, preset)
    if not objects:
        raise AnalysesError("Select the meshes to analyse.")
    added = []
    if preset.output is catalog.Output.OBJECT:
        before = {o.as_pointer() for o in bpy.data.objects}
        name, _notes = analysis_presets.add_preset(context, preset, objects, north=north)
        item = _new_item(scene, objects[0], "PRESET", preset.label)
        item.preset, item.computed = preset.id, True
        made = [o for o in bpy.data.objects if o.as_pointer() not in before]
        _set_outputs(item, [o for o in made if o.name == name] or made)
        return [item]
    for obj in objects:
        name = _preset_layer(scene, obj, preset)
        analysis_presets.add_preset(context, preset, [obj], name=name, north=north)
        item = _new_item(scene, obj, "PRESET", preset.label)
        item.preset, item.north, item.computed = preset.id, north, True
        _set_layers(item, [name])
        added.append(item)
    return added


def _add_fast(context, preset: catalog.Preset) -> list[GeoToolsAnalysisItem]:
    scene = context.scene
    objects = analysis_presets.preset_targets(context, preset)
    if not objects:
        raise AnalysesError("Select the meshes to analyse.")
    base = preset.layer if fast.is_bake_only(preset.id) else f"{preset.layer}_fast"
    observers = None
    if preset.id in fast.VIEWSHED:
        fills, _notes = analysis_presets.fill_inputs(context, preset, objects)
        observers = fills.get("Observers")
    label = preset.label if fast.is_bake_only(preset.id) else f"{preset.label} (Fast)"
    added = []
    for obj in objects:
        item = _new_item(scene, obj, "FAST", label)
        item.preset = preset.id
        for name, value in fast.default_inputs(preset.id).items():
            setattr(item.fast, FAST_FIELDS[name], value)
        item.fast.observers = observers
        _set_layers(item, [_free(base, _layers_of(scene, obj))])
        added.append(item)
    return added


_TOOL_LABELS = {
    "HYDROLOGY": "Hydrology",
    "SUN_HOURS": "Sun Hours",
    "INSOLATION": "Insolation",
    "LEAST_COST": "Least-Cost Routes",
    "CONTOURS": "Contours",
    "SECTIONS": "Sections",
}

_LAYER_FIELDS = {
    "HYDROLOGY": ("accumulation_layer", "depth_layer", "depression_layer"),
    "SUN_HOURS": ("hours_layer", "insolation_layer"),
    "LEAST_COST": ("time_layer", "band_layer"),
}


def _add_tool(context, which: str) -> list[GeoToolsAnalysisItem]:
    scene = context.scene
    kind = "SUN_HOURS" if which == "INSOLATION" else which
    objects = _targets(context)
    line = None
    if kind == "SECTIONS":  # a selected mesh without faces is the section line
        lines = [o for o in objects if not len(o.data.polygons)]
        line = lines[0] if lines else None
        objects = [o for o in objects if o not in lines] or objects
    sun = analysis.settings(context).sun
    added = []
    for obj in objects:
        mine = _layers_of(scene, obj, kind)
        item = _new_item(scene, obj, kind, _TOOL_LABELS[which])
        group = tool_settings(item)
        for attr in _LAYER_FIELDS.get(kind, ()):
            setattr(group, attr, _free(getattr(group, attr), mine))
        if kind == "SUN_HOURS":
            group.insolation = which == "INSOLATION"
            item.sun_year, item.sun_month, item.sun_day = sun.year, sun.month, sun.day
        elif kind == "CONTOURS":
            group.contour_name = _free_object_name(f"Contours {obj.name}")
        elif kind == "SECTIONS":
            group.section_name = _free_object_name(f"Sections {obj.name}")
            group.line = line
        _set_layers(item, planned_layers(kind, group))
        added.append(item)
    return added


def _add_layer(context, which: str) -> list[GeoToolsAnalysisItem]:
    scene = context.scene
    objects = _targets(context)
    taken = {layer.name for layer in scene.geotools.layers}
    if which == "TEXTURE":
        spec = LayerSpec(_free("bake", taken), Domain.TEXTURE, DataType.FLOAT, label="Bake")
        label = "Texture Bake"
    else:
        spec = LayerSpec(_free("layer", taken), Domain.FACE, DataType.FLOAT)
        label = "From Attribute"
    ensure_layer(scene, spec)
    added = []
    for obj in objects:
        item = _new_item(scene, obj, "LAYER", label)
        item.computed = True
        _set_layers(item, [spec.name])
        added.append(item)
    return added


# --- running ---------------------------------------------------------------------------


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

_NOT_INPUTS = frozenset(
    {
        "result",
        "error",
        "contour_result",
        "contour_error",
        "section_result",
        "section_error",
        "surface",
    }
)


def copy_settings(source: bpy.types.PropertyGroup, target: bpy.types.PropertyGroup) -> None:
    """Copy every writable property of one settings group onto another of the same type."""
    for prop in source.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"} or prop.is_readonly or prop.type == "COLLECTION":
            continue
        setattr(target, name, getattr(source, name))


def signature(item: GeoToolsAnalysisItem) -> str:
    """A result's inputs as text (cheap: a few RNA reads, no mesh)."""
    group = tool_settings(item)
    if group is None:
        return ""
    parts = [f"extent={item.extent}"]
    for prop in group.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"} or name in _NOT_INPUTS or prop.type == "COLLECTION":
            continue
        value = getattr(group, name)
        parts.append(f"{name}={getattr(value, 'name', value)!r}")
    if item.kind == "SUN_HOURS":
        parts.append(f"date={item.sun_year}-{item.sun_month}-{item.sun_day}")
    return ";".join(parts)


def out_of_date(item: GeoToolsAnalysisItem) -> bool:
    """Whether a computed result's inputs changed since its last run."""
    return item.kind in RUN_KINDS and item.computed and item.signature != signature(item)


def run_error(item: GeoToolsAnalysisItem) -> str:
    group = tool_settings(item)
    if group is None:
        return ""
    if item.kind == "CONTOURS":
        return group.contour_error
    if item.kind == "SECTIONS":
        return group.section_error
    return group.error


@contextmanager
def _selected(context: bpy.types.Context, objects: list[bpy.types.Object]) -> Iterator[None]:
    """Select exactly ``objects`` (the first active) for the run, then put things back."""
    view_layer = context.view_layer
    view_layer.update()  # an object removed just now can leave a stale entry
    everything = [o for o in view_layer.objects if o is not None]
    before = [o for o in everything if o.select_get()]
    active = view_layer.objects.active
    try:
        for obj in everything:
            obj.select_set(obj in objects)
        if objects:
            view_layer.objects.active = objects[0]
        yield
    finally:
        for obj in everything:
            if obj.name in view_layer.objects:
                obj.select_set(obj in before)
        if active is None or active.name in view_layer.objects:
            view_layer.objects.active = active


@contextmanager
def _sun_date(context: bpy.types.Context, item: GeoToolsAnalysisItem) -> Iterator[None]:
    """The Sun settings' date set to the result's own for the run, then put back."""
    sun = analysis.settings(context).sun
    before = (sun.year, sun.month, sun.day)
    try:
        if item.kind == "SUN_HOURS":
            sun.year, sun.month, sun.day = item.sun_year, item.sun_month, item.sun_day
        yield
    finally:
        sun.year, sun.month, sun.day = before


def run_objects(context: bpy.types.Context, item: GeoToolsAnalysisItem) -> list[bpy.types.Object]:
    """The meshes a run covers: the result's mesh, then (Surface) the others."""
    owner = item.object
    if owner is None or owner.name not in context.view_layer.objects:
        raise AnalysesError("This result's mesh is not in the view layer.")
    if item.extent == "SELECTED":
        return [owner, *[o for o in _selected_meshes(context) if o != owner]]
    if item.extent == "SOURCE":
        return [owner, *[o for o in source_objects(context.scene) if o != owner]]
    return [owner]


def compute(context: bpy.types.Context, item: GeoToolsAnalysisItem) -> str:
    """Run a tool or Fast Bake result with its inputs; return the error, if any."""
    if item.kind not in RUN_KINDS:
        raise AnalysesError(f"{item.name} has nothing to compute: it updates by itself.")
    objects = run_objects(context, item)
    before = {o.as_pointer() for o in bpy.data.objects}
    with _quiet(context.scene):
        if item.kind == "FAST":
            error = _compute_fast(context, item, objects)
        else:
            error = _compute_tool(context, item, objects)
    item.computed = True
    item.signature = signature(item)
    keep = [o for o in output_objects(item) if o.name in bpy.data.objects]
    made = [o for o in bpy.data.objects if o.as_pointer() not in before]
    _set_outputs(item, keep + [o for o in made if o not in keep])
    _index_changed(context.scene.geotools_analyses, context)
    return error


def _compute_tool(context, item, objects) -> str:
    group = tool_settings(item)
    scene_group = _SCENE_SETTINGS[item.kind](context)
    copy_settings(group, scene_group)
    scene_group.surface = "SOURCE" if item.extent == "SOURCE" else "SELECTED"
    failed = ""
    try:
        with _selected(context, objects), _sun_date(context, item):
            getattr(bpy.ops.geotools, _OPERATORS[item.kind])()
    except RuntimeError as exc:  # an error report (also in the settings) or a failed poll
        failed = str(exc).removeprefix("Error: ").strip()
    finally:
        surface = group.surface
        copy_settings(scene_group, group)
        group.surface = surface
    names = planned_layers(item.kind, group)
    _set_layers(item, [n for n in names if find_layer(context.scene, n) is not None] or names)
    return run_error(item) or failed


def fast_inputs(item: GeoToolsAnalysisItem) -> dict[str, object]:
    """A Fast Bake result's inputs by the preset's input names (floats as typed)."""
    values = {}
    for name in fast.input_names(item.preset):
        value = getattr(item.fast, FAST_FIELDS[name])
        values[name] = analysis_presets._typed(value) if isinstance(value, float) else value
    return values


def _compute_fast(context, item, objects) -> str:
    from . import preset_bake  # it imports analysis_presets, which this module imports

    group = item.fast
    try:
        result = preset_bake.compute(
            context,
            item.preset,
            objects,
            name=item.layer,
            inputs=fast_inputs(item),
            observers=group.observers,
            occluders=group.occluders,
        )
    except (preset_bake.FastPathError, ValueError, RuntimeError) as exc:
        group.error, group.result = str(exc), ""
        return group.error
    group.error = ""
    group.result = preset_bake.fast_summary(result)
    return ""


# --- removing --------------------------------------------------------------------------


def removal_plan(scene: bpy.types.Scene, item: GeoToolsAnalysisItem) -> list[str]:
    """What removing the result deletes, one line each (the confirmation lists them)."""
    mesh = item.object.name if item.object is not None else "its mesh"
    lines = []
    if item.layer and item_modifier(item) is not None:
        lines.append(f"Its modifier on {mesh}")
    elif item.kind != "PRESET":
        for entry in item.layers:
            layer = find_layer(scene, entry.name)
            if layer is None:
                continue
            what = "images" if layer.domain == Domain.TEXTURE.value else "values"
            lines.append(f"Layer {entry.name}: its {what} on {mesh}")
    lines += [f"Object {o.name}" for o in output_objects(item)]
    return lines


def _remove_data(layer, obj: bpy.types.Object) -> None:
    """Delete a layer's attribute or image on one mesh."""
    if layer.domain == Domain.TEXTURE.value:
        for index, entry in enumerate(layer.images):
            if entry.object == obj:
                if entry.image is not None:
                    bpy.data.images.remove(entry.image)
                layer.images.remove(index)
                break
        return
    if obj.data.library is None:
        remove_attribute(obj.data, spec_from_props(layer))


def remove_result(context: bpy.types.Context, item: GeoToolsAnalysisItem) -> str:
    """Remove a result: its modifier, its layer values on its mesh, the objects it made.

    A layer no other result names, which no mesh carries any more, leaves the
    registry. Returns the result's name.
    """
    with _quiet(context.scene):
        return _remove(context, item)


def _remove(context: bpy.types.Context, item: GeoToolsAnalysisItem) -> str:
    scene = context.scene
    props = scene.geotools_analyses
    name, owner = item.name, item.object
    modifier = item_modifier(item)
    if modifier is not None and item.layer and owner is not None:
        owner.modifiers.remove(modifier)
    layer_names = [entry.name for entry in item.layers]
    if owner is not None and item.kind != "PRESET":
        for layer_name in layer_names:
            layer = find_layer(scene, layer_name)
            if layer is not None:
                _remove_data(layer, owner)
    for obj in output_objects(item):
        bpy.data.objects.remove(obj)
    index = next(i for i, other in enumerate(props.items) if other == item)
    props.items.remove(index)
    context.view_layer.update()
    for layer_name in layer_names:
        _drop_if_unused(scene, layer_name)
    mine = results_of(scene, owner)
    props.index = mine[0] if mine else min(index, max(len(props.items) - 1, 0))
    return name


def _drop_if_unused(scene: bpy.types.Scene, layer_name: str) -> None:
    """Find the layer's carriers again; drop it from the registry when no result names
    it and no mesh carries it (its plain rows go with the next :func:`sync`)."""
    layer = find_layer(scene, layer_name)
    if layer is None:
        return
    carriers = refresh_carriers(scene, layer, sync_view=False)
    if carriers or any(
        e.name == layer_name
        for item in scene.geotools_analyses.items
        if not item.auto
        for e in item.layers
    ):
        return
    layer.presentation.show_in_viewport = False
    layers = scene.geotools.layers
    layers.remove(layers.find(layer_name))
    scene.geotools.layers_index = min(scene.geotools.layers_index, max(len(layers) - 1, 0))


# --- keeping the list in step with the scene -------------------------------------------


def _object_preset(obj: bpy.types.Object) -> str | None:
    """The preset an object preset's new object was made by, if it is one."""
    text = obj.get(catalog.PROVENANCE_KEY)
    if not isinstance(text, str):
        return None
    try:
        return catalog.preset_of(json.loads(text))
    except ValueError:
        return None


def _layer_for(modifier) -> str:
    try:
        attribute = str(analysis_presets.get_input(modifier, catalog.ATTRIBUTE_INPUT))
    except KeyError:
        return ""
    return attribute.removeprefix("gt_")


def sync(scene: bpy.types.Scene) -> int:
    """Bring the list in step with the scene; return how many rows it added or removed.

    Drops the results whose mesh is gone, whose preset modifier or object was
    deleted, or (a plain layer) whose layer is gone or, listed automatically,
    no longer carried by the mesh. Adds a result for a preset modifier or
    object with none (added by a script), and a plain layer row for each mesh
    a layer's stored carriers name that no result of the mesh covers.
    """
    props = scene.geotools_analyses
    layers = {layer.name: layer for layer in scene.geotools.layers}
    changes = 0
    stale: set[str] = set()
    for index in reversed(range(len(props.items))):
        item = props.items[index]
        gone = item.object is None or item.object.name not in scene.objects
        if not gone and item.kind == "PRESET":
            gone = preset_of(item) is None or item_modifier(item) is None
            if gone:
                stale.update(e.name for e in item.layers)
        if not gone and item.kind == "LAYER":
            layer = layers.get(item.layer)
            gone = layer is None or (item.auto and item.object not in stored_carriers(layer))
        if gone:
            props.items.remove(index)
            changes += 1
    for name in stale & set(layers):  # its modifier went: who carries the layer now?
        try:
            refresh_carriers(scene, layers[name], sync_view=False)
        except (RuntimeError, ValueError, KeyError):
            log.exception("could not find the carriers of layer %s", name)
    covered = {
        (item.object.as_pointer(), e.name)
        for item in props.items
        if item.object is not None
        for e in item.layers
    }
    made = {o.as_pointer() for item in props.items for o in output_objects(item)}
    for obj in scene.objects:
        if obj.type != "MESH":
            continue
        preset_id = _object_preset(obj)
        if preset_id is not None:
            if obj.as_pointer() not in made:
                changes += _list_object_preset(scene, obj, preset_id)
            continue
        for modifier, preset in analysis_presets.preset_modifiers(obj):
            name = _layer_for(modifier)
            if preset.output is not catalog.Output.LAYER or not name:
                continue
            if (obj.as_pointer(), name) in covered:
                continue
            item = _new_item(scene, obj, "PRESET", preset.label)
            item.preset, item.computed = preset.id, True
            north = preset.north and analysis_presets.get_input(modifier, catalog.CONVERGENCE_INPUT)
            item.north = "TRUE" if north else "GRID"
            _set_layers(item, [name])
            covered.add((obj.as_pointer(), name))
            changes += 1
    for name, layer in layers.items():
        for obj in stored_carriers(layer):
            if obj.type != "MESH" or (obj.as_pointer(), name) in covered:
                continue
            item = _new_item(scene, obj, "LAYER", layer.label.strip() or name)
            item.auto = item.computed = True
            _set_layers(item, [name])
            covered.add((obj.as_pointer(), name))
            changes += 1
    if changes:
        props.index = min(props.index, max(len(props.items) - 1, 0))
    return changes


def _list_object_preset(scene, obj: bpy.types.Object, preset_id: str) -> int:
    try:
        preset = catalog.get(preset_id)
    except KeyError:
        return 0
    try:
        surfaces = json.loads(obj[catalog.PROVENANCE_KEY])["parameters"]["objects"]
    except (KeyError, TypeError, ValueError):
        surfaces = []
    surface = next((bpy.data.objects[n] for n in surfaces if n in bpy.data.objects), None)
    item = _new_item(scene, surface or obj, "PRESET", preset.label)
    item.preset, item.computed = preset.id, True
    _set_outputs(item, [obj])
    return 1


def scene_key(scene: bpy.types.Scene) -> tuple:
    """What :func:`sync` depends on, cheaply: object, modifier, layer and carrier counts."""
    layers = scene.geotools.layers
    return (
        len(scene.objects),
        sum(len(o.modifiers) for o in scene.objects),
        len(layers),
        sum(len(layer.carriers) for layer in layers),
        len(scene.geotools_analyses.items),
    )


_synced: dict[int, tuple] = {}
_busy = [0]
"""Above 0 while this module changes the scene itself (the handler leaves it alone)."""


@contextmanager
def _quiet(scene: bpy.types.Scene) -> Iterator[None]:
    """Hold the depsgraph handler's sync while adding, running or removing; sync after."""
    _busy[0] += 1
    try:
        yield
    finally:
        _busy[0] -= 1
        if not _busy[0]:
            sync(scene)
            _synced[scene.as_pointer()] = scene_key(scene)


def sync_if_changed(scene: bpy.types.Scene) -> None:
    """:func:`sync` when :func:`scene_key` changed since the last time."""
    if _synced.get(scene.as_pointer()) == scene_key(scene):
        return
    sync(scene)
    _synced[scene.as_pointer()] = scene_key(scene)


@persistent
def _on_depsgraph_update(scene: bpy.types.Scene, _depsgraph: bpy.types.Depsgraph) -> None:
    if not hasattr(scene, "geotools_analyses") or _busy[0]:
        return
    try:
        if scene.geotools_analyses.version < MIGRATED:
            migrate(scene)
        sync_if_changed(scene)
    except Exception:  # never break the depsgraph
        log.exception("Keeping the Analyses list in step failed")


# --- files from before the list ---------------------------------------------------------


def _fast_from(item: GeoToolsAnalysisItem, provenance) -> None:
    inputs = (provenance or {}).get("parameters", {}).get("inputs", {})
    for name, value in fast.default_inputs(item.preset).items():
        setattr(item.fast, FAST_FIELDS[name], inputs.get(name, value))


def migrate(scene: bpy.types.Scene) -> int:
    """List a file's older analyses once; return how many results it added.

    Fast Bake layers become a FAST result on each carrier, with the inputs
    their provenance records. A hydrology, sun-hours or least-cost run's
    layers (one provenance) become one result per carrier, with the scene's
    last settings of that tool. Tool line and point objects join their
    tool's result, else a result on the first source mesh. Preset modifiers
    and objects and every other layer are listed by :func:`sync`, run last.
    """
    props = scene.geotools_analyses
    context = bpy.context
    runs: dict[tuple[int, str], GeoToolsAnalysisItem] = {}
    for layer in list(scene.geotools.layers):
        if not len(layer.carriers):
            try:
                refresh_carriers(scene, layer, sync_view=False)
            except (RuntimeError, ValueError, KeyError):
                log.exception("could not find the carriers of layer %s", layer.name)
        provenance = spec_from_props(layer).provenance
        fast_id = fast.fast_preset_of(provenance)
        kind = "FAST" if fast_id else TOOL_PROVENANCE.get(str((provenance or {}).get("tool")))
        if kind is None:
            continue
        for obj in stored_carriers(layer):
            if obj.type != "MESH":
                continue
            run = (obj.as_pointer(), layer.provenance)
            item = runs.get(run)
            if item is None:
                label = (
                    (layer.label.strip() or layer.name) if kind == "FAST" else _TOOL_LABELS[kind]
                )
                item = _new_item(scene, obj, kind, label)
                if kind == "FAST":
                    item.preset = fast_id
                    _fast_from(item, provenance)
                else:
                    copy_settings(_SCENE_SETTINGS[kind](context), tool_settings(item))
                item.computed = True
                runs[run] = item
            item.layers.add().name = layer.name
            if not item.layer:
                item.layer = layer.name
    for item in runs.values():
        item.signature = signature(item)
    sources = source_objects(scene)
    for obj in scene.objects:
        kind = OUTPUT_KINDS.get(str(obj.get(OUTPUT_TOOL_PROP, "")))
        if kind is None:
            continue
        owner = next((i for i in runs.values() if i.kind == kind), None)
        if owner is None:
            if not sources:
                continue
            owner = _new_item(scene, sources[0], kind, _TOOL_LABELS[kind])
            copy_settings(_SCENE_SETTINGS[kind](context), tool_settings(owner))
            owner.computed = True
            owner.signature = signature(owner)
            runs[(sources[0].as_pointer(), kind)] = owner
        owner.outputs.add().object = obj
    props.version = MIGRATED
    sync(scene)
    return len(runs)


def migrate_all() -> None:
    for scene in bpy.data.scenes:
        try:
            if getattr(scene, "geotools_analyses", None) is None:
                continue
            if scene.geotools_analyses.version < MIGRATED:
                migrate(scene)
            else:
                sync(scene)
        except Exception:  # never break file loading
            log.exception("Listing the analyses of scene %s failed", scene.name)


@persistent
def _on_load_post(*_args: object) -> None:
    _synced.clear()
    _subscribe()  # a file load drops msgbus subscriptions
    migrate_all()


def _after_register() -> None:
    migrate_all()


# --- Publish -----------------------------------------------------------------------------


def variant_choices(scene: bpy.types.Scene, edit_text: str) -> list[tuple[str, str]]:
    """Publish's Variant search: each layer of the results, with the results that make it."""
    query = edit_text.strip().lower()
    found: dict[str, list[str]] = {layer.name: [] for layer in scene.geotools.layers}
    for item in scene.geotools_analyses.items:
        mesh = item.object.name if item.object is not None else "?"
        for entry in item.layers:
            if entry.name in found:
                found[entry.name].append(f"{item.name} on {mesh}")
    choices = []
    for name, users in found.items():
        more = f" and {len(users) - 3} more" if len(users) > 3 else ""
        text = "; ".join(users[:3]) + more
        if query and query not in f"{name} {text}".lower():
            continue
        choices.append((name, text or "On no mesh's list"))
    return choices


# --- operators -------------------------------------------------------------------------


def _shown_poll(cls, context: bpy.types.Context) -> bool:
    if shown_item(context) is None:
        cls.poll_message_set("Select a mesh and one of its analyses")
        return False
    return True


class GEOTOOLS_OT_analyses_compute(bpy.types.Operator):
    """Run the selected result with its inputs (a tool or a Fast Bake)"""

    bl_idname = "geotools.analyses_compute"
    bl_label = "Compute"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _shown_poll(cls, context):
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        return shown_item(context).kind in RUN_KINDS

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = shown_item(context)
        try:
            error = compute(context, item)
        except AnalysesError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        redraw_ui(context)
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        self.report({"INFO"}, f"{item.name} on {item.object.name}: computed")
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_remove(bpy.types.Operator):
    """Remove the selected result from its mesh: its modifier, its layer values there and the objects it made"""

    bl_idname = "geotools.analyses_remove"
    bl_label = "Remove Analysis"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        return _shown_poll(cls, context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        return context.window_manager.invoke_props_dialog(self, confirm_text="Remove")

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        item = shown_item(context)
        if item is None:
            return
        layout.label(text=f"Remove {item.name} from {item.object.name}?")
        plan = removal_plan(context.scene, item)
        if plan:
            col = layout.column(align=True)
            col.label(text="This deletes:")
            for line in plan[:10]:
                col.label(text=line, icon="TRASH")
            if len(plan) > 10:
                col.label(text=f"... and {len(plan) - 10} more")

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            name = remove_result(context, shown_item(context))
        except (RuntimeError, AnalysesError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Removed {name}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_show_layer(bpy.types.Operator):
    """Show this layer in Display and as the result's variant"""

    bl_idname = "geotools.analyses_show_layer"
    bl_label = "Display This Layer"
    bl_options = {"REGISTER", "UNDO"}

    layer: StringProperty(name="Layer", description="The layer to display")

    def execute(self, context: bpy.types.Context) -> set[str]:
        item = shown_item(context)
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
        index = context.scene.geotools.layers.find(self.layer)
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
            if other is not None:
                other.select_set(other == obj)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class GEOTOOLS_OT_analyses_publish_variant(bpy.types.Operator):
    """Publish the selected result's layer as the active build profile's variant"""

    bl_idname = "geotools.analyses_publish_variant"
    bl_label = "Publish as Variant"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _shown_poll(cls, context):
            return False
        props = context.scene.geotools
        if not 0 <= props.build_profile_index < len(props.build_profiles):
            cls.poll_message_set("Add a build profile in Publish first")
            return False
        if item_layer(context.scene, shown_item(context)) is None:
            cls.poll_message_set("This result has no layer")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        profile = props.build_profiles[props.build_profile_index]
        item = shown_item(context)
        profile.variant_layer = item.layer
        self.report({"INFO"}, f"{profile.name}: variant {item.layer}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_UL_analyses(bpy.types.UIList):
    """The active mesh's results: kind icon, name, units (or state) and Show in Viewport."""

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
        if item.kind in RUN_KINDS and not item.computed:
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
        """The active mesh's results only, like its modifier stack (then the name filter)."""
        items = getattr(data, propname)
        mesh = active_mesh(context)
        pointer = mesh.as_pointer() if mesh is not None else 0
        if self.filter_name:
            named = bpy.types.UI_UL_list.filter_items_by_name(
                self.filter_name, self.bitflag_filter_item, items, "name"
            )
        else:
            named = [self.bitflag_filter_item] * len(items)
        flags = []
        for i, item in enumerate(items):
            mine = item.object is not None and item.object.as_pointer() == pointer
            flags.append(self.bitflag_filter_item if mine and named[i] else 0)
        return flags, []


classes = (
    GeoToolsAnalysisObject,
    GeoToolsAnalysisLayer,
    GeoToolsFastSettings,
    GeoToolsAnalysisItem,
    GeoToolsAnalyses,
    GEOTOOLS_OT_analyses_compute,
    GEOTOOLS_OT_analyses_remove,
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
    _synced.clear()
    if hasattr(bpy.types.Scene, "geotools_analyses"):
        del bpy.types.Scene.geotools_analyses
    _unregister_classes()
