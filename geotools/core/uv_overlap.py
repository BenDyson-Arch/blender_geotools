"""Find UV triangles that share texels: the ``uv-overlap`` check. numpy only.

The ownership rule
==================

rusty-tiles decides texture-space ownership in ``src/mesh_large/batching.rs``
(``shared_footprints``, ``sweep``, ``period_copies``, ``leaf_shared_triangles``;
contract: ``docs/architecture/h28-tile-batching-contract.md``, invariant 5; read
at rusty-tiles develop 85157619). Its
rule is "one triangle per texel" over decoded-UV footprints, and it is exact and
geometric, not a raster:

- Two triangles share texels when their UV *interiors* share area. Shared edges
  and shared corners do not count. A triangle of zero area owns no interior and
  is never shared.
- A coordinate that is not finite, or not below 8 in magnitude, makes its
  triangle shared, conservatively.
- On a period axis (sampler wrap REPEAT, period 1, or MIRRORED_REPEAT, period 2)
  a footprint is compared through its copies under the axis' symmetry that reach
  ``[0, 1]`` (REPEAT: ``x - 1``; MIRRORED: ``2 - x`` and ``x - 2``), so two
  triangles a whole period apart share, and a triangle that crosses a mirror
  line shares with its own mirror image. A footprint wider than 1 on a period
  axis reads some texel twice; rusty-tiles then counts *every* triangle of the
  page as shared.
- CLAMP_TO_EDGE has no copies: footprints are compared where they lie, also
  outside ``[0, 1]``.

GeoTools needs a texel count, and must run on 10M triangles in seconds, so it
rasterises the same rule. Texel ``(i, j)`` of a ``W`` x ``H`` grid has its
centre at ``((i + 0.5) / W, (j + 0.5) / H)``. A triangle owns a texel when the
centre, nudged by an infinitesimal ``(e, e^2)``, lies in the triangle's open
interior; concretely, for each edge the edge function is positive, or zero with
an inward normal ``n`` having ``n.x > 0``, or ``n.x == 0`` and ``n.y > 0``. Two
triangles whose interiors are disjoint therefore never own the same centre,
whatever their edges and corners share. Edge functions are evaluated in binary64
from a canonical endpoint order, so the two triangles of a shared edge get
exactly opposite values and exactly one of them owns a centre on it. On a period
axis the texel index is folded (REPEAT: ``i mod W``; MIRRORED: ``i mod 2W``,
mirrored in the second half), so texels reached through any copy are the same
texel. A texel is *overlapping* when two owned samples fold onto it, from two
triangles or from one triangle and its own mirror image.

The raster finds every overlap that covers a texel centre, and nothing that
rusty-tiles would not call shared. An overlap thinner than a texel can be
missed; rusty-tiles would still see it. The non-finite or out-of-range
coordinates and the wide footprints are flagged per triangle as rusty-tiles does.

Performance: triangles are grouped by the power-of-two size of their texel
bounding box and tested in broadcast blocks, with no per-triangle Python loop.
Cost grows with the number of candidate texels, roughly ``W * H`` times the sum
of the triangles' UV bounding-box areas. When that exceeds ``max_samples``, the
grid is halved until it fits and the result reports the resolution used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from .issues import Issue, Severity

CODE = "uv-overlap"
"""Issue code of the check."""

EXACT_LIMIT = 8.0
"""rusty-tiles' exact domain: coordinates must be finite and below this in magnitude."""

DEFAULT_MAX_SAMPLES = 1 << 25
"""Candidate texel tests before the grid is coarsened: a few seconds of numpy."""

DEFAULT_MAX_GRID = 1 << 26
"""Most texels in the grid (8192 x 8192): bounds the memory of the bookkeeping arrays."""


class Wrap(StrEnum):
    """A sampler wrap mode, as glTF names them."""

    CLAMP = "CLAMP"  # CLAMP_TO_EDGE, 33071
    REPEAT = "REPEAT"  # 10497
    MIRRORED = "MIRRORED"  # MIRRORED_REPEAT, 33648


