# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows semantic versioning where practical.

## [Unreleased]

GeoTools is being rebuilt to create, analyse and publish OGC 3D Tiles with
rusty-tiles. The next release will be 0.3.0. See `docs/ROADMAP.md`.

### Added

- Packaging as a Blender extension (`geotools/blender_manifest.toml`), with no
  `bl_info`.
- Bundled rusty-tiles wheels, pinned to 0.4.0 in `pyproject.toml`, for Linux x64,
  Windows x64, macOS arm64 and macOS x64. `scripts/fetch_wheels.py` fetches them
  and `scripts/build_extension.py` builds one extension zip per platform.
- A GeoTools sidebar panel and a preferences panel that show the extension
  version and the bundled rusty-tiles version, with a Check Dependencies
  operator.
- A `bpy`-free `geotools/core` package with pytest tests that run without
  Blender.

### Changed

- Blender 5.2.0 is now the minimum version (was 5.1). Only the latest Blender
  release is supported.

### Removed

- The 0.2 GeoJSON, GeoPackage, DEM and 3D Tiles importers, the analysis tools,
  and the `api/` add-on registry. They remain on the `legacy-0.2` branch and the
  `v0.2.0` tag.
- pyproj and GDAL support, the built-in reprojection and the MapTiler fallback.
  Nothing needs to be installed beside the extension. CRS maths will be done by
  rusty-tiles at publish time.
- The BlenderGIS-derived code in `core/proj/`.

## [0.2.0] - 2026-06-05

### Added

- A first-class internal addon API and discovery registry under `api/`.
- A clean `addons/` hierarchy with `addons/data/` for data modules and `addons/analysis/` for analysis-tool addons.
- Per-tool analysis addon manifests so individual analysis tools can be enabled and disabled independently.

### Changed

- Moved GeoJSON, GeoPackage, 3D Tiles, DEM, and analysis implementation packages under `addons/`.
- Reworked the unified UI and addon preferences to build from discovered addon manifests instead of hard-coded package lists.
- Renamed the shared analysis implementation package from `addons/analysis/core/` to `addons/analysis/shared/`.

### Fixed

- Blender addon installation no longer fails on dynamic enum registration for `import_format` and `active_tool`.
- The pure-Python test suite now imports the relocated addon packages from their new `addons/` namespace.

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

[Unreleased]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/BenDyson-Arch/blender_geotools/releases/tag/v0.1.0
