"""Regenerate the placement reference (tests/data/validation/geodesy/placement.json).

Not run in CI. Where published vertices land on Earth, computed without PROJ's
inverse projection or rusty-tiles::

    uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \\
        python scripts/validation/geodesy/make_placement_reference.py

SOURCE_CRS sites: each float32 scene vertex is grid E = x + offset E,
N = y + offset N, ellipsoidal height h = z + offset A + height offset.
Latitude and longitude come from Krüger's series (transverse Mercator) or
Newton's method on PROJ's forward projection (other projections), and ECEF
from the closed form on the CRS's own ellipsoid. Every datum here goes to
WGS 84 by a null transformation in EPSG (GDA2020, GDA94, RGF93 and WGS 84
itself), so the Cartesian coordinates carry over unchanged; the GRS80 and WGS 84
ellipsoids differ by 0.1 mm in the semi-minor axis, which is the size of the
ambiguity. pyproj's own route (EPSG:nnnn to EPSG:4978) is recorded beside it.

ANCHOR sites: ECEF = anchor + ENU basis @ (heading rotation @ (local + scene
offset)), the closed form.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_reference as mr
import refgeo

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "tests" / "data" / "validation" / "geodesy" / "placement.json"

SOURCE_SITES = [
    ("mga56-edge", "EPSG:7856", refgeo.GRS80, {**mr.MGA, "lon0": 153.0}, 150.0, -30.0),
    ("utm33n-arctic", "EPSG:32633", refgeo.WGS84, {**mr.UTM_N, "lon0": 15.0}, 17.5, 78.4),
    ("utm18s", "EPSG:32718", refgeo.WGS84, {**mr.MGA, "lon0": -75.0}, -76.6, -12.1),
    ("lambert93", "EPSG:2154", refgeo.GRS80, None, 2.35, 48.85),
    ("antarctic-ps", "EPSG:3031", refgeo.WGS84, None, 166.7, -77.8),
    ("albers-gda94", "EPSG:3577", refgeo.GRS80, None, 150.0, -12.0),
]
ANCHOR_SITES = [
    ("anchor-brisbane", (153.02, -27.47, 25.0), 30.0, (5.0, -3.0, 1.0)),
    ("anchor-arctic", (17.5, 78.4, 310.0), -120.0, (-2000.0, 1500.0, 0.0)),
]
A_OFFSET, HEIGHT_OFFSET = 12.5, 40.0


def _vertices() -> np.ndarray:
    """A 5 x 5 grid over +-5 km with relief, as float32 (what a GLB holds)."""
    s = np.linspace(-5000.0, 5000.0, 5)
    x, y = np.meshgrid(s, s)
    z = 100.0 * np.sin(x / 3000.0) + 50.0 * np.cos(y / 2000.0)
    return np.stack([x.ravel(), y.ravel(), z.ravel()], 1).astype(np.float32)


def main() -> None:
    vertices = _vertices()
    sources = []
    for name, crs, ellipsoid, tm, lon, lat in SOURCE_SITES:
        frame = mr.Frame(crs, ellipsoid, tm)
        e0, n0 = frame.forward(lon, lat)
        origin = (float(round(e0)), float(round(n0)))
        ecef, geodetic = [], []
        to_ecef = pyproj.Transformer.from_crs(crs, "EPSG:4978", always_xy=True)
        pyproj_ecef = []
        for x, y, z in vertices.astype(np.float64):
            east, north = origin[0] + x, origin[1] + y
            vlon, vlat = frame.inverse(east, north, (lon, lat))
            h = z + A_OFFSET + HEIGHT_OFFSET
            geodetic.append([vlon, vlat, h])
            ecef.append(list(refgeo.ecef(*ellipsoid, vlon, vlat, h)))
            pyproj_ecef.append(list(to_ecef.transform(east, north, h)))
        sources.append(
            {
                "name": name,
                "crs": crs,
                "pyproj_operation": (
                    to_ecef.description
                    if to_ecef.description.startswith("Inverse")
                    else "several candidates; PROJ picks per point"
                ),
                "source_offset": [origin[0], origin[1], A_OFFSET],
                "height_offset": HEIGHT_OFFSET,
                "geodetic": geodetic,
                "ecef": ecef,
                "pyproj_ecef": pyproj_ecef,
            }
        )
    anchors = []
    for name, anchor, heading, offset in ANCHOR_SITES:
        lon, lat, h = anchor
        origin = np.array(refgeo.ecef(*refgeo.WGS84, lon, lat, h))
        t = math.radians(heading)
        turn = np.array([[math.cos(t), -math.sin(t), 0], [math.sin(t), math.cos(t), 0], [0, 0, 1]])
        enu = (vertices.astype(np.float64) + offset) @ turn.T
        ecef = origin + enu @ refgeo.enu_basis(lon, lat)
        to_geodetic = pyproj.Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
        geodetic = np.stack(to_geodetic.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2]), 1)
        anchors.append(
            {
                "name": name,
                "anchor": list(anchor),
                "heading": heading,
                "scene_offset": list(offset),
                "ecef": ecef.tolist(),
                "geodetic": geodetic.tolist(),
            }
        )
    data = {
        "generator": "scripts/validation/geodesy/make_placement_reference.py",
        "versions": {"pyproj": pyproj.__version__, "proj": pyproj.proj_version_str},
        "vertices": vertices.astype(np.float64).tolist(),
        "source_crs": sources,
        "anchor": anchors,
    }
    OUT.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
