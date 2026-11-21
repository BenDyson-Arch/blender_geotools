"""Least-cost routes: directed travel times over one 3D surface, a route and a time layer.

The maths is bpy-free (``geotools.analysis.least_cost``); this module reads
one surface object (its loop triangles), the Start and End objects (or new
Empties at the 3D cursor, as the Least-Cost Path preset makes them) and an
optional cost layer, and writes:

* a line object, ``Least-Cost Route``, in the GeoTools Analysis collection:
  the route with ``elapsed`` (s from Start) per point and ``time`` per edge,
  exportable through Export Vectors;
* a FACE layer of the time from Start to each face's centre (s), and
  optionally a CATEGORY layer of isochrone bands, both publishable as variants.

One connected surface only: several objects are refused (connectors between
objects are issue #109). In a CRS project, plan distances are turned into
ground distances by the combined scale factor at the surface's centre.

``geotools.least_cost`` runs the search a few passes per timer tick when
invoked from the panel (Esc cancels), and straight through from ``execute``
(scripts, tests). Settings and the last result live on
``scene.geotools_least_cost``; each result in the Analyses list (``analyses``) keeps its own copy.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Generator

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..analysis import least_cost as lc
from ..core.layers import (
    Aggregation,
    DataType,
    Domain,
    LayerSpec,
    Origin,
    format_provenance,
    make_provenance,
)
from .common import extension_version, redraw_ui
from .layers import (
    ensure_layer,
    find_layer,
    layer_meshes,
    read_layer,
    refresh_layer,
    spec_from_props,
    write_layer,
)
from .properties import FieldError
from .source import analysis_notes, default_source
from .surface import (
    SURFACE_ITEMS,
    SurfaceError,
    ground_scale,
    output_collection,
    read_surface,
    remove_output,
    run_objects,
    surface_problem,
    write_lines,
)

log = logging.getLogger(__name__)

TOOL = "least_cost"
ROUTE_OBJECT = "Least-Cost Route"
START_NAME = "Route Start"
END_NAME = "Route End"
SNAP_NOTE = 0.5
"""Start or End this far (m) from the surface that can be crossed gets a note."""
STEP_SECONDS = 0.1
"""How long each timer tick of the modal search runs."""

MODEL_ITEMS = (
    (
        "TOBLER",
        "Tobler",
        "Tobler's hiking function (1993): fastest 5 % downhill, 5 km/h on the level",
    ),
    (
        "LANGMUIR",
        "Langmuir (r.walk)",
        "Naismith's rule with Langmuir's descent corrections, with GRASS r.walk's coefficients",
    ),
    ("NAISMITH", "Naismith", "5 km/h on the level plus an hour per 600 m of ascent"),
    (
        "WHEELCHAIR",
        "Wheelchair",
        "Manual wheelchair speeds, no running gradient steeper than Max Gradient either way",
    ),
)


class LeastCostRunError(RuntimeError):
    """The run cannot go ahead (bad settings, nothing to route)."""


class GeoToolsLeastCostSettings(bpy.types.PropertyGroup):
    """Settings and last result of least-cost routes (``scene.geotools_least_cost``)."""

    surface: EnumProperty(
        name="Surface",
        description="The mesh walked on: one object, a single connected surface",
        items=SURFACE_ITEMS,
        default="SELECTED",
    )
    start: PointerProperty(
        name="Start",
        description="Where routes start: this object's origin, snapped to the surface. "
        "Empty: a new Empty at the 3D cursor",
        type=bpy.types.Object,
    )
    end: PointerProperty(
        name="End",
        description="Where the route ends: this object's origin, snapped to the surface. "
        "Empty: a new Empty at the 3D cursor",
        type=bpy.types.Object,
    )
    model: EnumProperty(
        name="Model",
        description="How travel time depends on the slope walked",
        items=MODEL_ITEMS,
        default="TOBLER",
    )
    max_slope: FloatProperty(
        name="Max Slope",
        description="Faces steeper than this, in degrees, cannot be crossed (nor can faces "
        "facing down)",
        default=30.0,
        min=0.0,
        max=89.0,
        precision=1,
    )
    max_gradient: FloatProperty(
        name="Max Gradient",
        description="Wheelchair only: the steepest running gradient, up or down, in percent "
        "(8.33 % is 1:12, the ADA's maximum ramp slope)",
        default=100.0 / 12.0,
        min=0.1,
        max=100.0,
        precision=2,
    )
    cost_layer: StringProperty(
        name="Cost Layer",
        description="A FACE layer whose values multiply the time on each face (2 for dense "
        "scrub); 0, negative or no data is impassable. Empty for none",
        default="",
    )
    steiner: IntProperty(
        name="Steiner Points",
        description="Points added inside each triangle edge, so routes can cross faces at any "
        "angle; 0 follows mesh edges only (faster, up to 41 % longer routes on a grid)",
        default=3,
        min=0,
        max=16,
        soft_max=8,
    )
    make_route: BoolProperty(
        name="Route",
        description="Make a line object of the quickest route from Start to End, with the "
        "time at each point (exportable as vectors)",
        default=True,
    )
    make_layer: BoolProperty(
        name="Travel Time",
        description="Write the time from Start to every face's centre into a face layer",
        default=True,
    )
    time_layer: StringProperty(
        name="Time Layer",
        description="Layer that receives the travel time from Start, in seconds (created if "
        "missing)",
        default="travel_time",
    )
    band_minutes: FloatProperty(
        name="Isochrones",
        description="Also write bands of this many minutes into a category layer; 0 for none",
        default=0.0,
        min=0.0,
        soft_max=60.0,
        precision=1,
    )
    band_layer: StringProperty(
        name="Band Layer",
        description="Category layer that receives the isochrone bands (created if missing)",
        default="isochrones",
    )
    result: StringProperty(default="")
    error: StringProperty(default="")


def settings(context: bpy.types.Context) -> GeoToolsLeastCostSettings:
    return context.scene.geotools_least_cost


# --- the run ----------------------------------------------------------------------------------


@dataclasses.dataclass
class _Run:
    """A prepared run: the graph, Start and End on it, and the search in steps."""

    began: float
    timings: dict[str, float]
    notes: list[str]
    obj: bpy.types.Object
    writable: bool
    graph: lc.Graph
    start: lc.Anchor
    end: lc.Anchor | None
    steps: Generator[int, None, lc.Search]
    to_scene: tuple[np.ndarray, float]  # (centre, ground scale): ground xy -> scene xy
    csf: float
    parameters: dict

    def lap(self, what: str) -> None:
        self.timings[what] = round(time.perf_counter() - self.began - sum(self.timings.values()), 3)


def _layer_names(props: GeoToolsLeastCostSettings) -> list[tuple[str, str]]:
    names = []
    if props.make_layer:
        names.append((props.time_layer.strip(), "FLOAT"))
        if props.band_minutes > 0:
            names.append((props.band_layer.strip(), "CATEGORY"))
    return names


def _check(scene: bpy.types.Scene, props: GeoToolsLeastCostSettings) -> None:
    if not props.make_route and not props.make_layer:
        raise LeastCostRunError("Choose an output: Route, Travel Time or both.")
    names = _layer_names(props)
    if any(not n for n, _ in names) or len({n for n, _ in names}) != len(names):
        raise LeastCostRunError("The layers need different, non-empty names.")
    for name, data_type in names:
        item = find_layer(scene, name)
        if item is not None and (item.domain != "FACE" or item.data_type != data_type):
            raise LeastCostRunError(
                f"Layer {name} is a {item.domain.lower()} {item.data_type.lower()} layer; "
                f"choose another name (this one is a face {data_type.lower()} layer)."
            )
    cost = props.cost_layer.strip()
    if cost:
        item = find_layer(scene, cost)
        if item is None:
            raise LeastCostRunError(f"There is no layer {cost}.")
        if item.domain != "FACE" or item.data_type not in {"FLOAT", "INT"}:
            raise LeastCostRunError(
                f"Cost Layer {cost} must be a face layer of numbers (it is "
                f"{item.domain.lower()} {item.data_type.lower()})."
            )
        if cost in {n for n, _ in names}:
            raise LeastCostRunError("The cost layer cannot also receive the results.")


SELECT_SURFACE = "Select the mesh to route over"


def surface_issue(context: bpy.types.Context, props) -> str | None:
    """The operator's poll message when there is no surface to route over, or None."""
    exclude = [o for o in (props.start, props.end) if o is not None]
    return surface_problem(context, props.surface, exclude, select=SELECT_SURFACE)


