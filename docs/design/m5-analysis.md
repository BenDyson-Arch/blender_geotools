# M5 design: the sun, sun hours, archaeoastronomy, coordinates, hydrology, contours and sections

Status: the GeoTools-code tools of M5 built so far (see
[ROADMAP.md](../ROADMAP.md#m5-analysis-library)): the sun, sun hours and
insolation, archaeoastronomy, coordinates and measurement, 3D surface
hydrology, and contours and sections out. This note covers what they compute,
how accurate it is, the north convention, and how the later M5 tools build on
them.

The scope rule decides the split. GeoTools computes only what needs the
georeference: where the sun is from this site at this moment, and where a
scene point is on Earth. Blender does everything else. Its Sun light casts the
shadows, Cycles and EEVEE render them, and Cycles casts the rays of sun hours.

## Layout

| Module | What it holds |
|---|---|
| `geotools/analysis/sun.py` | NREL SPA, vectorised over time; ΔT; time zones; the sun's direction and the Euler angles of a light. numpy only. |
| `geotools/analysis/_spa_tables.py` | The SPA periodic terms (the paper's Tables A4.2 and A4.3). |
| `geotools/analysis/site.py` | Scene points on Earth: E/N/RL, latitude and longitude, grid convergence, the combined scale factor, distances and areas. Uses pyproj. |
| `geotools/analysis/sun_hours.py` | Sun hours and insolation, bpy-free: time steps over a period, the sun's rays and their weights, merging nearby directions, sky directions, the reference sum, ray offsets, equinox and solstice moments. |
| `geotools/analysis/clearsky.py` | The Ineichen–Perez clear-sky model. |
| `geotools/analysis/moon.py` | Meeus' lunar theory (ch. 47), apparent and topocentric, vectorised; the mean node and its crossings. |
| `geotools/analysis/_moon_tables.py` | Meeus' Tables 47.A and 47.B. |
| `geotools/analysis/horizon.py` | The horizon from a point: ray fans, best hits with curvature and terrestrial refraction, Bennett's refraction, the `HorizonProfile`. |
| `geotools/analysis/archaeo.py` | Rise and set for a declination against a horizon, dated sunrises, lunar standstills, the declination inversion, alignments, calendar dates (Julian and Gregorian). |
| `geotools/blender/analysis.py` | Settings and results on `scene.geotools_analysis`, and the sun and measurement operators. |
| `geotools/blender/ray_bake.py` | The ray bake engine shared by sun hours, the horizon and the fast preset paths: the sample object, probe groups in blocks, the probe material, Cycles set-up, render-only occluders and the bake. |
| `geotools/blender/sun_hours.py` | Sun hours: the sun probe on the ray bake engine, the layers it writes, and the shadow study. |
| `geotools/analysis/preset_bake.py` | The fast (bake) paths of the horizon and cumulative viewshed presets, bpy-free: inputs, the bisection model, values from bake sums, the layer and its provenance. |
| `geotools/blender/preset_bake.py` | The fast paths' probes (bisection and observer groups), observer points through the library's own groups, and the run. |
| `geotools/blender/archaeo.py` | Archaeoastronomy settings (`scene.geotools_analysis.archaeo`), the horizon bake, events, sightlines and the alignment check. |
| `geotools/blender/analysis_ui.py` | The Analysis panel (after Linked Data, before Layers) and its Measure, Presets, Sun (with Sun Hours and Shadow Study inside it) and Archaeoastronomy (`archaeo_ui.py`) sub-panels. The tree is in [ui-structure.md](ui-structure.md). |
| `geotools/analysis/hydrology.py` | 3D surface hydrology, bpy-free: the face graph, drops, filling, routing, accumulation and depressions. |
| `geotools/analysis/slicing.py` | Contours and sections: plane-triangle cuts, levels in the height datum, chainage. |
| `geotools/analysis/polylines.py` | Segments chained into open and closed polylines (pointer doubling). |
| `geotools/blender/surface.py` | Evaluated meshes as one world-space array mesh, vertical rays through a Geometry Nodes Raycast, and line and point objects out. |
| `geotools/blender/hydrology.py` | Hydrology settings (`scene.geotools_hydrology`), the operator and its layers and objects. |
| `geotools/blender/contours.py` | Contour and section settings (`scene.geotools_contours`) and operators. |
| `geotools/blender/surface_ui.py` | The Hydrology, and Contours and Sections, sub-panels (after Presets, before Sun). |

`geotools/analysis` is bpy-free, like `geotools/core`, and
`tests/core/test_no_bpy.py` checks it. Its tests are `tests/core/test_sun.py`,
`tests/core/test_site.py`, `tests/core/test_sun_hours.py`,
`tests/core/test_archaeo.py`, `tests/core/test_hydrology.py`,
`tests/core/test_slicing.py` and `tests/core/test_preset_bake.py`. The
operators are tested in `tests/blender/test_analysis.py`,
`tests/blender/test_sun_hours.py`, `tests/blender/test_archaeo.py`,
`tests/blender/test_surface_tools.py` and `tests/blender/test_preset_bake.py`.
`scripts/bench_sun_hours.py` times sun hours on a synthetic town,
`scripts/bench_archaeo.py` the horizon on a synthetic valley,
`scripts/bench_hydrology.py` hydrology, contours and sections on a synthetic
site, and `scripts/bench_preset_bake.py` the fast preset paths on the preset
benchmark's terrain.

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
known (`crs_catalog.epsg_code`: a plain `EPSG:nnnn`, as the EPSG picker
stores, or the code a saved preset's PROJ string stood for before it is
migrated), and otherwise the definition as written. For MGA zone 56 that is
`EPSG:7856`, whose geodetic CRS is GDA2020, so latitudes and longitudes are
labelled GDA2020. A test checks a saved preset string and its code give
identical readings.

## Coordinates and measurement

The **Measure** sub-panel of Analysis.

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
  Simpson's rule: (k_A + 4 k_mid + k_B) / 6. In a CRS that is not conformal on
  its ellipsoid (Albers equal-area, and Web Mercator, which is conformal only
  on a sphere) k depends on direction. There each point's grid-to-ellipsoid
  map is the ellipsoid's metric diag(N cos φ, M) times the inverse of the
  projection's Jacobian (PROJ's partial derivatives), and k is taken in the
  line's direction; the true bearing is the direction of the mapped line at
  its middle, and areas use the map's determinant. Whether a CRS is conformal
  is tested once, at the centre of its area of use. Every conformal CRS
  rusty-tiles accepts keeps PROJ's own k and convergence.
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

## Sun hours, insolation and shadow studies

**What it computes.** Hours of direct sun per face, per face corner or per
texel, over a day, a date range (a season) or a calendar year, sampled at a
chosen step from real SPA positions. Optionally the clear-sky irradiation on
the surface in kWh/m². Both are M3 layers (FLOAT, units `h` and `kWh/m²`,
`Origin.ANALYSIS`, aggregation `AREA_MEAN`, value range from the values,
provenance below), so they display, publish as a variant and export as GLB
metadata like any other layer. One click on **Compute Sun Hours** in the Sun
Hours sub-panel; the date, time zone, site (scene origin or 3D cursor) and
refraction are the Sun panel's.

### Time and the sun

`sun_hours.period_steps` cuts the period from local midnight of the first day
to local midnight after the last (so a daylight-saving day is 23 or 25 hours
long, as it is) into steps evenly spaced in UTC, the last one shortened to end
on time. Each step counts for its whole length and is represented by its
midpoint. `sun_steps` keeps the steps with the sun's centre above the horizon:
apparent elevation (with refraction when the Sun panel says so) above 0°, and
above the horizon profile when one is given. Directions are in scene axes,
true azimuth minus the grid convergence at the site (`site.sun_site`), as for
the Sun light. Convergence changes by about 0.005° per km across a site,
which is negligible here.

**Step error.** The midpoint rule is wrong by up to half a step at each
sunrise, sunset and shadow edge, in either direction. Over a year these
partly cancel. Measured against 1-minute steps at 27.5° S, for points behind
an east-west wall 1 m high at 0.05 to 5 m (`test_time_step_error`), the
largest error in the year's total of about 4,400 hours is:

| Step | Largest error | Of the total |
|---|---|---|
| 5 min | 0.7 h | 0.02 % |
| 10 min | 2.7 h | 0.06 % |
| 30 min | 15 h | 0.35 % |
| 60 min | 28 h | 0.64 % |

For a single day the error is up to about one step per shadow edge crossed,
so a day uses 5 to 15 minutes (the default is 15).

### Merging nearby directions

Rays to directions closer than a fraction of a degree see almost the same
shadows. `group_directions` puts the sun directions on a sky grid of cells
`tolerance` degrees high and about as wide (fewer cells towards the zenith),
and gives each cell one ray: the time-weighted mean direction of its steps,
carrying their summed hours and energy. Totals are kept exactly, and the
largest angle between a step and its ray is reported. It is the user's
**Merge Within** (default 2°; 0 casts one ray per step).

A year at 30-minute steps has about 8,800 daytime steps. Against the analytic
wall above (`test_grouping_error_against_an_analytic_wall`; errors as a share
of the largest total):

| Merge within | Rays | Largest step-to-ray angle | Mean error | Largest error |
|---|---|---|---|---|
| 0 | 8,789 | 0 | 0 | 0 |
| 0.5° | 4,727 | 0.36° | 0.03 % | 0.14 % |
| 1° | 2,457 | 0.90° | 0.05 % | 0.26 % |
| 2° | 1,165 | 1.89° | 0.13 % | 0.55 % |
| 5° | 353 | 5.2° | 0.33 % | 1.3 % |

So merging within 2° costs less than the 30-minute step itself, and cuts the
rays by 7.5 times. Merging blurs shadow edges by up to about the tolerance
times the occluder's distance; it never moves the totals of an open surface.
Coarser steps with interpolation were not used: visibility is on or off, so
interpolating it between steps adds nothing a finer step or merging does not.

### Visibility: Cycles Raycast nodes in a bake

The scope rule says Blender casts the rays. Three ways were measured on this
machine (16 threads, shared with other work), on a 1-million-triangle terrain:

| Way | Throughput | Notes |
|---|---|---|
| (a) Geometry Nodes Raycast, one direction per evaluation, or many with Duplicate Elements or a Repeat zone | 7 to 14 Mrays/s | Blender's own BVH. About 0.7 µs per ray even at zero length: the cost is per call, not traversal. |
| (b) A Cycles bake per time step (Sun light, diffuse or shadow pass) | about 1.3 s of fixed cost per bake | 8,800 bakes for a year; even 1,000 merged directions is over 20 minutes on 1M triangles. |
| (c) Cycles `Raycast` shader nodes in one EMIT bake | 40 to 60 Mrays/s, after about 1 s of fixed cost | Many directions per bake; Cycles' BVH. |

So the engine is (c), for all three domains. `mathutils.bvhtree` (d) was not
considered for large meshes (the performance rule). The engine is
`geotools/blender/ray_bake.py`, shared with the horizon profile and the
[fast preset paths](#fast-bake-paths-for-presets); sun hours adds its probe
(the ray group below) and its layers.

- **The probe.** A shader node group holds one `Raycast` node per direction:
  for each sample it adds, per ray, `(hours, energy × cos i, 0)` when the ray
  is in front of the surface (cos i = normal · direction > 0) and hits
  nothing, and emits the sums with 1 in the third channel to mark the texel as
  baked. That is exactly `sun_hours.accumulate`, the tested reference. A ray
  behind the surface gets length 0, so Cycles does not traverse it. A bake
  takes up to 1,024 rays (`RAYS_PER_BAKE`), and numpy adds the bakes in
  binary64. Within a bake the sums are float32: 4,400 hours to 0.0005 h.
- **How it is built.** Three things shaped the shader, all found by
  measuring. (1) Building a node tree from Python, and setting a socket
  value in one, slows down with the tree's size: 512 rays as one tree of 9
  nodes each took minutes. So each ray is one node of a shared 8-node "ray"
  group, with its direction and weights as socket values, and rays come in
  blocks of 32 (`BLOCK`), so no tree has more than about 35 nodes. Loading
  1,024 rays takes 0.75 s. (2) The sums travel as one vector. Two float
  chains overflowed Cycles' 255-float shader stack beyond about 128 rays,
  silently: everything baked to 0. (3) A Raycast node whose Direction or
  Position is unlinked uses the normal or the shading point, whatever its
  value, so both are always linked. (4) The node does not normalise its
  Direction: Length, and the Hit Distance it reports, are in units of the
  direction's length (direction (0, 0, 10) with Length 1 meets a ceiling 5 m
  up, at distance 0.5). The fast viewshed relies on it.
- **TEXTURE layers** bake the probe on each source object's UVs (the layer's
  UV map, or the active render one), through a temporary material per object
  as `bake_texture` does, with the layer's resolution and margin
  (`EXTEND`). The position and normal are the shading point and the true
  (geometric) normal. Texels the bake does not reach are no-data. The
  UV-overlap check runs and warns. Images are saved as EXR next to the
  `.blend`, so the `.blend` must have been saved.
- **FACE and CORNER layers** bake a temporary sample object: one triangle per
  sample, on its own texel of its own UV map (`gt_probe`), the texel's centre
  inside it and no other. The sample's position and normal are face
  attributes the probe reads, in world space, float32. A face's sample is its
  centre with the face normal; a corner's is 10 % of the way from the corner
  to the face centre, also with the face normal, so it is inside the face
  rather than on a vertex shared with others. The triangles are 4 float32
  steps across, at their sample point, and horizontal. Cycles bakes only
  objects some ray type can see, so the sample object is visible to camera
  rays, which also makes it visible to the Raycast node; but rays start 16
  float32 steps off the surface, so a sample never hits its own triangle, and
  hits another's only by passing within a few float32 steps of that sample.
  Values are written to the original mesh (`layers.write_layer`), so an
  object whose modifiers change the topology is skipped with a note (use a
  TEXTURE layer, or apply the modifiers), as is a second object sharing a
  mesh.
- **Occluders** are everything that renders: the source objects (shown for
  the bake if hidden) and any other object visible to render rays. Hide an
  object from renders, or turn off its ray visibility, to leave it out. The
  occluding geometry is the evaluated mesh. Rays are 10⁷ scene units long.
- **Ray offset.** Origins move along the normal by the larger of 16 float32
  steps at the largest coordinate and a millionth of the samples' extent
  (`sun_hours.ray_offset`; 1 mm at 10 km from the origin, 1.4 mm for a 1.4 km
  site), or by the panel's **Ray Offset**. Cycles also skips the triangle a
  TEXTURE sample lies on. A surface closer than the offset to an occluder (a
  wall standing on a slab) may see past it at the joint; the corner sample of
  a face touching a wall can sit on the wall's own face.

**Accuracy against a known answer** (`test_wall_shades_half_the_plane`): at
the equator on an equinox, refraction off, a north-south wall of height H
shades a point at distance d to its west until the sun clears atan(H/d), so
it sees 6 + (90° − atan(H/d)) / 15° hours. At 5-minute steps every face of a
20 × 20 m plane is within 0.1 h of that, and the two halves are symmetrical
within 0.1 h. Corners (away from the wall's own face) are within 0.1 h, and
texels within 0.2 h (a texel's centre is not a face centre).

### Insolation

**Model.** Ineichen–Perez (Ineichen and Perez 2002), as pvlib implements it
(its default clear-sky model): Kasten–Young (1989) air mass on the apparent
zenith, corrected for the site's pressure from its height (standard
atmosphere), extraterrestrial irradiance 1366.1 W/m² over the SPA Earth–sun
distance squared, and one Linke turbidity for the whole period (default 3,
clean rural). It gives DNI, DHI and GHI in W/m²; the tests match pvlib 0.16.1
to 1 × 10⁻⁶ relative at zeniths 0° to 89°, turbidities 2, 3 and 5 and heights
0 and 1,000 m, and the Kasten–Young air mass at the horizon (37.92).

**On the surface.** Direct: DNI × time × cos i, for each unblocked sun ray in
front of the surface. Diffuse (optional, **Diffuse Sky**, default on): the
isotropic sky, DHI × time summed over the period, times the sample's
cosine-weighted sky view. That is a fixed set of sky rays (`sky_directions`,
a Fibonacci spiral, equal solid angle each, 64 by default) with weight
2 × ΣDHI·t / M, so their sum over the unblocked rays in front of a surface is
ΣDHI·t × (1/π)∫V cos i dω. It costs M more rays per sample whatever the
period, so it is cheap next to a year of sun rays. For an open horizontal
surface the sky rays' cosines sum to exactly 1, so the result is ΣGHI·t
(a Blender test checks this to 0.2 %). Ground-reflected light is not
included: it would add about albedo × GHI × (1 − cos tilt) / 2, about 5 % of
GHI on a wall at an albedo of 0.2.

**Accuracy, honestly.** This is a clear-sky upper bound, not a forecast:
cloud typically brings real annual totals to 50 to 80 % of it. The Linke
turbidity dominates the error: 1 unit changes DNI by about 10 % at moderate
air mass, and it varies by month. Validated against measured clear-sky data
(Ineichen 2006), the model's global irradiance is within about ±5 % with a good
turbidity, and its beam component less so. Diffuse is isotropic: it misses
the brightening around the sun and at the horizon (Perez's anisotropic
model), so it overstates diffuse on surfaces facing away from the sun and
understates it towards it.

### The far horizon

Terrain beyond the mesh is unknown, so the sun counts from 0° elevation. The
**Horizon** setting raises the horizon in every direction (for a valley, or
a city of unmodelled buildings). **Use Horizon Profile** takes the profile
computed under Archaeoastronomy instead: (true azimuth, apparent altitude)
pairs, interpolated linearly and periodically (`sun_hours.horizon_elevation`),
raised to **Horizon** where that is higher. Sun steps and sky rays below it
are dropped. The profile is seen from one point and applies to every sample,
so it suits terrain far away compared with the site; its **Ignore Within**
leaves out the near terrain, which the bake's own rays already meet. The
provenance records where the profile was taken from.

### Provenance

Each layer's provenance (`make_provenance("sun_hours", ...)`) records the
algorithm, the first and last day, the time zone, the UTC start and end, the
step, refraction, the site (latitude, longitude, height, geodetic CRS, origin
or cursor), the convergence and what +Y is, the horizon, the domain, the
number of samples, the ray offset, the steps (all, sun up, below the profile),
the possible sun hours, the sun and sky rays, the merge tolerance and the
largest step-to-ray angle, the direct-normal and diffuse-horizontal totals,
and for insolation the clear-sky model, turbidity, altitude and diffuse
treatment.

### Shadow studies

**Set Up Shadow Study** keys the Sun light, one frame per moment, at the
chosen times (default 9:00, 12:00 and 15:00 local) on the March equinox, June
solstice and December solstice (the September equinox is a tick away) of the
Sun panel's year, from **First Frame** on, with CONSTANT interpolation and a
timeline marker per frame ("21 Jun 09:00"). It sets the scene's frame range
to those frames. The light is off for a moment with the sun below the horizon.
The days are the real equinoxes and solstices (`season_moments`: where the
sun's apparent longitude crosses 0°, 90°, 180° and 270°, within a minute of
the published times), taken in the chosen time zone. It uses the Place Sun
light and keyframing; rendering the frames (or a viewport playblast) is
Blender's.

### Timings

`scripts/bench_sun_hours.py`, Blender 5.2.4 on a 16-thread Linux machine that
was shared with other jobs throughout (load average 10 to 14), so these are
pessimistic. The mesh is a 1 km synthetic town: a gentle swell with 12 to
24 m blocks every 40 m, all triangles, one object; 27.5° S, 21 June or a
year from it, CPU bakes.

| Mesh | Case | Samples × rays | Bakes | Time |
|---|---|---|---|---|
| 10M triangles | FACE, one day at 15 min | 10.0M × 41 | 1 | 30 s (7.7 s samples, 22 s bake) |
| 10M triangles | FACE, one day, insolation with 64 sky rays | 10.0M × 105 | 1 | 68 s * |
| 10M triangles | FACE, a year at 30 min, merged within 5° | 10.0M × 351 | 1 | 147 s * |
| 10M triangles | TEXTURE 1024², a year at 30 min, merged within 5° | 1.05M × 351 | 1 | 36 s |
| 10M triangles | TEXTURE 1024², a year at 30 min, merged within 2° | 1.05M × 1,168 | 2 | 84 s |
| 10M triangles | TEXTURE 2048², a year at 30 min, merged within 2° | 4.2M × 1,168 | 2 | 210 s |
| 1M triangles | FACE, one day at 15 min | 1.0M × 41 | 1 | 6 s * |
| 1M triangles | FACE, a year at 30 min, merged within 5° | 1.0M × 351 | 1 | 13 s * |

\* Measured before the sample object was built through attribute arrays,
which saved about 9 s of the 10M face runs' sample time (16.5 s to 7.7 s)
and about 1 s at 1M.

Rays run at 15 to 25 Mrays/s here once the bake is set up (the shader costs
about as much as the traversal). Each bake on the 10M-triangle scene has a
fixed cost of about 15 to 20 s: Cycles syncs the mesh and the sample object
and builds its BVH. The 10M runs peak at about 10 GB.

Against the roadmap's exit ("under a minute on a 10M-triangle mesh, or one
bake"): a day of face values on 10M triangles, and a year as a texture layer
in one bake, are under a minute. A year of per-face values on 10M faces is
not: it is 10M samples × several hundred rays, about 2.5 minutes at 5°. For
a year on a large mesh, use a texture layer (1024² or 2048²), merge within
5°, or both. Cost is samples × rays plus the fixed cost per bake, so the
panel's numbers predict a run: the result shows samples, rays and bakes.

## Archaeoastronomy

**What it computes.** From an observer point (the 3D cursor or a click, as
Measure), in the Archaeoastronomy sub-panel:

1. the **horizon profile**: the apparent altitude of the skyline at every
   azimuth (default every 0.5°), relative to true north, with the scene's
   meshes as the terrain;
2. **events**: where the sun rises and sets at the solstices and equinoxes,
   and the Moon at the major and minor standstills, for a chosen **epoch**
   (any year within 10,000 years of 2000), on the astronomical horizon and on
   the profile; and the sunrise and sunset times and azimuths on the Sun
   panel year's equinoxes and solstices and on the Sun panel date;
3. **sightlines**: a mesh of lines from the eye towards each event, which
   Export Vectors writes as GeoPackage or GeoJSON lines and the vector publish
   publishes;
4. an **alignment check**: the true azimuth of each selected edge (a wall or
   passage axis imported with M8, say), the nearest events in either direction
   along it, and the declination it points to.

Results are tables, not layers: they are copied to the clipboard at full
precision (tab-separated) and stored as JSON of binary64 floats.

### The horizon profile

**Rays.** For each azimuth the observer's eye (the point plus **Eye Height**,
default 1.6 m) casts a fan of rays in the vertical plane, from −10° to +60°
every 0.1°. A ray stops at its first hit, which is a terrain point the eye
can see; the skyline is the visible point that appears highest. A second pass
casts 64 rays inside the 0.1° above each azimuth's best hit, so the skyline is
found to about 0.002°. With curvature on, a farther range hit by the coarse
ray above a near crest can appear lower than the crest, so the second pass
also fills the 0.1° above any other coarse hit that appears within 0.1° of the
best (the ray just below the skyline always does; usually one or two brackets
per azimuth). At 0.5° azimuth steps that is 504,720 rays and then 46,080 or a
little more. A skyline above 60° (an observer under a roof) is marked closed; an
azimuth no ray hits is open and takes **Open Horizon** (default 0°).

**The engine is the sun hours one, turned around.** Sun hours bakes a few
hundred rays per sample over millions of samples; here there is one origin
and half a million directions. So each ray is one sample of the temporary
sample object (`ray_bake.build_sample_object`): one tiny triangle per ray
on its own texel, with the ray's direction as the sample's normal attribute.
A probe material with a single Cycles `Raycast` node casts from the eye along
it and emits (hit, hit distance, 1). One EMIT bake casts every ray of a pass
with Cycles' BVH, with no Python loop over rays or faces. The triangles are
parked a grid apart well below the terrain (4× its extent, at least 10 km),
where only a ray pointing almost straight down could meet one; the fan never
does. Everything that renders is terrain (the source objects are shown for
the bake); hide an object from renders to leave it out.

**Curvature and refraction.** A visible point at horizontal ground distance
d and height Δz above the eye appears at

    e = atan(Δz / d) − (1 − k) d / 2R

with R = 6,371 km and k the coefficient of terrestrial refraction (0.13
when **Refraction** is on, else 0); **Earth Curvature** off drops the second
term. At 10 km the term is 0.039° (k = 0.13), at 50 km 0.20°. Grid distances
are divided by the combined scale factor at the observer to give ground
metres. Curvature never makes a point hidden in the flat scene visible (its
drop grows with distance), so first hits suffice. The arc-to-chord
correction of a straight grid line (seconds of arc over tens of km) is
ignored.

**North.** Rays are cast at scene azimuths, true azimuth minus the
convergence at the observer (`site.sun_site`), so the profile is in true
azimuths. A test in MGA zone 56 checks the turn.

**The edge of the terrain.** Where the skyline point lies within 1 % of the
terrain's extent from the edge of its bounding box, the skyline is probably
the end of the mesh rather than the real horizon, and the panel says how many
azimuths that is. Real horizons are often 10 to 50 km away: extend the
terrain there (a coarse regional DEM around a detailed site is enough).

**Ignore Within** starts the rays that far from the eye, leaving out nearer
terrain: a far horizon for sun hours, whose bake meets the near terrain
itself.

**Accuracy against known answers** (`tests/blender/test_archaeo.py`): an
east-west ridge 8.4 m above the eye with its face 100 m north gives
atan(8.4 cos A / 100) at azimuth A; the profile matches it within 0.003° from
285° to 75°. A ridge 498.4 m above the eye at 20 km is within 0.003° of the
closed form with curvature off, with curvature (−0.0899°) and with
curvature and terrestrial refraction (−0.0782°). The edge of a 400 m slab is
flagged, and its dip below level, atan(1.6 / 200), matches.

### Refraction of the sky

A body is on the horizon when the true altitude of its chosen limb equals the
horizon's apparent altitude h less the refraction there:

    h_true = h − R(h),   R = cot(h + 7.31 / (h + 4.4)) arc minutes

(Bennett 1982, Meeus eq. 16.4, at 1010 hPa and 10 °C): 34.5′ at h = 0, 9.9′
at 5°. Below −1° (an observer above the land around) the value at −1° is
used. **Refraction** off sets R to 0 (and k to 0). Real refraction near the
horizon varies by several arc minutes with temperature and pressure, and by
much more in inversions; it, and the horizon itself, limit an archaeoastronomical
azimuth more than any ephemeris here. **Limb** chooses the upper limb (first
or last gleam, the default), the centre, or the lower limb (the disc just
clear).

### Rise and set for a declination

For epoch geometry the declination is fixed, and `archaeo.rise_set_azimuths`
traces the body's diurnal circle in steps of 0.01° of hour angle, with its
altitude and azimuth at each, and finds where the limb's true topocentric
altitude crosses the horizon's (h − R(h)) by linear interpolation. Rise is
the first crossing upwards after lower culmination and set the last one
downwards, so a jagged skyline that hides a body for a while gives the outer
pair. It is vectorised over the declinations and needs no root-finding, so it
cannot miss a crossing behind a notch. Without refraction, limb or parallax on
a flat horizon it gives cos A = sin δ / cos φ to 1×10⁻⁴°.

**Parallax.** The Moon's horizontal parallax π (about 0.95°) lowers it at the
horizon by p with sin p = sin π cos h′. It is in the trace (mean π =
0.9507°, or the ephemeris' π at the extreme) and in the inversion. The
Moon's semi-diameter is asin(0.2725 sin π); its small topocentric
augmentation (under 0.3′, nil at the horizon) is ignored. The sun's parallax
(8.8″) is SPA's.

**The inversion** (`archaeo.declination`): along an azimuth A with horizon
altitude h, the body's centre is at true altitude h − R(h) − limb, parallax
is added back to give the geocentric altitude h_g, and

    sin δ = sin φ sin h_g + cos φ cos h_g cos A.

A test round-trips 150 cases (latitudes to ±60°, declinations to ±28°,
horizons −0.5° to 6°, each limb, with and without lunar parallax) within
0.002°: the trace step.

### Epochs and the obliquity

The event geometry takes its declinations from the mean obliquity ε at the
epoch: ±ε at the solstices, 0 at the equinoxes, ±(ε + i) at a major
standstill and ±(ε − i) at a minor one, with i = 5.1454°. ε is Laskar's
(1986) polynomial, as in SPA (`sun.mean_obliquity`), fitted for 10,000 years
either side of J2000 and good to 0.01″ over 1,000 years and a few arc seconds
at the ends; epochs beyond that are refused. A test evaluates it
independently and against the IAU 2006 polynomial (within 0.5″ from 1000 to
3000 CE and 0.002° at 3000 BCE).

| Epoch | ε | June sunrise at 51.18° N, flat horizon, upper limb | December sunrise |
|---|---|---|---|
| 2026 CE | 23.4358° | 49.25° | 128.04° |
| 1 CE | 23.6947° | 48.75° | 128.53° |
| 2000 BCE | 23.9239° | 48.30° | 128.96° |
| 3000 BCE | 24.0209° | 48.11° | 129.14° |
| 8000 BCE | 24.2329° | 47.70° | 129.54° |

So 3000 BCE moves the solstice declination by 0.58° and the June sunrise at
that latitude by 1.1°. The epoch stands for the middle of its year; ε moves
by 0.5″ a year, so the day does not matter. Nutation (±9″ in ε) is left out
of the epoch geometry: it is real but not what a monument would mark. Years
are astronomical (0 is 1 BCE, −2999 is 3000 BCE) and shown both ways.

**How far back to trust it** (shown in the panel for the chosen epoch):

- Declinations: ε to arc seconds within the 10,000 years; lunar extremes to
  about 0.01°. Refraction outweighs both.
- Standstill dates: to hours near the present, days beyond 2,000 years from
  2000 and weeks beyond 4,000, as the Moon's mean elements drift.
- ΔT (Earth rotation, Espenak and Meeus 2006) is uncertain by minutes around
  1 CE and by tens of minutes to hours before 2000 BCE. It moves times of
  day, never the azimuths of the epoch geometry.
- Dated sunrises use SPA, validated from −2000 to 6000; the Sun panel takes
  years 1 to 9999 and says so beyond 6000.

### The Moon

`moon.py` implements Meeus' Astronomical Algorithms (2nd ed.) chapter 47: the
mean elements (eqs. 47.1 to 47.5), the 60 + 60 periodic terms of Tables 47.A
and 47.B with the eccentricity factor E, and the additive terms A1 to A3. The
nutation, true obliquity and sidereal time are SPA's (`sun.nutation`,
`sun.apparent_sidereal_time`), the topocentric place Meeus' chapter 40 (the
same as SPA's 3.12).

| Example 47.a (1992-04-12 0h TD) | Meeus | GeoTools |
|---|---|---|
| Σl, Σb, Σr | −1127527, −3229126, −16590875 | −1127527.03, −3229126.42, −16590875.18 |
| λ, β | 133.162655°, −3.229126° | 133.1626547°, −3.2291264° |
| Δ, π | 368409.7 km, 0.991990° | 368409.68 km, 0.9919901° |
| α, δ (apparent) | 134.688470°, 13.768368° | 134.6884685°, 13.7683666° |

Against PyEphem (an independent ELP-based ephemeris) at 200 random times from
1950 to 2050, at 27.5° S, the geocentric declination agrees to 1.5″ RMS (6″
at most) and the topocentric altitude and azimuth to 3″ RMS (13″ at most): the
theory's own accuracy. (PyEphem is not a dependency; the check was run once
by hand.)