@dataclass(frozen=True, slots=True)
class OverlapResult:
    """What :func:`find_overlaps` found."""

    width: int  # resolution of the grid used
    height: int
    requested: tuple[int, int]  # resolution asked for
    triangles: int  # triangles looked at
    overlapping_texels: int  # texels owned more than once
    overlapping_triangles: int  # triangles rusty-tiles would count as shared
    examples: tuple[int, ...]  # some of them, lowest triangle index first
    out_of_range: int = 0  # non-finite or |uv| >= 8: shared conservatively
    wide: int = 0  # wider than one period on a period axis

    @property
    def coarsened(self) -> bool:
        return (self.width, self.height) != self.requested

    @property
    def ok(self) -> bool:
        return self.overlapping_triangles == 0


def find_overlaps(
    uv: np.ndarray,
    triangles: np.ndarray | None = None,
    *,
    resolution: int | tuple[int, int] = 1024,
    wrap: Wrap | str | Sequence[Wrap | str] = Wrap.REPEAT,
    max_samples: int = DEFAULT_MAX_SAMPLES,
    max_grid: int = DEFAULT_MAX_GRID,
    max_examples: int = 8,
) -> OverlapResult:
    """Count the texels that more than one UV triangle owns.

    ``uv`` is ``(T, 3, 2)`` corner UVs per triangle or, with ``triangles``
    (``(T, 3)`` indices into it), ``(N, 2)`` UVs. ``resolution`` is the image
    size (one int for a square), ``wrap`` the sampler wrap of both axes or of
    (S, T). The grid is coarsened (halved) while more than ``max_samples``
    candidate texels or more than ``max_grid`` texels would be needed.
    """
    corners = _corners(uv, triangles)
    count = corners.shape[0]
    req = _resolution(resolution)
    wraps = _wraps(wrap)

    lo, hi, in_range = _boxes(corners)
    wide = np.zeros(count, bool)
    for axis, mode in enumerate(wraps):
        if mode is not Wrap.CLAMP:
            wide |= in_range & (hi[:, axis] - lo[:, axis] > 1.0)

    idx = np.flatnonzero(in_range).astype(np.int32 if count < 2**31 else np.int64)
    lo, hi = lo[idx], hi[idx]
    grid = _choose_grid(lo, hi, req, wraps, max_samples, max_grid)
    flags = np.zeros(count, bool)
    texels = _raster(corners, idx, grid, wraps, flags)

    flags |= ~in_range | wide
    examples = tuple(int(i) for i in np.flatnonzero(flags)[: max(0, max_examples)])
    return OverlapResult(
        width=grid.width,
        height=grid.height,
        requested=req,
        triangles=count,
        overlapping_texels=texels,
        overlapping_triangles=count if wide.any() else int(np.count_nonzero(flags)),
        examples=examples,
        out_of_range=count - idx.size,
        wide=int(np.count_nonzero(wide)),
    )


DEFAULT_COVERAGE_SAMPLES = 1 << 28
"""Candidate texel tests of :func:`texel_coverage` before its grid is coarsened."""


@dataclass(frozen=True, slots=True)
class Coverage:
    """How many UV triangles own each texel: what :func:`texel_coverage` found.

    ``count`` is ``(height, width)`` uint8 (saturating at 255), row 0 at
    ``v = 0``: the row order of Blender's ``Image.pixels``. 0 is outside every
    UV island, 1 is owned once, 2 or more is overlapping.
    """

    width: int  # resolution of the grid used
    height: int
    requested: tuple[int, int]
    count: np.ndarray

    @property
    def coarsened(self) -> bool:
        return (self.width, self.height) != self.requested

    def resampled(self) -> np.ndarray:
        """``count`` on the requested grid (nearest texel if the grid was coarsened)."""
        req_w, req_h = self.requested
        if not self.coarsened:
            return self.count
        rows = (np.arange(req_h, dtype=np.int64) * self.height) // req_h
        cols = (np.arange(req_w, dtype=np.int64) * self.width) // req_w
        return self.count[rows[:, None], cols[None, :]]

    @property
    def inside(self) -> np.ndarray:
        """``(height, width)`` bool: texels inside a UV island, on the requested grid."""
        return self.resampled() > 0

    @property
    def overlapping(self) -> np.ndarray:
        """``(height, width)`` bool: texels owned more than once, on the requested grid."""
        return self.resampled() > 1


