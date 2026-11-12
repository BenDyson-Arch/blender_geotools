"""Texture cleanup maths for M6: hole filling, edge padding and page balance. numpy only.

Blender paints; GeoTools adds only what Blender's texture tools lack (see
``docs/texture-cleanup.md``):

- :func:`push_pull_fill` fills texels from their neighbours by push-pull
  (pyramid) interpolation (Gortler et al. 1996, "The Lumigraph", section 3.4):
  the image is averaged down a pyramid using only known texels, then pushed back
  up with bilinear upsampling, so each unknown texel takes the smooth average
  of the known texels around it at the finest level that has any. One pass
  over the pyramid costs about 4/3 of the image, whatever the hole size.
  :func:`fill_holes` restricts it to UV islands: it reads only known texels
  inside islands and writes only the holes inside islands.
- :func:`pad_islands` is island-aware edge padding (dilation): one ring of
  background texels per pass, each copying its nearest island texel
  (chessboard distance), so the padding continues the nearest island. It never
  writes a texel inside an island.
- :func:`fit_balance` fits a per-page, per-channel gain and offset so that
  colours sampled on both sides of seams between pages (atlas images) agree.

Images are ``(height, width, channels)`` float32 arrays with row 0 at ``v = 0``,
the row order of Blender's ``Image.pixels``. No function loops over texels in
Python: loops run over pyramid levels, padding rings, offsets or IRLS
iterations only.

Limits, stated in the user docs too: push-pull is a diffusion fill. It is smooth
and blurs texture detail across a filled area; it does not synthesise texture
the way content-aware (PatchMatch) fills do. Holes are filled from any island
texel within reach in texture space, so a large hole at an island's border can
borrow colour from a neighbouring island.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DARK_THRESHOLD = 1.0 / 255.0
"""A texel at or below this in every colour channel is a black hole."""

TRANSPARENT_THRESHOLD = 1.0 / 255.0
"""A texel at or below this alpha is a transparent hole."""

# The 8-neighbourhood, as (dy, dx).
_NEIGHBOURS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


# --- masks ---------------------------------------------------------------------------------


def hole_mask(
    pixels: np.ndarray,
    *,
    black: bool = True,
    transparent: bool = True,
    dark_threshold: float = DARK_THRESHOLD,
    alpha_threshold: float = TRANSPARENT_THRESHOLD,
) -> np.ndarray:
    """``(H, W)`` bool: texels that hold no data (black, or transparent).

    ``black`` texels have every colour channel at or below ``dark_threshold``;
    ``transparent`` ones have alpha (the fourth channel, if any) at or below
    ``alpha_threshold``.
    """
    pixels = _image(pixels)
    holes = np.zeros(pixels.shape[:2], bool)
    colour = min(pixels.shape[2], 3)
    if black:
        dark = pixels[..., 0] <= dark_threshold
        for c in range(1, colour):
            dark &= pixels[..., c] <= dark_threshold
        holes |= dark
    if transparent and pixels.shape[2] == 4:
        holes |= pixels[..., 3] <= alpha_threshold
    return holes


def grow_mask(mask: np.ndarray, pixels: int) -> np.ndarray:
    """``mask`` grown by ``pixels`` texels in every direction (chessboard distance)."""
    mask = np.asarray(mask, bool)
    if pixels <= 0 or not mask.any():
        return mask.copy()
    out = mask.copy()
    # Separable: a square structuring element is a row pass then a column pass,
    # each done by doubling shifts (1, 2, 4, ...) so the cost is O(log pixels).
    for axis in (0, 1):
        out = _grow_axis(out, pixels, axis)
    return out


def _grow_axis(mask: np.ndarray, radius: int, axis: int) -> np.ndarray:
    """Grow along one axis by ``radius``: OR of the shifts -radius..radius."""
    out = mask.copy()
    reach = 0  # out holds the OR of shifts in [-reach, reach]
    while reach < radius:
        step = min(reach + 1, radius - reach)
        grown = out.copy()
        _or_shift(grown, out, step, axis)
        _or_shift(grown, out, -step, axis)
        out = grown
        reach += step
    return out


def _or_shift(dst: np.ndarray, src: np.ndarray, shift: int, axis: int) -> None:
    """``dst |= src`` shifted by ``shift`` along ``axis`` (no wrap)."""
    n = src.shape[axis]
    if abs(shift) >= n:
        return
    if axis == 0:
        if shift > 0:
            dst[shift:] |= src[:-shift]
        else:
            dst[:shift] |= src[-shift:]
    elif shift > 0:
        dst[:, shift:] |= src[:, :-shift]
    else:
        dst[:, :shift] |= src[:, -shift:]


def island_border(inside: np.ndarray) -> np.ndarray:
    """``(H, W)`` bool: texels inside an island with an 8-neighbour outside every island."""
    inside = np.asarray(inside, bool)
    outside = ~inside
    near = np.zeros_like(inside)
    for dy, dx in _NEIGHBOURS:
        _or_shift_2d(near, outside, dy, dx)
    # The image border counts as outside.
    near[0, :] = near[-1, :] = True
    near[:, 0] = near[:, -1] = True
    return inside & near


def _or_shift_2d(dst: np.ndarray, src: np.ndarray, dy: int, dx: int) -> None:
    """``dst[y, x] |= src[y + dy, x + dx]`` where both are in the image."""
    h, w = src.shape
    ys = slice(max(0, -dy), h - max(0, dy))
    xs = slice(max(0, -dx), w - max(0, dx))
    ys2 = slice(max(0, dy), h - max(0, -dy))
    xs2 = slice(max(0, dx), w - max(0, -dx))
    dst[ys, xs] |= src[ys2, xs2]


# --- push-pull fill ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FillResult:
    """What a fill changed."""

    filled: int  # texels written
    unfilled: int  # texels asked for that had no known texel to read from
    levels: int  # pyramid levels used
    box: tuple[int, int, int, int]  # (y0, y1, x0, x1) of the region worked on


def push_pull_fill(
    pixels: np.ndarray,
    known: np.ndarray,
    region: np.ndarray,
    *,
    context: int | None = None,
) -> FillResult:
    """Fill ``pixels`` in place where ``region`` is True, from texels where ``known`` is.

    Every channel is filled. Texels outside ``region`` are never written, and
    only texels in ``known`` are read. The work is limited to the bounding box
    of ``region`` grown by ``context`` texels (default: the box's larger side,
    at least 32), which keeps small fills in a large image fast; the fill sees
    known texels in that box only.
    """
    pixels = _image(pixels)
    known = np.asarray(known, bool)
    region = np.asarray(region, bool) & ~known
    h, w = region.shape
    if pixels.shape[:2] != (h, w) or known.shape != (h, w):
        raise ValueError("pixels, known and region must have the same height and width")
    rows = np.flatnonzero(region.any(axis=1))
    if not rows.size:
        return FillResult(0, 0, 0, (0, 0, 0, 0))
    cols = np.flatnonzero(region.any(axis=0))
    y0, y1, x0, x1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    if context is None:
        context = max(32, y1 - y0, x1 - x0)
    y0, y1 = max(0, y0 - context), min(h, y1 + context)
    x0, x1 = max(0, x0 - context), min(w, x1 + context)
    box = (y0, y1, x0, x1)

    crop = pixels[y0:y1, x0:x1]
    have = known[y0:y1, x0:x1]
    want = region[y0:y1, x0:x1]
    if not have.any():
        return FillResult(0, int(np.count_nonzero(want)), 0, box)
    ry, rx = np.nonzero(want)
    values, ok, levels = _push_pull(crop, have, want, ry, rx)
    crop[ry[ok], rx[ok]] = values[ok]
    unfilled = int(ry.size - np.count_nonzero(ok))
    return FillResult(int(np.count_nonzero(ok)), unfilled, levels, box)


def _push_pull(
    pixels: np.ndarray, known: np.ndarray, want: np.ndarray, ry: np.ndarray, rx: np.ndarray
) -> tuple[np.ndarray, np.ndarray, int]:
    """Push-pull values at the wanted texels ``(ry, rx)``, from the known texels.

    Returns ``(values, ok, levels)``: ``(n, C)`` values, whether each texel got
    one, and the pyramid levels used.

    Pull: level 0 is read one channel at a time (never copied), and coarser
    levels are pulled only until every texel a wanted texel depends on has a
    value. With weights clamped to 1, every level's weight is 0 or 1, so a
    level's colour is the plain mean of the known texels under it.

    Push: only the texels some wanted texel depends on are computed, by
    normalised bilinear upsampling of the next coarser level (texels without a
    value are left out of the blend, so the background never darkens a fill).
    The cost of the push is proportional to the holes, not the image.
    """
    channels = pixels.shape[2]
    weight0 = known.astype(np.float32)
    wsum = _block_sum(weight0)
    csum = np.empty(wsum.shape + (channels,), np.float32)
    for c in range(channels):
        csum[..., c] = _block_sum(pixels[..., c], weight0)
    del weight0
    colour, weight = _clamp(csum, wsum)
    need = _grow1(_block_any(want))
    colours, weights, needs = [colour], [weight], [need]
    while (need & (weights[-1] <= 0)).any() and max(weights[-1].shape) > 1:
        lack = need & (weights[-1] <= 0)
        colour, weight = _clamp(_block_sum(colours[-1]), _block_sum(weights[-1]))
        need = _grow1(_block_any(lack))
        colours.append(colour)
        weights.append(weight)
        needs.append(need)
    # Push: fill the needed texels that have no value, coarse to fine; they
    # become known (weight 1) for the level below.
    for level in range(len(colours) - 2, -1, -1):
        wt = weights[level]
        ly, lx = np.nonzero(needs[level] & (wt <= 0))
        if not ly.size:
            continue
        values, ok = _bilinear_at(colours[level + 1], weights[level + 1] > 0, ly, lx)
        colours[level][ly[ok], lx[ok]] = values[ok]
        wt[ly[ok], lx[ok]] = 1.0
    # Level 0: the wanted texels have no weight, so they take the upsampled level 1.
    values, ok = _bilinear_at(colours[0], weights[0] > 0, ry, rx)
    return values, ok, len(colours) + 1


def _even(a: np.ndarray) -> np.ndarray:
    """``a`` zero padded to even sizes on its first two axes."""
    h, w = a.shape[:2]
    if h % 2 or w % 2:
        return np.pad(a, [(0, h % 2), (0, w % 2)] + [(0, 0)] * (a.ndim - 2))
    return a


def _block_sum(a: np.ndarray, weight: np.ndarray | None = None) -> np.ndarray:
    """2x2 block sums of the first two axes (of ``a * weight`` if given), zero padded.

    Four strided adds: faster than a reshaped ``sum`` and, with ``weight``, no
    full-size product is ever made.
    """
    a = _even(a)
    if weight is None:
        out = a[0::2, 0::2].copy()
        out += a[1::2, 0::2]
        out += a[0::2, 1::2]
        out += a[1::2, 1::2]
        return out
    weight = _even(weight)
    out = a[0::2, 0::2] * weight[0::2, 0::2]
    for dy, dx in ((1, 0), (0, 1), (1, 1)):
        out += a[dy::2, dx::2] * weight[dy::2, dx::2]
    return out


def _block_any(a: np.ndarray) -> np.ndarray:
    a = _even(a)
    out = a[0::2, 0::2] | a[1::2, 0::2]
    out |= a[0::2, 1::2]
    out |= a[1::2, 1::2]
    return out


def _grow1(mask: np.ndarray) -> np.ndarray:
    """``mask`` grown by one texel (8-neighbourhood): a bilinear footprint."""
    out = mask.copy()
    for dy, dx in _NEIGHBOURS:
        _or_shift_2d(out, mask, dy, dx)
    return out


def _clamp(csum: np.ndarray, wsum: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pull step: colour mean premultiplied by the weight clamped to 1."""
    clamped = np.minimum(wsum, 1.0)
    has = wsum > 0
    scale = np.zeros_like(wsum)
    scale[has] = clamped[has] / wsum[has]
    csum *= scale[..., None]
    return csum, clamped


