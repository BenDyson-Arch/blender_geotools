# Least-cost routes

This covers **Least-Cost Routes** in the Analysis panel
([guide](../guide/analysis.md#least-cost-routes)): the directed search, the
four walking models, routes across faces through Steiner points, the
travel-time layer and the isochrones. The Least-Cost Path preset is on
[Visibility, change, volumes and paths](visibility.md#least-cost-path). For
each check this page gives the definition followed, the reference, the cases,
the tolerance, the result and a verdict.

## Verdicts

| Check | Definition followed | Verdict |
|---|---|---|
| The search | Dijkstra on the directed graph: every node's least time from Start | **agrees** with networkx 3.7 on the same graph, built independently: route times identical, node times within 4×10⁻¹⁶ (six runs, four models) |
| Tobler, Langmuir, Naismith, Wheelchair | the formulas the guide cites | **agree** with the closed forms to 10⁻¹⁴; Langmuir with GRASS r.walk's own times on ramps to 4×10⁻⁸ |
| Constant slopes | straight up and straight down a plane | **agree** with the closed form both ways (10⁻¹², 16 cases) |
| Any-angle routes | Steiner points on every edge | **documented bias**: with 3 points (the default) the worst of 72 bearings is 2.9 % longer than the straight line and the mean 1.2 %; with none, 41.8 % and 16.5 % |
| Travel time | least time to each face's centre | **agrees** with the straight line on level ground (never quicker, 99 % of faces within 3 %) |
| Against r.walk | GRASS r.walk's accumulated cost, zero friction, on the same DEM | **agrees within the raster's direction bias**: GeoTools with 3 Steiner points is within 2 % of r.walk with 16 neighbours for 90 % of cells (median 0.995) and quicker than r.walk with 8 (median 0.966) |

## Method

### Cases

All inputs are synthetic. The hills are the visibility page's: eight
Gaussian hills on 64 × 64 m
([`scripts/validation/visibility/cases.py`](../../scripts/validation/visibility/cases.py)),
here at 1 m (8,192 triangles, every cell split south-west to north-east).
Start is at (−28, −28) and End at (28, 26), both on vertices; Max Slope 30°.
As a raster for GRASS, every vertex is a cell centre.

| Case | What | Used for |
|---|---|---|
| hills | above; with a cost field for one run: twice the cost for 0 < x < 10, no data for −20 < x < −16 north of y = −12 (a wall with a gap) | networkx, r.walk |
| planes | 16 × 16 m at 0.5 m, rising 0, 2, 5 and 15 % to the east | constant slopes, both ways |
| level | 24 × 24 m at 0.5 m, regular and with the inner vertices moved up to 0.3 cells at random | the bias by bearing, travel time |
| waves | 12 × 12 m at 0.5 m, jittered, z = 0.6 sin(x/1.5) cos(y/2), a band at 3 times the cost and a no-data strip | a heap Dijkstra on GeoTools' own links, all four models |

### Reference tools and versions

| Tool | Version | Used for |
|---|---|---|
| networkx | 3.7 | Dijkstra on the same directed graph, built in plain Python one triangle at a time |
| GRASS GIS | 8.5.0, Docker `osgeo/grass-gis:8.5.0-alpine` | `r.walk` (8 and 16 neighbours, `-k`), zero friction |
| GDAL | 3.13.3, Docker `ghcr.io/osgeo/gdal:ubuntu-small-3.13.3` | the DEM as a GeoTIFF for GRASS |
| numpy | 2.5.4 | the cases |

The rasters carry EPSG:32633 (synthetic coordinates). Docker ran with
`--cpus 3 --memory 6g`.

### Regenerating the references

From the repository root, with Docker:

```sh
uv run scripts/validation/least_cost/make_references.py   # networkx, r.walk; ~2 min
```

It writes `tests/data/validation/least_cost/` (`references.npz` and `.json`,
130 kB) and works in `.scratch/validation/least_cost/`. The tests
`tests/core/test_validation_least_cost.py` (42 tests, about 6 s, no Blender)
compare GeoTools with them and print the figures quoted below (lines starting
`VALIDATION`, with `pytest -s`); `tests/blender/test_least_cost.py` runs the
operator as a user would.

## Definitions

- **The graph.** Nodes are the mesh's vertices plus *k* Steiner points evenly
  spaced inside every triangle edge (**Steiner Points**). Within each triangle
  every pair of its boundary nodes is linked both ways by a straight segment.
  A link along an edge shared by two faces exists in both, each at its face's
  cost, so the cheaper counts. This is the Steiner-point scheme of Lanthier,
  Maheshwari and Sack (2001) with uniform spacing.
- **A link's time**: the model's time for the link's horizontal run and rise,
  times its face's cost factor. A face can be crossed when its normal points
  up, its slope is at most Max Slope and its cost factor is finite and above 0.
  The running slope of any segment inside a face is at most the face's slope,
  so this bounds every segment. The wheelchair also refuses any segment
  steeper than Max Gradient, either way.
- **The search**: Dijkstra's shortest-path tree from Start, computed in
  buckets (delta-stepping, Meyer and Sanders 2003): every active node within
  a bucket of the least active time is relaxed at once, and a node that
  improves becomes active again, so the result is Dijkstra's exactly.
- **Start and End** snap to the closest point of the nearest face that can be
  crossed, and join the graph by straight segments to that triangle's nodes.
- **Travel time** of a face: the least time to its centre, through the nodes
  of its triangles and a straight segment.

The models, for a step of horizontal run d and rise h (S = h / d):

| Model | Time | Source |
|---|---|---|
| Tobler | d / v, v = 6 exp(−3.5 \|S + 0.05\|) km/h | Tobler (1993) |
| Langmuir | 0.72 d + 6.0 h climbing; 0.72 d + 1.9998 h for −0.2125 < S < 0; 0.72 d − 1.9998 h below | GRASS r.walk's default `walk_coeff` and `slope_factor` (Aitken 1977, Langmuir 1984) |
| Naismith | 0.72 d + 6 max(h, 0) | Naismith (1892): 5 km/h, an hour per 600 m of ascent |
| Wheelchair | d / v, v = 1.12 m/s for S ≤ 0, falling linearly to 0.59 m/s at S = tan 4.8°; no step with \|S\| above Max Gradient (1:12) | Jahanian et al. (2016), standard wheels, self-selected speed; ADA Standards (2010) 405.2 |

r.walk's manual describes the downhill split as moderate between 5° and 12°
and steep beyond; its code, measured on the visibility page's ramps, applies
the moderate rate to every descent down to 12° (−5 %: 0.620 s/m = 0.72 −
1.9998 × 0.05), and GeoTools follows the code. Langmuir's time jumps at 12°
(0.295 s/m just above, 1.145 s/m just below), as r.walk's does.

The wheelchair speeds are means over six able-bodied people new to
wheelchairs, on an 8 ft ADA ramp; experienced users are often quicker. The
linear fall between the two measured speeds is GeoTools' choice. Beyond a
gradient of about 8.9 % the model's quickest way up is a traverse rather than
straight up (secant of the angle over the speed is least away from the fall
line), which only matters when Max Gradient is raised above 1:12.

## The search against networkx

On the hills, the reference script builds the same directed graph without
GeoTools (node numbering is GeoTools' documented order, and the test checks
each sampled node's coordinates), runs networkx's Dijkstra from Start and
keeps the times at 400 random nodes and the routes both ways.

| Run | Links | Route there (s) | Back (s) | GeoTools |
|---|---|---|---|---|
| Tobler, 2 Steiner points | 390,432 | 69.680965 | 69.783232 | same; nodes within 4×10⁻¹⁶ |
| Langmuir, 2 | 390,432 | 69.833587 | 71.173440 | same; 3×10⁻¹⁶ |
| Naismith, 2 | 390,432 | 75.860437 | 75.860093 | same; 2×10⁻¹⁶ |
| Wheelchair, 2 | 198,956 | 97.521234 | 97.590943 | same (2×10⁻¹⁶); 2×10⁻¹⁶ |
| Tobler, mesh edges only | 22,274 | 71.631842 | 71.923438 | same; 0 |
| Tobler, 2, cost field | 374,892 | 77.675497 | 79.025839 | same; 3×10⁻¹⁶ |

The number of nodes each run reaches is networkx's too. A heap Dijkstra
written in the test, on GeoTools' own links over the waves (all four models,
a cost band and a no-data strip), gives every node's time to 10⁻¹², and
stopping the search once End is settled gives the same route as the full
search. **Agrees.**

## Closed forms

Each model's time per metre equals its formula to 10⁻¹⁴ at slopes either
side of its breaks (−30° to 30°). Langmuir equals r.walk's measured cost per
metre on 50 m planar ramps (the visibility page's table) to 4×10⁻⁸, except at
−13°, where r.walk, with 8 directions, crosses the ramp diagonally at a
moderate slope.

Straight up and down planes rising 0, 2, 5 and 15 % to the east, 12 m along
the fall line, every model gives the closed form both ways to 10⁻¹². The
wheelchair cannot go straight up 15 %: the route traverses the plane, every
segment within 1:12 and 1.5 times longer or more. **Agrees.**

## Any-angle routes

On level ground from the centre of a 24 m plane at 0.5 m to points 10 m away
at 72 bearings (every 5°), the route's time over the straight line's:

| Steiner Points | Regular grid, worst | mean | Jittered, worst | mean |
|---|---|---|---|---|
| 0 (mesh edges) | 1.4176 | 1.1646 | 1.2771 | 1.1085 |
| 1 | 1.0828 | 1.0347 | 1.0555 | 1.0264 |
| 2 | 1.0619 | 1.0247 | 1.0394 | 1.0168 |
| **3 (default)** | **1.0290** | **1.0118** | **1.0252** | **1.0102** |
| 4 | 1.0196 | 1.0089 | 1.0177 | 1.0075 |
| 6 | 1.0105 | 1.0046 | 1.0109 | 1.0044 |
| 8 | 1.0073 | 1.0031 | 1.0086 | 1.0028 |

No route is quicker than the straight line. With no Steiner points the worst
is the √2 of a triangulated grid's missing diagonal (a little over, since End
is not on a vertex). The bias falls about as 1/k; densities do not nest, so
a single route can be a hair slower with one more point (the hills: 69.481 s
with 3, 69.493 s with 4). The travel time to each face of a 20 m plane from
an off-vertex Start is never below the straight line and, beyond 2 m, within
3 % of it for 99 % of faces. **Documented bias**, in the guide.

## Against GRASS r.walk

GeoTools' Langmuir times at the vertices (the raster's cell centres) against
r.walk's accumulated cost from the same Start, zero friction, Max Slope off
(89°; r.walk has none), at the cells more than 10 s from Start (about 3,950):

| GeoTools | r.walk | Ratio p5 | median | p95 | At End |
|---|---|---|---|---|---|
| mesh edges | 8 neighbours | 1.0000 | 1.0000 | 1.0487 | 72.419 s, both |
| mesh edges | 16 neighbours | 1.0080 | 1.0341 | 1.0971 | 72.419 against 69.767 s |
| 3 Steiner points | 8 neighbours | 0.9336 | 0.9657 | 0.9916 | 69.481 against 72.419 s |
| 3 Steiner points | 16 neighbours | 0.9842 | 0.9951 | 1.0000 | 69.481 against 69.767 s |

The model is the same (the ramps above); what remains is raster against mesh:

- **Directions.** r.walk moves to 8 neighbours (every 45°) or, with `-k`, 16
  (every 26.6°); GeoTools on mesh edges has this grid's 6, and with Steiner
  points any angle. The triangulated grid's 6 directions are 6 of r.walk's 8,
  so along them the two agree exactly (the median of 1 and the same time at
  End); between them both pay the detour. With 3 Steiner points GeoTools is
  quicker than 8 neighbours nearly everywhere and within 2 % of 16 for nine
  cells in ten.
- **Steps over the terrain.** r.walk costs a move by the height difference
  between two cell centres, so a knight's move (2.2 cells) skips the ground
  between them; GeoTools' segments follow the TIN across each face.
- **The 12° jump.** Langmuir's descent switches from −1.9998 to +1.9998 s per
  metre at 12°. The slope of a step depends on its direction, so r.walk and
  GeoTools cross the switch at different places, and either can be quicker on
  slopes near 12° (the −13° ramp).

**Agrees within the raster's direction bias.**

## Performance

On one terrain of 1,000,000 triangles (1 km square, slopes to 20°), Tobler,
route corner to corner and the travel-time layer, measured in Blender 5.2
with `scripts/bench_least_cost.py` on an i7-12700KF shared with other jobs
(load average 15 to 20):

| Steiner Points | Graph nodes | Route and layer | Search alone | Peak memory |
|---|---|---|---|---|
| 0 | 0.50 M | 2.1 s | 0.6 s | 1.1 GB |
| 1 | 2.0 M | 3.8 s | 2.2 s | 1.4 GB |
| 2 | 3.5 M | 8.0 s | 5.6 s | 1.7 GB |
| 3 | 5.0 M | 13.3 s | 11.5 s | 1.9 GB |
| 4 | 6.5 M | 15.6 s | 13.1 s | 2.2 GB |

The rest is reading the mesh and building the graph (1.2 to 2 s) and writing
the outputs (under 1 s). The search costs about 70 ns per link and relaxes
each link about once. From the panel it runs a few passes per 0.1 s timer
tick, so Blender stays responsive (measured: no gap over 0.11 s between UI
timers during a 1.5 s search), and Esc cancels.

## Edge cases

- **Several objects** are refused with a message pointing at issue
  [#109](https://github.com/BenDyson-Arch/blender_geotools/issues/109).
- **Faces facing down**, steeper than Max Slope or with a cost of 0, below 0
  or no data are never crossed; a mesh whose faces all face down is refused
  ("No face can be crossed"). A mirrored object's normals are flipped back.
- **Start or End off the surface** (in the air, or on ground that cannot be
  crossed) snaps to the nearest face that can be; the result notes moves over
  0.5 m.
- **End unreachable**: no route object; with Travel Time on, the layer is
  still written, unreached faces no data and `Unreached` in the isochrones.
- **Quads and n-gons** are routed through Blender's triangles; a face's time
  is its quickest triangle's.

## Limits users should know

All are in the [guide](../guide/analysis.md#least-cost-routes): one connected
surface, normals out of the ground, no cross-slope limit for the wheelchair,
times to face centres, and the remaining any-angle bias.

## References

- Aitken, R. (1977). *Wilderness areas in Scotland*. PhD thesis, University
  of Aberdeen.
- Aleksandrov, L., Maheshwari, A., Sack, J.-R. (2005). Determining
  approximate shortest paths on weighted polyhedral surfaces. *Journal of the
  ACM* 52(1), 25–53.
- Jahanian, O., Schnorenberg, A. J., Hawi, L., Slavens, B. A. (2016).
  Evaluation of shoulder joint kinematics and stroke cycle characteristics
  during geared and standard manual wheelchair mobility. RESNA Annual
  Conference.
- Langmuir, E. (1984). *Mountaincraft and Leadership*. Scottish Sports
  Council.
- Lanthier, M., Maheshwari, A., Sack, J.-R. (2001). Approximating shortest
  paths on weighted polyhedral surfaces. *Algorithmica* 30(4), 527–562.
- Meyer, U., Sanders, P. (2003). Δ-stepping: a parallelizable shortest path
  algorithm. *Journal of Algorithms* 49(1), 114–152.
- Naismith, W. W. (1892). *Scottish Mountaineering Club Journal* 2, 136.
- Tobler, W. (1993). Three presentations on geographical analysis and
  modeling. Technical Report 93-1, NCGIA.
- U.S. Department of Justice (2010). *ADA Standards for Accessible Design*,
  405.2 Slope.
