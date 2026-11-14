"""Where scene points are on Earth, for analysis: coordinates, north and scale.

bpy-free. The geodesy is pyproj's (PROJ): the inverse projection, the grid
convergence and the grid scale factor come from ``pyproj.Proj.get_factors``,
and ECEF to geodetic from a PROJ transformation. GeoTools adds only
arithmetic: the project offsets, the anchor frame (the same rigid frame
rusty-tiles builds, see ``tests/core/test_probe.py``) and the height factor.

The scene frame, by placement mode:

* SOURCE_CRS: a Blender point (x, y, z) is grid easting ``x + offset E``,
  northing ``y + offset N`` and reduced level ``z + offset A`` (heights in the
  source's vertical datum). Its ellipsoidal height is the RL plus the project's
  height offset, as rusty-tiles places it. +Y is grid north.
* ANCHOR: the scene is a rigid local frame at the anchor: east-north-up after
  the scene offset is added and the heading turns it counter-clockwise. +Y is
  the anchor's north turned by the heading. Local metres are true metres, so
  the scale factor is 1.
* LOCAL: not placed on Earth. Points have only local coordinates; +Y is taken
  as true north for the sun.

North convention: ``convergence`` is the angle from true north to the scene's
+Y (grid north), clockwise positive, in degrees: the sign of PROJ's
``meridian_convergence``. East of a transverse Mercator central meridian it is
positive in the northern hemisphere and negative in the southern. Hence::

    grid bearing = true bearing - convergence
    true bearing = grid bearing + convergence

For an ANCHOR scene it is minus the heading.

Distances: grid distance = scale factor x ellipsoid distance, and ellipsoid
distance = ground distance x R / (R + h), with R the Gaussian mean radius of
the CRS's ellipsoid at the latitude and h the ellipsoidal height. The combined
scale factor is their product, and ground distance = grid distance / combined
scale factor. Along a line the grid scale factor is averaged with Simpson's
rule from its ends and middle, and h is the mean of the ends' heights. That is
the usual survey reduction; it is exact to well under 1 ppm for lines of a
few kilometres.

The scale factor is taken in the line's own direction. Each point carries
the linear map from a grid step to the step it makes on the ellipsoid (true
east and north metres): the CRS's geodetic metric (the ellipsoid's radii of
curvature M and N) times the inverse of the projection's Jacobian (PROJ's
partial derivatives from ``get_factors``). In a conformal projection
(transverse Mercator, Lambert conformal conic, stereographic) that map is a
rotation by the convergence divided by the point scale factor, so the
reduction is the textbook one. In a non-conformal one (Albers equal-area,
Web Mercator, which is conformal only on a sphere) the scale and the turn
from grid to true bearing depend on the direction, and the map gives both.

Precision: Blender stores positions as float32. A local position is read as
exactly that float32 value, then the binary64 offsets are added in binary64,
so E, N and RL carry the full precision of the local position and no more:
float32 has 24 significant bits, so a point 1 km from the scene origin is held
to about 0.06 mm and one 100 km away to about 8 mm.
:func:`float32_resolution` gives that limit, and every reading carries it.

The project CRS reaches pyproj through :func:`crs_input`, the one place that
turns the project's CRS text into what pyproj is given.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import numpy as np

from ..core import crs_catalog, deps
from ..core.placement import Placement, PlacementMode, Vec3

__all__ = [
    "AreaReading",
    "GeodesyUnavailable",
    "Measurement",
    "PointReading",
    "SiteError",
    "SunSite",
    "available",
    "crs_input",
    "face_areas",
    "float32_resolution",
    "line_bearings",
    "measure",
    "read_point",
    "sun_site",
]


class GeodesyUnavailable(RuntimeError):
    """pyproj is not available, so the scene cannot be related to the Earth."""


class SiteError(ValueError):
    """The project cannot answer this: a bad CRS, a geographic CRS, bad input."""


def available() -> bool:
    """True if the bundled pyproj loads and has its PROJ data."""
    return deps.probe_pyproj().available


def crs_input(definition: str) -> str:
    """The project CRS as pyproj is given it: the EPSG code when one is known.

    A plain ``EPSG:nnnn``, which the EPSG picker stores, is normalised. A
    saved GeoTools preset's PROJ string not yet migrated
    (:mod:`geotools.core.crs_migration`) stands in for its EPSG code, which also names the preset's real datum (GDA2020 for MGA).
    Anything else (WKT, a PROJ string) is passed as written.
    """
    return crs_catalog.epsg_code(definition) or (definition or "").strip()


def float32_resolution(local: Vec3 | np.ndarray) -> float:
    """The spacing of float32 values at a local position: the most precise it is held.

    The largest component's spacing (one unit in the last place) in metres.
    """
    values = np.abs(np.asarray(local, dtype=np.float32))
    return float(np.spacing(values).max())


# --- pyproj, cached per CRS ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Grid:
    """A projected CRS and the pyproj objects that relate it to its geodetic CRS."""

    text: str
    proj: Any  # pyproj.Proj: lon/lat -> E/N and get_factors
    inverse: Any  # Transformer: E/N -> lon/lat (always_xy)
    geodetic_name: str
    semi_major: float
    flattening: float
    geographic: bool
    conformal: bool = True
    """Conformal on its own ellipsoid: PROJ's point scale factor and convergence hold."""


