# Validation: geodesy and measurement

Status: validated on `val/geodesy` (2026-11-14), against GeoTools develop at
38fd2e3 with rusty-tiles 0.4.1, pyproj 3.8.0 (PROJ 9.8.1) and Blender 5.2.4.
One bug was found and fixed (Measure in non-conformal CRSs); four points are
left for a decision, each with a recommendation
([Discrepancies](#discrepancies-left-for-a-decision)).

This covers the **Measure** sub-panel (`geotools/analysis/site.py` and its
operators in `geotools/blender/analysis.py`), the placement maths that analysis
and publishing share (the GeoScene frame, the offsets, latitude and longitude
sync, and where rusty-tiles puts published vertices), and the CRS handling of
the geo formats (GeoJSON, GeoPackage, GeoTIFF). For each tool it gives the
definition followed, the reference, the cases, the tolerances, the results and
a verdict.

## Verdicts

| Tool | Definition followed | Verdict |
|---|---|---|
| Point readout: E, N, RL | E = x + offset E, N = y + offset N, RL = z + A, binary64 offset plus the float32 local value | **agrees** (exact) |
| Point readout: latitude, longitude, datum | inverse projection in the CRS's own geodetic datum | **agrees** with Krüger's series to 3 nm |
| Point readout: ellipsoidal height | RL + height offset (how rusty-tiles places the mesh) | **agrees** (exact) |
| Point readout: float32 resolution | spacing of float32 at the largest local component | **agrees** |
| Point scale factor k | PROJ `get_factors` meridional scale; the scale along grid north in a non-conformal CRS | **agrees** with Krüger's closed form to 6×10⁻¹¹ |
| Grid convergence γ | PROJ `meridian_convergence`: true north to grid north, clockwise | **agrees** with Krüger (Karney's sign) to 2×10⁻⁶″ |
| Gaussian mean radius | √(MN) of the CRS's ellipsoid | **agrees** with M and N measured on GeographicLib geodesics to 4×10⁻¹¹ |
| Measure A→B: ground distance, CSF | ICSM survey reduction: grid / (k × R/(R+h)), k by Simpson's rule along the line | **agrees** (GeographicLib); **bug fixed** in non-conformal CRSs; documented convention differences (Gaussian radius, grid chord) |
| Measure A→B: grid and true bearing | grid bearing from +Y; true = grid + γ at the middle (conformal) | **agrees** with GeographicLib's midpoint azimuth; **bug fixed** in non-conformal CRSs |
| Measure A→B: height difference, slope distance | RL_B − RL_A; √(ground² + Δh²) | **agrees** (analytic) |
| `line_bearings` (archaeoastronomy) | as the true bearing above | **agrees**; **bug fixed** in non-conformal CRSs |
| Area of selected faces | per triangle: plan / CSF² (ground plan), horizontal / CSF and vertical kept (ground surface) | **agrees** with GeographicLib polygon areas; **bug fixed** in non-conformal CRSs |
| LOCAL / ANCHOR / SOURCE_CRS modes | LOCAL: no Earth; ANCHOR: rigid ENU frame at the anchor, heading counter-clockwise | **agrees** (ANCHOR against the closed form to 1 nm) |
| GeoScene frame and lat/lon sync | X = E − crs x, Y = N − crs y, grid north; lat/lon of the origin through rusty-tiles' CRS operation | **agrees** to 10⁻⁹° |
| Published placement (SOURCE_CRS → ECEF) | rusty-tiles' allow-listed operation, then ECEF | **agrees**: every vertex within 0.3 mm of the closed form at six sites |
| GLB metadata frame (`core.georef`) | one matrix: rusty-tiles' ENU frame times the measured grid axes | **discrepancy left for a decision**: one-sided probes tilt it |
| GeoJSON CRS and axis order | RFC 7946 lon/lat WGS 84; legacy `crs` member read; written for a projected CRS | **agrees** with GDAL 3.12.0 (identical numbers) |
| GeoPackage SRS and axis order | `srs_id` → `gpkg_spatial_ref_sys`; x = longitude/easting | **agrees** with GDAL; minor spec point on the `definition` text |
| GeoTIFF geotransform | GDAL's corner geotransform; PixelIsPoint shifted half a pixel; rotated refused | **agrees** with GDAL exactly |
| Import into the project frame and export back | binary64 reprojection, float32 local, binary64 offset back | **agrees**: 0.27 mm round trip, read back by GDAL |

## Bug fixed

**Measure in non-conformal CRSs** (commit "Measure: follow the line's direction
in non-conformal CRSs"). Measure took one point scale factor and one
convergence per point. That holds only in a conformal grid. rusty-tiles accepts
24 Albers equal-area CRSs and Web Mercator (EPSG:3857, conformal only on a
sphere; PROJ's factors for it are the sphere's). There an east-west kilometre in
EPSG:3577 at 12° S came out 3.9 % (39 m) too long, a north-east line's true
bearing 1.07° out, and EPSG:3857 distances at 60° N 0.08–0.25 % short.

Each point now carries the map from a grid step to the step on the ellipsoid:
the ellipsoid's metric diag(N cos φ, M) times the inverse of the projection's
Jacobian (PROJ's partial derivatives times the semi-major axis). Measure takes
k in the line's direction, the true bearing as the direction of the mapped
line, and areas through the map. Whether a CRS is conformal is tested once, at
the centre of its area of use: all 2,791 conformal projected CRSs rusty-tiles
accepts pass (to about 10⁻¹⁰) and keep PROJ's own k and γ exactly, poles
included; the 24 Albers CRSs and EPSG:3857 fail (by 10⁻³ or more) and use the
map. After the fix those sites agree with GeographicLib like the others (tables
below). Regression tests: `test_non_conformal_grid_follows_the_line_direction`
and `test_conformal_grids_keep_proj_scale_and_convergence` in
`tests/core/test_site.py`.

## Method

### Sites

All data are synthetic. Twelve sites, each a point placed by its latitude and
longitude, with the project offset rounded to a whole metre:

| Site | CRS | What it stresses |
|---|---|---|
| mga56-central | EPSG:7856 (GDA2020 / MGA 56) | near the central meridian |
| mga56-edge | EPSG:7856 | the zone's west edge, 150° E, 30° S (γ = 1.5°) |
| mga56-overlap | EPSG:7856 | 0.4° outside the zone |
| utm33n | EPSG:32633 | UTM, northern hemisphere |
| utm18s | EPSG:32718 | UTM, southern hemisphere, west longitudes |
| utm33n-arctic | EPSG:32633 | 78.4° N |
| utm33n-far | EPSG:32633 | 9° from the central meridian (k = 1.0089) |
| lambert93 | EPSG:2154 | Lambert conformal conic |
| antarctic-ps | EPSG:3031 | polar stereographic (variant B), 77.8° S |
| ups-north | EPSG:5041 | UPS North, 0.5° from the pole |
| albers | EPSG:3577 | Albers equal-area (not conformal), far from its standard parallels |
| webmercator | EPSG:3857 | Web Mercator at 60° N |

Per site: three points (the origin, one 1.2 km away, one 47 km away);
24 lines (lengths 10 m, 1 km, 10 km and 50 km, six grid bearings, heights 0
and 1,500 m, the end 37.5 m higher); a 1 km square, and a 10 km square turned
30° at heights 0 and 800 m. Local positions are float32 values, so E and N are
exact.

### Reference tools and versions

| Tool | Version | Used for |
|---|---|---|
| Krüger's series (`scripts/validation/geodesy/refgeo.py`) | to n⁶ (Karney 2011, as in the ICSM GDA2020 Technical Manual); constants typed from EPSG, not read from PROJ | TM forward and inverse (Newton), point scale factor, convergence; agrees with PROJ to 2 nm |
| GeographicLib (Python) | 2.1 | geodesic lengths and midpoint azimuths, polygon areas on the ellipsoid, M and N |
| pyproj / PROJ | 3.8.0 / 9.8.1 | the forward projection for the non-TM sites (Newton inverse and a central-difference Jacobian), ECEF for comparison |
| GDAL | 3.12.0 "Chicoutimi" with PROJ 9.7.0, Docker `ghcr.io/osgeo/gdal:ubuntu-small-3.12.0` (`sha256:9c3f7d8caf2a36762385d4868e198660a90b0c896f86b01fcab26953770b794e`) | GeoJSON, GeoPackage and GeoTIFF writing and reading, its own reprojection, `validate_gpkg` |
| ICSM GDA Technical Manual worked example | Flinders Peak to Buninyong (GRS80) | published 54 972.271 m, 306°52′05.37″, reverse 127°10′25.07″, Flinders Peak E 273 741.297, N 5 796 489.777 (MGA 55) |
| numpy | 2.5.4 | everything above |

The non-TM sites' references use PROJ's forward projection (not its inverse,
nor `get_factors`, which GeoTools uses). Every distance and area reference is
GeographicLib's, which is independent of the projection.

### Reproducing

Not run in CI:

```sh
# Measure references (Krüger, GeographicLib): tests/data/validation/geodesy/measure.json, 150 KB, 45 s
uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \
    --with geographiclib==2.1 python scripts/validation/geodesy/make_reference.py
# Placement references (closed-form ECEF): placement.json, 50 KB
uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \
    --with geographiclib==2.1 python scripts/validation/geodesy/make_placement_reference.py
# Geo formats with GDAL in Docker: formats/, 250 KB (two 96 KB GeoPackages)
scripts/validation/geodesy/run_formats.sh
# The tables below
uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \
    python scripts/validation/geodesy/report.py
```

`run_formats.sh` runs GDAL with `--cpus 3 --memory 6g --rm`: GDAL writes the
synthetic sources, GeoTools imports and exports them (`formats_geotools.py`),
and GDAL reads GeoTools' files back (`formats_gdal.py readback`).

Checks, in normal CI:

```sh
pytest tests/core/test_validation_geodesy.py tests/core/test_site.py   # 87 tests, about 3 s
python3 scripts/run_blender_tests.py --expect available -k test_validation_geodesy
```

The core CI job has no rusty-tiles, so the 8 tests that publish through it skip
there; they run wherever the rusty-tiles 0.4.1 wheel is installed, and the
Blender test `test_published_placement_against_the_closed_form` runs them in
the wheel job.

### Tolerances

- **Reference accuracy.** Krüger's series to n⁶ is good to a few nanometres
  within 4,000 km of the central meridian; Newton's inverse converges to
  10⁻⁹ m. The non-TM references take PROJ's forward projection with central
  differences (step 10⁻⁶°), good to about 10⁻⁸ in scale and 10⁻⁴″ in
  convergence. GeographicLib is accurate to 15 nm.
- **float32.** A local position is float32: 0.06 mm at 1 km from the scene
  origin, 1 mm at 10 km, 8 mm at 100 km, 0.5 m at an MGA northing.
- **The survey reduction.** Measure reduces the straight grid line (the
  chord), not the geodesic's image, which curves. The difference grows as the
  square of the length (below).
- **Areas.** One CSF per triangle, at its centroid: the error falls as the
  square of the triangle's size.

## Point readout

**Definition.** SOURCE_CRS: E = x + offset E, N = y + offset N, RL = z +
offset A, with x, y, z the float32 local position widened to binary64 and the
offsets binary64. Ellipsoidal height = RL + height offset. Latitude and
longitude by PROJ's inverse projection in the CRS's geodetic datum (GDA2020 for
MGA2020). k and γ from `get_factors`: γ is the angle from true north to grid
north, clockwise positive (PROJ's sign, Karney's, positive east of the central
meridian in the north). The ICSM manual uses the same magnitude; check the sign
convention of any other source before comparing.

**Cases.** Three points per site against Krüger (TM) or the Jacobian (other
projections); Flinders Peak against Krüger; a scene whose origin is the CRS
origin (local positions are full MGA coordinates).

| Site | lat/lon max error (m) | k max error | γ max error (″) |
|---|---|---|---|
| mga56-central | 7.9e-10 | 3.3e-11 | 4.8e-08 |
| mga56-edge | 1.6e-09 | 3.1e-11 | 7.9e-07 |
| mga56-overlap | 2.8e-09 | 2.1e-11 | 9.5e-07 |
| utm33n | 1.5e-09 | 6.1e-12 | 2.7e-07 |
| utm18s | 1.6e-09 | 4.6e-11 | 2.7e-07 |
| utm33n-arctic | 3.2e-09 | 5.9e-11 | 9.3e-08 |
| utm33n-far | 2.1e-09 | 6.2e-12 | 2.2e-06 |
| lambert93 | 7.9e-10 | 4.8e-09 | 6.2e-05 |
| antarctic-ps | 0.0 | 5.1e-09 | 7.1e-05 |
| ups-north | 1.6e-09 | 5.6e-08 | 1.8e-04 |
| albers | 3.4e-09 | 6.8e-09 | 1.6e-04 |
| webmercator | 1.6e-09 | 1.7e-08 | 0.0 |

The non-TM columns are the reference's own numerical-derivative noise. E, N,
RL and the ellipsoidal height are exact (asserted bit for bit), and the
resolution is 0.5 m for a scene whose local positions are MGA coordinates.

**Verdict: agrees.**

## Measure A→B

**Definition** (ICSM GDA2020 Technical Manual, grid and ground distances):
grid distance = plan distance in the scene; line scale factor k_L =
(k_A + 4 k_mid + k_B)/6; height factor = R/(R + h) with R = √(MN) at the middle
latitude and h the mean ellipsoidal height of the ends; CSF = k_L × height
factor; ground = grid / CSF; true bearing = grid bearing + γ at the middle.

**Cases.** 24 lines per site against GeographicLib: the ellipsoid distance
(grid / k_L) against the geodesic length between the ends, and the true
bearing against the geodesic's azimuth at its middle; the ground distance
against the geodesic raised by (R + h)/R, with R the Gaussian radius (the
convention) and with R the radius of curvature in the line's azimuth (Euler's;
rigorous for a normal section).

| Site | ellipsoid distance, max error (ppm), 10 m / 1 km / 10 km / 50 km | true bearing, max error (″), same lengths | ground vs Euler radius (ppm) |
|---|---|---|---|
| mga56-central | 1.8e-04 / 3.2e-05 / 3.1e-05 / 3.8e-05 | 4.4e-05 / 1.1e-04 / 0.011 / 0.27 | 0.31 |
| mga56-edge | 2.6e-04 / 3.6e-05 / 4.5e-04 / 0.011 | 3.2e-05 / 1.1e-04 / 0.010 / 0.26 | 0.27 |
| mga56-overlap | 9.5e-05 / 2.6e-05 / 5.3e-04 / 0.013 | 3.8e-05 / 1.1e-04 / 0.010 / 0.26 | 0.25 |
| utm33n | 3.3e-04 / 2.9e-06 / 5.5e-05 / 0.0014 | 3.0e-05 / 1.1e-04 / 0.011 / 0.26 | 0.17 |
| utm18s | 1.3e-04 / 4.8e-05 / 2.0e-04 / 0.0039 | 2.1e-05 / 1.1e-04 / 0.011 / 0.27 | 0.37 |
| utm33n-arctic | 2.4e-04 / 5.3e-05 / 6.0e-05 / 3.5e-04 | 4.4e-05 / 1.0e-04 / 0.010 / 0.26 | 0.01 |
| utm33n-far | 2.7e-04 / 2.9e-05 / 0.0025 / 0.063 | 3.2e-05 / 1.2e-04 / 0.012 / 0.29 | 0.12 |
| lambert93 | 1.4e-04 / 1.9e-05 / 3.4e-04 / 0.0088 | 3.0e-05 / 1.4e-04 / 0.014 / 0.36 | 0.18 |
| antarctic-ps | 1.1e-04 / 5.7e-05 / 0.0022 / 0.055 | 3.3e-05 / 3.2e-04 / 0.032 / 0.81 | 0.06 |
| ups-north | 1.3e-04 / 4.7e-05 / 4.9e-05 / 9.3e-05 | 1.3e-05 / 3.1e-04 / 0.031 / 0.78 | 0.00 |
| albers | 2.6e-04 / 1.3e-04 / 0.011 / 0.28 | 5.0e-05 / 1.6e-04 / 0.016 / 0.39 | 0.75 |
| webmercator | 2.8e-04 / 0.0016 / 0.15 / 3.8 | 4.8e-05 / 3.5e-04 / 0.034 / 0.86 | 3.80 |

Against the Gaussian-radius reference the ground distance differs exactly as
the ellipsoid distance does (to 10⁻⁹): the height reduction is implemented as
documented.

- Up to 10 km every site agrees to 0.015 ppm (0.15 ppm in Web Mercator) and
  0.035″. At 10 m the error is float32 (10⁻⁴ ppm is 1 nm).
- At 50 km the grid chord's image is not the geodesic: 0.06 ppm 9° from a TM
  central meridian, 0.28 ppm in Albers, and 3.8 ppm in Web Mercator at 60° N,
  where a straight grid line is a rhumb line. The true bearing is out by
  0.3–0.9″. This is the textbook survey reduction (the arc-to-chord terms are
  left out), and the m5 design note's "well under 1 ppm for lines of a few
  kilometres" holds.
- The Gaussian radius against the radius in the line's azimuth: at most
  0.4 ppm at 1.5 km of height (the radii differ by up to e′² cos² φ / 2 ≈ 0.3 %,
  times h/R). ICSM uses the Gaussian mean radius, as GeoTools does.

**ICSM worked example.** Flinders Peak's published MGA zone 55 coordinates and
Buninyong's (from its published latitude and longitude) in an EPSG:28355
project: GeoTools' ground distance at h = 0 is 54 972.2716 m against the
published 54 972.271 m (+0.6 mm, the rounding of the published values;
GeographicLib gives 54 972.2711) with a line scale factor of 1.000364. The
true bearing differs from the geodesic's midpoint azimuth by 0.32″ on this
55 km line. GeographicLib reproduces the published azimuths to 0.01″, and
Krüger the published E/N to the millimetre.

**Height difference and slope distance** are RL_B − RL_A and
√(ground² + Δh²), checked exactly in every line case (37.5 m) and in
`tests/core/test_site.py`.

**Verdict: agrees, with documented convention differences** (the Gaussian
radius; the chord, not the geodesic, for long lines). Bug fixed in
non-conformal CRSs (above).

## line_bearings

The archaeoastronomy alignments' true bearings and latitudes. Same definition
as Measure's true bearing; checked against Measure on every non-conformal case
(identical to 10⁻⁹°) and through Measure against GeographicLib. LOCAL takes +Y
as true north; ANCHOR subtracts the heading.

**Verdict: agrees.** Bug fixed in non-conformal CRSs.

## Area of selected faces

**Definition.** Per triangle, at its centroid: ground plan area = plan area /
CSF² (in a non-conformal grid, times the determinant of the grid-to-ground
map); ground surface area = the triangle with its horizontal components divided
by the CSF and its vertical component kept.

**Cases.** Each site's squares against GeographicLib's area of the same grid
polygon, its edges densified every 5 m (so the geodesic edges follow the grid
edges; checked against a 40 × 40 Gauss–Legendre integral of 1/k², which agrees
to 10⁻¹¹), raised by ((R + h)/R)²; a plane z = 0.3x against the closed form;
in Blender, a float32 1 km mesh through the operator.

| Site | 1 km square, 2 triangles | 10 km, 2 triangles | 10 km, 8 × 8 cells | 10 km, 32 × 32 cells |
|---|---|---|---|---|
| mga56-central | 1.3e-09 | 7.8e-08 | 1.2e-09 | 2.4e-11 |
| mga56-edge | 1.3e-09 | 7.7e-08 | 1.2e-09 | 2.0e-11 |
| mga56-overlap | 1.3e-09 | 7.7e-08 | 1.2e-09 | 4.1e-11 |
| utm33n | 1.4e-09 | 7.7e-08 | 1.2e-09 | 9.0e-11 |
| utm18s | 1.3e-09 | 7.8e-08 | 1.1e-09 | 1.8e-11 |
| utm33n-arctic | 1.5e-09 | 7.7e-08 | 1.3e-09 | 1.8e-10 |
| utm33n-far | 1.4e-09 | 7.5e-08 | 1.2e-09 | 6.9e-11 |
| lambert93 | 1.4e-09 | 2.0e-07 | 3.2e-09 | 2.2e-10 |
| antarctic-ps | 1.5e-09 | 1.4e-07 | 2.2e-09 | 2.3e-10 |
| ups-north | 1.5e-09 | 1.4e-07 | 2.2e-09 | 2.3e-10 |
| albers | 3.5e-11 | 1.9e-11 | 9.3e-12 | 8.8e-12 |
| webmercator | 1.8e-09 | 2.4e-07 | 3.9e-09 | 3.3e-10 |

(Relative errors of the ground plan area.) The error falls as the square of
the triangle size, as expected (Albers is equal-area, so its area scale barely
varies). The sloped plane agrees with the closed form to 1.6×10⁻⁹ (one CSF
for the whole plane against one per triangle).

Float32 matters for large faces: subdividing a 10 km square whose vertices are
rounded to float32 changes its grid area by 2×10⁻⁸ (the rounding of the edge
vertices). That is the mesh, not the reduction.

**Verdict: agrees.** Bug fixed in non-conformal CRSs.

## Gaussian mean radius

√(MN) = a √(1 − e²) / (1 − e² sin² φ), against M and N measured on
GeographicLib geodesics (a meridian arc and a parallel arc of 2×10⁻⁴°), for
GRS80 and WGS 84 at 0°, 27.5°, 45°, 60°, 78.4° and 89°: the largest difference
is 0.27 mm of 6,380 km (4×10⁻¹¹), the precision of the finite differences.

**Verdict: agrees.**

## Project modes

- **LOCAL**: E/N/RL are the local coordinates, ground = grid, no latitude
  (`tests/core/test_site.py`).
- **ANCHOR**: points move by the scene offset, then turn counter-clockwise by
  the heading, into east-north-up at the anchor. Two anchors (Brisbane with a
  30° heading, 78.4° N with −120° and a 2.5 km scene offset): 25 vertices each,
  Measure's latitude, longitude and height against the closed form (anchor ECEF
  + ENU basis × rotation × (local + offset)) to 10⁻¹⁰° and 1 µm; published
  through rusty-tiles, within 1.0×10⁻⁹ m of it. The convergence is −heading
  and the scale factor 1.
- **SOURCE_CRS**: as above.

**Verdict: agrees.**

## Placement maths shared with publishing

**GeoScene frame.** BlenderGIS's keys are the authority: X = E − `crs x`,
Y = N − `crs y`, +Y grid north, Z absolute height (A = 0 by default). GeoTools
reads `crs x`/`crs y` as exact binary64 text, so E = `crs x` + x exactly
(test). **Latitude and longitude sync** (`geoscene.origin_lon_lat`, written to
the `latitude`/`longitude` keys) goes through the definition rusty-tiles
resolves (null `+towgs84` for GDA2020, GDA94 and RGF93) to EPSG:4326: against
the reference at four sites (MGA 56, UTM 33N, Lambert-93, Albers GDA94) to
10⁻⁹°.

**Vertical offset and height offset.** The ellipsoidal height is z + A +
height offset, both in Measure and in publishing (checked through ECEF below).
With the height offset at 0 the height factor uses the RL; the m5 design note
gives the effect (N/R, 6 ppm for a 40 m geoid separation).

**The CRS operation recorded for publishing.** `rusty_tiles.resolve_source_crs`
names an allow-listed operation (EPSG:7856: "GDA2020 to WGS 84 (2)",
EPSG:8450, a null transformation, EPSG accuracy 3 m; EPSG:2154: "RGF93 v1 to
WGS 84 (1)"; EPSG:3577: "GDA94 to WGS 84 (1)"; WGS 84 CRSs: none), verified by
rusty-tiles against GDAL to 1 mm. pyproj picks the same null operations.

**Projected → ECEF.** A 5 × 5 vertex grid over ±5 km with 300 m of relief,
published lossless through rusty-tiles at six SOURCE_CRS sites (MGA 56 zone
edge, UTM 33N at 78° N, UTM 18S, Lambert-93, Antarctic polar stereographic,
Albers GDA94), decoded, and taken to ECEF with the tile transform in binary64,
against ECEF computed in closed form from Krüger (or Newton) latitude and
longitude and h = z + A + height offset:

| Site | max error (mm) | median (mm) | against pyproj's EPSG:nnnn → EPSG:4978 (mm) |
|---|---|---|---|
| mga56-edge | 0.25 | 0.12 | 0.31 |
| utm33n-arctic | 0.30 | 0.11 | 0.30 |
| utm18s | 0.28 | 0.15 | 0.28 |
| lambert93 | 0.24 | 0.12 | 0.29 |
| antarctic-ps | 0.24 | 0.08 | 0.24 |
| albers-gda94 | 0.28 | 0.10 | 0.29 |

The residual is float32 positions in the tile frame (a lossless tile at
±5 km: 0.5 mm at most). The reference's own ambiguity is 0.1 mm: under a null
datum shift GDA2020 and RGF93 coordinates are taken on GRS80 or WGS 84,
whose semi-minor axes differ by 0.1 mm.

**GLB metadata frame** (`core.georef.measure_frame`, the node matrix of a
`.glb` with Earth placement): see [the first discrepancy](#1-the-glb-metadata-frame-is-measured-one-sided).

**Verdict: agrees** (publishing); the GLB metadata frame is left for a
decision.

## Geo formats (M8)

GDAL 3.12.0 (PROJ 9.7.0) writes the sources; GeoTools reads them with its own
readers; GeoTools' exports go back to GDAL.

| Case | GeoTools | GDAL | Result |
|---|---|---|---|
| RFC 7946 GeoJSON (no `crs` member) | EPSG:4326, lon/lat | EPSG:4326, lon/lat | identical vertices |
| Legacy GeoJSON naming `urn:ogc:def:crs:EPSG::4326` (EPSG's axis order is lat, lon) | EPSG:4326, positions read lon, lat | the same | identical; not swapped |
| GeoPackage in EPSG:4326 (SRS definition with `AXIS["Latitude",NORTH]` first) | x = longitude | x = longitude | identical |
| GeoPackage in EPSG:7856 | EPSG:7856 | EPSG:7856 | identical |
| Reprojection of all four into EPSG:7856 | pyproj, "Inverse of GDA2020 to WGS 84 (2) + MGA zone 56" | GDAL/PROJ 9.7 | identical to the last bit (0 m) |
| GeoTools import → float32 project frame (offset 502 000, 6 961 000) → export as GeoJSON (legacy `crs` member) and GeoPackage | written | GDAL finds EPSG:7856 and reads exactly the written numbers; back to WGS 84 within 2.4×10⁻⁹° (0.27 mm) of the source | agrees |
| GeoTIFF PixelIsArea, north-up, EPSG:7856 | geotransform (502000, 2, 0, 6962000, 0, −2) | the same | identical |
| GeoTIFF PixelIsPoint (GDAL writes the tiepoint at the pixel centre) | shifted half a pixel | GDAL's default (`GTIFF_POINT_GEO_IGNORE=FALSE`) | identical |
| GeoTIFF south-up (positive dy) | (502000, 2, 0, 6961992, 0, 2), faces mirrored to face +z | the same | identical |
| GeoTIFF rotated (ModelTransformation with rotation terms) | refused: "rotated or sheared geotransform" | reads it | documented refusal |
| GeoTIFF geographic (EPSG:4326, 10⁻⁴° pixels) | EPSG:4326 | EPSG:4326 | identical |
| BigTIFF, 16 × 16 tiles, deflate, floating-point predictor, PixelIsPoint, EPSG:32633 | read by GeoTools' own decoder | GDAL | identical geotransform and pixel values |

For every GeoTIFF the DEM mesh's vertices are exactly GDAL's pixel centres
minus the offset (as float32); the nodata pixel and the one vertex whose faces
all touch it are dropped, as documented. GDAL's `validate_gpkg` accepts
GeoTools' GeoPackage.

**Verdict: agrees** (one minor spec point, below).

## Limits users should know

- **float32.** A Blender position holds about 7 significant digits. Keep the
  project offset near the site: 1 km from the origin is held to 0.06 mm, but a
  scene placed at the CRS origin holds MGA coordinates only to 0.5 m. Every
  reading shows its float32 resolution.
- **Long lines.** Measure reduces the grid chord. Beyond about 20 km the
  arc-to-chord terms reach 0.1 ppm in TM and Lambert grids, more in Albers,
  and 3.8 ppm at 50 km in Web Mercator at 60° N. True bearings are good to
  0.035″ up to 10 km, 1″ at 50 km.
- **Height factor.** With the height offset left at 0 the height factor uses
  the RL, not the ellipsoidal height (about 6 ppm per 40 m of geoid
  separation). The Gaussian radius against the azimuth's radius: below
  0.4 ppm at 1.5 km.
- **Non-conformal CRSs.** Measure is right there now, but a point's Scale is
  the scale along grid north only, and the sun and Aspect (True North) still
  turn directions by one convergence (discrepancy 3).
- **Rotated GeoTIFFs** are refused; warp them north-up in QGIS first.

## Discrepancies left for a decision

### 1. The GLB metadata frame is measured one-sided

`core.georef.measure_frame` builds the GLB's node matrix from three probes:
the bounds' centre, and the centre moved half the extent east and north. The
axes are secants from the centre, so they dip below the tangent plane by
step / 2R and carry half the change of k over the step. For a 10 km site that
tilts both axes by 4×10⁻⁴ rad and biases the scale by 18 ppm at the MGA zone
edge. Mapping the reference grid through the matrix: the vertex nearest the
centre is exact, the +E corner is 3.9 m off (Earth curvature over 7 km, which
no single matrix can follow), and the −E corner 7.8 m (twice that, from the
tilt). The design note says "exact at the centre, first order elsewhere"; with
the tilt the first-order term is wrong.

**Recommendation:** central differences: probe at the centre ± the step
(two more 10 ms probes) and take (P₊ − P₋) / 2d. That removes the tilt and the
scale bias and leaves the unavoidable curvature (3.9 m at 7 km, symmetric). It
changes the matrix in GLBs users may have published, so it is not changed
here.

### 2. `geotools:gridScale` is not the grid scale factor

The GLB metadata key `geotools:gridScale` (and `EcefFrame.scale`) is "true
metres per model metre along +X": about 1/CSF (0.99940 at the MGA zone edge
where k = 1.00063). The m4 design note defines it that way, but surveyors read
"grid scale factor" as k = grid / true, the inverse, and `EcefFrame.scale`'s
docstring calls it "the grid scale factor".

**Done here:** the docstring now says what the number is. **Recommendation:**
at the next metadata schema change, rename the key (for example
`geotools:metresPerUnit`) or publish k itself.

### 3. Sun and Aspect (True North) in non-conformal CRSs

`site.sun_site` gives one convergence, and the sun light (and the Aspect (True
North) preset) turn true azimuths into grid ones by subtracting it. In a
non-conformal grid the turn depends on the direction: in EPSG:3577 the sun's
grid azimuth is out by up to 1.1° at 150° E, 12° S (0.66° at 133° E, 25° S),
and in EPSG:3857 by up to 0.13°. Conformal CRSs are exact.

**Recommendation:** map the sun direction through the grid-to-ground map
`site` now computes (its inverse turns a true horizontal direction into a grid
one; the vertical is unchanged), or warn in the Sun panel that Albers and Web
Mercator are not conformal. Both change results users of those CRSs see; the
sun is outside this area's scope.

### 4. GeoPackage `definition` text

GeoTools writes the project CRS's text (`EPSG:7856`) in
`gpkg_spatial_ref_sys.definition`. The GeoPackage standard asks for WKT there
(or `undefined`). GDAL reads the file by `organization` and
`organization_coordsys_id` and its `validate_gpkg` accepts it, so nothing
fails today, but a strict reader could.

**Recommendation:** write pyproj's WKT1 (`CRS.to_wkt("WKT1_GDAL")`) when pyproj
is available, else `undefined`.
