"""Cross-model reference: GRASS GIS r.sun (ESRA clear sky) on synthetic planes, in Docker.

Writes ``tests/data/validation/solar/rsun.json``: r.sun's daily beam,
diffuse and global irradiation (Wh/m²/day, mode 2) and insolation time (h)
on one-cell synthetic planes (a flat DEM with constant slope and aspect
rasters, the way r.sun models a tilted panel) at the sites, days and surfaces of
``clearsky.py``, with the same Linke turbidity, the solar constant set to
GeoTools' 1366.1 W/m², albedo 0 (no ground-reflected light, as GeoTools)
and a 0.05 h step.

r.sun follows Šúri and Hofierka (2004) and the ESRA clear-sky model
(Rigollier, Bauer and Wald 2000) with Muneer's (1990) diffuse model on
inclined surfaces and its own solar geometry. GeoTools uses Ineichen-Perez
with an isotropic sky, so this is a comparison of models, not of
implementations: agreement is expected to within the models' published
differences, not to round-off.

Needs Docker and the image ``osgeo/grass-gis:8.4.1-alpine``. Run:

    .scratch/refenv/bin/python -I scripts/validation/solar/grass_rsun.py
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c
from clearsky import SITES, SURFACES

IMAGE = "osgeo/grass-gis:8.4.1-alpine"
SOLAR_CONSTANT = 1366.1
STEP_HOURS = 0.05


def grass_aspect(true_azimuth: float) -> float:
    """GRASS aspect (counter-clockwise from east, 360 for east) of a true azimuth faced."""
    value = (90.0 - true_azimuth) % 360.0
    return 360.0 if value == 0.0 else value


def main() -> None:
    lines = ["set -e", "grass -c EPSG:4326 /tmp/gproj --exec bash /work/run_cases.sh"]
    cases = []
    body = []
    for name, lat, lon, day, _zone, turbidity, altitude in SITES:
        doy = dt.date.fromisoformat(day).timetuple().tm_yday
        body.append(
            f"g.region n={lat + 0.005} s={lat - 0.005} w={lon - 0.005} e={lon + 0.005} res=0.01"
        )
        body.append(f'r.mapcalc "dem = {altitude}" --q --o')
        for surface, tilt, aspect in SURFACES:
            key = f"{len(cases)}"
            cases.append((name, lat, lon, day, doy, turbidity, altitude, surface, tilt, aspect))
            # Constant rasters, not slope_value/aspect_value: in GRASS 8.4.1 aspect_value
            # behaves as a compass bearing (180 faces south) although its help says
            # "270 is south", while an aspect raster follows the documented GRASS
            # convention (counter-clockwise from east).
            body.append(f'r.mapcalc "slp = {tilt}" --q --o')
            body.append(f'r.mapcalc "asp = {grass_aspect(aspect)}" --q --o')
            body.append(
                "r.sun elevation=dem slope=slp aspect=asp "
                f"linke_value={turbidity} albedo_value=0 solar_constant={SOLAR_CONSTANT} "
                f"day={doy} step={STEP_HOURS} beam_rad=b diff_rad=d glob_rad=g insol_time=t "
                "--q --o"
            )
            body.append(
                f'echo "CASE {key} $(r.what map=b,d,g,t coordinates={lon},{lat} separator=space)"'
            )
    with tempfile.TemporaryDirectory(dir=c.REPO / ".scratch") as tmp:
        work = Path(tmp)
        (work / "run.sh").write_text("\n".join(lines) + "\n")
        (work / "run_cases.sh").write_text("set -e\n" + "\n".join(body) + "\n")
        out = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--cpus",
                "3",
                "--memory",
                "6g",
                "-v",
                f"{work}:/work",
                IMAGE,
                "bash",
                "/work/run.sh",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    values = {}
    for line in out.splitlines():
        if line.startswith("CASE "):
            parts = line.split()
            values[int(parts[1])] = [float(v) for v in parts[-4:]]
    rows = []
    for i, (name, lat, lon, day, doy, turbidity, altitude, surface, tilt, aspect) in enumerate(
        cases
    ):
        beam, diffuse, total, hours = values[i]
        rows.append(
            {
                "site": name,
                "latitude": lat,
                "longitude": lon,
                "day": day,
                "day_of_year": doy,
                "linke_turbidity": turbidity,
                "altitude": altitude,
                "surface": surface,
                "tilt": tilt,
                "aspect": aspect,
                "beam": beam / 1000.0,
                "diffuse": diffuse / 1000.0,
                "global": total / 1000.0,
                "insolation_hours": hours,
            }
        )
        print(f"{name:28s} {surface:15s} beam {beam:8.1f} diff {diffuse:7.1f} h {hours:6.2f}")
    c.write(
        "rsun.json",
        {
            "about": __doc__.split("\n\n")[0],
            "grass": "GRASS GIS 8.4.1 (" + IMAGE + ")",
            "step_hours": STEP_HOURS,
            "solar_constant": SOLAR_CONSTANT,
            "cases": rows,
        },
    )


if __name__ == "__main__":
    main()