def _pyproj() -> Any:
    status = deps.probe_pyproj()
    if not status.available:
        raise GeodesyUnavailable(
            "Needs pyproj, which is not available: "
            f"{status.error or 'pyproj is not installed'}. See About."
        )
    import pyproj

    return pyproj


@lru_cache(maxsize=8)
def _grid(text: str) -> _Grid:
    pyproj = _pyproj()
    try:
        crs = pyproj.CRS.from_user_input(text)
    except pyproj.exceptions.CRSError as exc:
        raise SiteError(f"pyproj does not know the project CRS {_short(text)}: {exc}") from exc
    if crs.is_compound:
        crs = crs.sub_crs_list[0]
    geodetic = crs.geodetic_crs
    if geodetic is None:
        raise SiteError(f"The project CRS {_short(text)} has no geodetic CRS.")
    ellipsoid = crs.ellipsoid
    a = float(ellipsoid.semi_major_metre)
    inverse_flattening = float(ellipsoid.inverse_flattening or 0.0)
    flattening = 1.0 / inverse_flattening if inverse_flattening > 0 else 0.0
    if crs.is_geographic:
        return _Grid(text, None, None, geodetic.name, a, flattening, True)
    if not crs.is_projected:
        raise SiteError(f"The project CRS {_short(text)} is not a projected or geographic CRS.")
    try:
        proj = pyproj.Proj(crs)
        inverse = pyproj.Transformer.from_crs(crs, geodetic, always_xy=True)
    except pyproj.exceptions.ProjError as exc:
        raise SiteError(f"PROJ cannot use the project CRS {_short(text)}: {exc}") from exc
    grid = _Grid(text, proj, inverse, geodetic.name, a, flattening, False)
    return replace(grid, conformal=_is_conformal(grid, crs))


def _is_conformal(grid: _Grid, crs: Any) -> bool:
    """Whether the projection is conformal on the CRS's ellipsoid, tested once.

    At the centre of the CRS's area of use (or its natural origin): PROJ's
    Jacobian against the ellipsoid's metric must be its conformal map (point
    scale factor and convergence) to :data:`_CONFORMAL_TOLERANCE`. Transverse
    Mercator, Lambert conformal conic and the stereographics pass to about
    1e-10; Albers and Web Mercator fail by 1e-3 or more.
    """
    area = crs.area_of_use
    if area is not None:
        east = area.east + 360.0 if area.east < area.west else area.east
        lon = math.remainder((area.west + east) / 2.0, 360.0)
        lat = (area.south + area.north) / 2.0
    else:
        lon = lat = 0.0
    lat = max(-80.0, min(80.0, lat))  # PROJ's numerical derivatives fail at a pole
    factors = grid.proj.get_factors(lon, lat, errcheck=False)
    convergence = np.atleast_1d(np.asarray(factors.meridian_convergence, dtype=np.float64))
    jacobian = _jacobian_map(grid, factors, np.array([lat]))
    conformal = _conformal_map(factors, convergence)
    with np.errstate(invalid="ignore"):
        error = float(np.abs(jacobian - conformal).max() * float(factors.meridional_scale))
    return not error > _CONFORMAL_TOLERANCE


@lru_cache(maxsize=1)
def _ecef_to_wgs84() -> Any:
    return _pyproj().Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)