def _surface(context: bpy.types.Context, props) -> bpy.types.Object:
    exclude = [o for o in (props.start, props.end) if o is not None]
    try:
        objects = run_objects(context, props.surface, exclude, select=SELECT_SURFACE)
    except SurfaceError as ex:
        raise LeastCostRunError(str(ex)) from None
    if not objects:
        raise LeastCostRunError("The source collection has no mesh to route over.")
    if len(objects) > 1:
        which = "selected" if props.surface == "SELECTED" else "in the source collection"
        raise LeastCostRunError(
            f"Least-cost routes work on one connected surface, and {len(objects)} meshes are "
            f"{which}. Select one mesh, or join them into one object (Object > Join). Routes "
            "across objects are issue #109."
        )
    return objects[0]


def _cursor_empty(scene: bpy.types.Scene, name: str) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    obj.empty_display_type = "SINGLE_ARROW"
    obj.location = scene.cursor.location
    output_collection(scene).objects.link(obj)
    return obj


def _ends(context: bpy.types.Context, props) -> list[str]:
    """Fill Start (and End, for a route) with new Empties at the 3D cursor where empty."""
    notes = []
    scene = context.scene
    if props.start is None:
        props.start = _cursor_empty(scene, START_NAME)
        notes.append(f"Start: new {props.start.name} at the 3D cursor.")
    if props.make_route and props.end is None:
        props.end = _cursor_empty(scene, END_NAME)
        notes.append(f"End: new {props.end.name} at the 3D cursor.")
    return notes


