"""Add Analysis: the bundled Geometry Nodes presets as modifiers and layers (M5).

The presets are ordinary node groups of stock nodes in
``geotools/assets/analysis_presets.blend`` (``scripts/make_analysis_presets.py``
builds it; ``geotools.analysis.presets`` is the catalog). Blender 5.2 has no
API for an extension to register an asset library without writing the user's
preferences, so ``geotools.analysis_add`` appends the group from the bundled
file directly. The file's groups are also marked as assets in two catalogs, so
a user can add its folder as an asset library by hand.

``geotools.analysis_add``:

1. appends the preset's node group, unless the file already has it (found by
   its ``geotools_preset`` and ``geotools_preset_version`` custom properties,
   so a renamed or edited group is reused). Helper groups appended with it
   that the file already has are merged into the existing ones, so re-adding
   never duplicates a node group;
2. adds it as a Geometry Nodes modifier on each selected mesh, with its
   ``Attribute`` input set to the layer's ``gt_<name>`` (an object that already
   has that modifier keeps it) and, for aspect with true north, the
   ``Convergence`` input set from ``analysis.site`` at the object's centre;
3. creates or updates the FACE or CORNER layer: label, units, aggregation,
   description, a provenance naming the preset, its version and the inputs,
   and, for a new layer, the display palette and range.

The layer reads the evaluated mesh (M3), and the export applies modifiers, so
the result publishes like any Geometry Nodes layer. The user edits the inputs
on the modifier, or opens the group and edits it.

Object inputs (an observer, a second surface, a line, points) are filled from
the other selected objects, or with a new Empty or one-vertex mesh at the 3D
cursor (``catalog.Fill``). Presets that take a mesh, line or points from the
selection go on the active object only. **Object presets** (intervisibility,
line of sight, least-cost paths, flow paths) create a new mesh object in the
"GeoTools Analysis" collection whose modifier makes the lines, with their
provenance as JSON in the ``geotools_provenance`` custom property.

``geotools.analysis_totals`` reads totals from the evaluated meshes (cut and
fill volumes, closed volumes, walking time, the obstruction) and reports them,
summed in binary64; the groups also show float32 totals on the modifier.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from ..analysis import preset_bake as fast
from ..analysis import presets as catalog
from ..analysis import site
from ..core.issues import Severity
from ..core.layers import attribute_name, display_name, format_number, validate
from ..core.placement import PlacementMode
from . import blend_paths
from .common import extension_version, redraw_ui
from .layers import ensure_layer, find_layer, refresh_layer, source_objects, spec_from_props
from .properties import FieldError, placement_from_props
from .ui import wrapped

log = logging.getLogger(__name__)

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "assets" / catalog.LIBRARY_FILE

PRESET_ITEMS = tuple((p.id, p.label, p.description) for p in catalog.PRESETS)

NORTH_ITEMS = (
    ("GRID", "Grid North (+Y)", "Measure from the scene's +Y axis"),
    ("TRUE", "True North", "Add the grid convergence at each object's centre (analysis.site)"),
)


ENGINE_ITEMS = (
    (
        "GEONODES",
        "Live (Geometry Nodes)",
        (
            "Add the preset as a Geometry Nodes modifier: editable, and it updates when the mesh "
            "or an input changes"
        ),
    ),
    (
        "BAKE",
        "Fast (bake)",
        (
            "Compute the values once with a Cycles bake (the sun-hours ray engine) and store them "
            "as a layer: several times faster on large meshes, no modifier"
        ),
    ),
)


def _default(preset_id: str, name: str):
    return next(i.default for i in catalog.get(preset_id).inputs if i.name == name)


def _typed(value: float) -> float:
    """A float operator property (float32) as the decimal it was typed as: 0.002, not
    0.0020000000949949026."""
    return float(str(np.float32(value)))


ANALYSIS_COLLECTION = "GeoTools Analysis"
"""Where object presets' new objects and the Empties made at the 3D cursor go."""


class PresetError(RuntimeError):
    """The preset could not be added; the message says why."""


# --- node groups -------------------------------------------------------------------------


def _identity(group: bpy.types.NodeTree) -> tuple[object, object]:
    return group.get(catalog.PRESET_KEY), group.get(catalog.VERSION_KEY)


def find_group(ident: str, version: int) -> bpy.types.NodeTree | None:
    """The file's local node group of this preset (or helper) and version, if any."""
    for group in bpy.data.node_groups:
        if group.library is None and _identity(group) == (ident, version):
            return group
    return None