def _short(text: str, limit: int = 60) -> str:
    flat = " ".join(str(text).split())
    return repr(flat if len(flat) <= limit else flat[: limit - 3] + "...")


def _gaussian_radius(a: float, f: float, lat: np.ndarray) -> np.ndarray:
    """sqrt(M N): the ellipsoid's mean radius of curvature at ``lat`` (degrees)."""
    e2 = f * (2.0 - f)
    s = np.sin(np.radians(lat))
    w2 = 1.0 - e2 * s * s
    return a * np.sqrt(1.0 - e2) / w2


# --- the scene frame --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Located:
    """Points of the scene on Earth, as arrays (one value per point)."""

    east: np.ndarray  # grid E (SOURCE_CRS), ENU east from the anchor (ANCHOR), local x
    north: np.ndarray
    level: np.ndarray  # RL (SOURCE_CRS), ENU up from the anchor (ANCHOR), local z
    height: np.ndarray | None  # ellipsoidal, metres; None for LOCAL
    lon: np.ndarray | None
    lat: np.ndarray | None
    convergence: np.ndarray  # degrees, true north to scene +Y, clockwise
    grid_scale: np.ndarray  # grid / ellipsoid along grid north (+Y)
    radius: np.ndarray | None  # Gaussian radius for the height factor; None: no height factor
    geodetic_name: str | None
    to_ellipsoid: np.ndarray | None = None
    """(N, 2, 2): a grid step (dE, dN) to the step on the ellipsoid (true east, north
    metres), SOURCE_CRS projected only; None where scene metres are true metres."""

    def line_scale(self, d_east: float, d_north: float) -> np.ndarray:
        """Grid / ellipsoid length of a short step in the direction (d_east, d_north)."""
        if self.to_ellipsoid is None:
            return self.grid_scale
        length = math.hypot(d_east, d_north)
        if length == 0.0:
            return self.grid_scale
        step = np.array([d_east, d_north]) / length
        return 1.0 / np.linalg.norm(self.to_ellipsoid @ step, axis=1)

    def true_bearing(self, d_east: np.ndarray, d_north: np.ndarray) -> np.ndarray:
        """True bearing (degrees, [0, 360)) of grid steps, one per point; NaN for no step."""
        d_east = np.asarray(d_east, dtype=np.float64)
        d_north = np.asarray(d_north, dtype=np.float64)
        with np.errstate(invalid="ignore"):
            if self.to_ellipsoid is None:
                grid = np.degrees(np.arctan2(d_east, d_north))
                bearing = grid + self.convergence
            else:
                g = self.to_ellipsoid
                east = g[:, 0, 0] * d_east + g[:, 0, 1] * d_north
                north = g[:, 1, 0] * d_east + g[:, 1, 1] * d_north
                bearing = np.degrees(np.arctan2(east, north))
        return np.where(np.hypot(d_east, d_north) > 0, bearing % 360.0, np.nan)


