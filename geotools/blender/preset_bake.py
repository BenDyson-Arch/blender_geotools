"""Fast (bake) paths of the sky-view factor, openness and cumulative viewshed presets.

Add Analysis ▸ "(Fast Bake)" (``geotools.analysis_add`` with engine BAKE)
computes the preset's values once with the ray bake engine
(:mod:`.ray_bake`, the sun-hours engine) and stores them as an ordinary layer
(origin ANALYSIS) on the meshes, instead of adding a live Geometry Nodes
modifier. The rays, inputs and formulas are the preset's
(``geotools.analysis.preset_bake``):

* **Horizon presets**: one sample per vertex (its position and vertex normal,
  as Geometry Nodes' point domain), parked off-scene on the sample object. The
  probe chains one *direction* group per azimuth, each a chain of Steps
  *step* groups; a step computes its elevation from the bracket so far (the
  first is level), casts one Raycast node of length Max Distance from the
  offset origin, and narrows the bracket. The direction group adds the
  estimate and its sine to the running sum, so the bake emits (Σγ, Σ sin γ⁺, 1)
  per vertex. Values are spread onto the vertex's corners.
* **Cumulative viewshed**: one sample per face (centre and face normal). Each
  observer is one group with its eye as a socket value. The Raycast node's
  Direction is the segment eye − point itself and its Length 1: Cycles
  measures the length in units of the direction's, so only something between
  the face and the eye blocks it. Length 0 skips a face facing away or out of
  range. Six shader nodes per observer when every eye is in range: the
  shader, not the traversal, was most of the cost. Up to 1,024 observers per
  bake; numpy adds the bakes. The observer points (and Drape) come from the
  library's own Object Points and Drape groups, evaluated once.

Occluders are the meshes the analysis is added to (and, for the viewshed, the
Occluders collection): everything else is hidden from the render during the
bake. No Python loop runs over faces, vertices or rays.
"""

from __future__ import annotations

import logging
import time

import bpy
import numpy as np

from ..analysis import preset_bake as core
from ..analysis import presets as catalog
from ..core.issues import Severity
from ..core.layers import Domain, format_number, format_provenance, validate
from .bake_texture import _Saved
from .common import extension_version
from .layers import (
    ensure_layer,
    evaluated_depsgraph,
    find_layer,
    refresh_layer,
    remove_layer_data,
    source_objects,
    spec_from_props,
    write_layer,
)
from .ray_bake import (
    PROBE_UV,
    RAYS_PER_BAKE,
    VERTEX,
    Probe,
    RayBakeError,
    Run,
    bake,
    build_sample_object,
    collect_samples,
    combine,
    float_image,
    math_node,
    new_socket,
    parking,
    pixels,
    probe_material,
    raycast,
    render_only,
    separate,
    setup_cycles,
    vector_node,
)

log = logging.getLogger(__name__)

TEMP_PREFIX = "GeoTools Fast"


class FastPathError(RayBakeError):
    """The fast path cannot run; the message says why."""


# --- the horizon probe -------------------------------------------------------------------------


