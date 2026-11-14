"""3D surface hydrology: flow accumulation, ponding and depressions into layers.

The maths is bpy-free (``geotools.analysis.hydrology``); this module reads the
surface, casts the drops with Blender's Geometry Nodes Raycast
(``surface.cast_vertical``) and writes the results:

* FACE layers: flow accumulation (m², shown on a log scale), ponded depth (m)
  and depression (a CATEGORY: 0 none, then 1, 2 ... by volume);
* optionally a point object of the depressions (their pour points, with level,
  volume, area and depth) and a line object of the main flow paths, both
  exportable through Export Vectors (M8) and publishable as vectors.

Geometric only: no rainfall, infiltration or hydraulics. Settings and the last
result live on ``scene.geotools_hydrology``; the panel is in ``surface_ui``.
See docs/design/m5-analysis.md.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from ..analysis import hydrology as hy
from ..analysis import polylines, site
from ..analysis.slicing import lines_from_chains
from ..analysis.sun_hours import ray_offset
from ..core.layers import (
    Aggregation,
    DataType,
    Domain,
    LayerSpec,
    Origin,
    format_provenance,
    make_provenance,
)
from ..core.placement import PlacementMode
from .common import extension_version, redraw_ui
from .layers import ensure_layer, find_layer, refresh_layer, spec_from_props, write_layer
from .properties import FieldError, placement_from_props
from .surface import (
    SURFACE_ITEMS,
    SurfaceError,
    cast_vertical,
    datum,
    read_surface,
    remove_output,
    surface_objects,
    write_lines,
    write_points,
)

log = logging.getLogger(__name__)

TOOL = "hydrology"
DEPRESSIONS_OBJECT = "Depressions"
FLOW_OBJECT = "Flow Paths"
MAX_DEPRESSIONS = 1000
"""Depressions labelled one by one; smaller ones share the last category."""

GEOMETRIC_NOTE = (
    "Geometric only: where water would run and pond on this surface. No rainfall, "
    "infiltration, evaporation or hydraulics."
)

AREA_ITEMS = (
    ("PLAN", "Plan", "Plan (horizontal) area of upward faces: the area that catches rain"),
    ("SURFACE", "Surface", "True surface area of every face, walls included"),
)


class HydrologyRunError(RuntimeError):
    """The run cannot go ahead (bad settings, nothing to analyse)."""


class GeoToolsHydrologySettings(bpy.types.PropertyGroup):
    """Settings and last result of 3D surface hydrology (``scene.geotools_hydrology``)."""

    surface: EnumProperty(
        name="Surface",
        description="The meshes water runs over",
        items=SURFACE_ITEMS,
        default="SOURCE",
    )
    area: EnumProperty(
        name="Area",
        description="Which area of a face catches rain for the flow accumulation",
        items=AREA_ITEMS,
        default="PLAN",
    )
    accumulation_layer: StringProperty(
        name="Accumulation",
        description="Layer that receives the flow accumulation, m² (created if missing)",
        default="flow_accumulation",
    )
    depth_layer: StringProperty(
        name="Ponded Depth",
        description="Layer that receives the ponded depth, m (created if missing)",
        default="ponded_depth",
    )
    depression_layer: StringProperty(
        name="Depression",
        description="Category layer that receives each face's depression (created if missing)",
        default="depression",
    )
    min_depth: FloatProperty(
        name="Minimum Depth",
        description="Depressions shallower than this are filled but not listed or labelled "
        "(scene units)",
        default=0.01,
        min=0.0,
        soft_max=1.0,
        precision=3,
    )
    depression_points: BoolProperty(
        name="Depression Points",
        description="Make a point object of the depressions at their pour points, with level, "
        "volume, area and depth (exportable as vectors)",
        default=True,
    )
    flow_lines: BoolProperty(
        name="Flow Paths",
        description="Make a line object of the main flow paths (exportable as vectors)",
        default=False,
    )
    channel_area: FloatProperty(
        name="Channel From",
        description="Faces with at least this much area draining through them are on a "
        "flow path (m²)",
        default=1000.0,
        min=0.0,
        soft_max=1.0e6,
    )
    channel_count: IntProperty(
        name="Networks",
        description="Keep the flow paths of this many outlets, largest first (0 for all)",
        default=20,
        min=0,
    )
    offset: FloatProperty(
        name="Ray Offset",
        description="How far drops start off the surface, in scene units; 0 for automatic",
        default=0.0,
        min=0.0,
        soft_max=0.1,
        precision=4,
    )
    result: StringProperty(default="")
    error: StringProperty(default="")


def settings(context: bpy.types.Context) -> GeoToolsHydrologySettings:
    return context.scene.geotools_hydrology


def _names(props: GeoToolsHydrologySettings) -> list[str]:
    return [
        props.accumulation_layer.strip(),
        props.depth_layer.strip(),
        props.depression_layer.strip(),
    ]


def _ground_scale(context: bpy.types.Context, centre: np.ndarray) -> tuple[float, str]:
    """The combined scale factor at the surface's centre (1 when unknown) and a note."""
    try:
        placement = placement_from_props(context.scene.geotools)
    except FieldError:
        return 1.0, "Areas and volumes are in scene units (the project placement is not valid)."
    if PlacementMode(placement.mode) is not PlacementMode.SOURCE_CRS:
        return 1.0, ""
    if not site.available():
        return 1.0, "Areas and volumes are grid values: pyproj is needed for the scale factor."
    try:
        reading = site.read_point(placement, tuple(float(c) for c in centre))
    except (site.SiteError, site.GeodesyUnavailable, ValueError) as ex:
        return 1.0, f"Areas and volumes are grid values ({ex})."
    return float(reading.combined_scale), ""