def _cost_factors(context, obj: bpy.types.Object, name: str, n_faces: int) -> np.ndarray | None:
    if not name:
        return None
    spec = spec_from_props(find_layer(context.scene, name))
    depsgraph = context.evaluated_depsgraph_get()
    ((_, mesh),) = list(layer_meshes(context.scene, [obj], depsgraph=depsgraph))
    try:
        values = read_layer(mesh, spec).astype(np.float64)
    except (KeyError, ValueError, RuntimeError) as ex:
        raise LeastCostRunError(f"Cost Layer {name}: {ex}") from None
    if len(values) != n_faces:
        raise LeastCostRunError(f"Cost Layer {name} has {len(values)} values for {n_faces} faces.")
    if spec.nodata is not None:
        values[values == spec.nodata] = np.nan
    return values


def prepare(context: bpy.types.Context) -> _Run:
    """Check the settings, read the surface, build the graph and snap Start and End."""
    scene = context.scene
    props = settings(context)
    _check(scene, props)
    began = time.perf_counter()
    obj = _surface(context, props)
    if obj.data.is_editmode:
        raise LeastCostRunError(f"{obj.name} is in Edit Mode; switch to Object Mode first.")
    notes = _ends(context, props)
    run_timings: dict[str, float] = {}
    arrays = read_surface(context, [obj], topology=False, triangles=True)
    notes += arrays.notes
    part = arrays.parts[0]
    n_faces = part.face_count
    factors = _cost_factors(context, obj, props.cost_layer.strip(), n_faces)
    positions = arrays.positions
    centre = 0.5 * (positions.min(axis=0) + positions.max(axis=0))
    csf, note = ground_scale(context, centre, "Times and lengths")
    if note:
        notes.append(note)
    scale = 1.0 / csf
    ground = positions.copy()
    ground[:, :2] = centre[:2] + (positions[:, :2] - centre[:2]) * scale
    mirrored = bool(np.linalg.det(np.array(obj.matrix_world)[:3, :3]) < 0)
    triangles = arrays.triangles
    try:
        graph = lc.build_graph(
            ground,
            triangles,
            model=props.model,
            max_slope=props.max_slope,
            steiner=props.steiner,
            tri_face=arrays.triangle_faces,
            face_factor=factors,
            flip=np.full(len(triangles), mirrored),
            max_gradient=props.max_gradient / 100.0,
        )
    except lc.LeastCostError as ex:
        raise LeastCostRunError(str(ex)) from None

    def to_ground(point) -> np.ndarray:
        p = np.array(point, dtype=np.float64)
        p[:2] = centre[:2] + (p[:2] - centre[:2]) * scale
        return p

    start = lc.snap(graph, to_ground(props.start.matrix_world.translation))
    end = None
    if props.make_route:
        end = lc.snap(graph, to_ground(props.end.matrix_world.translation))
        if np.linalg.norm(end.on_surface - start.on_surface) < 1e-6:
            raise LeastCostRunError(
                "Start and End are at the same place on the surface: move one of them "
                "(new Empties are made at the 3D cursor)."
            )
    parameters = {
        "model": props.model,
        "max_slope_deg": props.max_slope,
        "max_gradient_percent": props.max_gradient if props.model == "WHEELCHAIR" else None,
        "cost_layer": props.cost_layer.strip() or None,
        "steiner_points": props.steiner,
        "start": [float(c) for c in props.start.matrix_world.translation],
        "start_object": props.start.name,
        "combined_scale_factor": csf,
        "object": obj.name,
        "graph": "vertices and Steiner points, linked across each triangle; directed",
    }
    run = _Run(
        began=began,
        timings=run_timings,
        notes=notes,
        obj=obj,
        writable=part.writable,
        graph=graph,
        start=start,
        end=end,
        steps=lc.search_steps(graph, start, end=None if props.make_layer else end),
        to_scene=(centre, scale),
        csf=csf,
        parameters=parameters,
    )
    run.lap("graph")
    return run