def _step_group(name: str, search: core.HorizonSearch):
    """One bisection step: State (lo, hi, 0) -> the bracket after one ray.

    Inputs State, Origin, Azimuth (sin a, cos a, 0) and Keep (0 on the first
    step: a level ray; else 1: the midpoint).
    """
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for socket in ("State", "Origin", "Azimuth"):
        new_socket(tree, socket, "Vector")
    new_socket(tree, "Keep", "Float")
    new_socket(tree, "State", "Vector", "OUTPUT")
    group_in = tree.nodes.new("NodeGroupInput")
    group_out = tree.nodes.new("NodeGroupOutput")
    get = group_in.outputs.get
    lo, hi, _ = separate(tree, get("State"))
    theta = math_node(tree, "MULTIPLY", math_node(tree, "ADD", lo, hi), 0.5)
    theta = math_node(tree, "MULTIPLY", theta, get("Keep"))
    rad = math_node(tree, "RADIANS", theta)
    flat = vector_node(tree, "SCALE", get("Azimuth"), scale=math_node(tree, "COSINE", rad))
    direction = vector_node(
        tree, "ADD", flat, combine(tree, 0.0, 0.0, math_node(tree, "SINE", rad))
    )
    hit = raycast(tree, get("Origin"), direction, search.max_distance).outputs["Is Hit"]
    # A hit raises the low end; from below (negative openness) a miss does.
    below = 1.0 if search.below else 0.0
    raise_low = math_node(tree, "MULTIPLY_ADD", hit, 1.0 - 2.0 * below, below)
    kept = combine(tree, lo, theta, 0.0)  # no raise: (lo, theta)
    raised = combine(tree, theta, hi, 0.0)  # raise: (theta, hi)
    change = vector_node(
        tree, "SCALE", vector_node(tree, "SUBTRACT", raised, kept), scale=raise_low
    )
    tree.links.new(vector_node(tree, "ADD", kept, change), group_out.inputs["State"])
    return tree


def _direction_group(name: str, step, search: core.HorizonSearch):
    """One azimuth: Sum + (estimate, sin(max(estimate, 0)), 0) after Steps steps."""
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    for socket in ("Sum", "Origin", "Azimuth"):
        new_socket(tree, socket, "Vector")
    new_socket(tree, "Sum", "Vector", "OUTPUT")
    group_in = tree.nodes.new("NodeGroupInput")
    group_out = tree.nodes.new("NodeGroupOutput")
    get = group_in.outputs.get
    state = combine(tree, search.lower, 90.0, 0.0)
    for index in range(search.steps):
        node = tree.nodes.new("ShaderNodeGroup")
        node.node_tree = step
        tree.links.new(state, node.inputs["State"])
        for socket in ("Origin", "Azimuth"):
            tree.links.new(get(socket), node.inputs[socket])
        node.inputs["Keep"].default_value = 0.0 if index == 0 else 1.0
        state = node.outputs["State"]
    lo, hi, _ = separate(tree, state)
    estimate = math_node(tree, "MULTIPLY", math_node(tree, "ADD", lo, hi), 0.5)
    sine = math_node(
        tree, "SINE", math_node(tree, "RADIANS", math_node(tree, "MAXIMUM", estimate, 0.0))
    )
    gains = combine(tree, estimate, sine, 0.0)
    tree.links.new(vector_node(tree, "ADD", get("Sum"), gains), group_out.inputs["Sum"])
    return tree


def _horizon_probe(run: Run, search: core.HorizonSearch) -> Probe:
    step = _step_group(f"{TEMP_PREFIX} Step", search)
    run.groups.append(step)
    direction = _direction_group(f"{TEMP_PREFIX} Direction", step, search)
    run.groups.append(direction)

    def head(tree, group_in) -> dict:
        lift = vector_node(tree, "SCALE", group_in.outputs["Normal"], scale=search.side)
        return {"Origin": vector_node(tree, "ADD", group_in.outputs["Position"], lift)}

    return Probe(run, TEMP_PREFIX, direction, ("Origin",), head)


# --- the viewshed probe ------------------------------------------------------------------------


