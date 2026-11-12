"""The uv-overlap check: texel ownership rasterised with rusty-tiles' rule."""

from __future__ import annotations

import math
from fractions import Fraction

import numpy as np
import pytest

from geotools.core import uv_overlap as uo
from geotools.core.issues import Severity


def grid(x: float, y: float) -> list[float]:
    return [x / 64.0, y / 64.0]


def clean_layout() -> list[list[list[float]]]:
    """rusty-tiles' clean control: an edge-sharing strip, a fan and a degenerate sliver."""
    tris = []
    for i in range(8):
        x = float(i)
        tris.append([grid(x, 0), grid(x + 1, 0), grid(x, 1)])
        tris.append([grid(x + 1, 0), grid(x + 1, 1), grid(x, 1)])
    for k in range(6):
        a = math.tau * k / 6
        b = math.tau * (k + 1) / 6
        tris.append(
            [
                [0.5, 0.5],
                [0.5 + 0.1 * math.cos(a), 0.5 + 0.1 * math.sin(a)],
                [0.5 + 0.1 * math.cos(b), 0.5 + 0.1 * math.sin(b)],
            ]
        )
    tris.append([grid(16, 16), grid(32, 32), grid(48, 48)])  # zero area, inside the fan
    return tris


def find(tris, **kwargs) -> uo.OverlapResult:
    return uo.find_overlaps(np.asarray(tris, np.float32), **kwargs)


# --- the ownership rule ----------------------------------------------------------------


@pytest.mark.parametrize("resolution", [64, 128, 256, 1000, 1024])
def test_shared_edges_and_corners_never_overlap(resolution: int) -> None:
    # At 128 the strip's diagonals run through texel centres: the tie rule decides.
    result = find(clean_layout(), resolution=resolution)
    assert result.ok, result
    assert result.overlapping_texels == 0
    assert result.triangles == len(clean_layout())
    assert result.examples == ()


def test_quad_grid_through_centres_owns_each_texel_once() -> None:
    # Vertices on texel centres, so every edge passes through centres.
    n = 8
    tris = []
    for j in range(n):
        for i in range(n):
            p = [[(i + di + 0.5) / 16, (j + dj + 0.5) / 16] for di, dj in ((0, 0), (1, 0), (1, 1))]
            q = [[(i + di + 0.5) / 16, (j + dj + 0.5) / 16] for di, dj in ((0, 0), (1, 1), (0, 1))]
            tris += [p, q[::-1] if (i + j) % 2 else q]  # mixed winding
    assert find(tris, resolution=16).ok
    assert find(tris, resolution=16, wrap="CLAMP").ok


def test_duplicate_mirrored_and_nested_triangles_overlap() -> None:
    clean = clean_layout()
    first = clean[0]
    result = find([*clean, first[::-1]], resolution=1024)  # a reversed (mirrored) copy
    assert result.overlapping_triangles == 2
    assert result.examples == (0, len(clean))
    nested = [grid(0.2, 0.2), grid(0.4, 0.2), grid(0.2, 0.4)]
    result = find([first, nested], resolution=4096)
    assert result.overlapping_triangles == 2
    assert result.overlapping_texels > 0


def test_known_texel_count() -> None:
    # Two squares over [0, 0.5]^2 at 8x8: 16 texels owned twice; a third square elsewhere.
    def square(x0, y0, s):
        return [
            [[x0, y0], [x0 + s, y0], [x0 + s, y0 + s]],
            [[x0, y0], [x0 + s, y0 + s], [x0, y0 + s]],
        ]

    tris = square(0, 0, 0.5) + square(0, 0, 0.5) + square(0.5, 0.5, 0.5)
    result = find(tris, resolution=8)
    assert result.overlapping_texels == 16
    assert result.overlapping_triangles == 4
    assert result.examples == (0, 1, 2, 3)
    # Half overlapping: a square shifted by two texels shares 2 x 4 texels.
    result = find(square(0, 0, 0.5) + square(0.25, 0, 0.5), resolution=8)
    assert result.overlapping_texels == 8


def test_overlap_thinner_than_a_texel_needs_a_finer_grid() -> None:
    a = [[0.0, 0.0], [0.5, 0.0], [0.0, 0.5]]
    sliver = [[0.5 - 1 / 512, 0.0], [0.6, 0.0], [0.5 - 1 / 512, 0.1]]
    assert find([a, sliver], resolution=16).ok  # misses it (rusty-tiles would not)
    assert not find([a, sliver], resolution=4096).ok