def _specs(names: list[str], categories: tuple[str, ...], provenance) -> list[LayerSpec]:
    acc, depth, dep = names
    return [
        LayerSpec(
            name=acc,
            domain=Domain.FACE,
            data_type=DataType.FLOAT,
            units="m²",
            origin=Origin.ANALYSIS,
            aggregation=Aggregation.MAX,
            label="Flow Accumulation",
            description="Area draining through the face, its own included (geometric)",
            provenance=provenance,
        ),
        LayerSpec(
            name=depth,
            domain=Domain.FACE,
            data_type=DataType.FLOAT,
            units="m",
            origin=Origin.ANALYSIS,
            aggregation=Aggregation.MAX,
            label="Ponded Depth",
            description="Depth of water the face's depression holds when filled to its spill "
            "level (geometric)",
            provenance=provenance,
        ),
        LayerSpec(
            name=dep,
            domain=Domain.FACE,
            data_type=DataType.CATEGORY,
            origin=Origin.ANALYSIS,
            aggregation=Aggregation.MODE,
            label="Depression",
            description="The depression the face lies in, largest volume first (0 none)",
            categories=categories,
            provenance=provenance,
        ),
    ]


def _check_names(scene: bpy.types.Scene, names: list[str]) -> None:
    if len(set(names)) != len(names) or not all(names):
        raise HydrologyRunError("The three layers need three different, non-empty names.")
    for name, data_type in zip(names, ("FLOAT", "FLOAT", "CATEGORY"), strict=True):
        item = find_layer(scene, name)
        if item is not None and (item.domain != "FACE" or item.data_type != data_type):
            raise HydrologyRunError(
                f"Layer {name} is a {item.domain.lower()} {item.data_type.lower()} layer; "
                f"choose another name (this one is a face {data_type.lower()} layer)."
            )


