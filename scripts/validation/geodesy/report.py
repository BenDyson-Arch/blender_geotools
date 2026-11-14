"""Compare GeoTools' Measure maths with the reference values and print the tables.

Runs GeoTools (``geotools.analysis.site``) on tests/data/validation/geodesy/measure.json
and prints Markdown tables for docs/validation/geodesy.md::

    uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \\
        python scripts/validation/geodesy/report.py

The CI test (tests/core/test_validation_geodesy.py) asserts the same quantities
against the tolerances in the doc.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from geotools.analysis import site
from geotools.core.placement import Placement, PlacementMode

DATA = ROOT / "tests" / "data" / "validation" / "geodesy" / "measure.json"


def placement(entry: dict, height_offset: float = 0.0) -> Placement:
    east, north = entry["origin"]
    return Placement(
        PlacementMode.SOURCE_CRS,
        source_crs=entry["crs"],
        source_offset=(east, north, 0.0),
        height_offset=height_offset,
    )


def angle(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def points_table(data: dict) -> None:
    print(
        '| Site | CRS | lat/lon max error (m) | scale factor max error | convergence max error (") |'
    )
    print("|---|---|---|---|---|")
    for entry in data["sites"]:
        p = placement(entry)
        dpos = dk = dg = 0.0
        a, f = entry["ellipsoid"]
        for point in entry["points"]:
            r = site.read_point(p, tuple(point["local"]))
            m = a * (1 - f * (2 - f))  # ~ metres per radian of latitude, enough for an error
            dlat = math.radians(r.latitude - point["lat"]) * m
            dlon = math.radians(r.longitude - point["lon"]) * a * math.cos(math.radians(r.latitude))
            dpos = max(dpos, math.hypot(dlat, dlon))
            dk = max(dk, abs(r.grid_scale - point["scale"]))
            dg = max(dg, abs(angle(r.convergence, point["convergence"])) * 3600)
        print(f"| {entry['name']} | {entry['crs']} | {dpos:.1e} | {dk:.1e} | {dg:.1e} |")


def lines_table(data: dict) -> None:
    print()
    print(
        "| Site | Lines | ellipsoid distance max rel. error (ppm) by length 10 m / 1 km / "
        '10 km / 50 km | true bearing max error (") by length | ground vs Gaussian-radius '
        "reference (ppm) | ground vs Euler-radius reference (ppm) |"
    )
    print("|---|---|---|---|---|---|")
    for entry in data["sites"]:
        by_length: dict[float, list[float]] = {}
        by_length_az: dict[float, list[float]] = {}
        gauss, euler = [], []
        for line in entry["lines"]:
            p = placement(entry, height_offset=line["start_height"])
            m = site.measure(p, tuple(line["start"]), tuple(line["end"]))
            ellipsoid = m.grid_distance / m.grid_scale
            length = round(
                math.hypot(line["end"][0] - line["start"][0], line["end"][1] - line["start"][1]), -1
            )
            rel = (ellipsoid - line["ellipsoid"]) / line["ellipsoid"] * 1e6
            by_length.setdefault(length, []).append(abs(rel))
            by_length_az.setdefault(length, []).append(
                abs(angle(m.true_bearing, line["azimuth_middle"])) * 3600
            )
            gauss.append(abs(m.ground_distance / line["ground_gaussian"] - 1) * 1e6)
            euler.append(abs(m.ground_distance / line["ground_euler"] - 1) * 1e6)
        lengths = sorted(by_length)
        dist = " / ".join(f"{max(by_length[k]):.1e}" for k in lengths)
        az = " / ".join(f"{max(by_length_az[k]):.1e}" for k in lengths)
        print(
            f"| {entry['name']} | {len(entry['lines'])} | {dist} | {az} | "
            f"{max(gauss):.1e} | {max(euler):.2f} |"
        )


def areas_table(data: dict) -> None:
    print()
    print(
        "| Site | 1 km square, 2 triangles | 10 km square, 2 triangles | 10 km, 8 x 8 cells | "
        "10 km, 32 x 32 cells |"
    )
    print("|---|---|---|---|---|")
    for entry in data["sites"]:
        cells = []
        for index, n in ((0, 1), (1, 1), (1, 8), (1, 32)):
            worst = 0.0
            for poly in entry["polygons"]:
                side = math.dist(poly["corners"][0], poly["corners"][1])
                if (index == 0) != (side < 5000):
                    continue
                p = placement(entry, height_offset=poly["height"])
                reading = site.face_areas(p, _triangulate(np.array(poly["corners"]), n))
                worst = max(worst, abs(reading.ground_plan / poly["ground_area"] - 1))
            cells.append(f"{worst:.1e}")
        print(f"| {entry['name']} | " + " | ".join(cells) + " |")


def _triangulate(corners: np.ndarray, cells: int) -> np.ndarray:
    """A quad split into cells x cells squares, two triangles each, at z = 0."""
    s = np.linspace(0.0, 1.0, cells + 1)
    u, v = np.meshgrid(s, s, indexing="ij")
    c0, c1, c2, c3 = corners
    xy = (
        (1 - u)[..., None] * (1 - v)[..., None] * c0
        + u[..., None] * (1 - v)[..., None] * c1
        + u[..., None] * v[..., None] * c2
        + (1 - u)[..., None] * v[..., None] * c3
    )
    # Binary64 vertices: float32 rounding along the edges would itself change the
    # grid area by about 2e-8 for a 10 km polygon (see the doc's precision section).
    p = np.concatenate([xy, np.zeros(xy.shape[:2] + (1,))], axis=2)
    a, b, c, d = p[:-1, :-1], p[1:, :-1], p[1:, 1:], p[:-1, 1:]
    tris = np.concatenate([np.stack([a, b, c], 2), np.stack([a, c, d], 2)]).reshape(-1, 3, 3)
    return tris


def radii_table(data: dict) -> None:
    print()
    print("| Ellipsoid 1/f | lat | GeoTools sqrt(MN) - GeographicLib (m) |")
    print("|---|---|---|")
    for r in data["radii"]:
        a, f = r["ellipsoid"]
        mine = float(site._gaussian_radius(a, f, np.array([r["lat"]]))[0])
        print(f"| {1 / f:.9f} | {r['lat']:g} | {mine - r['gaussian']:.2e} |")


def icsm_table(data: dict) -> None:
    icsm = data["icsm"]
    (fe, fn), (be, bn) = icsm["published"]["flinders_peak_en"], icsm["buninyong"]["en"]
    p = Placement(PlacementMode.SOURCE_CRS, source_crs=icsm["crs"], source_offset=(fe, fn, 0.0))
    m = site.measure(p, (0.0, 0.0, 0.0), (be - fe, bn - fn, 0.0))
    d, mi, s = icsm["published"]["azimuth_dms"]
    published_az = d + mi / 60 + s / 3600
    print()
    print(
        f"ICSM: GeoTools ellipsoid distance {m.ground_distance!r} m vs published "
        f"{icsm['published']['distance']} (diff {m.ground_distance - 54972.271:+.4f} m); "
        f"line scale factor {m.grid_scale!r}; true bearing {m.true_bearing!r} vs geodesic "
        f"midpoint azimuth {icsm['geographiclib']['azimuth_middle']!r} "
        f'(diff {angle(m.true_bearing, icsm["geographiclib"]["azimuth_middle"]) * 3600:+.3f}"); '
        f"published start azimuth {published_az!r}, GeographicLib "
        f"{icsm['geographiclib']['azimuth']!r}"
    )
    r = site.read_point(p, (0.0, 0.0, 0.0))
    print(
        f"ICSM: Flinders Peak lat {r.latitude!r} lon {r.longitude!r} vs "
        f"{icsm['flinders_peak']['lat']!r} {icsm['flinders_peak']['lon']!r}; "
        f"k {r.grid_scale!r} vs Krüger {icsm['flinders_peak']['scale']!r}; "
        f"convergence {r.convergence!r} vs Krüger {icsm['flinders_peak']['convergence']!r}"
    )


def main() -> None:
    data = json.loads(DATA.read_text())
    points_table(data)
    lines_table(data)
    areas_table(data)
    radii_table(data)
    icsm_table(data)


if __name__ == "__main__":
    main()
