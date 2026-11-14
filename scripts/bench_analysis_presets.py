"""Time each analysis preset on a synthetic triangulated terrain.

Runs inside Blender, against the source checkout:

    blender --background --factory-startup --python scripts/bench_analysis_presets.py -- \\
        [--cells 707] [--spacing 0.02] [PRESET ...]

``--cells`` n gives an n x n grid of quads split into 2n² triangles (707: about
1M triangles; 2236: about 10M). Each preset from the bundled library is added
alone, with its defaults, as the only modifier, and the time to evaluate it
and read its attribute is printed. docs/analysis-presets.md has the results.

Presets with object inputs get synthetic ones (``inputs``): an observer above
the centre, ``--observers`` observer points, a second terrain 5 cm above the
first as the reference (as many triangles again), a line across the terrain,
start and end at opposite corners, and ``--seeds`` flow seeds. An object
preset's modifier is on a separate empty mesh, as Add Analysis makes it.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from geotools.analysis import presets as catalog

LIBRARY = REPO_ROOT / "geotools" / "assets" / catalog.LIBRARY_FILE


def terrain(cells: int, spacing: float, name: str = "Terrain", lift: float = 0.0):
    import bpy

    size = cells * spacing
    axis = np.linspace(-size / 2, size / 2, cells + 1)
    xs, ys = np.meshgrid(axis, axis)
    zs = (
        0.05 * xs
        + 0.3 * np.sin(xs / 1.7) * np.cos(ys / 2.3)
        + 0.02 * np.sin(xs * 7) * np.sin(ys * 5)
        + lift
    )
    co = np.stack([xs.ravel(), ys.ravel(), zs.ravel()], axis=1).astype(np.float32)
    i = np.arange(cells)
    a = (i[None, :] + (cells + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + cells + 2, a + cells + 1], axis=1)
    tris = np.concatenate([quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]]).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _points(name: str, co) -> object:
    import bpy

    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata([tuple(map(float, c)) for c in co], [], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _empty(name: str, location) -> object:
    import bpy

    obj = bpy.data.objects.new(name, None)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def inputs(surface, args) -> dict[str, object]:
    """Synthetic object inputs for the presets that take them, by input name."""
    import bpy

    half = args.cells * args.spacing / 2 * 0.95
    rng = np.random.default_rng(1)
    line = bpy.data.meshes.new("Line")
    line.from_pydata([(-half, -half * 0.3, 0.0), (half, half * 0.3, 0.0)], [(0, 1)], [])
    line_obj = bpy.data.objects.new("Line", line)
    bpy.context.scene.collection.objects.link(line_obj)
    feature = bpy.data.meshes.new("Feature")
    xs = np.linspace(-half, half, 100)
    feature.from_pydata([(x, 0.3 * x, 0.0) for x in xs], [(i, i + 1) for i in range(99)], [])
    feature_obj = bpy.data.objects.new("Feature", feature)
    bpy.context.scene.collection.objects.link(feature_obj)
    return {
        "Observer": _empty("Observer", (0.0, 0.0, 1.0)),
        "Observers": _points(
            "Observers", rng.uniform(-half, half, (args.observers, 3)) * (1, 1, 0) + (0, 0, 1)
        ),
        "Points": _points("Points", rng.uniform(-half, half, (20, 3)) * (1, 1, 0) + (0, 0, 1)),
        "Seeds": _points("Seeds", rng.uniform(-half, half, (args.seeds, 3)) * (1, 1, 0)),
        "Reference": terrain(args.cells, args.spacing, "Reference", 0.05),
        "Feature": feature_obj,
        "Line": line_obj,
        "Start": _empty("Start", (-half, -half, 0.0)),
        "End": _empty("End", (half, half, 0.0)),
        "Surface": surface,
    }


def set_input(modifier, name: str, value) -> None:
    for item in modifier.node_group.interface.items_tree:
        if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name:
            getattr(modifier.properties.inputs, item.identifier).value = value
            return
    raise KeyError(name)


def main(argv: list[str]) -> None:
    import bpy

    parser = argparse.ArgumentParser(prog="bench_analysis_presets.py")
    parser.add_argument("--cells", type=int, default=707)
    parser.add_argument("--spacing", type=float, default=0.02)
    parser.add_argument("--observers", type=int, default=10, help="cumulative viewshed")
    parser.add_argument("--seeds", type=int, default=100, help="flow paths")
    parser.add_argument("presets", nargs="*", help="preset ids (default: all)")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_homefile(use_empty=True)
    with bpy.data.libraries.load(str(LIBRARY), link=False) as (src, dst):
        dst.node_groups = list(src.node_groups)
    obj = terrain(args.cells, args.spacing)
    print(f"{len(obj.data.polygons):,} triangles, {len(obj.data.vertices):,} vertices")
    wanted = [p for p in catalog.PRESETS if not args.presets or p.id in args.presets]
    given = inputs(obj, args) if any(p.object_inputs for p in wanted) else {}
    holder = bpy.data.objects.new("Lines", bpy.data.meshes.new("Lines"))
    bpy.context.scene.collection.objects.link(holder)
    for preset in wanted:
        obj.modifiers.clear()
        holder.modifiers.clear()
        on = holder if preset.output is catalog.Output.OBJECT else obj
        modifier = on.modifiers.new(preset.label, "NODES")
        modifier.node_group = bpy.data.node_groups[preset.group]
        for spec in preset.object_inputs:
            if spec.name in given:
                set_input(modifier, spec.name, given[spec.name])
        if preset.id == "viewshed_cumulative":
            set_input(modifier, "Drape", True)
        start = time.perf_counter()
        mesh = on.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        attribute = mesh.attributes[preset.attribute]
        values = np.empty(len(attribute.data), np.float32)
        attribute.data.foreach_get("value", values)
        seconds = time.perf_counter() - start
        print(
            f"{preset.id:20s} {seconds:8.2f} s  {attribute.domain:6s} {len(values):,} values, "
            f"no data {np.isnan(values).sum():,}"
        )


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
