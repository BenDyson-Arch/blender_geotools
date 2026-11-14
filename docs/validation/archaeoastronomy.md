# Validation: archaeoastronomy

Status: validated on `val/archaeoastronomy` (2026-11-14), against GeoTools
0.3.0-dev (develop at 38fd2e3) in Blender 5.2.4 with rusty-tiles 0.4.1. Two
bugs were found and fixed; four points are left for a decision
([below](#discrepancies-left-for-a-decision)).

This covers the Archaeoastronomy sub-panel
([m5-analysis.md](../design/m5-analysis.md#archaeoastronomy)): the horizon
profile (`geotools/analysis/horizon.py`, the Cycles bake in
`geotools/blender/archaeo.py`), the Moon (`moon.py`, `_moon_tables.py`), and
the events, standstills, sightlines and alignment check (`archaeo.py`). For
each tool it gives the definition followed, the reference, the cases, the
tolerances, the results and a verdict.

## Conventions

- **Azimuth** is **true** azimuth (from the geodetic meridian, not grid
  north), in degrees, **clockwise from north**, in [0°, 360°). In a projected
  CRS the scene's +Y is grid north and GeoTools converts with
  `true = grid + convergence`, the convergence being PROJ's meridian
  convergence at the point (sign checked against the meridian in
  `tests/core/test_site.py::test_convergence_at_an_mga_zone_edge`). A Local
  project's +Y is true north; an Anchor project turns by its heading. Rays are
  cast at scene azimuth `true − convergence`, so the profile is tabulated in
  true azimuths.
- **Altitudes** of the horizon are **apparent** (what an observer measures:
  curvature lowers, terrestrial refraction raises); a body is on the horizon
  when the **true** (airless) altitude of its chosen limb is `h − R(h)`.
- **Declinations** are geocentric, on the true equator of date for the
  ephemeris values and the mean equator of the epoch for the epoch geometry
  (`±ε`, `±(ε ± i)`). Years are astronomical (0 = 1 BCE); dates before 1582
  October 15 are Julian calendar (Meeus ch. 7); standstill dates are UT.

## Verdicts

| Tool | Definition followed | Verdict |
|---|---|---|
| Horizon profile: curvature and terrestrial refraction | `e = atan(Δz/d) − (1 − k) d / 2R`, k = 0.13, R = 6,371 km (the standard small-angle form; GRASS r.horizon uses the same with k = 0) | **agrees**: 0.0015° from exact spherical geometry for skylines to 5°, 1.3×10⁻⁶° from r.horizon's formula |
| Horizon profile: the ray search (Cycles bake) | the highest-appearing visible terrain point per azimuth | **bug, fixed** ([e9bc479](#bugs-fixed)): could miss a near crest by up to 0.1° with curvature on. Now within 0.0015° of the exact TIN skyline at 360 azimuths; median −0.0007° (biased low by the fine step) |
| Horizon flags: open, closed, edge, Ignore Within | as documented | **agrees** (existing tests; dip test here) |
| Astronomical refraction | Bennett (1982), Meeus eq. 16.4, 1010 hPa, 10 °C | **agrees**: 34.48′ at 0°, Saemundsson's inverse to 3.7″, Skyfield's Bennett to 2″ |
| Moon position | Meeus ch. 47 (truncated ELP-2000/82) + SPA nutation and sidereal time | **agrees**: 1.3″ RMS (4.1″ max) in declination against DE440 |
| Topocentric parallax | Meeus ch. 40 (ρ sin φ′, ρ cos φ′) | **agrees**: altitude 1.6–2.1″ RMS against DE440 topocentric |
| Standstills, Node Cycle | `±(ε + i)` major, `±(ε − i)` minor, i = 5.145396°, mean node (Meeus 47.7) | **agrees** (the textbook geometry; the real extremes differ by the 173-day wobble, ±0.15°, as documented) |
| Standstills, Ephemeris | most (major) / least (minor) extreme monthly limit within a year of the node crossing | **agrees**: 2″ now, 7″ at 3000 BCE, 52″ at 8000 BCE against DE441, almost all of it the obliquity series ([decision 1](#discrepancies-left-for-a-decision)) |
| Obliquity (epochs) | Laskar (1986) polynomial | **agrees with a documented difference**: within 1″ of Vondrák et al. (2011) from 1000 BCE to 6000 CE, 5.5″ at 3000 BCE, 42″ at 8000 BCE; the docs claim "a few arc seconds at the ends" |
| Epoch range | ±10,000 years of 2000 | **bug, fixed** ([943e320](#bugs-fixed)): Compute Events failed for 8000/7999 BCE and 11994–11999 CE |
| Rise/set azimuths (epoch geometry) | trace of the diurnal circle, limb, refraction, parallax, any horizon | **agrees**: 2×10⁻⁴° from the closed form (180 cases) |
| Dated sunrises and sunsets | SPA every 20 s, upper limb vs `h − R(h)`, first rise / last set | **agrees**: 0.0032° and 1 s from Skyfield + DE440 (216 events, three horizons, 64° N–60° S) |
| Moonrise and moonset geometry | as above with parallax | **agrees**: 0.0054° from DE440 topocentric (119 events); 1.9° off without parallax |
| Check Alignment: line azimuth | grid bearing + convergence at the line's middle | **agrees**: 0.1″ from the geodesic azimuth (pyproj.Geod) to 30 km |
| Check Alignment: declination | `sin δ = sin φ sin h_g + cos φ cos h_g cos A`, h_g from `h − R(h)` − limb + parallax | **agrees, documented convention**: the sun's parallax (8.8″) is left out; 10″ from DE440 without it, 1.5″ with it |
| Sightlines | eye to the profile's skyline point at the event's azimuth | **agrees** (by construction; `tests/blender/test_archaeo.py`) |

## Method

All inputs are synthetic: coordinates are round numbers, the terrain is
formulas. No survey or site data is used.

### Reference tools and versions

| Tool | Version | Used for |
|---|---|---|
| Skyfield | 1.53 (with its built-in ΔT and IERS tables) | apparent and topocentric Moon and Sun, `searchlib.find_discrete` for rise and set, Bennett refraction |
| JPL DE440s | NAIF `de440s.bsp`, sha256 `c1c7feea…60a49f2` | modern Moon and Sun (1849–2150) |
| JPL DE441 part 1 | NAIF `de441_part-1.bsp`, nine excerpts (jplephem `excerpt`, 0.4–0.8 MB each, targets 3, 10, 301, 399) | Moon and Sun 8000 BCE to 9 CE |
| jplephem | 2.23 | reading DE441 excerpts without a frame model |
| ERFA (pyerfa) | 2.0.1.5 | long-term precession `eraLtpb`, ecliptic and equator poles `eraLtpecl`/`eraLtpequ` (Vondrák, Capitaine and Wallace 2011), IAU 2000A nutation `eraNut06a`, IAU 2006 obliquity `eraObl06` |
| astropy | 7.1.0 (astropy-iers-data 0.2026.11.9) | a third Sun position at each sunrise (ERFA's own ephemeris, no JPL) |
| GRASS GIS | 8.5.0, Docker `osgeo/grass-gis:8.5.0-alpine` (`sha256:12598317c743c5ec31ad252aedfb1c0ff5da757fffe428a7b48ce3f3debc3cdd`) | `r.horizon` |
| pyproj | 3.8.0 (core tests) | geodesic azimuth for the line-bearing check |
| numpy | 2.3.4 (references), 2.5.4 (tests) | the exact TIN march |
| Meeus, *Astronomical Algorithms* 2nd ed. | — | example 47.a, eqs. 16.4, 22.3, 47.7, ch. 7 (existing tests) |

For ancient epochs Skyfield's own frame (IAU 2006 precession) is not used: it
is a polynomial for a few centuries (it leaves Vondrák's obliquity by 6″ at
3000 BCE and 200″ at 8000 BCE). Instead the DE441 geocentric vectors are
turned to the true equator of date with ERFA's long-term precession and IAU
2000A nutation. That pipeline agrees with Skyfield to 0.0007″ over 1900–2100.

### Reproducing

The reference environment lives in the clone's ignored `.scratch/`:

```sh
uv venv --python 3.13 .scratch/refenv
uv pip install --python .scratch/refenv/bin/python skyfield==1.53 jplephem==2.23 \
    astropy==7.1.0 pyerfa==2.0.1.5 numpy==2.3.4
mkdir -p .scratch/eph && cd .scratch/eph
curl -O https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp
U=https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de441_part-1.bsp
for w in "-8011/1/1 -8003/1/1 m8007" "-8003/1/1 -7995/1/1 m8000" \
         "-3004/1/1 -2994/1/1 m3000" "-2995/1/1 -2987/1/1 m2991" \
         "-2004/1/1 -1994/1/1 m2000" "-2009/1/1 -2002/1/1 m2005" \
         "-8/1/1 0/1/1 m0004" "2/1/1 9/1/1 p0005"; do
  set -- $w
  ../refenv/bin/python -m jplephem excerpt --targets 3,10,301,399 -- $1 $2 $U de441_$3.bsp
done
cd ../..
docker pull osgeo/grass-gis:8.5.0-alpine
.scratch/refenv/bin/python scripts/validation/archaeoastronomy/moon_reference.py --eph .scratch/eph
.scratch/refenv/bin/python scripts/validation/archaeoastronomy/events_reference.py --eph .scratch/eph
.scratch/refenv/bin/python scripts/validation/archaeoastronomy/horizon_reference.py --grass --work .scratch/grass
```

(`jplephem excerpt` fetches only the needed records over HTTP range requests,
so the 1.6 GB kernel is never downloaded.) The scripts do not import
GeoTools. They write `tests/data/validation/archaeoastronomy/*.json` (180 kB
in all), which `tests/core/test_validation_archaeoastronomy.py` (111 tests,
about 3 s) and `tests/blender/test_validation_archaeoastronomy.py` (4 tests,
about 1 s of bakes) compare GeoTools against in normal CI.

### Tolerances

- **Horizon:** the fine pass casts 64 rays per 0.1°, so the skyline is the
  highest fine ray that hits, up to 0.1°/64 = 0.0016° below the true one.
  Scene positions are float32 (2 mm at 20 km, 10⁻⁵° at 10 km). Allowed:
  0.003° against the exact TIN, 0.0025° against the cone's closed form, 0.002°
  for the dip and the near crest.
- **Moon:** Meeus gives 10″ in longitude and 4″ in latitude for his
  truncation. Allowed 6″ in declination, 10″ in R.A. cos δ, 10″ in altitude.
- **Standstills:** 3″ now; for ancient epochs, 3″ (12″ at 8000 BCE) after
  removing the Laskar–Vondrák obliquity gap, which is reported, not hidden.
- **Events:** the diurnal-circle trace steps 0.01° of hour angle and
  interpolates linearly: 2×10⁻⁴° against the closed form. Dated sunrises
  interpolate SPA every 20 s (the Sun's azimuth moves about 0.002° in 20 s
  at the horizon): 0.005° and 2 s against Skyfield's 1 ms root-finding.

## Horizon profile

**Definition.** For each true azimuth the skyline is the visible terrain point
with the largest apparent altitude `e = atan(Δz / d) − (1 − k) d / 2R`, with d
the horizontal ground distance (grid distance over the combined scale factor),
Δz the height above the eye, R = 6,371 km and k = 0.13 (0 with Refraction
off; no term with Earth Curvature off). This is the standard engineering form
(effective Earth radius R / (1 − k)); GRASS r.horizon uses the same with k = 0.

**Curvature model against exact geometry.** On a sphere of radius R/(1 − k)
with horizontal distances as arcs at the eye's height, the exact apparent
altitude is `atan2((R′+H) cos θ − (R′+h₀), (R′+H) sin θ)`. Over d = 100 m to
200 km, Δz = −500 m to 4 km and eyes at 0 to 3 km, the GeoTools form is within
**0.0015° for skylines up to 5°**, 0.003° to 10°, growing to 0.008° for 45°
near-vertical relief (where the drop itself is negligible).
(`test_curvature_against_spherical_geometry`.)

**The dip of a plain.** On a level plain the largest of
`−atan(e/d) − (1 − k) d / 2R` is the classical dip of the sea horizon,
`√(2e(1 − k)/R)`, at `d = √(2eR/(1 − k))`. The bake on a 100 km plain with the
eye 20 m up gives 0.1339° within 0.002° in every direction
(`test_dip_of_the_horizon`), and the pure two-pass search reproduces it for
eyes of 1.6, 20 and 200 m (`test_dip_of_the_horizon_on_a_plain`).

**A cone.** A 512-sided cone (400 m high, 1.5 km radius, apex 3 km north) on
a plain: the profile matches the cone's silhouette, found by a 0.25 m search
along each azimuth, within 0.0025° at every degree within 28° of the apex.

**Synthetic DEM against an exact TIN march and GRASS r.horizon.** A 40 km
square DEM, 401 × 401 nodes 100 m apart: a valley ringed by hills 7 km out
(140–560 m high, three lobes), a 900 m mountain 16 km north-east, a 120 m
knoll 2.3 km north-west and a 15 m undulation. GeoTools bakes it as the
triangulated grid (each cell split SW–NE). The reference march evaluates the
same triangles exactly: along an azimuth the terrain is linear between grid
and diagonal crossings, so the apparent altitude is evaluated at every crossing
(and halfway, for the curvature term). GRASS gets the DEM as a raster with
cell centres on the nodes, the observer's cell raised by the eye height
(r.horizon has no eye-height option, so every other cell is lowered by 1.6 m),
`distance=0.5`, curvature on, no refraction (r.horizon has none). r.horizon
reports negative horizons as 0, so the DEM's skyline is above level all round.

| GeoTools (1° steps, 360 azimuths) against | max | RMS | median |
|---|---|---|---|
| exact TIN, k = 0.13 (default) | 0.00149° | 0.00080° | −0.00068° |
| exact TIN, k = 0 | 0.00149° | 0.00079° | −0.00067° |
| exact TIN, no curvature | 0.00156° | 0.00088° | −0.00078° |
| GRASS r.horizon, all 360 | 0.206° | 0.028° | −0.00003° |
| GRASS r.horizon, the 338 azimuths whose skyline is > 4 km away | 0.055° | 0.015° | +0.00015° |

Against the exact TIN the error is the fine step's bias, as expected. Against
GRASS there is no bias; the scatter is r.horizon's raster sampling, largest at
the steep knoll 2.3 km away (where 50 m of sampling offset is 0.05–0.2°). A
bilinear march over the raster agrees with the TIN to 0.0018° RMS, and neither
a nearest-cell nor a bilinear march reproduces r.horizon exactly (0.03° RMS),
so the residual is in how r.horizon steps through cells, not in the angle
formula: on two bands of a level raster (100 m high 30 km away, 50 m high
10 km away) r.horizon gives `atan(H/d) − d/2R` to 1.3×10⁻⁶° at the distance
it reports. Skyline distances agree with the TIN's to a median of under 50 m
(a broad crest's grazing ray meets its near slope).

**The bug found.** The coarse pass casts every 0.1° and the fine pass filled
only the bracket above the best coarse hit. Without curvature that bracket
always holds the skyline. With curvature, the coarse ray just above a near
crest can pass over it and hit a farther range that appears *lower* than the
crest once curvature lowers it, yet higher than the crest's coarse hit. The
fine pass then refined the far range and the crest was lost: a crest 1 km off
at 1.099° with a range 26 km off at 1.101° (0.998° after curvature) gave
0.998° instead of 1.095°, 0.097° low. The coarse ray just below the skyline
meets the terrain no farther away than the skyline point, so it appears less
than one coarse step below it; the fine pass now refines every coarse hit
within 0.1° of the best (`horizon.fine_brackets`). On the DEM that is two
brackets per azimuth, 298,440 rays instead of 275,400 (+8 %, the bake still
under a second). Regression tests: `tests/core/test_archaeo.py::
test_fine_pass_finds_a_near_crest_below_a_farther_range` (the search with
first hits in closed form) and `test_near_crest_below_a_farther_range` (the
bake).

**Verdict:** agrees, after the fix.

## Refraction

Bennett's formula (Meeus eq. 16.4) at 1010 hPa and 10 °C, apparent to true:
34.48′ at 0°. Its inverse, Saemundsson's formula (true to apparent), gives the
same refraction to 3.7″ from 0° to 30°; Skyfield's implementation of Bennett
(a slightly different pressure scaling, 0.28 P / (T + 273)) agrees to 2″.
Below −1° GeoTools holds R at its −1° value (an observer above the land
around); Saemundsson's form is not valid there either. Real refraction at the
horizon varies by several arc minutes with the weather (more in inversions),
far more than any ephemeris error here; GeoTools does not take pressure or
temperature. **Verdict:** agrees.

## The Moon

**Definition.** Meeus ch. 47 (60 + 60 periodic terms of ELP-2000/82, with E
and A1–A3), SPA's nutation, true obliquity and sidereal time, Meeus ch. 40 for
the topocentric place (geodetic latitude via ρ sin φ′, ρ cos φ′).

**Against DE440** (300 random instants 1900–2100; Skyfield, apparent place on
the true equator and equinox of date; topocentric airless altitude and azimuth
at 51.18° N 1.83° W and 27.5° S 133° E, ΔT as Skyfield's):

| Quantity | max | RMS |
|---|---|---|
| geocentric declination | 4.1″ | 1.3″ |
| right ascension × cos δ | 8.3″ | 2.6″ |
| distance | 46 km | 28 km |
| topocentric altitude (N / S site) | 5.4″ / 5.9″ | 1.6″ / 2.1″ |
| topocentric azimuth × cos h (N / S) | 8.7″ / 7.6″ | 2.4″ / 2.0″ |

That is Meeus' stated accuracy. Without the parallax, altitudes near the
horizon would be about 1° wrong; with it they are within seconds.
**Verdict:** agrees.

## Lunar standstills and the obliquity

**Definition.** The mean node (Meeus eq. 47.7) at 0° is a major standstill,
at 180° a minor one; **Node Cycle** uses `±(ε + i)` and `±(ε − i)` with
i = 5.145396° and Laskar's ε; **Ephemeris** finds, hourly over a year either
side of the node crossing, the most extreme (major) or least extreme (minor)
monthly limit of the geocentric apparent declination, refined by a parabola.

**Reference.** The same quantity from JPL ephemerides found without any node
theory: the monthly declination limits over the whole DE441 excerpt (7–10
years round the standstill) or ±2 years of DE440s, and the most (least)
extreme of them. Declinations to 4 decimals for DE441, 5 for DE440s.

| Standstill | GeoTools north / south | JPL north / south | difference | Laskar − Vondrák ε |
|---|---|---|---|---|
| 2006 major (DE440) | +28.72264 / −28.72281 | +28.72266 / −28.72313 | −0.1″ / +1.2″ | — |
| 2015 minor | +18.14045 / −18.13347 | +18.13993 / −18.13320 | +1.9″ / −1.0″ | — |
| 2025 major | +28.71713 / −28.72588 | +28.71667 / −28.72567 | +1.6″ / −0.7″ | — |
| 2034 minor | +18.14428 / −18.15127 | +18.14420 / −18.15124 | +0.3″ / −0.1″ | — |
| 1 CE major (DE441) | +28.9732 / −28.9737 | +28.9733 / −28.9735 | −0.4″ / −0.8″ | −0.3″ |
| 1 CE minor | +18.4151 / −18.4141 | +18.4153 / −18.4142 | −0.6″ / +0.2″ | −0.3″ |
| 2000 BCE major | +29.2062 / −29.2182 | +29.2054 / −29.2177 | +2.9″ / −1.9″ | +2.0″ |
| 2000 BCE minor | +18.6482 / −18.6405 | +18.6475 / −18.6401 | +2.5″ / −1.7″ | +2.1″ |
| 3000 BCE major | +29.3163 / −29.3193 | +29.3147 / −29.3178 | +5.6″ / −5.4″ | +5.5″ |
| 3000 BCE minor | +18.7358 / −18.7473 | +18.7341 / −18.7453 | +5.9″ / −7.1″ | +5.5″ |
| 8000 BCE major | +29.5248 / −29.5133 | +29.5103 / −29.4991 | +52″ / −51″ | +42″ |
| 8000 BCE minor | +18.9291 / −18.9330 | +18.9155 / −18.9200 | +49″ / −47″ | +42″ |

Every ancient difference is almost exactly the gap between the two obliquity
series; with it removed the Moon agrees to 2″ back to 3000 BCE and 10″ at
8000 BCE, where Meeus' mean elements drift. (The 8000 BCE major standstill
was found only after the first bug was fixed: before it, that epoch raised.) The dates agree to the hour once
GeoTools' ΔT is added (its UT dates are 0.9 days before DE441's TT at
3000 BCE, 3.6 days at 8000 BCE: ΔT itself). GeoTools' ΔT (Espenak and Meeus
2006) and Skyfield's (Morrison, Stephenson et al.) differ by 17 minutes at
3000 BCE and 6 s in 2026; it moves times, never declinations.

**Obliquity.** DE441's own ecliptic, with Vondrák's equator, puts the June
solstice sun at Vondrák's ε to 1.1″ at every epoch, so for the ecliptic DE441
and Vondrák agree. Laskar's polynomial (GeoTools) against Vondrák et al.
(2011) and IAU 2006:

| Year | Laskar − Vondrák | Laskar − IAU 2006 |
|---|---|---|
| 8000 BCE | +42.1″ | −198″ |
| 5000 BCE | +17.5″ | −34″ |
| 3000 BCE | +5.5″ | −6.7″ |
| 2000 BCE | +2.0″ | −2.6″ |
| 1000 BCE | +0.3″ | −1.1″ |
| 1 CE to 6000 CE | within ±0.5″ | within ±2.8″ |
| 9000 CE | −6.6″ | +31″ |
| 11600 CE | −25.0″ | +155″ |
| 11999 CE | −26.0″ | +194″ |

Laskar (1986) and Vondrák (2011) agree within 1″ from 1000 BCE to 6000 CE;
IAU 2006 is a short-term polynomial and leaves both after about 1000 years.
At 8000 BCE the 42″ (0.012°) moves a June sunrise at 51° N by about 0.02°.
**Verdict:** the standstills agree; the obliquity agrees with a documented
difference: GeoTools' docs say Laskar's polynomial is good to "a few arc
seconds at the ends" and the lunar extremes to "about 0.01°"; against the
modern long-term series it is 42″ at 8000 BCE and the extremes 0.015°.

## Rise and set

**Epoch geometry against the closed form.** For a body of declination δ whose
limb (offset s, + for the upper limb) touches a level horizon of apparent
altitude h, the centre's topocentric true altitude is `h − R(h) − s`, the
geocentric `h_g` adds `asin(sin π cos h′)`, and
`cos A = (sin δ − sin φ sin h_g) / (cos φ cos h_g)`. Over six latitudes
(60° S to 64° N), five bodies (sun at ±ε and 0 with either limb, the Moon at
the major and minor standstills with mean parallax) and horizons of 0°, 0.7°
and 3.5°, `rise_set_azimuths` agrees to **2×10⁻⁴°**, including the cases
where the body never rises or never sets (both NaN). The equinox sunrise is
north of east by `asin(tan φ tan 0.84°)` (0.84° at 45°, 1.45° at 60°), and the
airless solstice amplitude is `asin(sin ε / cos φ)` to 10⁻⁴° from 66° S to
66° N. The epoch table in m5-analysis.md (June sunrise at 51.18° N: 49.25° in
2026 to 47.70° in 8000 BCE) is reproduced exactly.

**Dated sunrises against Skyfield.** Six sites (64° N, 51° N, 35° N, the
equator, 34° S, 60° S) on the 2026 equinoxes and solstices and two other days,
local day in a zone of round(lon/15) hours, upper limb (radius 695,700 km at
the day's distance), against three horizons: level (0°), raised (2°) and a
sinusoidal skyline `1.5° + sin(3A)`. Skyfield finds where the upper limb's
airless topocentric altitude crosses `h(A) − R(h(A))` with its own root
finder:

| | max | RMS |
|---|---|---|
| azimuth (216 events) | 0.0032° | 0.0008° |
| time | 0.97 s | 0.26 s |
| astropy Sun azimuth − Skyfield at the same instants | 3.4″ | |

The largest are at 64° N at the solstices, where the sun meets the horizon at
a grazing angle and 20 s of SPA interpolation matters most. Polar day and
night (no rise or no set) agree. **Verdict:** agrees.

**Moonrise and moonset.** Every moonrise and moonset in March 2025 (the major
standstill) at 51° N and 27.5° S, 119 events, from DE440's topocentric Moon
(upper limb, astronomical horizon). GeoTools' geometry, given the geocentric
declination and horizontal parallax at that instant, places them within
**0.0054°** (RMS 0.004°). With the parallax left out they would be up to
1.87° out, so the parallax is right in sign and size; the residual is the
oblate Earth (GeoTools takes the parallax along the vertical). **Verdict:**
agrees.

## Check Alignment

**Line azimuth.** An edge's true azimuth is its grid bearing plus the
convergence at its middle. At the west edge of MGA zone 56 (convergence 1.5°),
lines of 100 m, 5 km and 30 km every 15° agree with the geodesic azimuth at
their middle (pyproj.Geod) to 0.1″: the arc-to-chord correction GeoTools
leaves out is negligible at these lengths. **Verdict:** agrees.

**Declination.** `archaeo.declination` is the standard inversion (as in
Ruggles' and Hawkins' practice): true altitude `h − R(h)`, minus the limb,
plus the Moon's parallax when given, then
`sin δ = sin φ sin h_g + cos φ cos h_g cos A`. Feeding it each Skyfield
sunrise's azimuth and horizon altitude recovers DE440's declination of the sun
to 10″, and to 1.5″ when the sun's 8.8″ parallax is passed in. Check
Alignment computes it for a body's centre with no parallax (the sun or a
star), the usual convention. The round trip with the trace (existing test) is
within 0.002°. **Verdict:** agrees, with the documented convention.

## Bugs fixed

| Commit | Bug | Regression tests |
|---|---|---|
| 943e320 | Compute Events raised "Laskar's obliquity is only valid within 10,000 years" for the epochs 8000 BCE, 7999 BCE and 11994–11999 CE with the standstills on: the standstill nearest such an epoch lies up to 9.3 years beyond the obliquity's range. The standstill's ε now uses the polynomial unchecked (the epoch itself is still checked). | `tests/core/test_archaeo.py::test_events_at_the_ends_of_the_epoch_range` |
| e9bc479 | The horizon's fine pass could miss a near crest by up to 0.1° when, with curvature on, a farther range is hit by the next coarse ray up but appears lower than the crest. Every bracket within a coarse step of the best is now refined. | `tests/core/test_archaeo.py::test_fine_pass_finds_a_near_crest_below_a_farther_range`, `tests/blender/test_validation_archaeoastronomy.py::test_near_crest_below_a_farther_range` |

## Discrepancies left for a decision

1. **The obliquity at the ends of the epoch range.** Laskar's (1986)
   polynomial is 42″ from Vondrák et al. (2011) at 8000 BCE and 26″ at
   12000 CE (5.5″ at 3000 BCE), while `sun.mean_obliquity`, m5-analysis.md and
   the panel's trust note claim "a few arc seconds at the ends" and lunar
   extremes "to about 0.01°" (0.015° at 8000 BCE). The effect on an azimuth is
   about 0.02° at 8000 BCE, well under refraction's uncertainty.
   *Recommendation:* correct the stated accuracy now (e.g. "to 1″ from 1000 BCE
   to 6000 CE, 5″ at 3000 BCE and about 40″ at 8000 BCE against Vondrák et
   al. 2011"). Switching the epoch geometry to Vondrák's long-term ε_A (a
   closed-form series, no new dependency) would remove the gap but changes
   results users may have (≤ 0.012° in declination); worth doing before
   0.3.0 only if the far epochs matter to users.
2. **The sun's parallax in Check Alignment's declination** (8.8″, 0.0024°) is
   left out, as is customary. *Recommendation:* keep, and say so in the
   alignment notes; nothing in archaeoastronomy resolves 0.002°.
3. **The horizon's fine-step bias.** The skyline is the highest fine ray that
   hits, so it is 0 to 0.0016° low (median 0.0007°). *Recommendation:* leave
   it (it is inside the documented 0.002°); adding half a fine step would
   centre the error but makes a hard surface edge appear above itself.
4. **Fixed refraction conditions.** Astronomical refraction is Bennett's at
   1010 hPa and 10 °C, with no setting for pressure or temperature (the scale
   is (P/1010)(283/(273+T))). *Recommendation:* leave as is for 0.3.0; the
   panel already says that weather moves horizon refraction by arc minutes. A
   Pressure/Temperature pair would let a user bracket it.

## Limits users should know

- **Refraction near the horizon** is the largest error in any rise or set
  azimuth: several arc minutes with the weather (Bennett's formula assumes a
  standard atmosphere), more in inversions. On a 1° horizon at 51° N, 5′ of
  refraction moves a solstice sunrise by about 0.1°.
- The skyline is found to **0.002°** (biased low by up to 0.0016°) and the
  curvature model is within 0.0015° of exact geometry for skylines up to 5°;
  for very high, steep, near relief (above 10°) the curvature form is good
  to about 0.01°.
- **GRASS r.horizon** gives the same horizon statistically (median 0.0000°,
  0.015° RMS on distant skylines) but samples the raster differently, so on
  steep relief within a few kilometres expect differences of tenths of a
  degree between it and GeoTools' mesh. r.horizon has no eye height and
  reports negative horizons as 0.
- **Epoch declinations** use Laskar's obliquity (42″ from the modern series at
  8000 BCE, see decision 1); the Node Cycle standstills are the textbook
  `±(ε ± i)`, up to 0.15° from what the Moon actually reaches (use
  Ephemeris for that).
- **Times** of dated events and standstill dates carry ΔT: seconds now, about
  17 minutes of model difference at 3000 BCE and more before. Azimuths do not.
- **Check Alignment's declination** is for a body's centre with no parallax;
  for the Moon, apply its parallax (about 0.95°) yourself or compare
  azimuths with the events, which include it.
