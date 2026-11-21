"""Per-face surface metrics at stated scales: roughness, its resolution status and geometry quality.

bpy-free, numpy only, on :mod:`.neighbourhood`. For each scale s (a ball of
diameter s around a point on the surface):

* **Roughness** at s: the RMS distance of the surface in the ball from its
  area-weighted least-squares plane (:func:`.neighbourhood.plane`), over
  surface area. It includes any curvature smaller than s, so it grows with s
  on curved rock; compare scales, not absolute values across scales.
* **Status**: the resolution rule (:func:`.neighbourhood.resolution`). Roughness is
  NaN wherever the status is not 0: a value counts only where the mesh is fine
  enough for the scale, fills the ball and is not at a hole or the end of the
  scan.
* **Normal** at the scale: the plane's normal, for slope, aspect and overhang
  measured at s rather than from single faces.
* **Quality**: the area-weighted median edge length and the coverage in each
  ball, the distance to the nearest open edge, and the finest resolved scale
  per face, the single number that says what the geometry can support there.

**Where the values are computed.** Not at every face: at one query point per
cube of side ``s × QUERY_SPACING`` (:func:`.neighbourhood.thin`; s/4 by
default), with every face taking the values of the nearest query point. Fine
scales therefore cost about as much as coarse ones, and a face's value is at
most ``s/4 × √3`` from where it was computed (a quarter of the ball's
diameter, well inside it). Where faces are larger than the cube, every face is
a query point.

**The normal filter** uses each query face's own normal: samples turned more
than 90° from it are left out of its ball (:mod:`.neighbourhood`), so the far
side of a thin lip is not averaged in. A face normal is noisy on a fine mesh,
but only a turn of more than 90° drops a sample.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from . import neighbourhood as nb

SCALES = (0.01, 0.03, 0.10)
"""Default scales s in metres (ball diameters): 1, 3 and 10 cm."""
QUERY_SPACING = 0.25
"""Query points are one per cube of side this × s."""


@dataclass(frozen=True)
class Scale:
    """One scale's values, per face (:func:`surface_metrics`)."""

    scale: float
    """s in metres, the ball's diameter."""
    rms: np.ndarray
    """``(F,)`` float32 roughness (RMS) in metres; NaN unless resolved."""
    status: np.ndarray
    """``(F,)`` uint8 resolution status (:data:`.neighbourhood.RESOLVED` = 0)."""
    normal: np.ndarray
    """``(F, 3)`` float32 normal of the plane at this scale; NaN where the ball is empty."""
    size: np.ndarray
    """``(F,)`` float32 area-weighted median edge length in the ball, metres."""
    coverage: np.ndarray
    """``(F,)`` float32 ``|Σ a n| / π r²``."""
    queries: int
    """How many query points the scale was computed at."""


@dataclass(frozen=True)
class Metrics:
    """Surface metrics at several scales, per face."""

    scales: tuple[Scale, ...]
    edge_distance: np.ndarray
    """``(F,)`` float32 distance to the nearest open edge, metres; inf beyond the
    largest distance any scale tests (``EDGE_FRACTION × max(s)``)."""

    @property
    def finest(self) -> np.ndarray:
        """``(F,)`` float32 the finest scale resolved at each face; NaN where none is."""
        out = np.full(len(self.edge_distance), np.nan, dtype=np.float32)
        for sc in sorted(self.scales, key=lambda x: -x.scale):
            out[sc.status == nb.RESOLVED] = sc.scale
        return out


def surface_metrics(
    positions: np.ndarray,
    face_starts: np.ndarray,
    corner_verts: np.ndarray,
    scales: tuple[float, ...] = SCALES,
    *,
    samples: nb.Samples | None = None,
    query_spacing: float = QUERY_SPACING,
    min_cos: float = 0.0,
    spacing_factor: float = nb.SPACING_FACTOR,
    min_coverage: float = nb.MIN_COVERAGE,
    max_pairs: int = nb.MAX_PAIRS,
) -> Metrics:
    """Roughness, the resolution status and the scale's plane normal at each of
    ``scales``, and the geometry quality, for every face of a mesh
    (``positions``, ``face_starts``, ``corner_verts``: Blender's layout)."""
    if not scales or min(scales) <= 0:
        raise ValueError("Scales must be positive.")
    if samples is None:
        samples = nb.face_samples(positions, face_starts, corner_verts)
    n = len(samples)
    reach = nb.EDGE_FRACTION * max(scales)
    edge = nb.edge_distance(
        positions,
        face_starts,
        corner_verts,
        samples.points,
        reach,
        resolution=nb.EDGE_FRACTION * min(scales) / 8,
    ).astype(np.float32)
    out = []
    for s in sorted(scales):
        r = s / 2
        step = query_spacing * s
        qid = nb.thin(samples.points, step)
        q = samples.points[qid]
        g = nb.grid(samples.points, r)
        ball = nb.ball_stats(
            samples,
            q,
            r,
            g=g,
            query_normals=samples.normal[qid],
            min_cos=min_cos,
            max_pairs=max_pairs,
        )
        plane = nb.plane(ball, q)
        status = nb.resolution(
            ball,
            edge=edge[qid] < nb.EDGE_FRACTION * s,
            spacing_factor=spacing_factor,
            min_coverage=min_coverage,
        )
        if len(qid) == n:
            take = np.arange(n, dtype=np.int64)
        else:
            take, _ = nb.nearest(q, samples.points, step * math.sqrt(3) * (1 + 1e-9))
        missing = take < 0  # cannot happen: every face's cube holds a query point
        take = np.maximum(take, 0)
        st = status[take]
        st[missing] = nb.EMPTY
        rms = np.where(st == nb.RESOLVED, plane.rms[take], np.nan).astype(np.float32)
        out.append(
            Scale(
                scale=float(s),
                rms=rms,
                status=st,
                normal=plane.normal[take].astype(np.float32),
                size=ball.size[take].astype(np.float32),
                coverage=ball.coverage[take].astype(np.float32),
                queries=len(qid),
            )
        )
    return Metrics(scales=tuple(out), edge_distance=edge)