def _points(local: Any) -> np.ndarray:
    try:
        points = np.asarray(local, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise SiteError(f"points must be numbers: {exc}") from exc
    points = np.atleast_2d(points)
    if points.ndim != 2 or points.shape[1] != 3:
        raise SiteError(f"points must have shape (N, 3), not {points.shape}")
    if not np.isfinite(points).all():
        raise SiteError("points must be finite")
    return points


def _anchor_enu(placement: Placement, points: np.ndarray) -> np.ndarray:
    """Local points to east-north-up metres at the anchor (offset, then heading)."""
    moved = points + np.asarray(placement.scene_offset, dtype=np.float64)
    t = math.radians(float(placement.heading_degrees))
    c, s = math.cos(t), math.sin(t)
    return np.stack(
        [c * moved[:, 0] - s * moved[:, 1], s * moved[:, 0] + c * moved[:, 1], moved[:, 2]],
        axis=1,
    )


def _locate(placement: Placement, local: Any, *, need_geodetic: bool = True) -> _Located:
    mode = PlacementMode(placement.mode)
    points = _points(local)
    n = len(points)
    zeros = np.zeros(n)
    ones = np.ones(n)
    if mode is PlacementMode.LOCAL:
        return _Located(
            points[:, 0], points[:, 1], points[:, 2], None, None, None, zeros, ones, None, None
        )
    if mode is PlacementMode.ANCHOR:
        enu = _anchor_enu(placement, points)
        lon = lat = height = None
        if need_geodetic:
            lon, lat, height = _anchor_geodetic(placement, enu)
        convergence = np.full(n, -float(placement.heading_degrees))
        return _Located(
            enu[:, 0], enu[:, 1], enu[:, 2], height, lon, lat, convergence, ones, None, "WGS 84"
        )
    grid = _grid(crs_input(placement.source_crs))
    offset = np.asarray(placement.source_offset, dtype=np.float64)
    east = points[:, 0] + offset[0]
    north = points[:, 1] + offset[1]
    level = points[:, 2] + offset[2]
    height = level + float(placement.height_offset)
    if grid.geographic:
        # Coordinates are degrees: no grid, so no convergence or scale.
        return _Located(
            east, north, level, height, east, north, zeros, ones, None, grid.geodetic_name
        )
    lon, lat = grid.inverse.transform(east, north, errcheck=False)
    lon, lat = np.asarray(lon, dtype=np.float64), np.asarray(lat, dtype=np.float64)
    if not (np.isfinite(lon).all() and np.isfinite(lat).all()):
        raise SiteError(
            "Some points are outside the project CRS (PROJ cannot invert them); check the "
            "offset and the CRS."
        )
    factors = grid.proj.get_factors(lon, lat, errcheck=False)
    convergence = np.atleast_1d(np.asarray(factors.meridian_convergence, dtype=np.float64))
    to_ellipsoid = _to_ellipsoid(grid, factors, lat, convergence)
    # Along grid north: the meridional scale factor of a conformal projection.
    scale = 1.0 / np.linalg.norm(to_ellipsoid[:, :, 1], axis=1)
    radius = _gaussian_radius(grid.semi_major, grid.flattening, lat)
    return _Located(
        east,
        north,
        level,
        height,
        lon,
        lat,
        convergence,
        scale,
        radius,
        grid.geodetic_name,
        to_ellipsoid,
    )


_CONFORMAL_TOLERANCE = 1e-7
"""Largest difference (relative) between the Jacobian's map and PROJ's conformal one
that still counts as conformal. Conformal grids differ by about 1e-10 (PROJ takes its
derivatives numerically); Web Mercator differs by about 3e-3 and Albers by more."""


def _to_ellipsoid(
    grid: _Grid, factors: Any, lat: np.ndarray, convergence: np.ndarray
) -> np.ndarray:
    """(N, 2, 2) maps from a grid step to the step on the ellipsoid (true east, north metres).

    A conformal grid: PROJ's point scale factor and convergence. Otherwise the
    Jacobian's map (:func:`_jacobian_map`), with the conformal one standing in
    where the Jacobian is singular (a pole).
    """
    conformal = _conformal_map(factors, convergence)
    if grid.conformal:
        return conformal
    out = _jacobian_map(grid, factors, lat)
    bad = ~np.isfinite(out).all(axis=(1, 2))
    out[bad] = conformal[bad]
    return out


def _conformal_map(factors: Any, convergence: np.ndarray) -> np.ndarray:
    """A grid step turned clockwise by the convergence onto true east/north, divided by k."""
    k = np.broadcast_to(
        np.atleast_1d(np.asarray(factors.meridional_scale, dtype=np.float64)), convergence.shape
    )
    g = np.radians(convergence)
    c, s = np.cos(g) / k, np.sin(g) / k
    return np.stack([np.stack([c, s], -1), np.stack([-s, c], -1)], -2)


def _jacobian_map(grid: _Grid, factors: Any, lat: np.ndarray) -> np.ndarray:
    """metric @ inverse(J): the grid-to-ellipsoid map of any projection.

    PROJ's partial derivatives are of the projection's normalised coordinates,
    so times the semi-major axis they are grid metres per radian of longitude
    and latitude: the Jacobian J. The ellipsoid's metric is diag(N cos(lat), M)
    metres per radian. NaN where J is singular.
    """

    def column(values: Any) -> np.ndarray:
        return np.atleast_1d(np.asarray(values, dtype=np.float64)) * grid.semi_major

    x_lam, x_phi = column(factors.dx_dlam), column(factors.dx_dphi)
    y_lam, y_phi = column(factors.dy_dlam), column(factors.dy_dphi)
    e2 = grid.flattening * (2.0 - grid.flattening)
    phi = np.radians(lat)
    w2 = 1.0 - e2 * np.sin(phi) ** 2
    m = grid.semi_major * (1.0 - e2) / w2**1.5
    n = grid.semi_major / np.sqrt(w2)
    det = x_lam * y_phi - x_phi * y_lam
    out = np.empty((len(det), 2, 2))
    with np.errstate(divide="ignore", invalid="ignore"):
        east_per = np.where(det != 0.0, n * np.cos(phi) / det, np.nan)
        north_per = np.where(det != 0.0, m / det, np.nan)
        out[:, 0, 0] = east_per * y_phi
        out[:, 0, 1] = -east_per * x_phi
        out[:, 1, 0] = -north_per * y_lam
        out[:, 1, 1] = north_per * x_lam
    return out


def _anchor_geodetic(placement: Placement, enu: np.ndarray) -> tuple[np.ndarray, ...]:
    """WGS84 longitude, latitude and ellipsoidal height of anchor ENU points."""
    from ..core.georef import ecef_from_lon_lat_height

    transformer = _ecef_to_wgs84()
    lon0, lat0, h0 = (float(v) for v in placement.anchor)
    origin = ecef_from_lon_lat_height(lon0, lat0, h0)
    lo, la = math.radians(lon0), math.radians(lat0)
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    ecef = origin + enu @ np.stack([east, north, up])
    lon, lat, h = transformer.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2], errcheck=False)
    return (
        np.atleast_1d(np.asarray(lon, dtype=np.float64)),
        np.atleast_1d(np.asarray(lat, dtype=np.float64)),
        np.atleast_1d(np.asarray(h, dtype=np.float64)),
    )


