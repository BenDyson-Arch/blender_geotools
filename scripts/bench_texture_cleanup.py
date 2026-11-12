"""Time the M6 texture cleanup tools on synthetic 8K atlas pages.

    blender -b --factory-startup --python scripts/bench_texture_cleanup.py -- \\
        [--size 8192] [--triangles 2000000] [--chunks 48]

Runs the repository's ``geotools`` package directly (not an installed
extension). The source is a height field of ``--triangles`` triangles cut into
``chunks`` x ``chunks`` UV islands, packed into two atlas pages of
``--size`` texels a side (left and right halves of the site), with 8-texel
gaps of black background between islands, like a photogrammetry atlas. The
pages are synthetic: a smooth colour field plus noise, the right page darker
(gain 0.8) so the balance has something to fit. Before each tool the page gets
what that tool fixes: scattered black texels and a 300-texel hole, a block in
the key colour.

It times, through the operators (so including Blender's pixel reads and
writes): Paint Texture (the UV raster for the overlap stencil), Fill Holes,
Remove Masked, Pad Islands (16 texels) and Fit and Apply Balance, and reports
the fill's error against the known field.
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


def field(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """A smooth linear-light colour field over the site (u, v in [0, 1])."""
    return np.stack(
        [0.10 + 0.40 * u + 0.15 * v, 0.12 + 0.30 * v + 0.10 * u, 0.08 + 0.2 * (u * v)],
        axis=-1,
    )


def site(triangles: int, chunks: int, size: int):
    """Two objects (left and right halves), each on its own page, with chunked UV islands."""
    from geotools.core.texture_cleanup import linear_to_srgb

    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    bpy.context.scene.geotools.source_collection = collection
    per_half = max(2, round((triangles / 4) ** 0.5))  # quads per side of each half
    per_half -= per_half % chunks
    objects, pages, truths = [], [], []
    gap = 8 / size
    cell = 1.0 / chunks
    for half, gain in ((0, 1.0), (1, 0.8)):
        n = per_half
        xs = np.linspace(0, 1, n + 1)
        gx, gy = np.meshgrid(xs, xs)
        world_u = (half + gx) / 2.0  # u over the whole site
        z = 2.0 * np.sin(6 * world_u) * np.cos(5 * gy)
        co = np.stack([100.0 * world_u, 50.0 * gy, z], axis=-1).reshape(-1, 3).astype(np.float32)
        i, j = np.meshgrid(np.arange(n), np.arange(n))
        a = (j * (n + 1) + i).ravel()
        quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)
        # UVs: each chunk of the grid is its own island in its own atlas cell,
        # shrunk by the gap; the island keeps the chunk's layout.
        qi, qj = i.ravel(), j.ravel()
        ci, cj = qi * chunks // n, qj * chunks // n
        corner_i = np.stack([qi, qi + 1, qi + 1, qi], axis=1)
        corner_j = np.stack([qj, qj, qj + 1, qj + 1], axis=1)
        local_u = (corner_i - ci[:, None] * n / chunks) / (n / chunks)
        local_v = (corner_j - cj[:, None] * n / chunks) / (n / chunks)
        uv_u = ci[:, None] * cell + gap + local_u * (cell - 2 * gap)
        uv_v = cj[:, None] * cell + gap + local_v * (cell - 2 * gap)
        uv = np.stack([uv_u, uv_v], axis=-1).reshape(-1, 2).astype(np.float32)
        mesh = bpy.data.meshes.new(f"Half{half}")
        mesh.vertices.add(len(co))
        mesh.vertices.foreach_set("co", co.ravel())
        mesh.loops.add(quads.size)
        mesh.loops.foreach_set("vertex_index", quads.ravel())
        mesh.polygons.add(len(quads))
        mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
        mesh.uv_layers.new(name="UVMap").data.foreach_set("uv", uv.ravel())
        mesh.update()
        image = bpy.data.images.new(f"Page{half}", size, size, alpha=True)
        material = bpy.data.materials.new(f"Page{half}")
        material.use_nodes = True
        tree = material.node_tree
        bsdf = next(nd for nd in tree.nodes if nd.type == "BSDF_PRINCIPLED")
        tex = tree.nodes.new("ShaderNodeTexImage")
        tex.image = image
        tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        mesh.materials.append(material)
        obj = bpy.data.objects.new(f"Half{half}", mesh)
        collection.objects.link(obj)
        objects.append(obj)
        pages.append(image)
        truths.append((half, gain))
    # Pixels: the field, mapped back from each texel's island to the site.
    rng = np.random.default_rng(0)
    t = (np.arange(size) + 0.5) / size
    cu, cv = np.meshgrid(t, t)
    ci = np.minimum((cu / cell).astype(int), chunks - 1)
    cj = np.minimum((cv / cell).astype(int), chunks - 1)
    lu = (cu - ci * cell - gap) / (cell - 2 * gap)
    lv = (cv - cj * cell - gap) / (cell - 2 * gap)
    inside = (lu >= 0) & (lu <= 1) & (lv >= 0) & (lv <= 1)
    for image, (half, gain) in zip(pages, truths, strict=True):
        su = (half + (ci + np.clip(lu, 0, 1)) / chunks) / 2.0
        sv = (cj + np.clip(lv, 0, 1)) / chunks
        rgba = np.ones((size, size, 4), np.float32)
        lin = field(su, sv) * gain
        rgba[..., :3] = linear_to_srgb(lin) + rng.normal(0, 0.01, (size, size, 3))
        rgba[~inside, :3] = 0.0
        np.clip(rgba, 0, 1, out=rgba)
        image.pixels.foreach_set(rgba.ravel())
        image.pack()
    del rgba, lin
    return objects, pages, inside


def timed(label: str, op, **kw) -> float:
    began = time.perf_counter()
    result = op(**kw)
    seconds = time.perf_counter() - began
    print(f"{label}: {seconds:.2f} s {result}", flush=True)
    return seconds


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=8192)
    parser.add_argument("--triangles", type=int, default=2_000_000)
    parser.add_argument("--chunks", type=int, default=48)
    args = parser.parse_args(argv)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    geotools.register()
    from geotools.blender import texture_cleanup as cleanup
    from geotools.core import texture_cleanup as tc

    began = time.perf_counter()
    objects, pages, inside = site(args.triangles, args.chunks, args.size)
    tris = sum(len(o.data.polygons) * 2 for o in objects)
    print(
        f"site: {tris:,} triangles, 2 pages of {args.size}x{args.size}, "
        f"{inside.mean():.0%} of texels in islands, built in {time.perf_counter() - began:.1f} s",
        flush=True,
    )
    page = pages[0]
    bpy.context.view_layer.objects.active = objects[0]
    objects[0].select_set(True)
    props = bpy.context.scene.geotools_texture
    props.image = page

    started = time.perf_counter()
    px = cleanup.read_pixels(page)
    print(f"read the page's pixels: {time.perf_counter() - started:.2f} s", flush=True)
    truth = px.copy()

    timed("Paint Texture (UV raster, overlap stencil)", bpy.ops.geotools.texture_paint_setup)
    bpy.ops.object.mode_set(mode="OBJECT")

    # Holes: scattered single texels plus a 300-texel disc, inside islands.
    rng = np.random.default_rng(1)
    holes = (rng.random(inside.shape) < 0.002) & inside
    yy, xx = np.ogrid[0 : args.size, 0 : args.size]
    disc = ((xx - args.size * 0.3) ** 2 + (yy - args.size * 0.6) ** 2 < 150**2) & inside
    holes |= disc
    damaged = px.copy()
    damaged[holes, :3] = 0.0
    page.pixels.foreach_set(damaged.ravel())
    del damaged
    props.grow = 0
    timed(f"Fill Holes ({int(holes.sum()):,} texels)", bpy.ops.geotools.texture_fill_holes)
    after = cleanup.read_pixels(page)
    err = np.abs(after[holes, :3] - truth[holes, :3])
    err_disc = np.abs(after[disc, :3] - truth[disc, :3])
    print(
        f"  fill error (8-bit levels): all holes mean {255 * err.mean():.2f}, "
        f"300-texel disc mean {255 * err_disc.mean():.2f} max {255 * err_disc.max():.1f} "
        "(the field has noise of 2.6 levels, which no fill can recover)",
        flush=True,
    )

    keyed = after
    block = np.zeros(inside.shape, bool)
    y0, x0 = args.size // 2, (args.size * 5) // 8
    block[y0 : y0 + args.size // 20, x0 : x0 + args.size // 27] = True  # about 400 x 300 at 8K
    block &= inside
    keyed[block, :3] = (1.0, 0.0, 1.0)
    page.pixels.foreach_set(keyed.ravel())
    del keyed, after
    timed(
        f"Remove Masked, key colour ({int(block.sum()):,} texels)",
        bpy.ops.geotools.texture_remove_masked,
    )
    timed("Pad Islands (16 texels)", bpy.ops.geotools.texture_pad_islands)
    timed("Fit Balance (2 pages)", bpy.ops.geotools.texture_balance_fit)
    for item in props.balance_pages:
        print(f"  {item.image.name}: gain {tuple(round(g, 3) for g in item.gain)}")
    timed("Apply Balance", bpy.ops.geotools.texture_balance_apply)
    timed("Undo Cleanup", bpy.ops.geotools.texture_cleanup_undo)
    del tc


main()
