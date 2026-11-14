"""GeoTools' side of the export check: GeoPackages and GeoJSON for GDAL to read.

Writes into OUT_DIR, with GeoTools' own writers (no Blender):

- ``mga56.gpkg``, ``albers.gpkg``: EPSG:7856 and EPSG:3577, WKT1 definitions;
- ``itrf.gpkg``: EPSG:9989 (3D geographic, which WKT1 cannot express): WKT2
  in the CRS WKT extension, then a second layer in EPSG:7856;
- ``gdal_plus_itrf.gpkg``: GDAL's own ``mga56.gpkg`` fixture with an EPSG:9989
  layer added (the table rebuilt for the extension);
- ``custom_after.gpkg``, ``custom_before.gpkg``: a CRS without a code, with
  pyproj (WKT1) and as if without it (the PROJ string, as before 0.3.0);
- ``sections.gpkg``, ``sections.geojson``: two section pieces with chainage
  as M (GeoJSON: a per-point ``chainage`` property).

Run (from the repository root): scripts/validation/exports/run.sh
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from geotools.core import deps
from geotools.core.crs_match import DataCrs
from geotools.core.vector import ColumnType, Kind, gpkg
from geotools.core.vector import geojson as gj
from geotools.core.vector import mesh as vm
from geotools.core.vector.model import Column, GeometryBuilder, VectorData

E0, N0 = 502_000.0, 6_961_000.0


def squares() -> VectorData:
    builder = GeometryBuilder()
    for i in range(3):
        x = E0 + 20.0 * i
        ring = np.array([[x, N0, 10.0], [x + 5, N0, 10.0], [x + 5, N0 + 5, 11.0], [x, N0, 10.0]])
        builder.add_polygon(i, [ring])
    rows = np.arange(3)
    columns = {"n": Column("n", ColumnType.INT, rows.astype(np.int64))}
    return VectorData("squares", rows + 1, columns, builder.finish(), None)  # type: ignore[arg-type]


def sections() -> VectorData:
    positions = np.array([[E0 + 10.0 * i, N0 + 5.0 * i, 10.0 + i] for i in range(6)])
    chainage = np.array([0.0, 11.180339, 22.36068, 0.0, 11.18, 22.36], dtype=np.float32)
    empty = np.zeros(0, np.int64)
    mesh = vm.MeshInput(
        positions=positions,
        loop_starts=empty,
        loop_totals=empty,
        loop_vertices=empty,
        edges=np.array([[0, 1], [1, 2], [3, 4], [4, 5]]),
        loop_edges=empty,
        edge_attrs={"fid": (ColumnType.INT, np.array([1, 1, 2, 2]))},
        point_attrs={"chainage": (ColumnType.FLOAT, chainage)},
        measure="chainage",
    )
    (data,) = vm.vectors_from_mesh(mesh, "sections", None)
    return data


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("*.gpkg")) + list(out.glob("*.geojson")):
        old.unlink()
    data = squares()
    mga = DataCrs("EPSG:7856", "EPSG:7856", "project")
    gpkg.write_layer(out / "mga56.gpkg", "squares", data, Kind.POLYGONS, mga)
    albers = DataCrs("EPSG:3577", "EPSG:3577", "project")
    gpkg.write_layer(out / "albers.gpkg", "squares", data, Kind.POLYGONS, albers)
    itrf = DataCrs("EPSG:9989", None, "user")
    gpkg.write_layer(out / "itrf.gpkg", "squares", data, Kind.POLYGONS, itrf)
    gpkg.write_layer(out / "itrf.gpkg", "squares_mga", data, Kind.POLYGONS, mga)
    fixture = ROOT / "tests/data/validation/geodesy/formats/mga56.gpkg"
    shutil.copy(fixture, out / "gdal_plus_itrf.gpkg")
    gpkg.write_layer(out / "gdal_plus_itrf.gpkg", "itrf", data, Kind.POLYGONS, itrf)
    custom = DataCrs(
        None,
        "+proj=tmerc +lat_0=-27 +lon_0=153 +k=1 +x_0=50000 +y_0=50000 +ellps=GRS80 +units=m",
        "user",
    )
    gpkg.write_layer(out / "custom_after.gpkg", "squares", data, Kind.POLYGONS, custom)
    lines = sections()
    gpkg.write_layer(out / "sections.gpkg", "sections", lines, Kind.LINES, mga)
    gj.write(out / "sections.geojson", lines, mga)
    deps.probe_pyproj = lambda: deps.DependencyStatus(False, error="hidden", name="pyproj")
    gpkg.write_layer(out / "custom_before.gpkg", "squares", data, Kind.POLYGONS, custom)
    print(f"wrote {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
