# Analysis presets

Status: first batch of the M5 preset library (see
[ROADMAP.md](ROADMAP.md#m5-analysis-library)): terrain shape and
archaeological visualisation.

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
the objects differ). Later edits on the modifier are not tracked.

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
  marked as assets in the catalogs *GeoTools/Terrain Shape* and
  *GeoTools/Archaeological Visualisation* (`blender_assets.cats.txt` beside
  the file): add the extension's `assets` folder as an asset library in the
  preferences to drag them from the Asset Browser.
- A bumped preset version is appended alongside an older one already in a
  file; existing modifiers keep the old group.

Four helper groups are shared: **Window Heights** (eight vertical rays around
a point), **Surface Derivatives** (the Evans-Young derivatives from them),
**Mean Rise** (mean height over an annulus from a Repeat zone of vertical
rays) and **Horizon** (horizon angles by bisecting with rays, in nested Repeat
zones). Repeat zones keep their running values in temporary point attributes
named `.gt_tmp_*`, removed before the group ends.

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

### Curvature: profile, plan and mean (`curvature_profile`, `curvature_plan`, `curvature_mean`, CORNER, 1/m)

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
- **plan** curvature is the horizontal (tangential) curvature
  `k_h = −(q²r − 2pqs + p²t) / (g √(1 + g))`: the normal curvature along the
  contour. It is the contour curvature times sin(slope), so it stays bounded on
  gentle ground, where the contour curvature diverges;
- **mean** curvature `H = −((1 + q²)r − 2pqs + (1 + p²)t) / (2 (1 + g)^{3/2})`.

**Positive is convex** (crests, spurs, breaks of slope), negative concave
(hollows, ditches, feet of slope); a sphere of radius R gives +1/R for all
three. Profile and plan curvature are undefined on exactly level ground (no
data where g < 10⁻¹²). The window must be several mesh spacings: a window
close to the spacing samples the mesh's flat facets between vertices and
overstates curvature (+4% on a sphere of R = 5 m at w = 2.5 spacings, 0.03% at
w = 10 spacings).

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

| Input | Default | |
|---|---|---|
| Inner Radius | 0 m | 0 for a disc. |
| Outer Radius | 0.5 m | |
| Samples | 24 | Rounded up to an even number. |
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

| Input | Default | |
|---|---|---|
| Max Distance | 2 m | Search radius. |
| Directions | 16 | Azimuths from +Y, evenly spaced. |
| Steps | 8 | Rays per direction. |
| Ray Offset | 0.002 m | Start of each ray from the surface, along the vertex normal. |

### Positive and Negative Openness (`openness_positive`, `openness_negative`, CORNER, deg)

Yokoyama et al. (2002): positive openness `Φ = 90° − (1/N) Σ β_i`, with β_i
the highest elevation angle of the surface within Max Distance in direction
i; negative openness `Ψ = 90° + (1/N) Σ β′_i`, with β′_i the lowest. Both are
90° on a plane. Positive openness is high on crests and low in hollows;
negative openness the reverse. The search is the sky-view factor's over
−90° to 90° (the first ray, θ = 0, is the midpoint); negative openness starts
its rays Ray Offset *below* the surface, where a ray crosses the surface
within Max Distance if and only if the lowest surface point is at or below θ.
Same inputs and defaults; display range 60–120°.

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

## Performance

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
| TPI | 2.6 s | 34 s | 24 rays |
| local relief model | 4.7 s | 67 s | 48 rays |
| sky-view factor | 12.5 s | 163 s | 128 rays |
| positive, negative openness | 15–19 s | ~210 s (est.) | 128 rays |

Blender's raycast runs at about 4–5 million rays per second here, and the cost
is rays per vertex times vertices. A modifier evaluates when it is added, when
the mesh or an input changes and when the file is opened; otherwise its result
is cached.

**Not within the target at 10M.** Slope, aspect, rugosity, curvature, TRI
and TPI evaluate within a minute at 10M triangles. The local relief model
takes about a minute (67 s, or half that with 24 samples), and the horizon
presets (sky-view factor and openness) take three to four minutes, past the
M5 target of a minute per tool. Directions 8 and Steps 6 cut that to about
a third, with a coarser result. For large sites the proposal is a
**GeoTools-code path**: a horizon scan on a height raster resampled from the
mesh (numpy, as RVT computes SVF and openness on DEMs, written back to the
corners), or a **Cycles bake** of the visible sky (Embree ray tracing, many
times Blender's Geometry Nodes raycast), each written as a layer with
provenance. Neither is in this batch.

## Planned presets (next batch)

- **Visibility:** viewshed (point light and shadow bake), cumulative viewshed,
  intervisibility between points, line of sight.
- **Surface reading:** ambient occlusion and curvature bakes for worn
  surfaces, rock art and inscriptions.
- **Change and distance:** distance between two surveys or epochs, distance to
  a feature (M8 vectors).
- **Volumes:** cut and fill between two surfaces, closed-mesh volumes.
- **3D least-cost paths:** Shortest Edge Paths with a walking cost (Tobler)
  from slope, a maximum walkable slope and per-face costs from layers.
- **Flow-path tracing** for quick looks (a Repeat zone stepping downhill).

## References

- Evans, I. S. (1979). An integrated system of terrain analysis and slope
  mapping. Final report, Durham University.
- Florinsky, I. V. (2016). *Digital Terrain Analysis in Soil Science and
  Geology*, 2nd ed. Academic Press.
- Hesse, R. (2010). LiDAR-derived Local Relief Models. *Archaeological
  Prospection* 17, 67–72.
- Riley, S. J., DeGloria, S. D., Elliot, R. (1999). A terrain ruggedness index
  that quantifies topographic heterogeneity. *Intermountain Journal of
  Sciences* 5, 23–27.
- Weiss, A. (2001). Topographic Position and Landforms Analysis. ESRI User
  Conference poster.
- Yokoyama, R., Shirasawa, M., Pike, R. J. (2002). Visualizing topography by
  openness. *Photogrammetric Engineering & Remote Sensing* 68, 257–265.
- Zakšek, K., Oštir, K., Kokalj, Ž. (2011). Sky-View Factor as a relief
  visualization technique. *Remote Sensing* 3, 398–415.
