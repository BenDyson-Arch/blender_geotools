"""Reference values for the Moon, the lunar standstills and the obliquity (not run in CI).

Writes tests/data/validation/archaeoastronomy/moon_reference.json from:

* JPL DE440s through Skyfield (apparent geocentric place on the true equator
  and equinox of date, and topocentric altitude and azimuth) at 300 random
  instants 1900-2100, for ``moon.position``;
* JPL DE441 (excerpts around each ancient standstill, see the README in this
  folder) read with jplephem and turned to the true equator of date with
  ERFA's long-term precession (Vondrak, Capitaine and Wallace 2011,
  ``eraLtpb``) and IAU 2000A nutation (``eraNut06a``): the Moon's monthly
  declination extremes at the major and minor standstills, for
  ``archaeo.standstill``, and the June solstice sun's declination;
* three obliquity series: Vondrak et al. 2011 (from ERFA's long-term ecliptic
  and equator poles), IAU 2006 (``eraObl06``), for ``sun.mean_obliquity``
  (Laskar 1986).

Nothing here imports geotools: the reference is independent of the code it
checks. Run with the pinned environment in README.md:

    .scratch/refenv/bin/python scripts/validation/archaeoastronomy/moon_reference.py \
        --eph .scratch/eph
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import erfa
import numpy as np
from jplephem.spk import SPK
from skyfield.api import load, load_file, wgs84

OUT = Path(__file__).resolve().parents[3] / "tests/data/validation/archaeoastronomy"
C_KM_S = 299792.458
J2000 = 2451545.0

# Ancient standstill windows: (label, file, kind). Each DE441 excerpt spans the
# few years around one standstill (see README.md for the jplephem commands).
ANCIENT = [
    ("8000 BCE major", "de441_m8007.bsp", "major", -7999),
    ("8000 BCE minor", "de441_m8000.bsp", "minor", -7999),
    ("3000 BCE major", "de441_m3000.bsp", "major", -2999),
    ("3000 BCE minor", "de441_m2991.bsp", "minor", -2999),
    ("2000 BCE major", "de441_m2000.bsp", "major", -1999),
    ("2000 BCE minor", "de441_m2005.bsp", "minor", -1999),
    ("1 CE major", "de441_m0004.bsp", "major", 1),
    ("1 CE minor", "de441_p0005.bsp", "minor", 1),
]


def true_of_date(jd_tt: np.ndarray, mean: bool = False) -> np.ndarray:
    """(n, 3, 3) matrices ICRS -> true (or mean) equator and equinox of date (long-term)."""
    epj = erfa.epj(jd_tt, 0.0)
    rpb = erfa.ltpb(epj)
    if mean:
        return rpb
    dpsi, deps = erfa.nut06a(jd_tt, 0.0)
    eps = vondrak_obliquity(jd_tt)
    rn = erfa.numat(np.radians(eps), dpsi, deps)
    return rn @ rpb


def vondrak_obliquity(jd_tt: np.ndarray) -> np.ndarray:
    """Mean obliquity (degrees) from Vondrak et al. (2011): the angle between their poles."""
    epj = erfa.epj(np.asarray(jd_tt, dtype=np.float64), 0.0)
    ecl = erfa.ltpecl(epj)
    equ = erfa.ltpequ(epj)
    return np.degrees(np.arccos(np.clip(np.sum(ecl * equ, axis=-1), -1, 1)))


def declination(vectors: np.ndarray, matrices: np.ndarray) -> np.ndarray:
    v = np.einsum("nij,nj->ni", matrices, vectors)
    return np.degrees(np.arcsin(v[:, 2] / np.linalg.norm(v, axis=1)))


def moon_geocentric(kernel: SPK, jd: np.ndarray) -> np.ndarray:
    """Geocentric Moon (km, ICRS) at TDB ``jd``, corrected once for light time."""

    def at(t: np.ndarray) -> np.ndarray:
        return (kernel[3, 301].compute(t) - kernel[3, 399].compute(t)).T

    r = at(jd)
    return at(jd - np.linalg.norm(r, axis=1) / C_KM_S / 86400.0)


def sun_geocentric(kernel: SPK, jd: np.ndarray) -> np.ndarray:
    def at(t: np.ndarray) -> np.ndarray:
        earth = kernel[0, 3].compute(jd) + kernel[3, 399].compute(jd)
        return kernel[0, 10].compute(t) - earth

    r = at(jd).T
    return at(jd - np.linalg.norm(r, axis=1) / C_KM_S / 86400.0).T


def refine_peak(t: np.ndarray, y: np.ndarray, k: int) -> tuple[float, float]:
    y0, y1, y2 = y[k - 1 : k + 2]
    den = y0 - 2 * y1 + y2
    s = 0.5 * (y0 - y2) / den if den else 0.0
    return float(y1 - 0.25 * (y0 - y2) * s), float(t[k] + s * (t[1] - t[0]))


def monthly_extremes(t: np.ndarray, dec: np.ndarray, kind: str) -> dict:
    """The most (major) or least (minor) extreme monthly limit, north and south."""
    out = {}
    for which, sign in (("north", 1.0), ("south", -1.0)):
        v = sign * dec
        peaks = np.flatnonzero((v[1:-1] > v[:-2]) & (v[1:-1] >= v[2:])) + 1
        peaks = peaks[(peaks > 0) & (peaks < len(v) - 1)]
        values = np.array([refine_peak(t, v, int(k))[0] for k in peaks])
        pick = int(np.argmax(values) if kind == "major" else np.argmin(values))
        value, when = refine_peak(t, v, int(peaks[pick]))
        out[which] = sign * value
        out[f"{which}_jd_tt"] = when
    return out


def ancient(eph: Path) -> list[dict]:
    rows = []
    for label, name, kind, epoch in ANCIENT:
        kernel = SPK.open(str(eph / name))
        seg = kernel[3, 301]
        # The whole excerpt (7 to 10 years about the standstill), hourly: the
        # standstill is where the monthly limits are most (major) or least
        # (minor) extreme, found without reference to any node theory.
        t = np.arange(seg.start_jd + 2.0, seg.end_jd - 2.0, 1.0 / 24.0)
        dec = declination(moon_geocentric(kernel, t), true_of_date(t))
        found = monthly_extremes(t, dec, kind)
        # The June solstice sun: hourly over the excerpt, the largest declination each year.
        sun_dec = declination(sun_geocentric(kernel, t), true_of_date(t))
        sun_mean = declination(sun_geocentric(kernel, t), true_of_date(t, mean=True))
        k = int(np.argmax(sun_dec))
        km = int(np.argmax(sun_mean))
        rows.append(
            {
                "label": label,
                "kernel": name,
                "kind": kind,
                "epoch_year": epoch,
                "window_jd_tt": [float(t[0]), float(t[-1])],
                **found,
                "sun_max_declination_true": refine_peak(t, sun_dec, k)[0],
                "sun_max_declination_mean": refine_peak(t, sun_mean, km)[0],
                "sun_max_jd_tt": float(t[k]),
                # Vondrak's mean obliquity at the Moon's extremes and the sun's.
                "vondrak_at_north": float(vondrak_obliquity(np.array([found["north_jd_tt"]]))[0]),
                "vondrak_at_south": float(vondrak_obliquity(np.array([found["south_jd_tt"]]))[0]),
                "vondrak_at_sun": float(vondrak_obliquity(np.array([float(t[km])]))[0]),
                "sun_max_mean_jd_tt": float(t[km]),
            }
        )
        kernel.close()
    return rows


def modern(eph: Path, n: int = 300) -> dict:
    ts = load.timescale(builtin=True)
    planets = load_file(str(eph / "de440s.bsp"))
    earth, moon = planets["earth"], planets["moon"]
    rng = np.random.default_rng(20261114)
    jd_tt = np.sort(rng.uniform(2415020.5, 2488069.5, n))  # 1900 to 2100
    t = ts.tt_jd(jd_tt)
    ra, dec, distance = earth.at(t).observe(moon).apparent().radec(epoch="date")
    sites = {"north": (51.18, -1.83), "south": (-27.5, 133.0)}
    topo = {}
    for name, (lat, lon) in sites.items():
        alt, az, _d = (earth + wgs84.latlon(lat, lon)).at(t).observe(moon).apparent().altaz()
        topo[name] = {
            "latitude": lat,
            "longitude": lon,
            "altitude": alt.degrees.tolist(),
            "azimuth": az.degrees.tolist(),
        }
    # The standstills of the present (2006, 2015, 2025, 2034), as for the ancient ones.
    stand = []
    for label, kind, centre in (
        ("2006 major", "major", 2453900.0),
        ("2015 minor", "minor", 2457300.0),
        ("2025 major", "major", 2460680.0),
        ("2034 minor", "minor", 2464120.0),
    ):
        tt = np.arange(centre - 731.0, centre + 731.0, 1.0 / 24.0)
        d = earth.at(ts.tt_jd(tt)).observe(moon).apparent().radec(epoch="date")[1].degrees
        stand.append({"label": label, "kind": kind, **monthly_extremes(tt, d, kind)})
    # Check of the ERFA long-term pipeline used for ancient epochs against Skyfield now.
    tt = jd_tt[:50]
    kernel = SPK.open(str(eph / "de440s.bsp"))
    ltp = declination(moon_geocentric(kernel, tt), true_of_date(tt))
    kernel.close()
    pipeline = float(np.abs(ltp - dec.degrees[:50]).max() * 3600.0)
    return {
        "jd_tt": jd_tt.tolist(),
        "jd_ut1": t.ut1.tolist(),
        "delta_t": t.delta_t.tolist(),
        "ra": (ra._degrees % 360.0).tolist(),
        "dec": dec.degrees.tolist(),
        "distance_km": distance.km.tolist(),
        "topocentric": topo,
        "standstills": stand,
        "ltp_vs_skyfield_max_arcsec": pipeline,
    }


def obliquity() -> dict:
    years = np.arange(-7900, 12001, 500, dtype=np.float64)
    years[0] = -7999
    jd = J2000 + (years - 2000.0) * 365.25
    return {
        "year": years.tolist(),
        "jd_tt": jd.tolist(),
        "vondrak2011": vondrak_obliquity(jd).tolist(),
        "iau2006": np.degrees(erfa.obl06(jd, 0.0)).tolist(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--eph", type=Path, default=Path(".scratch/eph"))
    args = parser.parse_args()
    import importlib.metadata as md

    data = {
        "generated_by": "scripts/validation/archaeoastronomy/moon_reference.py",
        "versions": {p: md.version(p) for p in ("skyfield", "jplephem", "pyerfa", "numpy")},
        "ephemerides": "DE440s (Skyfield), DE441 part-1 excerpts (jplephem)",
        "modern": modern(args.eph),
        "ancient": ancient(args.eph),
        "obliquity": obliquity(),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "moon_reference.json"
    path.write_text(json.dumps(data, indent=None, separators=(",", ":")) + "\n")
    print(path, path.stat().st_size, "bytes")
    print(
        "ERFA long-term pipeline vs Skyfield, max",
        data["modern"]["ltp_vs_skyfield_max_arcsec"],
        '"',
    )


if __name__ == "__main__":
    main()
