"""Regenerate the hydrology, contour and section reference values (not run in CI).

Runs the reference implementations on synthetic DEMs, compares GeoTools with
them, prints the tables of docs/validation/hydrology.md and writes the small
fixture the CI tests read (tests/data/validation/hydrology/references.npz).

References (exact versions; see docs/validation/hydrology.md):

* WhiteboxTools 2.4.0 through the ``whitebox`` 2.3.6 Python package
  (FillDepressions, D8FlowAccumulation, DInfFlowAccumulation), in a venv;
* richdem 0.3.4 (Barnes' priority-flood, D8 and D-infinity accumulation) in
  the Docker image ``python:3.7.17-slim`` (its last wheels are for 3.7);
* GDAL 3.12.0 (gdal_contour, gdallocationinfo -r bilinear) in the Docker
  image ``ghcr.io/osgeo/gdal:ubuntu-small-3.12.0``;
* SciPy 1.18.1 ``solve_ivp`` for steepest-descent curves.

Usage (from the repository root)::

    uv venv .scratch/venv-ref --python 3.12
    uv pip install --python .scratch/venv-ref/bin/python \\
        whitebox==2.3.6 numpy==2.2.6 scipy==1.18.1 tifffile==2025.10.16
    .scratch/venv-ref/bin/python scripts/validation/hydrology/make_references.py

``--work DIR`` (default ``.scratch/validation-hydrology``) holds the rasters.
Docker containers are capped at 3 CPUs and 6 GB and removed when done.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "blender"))

import _hydrology_surfaces as hs

from geotools.analysis import hydrology as hy
from geotools.analysis import slicing

GDAL_IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.12.0"
RICHDEM_IMAGE = "python:3.7.17-slim"
RICHDEM_PINS = ("richdem==0.3.4", "numpy==1.21.6")
DOCKER = ("docker", "run", "--rm", "--cpus", "3", "--memory", "6g")
OUT = REPO / "tests" / "data" / "validation" / "hydrology" / "references.npz"


# --- GeoTools on a DEM -----------------------------------------------------------------------


def geotools_hydrology(pts, tris, **kwargs):
    """GeoTools on a single-sheet 2.5D mesh: every drop leaves the model (nothing below)."""
    starts = np.arange(0, 3 * len(tris) + 1, 3)
    corner_verts = tris.ravel()
    edges, n_edges = hy.corner_edges(starts, corner_verts)
    surface = hy.surface_from_mesh(pts, starts, corner_verts, edges, n_edges)
    queries = hy.drop_queries(surface, 1e-6)
    miss = np.full(len(queries.face), -1)
    drops = hy.resolve_drops(queries, surface, miss, queries.origin, miss, queries.origin)
    return surface, hy.analyse(surface, drops, **kwargs)


# --- raster I/O -----------------------------------------------------------------------------


def write_asc(path: Path, z, x, y):
    h = float(x[1] - x[0])
    xll, yll = float(x[0]) - h / 2, float(y[0]) - h / 2
    with path.open("w") as f:
        f.write(f"ncols {z.shape[0]}\nnrows {z.shape[1]}\n")
        f.write(f"xllcorner {xll!r}\nyllcorner {yll!r}\n")
        f.write(f"cellsize {h!r}\nNODATA_value -99999\n")
        np.savetxt(f, z.T[::-1], fmt="%.17g")


def read_tif(path: Path):
    import tifffile

    return np.asarray(tifffile.imread(path), dtype=np.float64)[::-1].T


# --- WhiteboxTools ---------------------------------------------------------------------------


def run_whitebox(work: Path, rasters, rd):
    """Wang and Liu's priority-flood fill; D8 and D-infinity accumulation of richdem's
    priority-flood+epsilon DEM (drained everywhere, so both tools route the same surface);
    and the volumes of WhiteboxTools' own FillDepressions, for the record.

    WhiteboxTools 2.4.0's flat fixing (``fix_flats`` of either fill) and
    BreachDepressionsLeastCost gave heights off by 10^2 to 10^13 on these DEMs, and its
    FillDepressions left the bowls unfilled, so neither conditions the accumulation.
    """
    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    wbt.set_working_dir(str(work))
    out = {}
    for name, (_z, x, y) in rasters.items():
        asc, eps = f"dem_{name}.asc", f"eps_{name}.asc"
        write_asc(work / eps, rd[name]["eps"], x, y)
        wbt.fill_depressions_wang_and_liu(asc, f"wbt_fill_{name}.tif", False)
        wbt.fill_depressions(asc, f"wbt_lindsay_{name}.tif", False)
        wbt.d8_flow_accumulation(eps, f"wbt_d8_{name}.tif", "cells")
        wbt.d_inf_flow_accumulation(eps, f"wbt_dinf_{name}.tif", "cells")
        # The wrapper returns 0 even when the tool panics: check the files.
        kinds = ("fill", "lindsay", "d8", "dinf")
        missing = [k for k in kinds if not (work / f"wbt_{k}_{name}.tif").exists()]
        if missing:
            raise RuntimeError(f"WhiteboxTools wrote no {missing} raster for {name}")
        out[name] = {k: read_tif(work / f"wbt_{k}_{name}.tif") for k in kinds}
    return out


def whitebox_version() -> str:
    import whitebox

    wbt = whitebox.WhiteboxTools()
    return wbt.version().strip().splitlines()[0]


# --- richdem ---------------------------------------------------------------------------------

_RICHDEM_SCRIPT = r"""
import glob, json, os, sys, contextlib, io
import numpy as np, richdem as rd
cells = json.load(open('/w/cells.json'))
for path in sorted(glob.glob('/w/dem_*.npy')):
    name = os.path.basename(path)[4:-4]
    h = cells[name]
    z = np.load(path)
    def arr(a):
        r = rd.rdarray(a.copy(), no_data=-1e30)
        r.geotransform = [0.0, h, 0.0, 0.0, 0.0, -h]
        return r
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        filled = rd.FillDepressions(arr(z), epsilon=False, in_place=False)
        eps = rd.FillDepressions(arr(z), epsilon=True, in_place=False)
    np.save('/w/rd_fill_%s.npy' % name, np.asarray(filled))
    np.save('/w/rd_eps_%s.npy' % name, np.asarray(eps))