### Standstills

The mean ascending node Ω (Meeus eq. 47.7) goes backwards round the ecliptic
in 18.61 years. At Ω = 0 the Moon's orbit is tilted furthest from the
equator, a major standstill, with monthly declination limits near ±(ε + i);
at Ω = 180°, a minor one, near ±(ε − i). `moon.node_crossings` solves Meeus'
polynomial for the last and next crossings around the epoch; the nearer is
the standstill used. Near the present that gives the major standstills of
June 2006 and January 2025 and the minor ones of October 2015 and May 2034.

The Moon does not sit exactly on ±(ε ± i): the inclination wobbles by about
±0.15° every 173 days. So `archaeo.standstill` also evaluates the ephemeris
hourly over a year either side of the node crossing and takes, of the monthly
limits, the most extreme (major) or least extreme (minor), refined by a
parabola. For 2025 that is +28.717° (7 March) and −28.726° (22 March),
against ε + i = 28.581°. **Standstills** chooses which declinations the
events use: **Node Cycle** (±(ε ± i) with the mean parallax, the usual
geometry) or **Ephemeris** (the extremes found, with the parallax then).
The panel shows both, with the dates (Julian calendar before 1582, Meeus'
chapter 7).

### Dated sunrises and sunsets

`archaeo.sun_rise_set` evaluates SPA's airless topocentric sun every 20 s
from local midnight to the next (4,320 positions, interpolated ephemeris) and
finds where the limb (the day's semi-diameter) crosses the horizon as above;
times are interpolated linearly. The days are the Sun panel year's equinoxes
and solstices (`sun_hours.season_moments`) taken in the Sun panel's time zone,
and the Sun panel date. Against the NOAA Solar Calculator's formulation
(written out in the test: centre at 90.833° zenith) the sunrise azimuth agrees
within 0.017° at six sites and dates from 64° N to 34° S (the test allows
0.1°). Times are on the astronomical and the real horizon; a test checks the
dated sunrise behind a 2° horizon against the declination geometry at the
day's declination (0.05°).

### Sightlines

**Create Sightlines** writes (or replaces) the mesh object "GeoTools
Sightlines" in a collection of that name: one loose edge per event with an
azimuth, from the eye to the skyline point at the profile sample nearest the
event (the ray that found it), or level to **Length** where there is no
profile or the azimuth is open. EDGE attributes: `fid`, `event`, `body`,
`phase`, `epoch`, `time`, `horizon` ("profile" or "astronomical"), and
`azimuth` (true), `altitude`, `declination` and `length` as FLOAT. Those four
are float32 attribute values (azimuths to about 3×10⁻⁵°), while the vertices
are scene positions; the copied table has every number in binary64. Each
edge has its own two vertices and its own `fid`, so M8's exporter writes one
LineString feature per event (`tests/blender/test_archaeo.py` exports them as
GeoJSON), and a Vectors build profile on the collection publishes them (also
tested). Loose edges do not render, so sightlines never occlude a later
horizon or sun hours bake.

### The alignment check

**Check Alignment** reads the active mesh's selected edges (every edge when
none are selected) with `foreach_get`, takes each edge's true bearing at its
middle (`site.line_bearings`: the grid bearing plus the convergence there),
and compares it, and its back bearing, with the best azimuth of every event
(on the profile when there is one), vectorised over lines and events. Each
line gets its true azimuth both ways, the horizon altitude along each (from
the profile, else 0), the declination each implies for a body's centre
without parallax (the inversion above), and the three nearest events with
the signed difference. A mesh with an EDGE `fid` (an M8 import) reports it.
Up to 2,000 lines are kept.

