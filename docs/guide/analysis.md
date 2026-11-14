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

Most tools work on the source collection's meshes, in Object Mode.

## Sun

Places a Blender Sun light where the sun is, for a date, time and place.
Shadows are then rendered or baked by Blender.

1. Set the date and time (year, month, day; hour, minute, second).
2. Choose the **Time**:
   - **Time Zone**: an IANA name such as `Australia/Brisbane`, with daylight
     saving;
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

**Limits.** Blender on Windows usually has no time zone database. Then only
`UTC` works as a zone name, and the panel says so: use a UTC offset instead.

## Sun Hours

Counts the hours of direct sun on each face, face corner or texel over a day,
a season or a year, with the shadows of the scene's meshes. Optionally it
also computes clear-sky insolation in kWh/m².

The date, time zone, site and refraction come from the Sun sub-panel.

1. Choose the **Period**: **Day** (the Sun date), **Dates** (from the Sun date
   to an end date) or **Year** (the Sun date's year).
2. Set the **Step (min)**: how often the sun's position is taken. Only steps
   with the sun up count.
3. Choose where the values go: **Values** **Face**, **Corner** or **Texture**
   (with a **Resolution**), and the **Hours Layer** name.
4. For insolation, tick **Insolation** and name its layer.
5. Press **Compute Sun Hours**.

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

The rays are cast by Cycles, in bakes of up to 1,024 directions each. The
result shows the hours the sun is up, the samples and rays, the time taken
and the range of each layer.

**Limits.** Insolation is a clear-sky model (Ineichen–Perez, matching pvlib):
no clouds and no weather. A year of sun hours per face on 10 million triangles
takes about 2.5 minutes; a texture layer, or Merge Within 5°, brings it under
a minute.

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

1. Select one or more meshes in the source collection.
2. In the **Presets** sub-panel, open **Add Analysis** and pick a preset.
3. The new layer is selected in the Layers panel, with its range, palette and
   units set.
4. Change the preset's inputs on the modifier.

The menu groups the presets:

- **Terrain Shape**: Slope, Slope (Percent), Aspect, Profile Curvature, Plan
  (Tangential) Curvature, Mean Curvature, Rugosity, Topographic Position
  (TPI), Ruggedness (TRI);
- **Archaeological Visualisation**: Local Relief Model, Sky-View Factor,
  Positive and Negative Openness;
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
layer instead of a live modifier.

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
Least-cost paths use one cost per edge both ways.

## Hydrology

Shows where water would run and pond on the 3D surface itself, so walls,
overhangs and several levels work, which a 2.5D DEM cannot do. It is
geometric only: no rainfall, infiltration, evaporation or hydraulics.

1. Choose the **Surface**: the **Source Collection** or the **Selected
   Objects**.
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

1. Choose the **Surface**.
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
   set it as the **Section Line**.
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

Design details for every tool: [design/m5-analysis.md](../design/m5-analysis.md).