def ensure_group(preset: catalog.Preset, library: Path = LIBRARY_PATH) -> bpy.types.NodeTree:
    """The preset's node group in this file, appended from ``library`` if needed.

    Groups appended alongside it (its helpers) that the file already has are
    remapped onto the existing ones and removed, so nothing is duplicated.
    """
    existing = find_group(preset.id, preset.version)
    if existing is not None:
        return existing
    if not library.is_file():
        raise PresetError(f"The preset library is missing: {library}")
    before = {group.as_pointer() for group in bpy.data.node_groups}
    with bpy.data.libraries.load(str(library), link=False) as (source, target):
        if preset.group not in source.node_groups:
            raise PresetError(f"{library.name} has no node group {preset.group!r}")
        target.node_groups = [preset.group]
    appended = target.node_groups[0]
    if appended is None or _identity(appended) != (preset.id, preset.version):
        raise PresetError(f"{preset.group!r} in {library.name} is not version {preset.version}")
    for group in [g for g in bpy.data.node_groups if g.as_pointer() not in before]:
        ident, version = _identity(group)
        if ident is None or group == appended:
            continue
        old = next(
            (
                g
                for g in bpy.data.node_groups
                if g.as_pointer() in before
                and g.library is None
                and _identity(g) == (ident, version)
            ),
            None,
        )
        if old is not None:
            group.user_remap(old)
            bpy.data.node_groups.remove(group)
    _forget_library(library)
    return appended


def _forget_library(library: Path) -> None:
    """Remove the Library entries an append leaves behind (nothing is linked from them)."""
    for entry in list(bpy.data.libraries):
        try:
            same = blend_paths.same_file(blend_paths.abspath(entry.filepath), library)
        except (OSError, ValueError):
            continue
        if same and not any(getattr(i, "library", None) == entry for i in bpy.data.node_groups):
            bpy.data.libraries.remove(entry)


# --- modifiers ---------------------------------------------------------------------------------


def _input_item(group: bpy.types.NodeTree, name: str):
    for item in group.interface.items_tree:
        if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name:
            return item
    return None


def get_input(modifier: bpy.types.Modifier, name: str) -> object:
    """A Geometry Nodes modifier's input value by socket name (Blender 5.2 API)."""
    item = _input_item(modifier.node_group, name)
    if item is None:
        raise KeyError(name)
    return getattr(modifier.properties.inputs, item.identifier).value


def set_input(modifier: bpy.types.Modifier, name: str, value: object) -> None:
    item = _input_item(modifier.node_group, name)
    if item is None:
        raise KeyError(name)
    getattr(modifier.properties.inputs, item.identifier).value = value
    modifier.id_data.update_tag()  # setting the value from Python does not re-evaluate


def preset_modifiers(obj: bpy.types.Object) -> list[tuple[bpy.types.Modifier, catalog.Preset]]:
    """The object's Geometry Nodes modifiers whose group is a GeoTools preset."""
    found = []
    for modifier in obj.modifiers:
        group = getattr(modifier, "node_group", None)
        if modifier.type != "NODES" or group is None:
            continue
        ident, version = _identity(group)
        try:
            preset = catalog.get(str(ident))
        except KeyError:
            continue
        if version == preset.version:
            found.append((modifier, preset))
    return found


def find_modifier(
    obj: bpy.types.Object, group: bpy.types.NodeTree, attribute: str
) -> bpy.types.Modifier | None:
    """The object's modifier of ``group`` that stores ``attribute``, if any."""
    for modifier in obj.modifiers:
        if modifier.type != "NODES" or modifier.node_group != group:
            continue
        try:
            if get_input(modifier, catalog.ATTRIBUTE_INPUT) == attribute:
                return modifier
        except KeyError:
            continue
    return None


def _inputs(modifier: bpy.types.Modifier, preset: catalog.Preset) -> dict[str, object]:
    values = {}
    for spec in preset.inputs:
        try:
            value = get_input(modifier, spec.name)
        except KeyError:
            continue
        if isinstance(value, bpy.types.ID):
            value = value.name  # an object or collection: its name
        values[spec.name] = float(value) if isinstance(value, float) else value
    return values


# --- north ---------------------------------------------------------------------------------------


def _centre(obj: bpy.types.Object) -> tuple[float, float, float]:
    """World-space centre of the object's bounding box (8 corners, not its vertices)."""
    corners = np.array(obj.bound_box, dtype=np.float64)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = corners @ matrix[:3, :3].T + matrix[:3, 3]
    return tuple(float(v) for v in world.mean(axis=0))


def convergence(scene: bpy.types.Scene, obj: bpy.types.Object) -> tuple[float, str]:
    """Degrees from true north to the scene's +Y at the object's centre, and what +Y is.

    LOCAL projects take +Y as true north (0). Raises ``PresetError`` when the
    project does not parse or a CRS project has no pyproj.
    """
    try:
        placement = placement_from_props(scene.geotools)
    except FieldError as exc:
        raise PresetError(f"The project placement is not valid: {exc}") from None
    mode = PlacementMode(placement.mode)
    if mode is PlacementMode.LOCAL:
        return 0.0, "true north"
    if mode is PlacementMode.SOURCE_CRS and not site.available():
        raise PresetError(
            "True north needs pyproj, which is not available (see About); use grid north."
        )
    try:
        where = site.sun_site(placement, _centre(obj))
    except (site.SiteError, site.GeodesyUnavailable, ValueError) as exc:
        raise PresetError(f"Could not find the convergence at {obj.name}: {exc}") from None
    # One angle: the convergence in a conformal grid; in a non-conformal one the
    # rotation nearest the grid-to-ground map (site.SunSite.rotation).
    return float(where.rotation), where.north


