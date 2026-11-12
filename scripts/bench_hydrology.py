"""Time hydrology, contours and sections on a synthetic site.

    blender -b --factory-startup --python scripts/bench_hydrology.py -- \\
        [--triangles 10000000] [--case hydrology|contours|sections|all]

Runs the repository's ``geotools`` package directly (not an installed
extension). The site is a 1 km square terrain of ``--triangles`` triangles
(rolling ground, 200 pits 0.5-3 m deep and 5 cm of noise, so about a fifth
of it ponds in small hollows), a closed building sunk 0.5 m into it, an open
roof sheet with eaves and a floating slab whose underside drips: four objects
in the source collection, a LOCAL project. Cases:

- ``hydrology``: accumulation, depth and depression layers, depression points
  and the flow paths of the 20 largest outlets;
- ``contours``: 1 m contours;
- ``sections``: a three-segment section line across the site.
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


def _object(name: str, verts: np.ndarray, tris: np.ndarray, collection) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.astype(np.float32).ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.astype(np.int32).ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _grid(n: int, x0: float, x1: float, y0: float, y1: float):
    xs = np.linspace(x0, x1, n + 1)
    ys = np.linspace(y0, y1, n + 1)
    gx, gy = np.meshgrid(xs, ys)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    b, c, d = a + 1, a + n + 2, a + n + 1
    tris = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    return gx, gy, tris


def _box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    pts = np.array(
        [
            [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
            [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
        ]
    )  # fmt: skip
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return pts, np.array([t for q in quads for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))])


def site(triangles: int) -> None:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.geotools.source_collection = collection
    n = max(2, round((triangles / 2) ** 0.5))
    gx, gy, tris = _grid(n, -500, 500, -500, 500)
    rng = np.random.default_rng(7)
    z = 15.0 * np.sin(gx / 170.0) * np.cos(gy / 230.0) + 0.01 * gx
    for cx, cy, depth, r in zip(
        rng.uniform(-480, 480, 200),
        rng.uniform(-480, 480, 200),
        rng.uniform(0.5, 3.0, 200),
        rng.uniform(3.0, 15.0, 200),
        strict=True,
    ):
        z -= depth * np.exp(-((gx - cx) ** 2 + (gy - cy) ** 2) / (r * r))
    z += rng.normal(0.0, 0.05, z.shape)
    verts = np.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1)
    _object("Terrain", verts, tris, collection)
    ground = float(z[n // 2, n // 2])
    _object("Building", *_box((-20, -20, ground - 0.5), (20, 20, ground + 15)), collection)
    rx, ry, rtris = _grid(40, 60, 100, -20, 20)
    roof = np.stack([rx.ravel(), ry.ravel(), ground + 12 + 0.3 * (ry.ravel() + 20)], axis=1)
    _object("Roof", roof, rtris, collection)
    _object("Slab", *_box((-100, 60, ground + 8), (-60, 80, ground + 9)), collection)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triangles", type=int, default=10_000_000)
    parser.add_argument("--case", default="all")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    bpy.context.scene.geotools.placement_mode = "LOCAL"
    began = time.perf_counter()
    site(args.triangles)
    faces = sum(len(o.data.polygons) for o in bpy.data.objects)
    print(f"site: {faces:,} faces in {time.perf_counter() - began:.1f} s", flush=True)
    cases = ["hydrology", "contours", "sections"] if args.case == "all" else [args.case]
    from geotools.blender import contours, hydrology

    for case in cases:
        began = time.perf_counter()
        if case == "hydrology":
            props = hydrology.settings(bpy.context)
            props.flow_lines = True
            props.channel_area = 5000.0
            result = hydrology.compute(bpy.context)
            detail = (
                f"{result['depressions']:,} depressions, {result['flow_lines']:,} flow lines, "
                f"drops {result['stats']['drops']:,}; phases {result['timings']}"
            )
        elif case == "contours":
            contours.settings(bpy.context).interval = "1"
            result = contours.make_contours(bpy.context)
            detail = f"{result['lines']:,} lines at {result['levels']} levels"
        else:
            line = bpy.data.meshes.new("Line")
            line.from_pydata(
                [(-450, -300, 0), (0, 0, 0), (300, 50, 0), (450, 400, 0)],
                [(0, 1), (1, 2), (2, 3)],
                [],
            )
            obj = bpy.data.objects.new("Line", line)
            bpy.context.scene.collection.objects.link(obj)
            contours.settings(bpy.context).line = obj
            result = contours.make_sections(bpy.context)
            detail = f"{result['pieces']:,} pieces"
        seconds = time.perf_counter() - began
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
        print(f"{case}: {seconds:.1f} s; {detail}; peak RSS {peak:.1f} GB", flush=True)


main()
