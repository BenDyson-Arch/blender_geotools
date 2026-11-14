"""GDAL's side of the export check: validate and read what GeoTools wrote.

Runs in ``ghcr.io/osgeo/gdal:ubuntu-small-3.12.0`` and never imports GeoTools.
For each file: ``validate_gpkg`` (GeoPackages), every layer's CRS as GDAL
reads it, its geometry type, and for measured lines the M values (and the
GeoJSON ``chainage`` property). Prints JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from osgeo import gdal, ogr
from osgeo_utils.samples import validate_gpkg

gdal.UseExceptions()


def main(directory: Path) -> None:
    out = {}
    for path in sorted([*directory.glob("*.gpkg"), *directory.glob("*.geojson")]):
        entry: dict = {}
        if path.suffix == ".gpkg":
            try:
                validate_gpkg.check(str(path))
                entry["validate_gpkg"] = "ok"
            except Exception as exc:  # noqa: BLE001 - report the validator's message
                entry["validate_gpkg"] = str(exc)
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        ds = ogr.Open(str(path))
        gdal.PopErrorHandler()
        layers = {}
        for i in range(ds.GetLayerCount()):
            layer = ds.GetLayer(i)
            srs = layer.GetSpatialRef()
            info = {
                "crs": None
                if srs is None
                else [srs.GetAuthorityName(None), srs.GetAuthorityCode(None), srs.GetName()],
                "proj4": None if srs is None else srs.ExportToProj4(),
                "geometry": ogr.GeometryTypeToName(layer.GetGeomType()),
                "features": layer.GetFeatureCount(),
            }
            measures, chainage = [], []
            defn = layer.GetLayerDefn()
            for feature in layer:
                geom = feature.GetGeometryRef()
                if geom is not None and geom.IsMeasured():
                    measures.append([geom.GetM(k) for k in range(geom.GetPointCount())])
                if defn.GetFieldIndex("chainage") >= 0:
                    chainage.append(feature.GetField("chainage"))
            if measures:
                info["m"] = measures
            if chainage:
                info["chainage_property"] = chainage
            layers[layer.GetName()] = info
        entry["layers"] = layers
        out[path.name] = entry
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
