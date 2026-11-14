"""Reference horizon profiles over a synthetic DEM (not run in CI).

Writes tests/data/validation/archaeoastronomy/horizon_reference.json with the
skyline seen from the middle of :func:`dem` (a valley ringed by hills, with a
mountain beyond and a near knoll), at every whole degree of azimuth, by two
methods independent of GeoTools:

* **TIN march**: the exact skyline of the triangulated grid GeoTools' test
  builds (each cell split along its south-west to north-east diagonal). The
  terrain along an azimuth is linear between the places where the line
  crosses a grid line or a diagonal, so the apparent altitude
  atan(dz / d) - (1 - k) d / 2R is evaluated at every crossing (and halfway
  between, for the curvature term); the largest is the skyline. For k = 0.13
  (GeoTools' default), k = 0 (curvature, no refraction) and no curvature.
* **GRASS GIS r.horizon** (``--grass``, in Docker): the same DEM as a raster
  (cell centres on the grid nodes), observer raised by the eye height by
  lowering every other cell, ``distance=0.5``. r.horizon includes curvature
  with R = 6,371 km and no refraction, so it matches GeoTools' curvature-on,
  refraction-off mode; it reports negative horizons as 0, so the DEM's
  skyline is above level all round.

Run (pinned environment and image in README.md):

    .scratch/refenv/bin/python scripts/validation/archaeoastronomy/horizon_reference.py \
        --grass --work .scratch/grass
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[3] / "tests/data/validation/archaeoastronomy"
SPACING = 100.0
HALF = 20_000.0
EYE = 1.6
R = 6_371_000.0
GRASS_IMAGE = "osgeo/grass-gis:8.5.0-alpine"


def dem() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(x, y, z): a 401 x 401 grid 100 m apart, z float32 metres; z[j, i] at (x[i], y[j])."""
    x = np.arange(-HALF, HALF + 0.5 * SPACING, SPACING)
    y = x.copy()
    xx, yy = np.meshgrid(x, y)
    r = np.hypot(xx, yy)
    theta = np.arctan2(xx, yy)  # azimuth, clockwise from +y
    z = (
        200.0
        + 350.0 * np.exp(-(((r - 7000.0) / 2000.0) ** 2)) * (1.0 + 0.6 * np.sin(3.0 * theta + 0.5))
        + 900.0 * np.exp(-((xx - 13000.0) ** 2 + (yy - 9000.0) ** 2) / 2500.0**2)
        + 120.0 * np.exp(-((xx + 1500.0) ** 2 + (yy - 1800.0) ** 2) / 500.0**2)
        + 15.0 * np.sin(xx / 650.0) * np.cos(yy / 810.0)
    )
    return x, y, z.astype(np.float32)


