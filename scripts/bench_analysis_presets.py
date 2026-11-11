"""Time each analysis preset on a synthetic triangulated terrain.

Runs inside Blender, against the source checkout:

    blender --background --factory-startup --python scripts/bench_analysis_presets.py -- \\
        [--cells 707] [--spacing 0.02] [PRESET ...]

``--cells`` n gives an n x n grid of quads split into 2n² triangles (707: about
1M triangles; 2236: about 10M). Each preset from the bundled library is added
alone, with its defaults, as the only modifier, and the time to evaluate it
and read its attribute is printed. docs/analysis-presets.md has the results.
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


def terrain(cells: int, spacing: float):
    import bpy

    size = cells * spacing
    axis = np.linspace(-size / 2, size / 2, cells + 1)
    xs, ys = np.meshgrid(axis, axis)
    zs = (
        0.05 * xs
        + 0.3 * np.sin(xs / 1.7) * np.cos(ys / 2.3)
        + 0.02 * np.sin(xs * 7) * np.sin(ys * 5)
    )
    co = np.stack([xs.ravel(), ys.ravel(), zs.ravel()], axis=1).astype(np.float32)
    i = np.arange(cells)
    a = (i[None, :] + (cells + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + cells + 2, a + cells + 1], axis=1)
    tris = np.concatenate([quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]]).astype(np.int32)
    mesh = bpy.data.meshes.new("Terrain")
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new("Terrain", mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def main(argv: list[str]) -> None:
    import bpy

    parser = argparse.ArgumentParser(prog="bench_analysis_presets.py")
    parser.add_argument("--cells", type=int, default=707)
    parser.add_argument("--spacing", type=float, default=0.02)
    parser.add_argument("presets", nargs="*", help="preset ids (default: all)")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_homefile(use_empty=True)
    with bpy.data.libraries.load(str(LIBRARY), link=False) as (src, dst):
        dst.node_groups = list(src.node_groups)
    obj = terrain(args.cells, args.spacing)
    print(f"{len(obj.data.polygons):,} triangles, {len(obj.data.vertices):,} vertices")
    for preset in catalog.PRESETS:
        if args.presets and preset.id not in args.presets:
            continue
        obj.modifiers.clear()
        modifier = obj.modifiers.new(preset.label, "NODES")
        modifier.node_group = bpy.data.node_groups[preset.group]
        start = time.perf_counter()
        mesh = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        attribute = mesh.attributes[preset.attribute]
        values = np.empty(len(attribute.data), np.float32)
        attribute.data.foreach_get("value", values)
        seconds = time.perf_counter() - start
        print(
            f"{preset.id:20s} {seconds:8.2f} s  {attribute.domain:6s} no data {np.isnan(values).sum():,}"
        )


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
