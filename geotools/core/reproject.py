"""Reproject vector points into the project CRS with the bundled pyproj.

Decided for M8 (docs/design/m8-geo-formats.md, "CRS matching"): rusty-tiles does
no general reprojection, so imported *vector points* in another CRS are
reprojected here, with PROJ choosing EPSG's best operation. Rasters are never
reprojected. Tile placement is unaffected: publishing still goes from the
project CRS through rusty-tiles.

pyproj is imported inside the functions, so this module (and ``geotools.core``)
imports without it; :func:`reproject_points` then raises
:class:`ReprojectionUnavailable`.

Behaviour:

- ``Transformer.from_crs(source, target, always_xy=True)``: x is easting or
  longitude, y is northing or latitude, whatever the CRS's axis order.
- PROJ's network (grid downloads from cdn.proj.org) is switched off for the call
  unless ``allow_network`` is set, and the previous setting is restored, so
  imports stay offline and reproducible. When a more accurate operation needs a
  grid that is not installed, the report says so.
- One vectorised call for all points. Z goes through PROJ only when both CRSs
  are 3D; otherwise it is passed through unchanged, and the report says so.
- The report names the operation PROJ used, at the centre of the data, and its
  accuracy; ballpark operations and points outside the operation's area of use
  are noted.
"""

from __future__ import annotations

import contextlib
import math
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import deps

_UNAVAILABLE_DESCRIPTION = "unavailable until proj_trans is called"


@dataclass(frozen=True, slots=True)
class ReprojectionReport:
    """What :func:`reproject_points` did, for the import report."""

    source: str
    """Source CRS, as given."""
    target: str
    """Target (project) CRS, as given."""
    operation: str
    """The coordinate operation PROJ used (``transformer.description``)."""
    accuracy_m: float | None
    """The operation's accuracy in metres; None when PROJ doesn't know."""
    network: bool
    """Whether PROJ was allowed to download grids."""
    notes: tuple[str, ...]
    """Caveats: ballpark operation, Z passed through, area of use, missing grids."""


class ReprojectionUnavailable(RuntimeError):
    """pyproj is not available, so points cannot be reprojected."""


class ReprojectionError(ValueError):
    """The points cannot be reprojected: a bad CRS, bad input or a failed transform."""


def available() -> bool:
    """True if the bundled pyproj loads and has its PROJ data."""
    return deps.probe_pyproj().available


