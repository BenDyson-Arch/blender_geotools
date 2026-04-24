# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0

import math
import logging
log = logging.getLogger(__name__)

from .srs import SRS
from .utm import UTM, UTM_EPSG_CODES
from .ellps import GRS80
from .srv import MapTilerCoordinates
from ..errors import ReprojError
from ..checkdeps import HAS_GDAL, HAS_PYPROJ
from .. import settings as _settings_mod

if HAS_GDAL:
    from osgeo import osr

if HAS_PYPROJ:
    import pyproj


# ---------------------------------------------------------------------------
# Built-in transforms (no external deps)

def _web_merc_to_lonlat(x, y):
    k = GRS80.perimeter / 360
    lon = x / k
    lat = y / k
    lat = 180 / math.pi * (2 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
    return lon, lat


def _lonlat_to_web_merc(lon, lat):
    k = GRS80.perimeter / 360
    x = lon * k
    lat = math.log(math.tan((90 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = lat * k
    return x, y


# ---------------------------------------------------------------------------

class Reproj:
    """
    Reprojects points between two coordinate reference systems.

    Backend priority (AUTO mode): PyProj → GDAL → BUILTIN → MapTiler API.
    BUILTIN only supports WGS84 ↔ WebMercator and WGS84 ↔ UTM.
    """

    def __init__(self, crs1, crs2):
        try:
            crs1 = SRS(crs1)
            crs2 = SRS(crs2)
        except Exception as exc:
            raise ReprojError(str(exc))

        if crs1 == crs2:
            self._engine = "NO_REPROJ"
            _settings_mod.set_last_reproj("NO_REPROJ", f"{crs1} -> {crs2}")
            return

        engine = _settings_mod.settings.proj_engine  # AUTO|PYPROJ|GDAL|BUILTIN|MAPTILER

        if engine == "AUTO":
            if HAS_PYPROJ:
                engine = "PYPROJ"
            elif HAS_GDAL:
                engine = "GDAL"
            elif self._builtin_capable(crs1, crs2):
                engine = "BUILTIN"
            else:
                raise ReprojError(
                    f"No reprojection backend available for {crs1} -> {crs2}. "
                    f"Built-in engine only supports WGS84 ↔ Web Mercator and WGS84 ↔ UTM. "
                    f"Install PyProj or GDAL, or set a UTM/Web Mercator scene CRS."
                )
        else:
            if engine == "PYPROJ" and not HAS_PYPROJ:
                raise ReprojError("PyProj is not installed")
            if engine == "GDAL" and not HAS_GDAL:
                raise ReprojError("GDAL is not installed")
            if engine == "BUILTIN" and not self._builtin_capable(crs1, crs2):
                raise ReprojError(
                    "Built-in engine only supports WGS84 ↔ Web Mercator and WGS84 ↔ UTM"
                )

        self._engine = engine
        self._crs1 = crs1
        self._crs2 = crs2
        _settings_mod.set_last_reproj(engine, f"{crs1} -> {crs2}")

        if engine == "PYPROJ":
            self._transformer = pyproj.Transformer.from_crs(
                str(crs1), str(crs2), always_xy=True
            )

        elif engine == "GDAL":
            src = crs1.getOgrSpatialRef()
            dst = crs2.getOgrSpatialRef()
            self._osr_transform = osr.CoordinateTransformation(src, dst)

        elif engine == "BUILTIN":
            if crs1.isUTM:
                self._utm = UTM.init_from_epsg(crs1.code)
            elif crs2.isUTM:
                self._utm = UTM.init_from_epsg(crs2.code)

        elif engine == "MAPTILER":
            api_key = _settings_mod.settings.maptiler_api_key
            if not api_key:
                raise ReprojError(
                    "MapTiler API key required for reprojection. "
                    "Add it in GeoTools preferences."
                )
            if not (crs1.isEPSG and crs2.isEPSG):
                raise ReprojError("MapTiler backend requires EPSG codes")
            self._maptiler = MapTilerCoordinates(api_key)

    # ------------------------------------------------------------------

    @staticmethod
    def _builtin_capable(crs1, crs2) -> bool:
        wgs84_to_utm = crs1.isWGS84 and crs2.isUTM
        utm_to_wgs84 = crs1.isUTM and crs2.isWGS84
        wgs84_to_wm = crs1.isWGS84 and crs2.isWM
        wm_to_wgs84 = crs1.isWM and crs2.isWGS84
        return wgs84_to_utm or utm_to_wgs84 or wgs84_to_wm or wm_to_wgs84

    def pts(self, pts: list) -> list:
        """Reproject a list of (x, y) tuples. Returns list of (x, y) tuples."""
        if not pts:
            return []
        if self._engine == "NO_REPROJ":
            return list(pts)

        if self._engine == "PYPROJ":
            xs, ys = zip(*pts)
            xs_out, ys_out = self._transformer.transform(xs, ys)
            return list(zip(xs_out, ys_out))

        elif self._engine == "GDAL":
            transformed = self._osr_transform.TransformPoints(list(pts))
            return [(x, y) for x, y, _z in transformed]

        elif self._engine == "BUILTIN":
            c1, c2 = self._crs1.code, self._crs2.code
            if c1 == 4326 and c2 == 3857:
                return [_lonlat_to_web_merc(*p) for p in pts]
            if c1 == 3857 and c2 == 4326:
                return [_web_merc_to_lonlat(*p) for p in pts]
            if c1 == 4326 and c2 in UTM_EPSG_CODES:
                return [self._utm.lonlat_to_utm(*p) for p in pts]
            if c1 in UTM_EPSG_CODES and c2 == 4326:
                return [self._utm.utm_to_lonlat(*p) for p in pts]
            raise ReprojError("BUILTIN: unexpected CRS combination")

        elif self._engine == "MAPTILER":
            return self._maptiler.reproj_pts(
                self._crs1.code, self._crs2.code, list(pts)
            )

        raise ReprojError(f"Unknown engine: {self._engine}")

    def pt(self, x, y) -> tuple:
        """Reproject a single point. Returns (x, y)."""
        return self.pts([(x, y)])[0]

    def bbox(self, xmin, ymin, xmax, ymax) -> tuple:
        """
        Reproject a bounding box by transforming all four corners.
        Returns (xmin, ymin, xmax, ymax) in the target CRS.
        """
        corners = [(xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)]
        reproj = self.pts(corners)
        xs = [p[0] for p in reproj]
        ys = [p[1] for p in reproj]
        return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Convenience module-level functions

def reproj_pt(crs1, crs2, x, y) -> tuple:
    """Reproject a single point. Constructs a Reproj instance each call — avoid in loops."""
    return Reproj(crs1, crs2).pt(x, y)


def reproj_pts(crs1, crs2, pts: list) -> list:
    """Reproject a list of (x, y) tuples."""
    return Reproj(crs1, crs2).pts(pts)


def reproj_bbox(crs1, crs2, xmin, ymin, xmax, ymax) -> tuple:
    """Reproject a bounding box. Returns (xmin, ymin, xmax, ymax)."""
    return Reproj(crs1, crs2).bbox(xmin, ymin, xmax, ymax)
