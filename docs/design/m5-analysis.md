# M5 design: the sun, and coordinates and measurement

Status: the first two GeoTools-code tools of M5 (see
[ROADMAP.md](../ROADMAP.md#m5-analysis-library)). This note covers what they
compute, how accurate it is, the north convention, and how the later M5 tools
build on them.

The scope rule decides the split. GeoTools computes only what needs the
georeference: where the sun is from this site at this moment, and where a
scene point is on Earth. Blender does everything else. Its Sun light casts the
shadows, Cycles and EEVEE render them, and its bakes will compute sun hours.

## Layout

| Module | What it holds |
|---|---|
| `geotools/analysis/sun.py` | NREL SPA, vectorised over time; ΔT; time zones; the sun's direction and the Euler angles of a light. numpy only. |
| `geotools/analysis/_spa_tables.py` | The SPA periodic terms (the paper's Tables A4.2 and A4.3). |
| `geotools/analysis/site.py` | Scene points on Earth: E/N/RL, latitude and longitude, grid convergence, the combined scale factor, distances and areas. Uses pyproj. |
| `geotools/blender/analysis.py` | Settings and results on `scene.geotools_analysis`, and the operators. |
| `geotools/blender/analysis_ui.py` | The Analysis panel and its Sun and Coordinates sub-panels, before About. |

`geotools/analysis` is bpy-free, like `geotools/core`, and
`tests/core/test_no_bpy.py` checks it. Its tests are `tests/core/test_sun.py`
and `tests/core/test_site.py`. The operators are tested in
`tests/blender/test_analysis.py`.

## Sun position

**Algorithm.** NREL's Solar Position Algorithm (Reda and Andreas,
NREL/TP-560-34302, 2003, revised 2008), implemented step by step from the
paper. The paper states an uncertainty of ±0.0003° from −2000 to 6000. Against
its worked example (2003-10-17 12:30:30 at UTC−7, 39.742476 N, 105.1786 W,
1830.14 m, 820 hPa, 11 °C, ΔT 67 s), the result is:

| Quantity | Paper | GeoTools | Difference |
|---|---|---|---|
| Topocentric zenith | 50.11162° | 50.111622° | 2×10⁻⁶° |
| Topocentric azimuth | 194.34024° | 194.340241° | 5×10⁻⁷° |

The intermediate values (Julian day, λ, β, R, Δψ, ε, δ′, e₀) match the paper's
Table A5.2 to its printed digits. Over a year at one-minute steps the result
agrees with pvlib-python's independent SPA to 1×10⁻⁶°. The brief's target was
about 0.01°, so the algorithm itself is not the limit.

**Speed.** SPA has two parts. The geocentric part (the Earth's heliocentric
position and the nutation, about 260 periodic terms) is costly, but it changes
slowly: its fastest term has a period of 13.6 days. The topocentric part (the
Earth's rotation, parallax, refraction and azimuth) is cheap. With many closely
spaced times, `solar_position_jd` evaluates the geocentric part exactly on an
hourly grid of nodes and interpolates it linearly. That stays within
about 1×10⁻⁷° of evaluating it at every time (tested to 5×10⁻⁷°), and the
Earth's rotation is still computed at every time. Sparse times are evaluated
directly. A year at one-minute steps (525,600 positions) takes about 0.5 s, and
a day of keyframes takes about 2 ms.

**Error budget, in practice.**

- Times are taken as UTC, with UT1 − UTC (at most 0.9 s) set to zero. That is
  up to 0.004° of hour angle, the largest term today.
- ΔT (TT − UT) comes from the Espenak and Meeus (2006) polynomials unless one
  is given. A few seconds of error moves the sun by about 0.00005° today. For
  ancient dates ΔT is uncertain by minutes to hours. That moves the sun's hour
  angle directly, so archaeoastronomy must pass its own ΔT and state it.
- Refraction uses the paper's formula, at standard pressure (1013.25 hPa) and
  12 °C, and only when the sun is above the paper's switch near the horizon.
  Real refraction at the horizon varies by a few tenths of a degree with the
  weather. It is a toggle in the UI.
- Height enters only through the parallax term (8.8″ at most), so
  ellipsoidal height and height above sea level give the same result.

**Time zones.** Named zones use `zoneinfo` IANA names, so daylight saving is
handled. Python on Windows has no system time zone database. Blender's bundled
Python does not ship the `tzdata` package, and GeoTools does not bundle it:
that would be a fourth wheel pin for a convenience. So:

- `"UTC"` always works, without a database.
- Any other name raises `TimeZoneUnavailable`, with `no_database` set when the
  database is missing. The Sun panel then shows a note in red: use a UTC
  offset.
- The UTC Offset mode is a fixed offset in hours, rounded to the minute, with
  no daylight saving. The panel says so.
- A wall-clock time that a daylight-saving change skips or repeats is resolved
  as `zoneinfo` does with `fold=0`, with a note.

Keyframes over a day are spaced evenly in UTC from the start time, so a
daylight-saving change in the middle of the day does not distort them.

## North and convergence

The scene's +Y axis is "north" for the scene. What north it is depends on the
placement mode:

| Placement | +Y is | `convergence` (true north to +Y, clockwise) |
|---|---|---|
| SOURCE_CRS, projected | grid north of the project CRS | PROJ's `meridian_convergence` at the site |
| SOURCE_CRS, geographic | true north (degrees are not a grid) | 0 |
| ANCHOR | the anchor's north turned by the heading | −heading |
| LOCAL | taken as true north | 0 |

The sign is PROJ's `meridian_convergence`: the angle from true north to grid
north, clockwise positive. East of a transverse Mercator central meridian it
is positive in the northern hemisphere and negative in the southern. At the
west edge of MGA zone 56 (150° E, 30° S) it is +1.50°, so grid north lies 1.5°
east of true north. A tested identity gives the same sign independently: the
grid bearing of a short step due north is minus the convergence. So:

```
grid bearing  = true bearing − convergence
true bearing  = grid bearing + convergence
```

The sun's true azimuth from SPA becomes a scene azimuth (clockwise from +Y) by
subtracting the convergence. The light is then turned so its −Z points away
from the sun: Euler XYZ = (90° − elevation, 0, 180° − scene azimuth). The Z
angle is unwrapped across keyframes, so the light never spins the long way
round. The panel shows the true azimuth, the elevation, the convergence
applied, and the resulting azimuth from +Y.

Convergence changes across a site by about 0.005° per kilometre of easting at
30° S. The sun's site is the scene origin (the project offset) or the 3D
cursor, chosen in the panel.

**The CRS lookup.** `site.crs_input(definition)` is the only place the
project's CRS text becomes pyproj input. It passes the EPSG code when one is
known (`crs_presets.epsg_code`: a preset's code, or a plain `EPSG:nnnn`), and
otherwise the definition as written. For the MGA presets that means
`EPSG:7856`, whose geodetic CRS is GDA2020, so latitudes and longitudes are
labelled GDA2020. The projection is the same as the preset's PROJ string, and
a test checks the two give identical readings. When the presets give way to
plain EPSG codes from a picker, nothing else changes.

## Coordinates and measurement

**Readout.** A Blender world position (from the 3D cursor, or a click on a
surface, which also moves the cursor) is read as its float32 value. The
binary64 project offsets are then added in binary64:

- SOURCE_CRS: E = x + offset E, N = y + offset N, RL = z + offset A (the
  source's vertical datum). The ellipsoidal height is RL + height offset,
  which is how rusty-tiles places the mesh. Latitude and longitude come from
  pyproj's inverse projection, in the CRS's geodetic datum.
- ANCHOR: E/N/U in the anchor's tangent plane (scene offset added, then
  turned by the heading, as rusty-tiles builds the frame). Latitude, longitude
  and ellipsoidal height come from that point in ECEF, through pyproj
  (EPSG:4978 to EPSG:4979). Without pyproj, E/N/U still read and the panel
  notes that latitude and longitude need it.
- LOCAL: local X/Y/Z only.

**Precision.** The local position itself is float32: 24 significant bits.
Every reading carries `resolution`, the float32 spacing at that position. That
is 0.06 mm at 1 km from the scene origin, 1 mm at 10 km and 8 mm at 100 km.
E, N and RL are exactly offset + float32(local) in binary64, and no more
precise than the local position. That is why scenes keep their offsets small.
Results are stored as JSON of Python floats (shortest `repr`, which reads back
exactly) in `StringProperty`s, never `FloatProperty`. Copy puts every value on
the clipboard at full precision, as tab-separated lines.

**Distance.** The grid distance is the plan distance in the scene. The ground
distance is the grid distance divided by the combined scale factor:

```
CSF = k × R / (R + h)
```

- k is the grid scale factor from `get_factors`, averaged along the line with
  Simpson's rule: (k_A + 4 k_mid + k_B) / 6.
- R is the Gaussian mean radius √(MN) of the CRS's ellipsoid at the middle
  latitude.
- h is the mean ellipsoidal height of the ends.

The panel also shows the height difference and the slope distance (the ground
horizontal distance combined with the height difference). It shows the grid
bearing from +Y and the true bearing, which adds the convergence at the middle
of the line. A test compares the ground distance with pyproj's geodesic
ellipsoid distance raised to the line's height, and they agree to 1×10⁻⁷ (0.1
mm per km). In ANCHOR projects local metres are true metres, so the CSF is 1.
A geographic project CRS (degrees) cannot measure, and says so.

If the height offset is left at 0 for heights above sea level, h is the RL
rather than the ellipsoidal height. That changes the height factor by N/R
(about 6 ppm for a 40 m geoid separation).

**Area.** The area of the selected faces of the active mesh is read with
`foreach_get` (loop triangles, polygon selection, positions), transformed to
world space in numpy, and passed to `site.face_areas`. That computes one CSF per
triangle at its centroid, vectorised (about 2.5 s for 2 million triangles, so
about 13 s for 10 million). It reports four areas:

- grid plan area and grid surface area;
- ground plan area: the plan area / CSF² per triangle;
- ground surface area: each triangle's horizontal extent / CSF, with the
  vertical unchanged.

## How the later M5 tools build on this

- **Sun hours and insolation.** `sun.solar_position` over a year of time steps
  (cheap, see above) gives directions in scene axes through `sun.direction` and
  the same convergence. Each time step is a Blender Cycles bake (a shadow
  catcher or a light-only bake with the Sun light placed at that direction),
  summed in numpy into a TEXTURE or FACE layer (M3) with
  `Origin.ANALYSIS` and provenance (site, dates, step, convergence). A
  clear-sky model (for example Ineichen–Perez, or the ASHRAE model) can use the
  SPA zenith and Earth-sun distance (`SunPosition.distance_au`) that the
  result already carries. Solstice and equinox studies are this tool at a few
  fixed moments.
- **Archaeoastronomy.** Sunrise and sunset azimuths come from the same SPA,
  searched for elevation crossings (the airless elevation with an explicit
  refraction and the sun's semi-diameter, `SunPosition.angular_diameter`).
  `solar_position_jd` takes Julian days, so dates before year 1 work. A
  user-given ΔT is essential there. Lunar standstills need a lunar ephemeris
  (Meeus chapter 47), which can share the nutation and sidereal time code of
  `sun.py`. The horizon profile from a point is a numpy ray-march over the mesh
  (or a Blender ray cast batch), with directions turned by `site.sun_site`'s
  convergence. Alignment checks compare true bearings from `site.measure`.
- **3D hydrology.** This needs no georeference beyond the scene's up axis. It
  is a numpy tool on mesh connectivity (pointer jumping), writing FACE layers.
  Its results report areas and volumes in ground units through
  `site.face_areas` and the CSF.
- **Coordinates out (contours and sections).** Lines are written in project
  CRS coordinates as offset + local in binary64, the same addition as
  `site.read_point`, through M8's GeoPackage writer.
- **Preset library.** Geometry Nodes presets (slope, aspect, viewshed and so
  on) run in Blender's own units. Where a preset needs north (aspect, sun
  direction inputs), the "Add Analysis" operator fills a group input with the
  site's convergence from `site.sun_site`, so aspects are true rather than grid
  bearings when asked. Where a preset reports lengths or areas, it can take the
  CSF as an input.

## Decisions

- **SPA rather than NOAA.** The NOAA spreadsheet formulation (Meeus' low
  precision method) is good to about 0.01° near the present. SPA is exact to
  the paper and covers the dates archaeoastronomy needs, and with the
  interpolated ephemeris it is fast enough.
- **No bundled `tzdata`.** A fixed UTC offset is the fallback where the
  database is missing (Blender on Windows), with a clear note. Adding `tzdata`
  as a pinned wheel can be revisited if users ask for named zones on Windows.
- **Below the horizon, the light is off.** A Blender Sun light below the
  horizon would light the scene from underneath, so its strength is 0 then
  (keyframed when animating).
- **Readings are snapshots.** Results are stored with the placement they were
  made with. When the project changes, the panel says so and offers Read Again.
- **The sun light is Blender's.** GeoTools creates or reuses one Sun light
  ("GeoTools Sun", or a chosen one) and sets only its rotation and strength,
  plus the sun's 0.533° angular diameter when it creates the light. Animating
  replaces only GeoTools' own actions on it.
