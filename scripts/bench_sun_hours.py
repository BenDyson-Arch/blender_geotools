"""Time sun hours on a synthetic town: terrain with box buildings, all triangles.

    blender -b --factory-startup --python scripts/bench_sun_hours.py -- \\
        [--triangles 10000000] [--case day|year|texture|all] [--texture 2048]

Runs the repository's ``geotools`` package directly (not an installed
extension). The mesh is a 1 km square height field (a gentle swell plus
12-24 m blocks every 40 m) cut into triangles, one object in the source
collection, in a LOCAL project at 27.5 S 153 E. Cases:

- ``day``: FACE values, 21 June, 15-minute steps, no merging.
- ``year``: FACE values, a year at 30-minute steps, directions merged within 5°.
- ``texture``: a TEXTURE layer (``--texture`` square) for a year at 30 minutes,
  merged within 2°.

``--tolerance`` overrides the merge tolerance of every case.
- ``insolation``: the day case with clear-sky insolation and 64 sky rays.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import bpy
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import geotools


def town(triangles: int) -> bpy.types.Object:
    n = max(2, round((triangles / 2) ** 0.5))
    size = 1000.0
    xs = np.linspace(-size / 2, size / 2, n + 1)
    gx, gy = np.meshgrid(xs, xs)
    z = 8.0 * np.sin(gx / 170.0) * np.cos(gy / 230.0)
    cell = np.floor((gx + size) / 40.0) + 7 * np.floor((gy + size) / 40.0)
    inside = ((gx + size) % 40.0 > 12.0) & ((gy + size) % 40.0 > 12.0)
    z += np.where(inside, 12.0 + 12.0 * ((cell * 0.618) % 1.0), 0.0)
    verts = np.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1).astype(np.float32)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    b, c, d = a + 1, a + n + 2, a + n + 1
    tris = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)]).astype(np.int32)
    mesh = bpy.data.meshes.new("Town")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    uv = (verts[tris.ravel(), :2] + size / 2) / size
    mesh.uv_layers.new(name="UVMap").data.foreach_set("uv", uv.astype(np.float32).ravel())
    mesh.update()
    obj = bpy.data.objects.new("Town", mesh)
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    collection.objects.link(obj)
    bpy.context.scene.geotools.source_collection = collection
    return obj


def run(case: str, texture: int, tolerance: float | None = None) -> dict:
    from geotools.blender import analysis, sun_hours

    settings = analysis.settings(bpy.context)
    sun_props, props = settings.sun, settings.sun_hours
    for prop in props.bl_rna.properties:
        if prop.identifier not in {"rna_type", "name"}:
            props.property_unset(prop.identifier)
    sun_props.year, sun_props.month, sun_props.day = 2026, 6, 21
    sun_props.tz_mode, sun_props.utc_offset = "OFFSET", 10.0
    if case in {"day", "insolation"}:
        props.period, props.step_minutes, props.tolerance = "DAY", 15, 0.0
        props.insolation = case == "insolation"
    elif case == "year":
        props.period, props.step_minutes, props.tolerance = "YEAR", 30, 5.0
    else:
        props.period, props.step_minutes, props.tolerance = "YEAR", 30, 2.0
        props.domain, props.resolution, props.hours_layer = "TEXTURE", texture, "sun_hours_tex"
    if tolerance is not None:
        props.tolerance = tolerance
    return sun_hours.compute(bpy.context)


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triangles", type=int, default=10_000_000)
    parser.add_argument("--case", default="all")
    parser.add_argument("--texture", type=int, default=2048)
    parser.add_argument("--tolerance", type=float, default=None, help="override Merge Within")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    project = bpy.context.scene.geotools
    project.placement_mode = "LOCAL"
    project.anchor_lon, project.anchor_lat = "153.0", "-27.5"
    began = time.perf_counter()
    obj = town(args.triangles)
    print(f"mesh: {len(obj.data.polygons):,} triangles in {time.perf_counter() - began:.1f} s")
    cases = ["day", "insolation", "year", "texture"] if args.case == "all" else [args.case]
    with tempfile.TemporaryDirectory(prefix="geotools-bench-") as tmp:
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(tmp) / "bench.blend"))
        for case in cases:
            began = time.perf_counter()
            result = run(case, args.texture, args.tolerance)
            seconds = time.perf_counter() - began
            rays = result["samples"] * result["rays"]
            print(
                f"{case}: {result['samples']:,} samples x {result['rays']:,} rays "
                f"({result['sun_rays']:,} sun) in {result['bakes']} bake(s): {seconds:.1f} s, "
                f"{rays / seconds / 1e6:.0f} Mrays/s; phases {result['timings']}; values {result['ranges']}",
                flush=True,
            )


main()
