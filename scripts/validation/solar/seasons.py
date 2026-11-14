"""Reference equinox and solstice moments for the Shadow Study, from Skyfield and DE440s.

Writes ``tests/data/validation/solar/seasons.json``: for each year 1850-2149
(every year), the March equinox, June solstice, September equinox and
December solstice from ``skyfield.almanac.seasons`` (the moments the Sun's
apparent geocentric ecliptic longitude, of date, crosses 0, 90, 180 and 270
degrees), as:

- ``ut1``: Unix seconds on the UT1 scale (Skyfield's own Delta T from IERS
  data and its long-term model), what GeoTools' times stand for; and
- ``tt``: the same moment in TT, as Unix-style seconds, to separate the
  ephemeris from the Delta T model.

Run:

    .scratch/refenv/bin/python -I scripts/validation/solar/seasons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c

UNIX_EPOCH_JD = 2440587.5


def main() -> None:
    from skyfield import almanac
    from skyfield.api import Loader, load_file

    eph = load_file(str(c.EPHEMERIS))
    ts = Loader(str(c.EPHEMERIS.parent)).timescale()
    first, last = 1850, 2149
    t0 = ts.ut1(first, 1, 1)
    t1 = ts.ut1(last + 1, 1, 1)
    times, which = almanac.find_discrete(t0, t1, almanac.seasons(eph))
    ut1 = (times.ut1 - UNIX_EPOCH_JD) * 86400.0
    tt = (times.tt - UNIX_EPOCH_JD) * 86400.0
    years = np.array([t.ut1_calendar()[0] for t in times])
    rows = {"year": [], "season": [], "ut1": [], "tt": []}
    for y, w, u, t in zip(years, which, ut1, tt, strict=True):
        rows["year"].append(int(y))
        rows["season"].append(int(w))
        rows["ut1"].append(round(float(u), 3))
        rows["tt"].append(round(float(t), 3))
    data = {
        "about": __doc__.split("\n\n")[0],
        "versions": c.versions(),
        "ephemeris": c.EPHEMERIS.name,
        "seasons": ["march", "june", "september", "december"],
        **rows,
    }
    c.write("seasons.json", data)


if __name__ == "__main__":
    main()
