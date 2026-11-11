# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows semantic versioning where practical.

## [Unreleased]

GeoTools is being rebuilt to create, analyse and publish OGC 3D Tiles with
rusty-tiles. The next release will be 0.3.0. See `docs/ROADMAP.md`.

### Added

- Analysis (M5), in a new Analysis panel. **Sun:** the sun's position from the
  project's site, date, time and time zone (NREL SPA, within 0.00001° of the
  paper's example), turned from true north to the scene's grid north by the
  grid convergence. It is placed on a Blender Sun light, or keyframed over a
  day at a chosen step, with the light off below the horizon. Named IANA time
  zones are used where Python has a time zone database, else a fixed UTC
  offset. **Coordinates:** read a point from the 3D cursor or a click as E, N,
  RL and latitude and longitude at full binary64 precision, with the float32
  limit of the local position shown. Grid and ground distance between two
  points uses the combined scale factor (grid scale from pyproj, height factor
  from the ellipsoidal height), and the grid and ground area of the selected
  faces is also available. Every result can be copied to the clipboard. A CRS
  project needs the bundled pyproj and says so without it. The design is in
  `docs/design/m5-analysis.md`.
- Analysis (M5): **Sun Hours**, **insolation** and **shadow studies**. Hours
  of direct sun per face, face corner or texel over a day, a date range or a
  year, from NREL SPA positions at a chosen step (only steps with the sun up
  count), turned to grid north by the convergence. Optionally clear-sky
  irradiation in kWh/m² (Ineichen–Perez with a fixed Linke turbidity, matching
  pvlib; direct beam × cos(incidence) plus an optional isotropic diffuse sky
  through each sample's sky view). Results are FACE, CORNER or TEXTURE layers
  with units, value range, provenance and area-mean aggregation, so they
  display and publish as variants. The rays are Cycles Raycast shader nodes in
  EMIT bakes, up to 1,024 directions per bake, with nearby sun directions
  merged (default within 2°; a year at 30-minute steps becomes about 1,200
  rays). A uniform far-horizon elevation can be set. **Set Up Shadow Study**
  keys the Sun light at chosen times on the real equinoxes and solstices, one
  frame each with timeline markers, for Blender to render. See
  `docs/design/m5-analysis.md`; `scripts/bench_sun_hours.py` times it.
- Packaging as a Blender extension (`geotools/blender_manifest.toml`), with no
  `bl_info`.
- Bundled rusty-tiles wheels, pinned to 0.4.0 in `pyproject.toml`, for Linux x64,
  Windows x64 and macOS arm64 (Blender 5.x has no Intel macOS build). `scripts/fetch_wheels.py` fetches them
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
  layers go into COLOR_0; texture layers into an 8-bit base-colour or emissive
  texture. Replace shows the ramp
  alone and Multiply keeps the material. A build profile's Variant layer
  publishes `<output>.<layer>.3tz`, an ordinary 3D Tiles archive with its own staging directory, the same placement and checks, and a
  `variant` record in `build.json` (palette, range and legend stops, or the
  category colours). The user's materials, images and active and render colour
  attributes are not changed. Write Colours previews COLOR_0 in a
  `gt_present` colour attribute, which no tileset publishes.
- A GLB with metadata (M4): one standard glTF 2.0 GLB of the source
  collection, separate from rusty-tiles' `source.glb`, written by a build
  profile whose Format is `.glb` (through the same checks and staging, with a
  `build.json` recording the extensions and options) or by File > Export >
  GeoTools GLB with metadata. Objects are an `EXT_mesh_features` feature ID
  set with a property table of names and persistent object ids (a UUID kept on
  each object across renames and saves). Layers become an
  `EXT_structural_metadata` schema with names, units, no-data, ranges and enums
  for category layers: face and corner layers as property attributes, or face
  layers as a per-face feature table (Face Table); texture layers as property
  textures, float32 values packed losslessly into PNGs. The georeference (CRS,
  offsets, origin and heading from the placement probe) is recorded with
  `KHR_xmp_json_ld`; Earth-Placed also puts the local-to-ECEF transform on the
  root node. The 3d-tiles-validator and the glTF-Validator accept the files,
  and Blender's glTF importer opens them. See `docs/design/m4-glb-metadata.md`.
- Bundled pyproj 3.8.0 (PROJ 9.8.1, no GDAL) and its dependency certifi, pinned
  under `[tool.geotools.wheels]` in `pyproject.toml` beside rusty-tiles, for the
  same four platforms (5 to 10 MB per platform zip). `scripts/fetch_wheels.py`
  fetches every pin (`--from-dir NAME=DIR` takes one from a local directory)
  and `--python` fetches and builds for a local Blender that is not on CPython
  3.13. The About panel, the preferences and Check Dependencies show the pyproj
  and PROJ versions; without pyproj the extension still registers and imports
  report that reprojection is not available.
- `geotools.core.reproject.reproject_points()`, which reprojects vector points
  into the project CRS with pyproj for M8 imports: one vectorised call, offline
  unless grid downloads are allowed, Z passed through when there is no vertical
  transform, and a report of the operation used, its accuracy and any caveats
  (ballpark operation, points outside the area of use, a missing grid, a CRS
  preset as the target).
- Geo formats (M8): GeoJSON, GeoPackage and GeoTIFF import, GeoJSON and
  GeoPackage export, in File > Import and File > Export and in the new Linked
  Data panel after Source. Everything is placed in the project frame:
  coordinates are shifted by the project offset in binary64 and only then
  become float32.
  - GeoJSON and GeoPackage import: points become loose vertices, lines loose
    edges and polygons faces (n-gons, or triangles for polygons with holes),
    one object per layer and geometry kind, with Z kept. Feature properties
    become FLOAT, INT, BOOLEAN or STRING attributes on the face, edge or point
    domain, with an INT `fid`. Degenerate polygon parts are skipped, as in 0.2.
  - Vector data in another CRS is reprojected into the project CRS with the
    bundled pyproj, before the offset is applied. When the project CRS is a
    GeoTools preset, the target is the preset's EPSG code (EPSG:7856 for MGA
    zone 56), so GDA94 data moves the real ~1.5 m into GDA2020. The import
    report names the operation, its accuracy and any caveats; the report is
    kept with the link and Refresh reprojects the same way. "Allow Grid
    Downloads" (off by default) lets PROJ fetch a transformation grid.
    Without pyproj, data in another CRS is refused with both CRSs named and
    "reprojection is not available". Data of unknown CRS can be confirmed as
    being in the project CRS.
  - GeoTIFF import: a DEM becomes a grid mesh at pixel centres (PixelIsArea
    and PixelIsPoint), with nodata cells removed, an optional decimation step
    that shows the resulting spacing, and UVs over the raster extent so an
    orthophoto of the same extent maps onto it. An orthophoto becomes a plane
    over its footprint with its image linked to the file. GeoTools parses the
    georeference, GeoKeys and GDAL_NODATA; pixels go through Blender's image
    loader where it is exact (float32, int16, uint16, uint8; any compression
    it reads, tiled or BigTIFF) and through a numpy reader for other sample
    types in uncompressed and deflate files. Rotated rasters and rasters in
    another CRS are refused, with a pointer to QGIS: rasters are never
    reprojected. A 4096² DEM imports in about 3.5 s.
  - LOCAL and ANCHOR projects take imported coordinates as local metres, with
    a warning, when they are within about 100 km of the origin or of unknown
    CRS. Projected data with larger coordinates is refused, with a one-click
    "Use This Data's Centre as the Project Origin" (`geotools.use_data_origin`)
    in a popup and in the Linked Data panel: the project switches to the
    data's CRS (the preset for its EPSG code when there is one), with the
    centre of its extent in whole metres as the E/N offset and A 0, and the
    import goes ahead. If the source collection already has objects, a dialog
    asks first, since the existing scene's placement on Earth changes.
  - Linked sources: each imported object keeps its file, layer, CRS, import
    options and a fingerprint (`obj.geotools_link`). Refresh re-reads the file
    and replaces only the mesh data (an orthophoto reloads its image and moves
    its plane), so modifiers, materials, transform and parent stay; vectors
    report features added, removed and changed by fid. Changed files are
    detected when a `.blend` is opened and on demand, and an optional
    auto-refresh polls the file every 2 seconds. Vectors and GeoTIFF share one
    refresher registry (`links.register_refresher`).
  - Import filters: an SQL `WHERE` clause on a GeoPackage table, run on a
    read-only connection that may only read that table, and "within the site
    extent" (the source collection's bounds).
  - Attribute write-back: chosen attributes go back to the GeoPackage by fid
    in one transaction (`UPDATE`, adding columns when needed), or into the
    GeoJSON file (rewritten, keeping unknown members and feature order).
    Refused if the file changed since the last sync. Geometry is never written
    back.
  - GeoJSON and GeoPackage export (File > Export, and Export in Linked Data)
    from the evaluated mesh, with Z and the face, edge and point attributes,
    in the project CRS. Faces grouped by `fid` become polygons with their
    holes. Projected GeoJSON carries the legacy `crs` member.
  - `scripts/vector_timing.py` times import and export of 100k polygons inside
    Blender, and the `scripts/ui_screenshots.py` demo shows a linked GeoJSON
    and a linked DEM.
  - Vector publish: a build profile Format **Vectors** publishes the mesh
    objects of a collection (imported or linked GeoJSON and GeoPackage, draped
    by modifiers or not) as a vector 3D Tiles archive with rusty-tiles'
    `vector_to_3tz`. The evaluated geometry (or the original mesh data with
    Apply Modifiers off) is written to a GeoPackage in the project CRS, with the
    offset restored in binary64. The project's `source_crs` is passed
    explicitly, so the CRS presets work with the bundled rusty-tiles 0.4.0.
    A republish passes the previous archive as `reuse_tileset`, so only changed
    features are re-encoded; an archive rusty-tiles cannot reuse is rebuilt in
    full. The archive is validated and recorded in `build.json`
    (`output_type` `"vector"`).
  - A recipe, [docs/recipes/drape-contexts.md](docs/recipes/drape-contexts.md):
    drape excavation contexts on a site mesh, lower them by a depth
    attribute, extrude volumes between their top and bottom levels, export
    them to a GeoPackage with Z, and re-drape after an edit in QGIS, with stock
    Geometry Nodes only. `examples/drape-contexts/` is a synthetic example
    (terrain, contexts GeoJSON and a ready `.blend`), generated by
    `scripts/make_example_drape.py`.
  - Linked file paths and build profile output paths accept `//` (relative to
    the `.blend`) without a warning.

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
    formatted for people ("0.461 – 39.9 deg"). The texture bake is the Bake
    sub-panel, shown for texture layers, followed by Display (was
    Presentation) and a closed Details sub-panel. The ramp
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
  for UI work. It builds the extension for the CPython of the Blender it runs,
  like `scripts/run_blender_tests.py`, so wheels fetched with `--python` work.