def _scene_points(run: _Run, points: np.ndarray) -> np.ndarray:
    centre, scale = run.to_scene
    out = np.array(points, dtype=np.float64)
    out[:, :2] = centre[:2] + (out[:, :2] - centre[:2]) / scale
    return out


def _specs(props, categories: tuple[str, ...], provenance) -> list[LayerSpec]:
    minutes = props.band_minutes
    specs = [
        LayerSpec(
            name=props.time_layer.strip(),
            domain=Domain.FACE,
            data_type=DataType.FLOAT,
            units="s",
            origin=Origin.ANALYSIS,
            aggregation=Aggregation.MIN,
            label="Travel Time",
            description="Least travel time from Start to the face's centre "
            f"({props.model.title()} model), seconds",
            provenance=provenance,
        )
    ]
    if minutes > 0:
        specs.append(
            LayerSpec(
                name=props.band_layer.strip(),
                domain=Domain.FACE,
                data_type=DataType.CATEGORY,
                origin=Origin.ANALYSIS,
                aggregation=Aggregation.MODE,
                label="Isochrones",
                description=f"Travel time from Start in bands of {minutes:g} min",
                categories=categories,
                provenance=provenance,
            )
        )
    return specs


def _write_layers(context, run: _Run, props, times: np.ndarray, provenance) -> list[str]:
    scene = context.scene
    if not run.writable:
        raise LeastCostRunError(
            f"{run.obj.name}: its modifiers change the topology, so face values cannot be "
            "stored on its mesh; apply the modifiers, or turn off Travel Time."
        )
    values = [times.astype(np.float32)]
    categories: tuple[str, ...] = ()
    if props.band_minutes > 0:
        ids, categories = lc.bands(times, props.band_minutes * 60.0)
        values.append(ids)
    specs = _specs(props, categories, provenance)
    for new, array in zip(specs, values, strict=True):
        spec = new
        item = find_layer(scene, new.name)
        if item is not None:  # keep the user's own label and description
            current = spec_from_props(item)
            spec = dataclasses.replace(
                new,
                label=current.label or new.label,
                description=current.description or new.description,
            )
        try:
            ensure_layer(scene, spec)
        except ValueError as ex:
            raise LeastCostRunError(str(ex)) from None
        write_layer(run.obj.data, spec_from_props(find_layer(scene, spec.name)), array)
        item = find_layer(scene, spec.name)
        item.provenance = format_provenance(provenance)
        item.origin = Origin.ANALYSIS.value
        refresh_layer(scene, item)
    return [s.name for s in specs]


def finish(context: bpy.types.Context, run: _Run, found: lc.Search) -> dict:
    """Write the route and the layers from a finished search; store and return the result."""
    props = settings(context)
    scene = context.scene
    run.lap("search")
    # Before the layers' refresh: it may set Source, which the layer status reads.
    if props.make_layer:
        source_notes = analysis_notes(context, [run.obj])
    else:
        source_notes = [n for n in [default_source(scene, [run.obj])] if n]
    provenance = make_provenance(TOOL, run.parameters, extension_version())
    out: dict = {
        "faces": run.graph.n_faces,
        "triangles": run.graph.stats["triangles"],
        "nodes": run.graph.n,
        "passes": found.passes,
        "model": props.model,
        "start_snap": run.start.distance,
        "layers": [],
        "route": None,
    }
    if props.make_layer:
        times = lc.face_times(run.graph, found)
        reached = np.isfinite(times)
        out["layers"] = _write_layers(context, run, props, times, provenance)
        out["reached"] = int(reached.sum())
        out["max_time"] = float(times[reached].max()) if reached.any() else None
    if props.make_route:
        r = lc.route(run.graph, found, run.end)
        out["end_snap"] = run.end.distance
        if r is None:
            remove_output(ROUTE_OBJECT, TOOL + ".route")
            if not props.make_layer:
                raise LeastCostRunError(
                    "End cannot be reached from Start: check Max Slope, the cost layer, and "
                    "that both are on the same connected part of the surface."
                )
            run.notes.append("End cannot be reached from Start.")
        else:
            points = _scene_points(run, r.points)
            n = len(points)
            edges = np.stack([np.arange(n - 1), np.arange(1, n)], axis=1)
            obj = write_lines(
                scene,
                ROUTE_OBJECT,
                TOOL + ".route",
                points,
                edges,
                {"fid": np.ones(n - 1, np.int64), "time": np.diff(r.elapsed)},
                {"elapsed": r.elapsed},
            )
            obj["geotools_provenance"] = format_provenance(provenance)
            out["route"] = {"time": r.time, "length": r.length, "points": n}
    for what, anchor in (("Start", run.start), ("End", run.end)):
        if anchor is not None and anchor.distance > SNAP_NOTE:
            run.notes.append(
                f"{what} is {anchor.distance:,.2f} m from the nearest face that can be "
                "crossed, and is taken from there."
            )
    run.lap("outputs")
    out["seconds"] = time.perf_counter() - run.began
    out["timings"] = run.timings
    out["notes"] = run.notes
    out["source_notes"] = source_notes
    props.result = json.dumps(out)
    props.error = ""
    return out


