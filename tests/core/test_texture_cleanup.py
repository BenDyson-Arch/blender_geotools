"""M6 texture cleanup maths: push-pull fill, island padding, masks, the page balance fit.

Every image here is synthetic: gradients, flat colours and noise.
"""

from __future__ import annotations

import numpy as np
import pytest

from geotools.core import texture_cleanup as tc


def _gradient(h: int = 128, w: int = 160, channels: int = 4) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    planes = [x / w, y / h, 0.25 + 0.5 * (x + y) / (w + h), np.ones_like(x)]
    return np.stack(planes[:channels], axis=-1).astype(np.float32)


def _disc(h: int, w: int, cy: float, cx: float, r: float) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w]
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _chessboard_grow(mask: np.ndarray, r: int) -> np.ndarray:
    """Reference dilation: OR of every shift in a (2r+1)^2 square."""
    out = np.zeros_like(mask)
    h, w = mask.shape
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            src = mask[max(0, -dy) : h - max(0, dy), max(0, -dx) : w - max(0, dx)]
            out[max(0, dy) : h - max(0, -dy), max(0, dx) : w - max(0, -dx)] |= src
    return out


# --- masks ---------------------------------------------------------------------------------


def test_hole_mask_black_and_transparent() -> None:
    px = _gradient(8, 8)
    px[1, 1, :3] = 0.0
    px[2, 2, 3] = 0.0
    px[3, 3, :3] = 2.0 / 255.0  # above the default black level
    assert np.flatnonzero(tc.hole_mask(px)).tolist() == [9, 18]
    assert np.flatnonzero(tc.hole_mask(px, transparent=False)).tolist() == [9]
    assert np.flatnonzero(tc.hole_mask(px, black=False)).tolist() == [18]
    assert np.flatnonzero(tc.hole_mask(px, dark_threshold=3 / 255)).tolist() == [9, 18, 27]
    rgb = px[..., :3]
    assert np.flatnonzero(tc.hole_mask(rgb)).tolist() == [9]  # no alpha: black only


@pytest.mark.parametrize("radius", [0, 1, 2, 3, 5, 8])
def test_grow_mask_matches_chessboard_dilation(radius: int) -> None:
    rng = np.random.default_rng(radius)
    mask = rng.random((37, 41)) > 0.985
    mask[0, 0] = mask[-1, -1] = True  # corners
    assert np.array_equal(tc.grow_mask(mask, radius), _chessboard_grow(mask, radius))


def test_island_border() -> None:
    inside = np.zeros((9, 9), bool)
    inside[2:7, 2:7] = True
    border = tc.island_border(inside)
    assert border.sum() == 16 and not border[3:6, 3:6].any()
    full = np.ones((4, 4), bool)  # the image edge counts as outside
    assert tc.island_border(full).sum() == 12


# --- push-pull fill ------------------------------------------------------------------------


def test_fill_recovers_a_smooth_gradient() -> None:
    truth = _gradient()
    px = truth.copy()
    hole = _disc(128, 160, 60, 80, 22)
    px[hole] = 0.0
    result = tc.fill_holes(px, hole)
    assert result.filled == hole.sum() and result.unfilled == 0
    err = np.abs(px[hole] - truth[hole])
    # A diffusion fill is exact for constants and close for linear ramps: within
    # 4% of the range at worst and 1% on average, over a hole 44 texels across
    # (the ramp spans 0.34 across it).
    assert err.max() < 0.04, err.max()
    assert err.mean() < 0.01, err.mean()
    assert np.array_equal(px[~hole], truth[~hole])


def test_fill_is_exact_for_a_flat_colour() -> None:
    px = np.full((64, 64, 4), 0.37, np.float32)
    hole = _disc(64, 64, 20, 40, 9) | _disc(64, 64, 50, 10, 3)
    px[hole] = 0.0
    tc.fill_holes(px, hole)
    np.testing.assert_allclose(px, 0.37, atol=1e-6)


def test_fill_reads_and_writes_inside_islands_only() -> None:
    truth = _gradient(96, 96)
    inside = np.zeros((96, 96), bool)
    inside[10:80, 12:70] = True
    px = truth.copy()
    px[~inside] = 1000.0  # background: must never be read
    hole = _disc(96, 96, 45, 40, 12) | _disc(96, 96, 75, 66, 6)  # the second crosses the border
    px[hole] = 0.0
    before = px.copy()
    result = tc.fill_holes(px, hole, inside)
    want = hole & inside
    assert result.filled == want.sum()
    assert np.array_equal(px[~want], before[~want])  # nothing else written
    assert px[want].max() <= 1.0  # no background colour leaked in
    interior = _disc(96, 96, 45, 40, 12)
    assert np.abs(px[interior] - truth[interior]).max() < 0.04
    # At an island border the fill extrapolates from one side only: no gradient
    # continues, so it is less exact there (stated in the docs).
    border = want & ~interior
    assert np.abs(px[border] - truth[border]).max() < 0.15


