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
- Vertex-space bake (M3): the Bake sub-panel of Layers, for a face or corner
  layer, freezes a named attribute that a Geometry Nodes modifier outputs into
  the layer on the source meshes. If the modifier keeps the topology,
  the values are copied. Otherwise they are transferred by Nearest, Raycast
  (both ways along the face normal, within a maximum distance, falling back to
  Nearest) or Area Mean (falling back to Nearest), with Geometry Nodes
  evaluations and numpy. Boolean and category layers take the most common
  value instead of a mean. No-data stays no-data. The report says how many
  faces each path filled. The bake records its provenance, updates the value
  range, and can turn the modifier off afterwards.
- Texture-space bake (M3): Bake Texture Layer, in the Bake sub-panel of
  Layers for a texture layer, bakes a face or corner layer, or a named
  attribute of the evaluated mesh, into a texture layer's float images on the source UVs with a
  one-sample Cycles emission bake and an extended gutter of `margin` pixels.
  An attribute that only a topology-changing Geometry Nodes result has is
  baked selected to active from a copy of that result. NaN, inf and texels no
  triangle covers hold the layer's no-data. The images are saved as OpenEXR
  next to the `.blend`, and the bake records its provenance and value range.
  Materials, modifiers, render and bake settings and the selection are
  restored afterwards, also when the bake fails.
- The `uv-overlap` check: UV triangles that share texels, by rusty-tiles'
  texel-ownership rule rasterised at the layer's resolution (coarsened for very
  large meshes, and reported). It is a warning in Checks for objects with
  texture layers, and a texture bake refuses overlapping UVs unless Bake
  Anyway is ticked.
- `build.json` schema 2: the layers, the SHA-256 of `source.glb` and of the
  saved `.blend` (null if unsaved or modified), and the placement's EPSG code
  when the CRS is a preset or an EPSG code.
- Layer presentation and variant tilesets (M3): the Display sub-panel of
  Layers shows a layer through a colour ramp (viridis, magma, cividis, turbo,
  diverging red-blue, greys) over a set or automatic (2nd to 98th percentile)
  range, or one colour per category, with a no-data colour. Face and corner
  layers go into COLOR_0 through a `gt_present` byte colour attribute; texture
  layers into an 8-bit base-colour or emissive texture. Replace shows the ramp
  alone and Multiply keeps the material. A build profile's Variant layer
  publishes `<output>.<layer>.3tz`, an ordinary 3D Tiles archive with its own staging directory, the same placement and checks, and a
  `variant` record in `build.json` (palette, range and legend stops, or the
  category colours). The user's materials, images and active and render colour
  attributes are not changed, and the plain tileset never publishes
  `gt_present`.
- GeoJSON and GeoPackage import (M8), in File > Import and the new Linked Data
  panel after Source. Points become loose vertices, lines loose edges and
  polygons faces (n-gons, or triangles for polygons with holes), one object
  per layer and geometry kind, in the project frame: coordinates are shifted
  by the project offset in binary64 and Z is kept. Feature properties become
  FLOAT, INT, BOOLEAN or STRING attributes on the face, edge or point domain,
  with an INT `fid`. Degenerate polygon parts are skipped, as in 0.2. Data in
  another CRS than the project's is refused with both CRSs named; data of
  unknown CRS can be confirmed as being in the project CRS.
- Linked sources: each imported object keeps its file, layer, CRS, import
  options and a fingerprint (`obj.geotools_link`). Refresh re-reads the file and
  replaces only the mesh data, so modifiers, materials, transform and parent
  stay, and reports features added, removed and changed by fid. Changed files
  are detected when a `.blend` is opened and on demand, and an optional
  auto-refresh polls the file every 2 seconds.
- Import filters: an SQL `WHERE` clause on a GeoPackage table, run on a
  read-only connection that may only read that table, and "within the site
  extent" (the source collection's bounds).
- Attribute write-back: chosen attributes go back to the GeoPackage by fid in
  one transaction (`UPDATE`, adding columns when needed), or into the GeoJSON
  file (rewritten, keeping unknown members and feature order). Refused if the
  file changed since the last sync. Geometry is never written back.
- GeoJSON and GeoPackage export (File > Export, and Export in Linked Data) from
  the evaluated mesh, with Z and the face, edge and point attributes, in the
  project CRS. Faces grouped by `fid` become polygons with their holes.
  Projected GeoJSON carries the legacy `crs` member.
- `scripts/vector_timing.py`, which times import and export of 100k polygons
  inside Blender.

### Changed

- Blender 5.2.0 is now the minimum version (was 5.1). Only the latest Blender
  release is supported.
- The sidebar is redesigned around the workflow: Project, Source, Layers,
  Checks, Publish, About. Each panel header shows whether the step is done,
  still to set up, or has a warning or error. It fits the default sidebar width
  without cut-off labels, and advanced settings are in closed sub-panels.
  - Project shows a CRS preset by name ("GDA2020 / MGA zone 56"); the
    definition and datum note are in CRS Details and the tooltip. Verify
    Placement and its result are in the Project panel.
  - Layers shows the selected layer at a glance, with its value range
    formatted for people ("0.461 – 39.9 deg"). The vertex and texture bakes
    are one Bake sub-panel that adapts to the layer's domain, followed by
    Display (was Presentation) and a closed Details sub-panel. The ramp
    minimum and maximum show the automatic range they will use.
  - Build Profiles is merged into Publish, which is the one place to choose a
    variant: the build profile's Variant field. The Publish button names the
    file it writes. Build Settings and Last Build (with Preview) are
    sub-panels.
  - Counts, sizes, ranges, times and memory are formatted with units,
    thousands separators and sensible precision; values are still stored at
    full double precision.
- Some labels are renamed: the placement mode "Source CRS" is "CRS", the build
  profile's "Profile" is "Quality" (its "Quality" option is "High"), "Jobs" is
  "Threads", "Variant Layer" is "Variant", and "Ingest Source" is drawn as
  "Import Mesh...". The RNA identifiers and operator ids are unchanged.
- `scripts/ui_screenshots.py` screenshots the sidebar in a real Blender window
  for UI work.

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
