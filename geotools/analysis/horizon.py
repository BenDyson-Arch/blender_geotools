"""The horizon seen from a point: refraction, Earth curvature and the profile.

bpy-free, numpy only. Blender casts the rays (``geotools.blender.archaeo``:
Cycles Raycast nodes in one EMIT bake per pass, as sun hours does); this
module decides which rays and turns their hits into a profile.

How the profile is found. For each azimuth, rays leave the observer's eye at
a fan of elevations in the vertical plane. A ray either misses or stops at
its first hit, a point on the terrain. Every first hit is a terrain point the
observer can see, at a known horizontal distance d and height difference
Δz. On a curved Earth such a point appears at

    e = atan(Δz / d) - (1 - k) d / (2 R)        (radians, small-angle)

with R the Earth's radius and k the coefficient of terrestrial refraction
(the line of sight bends with radius R / k). The skyline is the visible point
with the largest e. The coarse pass casts a fan every :data:`COARSE_STEP`
degrees; the fine pass fills the bracket above a coarse ray with
:data:`FINE_RAYS` more rays, so the skyline is found to about 0.002°.
Curvature never makes a point hidden in the flat scene visible (its drop
grows with distance), so the first hits are enough.

Which brackets. Without curvature the skyline is in the bracket above the
highest coarse ray that hits. With it, a farther range can be hit by the
coarse ray above a near crest and still appear lower than the crest, so the
best coarse hit is not always the one below the skyline. The coarse ray just
below the skyline meets the terrain no farther away than the skyline point,
so it appears less than one :data:`COARSE_STEP` below the skyline;
:func:`fine_brackets` therefore refines every coarse ray that appears within
one step of the best (usually one or two per azimuth).

Distances in the scene are grid metres in a projected CRS; horizontal ones
are divided by the combined scale factor at the observer to give ground
metres (``site``), heights are unchanged. Grid azimuths become true ones by
adding the convergence, as for the sun. The arc-to-chord correction of a
projected line (seconds of arc over tens of kilometres) is ignored.

Refraction of the sky. A celestial body appears higher than it is by the
astronomical refraction. The horizon's altitude h here is apparent (what an
observer would measure), so a body is on the horizon when its true altitude
is h - R(h), with R from Bennett's formula (:func:`refraction`), about 34'
at h = 0 at 1010 hPa and 10 °C at sea level; the pressure is scaled to the
site's height by the standard atmosphere (``sun.standard_pressure``), so 30'
at 1,000 m and 21' at 4,000 m. Real refraction near the horizon varies by
several arc minutes with the weather, more in inversions; that, not the
ephemerides, is the limit of an archaeoastronomical azimuth.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "COARSE_STEP",
    "EARTH_RADIUS",
    "FINE_RAYS",
    "REFRACTION_PRESSURE_HPA",
    "REFRACTION_TEMPERATURE_C",
    "TERRESTRIAL_K",
    "Hits",
    "HorizonProfile",
    "azimuths",
    "best_hits",
    "coarse_elevations",
    "curvature_drop",
    "fine_brackets",
    "fine_elevations",
    "make_profile",
    "merge_rows",
    "ray_directions",
    "refraction",
    "true_altitude",
]

EARTH_RADIUS = 6_371_000.0
"""Mean Earth radius, metres (used for curvature; the profile records it)."""
TERRESTRIAL_K = 0.13
"""Standard coefficient of terrestrial refraction (lines of sight curve with radius R / k)."""
COARSE_STEP = 0.1
"""Degrees between the coarse pass's rays in elevation."""
FINE_RAYS = 64
"""Rays in each azimuth's fine bracket (one coarse step)."""
LOWEST = -10.0
HIGHEST = 60.0
"""The coarse fan, degrees of elevation. A skyline above HIGHEST is reported as closed."""
BENNETT_LOWEST = -1.0
"""Bennett's formula is used down to this apparent altitude and held there below it."""


REFRACTION_PRESSURE_HPA = 1010.0
"""Bennett's sea-level pressure; GeoTools scales it to the site's height
(``sun.standard_pressure(height, REFRACTION_PRESSURE_HPA)``)."""
REFRACTION_TEMPERATURE_C = 10.0