def test_fill_without_known_texels_fills_nothing() -> None:
    px = np.zeros((16, 16, 4), np.float32)
    hole = np.zeros((16, 16), bool)
    hole[4:8, 4:8] = True
    result = tc.push_pull_fill(px, np.zeros((16, 16), bool), hole)
    assert result.filled == 0 and result.unfilled == 16
    assert not px.any()


def test_fill_works_on_a_crop_for_small_holes() -> None:
    truth = _gradient(512, 512)
    px = truth.copy()
    hole = _disc(512, 512, 500, 6, 4)  # near a corner
    px[hole] = 0.0
    result = tc.fill_holes(px, hole)
    y0, y1, x0, x1 = result.box
    assert (y1 - y0) * (x1 - x0) < 512 * 512 / 16  # only a crop was worked on
    assert np.abs(px[hole] - truth[hole]).max() < 0.02


def test_fill_single_channel_and_odd_sizes() -> None:
    y, x = np.mgrid[0:33, 0:47].astype(np.float32)
    truth = (x + 2 * y) / 113.0
    px = truth.copy()
    hole = np.zeros(truth.shape, bool)
    hole[10:15, 20:26] = True
    px[hole] = 0.0
    tc.fill_holes(px, hole)
    assert np.abs(px[hole] - truth[hole]).max() < 0.03


# --- island padding ------------------------------------------------------------------------


def test_padding_never_writes_inside_islands() -> None:
    rng = np.random.default_rng(3)
    inside = _chessboard_grow(rng.random((80, 90)) > 0.995, 3)
    px = rng.random((80, 90, 4)).astype(np.float32)
    px[~inside] = 0.0
    before = px.copy()
    result = tc.pad_islands(px, inside, 5)
    assert np.array_equal(px[inside], before[inside])
    reach = _chessboard_grow(inside, 5)
    assert result.padded == int((reach & ~inside).sum())
    assert not px[~reach].any()  # beyond the margin: untouched
    assert (px[reach & ~inside, 3] > 0).all()  # every texel in the margin was written
    # Each padding texel copies an island texel at the nearest chessboard distance.
    ys, xs = np.nonzero(reach & ~inside)
    iy, ix = np.nonzero(inside)
    for y, x in list(zip(ys.tolist(), xs.tolist(), strict=True))[::37]:
        d = np.maximum(np.abs(iy - y), np.abs(ix - x))
        near = d == d.min()
        assert (np.abs(before[iy[near], ix[near]] - px[y, x]).max(axis=1) == 0).any()


def test_padding_continues_the_nearest_island() -> None:
    inside = np.zeros((20, 40), bool)
    inside[5:15, 2:10] = True  # island A, red
    inside[5:15, 27:38] = True  # island B, blue
    px = np.zeros((20, 40, 3), np.float32)
    px[5:15, 2:10] = (1.0, 0.0, 0.0)
    px[5:15, 27:38] = (0.0, 0.0, 1.0)
    tc.pad_islands(px, inside, 20)
    np.testing.assert_array_equal(px[10, 10:18], [(1.0, 0.0, 0.0)] * 8)  # nearer A
    np.testing.assert_array_equal(px[10, 19:27], [(0.0, 0.0, 1.0)] * 8)  # nearer B
    assert tuple(px[10, 18]) in {(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)}  # a tie: either island


def test_padding_fill_rest_and_no_islands() -> None:
    inside = np.zeros((32, 32), bool)
    inside[8:12, 8:12] = True
    px = np.zeros((32, 32, 4), np.float32)
    px[inside] = (0.2, 0.4, 0.6, 1.0)
    before = px.copy()
    result = tc.pad_islands(px, inside, 2, fill_rest=True)
    assert result.rings == 2 and result.background > 0
    np.testing.assert_allclose(px, np.broadcast_to((0.2, 0.4, 0.6, 1.0), px.shape), atol=1e-6)
    assert np.array_equal(px[inside], before[inside])
    empty = np.zeros((8, 8, 4), np.float32)
    assert tc.pad_islands(empty, np.zeros((8, 8), bool), 4) == tc.PadResult(0, 0, 0)


