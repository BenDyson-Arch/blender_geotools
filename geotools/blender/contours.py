"""Contours and sections out: line objects with elevation or chainage, for M8 export.

The cutting is bpy-free (``geotools.analysis.slicing``): numpy plane-triangle
intersection of the evaluated meshes, chained into polylines. This module
reads the surface, writes the lines as a mesh object of loose edges in the
``GeoTools Analysis`` collection, and keeps the settings and last result on
``scene.geotools_contours``. The panel is in ``surface_ui``.

* **Contours** at an interval from a base, in the project's height datum: a
  SOURCE_CRS project's levels are real-world heights (RL, local z plus the
  binary64 offset A), so a 1 m interval falls on whole metres of RL. Each line
  has ``elevation``, ``index`` (level number), ``major`` and ``closed``.
* **Sections** along the edges of a line object (drawn, or an imported M8
  line), as 3D polylines of the surface with ``chainage`` on every point and
  ``chainage_from``/``chainage_to`` on every piece. A multi-level surface gives
  several pieces, all kept.

Export Vectors writes either object as GeoPackage or GeoJSON lines with
absolute Z in the project CRS, and a Vectors build profile publishes them.
See docs/design/m5-analysis.md.
"""

from __future__ import annotations

import json
import math
import time

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from ..analysis import polylines, slicing
from .common import redraw_ui
from .properties import FieldError
from .surface import SURFACE_ITEMS, SurfaceError, datum, read_surface, surface_objects, write_lines

CONTOUR_TOOL = "contours"
SECTION_TOOL = "sections"


def _is_line_object(_self, obj: bpy.types.Object) -> bool:
    return obj.type == "MESH"


class GeoToolsContourSettings(bpy.types.PropertyGroup):
    """Settings and last results of contours and sections (``scene.geotools_contours``)."""

    surface: EnumProperty(name="Surface", items=SURFACE_ITEMS, default="SOURCE")
    # Text, so the full binary64 value is kept (a FloatProperty is single precision).
    interval: StringProperty(
        name="Interval",
        description="Height between contours, in metres of the project's height datum",
        default="1",
    )
    base: StringProperty(
        name="Base",
        description="A height the contours pass through (levels are base + k x interval), "
        "in the project's height datum (RL for a CRS project)",
        default="0",
    )
    major_every: IntProperty(
        name="Major Every",
        description="Every n-th level is a major (index) contour; 0 for none",
        default=5,
        min=0,
    )
    contour_name: StringProperty(
        name="Object", description="Name of the contour line object", default="Contours"
    )
    line: PointerProperty(
        name="Section Line",
        type=bpy.types.Object,
        poll=_is_line_object,
        description="Mesh whose edges are the section lines (drawn, or an imported line "
        "layer); empty for the active object",
    )
    selected_edges: BoolProperty(
        name="Selected Edges Only",
        description="Use only the line object's selected edges",
        default=False,
    )
    section_name: StringProperty(
        name="Object", description="Name of the section line object", default="Sections"
    )
    contour_result: StringProperty(default="")
    contour_error: StringProperty(default="")
    section_result: StringProperty(default="")
    section_error: StringProperty(default="")


def settings(context: bpy.types.Context) -> GeoToolsContourSettings:
    return context.scene.geotools_contours


def _number(text: str, what: str) -> float:
    try:
        value = float(text.strip())
    except ValueError:
        raise slicing.SliceError(f"{what} is not a number: {text!r}") from None
    if not math.isfinite(value):
        raise slicing.SliceError(f"{what} must be a finite number")
    return value


def make_contours(context: bpy.types.Context) -> dict:
    """Cut the surface into contours; write the line object; store and return the result."""
    props = settings(context)
    interval = _number(props.interval, "Interval")
    base = _number(props.base, "Base")
    name = props.contour_name.strip() or "Contours"
    began = time.perf_counter()
    arrays = read_surface(
        context, surface_objects(context, props.surface), topology=False, triangles=True
    )
    a_datum, height_name = datum(context.scene)
    lines = slicing.contours(
        arrays.positions,
        arrays.triangles,
        interval,
        base=base,
        datum=a_datum,
        major_every=props.major_every,
    )
    if not lines.count:
        raise slicing.SliceError("No contour level crosses the surface.")
    e = lines.edge_line
    values = lines.line_values
    obj = write_lines(
        context.scene,
        name,
        CONTOUR_TOOL,
        lines.positions,
        lines.edges,
        {
            "fid": e + 1,
            "elevation": values["elevation"][e],
            "index": values["index"][e],
            "major": values["major"][e],
            "closed": lines.closed[e],
            "length": lines.length[e],
        },
    )
    elevation = values["elevation"]
    result = {
        "object": obj.name,
        "lines": lines.count,
        "closed": int(lines.closed.sum()),
        "levels": len(np.unique(values["index"])),
        "lowest": float(elevation.min()),
        "highest": float(elevation.max()),
        "datum": height_name,
        "triangles": len(arrays.triangles),
        "seconds": time.perf_counter() - began,
        "notes": [],
    }
    props.contour_result = json.dumps(result)
    props.contour_error = ""
    return result


