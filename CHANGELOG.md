# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows semantic versioning where practical.

## [Unreleased]

### Added

- Run Checks tests each mesh's normals: faces wound against their neighbours
  (`normals-inconsistent`), normals pointing into the surface
  (`normals-inward`) and a mostly downward surface (`normals-down`), with the
  Blender fix in the message. Hydrology, Sun Hours, Sky-View Factor and
  Openness warn about the first and last in their report. See
  [Normals](docs/guide/publish.md#normals).
- **Bake Device** in the GeoTools preferences: Sun Hours, the horizon
  profile, Fast Bake presets and texture bakes use the GPU when Cycles has
  one enabled (Auto), or always the GPU or CPU. Each report names the device.
  See [Bake device](docs/guide/analysis.md#bake-device).
- **Sky Visibility**, **Sky View (Cosine)** and **Openness (3D)** in Add
  Analysis (Fast Bake only) count the sky directly from each face, so they
  are right under overhangs, where the sky-view factor and openness presets
  (which assume a height field) read open sky. See
  [which to use](docs/analysis-presets.md#sky-visibility-sky-view-cosine-and-openness-3d-sky_visibility-sky_view_cosine-openness_3d-face-ratio).
- **Least-Cost Routes** in Add Analysis: the quickest route over a 3D
  surface, uphill and downhill costed apart and crossing faces at any angle,
  with travel time to every face as a layer. Tobler, Langmuir, Naismith and
  wheelchair (1:12) models, checked on the
  [validation page](docs/validation/least-cost.md).
- **Show in Viewport** in Analyses > Display shows a layer in its variant
  colours, live as the palette, range or Geometry Nodes change, with a
  viewport-only modifier and material that turning it off removes. Texture
  layers show in Material Preview. Publishing ignores it.
- **Coordinates: WGS84 (RFC 7946)** in GeoJSON export writes longitude and
  latitude that CesiumJS, MapLibre and geojson.io place correctly, beside
  your tileset. It needs pyproj and a project CRS; the project CRS stays
  the default. See [Geo formats](docs/guide/geo-formats.md#export).
- A variant tileset gets an SVG legend beside it, such as
  `site.slope.legend.svg`: the layer's label and units, the palette over the
  published range, or the category colours, and the no-data colour.
  **Legend** in Last Build opens it. See [Layers](docs/guide/layers.md#legend).

### Changed

- **Analyses** replaces the Analysis and Layers panels. Like the modifier
  stack, it lists the active mesh's analyses and layers, added from one
  searchable **Add Analysis** field (presets, Fast Bakes, tools, layers),
  each with its own inputs and **Recompute**. Measure, Sun and
  Horizon and Sky Events move to **Tools**. Older files are listed when
  opened. See [Analysis](docs/guide/analysis.md).

- The Archaeology group is now **Relief and Sky**, and Archaeoastronomy is
  **Horizon and Sky Events**.

- Analysis acts on the selected meshes; the source collection only decides
  what is published, and is set from the first mesh you analyse if unset.
  Layers and **Show in Viewport** cover every object with the layer. Run
  Checks flags a layer outside the source collection, with **Add to
  Source**.
- **Write Colours** is gone: Show in Viewport replaces it. Publishing still
  never writes the `gt_present` attribute it left on meshes.
- GLBs with metadata name their `geotools:` XMP terms with a permanent
  namespace IRI, the docs site's
  [GLB format reference](docs/reference/glb-format.md)
  (`https://bendyson-arch.github.io/blender_geotools/reference/glb-format/#`),
  instead of a link to a design note on GitHub. Files from 0.3.0 keep the old
  IRI; `gltf_metadata.geotools_terms` reads both.
- Messages for a missing source collection say "Set or import a source
  collection first" (not "ingest").
- The docs site has user pages only: the developer section (roadmap,
  backlog, design notes) is gone. Open work is in the GitHub issues, and
  the texture cleanup page is now one page in the guide.
- The publish record's `cleanup_diagnostics` and `rusty_tiles_version` come
  from rusty-tiles 0.4.1's API directly, with no fallback for older wheels.
- Named time zones such as `Australia/Brisbane` work in the Sun panel on
  Windows too: every zip bundles the `tzdata` time zone database, used where
  the system has none.

### Fixed

- On macOS, Publish > Build Settings no longer runs `vm_stat` each time the
  sidebar redraws. The Auto memory figure updates every few seconds instead.
- A project origin on the antimeridian keeps its BlenderGIS latitude and
  longitude. A rounding error put its longitude just past -180°, so the
  position was refused and the two keys were deleted.
- After GeoTools is updated without restarting Blender, About, Check
  Dependencies and Run Checks say "Restart Blender to finish the update"
  (`restart-after-update`). PROJ errors from reading its database now show
  as one plain sentence instead of PROJ's own text, which **Copy Details** in
  About copies.
- **Refresh** no longer reloads an orthophoto over unsaved edits (Texture
  Cleanup or painting), or replaces a mesh outside Object Mode or during a
  render or bake: it says why and changes nothing. **Auto** waits instead,
  and refreshes once you leave that state.
- **Write Back** and **Export Vectors** say in plain words when a GeoPackage
  is locked by another program, read-only or not a database, and **Preview
  in Browser** when the archive is not a zip, instead of showing a Python
  traceback.
- A GeoTIFF DEM too big for the free memory is refused before it is read,
  naming the smallest **Decimation** that fits, instead of getting Blender
  killed; the import dialog shows the memory it needs. A grid of more than
  about 536 million samples is refused too, where it used to overflow
  Blender's loop count.
## [0.3.1] - 2026-11-15

Two fixes for Blender 5.3. From 0.3.1, CI also runs the tests on Blender
5.2.0 and 5.3.0.

### Fixed

- In Blender releases after 5.2, the texture tools (Fill Holes, the paint
  setup's overlap mask) and the UV overlap check left out the texels on edges
  shared by two triangles, such as the diagonal of every quad.
- Distance to Surface gave 0 instead of the distance for a point in the plane
  of the face it is signed by, at a sharp edge. An analysis added before keeps
  the old node group until you remove its modifier and add it again.

## [0.3.0] - 2026-11-14

The rebuilt GeoTools: GIS for Blender 5.2, shipped as a Blender extension with
rusty-tiles 0.4.1, pyproj 3.8.0 and certifi bundled for Linux x64, Windows x64
and macOS arm64. It replaces the 0.2 importer add-on, which stays on the
`legacy-0.2` branch; remove 0.2 before installing. The
[0.3.0 release notes](.github/release-notes/0.3.0.md) tell the full story, and
the [user guide](docs/guide/README.md) has a page per area.

### Added

- **Projects and georeference.** Local, Anchor (a WGS84 point and heading)
  and CRS projects, with a searchable EPSG picker and the datum operation's
  accuracy shown before a publish. Offsets keep full double precision, offset
  files are offered rather than silently applied, and **Verify Placement**
  shows where the site lands.
- **BlenderGIS interop.** A CRS project reads and writes the scene's GeoScene
  keys, so data from either tool lands on the same coordinates; BlenderGIS is
  not needed. **Origin from Latitude/Longitude** fixes scenes that have only
  a geographic origin.
- **Geo formats.** GeoJSON and GeoPackage import and export, GeoTIFF import
  of DEMs and orthophotos, all in the project frame. Imported data stays
  linked to its file: Refresh and Auto-Refresh replace the mesh in place,
  filters choose what to import, and changed attribute columns can be written
  back. Vector data in another CRS is reprojected with the bundled pyproj.
- **Layers.** Named values per face, corner or texel, read from Geometry
  Nodes outputs or stored attributes, with units, no-data and value ranges;
  texture-space bakes with a UV-overlap check; colour ramps and categories,
  and variant tilesets such as `site.slope.3tz`.
- **Analysis.** The sun, sun hours, clear-sky insolation and shadow studies;
  archaeoastronomy (horizon profile, solar and lunar events within 10,000
  years, sightlines, alignment checks); points, distances and areas on Earth;
  3D surface hydrology; contours and sections.
- **Analysis presets.** Editable Geometry Nodes presets from **Add
  Analysis**: slope, aspect, curvature, rugosity, TPI, TRI, local relief,
  sky-view factor, openness, viewsheds, intervisibility, line of sight,
  distance to a surface or feature, cut and fill, closed volumes, least-cost
  and flow paths. Sky-view factor, openness and the cumulative viewshed also
  have a fast (bake) path for large meshes.
- **Validation.** Every analysis tool is checked against closed forms and
  reference tools (GDAL, GRASS, WhiteboxTools, RVT, richdem, pvlib, Skyfield,
  GeographicLib); the [validation pages](docs/validation/README.md) give the
  definitions, tolerances, results and known differences.
- **Texture cleanup.** A one-click paint setup with UV overlaps protected,
  Fill Holes, Remove Masked, Pad Islands and colour balance across atlas
  pages, with one level of undo. Publish saves edited images to a copy and
  never overwrites a source file.
- **Checks and publishing.** Run Checks against rusty-tiles' input rules,
  with fix buttons for images it refuses; build profiles; publishing to a
  validated `.3tz` in the background, with a build record and a face map back
  to the source; Extract for Web with a ready CesiumJS page, and Preview in
  Browser when the rusty-tiles CLI is installed.
- **GLB with metadata.** One standard glTF 2.0 file with objects, layers and
  the georeference as `EXT_mesh_features`, `EXT_structural_metadata` and XMP
  metadata, optionally placed on Earth.
- **Inspect Output.** Open published tiles in Blender, trace output faces to
  their source faces and back, and view distance to the source, texel density
  and levels of detail.
- A synthetic demo site (`examples/drape-contexts/`, also attached to the
  release) and a recipe for draping excavation contexts with stock Geometry
  Nodes.

### Changed

- The sidebar is one GeoTools tab whose panels follow the workflow, top to
  bottom, with each header showing whether its step is done.
- Paths GeoTools stores are portable: a project folder opens on Linux, macOS
  and Windows.

### Removed

- The 0.2 importers' pip-installed pyproj and GDAL: nothing is installed with
  pip any more.
- Importing 3D Tiles for editing. Published tiles open read-only through
  Inspect Output.

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

[Unreleased]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/BenDyson-Arch/blender_geotools/releases/tag/v0.1.0
