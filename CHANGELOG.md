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
- Project placement on the scene: local, WGS84 anchor with heading, or source
  CRS with E/N/A source offset and height offset, stored as binary64 text so
  large offsets keep full precision.
- Source ingest for GLB, glTF, OBJ and PLY into a source collection, with
  E/N/A offset files (rusty-tiles' `--source-offset-file` format, written by
  hand) detected next to the source or loaded explicitly.
- Checks of the source against rusty-tiles' mesh input rules, listed by
  severity in the sidebar.
- Verify Placement, which runs rusty-tiles on one triangle spanning the source
  bounds and shows the WGS84 origin, or why the CRS is refused.
- Build profiles (profile, jobs, memory budget, output path), stored for
  publishing.
- Publish: the source collection's evaluated meshes are written to one
  deterministic GLB that rusty-tiles' strict mesh input admits, tiled with
  `mesh_to_tileset` on a worker thread with progress and cancel, validated, and
  recorded in `<output>.geotools/build.json`. A failed or cancelled rebuild
  leaves the previous archive untouched.
- An automatic rusty-tiles memory budget when a build profile's memory is 0:
  60% of the memory available at publish time minus 4 GiB for Blender, shown in
  the profile. rusty-tiles' own default (at most 4 GiB) runs its coarse-level
  stage one job at a time on a workstation.
- Face map (`facemap.npz`): every rusty-tiles leaf triangle's
  `(source_primitive, source_triangle)` back to its Blender object and polygon.
- Optional local preview through the rusty-tiles CLI and a Cesium runtime set in
  preferences.
- Opt-in image remedies for images rusty-tiles 0.4.0 refuses (over 32 MiB
  encoded, over 16384 px, 16-bit): re-encode as JPEG, downscale, or convert to
  8-bit, applied only to the exported copy. Unsaved generated or painted images
  publish with their current pixels.
- CRS presets for GDA2020 MGA zones 46–59 and NZTM2000 as PROJ definitions the
  bundled rusty-tiles accepts, each stating its datum assumption.
- A layer registry (M3): named, typed values per face, face corner or texel of
  the source, with a display label, units, no-data, the range of values
  present, origin, provenance (tool, parameters, GeoTools version) and
  coarse-level aggregation, stored in the `.blend`. Face and corner layers live
  in `gt_<name>` mesh attributes, texture layers in float OpenEXR images in
  `//geotools_layers/`. A Layers panel after Source adds, renames and removes
  layers (optionally with their attributes and images), creates their
  attributes, works out their value range and shows what is missing.
- Vertex-space bake (M3): Bake from Geometry Nodes, a sub-panel of Layers,
  freezes a named attribute that a Geometry Nodes modifier outputs into a face
  or corner layer on the source meshes. If the modifier keeps the topology,
  the values are copied. Otherwise they are transferred by Nearest, Raycast
  (both ways along the face normal, within a maximum distance, falling back to
  Nearest) or Area Mean (falling back to Nearest), with Geometry Nodes
  evaluations and numpy. Boolean and category layers take the most common
  value instead of a mean. No-data stays no-data. The report says how many
  faces each path filled. The bake records its provenance, updates the value
  range, and can turn the modifier off afterwards.
- `build.json` schema 2: the layers, the SHA-256 of `source.glb` and of the
  saved `.blend` (null if unsaved or modified), and the placement's EPSG code
  when the CRS is a preset or an EPSG code.
- Layer presentation and variant tilesets (M3): a Presentation sub-panel of
  Layers shows a layer through a colour ramp (viridis, magma, cividis, turbo,
  diverging red-blue, greys) over a set or automatic (2nd to 98th percentile)
  range, or one colour per category, with a no-data colour. Face and corner
  layers go into COLOR_0 through a `gt_present` byte colour attribute; texture
  layers into an 8-bit base-colour or emissive texture. Replace shows the ramp
  alone and Multiply keeps the material. Publish Variant (or a build profile's
  variant layer) publishes `<output>.<layer>.3tz`, an ordinary 3D Tiles archive
  with its own staging directory, the same placement and checks, and a
  `variant` record in `build.json` (palette, range and legend stops, or the
  category colours). The user's materials, images and active and render colour
  attributes are not changed, and the plain tileset never publishes
  `gt_present`.

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