def _observer_group(name: str, max_distance: float | None, target_height: float):
    """One observer: Sum + 1 when the face sees the eye.

    Inputs Sum (float), Point (the offset face centre, raised), Normal, Flat
    (the face centre with z = 0), Eye and Eye Flat (the eye with z = 0). The
    Raycast node's Direction is the segment Eye - Point itself, unnormalised,
    with Length 1: Cycles measures a Raycast's length in units of its
    direction's length, so the ray covers exactly the segment (a Blender test
    holds this to batch 2's segment-box test). Length 0 casts nothing: when
    the face faces away (with Target Height 0) or the eye is beyond Max
    Distance in plan. ``max_distance`` None: every eye is in range of every
    face, and the plan test is left out. Six shader nodes in the common case.
    """
    tree = bpy.data.node_groups.new(name, "ShaderNodeTree")
    new_socket(tree, "Sum", "Float")
    for socket in ("Point", "Normal", "Flat", "Eye", "Eye Flat"):
        new_socket(tree, socket, "Vector")
    new_socket(tree, "Sum", "Float", "OUTPUT")
    group_in = tree.nodes.new("NodeGroupInput")
    group_out = tree.nodes.new("NodeGroupOutput")
    get = group_in.outputs.get
    sight = vector_node(tree, "SUBTRACT", get("Eye"), get("Point"))
    go = 1.0
    if target_height <= 0.0:  # the face must face the eye
        dot = vector_node(tree, "DOT_PRODUCT", get("Normal"), sight, out="Value")
        go = math_node(tree, "GREATER_THAN", dot, 0.0)
    if max_distance is not None:  # plan <= Max Distance, as float32 plan < next float up
        plan = vector_node(tree, "DISTANCE", get("Eye Flat"), get("Flat"), out="Value")
        limit = float(np.nextafter(np.float32(max_distance), np.float32(np.inf)))
        near = math_node(tree, "GREATER_THAN", limit, plan)
        go = near if go == 1.0 else math_node(tree, "MULTIPLY", go, near)
    hit = raycast(tree, get("Point"), sight, go).outputs["Is Hit"]
    seen = math_node(tree, "SUBTRACT", go, hit)
    tree.links.new(math_node(tree, "ADD", get("Sum"), seen), group_out.inputs["Sum"])
    return tree


def _all_in_range(centres: np.ndarray, eyes: np.ndarray, max_distance: float) -> bool:
    """Whether every eye is within ``max_distance`` in plan of every centre (by their boxes)."""
    a_low, a_high = centres[:, :2].min(axis=0), centres[:, :2].max(axis=0)
    b_low, b_high = eyes[:, :2].min(axis=0), eyes[:, :2].max(axis=0)
    farthest = np.hypot(*np.maximum(np.abs(a_high - b_low), np.abs(b_high - a_low)))
    return bool(farthest < max_distance * (1.0 - 1e-6))


def _viewshed_probe(run: Run, values: dict, centres: np.ndarray, eyes: np.ndarray) -> Probe:
    reach = values["Max Distance"]
    observer = _observer_group(
        f"{TEMP_PREFIX} Observer",
        None if len(eyes) and _all_in_range(centres, eyes, reach) else reach,
        values["Target Height"],
    )
    run.groups.append(observer)

    def head(tree, group_in) -> dict:
        centre, normal = group_in.outputs["Position"], group_in.outputs["Normal"]
        lift = vector_node(tree, "SCALE", normal, scale=values["Ray Offset"])
        point = vector_node(tree, "ADD", centre, lift)
        point = vector_node(tree, "ADD", point, combine(tree, 0.0, 0.0, values["Target Height"]))
        flat = vector_node(tree, "MULTIPLY", centre, (1.0, 1.0, 0.0))
        return {"Point": point, "Normal": normal, "Flat": flat}

    through = ("Point", "Normal", "Flat")
    return Probe(run, TEMP_PREFIX, observer, through, head, sum_kind="Float")


# --- observers -----------------------------------------------------------------------------------