### Timings

`scripts/bench_archaeo.py`, Blender 5.2.4 on the same 16-thread Linux machine,
shared with other jobs (load average 9 to 10). The terrain is a 20 km square
height field (a valley floor with ridges to about 600 m at 5 to 9 km), one
object, all triangles; the observer at the centre; 0.5° azimuth steps; CPU.

| Mesh | Horizon (504,720 + 46,080 rays, two bakes) | Events at 3000 BCE (22, with standstills and dated sunrises) | Sightlines |
|---|---|---|---|
| 1M triangles | 1.6 s (first pass 1.3 s) | 0.65 s | < 0.01 s |
| 10M triangles | 2.9 s (first pass 1.7 s) | 0.43 s | < 0.01 s |

A bake here syncs the terrain and a sample object of half a million tiny
triangles, far fewer than sun hours' 10M-sample face layers, so its fixed cost
is about a second rather than 15 to 20. The events are pure numpy: the two
standstills' hourly ephemeris over two years each (35,000 positions) is most
of it.

### Decisions and deferrals

- **Cycles Raycast in one bake, one ray per texel.** The sun hours
  measurements (Cycles Raycast nodes 3 to 5 times faster per ray than
  Geometry Nodes, and one bake for many rays) carry over. With one origin the
  rays become the samples, so a whole fan is one bake with one Raycast node,
  rather than a probe of hundreds of chained nodes.
