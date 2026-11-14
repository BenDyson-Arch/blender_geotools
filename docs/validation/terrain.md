# Validation: terrain shape and archaeological visualisation

Status: validated on `val/terrain` (2026-11-12), against GeoTools 0.4.0-dev
(develop at 4ea4fe0) in Blender 5.2.4. The five open points were decided on
2026-11-14 and are [resolved](#decisions-resolved): the plan curvature preset
is labelled Plan (Tangential) Curvature, the TPI default is 96 samples, and the
rest is documented in analysis-presets.md.

This covers the Geometry Nodes presets of
[analysis-presets.md](../analysis-presets.md) under *Terrain shape* and
*Archaeological visualisation*, and the fast (bake) paths of the sky-view
factor and openness. For each tool it gives the definition followed, the
reference tools, the cases, the tolerances, the results and a verdict.

## Verdicts

| Tool | Definition followed | Verdict |
|---|---|---|
| Slope | angle of the face normal from +Z, `acos(n_z)` | **agrees** |
| Slope (Percent) | `100 tan(slope)`, no data on vertical and overhanging faces | **agrees** |
| Aspect | downslope azimuth, 0 = +Y (north), clockwise, as GDAL, WhiteboxTools, ArcGIS | **agrees**, with a documented convention difference (Flat Slope threshold) |
| Aspect (True North) | `true = grid + convergence`, PROJ meridian convergence | **agrees** (sign checked against the meridian in six CRSs) |
| Profile curvature | Florinsky (2016) vertical curvature k_v, + convex, 1/m | **agrees** (WhiteboxTools `ProfileCurvature`) |
| Plan (Tangential) Curvature | Florinsky (2016) horizontal curvature k_h, + convex, 1/m | **agrees, with a naming difference**: WhiteboxTools' `PlanCurvature` is the contour curvature; GeoTools' plan curvature is WhiteboxTools' `TangentialCurvature`, now in its label |
| Mean curvature | Florinsky (2016) H, + convex, 1/m | **agrees** (WhiteboxTools `MeanCurvature`) |
| Rugosity | surface area / planimetric area over a blurred neighbourhood | **agrees** (σ ≈ R/2 confirmed) |
| TPI | Weiss (2001): height minus the mean over an annulus | **agrees**; the default 96 samples give the full mean to 1.7% RMS (24, the earlier default, 7.6%) |
| TRI | Riley et al. (1999), `√Σ(z_i − z)²` | **agrees** (equal to `gdaldem TRI -alg Riley` to 6.5×10⁻⁷ m) |
| Local relief model | the trend-removal step of Hesse (2010) | **agrees, with a documented convention difference** (no purge step; disc, not RVT's square) |
| Sky-view factor | Zakšek et al. (2011) | **agrees** |
| Positive / negative openness | Yokoyama et al. (2002) | **agrees** |
| Fast (bake) paths | the same rays as the live presets | **agree** with the live presets |

No bugs were found. The [discrepancies](#decisions-resolved) were conventions
or documentation; the user's decisions on them are implemented: one label and
one default changed (TPI Samples 24 → 96), and the rest is documented.

## Method

### Surfaces

All inputs are synthetic (`tests/blender/_terrain_surfaces.py`):

- **planes** at slopes 0, 0.4, 0.6, 5, 30, 60 and 85° facing 18 azimuths
  (every 22.5°, plus 1° and 359°); a vertical wall and a face overhanging by
  30°; and the planes again 1, 10 and 100 km from the origin;
- a **cone** of 30° (constant slope, radial aspect), sampled on a grid and as a
  polar mesh (128 rays from the apex), as a peak and as a pit;
- a **sphere cap** (R = 5 m) and **bowl**, and a **paraboloid** z = ±0.25 ρ²
  (known curvature everywhere);
- a **vertical-walled cylindrical pit** (radius 1 m, depth 0.5 m, a 256-gon;
  not a height field);
- **"waves"**: a sum of four plane waves (amplitudes 0.06–0.30 m,
  wavelengths 1.2–4 m, four directions), on which every quantity has a closed
  form: the derivatives, hence slope, aspect and the curvatures, and the mean
  over a disc or annulus (a plane wave's mean over a disc of radius R is its
  centre value times 2 J₁(kR)/kR), hence TPI and the local relief model. Slopes
  reach 57°, curvatures 5 m⁻¹. Its DEM is 141 × 141 cells at 0.1 m.

A DEM becomes a mesh with one vertex per cell centre and two triangles per
square of four cell centres (split south-west to north-east), as the brief
asks. GeoTools' own GeoTIFF import makes **quads**; a quad's normal is the
mean of its two triangles' (checked: within 1.3×10⁻⁴°), so the "square" rows
below are what a quad DEM gives.

### Reference tools and versions

| Tool | Version | Used for |
|---|---|---|
| GDAL | 3.12.0 "Chicoutimi", Docker `ghcr.io/osgeo/gdal:ubuntu-small-3.12.0` (`sha256:9c3f7d8caf2a36762385d4868e198660a90b0c896f86b01fcab26953770b794e`) | `gdaldem slope` (Horn, ZevenbergenThorne), `aspect` (both), `TRI` (Riley, Wilson), `TPI`, `roughness` |
| WhiteboxTools | v2.4.0, via the `whitebox` Python package 2.3.6 | `Slope`, `Aspect`, `ProfileCurvature`, `PlanCurvature`, `TangentialCurvature`, `MeanCurvature`, `RuggednessIndex`, `SurfaceAreaRatio` |
| rvt-py (Relief Visualization Toolbox) | 2.2.1 (`rvt.vis`), numpy 2.2.6, scipy 1.15.3 | `sky_view_factor` (SVF, openness), `slrm` |
| Exact horizons | `_terrain_surfaces.waves_horizon` / `mesh_horizon` (numpy) | SVF and openness of the continuous surface and of the mesh itself |

WhiteboxTools 2.4.0 (open source) has no openness or sky-view factor tool, so
RVT is the reference for those. WhiteboxTools' `DiffFromMeanElev` was tried as
a TPI reference and dropped: it differs from an exact 11 × 11 mean by 2.8 mm
RMS (on values of ±0.07 m), which we could not explain. WhiteboxTools needs a
GeoTIFF with a projected CRS (it refuses one without geokeys, and given an
ASCII grid it writes some outputs as Int32), so the DEM is a Float32 GeoTIFF
tagged EPSG:32755 (only so the tools read metres).

### Reproducing

References (not run in CI; writes `tests/data/validation/terrain/references.npz`,
300 KB):

```sh
uv venv -p 3.13 /tmp/terrain-ref
uv pip install -p /tmp/terrain-ref/bin/python whitebox==2.3.6 numpy==2.2.6 scipy==1.15.3
uv pip install -p /tmp/terrain-ref/bin/python --no-deps rvt-py==2.2.1   # rvt.vis needs no GDAL
/tmp/terrain-ref/bin/python scripts/validation/terrain/make_references.py
```

It writes the DEM as an ASCII grid, converts it to a GeoTIFF and runs gdaldem
in Docker (`--cpus 4 --memory 6g --rm`), runs WhiteboxTools and rvt-py, and
stores crops of the outputs (the central 61 × 61 cells for window tools; every
second cell of the central 101 × 101 for horizon tools) with the exact
horizons (25 s).

Checks (both run in normal CI):

```sh
pytest tests/core/test_validation_terrain.py            # 16 tests, about 1 s
python3 scripts/run_blender_tests.py --expect available -k test_validation_
```

The Blender tests (`tests/blender/test_validation_terrain.py`, 13 tests) take
15–19 s in the full suite on a shared 16-thread machine (46 s at a load
average of 20). With
`GEOTOOLS_TERRAIN_REPORT=/path/report.json` set they also run the report-only
measurements (the grid cone, the fitted rugosity σ, larger samples) and write
every statistic in the tables below (about 90 s).

### Tolerances

- **float32.** Blender holds positions as float32 (24 bits): a normal is good
  to about 10⁻⁷, so slope and aspect to 10⁻⁵–10⁻³° (worse where `acos` is
  ill-conditioned, near level); heights near 0.5 m to 6×10⁻⁸ m, so a 3 × 3
  curvature at w = 0.1 m to about 10⁻⁵ m⁻¹.
- **Discretisation.** A facet's gradient is the linear interpolant's: first
  order, off the tangent plane at its centre by about h·|curvature|/3 rad.
  Horn and Zevenbergen-Thorne are second order. Evans-Young derivatives on a
  3 × 3 window are exact for quadratics.
- **Bisection.** The horizon presets resolve each direction's angle to half a
  bracket: ±0.35° at Steps 8 (sin of that in SVF, 0.006).
- **Comparing a mesh with a raster.** Window tools are compared where both are
  defined (3 cells inside the crop), horizon tools more than Max Distance from
  any edge.

## Slope and Slope (Percent)

**Definition.** `slope = acos(n_z)` of the unit face normal, 0–180°; `slope_percent =
100 √(n_x² + n_y²)/n_z`, no data where n_z ≤ 10⁻⁶. This is the steepest
gradient of the face's plane; for a raster, GDAL and WhiteboxTools estimate the
same quantity from finite differences.

| Case | Result |
|---|---|
| 126 planes (2,268 faces), 0–85° | max error 7.4×10⁻⁴° (median 3×10⁻⁵°); percent within 3.4×10⁻⁵ relative |
| wall, overhang | 90°, 120°; percent no data |
| the planes moved to (1, 1), (10, 10) and (100, 100) km | max 0.08° / 1.3° / 9.8° (0.1 m facets in float32) |
| cone 30°, facets more than 1 m from the apex | median 0.11°, max 0.88°, bias −0.004° |
| cone, facets within 0.3 m of the apex | median 1.5°, max 11° |

On the waves DEM (5,832 facets; 2,916 squares):

| Compared | median | RMS | bias |
|---|---|---|---|
| GeoTools facet vs true slope at its centre | 0.80° | 1.21° | −0.06° |
| GeoTools square (quad) vs truth at its centre | 0.16° | 0.18° | −0.13° |
| WhiteboxTools (5 × 5) at cells vs truth | 0.04° | 0.05° | −0.04° |
| GDAL Zevenbergen-Thorne at cells vs truth | 0.33° | 0.36° | −0.27° |
| GDAL Horn at cells vs truth | 0.47° | 0.50° | −0.39° |
| GeoTools square vs WhiteboxTools square | 0.20° | 0.23° | +0.17° |
| GeoTools square vs GDAL ZT square | 0.48° | 0.53° | +0.39° |
| GeoTools square vs GDAL Horn square | 0.61° | 0.65° | +0.51° |

GeoTools measures the slope of the mesh's own faces, exactly. On a fine
triangle mesh of a curved surface, a facet differs from the tangent plane at
its centre by about h·|curvature|/3 (first order), up to 4° here. A quad, or the
mean of a square's two triangles, is a central difference and agrees with the
truth as well as the raster tools do. GDAL's Horn and Zevenbergen-Thorne
underestimate slope on curved ground (they smooth over 3 × 3 cells), which is
most of the GDAL–GeoTools difference. The cone shows the same effect near a
sharp apex. Use **Smoothing** for raster-like values on photogrammetry
meshes.

**Verdict: agrees.** Both tools follow the stated definition exactly; the
differences from raster tools are the expected first-order (facet) versus
second-order (raster stencil) discretisation, quantified above.

## Aspect and Aspect (True North)

**Definition.** `aspect = (atan2(n_x, n_y) + Convergence) mod 360`: the
downslope direction, 0 = +Y (north), 90 = east, clockwise, as GDAL
(`gdaldem aspect`, without `-trigonometric`), WhiteboxTools and ArcGIS.
No data where the slope is below Flat Slope (0.5°).

| Case | Result |
|---|---|
| 1,620 tilted faces, 18 azimuths, 0.6–85° | max 7.6×10⁻⁴°; all in [0, 360) |
| level faces and 0.4° faces | no data (NaN); 0.6° faces have an aspect |
| Flat Slope 0 | every non-level face has an aspect; level ones none |
| Convergence −10, +10, 180 on a face at 1° | 351°, 11°, 181° (wraps into [0, 360)) |
| wall facing +X; overhang facing −Y | 90°, 180° |
| cone 30°, facets more than 1 m out | median 0.24°, max 1.2° from radial |
| waves, facets steeper than 5° | median 2.5°, RMS 5.3° from the truth (facets) |
| waves, squares steeper than 5° | median 0.24°, RMS 0.51° from the truth |
| waves, GeoTools square vs WhiteboxTools / ZT / Horn | median 0.34° / 0.75° / 0.89° |

GDAL and WhiteboxTools use the same origin and direction (the reference check
`test_reference_conventions` confirms it on the waves DEM: median differences
from the true azimuth 0.09–0.67°).

**Flat areas (convention difference).** GDAL writes no data (−9999) only where
the gradient is exactly zero (or 0 with `-zero_for_flat`); GeoTools leaves faces
flatter than Flat Slope (default 0.5°) without an aspect, because a face's
aspect below half a degree is noise on photogrammetry. Set Flat Slope to 0 for
GDAL's behaviour.

**True north.** `tests/core/test_validation_terrain.py` checks the convergence
GeoTools reads from PROJ against the meridian itself (the grid bearing of a
point 10⁻⁶° north along it) in UTM 33N east and west of the central meridian,
MGA zone 56 east and west, Antarctic polar stereographic (EPSG:3031, 75° S) and
NSIDC north polar stereographic (EPSG:3413, 80° N): agreement within 10⁻⁴° in
every case, so `true = grid + convergence` has the right sign in both
hemispheres and both sides of the meridian. The preset uses one convergence per
object (at its bounding-box centre); across a 10 km site at 30° latitude it
varies by about 0.05°.

**Verdict: agrees**, with the Flat Slope convention above.

## Curvature: profile, plan (tangential) and mean

**Definition.** Evans-Young derivatives from a 3 × 3 window of spacing w,
sampled by vertical rays (docs: Florinsky 2016, eq. 4.4–4.8), then
k_v (profile), k_h (plan) and H (mean) with positive convex. The formulas were
checked symbolically against a sphere (all three = 1/R) and a paraboloid
z = cρ² (k_v = −2c/(1+g)^1.5, k_h = −2c/(1+g)^0.5, the curvatures of a surface
of revolution), in `test_curvature_formulas_on_a_sphere_and_a_paraboloid`.

Analytic surfaces (81 × 81 at 0.05 m, relative error, vertices more than 0.05 m
from the apex):

| Surface | w = 1 spacing (max) | w = 2 spacings (max) | w = 2.5 spacings (bias) |
|---|---|---|---|
| sphere R = 5 m (convex) | 1.3×10⁻³ | 5.8×10⁻⁴ | +4.0 % |
| bowl R = 5 m (concave, values −1/R) | 1.4×10⁻³ | 6.3×10⁻⁴ | +4.0 % |
| paraboloid dome / bowl | 3.1×10⁻⁴ | 7.7×10⁻⁵ | +4.0 % |
| paraboloid apex (level) | profile, plan: no data; mean −2c within 10⁻³ | | |

On the waves DEM (h = 0.1 m, w = 0.1 m; 3,025 cells; RMS of the true
curvature 0.74–0.99 m⁻¹):

| Compared | profile | plan | mean |
|---|---|---|---|
| GeoTools vs Evans-Young on the raster (numpy), max | 5.5×10⁻⁵ | 5.7×10⁻⁵ | 3.1×10⁻⁵ |
| GeoTools vs truth, RMS (median) | 0.053 (0.012) | 0.056 (0.025) | 0.018 (0.012) |
| WhiteboxTools vs truth, RMS | 0.081 | 0.095 (tangential) | 0.073 |
| GeoTools vs WhiteboxTools, RMS | 0.076 | 0.082 (tangential) | 0.055 |
| GeoTools vs **minus** WhiteboxTools, RMS | 1.58 | 1.87 | 1.39 |

With the window equal to the vertex spacing the rays land on vertices, and
GeoTools computes exactly the Evans-Young derivatives of the DEM (to float32).
WhiteboxTools 2.4 uses a 5 × 5 window (its two outer rows are degraded), so
the two differ by discretisation only; the sign test (last row) shows they share
the convex-positive sign and the unit (1/m).

**Plan curvature naming.** WhiteboxTools' `PlanCurvature` ("plan (contour)
curvature") matches the contour curvature k_h / sin(slope) (median difference
0.19 m⁻¹ against a median of 1.9 m⁻¹, larger only where it diverges on level
ground), not GeoTools' plan curvature; WhiteboxTools' `TangentialCurvature` is
GeoTools' plan curvature. Shary (1995) and Florinsky (2016) also use *plan
curvature* for the contour curvature and call k_h *horizontal curvature*. The
GeoTools docs say what is computed ("the horizontal (tangential) curvature …
the contour curvature times sin(slope)"), so this is a naming difference, not
a bug. **Resolved:** the preset is labelled Plan (Tangential) Curvature, and
analysis-presets.md names the WhiteboxTools and SAGA equivalents
(`TangentialCurvature`) and says their `PlanCurvature` is the contour
curvature; the id `curvature_plan` is unchanged.

**Window and mesh spacing.** At an integer number of spacings on a grid the
rays land on vertices and the error is the Evans-Young truncation (10⁻⁴–10⁻³
above). At 2.5 spacings every window point falls mid-facet, where the facets'
chords miss the curved surface, and the curvature is overstated by
(h/w)²/4 = 4.0 % on all four surfaces. On an irregular photogrammetry mesh the
window points land anywhere, so expect a bias of up to about (h/w)²/4: 1 % at
w ≈ 5 spacings, 0.25 % at w ≈ 10. **Resolved:** analysis-presets.md states
this rule (about no bias on vertices, up to (h/w)²/4 mid-facet) in place of
the earlier half- against whole-spacing sentence, with the advice to use a
window of at least five spacings.

**float32 heights.** The window reads heights in float32 and divides their
differences by w². On the sphere (R = 5 m, spacing 0.05 m) moved up:

| Height of the surface | w = 0.05 m: max (RMS) relative error | w = 0.2 m |
|---|---|---|
| near 5 m | 0.09 % (0.03 %) | 0.10 % |
| near 105 m | 1.3 % (0.4 %) | 0.17 % |
| near 1,005 m | 9.5 % (3.6 %) | 0.6 % |

**Resolved:** analysis-presets.md has this table, the rule of thumb and the
advice (keep heights near 0 with the project's Offset A, or widen the window).

**Verdict: agrees** (profile, plan and mean), with the plan-curvature naming
difference above, now in the label.

## Rugosity

**Definition.** `blur(A) / blur(A |n_z|)` over face areas A: surface area over
planimetric area in a neighbourhood (Jenness 2004 computes the same ratio
from a 3 × 3 cell; WhiteboxTools' `SurfaceAreaRatio`).

| Case | Result |
|---|---|
| plane at 30° | 1/cos 30° within 10⁻⁵ (existing test) |
| waves, Radius 0 (no blur) | each facet's own 1/cos(slope) within 3.5×10⁻⁷ |
| waves, Radius 0.1 m (2 iterations) vs WhiteboxTools SAR | RMS 0.0041, bias −1×10⁻⁵ |
| waves, Radius 0.1 m vs local truth √(1 + \|∇z\|²) | RMS 0.0072 (WhiteboxTools: 0.0090) |
| waves, Radius 0.3 m vs WhiteboxTools SAR | RMS 0.019 (a wider neighbourhood than 3 × 3 cells) |
| fitted Gaussian σ of the neighbourhood, Radius 0.2 / 0.4 m | 0.10 / 0.19 m |

The docs' claim that the blur spreads like a Gaussian of σ ≈ R/2 holds
(fitted, report-only: on a grid every facet has the same plan area, so the
preset's ratio is the blurred facet ratio, and a normalised Gaussian blur of
the facets' 1/cos(slope) reproduces it best at σ = 0.50 R and 0.48 R).

**Verdict: agrees.**

## Topographic Position Index (TPI)

**Definition.** Weiss (2001): `TPI = z − mean(z)` over an annulus Inner–Outer
Radius, the mean taken over Samples points on a sunflower pattern (pairs
symmetric about the vertex, even areal density) read from the mesh by vertical
rays. GDAL's `gdaldem TPI` is the 8-neighbour special case (z minus the mean
of the 3 × 3 ring).

On the waves DEM (121 vertices more than 2 m inside the crop), with the
default of 96 samples:

| Compared | Default (disc 0.5 m) | Annulus 0.3–0.8 m |
|---|---|---|
| GeoTools vs its sampling of the mesh (numpy), max | 8×10⁻⁸ m | 1.1×10⁻⁷ m |
| GeoTools vs the continuous Weiss mean, RMS (max) | 1.16 mm (2.7 mm) | 1.86 mm (3.7 mm) |
| of which: 96 samples vs the continuous mean, RMS | 0.80 mm | 1.77 mm |
| of which: mesh interpolation, RMS | 0.99 mm | 0.52 mm |
| RMS of the TPI itself | 48 mm | 109 mm |

With 24 samples, the default until this validation, the same rows were
3.7 mm (9.0 mm), 3.5 mm and 0.8 mm for the disc and 5.9 mm (13 mm), 6.1 mm
and 0.5 mm for the annulus.

The preset computes exactly what it documents. Its sampling is the
difference from the continuous definition (the paraboloid, where any
point-symmetric pattern with the right second moment is exact, hides this).
**Sample count** (resolved): the sunflower mean against the exact mean over
1,681 points of the waves DEM, as RMS of the TPI (numpy; the test
`test_tpi_default_samples_give_the_weiss_mean` checks the default):

| Samples | 24 | 32 | 40 | 48 | 64 | 72 | 80 | 88 | **96** | 128 | 256 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| disc 0.5 m | 7.6 % | 5.1 % | 3.9 % | 3.3 % | 3.0 % | 2.7 % | 2.4 % | 2.1 % | **1.7 %** | 1.4 % | 0.6 % |
| annulus 0.3–0.8 m | 5.7 % | 4.7 % | 3.9 % | 3.2 % | 2.2 % | 1.9 % | 1.9 % | 1.8 % | **1.7 %** | 0.9 % | 0.6 % |
| disc 0.3 m | | | | 3.4 % | 3.5 % | 3.3 % | 2.9 % | 2.4 % | **1.8 %** | 1.5 % | |
| disc 1.0 m | | | | 3.2 % | 1.1 % | 0.9 % | 1.2 % | 1.6 % | **1.7 %** | 1.1 % | |

96 is the smallest count that brings the default disc to 2 % or less (48 to 88
stay at 2–3.3 %), so it is the default now. The error does not fall evenly with
Samples (the 1 m disc is better at 64 than at 96): the pattern's radii beat
against the terrain's wavelengths, so judge a count over several radii. On the mesh, its linear
interpolation adds about 2 % at a radius of five spacings (1.16 mm RMS in
all, 2.4 %). Cost: rays per vertex grow with Samples. With
`scripts/bench_analysis_presets.py`'s terrain on this machine while it was
shared (load average 17–30), 1M triangles took 1.8–2.0 s at 24 samples, 4 s at
48 and 8.8 s at 96 (16.6 s in a run at higher load); 10M triangles 25 s at 24
and 76 s at 96.

**GDAL.** For a smooth surface every window's TPI is, to leading order,
`−½ ⟨x²⟩ ∇²z`, so windows with the same second moment ⟨x²⟩ agree. GDAL's
8-neighbour ring has ⟨x²⟩ = 0.75 h², the same as a disc of radius √3 h =
0.173 m. GDAL's TPI equals the continuous disc mean of that radius to 3×10⁻⁵ m
RMS (0.5 % of its RMS of 6.5 mm), and GeoTools with that radius and 256 samples
equals GDAL to 1.5 mm RMS: at a radius under two spacings, the samples fall
mid-facet and the mesh's linear interpolation is as large as the TPI itself.
Use a radius of several mesh spacings.

**Verdict: agrees.** It samples the disc or annulus rather than reading every
cell; at the default 96 samples that gives Weiss's mean to about 2 % RMS.
GDAL's 3 × 3 ring is a different window (matched above by its second moment).

## Terrain Ruggedness Index (TRI)

**Definition.** Riley et al. (1999): `TRI = √Σ (z_i − z)²` over the eight
neighbours of a 3 × 3 window of spacing Window. GDAL 3.12's default
(`gdaldem TRI`, and `-alg Riley`) is this. GDAL's `-alg Wilson` is the mean of
|z_i − z| (Wilson et al. 2007, for bathymetry), and WhiteboxTools 2.4's
`RuggednessIndex` is the root *mean* square, Riley/√8 (all three confirmed on
the waves DEM in `test_reference_conventions`).

| Case | Result |
|---|---|
| planes at 30°, four azimuths | w tan β √6 within 7×10⁻⁶ relative |
| plane edges | no data on the boundary ring (the window leaves the mesh) |
| waves, Window = cell size, vs `gdaldem TRI -alg Riley` | max 6.5×10⁻⁷ m (TRI RMS 0.13 m) |
| waves, vs √8 × WhiteboxTools `RuggednessIndex` | max 6.5×10⁻⁷ m |
| waves, vs GDAL Wilson | 3.2 × larger (median ratio): a different definition |

**Verdict: agrees** (exactly Riley's, and GDAL's default).

## Local Relief Model

**Definition.** `LRM = z − mean(z)` over a disc of Radius (48 samples): the
trend-removal step of Hesse (2010), documented as such (no purge step). RVT's
`slrm` ("simple local relief model") is the same step with a square
(2r + 1)² mean filter (radius 10–50 cells).

| Compared | Result |
|---|---|
| GeoTools (Radius 2 m, 48 samples) vs its sampling of the mesh, max | 2.1×10⁻⁷ m |
| vs the continuous disc mean, RMS (max) | 7.5 mm (16 mm), LRM RMS 229 mm (3.3 %) |
| GeoTools at a disc of matched ⟨x²⟩ (R = 1.211 m, 256 samples) vs RVT `slrm` 21 × 21 cells | RMS 3.2 mm, max 7.3 mm (RVT RMS 146 mm: 2.2 %) |
| the same GeoTools vs the continuous disc mean | RMS 1.2 mm |
| RVT `slrm` vs z minus an exact 21 × 21 mean (numpy) | 2×10⁻⁸ m |

The remaining 2 % between GeoTools and RVT is the kernel's shape (disc versus
square) beyond the second moment.

**Verdict: agrees, with documented convention differences**: trend removal
only (as the docs say), and a disc where RVT uses a square.

## Sky-view factor and openness

**Definitions.** Zakšek et al. (2011): `SVF = 1 − (1/N) Σ sin(max(γ_i, 0))`;
Yokoyama et al. (2002): positive openness `90° − mean γ_i`, negative openness
`90° + mean γ′_i` (γ′ the lowest angle). RVT computes exactly these (negative
openness as the positive openness of −z, which is the same thing). Both search
16 azimuths from north; GeoTools bisects each with 8 rays, RVT scans pixels.

**Analytic cases** (live and fast give the same values):

| Case | SVF | Positive openness | Negative openness |
|---|---|---|---|
| level plane (existing tests) | 1 | 90.35° (90 + half a bracket) | 90.35° |
| apex of a 30° peak (polar mesh) | 1 (exact 1) | 119.88° (120) | 60.12° (60) |
| apex of a 30° pit (polar mesh) | 0.5018 (0.5) | 60.12° (60) | 119.88° (120) |
| apex of a 30° peak or pit sampled on a grid | 0.458 (pit) | 57.13° (pit) | 57.13° (peak) |
| cylindrical pit, centre of the floor | 0.5559 (exact 0.5542 from 2 mm above the floor; the 8-step bisection of that horizon gives 0.5559) | | |
| cylindrical pit, 2,561 floor vertices vs the exact horizon run through the same bisection | median 3×10⁻⁸, max 1.3×10⁻³ | | |

On a grid, a cone's apex facets are up to 2° steeper than the cone between the
grid axes, and the horizon presets see that steeper mesh, as they should.

**Waves DEM** (961 vertices, Max Distance 2 m = 20 cells, 16 directions;
mesh-exact on 256 of them):

| Compared | SVF: RMS (max), bias | Positive openness | Negative openness |
|---|---|---|---|
| fast vs live | 1.2×10⁻⁸ (1.2×10⁻⁷) | 0 | 0 |
| live vs the mesh's exact horizon | 7.5×10⁻⁴ (2.0×10⁻³) | 0.050° (0.13°) | 0.051° (0.15°) |
| the mesh's exact vs the continuous surface's | 0.0051 (0.019) | 0.45° (2.9°) | 0.42° (1.7°) |
| live vs the continuous surface | 0.0052 (0.023), +0.002 | 0.45° (2.9°), +0.19° | 0.44° (3.0°), +0.19° |
| RVT vs the continuous surface (RVT's definition) | 0.018 (0.047), −0.014 | 1.22° (3.2°), −1.00° | 1.24° (3.0°), −1.01° |
| GeoTools' definition vs RVT's (continuous surface) | +0.0040 | +0.32° | +0.32° |
| live vs RVT | 0.024 (0.056), +0.021 | 1.75° (4.2°), +1.50° | 1.78° (4.3°), +1.53° |
| spread (SD) of the values | 0.093 | 6.9° | 6.9° |

Reading the table: the presets compute the horizon of the mesh to within the
bisection's half bracket (an implementation check). The mesh's horizon differs
from the smooth surface's where the horizon is set close to the vertex, by the
facets' first-order error (as for slope). The GeoTools–RVT difference has three
parts:

1. **RVT's pixel sampling** (−0.014 SVF, −1.0° openness against its own
   definition on the continuous surface): RVT rounds each search point to the
   nearest pixel, so it samples a fan of pixels around each azimuth, and a
   maximum over more points is higher (a minimum lower).
2. **Search region** (+0.004 SVF, +0.32°): GeoTools' Max Distance is the length
   of the ray (a sphere around the origin) and its rays start Ray Offset
   (2 mm) along the normal; RVT's radius is horizontal and it skips the first
   cell (its noise radius is at least one pixel).
3. **The mesh** (+0.002 SVF, +0.19°, see above).

**Verdict: agrees** (SVF, positive and negative openness). The differences from
RVT are explained, mostly by RVT's own pixel sampling.

## Fast (bake) paths

The fast paths of SVF and openness cast the same rays with Cycles: identical
to the live presets on all 961 waves vertices in the report run (an earlier
run on 2,601 vertices showed one differing by 0.010 SVF, the documented float32
grazing case),
within 1.2×10⁻⁷ on the cylindrical pit, and equal on the cone apexes. The
existing `test_preset_bake.py` covers the level plane and the V-trench.

**Verdict: agree** with the live presets.

## Limits users should know

- **Facets are first order.** A face's slope and aspect are exact for that
  face, but on a fine mesh of curved ground a single triangle can differ from
  the tangent plane by several degrees. Quads (GeoTIFF import) and Smoothing
  give raster-like values.
- **Curvature window.** Use a window of at least five mesh spacings; on
  irregular meshes expect a bias of up to (h/w)²/4 (documented).
- **Heights far from 0.** Window presets difference float32 heights: keep
  the scene's heights near 0 (the GeoScene/project offset A) or use a larger
  window. At z ≈ 1,000 m a 0.05 m window gives curvature errors near 10 %
  (documented).
- **Positions far from the origin.** At 10 km from the scene origin 0.1 m
  facets carry slope errors of about 1° (float32); keep meshes near the origin
  (imports do).
- **TPI and LRM sampling.** TPI's default 96 samples (LRM's 48) approximate the
  disc mean to about 2 % (3 %) RMS on smooth terrain; raise Samples when the
  values are used quantitatively, and use a radius of several mesh spacings.
- **Horizon search region.** Max Distance is a ray length, not a horizontal
  radius; on steep ground GeoTools searches slightly less far than RVT with
  the same number (documented).
- **2.5D sampling.** TPI, LRM, TRI and curvature read heights with vertical
  rays within ±Search Height: on slopes where the rise across the window
  exceeds Search Height the rays miss (window presets give no data; disc
  presets use the samples that hit). Documented in analysis-presets.md.

## Decisions (resolved)

The validation left five documentation and labelling choices. The user's
decisions (2026-11-14) are implemented on this branch:

1. **"Plan curvature" means different things.** GeoTools' plan curvature is
   k_h (WhiteboxTools' `TangentialCurvature`, Florinsky's *horizontal*
   curvature); WhiteboxTools' `PlanCurvature`, SAGA's *Plan Curvature* and
   Shary and Florinsky's *plan* curvature are the contour curvature
   k_h/sin(slope). **Done:** the computation is unchanged; the preset is
   labelled "Plan (Tangential) Curvature" (node group *GeoTools Plan
   (Tangential) Curvature*), and analysis-presets.md names the WhiteboxTools
   and SAGA equivalents. The id and layer `curvature_plan` are unchanged, so
   existing files and scripts keep working (a file that already holds the
   group keeps its old name; Add Analysis finds it by id).
2. **The curvature window sentence**, which compared a half-integer and an
   integer multiple of the spacing. **Done:** analysis-presets.md states the
   rule: about no bias when the rays land on vertices, up to (h/w)²/4 when they
   land mid-facet (as on irregular meshes), and a window of at least five
   spacings in practice.
3. **float32 heights in curvature.** **Done:** analysis-presets.md has the
   table (up to 9.5 % at 1,000 m with a 0.05 m window), the rule of thumb and
   the advice: keep heights near 0 with the project's Offset A, or widen the
   window.
4. **TPI default of 24 samples** (7.6 % RMS from Weiss's mean). **Done:** the
   default is 96, the smallest count within 2 % (1.7 %; see
   [TPI](#topographic-position-index-tpi) for the measurements and the time
   cost). TPI results change; the CHANGELOG says by how much. The preset
   stays version 1, so existing modifiers keep their Samples and are still
   listed; a file that already holds the TPI group keeps its default of 24 for
   new layers.
5. **Horizon search region.** **Done:** analysis-presets.md says that Max
   Distance is measured along the ray, and that RVT's search radius is
   horizontal and skips the nearest cell.