def observer_points(
    context: bpy.types.Context,
    observers: bpy.types.Object,
    surfaces: list[bpy.types.Object],
    *,
    drape: bool,
) -> np.ndarray:
    """The observer points in scene space, as the preset reads them, draped if asked.

    Evaluates the library's own Object Points and Drape groups (appended with
    the Cumulative Viewshed preset, then removed if they were new) on a
    temporary object: a mesh's vertices, a curve's evaluated points, a point
    cloud's points or else the object's origin; Drape drops each onto the
    topmost of ``surfaces`` by a vertical ray.
    """
    from . import analysis_presets  # it imports this module lazily too

    before = {g.as_pointer() for g in bpy.data.node_groups}
    analysis_presets.ensure_group(catalog.get("viewshed_cumulative"))
    appended = [g for g in bpy.data.node_groups if g.as_pointer() not in before]
    points_group = analysis_presets.find_group("_object_points", 1)
    drape_group = analysis_presets.find_group("_drape", 1)
    tree = bpy.data.node_groups.new(f"{TEMP_PREFIX} Observers", "GeometryNodeTree")
    mesh = bpy.data.meshes.new(f"{TEMP_PREFIX} Observers")
    obj = bpy.data.objects.new(mesh.name, mesh)
    try:
        if points_group is None or drape_group is None:
            raise FastPathError("The preset library lacks the Object Points or Drape group.")
        new_socket(tree, "Geometry", "Geometry", "OUTPUT")
        nodes, links = tree.nodes, tree.links
        out = nodes.new("NodeGroupOutput")
        node = nodes.new("GeometryNodeGroup")
        node.node_tree = points_group
        node.inputs["Object"].default_value = observers
        points = node.outputs["Points"]
        if drape:
            join = nodes.new("GeometryNodeJoinGeometry")
            for surface in surfaces:
                info = nodes.new("GeometryNodeObjectInfo")
                info.transform_space = "RELATIVE"
                info.inputs["Object"].default_value = surface
                links.new(info.outputs["Geometry"], join.inputs[0])
            realize = nodes.new("GeometryNodeRealizeInstances")
            links.new(join.outputs[0], realize.inputs[0])
            dropped = nodes.new("GeometryNodeGroup")
            dropped.node_tree = drape_group
            links.new(realize.outputs[0], dropped.inputs["Surface"])
            links.new(nodes.new("GeometryNodeInputPosition").outputs[0], dropped.inputs["Position"])
            moved = nodes.new("GeometryNodeSetPosition")
            links.new(points, moved.inputs["Geometry"])
            links.new(dropped.outputs["Position"], moved.inputs["Position"])
            points = moved.outputs["Geometry"]
        vertices = nodes.new("GeometryNodePointsToVertices")
        links.new(points, vertices.inputs["Points"])
        links.new(vertices.outputs[0], out.inputs[0])
        context.scene.collection.objects.link(obj)  # at the scene origin: scene space
        modifier = obj.modifiers.new("Observers", "NODES")
        modifier.node_group = tree
        evaluated = obj.evaluated_get(context.evaluated_depsgraph_get()).data
        co = np.empty(len(evaluated.vertices) * 3, np.float32)
        evaluated.vertices.foreach_get("co", co)
        return co.reshape(-1, 3).astype(np.float64)
    finally:
        bpy.data.objects.remove(obj)
        bpy.data.meshes.remove(mesh)
        bpy.data.node_groups.remove(tree)
        for group in appended:
            if group.name in bpy.data.node_groups and not group.users:
                bpy.data.node_groups.remove(group)


# --- running -------------------------------------------------------------------------------------


def _check_name(scene, preset: catalog.Preset, name: str) -> None:
    item = find_layer(scene, name)
    if item is not None and core.fast_preset_of(spec_from_props(item).provenance) != preset.id:
        raise FastPathError(
            f"Layer {name!r} already exists and is not a fast {preset.label} layer; give the "
            f"analysis another name (for example {preset.layer}_fast)."
        )
    others = [layer.name for layer in scene.geotools.layers if layer.name != name]
    errors = [
        i
        for i in validate(core.layer_spec(preset.id, name), others)
        if i.severity is Severity.ERROR
    ]
    if errors:
        raise FastPathError(errors[0].message)


