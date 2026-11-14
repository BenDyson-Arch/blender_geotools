# Validation: the sun, sun hours, clear-sky insolation and shadow studies

Status: validated on `val/solar` (2026-11-14), against GeoTools 0.4.0-dev
(develop at 38fd2e3) in Blender 5.2.4 with rusty-tiles 0.4.1 and pyproj 3.8.0.
Three discrepancies were left for a decision; none was a bug in what the
tools claim, and all three are now [resolved](#decisions-resolved).

This covers the Sun sub-panel (Place Sun, Over a Day), Sun Hours (with its
clear-sky insolation), and Shadow Study, as described in the
[user guide](../guide/analysis.md) and the
[M5 design](../design/m5-analysis.md). The code is
`geotools/analysis/sun.py`, `_spa_tables.py`, `sun_hours.py`, `clearsky.py`,
`site.py` (`sun_site`), and the Blender side in `geotools/blender/analysis.py`
and `sun_hours.py`. For each tool this page gives the definition followed, the
references, the cases, the tolerances and why, the results and a verdict.

## Verdicts

| Tool | Definition followed | Verdict |
|---|---|---|
| Sun position | NREL SPA (Reda and Andreas 2003, rev. 2008), azimuth clockwise from true north, topocentric, SPA refraction | **agrees**: pvlib's SPA to 1×10⁻⁹°, JPL DE440 (Skyfield) to 0.00017° |
| ΔT | Espenak and Meeus (2006) polynomials | **agrees** (pvlib's coding to 10⁻⁷ s); the 2005–2050 branch now runs 6 s ahead of IERS, a 0.00007° effect |
| Time zones | IANA zones through `zoneinfo`; fixed offsets; a skipped or repeated time read with the offset before the change | **agrees** (23, 24.5 and 25 h days, UTC+05:45, UTC+14 and UTC−11 checked) |
| Grid north (convergence) | true azimuth − PROJ `meridian_convergence` (conformal CRSs); the grid-to-ground map at the site (non-conformal) | **agrees** (geodesics to 10⁻⁸° in conformal CRSs; to 10⁻⁶° in Albers EPSG:3577 and Web Mercator EPSG:3857 since [decision 1](#decisions-resolved), up to 0.24° here before) |
| Place Sun / Over a Day | light +Z towards the sun, Euler (90° − e, 0, 180° − scene azimuth); light off while the refracted centre is below 0°, keyed constant | **agrees** (geodesic grid bearing to 0.001°; keys off exactly at Skyfield's sunrise and sunset; switching between keys since [decision 3](#decisions-resolved)) |
| Sun Hours | time with the sun's refracted centre above the horizon (or far horizon), in front of the surface and unblocked; midpoint rule over local days | **agrees** (within half a step per edge; Cycles wall shadow at a zone edge placed to 0.15 m) |
| Clear-sky insolation | Ineichen–Perez (2002) as pvlib implements it, beam × cos i, isotropic sky diffuse, no ground reflection, kWh/m² | **agrees** with pvlib to 10⁻⁵; 64 sky rays integrate the isotropic sky to 2.4 %. **Not** ESRA/r.sun: differs from GRASS r.sun by the models' known differences |
| Shadow Study | the moments the Sun's apparent geocentric longitude crosses 0°, 90°, 180°, 270°, taken as local dates | **agrees** (DE440 to 26 s in TT; within a minute in UT1 to 2067, 3.3 min by 2149) |

No bugs were found. The brief asked whether insolation is ESRA (Rigollier et
al. 2000, as in GRASS r.sun): it is not, and it does not claim to be. It is
Ineichen–Perez, and the code, the design doc and the guide all say so.

## Method

### Reference tools and versions

All installed in an isolated environment (`.scratch/refenv`, a uv venv on
CPython 3.13, or Docker); nothing system-wide, and GeoTools gains no
dependency.

| Tool | Version | Used for |
|---|---|---|
| pvlib-python | 0.16.1 | `spa.solar_position` (numpy SPA), `spa.calculate_deltat`, `solarposition.get_solarposition(method="nrel_numpy")`, `clearsky.ineichen`, `atmosphere`, `irradiance.get_extra_radiation`, `irradiance.get_total_irradiance(model="isotropic")` |
| Skyfield | 1.55 (jplephem 2.24) | apparent topocentric sun, `almanac.find_risings` / `find_settings`, `almanac.seasons`; its built-in IERS ΔT and UT1 |
| JPL ephemeris | DE440s (`de440s.bsp`, 1849–2150, sha256 `c1c7feeab882263fc493a9d5a5b2ddd71b54826cdf65d8d17a76126b260a49f2`) | the sun for Skyfield |
| pyproj | 3.8.0 (PROJ bundled) | geodesics (`Geod.fwd`) and projections, *not* `get_factors` |
| pandas | 3.1.1 | local-day boundaries (tz database) for the references |
| numpy | 2.5.4 | |
| GRASS GIS | 8.4.1, Docker `osgeo/grass-gis:8.4.1-alpine` (image `sha256:6b1f1fc0…`) | `r.sun` (ESRA clear sky, Muneer diffuse), mode 2 |

### Reproducing

The scripts in `scripts/validation/solar/` never import GeoTools. They write
the fixtures in `tests/data/validation/solar/` (172 KB in all). Not run in
CI.

```sh
uv venv -p 3.13 .scratch/refenv
uv pip install -p .scratch/refenv pvlib==0.16.1 skyfield==1.55 pyproj==3.8.0 numpy==2.5.4 pandas==3.1.1
mkdir -p .scratch/ephem
curl -fL -o .scratch/ephem/de440s.bsp https://ssd.jpl.nasa.gov/ftp/eph/planets/bsp/de440s.bsp
docker pull osgeo/grass-gis:8.4.1-alpine

.scratch/refenv/bin/python -I scripts/validation/solar/sun_position.py  # sun_position.json, 5 s
.scratch/refenv/bin/python -I scripts/validation/solar/seasons.py       # seasons.json, 10 s
.scratch/refenv/bin/python -I scripts/validation/solar/sun_hours.py     # sun_hours.json, 2.5 min
.scratch/refenv/bin/python -I scripts/validation/solar/clearsky.py      # clearsky.json, 2 s
.scratch/refenv/bin/python -I scripts/validation/solar/grass_rsun.py    # rsun.json, 20 s (Docker)
```

`GEOTOOLS_DE440S` points the scripts at another copy of the ephemeris. The
r.sun driver runs one container (`--cpus 3 --memory 6g --rm`).

Checks (both run in normal CI):

```sh
pytest tests/core/test_validation_solar.py       # 44 tests, about 7 s
python3 scripts/run_blender_tests.py --expect available -k test_validation_solar
```

The Blender tests (`tests/blender/test_validation_solar.py`, 6 tests) take
about 20 s, most of it five Cycles bakes of a year's worth of minutes on one
quad.

### Tolerances

- **The algorithm against itself.** GeoTools and pvlib both code SPA in
  binary64 from the same paper, so they must agree to round-off: 10⁻⁶°.
- **SPA against the JPL ephemeris.** The paper states ±0.0003° for −2000 to
  6000 (truncated VSOP87, IAU 1980 nutation). 0.0003° of the Sun's ecliptic
  longitude is 26 s of its motion.
- **UT1 − UTC.** GeoTools takes times as UTC = UT1. |UT1 − UTC| < 0.9 s
  turns the sky by up to 0.0042°/s.
- **Time steps.** Sun hours count each step whole by its midpoint, so each
  sunrise, sunset, crossing of a surface's own plane, or shadow edge is off by
  at most half a step: a day with *k* such edges is within *k* × step / 2.
- **Fixtures** keep 12 significant digits; their inputs are rounded first
  (whole Unix seconds, angles to 10⁻⁷°), so they are held exactly.

## Sun position

**Definition.** NREL's Solar Position Algorithm (Reda and Andreas,
NREL/TP-560-34302, 2003, revised 2008), step by step. Azimuth is clockwise
(eastward) from true north in [0, 360); elevation is topocentric, with the
paper's refraction (eq. 42) when the sun is above −(0.26667° + 0.5667°), at the
given pressure and temperature (the Sun panel uses 12 °C and the standard
atmosphere's pressure at the site's height, 1013.25 hPa at sea level, since
[decision 2](#decisions-resolved)).
Latitudes and longitudes are geodetic, east and north positive. These are the
NREL conventions, which pvlib also follows.

**Cases.**

| Set | Cases | Reference | Result |
|---|---|---|---|
| Random sites, −1999 to 5999, random height 0–4 km, pressure 600–1050 hPa, −30 to 40 °C, poles and ±180° included | 240 | pvlib `spa.solar_position`, same ΔT | max 1.0×10⁻⁹° in elevation and in azimuth × cos(elevation) |
| Near the horizon (airless −1.5° to 5°), standard air | 60 | pvlib, same ΔT | max 1.8×10⁻¹¹° (elevation), 5×10⁻¹⁰° (azimuth): the refraction formula and its switch match |
| Random sites 1850–2149, airless, same UT1 and ΔT | 240 | Skyfield + DE440s | max 0.00017°, median 0.00006°, RMS 0.00007° (great-circle) |
| Random UTC moments 1973–2025, airless | 120 | Skyfield + DE440s with IERS UT1 and ΔT | max 0.0029°, median 0.0008°, RMS 0.0012°; every case within 0.0004° + 0.0042° × \|UT1 − UTC\| (\|UT1 − UTC\| up to 0.68 s here) |
| The paper's worked example (existing test) | 1 | Reda and Andreas, Table A5.2 | zenith 2×10⁻⁶°, azimuth 5×10⁻⁷° |

A first run of the random set disagreed with pvlib by up to 0.0018° at the
ends of the range: the fixture had rounded Unix times near −1.2×10¹¹ s to the
second (0.004° of rotation). Rounding the inputs before computing the
references removed it; GeoTools was not at fault.

**ΔT.** `sun.delta_t` against pvlib's independent coding of Espenak and
Meeus (330 points from −1999 to 3000, including every branch boundary):
max 1.3×10⁻⁷ s. Against reality: the polynomial for 2005–2050 gives 75.4 s
for 2026, while IERS (Skyfield) has 69.1 s. ΔT enters only the ephemeris, so
6 s moves the sun by 0.00007°. For the future the gap grows (94 s against an
IERS-based 72 s in 2050, 204 s against 96 s in 2100), which matters for
season moments (below) but hardly for positions (0.0013° in 2100).

**Verdict: agrees.** The algorithm is SPA to round-off, and SPA is within its
stated accuracy of DE440. In practice the error a user sees is UT1 − UTC
(under 0.004°).

## Time zones

**Definition.** IANA names through `zoneinfo`, with daylight saving; "UTC"
always; a fixed UTC offset rounded to the minute. A wall-clock time that a
daylight-saving change skips or repeats is read with the offset before the
change (`fold=0`), with a note.

**Cases** (sun hours' local days, against pandas' tz-database arithmetic):
Europe/London on 29 March (23 h) and 25 October 2026 (25 h); Australia/Sydney
on 5 April (25 h) and 4 October (23 h); Australia/Lord_Howe on 5 April (a
30-minute change, 24.5 h); Asia/Kathmandu (UTC+05:45) and the same as a fixed
offset (identical results); Pacific/Kiritimati (UTC+14); Pacific/Pago_Pago
(UTC−11, Shadow Study). Every period length matched to 10⁻⁹ h.

**Verdict: agrees.**

## North and convergence

**Definition.** The scene's +Y is grid north in a CRS project, the anchor's
north turned by the heading in an Anchor project, and true north in a Local
project. Sun directions are turned by `scene azimuth = true azimuth −
convergence`, with the convergence from PROJ's `meridian_convergence`
(clockwise from true north to grid north).

**Reference.** For each point, the grid bearing of a 1 m geodesic step
(`Geod.fwd`, central difference, then the projection) along true azimuths 0°,
45°, …, 315°. The convergence is minus the bearing of the northward step.
This does not use `get_factors`.

| CRS | Point | Convergence | `sun_site` − geodesic | Worst azimuth mapped |
|---|---|---|---|---|
| EPSG:7856 MGA 56 | west edge, 30° S | +1.4975° | 3×10⁻⁹° | 1.2×10⁻⁷° |
| EPSG:7856 | central meridian | 0 | 0 | 1×10⁻⁸° |
| EPSG:7856 | east edge | −1.5027° | 6×10⁻¹⁰° | 9×10⁻⁸° |
| EPSG:7855 MGA 55 | 38° S | +1.3991° | 6×10⁻¹⁰° | 7×10⁻⁸° |
| EPSG:32633 UTM 33N | 59° N, west | −3.8029° | 3×10⁻⁹° | 8×10⁻⁸° |
| EPSG:32633 | 48° N, east | +2.2484° | 3×10⁻⁹° | 4×10⁻⁸° |
| EPSG:32733 UTM 33S | east | −1.0115° | 4×10⁻⁹° | 4×10⁻⁸° |
| EPSG:32633 | 79° N | −2.3620° | 3×10⁻⁹° | 8×10⁻⁸° |
| EPSG:2193 NZTM | centre, far east | 0, −2.9916° | 6×10⁻⁹° | 9×10⁻⁸° |
| EPSG:27700 BNG | far south-west, east | −2.7024°, +2.9283° | 4×10⁻⁹° | 5×10⁻⁸° |
| EPSG:3031 Antarctic polar stereographic | | −33.6901° | 7×10⁻⁹° | 1×10⁻⁸° |
| EPSG:3413 Arctic polar stereographic | | −61.9275° | 2×10⁻⁹° | 2×10⁻⁸° |
| EPSG:3112 GA Lambert (conformal conic) | | +7.1954° | 2×10⁻⁹° | 9×10⁻⁸° |
| **EPSG:3857 Web Mercator** | 47° N | 0 | 0 | **0.089°** |
| **EPSG:3577 Australian Albers** | far south-west | +8.8563° | 9×10⁻⁹° | **0.24°** |

The sign is right everywhere: east of a transverse Mercator meridian the
convergence is positive in the north and negative in the south. In Blender,
Place Sun at the MGA 56 edge points the light at Skyfield's elevation and the
geodesic grid bearing to 0.001°, and a 20 m wall's shadow edge lands where the
geodesic bearing puts it (see Sun Hours).

**Verdict: agrees** for conformal projections (every transverse Mercator,
Lambert conformal and stereographic grid). For non-conformal ones one
rotation cannot map every direction when the projection shears angles
(the table's 0.089° and 0.24°); EPSG:3577 and EPSG:3857 are both on
rusty-tiles 0.4.1's allow-list. **Resolved**
([decision 1](#decisions-resolved)): there the sun is now turned through the
grid-to-ground map at the site, and `test_convergence_matches_geodesics`
holds every point of the table, both non-conformal rows included, to 10⁻⁶°.

Two smaller effects are not modelled and are documented here only. The grid
scale factor (up to 1.0007 at a UTM zone edge) stretches horizontal scene
distances but not heights, so a shadow in grid metres is k times its ground
length (an elevation error of up to 0.02° if read as an angle). And an Anchor
project's convergence is −heading by construction (its frame is the tangent
plane), which the existing tests check.

## Place Sun and Over a Day

**Definition.** The light's +Z points at the sun: Euler XYZ = (90° −
elevation, 0, 180° − scene azimuth), its Z angle unwrapped between keys. Its
strength is the panel's while the refracted centre is above 0°, else 0. Over
a Day keys are evenly spaced in UTC from the start time, linear between keys.

**Cases.** (1) MGA 56 west edge at 2026-03-19 23:00 UTC, refraction off: the
light's elevation and azimuth from +Y against Skyfield's elevation and the
geodesic grid bearing: within 0.001° (and 1.5° from the true azimuth, so the
convergence is applied). (2) 51.18° N on the June solstice, Europe/London,
1-minute keys for the whole day: the light is on at exactly the keys between
Skyfield's sunrise and sunset of the refracted centre (none within 2 s of an
event), 990 keys, 16.5 h.

**Verdict: agrees.** One presentational point was resolved
([decision 3](#decisions-resolved)): the strength was keyed linearly, so with
a Frame Step above 1 the frames between the last key with the sun up and the
first with it down faded the light rather than switching it off.

## Sun Hours

**Definition** ([design](../design/m5-analysis.md#sun-hours-insolation-and-shadow-studies)):
over the local days of the period (midnight to midnight in the time zone, so
23, 24.5 or 25 h on a change day), the hours during which the sun's centre is
above the horizon (apparent elevation > 0 with refraction on, airless with it
off), above the far horizon when one is set (a constant or an azimuth profile
interpolated linearly and periodically), in front of the surface
(cos i > 0), and unblocked by any rendering geometry. Steps are evenly spaced
in UTC, each counted whole by its midpoint. Directions are turned to grid
north by the convergence.

There is no single GIS standard for "sun hours": GRASS `r.sun`'s
`insol_time`, ArcGIS's duration maps and SAGA's insolation duration all count
time with the sun up and the cell unshaded at their own steps. GeoTools'
definition is the same, with the sun's centre and refraction stated.

**References.** Skyfield + DE440s: risings and settings of the sun's centre
against the airless elevation at which the SPA-refracted elevation is 0°
(−0.48° at 1013.25 hPa and 12 °C, found by bisection in the script), or
against the far horizon; and for surfaces and profiles, the sun sampled every
2 s. GRASS r.sun's `insol_time` as a second implementation.

**Open ground, single days** (`hours` from Skyfield; error = GeoTools −
reference at 1-, 5-, 15- and 60-minute steps, in minutes):

| Case | Reference | 1 min | 5 min | 15 min | 60 min |
|---|---|---|---|---|---|
| Equator, March equinox, airless | 11.9972 h | −0.83 | −4.83 | −14.8 | +0.2 |
| Equator, March equinox | 12.0734 h | +0.60 | +0.60 | +10.6 | −4.4 |
| 51.18° N, June solstice | 16.5070 h | −0.42 | −0.42 | −0.4 | −30.4 |
| 51.18° N, December solstice | 7.8114 h | +0.31 | +1.31 | −3.7 | +11.3 |
| 51.18° N, clocks forward (23 h day) | 12.6994 h | +0.04 | +3.04 | +3.0 | +18.0 |
| 51.18° N, clocks back (25 h day) | 10.0496 h | +0.02 | +2.02 | +12.0 | −3.0 |
| 51.18° N, June, horizon 5° | 15.0809 h | +0.15 | +0.14 | +10.1 | −4.9 |
| 27.5° S, June / December | 10.3581 / 13.8393 h | −0.49 / −0.36 | | | |
| Sydney DST end / start; Lord Howe (24.5 h) | | +0.11 / +0.52 / +0.44 | | | |
| Kathmandu (zone and fixed +05:45) | 10.3453 h | +0.28 (both) | | | |
| Tromsø 19 May (near midnight sun) / 25 Nov (0.9 h) | 23.3473 / 0.9067 h | +0.16 / −0.40 | | | |
| 89° N, March equinox | 16.3776 h | +0.34 | | | |
| Polar day (Tromsø 22 May, 80° N, 78° S Dec) and night (80° N, 78° S Jun, Tromsø Dec) | 24 / 0 h | exact | exact | exact | exact |

Every day is within one step (the bound for two edges), and polar day and
night are exact. **Seasons and years** at 1-minute steps: 27.5° S June–August
−2.4 min of 983 h, 51.18° N 2026 +5.7 min of 4,456 h, 78.2° N (UTC+01:00)
−0.6 min, the equator +12.9 min: all within 10⁻⁴.

**Coarse steps over a year are biased, not random.** Where sunrise keeps
nearly the same clock time all year, the midpoint rule's error at each edge
has nearly the same sign every day. At the equator (UTC+02:00, 30° E) a year
at 15-minute steps is 7.1 h short (−0.16 %) and at 60 minutes 29 h short
(−0.65 %); at 51.18° N, 2.6 h and 9.8 h. This agrees with the design doc's
table (0.35 % at 30 minutes, 0.64 % at 60), measured at 27.5° S.

**Slopes, walls and overhangs** (70 cases: horizontal, south 30°, north 30°,
north 60°, east 45°, west 45°, vertical south, north and east, and an
overhang tilted 120° facing south; at 51.18° N in March, June and December,
27.5° S in June and December, the equator at the equinox and 80° N in June):
at 1-minute steps the largest error is 1.03 min (equator, vertical north wall,
four edges), at 15-minute steps 12.8 min. Within the bound of half a step per
edge. Analytic checks inside the table: a north wall at 51° N in December and
a north 60° slope at 51° N in December get 0 h, and the overhang (its normal
30° below the horizon, towards the south) sees a southern sun only while it is
below 60°, so it gets nothing on the June solstice at 51° N.

**Horizon profile** (2°, 12°, 4°, 8° at north, east, south and west, 9 cases):
largest error 0.53 min at 1-minute steps.

**Merge Within** on 51.18° N, a year at 30-minute steps (8,928 sun steps):

| Merge within | Rays | Horizontal | South 30° | Vertical east | Vertical north | North 60° |
|---|---|---|---|---|---|---|
| 1° | 2,523 | 0 | +1.0 h | +0.5 h | 0 | +0.5 h |
| 2° (default) | 1,170 | 0 | −0.5 h | −1.0 h | −4.0 h | +1.5 h |
| 5° | 343 | 0 | +6.0 h | +2.0 h | +10.5 h | +5.5 h |

Open horizontal ground never changes. A surface changes only where a merged
ray falls on the other side of its own plane (up to 0.1 % of a year at 2°).
Insolation changes by at most 0.17 % at 2° (north 60°).

**In Blender (Cycles).** Five of the surfaces above (east 45° in December,
north 60° in March, vertical north in June, north 30° at 27.5° S in June, the
overhang at 80° N), each one quad turned by its object's rotation and baked
at 1-minute steps: all within 2.1 min, as in the core test. And
a 20 m east–west wall at the MGA 56 zone edge, with one 12-hour step whose
midpoint is 2026-03-19 23:00 UTC (sun 36.45° up at grid bearing 63.69°): the
shadow edge on 0.25 m ground faces must be at y* = −0.05 − H cos(Ag) / tan(e)
= −12.05 m. Every face more than 0.145 m from y* is 12 h or 0 h as expected.
Without the convergence the edge would be 0.64 m away, so the test would
fail.

**Against GRASS r.sun's `insol_time`** (56 planes, 0.05 h steps): median
difference 3 min. The exceptions are explained. On open ground GeoTools'
days are 3 to 29 minutes longer, and with refraction off they match r.sun's
to within its 0.05 h step (51.18° N June: r.sun 16.30 h, GeoTools 16.50 h
refracted and 16.35 h airless), so r.sun counts the airless sun and the
difference is refraction. And r.sun takes one declination per day, so at the equator on the equinox
(declination crossing 0 that day) it puts the sun on one side of an east–west
wall all day (12 h on the south face and 0 on the north) where GeoTools and
Skyfield give 8.67 h and 3.40 h.

**Verdict: agrees.**

## Clear-sky insolation

**Definition.** Ineichen and Perez (2002), "A new airmass independent
formulation for the Linke turbidity coefficient", as pvlib's
`clearsky.ineichen` implements it (no Perez enhancement): Kasten–Young (1989)
air mass on the apparent zenith, pressure from the site height (standard
atmosphere), 1366.1 W/m² over the SPA Earth–Sun distance squared, one Linke
turbidity (TL at air mass 2) for the period. On a surface: beam = DNI × cos i
for unblocked sun rays in front of it; diffuse = the isotropic (Liu–Jordan)
sky, ΣDHI·t × (1/π)∫V cos i dω, integrated with M Fibonacci sky rays (64 by
default); no ground-reflected light. Units: kWh/m² (Wh/1000).

**Is it ESRA, as in r.sun?** No. ESRA (Rigollier, Bauer and Wald 2000),
which r.sun uses, is a different clear-sky model with the same input (the
Linke turbidity at air mass 2, Kasten's definition), its own beam transmission
and diffuse functions, and Muneer's (1990) anisotropic diffuse on inclined
surfaces.

**Against pvlib's own chain** (`get_solarposition` SPA, `ineichen`,
`get_total_irradiance(model="isotropic", albedo=0)`), daily totals at 1-minute
steps for 8 site-days (51.18° N March, June, December; 27.5° S June and
December; the equator at the equinox; 64.1° N June at TL 5; 27.5° S June at
1,500 m and TL 2) and 7 surfaces each:

| Quantity | Largest difference |
|---|---|
| DNI, DHI, GHI daily totals | 9×10⁻⁷ relative |
| Beam on each surface | 6×10⁻⁶ relative (1×10⁻³ kWh/m² where one minute flips at sunrise or a surface's own plane) |
| Isotropic diffuse, 64 sky rays | −1.8 % (vertical south) to +1.8 % (vertical north); 0 on horizontal |

The diffuse difference is the sky-ray integral, not the model. The cosine sum
of M sky rays against (1 + cos tilt) / 2, worst over 24 aspects:

| Tilt | Exact | 64 rays | 256 rays | 1024 rays |
|---|---|---|---|---|
| 0° | 1 | 0 | 0 | 0 |
| 30° | 0.933 | 0.8 % | 0.2 % | 0.04 % |
| 60° | 0.75 | 1.6 % | 0.3 % | 0.08 % |
| 90° | 0.5 | 2.4 % | 0.6 % | 0.15 % |
| 120° | 0.25 | 4.3 % | 1.0 % | 0.27 % |
| 150° | 0.067 | 10.7 % | 2.6 % | 0.62 % |

It converges as 1/M. On open horizontal ground it is exact, as the design doc
says.

**Against GRASS r.sun 8.4.1** (ESRA, same TL, solar constant 1366.1, albedo 0,
0.05 h steps, the same 56 planes):

| Quantity | GeoTools against r.sun |
|---|---|
| Beam, surfaces with > 0.5 kWh/m² | median −4.3 %, range −36 % to +0.5 % |
| Global on open ground | −4.1 % to −13.3 % |
| Diffuse on open ground | −8 % to −43 % |
| Diffuse on tilted surfaces | −81 % to +41 % |

Ineichen gives less beam than ESRA at high air mass (winter, low sun, north
walls), and less diffuse everywhere. On tilted surfaces the isotropic sky
gives less diffuse than Muneer's model on surfaces facing the sun (no
circumsolar brightening) and more on surfaces facing away. The design doc
already says this; these are its sizes. Neither model is right everywhere:
their accuracy against measurements depends on the site and on the turbidity
given (Ineichen 2006 compares both against measured clear-sky data).

A GRASS pitfall found on the way: in r.sun 8.4.1, `aspect_value=` behaves
as a compass bearing (180 faces south, 0 means no aspect), although its help
says "270 is south"; an aspect *raster* follows the documented convention
(counter-clockwise from east). The driver uses rasters.

**Verdict: agrees** with the model it claims, to 10⁻⁵; the diffuse sky's
integration error with the default 64 rays is up to 2.4 % on walls (raise
Sky Rays for steep surfaces). Differs from ESRA/r.sun by the known model
differences above.

## Shadow Study

**Definition.** The equinoxes and solstices of the Sun panel's year: the
moments the Sun's apparent geocentric longitude (SPA's λ, of date) crosses
0°, 90°, 180° and 270°, interpolated linearly between hourly values. Each is
turned into a local date in the chosen time zone, and the light is keyed at
the chosen local times on those dates.

**Against Skyfield's `almanac.seasons`** with DE440s (the same definition), all
1,200 moments from 1850 to 2149:

| Years | In TT (ephemeris only) | In UT1 (what the study uses) |
|---|---|---|
| 1850–1899 | 17 s | 19 s |
| 1900–1954 | 16 s | 16 s |
| 1955–2025 | 18 s | 18 s |
| 2026–2067 | 18 s | under 60 s |
| 2068–2099 | 18 s | up to 121 s |
| 2100–2149 | 26 s | up to 201 s |

In TT the error is SPA's longitude accuracy (26 s for 0.0003°). In UT1 the
ΔT models differ for future years. The design doc's "within a minute of the
published times" holds to 2067. Only the local date is used, so a moment off
by a minute or three changes the study only when it falls within that of
local midnight.

**In Blender:** the study's four days of 2026 in Pacific/Kiritimati
(UTC+14), Pacific/Pago_Pago (UTC−11) and UTC match the local dates of
Skyfield's moments.

**Verdict: agrees.**

## Edge cases covered

Poles (89.9° N and S in SPA; 89° N on the equinox; polar day and night at
69.65° N, 78° S and 80° N), the antimeridian (±180°), the equator at the
equinox (the sun through the zenith and across the east–west plane in a day),
heights to 4 km and pressures 600–1050 hPa, the horizon and the refraction
switch, dates from −1999 to 5999, 23, 24.5 and 25 h days, UTC+14 and UTC−11,
45-minute offsets, vertical faces and overhangs, faces that never see the sun,
and polar stereographic grids with convergence of 34° and 62°.

## Limits users should know

- Times are UTC with UT1 − UTC taken as 0: up to 0.004° today.
- ΔT for the future is the Espenak–Meeus extrapolation. It is already 6 s
  ahead of IERS today and runs further ahead of IERS-based predictions (about
  1 min by 2075, 2 min by 2100). Positions change by under 0.002°; season
  moments by that much time.
- Refraction is at 12 °C and the standard atmosphere's pressure at the
  site's ellipsoidal height (616 hPa at 4,000 m), not the day's weather; a
  Local project has no height and refracts at sea level
  ([decision 2](#decisions-resolved)).
- Sun hours over a year at coarse steps are biased low at low latitudes
  (−0.16 % at 15 minutes, −0.65 % at 60, at the equator). Use 5 minutes or
  less for a year at low latitudes when the totals matter.
- Diffuse insolation is isotropic and integrated with 64 rays by default:
  up to 2.4 % off the isotropic integral on walls and 4–11 % on overhangs.
  Against an anisotropic model it can be tens of percent off.
- Insolation is a clear-sky upper bound (Ineichen–Perez); r.sun's ESRA gives
  4–13 % more on open ground with the same turbidity.

## Decisions (resolved)

The validation left three points for a decision. The user's decisions
(2026-11-14) are implemented on `fix/validation-decisions`, each with
regression tests.

1. **Non-conformal CRSs.** `scene azimuth = true azimuth − convergence` is
   exact only where the projection preserves angles: in EPSG:3577 it was up
   to 0.24° off at this page's point (1.12° at 150° E, 12° S, 1.81° in
   Tasmania), in EPSG:3857 0.09° at 47° N (0.19° at the equator). The
   geodesy validation reached the same point independently
   ([geodesy.md, decision 3](geodesy.md#decisions-resolved)). **Done:** sun
   directions (Place Sun, Over a Day, Sun Hours, the sky rays' horizon
   lookups, Shadow Study) go through the grid-to-ground map at the sun's site
   that Measure uses (`site.SunSite.scene_azimuth`), exact for any projection
   and the old arithmetic, bit for bit, in conformal CRSs. Against projected
   geodesic steps: 10⁻⁶° at every point of the North and convergence table
   and at three more non-conformal sites. In Blender, Place Sun in EPSG:3577
   at 150° E, 12° S points the light at the geodesic grid bearing to 10⁻⁴°
   (float32 rotation), where the convergence alone is more than 0.5° off at
   that moment. The
   Sun panel notes a non-conformal CRS. Elevations are unchanged.
2. **Refraction at altitude.** The Sun panel refracted at 1013.25 hPa at any
   height. **Done:** SPA refracts at the ICAO standard atmosphere's pressure
   for the site's ellipsoidal height (`sun.standard_pressure`, exactly
   1013.25 hPa at 0 m, so sea-level results are unchanged; 898.8 hPa at
   1,000 m, 616.4 at 4,000 m), with the temperature at 12 °C as before, in
   Place Sun, Over a Day, Sun Hours, insolation and Shadow Study. At 4,000 m
   the June day at 51° N is 4.0 minutes shorter than GeoTools counted. The
   clear-sky fixture now gives pvlib the site's pressure (only its 1,500 m
   site changes: DNI 8.8177 → 8.8119 kWh/m², which GeoTools matches to
   10⁻⁸). The archaeoastronomy events do the same
   ([archaeoastronomy.md, decision 4](archaeoastronomy.md#decisions-resolved)).
3. **Over a Day strength between keys.** The strength was keyed LINEAR, so
   with a Frame Step of 4 the three frames between the last key with the sun
   up and the first with it down had 0.75, 1.5 and 2.25 of a strength of 3.
   **Done:** the strength is keyed CONSTANT, as Shadow Study keys it; the
   light switches at the first key with the sun up (or down), and its
   direction still moves smoothly. `tests/blender/test_analysis.py::test_day_strength_switches_between_keys`.

Also worth a look, not a discrepancy: the ΔT polynomial for 2005–2050 is
6 s high today. An updated table (IERS to date, then a current long-term
prediction) would bring season moments within a minute to 2100. Its effect on
positions is negligible.
