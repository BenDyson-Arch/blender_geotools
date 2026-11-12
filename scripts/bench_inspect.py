"""Time output inspection (M7) on a synthetic publish.

    blender -b --factory-startup --python scripts/bench_inspect.py -- \\
        [--triangles 2000000] [--texture 4096] [--profile balanced] [--mode anchor|crs|local]

Runs the repository's ``geotools`` package directly (not an installed
extension), with rusty-tiles and pyproj unpacked from ``geotools/wheels`` (run
``scripts/fetch_wheels.py`` first). The site is a 1 km square terrain of
``--triangles`` triangles with one ``--texture`` square texture, published
through the Publish operator. Then it times listing the archive, opening the
root, the largest leaf and every tile of the deepest depth with at most 64
tiles, tracing a whole leaf to the source and back, the distance heatmap on
the root and the texel density of the leaf.
"""

from __future__ import annotations

import argparse
import resource
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _unpack_wheels(target: Path) -> None:
    for wheel in sorted((ROOT / "geotools" / "wheels").glob("*.whl")):
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(target)
    sys.path.insert(0, str(target))


def site(triangles: int, texture: int) -> None:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.geotools.source_collection = collection
    n = max(2, round((triangles / 2) ** 0.5))
    xs = np.linspace(-500.0, 500.0, n + 1)
    gx, gy = np.meshgrid(xs, xs)
    z = 15.0 * np.sin(gx / 170.0) * np.cos(gy / 230.0) + 0.01 * gx
    verts = np.stack([gx.ravel(), gy.ravel(), z.ravel()], axis=1)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new("Terrain")
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.astype(np.float32).ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    uv = (verts[:, :2] + 500.0) / 1000.0
    mesh.uv_layers.new(name="UVMap").data.foreach_set(
        "uv", uv[quads.ravel()].astype(np.float32).ravel()
    )
    mesh.update(calc_edges=True)
    image = bpy.data.images.new("Ortho", texture, texture, alpha=False)
    # Blocks and gradients: detailed, yet a PNG well under rusty-tiles' 32 MiB limit.
    yy, xx = np.indices((texture, texture), dtype=np.float32) / texture
    px = np.ones((texture, texture, 4), np.float32)
    px[..., 0] = ((np.floor(xx * 64) + np.floor(yy * 64)) % 2) * 0.4 + 0.3
    px[..., 1] = xx
    px[..., 2] = yy
    image.pixels.foreach_set(px.ravel())
    image.pack()
    material = bpy.data.materials.new("Ortho")
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next(nd for nd in tree.nodes if nd.type == "BSDF_PRINCIPLED")
    node = tree.nodes.new("ShaderNodeTexImage")
    node.image = image
    tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    mesh.materials.append(material)
    obj = bpy.data.objects.new("Terrain", mesh)
    collection.objects.link(obj)


def place(mode: str) -> None:
    from geotools.blender.properties import write_number

    props = bpy.context.scene.geotools
    if mode == "anchor":
        props.placement_mode = "ANCHOR"
        write_number(props, "anchor_lon", 153.02)
        write_number(props, "anchor_lat", -27.47)
        write_number(props, "anchor_height", 10.0)
    elif mode == "crs":
        props.placement_mode = "SOURCE_CRS"
        props.source_crs = "EPSG:7856"
        write_number(props, "source_offset_e", 502000.0)
        write_number(props, "source_offset_n", 6960000.0)
    else:
        props.placement_mode = "LOCAL"


def timed(label: str, fn):
    began = time.perf_counter()
    result = fn()
    seconds = time.perf_counter() - began
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
    print(f"{label}: {seconds:.2f} s; {result}; peak RSS {peak:.1f} GB", flush=True)
    return result


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triangles", type=int, default=2_000_000)
    parser.add_argument("--texture", type=int, default=4096)
    parser.add_argument("--profile", default="balanced")
    parser.add_argument("--mode", default="anchor", choices=("anchor", "crs", "local"))
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.home() / ".cache",
        help="Where the archive and rusty-tiles' scratch go: a disk, not tmpfs, which "
        "rusty-tiles counts against its memory budget",
    )
    args = parser.parse_args(argv)
    args.workdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="geotools-bench-inspect-", dir=args.workdir))
    _unpack_wheels(work / "site-packages")
    import geotools

    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    from geotools.blender import inspect, publish

    place(args.mode)
    timed("site", lambda: site(args.triangles, args.texture))
    bpy.ops.geotools.build_profile_add()
    props = bpy.context.scene.geotools
    profile = props.build_profiles[props.build_profile_index]
    profile.output_path = str(work / "site.3tz")
    profile.profile = args.profile.upper()
    profile.validate = False

    def run_publish():
        try:
            bpy.ops.geotools.publish()
        except RuntimeError:
            for issue in props.issues:
                print(issue.severity, issue.code, issue.message, flush=True)
            raise
        job = publish.active_job()
        job.join()
        publish.update_from_job()
        return f"{job.result.triangles:,} triangles" if job.result else job.error

    timed("publish", run_publish)
    settings = inspect.settings(bpy.context)
    timed("list", lambda: (bpy.ops.geotools.inspect_list(), f"{len(settings.tiles)} tiles")[1])
    tiles = list(settings.tiles)

    def open_index(index: int) -> str:
        settings.scope = "SELECTED"
        settings.tiles_index = index
        bpy.ops.geotools.inspect_open()
        return settings.result

    timed("open root", lambda: open_index(0))
    root = bpy.context.view_layer.objects.active
    leaves = [i for i, t in enumerate(tiles) if t.role == "leaf"]
    largest = max(leaves, key=lambda i: tiles[i].triangles)
    timed("open largest leaf", lambda: open_index(largest))
    leaf = bpy.context.view_layer.objects.active
    by_depth: dict[int, int] = {}
    for t in tiles:
        by_depth[t.depth] = by_depth.get(t.depth, 0) + 1
    depth = max(d for d, c in by_depth.items() if c <= 64)
    settings.scope = "DEPTH"
    settings.open_depth = depth
    settings.limit = 64
    timed(
        f"open depth {depth} ({by_depth[depth]} tiles)",
        lambda: (bpy.ops.geotools.inspect_open(), settings.result)[1],
    )

    inspect.write_face_selection(leaf.data, np.ones(len(leaf.data.polygons), bool))
    bpy.context.view_layer.objects.active = leaf
    timed(
        "select source (whole leaf)",
        lambda: (bpy.ops.geotools.inspect_select_source(edit=False), settings.result)[1],
    )
    timed(
        "select output",
        lambda: (bpy.ops.geotools.inspect_select_output(edit=False), settings.result)[1],
    )
    bpy.context.view_layer.objects.active = root
    timed(
        "distance heatmap (root)", lambda: (bpy.ops.geotools.inspect_distance(), settings.result)[1]
    )
    bpy.context.view_layer.objects.active = leaf
    timed(
        "texel density (leaf)",
        lambda: (bpy.ops.geotools.inspect_texel_density(), settings.result)[1],
    )


main()