NON_CONFORMAL_ASPECT = (
    "The project CRS is not conformal (Albers, Web Mercator): one rotation per object "
    "makes aspects facing along and across the meridian true; others can be off by up "
    "to a degree or two (1.8° in Tasmania in EPSG:3577, 0.2° at the equator in Web Mercator)"
)


def _conformal(scene: bpy.types.Scene) -> bool:
    """Whether one rotation turns every direction to true north (not Albers, Web Mercator)."""
    try:
        placement = placement_from_props(scene.geotools)
    except FieldError:
        return True
    if PlacementMode(placement.mode) is not PlacementMode.SOURCE_CRS or not site.available():
        return True
    try:
        return site.sun_site(placement).conformal
    except (site.SiteError, site.GeodesyUnavailable, ValueError):
        return True


# --- the operation ------------------------------------------------------------------------------


def target_objects(context: bpy.types.Context) -> list[bpy.types.Object]:
    """The selected mesh objects, or the active one when it is a mesh and none is selected."""
    objects = [o for o in context.selected_objects if o.type == "MESH"]
    if not objects and context.active_object is not None and context.active_object.type == "MESH":
        objects = [context.active_object]
    return sorted(objects, key=lambda o: o.name)


def preset_targets(context: bpy.types.Context, preset: catalog.Preset) -> list[bpy.types.Object]:
    """The meshes a preset goes on: the selected ones, or only the active one.

    A preset that takes a mesh, line or points from the selection (and every
    object preset, whose surface is the active mesh) uses the active object.
    """
    if not preset.active_only:
        return target_objects(context)
    active = context.active_object
    if active is None or active.type != "MESH":
        raise PresetError(f"{preset.label} works on the active object: make a mesh active.")
    return [active]


def analysis_collection(scene: bpy.types.Scene) -> bpy.types.Collection:
    """The scene's "GeoTools Analysis" collection, created when missing (never the source)."""
    collection = next(
        (c for c in scene.collection.children_recursive if c.name == ANALYSIS_COLLECTION), None
    )
    if collection is None:
        collection = bpy.data.collections.new(ANALYSIS_COLLECTION)
        scene.collection.children.link(collection)
    return collection


def _new_object(scene, name: str, data) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, data)
    analysis_collection(scene).objects.link(obj)
    return obj


def _cursor_point(scene, name: str) -> bpy.types.Object:
    """A new Empty at the 3D cursor."""
    obj = _new_object(scene, name, None)
    obj.empty_display_type = "SINGLE_ARROW"
    obj.location = scene.cursor.location
    return obj


def _cursor_points(scene, name: str) -> bpy.types.Object:
    """A new mesh of one loose vertex at the 3D cursor (add more in Edit Mode)."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(1)
    obj = _new_object(scene, name, mesh)
    obj.location = scene.cursor.location
    return obj


def _fits(obj: bpy.types.Object, fill: catalog.Fill) -> bool:
    kind = obj.type
    if fill is catalog.Fill.POINT:
        return kind in {"EMPTY", "CAMERA", "LIGHT"}
    if fill is catalog.Fill.POINTS:
        return kind in {"MESH", "POINTCLOUD", "CURVE", "CURVES", "EMPTY"}
    if fill is catalog.Fill.MESH:
        return kind == "MESH" and len(obj.data.polygons) > 0
    if fill is catalog.Fill.LINE:
        return (kind == "MESH" and len(obj.data.edges) > 0) or kind in {"CURVE", "CURVES"}
    if fill is catalog.Fill.FEATURE:
        return kind in {"MESH", "CURVE", "CURVES", "POINTCLOUD"}
    return False


def fill_inputs(
    context: bpy.types.Context, preset: catalog.Preset, targets: list[bpy.types.Object]
) -> tuple[dict[str, bpy.types.Object], list[str]]:
    """The objects for the preset's object inputs, and notes on what was made.

    Each input takes the first fitting selected object (by name) that is not a
    target and not already used; a POINT input otherwise gets a new Empty at
    the 3D cursor, a POINTS input a new one-vertex mesh there. A missing
    surface, mesh, line or feature raises ``PresetError`` (nothing is made).
    """
    scene = context.scene
    used = {o.as_pointer() for o in targets}
    selected = sorted(context.selected_objects, key=lambda o: o.name)
    chosen: dict[str, bpy.types.Object | None] = {}
    for spec in preset.object_inputs:
        if spec.fill is catalog.Fill.NONE:
            continue
        if spec.fill is catalog.Fill.SURFACE:
            chosen[spec.name] = targets[0]
            continue
        found = next(
            (o for o in selected if o.as_pointer() not in used and _fits(o, spec.fill)), None
        )
        if found is None and spec.fill not in (catalog.Fill.POINT, catalog.Fill.POINTS):
            raise PresetError(
                f"{preset.label} needs {catalog.FILL_LABELS[spec.fill]} for {spec.name}: "
                "select it as well as the active mesh."
            )
        chosen[spec.name] = found
        if found is not None:
            used.add(found.as_pointer())
    notes, values = [], {}
    for spec in preset.object_inputs:  # make the cursor objects only once nothing can fail
        if spec.name not in chosen:
            continue
        obj = chosen[spec.name]
        if obj is None:
            make = _cursor_point if spec.fill is catalog.Fill.POINT else _cursor_points
            obj = make(scene, spec.name)
            notes.append(f"{spec.name}: new {obj.name} at the 3D cursor")
        values[spec.name] = obj
    return values, notes


def _recorded_objects(provenance) -> list[str]:
    """The object names a preset layer's provenance lists (empty if it lists none)."""
    objects = (provenance or {}).get("parameters", {}).get("objects")
    return [str(o) for o in objects] if isinstance(objects, list) else []


