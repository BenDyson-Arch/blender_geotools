"""Synthetic surfaces for the terrain validation (docs/validation/terrain.md).

numpy only, so the same definitions serve the Blender suite
(``test_validation_terrain.py``) and the reference scripts under
``scripts/validation/terrain/``, which run GDAL, WhiteboxTools and RVT on the
same DEMs. Nothing here comes from real survey data.

Grid convention: a DEM of ``n x n`` cells at spacing ``h`` centred on the
origin. Row 0 is the north row (largest y), column 0 the west column, as in a
north-up raster; the cell centre (row i, column j) is at
``x = -L + j h, y = L - i h`` with ``L = (n - 1) h / 2``. The mesh made from a
DEM has one vertex per cell centre (row-major, so vertex ``i n + j`` is cell
(i, j)) and two triangles per square of four cell centres, split along the
south-west to north-east diagonal.
"""

from __future__ import annotations

import itertools
import math

import numpy as np

# --- the cross-implementation DEM ----------------------------------------------------------------

H = 0.1
"""Cell size of the cross-implementation DEM, metres."""
N = 141
"""Cells per side of the cross-implementation DEM (14 m square)."""

WAVES = (
    # amplitude (m), wavelength (m), direction of the wave vector (deg from +Y, clockwise), phase
    (0.30, 4.0, 20.0, 0.3),
    (0.15, 2.5, 110.0, 1.1),
    (0.10, 1.6, 65.0, 2.0),
    (0.06, 1.2, 160.0, 0.7),
)
"""The "waves" terrain: a sum of plane waves z = A sin(k . x + phase).

Every quantity the presets compute has a closed form on it: the derivatives,
so slope, aspect and the curvatures, and the mean over a disc or annulus (a
plane wave's mean over a disc of radius R is its centre value times
2 J1(kR) / kR), so TPI and the local relief model.
"""


def axis(n: int = N, h: float = H) -> np.ndarray:
    half = (n - 1) * h / 2.0
    return np.linspace(-half, half, n)


def grid_xy(n: int = N, h: float = H) -> tuple[np.ndarray, np.ndarray]:
    """Cell-centre x and y, (n, n), row 0 north."""
    a = axis(n, h)
    x, y = np.meshgrid(a, a[::-1])
    return x, y


def _wave_vectors() -> list[tuple[float, float, float, float]]:
    out = []
    for amplitude, wavelength, direction, phase in WAVES:
        k = 2.0 * math.pi / wavelength
        d = math.radians(direction)
        out.append((amplitude, k * math.sin(d), k * math.cos(d), phase))
    return out


def waves(x, y) -> np.ndarray:
    """Height of the waves terrain."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    return sum(a * np.sin(kx * x + ky * y + ph) for a, kx, ky, ph in _wave_vectors())


def waves_derivatives(x, y) -> dict[str, np.ndarray]:
    """p = dz/dx, q = dz/dy, r = d2z/dx2, s = d2z/dxdy, t = d2z/dy2 (x east, y north)."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    out = {key: np.zeros(np.broadcast(x, y).shape) for key in "pqrst"}
    for a, kx, ky, ph in _wave_vectors():
        arg = kx * x + ky * y + ph
        c, s = a * np.cos(arg), a * np.sin(arg)
        out["p"] += kx * c
        out["q"] += ky * c
        out["r"] -= kx * kx * s
        out["s"] -= kx * ky * s
        out["t"] -= ky * ky * s
    return out


def waves_disc_mean(x, y, inner: float, outer: float) -> np.ndarray:
    """Exact mean height over the annulus inner..outer around (x, y) (a disc when inner = 0)."""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    total = np.zeros(np.broadcast(x, y).shape)
    for a, kx, ky, ph in _wave_vectors():
        k = math.hypot(kx, ky)
        factor = (outer**2 * disc_factor(k * outer) - inner**2 * disc_factor(k * inner)) / (
            outer**2 - inner**2
        )
        total += a * np.sin(kx * x + ky * y + ph) * factor
    return total


def disc_factor(u: float) -> float:
    """2 J1(u) / u: the mean of cos(k . x) over a disc of radius R, u = k R (1 at u = 0)."""
    if u == 0.0:
        return 1.0
    return 2.0 * bessel_j1(u) / u


