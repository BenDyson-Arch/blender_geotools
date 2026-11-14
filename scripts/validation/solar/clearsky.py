"""Reference clear-sky irradiation on open surfaces, from pvlib-python's own chain.

Writes ``tests/data/validation/solar/clearsky.json``: daily totals in kWh/m²
over a local day at 1-minute steps (step midpoints, as GeoTools sums them) of

- GHI, DNI and DHI from ``pvlib.clearsky.ineichen`` (Ineichen and Perez
  2002, no Perez enhancement) with the Kasten-Young (1989) air mass on the
  apparent zenith, the absolute air mass from ``pvlib.atmosphere.alt2pres``,
  and the extraterrestrial irradiance 1366.1 W/m² / R² (``method="nrel"``,
  pvlib's SPA Earth-sun distance), and
- the direct and sky-diffuse irradiation on tilted and vertical surfaces from
  ``pvlib.irradiance.get_total_irradiance(model="isotropic", albedo=0)``
  (the Liu-Jordan isotropic sky; GeoTools omits ground-reflected light).

The sun is pvlib's own NREL SPA (``get_solarposition(method="nrel_numpy")``,
standard pressure and 12 C, pvlib's Delta T). The surface azimuth is the
true azimuth the surface faces. A ``sky_view`` table gives the exact
isotropic sky view (1 + cos tilt) / 2 for comparing GeoTools' Fibonacci sky
rays.

Run:

    .scratch/refenv/bin/python -I scripts/validation/solar/clearsky.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c

SITES = [
    # (name, lat, lon, day, zone, linke turbidity, altitude m)
    ("51 N March", 51.18, -1.83, "2026-03-20", "Europe/London", 3.0, 0.0),
    ("51 N June", 51.18, -1.83, "2026-06-21", "Europe/London", 3.0, 0.0),
    ("51 N December", 51.18, -1.83, "2026-12-21", "Europe/London", 3.0, 0.0),
    ("27.5 S June", -27.5, 153.0, "2026-06-21", "Australia/Brisbane", 3.0, 0.0),
    ("27.5 S December", -27.5, 153.0, "2026-12-21", "Australia/Brisbane", 3.0, 0.0),
    ("equator equinox", 0.0, 0.0, "2026-03-20", "UTC", 3.0, 0.0),
    ("64 N June, hazy", 64.1, -21.9, "2026-06-21", "Atlantic/Reykjavik", 5.0, 0.0),
    ("27.5 S June, 1500 m, clean", -27.5, 153.0, "2026-06-21", "Australia/Brisbane", 2.0, 1500.0),
]

SURFACES = [
    ("horizontal", 0.0, 0.0),
    ("south 30", 30.0, 180.0),
    ("north 30", 30.0, 0.0),
    ("east 45", 45.0, 90.0),
    ("vertical south", 90.0, 180.0),
    ("vertical north", 90.0, 0.0),
    ("vertical east", 90.0, 90.0),
]


def day_totals(lat, lon, day, zone, turbidity, altitude) -> dict:
    import pandas as pd
    import pvlib

    start = pd.Timestamp(day).tz_localize(zone)
    end = (pd.Timestamp(day) + pd.Timedelta(days=1)).tz_localize(zone)
    minutes = round((end - start).total_seconds() / 60.0)
    times = start.tz_convert("UTC") + pd.to_timedelta(np.arange(minutes) + 0.5, unit="min")
    position = pvlib.solarposition.get_solarposition(
        times, lat, lon, altitude=altitude, pressure=101325.0, temperature=12.0, method="nrel_numpy"
    )
    zenith = position["apparent_zenith"].to_numpy()
    relative = pvlib.atmosphere.get_relative_airmass(zenith, model="kastenyoung1989")
    absolute = pvlib.atmosphere.get_absolute_airmass(relative, pvlib.atmosphere.alt2pres(altitude))
    extra = pvlib.irradiance.get_extra_radiation(times, solar_constant=1366.1, method="nrel")
    sky = pvlib.clearsky.ineichen(
        zenith,
        absolute,
        turbidity,
        altitude=altitude,
        dni_extra=np.asarray(extra),
        perez_enhancement=False,
    )
    ghi, dni, dhi = (
        np.nan_to_num(np.asarray(sky[k], dtype=np.float64)) for k in ("ghi", "dni", "dhi")
    )
    up = zenith < 90.0
    ghi, dni, dhi = (np.where(up, v, 0.0) for v in (ghi, dni, dhi))
    hours = 1.0 / 60.0
    out = {
        "ghi": ghi.sum() * hours / 1000.0,
        "dni": dni.sum() * hours / 1000.0,
        "dhi": dhi.sum() * hours / 1000.0,
        "surfaces": [],
    }
    for name, tilt, aspect in SURFACES:
        poa = pvlib.irradiance.get_total_irradiance(
            tilt,
            aspect,
            zenith,
            position["azimuth"].to_numpy(),
            dni,
            ghi,
            dhi,
            albedo=0.0,
            model="isotropic",
        )
        direct = np.nan_to_num(np.asarray(poa["poa_direct"], dtype=np.float64))
        diffuse = np.nan_to_num(np.asarray(poa["poa_sky_diffuse"], dtype=np.float64))
        out["surfaces"].append(
            {
                "surface": name,
                "tilt": tilt,
                "aspect": aspect,
                "direct": direct.sum() * hours / 1000.0,
                "diffuse": diffuse.sum() * hours / 1000.0,
            }
        )
    return out


def main() -> None:
    rows = []
    for name, lat, lon, day, zone, turbidity, altitude in SITES:
        totals = day_totals(lat, lon, day, zone, turbidity, altitude)
        rows.append(
            {
                "site": name,
                "latitude": lat,
                "longitude": lon,
                "day": day,
                "zone": zone,
                "linke_turbidity": turbidity,
                "altitude": altitude,
                **totals,
            }
        )
        print(
            f"{name:28s} GHI {totals['ghi']:.3f} DNI {totals['dni']:.3f} DHI {totals['dhi']:.3f}",
            [round(s["direct"] + s["diffuse"], 3) for s in totals["surfaces"]],
        )
    tilts = [0.0, 15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 150.0, 180.0]
    c.write(
        "clearsky.json",
        {
            "about": __doc__.split("\n\n")[0],
            "versions": c.versions(),
            "days": rows,
            "sky_view": {
                "tilt": tilts,
                "isotropic": [(1.0 + np.cos(np.radians(t))) / 2.0 for t in tilts],
            },
        },
    )


if __name__ == "__main__":
    main()
