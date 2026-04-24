# Changelog

All notable changes to this project should be documented in this file.

The format is based on Keep a Changelog and the project follows semantic versioning where practical.

## [0.1.0] - 2026-04-24

### Added

- GeoPackage import and write-back workflow.
- GeoJSON import tooling.
- OGC 3D Tiles import tooling.
- DEM import support with optional GDAL and NumPy integration.
- Analysis modules for terrain, LOS, shadow, attribute transfer, and surface tagging.
- Blender mesh attribute support for imported feature properties.

### Changed

- GeoPackage polygon import now skips degenerate polygon parts instead of aborting the full import.
- String mesh attributes are written using Blender-compatible byte values.
- Polygon and multipolygon mesh construction now share the same internal ring sanitation path.

### Fixed

- Importing `PYSU.gpkg` no longer fails on a degenerate MultiPolygon part with only two distinct exterior points.
- Blender 5.1 string mesh attribute writes no longer fail when assigning feature properties.