# --- balance -------------------------------------------------------------------------------


def _pairs(rng, k: int, pages: int):
    a = rng.integers(0, pages, k)
    b = (a + rng.integers(1, pages, k)) % pages
    return a, b


def test_fit_recovers_gain_and_offset() -> None:
    rng = np.random.default_rng(7)
    pages = 4
    true = rng.random((6000, 3))
    a, b = _pairs(rng, true.shape[0], pages)
    gain = np.array([[1, 1, 1], [1.2, 0.9, 1.1], [0.8, 1.0, 1.3], [1.05, 1.15, 0.95]])
    offset = np.array([[0, 0, 0], [0.05, -0.02, 0.0], [-0.03, 0.04, 0.01], [0.0, 0.02, -0.04]])
    ca = gain[a] * true + offset[a]
    cb = gain[b] * true + offset[b]
    fit = tc.fit_balance(a, b, ca, cb, pages, reference=0, regularisation=1e-9)
    # c_true = (c - O) / G, so the correction is gain 1/G and offset -O/G.
    np.testing.assert_allclose(fit.gain, 1 / gain, atol=1e-6)
    np.testing.assert_allclose(fit.offset, -offset / gain, atol=1e-6)
    assert fit.rms_after < 1e-6 < fit.rms_before
    assert fit.connected.all() and fit.samples.sum() == 2 * true.shape[0]


def test_fit_is_robust_to_mismatched_seams() -> None:
    rng = np.random.default_rng(11)
    true = rng.random((4000, 3))
    a = np.zeros(4000, np.int64)
    b = np.ones(4000, np.int64)
    ca = true.copy()
    cb = 0.8 * true + 0.05
    bad = rng.random(4000) < 0.1  # occluded or wrong neighbours
    cb[bad] = rng.random((bad.sum(), 3))
    fit = tc.fit_balance(a, b, ca, cb, 2, reference=0)
    np.testing.assert_allclose(fit.gain[1], 1 / 0.8, atol=0.02)
    np.testing.assert_allclose(fit.offset[1], -0.05 / 0.8, atol=0.01)


def test_fit_keeps_unlinked_pages_at_identity() -> None:
    rng = np.random.default_rng(5)
    true = rng.random((500, 3))
    a = np.zeros(500, np.int64)
    b = np.ones(500, np.int64)
    fit = tc.fit_balance(a, b, true, 1.1 * true, 3, reference=1)
    assert fit.reference == 1
    np.testing.assert_allclose(fit.gain[1], 1.0)
    np.testing.assert_allclose(fit.gain[0], 1.1, atol=1e-3)
    np.testing.assert_allclose(fit.gain[2], 1.0, atol=1e-9)  # no seam to it
    np.testing.assert_allclose(fit.offset[2], 0.0, atol=1e-9)
    assert fit.connected.tolist() == [True, True, False]
    with pytest.raises(ValueError, match="reference page"):
        tc.fit_balance(a, b, true, true, 2, reference=2)


def test_fit_ignores_same_page_pairs() -> None:
    a = np.array([0, 1, 1])
    b = np.array([0, 1, 0])
    c = np.array([[0.5, 0.5, 0.5]] * 3)
    fit = tc.fit_balance(a, b, c, c, 2)
    assert fit.samples.tolist() == [1, 1]


def test_apply_gain_offset_and_bilinear_sample() -> None:
    px = np.full((4, 4, 4), 0.5, np.float32)
    tc.apply_gain_offset(px, np.array([2.0, 1.0, 0.5]), np.array([0.1, 0.0, -0.1]))
    np.testing.assert_allclose(px[0, 0], [1.1, 0.5, 0.15, 0.5], atol=1e-6)
    ramp = _gradient(64, 64, 3)
    uv = np.array([[0.25, 0.5], [0.5 + 0.5 / 64, 0.75 + 0.5 / 64]])
    got = tc.bilinear_sample(ramp, uv)
    np.testing.assert_allclose(got[:, 0], [0.25 - 0.5 / 64, 0.5], atol=1e-6)
    np.testing.assert_allclose(got[:, 1], [0.5 - 0.5 / 64, 0.75], atol=1e-6)


def test_srgb_round_trip() -> None:
    values = np.linspace(0, 1, 101, dtype=np.float32)
    np.testing.assert_allclose(tc.linear_to_srgb(tc.srgb_to_linear(values)), values, atol=1e-6)
    assert tc.srgb_to_linear(np.float32(0.5)) == pytest.approx(0.21404, abs=1e-5)