# --- point readout ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PointReading:
    """One scene point on Earth. All numbers are binary64; None when not defined."""

    mode: PlacementMode
    local: Vec3
    """The Blender world position, exactly as its float32 value."""
    resolution: float
    """float32 spacing at ``local``, metres: E, N and RL are no more precise than this."""
    east: float
    """Grid easting (SOURCE_CRS); east of the anchor in its ENU frame (ANCHOR); local X."""
    north: float
    level: float
    """Reduced level, ``z + offset A`` (SOURCE_CRS); up from the anchor (ANCHOR); local Z."""
    height: float | None
    """Ellipsoidal height, metres."""
    longitude: float | None
    latitude: float | None
    geodetic_crs: str | None
    """The datum of the latitude and longitude, e.g. "GDA2020"."""
    convergence: float
    """Degrees from true north to the scene's +Y, clockwise."""
    grid_scale: float
    """Grid / ellipsoid length along the scene's +Y (grid north): the point scale factor
    in a conformal grid. In a non-conformal one (Albers, Web Mercator) the scale
    depends on direction, and :func:`measure` uses the line's own."""
    height_factor: float

    @property
    def combined_scale(self) -> float:
        return self.grid_scale * self.height_factor

    def as_text(self) -> str:
        """Every value at full precision, one per line (for the clipboard)."""
        names = {
            PlacementMode.SOURCE_CRS: ("E", "N", "RL"),
            PlacementMode.ANCHOR: ("Anchor E", "Anchor N", "Anchor U"),
            PlacementMode.LOCAL: ("X", "Y", "Z"),
        }[self.mode]
        lines = [
            f"{name}\t{value!r}"
            for name, value in zip(names, (self.east, self.north, self.level), strict=True)
        ]
        if self.height is not None:
            lines.append(f"Ellipsoidal height\t{self.height!r}")
        if self.latitude is not None and self.longitude is not None:
            lines.append(f"Latitude ({self.geodetic_crs})\t{self.latitude!r}")
            lines.append(f"Longitude ({self.geodetic_crs})\t{self.longitude!r}")
        if self.mode is not PlacementMode.LOCAL:
            lines.append(f"Convergence (deg)\t{self.convergence!r}")
            lines.append(f"Combined scale factor\t{self.combined_scale!r}")
        lines.append("Local\t" + ", ".join(repr(v) for v in self.local))
        lines.append(f"Local float32 resolution (m)\t{self.resolution!r}")
        return "\n".join(lines)


def _height_factor(located: _Located) -> np.ndarray:
    if located.radius is None or located.height is None:
        return np.ones_like(located.east)
    return located.radius / (located.radius + located.height)