def bessel_j1(u: float) -> float:
    """J1(u) from Bessel's integral (1/pi) int_0^pi cos(tau - u sin tau) dtau.

    The integrand is smooth and periodic, so the trapezoid rule converges
    geometrically; 512 nodes give full binary64 precision for u < 100.
    """
    tau = np.linspace(0.0, math.pi, 513)
    f = np.cos(tau - u * np.sin(tau))
    return float((f.sum() - 0.5 * (f[0] + f[-1])) * (math.pi / 512) / math.pi)


# --- the meshes -----------------------------------------------------------------------------------


def triangles(n: int) -> np.ndarray:
    """(2 (n-1)^2, 3) vertex indices of an n x n vertex grid, counter-clockwise from above."""
    i, j = np.meshgrid(np.arange(n - 1), np.arange(n - 1), indexing="ij")
    nw = (i * n + j).ravel()
    ne, sw = nw + 1, nw + n
    se = sw + 1
    # Rows run south, so (sw, se, ne) and (sw, ne, nw) are counter-clockwise seen from +Z.
    return np.concatenate([np.stack([sw, se, ne], 1), np.stack([sw, ne, nw], 1)]).astype(np.int32)


def quads(n: int) -> np.ndarray:
    """((n-1)^2, 4) quads of an n x n vertex grid, counter-clockwise from above (sw, se, ne, nw).

    Square k is quad k, and triangles k and k + (n-1)^2 of :func:`triangles`.
    """
    i, j = np.meshgrid(np.arange(n - 1), np.arange(n - 1), indexing="ij")
    nw = (i * n + j).ravel()
    return np.stack([nw + n, nw + n + 1, nw + 1, nw], 1).astype(np.int32)


def grid_vertices(z: np.ndarray, h: float) -> np.ndarray:
    """(n*n, 3) vertex positions of a DEM, row-major."""
    n = z.shape[0]
    x, y = grid_xy(n, h)
    return np.stack([x.ravel(), y.ravel(), np.asarray(z, np.float64).ravel()], 1)


def polar_mesh(ring_r, ring_z, segments: int, centre_z: float):
    """A mesh of rings around a centre vertex: (vertices, triangles), counter-clockwise up.

    Ring k has ``segments`` vertices at radius ``ring_r[k]`` and height
    ``ring_z[k]``; two rings at the same radius make a vertical wall.
    """
    angles = np.arange(segments) * (2.0 * math.pi / segments)
    verts = [np.array([[0.0, 0.0, centre_z]])]
    for r, z in zip(ring_r, ring_z, strict=True):
        verts.append(np.stack([r * np.sin(angles), r * np.cos(angles), np.full(segments, z)], 1))
    verts = np.concatenate(verts)
    seg = np.arange(segments)
    nxt = (seg + 1) % segments
    # Azimuths run clockwise seen from above, so (0, next, this) is counter-clockwise.
    tris = [np.stack([np.zeros(segments, int), 1 + nxt, 1 + seg], 1)]
    for ring in range(len(ring_r) - 1):
        a0, b0 = 1 + ring * segments, 1 + (ring + 1) * segments
        tris.append(np.stack([a0 + seg, a0 + nxt, b0 + nxt], 1))
        tris.append(np.stack([a0 + seg, b0 + nxt, b0 + seg], 1))
    return verts, np.concatenate(tris).astype(np.int32)


def polar_pit(radius: float, depth: float, outer: float, rings: int, segments: int):
    """A cylindrical pit with vertical walls in a level plain: (vertices, triangles).

    Floor (z = -depth) from the centre to ``radius``, a vertical wall there, and
    the plain (z = 0) out to ``outer``. Not a height field: the wall is two rings
    of vertices at the same radius.
    """
    floor_r = np.linspace(0.0, radius, rings + 1)[1:]
    plain_r = np.linspace(radius, outer, rings + 1)
    ring_r = np.concatenate([floor_r, plain_r])
    ring_z = np.concatenate([np.full(len(floor_r), -depth), np.zeros(len(plain_r))])
    return polar_mesh(ring_r, ring_z, segments, -depth)


