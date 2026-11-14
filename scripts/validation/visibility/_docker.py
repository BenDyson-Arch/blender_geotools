"""Run the pinned GDAL and GRASS images on files in a work directory (no system installs)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

GDAL_IMAGE = "ghcr.io/osgeo/gdal:ubuntu-small-3.13.3"
GRASS_IMAGE = "osgeo/grass-gis:8.5.0-alpine"
CRS = "EPSG:32633"  # any projected CRS: GDAL and GRASS need one for curvature; data is synthetic


def run(image: str, work: Path, *command: str) -> str:
    """Run ``command`` in ``image`` with ``work`` mounted at /data (3 CPUs, 6 GB).

    Written for rootless Docker, where the container's root is the calling user, so the
    files it writes are the caller's.
    """
    args = [
        "docker",
        "run",
        "--rm",
        "--cpus",
        "3",
        "--memory",
        "6g",
        "-e",
        "HOME=/tmp",
        "-v",
        f"{Path(work).resolve()}:/data",
        "-w",
        "/data",
        image,
        *command,
    ]
    done = subprocess.run(args, check=False, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError(f"{' '.join(command)} failed:\n{done.stdout}\n{done.stderr}")
    return done.stdout + done.stderr


def gdal(work: Path, *command: str) -> str:
    return run(GDAL_IMAGE, work, *command)


def grass(work: Path, script: str) -> str:
    """Run a shell script in a temporary GRASS project in CRS."""
    (Path(work) / "grass.sh").write_text("set -e\n" + script)
    return run(GRASS_IMAGE, work, "grass", "--tmp-project", CRS, "--exec", "sh", "/data/grass.sh")


def versions(work: Path) -> dict[str, str]:
    return {
        "gdal": gdal(work, "gdalinfo", "--version").strip(),
        "gdal_image": GDAL_IMAGE,
        "grass": run(GRASS_IMAGE, work, "grass", "--version").splitlines()[0].strip(),
        "grass_image": GRASS_IMAGE,
    }


def read_aaigrid(path: Path) -> np.ndarray:
    """An ESRI ASCII grid's values (rows north first), no data as NaN."""
    header: dict[str, str] = {}
    with open(path) as f:
        while True:
            pos = f.tell()
            line = f.readline()
            key = line.split()[0].lower() if line.split() else ""
            if key and not key.lstrip("-").replace(".", "").isdigit() and key != "nan":
                header[key] = line.split()[1]
            else:
                f.seek(pos)
                break
        values = np.loadtxt(f, dtype=np.float64)
    nodata = header.get("nodata_value")
    if nodata is not None:
        values[values == float(nodata)] = np.nan
    return values.reshape(int(header["nrows"]), int(header["ncols"]))
