# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0

import logging
log = logging.getLogger(__name__)

try:
    from osgeo import gdal
    HAS_GDAL = True
    log.debug("GDAL available: %s", gdal.__version__)
except ImportError:
    HAS_GDAL = False
    log.debug("GDAL not available")

try:
    import pyproj
    HAS_PYPROJ = True
    log.debug("PyProj available: %s", pyproj.__version__)
except ImportError:
    HAS_PYPROJ = False
    log.debug("PyProj not available")

try:
    import numpy
    HAS_NUMPY = True
    log.debug("NumPy available: %s", numpy.__version__)
except ImportError:
    HAS_NUMPY = False
    log.debug("NumPy not available")


def dep_versions():
    """Return a dict of {package: version_string_or_None} for all optional deps."""
    versions = {}
    if HAS_GDAL:
        from osgeo import gdal as _gdal
        versions["gdal"] = _gdal.__version__
    else:
        versions["gdal"] = None
    if HAS_PYPROJ:
        import pyproj as _pyproj
        versions["pyproj"] = _pyproj.__version__
    else:
        versions["pyproj"] = None
    if HAS_NUMPY:
        import numpy as _numpy
        versions["numpy"] = _numpy.__version__
    else:
        versions["numpy"] = None
    return versions