def test_out_of_range_and_non_finite_are_shared_conservatively() -> None:
    clean = clean_layout()
    far = [[9.0, 9.0], [10.0, 9.0], [9.0, 10.0]]
    nan = [[math.nan, 0.0], [0.1, 0.0], [0.0, 0.1]]
    result = find([far, nan, clean[0]], resolution=64, wrap="CLAMP")
    assert result.out_of_range == 2
    assert result.overlapping_triangles == 2
    assert result.examples == (0, 1)
    assert result.overlapping_texels == 0


# --- wrap and periods ------------------------------------------------------------------


def test_repeat_folds_whole_periods() -> None:
    a = [[0.1, 0.1], [0.3, 0.1], [0.1, 0.3]]
    shifted = [[u + 1.0, v - 2.0] for u, v in a]
    assert not find([a, shifted], resolution=64, wrap="REPEAT").ok
    assert find([a, shifted], resolution=64, wrap="CLAMP").ok
    # Only one axis repeats: the V shift keeps them apart.
    assert find([a, shifted], resolution=64, wrap=("REPEAT", "CLAMP")).ok
    # A triangle across the period seam reads texels at both edges.
    seam = [[0.9, 0.5], [1.1, 0.5], [0.9, 0.7]]
    left = [[0.0, 0.5], [0.05, 0.5], [0.0, 0.55]]
    assert not find([seam, left], resolution=64).ok
    assert find([seam, left], resolution=64, wrap="CLAMP").ok


def test_mirrored_repeat_shares_with_its_own_mirror_image() -> None:
    across = [[0.9, 0.2], [1.1, 0.2], [1.0, 0.4]]  # crosses the mirror line u = 1
    result = find([across], resolution=64, wrap="MIRRORED")
    assert result.overlapping_triangles == 1 and result.overlapping_texels > 0
    assert find([across], resolution=64, wrap="REPEAT").ok
    # Mirrored copies: 2 - u of a triangle in the first period lands on it.
    a = [[0.1, 0.1], [0.3, 0.1], [0.1, 0.3]]
    mirror = [[2.0 - u, v] for u, v in a]
    assert not find([a, mirror], resolution=64, wrap="MIRRORED").ok
    assert find([a, mirror], resolution=64, wrap="REPEAT").ok


def test_wide_footprint_shares_the_whole_page() -> None:
    wide = [[0.0, 0.0], [1.5, 0.0], [0.0, 0.1]]
    others = [[[0.2, 0.5], [0.3, 0.5], [0.2, 0.6]], [[0.6, 0.5], [0.7, 0.5], [0.6, 0.6]]]
    result = find([*others, wide], resolution=32)
    assert result.wide == 1
    assert result.overlapping_triangles == 3  # every triangle, as rusty-tiles counts it
    assert result.examples == (2,)  # the wide one; the others own their texels alone
    assert find([*others, wide], resolution=32, wrap="CLAMP").ok


# --- resolution, inputs and the issue --------------------------------------------------


def test_coarsens_to_the_sample_budget_and_reports_it() -> None:
    a = [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]]
    result = find([a, a], resolution=1024, max_samples=10_000)
    assert result.requested == (1024, 1024)
    assert result.coarsened and result.width == result.height
    assert result.width * result.height <= 10_000
    assert result.width == 64  # halved until the box fits
    assert not result.ok
    full = find([a, a], resolution=(64, 32))
    assert (full.width, full.height) == (64, 32) and not full.coarsened


def test_grid_budget_coarsens_clamp_layouts_far_from_the_unit_square() -> None:
    a = [[-7.0, -7.0], [7.0, -7.0], [-7.0, 7.0]]
    tiny = [[0.0, 0.0], [0.01, 0.0], [0.0, 0.01]]
    result = find([a, tiny], resolution=1024, wrap="CLAMP", max_grid=1 << 16)
    assert result.coarsened
    assert result.width * 14 * result.height * 14 <= 1 << 16


def test_indexed_and_corner_forms_agree() -> None:
    rng = np.random.default_rng(3)
    uv = rng.random((40, 2), dtype=np.float32)
    tris = rng.integers(0, 40, size=(30, 3))
    indexed = uo.find_overlaps(uv, tris, resolution=128)
    corners = uo.find_overlaps(uv[tris], resolution=128)
    assert indexed == corners
    with pytest.raises(ValueError):
        uo.find_overlaps(uv)
    with pytest.raises(ValueError):
        uo.find_overlaps(uv, tris[:, :2])
    with pytest.raises(ValueError):
        uo.find_overlaps(uv[tris], resolution=0)


def test_empty_input() -> None:
    result = uo.find_overlaps(np.zeros((0, 3, 2), np.float32), resolution=16)
    assert result.ok and result.triangles == 0 and result.overlapping_texels == 0


