"""Which CRS a data file is in, and whether it is the project's CRS.

The interface of docs/design/m8-geo-formats.md ("CRS matching"), shared by the
vector and raster importers. GeoTools never reprojects: data in another CRS is
refused with a message that suggests reprojecting in QGIS. :func:`reproject`
is the one hook for rusty-tiles' planned ``transform_points``; it raises
:class:`ReprojectionUnavailable` until that ships.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import crs_presets
from .placement import Placement, PlacementMode


@dataclass(frozen=True, slots=True)
class DataCrs:
    epsg: str | None
    """``"EPSG:7856"``, or None when the CRS has no code."""
    definition: str | None
    """WKT or PROJ text when there is no code (or the project's own text)."""
    source: str
    """Where it came from: "geojson-crs-member", "rfc7946-default", "gpkg-srs",
    "geotiff-geokeys", "user" or "project"."""

    def label(self) -> str:
        """Short text for messages: the EPSG code, else the definition, else "unknown"."""
        if self.epsg:
            return self.epsg
        if self.definition:
            text = " ".join(self.definition.split())
            return text if len(text) <= 60 else text[:57] + "..."
        return "an unknown CRS"


_EPSG = re.compile(r"\s*EPSG\s*:\s*(\d+)\s*", re.IGNORECASE)


def normalise_epsg(code: str | int | None) -> str | None:
    """``"EPSG:nnnn"`` from ``"epsg:nnnn"``, ``"EPSG: nnnn"`` or an int; else None."""
    if code is None:
        return None
    if isinstance(code, int):
        return f"EPSG:{code}"
    match = _EPSG.fullmatch(code)
    return f"EPSG:{int(match.group(1))}" if match else None


def project_crs(placement: Placement) -> DataCrs | None:
    """The project's CRS, or None for LOCAL and ANCHOR projects (or no CRS set)."""
    if PlacementMode(placement.mode) is not PlacementMode.SOURCE_CRS:
        return None
    definition = (placement.source_crs or "").strip()
    if not definition:
        return None
    return DataCrs(
        epsg=normalise_epsg(crs_presets.epsg_code(definition)),
        definition=definition,
        source="project",
    )


def matches(project: DataCrs | None, data: DataCrs) -> bool | None:
    """True or False when it can tell, None when it can't (no code to compare)."""
    if project is None:
        return None
    if project.epsg and data.epsg:
        return normalise_epsg(project.epsg) == normalise_epsg(data.epsg)
    if (
        project.definition
        and data.definition
        and " ".join(project.definition.split()) == " ".join(data.definition.split())
    ):
        return True
    return None


class ReprojectionUnavailable(RuntimeError):
    """Reprojection is not available (rusty-tiles has no ``transform_points`` yet)."""


def reproject(points: object, data_crs: DataCrs, project: DataCrs) -> object:
    """Reproject points from ``data_crs`` to the project CRS. Not available yet."""
    raise ReprojectionUnavailable(
        f"Reprojecting from {data_crs.label()} to {project.label()} is not available: "
        "reproject the data in QGIS first."
    )