def reproject_points(
    xyz: np.ndarray,
    source: str,
    target: str,
    *,
    allow_network: bool = False,
) -> tuple[np.ndarray, ReprojectionReport]:
    """Reproject points from ``source`` to ``target``.

    ``xyz`` is an (N, 3) or (N, 2) float64 array: x is easting or longitude (degrees),
    y northing or latitude, z height. ``source`` and ``target`` are anything
    pyproj accepts: ``"EPSG:7856"``, WKT or a PROJ string. Returns a new array of
    the same shape (the input is not changed) and a report.

    Raises :class:`ReprojectionUnavailable` without pyproj, and
    :class:`ReprojectionError` for an unknown CRS, bad input, or a non-finite
    result.
    """
    status = deps.probe_pyproj()
    if not status.available:
        raise ReprojectionUnavailable(
            f"reprojection is not available: {status.error or 'pyproj is not available'}"
        )
    points = _validated(xyz)

    import pyproj
    import pyproj.network

    source_crs = _crs(pyproj, source, "source")
    target_crs = _crs(pyproj, target, "target")
    notes: list[str] = []

    with _network(pyproj.network, allow_network):
        try:
            transformer = pyproj.Transformer.from_crs(source_crs, target_crs, always_xy=True)
        except pyproj.exceptions.ProjError as exc:
            raise ReprojectionError(
                f"no coordinate operation from {_short(source)} to {_short(target)}: {exc}"
            ) from exc

        three_d = points.shape[1] == 3
        vertical = three_d and _is_3d(source_crs) and _is_3d(target_crs)
        out = points.copy()
        x = np.ascontiguousarray(points[:, 0])
        y = np.ascontiguousarray(points[:, 1])
        try:
            if vertical:
                z = np.ascontiguousarray(points[:, 2])
                tx, ty, tz = transformer.transform(x, y, z, errcheck=False)
                out[:, 2] = tz
            else:
                tx, ty = transformer.transform(x, y, errcheck=False)
        except pyproj.exceptions.ProjError as exc:
            raise ReprojectionError(
                f"transform from {_short(source)} to {_short(target)} failed: {exc}"
            ) from exc
        out[:, 0] = tx
        out[:, 1] = ty

        bad = ~np.isfinite(out).all(axis=1)
        if bad.any():
            first = int(np.flatnonzero(bad)[0])
            raise ReprojectionError(
                f"{int(bad.sum())} of {len(out)} points could not be transformed from "
                f"{_short(source)} to {_short(target)} (non-finite result; first is point "
                f"{first} at {points[first, 0]!r}, {points[first, 1]!r}); are the "
                "coordinates in the source CRS?"
            )

        operation, accuracy, area = _operation_used(transformer, points, vertical)
        outside = _outside_area(pyproj, source_crs, points, area) if area is not None else 0
        grid_notes = _missing_grid_notes(pyproj, source_crs, target_crs, accuracy)

    ballpark = "ballpark" in operation.lower()
    if ballpark:
        notes.append(
            "Ballpark transformation: PROJ knows no datum shift between these CRSs and "
            "treats the two datums as the same; expect errors of metres to hundreds of "
            "metres."
        )
    elif accuracy is None:
        notes.append("PROJ does not know the accuracy of this operation.")
    if three_d and not vertical:
        if not _is_3d(source_crs) and not _is_3d(target_crs):
            which = "neither CRS has a"
        elif not _is_3d(target_crs):
            which = "the target CRS has no"
        else:
            which = "the source CRS has no"
        notes.append(
            f"No vertical transform: Z was passed through unchanged, since {which} vertical axis."
        )
    if outside:
        notes.append(
            f"{outside} of {len(points)} points lie outside the area of use of the "
            f"operation ({area.name}; lon {area.west:g} to {area.east:g}, "
            f"lat {area.south:g} to {area.north:g}); PROJ may have used another "
            "operation for them."
        )
    notes.extend(grid_notes)

    report = ReprojectionReport(
        source=source,
        target=target,
        operation=operation,
        accuracy_m=accuracy,
        network=allow_network,
        notes=tuple(notes),
    )
    return out, report


# --- helpers -----------------------------------------------------------------