def tin_height(z: np.ndarray, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """Height of the triangulated grid at (px, py) (each cell split SW-NE)."""
    fx = (px + HALF) / SPACING
    fy = (py + HALF) / SPACING
    n = z.shape[0] - 1
    i = np.clip(np.floor(fx).astype(np.int64), 0, n - 1)
    j = np.clip(np.floor(fy).astype(np.int64), 0, n - 1)
    u, v = fx - i, fy - j
    z = z.astype(np.float64)
    z00, z10, z01, z11 = z[j, i], z[j, i + 1], z[j + 1, i], z[j + 1, i + 1]
    lower = z00 + u * (z10 - z00) + v * (z11 - z10)  # u >= v: (00, 10, 11)
    upper = z00 + v * (z01 - z00) + u * (z11 - z01)  # u < v: (00, 11, 01)
    return np.where(u >= v, lower, upper)


def tin_profile(z: np.ndarray, azimuths: np.ndarray, eye_z: float) -> dict:
    out = {"k013": [], "k0": [], "flat": [], "distance_k013": []}
    nodes = np.arange(-HALF, HALF + 0.5 * SPACING, SPACING)
    diagonals = np.arange(-2 * HALF, 2 * HALF + 0.5 * SPACING, SPACING)
    for az in azimuths:
        sa, ca = math.sin(math.radians(az)), math.cos(math.radians(az))
        reach = min(
            HALF / abs(sa) if abs(sa) > 1e-12 else np.inf,
            HALF / abs(ca) if abs(ca) > 1e-12 else np.inf,
        )
        parts = []
        if abs(sa) > 1e-12:
            parts.append(nodes / sa)
        if abs(ca) > 1e-12:
            parts.append(nodes / ca)
        if abs(sa - ca) > 1e-12:
            parts.append(diagonals / (sa - ca))  # x - y = m h
        s = np.concatenate(parts)
        s = np.unique(s[(s > 1.0) & (s <= reach * (1 - 1e-12))])
        s = np.unique(np.concatenate([s, 0.5 * (s[1:] + s[:-1])]))
        height = tin_height(z, s * sa, s * ca) - eye_z
        flat = np.degrees(np.arctan2(height, s))
        for key, k in (("k013", 0.13), ("k0", 0.0), ("flat", None)):
            apparent = flat if k is None else flat - np.degrees((1 - k) * s / (2 * R))
            best = int(np.argmax(apparent))
            out[key].append(float(apparent[best]))
            if key == "k013":
                out["distance_k013"].append(float(s[best]))
    return out


def grass_profile(z: np.ndarray, work: Path, eye_z: float) -> dict:
    """r.horizon at the middle node, the DEM lowered by the eye height elsewhere."""
    work.mkdir(parents=True, exist_ok=True)
    raster = z.astype(np.float64) - EYE
    c = z.shape[0] // 2
    raster[c, c] = eye_z  # the observer's cell holds the eye
    e0, n0 = 500_000.0, 5_000_000.0  # UTM 33N false origin for the grid
    with open(work / "dem.asc", "w") as f:
        f.write(
            f"ncols {z.shape[1]}\nnrows {z.shape[0]}\n"
            f"xllcorner {e0 - HALF - SPACING / 2}\nyllcorner {n0 - HALF - SPACING / 2}\n"
            f"cellsize {SPACING}\nNODATA_value -9999\n"
        )
        np.savetxt(f, raster[::-1], fmt="%.4f")  # rows north first
    (work / "run.sh").write_text(
        "set -e\n"
        "r.in.gdal input=/data/dem.asc output=dem -o --quiet\n"
        "g.region raster=dem\n"
        f"r.horizon -d -c -l elevation=dem coordinates={e0},{n0} step=1 start=0 end=360 "
        "maxdistance=30000 distance=0.5 format=plain file=/data/horizon.csv --overwrite\n"
        "grass --version | head -1 > /data/version.txt || true\n"
        "r.horizon --help 2>&1 | head -1 >> /data/version.txt\n"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cpus",
            "3",
            "--memory",
            "6g",
            "-v",
            f"{work.resolve()}:/data",
            GRASS_IMAGE,
            "grass",
            "--tmp-project",
            "EPSG:32633",
            "--exec",
            "sh",
            "/data/run.sh",
        ],
        check=True,
    )
    rows = np.loadtxt(work / "horizon.csv", delimiter=",", skiprows=1)
    order = np.argsort(rows[:, 0])
    digest = subprocess.run(
        ["docker", "image", "inspect", GRASS_IMAGE, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return {
        "image": digest,
        "azimuth": rows[order, 0].tolist(),
        "k0": rows[order, 1].tolist(),
        "distance": rows[order, 2].tolist(),
    }


def grass_bands(work: Path) -> dict:
    """r.horizon's angle formula on its own: two bands on a level raster.

    A band 100 m high 30 km north and one 50 m high 10 km east, observer on the
    plain. r.horizon's angles are compared with atan(H / d) - d / 2R at the
    distance it reports (curvature, R = 6,371 km, no refraction).
    """
    work.mkdir(parents=True, exist_ok=True)
    n = 801
    z = np.zeros((n, n))
    c = n // 2
    z[c - 302 : c - 299, :] = 100.0  # rows (north first) about 30 km north
    z[:, c + 99 : c + 102] = 50.0  # columns about 10 km east
    with open(work / "bands.asc", "w") as f:
        f.write(f"ncols {n}\nnrows {n}\nxllcorner 460000\nyllcorner 5960000\n")
        f.write(f"cellsize {SPACING}\nNODATA_value -9999\n")
        np.savetxt(f, z, fmt="%.4f")
    x0, y0 = 460000 + c * SPACING + SPACING / 2, 5960000 + c * SPACING + SPACING / 2
    (work / "bands.sh").write_text(
        "set -e\n"
        "r.in.gdal input=/data/bands.asc output=dem -o --quiet\n"
        "g.region raster=dem\n"
        f"r.horizon -d -c -l elevation=dem coordinates={x0},{y0} step=90 maxdistance=40000 "
        "format=plain file=/data/bands.csv --overwrite\n"
    )
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cpus",
            "3",
            "--memory",
            "6g",
            "-v",
            f"{work.resolve()}:/data",
            GRASS_IMAGE,
            "grass",
            "--tmp-project",
            "EPSG:32633",
            "--exec",
            "sh",
            "/data/bands.sh",
        ],
        check=True,
    )
    rows = np.loadtxt(work / "bands.csv", delimiter=",", skiprows=1)
    out = []
    for az, angle, d in rows:
        height = {0.0: 100.0, 90.0: 50.0}.get(float(az))
        if height is None:
            continue
        formula = math.degrees(math.atan(height / d) - d / (2 * R))
        out.append(
            {
                "azimuth": float(az),
                "grass": float(angle),
                "distance": float(d),
                "formula_k0": formula,
            }
        )
    return {"bands": out}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--grass", action="store_true", help="also run r.horizon in Docker")
    parser.add_argument("--work", type=Path, default=Path(".scratch/grass"))
    args = parser.parse_args()
    x, _y, z = dem()
    c = len(x) // 2
    eye_z = float(z[c, c]) + EYE
    azimuths = np.arange(360, dtype=np.float64)
    data = {
        "generated_by": "scripts/validation/archaeoastronomy/horizon_reference.py",
        "numpy": np.__version__,
        "dem_sha256": hashlib.sha256(z.tobytes()).hexdigest(),
        "eye_height": EYE,
        "eye_z": eye_z,
        "azimuth": azimuths.tolist(),
        "tin": tin_profile(z, azimuths, eye_z),
    }
    if args.grass:
        data["grass"] = grass_profile(z, args.work, eye_z)
        data["grass"].update(grass_bands(args.work))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "horizon_reference.json"
    path.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(path, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