def texel_coverage(
    uv: np.ndarray,
    triangles: np.ndarray | None = None,
    *,
    resolution: int | tuple[int, int] = 1024,
    wrap: Wrap | str | Sequence[Wrap | str] = Wrap.REPEAT,
    max_samples: int = DEFAULT_COVERAGE_SAMPLES,
) -> Coverage:
    """Count the UV triangles that own each texel of a ``resolution`` image.

    The same raster and ownership rule as :func:`find_overlaps`, so texels with
    a count above 1 are exactly the texels it calls overlapping. Triangles with
    coordinates outside rusty-tiles' exact domain are left out. On a CLAMP axis
    texels outside the image are dropped. The grid is halved while more than
    ``max_samples`` candidate texels would be tested; :meth:`Coverage.resampled`
    brings the counts back to the requested size.
    """
    corners = _corners(uv, triangles)
    req = _resolution(resolution)
    wraps = _wraps(wrap)
    lo, hi, in_range = _boxes(corners)
    count = corners.shape[0]
    idx = np.flatnonzero(in_range).astype(np.int32 if count < 2**31 else np.int64)
    lo, hi = lo[idx], hi[idx]
    grid = _choose_grid(lo, hi, req, wraps, max_samples, max(DEFAULT_MAX_GRID, req[0] * req[1]))
    keys, _ = _samples(corners, idx, grid, wraps, owners=False)
    w, h = grid.width, grid.height
    if keys is None:
        return Coverage(w, h, req, np.zeros((h, w), np.uint8))
    counts = np.bincount(keys, minlength=grid.gw * grid.gh)
    del keys
    counts = np.minimum(counts, 255).astype(np.uint8).reshape(grid.gh, grid.gw)
    # Crop the key grid (larger only on a CLAMP axis) to the image.
    x, y = -grid.gx0, -grid.gy0
    counts = np.ascontiguousarray(counts[y : y + h, x : x + w])
    return Coverage(w, h, req, counts)


def overlap_issue(
    result: OverlapResult,
    subject: str,
    *,
    uv_map: str = "",
    severity: Severity = Severity.WARNING,
    examples: Sequence[int] | None = None,
    example_label: str = "triangles",
) -> Issue | None:
    """The ``uv-overlap`` Issue for ``result`` (None if nothing overlaps).

    ``examples`` replaces ``result.examples`` in the message, e.g. with face
    indices; ``example_label`` names them.
    """
    if result.ok:
        return None
    where = f"{subject}" + (f" (UV map {uv_map})" if uv_map else "")
    grid = f"{result.width}x{result.height}"
    if result.coarsened:
        req_w, req_h = result.requested
        grid += f", coarsened from {req_w}x{req_h}"
    texels = result.overlapping_texels
    parts = [
        (
            f"{where}: {texels:,} texel{'s' * (texels != 1)} at {grid} "
            f"{'are' if texels != 1 else 'is'} covered by more than one UV triangle, and "
            f"{result.overlapping_triangles:,} of {result.triangles:,} triangles share texels."
        )
    ]
    if result.wide:
        parts.append(
            f"{result.wide:,} triangle{'s span' if result.wide != 1 else ' spans'} more than "
            "one UV period, so every triangle counts as sharing."
        )
    if result.out_of_range:
        parts.append(
            f"{result.out_of_range:,} triangle{'s have' if result.out_of_range != 1 else ' has'} "
            f"UVs that are not finite or not within ±{EXACT_LIMIT:g}."
        )
    shown = list(result.examples if examples is None else examples)
    if shown:
        parts.append(f"For example {example_label} {', '.join(str(i) for i in shown)}.")
    return Issue(Severity(severity), CODE, " ".join(parts), subject)


# --- internals ------------------------------------------------------------------------------

_BLOCK = 1 << 18  # candidate texels per broadcast block
_CHUNK = 1 << 20  # triangles per chunk of the per-triangle passes
_TINY = float(np.nextafter(0.0, 1.0))


@dataclass(slots=True)
class _Grid:
    """The grid chosen, and the texel-centre box of each in-range triangle on it."""

    width: int
    height: int
    x0: np.ndarray  # first texel index of the box
    nx: np.ndarray  # texel centres in the box (0: none)
    y0: np.ndarray
    ny: np.ndarray
    gx0: int  # origin and size of the key grid (beyond [0, size) only for CLAMP)
    gw: int
    gy0: int
    gh: int