def polar_cone(slope_deg: float, outer: float, rings: int, segments: int, sign: float = -1.0):
    """A cone with its apex at the origin, z = sign rho tan(slope): a peak (-1) or a pit (+1).

    Its facets keep the cone's slope along every ray from the apex, which a
    cone sampled on a square grid does not near the apex.
    """
    ring_r = np.linspace(0.0, outer, rings + 1)[1:]
    return polar_mesh(ring_r, sign * ring_r * math.tan(math.radians(slope_deg)), segments, 0.0)


def pit_horizon(
    px: float, py: float, radius: float, depth: float, azimuth: np.ndarray
) -> np.ndarray:
    """Horizon elevation (rad) from a floor point inside a vertical-walled cylindrical pit."""
    dx, dy = np.sin(azimuth), np.cos(azimuth)
    b = px * dx + py * dy
    distance = -b + np.sqrt(b * b - (px * px + py * py - radius * radius))
    return np.arctan(depth / distance)


# --- the GeoTools sampling patterns ----------------------------------------------------------------

GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def sunflower(samples: int, inner: float, outer: float) -> tuple[np.ndarray, np.ndarray]:
    """The (dx, dy) offsets the Mean Rise helper samples (TPI, local relief model).

    ``ceil(samples / 2)`` point-symmetric pairs on a sunflower of even areal
    density over the annulus, as ``scripts/make_analysis_presets.py`` builds it.
    """
    pairs = math.ceil(samples * 0.5)
    k = np.arange(pairs, dtype=np.float64)
    radius = np.sqrt(inner**2 + (outer**2 - inner**2) * (k + 0.5) / pairs)
    dx, dy = radius * np.sin(k * GOLDEN_ANGLE), radius * np.cos(k * GOLDEN_ANGLE)
    return np.concatenate([dx, -dx]), np.concatenate([dy, -dy])


def matched_disc_radius(second_moment: float) -> float:
    """Radius of the disc whose <x^2> (R^2 / 4) equals ``second_moment``."""
    return 2.0 * math.sqrt(second_moment)


def square_second_moment(radius_cells: int, h: float, centre: bool = True) -> float:
    """<x^2> over a (2m+1)^2 square window of cells (with or without the centre cell)."""
    m = radius_cells
    total = (2 * m + 1) * h * h * m * (m + 1) * (2 * m + 1) / 3.0  # sum of x^2 over the window
    count = (2 * m + 1) ** 2 - (0 if centre else 1)
    return total / count


# --- curvature from derivatives --------------------------------------------------------------------


def curvatures(p, q, r, s, t) -> dict[str, np.ndarray]:
    """Profile (k_v), plan (k_h) and mean (H) curvature, Florinsky (2016) with +convex.

    Exactly the formulas docs/analysis-presets.md states, in binary64.
    """
    p, q, r, s, t = (np.asarray(v, np.float64) for v in (p, q, r, s, t))
    g = p * p + q * q
    w = 1.0 + g
    with np.errstate(divide="ignore", invalid="ignore"):
        profile = -(p * p * r + 2 * p * q * s + q * q * t) / (g * w**1.5)
        plan = -(q * q * r - 2 * p * q * s + p * p * t) / (g * np.sqrt(w))
    mean = -((1 + q * q) * r - 2 * p * q * s + (1 + p * p) * t) / (2 * w**1.5)
    return {"profile": profile, "plan": plan, "mean": mean}


def evans_young(z: np.ndarray, h: float, step: int = 1) -> dict[str, np.ndarray]:
    """The Evans-Young derivatives of a DEM on a 3 x 3 window of spacing ``step`` cells.

    NaN within ``step`` cells of the edge. Row 0 north, as everywhere here.
    """
    z = np.asarray(z, np.float64)
    n0, n1 = z.shape
    k = step
    w = k * h

    def at(di: int, dj: int) -> np.ndarray:
        out = np.full(z.shape, np.nan)
        out[k : n0 - k, k : n1 - k] = z[k + di : n0 - k + di, k + dj : n1 - k + dj]
        return out

    z1, z2, z3 = at(-k, -k), at(-k, 0), at(-k, k)  # north row: NW, N, NE
    z4, z5, z6 = at(0, -k), at(0, 0), at(0, k)
    z7, z8, z9 = at(k, -k), at(k, 0), at(k, k)  # south row
    return {
        "p": (z3 + z6 + z9 - z1 - z4 - z7) / (6 * w),
        "q": (z1 + z2 + z3 - z7 - z8 - z9) / (6 * w),
        "r": (z1 + z3 + z4 + z6 + z7 + z9 - 2 * (z2 + z5 + z8)) / (3 * w * w),
        "t": (z1 + z2 + z3 + z7 + z8 + z9 - 2 * (z4 + z5 + z6)) / (3 * w * w),
        "s": (z3 + z7 - z1 - z9) / (4 * w * w),
    }


