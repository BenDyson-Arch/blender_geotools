"""The GeoTools side of the geo formats validation (host Python, not in the container).

``run_formats.sh`` runs it between the two GDAL steps::

    python scripts/validation/geodesy/formats_geotools.py DIR

It imports GDAL's ``rfc7946.geojson`` the way Import GeoJSON does (read, CRS,
reprojection into the project CRS EPSG:7856, then into the project frame as
float32 and back out as Export does), and writes ``geotools.geojson`` and
``geotools.gpkg`` in EPSG:7856 for GDAL to read back. The CI test
(tests/core/test_validation_geodesy.py) repeats this pipeline and compares it
with what GDAL read.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from geotools.core.crs_match import project_crs
from geotools.core.placement import Placement, PlacementMode
from geotools.core.vector import geojson, gpkg
from geotools.core.vector.mesh import from_local, to_local
from geotools.core.vector.transform import reproject_data

OFFSET = (502000.0, 6961000.0, 0.0)
PROJECT = project_crs(
    Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:7856", source_offset=OFFSET)
)


def import_and_export(source: Path):
    """GeoJSON in, reprojected into EPSG:7856, through float32 local positions and back."""
    data = geojson.read(source)
    moved, _report = reproject_data(data, PROJECT)
    geometries = {
        kind: replace(g, coords=from_local(to_local(g.coords, OFFSET), OFFSET))
        for kind, g in moved.geometries.items()
    }
    return moved, replace(moved, geometries=geometries, crs=PROJECT)


def write_outputs(directory: Path) -> None:
    _moved, exported = import_and_export(directory / "rfc7946.geojson")
    geojson.write(directory / "geotools.geojson", exported, PROJECT)
    out = directory / "geotools.gpkg"
    if out.exists():
        out.unlink()
    for kind in exported.kinds():
        gpkg.write_layer(out, f"features_{kind.value}", exported, kind, PROJECT)


if __name__ == "__main__":
    write_outputs(Path(sys.argv[1]))
    print("ok", np.__version__)