def _corners(uv: np.ndarray, triangles: np.ndarray | None) -> np.ndarray:
    uv = np.asarray(uv)
    if triangles is not None:
        tris = np.asarray(triangles)
        if uv.ndim != 2 or uv.shape[1] != 2:
            raise ValueError(f"uv must be (N, 2) with triangles, got {uv.shape}")
        if tris.ndim != 2 or tris.shape[1] != 3:
            raise ValueError(f"triangles must be (T, 3), got {tris.shape}")
        uv = uv[tris]
    if uv.ndim != 3 or uv.shape[1:] != (3, 2):
        raise ValueError(f"uv must be (T, 3, 2), got {uv.shape}")
    if uv.dtype not in (np.float32, np.float64):
        uv = uv.astype(np.float64)
    return uv


def _resolution(resolution: int | tuple[int, int]) -> tuple[int, int]:
    if isinstance(resolution, (int, np.integer)):
        w = h = int(resolution)
    else:
        w, h = (int(v) for v in resolution)
    if w < 1 or h < 1:
        raise ValueError(f"resolution must be positive, got {resolution!r}")
    return w, h


def _wraps(wrap: Wrap | str | Sequence[Wrap | str]) -> tuple[Wrap, Wrap]:
    if isinstance(wrap, str):
        return Wrap(wrap), Wrap(wrap)
    s, t = wrap
    return Wrap(s), Wrap(t)