def slope_aspect(p, q) -> tuple[np.ndarray, np.ndarray]:
    """Slope (deg) and aspect (deg clockwise from +Y, the downslope direction) from p, q."""
    p, q = np.asarray(p, np.float64), np.asarray(q, np.float64)
    slope = np.degrees(np.arctan(np.hypot(p, q)))
    aspect = np.degrees(np.arctan2(-p, -q)) % 360.0
    return slope, aspect


def angle_difference(a, b) -> np.ndarray:
    """Signed smallest difference a - b between two angles in degrees, in [-180, 180)."""
    return (np.asarray(a, np.float64) - np.asarray(b, np.float64) + 180.0) % 360.0 - 180.0


# --- sampling a DEM's mesh and rasters -------------------------------------------------------------


def mesh_height(z: np.ndarray, h: float, x, y) -> np.ndarray:
    """Height of the triangulated DEM (see :func:`triangles`) at (x, y); NaN off the mesh."""
    z = np.asarray(z, np.float64)
    n = z.shape[0]
    half = (n - 1) * h / 2.0
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    fj = (x + half) / h  # columns from the west
    fi = (half - y) / h  # rows from the north
    j = np.clip(np.floor(fj).astype(int), 0, n - 2)
    i = np.clip(np.ceil(fi).astype(int), 1, n - 1)  # the south row of the square
    u, v = fj - j, i - fi  # 0..1 east from the west column, north from the south row
    sw, se = z[i, j], z[i, j + 1]
    nw, ne = z[i - 1, j], z[i - 1, j + 1]
    lower = sw + u * (se - sw) + v * (ne - se)  # triangle (sw, se, ne): u >= v
    upper = sw + u * (ne - nw) + v * (nw - sw)  # triangle (sw, ne, nw): u < v
    out = np.where(u >= v, lower, upper)
    off = (fj < -1e-9) | (fj > n - 1 + 1e-9) | (fi < -1e-9) | (fi > n - 1 + 1e-9)
    return np.where(off, np.nan, out)


def bilinear(grid: np.ndarray, x0: float, y0: float, h: float, x, y) -> np.ndarray:
    """Bilinear interpolation of a north-up raster whose cell (0, 0) centre is (x0, y0)."""
    grid = np.asarray(grid, np.float64)
    fj = (np.asarray(x, np.float64) - x0) / h
    fi = (y0 - np.asarray(y, np.float64)) / h
    j = np.clip(np.floor(fj).astype(int), 0, grid.shape[1] - 2)
    i = np.clip(np.floor(fi).astype(int), 0, grid.shape[0] - 2)
    u, v = fj - j, fi - i
    top = grid[i, j] * (1 - u) + grid[i, j + 1] * u
    bottom = grid[i + 1, j] * (1 - u) + grid[i + 1, j + 1] * u
    return top * (1 - v) + bottom * v


# --- the horizon on the continuous waves surface ------------------------------------------------------


def waves_normal(x, y) -> np.ndarray:
    d = waves_derivatives(x, y)
    n = np.stack([-d["p"], -d["q"], np.ones_like(d["p"])], -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True)


