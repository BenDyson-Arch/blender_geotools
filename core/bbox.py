# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0
#
# Bug fix vs. original: __iter__ for 3D BBOX returned ymax twice instead of zmax.


class _XY:
    """Minimal 2D/3D coordinate container used by BBOX.center and BBOX.dimensions."""

    def __init__(self, x, y, z=None):
        self.x = x
        self.y = y
        self.z = z

    def __iter__(self):
        if self.z is not None:
            return iter((self.x, self.y, self.z))
        return iter((self.x, self.y))

    def __repr__(self):
        if self.z is not None:
            return f"XY({self.x}, {self.y}, {self.z})"
        return f"XY({self.x}, {self.y})"


class BBOX:
    """
    Immutable-style 2D or 3D axis-aligned bounding box.

    Construction options:
        BBOX(xmin, ymin, xmax, ymax)
        BBOX(xmin, ymin, zmin, xmax, ymax, zmax)
        BBOX((xmin, ymin, xmax, ymax))
        BBOX(xmin=, ymin=, xmax=, ymax=, [zmin=, zmax=])
    """

    def __init__(self, *args, **kwargs):
        if args:
            if len(args) == 1:
                args = args[0]
            if len(args) == 4:
                self.xmin, self.ymin, self.xmax, self.ymax = args
            elif len(args) == 6:
                self.xmin, self.ymin, self.zmin, self.xmax, self.ymax, self.zmax = args
            else:
                raise ValueError(f"BBOX expects 4 or 6 positional arguments, got {len(args)}")
        elif kwargs:
            required = {"xmin", "ymin", "xmax", "ymax"}
            if not required.issubset(kwargs):
                raise ValueError(f"BBOX missing required keyword arguments: {required - kwargs.keys()}")
            self.xmin = kwargs["xmin"]
            self.ymin = kwargs["ymin"]
            self.xmax = kwargs["xmax"]
            self.ymax = kwargs["ymax"]
            if "zmin" in kwargs and "zmax" in kwargs:
                self.zmin = kwargs["zmin"]
                self.zmax = kwargs["zmax"]
        else:
            raise ValueError("BBOX requires positional or keyword arguments")

    # ------------------------------------------------------------------

    @property
    def hasZ(self):
        return hasattr(self, "zmin") and hasattr(self, "zmax")

    def to2D(self):
        return BBOX(self.xmin, self.ymin, self.xmax, self.ymax)

    @classmethod
    def fromXYZ(cls, lst):
        """Create from (xmin, xmax, ymin, ymax) or (xmin, xmax, ymin, ymax, zmin, zmax)."""
        if len(lst) == 4:
            xmin, xmax, ymin, ymax = lst
            return cls(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax)
        elif len(lst) == 6:
            xmin, xmax, ymin, ymax, zmin, zmax = lst
            return cls(xmin=xmin, ymin=ymin, zmin=zmin, xmax=xmax, ymax=ymax, zmax=zmax)
        raise ValueError("fromXYZ expects 4 or 6 values")

    def toXYZ(self):
        if self.hasZ:
            return (self.xmin, self.xmax, self.ymin, self.ymax, self.zmin, self.zmax)
        return (self.xmin, self.xmax, self.ymin, self.ymax)

    @classmethod
    def fromLatlon(cls, lst):
        """Create from (latmin, lonmin, latmax, lonmax)."""
        ymin, xmin, ymax, xmax = lst
        return cls(xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax)

    def toLatlon(self):
        return (self.ymin, self.xmin, self.ymax, self.xmax)

    # ------------------------------------------------------------------

    def __iter__(self):
        if self.hasZ:
            return iter([self.xmin, self.ymin, self.zmin, self.xmax, self.ymax, self.zmax])
        return iter([self.xmin, self.ymin, self.xmax, self.ymax])

    def __str__(self):
        if self.hasZ:
            return f"xmin:{self.xmin}, ymin:{self.ymin}, zmin:{self.zmin}, xmax:{self.xmax}, ymax:{self.ymax}, zmax:{self.zmax}"
        return f"xmin:{self.xmin}, ymin:{self.ymin}, xmax:{self.xmax}, ymax:{self.ymax}"

    def __repr__(self):
        return f"BBOX({self})"

    def __eq__(self, bb):
        if self.xmin != bb.xmin or self.xmax != bb.xmax:
            return False
        if self.ymin != bb.ymin or self.ymax != bb.ymax:
            return False
        if self.hasZ and bb.hasZ:
            return self.zmin == bb.zmin and self.zmax == bb.zmax
        return not self.hasZ and not bb.hasZ

    # ------------------------------------------------------------------
    # Spatial tests

    def overlap(self, bb) -> bool:
        def _ov(a_min, a_max, b_min, b_max):
            return not (a_min > b_max or b_min > a_max)
        return _ov(self.xmin, self.xmax, bb.xmin, bb.xmax) and _ov(self.ymin, self.ymax, bb.ymin, bb.ymax)

    def isWithin(self, bb) -> bool:
        return bb.xmin <= self.xmin and bb.xmax >= self.xmax and bb.ymin <= self.ymin and bb.ymax >= self.ymax

    def contains(self, bb) -> bool:
        return bb.xmin > self.xmin and bb.xmax < self.xmax and bb.ymin > self.ymin and bb.ymax < self.ymax

    # ------------------------------------------------------------------
    # Arithmetic

    def __add__(self, bb):
        """Union of two bounding boxes."""
        xmin = min(self.xmin, bb.xmin)
        xmax = max(self.xmax, bb.xmax)
        ymin = min(self.ymin, bb.ymin)
        ymax = max(self.ymax, bb.ymax)
        if self.hasZ and bb.hasZ:
            return BBOX(xmin, ymin, min(self.zmin, bb.zmin), xmax, ymax, max(self.zmax, bb.zmax))
        return BBOX(xmin, ymin, xmax, ymax)

    def shift(self, dx, dy):
        self.xmin += dx
        self.xmax += dx
        self.ymin += dy
        self.ymax += dy

    # ------------------------------------------------------------------
    # Geometry helpers

    @property
    def center(self):
        x = (self.xmin + self.xmax) / 2
        y = (self.ymin + self.ymax) / 2
        if self.hasZ:
            return _XY(x, y, (self.zmin + self.zmax) / 2)
        return _XY(x, y)

    @property
    def dimensions(self):
        dx = self.xmax - self.xmin
        dy = self.ymax - self.ymin
        if self.hasZ:
            return _XY(dx, dy, self.zmax - self.zmin)
        return _XY(dx, dy)

    @property
    def ul(self):
        return (self.xmin, self.ymax)

    @property
    def ur(self):
        return (self.xmax, self.ymax)

    @property
    def br(self):
        return (self.xmax, self.ymin)

    @property
    def bl(self):
        return (self.xmin, self.ymin)

    @property
    def corners(self):
        """[ul, ur, br, bl] — upper-left first, clockwise."""
        return [self.ul, self.ur, self.br, self.bl]