- **Two passes rather than bisection.** A coarse fan and one fine bracket per
  azimuth are two bakes; bisection would be one bake per halving.
- **Epoch geometry from declinations.** Python dates stop at year 1 and SPA
  at −2000, but the azimuth of a solstice or standstill depends only on its
  declination, which the obliquity sets. So epochs reach 10,000 years either
  side of 2000, and dated events stay with the Sun panel's calendar.
- **Deferred:** a local ΔT, pressure and temperature for the refraction
  (standard conditions now), stellar risings (they need precession and proper
  motion), the Moon's dated rises and sets on a chosen day, an animated
  sightline or horizon diagram, and grouping a polyline's edges into one
  feature axis in the alignment check (each edge is a line).

## 3D surface hydrology

**What it computes.** Where water would run over the surface, how much
surface drains through each face (flow accumulation, m²), where it ponds,
how deep, and each depression's spill level, pour point and volume. One click
on **Compute Hydrology** in the Hydrology sub-panel, over the source
collection or the selected objects, all objects at once (a roof's water
lands on the terrain object under it). Geometric only: no rainfall,
infiltration, evaporation or hydraulics, and the panel says so.

**Why it is code.** Stock Blender has no hydrology. A GIS does it on a 2.5D
DEM, which cannot hold a wall, an overhang or two levels at one place; that
is what this tool is for. Catchment-scale 2.5D hydrology stays in the GIS
(QGIS, WhiteboxTools), its results imported through M8. The planned
"flow-path tracing for quick looks" preset ([analysis-presets.md](../analysis-presets.md))
can step a point downhill in a Repeat zone, but accumulation needs every face
in a topological order and filling needs a global priority flood: neither is
a field evaluation, so neither fits Geometry Nodes. Where Blender does the
work, it does it: the drops are Blender's Geometry Nodes Raycast.