def waves_horizon(
    px,
    py,
    directions: int,
    max_distance: float,
    *,
    ray_offset: float = 0.0,
    below: bool = False,
    ball: bool = True,
    s_min: float = 0.0,
    ds: float = 0.002,
    azimuth_offset: float = 0.0,
) -> np.ndarray:
    """Horizon elevation angles (deg), (points, directions), on the continuous waves surface.

    For each azimuth i 360/N from +Y, the highest (``below``: lowest) elevation
    angle from the origin to a surface point along that azimuth. The origin is
    the surface point moved ``ray_offset`` along the surface normal (against it
    when ``below``). ``ball``: only points within ``max_distance`` of the
    origin count (GeoTools: rays of length Max Distance); otherwise points
    within a horizontal distance ``max_distance`` (RVT's search radius).
    ``s_min`` skips points nearer than that horizontally (RVT starts one cell
    out). ``azimuth_offset`` (deg) turns every azimuth clockwise. Points are sampled every ``ds`` metres, so angles are exact to about
    ``ds`` times the surface curvature.
    """
    px, py = np.asarray(px, np.float64).ravel(), np.asarray(py, np.float64).ravel()
    normal = waves_normal(px, py)
    side = -ray_offset if below else ray_offset
    ox, oy = px + side * normal[:, 0], py + side * normal[:, 1]
    oz = waves(px, py) + side * normal[:, 2]
    s = np.arange(max(ds, s_min), max_distance + ds / 2, ds)
    out = np.empty((len(px), directions))
    for k in range(directions):
        a = 2.0 * math.pi * k / directions + math.radians(azimuth_offset)
        ux, uy = math.sin(a), math.cos(a)
        sx = ox[:, None] + s[None, :] * ux
        sy = oy[:, None] + s[None, :] * uy
        dz = waves(sx, sy) - oz[:, None]
        # Horizontal distance from the origin (the origin is off the vertical of p).
        dist_h = np.broadcast_to(s[None, :], dz.shape)
        reach = np.hypot(dist_h, dz) if ball else dist_h
        angle = np.degrees(np.arctan2(dz, dist_h))
        angle = np.where(reach <= max_distance, angle, np.nan)
        out[:, k] = np.nanmin(angle, axis=1) if below else np.nanmax(angle, axis=1)
    return out


def horizon_values(angles: np.ndarray) -> dict[str, np.ndarray]:
    """SVF, positive and negative openness from horizon angles (deg), mean over axis 1.

    ``angles`` for SVF and positive openness are the highest angles; pass the
    lowest angles to get negative openness from ``openness_negative``.
    """
    rad = np.radians(angles)
    return {
        "svf": 1.0 - np.mean(np.sin(np.maximum(rad, 0.0)), axis=1),
        "openness_positive": 90.0 - np.mean(angles, axis=1),
        "openness_negative": 90.0 + np.mean(angles, axis=1),
    }


def mesh_horizon(
    z: np.ndarray,
    h: float,
    origins: np.ndarray,
    directions: int,
    max_distance: float,
    *,
    below: bool = False,
    ds: float = 0.002,
) -> np.ndarray:
    """Horizon angles (deg), (points, directions), on the triangulated DEM itself.

    As :func:`waves_horizon` with ``ball``, but over the mesh (piecewise
    linear) from given ray origins (a vertex moved Ray Offset along its
    vertex normal): the highest (``below``: lowest) elevation angle to a mesh
    point within ``max_distance`` of the origin along each azimuth.
    """
    origins = np.asarray(origins, np.float64)
    s = np.arange(ds, max_distance + ds / 2, ds)
    out = np.empty((len(origins), directions))
    for k in range(directions):
        a = 2.0 * math.pi * k / directions
        sx = origins[:, :1] + s[None, :] * math.sin(a)
        sy = origins[:, 1:2] + s[None, :] * math.cos(a)
        dz = mesh_height(z, h, sx, sy) - origins[:, 2:3]
        angle = np.degrees(np.arctan2(dz, s[None, :]))
        angle = np.where(np.hypot(s[None, :], dz) <= max_distance, angle, np.nan)
        out[:, k] = np.nanmin(angle, axis=1) if below else np.nanmax(angle, axis=1)
    return out


# --- extruded profiles: overhangs with exact answers (direct visibility) ------------------------
#
# A profile (x, z) extruded along y far enough (``half_length``, hundreds of
# times its size) that it is infinite for every ray that matters. Then a
# direction's fate depends only on its projection onto the x-z plane, at angle
# psi from +x towards +z, and directions are uniform in (psi, y) (dω = dψ dy,
# with y the pole). So with the sample's visible psi interval (lo, hi), from
# the ray origin, and n at angle psi_n in the plane:
#
#   sky visibility = |visible ∩ front ∩ (0, π)| / π
#   sky view (cos)  = ½ ∫ cos(ψ - ψ_n) dψ over visible ∩ front ∩ (0, π)
#   openness (3D)   = |visible ∩ front| / π
#
# and the interval ends are angles to vertices of the mesh itself (the lip of
# the vault, the rim of a trench wall), so these are exact for the mesh.