def compute(
    context: bpy.types.Context,
    preset_id: str,
    objects: list[bpy.types.Object],
    *,
    name: str = "",
    inputs: dict | None = None,
    observers: bpy.types.Object | None = None,
    occluders: bpy.types.Collection | None = None,
) -> dict:
    """Compute a preset's values on ``objects`` with the ray bake; store them as a layer.

    Returns a summary: the layer, samples, rays per sample, bakes, seconds and
    their split (``timings``), notes. Raises ``FastPathError`` (or
    ``ValueError`` for an input out of range) before changing anything when it
    cannot run; a failed bake leaves no new layer.
    """
    scene = context.scene
    preset = catalog.get(preset_id)
    if not core.is_fast(preset_id):
        raise FastPathError(f"{preset.label} has no fast path.")
    if not hasattr(scene, "cycles"):
        raise FastPathError("Enable the Cycles add-on: the fast path uses a Cycles bake.")
    if not objects:
        raise FastPathError("Select one or more mesh objects first.")
    values = core.check_inputs(preset_id, inputs or {})
    name = (name or "").strip() or preset.layer
    _check_name(scene, preset, name)
    horizon = preset_id in core.HORIZON
    if not horizon and observers is None:
        raise FastPathError("The cumulative viewshed needs an Observers object.")

    began = time.perf_counter()
    timings: dict[str, float] = {}
    notes: list[str] = []
    eye = np.zeros((0, 3))
    if not horizon:
        points = observer_points(context, observers, objects, drape=bool(values["Drape"]))
        eye = core.eyes(points, values["Observer Height"])
        timings["observers"] = time.perf_counter() - began

    is_new = find_layer(scene, name) is None
    record = dict(values)
    if not horizon:
        record["Observers"] = observers.name
        record["Occluders"] = occluders.name if occluders is not None else None
    spec = core.layer_spec(
        preset_id,
        name,
        inputs=record,
        objects=tuple(o.name for o in objects),
        geotools_version=extension_version(),
    )
    ensure_layer(scene, spec)
    try:
        summary = _bake(context, preset_id, objects, values, eye, occluders, timings, notes)
    except BaseException:
        if is_new:
            layers = scene.geotools.layers
            index = layers.find(name)
            if index >= 0:
                remove_layer_data(scene, layers[index])
                layers.remove(index)
        raise
    started = time.perf_counter()
    samples = summary.pop("samples_object")
    spec = core.layer_spec(
        preset_id,
        name,
        inputs=record,
        objects=tuple(o.name for o in objects),
        run={k: summary[k] for k in ("samples", "rays_per_sample", "bakes")},
        geotools_version=extension_version(),
    )
    item = find_layer(scene, name)
    item.provenance = format_provenance(spec.provenance)
    item.origin = spec.origin.value
    for part, part_values in samples:
        write_layer(part.obj.data, spec_from_props(item), part_values)
    if is_new:
        item.presentation.palette = preset.palette
        if preset.display_range is not None:
            item.presentation.range_min = format_number(preset.display_range[0])
            item.presentation.range_max = format_number(preset.display_range[1])
    refresh_layer(scene, find_layer(scene, name))
    layers = scene.geotools.layers
    scene.geotools.layers_index = layers.find(name)
    timings["write"] = time.perf_counter() - started
    sources = {o.as_pointer() for o in source_objects(scene)}
    outside = [o.name for o in objects if o.as_pointer() not in sources]
    if outside:
        notes.append("Not in the source collection, so not published: " + ", ".join(outside[:3]))
    summary.update(
        layer=name,
        preset=preset_id,
        seconds=time.perf_counter() - began,
        timings=timings,
        notes=notes,
        observers=len(eye),
    )
    return summary