def _bilinear_at(
    value: np.ndarray, has: np.ndarray, ry: np.ndarray, rx: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Normalised bilinear upsampling of level 1 at level-0 texels ``(ry, rx)``."""
    h, w = has.shape
    y = ry / 2.0 - 0.25
    x = rx / 2.0 - 0.25
    y0 = np.floor(y).astype(np.int64)
    x0 = np.floor(x).astype(np.int64)
    fy = (y - y0).astype(np.float32)
    fx = (x - x0).astype(np.float32)
    total = np.zeros((ry.size, value.shape[2]), np.float32)
    norm = np.zeros(ry.size, np.float32)
    for dy, wy in ((0, 1 - fy), (1, fy)):
        yy = np.clip(y0 + dy, 0, h - 1)
        for dx, wx in ((0, 1 - fx), (1, fx)):
            xx = np.clip(x0 + dx, 0, w - 1)
            wgt = wy * wx * has[yy, xx]
            total += value[yy, xx] * wgt[:, None]
            norm += wgt
    ok = norm > 0
    total[ok] /= norm[ok][:, None]
    return total, ok


def fill_holes(
    pixels: np.ndarray, holes: np.ndarray, inside: np.ndarray | None = None
) -> FillResult:
    """Fill ``holes`` inside UV islands from the island texels around them, in place.

    Reads only texels inside islands that are not holes, writes only holes
    inside islands. Without ``inside`` the whole image counts as one island.
    """
    holes = np.asarray(holes, bool)
    if inside is None:
        inside = np.ones(holes.shape, bool)
    inside = np.asarray(inside, bool)
    return push_pull_fill(pixels, inside & ~holes, holes & inside)


# --- island padding ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PadResult:
    padded: int  # background texels written by the rings
    rings: int  # rings grown (at most the margin)
    background: int  # background texels written by the push-pull fill of the rest


def pad_islands(
    pixels: np.ndarray, inside: np.ndarray, margin: int, *, fill_rest: bool = False
) -> PadResult:
    """Pad UV islands outwards by ``margin`` texels, in place; never writes inside them.

    Each ring of background texels copies the colour of its nearest island
    texel (chessboard distance; on a tie, the first neighbour in the order
    left, right, down, up, then the diagonals), so the padding continues the
    nearest island and two islands' paddings meet halfway. Only source indices
    move from ring to ring; the colours are gathered once at the end.
    ``fill_rest`` then fills every texel still outside with a push-pull fill
    from the islands and their padding, so coarse mip levels average no
    background at all.
    """
    pixels = _image(pixels)
    inside = np.asarray(inside, bool)
    h, w = inside.shape
    if pixels.shape[:2] != (h, w):
        raise ValueError("pixels and inside must have the same height and width")
    if not inside.any():
        return PadResult(0, 0, 0)
    # A grid with a 1-texel border, so neighbour indices need no bounds checks:
    # the border is "taken" (never a target) but never a source.
    wp = w + 2
    taken = np.ones((h + 2) * wp, bool)
    filled = np.zeros((h + 2) * wp, bool)
    interior = np.zeros((h + 2, wp), bool)
    interior[1:-1, 1:-1] = True
    interior = interior.ravel()
    taken[interior] = inside.ravel()
    filled[interior] = inside.ravel()
    source = np.full((h + 2) * wp, -1, np.int64)  # padded index -> image index of its colour
    source[interior] = np.arange(h * w)
    border = np.zeros((h + 2, wp), bool)
    border[1:-1, 1:-1] = island_border(inside)
    ring = np.flatnonzero(border.ravel())
    del border
    offsets = np.array([dy * wp + dx for dy, dx in _PAD_ORDER], np.int64)
    scratch = np.zeros((h + 2) * wp, bool)
    written: list[tuple[np.ndarray, np.ndarray]] = []
    padded = rings = 0
    for _ in range(max(0, margin)):
        for off in offsets:
            q = ring + off
            scratch[q[~taken[q]]] = True
        targets = np.flatnonzero(scratch)
        if not targets.size:
            break
        scratch[targets] = False
        chosen = np.full(targets.size, -1, np.int64)
        for off in offsets:
            q = targets + off
            pick = (chosen < 0) & filled[q]
            chosen[pick] = source[q[pick]]
        source[targets] = chosen
        filled[targets] = True
        taken[targets] = True
        written.append((targets, chosen))
        ring = targets
        padded += targets.size
        rings += 1
    if written:
        dst = np.concatenate([t for t, _ in written])
        src = np.concatenate([c for _, c in written])
        dst = (dst // wp - 1) * w + (dst % wp - 1)  # back to image indices
        flat = pixels.reshape(h * w, -1)
        flat[dst] = flat[src]
    background = 0
    if fill_rest and padded + int(inside.sum()) < h * w:
        known = filled.reshape(h + 2, wp)[1:-1, 1:-1]
        result = push_pull_fill(pixels, known, ~known, context=max(h, w))
        background = result.filled
    return PadResult(padded, rings, background)


# Neighbour order of the padding's tie-break: edge neighbours first.
_PAD_ORDER = ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (-1, 1), (1, -1), (1, 1))


# --- page balance --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BalanceFit:
    """Per-page gain and offset that make seam colours agree: ``c' = gain * c + offset``."""

    gain: np.ndarray  # (pages, channels)
    offset: np.ndarray  # (pages, channels)
    reference: int
    samples: np.ndarray  # (pages,) seam samples that touch each page
    rms_before: float  # RMS colour difference across seams, before
    rms_after: float  # and after the correction
    connected: np.ndarray  # (pages,) bool: linked to the reference through seams

    def apply(self, page: int, pixels: np.ndarray, channels: int = 3) -> None:
        """Correct ``pixels`` of ``page`` in place (the first ``channels`` channels)."""
        apply_gain_offset(pixels, self.gain[page], self.offset[page], channels)


def bilinear_sample(pixels: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Sample ``pixels`` at UV coordinates ``(N, 2)`` bilinearly, clamped at the edges."""
    pixels = _image(pixels)
    h, w = pixels.shape[:2]
    uv = np.asarray(uv, np.float64)
    x = np.clip(uv[:, 0] * w - 0.5, 0.0, w - 1.0)
    y = np.clip(uv[:, 1] * h - 0.5, 0.0, h - 1.0)
    x0 = np.minimum(np.floor(x).astype(np.int64), w - 1)
    y0 = np.minimum(np.floor(y).astype(np.int64), h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (x - x0)[:, None]
    fy = (y - y0)[:, None]
    top = pixels[y0, x0] * (1 - fx) + pixels[y0, x1] * fx
    bottom = pixels[y1, x0] * (1 - fx) + pixels[y1, x1] * fx
    return (top * (1 - fy) + bottom * fy).astype(np.float64)


def fit_balance(
    page_a: np.ndarray,
    page_b: np.ndarray,
    colour_a: np.ndarray,
    colour_b: np.ndarray,
    pages: int,
    *,
    reference: int = 0,
    regularisation: float = 1e-3,
    iterations: int = 4,
) -> BalanceFit:
    """Fit ``gain`` and ``offset`` per page and channel from seam colour pairs.

    Pair ``k`` says texel colour ``colour_a[k]`` of page ``page_a[k]`` and
    ``colour_b[k]`` of page ``page_b[k]`` show the same surface point. The fit
    minimises ``sum_k (g_a c_a + o_a - g_b c_b - o_b)^2`` with the reference
    page held at gain 1, offset 0, plus ``regularisation`` times
    ``(g - 1)^2 + o^2`` per page, which keeps a page with no seam path to the
    reference at the identity. Pairs are re-weighted (Huber, a few iterations)
    so seams that do not really match (occlusion, a wrong neighbour) count
    less. Each channel is fitted on its own.
    """
    page_a = np.asarray(page_a, np.int64)
    page_b = np.asarray(page_b, np.int64)
    ca = np.asarray(colour_a, np.float64)
    cb = np.asarray(colour_b, np.float64)
    if ca.ndim == 1:
        ca, cb = ca[:, None], cb[:, None]
    if not 0 <= reference < pages:
        raise ValueError(f"reference page {reference} is not one of {pages} pages")
    keep = page_a != page_b
    page_a, page_b, ca, cb = page_a[keep], page_b[keep], ca[keep], cb[keep]
    k, channels = ca.shape
    samples = np.bincount(page_a, minlength=pages) + np.bincount(page_b, minlength=pages)
    connected = _connected(page_a, page_b, pages, reference)

    free = np.array([p for p in range(pages) if p != reference], np.int64)  # pages, not texels
    column = np.full(pages, -1, np.int64)
    column[free] = np.arange(free.size)
    n = 2 * free.size
    gain = np.ones((pages, channels))
    offset = np.zeros((pages, channels))
    rows = np.arange(k)
    for ch in range(channels):
        if n == 0 or k == 0:
            break
        a = np.zeros((k, n))
        rhs = np.zeros(k)
        # Residual r = g_a ca + o_a - g_b cb - o_b; the reference's terms are constants.
        for page, colour, sign in ((page_a, ca[:, ch], 1.0), (page_b, cb[:, ch], -1.0)):
            col = column[page]
            var = col >= 0
            a[rows[var], 2 * col[var]] = sign * colour[var]
            a[rows[var], 2 * col[var] + 1] = sign
            rhs[~var] -= sign * colour[~var]
        reg_a = np.sqrt(regularisation) * np.eye(n)
        reg_rhs = np.sqrt(regularisation) * np.tile([1.0, 0.0], free.size)
        weights = np.ones(k)
        x = np.tile([1.0, 0.0], free.size)
        for _ in range(max(1, iterations)):
            sw = np.sqrt(weights)[:, None]
            x = np.linalg.lstsq(
                np.vstack([a * sw, reg_a]), np.concatenate([rhs * sw[:, 0], reg_rhs]), rcond=None
            )[0]
            residual = a @ x - rhs
            scale = 1.4826 * float(np.median(np.abs(residual))) + 1e-12
            limit = 2.5 * scale
            weights = np.minimum(1.0, limit / np.maximum(np.abs(residual), 1e-300))
        gain[free, ch] = x[0::2]
        offset[free, ch] = x[1::2]
    before = ca - cb
    after = (gain[page_a] * ca + offset[page_a]) - (gain[page_b] * cb + offset[page_b])
    return BalanceFit(
        gain=gain,
        offset=offset,
        reference=reference,
        samples=samples,
        rms_before=_rms(before),
        rms_after=_rms(after),
        connected=connected,
    )


def apply_gain_offset(
    pixels: np.ndarray, gain: np.ndarray, offset: np.ndarray, channels: int = 3
) -> None:
    """``pixels[..., c] = gain[c] * pixels[..., c] + offset[c]`` for the first channels, in place."""
    pixels = _image(pixels)
    channels = min(channels, pixels.shape[2], len(gain))
    flat = pixels.reshape(-1, pixels.shape[2])
    for c in range(channels):
        column = flat[:, c]
        column *= np.float32(gain[c])
        column += np.float32(offset[c])


def _connected(page_a: np.ndarray, page_b: np.ndarray, pages: int, reference: int) -> np.ndarray:
    """Pages linked to the reference by seam pairs (label propagation over page pairs)."""
    label = np.arange(pages)
    if page_a.size:
        edges = np.unique(np.stack([page_a, page_b], axis=1), axis=0)
        for _ in range(pages):
            low = np.minimum(label[edges[:, 0]], label[edges[:, 1]])
            new = label.copy()
            np.minimum.at(new, edges[:, 0], low)
            np.minimum.at(new, edges[:, 1], low)
            if np.array_equal(new, label):
                break
            label = new
    return label == label[reference]


def _rms(diff: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(diff)))) if diff.size else 0.0


# --- colour encoding -----------------------------------------------------------------------


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    """sRGB-encoded values to linear light (float, in place where possible)."""
    c = np.asarray(c)
    return np.where(c <= 0.04045, c / 12.92, np.power((np.maximum(c, 0) + 0.055) / 1.055, 2.4))


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(c), 0.0, None)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1.0 / 2.4) - 0.055)


def _image(pixels: np.ndarray) -> np.ndarray:
    if pixels.ndim == 2:
        return pixels[..., None]
    if pixels.ndim != 3:
        raise ValueError(f"pixels must be (H, W) or (H, W, C), got {pixels.shape}")
    return pixels
