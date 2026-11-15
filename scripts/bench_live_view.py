"""Time the live layer view (Display > Show in Viewport) on a synthetic mesh.

    blender -b --factory-startup --python scripts/bench_live_view.py -- \\
        [--triangles 1000000] [--domain FACE]

Runs the repository's ``geotools`` package directly (not an installed
extension). The source is one height field of ``--triangles`` triangles with a
float layer (a smooth synthetic field, a few NaN) and automatic ramp ends.

It times turning the view on (the automatic range read from the evaluated
mesh, the node tree built, the modifier added) and its first evaluation; a
Display change (palette, a range end, the no-data colour) and the evaluation
after it; turning it off; the Display panel's ``draw()``; and
``export.extract_scene`` with the view on and off.
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


def height_field(triangles: int) -> bpy.types.Object:
    """A triangulated grid of about ``triangles`` triangles in a new source collection."""
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.geotools.source_collection = collection
    n = max(2, round((triangles / 2) ** 0.5))
    xs = np.linspace(0.0, 1000.0, n + 1)
    gx, gy = np.meshgrid(xs, xs)
    z = 20.0 * np.sin(gx / 90.0) * np.cos(gy / 130.0)
    co = np.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1).astype(np.float32)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (i + (n + 1) * j).ravel()
    tris = np.concatenate(
        [np.stack([a, a + 1, a + n + 2], 1), np.stack([a, a + n + 2, a + n + 1], 1)]
    ).astype(np.int32)
    mesh = bpy.data.meshes.new("Site")
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new("Site", mesh)
    collection.objects.link(obj)
    return obj


class _Layout:
    """A UILayout stand-in for timing ``draw()`` in background Blender."""

    alert = False
    use_property_split = False
    use_property_decorate = False

    def __getattr__(self, _name: str):
        return lambda *_args, **_kwargs: self


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--triangles", type=int, default=1_000_000)
    parser.add_argument("--domain", choices=("FACE", "CORNER"), default="FACE")
    args = parser.parse_args(argv)

    geotools.register()
    from geotools.blender import export, layers, presentation
    from geotools.core.layers import DataType, Domain, LayerSpec

    scene = bpy.context.scene
    obj = height_field(args.triangles)
    mesh = obj.data
    domain = Domain(args.domain)
    count = len(mesh.polygons) if domain is Domain.FACE else len(mesh.loops)
    spec = LayerSpec("slope", domain, DataType.FLOAT, units="deg")
    item = layers.ensure_layer(scene, spec)
    values = (np.sin(np.arange(count) / 977.0) * 20.0 + 25.0).astype(np.float32)
    values[::5000] = np.nan
    layers.write_layer(mesh, spec, values)
    print(f"{len(mesh.polygons):,} triangles, {len(mesh.loops):,} corners, {domain.value} layer")

    def evaluate() -> None:
        bpy.context.evaluated_depsgraph_get().update()
        obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.attributes.get("gt_view")

    def timed(label: str, fn, repeat: int = 1) -> None:
        best = float("inf")
        for _ in range(repeat):
            start = time.perf_counter()
            fn()
            best = min(best, time.perf_counter() - start)
        unit = f"{best * 1e3:9.1f} ms" if best >= 1e-3 else f"{best * 1e6:9.1f} us"
        print(f"{label:<52} {unit}")

    evaluate()
    timed("Turn on (auto range read, tree, modifier)", lambda: _set(item, True))
    timed("  first evaluation", evaluate)
    timed("Palette change", lambda: setattr(item.presentation, "palette", "magma"))
    timed("  evaluation", evaluate)
    timed("Range end change", lambda: setattr(item.presentation, "range_max", "40"))
    timed("  evaluation", evaluate)
    timed("No-data colour change", lambda: setattr(item.presentation, "nodata_color", (1, 0, 0)))
    timed("  evaluation", evaluate)
    timed("Same setting again (no rebuild)", lambda: setattr(item.presentation, "range_max", "40"))

    context = bpy.context
    timed("Display panel draw()", lambda: presentation.draw_display(_Layout(), context, item), 200)
    with tempfile.TemporaryDirectory() as tmp:
        timed(
            "extract_scene, view on",
            lambda: export.extract_scene(context, obj.users_collection[0], Path(tmp)),
        )
        timed("  evaluation after it", evaluate)
        timed("Turn off", lambda: _set(item, False))
        timed("  evaluation", evaluate)
        timed(
            "extract_scene, view off",
            lambda: export.extract_scene(context, obj.users_collection[0], Path(tmp)),
        )


def _set(item, on: bool) -> None:
    item.presentation.show_in_viewport = on


if __name__ == "__main__":
    main()
