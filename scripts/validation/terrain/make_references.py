"""Regenerate the terrain validation references (docs/validation/terrain.md).

Runs the reference implementations on the synthetic "waves" DEM of
``tests/blender/_terrain_surfaces.py`` and writes small crops of their outputs
to ``tests/data/validation/terrain/references.npz``, which the Blender suite
(``tests/blender/test_validation_terrain.py``) compares GeoTools against. Not
run in CI.

Reference tools, pinned:

* GDAL 3.12.0, Docker image ``ghcr.io/osgeo/gdal:ubuntu-small-3.12.0``
  (gdaldem slope, aspect, TRI, TPI, roughness);
* WhiteboxTools 2.4.0, through the ``whitebox`` 2.3.6 Python package (slope,
  aspect, curvatures, ruggedness index, surface area ratio);
* rvt-py 2.2.1 (sky-view factor, openness, simple local relief model), with
  numpy 2.2.6 and scipy 1.15.3.

Usage (a venv outside the repo; rvt-py's GDAL dependency is not needed for
``rvt.vis``, so it is installed without dependencies)::

    uv venv -p 3.13 /tmp/terrain-ref
    uv pip install -p /tmp/terrain-ref/bin/python whitebox==2.3.6 numpy==2.2.6 scipy==1.15.3
    uv pip install -p /tmp/terrain-ref/bin/python --no-deps rvt-py==2.2.1
    /tmp/terrain-ref/bin/python scripts/validation/terrain/make_references.py

The first ``whitebox`` call downloads the WhiteboxTools binary into the venv.
Docker runs with ``--cpus 4 --memory 6g`` and removes its container.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "tests" / "blender"))
import _terrain_surfaces as ts

OUT = REPO / "tests" / "data" / "validation" / "terrain" / "references.npz"
GDAL_IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.12.0"

LOCAL_CROP = slice(40, 101)  # central 61 x 61 cells (6 m): window tools
HORIZON_CROP = slice(20, 121, 2)  # central 101 x 101 cells every 2nd: 51 x 51
SVF_RADIUS_CELLS = 20  # 2 m, the presets' default Max Distance
DIRECTIONS = 16
SLRM_RADIUS_CELLS = 10  # rvt-py allows 10..50


def write_asc(path: Path, z: np.ndarray, h: float) -> None:
    n0, n1 = z.shape
    corner = -(n1 - 1) * h / 2 - h / 2
    lines = [
        f"ncols {n1}",
        f"nrows {n0}",
        f"xllcorner {corner!r}",
        f"yllcorner {-(n0 - 1) * h / 2 - h / 2!r}",
        f"cellsize {h!r}",
        "NODATA_value -9999",
    ]
    lines += [" ".join(f"{v:.10g}" for v in row) for row in z]
    path.write_text("\n".join(lines) + "\n")


def read_asc(path: Path) -> np.ndarray:
    header, rows = {}, []
    for line in path.read_text().splitlines():
        parts = line.split()
        if parts and parts[0][0].isalpha():
            header[parts[0].lower()] = float(parts[1])
        elif parts:
            rows.append([float(v) for v in parts])
    z = np.array(rows, np.float64)
    z[z == header.get("nodata_value", -9999.0)] = np.nan
    return z


def run_whitebox(work: Path) -> dict[str, str]:
    import whitebox

    wbt = whitebox.WhiteboxTools()
    wbt.set_verbose_mode(False)
    # A GeoTIFF with a projected CRS: WhiteboxTools refuses a GeoTIFF without
    # geokeys and, given an ASCII grid, writes some outputs as Int32.
    dem = str(work / "dem.tif")
    outputs = {
        "wbt_slope": ("slope", {"units": "degrees"}),
        "wbt_aspect": ("aspect", {}),
        "wbt_profile_curvature": ("profile_curvature", {}),
        "wbt_plan_curvature": ("plan_curvature", {}),
        "wbt_tangential_curvature": ("tangential_curvature", {}),
        "wbt_mean_curvature": ("mean_curvature", {}),
        "wbt_ruggedness_index": ("ruggedness_index", {}),
        "wbt_surface_area_ratio": ("surface_area_ratio", {}),
    }
    for key, (tool, extra) in outputs.items():
        status = getattr(wbt, tool)(dem, str(work / f"{key}.tif"), **extra)
        if status != 0:
            raise RuntimeError(f"WhiteboxTools {tool} failed ({status})")
    return {
        "whitebox_tools": wbt.version().split("\n")[0].strip(),
        "whitebox": whitebox.__version__,
    }


GDAL_COMMANDS = {
    "gdal_slope_horn": "gdaldem slope dem.tif {out}",
    "gdal_slope_zt": "gdaldem slope dem.tif {out} -alg ZevenbergenThorne",
    "gdal_aspect_horn": "gdaldem aspect dem.tif {out}",
    "gdal_aspect_zt": "gdaldem aspect dem.tif {out} -alg ZevenbergenThorne",
    "gdal_tri_riley": "gdaldem TRI dem.tif {out} -alg Riley",
    "gdal_tri_wilson": "gdaldem TRI dem.tif {out} -alg Wilson",
    "gdal_tpi": "gdaldem TPI dem.tif {out}",
    "gdal_roughness": "gdaldem roughness dem.tif {out}",
}


CRS = "EPSG:32755"
"""Any projected CRS in metres: only so WhiteboxTools treats the grid as metres (the data are synthetic)."""


def make_geotiff(work: Path) -> None:
    """dem.asc to a Float32 GeoTIFF with a projected CRS (what every tool reads)."""
    docker_sh(work, [f"gdal_translate -q -ot Float32 -a_srs {CRS} dem.asc dem.tif"])


def run_gdal(work: Path, wbt_keys: list[str]) -> str:
    """gdaldem on the DEM and WhiteboxTools' GeoTIFFs to AAIGrid, in one container."""
    script = [command.format(out=f"{key}.tif") + " -q" for key, command in GDAL_COMMANDS.items()]
    for key in [*GDAL_COMMANDS, *wbt_keys]:
        script.append(f"gdal_translate -q -of AAIGrid -co DECIMAL_PRECISION=10 {key}.tif {key}.asc")
    script.append("gdal_translate -q -of AAIGrid -co DECIMAL_PRECISION=10 dem.tif dem32.asc")
    script.append("gdalinfo --version > gdal_version.txt")
    docker_sh(work, script)
    return (work / "gdal_version.txt").read_text().strip()


