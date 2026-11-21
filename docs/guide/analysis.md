# Analysis

GeoTools does the analyses that need the project's georeference, or that
neither a GIS nor stock Blender does well. Everything else is a Geometry
Nodes preset you can open and edit. They sit in two panels of the sidebar:

- **Analyses**, between Linked Data and Tools, lists the active mesh's
  analyses and layers, as Blender's modifier stack lists its modifiers:
  [presets](#presets) (with their Fast Bakes), [Hydrology](#hydrology),
  [Sun Hours](#sun-hours), [Least-Cost Routes](#least-cost-routes), and
  [Contours and Sections](#contours-and-sections). Results are
  [layers](layers.md), so they display, publish as variants and export as
  metadata. Lines and points are mesh objects that **Export Vectors** writes
  as GeoPackage or GeoJSON.
- **Tools**, closed by default, holds the readings that are not layers:
  [Measure](#measure), [Sun](#sun) (with [Shadow Study](#shadow-study)) and
  [Archaeoastronomy](#archaeoastronomy).

**What the tools need:**

- In a **CRS** project, the bundled pyproj. Without it the Tools panel says
  so, and the tools that relate the scene to the Earth stop.
- In an **Anchor** project, the anchor.
- In a **Local** project, **Tools > Sun** asks for a longitude and latitude,
  and takes +Y as true north. Points have local coordinates only.

Only the source collection is published: a layer on a mesh outside it works
and shows, and the report says it is not published (see
[Source](projects.md#source)). If no source collection is set, adding an
analysis sets it to the collection of the mesh, and the report says so.

Hydrology, Sun Hours, Sky-View Factor and Openness read each face's normal as
pointing out of the surface. They check the normals first and warn in their
report when faces are wound against their neighbours or most of the area
faces down; they run anyway. See [Normals](publish.md#normals).

### Bake device

Sun Hours, the horizon profile, the Fast Bakes and
[texture bakes](layers.md#texture-layers-and-the-bake-box) cast their
rays with Cycles. **Bake Device** in the GeoTools preferences (**Edit >
Preferences > Add-ons**, GeoTools) chooses where:

- **Auto** (the default): the GPU when Cycles has one enabled under **Edit >
  Preferences > System > Cycles Render Devices** (CUDA, OptiX, HIP, Metal or
  oneAPI), otherwise the CPU. With **None** chosen there, bakes use the CPU.
- **GPU**: the same, with a warning when it falls back to the CPU.
- **CPU**: always the CPU.

GeoTools only reads Cycles' device settings; it never changes them. The
scene's own **Device** (Render Properties) is put back after each bake. The
report says which device a bake used.
Results agree between devices: the same rays hit, and sums differ at most in
the last digits. The GPU speeds up casting rays; reading the mesh and saving
images take as long as before, so a texture bake gains little. The first GPU bake after installing Blender or a new driver
can take a minute or two while Cycles prepares its GPU code; later ones start
at once.

## The Analyses panel

The panel shows the analyses of the **active** object only. With no mesh
active it says "Select a mesh to see its analyses", and **Add Analysis** is
greyed out until a mesh is selected. Work in Object Mode.

### Add an analysis

1. Select one or more meshes, in any collection.
2. Click **Add Analysis** at the top of the panel and type to filter, for
   example "slope", "water" or "sun".
3. Pick an entry. The new result is added and selected in the list.

The entries are named *Group > Name*:

| Group | Entries |
|---|---|
| Terrain | Slope, Slope (Percent), Aspect, Aspect (True North), Profile Curvature, Plan (Tangential) Curvature, Mean Curvature, Rugosity, Topographic Position (TPI), Ruggedness (TRI) |
| Archaeology | Local Relief Model; Sky-View Factor, Positive Openness and Negative Openness, each also as a Fast Bake; Sky Visibility, Sky View (Cosine) and Openness (3D), which are only baked |
| Visibility | Viewshed, Cumulative Viewshed, Cumulative Viewshed (Fast Bake), Intervisibility, Line of Sight |
| Change | Distance to Surface, Distance to Feature |
| Volumes | Cut and Fill, Closed Volume |
| Water | Flow Path (Quick Look), [Hydrology](#hydrology) |
| Sun | [Sun Hours](#sun-hours), Insolation (Sun Hours with insolation on) |
| Movement | Least-Cost Path, [Least-Cost Routes](#least-cost-routes) |
| Lines | [Contours and Sections](#contours-and-sections) |
| Layers | **From Attribute** and **Texture Bake**: a plain [layer](layers.md#add-a-layer) |

With several meshes selected, Add Analysis makes **one result per mesh**,
each with its own inputs, as a modifier added to each. Two meshes with Slope
have two results, each with its own modifier, that share the layer `slope`;
its colours and its variant cover both. Adding the same preset to a mesh
again stores a new layer, `slope_2`. Presets that take a line, points or a
second surface from the selection go on the active mesh only.

### The list

Each row shows the kind's icon, the name, and on the right the layer's units,
"Not run" for a result that has not been computed yet, or a refresh icon when
its inputs changed since it ran. The eye button is **Show in Viewport**
([Display](layers.md#show-a-layer-the-display-sub-panel)).

A layer that no analysis on the mesh makes, such as a From Attribute or
Texture Bake layer, a GeoTIFF import, values written by a script, or values a
tool wrote over several meshes, is a plain row on each mesh that carries it.

When you make another mesh active, the list follows it. If the new mesh has a
result of the same kind (Slope to Slope), that result is selected, otherwise
its first.

The **−** button is **Remove Analysis**. Its confirmation lists what it
deletes: the result's modifier on its mesh, its layer values on that mesh, and
the objects it made, such as contour lines. A layer that no other result uses
and no mesh carries any more leaves the project.

### The selected result

Under the list, sub-panels show the selected row:

- **Inputs**:
    - a preset's modifier inputs, drawn from the modifier itself: editing
      them here or in the Properties editor is the same thing. Aspect says
      whether it is "Measured from grid north (+Y)" or "true north". Cut and
      Fill, Closed Volume, Least-Cost Path, Intervisibility and Line of Sight
      have **Read Totals**;
    - a Fast Bake's or a tool's own inputs, its **Surface**, and **Compute**
      (**Recompute** after the first run). When you change an input after a
      run, the panel says "Inputs changed since the last run: out of date."
      Under the button it shows a short summary of the run, or its error;
    - a plain layer's fields (see [Layers](layers.md#add-a-layer)).
- **Display**: the layer's colours, and **Show in Viewport**
  ([Display](layers.md#show-a-layer-the-display-sub-panel)).
- **Details** (closed by default): the layer's rarely changed fields
  ([Details](layers.md#details)).
- **Outputs** (closed by default): the layers the result made, with a radio
  button that picks the one Display shows and an eye for each; the objects it
  made, with a button that selects them; **Publish as Variant**, which sets
  the active build profile's **Variant** to the layer; and, for line objects,
  "Export: File > Export > GeoPackage".

**Surface** is the meshes a Fast Bake or a tool runs on:

- **This Mesh** (the default): the result's own mesh;
- **Selected Objects**: this mesh and the meshes selected when you press
  Compute, as one surface;
- **Source Collection**: every mesh of the source collection, the whole site.

The result still belongs to its mesh. The values a run writes on the other
meshes show there as plain layer rows.

### Files from earlier versions

A file saved before the Analyses panel lists its analyses when you open it.
Preset modifiers and line objects become preset results on their meshes. Fast
Bake layers become Fast Bake results with the inputs they recorded.
Hydrology, sun-hours and least-cost layers become results of their tool, with
the scene's last settings for that tool, and contour and section objects join
a result. Every other layer is a plain row on each mesh that carries it.

## Presets

A preset is an ordinary Geometry Nodes group of stock nodes. Adding one puts
it as a modifier on the mesh and creates its layer, ready to display and
publish as a variant. Nothing is computed by GeoTools code, so you can open
the node group and change it. Change the preset's inputs under **Inputs**, or
on the modifier in the Properties editor.

Aspect has a **(True North)** entry beside the grid-north one. It turns each
object by one angle, the convergence at its centre, which is exact in UTM,
MGA and other conformal grids. In Albers or Web Mercator aspects facing
between the meridian and the parallel stay up to a degree or two out (see
[analysis-presets.md](../analysis-presets.md#aspect-aspect-face-deg)); use a
conformal CRS for exact true aspects there.

Sky-view factor, openness and the cumulative viewshed have a **(Fast Bake)**
entry: the same rays cast once by a Cycles bake, much faster on large meshes,
stored as a layer (such as `sky_view_factor_fast`) instead of a live
modifier. Set its inputs and press **Compute**; press **Recompute** after
editing the mesh or the inputs. Sky-view factor and openness assume a
height field and read open sky under an overhang; on meshes with overhangs
use Sky Visibility, Sky View (Cosine) or Openness (3D), which count the sky
directly ([which to use](../analysis-presets.md#sky-visibility-sky-view-cosine-and-openness-3d-sky_visibility-sky_view_cosine-openness_3d-face-ratio)).

Presets that need an observer, a line or a second surface take it from the
other selected objects or the 3D cursor. New helper objects and line results
go in a **GeoTools Analysis** collection, never the source collection.
**Read Totals** reports volumes, walking times and obstructions.

In the demo, select `terrain` and add **Terrain > Slope**: a `slope` face
layer in degrees appears. Press **Publish as Variant** under **Outputs** to
publish `site.slope.3tz`.

Every preset's formula, inputs, accuracy and timing are in
[analysis-presets.md](../analysis-presets.md).

**Limits.** Live presets re-evaluate with the mesh, which can take minutes at
10 million triangles for the visibility presets; use the Fast Bake entries.
The Least-Cost Path preset follows mesh edges and uses one cost per edge both
ways; [Least-Cost Routes](#least-cost-routes) says when to use which.

## Hydrology

Shows where water would run and pond on the 3D surface itself, so walls,
overhangs and several levels work, which a 2.5D DEM cannot do. It is
geometric only: no rainfall, infiltration, evaporation or hydraulics.

1. Add **Water > Hydrology** to the mesh.
2. Choose the **Surface**. With **This Mesh**, water runs on this mesh
   only; for a site of several meshes choose **Selected Objects** or
   **Source Collection**.
3. Choose the **Area** to accumulate: **Plan** (the area of upward faces that
   catches rain) or **Surface** (every face, walls included).
4. Press **Compute**.

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
open edges and overhangs onto whatever lies below, across objects. The
summary shows the depressions, the ponded volume and the time taken. It takes
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

## Sun Hours

Counts the hours of direct sun on each face, face corner or texel over a day,
a season or a year, with the shadows of the scene's meshes. Optionally it
also computes clear-sky insolation in kWh/m².

Each result has its own dates. The time zone, site and refraction are the
project's, set in **Tools > Sun**, because they describe the place.

1. Add **Sun > Sun Hours** to the meshes that get the values, or **Sun >
   Insolation**, which is Sun Hours with **Insolation** on.
2. Choose the **Surface**. Every mesh that renders casts shadows either way.
3. Choose the **Period** and its dates: **Day** (one **Date**), **Dates**
   (**From** and **To**, a season) or **Year**. A new result starts on the
   date set in Tools > Sun.
4. Set the **Step (min)**: how often the sun's position is taken. Only steps
   with the sun up count.
5. Choose where the values go: **Values** **Face**, **Corner** or **Texture**
   (with a **Resolution**), and the **Hours Layer** name.
6. For insolation, tick **Insolation** and name its layer.
7. Press **Compute**.

The options that matter:

- **Merge Within** (2° by default): sun directions this close are cast as one
  ray. A year at 30-minute steps becomes about 1,200 rays. Raise it to go
  faster.
- **Horizon**: the elevation of the far horizon in every direction, for
  terrain beyond the scene. **Use Horizon Profile** uses the profile from
  [Archaeoastronomy](#archaeoastronomy) instead, by azimuth.
- **Linke Turbidity** (3) and **Diffuse Sky**: the clear-sky model. With
  Diffuse Sky on, each sample also sees the sky through **Sky Rays** (64).
- **Ray Offset**: how far rays start off the surface. 0 is automatic.

The rays are cast by Cycles, in bakes of up to 1,024 directions each, on the
[bake device](#bake-device). The summary shows the days covered and the time
taken.

**Limits.** Insolation is a clear-sky model (Ineichen–Perez, matching pvlib):
no clouds and no weather. A year of sun hours per face on 10 million triangles
takes about 2.5 minutes on the CPU; a texture layer, or Merge Within 5°, brings
it under a minute. A GPU casts the rays several times faster: a year at
hourly steps on 10 million faces took 23 s on an RTX 4080 SUPER against 145 s
on the CPU.

## Least-Cost Routes

The quickest way to walk (or wheel) over one 3D surface: a route from a
Start to an End, and the travel time from Start to every face. Uphill and
downhill cost differently, so the route there and the route back can differ,
and routes cross faces at any angle instead of following mesh edges.

1. Add **Movement > Least-Cost Routes** to the mesh to walk on. Its
   **Surface** must be one mesh object.
2. Set **Start** and **End**: any objects, usually Empties. Their origins snap
   to the nearest face that can be crossed. Left empty, each gets a new Empty
   at the 3D cursor (`Route Start`, `Route End`, in GeoTools Analysis): move
   it and press **Recompute**.
3. Choose the **Model** and set **Max Slope** (30° by default): faces steeper
   than this, or facing down, cannot be crossed.
4. Optionally choose a **Cost Layer**: a face layer whose values multiply the
   time on each face (2 for dense scrub). 0, negative or no data is
   impassable, so a wall or a trench is a band of no data with its gaps.
5. Choose the outputs: **Route**, **Travel Time** (into the **Time Layer**,
   `travel_time`) or both, and optionally **Isochrones**.
6. Press **Compute**. The summary shows the route's time and length and the
   time the search took.

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

## Contours and Sections

Cuts contour lines and cross-sections from the meshes, with heights in the
project's height system (RL: Blender Z plus offset A).

**Contours:**

1. Add **Lines > Contours** to the mesh, and choose the **Surface**.
2. Set the **Interval**, the **Base** level and **Major Every** (every fifth
   line is major by default).
3. Press **Compute**.

The result is a line object (`Contours <mesh>`) with `elevation`, `index`,
`major` and `closed` attributes. A level that only touches the surface gives
no line: the lowest level of a flat-bottomed surface, or an isolated vertex
(a peak or pit) exactly on a level. GDAL's `gdal_contour` draws a line along
the bottom edge or a tiny loop there; no area is missed either way.

**Sections:**

1. Draw a line object along the section, or use an imported line layer.
2. Select the meshes to cut and the line, and add **Lines > Sections**: the
   line becomes the **Section Line**, and is never cut. Or set the Section
   Line afterwards.
3. Under **Edges**, tick **Selected Only** to cut along the selected edges only.
4. Press **Compute**.

The result is a line object (`Sections <mesh>`) with `chainage` per point.
Every piece of a multi-level surface is kept. Exported to a GeoPackage, each
piece is a LineString ZM whose M is the chainage, the GIS convention for
linear referencing (QGIS and GDAL read it). GeoJSON has no M, so a GeoJSON
piece carries a `chainage` property instead: one value per point, in the
order of its coordinates. Both also have `chainage_from` and `chainage_to`
per piece. A Vectors publish leaves M out (rusty-tiles 0.4 refuses measured
geometry) and keeps `chainage_from` and `chainage_to`.

Both go in the **GeoTools Analysis** collection, and are listed under the
result's **Outputs**. Export them with **File > Export > GeoPackage (.gpkg)**,
with absolute heights, or publish them with a Vectors build profile. 1 m
contours of 10 million triangles take about 6 seconds.

## Tools

The **Tools** panel sits below Analyses and is closed by default. Its
sub-panels are **Measure**, **Sun** (with **Shadow Study** inside it) and
**Archaeoastronomy**.

### Measure

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

### Sun

Places a Blender Sun light where the sun is, for a date, time and place.
Shadows are then rendered or baked by Blender. Its time zone, site and
refraction are also the ones [Sun Hours](#sun-hours) uses.

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

#### Shadow Study

Keys the Sun light at chosen times on the real equinoxes and solstices of the
Sun sub-panel's year, one frame per moment, with timeline markers.

1. Tick the days: **March Equinox**, **June Solstice**, **September
   Equinox**, **December Solstice**.
2. Set the **Hours** (from and to) and **Every** (hours between frames).
3. Press **Set Up Shadow Study**.
4. Render the frames it lists.

### Archaeoastronomy

Finds where the sun and Moon rise and set on the real skyline, for any epoch
within 10,000 years of 2000, and checks alignments against them.

#### Observer and horizon

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

#### Events

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

#### Sightlines and alignments

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

How each tool was checked against reference tools, with its measured
accuracy, is in [Validation](../validation/README.md).
