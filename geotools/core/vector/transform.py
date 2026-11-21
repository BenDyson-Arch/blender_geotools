"""Vector coordinates between CRSs: the extent, and reprojection with pyproj.

Every geometry kind's ``coords`` go through one :func:`geotools.core.crs_match.reproject`
call, in binary64, before the project offset is subtracted. A missing Z (NaN)
stays missing: when any Z is missing only x and y are reprojected.

:func:`to_rfc7946` is the other direction, for a GeoJSON export a web viewer
can read: project coordinates to WGS84 longitude and latitude, rounded, with
the polygon winding RFC 7946 asks for.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .. import crs_match
from ..crs_match import SOURCE_PROJECT, WGS84, DataCrs
from ..data_origin import Bounds
from ..reproject import ReprojectionReport
from .model import Geometries, Kind, VectorData


def bounds(datas: list[VectorData]) -> Bounds | None:
    """XY extent of every coordinate in ``datas``; None when there are none."""
    coords = [g.coords for d in datas for g in d.geometries.values() if len(g.coords)]
    if not coords:
        return None
    xy = np.concatenate([c[:, :2] for c in coords])
    finite = np.isfinite(xy).all(axis=1)
    if not finite.any():
        return None
    xy = xy[finite]
    return (
        float(xy[:, 0].min()),
        float(xy[:, 1].min()),
        float(xy[:, 0].max()),
        float(xy[:, 1].max()),
    )


def reproject_data(
    data: VectorData, project: DataCrs, *, allow_network: bool = False
) -> tuple[VectorData, ReprojectionReport | None]:
    """``data`` with its coordinates reprojected from ``data.crs`` into ``project``.

    The report is None when there are no coordinates to reproject.

    Raises ``crs_match.ReprojectionUnavailable`` without pyproj and
    ``reproject.ReprojectionError`` (a ValueError) when PROJ cannot do it.
    """
    kinds = [k for k, g in data.geometries.items() if len(g.coords)]
    coords = (
        np.concatenate([data.geometries[k].coords for k in kinds])
        if kinds
        else np.zeros((0, 3), dtype=np.float64)
    )
    if not len(coords):
        return data, None
    if data.crs is None:
        raise ValueError(f"{data.name} has no CRS to reproject from")
    has_z = bool(np.isfinite(coords[:, 2]).all())
    points = coords if has_z else coords[:, :2]
    moved, report = crs_match.reproject(points, data.crs, project, allow_network=allow_network)
    out = coords.copy()
    out[:, : points.shape[1]] = np.asarray(moved, dtype=np.float64)
    geometries: dict = {}
    start = 0
    for kind, geoms in data.geometries.items():
        n = len(geoms.coords)
        if kind in kinds:
            geometries[kind] = _with_coords(geoms, out[start : start + n])
            start += n
        else:
            geometries[kind] = geoms
    return replace(data, geometries=geometries), report


def _with_coords(geoms: Geometries, coords: np.ndarray) -> Geometries:
    return replace(geoms, coords=np.ascontiguousarray(coords))


WGS84_3D = "EPSG:4979"
"""WGS84 with ellipsoidal heights: the RFC 7946 target for a project CRS with a height axis."""

RFC7946_DECIMALS = 8
"""Decimal places of longitude and latitude in RFC 7946 output (1e-8 degrees is about 1 mm)."""

HEIGHTS_NOT_ELLIPSOIDAL = (
    "Heights are the project's heights, not the WGS84 ellipsoidal heights RFC 7946 asks "
    "for: the file is not conformant on Z."
)


def to_rfc7946(
    data: VectorData, project: DataCrs, *, allow_network: bool = False
) -> tuple[VectorData, ReprojectionReport | None]:
    """``data``, in the project CRS ``project``, as RFC 7946 coordinates.

    Longitude and latitude in WGS84 (always x first), rounded to
    :data:`RFC7946_DECIMALS`; polygon exterior rings counterclockwise and
    holes clockwise. A project CRS with a height axis (3D or compound) goes to
    :data:`WGS84_3D`, so PROJ converts heights to ellipsoidal ones where it
    can (it needs the geoid grid); otherwise heights pass through and the
    report says the file is not conformant on Z. Write the result with the ``WGS84`` CRS, so it carries no
    ``crs`` member.

    Raises ``crs_match.ReprojectionUnavailable`` without pyproj and
    ``reproject.ReprojectionError`` (a ValueError) when PROJ cannot do it.
    """
    three_d = crs_match.has_vertical_axis(project)
    target = DataCrs(WGS84_3D if three_d else WGS84, None, SOURCE_PROJECT)
    moved, report = reproject_data(replace(data, crs=project), target, allow_network=allow_network)
    geometries = {kind: _rfc7946_geometries(g) for kind, g in moved.geometries.items()}
    out = replace(moved, geometries=geometries, crs=DataCrs(WGS84, None, SOURCE_PROJECT))
    coords = [g.coords for g in data.geometries.values() if len(g.coords)]
    z = np.concatenate(coords)[:, 2] if coords else np.zeros(0)
    converted = three_d and np.isfinite(z).all()
    if report is not None and "ballpark vertical" in report.operation.lower():
        converted = False  # PROJ had no geoid model: the heights passed through
    if report is not None and np.isfinite(z).any() and not converted:
        notes = [n for n in report.notes if not n.startswith("No vertical transform")]
        report = replace(report, notes=(*notes, HEIGHTS_NOT_ELLIPSOIDAL))
    return out, report


def _rfc7946_geometries(geoms: Geometries) -> Geometries:
    coords = geoms.coords.copy()
    coords[:, :2] = np.round(coords[:, :2], RFC7946_DECIMALS)
    geoms = _with_coords(geoms, coords)
    if geoms.kind is not Kind.POLYGONS or not geoms.n_rings:
        return geoms
    order = _rfc7946_ring_order(geoms)
    measures = None if geoms.measures is None else geoms.measures[order]
    return replace(geoms, coords=np.ascontiguousarray(geoms.coords[order]), measures=measures)


def _rfc7946_ring_order(geoms: Geometries) -> np.ndarray:
    """The coordinate order that winds exterior rings counterclockwise and holes clockwise."""
    starts = geoms.ring_offsets[:-1]
    ends = geoms.ring_offsets[1:]
    ring = geoms.coord_ring()
    index = np.arange(len(geoms.coords))
    following = index + 1
    nonempty = ends > starts
    following[ends[nonempty] - 1] = starts[nonempty]
    # Relative to each ring's first vertex: degrees near 150 would swamp a small ring's area.
    xy = geoms.coords[:, :2] - geoms.coords[starts[ring], :2]
    cross = xy[:, 0] * xy[following, 1] - xy[following, 0] * xy[:, 1]
    area = np.bincount(ring, weights=cross, minlength=geoms.n_rings)  # twice the signed area
    exterior = np.zeros(geoms.n_rings, dtype=bool)
    part_starts = geoms.part_offsets[:-1]
    exterior[part_starts[geoms.part_offsets[1:] > part_starts]] = True
    flip = np.where(exterior, area < 0, area > 0)
    return np.where(flip[ring], starts[ring] + ends[ring] - 1 - index, index)