- Face and corner layers read their values from each source object's
  evaluated mesh, with its modifiers and Geometry Nodes applied, as the export
  does: the value range, the layer status, the presentation and the variant
  publish. A Geometry Nodes Store Named Attribute that writes the layer's
  attribute computes it with no bake, even when the tree changes the
  topology. A layer's new Attribute field names that attribute (empty for
  `gt_<name>`), so an output such as `slope` backs a layer directly. Values
  written by tools and Create Empty Attributes still go to the original mesh.
  A missing attribute says to add a Store Named Attribute or create it. The
  panels show a status cached by the layer operators, Run Checks and Publish,
  and never evaluate meshes while drawing.
- A variant's COLOR_0 is worked out at publish time from the layer's values
  on the exported meshes, instead of being read back from a `gt_present`
  attribute, so no colour attribute has to survive the modifiers. Write
  Colours still paints `gt_present` for a viewport preview where the
  modifiers keep the topology; the export never reads it.
- Build profiles have an Apply Modifiers option (Build Settings, on by
  default), as in Blender's glTF exporter. Off, the export and the layer reads
  use the original mesh data; `build.json` records the choice.

### Removed

- The 0.2 GeoJSON, GeoPackage, DEM and 3D Tiles importers, the analysis tools,
  and the `api/` add-on registry. They remain on the `legacy-0.2` branch and the
  `v0.2.0` tag.
- 0.2's pyproj and GDAL support (installed separately), its built-in
  reprojection and the MapTiler fallback. Nothing needs to be installed beside
  the extension: pyproj is now bundled (see Added) and used only to reproject
  imported vector points, and tile placement is done by rusty-tiles at publish
  time.
- The BlenderGIS-derived code in `core/proj/`.
- The vertex-space bake (`geotools.bake_geonodes_layer`) of M3 and its
  attribute transfers (`core/transfer.py`): it repeated Blender's Apply
  Modifier and Geometry Nodes sampling, and layers now read the evaluated mesh
  directly. The texture bake is unchanged.

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