def add_preset(
    context: bpy.types.Context,
    preset: catalog.Preset,
    objects: list[bpy.types.Object],
    *,
    name: str = "",
    north: str = "GRID",
    library: Path = LIBRARY_PATH,
) -> tuple[str, list[str]]:
    """Add ``preset`` to ``objects`` and create its layer; returns (layer name, notes).

    Raises ``PresetError`` (nothing changed) when the layer name belongs to a
    layer of something else, the name is invalid, or north cannot be found.
    """
    scene = context.scene
    if not objects:
        raise PresetError("Select one or more mesh objects first.")
    if preset.output is catalog.Output.OBJECT:
        return _add_object_preset(context, preset, objects[0], name=name, library=library)
    name = name.strip() or preset.layer
    item = find_layer(scene, name)
    if item is not None and catalog.preset_of(spec_from_props(item).provenance) != preset.id:
        raise PresetError(
            f"Layer {name!r} already exists and is not a {preset.label} layer; "
            "give the analysis another name."
        )
    others = [layer.name for layer in scene.geotools.layers if layer.name != name]
    errors = [
        i
        for i in validate(catalog.layer_spec(preset, name), others)
        if i.severity is Severity.ERROR
    ]
    if errors:
        raise PresetError(errors[0].message)

    true_north = preset.north and north == "TRUE"
    angles: dict[str, float] = {}
    north_label = "grid"
    if true_north:
        for obj in objects:
            angles[obj.name], north_label = convergence(scene, obj)
        if not _conformal(scene):
            notes_north = [NON_CONFORMAL_ASPECT]
        else:
            notes_north = []

    group = ensure_group(preset, library)
    attribute = attribute_name(name)
    fills: dict[str, bpy.types.Object] = {}
    notes: list[str] = []
    if any(find_modifier(obj, group, attribute) is None for obj in objects):
        fills, notes = fill_inputs(context, preset, objects)
    modifier_inputs: dict[str, dict[str, object]] = {}
    added = 0
    for obj in objects:
        modifier = find_modifier(obj, group, attribute)
        if modifier is None:
            modifier = obj.modifiers.new(f"GeoTools {preset.label} ({name})", "NODES")
            modifier.node_group = group
            set_input(modifier, catalog.ATTRIBUTE_INPUT, attribute)
            for input_name, value in fills.items():
                set_input(modifier, input_name, value)
            added += 1
        if preset.north:
            set_input(modifier, catalog.CONVERGENCE_INPUT, angles.get(obj.name, 0.0))
        modifier_inputs[obj.name] = _inputs(modifier, preset)

    is_new = item is None
    previous = [] if is_new else _recorded_objects(spec_from_props(item).provenance)
    names = sorted(set(previous) | {o.name for o in objects})
    first = modifier_inputs[objects[0].name]
    spec = catalog.layer_spec(
        preset,
        name,
        inputs=first if len({repr(v) for v in modifier_inputs.values()}) == 1 else modifier_inputs,
        objects=tuple(names),
        north=None if not preset.north else ("true" if true_north else "grid"),
        geotools_version=extension_version(),
    )
    ensure_layer(scene, spec)
    item = find_layer(scene, name)
    if is_new:
        item.presentation.palette = preset.palette
        if preset.display_range is not None:
            item.presentation.range_min = format_number(preset.display_range[0])
            item.presentation.range_max = format_number(preset.display_range[1])
    layers = scene.geotools.layers
    scene.geotools.layers_index = next(i for i, layer in enumerate(layers) if layer.name == name)

    if true_north:
        spread = ", ".join(f"{angles[o.name]:+.4f}°" for o in objects[:3])
        notes.append(f"Convergence {spread} ({north_label})")
        notes.extend(notes_north)
    sources = {o.as_pointer() for o in source_objects(scene)}
    outside = [o.name for o in objects if o.as_pointer() not in sources]
    if outside:
        notes.append("Not in the source collection, so not published: " + ", ".join(outside[:3]))
    if added == 0:
        notes.append("Every object already had it")
    try:
        refresh_layer(scene, item)
    except (RuntimeError, ValueError, KeyError):
        log.exception("could not refresh layer %s", name)
    for obj in objects[:1]:
        notes.extend(totals(context, obj))
    return name, notes


