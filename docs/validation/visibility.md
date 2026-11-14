# Validation: visibility, change, volumes and paths

Status: validated on `val/visibility` (2026-11-14), against GeoTools 0.4.0-dev
(develop at 3a90e20) in Blender 5.2.4.

This covers the Geometry Nodes presets of
[analysis-presets.md](../analysis-presets.md) under *Visibility*, *Change and
distance*, *Volumes* and the least-cost path of *Paths and flow*, and the fast
(bake) path of the cumulative viewshed. For each tool it gives the definition
followed, the reference tools, the cases, the tolerances, the results and a
verdict.

## Verdicts

| Tool | Definition followed | Verdict |
|---|---|---|
| Viewshed | exact line of sight over the mesh, from the eye to each face centre (+ Ray Offset along the normal, + Target Height); a face facing away is hidden when Target Height is 0 | **agrees**, with documented convention differences: no Earth curvature or refraction (GDAL applies them by default), and the 1 cm Ray Offset acts as a 1 cm target height |
| Cumulative Viewshed (live and fast) | the viewshed's test per observer, counted | **agrees**; the fast path equals the live preset on every face |
| Intervisibility | segment test eye to eye, Ray Offset from each end | **agrees** |
| Line of Sight | first hit of the sight line; profile seen or hidden | **agrees** (obstruction to 10⁻⁷ m) |
| Distance to Surface | CloudCompare's cloud-to-mesh distance (nearest point), signed by the normal of the nearest face | **agrees** on the distance; **open point** on the sign within a few cm of edges sharper than 90° ([below](#sign-at-sharp-edges)) |
| Distance to Feature | plan (2D) distance as a GIS buffer, 0 inside polygons; or 3D | **agrees** (GEOS via shapely, 5×10⁻⁷ m) |
| Cut and Fill | vertical depth at face centres × plan area, by face | **agrees**; the net is exact, cut and fill are split by face (documented) |
| Closed Volume | divergence theorem over each closed part | **bug fixed** ([commit](#bugs-fixed)): float32 lost 0.27 % 10 km from the origin; now within 10⁻⁵ of trimesh (4,096 faces) and 10⁻⁴ (262,144 faces); Read Totals exact |
| Least-Cost Path | Tobler (1993), Dijkstra over the mesh edges with the mean of both directions' times | **agrees** with networkx on the same graph (7×10⁻⁸); documented convention differences: symmetric routing (0.16 % slower than the one-way best here) and edge-graph bias (up to √2); GRASS r.walk uses a different walking model (Langmuir) |

## Method

### Cases

All inputs are synthetic, defined once in
[`scripts/validation/visibility/cases.py`](../../scripts/validation/visibility/cases.py)
(numpy only) and built from there both by the reference scripts and by the
Blender test. A DEM is a regular grid; as a mesh every cell is two triangles
split south-west to north-east; as a raster (for GDAL and GRASS) every grid
node is a cell centre.

| Case | What | Used for |
|---|---|---|
| ridge | a triangular ridge along y, crest x = 10 m, 1 m high, 2 m half-width, on level ground 60 × 30 m at 0.5 m (14,400 triangles); observer at the origin, eye 1.6 m | viewshed (Target Height 0 and 0.5 m), line of sight |
| dome | a hemisphere R = 5 m at (15, 0) on level ground 40 × 30 m at 0.25 m (38,400 triangles); eye on an 8 m tower at the origin | viewshed against the tangent cone |
| hills | eight Gaussians (2.5–8 m high, σ 3–7 m, slopes to 35°) on 64 × 64 m at 0.5 m (32,768 triangles) | viewshed, cumulative viewshed (4 observers), intervisibility (6 points), least-cost path; and again 10 km east and 20 km north of the origin |
| long | level ground 20 × 20 km at 100 m (80,000 triangles), a 10 m mast in the middle, Max Distance 20 km | Earth curvature |
| tilted | z = x tan 30°, and a copy 0.25 m higher (larger, so no edge is near) | distance to surface against cut and fill |
| sphere | a UV sphere R = 3 m (24 × 48) under a level grid 4 m up | distance to surface |
| gable, notch | a ridge (and a V-notch) with 60° faces, normals 120° apart; 161 points 0.3 m from the crest, within the wedge of the two faces' normals | the sign of distance to surface |
| features | an L-shaped polygon, a four-vertex line and three points at z = 0, a level grid 3 m up | distance to feature |
| solids | a triangular prism, a 64-gon pyramid and frustum, a hemisphere (32 × 64), at the origin and at (10000, 20000, 100); a hemisphere of 262,144 faces at the latter | closed volume |
| mound and pit | a cone R = 4 m, 2 m high, and a square frustum pit (top 8 m, bottom 4 m, 2 m deep) at 0.25 m | cut and fill |
| wave | 0.5 sin(πx/8) sin(πy/8) over [−8, 8]², at 0.25 m, over z = 0 and over the plane z = 0.1 + 0.02x | cut and fill |
| ramps, flat | planes at slopes −30° to 30°, level ground at 1 m | least-cost path cost model and bias |

### The exact line of sight

The visibility references are computed exactly over the same TIN, without
Blender (`cases.tin_margin`): a segment is checked against the TIN's height at
every point where it crosses a grid line or a cell diagonal. Between two such
crossings the segment and the TIN are both linear, so their difference is
extreme at the crossings, and the test is exact (binary64), with no sampling
step and no ray tracer. A sight line that passes within 1 mm of the TIN is
marked *grazing* and left out: there float32 may decide either way.

### Reference tools and versions

| Tool | Version | Used for |
|---|---|---|
| GDAL | 3.13.3 "Iowa City", Docker `ghcr.io/osgeo/gdal:ubuntu-small-3.13.3` (`sha256:64250faf833c06d4b21afce4c27190039ba7ab58d70f0eebc87cf77d929c0b40`) | `gdal_viewshed` (`-cc 0`, and the default 0.85714 for the long case) |
| GRASS GIS | 8.5.0, Docker `osgeo/grass-gis:8.5.0-alpine` (`sha256:12598317c743c5ec31ad252aedfb1c0ff5da757fffe428a7b48ce3f3debc3cdd`) | `r.viewshed -b` (and `-c -r` for the long case), `r.walk` |
| trimesh | 5.2.1 (with rtree 1.4.1, scipy 1.18.1) | closest points on a mesh, mesh volume |
| shapely | 2.2.0 (GEOS) | plan distances to features |
| networkx | 3.7 | Dijkstra on the mesh's edge graph |
| numpy | 2.5.4 | the exact line of sight, raster cut and fill, the cases |

The rasters carry EPSG:32633 (synthetic coordinates; GDAL and GRASS need a
projected CRS for curvature). Docker ran with `--cpus 3 --memory 6g`.

### Regenerating the references

From the repository root, with Docker (the Python dependencies are pinned in
each script's header, so `uv run` makes its own environment):

```sh
uv run scripts/validation/visibility/refs_visibility.py      # GDAL, GRASS, exact LOS; ~4 min
uv run scripts/validation/visibility/refs_distance_volume.py # trimesh, shapely; ~10 s
uv run scripts/validation/visibility/refs_paths.py           # networkx, GRASS r.walk; ~2 min
```

They write `tests/data/validation/visibility/` (`visibility.npz` and `.json`,
`distance_volume.*`, `paths.*`; 29 kB in all) and work in
`.scratch/validation/visibility/`. The Blender test
`tests/blender/test_validation_visibility.py` (16 tests, about 5 s) builds the
same cases, runs the presets with the operator as a user would, compares them
with the references, and prints the statistics quoted below (lines starting
`VALIDATION`):

```sh
python scripts/run_blender_tests.py --expect available -k validation_visibility
```

## Visibility

### Definitions

- **GeoTools**: the eye is the observer's origin plus Observer Height. A face
  is seen when nothing lies on the segment from its centre, moved Ray Offset
  (1 cm) along its normal and raised by Target Height, to the eye; with Target
  Height 0 it must also face the eye. The sight line is exact against the mesh
  (Blender's BVH raycast in float32).
- **GDAL `gdal_viewshed`**: the reference-plane algorithm of Wang, Robinson
  and White (2000) on the raster: approximate lines of sight to cell centres.
  It applies Earth curvature and refraction **by default**
  (`-cc 0.85714` = 1 − 1/7, for an Earth CRS).
- **GRASS `r.viewshed`**: the sweep algorithm of Haverkort et al. on the
  raster, with the terrain between cell centres interpolated; curvature (`-c`)
  and refraction (`-r`, coefficient 0.14286) are off by default.

GeoTools is compared with the exact reference face by face. Against the
rasters, each face takes the raster's value at the grid node nearest its
centre; the rasters are also compared with the exact reference at the nodes.

### Results

| Case | Faces | GeoTools vs exact (grazing left out) | Analytic | vs GDAL | vs GRASS |
|---|---|---|---|---|---|
| ridge, target 0 | 14,400 | **0 differ** (0 grazing) | shadow on level ground ends at 26.50 m (similar triangles, from 1 cm up; 26.67 m from the ground): last hidden centre 26.33 m, first seen 26.67 m | 99.17 % | 96.67 % |
| ridge, target 0.5 m | 14,400 | **0 differ** (60 grazing) | ends at 18.17 m: 17.83 / 18.17 m | 99.17 % | 98.17 % |
| dome, 8 m tower | 38,400 | **0 differ** (5 grazing) | true hemisphere (ground: segment within R of the centre; dome: outside the tangent cone), away from the rim and 0.1 m from the edge: **0 of 36,739 differ** | 99.41 % | 99.51 % |
| hills | 32,768 | **0 differ** (6 grazing); 31.9 % seen | | 98.93 % | 99.17 % |
| hills 10 km from the origin | 32,768 | 2 differ outside a 1 cm grazing band (69 within it) | | | |
| long, level, 10 m mast | 80,000 | **0 differ**; all seen | all seen without curvature | all seen (`-cc 0`) | all seen |

The rasters against the exact reference at the nodes:

| Case | GDAL | GRASS |
|---|---|---|
| ridge, target 0 | 100 % | 97.5 %: 182 nodes, up to 8 cells from the shadow edge |
| ridge, target 0.5 m | 100 % | 98.3 %: 126 nodes, up to 5 cells |
| dome | 99.54 %: 90 nodes, all within 1 cell of an edge | 99.88 %: 24 nodes, within 1.4 cells |
| hills | 99.28 %: 119 nodes, within 2 cells | 99.45 %: 92 nodes, within 2 cells |

Every difference between GeoTools and GDAL or GRASS is at a visibility edge:
a face is tested at its centre and a cell at its centre, which are different
points, and GDAL's algorithm approximates the sight lines. One difference is
GRASS's own: behind the ridge, where sight lines cross the crest obliquely
(more than about 7 m off the observer's axis), `r.viewshed` ends the shadow
up to 4 m early; the shadow's end does not depend on y (similar triangles in
the vertical plane of each sight line), and GDAL and the exact reference agree
on that.

The **cumulative viewshed** of the hills from four observers (counts 0–3 on
6,111, 20,544, 6,057 and 56 faces): the live preset and the fast (bake) path
both equal the exact counts on every face (20 grazing), and equal each other
on every face; against the sums of four GDAL and GRASS viewsheds, 97.1 % and
97.8 % of faces agree (the edges of four viewsheds). **Intervisibility** of
six points on the hills (15 pairs, 5 visible): equal to the exact test, and
to GDAL and GRASS (viewsheds with target height = eye height, read at the
other points), on every pair. **Line of sight** over the ridge to a target at
x = 20 m in its shadow: the obstruction at (9.65517, 0, 0.82759), the analytic
(9.655172, 0, 0.827586) to 10⁻⁷ m; the profile seen up to the crest and hidden
after it.

### Earth curvature and refraction

GeoTools applies neither, and says so. GDAL does by default; GRASS does with
`-c -r`. Both lower a target at distance d by (1 − k) d² / 2R (k = 1/7, the
refraction coefficient both use):

| d | 100 m | 500 m | 1 km | 2 km | 5 km | 10 km | 20 km |
|---|---|---|---|---|---|---|---|
| drop, k = 1/7 | 0.7 mm | 1.7 cm | 6.7 cm | 0.27 m | 1.68 m | 6.7 m | 26.9 m |

On the long case (level ground, eye 10 m up) the horizon is at
√(2R × 10 / (1 − k)) = 12.19 km: GDAL with its default hides everything past
12.27 km (the last cell seen; cells are 100 m), GRASS with `-c -r` past
12.23 km, and GeoTools sees all of it, as GDAL with `-cc 0` and GRASS without
`-c` do. For a site (hundreds of metres) the drop is millimetres to
centimetres, below the photogrammetry's own error; it matters for landscape
viewsheds beyond about 2 km, where a target 0.3 m below a ridge line of sight
can be hidden. analysis-presets.md states this ("over 1 km they lower a
distant point by about 7 cm"), and it is right. **Recommendation** (not done):
for landscape work a Curvature input (lowering each target by the drop above)
would match GDAL and GRASS; until then, use them through M8 for views over
kilometres.

### Ray Offset

The 1 cm Ray Offset lifts each face's sample point, so it acts as a 1 cm
target height: behind the ridge the shadow ends at 26.50 m rather than the
ground's 26.67 m. With target height t the shadow of a crest of height H at
distance D, seen from an eye h_e high, ends at D (h_e − t) / (h_e − H); the
offset's effect grows as the eye and the crest come closer in height.

## Change and distance

### Distance to Surface

The definition is CloudCompare's cloud-to-mesh (C2M) distance: the distance
to the nearest point of the reference, signed by the normal of the face that
point lies on. trimesh's `closest_point` is the reference.

| Case | Expected | Result |
|---|---|---|
| two planes at 30°, the reference 0.25 m higher | −0.25 cos 30° = −0.216506 m everywhere (nearest, not vertical) | to 2.9×10⁻⁷ m; Cut and Fill on the same pair gives the vertical −0.25 m |
| sphere R = 3 m (1,152 faces) under a grid 4 m up | trimesh's signed distance | to 4.1×10⁻⁷ m; to the true sphere within 1.07 cm (the facets lie inside it) |
| gable and notch, 60° faces, 161 points 0.3 m from the crest | 0.3 m, + above the gable, − below the notch | distance to 5×10⁻⁸ m; **sign wrong on 11 and 6 points** |

#### Sign at sharp edges

Where the nearest point is on an edge (or vertex) shared by faces whose
normals differ by more than 90°, the two faces are equally near, and the
sign depends on which one is picked: a point in the wedge between the two
normals is on the outer side of one face's plane and may be on the inner side
of the other's. GeoTools (Blender's Sample Nearest Surface) gave the wrong
sign for 11 of 161 points over the gable and 6 of 161 under the notch;
trimesh's nearest face does the same (12 and 65). The angle-weighted
pseudonormal (Bærentzen and Aanæs 2005), the sum of the faces' normals at an
edge, gives every sign right. This only happens within the wedge of an edge
sharper than 90° (the top of a thin wall, a knife-edge ridge, a deep notch),
and only for points nearer that edge than any face; on terrain and on most
change maps it never arises. CloudCompare's C2M signs by the nearest
triangle's normal too, so GeoTools matches the definition it cites.

**Decision needed**: keep it (documented, matches CloudCompare) or sign by a
smooth normal at the nearest point (Sample Nearest Surface of the vertex
normals, barycentric), which is right at edges and is a small change to the
preset. Recommendation: keep the definition and add one sentence to
analysis-presets.md; change it only if users report sign flips on walls.

### Distance to Feature

Plan distances from a level grid (1,089 vertices, 3 m up) to an L-shaped
polygon, a line and three points at z = 0 (one mesh: a face, edges and loose
vertices) equal GEOS's (shapely) to 4.8×10⁻⁷ m, and are 0 (to 5×10⁻⁷) on the
115 vertices inside the polygon. With Horizontal off, the 3D distances equal
√(d² + 3²) to 3.6×10⁻⁷ m. **Agrees.**

## Volumes

### Closed Volume

The divergence theorem, V = (1/3) Σ c_f · S_f, against trimesh's mesh volume
(the same formula as signed tetrahedra, binary64, on the float32 vertices
Blender stores) and the exact volume of each faceted solid:

| Solid | Faces | Exact (faceted) | At the origin | At (10000, 20000, 100), version 1 | Version 2 |
|---|---|---|---|---|---|
| triangular prism | 8 | 15 | 15 exactly | 1.2×10⁻⁶ | exact |
| pyramid, 64-gon | 128 | 12.546194 (π r² h/3 = 12.566) | 5.6×10⁻⁷ | 9.2×10⁻⁵ | exact |
| frustum, 64-gon | 256 | 14.637226 | 7.2×10⁻⁷ | 8.8×10⁻⁵ | 4.3×10⁻⁸ |
| hemisphere 32 × 64 | 4,096 | (2πR³/3 = 16.755) | 4.9×10⁻⁶ | 9.6×10⁻⁵ | 1.7×10⁻⁶ |
| hemisphere 256 × 512 | 262,144 | 16.754635 | | **2.7×10⁻³** | 9.2×10⁻⁵ |

(relative errors of the per-part value against trimesh; 10 km out, float32
rounds the vertices to 1–2 mm, and trimesh is given the same rounded vertices.)

Version 1 of the preset took c̄ as the float32 mean of the part's face centres
and used Blender's Normal and Face Area fields. Far from the origin, a sum of
hundreds of thousands of positions near 2×10⁴ m loses the mean, and the
Normal field lost precision on small faces (on the 262,144-face hemisphere it
differed from Blender's own exact face normals by up to 2.6×10⁻³ at the
origin, and was unusable 10 km out, while the area and position fields were
exact). Version 2 takes c̄ as the part's first vertex, subtracts it from every
vertex (exact in float32 for nearby vertices, Sterbenz's lemma) and builds
c_f and S_f (Newell's formula) from those; what is left is float32 summation
over the part's faces (Accumulate Field), 9×10⁻⁵ on 262,144 faces. Read
Totals now computes centres and area vectors from the vertices in binary64 and
is exact (it used Blender's float32 face centres: 2×10⁻⁵ there). The
modifier's own total (Attribute Statistic) is 2×10⁻⁵ out.

### Cut and Fill

Against the analytic volumes, the raster method (depth at each node × cell
area, numpy, at the mesh's spacing) and a fine numerical integral (1 cm):

| Case | GeoTools | Analytic | Raster at 0.25 m | Fine (1 cm) |
|---|---|---|---|---|
| cone mound, fill | 33.510 | π R² H / 3 = 33.5103 | 33.5103 | 33.5103 |
| square frustum pit, cut | 74.625 | h/3 (A₀ + A₁ + √(A₀A₁)) = 74.667 | 74.625 | 74.6666 |
| wave over z = 0: fill, cut, net | 25.897, 25.897, 0.000 | A (2L/π)² × 2 = 25.938 each, net 0 | 25.8966 | 25.9382 |
| wave over a tilted plane: fill, cut, net | 17.052, 42.652, −25.600 | net −0.1 × 256 = −25.6 | 17.102, 42.702 | 17.102, 42.702 |

The net is exact for a TIN over a planar reference (the plane case). The pit's
TIN cuts two of its four hips across the grid's diagonals, so GeoTools and the
raster both find 74.625 (−0.06 %). Over the curved wave the TIN is 0.16 % below
the continuous surface (O(h²)), as the raster at the same spacing is. Cut and
fill are split **by face**: a face that crosses the reference counts wholly as
cut or as fill, which lowers both totals by the same amount (0.29 % and 0.12 %
on the tilted plane, where the zero line crosses faces) and leaves the net
exact; analysis-presets.md says so. The same preset 10 km from the origin
gives the same totals to 10⁻³ m³ (its plan areas come from near-level faces,
which the Normal field keeps). **Agrees.**

## Least-Cost Path

### Definitions

- **GeoTools**: Tobler (1993), v = 6 exp(−3.5 |S + 0.05|) km/h with
  S = dz/dx along the edge and time = horizontal run / v; Dijkstra over the
  mesh's edges (Shortest Edge Paths), each edge costing the mean of its times
  both ways; edges steeper than Max Slope removed. `elapsed` is Tobler's
  time in the direction walked along the route found.
- **GRASS `r.walk`**: Aitken (1977) and Langmuir (1984), after Naismith:
  T = a ΔS + b ΔH↑ + c ΔH↓moderate + d ΔH↓steep, defaults 0.72 s/m, 6.0,
  1.9998, −1.9998, threshold tan 12°; Dijkstra over 8 (or 16, `-k`)
  neighbours of the raster, directed.

### Results

On the hills, from (−28, −28) to (28, 26), Max Slope 30° (1,770 of 49,408
edges too steep):

| | Time (s) |
|---|---|
| networkx, the same edge graph and symmetric cost: route cost | 72.04807 |
| GeoTools route, its symmetric cost | 72.04807 (7×10⁻⁸) |
| GeoTools `elapsed`, walked start to end | 71.866 (networkx along its route: 71.866, 4×10⁻⁷) |
| networkx, directed Tobler: the one-way quickest route | 71.754 (GeoTools' route is 0.16 % slower) |
| r.walk, 8 neighbours / 16 neighbours (Langmuir) | 72.47 / 69.91 |

GeoTools' route has the same cost as networkx's optimum (it differs at a few
vertices where two routes cost the same in float32). The symmetric cost is
the documented limit of Shortest Edge Paths: the route is optimal for the
mean of the two directions, and here 0.16 % slower than the true one-way
route; its `elapsed` is the true directional time along it.

Tobler against r.walk's model, on planar ramps (seconds per horizontal metre,
r.walk measured on 50 m ramps; it equals Langmuir's formula to 10⁻⁸ except
at −13°, below):

| Slope | Tobler (GeoTools) | r.walk (measured) | Langmuir, straight down the slope |
|---|---|---|---|
| −30° | 3.800 | 1.875 | 1.875 |
| −20° | 1.801 | 1.448 | 1.448 |
| −13° | 1.130 | 0.557 | 1.182 |
| −11° | 0.995 | 0.331 | 0.331 |
| −8° | 0.824 | 0.439 | 0.439 |
| −5 % (2.86°) | 0.600 | 0.620 | 0.620 |
| 0° | 0.715 | 0.720 | 0.720 |
| +5 % (2.86°) | 0.851 | 1.020 | 1.020 |
| +8° | 1.169 | 1.563 | 1.563 |
| +12° | 1.504 | 1.995 | 1.995 |
| +20° | 2.555 | 2.904 | 2.904 |
| +30° | 5.392 | 4.184 | 4.184 |

(At −13° r.walk walks the ramp diagonally: each diagonal step is a
"moderate" descent, cheaper than straight down the "steep" slope.)

On the level they agree within 0.7 % (5.04 against 5.00 km/h). Downhill,
r.walk is quicker beyond about −3° (Langmuir's descent bonus), and Tobler much
slower on steep descents (−30°: 3.80 against 1.87 s/m); uphill, r.walk is
slower up to about 24° and quicker above. These are
two hiking models, not an error; GeoTools follows the one it cites, with the
horizontal run as r.walk uses (dividing slope distance instead would add up to
15 % at 30°).

**Edge-graph bias.** On level ground from (0, 0), GeoTools' time over the
straight-line time (all equal to networkx's prediction to 10⁻⁵):

| Toward | (10, 0) | (10, 5) | (10, 10) | (−10, 10) |
|---|---|---|---|---|
| GeoTools (6 edge directions of a triangulated grid) | 1.0000 | 1.0797 | 1.0000 | **1.4142** |
| r.walk, 8 neighbours | 1 | 1.0797 | 1 | 1 |
| r.walk, 16 neighbours (`-k`) | 1 | 1 | 1 | 1 |

A triangulated grid has one diagonal per cell, so a route across the other
diagonal walks the two sides: √2, as analysis-presets.md says ("up to √2").
Photogrammetry meshes have edges in every direction and are far less biased.
**Agrees**, with these documented convention differences.

## Edge cases

- **Flat ground at long range** (20 km, 100 m faces, grazing sight lines down
  to 0.03°): every face seen, no self-hits.
- **Far from the origin** (10 km, 20 km): viewshed 2 of 32,768 faces differ
  outside a 1 cm grazing band (float32 keeps 2 mm there); closed volume fixed
  (above); cut and fill unaffected.
- **Facing away**: the back of the ridge and the dome's far side are hidden
  with Target Height 0 and seen when it is above 0, as documented.
- **No data**: beyond Max Distance (viewshed, distance to surface), outside the
  reference (cut and fill) and on open parts (closed volume), as the existing
  tests check.
- **Sharp edges**: the sign of distance to surface (above).
- **Ties**: on a level grid many routes cost the same; GeoTools picks one of
  them, with the optimal cost.

## Limits users should know

- Visibility is tested at face centres; the edge of a shadow is resolved to
  the face size. No Earth curvature or refraction: for views beyond about
  2 km use GDAL or GRASS (their defaults differ from each other, too).
- Distance to Surface may give the wrong sign within the wedge of an edge
  sharper than 90°.
- Cut and fill split by face; the net is exact.
- Closed volume per part is float32 (10⁻⁵ to 10⁻⁴); Read Totals is binary64.
- Least-cost routes use the mean of both directions and follow mesh edges.

## Bugs fixed

- **Closed Volume lost float32 precision far from the origin** (preset
  version 1 → 2; Read Totals in binary64 from the vertices). Regression tests:
  `test_closed_volume_of_solids_near_and_far` and
  `test_closed_volume_totals_float32_and_binary64`.

## Open points

| Point | Recommendation |
|---|---|
| Sign of Distance to Surface at edges sharper than 90° | keep (matches CloudCompare); document. Alternative: sign by the interpolated vertex normal |
| Earth curvature in visibility | keep off for sites; consider an optional Curvature input for landscape work |

## References

- Bærentzen, J. A., Aanæs, H. (2005). Signed distance computation using the
  angle weighted pseudonormal. *IEEE TVCG* 11(3), 243–253.
- Haverkort, H., Toma, L., Zhuang, Y. (2009). Computing visibility on terrains
  in external memory. *ACM J. Experimental Algorithmics* 13.
- Langmuir, E. (1984). *Mountaincraft and Leadership*. Scottish Sports Council.
- Tobler, W. (1993). Three presentations on geographical analysis and
  modeling. Technical Report 93-1, NCGIA.
- Wang, J., Robinson, G. J., White, K. (2000). Generating viewsheds without
  using sightlines. *Photogrammetric Engineering & Remote Sensing* 66(1), 87–90.