def compute(context: bpy.types.Context) -> dict:
    """Run hydrology with the scene's settings; write layers and objects; store the result."""
    scene = context.scene
    props = settings(context)
    names = _names(props)
    _check_names(scene, names)
    began = time.perf_counter()
    timings: dict[str, float] = {}

    def lap(what: str) -> None:
        timings[what] = round(time.perf_counter() - began - sum(timings.values()), 3)

    objects = surface_objects(context, props.surface)
    arrays = read_surface(context, objects, topology=True)
    notes = list(arrays.notes)
    lap("read")
    surface = hy.surface_from_mesh(
        arrays.positions,
        arrays.face_starts,
        arrays.corner_verts,
        arrays.corner_edges,
        arrays.n_edges,
        flip=arrays.flip,
    )
    lap("graph")
    sample = surface.centroids[:: max(1, surface.n // 100_000)]
    offset = ray_offset(sample, props.offset)
    queries = hy.drop_queries(surface, offset)
    lo, hi = hy.face_bounds(arrays.positions, arrays.face_starts, arrays.corner_verts)
    target = hy.faces_under(queries.origin, lo, hi)
    hits = cast_vertical(
        context, arrays.positions, arrays.face_starts, arrays.corner_verts, target, queries.origin
    )
    drops = hy.resolve_drops(queries, surface, *hits)
    lap("drops")
    result = hy.analyse(surface, drops, area=props.area, min_depth=props.min_depth)
    lap("analysis")

    a_datum, height_name = datum(scene)
    centre = 0.5 * (surface.centroids.min(axis=0) + surface.centroids.max(axis=0))
    csf, note = _ground_scale(context, centre)
    if note:
        notes.append(note)
    area_scale, volume_scale = hy.ground_scale(csf)
    accumulation = result.accumulation * area_scale
    table = result.depressions
    labels = result.depression.copy()
    count = table.count
    categories = ["None"] + [f"Depression {i}" for i in range(1, min(count, MAX_DEPRESSIONS) + 1)]
    if count > MAX_DEPRESSIONS:
        labels[labels > MAX_DEPRESSIONS] = MAX_DEPRESSIONS + 1
        categories.append(f"Smaller ({count - MAX_DEPRESSIONS:,})")
        notes.append(
            f"{count:,} depressions: the {MAX_DEPRESSIONS:,} largest are labelled one by one."
        )

    parameters = {
        "graph": "faces linked by shared edges; steepest descent by drop over 3D distance",
        "area": props.area,
        "min_depth": props.min_depth,
        "drip_normal_z": hy.DRIP_NZ,
        "ray_offset": offset,
        "drops": "Geometry Nodes Raycast, down then up (upward faces only)",
        "combined_scale_factor": csf,
        "datum": height_name,
        "datum_offset": a_datum,
        "faces": surface.n,
        "objects": [p.obj.name for p in arrays.parts],
        "geometric_only": True,
    }
    provenance = make_provenance(TOOL, parameters, extension_version())
    specs = _specs(names, tuple(categories), provenance)
    created = [s.name for s in specs if find_layer(scene, s.name) is None]
    for spec in specs:
        item = find_layer(scene, spec.name)
        if item is not None:  # keep the user's own label and description
            current = spec_from_props(item)
            spec = dataclasses.replace(
                spec,
                label=current.label or spec.label,
                description=current.description or spec.description,
            )
        try:
            ensure_layer(scene, spec)
        except ValueError as ex:
            raise HydrologyRunError(str(ex)) from None
    if names[0] in created:
        find_layer(scene, names[0]).presentation.scale = "LOG"
    values = (
        accumulation.astype(np.float32),
        result.depth.astype(np.float32),
        labels.astype(np.int32),
    )
    for part in arrays.parts:
        if not part.writable:
            continue
        window = slice(part.face_start, part.face_start + part.face_count)
        for name, array in zip(names, values, strict=True):
            write_layer(part.obj.data, spec_from_props(find_layer(scene, name)), array[window])
    for name in names:
        item = find_layer(scene, name)
        item.provenance = format_provenance(provenance)
        item.origin = Origin.ANALYSIS.value
        refresh_layer(scene, item)
    lap("layers")

    if props.depression_points:
        if count:
            _write_depressions(scene, table, a_datum, area_scale, volume_scale)
        else:
            remove_output(DEPRESSIONS_OBJECT, TOOL + ".depressions")
    lines = 0
    if props.flow_lines:
        lines = _write_flow_paths(scene, surface, result, accumulation, props, area_scale)
    lap("outputs")

    top = [
        {
            "id": i + 1,
            "level": float(table.level[i] + a_datum),
            "volume": float(table.volume[i] * volume_scale),
            "area": float(table.area[i] * area_scale),
            "max_depth": float(table.max_depth[i]),
        }
        for i in range(min(count, 5))
    ]
    out = {
        "layers": names,
        "faces": surface.n,
        "objects": len(arrays.parts),
        "seconds": time.perf_counter() - began,
        "timings": timings,
        "stats": result.stats,
        "depressions": count,
        "ponded_volume": float(table.volume.sum() * volume_scale),
        "top": top,
        "datum": height_name,
        "max_accumulation": float(accumulation.max()),
        "flow_lines": lines,
        "notes": notes,
    }
    props.result = json.dumps(out)
    props.error = ""
    return out


def _write_depressions(scene, table: hy.Depressions, a_datum, area_scale, volume_scale):
    ids = np.arange(1, table.count + 1)
    write_points(
        scene,
        DEPRESSIONS_OBJECT,
        TOOL + ".depressions",
        table.pour_point,
        {
            "fid": ids,
            "depression": ids,
            "level": table.level + a_datum,
            "volume": table.volume * volume_scale,
            "area": table.area * area_scale,
            "max_depth": table.max_depth,
            "faces": table.faces.astype(np.int64),
            "catchment": table.catchment * area_scale,
        },
    )


def _write_flow_paths(scene, surface, result, accumulation, props, area_scale) -> int:
    """Lines through the face centres of the main flow paths, split at confluences."""
    ch = hy.channels(result, props.channel_area / area_scale, props.channel_count)
    if not len(ch.faces):
        remove_output(FLOW_OBJECT, TOOL + ".flow")
        return 0
    n = surface.n
    node = np.full(n, -1, dtype=np.int64)
    node[ch.faces] = np.arange(len(ch.faces))
    rec = result.receiver[ch.faces]
    linked = rec >= 0
    src = np.flatnonzero(linked)
    dst = node[rec[linked]]
    # A drop goes through where it lands.
    landing = result.drop_landing[ch.faces[src]]
    drop = ~np.isnan(landing[:, 0])
    land_node = len(ch.faces) + np.arange(int(drop.sum()))
    a = np.concatenate([src[~drop], src[drop], land_node])
    b = np.concatenate([dst[~drop], land_node, dst[drop]])
    points = np.concatenate([surface.centroids[ch.faces], landing[drop]])
    acc = np.concatenate([accumulation[ch.faces], accumulation[ch.faces[src[drop]]]])
    network = np.concatenate([ch.network, ch.network[src[drop]]])
    chains = polylines.chain(len(points), a, b)
    # Downstream: from the end with less water to the end with more.
    first = acc[chains.nodes[chains.offsets[:-1]]]
    last = acc[chains.nodes[chains.offsets[1:] - 1]]
    chains = polylines.reverse(chains, first > last)
    lines = lines_from_chains(points, chains)
    node_acc = acc[chains.nodes]
    poly = np.repeat(np.arange(chains.count), chains.lengths())
    line_acc = np.zeros(chains.count)
    np.maximum.at(line_acc, poly, node_acc)
    line_net = network[chains.nodes[chains.offsets[:-1]]]
    e = lines.edge_line
    write_lines(
        scene,
        FLOW_OBJECT,
        TOOL + ".flow",
        lines.positions,
        lines.edges,
        {
            "fid": e + 1,
            "accumulation": line_acc[e],
            "network": line_net[e],
            "length": lines.length[e],
        },
    )
    return chains.count


class GEOTOOLS_OT_hydrology(bpy.types.Operator):
    """Flow accumulation, ponded depth and depressions over the 3D surface, into face
    layers (geometric only: no rainfall, infiltration or hydraulics)"""

    bl_idname = "geotools.hydrology"
    bl_label = "Compute Hydrology"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = settings(context)
        try:
            result = compute(context)
        except (
            HydrologyRunError,
            SurfaceError,
            hy.HydrologyError,
            FieldError,
            ValueError,
            RuntimeError,
            MemoryError,
        ) as exc:
            props.error = str(exc)
            props.result = ""
            self.report({"ERROR"}, str(exc))
            redraw_ui(context)
            return {"CANCELLED"}
        for note in result["notes"]:
            self.report({"WARNING"}, note)
        self.report(
            {"INFO"},
            f"Hydrology on {result['faces']:,} faces: {result['depressions']:,} depressions, "
            f"{result['seconds']:.1f} s",
        )
        redraw_ui(context)
        return {"FINISHED"}


classes = (GeoToolsHydrologySettings, GEOTOOLS_OT_hydrology)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools_hydrology = bpy.props.PointerProperty(type=GeoToolsHydrologySettings)


def unregister() -> None:
    del bpy.types.Scene.geotools_hydrology
    _unregister_classes()
