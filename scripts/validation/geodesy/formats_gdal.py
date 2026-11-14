"""The GDAL side of the geo formats validation. Runs INSIDE the GDAL Docker image.

Not GeoTools code and not run in CI; ``run_formats.sh`` runs it in
ghcr.io/osgeo/gdal:ubuntu-small-3.12.0 (GDAL 3.12.0, PROJ 9.7.0)::

    python3 formats_gdal.py sources  OUT_DIR   # synthetic files written by GDAL
    python3 formats_gdal.py readback IN_DIR    # GDAL reads what GeoTools wrote

``sources`` writes small GeoJSON, GeoPackage and GeoTIFF files with GDAL and
``gdal_sources.json``: what GDAL itself reads from them (CRS, geotransform,
coordinates) and GDAL's own reprojection of every vector vertex into
EPSG:7856. ``readback`` opens ``geotools.geojson`` and ``geotools.gpkg``
(written by ``formats_geotools.py``) and writes ``gdal_readback.json``: the CRS
GDAL finds, the coordinates it reads, and those taken back to WGS 84.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()
ogr.UseExceptions()
osr.UseExceptions()

# Synthetic features near 153.03 E, 27.47 S (MGA zone 56): lon, lat, ellipsoidal h.
POINTS = [(153.03, -27.47, 12.5), (153.0412345678, -27.4498765432, 40.0), (152.95, -27.52, 0.0)]
LINE = [(153.02, -27.48, 5.0), (153.025, -27.475, 6.0), (153.04, -27.46, 7.5)]
RING = [
    (153.01, -27.49, 3.0),
    (153.02, -27.49, 3.0),
    (153.02, -27.48, 3.0),
    (153.01, -27.48, 3.0),
    (153.01, -27.49, 3.0),
]


def _srs(code: int) -> osr.SpatialReference:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(code)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)  # x = lon/easting
    return srs


def _features(layer: ogr.Layer) -> None:
    defn = layer.GetLayerDefn()
    geoms = []
    for i, (lon, lat, h) in enumerate(POINTS):
        g = ogr.Geometry(ogr.wkbPoint25D)
        g.AddPoint(lon, lat, h)
        geoms.append(("point", i, g))
    g = ogr.Geometry(ogr.wkbLineString25D)
    for lon, lat, h in LINE:
        g.AddPoint(lon, lat, h)
    geoms.append(("line", 0, g))
    ring = ogr.Geometry(ogr.wkbLinearRing)
    for lon, lat, h in RING:
        ring.AddPoint(lon, lat, h)
    poly = ogr.Geometry(ogr.wkbPolygon25D)
    poly.AddGeometry(ring)
    geoms.append(("polygon", 0, poly))
    for kind, i, g in geoms:
        f = ogr.Feature(defn)
        f.SetField("kind", kind)
        f.SetField("i", i)
        f.SetGeometry(g)
        layer.CreateFeature(f)


def _vector(path: Path, driver: str, code: int, options: list[str] | None = None) -> None:
    if path.exists():
        path.unlink()
    ds = ogr.GetDriverByName(driver).CreateDataSource(str(path))
    layer = ds.CreateLayer("features", _srs(4326), ogr.wkbUnknown, options=options or [])
    layer.CreateField(ogr.FieldDefn("kind", ogr.OFTString))
    layer.CreateField(ogr.FieldDefn("i", ogr.OFTInteger))
    _features(layer)
    ds = None
    if code != 4326:  # reproject with GDAL into the target CRS
        tmp = path.with_suffix(".tmp" + path.suffix)
        gdal.VectorTranslate(
            str(tmp), str(path), dstSRS=f"EPSG:{code}", reproject=True, format=driver
        )
        path.unlink()
        tmp.rename(path)


def _coords(path: Path, to_code: int | None = None) -> dict:
    """Every vertex as GDAL reads it (x = lon/easting), optionally reprojected by GDAL.

    Every layer of the file, in order; the CRS is the last layer's."""
    ds = ogr.Open(str(path))
    out = []
    crs = None
    for index in range(ds.GetLayerCount()):
        layer = ds.GetLayer(index)
        srs = layer.GetSpatialRef()
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        crs = f"{srs.GetAuthorityName(None)}:{srs.GetAuthorityCode(None)}"
        transform = None
        if to_code is not None:
            transform = osr.CoordinateTransformation(srs, _srs(to_code))
        for f in layer:
            g = f.GetGeometryRef().Clone()
            if transform is not None:
                g.Transform(transform)
            pts = []
            for part in _parts(g):
                pts.extend([list(p) for p in part.GetPoints()])
            out.append(
                {
                    "layer": layer.GetName(),
                    "kind": f.GetField("kind"),
                    "i": f.GetField("i"),
                    "xyz": pts,
                }
            )
    return {"crs": crs, "features": out}


