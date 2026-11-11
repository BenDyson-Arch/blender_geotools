"""Vector coordinates into the project CRS: the extent, and reprojection with pyproj.

Every geometry kind's ``coords`` go through one :func:`geotools.core.crs_match.reproject`
call, in binary64, before the project offset is subtracted. A missing Z (NaN)
stays missing: when any Z is missing only x and y are reprojected.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from .. import crs_match
from ..crs_match import DataCrs
from ..data_origin import Bounds
from ..reproject import ReprojectionReport
from .model import Geometries, VectorData


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
