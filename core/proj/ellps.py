# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0

import math


class Ellps:
    """Ellipsoid definition."""

    def __init__(self, a, b):
        self.a = a                          # equatorial radius (metres)
        self.b = b                          # polar radius (metres)
        self.f = (self.a - self.b) / self.a # flattening
        self.perimeter = 2 * math.pi * self.a


GRS80 = Ellps(6378137, 6356752.314245)


def dd2meters(dst):
    """Approximate conversion from decimal degrees to metres (true at equator only)."""
    k = GRS80.perimeter / 360
    return dst * k


def meters2dd(dst):
    """Approximate conversion from metres to decimal degrees (true at equator only)."""
    k = GRS80.perimeter / 360
    return dst / k