### The graph: faces, linked by shared edges

Each face is a node at its centre (the mean of its corners, as
`polygon.center`), linked to the faces it shares a mesh edge with. An edge of
three or more faces (a wall standing on a slab, both welded) links all of
them. Objects are joined only through drops, never by matching positions: the
mesh's own connectivity is the graph.

Faces rather than vertices, because:

- a face has one orientation. A face whose normal points down is an
  overhang's underside, and its water drips; a vertex where a wall meets the
  ground has no such answer. Water is on the side the normal points to, so
  normals must point out of solids (they do in photogrammetry and in
  Blender's primitives);
- the results are FACE layers (M3) with no interpolation, and contributing
  area is a plain sum of face areas;
- a triangle mesh has twice as many faces as vertices but about the same
  number of links (one per interior edge), so the cost is close.

The price is direction. A triangle has three neighbours, so a single flow
direction can only leave it three ways, and on a regular triangulation side
flow joins a valley at up to 45° off its true direction. In the V-valley test
(side slope 0.3, fall 0.05 along it) the thalweg has everything from 20
cells upstream (the valley's half-width) but not yet the side water that
started closer: `test_v_valley_concentrates_down_the_valley`. This is the
usual single-flow-direction bias (D8 has the same on a grid); a graph of
faces sharing a vertex (about 12 neighbours) would narrow it, at four times
the links (several GB more at 10 million faces).

### Steepest descent, walls and drops

A face drains to the neighbour with the largest drop over the 3D distance
between their centres (the sine of the slope along the step), so the water
on a wall goes down the wall: the face below it is a drop of its full height
over that height. Water leaves the surface graph straight down, through a
**drop**, in two cases:

- **a face pointing down** (normal z below −0.1, more than about 5.7° past
  vertical; `DRIP_NZ`) always drips, from just below its centre;
- **a face on an open edge** (an edge of one face: a roof's eaves, the foot
  of a wall not joined to the ground, the edge of the model) drops from just
  outside and above the middle of its lowest open edge, when none of its
  neighbours is lower. "Outside" is across the edge in the face's plane, seen
  in plan, or, for a vertical face, the way it faces.

Each drop is one vertical ray, cast by Blender: the faces under any drop's
start (a summed-area table of the starts' grid cells against each face's plan
box, which keeps every face a ray can hit) become a temporary target object
with their face ids, the starts a point object with a Geometry Nodes Raycast
modifier, evaluated once (`surface.cast_vertical`). The drop lands on the
first face below; failing that on the first face above, if it faces up (the
start is under the ground, as at the foot of a wall sunk into it); failing
that, the water leaves the model. Rays start `ray_offset` off the surface (as
for sun hours; 16 float32 steps or a millionth of the extent).

### Filling: priority-flood levels from the basins

A pit (a face with no lower neighbour and no drop out) holds water up to the
lowest level at which it spills. Those levels are what a priority-flood would
give, worked out on basins rather than faces, all vectorised:

1. Steepest descent on the surface as it is, with drops, gives each face a
   receiver; pointer jumping gives each face its basin (the root: a pit, or
   the outside).
2. Every link and drop between two basins is a pass, at the higher of its two
   ends; each pair of basins keeps its lowest pass.
3. The minimum spanning tree of this basin graph (Borůvka's algorithm: each
   component takes its cheapest edge, components merge by pointer jumping,
   O(log n) rounds) rooted at the outside gives each basin's spill level: the
   highest pass on its way out (breadth-first down the tree).
4. A face's filled height is the larger of its own and its basin's level.

A part of the surface that never reaches the outside (a closed object whose
drops all land on itself) drains at its lowest pit, with a count in the
stats.

### Routing, flats and accumulation

Flow is routed again over the filled surface: steepest descent (by filled
height), drops as above, and on a flat (a filled lake, or flat ground) to a
neighbour nearer the flat's exit, breadth-first from the faces that already
drain. Every face's water reaches an outlet; the receivers form a forest.
**Accumulation** is each face's area plus everything upstream: depths by
pointer jumping, then one `np.add.at` per depth, deepest first. The area is
the plan area of upward faces by default (**Area: Plan**: the rain catch;
walls and undersides add none), or the true area (**Surface**).

### Depressions

Ponded depth is filled minus actual height at the face centre. Connected
ponded faces are one depression (adjacent ponded faces always share a level).
Its **level** is the spill level; its **pour point** is the centre of the face
its main exit (the exit with the most water) drains to, at the spill level;
its **volume** is `sum(depth × signed plan area)`, so an underside below the
water (negative plan area) takes out the solid above it; its **area** is the
plan area of its upward faces; its **catchment** is the accumulation at the
main exit. Depressions shallower than **Minimum Depth** (default 1 cm) are
filled but not listed or labelled.

### Outputs

| Output | What |
|---|---|
| `flow_accumulation` | FACE, FLOAT, m², aggregation MAX, displayed on a **Log** scale (a new layer display option: colours spaced by decades, automatic range from the 2nd percentile of the positive values to the largest) |
| `ponded_depth` | FACE, FLOAT, m, MAX; 0 where dry |
| `depression` | FACE, CATEGORY, MODE: 0 None, then Depression 1, 2 ... by volume; past 1,000, one "Smaller" category |
| **Depressions** object | A vertex per depression at its pour point: `depression`, `fid`, `level` (RL), `volume`, `area`, `max_depth`, `faces`, `catchment` |
| **Flow Paths** object (optional) | Lines through the face centres (and drop landings) of faces draining at least **Channel From** m², for the **Networks** largest outlets, split at confluences and run downstream: `fid`, `accumulation`, `network`, `length` |

Both objects are in the `GeoTools Analysis` collection and export with
Export Vectors (points and lines with Z). Layers are written to the original
meshes; an object whose modifiers change its topology still takes part but
gets no values (a note says so), as for sun hours. The provenance records the
graph, area mode, minimum depth, drip threshold, ray offset, combined scale
factor, datum and objects.

**Ground units.** In a CRS project with pyproj, areas and volumes are divided
by the square of the combined scale factor at the surface's centre (one
factor: it changes by about 10⁻⁵ across a kilometre). Depths and levels are
heights and unchanged. Levels are reported in RL (plus offset A).

### Accuracy

- **Bowl** (`test_bowl_ponds_to_its_rim_with_the_analytic_volume`): a
  paraboloid z = 0.5 r² up to its rim at 2 m, flat beyond, ponds to exactly
  its rim. Its volume against the analytic πa²h/2:

  | Grid cell | Volume error | Area error |
  |---|---|---|
  | 0.150 m (a/13) | −0.004 % | +7.3 % |
  | 0.075 m | +0.010 % | +4.0 % |
  | 0.038 m | −0.0002 % | +2.1 % |
  | 0.019 m | +0.0003 % | +1.0 % |

  The volume is nearly exact because a face centre's height is the mean of
  its corners'; the area counts whole faces whose centre is under the water,
  so it overstates by about a rim's worth of faces.

- **Inclined plane:** every face drains to a lower one and the water leaves
  only over the low edge; each column's mean accumulation is within 15 % of
  its upslope strip (single-direction routing makes parallel streaks).

- **Two levels:** a step joined by a wall routes down the wall and on; an
  open roof sheet drops from its eaves onto the ground; a floating slab's
  underside drips onto the ground straight below; a wall sunk into the ground
  comes up through it; a closed box alone drains at its lowest point. In every
  case the accumulation leaving the model is the whole plan area, to 10⁻⁹.

- **Two pits** on a slope: the shallow one spills downhill into the deep
  one's lake, which spills out over the low edge.

**Limits.** Single flow direction (above). Heights are face centres, so a
face is wet or dry as a whole. A V-shaped channel running along a line of mesh edges
is a chain of shallow pits, (side slope − 2 × fall) × h/3 deep, since the two
lowest faces of each cell meet only at a vertex; filled, they pass their water
on, but on coarse meshes they are listed as depressions
([validation](../validation/hydrology.md#v-channels-along-mesh-edges)). Faces are not welded across objects: two
objects that touch exchange water only by drops, so a building closed at its
base and sunk into the terrain drips from its base, which is under the ground
(join walls to the ground, or leave the base open). Drops are vertical; water
does not cling to an underside. Rays are cast at float32 world positions.

### Timings

`scripts/bench_hydrology.py`, Blender 5.2.4 on a 16-thread Linux machine
shared with other work. A 1 km square terrain with 200 pits and 5 cm of noise
(about a fifth of it ponds in small hollows), a closed building sunk into it,
an open roof and a floating slab; LOCAL project; flow paths on.

| Faces | Hydrology | Read | Graph | Drops | Analysis | Layers and objects | Peak RSS |
|---|---|---|---|---|---|---|---|
| 1.0M | 1.8 s | 0.2 s | 0.3 s | 0.4 s | 0.8 s | 0.15 s | 0.8 GB |
| 10.0M | 20.4 s | 1.9 s | 2.6 s | 3.1 s | 11.5 s | 1.3 s | 5.2 GB |

The 10M run found 324,812 depressions (most a few centimetres deep, from the
noise) and cast 9,104 drops. Casting against the whole 10M-face mesh would
cost about 14 s, nearly all of it building Blender's ray-cast tree, so only the
faces under a drop go to the ray caster. The exit ("under a minute on 10M
triangles") holds with about 40 s to spare.

## Contours and sections

**What they make.** **Create Contours** cuts the surface (the source collection
or the selected objects, evaluated) at `base + k × interval` in the project's
height datum and writes a line object, **Contours**, with `elevation`,
`index` (k), `major` (k divisible by **Major Every**), `closed`, `length` and
`fid` on each line. **Create Sections** cuts it with the vertical plane of each
edge of a line object (drawn, or an imported M8 line layer; the chosen
**Section Line** or the active object, all its edges or the selected ones)
and writes **Sections**: 3D polylines of the surface with `chainage` on every
point (plan distance along the section line from its first point) and
`section`, `chainage_from`, `chainage_to`, `length` and `fid` on each piece.
A multi-level surface (an overhang, a bridge, a closed object) gives several
pieces, all kept. Both objects are in `GeoTools Analysis`.

**Chainage on export.** The Sections object's `geotools_measure` property
names `chainage`, so Export Vectors writes it as each point's M: a GeoPackage
LineString ZM (`gpkg_geometry_columns.m` = 1, ISO WKB type 3002), the GIS
convention for linear referencing, which GDAL reads as a measured line
string. GeoJSON positions hold no M (RFC 7946 advises against a fourth
element), so a GeoJSON piece carries a `chainage` property with one value per
position, shaped like the coordinates (GDAL reads it as a RealList). Any line
object with a `geotools_measure` property naming a numeric POINT attribute
exports the same way. The Vectors publish drops M before tiling: rusty-tiles
0.4 refuses measured GeoPackage geometry. GeoTools' own readers drop M.

**Why it is code.** Blender's Bisect cuts one plane at a time in Edit Mode
and changes the mesh; Geometry Nodes has no plane-mesh intersection; a
Boolean against slabs gives faces, not lines, and is slow at 10M faces; a
Raycast down along a line gives a 2.5D profile only (one hit, no walls, no
second level). One numpy cut serves both tools, and the levels must be
real-world heights.

**The cut** (`slicing.plane_cuts`). A vertex is above a plane when its signed
distance is ≥ the plane's, so a triangle crossing it has exactly two
crossing edges and gives one segment, and a vertex exactly on a level is no
special case: the level of the lowest vertices of a flat-bottomed surface
crosses nothing, and the highest is drawn along its edge. A crossing point is
named by its mesh edge (two vertex ids) and plane, or by the vertex when it
falls exactly on one, so the two triangles of an edge share it; faces with
more than three corners come in as Blender's loop triangles, whose diagonals
work the same way. Contours try every level a triangle spans (`searchsorted`
on the sorted levels), all levels in one pass.

**Chaining** (`polylines.chain`). Segments become half-edges; a half-edge
`u → v` continues with the other half-edge leaving `v` when `v` has exactly
two segments, so every run is a path or a cycle. Pointer doubling labels each
cycle with its smallest half-edge and cuts it there, then finds each path's
last half-edge and each half-edge's distance to it; of a run and its reverse
the one with the smaller last half-edge is kept. Closed lines repeat their
first point. A point where three or more segments meet ends the lines (a
saddle exactly on a level, a non-manifold edge). The line object's edges are
written line by line, end to end, so Export Vectors joins them into one line
string each.

**Heights.** A SOURCE_CRS project's levels are RL in the source's vertical
datum: local z + offset A, the binary64 offset, and **Interval** and **Base**
are text fields (binary64, like the project offsets). Points are placed at
exactly their level in the local frame and stored float32 in Blender; Export
Vectors writes `A + local z` in binary64, so the GeoPackage Z is the level to
the float32 step of the local z (8 mm at 100 km from the origin, far less near
it). The `elevation` attribute is float32: exact for levels on a binary
fraction (0.25, 0.5, 1, 5 m intervals), and Export Vectors now writes float32
values as their shortest decimal, so 123.4 reads as 123.4 in a GIS. LOCAL and
ANCHOR projects contour local z.

**Sections.** Each segment of the section line cuts only the triangles whose
plan box meets the segment's, and keeps what lies between its ends. A piece
running past an end is clipped there, inside its triangle; the clipped point
and a crossing that falls on the vertical line through a vertex of the
section line (within 10⁻⁹ of the segment's scale) are named by that vertex,
so the pieces of consecutive segments join into one polyline. Each line runs
the way most of its edges were drawn, so an imported line's chainage starts
at its first point; pieces run with increasing chainage.

**Accuracy** (`tests/core/test_slicing.py`):

| Case | Grid | Largest error |
|---|---|---|
| Cone z = 10 − r, contour radius | 0.4 m | 8.6 mm |
| | 0.2 m | 3.1 mm |
| | 0.1 m | 1.1 mm |
| | 0.05 m | 0.3 mm |
| Sphere r = 5 m, section 0.3 m off centre, distance from the circle | 32 × 16 UV sphere | 41 mm |
| | 64 × 32 | 11 mm |
| | 128 × 64 | 2.6 mm |

These are the facets' own departure from the true surface (a cut is exact on
the triangles); chainage is exact to binary64.

**Export and publish.** Export Vectors writes either object as a GeoPackage
table or GeoJSON of LineStrings with Z (absolute, in the project CRS) and
their attributes; the Blender tests read them back. A Vectors build profile
whose collection is `GeoTools Analysis` publishes them (the table of the
Contours object is `Contours_lines`).

**Timings** (same site as hydrology): 1 m contours of 1.0M faces in 0.7 s
(1,889 lines at 44 levels); of 10.0M faces in 5.7 s (47,403 lines). A
three-segment section across the 10.0M site in 7.0 s (most of it reading
the meshes and one signed-distance pass over every vertex per segment).

## Fast (bake) paths for presets

**What it computes.** The sky-view factor, positive and negative openness
and the cumulative viewshed presets cast many rays per element: 128 per
vertex for the horizon presets at their defaults, one per face per observer
for the viewshed. In Geometry Nodes that is 163 s, about 210 s and 229 s
(100 observers) on 10M triangles, past the M5 exit. Their fast paths cast the
same rays with the ray bake engine and store an ordinary layer (origin
ANALYSIS, the preset's domain, units and aggregation, provenance tool
`analysis_bake` naming the preset version it reproduces, the method, the
inputs and the run). The user picks the engine in Add Analysis (*Fast
(bake)*, or the "(Fast Bake)" menu entries); the live preset stays.
[analysis-presets.md](../analysis-presets.md#fast-bake-paths) has the
user-facing description, agreement and timings.

**The horizon presets, ray for ray.** One sample per vertex: its position and
vertex normal (Geometry Nodes' point domain), so values are spread onto the
vertex's corners as the preset stores them. The probe is a chain of one
*direction* group per azimuth (`Directions`, from +Y), each a chain of
`Steps` *step* groups. A step takes the bracket (lo, hi) as a vector,
computes its elevation (0 for the first step, else the midpoint), casts one
Raycast node of length Max Distance from the vertex moved Ray Offset along
its normal (against it for negative openness), and returns the new bracket:
a hit raises lo (from below, a miss does). The direction group then adds the
final midpoint and the sine of its positive part to the running sum, so the
bake emits (Σγ, Σ sin γ⁺, 1). Numpy turns that into the preset's formula.
`geotools.analysis.preset_bake.bisect_horizon` is the same bisection in numpy
over a hit function, which the tests run with exact ray-surface intersections.
Each step is about 15 shader nodes, so the shader's arithmetic, not the
traversal of 2 m rays, sets the rate: 14 to 20 Mrays/s at 10M.

**The cumulative viewshed.** One sample per face (centre, face normal). Each
observer is one 6-node group with its eye as a socket value: the Raycast
node's Direction is eye − (centre + Ray Offset × normal + Target Height),
unnormalised, with Length 1, so it covers exactly the segment to the eye
(pitfall 4 above); Length 0 skips a face that faces away (Target Height 0) or,
when not every eye is in range of every face, one beyond Max Distance in plan
(compared as float32 below the next float up, i.e. ≤). The count runs as one
float. A first version with a normalised direction, a length, a spare-slot
input and the plan test always on was 16 nodes per observer and spent most
of its time in the shader: with no ray cast at all it took 4.4 of its 5.1 s
at 1M. The blocks now hold exactly the observers left (no spare slots), and
the bake went from 59 s to 36–45 s at 10M with 100 observers. The observers
come from the library's own *Object Points* and *Drape* groups, evaluated
once on a temporary object, so they are the live preset's points.

**Samples parked.** The sample triangles sit in a grid below everything a
ray can reach (Max Distance, or the hull of the samples and the eyes), so no
ray meets one; sun hours keeps its samples at the sample points.

**Occluders.** Cycles' Raycast meets whatever renders, so for the bake every
object except the meshes the analysis is on (and the viewshed's Occluders
collection) is hidden from the render, and restored after. That matches the
live presets, which see only their own geometry and the Occluders, except
that several selected meshes shade each other here.

**Agreement and timings.** Identical to the live presets away from mesh
edges on the analytic tests; on a 1M-triangle terrain identical on 99.98 % of
interior corners, the rest where a bisection ray grazes the surface within
float32. On the trench the fast path also matches exact intersections near
the edge, where the live preset does not on 13 % of corners (not explained).
At 10M triangles (shared machine): SVF 37–40 s, openness 47–52 s, the
viewshed with 100 observers 49–65 s, of which about 16 s is fixed (8.4 s to
build the 10M-face sample object, 6.8 s for Cycles' sync and BVH, 3 s to drape
the observers).

## How the later M5 tools build on this

- **Sun hours, insolation and shadow studies** are built: see the next
  section.
- **Archaeoastronomy** is built: see its section above.
- **3D hydrology** and **contours and sections** are built: see their
  sections below.
- **Preset library.** Geometry Nodes presets (slope, aspect, viewshed and so
  on) run in Blender's own units. Where a preset needs north (aspect, sun
  direction inputs), the "Add Analysis" operator fills a group input with the
  site's convergence from `site.sun_site`, so aspects are true rather than grid
  bearings when asked. Where a preset reports lengths or areas, it can take the
  CSF as an input.

## Decisions

- **Sun hours cast rays with Cycles, not Geometry Nodes.** The obvious
  candidates were a Geometry Nodes Raycast for face and corner layers and a
  Cycles bake per time step for textures. Measured, Cycles Raycast shader nodes in one
  EMIT bake are 3 to 5 times faster per ray than Geometry Nodes and need one
  bake per 1,024 directions rather than one per step, so one engine serves
  all three domains; face and corner values bake through the sample object.
- **Merge within 2° by default.** It keeps a year to about 1,200 rays at an
  error below the 30-minute step's own, and is 0 for exactness.
- **Ineichen–Perez, isotropic diffuse, no ground reflection.** The standard
  clear-sky model with one input most users can estimate (turbidity), checked
  against pvlib. Deferred: monthly turbidity, the Perez anisotropic sky,
  ground-reflected light, and measured or typical-year weather (TMY) files,
  which would replace the clear sky with real totals.
- **Fast preset paths in the ray bake, not a raster scan.** The horizon
  presets could have run on a height raster (as RVT does), but that is 2.5D
  and a second code path; the bake casts the preset's own rays on the mesh,
  so walls and overhangs behave as in the live preset, and it reuses the sun
  hours engine (now `ray_bake.py`). Deferred: the same for the local relief
  model (67 s at 10M), a texture-domain fast path, and building the sample
  object from images rather than 10M tiny triangles (most of the fixed cost).
- **Deferred for sun hours:** cancelling a run between bakes (each bake is
  Blender's and blocks the UI), a GPU device (bakes use the CPU, as the
  texture bake does), and faster sample building and bake set-up for
  10M-triangle face layers (see Timings). The horizon profile is now
  archaeoastronomy's.
- **SPA rather than NOAA.** The NOAA spreadsheet formulation (Meeus' low
  precision method) is good to about 0.01° near the present. SPA is exact to
  the paper and covers the dates archaeoastronomy needs, and with the
  interpolated ephemeris it is fast enough.
- **No bundled `tzdata`.** A fixed UTC offset is the fallback where the
  database is missing (Blender on Windows), with a clear note. Adding `tzdata`
  as a pinned wheel can be revisited if users ask for named zones on Windows.
- **Below the horizon, the light is off.** A Blender Sun light below the
  horizon would light the scene from underneath, so its strength is 0 then
  (keyframed when animating, with constant interpolation, so it switches at
  the first key with the sun up or down rather than fading between keys).
- **Readings are snapshots.** Results are stored with the placement they were
  made with. When the project changes, the panel says so and offers Read Again.
- **The sun light is Blender's.** GeoTools creates or reuses one Sun light
  ("GeoTools Sun", or a chosen one) and sets only its rotation and strength,
  plus the sun's 0.533° angular diameter when it creates the light. Animating
  replaces only GeoTools' own actions on it.
- **Hydrology on faces, not vertices.** Faces have an orientation (an
  underside drips) and are what FACE layers hold; the cost is three flow
  directions per face, documented above. Single flow direction, not a
  multiple-flow split: accumulation stays a tree, so it is vectorised.
- **Filling on basins.** Priority-flood levels come from a spanning tree of
  the basin graph, not a heap over faces, so there is no Python loop over
  faces; the cost is one pass per tree depth for the levels and per flat
  width for the flat routing, both small.
- **Drops through Blender's Raycast.** Geometry Nodes casts them in one
  evaluation, against only the faces under some drop; GeoTools does not ship
  its own ray caster. Deferred: water clinging to undersides, and welding
  objects that touch without sharing edges.
- **Contours and sections share one cut**, in numpy, chained by pointer
  doubling. Deferred: smoothing or simplifying contour lines (Blender's or a
  GIS's tools do that), and drawing a 2D profile view of a section (the
  `chainage` attribute, exported as M, and Z are there for a GIS or a
  Geometry Nodes setup).