def _validated(xyz: Any) -> np.ndarray:
    try:
        points = np.asarray(xyz, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ReprojectionError(f"points must be a float64 array: {exc}") from exc
    if points.ndim != 2 or points.shape[1] not in (2, 3):
        raise ReprojectionError(f"points must have shape (N, 2) or (N, 3), not {points.shape}")
    finite = np.isfinite(points).all(axis=1)
    if not finite.all():
        first = int(np.flatnonzero(~finite)[0])
        raise ReprojectionError(
            f"{int((~finite).sum())} of {len(points)} input points are not finite "
            f"(first is point {first})"
        )
    return points


def _crs(pyproj: Any, text: str, role: str) -> Any:
    if not isinstance(text, str) or not text.strip():
        raise ReprojectionError(f"no {role} CRS given")
    try:
        return pyproj.CRS.from_user_input(text)
    except pyproj.exceptions.CRSError as exc:
        raise ReprojectionError(f"unknown {role} CRS {_short(text)}: {exc}") from exc


def _short(text: str, limit: int = 80) -> str:
    """A CRS string short enough for a message (WKT can be kilobytes)."""
    flat = " ".join(str(text).split())
    return repr(flat if len(flat) <= limit else flat[: limit - 3] + "...")


def _is_3d(crs: Any) -> bool:
    return len(crs.axis_info) >= 3


@contextlib.contextmanager
def _network(network: Any, allow: bool) -> Iterator[None]:
    """Set PROJ's network switch for the block, then restore the previous one."""
    previous = network.is_network_enabled()
    network.set_network_enabled(active=allow)
    try:
        yield
    finally:
        network.set_network_enabled(active=previous)


def _operation_used(
    transformer: Any, points: np.ndarray, vertical: bool
) -> tuple[str, float | None, Any]:
    """Description, accuracy and area of use of the operation PROJ uses for the data.

    With several candidate operations PROJ picks one per point and the
    transformer only says "unavailable until proj_trans is called"; transform
    the point nearest the data's centre once more and ask which one it used.
    """
    chosen = transformer
    if transformer.description == _UNAVAILABLE_DESCRIPTION and len(points):
        centre = np.median(points, axis=0)
        nearest = points[int(np.argmin(((points - centre) ** 2).sum(axis=1)))]
        args = nearest[:3] if vertical else nearest[:2]
        transformer.transform(*(float(v) for v in args), errcheck=False)
        with contextlib.suppress(Exception):  # pyproj.exceptions.ProjError
            chosen = transformer.get_last_used_operation()
    description = chosen.description or chosen.name or "unknown operation"
    accuracy = chosen.accuracy
    accuracy_m = float(accuracy) if accuracy is not None and accuracy >= 0 else None
    return description, accuracy_m, chosen.area_of_use


def _missing_grid_notes(
    pyproj: Any, source_crs: Any, target_crs: Any, accuracy: float | None
) -> list[str]:
    """Note a more accurate operation that needs a grid PROJ doesn't have."""
    try:
        with warnings.catch_warnings():
            # pyproj warns "Best transformation is not available"; that is this note.
            warnings.simplefilter("ignore", UserWarning)
            group = pyproj.transformer.TransformerGroup(source_crs, target_crs, always_xy=True)
    except (pyproj.exceptions.ProjError, ValueError, TypeError):  # only advisory
        return []
    known = [op for op in group.unavailable_operations if (op.accuracy or -1) >= 0]
    if not known:
        return []
    best = min(known, key=lambda op: op.accuracy)
    if accuracy is not None and best.accuracy >= accuracy:
        return []
    grids = sorted({g.short_name for g in best.grids if not g.available})
    grid_text = f", needs {', '.join(grids)}" if grids else ""
    return [
        (
            f"A more accurate operation is not available offline: {best.name} "
            f"(accuracy {best.accuracy:g} m{grid_text}). Allow grid downloads, or install "
            "the grid, to use it."
        )
    ]


def _outside_area(pyproj: Any, source_crs: Any, points: np.ndarray, area: Any) -> int:
    """How many points fall outside ``area`` (lon/lat bounds), or 0 if we can't tell."""
    try:
        geodetic = source_crs.geodetic_crs
        if geodetic is None:
            return 0
        to_geodetic = pyproj.Transformer.from_crs(source_crs, geodetic, always_xy=True)
        lon, lat = to_geodetic.transform(
            np.ascontiguousarray(points[:, 0]),
            np.ascontiguousarray(points[:, 1]),
            errcheck=False,
        )
    except (pyproj.exceptions.ProjError, ValueError, TypeError):  # only advisory
        return 0
    lon = np.asarray(lon)
    lat = np.asarray(lat)
    if area.west <= area.east:
        in_lon = (lon >= area.west) & (lon <= area.east)
    else:  # the area crosses the antimeridian
        in_lon = (lon >= area.west) | (lon <= area.east)
    inside = in_lon & (lat >= area.south) & (lat <= area.north)
    return int(np.count_nonzero(~inside & np.isfinite(lon) & np.isfinite(lat)))


def format_accuracy(accuracy_m: float | None) -> str:
    """``"0.01 m"``, or ``"unknown"`` when PROJ doesn't know, for the import report."""
    if accuracy_m is None or not math.isfinite(accuracy_m):
        return "unknown"
    return f"{accuracy_m:g} m"