def _add_object_preset(
    context: bpy.types.Context,
    preset: catalog.Preset,
    surface: bpy.types.Object,
    *,
    name: str,
    library: Path,
) -> tuple[str, list[str]]:
    """A new object whose modifier makes the preset's lines from ``surface`` and the inputs."""
    if not len(surface.data.polygons):
        raise PresetError(f"{preset.label} needs a surface: make a mesh with faces active.")
    scene = context.scene
    fills, notes = fill_inputs(context, preset, [surface])
    group = ensure_group(preset, library)
    obj = _new_object(scene, name.strip() or preset.label, bpy.data.meshes.new(preset.layer))
    modifier = obj.modifiers.new(f"GeoTools {preset.label}", "NODES")
    modifier.node_group = group
    for input_name, value in fills.items():
        set_input(modifier, input_name, value)
    provenance = catalog.object_provenance(
        preset,
        inputs=_inputs(modifier, preset),
        surface=surface.name,
        geotools_version=extension_version(),
    )
    obj[catalog.PROVENANCE_KEY] = json.dumps(provenance, sort_keys=True)
    for other in context.selected_objects:
        other.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    notes.append(f"in {ANALYSIS_COLLECTION}")
    notes.extend(totals(context, obj))
    return obj.name, notes


# --- totals ------------------------------------------------------------------------------------


def _evaluated_mesh(context: bpy.types.Context, obj: bpy.types.Object) -> bpy.types.Mesh:
    return obj.evaluated_get(context.evaluated_depsgraph_get()).data


def _array(collection, key: str, width: int = 1, dtype=np.float32) -> np.ndarray:
    values = np.empty(len(collection) * width, dtype)
    collection.foreach_get(key, values)
    return values.reshape(-1, width) if width > 1 else values


def _attribute(mesh: bpy.types.Mesh, name: str) -> np.ndarray | None:
    attribute = mesh.attributes.get(name)
    if attribute is None:
        return None
    return _array(attribute.data, "value")


