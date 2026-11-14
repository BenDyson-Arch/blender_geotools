"""Synthetic cases for validating GeoTools' visibility, distance, volume and path presets.

Pure numpy, no bpy: the reference scripts next to this file and the Blender
test ``tests/blender/test_validation_visibility.py`` build the same inputs
from here, so a case is defined once.

A DEM is a regular grid of ``(nx + 1) x (ny + 1)`` nodes at spacing ``h``
with its south-west node at ``(x0, y0)``. As a mesh, every cell is split into
two triangles along its south-west to north-east diagonal; as a raster (for
GDAL and GRASS), every node is a cell centre, so the raster's extent is the
grid's grown by ``h / 2``.

The exact line of sight over that TIN (``tin_clear``) checks a segment
against the TIN's height at every point where it crosses a grid line or a
cell diagonal. Between two such crossings the segment and the TIN are both
linear, so their difference is extreme at the crossings: the test is exact
(binary64), with no sampling step. It is independent of Blender: no ray
tracer, no BVH.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# --- grids ---------------------------------------------------------------------------------


@dataclass
class Grid:
    """A DEM on a regular grid: ``z[j, i]`` is the height at ``(x0 + i h, y0 + j h)``."""

    x0: float
    y0: float
    h: float
    z: np.ndarray  # (ny + 1, nx + 1), binary64
    name: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def nx(self) -> int:
        return self.z.shape[1] - 1

    @property
    def ny(self) -> int:
        return self.z.shape[0] - 1

    def axes(self) -> tuple[np.ndarray, np.ndarray]:
        return self.x0 + self.h * np.arange(self.nx + 1), self.y0 + self.h * np.arange(self.ny + 1)

    def nodes(self) -> np.ndarray:
        """Every node as (x, y, z), row by row from the south (vertex order of the mesh)."""
        xs, ys = np.meshgrid(*self.axes())
        return np.stack([xs.ravel(), ys.ravel(), self.z.ravel()], axis=1)

    def triangles(self) -> np.ndarray:
        """Vertex indices of the two counter-clockwise triangles of every cell."""
        i, j = np.meshgrid(np.arange(self.nx), np.arange(self.ny))
        a = (j * (self.nx + 1) + i).ravel()
        b, c, d = a + 1, a + self.nx + 2, a + self.nx + 1
        tris = np.empty((2 * a.size, 3), np.int64)
        tris[0::2] = np.stack([a, b, c], axis=1)  # south-east half
        tris[1::2] = np.stack([a, c, d], axis=1)  # north-west half
        return tris

    def faces(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(centroids, unit normals, areas) of the triangles, binary64."""
        co = self.nodes()
        p = co[self.triangles()]
        cross = np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0])
        area = np.linalg.norm(cross, axis=1)
        return p.mean(axis=1), cross / area[:, None], area / 2.0

    def node_normals(self) -> np.ndarray:
        """Unit normals at the nodes (central differences)."""
        gy, gx = np.gradient(self.z, self.h)
        n = np.stack([-gx, -gy, np.ones_like(self.z)], axis=-1).reshape(-1, 3)
        return n / np.linalg.norm(n, axis=1)[:, None]

    def raster(self) -> np.ndarray:
        """The heights as raster rows, north first."""
        return self.z[::-1]

    def height(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        """The TIN's height at (x, y); NaN outside the grid."""
        return tin_height(
            self, (np.asarray(x) - self.x0) / self.h, (np.asarray(y) - self.y0) / self.h
        )


def grid(fn, x0: float, y0: float, x1: float, y1: float, h: float, name: str = "", **meta) -> Grid:
    nx, ny = round((x1 - x0) / h), round((y1 - y0) / h)
    xs = x0 + h * np.arange(nx + 1)
    ys = y0 + h * np.arange(ny + 1)
    X, Y = np.meshgrid(xs, ys)
    return Grid(x0, y0, h, np.asarray(fn(X, Y), np.float64) + 0.0 * X, name, dict(meta))


def write_aaigrid(path, g: Grid) -> None:
    """An ESRI ASCII grid (GDAL's AAIGrid driver) whose cell centres are the grid's nodes."""
    with open(path, "w") as f:
        f.write(f"ncols {g.nx + 1}\nnrows {g.ny + 1}\n")
        f.write(f"xllcorner {g.x0 - g.h / 2!r}\nyllcorner {g.y0 - g.h / 2!r}\n")
        f.write(f"cellsize {g.h!r}\nNODATA_value -9999\n")
        np.savetxt(f, g.raster(), fmt="%.9g")


# --- the exact line of sight over the TIN -------------------------------------------------------


def tin_height(g: Grid, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """The TIN's height at grid coordinates (u, v) = ((x - x0) / h, (y - y0) / h)."""
    u, v = np.broadcast_arrays(np.asarray(u, np.float64), np.asarray(v, np.float64))
    inside = (u >= 0) & (u <= g.nx) & (v >= 0) & (v <= g.ny)
    u, v = np.where(inside, u, 0.0), np.where(inside, v, 0.0)
    i = np.clip(np.floor(u), 0, g.nx - 1).astype(np.int64)
    j = np.clip(np.floor(v), 0, g.ny - 1).astype(np.int64)
    fu, fv = u - i, v - j
    z = g.z
    za, zb, zc, zd = z[j, i], z[j, i + 1], z[j + 1, i + 1], z[j + 1, i]
    lower = za + fu * (zb - za) + fv * (zc - zb)  # triangle a b c (fu >= fv)
    upper = za + fv * (zd - za) + fu * (zc - zd)  # triangle a c d
    return np.where(inside, np.where(fu >= fv, lower, upper), np.nan)


def tin_margin(g: Grid, start: np.ndarray, end: np.ndarray, chunk: int = 2048) -> np.ndarray:
    """The least height of each segment above the TIN, over its crossings strictly inside it.

    Negative: the segment passes below the TIN (blocked). ``+inf`` when it crosses no grid
    line or diagonal over the grid. ``start`` and ``end`` are (N, 3) or broadcastable.
    """
    start, end = np.broadcast_arrays(np.asarray(start, np.float64), np.asarray(end, np.float64))
    out = np.full(len(start), np.inf)
    ks_u = np.arange(g.nx + 1, dtype=np.float64)
    ks_v = np.arange(g.ny + 1, dtype=np.float64)
    ks_d = np.arange(-g.ny, g.nx + 1, dtype=np.float64)
    for lo in range(0, len(start), chunk):
        p, e = start[lo : lo + chunk], end[lo : lo + chunk]
        u0, v0 = (p[:, 0] - g.x0) / g.h, (p[:, 1] - g.y0) / g.h
        du, dv = (e[:, 0] - p[:, 0]) / g.h, (e[:, 1] - p[:, 1]) / g.h
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.concatenate(
                [
                    (ks_u[None] - u0[:, None]) / du[:, None],
                    (ks_v[None] - v0[:, None]) / dv[:, None],
                    (ks_d[None] - (u0 - v0)[:, None]) / (du - dv)[:, None],
                ],
                axis=1,
            )
        t = np.where(np.isfinite(t) & (t > 0) & (t < 1), t, np.nan)
        u = u0[:, None] + t * du[:, None]
        v = v0[:, None] + t * dv[:, None]
        zs = p[:, 2:3] + t * (e[:, 2] - p[:, 2])[:, None]
        diff = zs - tin_height(g, u, v)
        diff = np.where(np.isnan(diff), np.inf, diff)
        out[lo : lo + chunk] = diff.min(axis=1)
    return out


# --- the cases -------------------------------------------------------------------------------

EYE = 1.6  # GeoTools' default Observer Height
OFFSET = 0.01  # GeoTools' default Ray Offset (visibility)

# Ridge: a triangular ridge along y, crest at x = D, height H, half-width W.
RIDGE = {"D": 10.0, "H": 1.0, "W": 2.0, "observer": (0.0, 0.0)}


def ridge() -> Grid:
    D, H, W = RIDGE["D"], RIDGE["H"], RIDGE["W"]
    return grid(
        lambda x, y: H * np.clip(1 - np.abs(x - D) / W, 0, None),
        -10.0,
        -15.0,
        50.0,
        15.0,
        0.5,
        "ridge",
        **RIDGE,
    )


def ridge_shadow_end(eye: float = EYE, target: float = 0.0) -> float:
    """Where the shadow behind the ridge ends on level ground (similar triangles)."""
    D, H = RIDGE["D"], RIDGE["H"]
    return D * (eye - target) / (eye - H)


# Dome: a hemisphere of radius R at C on level ground, seen from a tower EYE_DOME high.
DOME = {"R": 5.0, "C": (15.0, 0.0), "observer": (0.0, 0.0), "eye": 8.0}


def dome() -> Grid:
    R, (cx, cy) = DOME["R"], DOME["C"]
    return grid(
        lambda x, y: np.sqrt(np.clip(R * R - (x - cx) ** 2 - (y - cy) ** 2, 0, None)),
        -5.0,
        -15.0,
        35.0,
        15.0,
        0.25,
        "dome",
        **DOME,
    )


def dome_visible(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Analytic visibility from the tower over a true hemisphere, and the margin (m).

    A ground point is hidden when the segment from it to the eye passes within R of the
    centre; a point on the sphere is seen when it is outside the tangent cone from the eye,
    (p - C) . (E - C) > R² (scaled to metres by dividing by |E - C|).
    """
    R = DOME["R"]
    c = np.array([*DOME["C"], 0.0])
    e = np.array([*DOME["observer"], DOME["eye"]])
    p = np.asarray(points, np.float64)
    on_dome = np.linalg.norm(p - c, axis=1) < R + 0.05
    d = e - p
    t = np.clip(np.einsum("ij,ij->i", c - p, d) / np.einsum("ij,ij->i", d, d), 0, 1)
    closest = np.linalg.norm(p + t[:, None] * d - c, axis=1)
    ground_margin = closest - R
    ec = e - c
    sphere_margin = (np.einsum("ij,j->i", p - c, ec) - R * R) / np.linalg.norm(ec)
    margin = np.where(on_dome, sphere_margin, ground_margin)
    return margin > 0, margin


# Hills: a sum of Gaussians, for cross-implementation statistics.
HILLS = [  # (x, y, height, sigma)
    (-12.0, 8.0, 6.0, 6.0),
    (10.0, 14.0, 4.0, 4.0),
    (14.0, -10.0, 8.0, 7.0),
    (-6.0, -14.0, 3.0, 3.0),
    (0.0, 0.0, 2.5, 5.0),
    (22.0, 4.0, 3.5, 3.5),
    (-22.0, -4.0, 5.0, 4.5),
    (4.0, 24.0, 6.0, 5.0),
]
HILLS_OBSERVER = (-20.0, -20.0)
HILLS_OBSERVERS = [(-20.0, -20.0), (20.0, 20.0), (0.0, 20.0), (25.0, -25.0)]
HILLS_POINTS = [
    (-20.0, -20.0),
    (20.0, 20.0),
    (0.0, 20.0),
    (25.0, -25.0),
    (-25.0, 25.0),
    (8.0, -2.0),
]


def hills_height(x, y):
    z = 0.0 * np.asarray(x, np.float64)
    for hx, hy, a, s in HILLS:
        z = z + a * np.exp(-((x - hx) ** 2 + (y - hy) ** 2) / (2 * s * s))
    return z


def hills(h: float = 0.5) -> Grid:
    return grid(hills_height, -32.0, -32.0, 32.0, 32.0, h, "hills")


# Long range: level ground 20 km across at 100 m, a 10 m mast in the middle.
LONG = {"observer": (0.0, 0.0), "eye": 10.0, "max_distance": 20000.0}


def long_range() -> Grid:
    return grid(lambda x, y: 0.0 * x, -10000.0, -10000.0, 10000.0, 10000.0, 100.0, "long", **LONG)


def curvature_drop(d, k: float = 1.0 / 7.0, radius: float = 6371008.8):
    """The apparent drop of a point at distance d by Earth curvature with refraction k."""
    return (1.0 - k) * np.asarray(d, np.float64) ** 2 / (2.0 * radius)


def horizon_distance(eye: float, k: float = 1.0 / 7.0, radius: float = 6371008.8) -> float:
    """Distance to the horizon over level ground from an eye ``eye`` metres up."""
    return math.sqrt(2.0 * radius * eye / (1.0 - k))


# --- visibility of faces and nodes ------------------------------------------------------------


def face_view_points(g: Grid, target: float = 0.0, offset: float = OFFSET):
    """GeoTools' viewshed sample points: centroid + offset x normal + target up."""
    centres, normals, _ = g.faces()
    points = centres + offset * normals
    points[:, 2] += target
    return points, normals, centres


def viewshed_faces(g: Grid, eye, target: float = 0.0, offset: float = OFFSET, grazing=1e-3):
    """Exact viewshed of the TIN's faces, by GeoTools' definition.

    Returns (seen, unsure): seen is GeoTools' rule evaluated exactly in binary64 (the
    face fronts the eye when target is 0, and nothing of the TIN lies on the segment
    from the sample point to the eye); unsure marks sight lines that pass within
    ``grazing`` metres of the TIN (either answer is right in float32).
    """
    eye = np.asarray(eye, np.float64)
    points, normals, _ = face_view_points(g, target, offset)
    margin = tin_margin(g, points, eye[None])
    facing = (target > 0) | (np.einsum("ij,ij->i", normals, eye - points) > 0)
    return facing & (margin >= 0), facing & (np.abs(margin) < grazing)


def viewshed_nodes(g: Grid, eye, target: float = 0.0, offset: float = OFFSET, grazing=1e-3):
    """Exact visibility of the nodes (the raster cell centres), raised by offset + target."""
    eye = np.asarray(eye, np.float64)
    points = g.nodes().copy()
    points[:, 2] += target + offset
    margin = tin_margin(g, points, eye[None])
    return margin >= 0, np.abs(margin) < grazing


def node_eye(g: Grid, xy, height: float) -> np.ndarray:
    """The eye above a point of the grid (on the TIN), as GeoTools drapes it."""
    x, y = xy
    return np.array([x, y, float(g.height(x, y)) + height])


# --- distance cases ---------------------------------------------------------------------------

TAN30 = math.tan(math.radians(30.0))
TILT_RISE = 0.25  # the reference plane is this far above the measured one, vertically


def tilted(offset: float = 0.0, half: float = 10.0, h: float = 0.5, name: str = "tilted") -> Grid:
    """The plane z = x tan 30° + offset."""
    return grid(lambda x, y: x * TAN30 + offset, -half, -half, half, half, h, name)


def uv_sphere(radius: float, rings: int, segments: int, centre=(0.0, 0.0, 0.0)):
    """A closed triangulated UV sphere (outward), as (vertices, triangles)."""
    theta = np.linspace(0, math.pi, rings + 1)[1:-1]  # polar angle, poles apart
    phi = np.linspace(0, 2 * math.pi, segments, endpoint=False)
    T, P = np.meshgrid(theta, phi, indexing="ij")
    co = np.stack([np.sin(T) * np.cos(P), np.sin(T) * np.sin(P), np.cos(T)], -1).reshape(-1, 3)
    co = np.vstack([[0, 0, 1], co, [0, 0, -1]]) * radius + np.asarray(centre, np.float64)
    tris = []
    ring = lambda r, s: 1 + r * segments + s % segments
    for s in range(segments):
        tris.append((0, ring(0, s), ring(0, s + 1)))
        last = len(co) - 1
        tris.append((last, ring(rings - 2, s + 1), ring(rings - 2, s)))
        for r in range(rings - 2):
            a, b = ring(r, s), ring(r, s + 1)
            c, d = ring(r + 1, s + 1), ring(r + 1, s)
            tris += [(a, d, c), (a, c, b)]
    return co, np.array(tris, np.int64)


def gable(half: float = 6.0, h: float = 0.25, pitch: float = 60.0) -> Grid:
    """A sharp ridge along y: z = 3 - |x| tan(pitch), its faces' normals 2 x pitch apart."""
    t = math.tan(math.radians(pitch))
    return grid(lambda x, y: 3.0 - np.abs(x) * t, -1.5, -half, 1.5, half, h, "gable")


# Features for Distance to Feature (plan): an L-shaped polygon, a polyline and points.
FEATURE_POLYGON = [(-6.0, -6.0), (-1.0, -6.0), (-1.0, -4.0), (-4.0, -4.0), (-4.0, 1.0), (-6.0, 1.0)]
FEATURE_LINE = [(1.0, -7.0), (4.0, -2.0), (2.0, 3.0), (7.0, 6.0)]
FEATURE_POINTS = [(-5.0, 6.0), (0.0, 0.0), (6.0, -6.0)]
FEATURE_Z = 3.0  # the measured grid's height; the features sit at z = 0 (2D imports)


# --- volume cases -----------------------------------------------------------------------------


def _fan(apex: int, ring: list[int], up: bool) -> list[tuple[int, int, int]]:
    n = len(ring)
    return [
        (apex, ring[k], ring[(k + 1) % n]) if up else (apex, ring[(k + 1) % n], ring[k])
        for k in range(n)
    ]


def frustum(r0: float, r1: float, height: float, n: int, centre=(0.0, 0.0, 0.0)):
    """A closed regular n-gon frustum (r1 = 0: a pyramid), outward triangles."""
    a = 2 * math.pi * np.arange(n) / n
    ring0 = np.stack([r0 * np.cos(a), r0 * np.sin(a), np.zeros(n)], 1)
    co = [np.zeros((1, 3)), ring0]
    bottom = list(range(1, n + 1))
    tris = _fan(0, bottom, up=False)
    if r1 > 0:
        ring1 = np.stack([r1 * np.cos(a), r1 * np.sin(a), np.full(n, height)], 1)
        co += [ring1, [[0.0, 0.0, height]]]
        top = list(range(n + 1, 2 * n + 1))
        tris += _fan(2 * n + 1, top, up=True)
        for k in range(n):
            b0, b1, t0, t1 = bottom[k], bottom[(k + 1) % n], top[k], top[(k + 1) % n]
            tris += [(b0, b1, t1), (b0, t1, t0)]
    else:
        co.append([[0.0, 0.0, height]])
        tris += _fan(n + 1, bottom, up=True)
    co = np.vstack(co) + np.asarray(centre, np.float64)
    return co, np.array(tris, np.int64)


def frustum_volume(r0: float, r1: float, height: float, n: int) -> float:
    """Exact volume of the faceted frustum: h/3 (A0 + A1 + sqrt(A0 A1)), A = n/2 r² sin(2π/n)."""
    area = lambda r: n / 2 * r * r * math.sin(2 * math.pi / n)
    a0, a1 = area(r0), area(r1)
    return height / 3 * (a0 + a1 + math.sqrt(a0 * a1))


def prism(base: float, height: float, length: float, centre=(0.0, 0.0, 0.0)):
    """A closed triangular prism along y (isosceles cross-section), outward triangles."""
    b, h, L = base / 2, height, length / 2
    co = np.array([(-b, -L, 0), (b, -L, 0), (0, -L, h), (-b, L, 0), (b, L, 0), (0, L, h)], float)
    tris = [(0, 1, 2), (3, 5, 4), (0, 4, 1), (0, 3, 4), (1, 5, 2), (1, 4, 5), (2, 3, 0), (2, 5, 3)]
    return co + np.asarray(centre, np.float64), np.array(tris, np.int64)


def hemisphere(radius: float, rings: int, segments: int, centre=(0.0, 0.0, 0.0)):
    """A closed hemisphere (dome and flat base disc), outward triangles."""
    theta = np.linspace(0, math.pi / 2, rings + 1)[1:]
    phi = np.linspace(0, 2 * math.pi, segments, endpoint=False)
    T, P = np.meshgrid(theta, phi, indexing="ij")
    co = np.stack([np.sin(T) * np.cos(P), np.sin(T) * np.sin(P), np.cos(T)], -1).reshape(-1, 3)
    co = np.vstack([[0, 0, 1], co, [0, 0, 0]]) * radius + np.asarray(centre, np.float64)
    ring = lambda r, s: 1 + r * segments + s % segments
    tris = []
    for s in range(segments):
        tris.append((0, ring(0, s), ring(0, s + 1)))
        for r in range(rings - 1):
            a, b = ring(r, s), ring(r, s + 1)
            c, d = ring(r + 1, s + 1), ring(r + 1, s)
            tris += [(a, d, c), (a, c, b)]
        tris.append((len(co) - 1, ring(rings - 1, s + 1), ring(rings - 1, s)))
    return co, np.array(tris, np.int64)


# Cut and fill: a cone mound, a square frustum pit, and sin x sin over two references.
MOUND = {"R": 4.0, "H": 2.0, "C": (-6.0, 0.0)}
PIT = {"top": 4.0, "bottom": 2.0, "depth": 2.0, "C": (6.0, 0.0)}
WAVE = {"A": 0.5, "L": 8.0}


def mound_pit(x, y):
    R, H, (cx, cy) = MOUND["R"], MOUND["H"], MOUND["C"]
    z = H * np.clip(1 - np.hypot(x - cx, y - cy) / R, 0, None)
    t, b, d, (px, py) = PIT["top"], PIT["bottom"], PIT["depth"], PIT["C"]
    # A square frustum pit: depth falls linearly from the top square to the bottom square.
    m = np.maximum(np.abs(x - px), np.abs(y - py))
    z = z - d * np.clip((t - m) / (t - b), 0, 1)
    return z


def mound_pit_grid(h: float = 0.25) -> Grid:
    return grid(mound_pit, -12.0, -6.0, 12.0, 6.0, h, "mound_pit")


def mound_volume() -> float:
    return math.pi * MOUND["R"] ** 2 * MOUND["H"] / 3


def pit_volume() -> float:
    a0, a1, d = (2 * PIT["top"]) ** 2, (2 * PIT["bottom"]) ** 2, PIT["depth"]
    return d / 3 * (a0 + a1 + math.sqrt(a0 * a1))


def wave(x, y):
    A, L = WAVE["A"], WAVE["L"]
    return A * np.sin(np.pi * x / L) * np.sin(np.pi * y / L)


def wave_grid(h: float = 0.25) -> Grid:
    L = WAVE["L"]
    return grid(wave, -L, -L, L, L, h, "wave")


def wave_fill() -> float:
    """Fill (= cut) of the wave over z = 0: A (2L/π)² over the two positive quadrants."""
    A, L = WAVE["A"], WAVE["L"]
    return A * 2 * (2 * L / math.pi) ** 2


def wave_plane(x, y):
    """The second reference: a tilted plane through the wave."""
    return 0.1 + 0.02 * x


# --- least-cost path cases --------------------------------------------------------------------

TOBLER_KMH, TOBLER_RATE, TOBLER_SHIFT = 6.0, 3.5, 0.05
LCP = {"start": (-28.0, -28.0), "end": (28.0, 26.0), "max_slope": 30.0}


def tobler_time(run, rise):
    """Seconds to walk ``run`` m horizontally rising ``rise`` m (Tobler 1993, as GeoTools)."""
    run = np.asarray(run, np.float64)
    slope = np.asarray(rise, np.float64) / np.maximum(run, 1e-9)
    speed = TOBLER_KMH / 3.6 * np.exp(-TOBLER_RATE * np.abs(slope + TOBLER_SHIFT))
    return run / speed


def grid_edges(g: Grid) -> np.ndarray:
    """The unique edges of the triangulated grid (east, north and north-east diagonals)."""
    t = g.triangles()
    e = np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]])
    e = np.sort(e, axis=1)
    return np.unique(e, axis=0)


def walk_ramp(slope_deg: float, length: float = 50.0, h: float = 1.0) -> Grid:
    """A plane rising along +x at ``slope_deg`` (negative: falling), for r.walk."""
    t = math.tan(math.radians(slope_deg))
    return grid(lambda x, y: x * t, 0.0, -5.0, length, 5.0, h, f"ramp_{slope_deg:g}")


RAMP_SLOPES = (-30.0, -20.0, -13.0, -11.0, -8.0, -2.862, 0.0, 2.862, 8.0, 12.0, 20.0, 30.0)


FAR = (10000.0, 20000.0, 100.0)


def solid(name: str, at=(0.0, 0.0, 0.0)):
    """The closed volume cases by name."""
    if name == "prism":
        return prism(3.0, 2.0, 5.0, at)
    if name == "pyramid":
        return frustum(2.0, 0.0, 3.0, 64, at)
    if name == "frustum":
        return frustum(2.0, 1.0, 2.0, 64, at)
    if name == "hemisphere":
        return hemisphere(2.0, 32, 64, at)
    raise KeyError(name)
