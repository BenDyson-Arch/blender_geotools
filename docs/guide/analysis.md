# Analysis

The **Analysis** panel holds tools that need the project's georeference, or
that neither a GIS nor stock Blender does well. Everything else is a Geometry
Nodes preset you can open and edit. Results are [layers](layers.md), so they
display, publish as variants and export as metadata. Lines and points are mesh
objects that **Export Vectors** writes as GeoPackage or GeoJSON.

The panel sits between Linked Data and Layers, and is closed by default. Its
sub-panels, top to bottom (Measure and Presets start open):

| Sub-panel | What it does |
|---|---|
| [Measure](#measure) | E, N, height and latitude and longitude of a point; distances and areas. |
| [Presets](#presets) | Geometry Nodes analyses: slope, aspect, visibility, volumes and more. |
| [Least-Cost Routes](#least-cost-routes) | The quickest route over the 3D surface, uphill and downhill costed apart, and the travel time to every face. |
| [Hydrology](#hydrology) | Where water runs and ponds on the 3D surface. |
| [Contours and Sections](#contours-and-sections) | Contour and section lines. |
| [Sun](#sun) | Places or animates a Blender Sun light for a date, time and place. |
| &nbsp;&nbsp;[Sun Hours](#sun-hours) | Inside Sun: hours of direct sun, and optionally clear-sky insolation, into a layer. |
| &nbsp;&nbsp;[Shadow Study](#shadow-study) | Inside Sun: keys the Sun at the equinoxes and solstices, ready to render. |
| [Archaeoastronomy](#archaeoastronomy) | Horizon profile, sunrise, sunset and lunar standstill events, sightlines and alignment checks. |

**What the tools need:**

- In a **CRS** project, the bundled pyproj. Without it the panel says so, and
  the tools that relate the scene to the Earth stop.
- In an **Anchor** project, the anchor.
- In a **Local** project, the Sun sub-panel asks for a longitude and latitude,
  and takes +Y as true north. Points have local coordinates only.

The tools work on the meshes you select, in any collection, in Object Mode.
With nothing selected, the button is greyed out and says "Select the meshes to
analyse". Sun Hours, Least-Cost Routes, Hydrology, and Contours and Sections
have a **Surface** choice: **Selected Objects** (the default) or the whole
**Source Collection**. Only the source collection is published: a layer on a
mesh outside it works and shows, and the report says it is not published (see
[Source](projects.md#source)). If no source collection is set, a tool sets it
to the collection of the mesh it ran on, and the report says so.

Hydrology, Sun Hours, Sky-View Factor and Openness read each face's normal as
pointing out of the surface. They check the normals first and warn in their
report when faces are wound against their neighbours or most of the area
faces down; they run anyway. See [Normals](publish.md#normals).

### Bake device

Sun Hours, the horizon profile, the Fast Bake presets and
[texture bakes](layers.md#texture-layers-and-the-bake-sub-panel) cast their
rays with Cycles. **Bake Device** in the GeoTools preferences (**Edit >
Preferences > Add-ons**, GeoTools) chooses where:

- **Auto** (the default): the GPU when Cycles has one enabled under **Edit >
  Preferences > System > Cycles Render Devices** (CUDA, OptiX, HIP, Metal or
  oneAPI), otherwise the CPU. With **None** chosen there, bakes use the CPU.
- **GPU**: the same, with a warning when it falls back to the CPU.
- **CPU**: always the CPU.

GeoTools only reads Cycles' device settings; it never changes them. The
scene's own **Device** (Render Properties) is put back after each bake. The
report says which device a bake used, and Sun Hours shows it in its result.
Results agree between devices: the same rays hit, and sums differ at most in
the last digits. The GPU speeds up casting rays; reading the mesh and saving
images take as long as before, so a texture bake gains little. The first GPU bake after installing Blender or a new driver
can take a minute or two while Cycles prepares its GPU code; later ones start
at once.

## Sun

Places a Blender Sun light where the sun is, for a date, time and place.
Shadows are then rendered or baked by Blender.

1. Set the date and time (year, month, day; hour, minute, second).
2. Choose the **Time**:
   - **Time Zone**: an IANA name such as `Australia/Brisbane`, with daylight
     saving. Named zones work on every platform: GeoTools bundles the time
     zone database;
   - **UTC Offset**: a fixed number of **Hours**, with no daylight saving.
3. Choose the **Site**: the **Scene Origin** (the project offset) or the
   **3D Cursor**.
4. Choose a **Light**, or leave it empty to create (or reuse) one named
   `GeoTools Sun`.
5. Press **Place Sun**.

The result shows the time in UTC, the sun's **Azimuth** from true north, its
**Elevation**, the grid **Convergence**, and the azimuth in the scene (**Scene
Az.**, from +Y). In a CRS project the sun is turned from true north to grid
north by the convergence, up to about 1.5° at the edge of a UTM or MGA zone.
In a CRS that is not conformal (Albers equal-area, Web Mercator) the turn
depends on the direction, so GeoTools turns the sun through the grid's own
map at the site (the one Measure uses), and the panel says so; the
**Convergence** shown is then the turn along the meridian only.
**Refraction** (on) lifts the sun near the horizon as the atmosphere does,
at the standard atmosphere's pressure for the site's height (1013.25 hPa at
sea level, 899 hPa at 1,000 m) and 12 °C: on high ground the sun rises a
little later and sets a little earlier (4 minutes off a June day at 51° N at
4,000 m). The height is the site's ellipsoidal height; a Local project has
none, so it refracts at sea level.

**Over a Day** keyframes the light: set the **Hours** (from and to), the **Step
(min)**, the **First Frame** and the **Frame Step**, then press **Animate
Day**. The light is switched off while the sun is below the horizon: its
strength switches at the first key with the sun up (or down), and the frames
between two keys keep the earlier key's strength, as in a Shadow Study. Its
direction moves smoothly between keys.

The sun's position is NREL's Solar Position Algorithm, within 0.00001° of the
paper's own example.

## Sun Hours

Counts the hours of direct sun on each face, face corner or texel over a day,
a season or a year, with the shadows of the scene's meshes. Optionally it
also computes clear-sky insolation in kWh/m².

The date, time zone, site and refraction come from the Sun sub-panel.

1. Choose the **Surface**, the meshes that get the values: the **Selected
   Objects** or the **Source Collection**. Every mesh that renders casts
   shadows either way.
2. Choose the **Period**: **Day** (the Sun date), **Dates** (from the Sun date
   to an end date) or **Year** (the Sun date's year).
3. Set the **Step (min)**: how often the sun's position is taken. Only steps
   with the sun up count.
4. Choose where the values go: **Values** **Face**, **Corner** or **Texture**
   (with a **Resolution**), and the **Hours Layer** name.
5. For insolation, tick **Insolation** and name its layer.
6. Press **Compute Sun Hours**.

The options that matter:

- **Merge Within** (2° by default): sun directions this close are cast as one
  ray. A year at 30-minute steps becomes about 1,200 rays. Raise it to go
  faster; the result shows the largest merge used.
- **Horizon**: the elevation of the far horizon in every direction, for
  terrain beyond the scene. **Use Horizon Profile** uses the profile from
  [Archaeoastronomy](#archaeoastronomy) instead, by azimuth.
- **Linke Turbidity** (3) and **Diffuse Sky**: the clear-sky model. With
  Diffuse Sky on, each sample also sees the sky through **Sky Rays** (64).
- **Ray Offset**: how far rays start off the surface. 0 is automatic.

The rays are cast by Cycles, in bakes of up to 1,024 directions each, on the
[bake device](#bake-device). The result shows the hours the sun is up, the
samples and rays, the time taken, the device and the range of each layer.

**Limits.** Insolation is a clear-sky model (Ineichen–Perez, matching pvlib):
no clouds and no weather. A year of sun hours per face on 10 million triangles
takes about 2.5 minutes on the CPU; a texture layer, or Merge Within 5°, brings
it under a minute. A GPU casts the rays several times faster: a year at
hourly steps on 10 million faces took 23 s on an RTX 4080 SUPER against 145 s
on the CPU.

## Shadow Study

Keys the Sun light at chosen times on the real equinoxes and solstices of the
Sun sub-panel's year, one frame per moment, with timeline markers.

1. Tick the days: **March Equinox**, **June Solstice**, **September
   Equinox**, **December Solstice**.
2. Set the **Hours** (from and to) and **Every** (hours between frames).
3. Press **Set Up Shadow Study**.
4. Render the frames it lists.

## Archaeoastronomy

Finds where the sun and Moon rise and set on the real skyline, for any epoch
within 10,000 years of 2000, and checks alignments against them.

### Observer and horizon

1. Set the **Observer**: **Cursor** (the 3D cursor) or **Pick** (click on a
   surface).
2. Set the **Eye Height** (1.6 m by default) and the **Azimuth Step** (0.5°).
3. Press **Compute Horizon**.

GeoTools casts rays over the scene's meshes and finds the apparent altitude of
the skyline in each direction, to about 0.002°: it reads 0 to 0.0016° low,
since the skyline is the highest of the fine rays that hit. **Earth Curvature** and
**Refraction** (terrestrial, k = 0.13) are on by default. **Ignore Within**
leaves out terrain near the eye. **Open Horizon** is the altitude used where no
terrain is hit; such open azimuths, and skylines at the edge of the terrain,
are flagged. To use the profile as the far horizon in
[Sun Hours](#sun-hours), tick **Use Horizon Profile** there. **Clear Horizon** removes it.

### Events

1. Set the **Epoch**: an astronomical year, so 0 is 1 BCE and -2999 is
   3000 BCE. The panel shows it as people say it.
2. Choose the **Limb**: **Upper Limb** (first or last gleam), **Centre** or
   **Lower Limb** (the whole disc clear).
3. **Lunar Standstills** adds the Moon's rise and set at the major and minor
   standstills. **Standstills** picks **Node Cycle** (the standstill geometry)
   or **Ephemeris** (the extremes the Moon actually reaches near the epoch).
4. **Dated Sunrises** adds sunrise and sunset times on the Sun sub-panel
   year's equinoxes and solstices and on the Sun date.
5. Press **Compute Events**.

The table gives each event's azimuth on the astronomical horizon and on the
real one (when a horizon profile exists). Copy it with the copy button. The
panel says how far back the results can be trusted.

Rises and sets include astronomical refraction (Bennett's formula, 34′ at the
horizon at sea level and 10 °C), at the standard atmosphere's pressure for
the site's ellipsoidal height: 30′ at 1,000 m, 21′ at 4,000 m. A June
sunrise at 51° N is 0.11° further from north at 1,000 m than at sea level,
0.37° at 4,000 m. Check Alignment uses the same pressure as the events. The
weather moves refraction at the horizon by several arc minutes either way.

### Sightlines and alignments

- **Create Sightlines** makes a mesh of lines, `GeoTools Sightlines`, from the
  eye to the horizon (or the **Length** you set) for each event, with event,
  azimuth, altitude, declination and epoch attributes. Export it with Export
  Vectors, or publish it as vectors.
- **Check Alignment** takes each selected edge of a mesh, such as a wall or a
  line of stones you drew, and gives its true azimuth both ways, the nearest
  events and the declination it implies. That declination is for the centre
  of the sun or a star with no parallax: the sun's 8.8″ is left out, as is
  usual in archaeoastronomy. For the Moon, add its parallax (about 0.95°)
  yourself, or compare azimuths with the events, which include it.

**Limits.** The horizon is only as good as the terrain in the scene. Beyond
it, set **Open Horizon** or import a wider terrain. Results for old epochs
carry the uncertainty the panel states.

## Measure

Reads points at full double precision, and measures between them.

1. For **Point A**, press **Cursor** (the 3D cursor) or **Pick** (click in the
   viewport). Do the same for **Point B**.
2. Each point shows **E**, **N** and **RL** (height in the project's height
   system), the **Ellipsoidal** height, **Latitude** and **Longitude**, the
   geodetic **Datum**, the combined **Scale** factor, and how precisely Blender
   stores the point locally (**float32**).
3. With both points, **A to B** shows the **Grid** and **Ground** distance, the
   **Height Diff**, the **Slope** distance, the grid and true bearings and the
   combined scale factor (**CSF**).

**Area of Selected Faces** measures the selected faces of the active mesh:
ground and grid area, in plan and over the surface.

Every result has a copy button. When the project changes after a reading, the
panel says **Project changed** and offers **Read Again**. **Clear** removes the
readings.

Ground distances use the combined scale factor: the grid scale from pyproj and
the height factor from the ellipsoidal height. In a CRS that is not conformal
(Albers equal-area, Web Mercator EPSG:3857) the grid scale depends on the
direction: a line uses the scale in its own direction, and a point's **Scale**
is the one along grid north. An Anchor project gives E, N
and U relative to the anchor; a Local project gives X, Y and Z.

## Presets

A preset is an ordinary Geometry Nodes group of stock nodes. **Add Analysis**
adds it as a modifier on the selected meshes and creates its layer, ready to
display and publish as a variant. Nothing is computed by GeoTools code, so you
can open the node group and change it.

1. Select one or more meshes. A mesh outside the source collection gets the
   layer too, but is not published; the report says so.
2. In the **Presets** sub-panel, open **Add Analysis** and pick a preset.
3. The new layer is selected in the Layers panel, with its range, palette and
   units set.
4. Change the preset's inputs on the modifier.

The menu groups the presets:

- **Terrain Shape**: Slope, Slope (Percent), Aspect, Profile Curvature, Plan
  (Tangential) Curvature, Mean Curvature, Rugosity, Topographic Position
  (TPI), Ruggedness (TRI);
- **Archaeological Visualisation**: Local Relief Model, Sky-View Factor,
  Positive and Negative Openness, and the bake-only Sky Visibility, Sky View
  (Cosine) and Openness (3D);
- **Visibility**: Viewshed, Cumulative Viewshed, Intervisibility, Line of
  Sight;
- **Change and Distance**: Distance to Surface, Distance to Feature;
- **Volumes**: Cut and Fill, Closed Volume;
- **Paths and Flow**: Least-Cost Path, Flow Path (Quick Look).

Entries with a clock icon are heavy on large meshes.

Aspect has a **(True North)** entry beside the grid-north one. It turns each
object by one angle, the convergence at its centre, which is exact in UTM,
MGA and other conformal grids. In Albers or Web Mercator aspects facing
between the meridian and the parallel stay up to a degree or two out (see
[analysis-presets.md](../analysis-presets.md#aspect-aspect-face-deg)); use a
conformal CRS for exact true aspects there. Sky-view factor,
openness and the cumulative viewshed have a **(Fast Bake)** entry: the same
rays cast once by a Cycles bake, much faster on large meshes, stored as a
layer instead of a live modifier. Sky-view factor and openness assume a
height field and read open sky under an overhang; on meshes with overhangs
use Sky Visibility, Sky View (Cosine) or Openness (3D), which count the sky
directly ([which to use](../analysis-presets.md#sky-visibility-sky-view-cosine-and-openness-3d-sky_visibility-sky_view_cosine-openness_3d-face-ratio)).

Presets that need an observer, a line or a second surface take it from the
other selected objects or the 3D cursor. New helper objects and line results
go in a **GeoTools Analysis** collection, never the source collection.
**Read Totals** reports volumes, walking times and obstructions.

In the demo, select `terrain` and add **Slope**: a `slope` face layer in
degrees appears. Set it as the build profile's **Variant** to publish
`site.slope.3tz`.

Every preset's formula, inputs, accuracy and timing are in
[analysis-presets.md](../analysis-presets.md).

**Limits.** Live presets re-evaluate with the mesh, which can take minutes at
10 million triangles for the visibility presets; use the Fast Bake entries.
The Least-Cost Path preset follows mesh edges and uses one cost per edge both
ways; [Least-Cost Routes](#least-cost-routes) says when to use which.

## Least-Cost Routes

The quickest way to walk (or wheel) over one 3D surface: a route from a
Start to an End, and the travel time from Start to every face. Uphill and
downhill cost differently, so the route there and the route back can differ,
and routes cross faces at any angle instead of following mesh edges.

1. Choose the **Surface**: the **Selected Objects** (the default: select the
   mesh to walk on) or the **Source Collection**. It must be one mesh object.
2. Set **Start** and **End**: any objects, usually Empties. Their origins snap
   to the nearest face that can be crossed. Left empty, each gets a new Empty
   at the 3D cursor (`Route Start`, `Route End`, in GeoTools Analysis): move
   it and press again.
3. Choose the **Model** and set **Max Slope** (30° by default): faces steeper
   than this, or facing down, cannot be crossed.
4. Optionally choose a **Cost Layer**: a face layer whose values multiply the
   time on each face (2 for dense scrub). 0, negative or no data is
   impassable, so a wall or a trench is a band of no data with its gaps.
5. Choose the outputs: **Route**, **Travel Time** (into the **Time Layer**,
   `travel_time`) or both, and optionally **Isochrones**.
6. Press **Compute Least-Cost**. The status bar counts the search's passes;
   Esc cancels.

The models, per step of horizontal run d and rise h:

| Model | Time | Source |
|---|---|---|
| **Tobler** | d at 6 exp(−3.5 \|h/d + 0.05\|) km/h: 6 km/h at 5 % downhill, 5.04 on the level | Tobler (1993), as the Least-Cost Path preset |
| **Langmuir (r.walk)** | 0.72 s/m × d + 6.0 s per metre climbed; downhill −1.9998 s per metre dropped down to 12° (tan 12° = 0.2125), +1.9998 s per metre steeper | GRASS r.walk's default coefficients (Aitken 1977, Langmuir 1984) |
| **Naismith** | 0.72 s/m × d (5 km/h) + 6 s per metre climbed (600 m an hour); descents add nothing | Naismith (1892) |
| **Wheelchair** | 1.12 m/s on the level and downhill, falling linearly to 0.59 m/s at a 4.8° climb; no step steeper than **Max Gradient** (8.33 %, 1:12) either way | Speeds measured for manual wheelchairs by Jahanian et al. (2016); 1:12 is the steepest ramp of the ADA Standards (2010, 405.2) |

The outputs:

- **Route**: a line object, `Least-Cost Route`, in GeoTools Analysis, with
  `elapsed` (seconds from Start) at each point and `time` on each edge. Export
  it with **Export Vectors**, or publish it with a Vectors build profile.
- **Travel Time**: a face layer of the seconds from Start to each face's
  centre. Faces that cannot be reached have no data. It displays and
  publishes as a variant like any layer.
- **Isochrones**: with a step in minutes (0 for none), a category layer
  (**Band Layer**, `isochrones`) of the bands 0 to 5 min, 5 to 10 min and so
  on, with `Unreached` for faces with no time.

**Steiner Points** (3 by default) are points added inside every triangle edge,
so a route can cross a face in more directions. With none, routes follow mesh
edges: up to 41 % longer than the straight line on a triangulated grid. With 3
the worst direction is 2.9 % longer and the average 1.2 %; with 6, 1.1 % and
0.5 % ([validation](../validation/least-cost.md#any-angle-routes)). More points
cost time: on one million triangles, 2 s with none, about 13 s with 3 and
16 s with 4.

In a CRS project, plan distances are ground distances: grid distances
divided by the combined scale factor at the surface's centre.

**When to use which.** The Least-Cost Path preset is a live Geometry Nodes
modifier: it updates as you move Start and End or edit the mesh, which suits
trying routes out. Least-Cost Routes is computed once, and gives the one-way
quickest route (the preset routes on the mean of both directions), routes not
tied to mesh edges, the travel time to every face, isochrones, and the
Langmuir, Naismith and wheelchair models. Use it for results you publish or
report.

**Limits.**

- **One connected surface.** Several objects are refused: join them into
  one object (Object > Join) where they meet. Routes from one object to
  another through stairs, ladders or doorways are issue
  [#109](https://github.com/BenDyson-Arch/blender_geotools/issues/109).
- **Normals point out of the ground.** A face facing down is an overhang's
  underside and is never crossed; recalculate the normals of a mesh imported
  inside out.
- **The wheelchair model has no cross-slope limit.** Each step is held to
  Max Gradient, so on a slope steeper than that the route traverses it, as a
  ramp's switchbacks do; the ADA's 1:48 cross slope is not checked.
- **Times are to face centres** and Start and End snap to the surface, so a
  Start in mid-air or on a wall starts from the nearest face that can be
  crossed; the result says how far it moved when that is more than 0.5 m.

## Hydrology

Shows where water would run and pond on the 3D surface itself, so walls,
overhangs and several levels work, which a 2.5D DEM cannot do. It is
geometric only: no rainfall, infiltration, evaporation or hydraulics.

1. Choose the **Surface**: the **Selected Objects** (the default) or the
   **Source Collection**.
2. Choose the **Area** to accumulate: **Plan** (the area of upward faces that
   catches rain) or **Surface** (every face, walls included).
3. Press **Compute Hydrology**.

It writes three face layers:

- `flow_accumulation`: the upslope area that drains through each face, in m²,
  shown on a log scale;
- `ponded_depth`: how deep water stands where depressions fill;
- `depression`: which depression each face is in, largest volume first.

Options:

- **Minimum Depth** (0.01): shallower depressions are filled but not listed.
- **Depression Points** (on): a point object, `Depressions`, at each
  depression's pour point, with level, volume, area, depth and catchment.
- **Flow Paths** (off): a line object, `Flow Paths`, of the main flow paths:
  faces with at least **Channel From** m² draining through them, for the
  largest **Networks** outlets.

Water runs from face to face across shared edges, down walls, and drops from
open edges and overhangs onto whatever lies below, across objects. The result
lists the largest catchment, the depressions and the ponded volume. It takes
about 20 seconds on 10 million triangles.

**Limits.**

- **False pits in V-channels.** Faces link only across shared edges. Where a
  V-shaped channel runs exactly along a line of mesh edges (common on a DEM
  converted to a mesh), the channel's two lowest faces in each cell meet only
  at a vertex, so each cell is a shallow pit, (side slope − 2 × fall) × h/3
  deep for cells h wide: 67 mm for 0.3 side slopes and a 5 % fall on 1 m
  cells. Water fills them and passes on, so flow accumulation is unaffected,
  but `ponded_depth` and `depression` show them and coarse meshes list them.
  Set **Minimum Depth** to about a third of a face's fall across the channel
  (side slope × h/3) to hide them; on photogrammetry meshes, with faces of a
  few centimetres, they are millimetres deep and the default hides them.
- **Spill levels at face centres.** Heights are taken at face centres, so a
  depression's spill level reads about a third of a cell times the slope
  lower than a raster tool's (h/3 × slope): millimetres on photogrammetry
  meshes, 3 % of the volume of a cone-shaped pit at 1 m cells.

## Contours and Sections

Cuts contour lines and cross-sections from the meshes, with heights in the
project's height system (RL: Blender Z plus offset A).

**Contours:**

1. Choose the **Surface**: the **Selected Objects** (the default) or the
   **Source Collection**.
2. Set the **Interval**, the **Base** level and **Major Every** (every fifth
   line is major by default).
3. Press **Create Contours**.

The result is a line object (`Contours`) with `elevation`, `index`, `major`
and `closed` attributes. A level that only touches the surface gives no line:
the lowest level of a flat-bottomed surface, or an isolated vertex (a peak
or pit) exactly on a level. GDAL's `gdal_contour` draws a line along the
bottom edge or a tiny loop there; no area is missed either way.

**Sections:**

1. Draw a line object along the section, or use an imported line layer, and
   set it as the **Section Line**. With **Selected Objects**, select the meshes
   to cut as well: the line itself is never cut.
2. Under **Edges**, tick **Selected Only** to cut along the selected edges only.
3. Press **Create Sections**.

The result is a line object (`Sections`) with `chainage` per point. Every
piece of a multi-level surface is kept. Exported to a GeoPackage, each piece is
a LineString ZM whose M is the chainage, the GIS convention for linear
referencing (QGIS and GDAL read it). GeoJSON has no M, so a GeoJSON piece
carries a `chainage` property instead: one value per point, in the order of
its coordinates. Both also have `chainage_from` and `chainage_to` per piece.
A Vectors publish leaves M out (rusty-tiles 0.4 refuses measured geometry)
and keeps `chainage_from` and `chainage_to`.

Both go in the **GeoTools Analysis** collection. Export them with **File >
Export > GeoPackage (.gpkg)**, with absolute heights, or publish them with a
Vectors build profile. 1 m contours of 10 million triangles take about 6
seconds.

How each tool was checked against reference tools, with its measured
accuracy, is in [Validation](../validation/README.md).