def test_issue_message() -> None:
    a = [[0.0, 0.0], [0.5, 0.0], [0.0, 0.5]]
    ok = find([a], resolution=8)
    assert uo.overlap_issue(ok, "Ground") is None
    result = find([a, a], resolution=1024, max_samples=4096)
    issue = uo.overlap_issue(
        result, "Ground", uv_map="UVMap", examples=[3, 4], example_label="faces"
    )
    assert issue.code == "uv-overlap" and issue.subject == "Ground"
    assert issue.severity is Severity.WARNING
    text = issue.message
    assert "Ground (UV map UVMap)" in text
    assert f"{result.overlapping_texels:,} texels at {result.width}x{result.height}" in text
    assert "coarsened from 1024x1024" in text
    assert "2 of 2 triangles" in text and "faces 3, 4" in text
    error = uo.overlap_issue(result, "Ground", severity=Severity.ERROR)
    assert error.severity is Severity.ERROR and "triangles 0, 1" in error.message
    wide = find([[[0.0, 0.0], [1.5, 0.0], [0.0, 0.1]]], resolution=8)
    assert "more than one UV period" in uo.overlap_issue(wide, "G").message


# --- against independent references ------------------------------------------------------


def _owns(tri, px, py) -> bool:
    """The rule written plainly with exact fractions: centre nudged by (e, e^2) inside."""
    (ax, ay), (bx, by), (cx, cy) = tri
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if area == 0:
        return False
    if area < 0:
        (bx, by), (cx, cy) = (cx, cy), (bx, by)
    for (x1, y1), (x2, y2) in (((ax, ay), (bx, by)), ((bx, by), (cx, cy)), ((cx, cy), (ax, ay))):
        ex, ey = x2 - x1, y2 - y1
        w = ex * (py - y1) - ey * (px - x1)
        nx, ny = -ey, ex
        if w < 0 or (w == 0 and not (nx > 0 or (nx == 0 and ny > 0))):
            return False
    return True


def _brute(tris, w, h, wrap):
    owners: dict[tuple[int, int], int] = {}
    hit = set()
    for t, tri in enumerate(tris):
        pts = [(Fraction(u) * w, Fraction(v) * h) for u, v in tri]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        for i in range(
            math.ceil(min(xs) - Fraction(1, 2)), math.floor(max(xs) - Fraction(1, 2)) + 1
        ):
            for j in range(
                math.ceil(min(ys) - Fraction(1, 2)), math.floor(max(ys) - Fraction(1, 2)) + 1
            ):
                if _owns(pts, i + Fraction(1, 2), j + Fraction(1, 2)):
                    key = (i % w if wrap == "REPEAT" else i, j % h if wrap == "REPEAT" else j)
                    owners[key] = owners.get(key, 0) + 1
                    hit.add((t, key))
    double = {k for k, n in owners.items() if n > 1}
    flagged = {t for t, k in hit if k in double}
    return len(double), flagged


@pytest.mark.parametrize("wrap", ["CLAMP", "REPEAT"])
@pytest.mark.parametrize("seed", range(6))
def test_matches_a_brute_force_raster(wrap: str, seed: int, monkeypatch) -> None:
    rng = np.random.default_rng(seed)
    # Vertices on a 1/16 grid (exact in float32), some outside [0, 1].
    tris = (rng.integers(-4, 21, size=(25, 3, 2)) / 16).astype(np.float32)
    w, h = 24, 16  # not powers of two, and not square
    texels, flagged = _brute(tris.tolist(), w, h, wrap)
    flagged |= (
        {int(i) for i in np.flatnonzero(np.ptp(tris, axis=1).max(axis=1) > 1)}
        if wrap == "REPEAT"
        else set()
    )
    for block, chunk in ((1 << 18, 1 << 20), (7, 5)):  # tiny: slabs, split groups, chunks
        monkeypatch.setattr(uo, "_BLOCK", block)
        monkeypatch.setattr(uo, "_CHUNK", chunk)
        result = uo.find_overlaps(tris, resolution=(w, h), wrap=wrap, max_examples=100)
        assert result.overlapping_texels == texels
        if not result.wide:
            assert set(result.examples) == flagged
            assert result.overlapping_triangles == len(flagged)


def _exact_overlap(a, b) -> bool:
    """rusty-tiles' interiors_overlap: no edge-normal axis weakly separates them."""
    for t in (a, b):
        for i in range(3):
            ex = t[(i + 1) % 3][0] - t[i][0]
            ey = t[(i + 1) % 3][1] - t[i][1]
            ax, ay = -ey, ex
            pa = [ax * p[0] + ay * p[1] for p in a]
            pb = [ax * p[0] + ay * p[1] for p in b]
            if max(pa) <= min(pb) or max(pb) <= min(pa):
                return False
    return True


def _area2(t) -> Fraction:
    (ax, ay), (bx, by), (cx, cy) = t
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