def section_path(
    context: bpy.types.Context, obj: bpy.types.Object, selected_only: bool
) -> tuple[np.ndarray, np.ndarray]:
    """The line object's edges as polylines: world points and their offsets.

    Read from the object's own mesh (where edges are selected), not the evaluated
    one: a section runs along the line as drawn or imported, in plan.
    """
    mesh = obj.data
    co = np.empty(3 * len(mesh.vertices), np.float32)
    mesh.vertices.foreach_get("co", co)
    edges = np.empty(2 * len(mesh.edges), np.int32)
    mesh.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    if selected_only:
        select = np.empty(len(mesh.edges), bool)
        mesh.edges.foreach_get("select", select)
        edges = edges[select]
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    world = co.reshape(-1, 3).astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3]
    if not len(edges):
        raise slicing.SliceError(f"{obj.name} has no {'selected ' if selected_only else ''}edges.")
    chains = polylines.chain(len(world), edges[:, 0], edges[:, 1])
    # Each line runs the way most of its edges were drawn (an imported line's
    # edges go from its first point to its last), so chainage starts there.
    step_from = polylines.edges(chains)[:, 0]
    forward = (edges[chains.segments, 0] == step_from).astype(np.int64)
    steps = chains.lengths() - 1
    along = np.bincount(chains.polyline_of_step(), weights=forward, minlength=chains.count)
    chains = polylines.reverse(chains, 2 * along < steps)
    return world[chains.nodes], chains.offsets


def make_sections(context: bpy.types.Context) -> dict:
    """Cut the surface along the section line; write the line object; store the result."""
    props = settings(context)
    line = props.line or context.active_object
    if line is None or line.type != "MESH":
        raise slicing.SliceError("Choose a section line: a mesh object whose edges are the line.")
    name = props.section_name.strip() or "Sections"
    began = time.perf_counter()
    path, offsets = section_path(context, line, props.selected_edges)
    made = bpy.data.objects.get(name)
    exclude = {line} | ({made} if made is not None else set())
    arrays = read_surface(
        context,
        surface_objects(context, props.surface, exclude=exclude),
        topology=False,
        triangles=True,
    )
    lines = slicing.sections(arrays.positions, arrays.triangles, path, offsets)
    e = lines.edge_line
    values = lines.line_values
    obj = write_lines(
        context.scene,
        name,
        SECTION_TOOL,
        lines.positions,
        lines.edges,
        {
            "fid": e + 1,
            "section": values["section"][e] + 1,
            "chainage_from": values["chainage_from"][e],
            "chainage_to": values["chainage_to"][e],
            "length": lines.length[e],
        },
        {"chainage": lines.point_values["chainage"]},
    )
    a_datum, height_name = datum(context.scene)
    z = lines.positions[:, 2] + a_datum
    result = {
        "object": obj.name,
        "line": line.name,
        "sections": len(offsets) - 1,
        "pieces": lines.count,
        "length": float(lines.length.sum()),
        "lowest": float(z.min()),
        "highest": float(z.max()),
        "datum": height_name,
        "seconds": time.perf_counter() - began,
        "notes": [],
    }
    props.section_result = json.dumps(result)
    props.section_error = ""
    return result


class _Run:
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        return True

    def _run(self, context, work, result_attr: str, error_attr: str, done) -> set[str]:
        props = settings(context)
        try:
            result = work(context)
        except (slicing.SliceError, SurfaceError, FieldError, ValueError, MemoryError) as exc:
            setattr(props, error_attr, str(exc))
            setattr(props, result_attr, "")
            self.report({"ERROR"}, str(exc))
            redraw_ui(context)
            return {"CANCELLED"}
        self.report({"INFO"}, done(result))
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_contours(_Run, bpy.types.Operator):
    """Contour lines of the surface at the interval, in the project's height datum, as a line
    object with elevations (Export Vectors writes it as GeoPackage or GeoJSON lines with Z)"""

    bl_idname = "geotools.contours"
    bl_label = "Make Contours"

    def execute(self, context: bpy.types.Context) -> set[str]:
        return self._run(
            context,
            make_contours,
            "contour_result",
            "contour_error",
            lambda r: (
                f"{r['lines']:,} contour lines at {r['levels']:,} levels, {r['seconds']:.1f} s"
            ),
        )


class GEOTOOLS_OT_sections(_Run, bpy.types.Operator):
    """Sections of the surface along the section line's edges, as 3D lines with chainage
    (Export Vectors writes them as GeoPackage or GeoJSON lines with Z)"""

    bl_idname = "geotools.sections"
    bl_label = "Make Sections"

    def execute(self, context: bpy.types.Context) -> set[str]:
        return self._run(
            context,
            make_sections,
            "section_result",
            "section_error",
            lambda r: f"{r['pieces']:,} section pieces along {r['line']}, {r['seconds']:.1f} s",
        )


classes = (GeoToolsContourSettings, GEOTOOLS_OT_contours, GEOTOOLS_OT_sections)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Scene.geotools_contours = PointerProperty(type=GeoToolsContourSettings)


def unregister() -> None:
    del bpy.types.Scene.geotools_contours
    _unregister_classes()
