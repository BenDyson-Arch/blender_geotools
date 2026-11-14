# Validation: hydrology, contours and sections

Status: validated on `val/hydrology` (2026-11-14), against GeoTools 0.4.0-dev
(develop at 3a90e20) in Blender 5.2.4. One bug was found and fixed (duplicate
contour and section segments). Five discrepancies were left for a decision;
all five are now [resolved](#decisions-resolved).

This covers 3D surface hydrology (`geotools/analysis/hydrology.py`,
`geotools/blender/hydrology.py`), the Flow Path (Quick Look) preset, contours
and sections (`geotools/analysis/slicing.py`, `polylines.py`,
`geotools/blender/contours.py`), and Export Vectors for the lines and points
they make. For each tool it gives the definition followed, the reference tools,
the cases, the tolerances, the results and a verdict. The design is in
[m5-analysis.md](../design/m5-analysis.md).

## Verdicts

| Tool | Definition followed | Verdict |
|---|---|---|
| Depression filling (ponded depth, spill level) | Priority-flood (Barnes, Lehman and Mulla 2014; Wang and Liu 2006) on the graph of faces | **agrees**: equal to a heap priority-flood on the same graph; against raster priority-flood, with a **documented convention difference** (heights sampled at face centres, so spill levels are low by about h/3 × slope) |
| Depressions (Minimum Depth, Depression Points) | connected ponded faces; level, pour point, volume Σ depth × plan area | **agrees** (closed-form bowls; WhiteboxTools and richdem lake by lake) |
| Flow routing | single-direction steepest descent (the D8 idea of O'Callaghan and Mark 1984), on three edge-neighbours instead of eight cells | **agrees, with a documented convention difference**: three directions, and spurious shallow pits along V-channels that follow mesh edges ([limit](#v-channels-along-mesh-edges)) |
| Flats | each flat face drains to a neighbour one step nearer the flat's exit (breadth-first) | **agrees, with a documented convention difference**: only the "towards lower terrain" half of Garbrecht and Martz (1997) / Barnes et al. (2014b) |
| Flow accumulation | each face's plan area plus everything upstream (O'Callaghan and Mark 1984) | **agrees** (exact against brute force; channels against WhiteboxTools D8: median ratio 1.01) |
| Channel From, Networks, Flow Paths object | threshold contributing area for channel start, as WhiteboxTools ExtractStreams or GRASS r.stream.extract; networks ranked by outlet area | **agrees**; no Strahler or Shreve ordering is offered |
| Drops (3D: overhangs, roofs, walls) | vertical fall to the first face below (Blender Raycast) | **agrees** with the core (no external 3D reference exists; see [3D](#drops-and-the-3d-surface)) |
| Flow Path (Quick Look) | line of steepest descent, explicit steps on the facets | **agrees** (SciPy `solve_ivp`: within 1.0 cm at a 2 cm step, 4.4 cm at the default 10 cm) |
| Contours | linear interpolation on each triangle ("marching triangles" on the TIN), levels `base + k × interval` in the height datum | **agrees** after the fix (gdal_contour: the same points on every grid edge, to 2×10⁻¹⁴ m); **documented convention difference** for levels exactly at vertex heights |
| Sections | the vertical plane of each section-line segment cut through the TIN; chainage is plan distance along the line | **agrees** after the fix (GDAL bilinear: equal on a twist-free surface to 6×10⁻¹⁵ m, within the twist bound elsewhere; chainage exact) |
| Export Vectors (GeoPackage, GeoJSON) | simple-feature points and line strings with Z in the project CRS; sections as LineString ZM with chainage as M (GeoJSON: a per-point `chainage` property) | **agrees** (read back with sqlite3 and json, not GeoTools' reader, and with GDAL 3.12.0); per-point chainage exported since [decision 3](#decisions-resolved) |

**Bug fixed** (commit e420aae, "Contours and sections: draw an edge lying on
the plane once"): a mesh edge lying exactly on a contour level, or on a section
plane, with both of its triangles on the same side was cut by both triangles,
so the same segment was drawn twice. The output held duplicate lines, and a
section through a box corner a two-point "ring". This happens on real data:
heights quantised to the centimetre put ridges exactly on whole-metre levels.
Regression tests: `tests/core/test_slicing.py`
(`test_a_ridge_exactly_on_a_level_is_drawn_once`,
`test_section_through_a_box_edge_on_its_plane_is_one_segment`) and the monkey
saddle below.

## Method

### Surfaces and meshes

All inputs are synthetic (`tests/blender/_hydrology_surfaces.py`, shared by the
script and both test suites). A DEM becomes a mesh with one vertex per cell
centre and two triangles per square of four (south-west to north-east
diagonal), so GeoTools and the raster tools see the same heights. What differs
is where they sample them (GeoTools: face centres, the mean of three corners;
rasters: cell centres) and the graph water moves on (GeoTools: the three faces
sharing an edge; D8: eight cells).

| Surface | Used for |
|---|---|
| plane z = −0.1 x, 64 × 32 m at 1 m | parallel flow, accumulation linear downslope |
| plane falling 30° north of east, 64 × 64 m at 1 m | routing across the triangulation (direction bias) |
| cone z = 32 − r (a peak), 64 m at 1 m | divergent flow |
| inverted cone z = r on [−32, 32]² at 2, 1, 0.5 and 0.25 m | one depression; spills at the edge midpoints at z = 32, volume π L³/3 |
| two bowls: min(0.5 r₁² − 1, 0.5 r₂² − 0.3), centres (−1.5, 0) and (1.2, 0), on 5 × 4 m at 0.1 to 0.0125 m | spill order: the shallow east bowl fills to the saddle (x = 0.1093, z = 0.2949) and spills into the deep west bowl, which fills to the west edge (z = −0.5); volumes π d² |
| V-valley z = 0.3 \|y\| + 0.05 x, 80 × 40 m at 1 m (and other slopes and cells) | a channel along the thalweg |
| "terrain": 40 seeded Gaussian hills and pits on a slope with 2 cm noise, 95 × 95 m at 1 m | many depressions; channels |
| contours: plane z = x (levels on vertex columns), cone z = 10 − r, monkey saddle z = x³ − 3xy², terrain | positions, topology, levels at vertex heights |
| sections: paraboloid z = 0.05 (x² + y²) (no twist), cone, terrain; a bent plane | heights and chainage |
| two Gaussian hills on a plane, [−4, 4]² at 2.5 cm | Flow Path (Quick Look) |

A single-sheet DEM mesh has nothing below it, so every drop leaves the model;
the core tests resolve drops that way, and the Blender test checks that
Blender's Raycast gives exactly the same result.

### Reference tools

| Tool | Version | Installed as | Used for |
|---|---|---|---|
| WhiteboxTools | 2.4.0 (`whitebox` 2.3.6 Python package, numpy 2.2.6) | a uv venv under the ignored `.scratch/` | FillDepressionsWangAndLiu (priority-flood fill); D8FlowAccumulation and DInfFlowAccumulation (Tarboton 1997) |
| richdem | 0.3.4 (numpy 1.21.6) | Docker `python:3.7.17-slim` (its last wheels are for Python 3.7) | FillDepressions (Barnes 2014 priority-flood; with ε, the drained DEM the accumulations route over) |
| GDAL | 3.12.0 "Chicoutimi" | Docker `ghcr.io/osgeo/gdal:ubuntu-small-3.12.0` | gdal_contour; gdallocationinfo -r bilinear |
| SciPy | 1.18.1 | the same venv | `solve_ivp` (DOP853, rtol 10⁻¹⁰) for lines of steepest descent |

Three reference functions were not usable, and are not used. In our runs
WhiteboxTools 2.4.0's own FillDepressions left the bowls unfilled; its flat
fixing (`fix_flats` of either fill) and BreachDepressionsLeastCost returned
heights off by 10² to 10¹³ m. richdem 0.3.4's FlowAccumulation (D8 and Dinf)
gave wrong counts on a 4 × 7 plane. So both fills are priority-floods
(WhiteboxTools' Wang and Liu, richdem's Barnes), which agree with each other
to 0.0 m on every DEM. WhiteboxTools' accumulations run on richdem's
priority-flood+ε DEM, which drains everywhere.

GRASS (r.watershed, r.fill.dir) was not used: two independent priority-flood
implementations and WhiteboxTools' D8 and D-infinity cover the same
definitions.

### Commands

```sh
uv venv .scratch/venv-ref --python 3.12
uv pip install --python .scratch/venv-ref/bin/python \
    whitebox==2.3.6 numpy==2.2.6 scipy==1.18.1 tifffile==2025.10.16
.scratch/venv-ref/bin/python scripts/validation/hydrology/make_references.py > report.json
```

The script (`scripts/validation/hydrology/make_references.py`, not run in CI)
writes the DEMs as ESRI ASCII grids, runs the tools (Docker capped at 3 CPUs
and 6 GB, containers removed), prints the comparison report the tables below
come from, and writes `tests/data/validation/hydrology/references.npz`
(250 KB): the reference fills and accumulations, the terrain DEM, GDAL's
contour vertices and bilinear heights, and the SciPy curves.

CI runs `tests/core/test_validation_hydrology.py` (30 tests, about 1 s) and
`tests/blender/test_validation_hydrology.py` (3 tests, under 1 s;
`GEOTOOLS_HYDROLOGY_REPORT=path.json` writes the measured statistics).

## Depression filling and depressions

**Definition.** A face's filled height is the lowest level at which water on it
can leave the model: the minimax height over all paths to the outside. That is
what priority-flood computes (Barnes, Lehman and Mulla 2014; Wang and Liu 2006
order the same queue). GeoTools works it out on basins (steepest-descent
basins, linked at their lowest passes, a minimum spanning tree rooted at the
outside), not by flooding faces one at a time.

**Exactness.** `test_filling_is_a_priority_flood` floods the same face graph
with a plain heap (Barnes 2014, Algorithm 1: seed the faces that drop out of
the model at their own height, pop the lowest, raise each unvisited neighbour
to at least it). On the terrain, both bowls surfaces and the valley,
GeoTools' filled heights are **bit-identical** to it.

**Closed forms.** The inverted cone spills at the middle of each edge. GeoTools'
pass is the higher face of the lowest link, whose centre is a third of a cell
inside the edge, so the level is low by about h/3 × slope. At the level it
finds, the volume is π level³/3 to O(h²):

| Cell h | Level (exact 32) | Level error | Volume error at that level | Volume error against π 32³/3 |
|---|---|---|---|---|
| 2 m | 31.354 | −0.646 | −0.281 % | −6.20 % |
| 1 m | 31.672 | −0.328 | −0.067 % | −3.11 % |
| 0.5 m | 31.835 | −0.165 | −0.017 % | −1.56 % |
| 0.25 m | 31.917 | −0.083 | −0.004 % | −0.78 % |

Two bowls: two depressions; the east one's pour point is at the saddle (within
2 cells), the west one's at the west edge; the east lake's exit leads into the
west lake (`test_two_bowls_spill_in_order`).

| Cell h | East level error (saddle) | West level error (edge) | East volume against π d² (true level) | West | East, at the level found | West, at the level found |
|---|---|---|---|---|---|---|
| 0.1 m | −0.040 | −0.030 | −13.6 % | −12.5 % | −0.79 % | −0.99 % |
| 0.05 m | −0.024 | −0.016 | −8.1 % | −6.5 % | −0.19 % | −0.24 % |
| 0.025 m | −0.016 | −0.008 | −5.2 % | −3.3 % | −0.05 % | −0.06 % |
| 0.0125 m | −0.007 | −0.004 | −2.4 % | −1.7 % | −0.01 % | −0.01 % |

All volume error comes from the level: the paraboloid's volume near the rim
grows like the rim's area times the level error. The level converges at first
order; the volume at a given level converges at second order.

**Against the rasters** (same DEM, cell = mesh spacing; levels compared at the
cell under each GeoTools depression's deepest face):

| Surface | GeoTools volume | WhiteboxTools (Wang and Liu) = richdem | Ratio | Depressions ≥ 1 cm (GeoTools / rasters) | Level difference, median / max |
|---|---|---|---|---|---|
| inverted cone, 1 m | 33,247.5 m³ | 34,313.5 m³ | 0.969 | 1 / 1 | 0.328 / 0.328 m |
| two bowls, 5 cm | 1.756 m³ | 1.842 m³ | 0.953 | 2 / 2 | 0.0125 / 0.0158 m |
| terrain, 1 m | 438.4 m³ | 458.4 m³ | 0.956 | 22 / 22 | 0.008 / 0.058 m |

On the terrain 17 of the 22 depressions sit over a raster lake at the same
place; their volume ratios have median 0.983. A few differ widely (0.41, 7.6)
where neighbouring hollows join differently: a raster lake is 8-connected
cells, a GeoTools depression is faces sharing edges, and a pass between two
hollows is sampled at a face centre or a cell centre. The inverted cone's
ratio, 0.969, is exactly (31.672/32)³: the volume difference is the level
difference.

**Tolerances.** Levels: within h × slope of the raster's (the face centre at
the pass is at most two thirds of a cell from the cell centre). Volumes:
within the level's effect, 2 to 7 % at these cells. Exactness tests: equality.

**Verdict.** Agrees. The face-centre sampling is a convention: heights are
FACE values throughout the hydrology tool, so a face is wet or dry as a whole.
It makes spill levels low by about h/3 × slope (a third of a cell's fall) and
volumes low by that level's effect. On photogrammetry meshes (faces of 1 to
5 cm) that is millimetres.

## Flow routing, flats and accumulation

**Definition.** Single flow direction: each face drains to the neighbour with
the steepest drop, by drop over the 3D distance between centres. On a 2.5D
surface this picks the same neighbour as drop over plan distance, the D8 rule
(O'Callaghan and Mark 1984; sin is monotonic in tan), but over a face's three
edge-neighbours, not eight cells. Accumulation is each face's plan area (the
rain catch; **Surface** uses the true area) plus everything upstream.

**Exactness.** `test_routing_is_steepest_descent_and_accumulation_sums_upstream`
checks on the terrain that every face with a lower neighbour drains to the
steepest one (by brute force over its neighbours, on the filled surface). It
also checks that every other face lies on a flat or drains out, that the
receivers form a forest (Kahn's order reaches every face), and that the
accumulation equals the upstream sum to 10⁻¹² relative. All the water
leaves the model.

**Planes.** On z = −0.1 x, a face's mean accumulation over a 4 m band across
the slope is the distance from the high edge plus 0.25 m, to 0.01 m
(`test_inclined_plane_accumulation_grows_linearly`): linear, as for parallel
flow. On the plane falling 30° across the triangulation, the accumulation over
bands of upslope flow length, away from the outlet edges, against the same for
WhiteboxTools:

| Band of upslope length (m) | 0–8 | 8–16 | 16–24 | 24–32 | 32–40 | 40–48 | 48–56 | 56–64 | 64–72 |
|---|---|---|---|---|---|---|---|---|---|
| GeoTools | 0.795 | 0.751 | 0.740 | 0.733 | 0.723 | 0.721 | 0.712 | 0.698 | 0.685 |
| WhiteboxTools D8 | 0.859 | 0.772 | 0.753 | 0.742 | 0.734 | 0.725 | 0.711 | 0.705 | 0.694 |
| WhiteboxTools D-infinity | 0.852 | 0.826 | 0.826 | 0.826 | 0.828 | 0.830 | 0.831 | 0.840 | 0.844 |

(Mean accumulation over the band's mean upslope length; the absolute level
depends on each method's flow width.) Single-direction routing drifts water
off the fall line toward the mesh's (or grid's) directions, and GeoTools'
drift matches D8's: within 0.064 in the first band, 0.021 in the others.
D-infinity, which splits flow between two cells, holds a steady ratio. GeoTools does not offer multiple-flow
routing (D-infinity, MFD); see m5-analysis.md for why (accumulation stays a
tree, vectorised).

**Cone.** Every step goes outward; a step is a median 28° off radial (at most
90°: a step can run nearly across the slope while still going outward). D8's
eight directions are at most 22.5° off. The divergent flow keeps accumulation
small (`test_cone_flow_diverges`).

**Channels against D8** (terrain; cells with at least 100 cells upstream in
WhiteboxTools, dry in both, against the largest GeoTools accumulation of the
faces at or next to the cell, to allow a channel one cell to the side):

| Reference | Cells | Ratio p10 | Median | p90 |
|---|---|---|---|---|
| WhiteboxTools D8 | 70 | 0.30 | 1.01 | 1.18 |
| WhiteboxTools D-infinity | 97 | 0.25 | 1.21 | 5.27 |

The median channel carries the same area in both. The low tail is where a
GeoTools channel runs two or more cells from D8's. D-infinity spreads flow, so
its "channel" cells include spread hillslope cells where GeoTools has a
single channel (the high p90).

**Flats** (a lake surface or level ground) drain breadth-first from their
exits: each flat face's receiver is a neighbour one step nearer the exit, so
the routes are shortest paths to the outlet
(`test_flats_drain_by_the_shortest_way_to_their_exit`, a walled floor open on
one side). This is the "towards lower terrain" gradient of Garbrecht and Martz
(1997), used alone. Barnes, Lehman and Mulla (2014b) and Garbrecht and Martz
also apply an "away from higher terrain" gradient, which makes flow on a flat
converge along its middle rather than run parallel to the exit. The
accumulation leaving a flat is the same either way; the paths across it
differ.

**Verdict.** Agrees, with documented convention differences: three directions
per face (D8 has eight); flats routed towards the exit only.

### V-channels along mesh edges

The face graph links faces that share an edge, not faces that share only a
vertex. Where a V-shaped channel runs along a line of mesh edges, the two
lowest faces of each cell meet only at a vertex. So each is a pit: water
reaches the next one only by filling. The filling is correct for that graph
(priority-flood above). But the rasters see no depression at all, and the
tool reports depressions that are not on the surface. On the valley
z = s |y| + f x with the channel along a row of vertices, each pit is
(s − 2f) × h/3 deep:

| Side slope s | Fall f | Cell h | Depressions | Deepest | Listed at the default Minimum Depth (1 cm) |
|---|---|---|---|---|---|
| 0.3 | 0.05 | 1 m | 79 | 66.7 mm | 79 |
| 0.3 | 0.05 | 0.25 m | 79 | 16.7 mm | 78 |
| 0.3 | 0.05 | 0.05 m | 79 | 3.3 mm | 0 |
| 0.3 | 0.05 | 1 m, channel 30° to the grid | 17 | 22.9 mm | 10 |
| 0.1 | 0.01 | 1 m | 79 | 26.7 mm | 78 |
| 0.1 | 0.01 | 0.25 m | 79 | 6.7 mm | 0 |
| 0.03 | 0.01 | 1 m | 79 | 3.3 mm | 0 |

The accumulation is not affected: a filled pit passes all its water on, so
the thalweg still carries the whole valley
(`test_valley_thalweg_carries_everything_but_breaks_into_shallow_pits`). Ponded
depth, the depression layer and the Depressions object do show the pits. On
photogrammetry meshes (faces of a few centimetres) they are millimetres deep
and the default Minimum Depth hides them; on coarse meshes (a DEM converted at
1 m) they are listed. See [recommendation 1](#decisions-resolved).

## Channel From, Networks and the Flow Paths object

**Definition.** A face is on a flow path when its accumulation is at least
**Channel From** (m²): threshold channel initiation, as in WhiteboxTools
ExtractStreams or GRASS r.stream.extract. Channel faces are grouped by the
outlet they drain to, and the **Networks** largest outlets are kept. Lines run
through the face centres (and drop landings), split at confluences, run
downstream.

**Checks.** `tests/core/test_hydrology.py::test_channels_rank_networks`
(ranking); the Blender test checks every Flow Paths vertex is the centre of a
face at or above the threshold, and the export carries `accumulation`,
`network` and `length`. The threshold is applied to the same accumulation
validated above, so the network is D8-like (channels median ratio 1.01).

**Stream order.** There is no Strahler (1957) or Shreve (1966) order; the
networks are ranked by outlet area only. See
[recommendation 5](#decisions-resolved).

**Verdict.** Agrees.

## Drops and the 3D surface

No GIS tool routes water over walls, overhangs and separate objects, so the 3D
part has no external reference. What can be checked is checked:

- `test_hydrology_operator_equals_the_core` (Blender): on a single sheet,
  **Compute Hydrology**, with drops cast by Blender's Geometry Nodes Raycast,
  gives exactly the core's accumulation, depth and depression labels. Its
  Depressions object holds the core's table (pour points to 10⁻⁵ m, levels to
  10⁻⁶ m).
- Existing tests (`tests/core/test_hydrology.py`,
  `tests/blender/test_surface_tools.py`): a step joined by a wall routes down
  the wall; a roof sheet drops from its eaves onto the ground; a floating
  slab's underside drips; a wall sunk into the ground comes up through it; a
  closed box drains at its lowest point. In each case the water leaving the
  model is the whole plan area, to 10⁻⁹. And drops land where Blender casts
  them.

**Verdict.** Agrees with its own definition (vertical drops, first face below,
upward faces above a sunken start).

## Flow Path (Quick Look)

**Definition.** A line of steepest descent: from each seed, draped on the
surface, a step of **Step** (horizontal) down the face's steepest direction,
draped again, while the new point is lower. This is explicit Euler on the
facets, so its error grows with the step and with the facets' departure from
the surface.

**Reference.** The lines of steepest descent of two Gaussian hills on a
falling plane (`_hydrology_surfaces.hills`, analytic gradient), integrated in
plan arc length by SciPy `solve_ivp` (DOP853, rtol 10⁻¹⁰), from five seeds
to 0.05 m from the edge. The mesh is [−4, 4]² at 2.5 cm.

| Step | Largest distance from the reference line, by seed | Median, by seed |
|---|---|---|
| 0.02 m | 4.0, 10.3, 7.9, 9.2, 8.5 mm | 1.0, 3.9, 4.8, 2.5, 7.9 mm |
| 0.1 m (default) | 36.5, 40.7, 31.6, 43.6, 42.0 mm | 8.8, 16.9, 20.8, 9.4, 36.9 mm |

Every path reaches the edge, and its heights are the surface's to 6×10⁻⁵ m.
Tolerances: the largest distance below one step, the median below half a step.

**Verdict.** Agrees. It is a quick look, as documented: no accumulation, no
filling, 2.5D.

## Contours

**Definition.** A contour at level L is where the TIN (linear on each triangle)
crosses L: each triangle crossing L gives one segment between its two crossing
edges, and the segments are chained into lines. Levels are `base + k ×
interval` in the project's height datum (RL in a CRS project); `major` marks
every **Major Every**-th level. A vertex exactly on a level counts as above it.

**Against gdal_contour.** gdal_contour runs marching squares on the pixel
centres, which are the mesh's vertices. Where a contour crosses an edge of the
grid, both interpolate linearly along that edge: the same point. Inside a cell
GeoTools follows the cell's two triangles, while marching squares joins the
edge points directly (and resolves saddle cells by the cell's mean). Distances
in plan, inside the hull of the cell centres (GDAL's lines run on to the
raster's outer edge, half a cell beyond):

| Surface | GDAL vertices to GeoTools lines, max | GeoTools grid-edge vertices to GDAL lines, max | GeoTools diagonal vertices to GDAL lines, median / max | Lines (GeoTools / GDAL) |
|---|---|---|---|---|
| terrain, 1 m, every 0.25 m | 2.0×10⁻¹⁴ m | 2.1×10⁻¹⁴ m | 0.036 / 0.96 m | 112 / 113 |
| cone, 0.25 m | 1.1×10⁻⁶ m | 1.0×10⁻⁶ m | 0.0004 / 0.015 m | 26 / 26 |
| monkey saddle, 0.05 m | median 6×10⁻¹⁶ m | 3.9×10⁻⁴ m | 0.0006 / 0.0069 m | 100 / 102 |
| plane z = x, levels on vertices | 1.0×10⁻⁶ m | 1.0×10⁻⁶ m | (none) | 20 / 21 |

The 10⁻⁶ m differences come from GDAL nudging a value that lies exactly on a
level (GeoTools uses the vertex). The terrain's line counts differ at 3 of 17
levels, where a saddle cell is joined the other way. The diagonal distances are
the two interpolations' disagreement inside a cell, at most about a cell where
the cell is strongly twisted.

**Levels at vertex heights** (both treat the vertex as above the level). On
z = x every level falls on a vertex column. GeoTools draws levels 1 to 20, the
highest along the top edge; GDAL draws 0 to 20, including the lowest along the
bottom edge. A vertex that is a local peak exactly on a level gives GDAL a
microscopic line around it and GeoTools nothing (the monkey saddle's corners
at ±2 and its edge peak at 1.0). Either way no area is missed. See
[recommendation 4](#decisions-resolved).

**Topology** (`test_monkey_saddle_contour_topology`, z = x³ − 3xy², level 0
through the saddle along the vertex column x = 0): every point is exactly on its
level; no segment appears twice (this test failed before the fix); no two
segments cross; the zero level lies on x = 0 and y = ±x/√3, to within a cell.

**Accuracy against the surface** (cone z = 10 − r; the TIN is exact for the
TIN, so this is the facets' departure from the cone):

| Cell h | Largest radius error | h²/(2 × 1 m), the chord sag bound |
|---|---|---|
| 0.5 m | 22.5 mm | 125 mm |
| 0.25 m | 7.5 mm | 31.3 mm |
| 0.125 m | 1.6 mm | 7.8 mm |
| 0.0625 m | 0.5 mm | 2.0 mm |

**Verdict.** Agrees after the fix. Documented convention: the lowest level of
a flat-bottomed surface, and isolated vertices exactly on a level, give no
line.

## Sections

**Definition.** Each segment of the section line cuts the TIN with its vertical
plane, clipped to the segment; the pieces of consecutive segments join at the
line's vertices. **Chainage** is plan distance along the section line from its
first point; every piece runs with increasing chainage.

**Against GDAL bilinear** (`gdallocationinfo -r bilinear` at every section
point). Triangles and bilinear interpolation agree on a cell's edges. Inside a
cell they differ by the cell's twist b = z₀₀ − z₁₀ − z₀₁ + z₁₁ times s(1 − t)
or t(1 − s), at most |b|/4. On a surface without twist (z = f(x) + g(y)) they
are the same surface.

| Surface | Points | Largest difference | Bound |
|---|---|---|---|
| paraboloid 0.05 (x² + y²), 0.5 m, a bent line | 115 | 5.8×10⁻¹⁵ m | 0 (no twist) |
| cone 10 − r, 0.5 m | 75 | 0.028 m | each point within its cell's \|b\|/4 |
| terrain, 1 m, a bent line | 418 | 0.029 m | each point within its cell's \|b\|/4 |

**Chainage and heights** (`test_section_chainage_and_heights_on_a_plane`, a
line with two bends over a plane): one piece round both bends; every point
on the plane to 10⁻¹² m, on the section line, with its chainage equal to the
plan distance along the line to 10⁻¹² m; `chainage_to` is the line's length.
The sphere sections of m5-analysis.md (41, 11 and 2.6 mm at 32, 64 and 128
segments) are the facets' departure from the sphere.

**Verdict.** Agrees after the fix (a box edge lying on the section plane gave
a two-point ring).

## Export Vectors

`test_export_vectors_round_trips_hydrology_contours_and_sections` (Blender, a
CRS project in EPSG:7856 with E = 500,000, N = 6,960,000, A = 100). It exports
the Depressions, Flow Paths, Contours and Sections objects as GeoPackage and
GeoJSON and reads them back without GeoTools: sqlite3 and a hand-written WKB
parser, and json.

- GeoPackage: the table's SRS is EPSG 7856 (`gpkg_spatial_ref_sys`), Z is
  declared. GeoJSON: the legacy `crs` member names EPSG:7856 (outside RFC 7946,
  as the export option says).
- Attributes: Depressions `depression`, `level`, `volume`, `area`, `max_depth`,
  `faces`, `catchment`; Flow Paths `accumulation`, `network`, `length`;
  Contours `elevation`, `index`, `major`, `closed`, `length`; Sections
  `section`, `chainage_from`, `chainage_to`, `length`.
- Coordinates: every exported vertex is a vertex of the object plus (E, N, A),
  to 10⁻⁶ m. Contour vertices are at their line's `elevation` (RL, on the 0.25 m
  levels). Depression levels are A plus the core's level, to 10⁻⁴ m (float32
  attributes). Volumes are the core's divided by the combined scale factor
  squared (a factor between 1 and 1.002 here).

**Chainage (resolved).** A section's per-point `chainage` is exported: as
each point's M in the GeoPackage (LineString ZM, `m` = 1 in
`gpkg_geometry_columns`) and as a per-point `chainage` property in GeoJSON.
The test checks every piece's M against the object's chainage at its
vertices, that it runs from `chainage_from` to `chainage_to` without going
back, and that the GeoJSON property equals it; the other objects have no M.
GDAL 3.12.0 reads the GeoPackage pieces as measured 3D line strings with
those M values and the GeoJSON property as a RealList, and `validate_gpkg`
accepts the file. Before, the export had `chainage_from` and `chainage_to`
per piece only. See [decision 3](#decisions-resolved).

**Verdict.** Agrees.

## Decisions (resolved)

The validation left five points for a decision. The user's decisions
(2026-11-14) are implemented on `fix/validation-decisions`.

1. **Pits along V-channels that follow mesh edges** ([above](#v-channels-along-mesh-edges)).
   Faces linked only through shared edges make each cell of such a channel a
   pit (s − 2f) × h/3 deep: on 1 m DEM-derived meshes dozens of depressions
   of several centimetres that a raster tool does not see; accumulation is
   unaffected. **Done (documented):** the guide's hydrology Limits explain
   the pits, how deep and why, that accumulation is unaffected, and suggest a
   Minimum Depth of about a third of a face's fall across the channel
   (side slope × h/3) on coarse meshes; m5-analysis.md says the same. Linking
   faces that share a vertex is in the backlog
   ([ANA-2](../BACKLOG.md#ana-2-link-hydrology-faces-through-shared-vertices)).
2. **Spill levels at face centres.** Levels are low by about h/3 × slope
   against a raster priority-flood (3 % of the inverted cone's volume at 1 m
   cells). **Done (documented):** kept; the guide states it next to Minimum
   Depth, and m5-analysis.md in its Limits.
3. **Section chainage on export.** **Done:** sections export as LineString
   ZM with chainage as M in a GeoPackage (`m` = 1, ISO WKB 3002), and, since
   GeoJSON positions hold no M, with a `chainage` property holding one value
   per point; `chainage_from`/`chainage_to` stay. GDAL 3.12.0 reads the
   GeoPackage as a measured 3D line string with those M values and the
   GeoJSON property as a RealList; `validate_gpkg` accepts it
   (`scripts/validation/exports/run.sh`). A Vectors
   publish leaves M out, as rusty-tiles 0.4.1 refuses measured geometry
   ("retain or explicitly remove M"). Tests:
   `test_export_vectors_round_trips_hydrology_contours_and_sections` (above),
   `tests/core/test_vector_mesh.py::test_lines_carry_a_point_attribute_as_m`,
   `tests/core/test_publish_vector.py::test_write_source_drops_measures`.
4. **Contours at the lowest level and at isolated vertex peaks** give no line
   in GeoTools, where gdal_contour draws a line along the bottom edge or a
   microscopic loop. **Done (documented):** kept (no area is missed); the
   guide's Contours section now says so, as m5-analysis.md does.
5. **No stream order and no flat convergence.** **Done:** in the backlog
   ([ANA-3](../BACKLOG.md#ana-3-stream-order-and-convergent-flow-on-flats)):
   Strahler or Shreve order over the channel tree, and the "away from higher
   terrain" gradient on flats.

## References

- Barnes, R., Lehman, C., Mulla, D. (2014). Priority-flood: an optimal
  depression-filling and watershed-labeling algorithm for digital elevation
  models. *Computers & Geosciences* 62, 117–127.
- Barnes, R., Lehman, C., Mulla, D. (2014b). An efficient assignment of
  drainage direction over flat surfaces in raster digital elevation models.
  *Computers & Geosciences* 62, 128–135.
- Garbrecht, J., Martz, L. W. (1997). The assignment of drainage direction
  over flat surfaces in raster digital elevation models. *Journal of
  Hydrology* 193, 204–213.
- O'Callaghan, J. F., Mark, D. M. (1984). The extraction of drainage networks
  from digital elevation data. *Computer Vision, Graphics, and Image
  Processing* 28, 323–344.
- Shreve, R. L. (1966). Statistical law of stream numbers. *Journal of
  Geology* 74, 17–37.
- Strahler, A. N. (1957). Quantitative analysis of watershed geomorphology.
  *Transactions of the American Geophysical Union* 38, 913–920.
- Tarboton, D. G. (1997). A new method for the determination of flow
  directions and upslope areas in grid digital elevation models. *Water
  Resources Research* 33, 309–319.
- Wang, L., Liu, H. (2006). An efficient method for identifying and filling
  surface depressions in digital elevation models for hydrologic analysis and
  modelling. *International Journal of Geographical Information Science* 20,
  193–213.