def read_point(placement: Placement, local: Vec3, *, geodetic: bool = True) -> PointReading:
    """Read one Blender world position (float32 values) on Earth.

    Raises :class:`GeodesyUnavailable` without pyproj for a SOURCE_CRS project
    (and for an ANCHOR project's latitude and longitude, unless ``geodetic`` is
    False), and :class:`SiteError` for an unusable CRS or point.
    """
    as_float32 = np.asarray(local, dtype=np.float32).astype(np.float64)
    located = _locate(placement, as_float32[None, :], need_geodetic=geodetic)

    def one(values: np.ndarray | None) -> float | None:
        return None if values is None else float(values[0])

    mode = PlacementMode(placement.mode)
    height = one(located.height)
    if mode is PlacementMode.ANCHOR and height is None:
        height = float(placement.anchor[2]) + float(located.level[0])  # tangent plane
    return PointReading(
        mode=mode,
        local=(float(as_float32[0]), float(as_float32[1]), float(as_float32[2])),
        resolution=float32_resolution(as_float32),
        east=float(located.east[0]),
        north=float(located.north[0]),
        level=float(located.level[0]),
        height=height,
        longitude=one(located.lon),
        latitude=one(located.lat),
        geodetic_crs=located.geodetic_name if located.lon is not None else None,
        convergence=float(located.convergence[0]),
        grid_scale=float(located.grid_scale[0]),
        height_factor=float(_height_factor(located)[0]),
    )


# --- distance ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measurement:
    """From point A to point B. Distances in metres (grid ones in CRS units)."""

    grid_distance: float
    """Horizontal distance on the grid (the plan distance in the scene)."""
    ground_distance: float
    """Horizontal distance at ground level: grid distance / combined scale factor."""
    height_difference: float
    """B's level minus A's."""
    slope_distance: float
    """Ground horizontal distance and height difference combined."""
    grid_bearing: float
    """Degrees clockwise from the scene's +Y (grid north), in [0, 360)."""
    true_bearing: float
    """The line's direction at its middle from true north, in [0, 360): the grid bearing
    plus the convergence there in a conformal grid."""
    grid_scale: float
    """Grid scale factor along the line, in its direction (Simpson's rule)."""
    height_factor: float
    combined_scale: float
    resolution: float
    """The coarser float32 spacing of the two local positions, metres."""

    def as_text(self) -> str:
        rows = (
            ("Grid distance (m)", self.grid_distance),
            ("Ground distance (m)", self.ground_distance),
            ("Height difference (m)", self.height_difference),
            ("Slope distance (m)", self.slope_distance),
            ("Grid bearing (deg)", self.grid_bearing),
            ("True bearing (deg)", self.true_bearing),
            ("Grid scale factor", self.grid_scale),
            ("Height factor", self.height_factor),
            ("Combined scale factor", self.combined_scale),
            ("Local float32 resolution (m)", self.resolution),
        )
        return "\n".join(f"{name}\t{value!r}" for name, value in rows)


def measure(placement: Placement, a: Vec3, b: Vec3) -> Measurement:
    """Grid and ground distance from ``a`` to ``b`` (Blender world positions).

    Raises :class:`GeodesyUnavailable` without pyproj for a SOURCE_CRS project
    and :class:`SiteError` for a geographic project CRS, whose coordinates are
    degrees.
    """
    ends = np.asarray([a, b], dtype=np.float32).astype(np.float64)
    middle = ends.mean(axis=0)
    located = _locate(placement, np.vstack([ends, middle]), need_geodetic=False)
    if located.radius is None and PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS:
        raise SiteError(
            "The project CRS is geographic: scene coordinates are degrees, so distances "
            "cannot be measured. Use a projected CRS."
        )
    # In the scene's own axes: +Y is grid north (SOURCE_CRS) or the anchor's heading.
    d_east, d_north, d_level = (float(v) for v in ends[1] - ends[0])
    grid = math.hypot(d_east, d_north)
    k = located.line_scale(d_east, d_north)  # in the line's direction
    grid_scale = float((k[0] + 4.0 * k[2] + k[1]) / 6.0)
    if located.radius is not None and located.height is not None:
        radius = float(located.radius[2])
        height = float((located.height[0] + located.height[1]) / 2.0)
        height_factor = radius / (radius + height)
    else:
        height_factor = 1.0
    combined = grid_scale * height_factor
    ground = grid / combined
    bearing = math.degrees(math.atan2(d_east, d_north)) % 360.0 if grid > 0 else 0.0
    if grid > 0 and located.to_ellipsoid is not None:
        true_bearing = float(located.true_bearing(np.full(3, d_east), np.full(3, d_north))[2])
    else:
        true_bearing = (bearing + float(located.convergence[2])) % 360.0
    return Measurement(
        grid_distance=grid,
        ground_distance=ground,
        height_difference=d_level,
        slope_distance=math.hypot(ground, d_level),
        grid_bearing=bearing,
        true_bearing=true_bearing,
        grid_scale=grid_scale,
        height_factor=height_factor,
        combined_scale=combined,
        resolution=max(float32_resolution(ends[0]), float32_resolution(ends[1])),
    )


