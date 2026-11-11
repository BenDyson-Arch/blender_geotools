"""Time vector import and export on a synthetic layer of 100k polygons (a dev tool).

Runs inside Blender, against the source checkout (nothing is installed):

    blender -b --factory-startup --python scripts/vector_timing.py -- [--features 100000]

The layer is a grid of octagons in GDA2020 / MGA zone 56 (synthetic numbers),
every tenth with a square hole, with a float, an int and a text column. It is
written once as GeoJSON and once as a GeoPackage, then each step is timed:
reading, building the mesh arrays (tessellating the polygons with holes),
making the Blender mesh with its attributes, and exporting the mesh back to a
GeoPackage. The design's target is "100k polygon features in a few seconds".
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

from geotools.core.crs_match import DataCrs
from geotools.core.vector import geojson, gpkg
from geotools.core.vector import mesh as vmesh
from geotools.core.vector.model import Column, ColumnType, GeometryBuilder, Kind, VectorData

E0, N0, A0 = 502_000.0, 6_961_000.0, 10.0
CRS = DataCrs("EPSG:7856", None, "user")


def synthetic(n: int) -> VectorData:
    side = int(np.ceil(np.sqrt(n)))
    angles = np.linspace(0.0, 2 * np.pi, 9)[:-1]
    octagon = np.column_stack([np.cos(angles), np.sin(angles)]) * 4.0
    octagon = np.vstack([octagon, octagon[:1]])
    hole = np.array([[-1, -1], [-1, 1], [1, 1], [1, -1], [-1, -1]], dtype=np.float64)
    builder = GeometryBuilder()
    for i in range(n):
        centre = np.array([E0 + 10.0 * (i % side), N0 + 10.0 * (i // side)])
        rings = [octagon + centre]
        if i % 10 == 0:
            rings.append(hole + centre)
        builder.add_polygon(i, rings)
    rows = np.arange(n)
    return VectorData(
        name="timing",
        fids=rows + 1,
        columns={
            "depth": Column("depth", ColumnType.FLOAT, rows * 0.01),
            "phase": Column("phase", ColumnType.INT, rows % 7),
            "context": Column("context", ColumnType.STRING, [f"C{i:06d}" for i in range(n)]),
        },
        geometries=builder.finish(),
        crs=CRS,
    )


def main(argv: list[str]) -> None:
    import bpy

    from geotools.blender import vectors

    parser = argparse.ArgumentParser()
    parser.add_argument("--features", type=int, default=100_000)
    args = parser.parse_args(argv)
    n = args.features
    timings: list[tuple[str, float]] = []

    def timed(label: str, fn):
        t0 = time.perf_counter()
        out = fn()
        timings.append((label, time.perf_counter() - t0))
        return out

    with tempfile.TemporaryDirectory(prefix="geotools-timing-") as tmp:
        tmp_path = Path(tmp)
        data = synthetic(n)
        gj, gp = tmp_path / "timing.geojson", tmp_path / "timing.gpkg"
        geojson.write(gj, data, CRS)
        gpkg.write_layer(gp, "timing", data, Kind.POLYGONS, CRS)
        print(
            f"{n} polygons ({n // 10} with holes): GeoJSON {gj.stat().st_size / 2**20:.1f} MiB, "
            f"GeoPackage {gp.stat().st_size / 2**20:.1f} MiB"
        )

        timed("read GeoJSON", lambda: geojson.read(gj))
        read = timed("read GeoPackage", lambda: gpkg.read_layer(gp, "timing"))
        geoms = read.geometries[Kind.POLYGONS]
        arrays = timed(
            "mesh arrays (tessellate holes)",
            lambda: vmesh.polygon_mesh(geoms, (E0, N0, A0), vectors._tessellate),
        )
        columns = read.columns_for(Kind.POLYGONS)
        names = vectors.blender_names(columns)
        mesh = timed("Blender mesh", lambda: vectors.build_mesh("timing", arrays))
        numeric = {k: v for k, v in names.items() if columns[v].type is not ColumnType.STRING}
        fids = read.fids_for(Kind.POLYGONS)
        timed(
            "attributes: fid, float, int",
            lambda: vectors.write_attributes(mesh, Kind.POLYGONS, arrays, fids, columns, numeric),
        )
        # The STRING column alone: set element by element, as write_attributes does.
        t0 = time.perf_counter()
        attr = mesh.attributes.new("context", "STRING", "FACE")
        values = vmesh.stored_values(columns["context"])
        for item, i in zip(attr.data, arrays.element_feature.tolist(), strict=True):
            item.value = values[i]
        timings.append(("attributes: one STRING column", time.perf_counter() - t0))
        print(f"mesh: {len(mesh.vertices)} vertices, {len(mesh.polygons)} faces")

        obj = bpy.data.objects.new("timing", mesh)
        bpy.context.scene.collection.objects.link(obj)
        depsgraph = bpy.context.evaluated_depsgraph_get()
        exported = timed(
            "export: mesh to polygons", lambda: vectors.mesh_input(obj, depsgraph, (E0, N0, A0))
        )
        timed(
            "export: write GeoPackage",
            lambda: gpkg.write_layer(tmp_path / "out.gpkg", "out", exported[0], Kind.POLYGONS, CRS),
        )
        assert exported[0].n_rows == n, exported[0].n_rows

    width = max(len(label) for label, _ in timings)
    for label, seconds in timings:
        print(f"  {label:<{width}}  {seconds:6.2f} s")
    import_total = sum(s for label, s in timings[1:6])
    print(f"  {'import from GeoPackage, total':<{width}}  {import_total:6.2f} s")


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
