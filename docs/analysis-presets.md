# Analysis presets

Status: the M5 preset library (see
[ROADMAP.md](ROADMAP.md#m5-analysis-library)). First batch: terrain shape and
archaeological visualisation. Second batch: visibility, change and distance,
volumes, and paths and flow. Sky-view factor, openness and the cumulative
viewshed also have a [fast (bake) path](#fast-bake-paths) for large meshes.

A preset is an ordinary Geometry Nodes group built from stock nodes. **Add
Analysis** (Analysis panel ▸ Presets) appends it to the file, adds it as a
modifier on the selected meshes and creates the [layer](design/m3-layers.md)
its Store Named Attribute writes. Nothing is computed by GeoTools code: the
modifier computes the values, the layer reads them from the evaluated mesh,
and publishing applies the modifier. Change a preset's inputs on the modifier,
or open its node group and edit it. Apply the modifier (Blender's own tool) to
freeze a result.

## Using a preset

1. Select one or more meshes (in the source collection, so they publish).
2. **Add Analysis**, then pick a preset. One click adds it with the defaults
   below; the Adjust Last Operation panel changes the layer name and, for
   aspect, the north.
3. The new layer is selected in the Layers panel, with its value range already
   read, a palette and, for bounded quantities, a fixed display range. Publish
   it as a variant (Publish ▸ Variant) like any layer.

Adding the same preset to more objects reuses the layer and the node group.
Adding it under another layer name (for example `slope_smooth` with a
different Smoothing) adds a second modifier on the same node group.

Each layer records its preset in its provenance:

```json
{"tool": "analysis_preset", "geotools_version": "0.3.0-dev",
 "parameters": {"preset": "slope", "preset_version": 1, "node_group": "GeoTools Slope",
                "library": "analysis_presets.blend", "inputs": {"Smoothing": 0},
                "objects": ["Site"]}}
```

`inputs` are the modifier's values when the preset was added (per object, when
the objects differ); an object input is recorded by the object's name. Later
edits on the modifier are not tracked.

### Object inputs

Presets that need an observer, a second surface, a line or a set of points
have object inputs on the modifier. Add Analysis fills them as it adds the
preset, from the other selected objects (in name order) or the 3D cursor:

| The input needs | Taken from the selection | Otherwise |
|---|---|---|
| a point (Observer, Start, End) | a selected Empty, camera or light: its origin | a new Empty at the 3D cursor |
| points (Observers, Points, Seeds) | another selected mesh, curve or point cloud (its vertices or points), or an Empty (its origin) | a new mesh of one vertex at the 3D cursor; add more vertices in Edit Mode |
| a second surface (Reference) | another selected mesh with faces | refused, nothing changed |
| a line (Line) | a selected mesh with edges, or a curve | refused, nothing changed |
| a feature (Feature) | another selected mesh, curve or point cloud | refused, nothing changed |

New Empties and meshes go in the **GeoTools Analysis** collection, never in
the source collection, so they do not publish with the site. Move an Empty,
edit the points, or pick another object on the modifier: the result updates.

Presets that take a mesh, line or points from the selection go on the
**active** object only; viewshed and closed volume, like the first batch, go
on every selected mesh. Adding a preset to an object that already has it keeps
the modifier and its inputs, and makes no new Empty.

### Object presets: lines

Intervisibility, line of sight, least-cost path and flow path make lines, not
a layer. Add Analysis creates a new mesh object in GeoTools Analysis (named
after the preset, or the name given), whose modifier builds the lines from the
surface (the active mesh) and the other inputs. The lines carry fixed POINT
and EDGE attributes, listed with each preset. The object's
`geotools_provenance` custom property holds the same provenance a layer
records (JSON), plus the attributes and their units. Choose the collection in
a Vectors build profile to publish the lines with their attributes: loose
edges become lines and loose vertices points, and EDGE and POINT attributes
become columns ([m8-geo-formats.md](design/m8-geo-formats.md)).

### Totals

Cut and fill and closed volume show totals on the modifier, under its name
(Blender's info line from a Warning node, summed in float32 by Attribute
Statistic), as does a least-cost path (its walking time). **Read Totals** in the
Presets panel reads them for the active object from the evaluated mesh,
summed in binary64, and also reports intervisibility counts and the line of
sight's obstruction; Add Analysis reports the same when it adds the preset.
Read Totals only sums values the nodes made (and face areas and normals).

## Fast (bake) paths

Sky-view factor, positive and negative openness and the cumulative viewshed
cast many rays per element, and in Geometry Nodes take minutes on a
10M-triangle mesh. Each also has a **fast path**: the same rays, inputs and
formulas, cast by Cycles `Raycast` shader nodes in one EMIT bake (the sun-hours
engine, [m5-analysis.md](design/m5-analysis.md#fast-bake-paths-for-presets)),
and stored as a layer.

**Using it.** Add Analysis ▸ *Sky-View Factor (Fast Bake)* (and likewise for
the other three), or set **Engine** to *Fast (bake)* in the Adjust Last
Operation panel. The preset's inputs are on that panel, with the preset's
defaults; the cumulative viewshed takes its Observers from the selection or
the 3D cursor as the live preset does, and an optional Occluders collection.
No modifier is added: the values are computed once, written to the meshes
(`gt_<layer>`, CORNER or FACE as the preset's) and the layer's origin is
*analysis*, with a provenance naming the preset and version it reproduces,
the method (`"engine": "bake"`), the inputs, the objects, the samples, the
rays per sample and the bakes:

```json
{"tool": "analysis_bake", "parameters": {"preset": "sky_view_factor", "preset_version": 1,
  "engine": "bake", "method": "Zakšek et al. (2011) SVF = 1 - mean sin(max(horizon, 0)); ...",
  "inputs": {"Max Distance": 2.0, "Directions": 16, "Steps": 8, "Ray Offset": 0.002},
  "objects": ["Site"], "samples": 5004169, "rays_per_sample": 128, "bakes": 1, ...}}
```

To recompute (after editing the mesh, or with other inputs), run it again
under the same layer name. A fast layer and a live preset layer cannot share a
name: Add Analysis refuses and suggests another (`sky_view_factor_fast`).

**Why a choice in Add Analysis, not a button on the layer.** A button on a
live preset's layer would first need the live modifier evaluated: the minutes
this path saves. The choice sits where the analysis is added, offers the
preset's own inputs, and the live preset stays one click away (and is the one
to use for editing the node group or keeping the result live).

**How it differs from the live preset.**

- *Computed once*, not live: moving an observer or editing the mesh does not
  update it.
- *Scene space*: rays run in the scene's axes and units. For an unrotated,
  unscaled object (imported meshes are) that is the preset's own space.
- *Occluders*: the meshes it is added to (all of them, so selected tiles of
  one site shade each other) and, for the viewshed, the Occluders collection.
  Everything else is hidden from the bake. An occluder outside the view layer
  cannot render, so it does not occlude (the panel says so).
- An object whose modifiers change its topology is skipped with a note, as
  sun hours does (apply them, or use the live preset).

**Agreement with the live presets** (`tests/blender/test_preset_bake.py`,
and `scripts/bench_preset_bake.py --compare` on the benchmark terrain):

| Case | Result |
|---|---|
| level plane: SVF, openness | SVF 1 exactly; openness 90.35° (as the preset); fast and live identical more than Max Distance from the edge |
| V-trench, walls 30°: bottom | SVF 0.6737 (exact 0.6714), openness 70.75° and 109.51°, as the preset; fast and live identical away from the edge |
| V-trench: every corner not on the mesh boundary | equal to the preset's bisection with exact ray-plane intersections (binary64), within 2×10⁻⁴, also where rays leave the mesh. The live preset differs from that on 13 % of the trench's corners, all within 1.5 m of the mesh's edge (not yet explained); on the boundary itself a ray along the edge grazes it, and either engine may call it a hit |
| benchmark terrain, 1M triangles, corners more than Max Distance from the edge | identical on 99.98 %; the rest (558 of 2.5M for SVF) differ where a bisection ray grazes the surface within float32, which either engine may call a hit: up to 0.02 SVF or 7° openness, mean 5×10⁻⁶ SVF |
| wall on a plane, three observers (draped from 5 m) | every ground face equal to batch 2's segment-box test, and fast equals live on every face; the same over two bakes, and with an Occluders collection |
| benchmark terrain, 1M triangles, 100 observers | equal to the live preset on all but 4 of 1M faces (grazing sight lines) |

**Timings** (`scripts/bench_preset_bake.py`, the benchmark terrain at 2 cm,
defaults, the viewshed with 100 draped observers; Blender 5.2.4, 16 threads,
a machine shared with other jobs at load averages of 8 to 17, so upper
bounds):

| Preset | 1M triangles | 10M triangles | Live preset, 1M / 10M |
|---|---|---|---|
| sky-view factor | 4.2 s | 37–40 s | 12.5 s / 163 s |
| positive openness | 5.0 s | 47–49 s | 15–19 s / ~210 s |
| negative openness | 5.0–5.4 s | 52 s | 15–19 s / ~210 s |
| cumulative viewshed, 100 observers | 3.6 s | 49–65 s | 11 s / 229 s |

Fixed cost, separately (the cumulative viewshed with no ray cast): at 10M,
8.4 s to read the mesh and build the sample object of 10M faces (3.9 s for
the horizon presets' 5M vertices), 6.8 s for Cycles to sync the scene and
build its BVH, and 3 s to drape the observers (the live preset's own Drape
group, one BVH build); at 1M, 1.5 s in all. Beyond that, the horizon presets
cast 640M rays at 10M in 31 to 41 s (14 to 20 Mrays/s with the shader's
bisection arithmetic), and the viewshed 1,000M rays in 29 to 39 s (25 to
35 Mrays/s): the six shader nodes per observer cost more than the traversal.
More observers cost about 0.3 to 0.4 s per observer at 10M; the bake takes
1,024 observers at a time, each further bake paying the fixed cost again.

## The library

- `geotools/assets/analysis_presets.blend` holds every group; it is built by
  `scripts/make_analysis_presets.py` from the catalog in
  `geotools/analysis/presets.py` (names, versions, inputs, defaults and the
  layer each makes). Never edit the .blend by hand:

  ```sh
  blender --background --factory-startup --python scripts/make_analysis_presets.py
  ```

  `-- --check` compares a fresh build with the file, node by node, and fails
  when they differ. The Blender tests run it, so a change to the script or the
  catalog without regenerating fails CI.

- Each group carries `geotools_preset` and `geotools_preset_version` custom
  properties. Add Analysis finds an existing group by them (so an edited or
  renamed group is reused, never duplicated) and merges the helper groups
  appended with a preset into the ones the file already has.
- Blender 5.2 has no API for an extension to register an asset library
  without writing the user's preferences (which would outlive the extension),
  so GeoTools appends from the bundled file directly. The presets are also
  marked as assets in one catalog per category (*GeoTools/Terrain Shape*,
  *GeoTools/Archaeological Visualisation*, *GeoTools/Visibility*,
  *GeoTools/Change and Distance*, *GeoTools/Volumes* and *GeoTools/Paths and
  Flow*; `blender_assets.cats.txt` beside the file): add the extension's `assets` folder as an asset library in the
  preferences to drag them from the Asset Browser.
- A bumped preset version is appended alongside an older one already in a
  file; existing modifiers keep the old group.

Seven helper groups are shared: **Window Heights** (eight vertical rays around
a point), **Surface Derivatives** (the Evans-Young derivatives from them),
**Mean Rise** (mean height over an annulus from a Repeat zone of vertical
rays), **Horizon** (horizon angles by bisecting with rays, in nested Repeat
zones), **Object Points** (an object's vertices or points, or its origin),
**Sight Target** (the surface joined with an Occluders collection) and
**Drape** (a position dropped onto the topmost surface by a vertical ray from
above its bounding box). Repeat zones keep their running values in temporary
attributes named `.gt_tmp_*`, removed before the group ends.

## Conventions

- Scene units are metres, +Z is up, and +Y is north (grid north for a CRS
  project; see [m5-analysis.md](design/m5-analysis.md#north-and-convergence)).
- Every layer is FLOAT. No data is NaN, which the ramps show in the no-data
  colour.
- **FACE** presets work from face normals and areas, on any mesh, including
  walls and overhangs.
- **CORNER** presets sample the surface around each vertex, then store the
  vertex value on its corners, so the colours are smooth across faces. The
  sampling is computed once per vertex (half as many as triangles on a
  photogrammetry mesh), never per corner.
- **2.5D sampling.** Window Heights and Mean Rise read neighbour heights with
  vertical rays started `Search Height` above the point and running twice as
  far down: they see the topmost surface within that band. On terrain this is
  the height field a GIS uses; under an overhang or on a vertical wall the
  neighbours are taken from the top surface, and the values mean little there.
- **Edges.** A window or horizon that leaves the mesh sees nothing there.
  Window presets give no data within one window of the boundary; disc and
  annulus presets use the samples that hit; horizon presets see an open sky
  beyond the edge, so openness rises (to about 160°) within Max Distance of
  the boundary.

- **Object inputs** are read in the space of the object the modifier is on
  (Object Info, Relative), so their transforms count. Heights (Observer
  Height, Target Height) and distances are along that object's axes: the
  scene's for an unrotated, unscaled mesh, as imported meshes are.

Defaults suit a photogrammetry site mesh at 1–10 cm spacing.

## Terrain shape

### Slope (`slope`, FACE, deg) and Slope (Percent) (`slope_percent`, FACE, %)

`slope = acos(n_z)` for the unit face normal n: 0 level, 90 vertical, above 90
overhanging. `slope_percent = 100 · √(n_x² + n_y²) / n_z`, no data where
`n_z ≤ 10⁻⁶` (vertical and overhanging faces).

| Input | Default | |
|---|---|---|
| Smoothing | 0 | Blur Attribute iterations of the face normals over neighbouring faces before measuring. Each iteration averages a face with its edge neighbours; use a few to calm photogrammetry noise. |

Display range 0–90° and 0–100 %. Aggregation: area mean.

### Aspect (`aspect`, FACE, deg)

The compass direction a face faces (its downslope direction), clockwise from
north: `aspect = (atan2(n_x, n_y) + Convergence) mod 360`. No data where the
slope is below Flat Slope.

| Input | Default | |
|---|---|---|
| Convergence | 0 | Degrees from true north to +Y, clockwise. 0 measures from +Y (grid north). |
| Flat Slope | 0.5° | Faces flatter than this have no aspect. |
| Smoothing | 0 | As for slope. |

**Aspect (True North)** in the menu fills Convergence for each object from
`analysis.site` at the object's bounding-box centre (`true = grid +
convergence`): PROJ's meridian convergence for a CRS project (needs pyproj),
minus the heading for an anchor project, and 0 for a local project (+Y is
taken as true north). The layer's provenance records `"north": "true"` and
the value used. Aggregation is **none**: aspect is circular, and an area mean
of 350° and 10° is not 0°.

### Curvature: profile, plan (tangential) and mean (`curvature_profile`, `curvature_plan`, `curvature_mean`, CORNER, 1/m)

From a 3 × 3 window of heights at spacing w around each vertex (eight vertical
rays; the centre is the vertex), the Evans-Young derivatives (Florinsky 2016,
eq. 4.4–4.8):

```
p = (z3 + z6 + z9 − z1 − z4 − z7) / 6w          (∂z/∂x)
q = (z1 + z2 + z3 − z7 − z8 − z9) / 6w          (∂z/∂y)
r = (z1 + z3 + z4 + z6 + z7 + z9 − 2(z2 + z5 + z8)) / 3w²
t = (z1 + z2 + z3 + z7 + z8 + z9 − 2(z4 + z5 + z6)) / 3w²
s = (z3 + z7 − z1 − z9) / 4w²
```

(z1 is north-west, z3 north-east, z7 south-west.) With g = p² + q²:

- **profile** (vertical) curvature `k_v = −(p²r + 2pqs + q²t) / (g (1 + g)^{3/2})`:
  the normal curvature along the slope line;
- **plan (tangential)** curvature is the horizontal (tangential) curvature
  `k_h = −(q²r − 2pqs + p²t) / (g √(1 + g))`: the normal curvature along the
  contour. It is the contour curvature times sin(slope), so it stays bounded on
  gentle ground, where the contour curvature diverges;
- **mean** curvature `H = −((1 + q²)r − 2pqs + (1 + p²)t) / (2 (1 + g)^{3/2})`.

**Positive is convex** (crests, spurs, breaks of slope), negative concave
(hollows, ditches, feet of slope); a sphere of radius R gives +1/R for all
three. Profile and plan curvature are undefined on exactly level ground (no
data where g < 10⁻¹²).

**Names in other tools.** "Plan curvature" means different things in
different tools, so the preset is labelled **Plan (Tangential) Curvature**
(its id and layer stay `curvature_plan`). It equals WhiteboxTools'
`TangentialCurvature` and SAGA's *Tangential Curvature* (Florinsky's
*horizontal* curvature). WhiteboxTools' `PlanCurvature`, SAGA's *Plan
Curvature* and Shary and Florinsky's *plan curvature* are the **contour
curvature**, k_h / sin(slope): it diverges on gentle ground, so it differs
from this preset most where the ground is nearly level. Compare with the
tangential output of those tools. Sign and unit agree: positive convex, 1/m.

**Window and mesh spacing.** The window's rays read the mesh, which is flat
between vertices. Where they land on vertices (a regular grid with w a whole
number of spacings), the result is the Evans-Young curvature of the vertices
themselves, with no added bias. Where they land mid-facet, the facets' chords
cut inside the curved surface and the curvature is overstated by up to
(h/w)²/4, for mesh spacing h: measured +4.0% at w = 2.5 h on a sphere, a bowl
and a paraboloid. On an irregular photogrammetry mesh the rays land anywhere,
so expect up to that bias: 4% at w = 2.5 h, 1% at 5 h, 0.25% at 10 h. In
practice set Window to at least five times the mesh spacing, and choose it
for the scale of the landform you want, not the finest the mesh allows.

**Heights far from 0 (float32).** Blender stores heights in float32, and the
window divides small height differences by w², so the rounding of large
heights shows in the curvature. On a sphere of R = 5 m (spacing 0.05 m):

| Heights near | Window 0.05 m: max (RMS) error | Window 0.2 m: max error |
|---|---|---|
| 5 m | 0.09% (0.03%) | 0.10% |
| 105 m | 1.3% (0.4%) | 0.17% |
| 1,005 m | 9.5% (3.6%) | 0.6% |

The relative error grows as about 3×10⁻⁸ |z| / (w² |k|) (z in m, w in m, k in
1/m). Keep the scene's heights near 0, with the project's **Offset A**
carrying the absolute height ([Projects](guide/projects.md#heights-a-and-the-height-offset)),
or widen the window.

| Input | Default | |
|---|---|---|
| Window | 0.1 m | Window spacing w (the window is 2w wide): the scale of the curvature. |
| Search Height | 2 m | See 2.5D sampling. |

Display: diverging ramp, automatic range. Aggregation: area mean.

### Rugosity (`rugosity`, FACE, m²/m²)

Surface area over planimetric area in a neighbourhood:
`blur(A) / blur(A |n_z|)` for the face areas A, with the same Blur Attribute
neighbourhood for both (a weighted Σ A / Σ A_plan). 1 on level ground,
1/cos(slope) on a plane, higher where the surface is rough.

| Input | Default | |
|---|---|---|
| Radius | 0.1 m | Blur iterations are `ceil(0.75 R² / mean face area)`: repeated averaging over edge neighbours spreads like a Gaussian of σ ≈ R/2. |
| Max Iterations | 1000 | Upper limit, so a large radius on a fine mesh stays bounded (the neighbourhood is then smaller than asked). |

The neighbourhood is topological (faces sharing edges), so it follows walls
and overhangs. Display range 1–2.

### Topographic Position Index (`tpi`, CORNER, m)

`TPI = z − mean(z_i)` over Samples heights in an annulus Inner–Outer Radius
around the vertex (Weiss 2001; Guisan et al. 1999). Samples lie on a
sunflower pattern of even areal density, in point-symmetric pairs, so a plane
gives exactly 0. Positive on rises and crests, negative in hollows. No data
where no sample hit.

Weiss's TPI, as other tools compute it, subtracts the mean over every cell of
the neighbourhood; the preset samples it. The default 96 samples give that mean
to about 2% RMS of the TPI on smooth terrain (measured against the exact disc
and annulus mean: 1.7%; 24 samples gave 7.6%, 48 gave 3.3%). Fewer samples are
proportionally faster and noisier. Use a radius of several mesh spacings: the
mesh's linear interpolation adds about 2% at five spacings and is as large as
the TPI itself below two. `gdaldem TPI` uses the eight neighbours of a 3 × 3
window, which matches a disc of radius √3 cells (the same second moment).

| Input | Default | |
|---|---|---|
| Inner Radius | 0 m | 0 for a disc. |
| Outer Radius | 0.5 m | |
| Samples | 96 | Rounded up to an even number. Was 24 up to GeoTools 0.3; a file that already holds that TPI group keeps its default of 24 for new layers. |
| Search Height | 2 m | See 2.5D sampling. |

### Terrain Ruggedness Index (`tri`, CORNER, m)

Riley et al. (1999), as `gdaldem TRI` computes it for land:
`TRI = √Σ (z_i − z)²` over the eight neighbours of a 3 × 3 window of spacing
Window (default 0.05 m). On a plane of slope β it is `w tan β √6`. No data
within one window of the edge.

## Archaeological visualisation

### Local Relief Model (`local_relief`, CORNER, m)

`LRM = z − mean(z_i)` over Samples heights in a disc of radius Radius (default
2 m, 48 samples): the surface minus a low-pass of itself, which keeps banks,
ditches and plough ridges smaller than the radius and removes the landform.
This is the trend-removal step of Hesse's LRM (2010). The full method then
extracts the zero contours of that difference, interpolates a "purged" surface
from the heights along them, and subtracts that; contour extraction and
scattered-data interpolation have no clean stock-node form, so that step is
**not** done. Use a radius larger than the features of interest, and expect a
slight halo of the opposite sign around large features, which the purge step
would remove. A GeoTools-code path (numpy on a height raster from M8) or
RVT/QGIS through M8 can give the full LRM.

### Sky-View Factor (`sky_view_factor`, CORNER, ratio)

Zakšek et al. (2011): `SVF = 1 − (1/N) Σ sin γ_i`, with γ_i the horizon
elevation angle in each of N directions within Max Distance, taken as 0 when
the horizon is below level. 1 in the open, lower in ditches and against
walls; display range 0.6–1, greys.

**Horizon search.** From each vertex, offset Ray Offset along its normal, a
ray at elevation θ hits the surface within Max Distance if and only if the
horizon in that direction is at least θ (for a height field). Each direction
first casts a level ray (θ = 0; a miss means the horizon is at or below
level, exactly) and then bisects: Steps rays in all, resolving γ to
`90° / 2^(Steps − 1)` (0.7° at the default 8), and the midpoint of the last
interval is used. Raycast hits back faces too, so thin walls occlude.

**Search region.** Max Distance is measured along each ray, so the search
reaches a sphere around the ray's start: a horizon point at elevation γ counts
if it is within Max Distance in a straight line, so at most Max Distance × cos γ
away horizontally. RVT (the Relief Visualization Toolbox) measures its search
radius horizontally and skips the nearest cell (its noise radius is at least
one pixel). So with the same number GeoTools searches slightly less far on
steep ground, but also looks at the closest surface, which RVT leaves out. On
the validation terrain the two definitions differ by 0.004 in SVF and 0.3° in
openness ([validation](validation/terrain.md#sky-view-factor-and-openness)).

| Input | Default | |
|---|---|---|
| Max Distance | 2 m | Length of each ray, measured along it from the ray's start. |
| Directions | 16 | Azimuths from +Y, evenly spaced. |
| Steps | 8 | Rays per direction. |
| Ray Offset | 0.002 m | Start of each ray from the surface, along the vertex normal. |

On a large mesh use the [fast path](#fast-bake-paths): the same search in a
Cycles bake, about four times faster.

### Positive and Negative Openness (`openness_positive`, `openness_negative`, CORNER, deg)

Yokoyama et al. (2002): positive openness `Φ = 90° − (1/N) Σ β_i`, with β_i
the highest elevation angle of the surface within Max Distance in direction
i; negative openness `Ψ = 90° + (1/N) Σ β′_i`, with β′_i the lowest. Both are
90° on a plane. Positive openness is high on crests and low in hollows;
negative openness the reverse. The search is the sky-view factor's over
−90° to 90° (the first ray, θ = 0, is the midpoint); negative openness starts
its rays Ray Offset *below* the surface, where a ray crosses the surface
within Max Distance if and only if the lowest surface point is at or below θ.
Same inputs and defaults; display range 60–120°. Both have a
[fast path](#fast-bake-paths).

## Visibility

**Geometry Nodes Raycast, not a bake.** Every visibility preset casts one ray
per element per observer, from the element to the eye. For one observer that
is one ray per face: 1M rays on a 1M-triangle mesh, which Geometry Nodes casts,
BVH build included, in 0.7 s (7.7 s at 10M). The roadmap's other
candidate, a point light at the observer and a shadow bake, pays a Cycles
bake's fixed cost before casting a ray: measured on this machine for sun hours
([m5-analysis.md](design/m5-analysis.md#visibility-cycles-raycast-nodes-in-a-bake)),
about 1.3 s per bake at 1M triangles and 15–20 s at 10M, plus about 1 s (1M)
and 7.7 s (10M) to build the sample object that carries face values, and the
values must then be written back as a layer by GeoTools code. So a bake is
slower for one observer at both sizes, and is not an editable preset. A
cumulative viewshed casts observers × faces rays: here Geometry Nodes ran at
about 4.5 Mrays/s on 10M faces (10 on 1M), the sun-hours Cycles probe at
15–25 Mrays/s, so on a 10M mesh a bake would overtake Geometry Nodes beyond
about 15–20 observers (see Performance). All four presets use Geometry Nodes
Raycast; the cumulative viewshed also has a [fast path](#fast-bake-paths)
that casts the same sight lines in a bake, for many observers.

Common rules:

- The **eye** is an observer point plus **Observer Height** (along +Z).
- A ray **hits back faces too**, so a thin wall blocks sight from either side.
- **Occluders**: an optional collection of other objects that block sight as
  well as the surface (buildings, vegetation models). It must not contain the
  object the modifier is on (Blender reports a dependency cycle).
- **Ray Offset** (default 1 cm): rays start this far off the surface, along
  the face normal (viewshed) or along the sight line (intervisibility), so the
  surface does not block itself. A surface closer than this to an occluder may
  see past it at the joint.
- Earth curvature and refraction are not applied: over 1 km they lower a
  distant point by about 7 cm (k = 0.13), which matters for landscape
  viewsheds, not for a site.

### Viewshed (`viewshed`, FACE, ratio)

1 where the face can be seen from the eye, 0 where it cannot, no data beyond
Max Distance. A face is tested at its centre, moved Ray Offset along its normal
and raised by Target Height: it is seen when nothing lies on the segment from
there to the eye and, with Target Height 0, its front faces the eye. So a
face is all seen or all hidden: the edge of a shadow is resolved to the face
size. Aggregation area mean (the share of the area that is seen); display
0–1, greys.

| Input | Default | |
|---|---|---|
| Observer | (filled) | The observer: this object's origin (a selected Empty, or a new one at the 3D cursor). Move it and the viewshed follows. |
| Observer Height | 1.6 m | Eye above the observer point. |
| Target Height | 0 m | Height above each face that must be seen (for example 1.7 m: could a person standing here be seen). Above 0, faces that face away count too. |
| Max Distance | 1000 m | Horizontal reach; faces farther away are no data. |
| Ray Offset | 0.01 m | See above. |
| Occluders | none | See above. |

### Cumulative Viewshed (`viewshed_cumulative`, FACE, count)

How many observers see each face: the viewshed test once per vertex of the
**Observers** object (a Repeat zone, one iteration per observer), counted. A
face beyond an observer's Max Distance counts 0 for it (no no-data). Use it
for prominence and exposure studies (how many of the barrows see this spot)
or, with one observer per point along a route, for visibility from a path.

Inputs as viewshed, with **Observers** (this object's vertices, or its origin
if it has none) in place of Observer, and **Drape**: drop the observer points
onto the topmost surface first, for 2D points (vector points imported at
height 0). Drape takes the topmost surface under the point, so a point inside
a building would land on its roof. Cost: observers × faces rays. For more than
a few dozen observers on a large mesh, use the [fast path](#fast-bake-paths):
100 observers on 10M triangles in about a minute rather than four.

### Intervisibility (`intervisibility`, lines)

A line between every pair of points, eye to eye: for N points, N(N − 1)/2
lines. Each point is raised by **Height** (after Drape, if on); a line is
visible when nothing lies on it between Ray Offset from either end.

| Attribute | Domain | |
|---|---|---|
| `visible` | EDGE | 1 visible, 0 blocked |
| `distance` | EDGE | Eye-to-eye length, m |

Inputs: **Points**, **Surface** (the active mesh), **Height** (1.6 m),
**Ray Offset**, **Drape** and **Occluders**. The group builds all N² ordered
pairs and keeps i < j, so it suits up to a few thousand points.

### Line of Sight (`line_of_sight`, lines)

From an observer at a line's first vertex to a target at its last:

- **The ground profile.** The line resampled every **Spacing** (Resample
  Curve) and draped onto the topmost surface (points off the surface are no
  data). Each profile point, raised by Target Height, is seen from the eye or
  not; an edge of the profile holds the mean of its two ends (1 seen, 0
  hidden, 0.5 where visibility changes).
- **The sight line** from the eye (the first profile point plus Observer
  Height) to the target (the last plus Target Height), split at the first
  obstruction: eye to obstruction `visible` 1, obstruction to target 0. An
  unobstructed sight line is one edge, visible 1.
- **The obstruction**, a loose vertex at the first point where the sight line
  meets the surface, with `obstruction` 1; it publishes as a point, and Read
  Totals reports where it is.

| Attribute | Domain | |
|---|---|---|
| `visible` | EDGE | As above |
| `part` | EDGE | 0 on the ground profile, 1 on the sight line |
| `obstruction` | POINT | 1 on the obstruction vertex |

Inputs: **Line** (a mesh with edges, such as an imported vector line, or a
curve; the observer is the first point of its first part and the target the
last point of its last), **Surface**, **Observer Height** (1.6 m), **Target
Height** (0 m), **Spacing** (0.5 m), **Ray Offset** and **Occluders**.

## Change and distance

### Distance to Surface (`surface_distance`, CORNER, m)

The signed distance from each vertex p to the nearest point q of the
**Reference** surface (Geometry Proximity to its faces):
`d = sign((p − q) · n_q) |p − q|`, with n_q the normal of the reference face q
lies on (Sample Nearest Surface). Positive on the side the reference faces
(above, for terrain), negative behind it. For two surveys, add it to the later
one with the earlier as Reference: positive is gain (deposition, growth,
backfill), negative loss (erosion, excavation, collapse). Display diverging.

This is the cloud-to-mesh distance (CloudCompare's C2M) at the vertices: the
nearest distance, not along a fixed direction, so on steep ground it is
shorter than the vertical difference cut and fill measures. It is not M3C2
(Lague et al. 2013):
there is no normal-scale averaging and no confidence interval; for that,
compare in CloudCompare and import the result (M8). Beyond the reference's
edge the nearest point is on its boundary and the distance means little:
**Max Distance** (default 1 m) makes anything farther no data, but within Max
Distance of the edge the values are still to the edge, so trim the meshes to
their common footprint for a change map.

| Input | Default | |
|---|---|---|
| Reference | (filled) | The surface measured to (another selected mesh). |
| Max Distance | 1 m | Farther than this is no data. |

### Distance to Feature (`feature_distance`, CORNER, m)

The distance from each vertex to the nearest face, edge or vertex of the
**Feature** object: polygons, lines and points (curves become edges, point
clouds vertices), such as vector features imported with M8. With
**Horizontal** (default) both are flattened to z = 0 first: the plan distance
a GIS buffer uses, 0 inside a polygon, whatever the features' heights (2D
imports sit at height 0). Off, it is the 3D distance.

| Input | Default | |
|---|---|---|
| Feature | (filled) | Another selected mesh, curve or point cloud. |
| Horizontal | on | Plan distance; off: 3D. |

## Volumes

### Cut and Fill (`cut_fill`, FACE, m)

The vertical depth of each face centre above the **Reference** surface:
`d = z_c − z_ref(x_c, y_c)`, with z_ref the topmost reference surface at the
centre's x and y (a vertical ray from above the reference's bounding box). No
data where the reference is not vertically below or above the centre. Add it to
the new surface (the design, or the later survey) with the existing ground as
Reference: positive is fill, negative cut.

Totals, over the faces with a reference: **fill** `Σ max(d, 0) A n_z`, **cut**
`Σ max(−d, 0) A n_z`, and net, with `A n_z` the face's plan area (area times
the z of its unit normal). This is exact for planar faces over a reference
that is planar beneath each face (a linear function's mean over a polygon is
its value at the centroid); for a curved reference the error falls with the
square of the face size.

**The footprint, honestly.** Only where this mesh has faces and the reference
lies vertically under or over their centres. The totals line gives the plan
area covered and the plan area outside the reference. Parts of the reference
outside this mesh are not counted: add the preset to the mesh that covers the
whole works, or trim both to the same boundary. It is 2.5D: vertical faces
(n_z = 0) add nothing and overhangs (n_z < 0) count negatively, so the net
volume of a folded surface is right (the divergence theorem) but the split
into cut and fill is by face. Display diverging, automatic range.

| Input | Default | |
|---|---|---|
| Reference | (filled) | The surface compared with (another selected mesh). |

### Closed Volume (`closed_volume`, FACE, m³)

The volume of each closed part of the mesh (each Mesh Island), stored on every
face of that part:
`V = (1/3) Σ ((c_f − c̄) · n_f) A_f`, over the part's faces f with centre c_f,
unit normal n_f and area A_f (the divergence theorem). Any point c̄ gives the
same sum for a closed part; the mean of the part's face centres keeps float32
exact far from the origin. It is exact for planar faces (p · n is constant
over a planar face); a quad that is not planar uses Blender's face normal and
area, so triangulate first. A part is **closed** when every one of its edges
has exactly two faces; open or non-manifold parts are no data (merge by
distance first if the walls, top and bottom of a context volume are separate).
A negative volume means the normals point inwards (Recalculate Outside).
Overlapping parts are counted twice. Aggregation none (a part's volume is not
an area mean). The modifier shows the total of the closed parts and how many
parts are closed and open; Read Totals sums it in binary64.

Context volumes from the drape recipe ([drape-contexts.md](recipes/drape-contexts.md))
are one closed part per extruded polygon.

## Paths and flow

### Least-Cost Path (`least_cost_path`, lines)

The quickest walking route along the surface's edges from **Start** to
**End**, by Tobler's hiking function (Tobler 1993):
`v = 6 exp(−3.5 |S + 0.05|)` km/h, S = dz/dx in the walking direction, and
time = horizontal run / v. Fastest at 5 % downhill (6 km/h), 5.04 km/h on the
level, 0.67 km/h up 30°.

- **The route**: Shortest Edge Paths (Dijkstra over the edges) from the
  surface vertex nearest Start to the vertex nearest End. The node takes one
  cost per edge, used both ways, so each edge costs the mean of Tobler's time
  up and down it, times its cost factor. Edges steeper than **Max Slope**, or
  whose cost factor is 0 or no data, are removed (impassable).
- **The time** (`elapsed` at each point from the start, `time` per edge, and
  the total) is Tobler's in the direction walked along that route, times the
  cost factor. Where uphill and downhill times differ a lot the route may not
  be the one-way fastest, but its time is its own.
- **Cost Attribute**: the name of a FACE attribute, such as a layer's
  `gt_<name>`, whose values multiply the walking time (2 for dense scrub, 0 for
  water or an open trench). Face values are averaged onto vertices and an edge
  takes the mean of its two ends. Empty: no factor.

The modifier shows "Walking time … min over … m", or that no walkable path
exists. **Paths follow mesh edges**, so on a regular grid they zig-zag: a route
between the directions of the grid's edges is up to √2 longer than straight
on a triangulated grid (1.08 with both diagonals). Irregular photogrammetry
meshes have edges in every direction and are far less biased. The zig-zag
shows as extra length and time, not hidden. Landscape-scale cost surfaces
belong in a GIS (QGIS r.walk, imported through M8).

| Input | Default | |
|---|---|---|
| Surface | (filled) | The surface walked on: the active mesh. |
| Start, End | (filled) | Selected Empties (by name), or new ones at the 3D cursor; move them to change the route. |
| Max Slope | 30° | Steeper edges cannot be walked. |
| Cost Attribute | (empty) | See above. |

| Attribute | Domain | |
|---|---|---|
| `elapsed` | POINT | Seconds from the start |
| `time` | EDGE | Seconds along the edge |

### Flow Path (Quick Look) (`flow_path`, lines)

**A quick look, not hydrology.** From each seed, draped onto the topmost
surface, a Repeat zone steps **Step** horizontally down the steepest slope of
the face below (the horizontal part of its normal points downhill), drops onto
the surface again, and goes on while the new point is lower, the slope is
above **Flat Slope** and there is surface below. A path stops in a pit, on a
flat, at the edge, after **Max Steps**, or where a step would not go lower (a
valley narrower than Step: a path crossing a valley floor zig-zags down it or
stops).

There is no flow accumulation, no depression filling, no spreading on flats,
and it is 2.5D (the topmost surface). For real 3D surface hydrology on the
mesh's connectivity (flow paths, accumulation and ponding, with walls and
overhangs), use GeoTools' hydrology tool (M5, a separate GeoTools tool).

| Input | Default | |
|---|---|---|
| Surface | (filled) | The active mesh. |
| Seeds | (filled) | This object's vertices (or its origin): a selected mesh of points, or a new one-vertex mesh at the 3D cursor. |
| Step | 0.1 m | Horizontal length of each step. |
| Max Steps | 1000 | |
| Flat Slope | 0.5° | Paths stop on flatter ground. |

| Attribute | Domain | |
|---|---|---|
| `step` | POINT | Steps from the seed (0 at the seed) |
| `seed` | EDGE | Index of the seed vertex |

## Accuracy

Blender tests (`tests/blender/test_analysis_presets.py`), on analytic surfaces:

| Surface | Preset | Expected | Result |
|---|---|---|---|
| plane tilted 30°, rising north | slope | 30° | 30.000 ± 0.0001° |
| | slope percent | 57.735 % | to 1×10⁻⁵ relative |
| | aspect | 180° (grid), 170° (anchor heading 10°, true), 181.50° (MGA56 west edge, true) | to 0.001° |
| | rugosity | 1/cos 30° = 1.1547 | to 1×10⁻⁶ |
| | TRI (w = 0.05) | w tan 30° √6 = 0.07071 m | to 1×10⁻⁴ relative |
| | TPI, LRM, mean curvature | 0 | below 10⁻⁵ m, 10⁻³ /m |
| sphere R = 5 m, 1 cm mesh | profile, plan, mean curvature | +0.2 /m | 0.2000 ± 0.0002 |
| bowl R = 5 m | profile, plan, mean curvature | −0.2 /m | −0.2000 ± 0.0002 |
| level plane | sky-view factor | 1 | 1 exactly |
| | positive, negative openness | 90° | 90.35° (the Steps resolution, 0.70°, halved) |
| V-trench, walls 30° | SVF at the bottom | 1 − mean sin(atan(\|sin φ\| tan 30°)) = 0.6714 | 0.6737 |
| | positive openness at the bottom | 70.56° | 70.75° |
| | negative openness at the bottom | 109.44° | 109.51° |

Second batch (`tests/blender/test_analysis_presets_2.py`):

| Case | Preset | Expected | Result |
|---|---|---|---|
| 20 m plane at 0.5 m faces, a 6 × 0.4 × 1 m wall, eye 1.6 m up 2 m in front | viewshed | hidden exactly where the segment from the face centre to the eye meets the wall (slab test, binary64) | every ground face outside the wall agrees; no face centre within 1 mm of a shadow edge; wall front and top seen, back hidden |
| same, three observers | cumulative viewshed | sum of the three analytic viewsheds (1, 2 and 3 all occur) | equal on every ground face |
| same, four points at 0.5 m | intervisibility | segment-box test of each of the 6 lines | equal; `distance` to 1×10⁻⁶ relative |
| same, a line from 1 m in front of the wall to 8 m behind it | line of sight | sight line meets the wall top at (2.375, 0, 1.0); ground seen in front, hidden behind to 8 m | obstruction within 10⁻⁴ m; profile as expected |
| plane and a copy 0.25 m below (then above) | distance to surface | +0.25 m (−0.25 m); 2 m: no data | ±0.25 to 10⁻⁶ m |
| plane 3 m up, a line feature | distance to feature | \|x\| in plan, √(x² + 9) in 3D, distance to a square in plan (0 inside) | to 10⁻⁵ m |
| 16 m plane at 5 cm, a Gaussian bump exp(−r²) and a pit −0.5 exp(−r²), over a 12 m reference | cut and fill | fill π(1 − erfc(2)/2) = 3.1343 m³ inside the reference (π for the whole bump), cut half that; 112 m² outside | within 0.1 % (and within 1 % of π) |
| a cube of side 2 at (1000, 2000, 50), a triangulated unit cube, an open box | closed volume | 8, 1, no data; total 9 m³ | 8 exactly, 1 to 10⁻⁶, no data; 9.000 |
| 20 m plane at 0.5 m, start and end 15 m apart on a row | least-cost path | straight, 15 m / 5.0367 km/h = 10.721 s | straight, 10.721 s (10⁻⁴ relative) |
| a 35° ridge from the west edge to x = 4 between start and end | least-cost path | round the east end with Max Slope 30°; over it, and quicker, with 40° | as expected; `elapsed` equals Tobler along the path to 10⁻⁵ |
| cone z = 5 − r at 0.2 m, 8 seeds at r = 1 | flow path | radial, down to the edge | within 3° of radial, r and z monotonic, to r > 9.5 m |

## Performance

### First batch

Measured with `scripts/bench_analysis_presets.py` (one evaluation of each
preset alone, with its defaults, on a synthetic triangulated terrain at 2 cm
spacing), Blender 5.2.4, 16 threads. The first ray-casting preset also builds
the mesh's BVH, which accounts for the spread of the 1M figures. Photogrammetry
meshes are triangles, so 1M faces is about 0.5M vertices.

| Preset | 1M triangles | 10M triangles | Cost per vertex |
|---|---|---|---|
| slope, slope percent, aspect | 0.02 s | 0.1 s | normals |
| rugosity (R = 0.1 m) | 0.2 s | 1.9 s | ~10 blur iterations |
| profile, plan, mean curvature | 0.5–1 s | 13 s | 8 rays |
| TRI | 0.5–1.3 s | ~13 s (est., as curvature) | 8 rays |
| TPI | 9 s | 76 s | 96 rays |
| local relief model | 4.7 s | 67 s | 48 rays |
| sky-view factor | 12.5 s | 163 s | 128 rays |
| positive, negative openness | 15–19 s | ~210 s (est.) | 128 rays |

Blender's raycast runs at about 4–5 million rays per second here, and the cost
is rays per vertex times vertices. A modifier evaluates when it is added, when
the mesh or an input changes and when the file is opened; otherwise its result
is cached.

The TPI row is the default of 96 samples, measured on 2026-11-14 on a
20-thread desktop while it was shared (load average 17–30; a second 1M run
took 16.6 s).
In the same runs 24 samples took 1.8–2.0 s at 1M and 25 s at 10M, and 48
samples 4 s at 1M: the time grows about in proportion to Samples.

**Not within the target at 10M.** Slope, aspect, rugosity, curvature and TRI
evaluate within a minute at 10M triangles. TPI takes about 76 s with its
default 96 samples (25 s with 24), and the local relief model about a minute
(67 s, or half that with 24 samples). The horizon presets (sky-view factor
and openness) take three to four minutes, past the M5 target of a minute per
tool. Directions 8 and Steps 6 cut that to about
a third, with a coarser result. Their [fast path](#fast-bake-paths), the same
search cast in a Cycles bake, takes 37 to 52 s at 10M triangles and is the
way for large sites.

### Second batch

`scripts/bench_analysis_presets.py` with synthetic inputs (`inputs()`): an
observer 1 m above the centre, 10 observers (Drape on), a reference terrain
5 cm above the first with as many triangles, a 100-vertex line feature, a line
across the terrain, start and end at opposite corners, 20 intervisibility
points and 100 flow seeds. Each preset ran alone in its own Blender (so each
pays for its own BVH), Blender 5.2.4, 16 threads, on a machine shared with
other jobs (load average 5 to 16 during the runs): treat these as upper
bounds.

| Preset | 1M triangles | 10M triangles | What it costs |
|---|---|---|---|
| viewshed | 0.7 s | 7.7 s | 1 ray per face, plus the BVH |
| cumulative viewshed, 10 observers | 1.3 s | 24 s | 1 ray per face per observer |
| cumulative viewshed, 100 observers | 11 s | 229 s | as above |
| intervisibility, 20 points (190 lines) | 0.3 s | 2.8 s | the surface's BVH; the rays are nothing |
| line of sight | 0.3 s | 2.8 s | as above |
| distance to surface | 0.5 s | 5.2 s | a proximity query and a nearest-surface sample per vertex, and the reference's BVH |
| distance to feature (100-vertex line) | 0.05 s | 0.6 s | proximity to a small feature |
| cut and fill | 1.2 s | 14 s | the reference's BVH, a vertical ray per face, four sums |
| closed volume | 0.7 s | 6.6 s | mesh islands, edge neighbours, accumulations |
| least-cost path | 0.3 s | 3.1 s | Dijkstra over every edge |
| flow path, 100 seeds | 0.3 s | 2.8 s | the surface's BVH, then a ray per seed per step |
| flow path, 1,000 seeds | | 2.9 s | as above |

At 10M triangles, about 2.8 s of every preset that reads the surface through
an object input is Blender building its BVH (intervisibility, line of sight
and flow paths cost little more). Every preset is within the M5 target of a
minute at 10M except **the cumulative viewshed beyond about 25 observers**:
about 2.2 s per observer at 10M faces, so 100 observers take four minutes.
Its [fast path](#fast-bake-paths) casts the same sight lines in a Cycles bake,
one Raycast node per observer: 100 observers on 10M faces in 49 to 65 s on
this shared machine (about 16 s of it fixed cost), 3.6 s at 1M.

## What stock nodes cannot do well

- **Directed least-cost routing.** Shortest Edge Paths takes one cost per
  edge, used both ways, so Tobler's asymmetry (downhill faster than uphill)
  cannot steer the route; the route uses the mean of the two directions and
  the time reported is the directional time along it. A directed graph search
  (numpy, in GeoTools code) or QGIS r.walk through M8 would give the true
  one-way route. A cost-distance layer (the time from every vertex to End) is
  possible in stock nodes: Shortest Edge Paths' Total Cost is exactly that
  (with the same symmetric cost), and could be a later preset.
- **Paths not tied to edges.** Graph paths on a mesh follow its edges (see
  Least-Cost Path); routing across faces needs a fast-marching solver, which
  has no stock-node form.
- **Hydrology.** Flow accumulation, depression filling and routing over flats
  need ordering by height and propagation along receivers (pointer jumping),
  which a Repeat zone can do only one step per iteration over the whole mesh:
  thousands of iterations on a real site. That is GeoTools' 3D hydrology
  tool's job; the flow-path preset stays a quick look.
- **Totals in binary64.** Attribute Statistic sums in float32 (about seven
  digits); Read Totals re-sums in binary64.
- **Sub-face visibility.** A face is seen or hidden by its centre; a finer
  mesh (Subdivide on a copy) gives finer edges.
- **Change detection with uncertainty (M3C2).** Averaging along local normals
  at a chosen scale, with confidence intervals, is possible in principle with
  Repeat zones but slow and fragile; CloudCompare through M8 is the better
  route.

## Planned presets (next batch)

- **Surface reading:** ambient occlusion and curvature bakes for worn
  surfaces, rock art and inscriptions.
- A cost-distance layer (see above).

## References

- Evans, I. S. (1979). An integrated system of terrain analysis and slope
  mapping. Final report, Durham University.
- Florinsky, I. V. (2016). *Digital Terrain Analysis in Soil Science and
  Geology*, 2nd ed. Academic Press.
- Hesse, R. (2010). LiDAR-derived Local Relief Models. *Archaeological
  Prospection* 17, 67–72.
- Lague, D., Brodu, N., Leroux, J. (2013). Accurate 3D comparison of complex
  topography with terrestrial laser scanner: application to the Rangitikei
  canyon (N-Z). *ISPRS Journal of Photogrammetry and Remote Sensing* 82,
  10–26.
- Riley, S. J., DeGloria, S. D., Elliot, R. (1999). A terrain ruggedness index
  that quantifies topographic heterogeneity. *Intermountain Journal of
  Sciences* 5, 23–27.
- Tobler, W. (1993). Three presentations on geographical analysis and
  modeling. Technical Report 93-1, NCGIA, University of California, Santa
  Barbara.
- Weiss, A. (2001). Topographic Position and Landforms Analysis. ESRI User
  Conference poster.
- Yokoyama, R., Shirasawa, M., Pike, R. J. (2002). Visualizing topography by
  openness. *Photogrammetric Engineering & Remote Sensing* 68, 257–265.
- Zakšek, K., Oštir, K., Kokalj, Ž. (2011). Sky-View Factor as a relief
  visualization technique. *Remote Sensing* 3, 398–415.