def compute(context: bpy.types.Context) -> dict:
    """Run straight through with the scene's settings."""
    run = prepare(context)
    while True:
        try:
            next(run.steps)
        except StopIteration as done:
            return finish(context, run, done.value)


# --- the operator -------------------------------------------------------------------------------

_ERRORS = (
    LeastCostRunError,
    SurfaceError,
    lc.LeastCostError,
    FieldError,
    ValueError,
    RuntimeError,
    MemoryError,
)


def _minutes(seconds: float) -> str:
    return f"{seconds / 60.0:,.1f} min" if seconds >= 60 else f"{seconds:,.1f} s"


class GEOTOOLS_OT_least_cost(bpy.types.Operator):
    """The quickest route from Start to End over one surface, and the travel time from Start
    to every face, with a walking or wheelchair model (directed: uphill and downhill differ)"""

    bl_idname = "geotools.least_cost"
    bl_label = "Compute Least-Cost"
    bl_options = {"REGISTER", "UNDO"}

    _run: _Run | None = None
    _timer = None

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        problem = surface_issue(context, settings(context))
        if problem is not None:
            cls.poll_message_set(problem)
            return False
        return True

    def _fail(self, context, exc: BaseException) -> set[str]:
        props = settings(context)
        props.error = str(exc)
        props.result = ""
        self.report({"ERROR"}, str(exc))
        redraw_ui(context)
        return {"CANCELLED"}

    def _done(self, context, result: dict) -> set[str]:
        for note in result["notes"]:
            self.report({"WARNING"}, note)
        route = result["route"]
        what = (
            f"route {_minutes(route['time'])} over {route['length']:,.1f} m"
            if route
            else f"{result.get('reached', 0):,} faces reached"
        )
        message = f"Least-cost: {what}, {result['seconds']:.1f} s"
        self.report({"INFO"}, "; ".join([message, *result["source_notes"]]))
        redraw_ui(context)
        return {"FINISHED"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            result = compute(context)
        except _ERRORS as exc:
            return self._fail(context, exc)
        return self._done(context, result)

    def invoke(self, context: bpy.types.Context, event) -> set[str]:
        try:
            self._run = prepare(context)
        except _ERRORS as exc:
            return self._fail(context, exc)
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _stop(self, context) -> None:
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None
        context.workspace.status_text_set(None)

    def modal(self, context: bpy.types.Context, event) -> set[str]:
        if event.type == "ESC":
            self._stop(context)
            self._run = None
            self.report({"WARNING"}, "Least-cost cancelled.")
            return {"CANCELLED"}
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        run = self._run
        until = time.perf_counter() + STEP_SECONDS
        passes = 0
        try:
            while time.perf_counter() < until:
                passes = next(run.steps)
        except StopIteration as done:
            self._stop(context)
            try:
                result = finish(context, run, done.value)
            except _ERRORS as exc:
                return self._fail(context, exc)
            finally:
                self._run = None
            return self._done(context, result)
        except _ERRORS as exc:
            self._stop(context)
            self._run = None
            return self._fail(context, exc)
        elapsed = time.perf_counter() - run.began
        context.workspace.status_text_set(
            f"Least-cost: {passes:,} passes, {elapsed:.0f} s (Esc to cancel)"
        )
        return {"RUNNING_MODAL"}


classes = (GeoToolsLeastCostSettings, GEOTOOLS_OT_least_cost)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools_least_cost = bpy.props.PointerProperty(type=GeoToolsLeastCostSettings)


def unregister() -> None:
    del bpy.types.Scene.geotools_least_cost
    _unregister_classes()
