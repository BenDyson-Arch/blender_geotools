"""Time least-cost routes on a synthetic terrain.

    blender -b --factory-startup --python scripts/bench_least_cost.py -- \\
        [--triangles 1000000] [--steiner 0,1,2,3,4] [--model TOBLER]

Runs the repository's ``geotools`` package directly (not an installed
extension). The site is one 1 km square terrain of ``--triangles`` triangles
(rolling ground with slopes to about 20°, and 5 cm of noise), a LOCAL project.
For each Steiner density it computes the route corner to corner and the
travel-time layer of every face (``least_cost.compute``, as the operator's
``execute``), then the route alone (the search stops once End is settled),
and prints the phases and the peak memory.
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geotools


def site(triangles: int) -> bpy.types.Object:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.geotools.source_collection = collection
    n = max(2, round((triangles / 2) ** 0.5))
    xs = np.linspace(-500, 500, n + 1)
    gx, gy = np.meshgrid(xs, xs)
    rng = np.random.default_rng(7)
    z = 60.0 * np.sin(gx / 170.0) * np.cos(gy / 230.0) + 0.05 * gx
    z += rng.normal(0.0, 0.05, z.shape)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    b, c, d = a + 1, a + n + 2, a + n + 1
    tris = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    verts = np.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1)
    mesh = bpy.data.meshes.new("Terrain")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.astype(np.float32).ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.astype(np.int32).ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new("Terrain", mesh)
    collection.objects.link(obj)
    return obj


def _empty(name: str, location) -> bpy.types.Object:
    obj = bpy.data.objects.new(name, None)
    obj.location = location
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triangles", type=int, default=1_000_000)
    parser.add_argument("--steiner", default="0,1,2,3,4")
    parser.add_argument("--model", default="TOBLER")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    bpy.context.scene.geotools.placement_mode = "LOCAL"
    began = time.perf_counter()
    terrain = site(args.triangles)
    print(f"site: {len(terrain.data.polygons):,} faces in {time.perf_counter() - began:.1f} s")
    from geotools.blender import least_cost

    props = least_cost.settings(bpy.context)
    props.surface = "SOURCE"  # the whole site, selected or not
    props.start = _empty("A", (-450.0, -450.0, 0.0))
    props.end = _empty("B", (450.0, 430.0, 0.0))
    props.model = args.model
    for k in (int(s) for s in args.steiner.split(",")):
        props.steiner = k
        props.make_layer = props.make_route = True
        began = time.perf_counter()
        result = least_cost.compute(bpy.context)
        both = time.perf_counter() - began
        props.make_layer = False
        began = time.perf_counter()
        least_cost.compute(bpy.context)
        alone = time.perf_counter() - began
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        print(
            f"steiner {k}: route and layer {both:.1f} s (phases {result['timings']}, "
            f"{result['nodes']:,} nodes, {result['passes']:,} passes); route alone "
            f"{alone:.1f} s; route {result['route']['time'] / 60:.2f} min over "
            f"{result['route']['length']:,.0f} m; peak RSS {peak:.1f} GB",
            flush=True,
        )


main()
