# Validation

Independent evidence that each analysis tool is correct and follows the GIS
definition it claims, or a statement of where and why it differs. One page per
area: the definitions, reference tools and versions, cases, tolerances,
results and a verdict per tool.

## Terrain shape and archaeological visualisation

[terrain.md](terrain.md): slope, slope (percent), aspect (grid and true north),
profile, plan (tangential) and mean curvature, rugosity, TPI, TRI, local
relief model, sky-view factor and positive and negative openness, live and
fast (bake), against closed forms, GDAL 3.12.0, WhiteboxTools 2.4.0 and
rvt-py 2.2.1. All agree; plan-curvature naming, TPI sampling and the horizon
search region are documented convention differences. No bugs found. Resolved:
the plan curvature preset is labelled Plan (Tangential) Curvature and the TPI
default is 96 samples.

## Geodesy and measurement

[geodesy.md](geodesy.md): Measure's point readout (E/N/RL, latitude and
longitude, scale factor, convergence, float32 resolution), distances, bearings
and areas, the Gaussian radius, the LOCAL/ANCHOR/SOURCE_CRS frames, the
GeoScene frame and lat/lon sync, where publishing puts vertices (ECEF), and the
CRS handling of GeoJSON, GeoPackage and GeoTIFF, against Krüger's series,
GeographicLib 2.1, the ICSM worked example and GDAL 3.12.0 at twelve synthetic
sites. One bug fixed (Measure in non-conformal CRSs: Albers, Web Mercator);
four points left for a decision (the GLB metadata frame's one-sided probes,
the `geotools:gridScale` name, the sun in non-conformal CRSs, the GeoPackage
`definition` text).

## Hydrology, contours and sections

[hydrology.md](hydrology.md): depression filling and depressions, flow routing,
flats, accumulation, Channel From and the Flow Paths object, drops, Flow Path
(Quick Look), contours, sections and their export, against closed forms, an
independent priority-flood on the same face graph, WhiteboxTools 2.4.0,
richdem 0.3.4, GDAL 3.12.0 and SciPy 1.18.1. All agree. One bug fixed
(duplicate contour and section segments where a mesh edge lies on the plane);
face-centre spill levels, three flow directions per face and flats routed
towards their exit are documented convention differences. Five points are left
for a decision, among them spurious shallow pits along V-channels that follow
mesh edges.
