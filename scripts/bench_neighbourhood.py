"""Time the neighbourhood engine (``geotools.analysis.neighbourhood``) at several scales.

Plain Python with numpy, against the source checkout:

    python scripts/bench_neighbourhood.py [--cells 3391] [--spacing 0.0025] \\
        [--scales 0.01 0.03 0.1] [--mesh MESH.npz] [--max-pairs N]

The default surface is a synthetic rough terrain of ``2 × cells²`` triangles
(3391: about 23M) at ``spacing`` metres: a sum of random sines from 2 cm to
2 m wavelength, with amplitude falling with wavelength. ``--mesh`` times a
mesh from an ``.npz`` file with ``V`` (vertices, n × 3) and ``F`` (triangles,
m × 3) instead.

For each scale s (a ball of radius s/2) it runs what a surface metric does:
the query points (one per cube of side s/4, so dense patches cost no more
than coarse ones), the grid, :func:`ball_stats` with the normal filter,
:func:`plane`, :func:`resolution` and the transfer of the values back to every
face (:func:`nearest`). The distance to the nearest open edge is measured once
for all scales. It prints each step's time,
the pairs per second, the share of faces resolved and the peak memory.
"""

from __future__ import annotations

import argparse
import resource
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from geotools.analysis import neighbourhood as nb


def terrain(cells: int, spacing: float, seed: int = 0):
    """``(positions, face_starts, corner_verts)`` of a rough synthetic surface."""
    rng = np.random.default_rng(seed)
    x = np.arange(cells + 1) * spacing
    xx, yy = np.meshgrid(x, x, indexing="ij")
    z = np.zeros_like(xx)
    for wavelength in np.geomspace(0.02, 2.0, 24):
        angle = rng.uniform(0, np.pi)
        k = 2 * np.pi / wavelength
        phase = rng.uniform(0, 2 * np.pi)
        z += 0.05 * wavelength * np.sin(k * (np.cos(angle) * xx + np.sin(angle) * yy) + phase)
    positions = np.stack([xx, yy, z], -1).reshape(-1, 3)
    del xx, yy, z
    return positions, *triangles(cells)


def triangles(cells: int):
    idx = np.arange((cells + 1) ** 2, dtype=np.int64).reshape(cells + 1, cells + 1)
    a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
    tris = np.concatenate(
        [np.stack([a, b, c], -1).reshape(-1, 3), np.stack([a, c, d], -1).reshape(-1, 3)]
    )
    starts = np.arange(0, 3 * len(tris) + 1, 3, dtype=np.int64)
    return starts, tris.ravel()


def load(path: Path):
    z = np.load(path)
    faces = np.asarray(z["F"], dtype=np.int64)
    starts = np.arange(0, 3 * len(faces) + 1, 3, dtype=np.int64)
    return np.asarray(z["V"], dtype=np.float64), starts, faces.ravel()


def peak_gb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**2


class Clock:
    def __init__(self) -> None:
        self.t = time.perf_counter()

    def lap(self, label: str, extra: str = "") -> float:
        now = time.perf_counter()
        dt, self.t = now - self.t, now
        print(f"  {label:<28} {dt:8.1f} s  {extra}", flush=True)
        return dt


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cells", type=int, default=3391)
    ap.add_argument("--spacing", type=float, default=0.0025)
    ap.add_argument("--scales", type=float, nargs="+", default=[0.01, 0.03, 0.1])
    ap.add_argument("--mesh", type=Path)
    ap.add_argument("--max-pairs", type=int, default=nb.MAX_PAIRS)
    a = ap.parse_args()

    clock = Clock()
    mesh = load(a.mesh) if a.mesh else terrain(a.cells, a.spacing)
    positions, starts, corners = mesh
    print(f"{len(starts) - 1:,} faces, {len(positions):,} vertices", flush=True)
    clock.lap("mesh")
    smp = nb.face_samples(positions, starts, corners)
    clock.lap("face_samples")
    reach = nb.EDGE_FRACTION * max(a.scales)
    edge = nb.edge_distance(positions, starts, corners, smp.points, reach)
    clock.lap("edge_distance", f"{int(np.isfinite(edge).sum()):,} faces within {reach * 1000:g} mm")
    order = np.argsort(smp.size)
    run = np.cumsum(smp.area[order])
    h = float(smp.size[order][np.searchsorted(run, run[-1] / 2)])
    print(f"  area-weighted median edge {h * 1000:.2f} mm, peak {peak_gb():.1f} GB")

    for s in a.scales:
        r = s / 2
        print(f"scale {s * 100:g} cm (radius {r * 1000:g} mm)", flush=True)
        clock = Clock()
        step = s / 4
        qid = nb.thin(smp.points, step)
        q = smp.points[qid]
        clock.lap("queries", f"{len(q):,}")
        g = nb.grid(smp.points, r)
        clock.lap("grid", f"{len(g.keys):,} cells")
        ball = nb.ball_stats(smp, q, r, g=g, query_normals=smp.normal[qid], max_pairs=a.max_pairs)
        pairs = int(ball.count.sum())
        dt = clock.lap("ball_stats", f"{pairs:,} pairs")
        print(f"  {'':<28} {pairs / max(dt, 1e-9) / 1e6:8.1f} M pairs/s")
        pl = nb.plane(ball, q)
        clock.lap("plane")
        status = nb.resolution(ball, edge=edge[qid] < nb.EDGE_FRACTION * s)
        clock.lap("resolution")
        if len(q) < len(smp):
            index, _ = nb.nearest(q, smp.points, step * np.sqrt(3))
            clock.lap("transfer to faces")
            face_status = np.where(index >= 0, status[np.maximum(index, 0)], nb.EMPTY)
        else:
            face_status = status
        ok = face_status == nb.RESOLVED
        share = float(smp.area[ok].sum() / smp.area.sum())
        print(
            f"  resolved {share:.1%} of the area; median RMS "
            f"{np.nanmedian(pl.rms[status == nb.RESOLVED]) * 1000 if (status == nb.RESOLVED).any() else float('nan'):.3f} mm; "
            f"peak {peak_gb():.1f} GB",
            flush=True,
        )


if __name__ == "__main__":
    main()
