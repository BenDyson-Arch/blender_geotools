"""Time the fast (bake) paths of the horizon and cumulative viewshed presets.

Runs inside Blender, against the source checkout:

    blender --background --factory-startup --python scripts/bench_preset_bake.py -- \\
        [--cells 707] [--spacing 0.02] [--observers 100] [--compare] [--fixed] [PRESET ...]

The terrain and the observers are ``bench_analysis_presets``'s (707
cells: about 1M triangles; 2236: about 10M), so the numbers compare with the
Geometry Nodes presets' in docs/analysis-presets.md. Each preset is computed by
``preset_bake.compute`` with the preset's defaults (the viewshed with Drape on),
and the split is printed: observers (the library's Object Points and Drape),
samples (reading the mesh and building the sample object), each bake (loading
the probe, then Cycles' sync, BVH and rays), values and writing the layer.

``--fixed`` also measures a bake's fixed cost: the cumulative viewshed with one
observer and Max Distance 0, where no ray is cast. ``--compare`` also evaluates
the live preset (slow at 10M) and reports its time and the agreement.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import numpy as np

DEFAULT = ("sky_view_factor", "openness_positive", "openness_negative", "viewshed_cumulative")


def _live(obj, preset_id: str, observers) -> tuple[np.ndarray, float]:
    """The live preset's values on ``obj`` (its own modifier, defaults) and the seconds."""
    import bpy
    from bench_analysis_presets import LIBRARY, set_input

    from geotools.analysis import presets as catalog

    preset = catalog.get(preset_id)
    if preset.group not in bpy.data.node_groups:
        with bpy.data.libraries.load(str(LIBRARY), link=False) as (_src, dst):
            dst.node_groups = [preset.group]
    modifier = obj.modifiers.new(preset.label, "NODES")
    modifier.node_group = bpy.data.node_groups[preset.group]
    if observers is not None:
        set_input(modifier, "Observers", observers)
        set_input(modifier, "Drape", True)
    start = time.perf_counter()
    mesh = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
    attribute = mesh.attributes[preset.attribute]
    values = np.empty(len(attribute.data), np.float32)
    attribute.data.foreach_get("value", values)
    seconds = time.perf_counter() - start
    obj.modifiers.remove(modifier)
    return values, seconds


def _inside(obj, count: int, margin: float) -> np.ndarray:
    """Whether each value (per corner, or per face) lies more than ``margin`` inside the edge."""
    mesh = obj.data
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if count == len(mesh.loops):
        loops = np.empty(count, np.int32)
        mesh.loops.foreach_get("vertex_index", loops)
        where = co[loops]
    else:
        where = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get("center", where)
        where = where.reshape(-1, 3)
    low, high = co.min(axis=0), co.max(axis=0)
    return np.all((where[:, :2] > low[:2] + margin) & (where[:, :2] < high[:2] - margin), axis=1)


def _stored(obj, layer: str) -> np.ndarray:
    attribute = obj.data.attributes[f"gt_{layer}"]
    values = np.empty(len(attribute.data), np.float32)
    attribute.data.foreach_get("value", values)
    return values


def _line(preset_id: str, result: dict) -> str:
    t = result["timings"]
    bakes = t.get("bakes", [])
    probe = sum(b["probe"] for b in bakes)
    bake = sum(b["bake"] for b in bakes)
    rays = result["samples"] * result["rays_per_sample"]
    return (
        f"{preset_id:20s} {result['seconds']:7.1f} s  {result['samples']:,} samples x "
        f"{result['rays_per_sample']:,} rays in {result['bakes']} bake(s): observers "
        f"{t.get('observers', 0.0):.1f} s, samples {t['samples']:.1f} s, probe {probe:.2f} s, "
        f"bake {bake:.1f} s, values {t['values']:.1f} s, write {t['write']:.1f} s; "
        f"{rays / max(bake, 1e-9) / 1e6:.0f} Mrays/s in the bake"
    )


def main(argv: list[str]) -> None:
    import bpy
    from bench_analysis_presets import _points, terrain

    import geotools

    parser = argparse.ArgumentParser(prog="bench_preset_bake.py")
    parser.add_argument("--cells", type=int, default=707)
    parser.add_argument("--spacing", type=float, default=0.02)
    parser.add_argument("--observers", type=int, default=100, help="cumulative viewshed")
    parser.add_argument("--compare", action="store_true", help="also time the live preset")
    parser.add_argument("--fixed", action="store_true", help="also measure a bake's fixed cost")
    parser.add_argument("presets", nargs="*", help=f"preset ids (default: {' '.join(DEFAULT)})")
    args = parser.parse_args(argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    from geotools.blender import preset_bake

    obj = terrain(args.cells, args.spacing)
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.collection.objects.unlink(obj)
    collection.objects.link(obj)
    bpy.context.scene.geotools.source_collection = collection
    print(f"{len(obj.data.polygons):,} triangles, {len(obj.data.vertices):,} vertices", flush=True)
    # The observers of bench_analysis_presets.inputs(): its first draw from the same seed.
    half = args.cells * args.spacing / 2 * 0.95
    rng = np.random.default_rng(1)
    where = rng.uniform(-half, half, (args.observers, 3)) * (1, 1, 0) + (0, 0, 1)
    observers = _points("Observers", where)
    if args.fixed:
        one = bpy.data.objects.new("One", bpy.data.meshes.new("One"))
        one.data.from_pydata([(0.0, 0.0, 1.0)], [], [])
        bpy.context.scene.collection.objects.link(one)
        result = preset_bake.compute(
            bpy.context,
            "viewshed_cumulative",
            [obj],
            name="fixed_cost",
            inputs={"Max Distance": 0.0},
            observers=one,
        )
        print("fixed cost (no ray cast): " + _line("viewshed_cumulative", result), flush=True)
    for preset_id in args.presets or DEFAULT:
        viewshed = preset_id == "viewshed_cumulative"
        result = preset_bake.compute(
            bpy.context,
            preset_id,
            [obj],
            name=f"{preset_id}_fast",
            inputs={"Drape": True} if viewshed else {},
            observers=observers if viewshed else None,
        )
        print(_line(preset_id, result), flush=True)
        if args.compare:
            fast = _stored(obj, f"{preset_id}_fast")
            live, seconds = _live(obj, preset_id, observers if viewshed else None)
            diff = np.abs(fast.astype(np.float64) - live)
            inner = _inside(obj, len(diff), 0.0 if viewshed else 2.0)
            print(
                f"{'':20s} live {seconds:7.1f} s; |fast - live| max {diff.max():.4g}, "
                f"mean {diff.mean():.3g}, values differing by > 1e-4: "
                f"{np.count_nonzero(diff > 1e-4):,} of {len(diff):,}, of which "
                f"{np.count_nonzero((diff > 1e-4) & inner):,} more than Max Distance from the "
                "edge",
                flush=True,
            )


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