def extruded(
    profile: np.ndarray, half_length: float, half_width: float
) -> tuple[np.ndarray, np.ndarray]:
    """A profile (P, 2) in (x, z) extruded along y: (vertices, quads), quads (3 (P - 1), 4).

    Sections at y = -half_length, -half_width, half_width, half_length; the
    middle strip's quads are ``P - 1 .. 2 (P - 1) - 1``, in profile order. A
    face's normal is the profile direction turned +90° in the x-z plane (left
    of the direction of travel, from +x towards +z).
    """
    profile = np.asarray(profile, np.float64)
    p = len(profile)
    sections = np.array([-half_length, -half_width, half_width, half_length])
    verts = np.concatenate(
        [np.stack([profile[:, 0], np.full(p, y), profile[:, 1]], axis=1) for y in sections]
    )
    k = np.arange(p - 1)
    quads = [
        np.stack([j * p + k, j * p + k + 1, (j + 1) * p + k + 1, (j + 1) * p + k], axis=1)
        for j in range(3)
    ]
    return verts, np.concatenate(quads).astype(np.int32)


def vault_profile(radius: float, front: float, arc_segments: int, floor_step: float) -> np.ndarray:
    """A quarter-cylinder vault over a level floor, open at the front: the cylinder overhang.

    From the lip (0, radius) down the arc (centre at the origin) to the back
    corner (-radius, 0), then the floor (z = 0) out to x = ``front``, in steps
    of ``floor_step``. Normals point into the shelter (inwards on the arc, up
    on the floor).
    """
    theta = np.linspace(0.5 * math.pi, math.pi, arc_segments + 1)
    arc = radius * np.stack([np.cos(theta), np.sin(theta)], axis=1)
    steps = round((front + radius) / floor_step)
    floor = np.stack([np.linspace(-radius, front, steps + 1)[1:], np.zeros(steps)], axis=1)
    return np.concatenate([arc, floor])


def trench_profile(slope_deg: float, left: float, right: float, segments: int) -> np.ndarray:
    """A V-trench with walls at ``slope_deg``, ``left`` and ``right`` long down the slope.

    From the left rim down to the crease at the origin and up to the right
    rim, ``segments`` faces per wall; normals point into the trench.
    """
    b = math.radians(slope_deg)
    s_left = np.linspace(left, 0.0, segments + 1)
    s_right = np.linspace(0.0, right, segments + 1)[1:]
    left_wall = np.stack([-s_left * math.cos(b), s_left * math.sin(b)], axis=1)
    right_wall = np.stack([s_right * math.cos(b), s_right * math.sin(b)], axis=1)
    return np.concatenate([left_wall, right_wall])


def profile_faces(profile: np.ndarray, offset: float) -> tuple[np.ndarray, np.ndarray]:
    """Each profile segment's ray origin (centre + offset x normal), (S, 2), and normal angle."""
    a, b = profile[:-1], profile[1:]
    t = b - a
    t = t / np.linalg.norm(t, axis=1, keepdims=True)
    normal = np.stack([-t[:, 1], t[:, 0]], axis=1)
    return 0.5 * (a + b) + offset * normal, np.arctan2(normal[:, 1], normal[:, 0])


def angle_to(origin: np.ndarray, point) -> np.ndarray:
    """Angle (rad, from +x towards +z) of ``point - origin`` in the x-z plane."""
    d = np.asarray(point, np.float64) - origin
    return np.arctan2(d[..., 1], d[..., 0])


