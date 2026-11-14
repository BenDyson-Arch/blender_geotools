"""Sun hours and insolation: the time steps, the sun's rays and how they add up.

bpy-free, numpy only. Blender casts the rays (``geotools.blender.sun_hours``:
Cycles Raycast nodes in an EMIT bake); this module decides which rays, with
what weights, and defines what the bake sums, so it can be tested with plain
pytest. See ``docs/design/m5-analysis.md``.

The pipeline:

1. :func:`period_steps`: a day, a date range or a year, from local midnight
   of the first day to local midnight after the last, cut into steps of a
   chosen length. Each step is represented by its midpoint (the midpoint rule:
   the error of a sum of on/off steps is at most about half a step at each
   sunrise, sunset and shadow edge, and averages out to much less).
2. :func:`sun_steps`: NREL SPA at each midpoint. Only steps with the sun's
   centre above the horizon (apparent elevation > 0, or above a horizon
   profile) count. Directions are turned from true north to the scene's +Y by
   the grid convergence (``site.sun_site``).
3. :func:`group_directions` (optional): sun directions closer than a
   tolerance share one ray, with their weights summed. Shadows are only as
   sharp as that tolerance, but a year collapses from thousands of rays per
   sample to a few hundred.
4. :func:`build_rays`: the rays and two weights per ray. ``hours`` adds up
   the time the sample sees the sun. ``energy`` (Wh/m² per unit cosine) adds
   up direct-normal irradiance x time x cos(incidence), from the clear-sky
   model (:mod:`.clearsky`). An optional isotropic diffuse term adds rays to
   a fixed set of sky directions (:func:`sky_directions`), weighted so that
   their sum is the diffuse horizontal irradiation times the sky-view factor
   seen by the surface.

:func:`accumulate` is the reference of what a sample gets from a visibility
matrix; the Blender shader computes exactly this per sample.
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

import numpy as np

from . import clearsky, sun

__all__ = [
    "MAX_STEPS",
    "Rays",
    "SunSteps",
    "TimeSteps",
    "accumulate",
    "build_rays",
    "group_directions",
    "horizon_elevation",
    "period_steps",
    "ray_offset",
    "season_moments",
    "sky_directions",
    "sun_steps",
]

MAX_STEPS = 2_000_000
"""More time steps than this is refused (a year at 15 s steps)."""


@dataclass(frozen=True, slots=True)
class TimeSteps:
    """Time steps over a period: midpoints (Unix seconds, UTC) and lengths (seconds)."""

    times: np.ndarray
    durations: np.ndarray
    start_utc: _dt.datetime
    end_utc: _dt.datetime
    notes: tuple[str, ...] = ()

    @property
    def hours(self) -> float:
        """Length of the period, hours."""
        return (self.end_utc - self.start_utc).total_seconds() / 3600.0


def period_steps(
    first_day: _dt.date,
    last_day: _dt.date,
    tz: _dt.tzinfo,
    step_minutes: float,
) -> TimeSteps:
    """Steps from local midnight of ``first_day`` to local midnight after ``last_day``.

    Local days follow ``tz``, so a daylight-saving change makes that day 23 or
    25 hours long, as it is. Steps are spaced evenly in UTC; the last one is
    shortened to end exactly at the end of the period.
    """
    if last_day < first_day:
        raise ValueError("The last day is before the first.")
    step = float(step_minutes) * 60.0
    if not (math.isfinite(step) and step > 0):
        raise ValueError(f"The step must be positive, not {step_minutes!r} minutes.")
    start_local = _dt.datetime(first_day.year, first_day.month, first_day.day)
    end_local = _dt.datetime(last_day.year, last_day.month, last_day.day) + _dt.timedelta(days=1)
    start_utc, notes = sun.to_utc(start_local, tz)
    end_utc, end_notes = sun.to_utc(end_local, tz)
    length = (end_utc - start_utc).total_seconds()
    count = math.ceil(length / step - 1e-9)
    if count > MAX_STEPS:
        raise ValueError(f"{count:,} time steps is too many; use a longer step.")
    edges = np.minimum(np.arange(count + 1, dtype=np.float64) * step, length)
    durations = np.diff(edges)
    times = start_utc.timestamp() + 0.5 * (edges[:-1] + edges[1:])
    return TimeSteps(times, durations, start_utc, end_utc, (*notes, *end_notes))


def horizon_elevation(
    azimuth_deg: np.ndarray, profile: float | tuple[np.ndarray, np.ndarray] | None
) -> np.ndarray:
    """The horizon's elevation (degrees) at true azimuths ``azimuth_deg``.

    ``profile`` is None (a flat horizon at 0), one elevation for every
    azimuth, or ``(azimuths, elevations)`` in degrees (true azimuths, any
    order), interpolated linearly and periodically in azimuth.
    """
    az = np.asarray(azimuth_deg, dtype=np.float64)
    if profile is None:
        return np.zeros_like(az)
    if np.isscalar(profile):
        return np.full_like(az, float(profile))
    azimuths, elevations = (np.asarray(v, dtype=np.float64).ravel() for v in profile)
    if azimuths.shape != elevations.shape or azimuths.size == 0:
        raise ValueError("A horizon profile needs as many elevations as azimuths.")
    if not (np.isfinite(azimuths).all() and np.isfinite(elevations).all()):
        raise ValueError("A horizon profile must be finite.")
    return np.interp(az % 360.0, azimuths % 360.0, elevations, period=360.0)


@dataclass(frozen=True, slots=True)
class SunSteps:
    """The steps with the sun up, and where it is (float64 arrays, one per step)."""

    directions: np.ndarray
    """(N, 3) unit vectors towards the sun in scene axes (+Y the scene's north)."""
    hours: np.ndarray
    """Length of each step, hours."""
    azimuth: np.ndarray
    """True azimuth, degrees."""
    elevation: np.ndarray
    """Apparent elevation, degrees."""
    distance_au: np.ndarray
    total_steps: int
    """Steps in the period, sun up or not."""
    below_profile: int
    """Steps with the sun above the horizon but below the horizon profile."""


def sun_steps(
    steps: TimeSteps,
    latitude: float,
    longitude: float,
    height: float = 0.0,
    convergence: float = 0.0,
    *,
    refraction: bool = True,
    horizon: float | tuple[np.ndarray, np.ndarray] | None = None,
) -> SunSteps:
    """SPA at each step; keep the steps with the sun's centre above the horizon.

    ``convergence`` (degrees, true north to the scene's +Y, clockwise) is
    subtracted from the azimuths, as for a Sun light. ``horizon`` raises the
    horizon (see :func:`horizon_elevation`); only steps above both it and 0°
    count. Refraction is at the standard-atmosphere pressure for ``height``
    (:func:`.sun.standard_pressure`).
    """
    position = sun.solar_position(
        steps.times,
        latitude,
        longitude,
        height,
        pressure_hpa=sun.standard_pressure(height),
        refraction=refraction,
    )
    up = position.elevation > 0.0
    above = up & (position.elevation > horizon_elevation(position.azimuth, horizon))
    keep = np.flatnonzero(above)
    azimuth = position.azimuth[keep]
    elevation = position.elevation[keep]
    return SunSteps(
        directions=sun.direction((azimuth - float(convergence)) % 360.0, elevation),
        hours=steps.durations[keep] / 3600.0,
        azimuth=azimuth,
        elevation=elevation,
        distance_au=position.distance_au[keep],
        total_steps=len(steps.times),
        below_profile=int(np.count_nonzero(up & ~above)),
    )


def group_directions(
    directions: np.ndarray, weights: np.ndarray, tolerance_deg: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """Merge directions that fall in the same cell of a sky grid ``tolerance_deg`` wide.

    The grid has elevation bands ``tolerance_deg`` high, each cut into
    azimuth cells about ``tolerance_deg`` wide on the sky (fewer cells nearer
    the zenith). A cell's direction is the mean of its members weighted by the
    first column of ``weights``; the weights (``(N,)`` or ``(N, k)``) are
    summed, so totals are kept exactly.

    Returns ``(directions, weights, group, max_deviation_deg)``: the merged
    unit vectors, their summed weights, the group of each input, and the
    largest angle between an input and its group's direction. A tolerance of
    0 or less merges nothing.
    """
    dirs = np.asarray(directions, dtype=np.float64).reshape(-1, 3)
    w = np.asarray(weights, dtype=np.float64)
    if w.shape[0] != dirs.shape[0]:
        raise ValueError("One weight row per direction is needed.")
    if tolerance_deg <= 0 or len(dirs) == 0:
        return dirs.copy(), w.copy(), np.arange(len(dirs)), 0.0
    tol = float(tolerance_deg)
    elevation = np.degrees(np.arcsin(np.clip(dirs[:, 2], -1.0, 1.0)))
    azimuth = np.degrees(np.arctan2(dirs[:, 0], dirs[:, 1])) % 360.0
    band = np.floor((elevation + 90.0) / tol).astype(np.int64)
    middle = np.radians(np.minimum((band + 0.5) * tol - 90.0, 90.0))
    cells = np.maximum(1, np.floor(360.0 * np.cos(middle) / tol)).astype(np.int64)
    cell = np.minimum(np.floor(azimuth / 360.0 * cells).astype(np.int64), cells - 1)
    key = band * 1_000_000 + cell
    _keys, group = np.unique(key, return_inverse=True)
    count = int(group.max()) + 1
    first = w if w.ndim == 1 else w[:, 0]
    mean = np.stack(
        [np.bincount(group, weights=first * dirs[:, i], minlength=count) for i in range(3)],
        axis=1,
    )
    norm = np.linalg.norm(mean, axis=1)
    # A cell whose weights are all zero takes its plain mean.
    plain = np.stack(
        [np.bincount(group, weights=dirs[:, i], minlength=count) for i in range(3)], axis=1
    )
    mean = np.where((norm > 0)[:, None], mean, plain)
    mean /= np.linalg.norm(mean, axis=1, keepdims=True)
    if w.ndim == 1:
        summed = np.bincount(group, weights=w, minlength=count)
    else:
        summed = np.stack(
            [np.bincount(group, weights=w[:, j], minlength=count) for j in range(w.shape[1])],
            axis=1,
        )
    cosine = np.clip(np.einsum("ij,ij->i", dirs, mean[group]), -1.0, 1.0)
    deviation = float(np.degrees(np.arccos(cosine)).max())
    return mean, summed, group, deviation


def sky_directions(count: int) -> np.ndarray:
    """``count`` directions spread evenly over the upper hemisphere, (count, 3).

    A Fibonacci spiral, equal solid angle per direction (2π / count sr), so a
    sum over them is a midpoint-rule integral over the sky.
    """
    n = int(count)
    if n < 1:
        raise ValueError("At least one sky direction is needed.")
    i = np.arange(n, dtype=np.float64)
    z = 1.0 - (i + 0.5) / n  # cos(zenith) uniform in (0, 1): equal solid angle
    radius = np.sqrt(1.0 - z * z)
    phi = i * math.pi * (3.0 - math.sqrt(5.0))
    return np.stack([radius * np.sin(phi), radius * np.cos(phi), z], axis=1)


@dataclass(frozen=True, slots=True)
class Rays:
    """The rays every sample casts, and their weights (float64)."""

    directions: np.ndarray
    """(D, 3) unit vectors in scene axes."""
    hours: np.ndarray
    """(D,) hours added when the ray is unblocked and in front of the surface."""
    energy: np.ndarray
    """(D,) Wh/m² added, times cos(incidence), when the ray is unblocked."""
    sun_rays: int
    """The first ``sun_rays`` rays are the sun's; the rest are sky rays (diffuse)."""
    stats: dict[str, object] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.directions.shape[0])


def build_rays(
    steps: SunSteps,
    *,
    tolerance_deg: float = 0.0,
    insolation: bool = True,
    linke_turbidity: float = clearsky.DEFAULT_LINKE_TURBIDITY,
    altitude_m: float = 0.0,
    diffuse_rays: int = 0,
    convergence: float = 0.0,
    horizon: float | tuple[np.ndarray, np.ndarray] | None = None,
) -> Rays:
    """The rays for sun hours and (optionally) clear-sky insolation.

    Sun rays carry ``hours`` (the step length) and ``energy`` (DNI x hours,
    Wh/m² per unit cosine). With ``diffuse_rays`` > 0, that many sky rays
    follow (:func:`sky_directions`, those below the horizon profile dropped)
    with ``hours`` 0 and ``energy`` = DHI x hours summed over the period x
    2 / ``diffuse_rays``: summed over the unblocked rays in front of a surface,
    times their cosines, that is the isotropic sky's diffuse irradiation on
    it (the sky-view factor, cosine-weighted, times the diffuse horizontal
    irradiation). Ground-reflected light is not included.
    """
    hours = steps.hours
    if insolation and len(hours):
        _ghi, dni, dhi = clearsky.ineichen(
            90.0 - steps.elevation,
            linke_turbidity=linke_turbidity,
            altitude_m=altitude_m,
            distance_au=steps.distance_au,
        )
        energy = dni * hours
        diffuse_total = float(np.sum(dhi * hours))
        direct_normal_total = float(np.sum(energy))
    else:
        energy = np.zeros_like(hours)
        diffuse_total = direct_normal_total = 0.0
    weights = np.stack([hours, energy], axis=1)
    directions, merged, _group, deviation = group_directions(
        steps.directions, weights, tolerance_deg
    )
    sun_count = len(directions)
    sky_count = 0
    if insolation and diffuse_rays > 0:
        sky = sky_directions(diffuse_rays)
        true_azimuth = (np.degrees(np.arctan2(sky[:, 0], sky[:, 1])) + convergence) % 360.0
        sky_elevation = np.degrees(np.arcsin(sky[:, 2]))
        sky = sky[sky_elevation > horizon_elevation(true_azimuth, horizon)]
        sky_count = len(sky)
        sky_weight = np.full(sky_count, diffuse_total * 2.0 / diffuse_rays)
        directions = np.concatenate([directions, sky])
        merged = np.concatenate([merged, np.stack([np.zeros(sky_count), sky_weight], 1)])
    stats = {
        "steps": steps.total_steps,
        "steps_sun_up": len(hours),
        "steps_below_horizon_profile": steps.below_profile,
        "sun_hours_possible": float(np.sum(hours)),
        "sun_rays": sun_count,
        "sky_rays": sky_count,
        "group_tolerance_deg": float(max(tolerance_deg, 0.0)),
        "max_group_deviation_deg": deviation,
        "direct_normal_kwh_m2": direct_normal_total / 1000.0,
        "diffuse_horizontal_kwh_m2": diffuse_total / 1000.0,
    }
    return Rays(directions, merged[:, 0].copy(), merged[:, 1].copy(), sun_count, stats)


def accumulate(
    visible: np.ndarray, normals: np.ndarray, rays: Rays
) -> tuple[np.ndarray, np.ndarray]:
    """Sun hours and insolation (kWh/m²) of samples from their visibility, the reference.

    ``visible`` is (samples, rays) booleans: True where a ray leaves the
    sample unblocked. A ray counts only in front of the surface
    (cos(incidence) = normal . direction > 0). Returns float64 hours and
    kWh/m² per sample. This is what the Blender bake's shader sums.
    """
    vis = np.asarray(visible, dtype=bool)
    cos = np.asarray(normals, dtype=np.float64) @ rays.directions.T
    lit = vis & (cos > 0.0)
    hours = lit @ rays.hours
    energy = (np.where(lit, cos, 0.0)) @ rays.energy / 1000.0
    return hours, energy


def ray_offset(points: np.ndarray, override: float = 0.0) -> float:
    """How far ray origins move off the surface along its normal, in scene units.

    ``override`` > 0 is used as given. Otherwise the larger of 16 float32
    steps at the largest coordinate (positions are float32 in Blender) and a
    millionth of the samples' bounding-box diagonal.
    """
    if override > 0:
        return float(override)
    p = np.asarray(points, dtype=np.float64).reshape(-1, 3)
    if len(p) == 0:
        return 1e-6
    largest = float(np.abs(p).max())
    ulp = float(np.spacing(np.float32(max(largest, 1.0))))
    diagonal = float(np.linalg.norm(p.max(axis=0) - p.min(axis=0)))
    return max(16.0 * ulp, 1e-6 * diagonal)


SEASONS = ("march", "june", "september", "december")
"""The equinoxes and solstices, in calendar order."""


def season_moments(year: int) -> dict[str, _dt.datetime]:
    """UTC moments of the March equinox, June solstice, September equinox and December solstice.

    Where the sun's geocentric apparent longitude (SPA) crosses 0, 90, 180 and
    270 degrees, interpolated linearly between hourly values: good to well
    under a minute.
    """
    start = _dt.datetime(int(year), 1, 1, tzinfo=_dt.UTC).timestamp()
    times = start + 3600.0 * np.arange(366 * 24 + 1, dtype=np.float64)
    lam = np.unwrap(np.radians(sun.apparent_longitude(sun.julian_day(times))))
    lam = np.degrees(lam - 2.0 * np.pi * np.floor(lam[0] / (2.0 * np.pi)))
    moments: dict[str, _dt.datetime] = {}
    for name, target in zip(SEASONS, (360.0, 450.0, 540.0, 630.0), strict=True):
        seconds = float(np.interp(target, lam, times))
        moments[name] = _dt.datetime.fromtimestamp(round(seconds), _dt.UTC)
    return moments