@pytest.mark.parametrize("seed", range(4))
def test_never_flags_what_rusty_tiles_calls_unique(seed: int) -> None:
    rng = np.random.default_rng(100 + seed)
    tris = (rng.integers(0, 17, size=(40, 3, 2)) / 16).astype(np.float32)
    exact = [[(Fraction(float(u)), Fraction(float(v))) for u, v in t] for t in tris]
    shared = set()
    for i in range(len(exact)):
        for j in range(i + 1, len(exact)):
            if _area2(exact[i]) and _area2(exact[j]) and _exact_overlap(exact[i], exact[j]):
                shared |= {i, j}
    for res in (16, 64, 512):
        result = uo.find_overlaps(tris, resolution=res, wrap="CLAMP", max_examples=100)
        assert set(result.examples) <= shared
    # Fine enough, every overlap of these grid triangles covers a centre.
    fine = uo.find_overlaps(tris, resolution=2048, wrap="CLAMP", max_examples=100)
    assert set(fine.examples) == shared


# --- texel coverage (M6) -------------------------------------------------------------------


def _brute_counts(tris, w, h, wrap) -> np.ndarray:
    counts = np.zeros((h, w), np.int64)
    for tri in tris:
        pts = [(Fraction(u) * w, Fraction(v) * h) for u, v in tri]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        for i in range(
            math.ceil(min(xs) - Fraction(1, 2)), math.floor(max(xs) - Fraction(1, 2)) + 1
        ):
            for j in range(
                math.ceil(min(ys) - Fraction(1, 2)), math.floor(max(ys) - Fraction(1, 2)) + 1
            ):
                if _owns(pts, i + Fraction(1, 2), j + Fraction(1, 2)):
                    x, y = (i % w, j % h) if wrap == "REPEAT" else (i, j)
                    if 0 <= x < w and 0 <= y < h:
                        counts[y, x] += 1
    return counts


@pytest.mark.parametrize("wrap", ["CLAMP", "REPEAT"])
@pytest.mark.parametrize("seed", range(4))
def test_coverage_matches_a_brute_force_raster(wrap: str, seed: int, monkeypatch) -> None:
    rng = np.random.default_rng(100 + seed)
    tris = (rng.integers(-4, 21, size=(25, 3, 2)) / 16).astype(np.float32)
    w, h = 24, 16
    expected = _brute_counts(tris.tolist(), w, h, wrap)
    for block, chunk in ((1 << 18, 1 << 20), (7, 5)):
        monkeypatch.setattr(uo, "_BLOCK", block)
        monkeypatch.setattr(uo, "_CHUNK", chunk)
        cov = uo.texel_coverage(tris, resolution=(w, h), wrap=wrap)
        assert not cov.coarsened and cov.count.shape == (h, w)
        np.testing.assert_array_equal(cov.count, np.minimum(expected, 255))
    if wrap == "REPEAT" and not (np.ptp(tris, axis=1).max(axis=1) > 1).any():
        result = uo.find_overlaps(tris, resolution=(w, h), wrap=wrap)
        assert int(cov.overlapping.sum()) == result.overlapping_texels


def test_coverage_of_a_quad_grid_is_one_everywhere_inside() -> None:
    n = 8
    tris = []
    for i in range(n):
        for j in range(n):
            a, b = (
                (0.25 + 0.5 * i / n, 0.25 + 0.5 * j / n),
                (0.25 + 0.5 * (i + 1) / n, 0.25 + 0.5 * j / n),
            )
            c, d = (a[0], 0.25 + 0.5 * (j + 1) / n), (b[0], 0.25 + 0.5 * (j + 1) / n)
            tris += [[a, b, d], [a, d, c]]
    cov = uo.texel_coverage(np.array(tris), resolution=64)
    assert cov.count.max() == 1
    assert cov.count.sum() == 32 * 32  # [0.25, 0.75] of 64 texels: rows and columns 16..47
    assert cov.inside[16:48, 16:48].all() and not cov.inside[:16].any()
    assert not cov.overlapping.any()


def test_coverage_coarsens_and_resamples() -> None:
    tris = np.array([[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]], [[0.0, 0.0], [1.0, 1.0], [0.0, 1.0]]])
    cov = uo.texel_coverage(tris, resolution=(256, 128), max_samples=5000)
    assert cov.coarsened and cov.width < 256 and cov.height < 128
    assert cov.resampled().shape == (128, 256)
    assert cov.inside.all()
    empty = uo.texel_coverage(np.empty((0, 3, 2)), resolution=16)
    assert empty.count.shape == (16, 16) and not empty.count.any()
    assert (
        uo.texel_coverage(np.array([[[9.0, 0], [10, 0], [10, 1]]]), resolution=8).count.sum() == 0
    )
