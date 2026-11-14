"""Time the uv-overlap check on a synthetic 10M-triangle UV layout.

    python scripts/bench_uv_overlap.py [--triangles 10000000] [--resolution 4096]

The layout is a regular grid of quads over [0, 1]^2 (two triangles each), with
one island copied onto another so the check has something to find. Needs numpy.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geotools.core import uv_overlap


def layout(triangles: int) -> tuple[np.ndarray, np.ndarray]:
    n = max(1, round((triangles / 2) ** 0.5))
    xs = np.linspace(0.0, 1.0, n + 1, dtype=np.float32)
    u, v = np.meshgrid(xs, xs)
    uv = np.stack([u.ravel(), v.ravel()], axis=1)
    i, j = np.meshgrid(np.arange(n), np.arange(n))
    a = (j * (n + 1) + i).ravel()
    b, c, d = a + 1, a + n + 2, a + n + 1
    tris = np.concatenate([np.stack([a, b, c], 1), np.stack([a, c, d], 1)])
    # Overlap: the first 1000 quads' triangles reuse the corners of quads further on.
    tris[:1000] = tris[n * n // 2 : n * n // 2 + 1000]
    return uv, tris.astype(np.int32)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--triangles", type=int, default=10_000_000)
    parser.add_argument("--resolution", type=int, default=4096)
    parser.add_argument("--max-samples", type=int, default=uv_overlap.DEFAULT_MAX_SAMPLES)
    args = parser.parse_args()
    uv, tris = layout(args.triangles)
    start = time.perf_counter()
    corners = uv[tris]
    gathered = time.perf_counter()
    result = uv_overlap.find_overlaps(
        corners, resolution=args.resolution, max_samples=args.max_samples
    )
    done = time.perf_counter()
    print(
        f"{len(tris):,} triangles, requested {args.resolution}, used {result.width}x{result.height}"
    )
    print(f"gather {gathered - start:.2f} s, check {done - gathered:.2f} s")
    print(
        f"{result.overlapping_texels:,} overlapping texels, "
        f"{result.overlapping_triangles:,} triangles share, e.g. {result.examples[:4]}"
    )


if __name__ == "__main__":
    main()