def _face_frame(mesh: bpy.types.Mesh) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Face centres, unit normals and areas of ``mesh``, as binary64."""
    centres = _array(mesh.polygons, "center", 3).astype(np.float64)
    normals = _array(mesh.polygons, "normal", 3).astype(np.float64)
    areas = _array(mesh.polygons, "area").astype(np.float64)
    return centres, normals, areas


def _face_vectors(mesh: bpy.types.Mesh) -> tuple[np.ndarray, np.ndarray]:
    """Face centres (from the vertices' mean) and area vectors (Newell), binary64.

    Both are measured from the vertices' mean, which any closed part's volume ignores and
    which keeps the sums exact far from the origin (Blender's float32 face centres are not).
    """
    co = _array(mesh.vertices, "co", 3).astype(np.float64)
    co -= co.mean(axis=0)
    loops = _array(mesh.loops, "vertex_index", 1, np.int32)
    start = _array(mesh.polygons, "loop_start", 1, np.int32)
    total = _array(mesh.polygons, "loop_total", 1, np.int32)
    face = np.repeat(np.arange(len(start)), total)
    after = np.arange(len(loops)) + 1
    after[start + total - 1] = start  # each face's last corner turns to its first
    here, there = co[loops], co[loops[after]]
    centres = np.add.reduceat(here, start, axis=0) / total[:, None]
    area_vectors = np.zeros((len(start), 3))
    np.add.at(area_vectors, face, np.cross(here, there) / 2.0)
    return centres, area_vectors


def _cut_fill_totals(mesh: bpy.types.Mesh, attribute: str) -> str | None:
    depth = _attribute(mesh, attribute)
    if depth is None or len(depth) != len(mesh.polygons):
        return None
    _centres, normals, areas = _face_frame(mesh)
    plan = areas * normals[:, 2]
    covered = np.isfinite(depth)
    volume = np.where(covered, depth.astype(np.float64), 0.0) * plan
    fill, cut = volume[volume > 0].sum(), -volume[volume < 0].sum()
    return (
        f"fill {fill:,.3f} m³, cut {cut:,.3f} m³, net {fill - cut:,.3f} m³ over "
        f"{plan[covered].sum():,.2f} m² ({plan[~covered].sum():,.2f} m² outside the reference)"
    )


def _closed_volume_totals(mesh: bpy.types.Mesh, attribute: str) -> str | None:
    values = _attribute(mesh, attribute)
    if values is None or len(values) != len(mesh.polygons):
        return None
    closed = np.isfinite(values)
    if not closed.any():
        return "no closed parts"
    centres, area_vectors = _face_vectors(mesh)
    volume = np.einsum("ij,ij->i", centres, area_vectors)[closed].sum() / 3.0
    open_faces = int((~closed).sum())
    return f"volume {volume:,.3f} m³" + (
        f" ({open_faces:,} faces on open parts)" * bool(open_faces)
    )


def _edge_lengths(mesh: bpy.types.Mesh) -> np.ndarray:
    co = _array(mesh.vertices, "co", 3).astype(np.float64)
    ends = _array(mesh.edges, "vertices", 2, np.int32)
    return np.linalg.norm(co[ends[:, 1]] - co[ends[:, 0]], axis=1)


def _object_totals(mesh: bpy.types.Mesh, preset: catalog.Preset) -> str | None:
    if preset.id == "least_cost_path":
        elapsed = _attribute(mesh, "elapsed")
        if elapsed is None or not len(mesh.edges):
            return "no walkable path from Start to End"
        minutes = float(elapsed.max()) / 60.0
        return f"walking time {minutes:,.1f} min over {_edge_lengths(mesh).sum():,.1f} m"
    if preset.id == "intervisibility":
        visible = _attribute(mesh, "visible")
        if visible is None:
            return "no pairs of points"
        return f"{int((visible > 0.5).sum())} of {len(visible)} pairs see each other"
    if preset.id == "line_of_sight":
        obstruction = _attribute(mesh, "obstruction")
        if obstruction is None:
            return None
        blocked = np.flatnonzero(obstruction > 0.5)
        if not len(blocked):
            return "the target is in sight"
        x, y, z = _array(mesh.vertices, "co", 3)[blocked[0]]
        return f"the sight line is blocked at ({x:.3f}, {y:.3f}, {z:.3f})"
    if preset.id == "flow_path":
        return f"{len(mesh.edges):,} steps"
    return None


def totals(context: bpy.types.Context, obj: bpy.types.Object) -> list[str]:
    """Totals of the object's presets that have them, read from its evaluated mesh."""
    found = preset_modifiers(obj)
    if not found:
        return []
    mesh = _evaluated_mesh(context, obj)
    lines = []
    for modifier, preset in found:
        if preset.output is catalog.Output.OBJECT:
            line = _object_totals(mesh, preset)
        elif preset.id in ("cut_fill", "closed_volume"):
            attribute = str(get_input(modifier, catalog.ATTRIBUTE_INPUT))
            read = _cut_fill_totals if preset.id == "cut_fill" else _closed_volume_totals
            line = read(mesh, attribute)
        else:
            line = None
        if line:
            lines.append(f"{preset.label}: {line}")
    return lines


# --- operator and menu -----------------------------------------------------------------------


class GEOTOOLS_OT_analysis_add(bpy.types.Operator):
    """Add a Geometry Nodes analysis preset to the selected meshes and create its layer"""

    bl_idname = "geotools.analysis_add"
    bl_label = "Add Analysis"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Preset", description="The analysis preset to add", items=PRESET_ITEMS
    )
    name: StringProperty(
        name="Layer",
        description="Layer name (lowercase letters, digits and underscores), or the new "
        "object's name for presets that make lines; empty for the preset's own",
        options={"SKIP_SAVE"},
    )
    north: EnumProperty(
        name="North",
        items=NORTH_ITEMS,
        default="GRID",
        description="What aspect is measured from (other presets ignore it)",
        options={"SKIP_SAVE"},
    )
    engine: EnumProperty(
        name="Engine",
        items=ENGINE_ITEMS,
        default="GEONODES",
        description="Sky-view factor, openness and the cumulative viewshed can also be "
        "computed once by a Cycles bake (other presets are always live)",
        options={"SKIP_SAVE"},
    )
    # The fast path's inputs: the preset's own, with its defaults (a live
    # preset takes them on its modifier instead).
    max_distance: FloatProperty(
        name="Max Distance",
        default=_default("sky_view_factor", "Max Distance"),
        min=0.001,
        subtype="DISTANCE",
        description="How far the horizon is searched along each direction",
    )
    directions: IntProperty(
        name="Directions",
        default=_default("sky_view_factor", "Directions"),
        min=1,
        max=64,
        description="Azimuths searched, evenly spaced from +Y",
    )
    steps: IntProperty(
        name="Steps",
        default=_default("sky_view_factor", "Steps"),
        min=1,
        max=16,
        description="Rays per direction that bisect the horizon angle",
    )
    ray_offset: FloatProperty(
        name="Ray Offset",
        default=_default("sky_view_factor", "Ray Offset"),
        min=0.0,
        subtype="DISTANCE",
        precision=4,
        description="Rays start this far from the surface along the vertex normal",
    )
    observer_height: FloatProperty(
        name="Observer Height",
        default=_default("viewshed_cumulative", "Observer Height"),
        min=0.0,
        subtype="DISTANCE",
        description="Eye height above each observer point",
    )
    target_height: FloatProperty(
        name="Target Height",
        default=_default("viewshed_cumulative", "Target Height"),
        min=0.0,
        subtype="DISTANCE",
        description="Height above the surface that must be seen (0: the surface itself, "
        "seen from the front only)",
    )
    view_distance: FloatProperty(
        name="Max Distance",
        default=_default("viewshed_cumulative", "Max Distance"),
        min=0.0,
        subtype="DISTANCE",
        description="Horizontal reach of the view",
    )
    view_offset: FloatProperty(
        name="Ray Offset",
        default=_default("viewshed_cumulative", "Ray Offset"),
        min=0.0,
        subtype="DISTANCE",
        precision=4,
        description="Sight lines start this far from the surface",
    )
    drape: BoolProperty(
        name="Drape",
        default=_default("viewshed_cumulative", "Drape"),
        description="Drop the observer points vertically onto the topmost surface first",
    )
    occluders: StringProperty(
        name="Occluders",
        description="A collection of other objects that block sight as well (optional)",
    )

    @classmethod
    def description(cls, _context, properties) -> str:
        try:
            preset = catalog.get(properties.preset)
        except KeyError:
            return cls.__doc__
        cost = {"heavy": " Slow on large meshes: see the docs."}.get(preset.cost, "")
        needs = [
            catalog.FILL_LABELS[i.fill]
            for i in preset.object_inputs
            if i.fill not in (catalog.Fill.NONE, catalog.Fill.SURFACE)
        ]
        where = " Goes on the active mesh." if preset.active_only else ""
        if needs:
            where += " Uses " + "; ".join(needs) + "."
        if preset.output is catalog.Output.OBJECT:
            where += " Makes a new object of lines."
        if fast.is_fast(preset.id) and properties.engine == "BAKE":
            return (
                f"{preset.description}. Fast: computed once by a Cycles bake into a stored "
                f"layer, with no modifier.{where}"
            )
        return f"{preset.description}.{where}{cost}"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        if not target_objects(context):
            cls.poll_message_set("Select a mesh object")
            return False
        return True

    def _is_object(self) -> bool:
        return catalog.get(self.preset).output is catalog.Output.OBJECT

    def draw(self, _context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "preset")
        layout.prop(self, "name", text="Object" if self._is_object() else "Layer")
        if catalog.get(self.preset).north:
            layout.prop(self, "north")
        if not fast.is_fast(self.preset):
            return
        layout.prop(self, "engine")
        if self.engine != "BAKE":
            return
        if self.preset in fast.HORIZON:
            for prop in ("max_distance", "directions", "steps", "ray_offset"):
                layout.prop(self, prop)
        else:
            for prop in ("observer_height", "target_height", "view_distance", "view_offset"):
                layout.prop(self, prop)
            layout.prop(self, "drape")
            layout.prop_search(self, "occluders", bpy.data, "collections")

    def fast_inputs(self) -> dict[str, object]:
        """The fast path's inputs, by the preset's input names."""
        if self.preset in fast.HORIZON:
            return {
                "Max Distance": _typed(self.max_distance),
                "Directions": self.directions,
                "Steps": self.steps,
                "Ray Offset": _typed(self.ray_offset),
            }
        return {
            "Observer Height": _typed(self.observer_height),
            "Target Height": _typed(self.target_height),
            "Max Distance": _typed(self.view_distance),
            "Ray Offset": _typed(self.view_offset),
            "Drape": self.drape,
        }

    def _execute_fast(self, context: bpy.types.Context, preset: catalog.Preset) -> set[str]:
        from . import preset_bake  # it imports this module

        notes: list[str] = []
        try:
            objects = preset_targets(context, preset)
            observers = None
            if preset.id in fast.VIEWSHED:
                fills, notes = fill_inputs(context, preset, objects)
                observers = fills["Observers"]
            occluders = None
            if self.occluders:
                occluders = bpy.data.collections.get(self.occluders)
                if occluders is None:
                    raise PresetError(f"There is no collection {self.occluders!r}.")
            result = preset_bake.compute(
                context,
                preset.id,
                objects,
                name=self.name,
                inputs=self.fast_inputs(),
                observers=observers,
                occluders=occluders,
            )
        except (PresetError, ValueError, RuntimeError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        count = len(objects)
        message = (
            f"{preset.label} (fast) on {count} object{'s' * (count != 1)}, layer "
            f"{result['layer']}: {preset_bake.fast_summary(result)}"
        )
        self.report({"INFO"}, "; ".join([message, *notes, *result["notes"]]))
        redraw_ui(context)
        return {"FINISHED"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        preset = catalog.get(self.preset)
        if self.engine == "BAKE" and fast.is_fast(preset.id):
            return self._execute_fast(context, preset)
        try:
            objects = preset_targets(context, preset)
            name, notes = add_preset(context, preset, objects, name=self.name, north=self.north)
        except PresetError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        count = len(objects)
        if preset.output is catalog.Output.OBJECT:
            message = f"{preset.label} on {objects[0].name}: new object {name}"
        else:
            message = f"{preset.label} on {count} object{'s' * (count != 1)}, layer {name}"
        self.report({"INFO"}, "; ".join([message, *notes]))
        redraw_ui(context)
        return {"FINISHED"}


TOTALS = frozenset(
    {"cut_fill", "closed_volume", "least_cost_path", "intervisibility", "line_of_sight"}
)
"""Presets whose totals ``geotools.analysis_totals`` reads."""


class GEOTOOLS_OT_analysis_totals(bpy.types.Operator):
    """Read the totals of the active object's presets (volumes, walking time, obstruction), summed in binary64 from the evaluated mesh"""

    bl_idname = "geotools.analysis_totals"
    bl_label = "Read Totals"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        obj = context.active_object
        if obj is None or obj.type != "MESH":
            cls.poll_message_set("Make an object with an analysis preset active")
            return False
        return any(p.id in TOTALS for _m, p in preset_modifiers(obj))

    def execute(self, context: bpy.types.Context) -> set[str]:
        lines = totals(context, context.active_object)
        if not lines:
            self.report({"WARNING"}, "No totals to read")
            return {"CANCELLED"}
        self.report({"INFO"}, "; ".join(lines))
        return {"FINISHED"}


class GEOTOOLS_MT_analysis_presets(bpy.types.Menu):
    """The bundled Geometry Nodes analysis presets"""

    bl_idname = "GEOTOOLS_MT_analysis_presets"
    bl_label = "Add Analysis"

    def draw(self, _context: bpy.types.Context) -> None:
        layout = self.layout
        layout.operator_context = "EXEC_DEFAULT"
        for index, category in enumerate(catalog.Category):
            if index:
                layout.separator()
            layout.label(text=catalog.CATEGORY_LABELS[category])
            for preset in catalog.by_category(category):
                icon = "TIME" if preset.cost == "heavy" else "NONE"
                layout.operator(
                    GEOTOOLS_OT_analysis_add.bl_idname, text=preset.label, icon=icon
                ).preset = preset.id
                if preset.north:
                    op = layout.operator(
                        GEOTOOLS_OT_analysis_add.bl_idname, text=f"{preset.label} (True North)"
                    )
                    op.preset = preset.id
                    op.north = "TRUE"
                if fast.is_fast(preset.id):
                    op = layout.operator(
                        GEOTOOLS_OT_analysis_add.bl_idname,
                        text=f"{preset.label} (Fast Bake)",
                        icon="RENDER_STILL",
                    )
                    op.preset = preset.id
                    op.engine = "BAKE"


def draw_panel(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    """The Presets sub-panel: the Add Analysis menu and the active object's presets."""
    row = layout.row()
    row.scale_y = 1.4
    row.menu(GEOTOOLS_MT_analysis_presets.bl_idname, text="Add Analysis", icon="ADD")
    obj = context.active_object
    found = preset_modifiers(obj) if obj is not None and obj.type == "MESH" else []
    if not found:
        wrapped(layout, context, "Adds a Geometry Nodes modifier and a layer.", "INFO")
        return
    col = layout.column(align=True)
    for modifier, preset in found:
        icon = "MODIFIER_ON" if modifier.show_viewport else "MODIFIER_OFF"
        if preset.output is catalog.Output.OBJECT:
            names = ", ".join(a.name for a in preset.attributes)
            col.label(text=f"{preset.label}: lines ({names})", icon=icon)
            continue
        try:
            attribute = str(get_input(modifier, catalog.ATTRIBUTE_INPUT))
        except KeyError:
            attribute = "?"
        layer_name = attribute.removeprefix("gt_")
        item = find_layer(context.scene, layer_name)
        label = display_name(spec_from_props(item)) if item is not None else preset.label
        col.label(text=f"{label}: {attribute}", icon=icon)
    if any(p.id in TOTALS for _m, p in found):
        layout.operator(GEOTOOLS_OT_analysis_totals.bl_idname, icon="INFO")
    layout.label(text="Edit inputs on the modifier", icon="MODIFIER")


classes = (GEOTOOLS_OT_analysis_add, GEOTOOLS_OT_analysis_totals, GEOTOOLS_MT_analysis_presets)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()


def unregister() -> None:
    _unregister_classes()
