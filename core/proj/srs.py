# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0

import logging
log = logging.getLogger(__name__)

from .utm import UTM_EPSG_CODES
from ..checkdeps import HAS_GDAL, HAS_PYPROJ

if HAS_GDAL:
    from osgeo import osr

if HAS_PYPROJ:
    import pyproj


class SRS:
    """
    Lightweight wrapper around a Spatial Reference System definition.

    Accepts:
    - Integer or string EPSG code:  4326 / "4326"
    - SRID string:                  "EPSG:4326" / "epsg:4326"
    - Proj4 string:                 "+proj=utm +zone=34 +datum=WGS84 ..."
    """

    @classmethod
    def validate(cls, crs):
        try:
            cls(crs)
            return True
        except Exception:
            log.debug("Cannot validate CRS: %s", crs, exc_info=True)
            return False

    def __init__(self, crs):
        crs = str(crs).strip()

        # Case 1: plain numeric code → assume EPSG
        if crs.isdigit():
            self.auth = "EPSG"
            self.code = int(crs)

        # Case 2: AUTH:CODE
        elif ":" in crs:
            auth_part, code_part = crs.split(":", 1)
            if not code_part.isdigit():
                raise ValueError(f"Invalid CRS (non-numeric code): {crs}")
            # Strip legacy "+init=" prefix (e.g. "+init=epsg:4326")
            if auth_part.lower().startswith("+init="):
                auth_part = auth_part[len("+init="):]
            self.auth = auth_part.upper()
            self.code = int(code_part)

        # Case 3: proj4 string (every token starts with '+')
        elif all(param.startswith("+") for param in crs.split() if param):
            self.auth = None
            self.code = None
            self._proj4_str = crs

        else:
            raise ValueError(f"Invalid CRS: {crs}")

    # ------------------------------------------------------------------
    # Properties

    @property
    def proj4(self):
        if hasattr(self, "_proj4_str"):
            return self._proj4_str
        if self.isSRID:
            if HAS_PYPROJ:
                try:
                    return pyproj.CRS(str(self)).to_proj4()
                except Exception:
                    pass
            if HAS_GDAL:
                try:
                    sref = self.getOgrSpatialRef()
                    return sref.ExportToProj4()
                except Exception:
                    pass
        return None

    @property
    def SRID(self):
        if self.isSRID:
            return f"{self.auth}:{self.code}"
        return None

    @property
    def hasCode(self):
        return self.code is not None

    @property
    def hasAuth(self):
        return self.auth is not None

    @property
    def isSRID(self):
        return self.hasAuth and self.hasCode

    @property
    def isEPSG(self):
        return self.auth == "EPSG" and self.code is not None

    @property
    def isWM(self):
        return self.auth == "EPSG" and self.code == 3857

    @property
    def isWGS84(self):
        return self.auth == "EPSG" and self.code == 4326

    @property
    def isUTM(self):
        return self.auth == "EPSG" and self.code in UTM_EPSG_CODES

    @property
    def isGeo(self):
        if self.code == 4326:
            return True
        if HAS_PYPROJ:
            try:
                return pyproj.CRS(str(self)).is_geographic
            except Exception:
                pass
        if HAS_GDAL:
            try:
                return bool(self.getOgrSpatialRef().IsGeographic())
            except Exception:
                pass
        return None

    def __str__(self):
        """Best string representation: SRID if available, else proj4."""
        return self.SRID if self.isSRID else (self._proj4_str if hasattr(self, "_proj4_str") else "")

    def __eq__(self, other):
        return str(self) == str(other)

    def __repr__(self):
        return f"SRS({str(self)!r})"

    # ------------------------------------------------------------------
    # Backend object constructors

    def getOgrSpatialRef(self):
        """Build a GDAL osr.SpatialReference object."""
        if not HAS_GDAL:
            raise ImportError("GDAL not available")
        prj = osr.SpatialReference()
        prj.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if self.isEPSG:
            r = prj.ImportFromEPSG(self.code)
        elif hasattr(self, "_proj4_str"):
            r = prj.ImportFromProj4(self._proj4_str)
        else:
            raise ValueError(f"Cannot build OGR SpatialReference for {self}")
        if r != 0:
            raise ValueError(f"GDAL rejected CRS: {self}")
        return prj

    def getPyProjCRS(self):
        """Build a pyproj.CRS object."""
        if not HAS_PYPROJ:
            raise ImportError("PyProj not available")
        return pyproj.CRS(str(self))