def refraction(
    apparent_deg: np.ndarray | float,
    *,
    pressure_hpa: float = REFRACTION_PRESSURE_HPA,
    temperature_c: float = REFRACTION_TEMPERATURE_C,
) -> np.ndarray:
    """Astronomical refraction (degrees) at apparent altitudes ``apparent_deg``: Bennett (1982).

    R = cot(h + 7.31 / (h + 4.4)) arc minutes, h in degrees, scaled by
    (P / 1010) (283 / (273 + T)) (Meeus eq. 16.4). 34.5' at the horizon,
    good to 0.07' above it. Below -1° (an observer above the terrain around)
    the value at -1° is used.
    """
    h = np.maximum(np.asarray(apparent_deg, dtype=np.float64), BENNETT_LOWEST)
    minutes = 1.0 / np.tan(np.radians(h + 7.31 / (h + 4.4)))
    scale = (float(pressure_hpa) / 1010.0) * (283.0 / (273.0 + float(temperature_c)))
    return minutes * scale / 60.0


def true_altitude(
    apparent_deg: np.ndarray | float,
    refract: bool = True,
    pressure_hpa: float = REFRACTION_PRESSURE_HPA,
) -> np.ndarray:
    """The true (airless) altitude of a body seen at ``apparent_deg``: h - R(h).

    R at ``pressure_hpa`` (the site's, see :func:`.sun.standard_pressure`) and 10 °C.
    """
    h = np.asarray(apparent_deg, dtype=np.float64)
    return h - refraction(h, pressure_hpa=pressure_hpa) if refract else h.copy()


def curvature_drop(
    ground_distance: np.ndarray | float,
    *,
    k: float = TERRESTRIAL_K,
    radius: float = EARTH_RADIUS,
) -> np.ndarray:
    """How far (degrees) curvature lowers a point at ``ground_distance`` metres: (1 - k) d / 2R."""
    d = np.asarray(ground_distance, dtype=np.float64)
    return np.degrees((1.0 - float(k)) * d / (2.0 * float(radius)))


def azimuths(step_deg: float) -> np.ndarray:
    """True azimuths 0, step, 2 step, ... below 360 (the step is fitted to divide 360)."""
    step = float(step_deg)
    if not (math.isfinite(step) and 0.01 <= step <= 30.0):
        raise ValueError(f"The azimuth step must be 0.01 to 30 degrees, not {step_deg!r}.")
    count = round(360.0 / step)
    return np.arange(count, dtype=np.float64) * (360.0 / count)


def coarse_elevations(lowest: float = LOWEST, highest: float = HIGHEST) -> np.ndarray:
    """The coarse fan, degrees of elevation, :data:`COARSE_STEP` apart."""
    count = round((highest - lowest) / COARSE_STEP) + 1
    return lowest + COARSE_STEP * np.arange(count, dtype=np.float64)


def fine_elevations(base: np.ndarray, rays: int = FINE_RAYS) -> np.ndarray:
    """``rays`` elevations per azimuth spread over (base, base + COARSE_STEP], (n_az, rays)."""
    fractions = np.arange(1, rays + 1, dtype=np.float64) / rays
    return np.asarray(base, dtype=np.float64)[:, None] + COARSE_STEP * fractions[None, :]


def ray_directions(scene_azimuth: np.ndarray, elevation: np.ndarray) -> np.ndarray:
    """Unit vectors (n_az * n_el, 3) for each azimuth (clockwise from +Y) and elevation.

    ``elevation`` is (n_el,) shared by every azimuth or (n_az, n_el). The
    azimuth varies slowest.
    """
    az = np.radians(np.asarray(scene_azimuth, dtype=np.float64))
    el = np.radians(np.asarray(elevation, dtype=np.float64))
    if el.ndim == 1:
        el = np.broadcast_to(el[None, :], (len(az), len(el)))
    az = np.broadcast_to(az[:, None], el.shape)
    cos_el = np.cos(el)
    return np.stack([np.sin(az) * cos_el, np.cos(az) * cos_el, np.sin(el)], axis=-1).reshape(-1, 3)


@dataclass(frozen=True, slots=True)
class Hits:
    """The best visible point per azimuth from one or more passes (float64, one per azimuth)."""

    apparent: np.ndarray
    """Apparent altitude of the best point, degrees (curvature and terrestrial refraction)."""
    geometric: np.ndarray
    """Elevation of the ray that found it, in the flat scene, degrees."""
    distance: np.ndarray
    """Along that ray, scene units; NaN where nothing was hit."""


