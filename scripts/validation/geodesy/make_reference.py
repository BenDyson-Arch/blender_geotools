"""Regenerate the geodesy reference values (tests/data/validation/geodesy/measure.json).

Not run in CI. Needs the pinned reference tools, never GeoTools::

    uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \\
        --with geographiclib==2.1 python scripts/validation/geodesy/make_reference.py

What is independent of GeoTools' own path (pyproj's ``get_factors`` and
inverse projection):

* transverse Mercator sites: E/N, latitude and longitude, the point scale
  factor and the convergence from Krüger's series (``refgeo``), with the
  projection constants typed in here, not read from PROJ;
* other projections: latitude and longitude by Newton's method on PROJ's
  *forward* projection, and the scale and convergence from a central-difference
  Jacobian of it against the ellipsoid's radii;
* every line: the ellipsoidal length and midpoint azimuth from GeographicLib;
* every polygon: the ellipsoidal area of the grid polygon from GeographicLib,
  its edges densified every few metres so the geodesic edges follow the grid
  edges.

Scene points are float32 values and offsets whole metres, so E and N are exact.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import geographiclib
import numpy as np
import pyproj
from geographiclib.geodesic import Geodesic

sys.path.insert(0, str(Path(__file__).resolve().parent))
import refgeo

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "tests" / "data" / "validation" / "geodesy" / "measure.json"

# Sites: (name, CRS, ellipsoid, TM constants or None, lon, lat, what it stresses).
# The constants are typed from the EPSG definitions, not taken from PROJ.
MGA = {"k0": 0.9996, "false_e": 500000.0, "false_n": 10000000.0}
UTM_N = {"k0": 0.9996, "false_e": 500000.0, "false_n": 0.0}
SITES = [
    (
        "mga56-central",
        "EPSG:7856",
        refgeo.GRS80,
        {**MGA, "lon0": 153.0},
        153.03,
        -27.47,
        "MGA2020 zone 56 near the central meridian",
    ),
    (
        "mga56-edge",
        "EPSG:7856",
        refgeo.GRS80,
        {**MGA, "lon0": 153.0},
        150.0,
        -30.0,
        "MGA zone 56 at its west edge (convergence 1.5 deg)",
    ),
    (
        "mga56-overlap",
        "EPSG:7856",
        refgeo.GRS80,
        {**MGA, "lon0": 153.0},
        149.6,
        -33.0,
        "0.4 deg outside zone 56, in the zone overlap",
    ),
    (
        "utm33n",
        "EPSG:32633",
        refgeo.WGS84,
        {**UTM_N, "lon0": 15.0},
        16.4,
        48.2,
        "UTM north hemisphere",
    ),
    (
        "utm18s",
        "EPSG:32718",
        refgeo.WGS84,
        {**MGA, "lon0": -75.0},
        -76.6,
        -12.1,
        "UTM south hemisphere, west longitudes",
    ),
    (
        "utm33n-arctic",
        "EPSG:32633",
        refgeo.WGS84,
        {**UTM_N, "lon0": 15.0},
        17.5,
        78.4,
        "UTM at 78 deg N",
    ),
    (
        "utm33n-far",
        "EPSG:32633",
        refgeo.WGS84,
        {**UTM_N, "lon0": 15.0},
        24.0,
        45.0,
        "9 deg from the central meridian (k = 1.0089)",
    ),
    ("lambert93", "EPSG:2154", refgeo.GRS80, None, 2.35, 48.85, "Lambert conformal conic (2SP)"),
    (
        "antarctic-ps",
        "EPSG:3031",
        refgeo.WGS84,
        None,
        166.7,
        -77.8,
        "Antarctic polar stereographic",
    ),
    ("ups-north", "EPSG:5041", refgeo.WGS84, None, 40.0, 89.5, "UPS north, 0.5 deg from the pole"),
    (
        "albers",
        "EPSG:3577",
        refgeo.GRS80,
        None,
        150.0,
        -12.0,
        "Australian Albers (not conformal), far from its standard parallels",
    ),
    (
        "webmercator",
        "EPSG:3857",
        refgeo.WGS84,
        None,
        10.0,
        60.0,
        "Web Mercator (conformal only on a sphere)",
    ),
]

LENGTHS = (10.0, 1000.0, 10000.0, 50000.0)
BEARINGS = (0.0, 45.0, 90.0, 135.0, 200.0, 300.0)
HEIGHTS = (0.0, 1500.0)  # ellipsoidal height of the line's start; the end is 37.5 m higher


def _f32(value: float) -> float:
    return float(np.float32(value))


class Frame:
    """Forward and inverse projection, scale and convergence, by the reference route."""

    def __init__(self, crs: str, ellipsoid: tuple[float, float], tm: dict | None) -> None:
        self.crs = crs
        self.a, self.f = ellipsoid
        self.tm = refgeo.TransverseMercator(self.a, self.f, **tm) if tm else None
        geodetic = pyproj.CRS(crs).geodetic_crs
        self._fwd = pyproj.Transformer.from_crs(geodetic, crs, always_xy=True)

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        if self.tm:
            e, n, _k, _g = self.tm.forward(lon, lat)
            return e, n
        return self._fwd.transform(lon, lat, errcheck=True)

    def inverse(self, east: float, north: float, guess: tuple[float, float]) -> tuple[float, float]:
        if self.tm:
            return self.tm.inverse(east, north)
        lon, lat = guess
        for _ in range(60):
            e0, n0 = self.forward(lon, lat)
            de, dn = east - e0, north - n0
            if math.hypot(de, dn) < 1e-9:
                break
            jac = self.jacobian_degrees(lon, lat)
            dlon, dlat = np.linalg.solve(jac, [de, dn])
            lon, lat = lon + float(dlon), lat + float(dlat)
        return lon, lat

    def jacobian_degrees(self, lon: float, lat: float, h: float = 1e-6) -> np.ndarray:
        """Grid metres per degree of (lon, lat), central differences."""
        e1, n1 = self.forward(lon + h, lat)
        e0, n0 = self.forward(lon - h, lat)
        e3, n3 = self.forward(lon, lat + h)
        e2, n2 = self.forward(lon, lat - h)
        return np.array([[e1 - e0, e3 - e2], [n1 - n0, n3 - n2]]) / (2 * h)

    def to_ellipsoid(self, lon: float, lat: float) -> np.ndarray:
        """Grid step to true (east, north) metres on the ellipsoid."""
        m, n = refgeo.radii(self.a, self.f, lat)
        metric = np.diag([n * math.cos(math.radians(lat)), m]) * math.pi / 180.0
        return metric @ np.linalg.inv(self.jacobian_degrees(lon, lat))

    def scale_and_convergence(self, lon: float, lat: float) -> tuple[float, float]:
        """(scale along grid north, convergence): true north to grid north, clockwise."""
        if self.tm:
            _e, _n, k, gamma = self.tm.forward(lon, lat)
            return k, gamma
        g = self.to_ellipsoid(lon, lat)
        north_step = g[:, 1]
        jac = self.jacobian_degrees(lon, lat)
        # Grid bearing of a step due north is minus the convergence.
        gamma = -math.degrees(math.atan2(jac[0, 1], jac[1, 1]))
        return 1.0 / float(np.linalg.norm(north_step)), gamma


def _site(name, crs, ellipsoid, tm, lon, lat, about):
    frame = Frame(crs, ellipsoid, tm)
    geod = Geodesic(*ellipsoid)
    e0, n0 = frame.forward(lon, lat)
    origin = (float(round(e0)), float(round(n0)))
    # Points: the origin, and float32 locals out to 50 km.
    points = []
    for local in [(0.0, 0.0, 0.0), (1234.567, -89.1, 3.3), (-25000.25, 40000.5, 812.0)]:
        lx, ly, lz = (_f32(v) for v in local)
        east, north = origin[0] + lx, origin[1] + ly
        plon, plat = frame.inverse(east, north, (lon, lat))
        k, gamma = frame.scale_and_convergence(plon, plat)
        points.append(
            {"local": [lx, ly, lz], "lon": plon, "lat": plat, "scale": k, "convergence": gamma}
        )
    lines = []
    for length in LENGTHS:
        for i, bearing in enumerate(BEARINGS):
            height = HEIGHTS[i % 2]
            b = math.radians(bearing)
            start = (_f32(-0.5 * length * math.sin(b)), _f32(-0.5 * length * math.cos(b)))
            end = (_f32(0.5 * length * math.sin(b)), _f32(0.5 * length * math.cos(b)))
            ends = [
                frame.inverse(origin[0] + x, origin[1] + y, (lon, lat)) for x, y in (start, end)
            ]
            line = geod.InverseLine(ends[0][1], ends[0][0], ends[1][1], ends[1][0])
            middle = line.Position(line.s13 / 2.0)
            gaussian = math.sqrt(math.prod(refgeo.radii(*ellipsoid, middle["lat2"])))
            euler = refgeo.euler_radius(*ellipsoid, middle["lat2"], middle["azi2"])
            h = height + 37.5 / 2.0
            grid = math.hypot(end[0] - start[0], end[1] - start[1])
            lines.append(
                {
                    "start": [start[0], start[1], 0.0],
                    "end": [end[0], end[1], 37.5],
                    "start_height": height,
                    "ellipsoid": line.s13,
                    "line_scale": grid / line.s13,
                    "azimuth_middle": middle["azi2"],
                    "lat_middle": middle["lat2"],
                    "ground_gaussian": line.s13 * (gaussian + h) / gaussian,
                    "ground_euler": line.s13 * (euler + h) / euler,
                }
            )
    polygons = []
    for side, turn, height in ((1000.0, 0.0, 0.0), (10000.0, 30.0, 0.0), (10000.0, 30.0, 800.0)):
        t = math.radians(turn)
        corners = [
            (-side / 2, -side / 2),
            (side / 2, -side / 2),
            (side / 2, side / 2),
            (-side / 2, side / 2),
        ]
        corners = [
            (_f32(x * math.cos(t) - y * math.sin(t)), _f32(x * math.sin(t) + y * math.cos(t)))
            for x, y in corners
        ]
        poly = geod.Polygon()
        steps = int(side / 5.0)
        for (x0, y0), (x1, y1) in zip(corners, corners[1:] + corners[:1], strict=True):
            for j in range(steps):
                f = j / steps
                plon, plat = frame.inverse(
                    origin[0] + x0 + (x1 - x0) * f, origin[1] + y0 + (y1 - y0) * f, (lon, lat)
                )
                poly.AddPoint(plat, plon)
        _n, _perimeter, area = poly.Compute(False, True)
        _clon, clat = frame.inverse(*origin, (lon, lat))
        gaussian = math.sqrt(math.prod(refgeo.radii(*ellipsoid, clat)))
        polygons.append(
            {
                "corners": [list(c) for c in corners],
                "height": height,
                "ellipsoid_area": abs(area),
                "ground_area": abs(area) * ((gaussian + height) / gaussian) ** 2,
            }
        )
    return {
        "name": name,
        "crs": crs,
        "about": about,
        "ellipsoid": list(ellipsoid),
        "origin": list(origin),
        "points": points,
        "lines": lines,
        "polygons": polygons,
    }


def _radii_checks() -> list[dict]:
    """Gaussian radius sqrt(MN), with M and N measured on GeographicLib geodesics."""
    out = []
    for ellipsoid in (refgeo.GRS80, refgeo.WGS84):
        geod = Geodesic(*ellipsoid)
        for lat in (0.0, -27.5, 45.0, 60.0, 78.4, 89.0):
            d = 1e-4
            m = geod.Inverse(lat - d, 0.0, lat + d, 0.0)["s12"] / math.radians(2 * d)
            arc = geod.Inverse(lat, 0.0, lat, 2 * d)["s12"]
            n = arc / (math.radians(2 * d) * math.cos(math.radians(lat)))
            out.append({"ellipsoid": list(ellipsoid), "lat": lat, "gaussian": math.sqrt(m * n)})
    return out


def _icsm() -> dict:
    """The ICSM GDA Technical Manual's worked example: Flinders Peak to Buninyong.

    Published (GRS80): ellipsoidal distance 54 972.271 m, azimuth 306 52 05.37,
    reverse azimuth 127 10 25.07; Flinders Peak MGA zone 55 E 273 741.297,
    N 5 796 489.777. Buninyong's grid coordinates are computed here from its
    published latitude and longitude, by the Krüger series.
    """
    flinders = (144 + 25 / 60 + 29.52440 / 3600, -(37 + 57 / 60 + 3.72030 / 3600))
    buninyong = (143 + 55 / 60 + 35.38390 / 3600, -(37 + 39 / 60 + 10.15610 / 3600))
    tm = refgeo.TransverseMercator(*refgeo.GRS80, lon0=147.0, **MGA)
    fe, fn, fk, fg = tm.forward(*flinders)
    be, bn, bk, bg = tm.forward(*buninyong)
    geod = Geodesic(*refgeo.GRS80)
    line = geod.InverseLine(flinders[1], flinders[0], buninyong[1], buninyong[0])
    middle = line.Position(line.s13 / 2.0)
    return {
        "crs": "EPSG:28355",
        "published": {
            "distance": 54972.271,
            "azimuth_dms": [306, 52, 5.37],
            "reverse_azimuth_dms": [127, 10, 25.07],
            "flinders_peak_en": [273741.297, 5796489.777],
        },
        "flinders_peak": {
            "lon": flinders[0],
            "lat": flinders[1],
            "en": [fe, fn],
            "scale": fk,
            "convergence": fg,
        },
        "buninyong": {
            "lon": buninyong[0],
            "lat": buninyong[1],
            "en": [be, bn],
            "scale": bk,
            "convergence": bg,
        },
        "geographiclib": {
            "distance": line.s13,
            "azimuth": line.azi1,
            "reverse_azimuth": (line.Position(line.s13)["azi2"] + 180.0) % 360.0,
            "azimuth_middle": middle["azi2"],
        },
    }


def main() -> None:
    data = {
        "generator": "scripts/validation/geodesy/make_reference.py",
        "versions": {
            "pyproj": pyproj.__version__,
            "proj": pyproj.proj_version_str,
            "geographiclib": geographiclib.__version__,
            "numpy": np.__version__,
        },
        "sites": [_site(*s) for s in SITES],
        "radii": _radii_checks(),
        "icsm": _icsm(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