def docker_sh(work: Path, script: list[str]) -> None:
    (work / "run.sh").write_text("\n".join(["set -e", *script]) + "\n")
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--cpus",
            "4",
            "--memory",
            "6g",
            "-u",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{work}:/work",
            "-w",
            "/work",
            GDAL_IMAGE,
            "sh",
            "run.sh",
        ],
        check=True,
    )


def run_rvt(z: np.ndarray, h: float) -> tuple[dict[str, np.ndarray], str]:
    from importlib.metadata import version

    import rvt.vis

    dem = z.astype(np.float32)
    common = {
        "resolution": h,
        "compute_svf": True,
        "compute_opns": True,
        "svf_n_dir": DIRECTIONS,
        "svf_r_max": SVF_RADIUS_CELLS,
        "svf_noise": 0,
    }
    up = rvt.vis.sky_view_factor(dem, **common)
    down = rvt.vis.sky_view_factor(-dem, **common)  # rvt.default's negative openness
    slrm = rvt.vis.slrm(dem.copy(), radius_cell=SLRM_RADIUS_CELLS)
    out = {
        "rvt_svf": up["svf"],
        "rvt_openness_positive": up["opns"],
        "rvt_openness_negative": down["opns"],
        "rvt_slrm": slrm,
    }
    return out, version("rvt-py")


def exact_horizons() -> dict[str, np.ndarray]:
    """SVF and openness of the continuous waves surface at the horizon crop's cell centres.

    Two definitions: GeoTools' (surface points within Max Distance of the ray
    origin, which is Ray Offset along the normal) and RVT's (points from one
    cell to Max Distance away horizontally, from the surface point itself).
    """
    x, y = ts.grid_xy()
    px, py = x[HORIZON_CROP, HORIZON_CROP], y[HORIZON_CROP, HORIZON_CROP]
    shape = px.shape
    distance = SVF_RADIUS_CELLS * ts.H
    out = {}
    for label, kwargs in (
        ("exact_geotools", {"ray_offset": 0.002, "ball": True}),
        ("exact_rvt", {"ray_offset": 0.0, "ball": False, "s_min": ts.H}),
    ):
        up = ts.horizon_values(ts.waves_horizon(px, py, DIRECTIONS, distance, **kwargs))
        down = ts.horizon_values(
            ts.waves_horizon(px, py, DIRECTIONS, distance, below=True, **kwargs)
        )
        out[f"{label}_svf"] = up["svf"].reshape(shape)
        out[f"{label}_openness_positive"] = up["openness_positive"].reshape(shape)
        out[f"{label}_openness_negative"] = down["openness_negative"].reshape(shape)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--keep", type=Path, help="keep the work directory here")
    args = parser.parse_args(argv)

    z = ts.waves(*ts.grid_xy())
    work = Path(tempfile.mkdtemp(prefix="terrain-ref-"))
    try:
        write_asc(work / "dem.asc", z, ts.H)
        make_geotiff(work)
        versions = run_whitebox(work)
        wbt_keys = sorted(p.stem for p in work.glob("wbt_*.tif"))
        versions["gdal"] = run_gdal(work, wbt_keys)
        z32 = read_asc(work / "dem32.asc")  # what GDAL and WhiteboxTools read
        # Within one float32 ulp of the heights the Blender mesh is built from.
        assert np.abs(z32 - z).max() < 1e-7, np.abs(z32 - z).max()
        rvt_out, versions["rvt_py"] = run_rvt(z32, ts.H)
        arrays: dict[str, np.ndarray] = {}
        for key in [*GDAL_COMMANDS, *wbt_keys]:
            grid = read_asc(work / f"{key}.asc")
            assert grid.shape == z.shape, (key, grid.shape)
            arrays[key] = grid[LOCAL_CROP, LOCAL_CROP].astype(np.float32)
        for key, grid in exact_horizons().items():
            arrays[key] = grid.astype(np.float32)
        for key, grid in rvt_out.items():
            arrays[key] = np.asarray(grid)[HORIZON_CROP, HORIZON_CROP].astype(np.float32)
        meta = {
            "dem": "tests/blender/_terrain_surfaces.py waves(), N x N cells at H",
            "n": ts.N,
            "h": ts.H,
            "local_crop": [LOCAL_CROP.start, LOCAL_CROP.stop, 1],
            "horizon_crop": [HORIZON_CROP.start, HORIZON_CROP.stop, HORIZON_CROP.step],
            "svf_radius_cells": SVF_RADIUS_CELLS,
            "directions": DIRECTIONS,
            "slrm_radius_cells": SLRM_RADIUS_CELLS,
            "versions": versions,
            "gdal_image": GDAL_IMAGE,
        }
        args.out.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(args.out, meta=np.array(json.dumps(meta)), **arrays)
        print(json.dumps(meta, indent=1))
        print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    finally:
        if args.keep:
            shutil.copytree(work, args.keep, dirs_exist_ok=True)
        shutil.rmtree(work)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