def _parts(g: ogr.Geometry) -> list[ogr.Geometry]:
    if g.GetGeometryCount() == 0:
        return [g]
    out = []
    for i in range(g.GetGeometryCount()):
        out.extend(_parts(g.GetGeometryRef(i)))
    return out


def _raster(path: Path, gt: tuple, code: int, *, point: bool = False, options=None) -> dict:
    rows, cols = 4, 5
    data = np.arange(rows * cols, dtype=np.float32).reshape(rows, cols) * 1.5 + 100.0
    data[1, 2] = -9999.0
    ds = gdal.GetDriverByName("GTiff").Create(
        str(path), cols, rows, 1, gdal.GDT_Float32, options=options or []
    )
    if point:
        ds.SetMetadataItem("AREA_OR_POINT", "Point")
    ds.SetGeoTransform(gt)
    ds.SetSpatialRef(_srs(code))
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(data)
    ds = None
    # What GDAL reads back (its geotransform is always for pixel corners).
    ds = gdal.Open(str(path))
    gt_read = ds.GetGeoTransform()
    centres = [
        [
            gt_read[0] + (c + 0.5) * gt_read[1] + (r + 0.5) * gt_read[2],
            gt_read[3] + (c + 0.5) * gt_read[4] + (r + 0.5) * gt_read[5],
        ]
        for r in range(rows)
        for c in range(cols)
    ]
    return {
        "file": path.name,
        "geotransform": list(gt_read),
        "area_or_point": ds.GetMetadataItem("AREA_OR_POINT"),
        "crs": f"EPSG:{ds.GetSpatialRef().GetAuthorityCode(None)}",
        "size": [ds.RasterXSize, ds.RasterYSize],
        "nodata": ds.GetRasterBand(1).GetNoDataValue(),
        "pixel_centres": centres,
        "values": ds.GetRasterBand(1).ReadAsArray().astype(float).ravel().tolist(),
    }


def sources(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    _vector(out / "rfc7946.geojson", "GeoJSON", 4326, ["RFC7946=YES"])
    _vector(out / "wgs84.gpkg", "GPKG", 4326)
    _vector(out / "mga56.gpkg", "GPKG", 7856)
    # A pre-RFC 7946 file naming EPSG:4326 by URN: EPSG's axis order is lat, lon,
    # but GeoJSON positions are always lon, lat (GDAL and RFC 7946 agree).
    legacy = json.loads((out / "rfc7946.geojson").read_text())
    legacy["crs"] = {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}}
    (out / "legacy_epsg4326.geojson").write_text(json.dumps(legacy))

    gt = (502000.0, 2.0, 0.0, 6962000.0, 0.0, -2.0)
    rasters = [
        _raster(out / "area.tif", gt, 7856),
        _raster(out / "point.tif", gt, 7856, point=True),
        _raster(out / "south_up.tif", (502000.0, 2.0, 0.0, 6961992.0, 0.0, 2.0), 7856),
        _raster(out / "rotated.tif", (502000.0, 2.0, 0.5, 6962000.0, 0.5, -2.0), 7856),
        _raster(out / "geographic.tif", (153.0, 1e-4, 0.0, -27.47, 0.0, -1e-4), 4326),
        _raster(
            out / "tiled_bigtiff.tif",
            (300000.25, 0.5, 0.0, 5000000.75, 0.0, -0.5),
            32633,
            point=True,
            options=[
                "TILED=YES",
                "BLOCKXSIZE=16",
                "BLOCKYSIZE=16",
                "BIGTIFF=YES",
                "COMPRESS=DEFLATE",
                "PREDICTOR=3",
            ],
        ),
    ]
    info = {
        "gdal": gdal.__version__,
        "proj": ".".join(
            str(
                osr.GetPROJVersionMajor()
                if i == 0
                else osr.GetPROJVersionMinor()
                if i == 1
                else osr.GetPROJVersionMicro()
            )
            for i in range(3)
        ),
        "lon_lat": {"points": POINTS, "line": LINE, "ring": RING},
        "vectors": {
            name: {"read": _coords(out / name), "to_7856": _coords(out / name, 7856)}
            for name in ("rfc7946.geojson", "legacy_epsg4326.geojson", "wgs84.gpkg", "mga56.gpkg")
        },
        "rasters": rasters,
    }
    (out / "gdal_sources.json").write_text(json.dumps(info, indent=1) + "\n")


def readback(directory: Path) -> None:
    out = {}
    for name in ("geotools.geojson", "geotools.gpkg"):
        out[name] = {"read": _coords(directory / name), "to_4326": _coords(directory / name, 4326)}
    (directory / "gdal_readback.json").write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    {"sources": sources, "readback": readback}[sys.argv[1]](Path(sys.argv[2]))
    print("ok", sys.argv[1], math.pi > 3)