def line_bearings(
    placement: Placement,
    starts: Any,
    ends: Any,
    *,
    local_lon_lat: tuple[float, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """True bearings (degrees, [0, 360)) and latitudes of lines from ``starts`` to ``ends``.

    Both are (N, 3) Blender world positions. The grid bearing in the scene
    plus the convergence at each line's middle, and the latitude there.
    Zero-length lines give NaN. SOURCE_CRS needs pyproj; ANCHOR uses the
    anchor's latitude (the frame is its tangent plane); LOCAL takes
    ``local_lon_lat`` (longitude, latitude) with +Y as true north.
    """
    a = _points(np.asarray(starts, dtype=np.float32).astype(np.float64))
    b = _points(np.asarray(ends, dtype=np.float32).astype(np.float64))
    if a.shape != b.shape:
        raise SiteError("starts and ends must have the same shape")
    d = b - a
    with np.errstate(invalid="ignore"):
        grid = np.degrees(np.arctan2(d[:, 0], d[:, 1]))
    grid = np.where(np.hypot(d[:, 0], d[:, 1]) > 0, grid, np.nan)
    mode = PlacementMode(placement.mode)
    n = len(a)
    if mode is PlacementMode.LOCAL:
        if local_lon_lat is None:
            raise SiteError("A local project needs a latitude and longitude.")
        lon, lat = (float(v) for v in local_lon_lat)
        _check_lon_lat(lon, lat)
        return grid % 360.0, np.full(n, lat)
    if mode is PlacementMode.ANCHOR:
        lon, lat, _h = (float(v) for v in placement.anchor)
        _check_lon_lat(lon, lat)
        return (grid - float(placement.heading_degrees)) % 360.0, np.full(n, lat)
    located = _locate(placement, (a + b) / 2.0)
    if located.lat is None:
        raise SiteError("The project CRS has no latitude.")
    if located.to_ellipsoid is not None:
        true = located.true_bearing(d[:, 0], d[:, 1])
    else:
        true = (grid + located.convergence) % 360.0
    return true, np.asarray(located.lat, dtype=np.float64)


# --- area -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AreaReading:
    """The area of some triangles, in square metres (grid ones in CRS units)."""

    triangles: int
    grid_surface: float
    """3D area of the triangles as they are in the scene."""
    grid_plan: float
    """Area projected onto the horizontal (XY) plane."""
    ground_surface: float
    """3D area with each triangle's horizontal extent at ground level (divided by its CSF
    in a conformal grid), its vertical extent unchanged."""
    ground_plan: float
    """Horizontal area at ground level: plan area / CSF squared, per triangle (in a
    non-conformal grid, times the area scale of the grid-to-ground map)."""

    def as_text(self) -> str:
        rows = (
            ("Triangles", self.triangles),
            ("Grid surface area (m2)", self.grid_surface),
            ("Grid plan area (m2)", self.grid_plan),
            ("Ground surface area (m2)", self.ground_surface),
            ("Ground plan area (m2)", self.ground_plan),
        )
        return "\n".join(f"{name}\t{value!r}" for name, value in rows)


def face_areas(placement: Placement, triangles: np.ndarray) -> AreaReading:
    """Grid and ground areas of triangles (T, 3, 3) in Blender world coordinates.

    One grid-to-ground map per triangle, at its centroid (the combined scale
    factor in a conformal grid), all vectorised (about a second per million
    triangles in PROJ). Raises like :func:`measure`.
    """
    tris = np.asarray(triangles, dtype=np.float64)
    if tris.ndim != 3 or tris.shape[1:] != (3, 3):
        raise SiteError(f"triangles must have shape (T, 3, 3), not {tris.shape}")
    if len(tris) == 0:
        return AreaReading(0, 0.0, 0.0, 0.0, 0.0)
    centroids = tris.mean(axis=1)
    located = _locate(placement, centroids, need_geodetic=False)
    if located.radius is None and PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS:
        raise SiteError(
            "The project CRS is geographic: scene coordinates are degrees, so areas cannot "
            "be measured. Use a projected CRS."
        )
    height_factor = _height_factor(located)
    u = tris[:, 1] - tris[:, 0]
    v = tris[:, 2] - tris[:, 0]
    cross = np.cross(u, v)
    grid_surface = 0.5 * np.linalg.norm(cross, axis=1)
    grid_plan = 0.5 * np.abs(cross[:, 2])
    # Ground: horizontal components taken to ground metres, vertical unchanged.
    # Conformal grids: divided by the CSF (and turned by the convergence, which
    # changes no area). Otherwise the full grid-to-ellipsoid map, per triangle.
    if located.to_ellipsoid is not None:
        to_ground = located.to_ellipsoid / height_factor[:, None, None]
    else:
        to_ground = np.eye(2)[None, :, :] / (located.grid_scale * height_factor)[:, None, None]
    ground_u = np.concatenate([np.einsum("tij,tj->ti", to_ground, u[:, :2]), u[:, 2:]], axis=1)
    ground_v = np.concatenate([np.einsum("tij,tj->ti", to_ground, v[:, :2]), v[:, 2:]], axis=1)
    ground_surface = 0.5 * np.linalg.norm(np.cross(ground_u, ground_v), axis=1)
    plan_scale = np.abs(np.linalg.det(to_ground))
    return AreaReading(
        triangles=len(tris),
        grid_surface=float(grid_surface.sum()),
        grid_plan=float(grid_plan.sum()),
        ground_surface=float(ground_surface.sum()),
        ground_plan=float((grid_plan * plan_scale).sum()),
    )


# --- the sun's site ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SunSite:
    """Where the sun is seen from, and how the scene's +Y relates to true north."""

    longitude: float
    latitude: float
    height: float
    """Ellipsoidal metres (only the SPA's tiny parallax term uses it)."""
    convergence: float
    """Degrees from true north to the scene's +Y, clockwise: subtract from true azimuths."""
    north: str
    """What the scene's +Y is: "grid north", "anchor heading" or "true north"."""
    geodetic_crs: str | None


def sun_site(
    placement: Placement,
    local: Vec3 = (0.0, 0.0, 0.0),
    *,
    local_lon_lat: tuple[float, float] | None = None,
) -> SunSite:
    """The sun's site for a project, at the Blender world position ``local``.

    SOURCE_CRS: the point's latitude and longitude and the grid convergence
    there (needs pyproj). ANCHOR: the anchor's latitude and longitude, and the
    heading (no pyproj needed: the frame is the anchor's tangent plane). LOCAL:
    ``local_lon_lat`` (longitude, latitude), with +Y taken as true north.
    """
    mode = PlacementMode(placement.mode)
    if mode is PlacementMode.LOCAL:
        if local_lon_lat is None:
            raise SiteError("A local project needs a latitude and longitude for the sun.")
        lon, lat = (float(v) for v in local_lon_lat)
        _check_lon_lat(lon, lat)
        return SunSite(lon, lat, 0.0, 0.0, "true north", None)
    if mode is PlacementMode.ANCHOR:
        lon, lat, height = (float(v) for v in placement.anchor)
        _check_lon_lat(lon, lat)
        return SunSite(
            lon, lat, height, -float(placement.heading_degrees), "anchor heading", "WGS 84"
        )
    located = _locate(placement, np.asarray(local, dtype=np.float32).astype(np.float64)[None, :])
    lon, lat = float(located.lon[0]), float(located.lat[0])  # type: ignore[index]
    _check_lon_lat(lon, lat)
    return SunSite(
        lon,
        lat,
        float(located.height[0]),  # type: ignore[index]
        float(located.convergence[0]),
        "grid north" if located.radius is not None else "true north",
        located.geodetic_name,
    )


def _check_lon_lat(lon: float, lat: float) -> None:
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise SiteError(f"The site is not on Earth: longitude {lon!r}, latitude {lat!r}.")