def _boxes(corners: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """UV boxes in the input precision (zero outside the exact domain) and in-domain flags."""
    count = corners.shape[0]
    lo = np.zeros((count, 2), corners.dtype)
    hi = np.zeros((count, 2), corners.dtype)
    in_range = np.zeros(count, bool)
    for s in range(0, count, _CHUNK):
        c = corners[s : s + _CHUNK]
        low = np.minimum(np.minimum(c[:, 0], c[:, 1]), c[:, 2])  # NaN propagates
        high = np.maximum(np.maximum(c[:, 0], c[:, 1]), c[:, 2])
        ok = (np.maximum(-low, high) < EXACT_LIMIT).all(axis=1)  # False for NaN
        ok &= np.isfinite(low).all(axis=1)  # -inf < 8
        in_range[s : s + _CHUNK] = ok
        lo[s : s + _CHUNK] = np.where(ok[:, None], low, 0)
        hi[s : s + _CHUNK] = np.where(ok[:, None], high, 0)
    return lo, hi, in_range


def _span(lo: np.ndarray, hi: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    """First texel index and count of texel centres in ``[lo, hi]`` (UV) on one axis."""
    first = np.ceil(lo.astype(np.float64) * size - 0.5)
    last = np.floor(hi.astype(np.float64) * size - 0.5)
    np.subtract(last, first, out=last)
    last += 1
    np.maximum(last, 0, out=last)
    return first.astype(np.int32), last.astype(np.int32)  # |index| < 2^31 within the budgets


def _extent(first: np.ndarray, n: np.ndarray, size: int, mode: Wrap) -> tuple[int, int]:
    """Origin and size of the key grid on one axis."""
    if mode is not Wrap.CLAMP:
        return 0, size
    has = n > 0
    if not has.any():
        return 0, size
    start = min(0, int(first[has].min()))
    stop = max(size, int((first[has] + n[has]).max()))
    return start, stop - start


def _choose_grid(
    lo: np.ndarray,
    hi: np.ndarray,
    req: tuple[int, int],
    wraps: tuple[Wrap, Wrap],
    max_samples: int,
    max_grid: int,
) -> _Grid:
    """The largest grid, halving from ``req``, within both budgets."""
    w, h = req
    extent = np.maximum(hi - lo, 0).astype(np.float64)
    area = float(np.dot(extent[:, 0], extent[:, 1])) if extent.size else 0.0
    if extent.size:
        far = float(np.max(np.abs(np.concatenate([lo, hi]))))
    else:
        far = 0.0
    # Cheap estimates first: candidates ~ w * h * area, grid <= (2 * far + 1)^2 w h.
    reach = 2 * far + 1 if Wrap.CLAMP in wraps else 1.0
    while w * h > 1 and (w * h * area > max_samples or w * h * reach * reach > 64 * max_grid):
        w, h = max(1, w // 2), max(1, h // 2)
    while True:
        x0, nx = _span(lo[:, 0], hi[:, 0], w)
        y0, ny = _span(lo[:, 1], hi[:, 1], h)
        gx0, gw = _extent(x0, nx, w, wraps[0])
        gy0, gh = _extent(y0, ny, h, wraps[1])
        fits = int(np.dot(nx.astype(np.int64), ny)) <= max_samples and gw * gh <= max_grid
        if fits or (w == 1 and h == 1):
            return _Grid(w, h, x0, nx, y0, ny, gx0, gw, gy0, gh)
        w, h = max(1, w // 2), max(1, h // 2)


def _fold(index: np.ndarray, size: int, mode: Wrap) -> np.ndarray:
    if mode is Wrap.REPEAT:
        return np.mod(index, size)
    if mode is Wrap.MIRRORED:
        m = np.mod(index, 2 * size)
        return np.where(m < size, m, 2 * size - 1 - m)
    return index


def _edge(px, py, qx, qy) -> tuple[np.ndarray, ...]:
    """Edge function terms of the directed edges p -> q, from canonical endpoints.

    Returns ``(lx, ly, ex, ey, threshold)``: ``w(x, y) = ex * (y - ly) - ey * (x - lx)``
    is positive left of p -> q, and exactly opposite for q -> p, since both are
    computed from the same endpoint. A centre is on the inside of the edge when
    ``w > threshold``: 0, or just below 0 when a point on the edge belongs to the
    left side (the inward normal (-ey, ex) points +x, or +y on a vertical edge).
    """
    swap = (qx < px) | ((qx == px) & (qy < py))
    lx = np.where(swap, qx, px)
    ly = np.where(swap, qy, py)
    ex = qx - px  # = sign * (high - low), exactly
    ey = qy - py
    tie = (ey < 0) | ((ey == 0) & (ex > 0))
    # w >= 0 is w > -(smallest subnormal): nothing lies between them.
    return lx, ly, ex, ey, np.where(tie, -_TINY, 0.0)


def _raster(
    corners: np.ndarray, idx: np.ndarray, grid: _Grid, wraps: tuple[Wrap, Wrap], flags: np.ndarray
) -> int:
    """Set ``flags`` of the triangles that own an overlapping texel; return the texel count.

    ``idx`` are the in-range triangles, in the order of ``grid``'s boxes.
    """
    keys, owners = _samples(corners, idx, grid, wraps, owners=True)
    if keys is None:
        return 0
    size = grid.gw * grid.gh
    sample = np.arange(keys.size, dtype=np.int32 if keys.size < 2**31 else np.int64)
    # Each texel keeps one writer; a sample that is not its texel's writer shares it.
    winner = np.empty(size, sample.dtype)
    winner[keys] = sample
    lost = winner[keys] != sample
    del winner, sample
    overlapping = np.zeros(size, bool)
    overlapping[keys[lost]] = True
    texels = int(np.count_nonzero(overlapping))
    if texels:
        flags[owners[overlapping[keys]]] = True
    return texels


def _samples(
    corners: np.ndarray,
    idx: np.ndarray,
    grid: _Grid,
    wraps: tuple[Wrap, Wrap],
    *,
    owners: bool,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Every owned texel sample: its key in the key grid and, if asked, its triangle.

    ``(None, None)`` when no triangle owns a texel.
    """
    has = (grid.nx > 0) & (grid.ny > 0)
    tri, x0, nx, y0, ny = idx[has], grid.x0[has], grid.nx[has], grid.y0[has], grid.ny[has]
    if not tri.size:
        return None, None

    # Split tall boxes into row slabs so no box exceeds a block.
    rows = np.maximum(_BLOCK // nx, 1)
    slabs = (ny + rows - 1) // rows
    if np.any(slabs > 1):
        slab_of = np.repeat(np.arange(tri.size), slabs)
        first = np.cumsum(slabs) - slabs
        part = np.arange(slab_of.size) - np.repeat(first, slabs)
        y0 = y0[slab_of] + part * rows[slab_of]
        ny = np.minimum(rows[slab_of], ny[slab_of] - part * rows[slab_of])
        x0, nx, tri = x0[slab_of], nx[slab_of], tri[slab_of]

    # Bucket by power-of-two box size; each bucket is tested as broadcast blocks
    # over contiguous slices.
    bucket = (_ceil_log2(nx) << 6) | _ceil_log2(ny)
    order = np.argsort(bucket, kind="stable")
    bucket, tri, x0, nx, y0, ny = (v[order] for v in (bucket, tri, x0, nx, y0, ny))
    del order

    size = grid.gw * grid.gh
    key_type = np.int32 if size < 2**31 else np.int64
    fx0 = x0 + 0.5
    fy0 = y0 + 0.5
    keys_out: list[np.ndarray] = []
    tris_out: list[np.ndarray] = []
    for c0 in range(0, tri.size, _CHUNK):
        c1 = min(c0 + _CHUNK, tri.size)
        edges = _triangle_edges(corners[tri[c0:c1]], grid)
        part = bucket[c0:c1]
        starts = np.concatenate(([0], np.flatnonzero(np.diff(part)) + 1, [part.size]))
        for g0, g1 in zip(starts[:-1].tolist(), starts[1:].tolist(), strict=True):
            gx = 1 << int(part[g0] >> 6)
            gy = 1 << int(part[g0] & 63)
            cells = gx * gy
            jx = np.tile(np.arange(gx, dtype=np.float64), gy)
            jy = np.repeat(np.arange(gy, dtype=np.float64), gx)
            per = max(1, _BLOCK // cells)
            for s in range(g0, g1, per):
                e = min(s + per, g1)
                o = slice(c0 + s, c0 + e)  # into the slab arrays
                px = fx0[o, None] + jx
                py = fy0[o, None] + jy
                if cells > 1:
                    inside = (jx < nx[o, None]) & (jy < ny[o, None])
                else:
                    inside = np.ones(px.shape, bool)
                t1 = np.empty(px.shape)
                t2 = np.empty(px.shape)
                hit = np.empty(px.shape, bool)
                for lx, ly, ex, ey, thr in edges:
                    np.subtract(py, ly[s:e, None], out=t1)
                    t1 *= ex[s:e, None]
                    np.subtract(px, lx[s:e, None], out=t2)
                    t2 *= ey[s:e, None]
                    t1 -= t2
                    np.greater(t1, thr[s:e, None], out=hit)
                    inside &= hit
                r, k = np.nonzero(inside)
                if not r.size:
                    continue
                r += c0 + s
                fx = _fold(x0[r] + jx[k].astype(np.int64), grid.width, wraps[0]) - grid.gx0
                fy = _fold(y0[r] + jy[k].astype(np.int64), grid.height, wraps[1]) - grid.gy0
                keys_out.append((fy * grid.gw + fx).astype(key_type, copy=False))
                if owners:
                    tris_out.append(tri[r])
    if not keys_out:
        return None, None
    keys = np.concatenate(keys_out)
    del keys_out
    return keys, (np.concatenate(tris_out) if owners else None)


def _triangle_edges(c: np.ndarray, grid: _Grid) -> list[tuple[np.ndarray, ...]]:
    """The three counter-clockwise edges of each triangle in texel space (binary64).

    A zero-area triangle gets an infinite threshold: it owns nothing.
    """
    c = c.astype(np.float64)  # a float32 array times a Python float stays float32
    sx, sy = float(grid.width), float(grid.height)
    ax, ay = c[:, 0, 0] * sx, c[:, 0, 1] * sy
    bx, by = c[:, 1, 0] * sx, c[:, 1, 1] * sy
    cx, cy = c[:, 2, 0] * sx, c[:, 2, 1] * sy
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    cw = area < 0  # make counter-clockwise: swap b and c
    bx, cx = np.where(cw, cx, bx), np.where(cw, bx, cx)
    by, cy = np.where(cw, cy, by), np.where(cw, by, cy)
    edges = [_edge(ax, ay, bx, by), _edge(bx, by, cx, cy), _edge(cx, cy, ax, ay)]
    edges[0][4][area == 0] = np.inf
    return edges


def _ceil_log2(n: np.ndarray) -> np.ndarray:
    """Smallest k with 2**k >= n, for n >= 1."""
    mantissa, exponent = np.frexp(n.astype(np.float64))
    return (exponent - (mantissa == 0.5)).astype(np.int64)