"""


def run_richdem(work: Path, rasters):
    rd_dir = work / "richdem"
    rd_dir.mkdir(exist_ok=True)
    cells = {}
    for name, (z, x, _y) in rasters.items():
        np.save(rd_dir / f"dem_{name}.npy", z.T[::-1].copy())  # raster order, north up
        cells[name] = float(x[1] - x[0])
    (rd_dir / "cells.json").write_text(json.dumps(cells))
    (rd_dir / "run.py").write_text(_RICHDEM_SCRIPT)
    pins = " ".join(RICHDEM_PINS)
    cmd = [*DOCKER, "-v", f"{rd_dir}:/w", RICHDEM_IMAGE, "sh", "-c"]
    cmd.append(f"pip install -q --no-cache-dir {pins} >/dev/null 2>&1 && python /w/run.py")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    out = {}
    for name in rasters:
        out[name] = {
            k: np.load(rd_dir / f"rd_{k}_{name}.npy")[::-1].T.astype(np.float64)
            for k in ("fill", "eps")
        }
    return out


# --- GDAL ------------------------------------------------------------------------------------


def gdal(work: Path, script: str) -> str:
    cmd = [*DOCKER, "-v", f"{work}:/w", "-w", "/w", GDAL_IMAGE, "sh", "-c", script]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def run_gdal_contours(work: Path, cases):
    script = []
    for name, (_z, _x, _y, interval, base) in cases.items():
        script.append(
            f"gdal_translate -q -oo DATATYPE=Float64 cdem_{name}.asc cdem_{name}.tif && "
            f"gdal_contour -q -3d -a elev -i {interval!r} -off {base!r} "
            f"cdem_{name}.tif gdal_contour_{name}.geojson"
        )
    for name in cases:
        (work / f"gdal_contour_{name}.geojson").unlink(missing_ok=True)
    gdal(work, " && ".join(script))
    out = {}
    for name in cases:
        data = json.loads((work / f"gdal_contour_{name}.geojson").read_text())
        pts, line, elev = [], [], []
        for k, feature in enumerate(data["features"]):
            geom = feature["geometry"]
            parts = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
            for part in parts:
                pts.append(np.asarray(part, dtype=np.float64))
                line.append(np.full(len(part), len(elev)))
                elev.append(feature["properties"]["elev"])
        out[name] = (np.concatenate(pts), np.concatenate(line), np.asarray(elev))
    return out


def run_gdal_bilinear(work: Path, name: str, xy: np.ndarray) -> np.ndarray:
    (work / f"sxy_{name}.txt").write_text("".join(f"{float(x)!r} {float(y)!r}\n" for x, y in xy))
    text = gdal(
        work,
        f"gdal_translate -q -oo DATATYPE=Float64 sdem_{name}.asc sdem_{name}.tif && "
        f"gdallocationinfo -geoloc -valonly -r bilinear sdem_{name}.tif < sxy_{name}.txt",
    )
    return np.asarray([float(v) for v in text.split()])


def gdal_version(work: Path) -> str:
    return gdal(work, "gdalinfo --version").strip()


# --- comparisons -----------------------------------------------------------------------------


def hydrology_comparison(name, z, x, y, wbt, rd, fixture, report):
    h = float(x[1] - x[0])
    pts, tris = hs.grid_mesh(z, x, y)
    surface, result = geotools_hydrology(pts, tris, min_depth=0.01)
    measures = hs.hydrology_measures(
        z, x, y, tris, surface.centroids, surface.plan_areas, result,
        wbt[name]["fill"] - z, rd[name]["fill"] - z,
        {k: wbt[name][k] for k in ("d8", "dinf")},
    )  # fmt: skip
    measures["wbt_filldepressions_volume"] = float((wbt[name]["lindsay"] - z).sum() * h * h)
    measures["wbt_fill_vs_richdem_fill"] = float(np.abs(wbt[name]["fill"] - rd[name]["fill"]).max())
    report[f"hydrology_{name}"] = measures
    if name == "terrain":
        fixture["terrain_dem"] = z
    fixture[f"hy_{name}_wbt_depth"] = (wbt[name]["fill"] - z).astype(np.float32)
    fixture[f"hy_{name}_richdem_depth"] = (rd[name]["fill"] - z).astype(np.float32)
    for k in ("d8", "dinf"):
        fixture[f"hy_{name}_wbt_{k}"] = wbt[name][k].astype(np.float32)


def contour_comparison(name, z, x, y, interval, base, gdal_lines, fixture, report):
    """GDAL's vertices against GeoTools' lines of the same level and the other way round,
    inside the hull of the cell centres (GDAL's lines run on to the raster's outer edge)."""
    pts, tris = hs.grid_mesh(z, x, y)
    lines = slicing.contours(pts, tris, interval, base=base, major_every=0)
    gp, gline, gelev = gdal_lines
    inside = (gp[:, 0] >= x[0]) & (gp[:, 0] <= x[-1]) & (gp[:, 1] >= y[0]) & (gp[:, 1] <= y[-1])
    measures = hs.contour_measures(lines, gp, gline, gelev, x, y)
    report[f"contours_{name}"] = {
        "geotools_lines": lines.count,
        "geotools_closed": int(lines.closed.sum()),
        "geotools_levels": len(np.unique(lines.line_values["elevation"])),
        "gdal_lines": len(gelev),
        "gdal_levels": len(np.unique(gelev)),
        "gdal_vertices_outside_hull": int((~inside).sum()),
        **{k: hs.stats(v) if isinstance(v, np.ndarray) else v for k, v in measures.items()},
    }
    fixture[f"ct_{name}_gdal_points"] = gp
    fixture[f"ct_{name}_gdal_line"] = gline.astype(np.int32)
    fixture[f"ct_{name}_gdal_elev"] = gelev


def section_comparison(name, z, x, y, path, work, fixture, report):
    pts, tris = hs.grid_mesh(z, x, y)
    path3 = np.array([(px, py, 0.0) for px, py in path])
    lines = slicing.sections(pts, tris, path3, np.array([0, len(path3)]))
    p = lines.positions
    zb = run_gdal_bilinear(work, name, p[:, :2])
    report[f"sections_{name}"] = {
        "pieces": lines.count,
        "points": len(p),
        "z_vs_gdal_bilinear": hs.stats(p[:, 2] - zb),
    }
    fixture[f"sc_{name}_gdal_xy"] = p[:, :2].copy()
    fixture[f"sc_{name}_gdal_z"] = zb


# --- steepest-descent curves for the Flow Path (Quick Look) preset ------------------------------


def descent_curves(seeds, half=hs.HILLS_HALF, step=0.02):
    """Lines of steepest descent of the hills (arc length in plan), to 0.05 from the edge."""
    from scipy.integrate import solve_ivp

    def rhs(_s, p):
        gx, gy = hs.hills_gradient(p[0], p[1])
        n = math.hypot(gx, gy)
        return [-gx / n, -gy / n]

    def leave(_s, p):
        return half - 0.05 - max(abs(p[0]), abs(p[1]))

    leave.terminal = True
    curves = []
    for seed in seeds:
        sol = solve_ivp(
            rhs, (0, 30), seed, method="DOP853", rtol=1e-10, atol=1e-12, events=leave,
            dense_output=True,
        )  # fmt: skip
        s = np.append(np.arange(0, sol.t[-1], step), sol.t[-1])
        curves.append(sol.sol(s).T)
    return curves


# --- main ------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--work", type=Path, default=REPO / ".scratch" / "validation-hydrology")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    os.chdir(work)
    fixture: dict[str, np.ndarray] = {}
    report: dict[str, object] = {"gdal": gdal_version(work), "whitebox": whitebox_version()}

    rasters = {}
    for name, (height, *box) in hs.HYDRO.items():
        z, x, y = hs.dem(height, *box)
        rasters[name] = (z, x, y)
        write_asc(work / f"dem_{name}.asc", z, x, y)
    rd = run_richdem(work, rasters)
    wbt = run_whitebox(work, rasters, rd)
    for name, (z, x, y) in rasters.items():
        hydrology_comparison(name, z, x, y, wbt, rd, fixture, report)

    cases = {}
    for name, (height, x0, x1, y0, y1, h, interval, base) in hs.CONTOURS.items():
        z, x, y = hs.dem(height, x0, x1, y0, y1, h)
        write_asc(work / f"cdem_{name}.asc", z, x, y)
        cases[name] = (z, x, y, interval, base)
    gdal_lines = run_gdal_contours(work, cases)
    for name, (z, x, y, interval, base) in cases.items():
        contour_comparison(name, z, x, y, interval, base, gdal_lines[name], fixture, report)

    for name, (height, x0, x1, y0, y1, h, path) in hs.SECTIONS.items():
        z, x, y = hs.dem(height, x0, x1, y0, y1, h)
        write_asc(work / f"sdem_{name}.asc", z, x, y)
        section_comparison(name, z, x, y, path, work, fixture, report)

    curves = descent_curves(np.array(hs.HILLS_SEEDS))
    fixture["fp_points"] = np.concatenate(curves)
    fixture["fp_curve"] = np.repeat(np.arange(len(curves)), [len(c) for c in curves]).astype(
        np.int32
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **fixture)
    report["fixture_bytes"] = args.out.stat().st_size
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
