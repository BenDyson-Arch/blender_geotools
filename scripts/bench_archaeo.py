"""Time the archaeoastronomy horizon on a synthetic valley: one height-field mesh, all triangles.

    blender -b --factory-startup --python scripts/bench_archaeo.py -- \\
        [--triangles 10000000] [--step 0.5]

Runs the repository's ``geotools`` package directly (not an installed
extension). The terrain is a 20 km square height field: a valley floor with
ridges rising to about 600 m at 5 to 9 km, plus smaller hills, cut into
triangles, one object in the source collection, in a LOCAL project at 51 N.
The observer stands at the centre. It times the horizon profile (both passes),
the events at 3000 BCE (with the standstills and the dated sunrises) and the
sightlines.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geotools


def valley(triangles: int) -> bpy.types.Object:
    n = max(2, round((triangles / 2) ** 0.5))
    size = 20_000.0
    xs = np.linspace(-size / 2, size / 2, n + 1)
    gx, gy = np.meshgrid(xs, xs)
    r = np.hypot(gx, gy)
    angle = np.arctan2(gx, gy)
    ridge = 600.0 * np.clip((r - 3000.0) / 5000.0, 0.0, 1.0) ** 1.5
    ridge *= 0.75 + 0.25 * np.sin(3.0 * angle) * np.cos(5.0 * angle + 0.4)
    z = ridge + 40.0 * np.sin(gx / 700.0) * np.cos(gy / 900.0)
    verts = np.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1).astype(np.float32)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    b, c, d = a + 1, a + n + 2, a + n + 1
    tris = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)]).astype(np.int32)
    mesh = bpy.data.meshes.new("Valley")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new("Valley", mesh)
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    collection.objects.link(obj)
    bpy.context.scene.geotools.source_collection = collection
    return obj


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triangles", type=int, default=10_000_000)
    parser.add_argument("--step", type=float, default=0.5, help="azimuth step, degrees")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    from geotools.blender import archaeo

    project = bpy.context.scene.geotools
    project.placement_mode = "LOCAL"
    project.anchor_lon, project.anchor_lat = "-1.8", "51.0"
    began = time.perf_counter()
    obj = valley(args.triangles)
    print(f"mesh: {len(obj.data.polygons):,} triangles in {time.perf_counter() - began:.1f} s")
    archaeo.set_observer(bpy.context, (0.0, 0.0, 0.0))  # the valley floor is at 0 there
    props = archaeo.settings(bpy.context)
    props.azimuth_step = args.step
    props.epoch_year = -2999

    began = time.perf_counter()
    profile = archaeo.compute_horizon(bpy.context)
    seconds = time.perf_counter() - began
    info = profile.info
    print(
        f"horizon: {len(profile.azimuth)} azimuths, {info['rays']:,} rays in {seconds:.1f} s "
        f"(first pass {info['first_pass_seconds']:.1f} s); altitude "
        f"{profile.elevation.min():.2f} to {profile.elevation.max():.2f} deg, "
        f"{int(profile.edge.sum())} at the edge",
        flush=True,
    )
    began = time.perf_counter()
    events, _summary = archaeo.compute_events(bpy.context)
    print(f"events: {len(events)} in {time.perf_counter() - began:.2f} s", flush=True)
    began = time.perf_counter()
    lines = archaeo.make_sightlines(bpy.context)
    print(f"sightlines: {len(lines.data.edges)} in {time.perf_counter() - began:.2f} s")


main()
