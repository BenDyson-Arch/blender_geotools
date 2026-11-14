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
## Sun, sun hours, clear-sky insolation and shadow studies

[solar.md](solar.md): sun position (NREL SPA), ΔT, time zones, grid
convergence, Place Sun and Over a Day, Sun Hours, clear-sky insolation and
Shadow Study, against pvlib 0.16.1, Skyfield 1.55 with JPL DE440s, pyproj
3.8.0 geodesics and GRASS GIS 8.4.1 r.sun. All agree with the definitions
they claim; insolation is Ineichen–Perez (as pvlib), not r.sun's ESRA. No bugs
found. Open: sun directions in non-conformal CRSs, refraction at altitude,
and the Over a Day strength between keys.