def _bake(context, preset_id, objects, values, eye, occluders, timings, notes) -> dict:
    """Sample, bake and turn the sums into values; returns the summary and the values."""
    scene = context.scene
    horizon = preset_id in core.HORIZON
    started = time.perf_counter()
    saved = _Saved.capture(context)
    run = Run()
    try:
        depsgraph = evaluated_depsgraph(scene)
        samples = collect_samples(scene, objects, VERTEX if horizon else Domain.FACE, depsgraph)
        notes.extend(samples.skipped)
        if not samples.parts:
            raise FastPathError(
                "No selected mesh can hold the values. " + " ".join(samples.skipped)
            )
        points = np.concatenate(samples.points)
        normals = np.concatenate(samples.normals)
        setup_cycles(saved, scene)
        keep = list(objects)
        if occluders is not None:
            keep += list(occluders.all_objects)
        missing = render_only(saved, context, keep)
        if missing:
            notes.append("Not in the view layer, so not occluding: " + ", ".join(missing[:3]))
        # Park the sample triangles below everything a ray can reach: segments
        # stay inside the hull of the samples and eyes; horizon rays reach Max
        # Distance from a sample.
        everything = np.concatenate([points, eye]) if len(eye) else points
        low, high = everything.min(axis=0), everything.max(axis=0)
        extent = float(np.max(high - low))
        reach = values["Max Distance"] if horizon else 0.0
        depth = reach + max(10.0, 0.01 * extent)
        at = parking(low, len(points), depth)
        obj, width, height = build_sample_object(
            saved, context, points, normals, name=f"{TEMP_PREFIX} Samples", at=at
        )
        image = float_image(f"{TEMP_PREFIX} Samples", width, height)
        run.images.append(image)
        timings["samples"] = time.perf_counter() - started

        if horizon:
            search = core.horizon_search(preset_id, values)
            probe = _horizon_probe(run, search)
            azimuths = core.azimuth_vectors(search.directions)
            batches = [[{"Azimuth": tuple(map(float, a))} for a in azimuths]]
        else:
            probe = _viewshed_probe(run, values, points, eye)
            # Each observer is one group node with its eye (and the eye at z = 0)
            # as socket values; the last block holds exactly the observers left.
            batches = [
                [
                    {"Eye": tuple(map(float, e)), "Eye Flat": (float(e[0]), float(e[1]), 0.0)}
                    for e in eye[i : i + RAYS_PER_BAKE]
                ]
                for i in range(0, max(1, len(eye)), RAYS_PER_BAKE)
            ]
        material = None
        total = None
        timings["bakes"] = []
        wm = context.window_manager
        wm.progress_begin(0, len(batches))
        try:
            for index, batch in enumerate(batches):
                began = time.perf_counter()
                group = probe.load(batch)
                if material is None:
                    material = probe_material(f"{TEMP_PREFIX} Probe", group, image, attributes=True)
                    saved.temp_materials.append(material)
                    obj.data.materials.append(material)
                else:
                    node = next(n for n in material.node_tree.nodes if n.type == "GROUP")
                    node.node_tree = group
                built = time.perf_counter()
                bake(context, [obj], PROBE_UV, error=FastPathError)
                sums = pixels(image)[: len(points), :3].astype(np.float64)
                total = sums if total is None else total + sums * (1.0, 1.0, 0.0)
                timings["bakes"].append(
                    {"probe": built - began, "bake": time.perf_counter() - built}
                )
                wm.progress_update(index + 1)
        finally:
            wm.progress_end()
    finally:
        unrestored = saved.restore(context)
        run.clean()
        if unrestored:
            notes.append(f"Could not restore the {', '.join(unrestored)}.")
    started = time.perf_counter()
    missing = int(np.count_nonzero(total[:, 2] <= 0.5))
    if missing:
        raise FastPathError(f"{missing:,} samples were not baked (texel layout).")
    if horizon:
        result = core.horizon_values(preset_id, total[:, 0], total[:, 1], search.directions)
        rays = search.directions * search.steps
    else:
        result = total[:, 0]
        rays = len(eye)
    out = []
    for i, part in enumerate(samples.parts):
        window = result[part.start : part.start + part.count]
        if horizon:
            window = window[samples.corner_vertex[i]]
        out.append((part, window.astype(np.float32)))
    timings["values"] = time.perf_counter() - started
    return {
        "samples": len(points),
        "rays_per_sample": int(rays),
        "bakes": len(batches),
        "samples_object": out,
    }


def fast_summary(result: dict) -> str:
    """One line for the report."""
    bakes = result["timings"].get("bakes", [])
    bake_seconds = sum(b["bake"] for b in bakes)
    return (
        f"{result['samples']:,} samples x {result['rays_per_sample']:,} rays, "
        f"{result['bakes']} bake{'s' * (result['bakes'] != 1)}, {result['seconds']:.1f} s "
        f"(bake {bake_seconds:.1f} s)"
    )