def extruded_values(lo, hi, normal_angle) -> dict[str, np.ndarray]:
    """The three direct-visibility values of samples whose visible psi interval is (lo, hi).

    Angles in radians; the interval is clipped to the face's front half, and
    the sky part to psi in (0, π) (split where sin psi changes sign).
    """
    lo, hi, n = np.broadcast_arrays(
        *(np.atleast_1d(np.asarray(v, np.float64)) for v in (lo, hi, normal_angle))
    )
    out = {"sky_visibility": [], "sky_view_cosine": [], "openness_3d": []}
    for a, b, psi_n in zip(lo, hi, n, strict=True):
        # Relative to the normal, in (-π, π].
        ra = (a - psi_n + math.pi) % (2 * math.pi) - math.pi
        rb = ra + (b - a) % (2 * math.pi)
        ra, rb = max(ra, -0.5 * math.pi), min(rb, 0.5 * math.pi)
        length = max(rb - ra, 0.0)
        cuts = [ra, rb]
        if length > 0:
            k = np.arange(-4, 5)
            zeros = -psi_n + k * math.pi
            cuts = sorted([ra, rb, *zeros[(zeros > ra) & (zeros < rb)]])
        sky, cosine = 0.0, 0.0
        for u, v in itertools.pairwise(cuts):
            if v > u and math.sin(psi_n + 0.5 * (u + v)) > 0:
                sky += v - u
                cosine += 0.5 * (math.sin(v) - math.sin(u))
        out["sky_visibility"].append(sky / math.pi)
        out["sky_view_cosine"].append(cosine)
        out["openness_3d"].append(length / math.pi)
    return {k: np.array(v) for k, v in out.items()}


def ray_mesh_hits(
    verts: np.ndarray,
    faces: np.ndarray,
    origins: np.ndarray,
    direction: np.ndarray,
    length: float,
) -> np.ndarray:
    """Whether each ray from ``origins`` along ``direction`` meets the mesh within ``length``.

    Binary64 Möller-Trumbore over the faces (quads split into two triangles),
    both sides, as Cycles' Raycast node.
    """
    faces = np.asarray(faces)
    tris = faces if faces.shape[1] == 3 else np.concatenate([faces[:, :3], faces[:, [0, 2, 3]]])
    v0, v1, v2 = (verts[tris[:, i]] for i in range(3))
    e1, e2 = v1 - v0, v2 - v0
    d = np.asarray(direction, np.float64)
    p = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > 1e-15
    inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    s = origins[:, None, :] - v0[None, :, :]
    u = np.einsum("rtj,tj->rt", s, p) * inv
    q = np.cross(s, e1[None, :, :])
    v = (q @ d) * inv
    t = np.einsum("rtj,tj->rt", q, e2) * inv
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0) & (t <= length)
    return hit.any(axis=1)


EXTRUSION = 500.0
"""Half length of the extruded cases along y: 250 to 500 times their size."""
VAULT = {"radius": 1.0, "front": 3.0, "arc_segments": 32, "floor_step": 0.25}
TRENCH = {"slope_deg": 30.0, "left": 2.0, "right": 1.0, "segments": 8}


def extruded_case(name: str, offset: float) -> dict[str, np.ndarray]:
    """The vault ("vault") or the uneven V-trench ("trench") and the exact values of its faces.

    Returns the mesh (``verts``, ``quads``), per middle-strip face (in profile
    order) the ray ``origins`` and ``normals`` in 3D, the face's ``side`` (vault:
    "floor" or "arc"; trench: "left" or "right") and the exact values by entry
    id. Vault: a floor face sees the sky from level up to its lip; an arc face
    sees out between the floor's front edge and the lip. Trench: a face sees
    from the opposite rim to the top of its own wall's front half. Rays start
    ``offset`` along the normal, as the bake's (Ray Offset).
    """
    if name == "vault":
        profile = vault_profile(**VAULT)
        arc = VAULT["arc_segments"]
    else:
        profile = trench_profile(**TRENCH)
        arc = TRENCH["segments"]
    origins, normal = profile_faces(profile, offset)
    first = np.arange(len(origins)) < arc
    if name == "vault":
        side = np.where(first, "arc", "floor")
        lo = np.where(first, angle_to(origins, profile[-1]), 0.0)
        hi = angle_to(origins, profile[0])
    else:
        side = np.where(first, "left", "right")
        lo = np.where(first, angle_to(origins, profile[-1]), normal - 0.5 * math.pi)
        hi = np.where(first, normal + 0.5 * math.pi, angle_to(origins, profile[0]))
    verts, quads = extruded(profile, EXTRUSION, 0.05)
    count = len(origins)
    return {
        "verts": verts,
        "quads": quads,
        "middle": np.arange(count, 2 * count),
        "origins": np.stack([origins[:, 0], np.zeros(count), origins[:, 1]], axis=1),
        "normals": np.stack([np.cos(normal), np.zeros(count), np.sin(normal)], axis=1),
        "side": side,
        "exact": extruded_values(lo, hi, normal),
    }
