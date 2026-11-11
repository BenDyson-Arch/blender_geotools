"""Build the analysis preset library: stock Geometry Nodes groups in a .blend.

Runs inside Blender, against the source checkout (nothing is installed):

    blender --background --factory-startup --python scripts/make_analysis_presets.py -- \\
        [--out geotools/assets/analysis_presets.blend] [--check]

It builds one node group per preset in ``geotools/analysis/presets.py`` (the
catalog: names, versions, inputs and defaults), plus four helper groups they
share, marks the presets as assets in two catalogs (GeoTools/Terrain Shape and
GeoTools/Archaeological Visualisation) and writes them to ``--out`` with a
``blender_assets.cats.txt`` beside it. Every node is a stock node, so a user
can open and edit any of them; GeoTools only appends them ("Add Analysis").

``--check`` builds the groups in memory and compares them, node by node, with
the groups in the existing ``--out`` file. It exits 1 and lists what differs
when the file is out of date with this script or the catalog; the Blender tests
run it. A .blend is not byte-reproducible, so the comparison is of a canonical
description (``describe``), not of the bytes.

The formulas, inputs and limits are in docs/analysis-presets.md.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from geotools.analysis import presets as catalog

DEFAULT_OUT = REPO_ROOT / "geotools" / "assets" / catalog.LIBRARY_FILE

CATALOG_IDS = {
    catalog.Category.TERRAIN: "7d1f6a52-3c1e-4f0b-9a57-2a8e5c4b6f01",
    catalog.Category.ARCHAEOLOGY: "b3c9e2d4-8a7f-4e61-b5d0-6f1a2c3e4d02",
}
HELPER_VERSION = 1

WINDOW_HEIGHTS = "GeoTools Window Heights"
DERIVATIVES = "GeoTools Surface Derivatives"
MEAN_RISE = "GeoTools Mean Rise"
HORIZON = "GeoTools Horizon"

GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))  # radians, for sunflower sampling
NEIGHBOURS = ("NW", "N", "NE", "W", "E", "SW", "S", "SE")
OFFSETS = {
    "NW": (-1, 1),
    "N": (0, 1),
    "NE": (1, 1),
    "W": (-1, 0),
    "E": (1, 0),
    "SW": (-1, -1),
    "S": (0, -1),
    "SE": (1, -1),
}
TEMP_PREFIX = ".gt_tmp_"

_SOCKET_TYPES = {
    "GEOMETRY": "NodeSocketGeometry",
    "FLOAT": "NodeSocketFloat",
    "INT": "NodeSocketInt",
    "BOOLEAN": "NodeSocketBool",
    "STRING": "NodeSocketString",
    "VECTOR": "NodeSocketVector",
}


# --- a small builder over the node API ----------------------------------------------


class Tree:
    """A Geometry Nodes group under construction, with terse node helpers.

    Values passed to a helper are linked when they are sockets and set as the
    input's default otherwise.
    """

    def __init__(self, name: str, description: str, ident: str, version: int) -> None:
        import bpy

        self.bpy = bpy
        self.tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
        self.tree.description = description
        self.tree[catalog.PRESET_KEY] = ident
        self.tree[catalog.VERSION_KEY] = version
        self.nodes, self.links = self.tree.nodes, self.tree.links
        self.inp = self.nodes.new("NodeGroupInput")
        self.out = self.nodes.new("NodeGroupOutput")
        self._nan = None

    # interface
    def socket(
        self,
        name: str,
        in_out: str,
        kind: str,
        *,
        default=None,
        description: str = "",
        min=None,
        max=None,
        subtype: str = "NONE",
    ):
        item = self.tree.interface.new_socket(name, in_out=in_out, socket_type=_SOCKET_TYPES[kind])
        item.description = description
        if subtype != "NONE":
            item.subtype = subtype
        if min is not None:
            item.min_value = min
        if max is not None:
            item.max_value = max
        if default is not None:
            item.default_value = default
        return item

    def preset_input(self, spec: catalog.PresetInput):
        return self.socket(
            spec.name,
            "INPUT",
            spec.kind,
            default=spec.default,
            description=spec.description,
            min=spec.min,
            max=spec.max,
            subtype=spec.subtype,
        )

    def i(self, name: str):
        return self.inp.outputs[name]

    def output(self, name: str, value) -> None:
        self.feed(self.out.inputs[name], value)

    # nodes
    def node(self, kind: str, inputs: dict | None = None, label: str = "", **props):
        node = self.nodes.new(kind)
        for key, value in props.items():
            setattr(node, key, value)
        if label:
            node.label = label
        for key, value in (inputs or {}).items():
            self.feed(node.inputs[key], value)
        return node

    def feed(self, socket, value) -> None:
        if isinstance(value, self.bpy.types.NodeSocket):
            self.links.new(value, socket)
        else:
            socket.default_value = value

    def math(self, op: str, *args, label: str = ""):
        node = self.node("ShaderNodeMath", operation=op, label=label)
        for index, arg in enumerate(args):
            self.feed(node.inputs[index], arg)
        return node.outputs[0]

    def add(self, a, b):
        return self.math("ADD", a, b)

    def sub(self, a, b):
        return self.math("SUBTRACT", a, b)

    def mul(self, a, b):
        return self.math("MULTIPLY", a, b)

    def div(self, a, b):
        return self.math("DIVIDE", a, b)

    def total(self, values: Iterable, label: str = ""):
        values = list(values)
        result = values[0]
        for value in values[1:]:
            result = self.add(result, value)
        if label:
            result.node.label = label
        return result

    def vmath(self, op: str, *args, out: str = "Vector"):
        node = self.node("ShaderNodeVectorMath", operation=op)
        for index, arg in enumerate(args):
            self.feed(node.inputs[index], arg)
        return node.outputs[out]

    def combine(self, x=0.0, y=0.0, z=0.0, label: str = ""):
        return self.node("ShaderNodeCombineXYZ", {"X": x, "Y": y, "Z": z}, label=label).outputs[0]

    def separate(self, vector):
        node = self.node("ShaderNodeSeparateXYZ", {"Vector": vector})
        return node.outputs["X"], node.outputs["Y"], node.outputs["Z"]

    def compare(self, op: str, a, b, kind: str = "FLOAT"):
        node = self.node("FunctionNodeCompare", {"A": a, "B": b}, data_type=kind, operation=op)
        return node.outputs["Result"]

    def boolean(self, op: str, a, b=None):
        node = self.node("FunctionNodeBooleanMath", operation=op)
        self.feed(node.inputs[0], a)
        if b is not None:
            self.feed(node.inputs[1], b)
        return node.outputs[0]

    def switch(self, kind: str, condition, false, true, label: str = ""):
        node = self.node("GeometryNodeSwitch", input_type=kind, label=label)
        self.feed(node.inputs["Switch"], condition)
        self.feed(node.inputs["False"], false)
        self.feed(node.inputs["True"], true)
        return node.outputs[0]

    def nan(self):
        """A float NaN: the no-data value of a FLOAT layer."""
        if self._nan is None:
            node = self.node("ShaderNodeValue", label="No Data (NaN)")
            node.outputs[0].default_value = float("nan")
            self._nan = node.outputs[0]
        return self._nan

    def or_nan(self, valid, value, label: str = "Or No Data"):
        return self.switch("FLOAT", valid, self.nan(), value, label=label)

    def position(self):
        return self.node("GeometryNodeInputPosition").outputs[0]

    def normal(self):
        return self.node("GeometryNodeInputNormal").outputs["Normal"]

    def named(self, name: str):
        return self.node(
            "GeometryNodeInputNamedAttribute", {"Name": name}, data_type="FLOAT"
        ).outputs["Attribute"]

    def store(self, geometry, name, value, domain: str, label: str = ""):
        node = self.node(
            "GeometryNodeStoreNamedAttribute",
            {"Geometry": geometry, "Name": name, "Value": value},
            data_type="FLOAT",
            domain=domain,
            label=label,
        )
        return node.outputs["Geometry"]

    def remove(self, geometry, *names: str):
        for name in names:
            geometry = self.node(
                "GeometryNodeRemoveAttribute", {"Geometry": geometry, "Name": name}
            ).outputs["Geometry"]
        return geometry

    def capture(self, geometry, domain: str, items: dict, label: str = ""):
        node = self.node("GeometryNodeCaptureAttribute", domain=domain, label=label)
        for name in items:
            node.capture_items.new("FLOAT", name)
        self.feed(node.inputs["Geometry"], geometry)
        for name, value in items.items():
            self.feed(node.inputs[name], value)
        return node.outputs["Geometry"], {name: node.outputs[name] for name in items}

    def raycast(self, target, source, direction, length, label: str = ""):
        node = self.node(
            "GeometryNodeRaycast",
            {
                "Target Geometry": target,
                "Source Position": source,
                "Ray Direction": direction,
                "Ray Length": length,
            },
            label=label,
        )
        return node.outputs["Is Hit"], node.outputs["Hit Position"]

    def group(self, tree, inputs: dict, label: str = ""):
        node = self.node("GeometryNodeGroup", label=label)
        node.node_tree = tree
        for key, value in inputs.items():
            self.feed(node.inputs[key], value)
        return node

    def repeat(self, geometry, iterations, label: str = ""):
        """A Repeat zone carrying a geometry; returns (input node, output node)."""
        start = self.node("GeometryNodeRepeatInput", label=label)
        end = self.node("GeometryNodeRepeatOutput", label=label)
        start.pair_with_output(end)
        self.feed(start.inputs["Geometry"], geometry)
        self.feed(start.inputs["Iterations"], iterations)
        return start, end

    def finish(self) -> object:
        _layout(self.tree)
        return self.tree


def _layout(tree) -> None:
    """Columns by the longest path from the inputs, rows in creation order."""
    incoming: dict[str, list[str]] = {node.name: [] for node in tree.nodes}
    for link in tree.links:
        incoming[link.to_node.name].append(link.from_node.name)
    for node in tree.nodes:  # a zone's output sits after its input
        if node.bl_idname == "GeometryNodeRepeatInput" and node.paired_output is not None:
            incoming[node.paired_output.name].append(node.name)
    depth: dict[str, int] = {}

    def visit(name: str, stack: tuple[str, ...] = ()) -> int:
        if name in depth:
            return depth[name]
        sources = [s for s in incoming[name] if s not in stack]
        depth[name] = 1 + max((visit(s, (*stack, name)) for s in sources), default=-1)
        return depth[name]

    for node in tree.nodes:
        visit(node.name)
    heights: dict[int, float] = {}
    for node in tree.nodes:
        column = depth[node.name]
        top = heights.get(column, 0.0)
        node.location = (column * 220.0, -top)
        rows = len([s for s in node.inputs if s.enabled]) + len(
            [s for s in node.outputs if s.enabled]
        )
        heights[column] = top + 60.0 + 22.0 * rows


# --- helper groups -------------------------------------------------------------------


def _ray_down_height(t: Tree, target, x, y, z, height):
    """Height of the topmost surface at (x, y) within ``height`` of ``z``, and whether hit."""
    source = t.combine(x, y, t.add(z, height))
    hit, where = t.raycast(target, source, (0.0, 0.0, -1.0), t.mul(height, 2.0))
    return hit, t.separate(where)[2]


def build_window_heights():
    t = Tree(
        WINDOW_HEIGHTS,
        "Heights of the 8 neighbours of a 3 x 3 window around each element, relative to "
        "its own height, read by vertical rays from above (2.5D). Valid is false when a "
        "ray misses",
        "_window_heights",
        HELPER_VERSION,
    )
    t.socket("Target", "INPUT", "GEOMETRY", description="Surface the heights are read from")
    t.socket("Window", "INPUT", "FLOAT", default=0.1, min=0.0001, subtype="DISTANCE")
    t.socket("Search Height", "INPUT", "FLOAT", default=2.0, min=0.001, subtype="DISTANCE")
    for name in NEIGHBOURS:
        t.socket(name, "OUTPUT", "FLOAT", description=f"Height of the {name} neighbour")
    t.socket("Valid", "OUTPUT", "BOOLEAN", description="Every ray found the surface")
    x, y, z = t.separate(t.position())
    w = t.i("Window")
    valid = None
    for name in NEIGHBOURS:
        dx, dy = OFFSETS[name]
        hit, height = _ray_down_height(
            t,
            t.i("Target"),
            t.add(x, t.mul(w, float(dx))) if dx else x,
            t.add(y, t.mul(w, float(dy))) if dy else y,
            z,
            t.i("Search Height"),
        )
        t.output(name, t.sub(height, z))
        valid = hit if valid is None else t.boolean("AND", valid, hit)
    t.output("Valid", valid)
    return t.finish()


def build_derivatives(window):
    t = Tree(
        DERIVATIVES,
        "Partial derivatives of height over the 3 x 3 window (Evans-Young): p = dz/dx, "
        "q = dz/dy, r = d2z/dx2, s = d2z/dxdy, t = d2z/dy2",
        "_surface_derivatives",
        HELPER_VERSION,
    )
    t.socket("Target", "INPUT", "GEOMETRY")
    t.socket("Window", "INPUT", "FLOAT", default=0.1, min=0.0001, subtype="DISTANCE")
    t.socket("Search Height", "INPUT", "FLOAT", default=2.0, min=0.001, subtype="DISTANCE")
    for name in ("p", "q", "r", "s", "t"):
        t.socket(name, "OUTPUT", "FLOAT")
    t.socket("Valid", "OUTPUT", "BOOLEAN")
    node = t.group(
        window,
        {
            "Target": t.i("Target"),
            "Window": t.i("Window"),
            "Search Height": t.i("Search Height"),
        },
    )
    z = {n: node.outputs[n] for n in NEIGHBOURS}  # the centre's own height is 0
    w = t.i("Window")
    w2 = t.mul(w, w)
    six_w, three_w2, four_w2 = t.mul(w, 6.0), t.mul(w2, 3.0), t.mul(w2, 4.0)
    east = t.total((z["NE"], z["E"], z["SE"]))
    west = t.total((z["NW"], z["W"], z["SW"]))
    north = t.total((z["NW"], z["N"], z["NE"]))
    south = t.total((z["SW"], z["S"], z["SE"]))
    corners = t.total((z["NW"], z["NE"], z["SW"], z["SE"]))
    t.output("p", t.div(t.sub(east, west), six_w))
    t.output("q", t.div(t.sub(north, south), six_w))
    across_x = t.add(corners, t.add(z["W"], z["E"]))
    across_y = t.add(corners, t.add(z["N"], z["S"]))
    t.output("r", t.div(t.sub(across_x, t.mul(t.add(z["N"], z["S"]), 2.0)), three_w2))
    t.output("t", t.div(t.sub(across_y, t.mul(t.add(z["W"], z["E"]), 2.0)), three_w2))
    t.output(
        "s",
        t.div(t.sub(t.add(z["NE"], z["SW"]), t.add(z["NW"], z["SE"])), four_w2),
    )
    t.output("Valid", node.outputs["Valid"])
    return t.finish()


def build_mean_rise():
    t = Tree(
        MEAN_RISE,
        "Mean height of the surface over an annulus around each point, relative to the "
        "point, from vertical rays on a sunflower pattern (2.5D). Stores two temporary "
        "point attributes; the caller removes them",
        "_mean_rise",
        HELPER_VERSION,
    )
    t.socket("Geometry", "INPUT", "GEOMETRY")
    t.socket("Inner Radius", "INPUT", "FLOAT", default=0.0, min=0.0, subtype="DISTANCE")
    t.socket("Outer Radius", "INPUT", "FLOAT", default=0.5, min=0.0001, subtype="DISTANCE")
    t.socket("Samples", "INPUT", "INT", default=24, min=1, max=1024)
    t.socket("Search Height", "INPUT", "FLOAT", default=2.0, min=0.001, subtype="DISTANCE")
    t.socket("Geometry", "OUTPUT", "GEOMETRY")
    t.socket(
        "Mean Rise", "OUTPUT", "FLOAT", description="Mean height above the point (no data: NaN)"
    )
    total, count = TEMP_PREFIX + "rise_sum", TEMP_PREFIX + "rise_count"
    # Samples come in point-symmetric pairs, so a plane's mean rise is exactly 0.
    pairs = t.math("CEIL", t.mul(t.i("Samples"), 0.5), label="Sample Pairs")
    start, end = t.repeat(t.i("Geometry"), pairs, label="Each Sample Pair")
    k = start.outputs["Iteration"]
    angle = t.mul(k, GOLDEN_ANGLE)
    inner2 = t.mul(t.i("Inner Radius"), t.i("Inner Radius"))
    outer2 = t.mul(t.i("Outer Radius"), t.i("Outer Radius"))
    share = t.div(t.add(k, 0.5), pairs)
    radius = t.math("SQRT", t.add(inner2, t.mul(t.sub(outer2, inner2), share)))
    dx = t.mul(radius, t.math("SINE", angle))
    dy = t.mul(radius, t.math("COSINE", angle))
    x, y, z = t.separate(t.position())
    rises, hits = [], []
    for sign in (1.0, -1.0):
        hit, height = _ray_down_height(
            t,
            t.i("Geometry"),
            t.add(x, t.mul(dx, sign)),
            t.add(y, t.mul(dy, sign)),
            z,
            t.i("Search Height"),
        )
        rises.append(t.switch("FLOAT", hit, 0.0, t.sub(height, z)))
        hits.append(t.switch("FLOAT", hit, 0.0, 1.0))
    geometry = t.store(start.outputs["Geometry"], total, t.total((t.named(total), *rises)), "POINT")
    geometry = t.store(geometry, count, t.total((t.named(count), *hits)), "POINT")
    t.feed(end.inputs["Geometry"], geometry)
    t.output("Geometry", end.outputs["Geometry"])
    n = t.named(count)
    t.output("Mean Rise", t.or_nan(t.compare("GREATER_THAN", n, 0.5), t.div(t.named(total), n)))
    return t.finish()


def build_horizon():
    t = Tree(
        HORIZON,
        "Horizon angle around each point: for each of N azimuths, the elevation angle of "
        "the highest surface within Max Distance (or, Below, of the lowest), found by "
        "bisecting with rays. Stores temporary point attributes; the caller removes them",
        "_horizon",
        HELPER_VERSION,
    )
    t.socket("Geometry", "INPUT", "GEOMETRY")
    t.socket(
        "Below",
        "INPUT",
        "BOOLEAN",
        default=False,
        description="Search below the surface (negative openness) rather than above",
    )
    t.socket(
        "Lower Angle",
        "INPUT",
        "FLOAT",
        default=-90.0,
        min=-90.0,
        max=90.0,
        description="Lowest angle searched, degrees (0: the horizon is never below level)",
    )
    t.socket("Max Distance", "INPUT", "FLOAT", default=2.0, min=0.001, subtype="DISTANCE")
    t.socket("Directions", "INPUT", "INT", default=16, min=1, max=64)
    t.socket("Steps", "INPUT", "INT", default=8, min=1, max=16)
    t.socket("Ray Offset", "INPUT", "FLOAT", default=0.002, min=0.0, subtype="DISTANCE")
    t.socket("Geometry", "OUTPUT", "GEOMETRY")
    t.socket("Mean Angle", "OUTPUT", "FLOAT", description="Mean horizon angle, degrees")
    t.socket(
        "Mean Sine",
        "OUTPUT",
        "FLOAT",
        description="Mean sine of the horizon angle, taken as 0 below level",
    )
    lo_name, hi_name = TEMP_PREFIX + "lo", TEMP_PREFIX + "hi"
    angle_name, sine_name = TEMP_PREFIX + "angle_sum", TEMP_PREFIX + "sine_sum"

    outer, outer_end = t.repeat(t.i("Geometry"), t.i("Directions"), label="Each Direction")
    azimuth = t.mul(outer.outputs["Iteration"], t.div(2.0 * math.pi, t.i("Directions")))
    inner, inner_end = t.repeat(outer.outputs["Geometry"], t.i("Steps"), label="Bisect")
    first = t.compare("EQUAL", inner.outputs["Iteration"], 0, kind="INT")
    lo = t.switch("FLOAT", first, t.named(lo_name), t.i("Lower Angle"), label="Low")
    hi = t.switch("FLOAT", first, t.named(hi_name), 90.0, label="High")
    theta = t.switch("FLOAT", first, t.mul(t.add(lo, hi), 0.5), 0.0, label="Ray Angle")
    rad = t.math("RADIANS", theta)
    cos_theta = t.math("COSINE", rad)
    direction = t.combine(
        t.mul(cos_theta, t.math("SINE", azimuth)),
        t.mul(cos_theta, t.math("COSINE", azimuth)),
        t.math("SINE", rad),
        label="Ray Direction",
    )
    side = t.switch("FLOAT", t.i("Below"), t.i("Ray Offset"), t.mul(t.i("Ray Offset"), -1.0))
    origin = t.vmath("MULTIPLY_ADD", t.normal(), t.combine(side, side, side), t.position())
    hit, _where = t.raycast(t.i("Geometry"), origin, direction, t.i("Max Distance"))
    raise_low = t.boolean("XOR", hit, t.i("Below"))
    geometry, captured = t.capture(
        inner.outputs["Geometry"],
        "POINT",
        {
            "Low": t.switch("FLOAT", raise_low, lo, theta),
            "High": t.switch("FLOAT", raise_low, theta, hi),
        },
        label="Bisect Step",
    )
    geometry = t.store(geometry, lo_name, captured["Low"], "POINT")
    geometry = t.store(geometry, hi_name, captured["High"], "POINT")
    t.feed(inner_end.inputs["Geometry"], geometry)

    estimate = t.mul(t.add(t.named(lo_name), t.named(hi_name)), 0.5)
    geometry = t.store(
        inner_end.outputs["Geometry"], angle_name, t.add(t.named(angle_name), estimate), "POINT"
    )
    sine = t.math("SINE", t.math("RADIANS", t.math("MAXIMUM", estimate, 0.0)))
    geometry = t.store(geometry, sine_name, t.add(t.named(sine_name), sine), "POINT")
    t.feed(outer_end.inputs["Geometry"], geometry)
    t.output("Geometry", outer_end.outputs["Geometry"])
    t.output("Mean Angle", t.div(t.named(angle_name), t.i("Directions")))
    t.output("Mean Sine", t.div(t.named(sine_name), t.i("Directions")))
    return t.finish()


HORIZON_TEMPS = tuple(TEMP_PREFIX + n for n in ("lo", "hi", "angle_sum", "sine_sum"))
MEAN_RISE_TEMPS = (TEMP_PREFIX + "rise_sum", TEMP_PREFIX + "rise_count")


# --- presets ---------------------------------------------------------------------------


def _preset_tree(preset: catalog.Preset) -> Tree:
    t = Tree(preset.group, preset.description, preset.id, preset.version)
    t.socket("Geometry", "INPUT", "GEOMETRY")
    t.socket(
        catalog.ATTRIBUTE_INPUT,
        "INPUT",
        "STRING",
        default=preset.attribute,
        description="Attribute to store the result in (Add Analysis sets the layer's)",
    )
    for spec in preset.inputs:
        t.preset_input(spec)
    t.socket("Geometry", "OUTPUT", "GEOMETRY")
    return t


def _store_result(t: Tree, preset: catalog.Preset, geometry, value, temps=()) -> None:
    geometry = t.store(
        geometry, t.i(catalog.ATTRIBUTE_INPUT), value, preset.domain.value, label=preset.label
    )
    t.output("Geometry", t.remove(geometry, *temps))


def _face_normal(t: Tree):
    """The face normal, blurred over neighbouring faces ``Smoothing`` times."""
    blur = t.node(
        "GeometryNodeBlurAttribute",
        {"Value": t.normal(), "Iterations": t.i("Smoothing")},
        data_type="FLOAT_VECTOR",
    )
    return t.separate(t.vmath("NORMALIZE", blur.outputs["Value"]))


def build_slope(preset):
    t = _preset_tree(preset)
    _nx, _ny, nz = _face_normal(t)
    cos = t.math("MINIMUM", t.math("MAXIMUM", nz, -1.0), 1.0)
    _store_result(t, preset, t.i("Geometry"), t.math("DEGREES", t.math("ARCCOSINE", cos)))
    return t.finish()


def build_slope_percent(preset):
    t = _preset_tree(preset)
    nx, ny, nz = _face_normal(t)
    run = t.math("SQRT", t.add(t.mul(nx, nx), t.mul(ny, ny)))
    value = t.mul(t.div(run, nz), 100.0)
    _store_result(t, preset, t.i("Geometry"), t.or_nan(t.compare("GREATER_THAN", nz, 1e-6), value))
    return t.finish()


def build_aspect(preset):
    t = _preset_tree(preset)
    nx, ny, _nz = _face_normal(t)
    run = t.math("SQRT", t.add(t.mul(nx, nx), t.mul(ny, ny)))
    grid = t.math("DEGREES", t.math("ARCTAN2", nx, ny))  # clockwise from +Y
    value = t.math("FLOORED_MODULO", t.add(grid, t.i(catalog.CONVERGENCE_INPUT)), 360.0)
    flat = t.math("SINE", t.math("RADIANS", t.i("Flat Slope")))
    valid = t.boolean(
        "AND", t.compare("GREATER_THAN", run, flat), t.compare("GREATER_THAN", run, 0.0)
    )
    _store_result(t, preset, t.i("Geometry"), t.or_nan(valid, value))
    return t.finish()


def _curvature(t: Tree, derivatives, kind: str):
    node = t.group(
        derivatives,
        {
            "Target": t.i("Geometry"),
            "Window": t.i("Window"),
            "Search Height": t.i("Search Height"),
        },
    )
    p, q, r, s, tt = (node.outputs[n] for n in ("p", "q", "r", "s", "t"))
    pp, qq = t.mul(p, p), t.mul(q, q)
    g = t.add(pp, qq)
    w = t.add(g, 1.0)
    two_pqs = t.mul(t.mul(t.mul(p, q), s), 2.0)
    if kind == "profile":
        numerator = t.add(t.add(t.mul(pp, r), two_pqs), t.mul(qq, tt))
        denominator = t.mul(g, t.math("POWER", w, 1.5))
    elif kind == "plan":
        numerator = t.add(t.sub(t.mul(qq, r), two_pqs), t.mul(pp, tt))
        denominator = t.mul(g, t.math("SQRT", w))
    else:
        numerator = t.add(t.sub(t.mul(t.add(qq, 1.0), r), two_pqs), t.mul(t.add(pp, 1.0), tt))
        denominator = t.mul(t.math("POWER", w, 1.5), 2.0)
    value = t.mul(t.div(numerator, denominator), -1.0)
    valid = node.outputs["Valid"]
    if kind != "mean":  # undefined on level ground
        valid = t.boolean("AND", valid, t.compare("GREATER_THAN", g, 1e-12))
    return t.or_nan(valid, value)


def build_curvature(preset, derivatives, kind: str):
    t = _preset_tree(preset)
    geometry, captured = t.capture(
        t.i("Geometry"), "POINT", {"Value": _curvature(t, derivatives, kind)}, label="At Points"
    )
    _store_result(t, preset, geometry, captured["Value"])
    return t.finish()


def build_tri(preset, window):
    t = _preset_tree(preset)
    node = t.group(
        window,
        {
            "Target": t.i("Geometry"),
            "Window": t.i("Window"),
            "Search Height": t.i("Search Height"),
        },
    )
    squares = t.total(t.mul(node.outputs[n], node.outputs[n]) for n in NEIGHBOURS)
    value = t.or_nan(node.outputs["Valid"], t.math("SQRT", squares))
    geometry, captured = t.capture(t.i("Geometry"), "POINT", {"Value": value}, label="At Points")
    _store_result(t, preset, geometry, captured["Value"])
    return t.finish()


def build_rugosity(preset):
    t = _preset_tree(preset)
    area = t.node("GeometryNodeInputMeshFaceArea").outputs["Area"]
    _nx, _ny, nz = t.separate(t.normal())
    plan = t.mul(area, t.math("ABSOLUTE", nz))
    stats = t.node(
        "GeometryNodeAttributeStatistic",
        {"Geometry": t.i("Geometry"), "Attribute": area},
        data_type="FLOAT",
        domain="FACE",
        label="Mean Face Area",
    )
    radius2 = t.mul(t.i("Radius"), t.i("Radius"))
    wanted = t.math("CEIL", t.div(t.mul(radius2, 0.75), stats.outputs["Mean"]))
    iterations = t.math("MINIMUM", wanted, t.i("Max Iterations"), label="Blur Iterations")

    def blur(value):
        return t.node(
            "GeometryNodeBlurAttribute",
            {"Value": value, "Iterations": iterations},
            data_type="FLOAT",
        ).outputs["Value"]

    surface, planimetric = blur(area), blur(plan)
    value = t.or_nan(t.compare("GREATER_THAN", planimetric, 0.0), t.div(surface, planimetric))
    _store_result(t, preset, t.i("Geometry"), value)
    return t.finish()


def build_relief(preset, mean_rise, inner_radius):
    t = _preset_tree(preset)
    outer = t.i("Outer Radius") if inner_radius else t.i("Radius")
    node = t.group(
        mean_rise,
        {
            "Geometry": t.i("Geometry"),
            "Inner Radius": t.i("Inner Radius") if inner_radius else 0.0,
            "Outer Radius": outer,
            "Samples": t.i("Samples"),
            "Search Height": t.i("Search Height"),
        },
    )
    value = t.mul(node.outputs["Mean Rise"], -1.0)
    _store_result(t, preset, node.outputs["Geometry"], value, MEAN_RISE_TEMPS)
    return t.finish()


def build_horizon_preset(preset, horizon):
    t = _preset_tree(preset)
    below = preset.id == "openness_negative"
    node = t.group(
        horizon,
        {
            "Geometry": t.i("Geometry"),
            "Below": below,
            "Lower Angle": 0.0 if preset.id == "sky_view_factor" else -90.0,
            "Max Distance": t.i("Max Distance"),
            "Directions": t.i("Directions"),
            "Steps": t.i("Steps"),
            "Ray Offset": t.i("Ray Offset"),
        },
    )
    if preset.id == "sky_view_factor":
        value = t.sub(1.0, node.outputs["Mean Sine"])
    elif below:
        value = t.add(90.0, node.outputs["Mean Angle"])
    else:
        value = t.sub(90.0, node.outputs["Mean Angle"])
    _store_result(t, preset, node.outputs["Geometry"], value, HORIZON_TEMPS)
    return t.finish()


def build_all() -> dict[str, object]:
    """Every group of the library, by name, built into the current file."""
    window = build_window_heights()
    derivatives = build_derivatives(window)
    mean_rise = build_mean_rise()
    horizon = build_horizon()
    builders = {
        "slope": build_slope,
        "slope_percent": build_slope_percent,
        "aspect": build_aspect,
        "curvature_profile": lambda p: build_curvature(p, derivatives, "profile"),
        "curvature_plan": lambda p: build_curvature(p, derivatives, "plan"),
        "curvature_mean": lambda p: build_curvature(p, derivatives, "mean"),
        "rugosity": build_rugosity,
        "tpi": lambda p: build_relief(p, mean_rise, inner_radius=True),
        "tri": lambda p: build_tri(p, window),
        "local_relief": lambda p: build_relief(p, mean_rise, inner_radius=False),
        "sky_view_factor": lambda p: build_horizon_preset(p, horizon),
        "openness_positive": lambda p: build_horizon_preset(p, horizon),
        "openness_negative": lambda p: build_horizon_preset(p, horizon),
    }
    assert set(builders) == {p.id for p in catalog.PRESETS}, "a preset has no builder"
    groups = {g.name: g for g in (window, derivatives, mean_rise, horizon)}
    for preset in catalog.PRESETS:
        tree = builders[preset.id](preset)
        assert tree.name == preset.group, (tree.name, preset.group)
        tree.is_modifier = True
        tree.asset_mark()
        tree.asset_data.description = preset.description
        tree.asset_data.catalog_id = CATALOG_IDS[preset.category]
        tree.asset_data.author = "GeoTools"
        groups[tree.name] = tree
    return groups


# --- canonical description (for --check) ---------------------------------------------

_SKIP_PROPS = {
    "rna_type",
    "location_absolute",
    "width",
    "height",
    "dimensions",
    "select",
    "show_options",
    "show_preview",
    "show_texture",
    "color",
    "use_custom_color",
    "color_tag",
    "warning_propagation",
    "internal_links",
    "inputs",
    "outputs",
    "parent",
    "panel_states",
}


def _value(value):
    if isinstance(value, float):
        return None if math.isnan(value) else round(value, 6)
    if isinstance(value, (int, bool, str)) or value is None:
        return value
    if hasattr(value, "name") and hasattr(value, "bl_rna"):  # an ID
        return value.name
    try:
        return [_value(v) for v in value]
    except TypeError:
        return str(value)


def _props(struct) -> dict:
    out = {}
    for prop in struct.bl_rna.properties:
        key = prop.identifier
        if key in _SKIP_PROPS or key.startswith("bl_"):
            continue
        if prop.type in {"COLLECTION"}:
            continue
        if prop.type == "POINTER" and key != "node_tree":
            continue
        out[key] = _value(getattr(struct, key))
    return out


def describe(tree) -> dict:
    """A JSON-safe description of a node group: interface, nodes, defaults and links."""
    interface = []
    for item in tree.interface.items_tree:
        entry = {"item_type": item.item_type, "name": item.name}
        if item.item_type == "SOCKET":
            entry.update(
                in_out=item.in_out,
                socket_type=item.socket_type,
                description=item.description,
            )
            for key in ("default_value", "min_value", "max_value", "subtype"):
                if hasattr(item, key):
                    entry[key] = _value(getattr(item, key))
        interface.append(entry)
    nodes = {}
    for node in tree.nodes:
        entry = _props(node)
        entry["inputs"] = [
            [s.identifier, _value(getattr(s, "default_value", None))]
            for s in node.inputs
            if not s.is_linked and hasattr(s, "default_value")
        ]
        entry["outputs_values"] = [
            [s.identifier, _value(s.default_value)]
            for s in node.outputs
            if node.bl_idname == "ShaderNodeValue"
        ]
        if node.bl_idname == "GeometryNodeRepeatInput":
            entry["paired_output"] = node.paired_output.name
        if node.bl_idname == "GeometryNodeCaptureAttribute":
            entry["capture_items"] = [[i.name, i.data_type] for i in node.capture_items]
        nodes[node.name] = entry
    links = sorted(
        [
            link.from_node.name,
            link.from_socket.identifier,
            link.to_node.name,
            link.to_socket.identifier,
        ]
        for link in tree.links
    )
    asset = None
    if tree.asset_data is not None:
        asset = {
            "description": tree.asset_data.description,
            "catalog_id": tree.asset_data.catalog_id,
            "author": tree.asset_data.author,
        }
    return {
        "name": tree.name,
        "description": tree.description,
        "preset": tree.get(catalog.PRESET_KEY),
        "version": tree.get(catalog.VERSION_KEY),
        "is_modifier": tree.is_modifier,
        "asset": asset,
        "interface": interface,
        "nodes": nodes,
        "links": links,
    }


def describe_all(groups: Iterable) -> dict[str, dict]:
    return {g.name: describe(g) for g in sorted(groups, key=lambda g: g.name)}


# --- main ---------------------------------------------------------------------------------


def catalog_file_text() -> str:
    lines = [
        "# Asset catalogs of the GeoTools analysis presets, written by",
        "# scripts/make_analysis_presets.py.",
        "",
        "VERSION 1",
        "",
    ]
    for category, uuid in CATALOG_IDS.items():
        path = f"GeoTools/{catalog.CATEGORY_LABELS[category]}"
        lines.append(f"{uuid}:{path}:{path.replace('/', '-')}")
    return "\n".join(lines) + "\n"


def write(out: Path) -> None:
    import bpy

    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0  # no .blend1
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    build_all()
    out.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(out), compress=True, relative_remap=False)
    (out.parent / "blender_assets.cats.txt").write_text(catalog_file_text(), encoding="utf-8")


def _diff(a, b, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"{path}/{key}: only in the file")
            elif key not in b:
                out.append(f"{path}/{key}: missing from the file")
            else:
                out.extend(_diff(a[key], b[key], f"{path}/{key}"))
        return out
    return [] if a == b else [f"{path}: script {a!r}, file {b!r}"]


def check(out: Path) -> list[str]:
    """What differs between a fresh build and the groups in ``out``; empty if none."""
    import bpy

    if not out.is_file():
        return [f"{out} does not exist"]
    bpy.ops.wm.read_homefile(use_empty=True)
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    fresh = describe_all(build_all().values())
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    with bpy.data.libraries.load(str(out), link=False) as (src, dst):
        dst.node_groups = list(src.node_groups)
    stored = describe_all(g for g in bpy.data.node_groups if g is not None)
    problems = _diff(fresh, stored)
    cats = out.parent / "blender_assets.cats.txt"
    if not cats.is_file() or cats.read_text(encoding="utf-8") != catalog_file_text():
        problems.append(f"{cats} is missing or out of date")
    return problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="make_analysis_presets.py", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    parser.add_argument("--describe", type=Path, help="write the fresh description as JSON")
    args = parser.parse_args(argv)
    if args.describe:
        import bpy

        bpy.ops.wm.read_homefile(use_empty=True)
        args.describe.write_text(json.dumps(describe_all(build_all().values()), indent=1))
        return 0
    if args.check:
        problems = check(args.out)
        for line in problems[:50]:
            print(line)
        if problems:
            print(
                f"{args.out} is out of date ({len(problems)} differences): run "
                "scripts/make_analysis_presets.py"
            )
            return 1
        print(f"{args.out} is up to date")
        return 0
    write(args.out)
    print(f"Wrote {args.out} ({args.out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    sys.exit(main(argv))