def _apparent(
    elevation: np.ndarray,
    hit: np.ndarray,
    distance: np.ndarray,
    scale_factor: float,
    curvature: bool,
    k: float,
    radius: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(hit, elevation, apparent altitude) as (n_az, n_el) arrays; -inf where nothing was hit."""
    hit = np.asarray(hit, dtype=bool)
    distance = np.asarray(distance, dtype=np.float64)
    el = np.asarray(elevation, dtype=np.float64)
    if el.ndim == 1:
        el = np.broadcast_to(el[None, :], hit.shape)
    el_r = np.radians(el)
    grid_horizontal = distance * np.cos(el_r)
    rise = distance * np.sin(el_r)
    ground = grid_horizontal / float(scale_factor)
    with np.errstate(invalid="ignore"):
        apparent = np.degrees(np.arctan2(rise, ground))
    if curvature:
        apparent = apparent - curvature_drop(ground, k=k, radius=radius)
    return hit, el, np.where(hit, apparent, -np.inf)


def fine_brackets(
    elevation: np.ndarray,
    hit: np.ndarray,
    distance: np.ndarray,
    *,
    scale_factor: float = 1.0,
    curvature: bool = True,
    k: float = TERRESTRIAL_K,
    radius: float = EARTH_RADIUS,
) -> tuple[np.ndarray, np.ndarray]:
    """(azimuth index, coarse elevation) of every bracket the fine pass must fill.

    Arguments as :func:`best_hits` for the coarse pass. A coarse ray's bracket
    can hold the skyline when its hit appears within :data:`COARSE_STEP` of the
    azimuth's best (see the module notes); the indices ascend.
    """
    hit, el, apparent = _apparent(elevation, hit, distance, scale_factor, curvature, k, radius)
    best = apparent.max(axis=1, initial=-np.inf)
    keep = hit & (apparent > best[:, None] - COARSE_STEP * (1.0 + 1e-9))
    rows, cols = np.nonzero(keep)
    return rows, el[rows, cols]


def merge_rows(base: Hits, rows: np.ndarray, extra: Hits) -> Hits:
    """``base`` (one per azimuth) with each ``extra[j]`` (for azimuth ``rows[j]``) where higher."""
    rows = np.asarray(rows, dtype=np.int64)
    top = np.full(len(base.apparent), -np.inf)
    np.maximum.at(top, rows, extra.apparent)
    winner = np.zeros(len(top), dtype=np.int64)
    is_top = extra.apparent == top[rows]
    winner[rows[is_top]] = np.flatnonzero(is_top)
    better = top > base.apparent
    return Hits(
        apparent=np.where(better, top, base.apparent),
        geometric=np.where(better, extra.geometric[winner], base.geometric),
        distance=np.where(better, extra.distance[winner], base.distance),
    )


def best_hits(
    elevation: np.ndarray,
    hit: np.ndarray,
    distance: np.ndarray,
    *,
    scale_factor: float = 1.0,
    curvature: bool = True,
    k: float = TERRESTRIAL_K,
    radius: float = EARTH_RADIUS,
    previous: Hits | None = None,
) -> Hits:
    """The highest-appearing first hit per azimuth, merged with ``previous`` if given.

    ``elevation`` is (n_el,) or (n_az, n_el) degrees in the flat scene;
    ``hit`` and ``distance`` are (n_az, n_el) from the bake. ``scale_factor``
    is the combined scale factor (grid / ground) at the observer.
    """
    hit, el, apparent = _apparent(elevation, hit, distance, scale_factor, curvature, k, radius)
    best = np.argmax(apparent, axis=1)
    rows = np.arange(hit.shape[0])
    found = hit[rows, best]
    result = Hits(
        apparent=np.where(found, apparent[rows, best], -np.inf),
        geometric=np.where(found, el[rows, best], np.nan),
        distance=np.where(found, distance[rows, best], np.nan),
    )
    if previous is None:
        return result
    better = result.apparent > previous.apparent
    return Hits(
        apparent=np.where(better, result.apparent, previous.apparent),
        geometric=np.where(better, result.geometric, previous.geometric),
        distance=np.where(better, result.distance, previous.distance),
    )


@dataclass(frozen=True)
class HorizonProfile:
    """The horizon from one point: apparent altitude per true azimuth.

    ``elevation`` is what an observer at the eye would measure: the skyline's
    altitude, lowered by curvature (when on) and raised by terrestrial
    refraction (when refraction is on). Open azimuths (no terrain in the
    scene) hold ``open_elevation``.
    """

    azimuth: np.ndarray
    """True azimuths, degrees, ascending in [0, 360)."""
    elevation: np.ndarray
    """Apparent horizon altitude, degrees."""
    geometric: np.ndarray
    """The skyline's elevation in the flat scene (no curvature or refraction), degrees; NaN if open."""
    distance: np.ndarray
    """Eye to skyline point along the ray, scene units; NaN if open."""
    open: np.ndarray
    """True where no terrain was hit."""
    edge: np.ndarray
    """True where the skyline point is at the edge of the scene's terrain (likely not the real horizon)."""
    closed: np.ndarray
    """True where the skyline is above the highest ray (:data:`HIGHEST`)."""
    info: dict = field(default_factory=dict)
    """Where it was taken from and how: observer, site, settings (JSON-safe)."""

    def at(self, true_azimuth: np.ndarray | float) -> np.ndarray:
        """Apparent horizon altitude at ``true_azimuth``, interpolated linearly and periodically."""
        az = np.asarray(true_azimuth, dtype=np.float64)
        return np.interp(az % 360.0, self.azimuth, self.elevation, period=360.0)

    def nearest(self, true_azimuth: float) -> int:
        """Index of the sample nearest ``true_azimuth``."""
        diff = np.abs((self.azimuth - float(true_azimuth) + 180.0) % 360.0 - 180.0)
        return int(np.argmin(diff))

    def as_pair(self) -> tuple[np.ndarray, np.ndarray]:
        """(azimuths, elevations): the form ``sun_hours.horizon_elevation`` takes."""
        return self.azimuth.copy(), self.elevation.copy()

    def to_json(self) -> str:
        def floats(values: np.ndarray) -> list:
            return [None if not math.isfinite(v) else float(v) for v in values.tolist()]

        return json.dumps(
            {
                "azimuth": floats(self.azimuth),
                "elevation": floats(self.elevation),
                "geometric": floats(self.geometric),
                "distance": floats(self.distance),
                "open": np.flatnonzero(self.open).tolist(),
                "edge": np.flatnonzero(self.edge).tolist(),
                "closed": np.flatnonzero(self.closed).tolist(),
                "info": self.info,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> HorizonProfile | None:
        if not text:
            return None
        data = json.loads(text)

        def array(name: str) -> np.ndarray:
            return np.array([np.nan if v is None else v for v in data[name]], dtype=np.float64)

        n = len(data["azimuth"])

        def mask(name: str) -> np.ndarray:
            out = np.zeros(n, dtype=bool)
            out[np.asarray(data.get(name, []), dtype=np.int64)] = True
            return out

        return cls(
            azimuth=array("azimuth"),
            elevation=array("elevation"),
            geometric=array("geometric"),
            distance=array("distance"),
            open=mask("open"),
            edge=mask("edge"),
            closed=mask("closed"),
            info=data.get("info", {}),
        )

    def as_text(self) -> str:
        """Tab-separated lines at full precision (for the clipboard)."""
        lines = ["True azimuth (deg)\tApparent altitude (deg)\tDistance (m)\tNote"]
        for i, az in enumerate(self.azimuth.tolist()):
            note = "open" if self.open[i] else "closed" if self.closed[i] else ""
            if self.edge[i]:
                note = "scene edge"
            dist = self.distance[i]
            lines.append(
                f"{az!r}\t{float(self.elevation[i])!r}\t"
                f"{'' if not math.isfinite(dist) else repr(float(dist))}\t{note}"
            )
        return "\n".join(lines)


def make_profile(
    true_azimuth: np.ndarray,
    hits: Hits,
    *,
    open_elevation: float = 0.0,
    closed: np.ndarray | None = None,
    edge: np.ndarray | None = None,
    info: dict | None = None,
) -> HorizonProfile:
    """A profile from the merged hits; azimuths sorted ascending."""
    az = np.asarray(true_azimuth, dtype=np.float64) % 360.0
    order = np.argsort(az, kind="stable")
    is_open = ~np.isfinite(hits.apparent)
    elevation = np.where(is_open, float(open_elevation), hits.apparent)
    n = len(az)
    closed = np.zeros(n, dtype=bool) if closed is None else np.asarray(closed, dtype=bool)
    edge = np.zeros(n, dtype=bool) if edge is None else np.asarray(edge, dtype=bool)
    return HorizonProfile(
        azimuth=az[order],
        elevation=elevation[order],
        geometric=hits.geometric[order],
        distance=hits.distance[order],
        open=is_open[order],
        edge=(edge & ~is_open)[order],
        closed=closed[order],
        info=dict(info or {}),
    )
