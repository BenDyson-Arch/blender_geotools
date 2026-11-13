# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project follows semantic versioning where practical.

## [Unreleased]

### Added

- **Extract for Web** in Last Build: writes a published `.3tz` as a folder any
  web server can serve, next to it (`site.web/` for `site.3tz`, and one per
  variant), with `tileset.json`, the tiles and an optional `index.html`
  CesiumJS page. The page loads CesiumJS 1.146.0 from the jsDelivr CDN and uses
  the Natural Earth II imagery bundled with CesiumJS, so it needs no Cesium ion
  token, and no rusty-tiles CLI is needed to view a build. GeoTools only ever
  replaces a folder it wrote itself, and the panel says when the folder is
  older than the archive.

### Fixed

- Getting started step 8 (View the result) works as written: it uses Extract
  for Web and a complete viewer page instead of a snippet with an undefined
  `viewer`, and gives the Windows `py` command for Python's web server. The
  publish guide shows the page and explains why a viewer on another origin,
  such as Sandcastle, is blocked by CORS.

- File paths GeoTools stores itself now work across Linux, macOS and Windows,
  so a project folder (the `.blend` and its data) opens on any of them and
  finds its files. Linked GeoJSON, GeoPackage and GeoTIFF files, the build
  profile's output path, the last output and build record, the source file and
  the offset file are stored as `//sub/file` (forward slashes) when they are
  under the `.blend`'s folder, and stay absolute otherwise. `//` paths with
  either separator resolve on every OS: a `//out\site.3tz` typed on Windows no
  longer becomes a file named with a backslash on Linux or macOS. Saving to
  another folder re-expresses them for it, as Blender does for its own paths;
  a copy leaves the open file as it was. Older files with absolute or
  backslash paths keep working and are rewritten only when GeoTools next sets
  that path, never on load. Checks Linked Files reports Up to Date, Changed on
  Disk and Missing for both forms, and GeoTools compares files by their
  resolved paths (macOS `/var` and `/private/var`, Windows 8.3 short names).
  The rule is in `geotools/core/paths.py` and CONTRIBUTING.md ("File paths").

## [0.3.0] - Unreleased

GeoTools is being rebuilt to create, analyse and publish OGC 3D Tiles with
rusty-tiles. The next release will be 0.3.0. See `docs/ROADMAP.md`.

### Added

- A user guide in `docs/guide/` (index: `docs/guide/README.md`). Getting
  started walks through installing the extension, reading the dependency
  status, placing the synthetic `examples/drape-contexts` site in EPSG:32631
  with its offset file, running Checks, publishing a `.3tz` and viewing it in
  CesiumJS. One page per area: georeference and projects, layers and bakes,
  checks and publishing, GLB with metadata, analysis (sun, sun hours, shadow
  studies, archaeoastronomy, measurement, presets, hydrology, contours and
  sections), texture cleanup, inspecting output, geo formats, and working
  alongside BlenderGIS (the GeoScene keys GeoTools reads and writes, and what
  stays GeoTools-only). The README is now a short overview that points into
  the guide, and CONTRIBUTING links it.
- Texture cleanup (M6), in a new **Textures** panel between Layers and Checks
  ([docs/texture-cleanup.md](docs/texture-cleanup.md)). Painting stays
  Blender's: the docs have recipes for clone and patch across UV islands
  (projection paint's Clone brush), smear, soften, masks and Quick Edit.
  GeoTools adds:
  - **Paint Texture**: one click into Texture Paint on the active object's
    base colour image, with projection occlusion, backface culling and normal
    falloff on, and Solid, Flat, Texture shading so the view has no lighting.
    UV overlaps (any face that uses the image, with the `uv-overlap` rule) are
    warned about and written as a mask image, which becomes the paint stencil
    by default, so a stroke cannot change two places at once.
  - **Fill Holes** (black or transparent texels) and **Remove Masked**
    (people, vehicles or scale bars painted in a key colour or on a mask
    image), both by push-pull inpainting that reads and writes only inside the
    UV islands of the faces that use the image. It is a diffusion fill: it
    blurs, and it is not content-aware.
  - **Pad Islands**: island-aware edge padding (each background ring takes
    the nearest island's colour), optionally filling the whole background, so
    coarse levels and mipmaps do not bleed it in. It never writes inside an
    island.
  - **Fit Balance**, **Apply Balance**: a per-page, per-channel gain and
    offset in linear light, fitted (robustly) to colour samples on both sides
    of the 3D seams between atlas pages, previewed with a shader node before
    it is applied to the pixels.
  - **Undo Cleanup** puts back the images the last tool changed (one level).
  - Publish saves every edited source image first (repacked, saved to its
    file, or saved as `//textures/<image>.png`), so a painted texture is never
    lost or published stale. `build.json` records each source image's SHA-256
    in `source.images`. The Publish panel shows "N textures changed since last
    publish", and Run Checks reports `image-unsaved`, `textures-changed` and
    `texture-balance-preview` (an error: the preview node would publish a flat
    colour). Publishing still means a full retile until rusty-tiles'
    appearance-only rebuild lands. A Blender test repaints part of a texture,
    republishes, and checks that leaf and coarse tiles both get new texture
    bytes.
  - `core.uv_overlap.texel_coverage`: how many UV triangles own each texel,
    with the `uv-overlap` raster rule; `core.texture_cleanup` and
    `core.texture_state` hold the bpy-free maths and records.
    `scripts/bench_texture_cleanup.py` times the tools on 8K pages.
- Output inspection (M7), in a new Inspect Output panel after Publish. List
  the tiles of the last published `.3tz` (or a chosen one) by depth, with role,
  geometric error and triangle count, and **Open Tiles**: a tile, one depth or every
  leaf (within a limit) as read-only objects in a "GeoTools Output"
  collection: built from rusty-tiles' decoded arrays with per-face
  `source_primitive` and `source_triangle` attributes, the tile's texture
  pages as packed images and its materials. Tiles are placed by the
  placement `build.json` recorded (CRS projects through a vectorised pyproj
  inverse, anchor projects by the inverse rigid transform), with a note when
  the scene's placement has changed since; leaves land on the source within
  rusty-tiles' reported `maximum_leaf_geometry_error` (0 to 0.005 mm for
  `quality` and `lossless`, the 16-bit position step for `fast` and
  `balanced`). **Trace to Source** selects the source faces of the selected
  output faces, and the reverse, through the face map; a coarse tile carries
  only a representative source primitive, so it selects that whole
  primitive and says so, and a face map that no longer fits the edited scene
  is refused. **Quality**: each face's distance from its centre to the
  source (Geometry Proximity), the texels per m² of each face, and levels
  compared one depth at a time or side by side. The design is in
  `docs/design/m7-output-inspection.md`; `scripts/bench_inspect.py` times it.
- CI on macOS arm64 and Windows x64: the core tests, and the Blender tests
  (no wheels) on the official, checksum-pinned Blender 5.2 LTS `.dmg` and
  `.zip`. They are not required checks yet.

- Interoperability with BlenderGIS and other Blender GIS tools. A CRS
  project's CRS and origin are now kept in the scene's GeoScene keys (`SRID`,
  `crs x` and `crs y`, with `latitude` and `longitude` kept in sync through the
  bundled pyproj, using rusty-tiles' datum convention). These keys are the
  BlenderGIS convention. A scene georeferenced by BlenderGIS is read as it is,
  and a scene set up in GeoTools is georeferenced for those tools. GeoTools
  does not depend on BlenderGIS. `SRID` is written only as a plain EPSG code
  or a PROJ string, because BlenderGIS cannot read WKT or compound codes; such
  a CRS stays a GeoTools setting and the scene gets no keys. Local and anchor
  projects write no keys. If keys exist while the project is local or anchor,
  the Project panel and Check warn and offer **Use the Scene's BlenderGIS
  Georeference**. A `.blend` from an earlier GeoTools is migrated on load,
  keeping its values and its A. Where both exist and differ, the keys win, and
  a one-time notice names what was taken from where. "Use data origin" adopts
  a scene's existing GeoScene georeference rather than moving it. See
  `docs/design/georeference-interop.md`.

- Analysis (M5), in a new Analysis panel. **Sun:** the sun's position from the
  project's site, date, time and time zone (NREL SPA, within 0.00001° of the
  paper's example), turned from true north to the scene's grid north by the
  grid convergence. It is placed on a Blender Sun light, or keyframed over a
  day at a chosen step, with the light off below the horizon. Named IANA time
  zones are used where Python has a time zone database, else a fixed UTC
  offset. **Measure:** read a point from the 3D cursor or a click as E, N,
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
- Analysis (M5): **Archaeoastronomy**, a new sub-panel. **Horizon:** from an
  observer picked with the 3D cursor or a click, the apparent altitude of the
  skyline every 0.5° (true azimuths, grid convergence applied), with the
  scene's meshes as terrain: rays in a fan of elevations cast by Cycles
  Raycast nodes in two EMIT bakes (one ray per texel of a temporary sample
  object; about 3 s on a 10M-triangle terrain), found to about 0.002°, with
  Earth curvature and terrestrial refraction (k 0.13), each switchable. Open
  azimuths, and skylines at the edge of the terrain, are flagged. Sun Hours
  can use the profile as its far horizon (**Use Horizon Profile**).
  **Events:** sunrise and sunset at the solstices and equinoxes and the
  Moon's rise and set at the major and minor standstills, for any epoch
  within 10,000 years of 2000 (Laskar's obliquity: the solstice declination
  is 24.02° in 3000 BCE), on the astronomical horizon and the real one, with
  Bennett's refraction, a chosen limb and the Moon's topocentric parallax; and
  dated sunrise and sunset times on the Sun panel year's equinoxes and
  solstices and a chosen date (NREL SPA, within 0.02° of NOAA's formulation).
  The Moon is Meeus' chapter 47 (his worked example to the unit); standstill
  dates come from the mean node, and the extremes the Moon reaches from the
  ephemeris. The panel says how far back the results can be trusted. The
  table copies to the clipboard. **Sightlines:** a mesh of lines from the eye
  to the horizon (or a chosen length) per event, with event, azimuth,
  altitude, declination and epoch attributes, which Export Vectors writes as
  GeoPackage or GeoJSON and the vector publish publishes. **Alignment
  check:** each selected edge's true azimuth both ways, the nearest events
  and the declination it implies. See `docs/design/m5-analysis.md`;
  `scripts/bench_archaeo.py` times it.
- Analysis presets (M5), from **Add Analysis** in the Analysis panel's new
  Presets sub-panel: ordinary Geometry Nodes groups of stock nodes, shipped in
  `geotools/assets/analysis_presets.blend` (generated by
  `scripts/make_analysis_presets.py`, which the Blender tests check it against)
  and appended on use. One click adds the group as a modifier on the selected
  meshes and creates its FACE or CORNER layer with units, aggregation, a
  display palette and range, and a provenance naming the preset and version.
  Re-adding never duplicates node groups. Terrain shape: slope (degrees and
  percent), aspect (grid north, or true north with the site's convergence),
  profile, plan and mean curvature, rugosity, TPI and TRI. Archaeological
  visualisation: local relief model (trend removal), sky-view factor and
  positive and negative openness. Formulas, inputs, accuracy against analytic
  surfaces and timings are in `docs/analysis-presets.md`.
- Analysis presets (M5), second batch. Visibility: viewshed from an observer
  (an Empty, or one made at the 3D cursor) with observer and target heights
  and a maximum distance, cumulative viewshed from a set of points,
  intervisibility lines between every pair of points and a line of sight along
  a line with its ground profile and first obstruction. Change and distance:
  signed distance to a second survey and distance to a feature (such as
  imported vectors), in plan or 3D. Volumes: cut and fill against a reference
  surface (no data outside its footprint, with fill, cut, net and covered area
  totals) and the volume of each closed part of a mesh. Paths and flow:
  least-cost walking paths (Shortest Edge Paths with Tobler's hiking function,
  a maximum slope and an optional per-face cost attribute) with their walking
  time, and quick-look flow paths stepping downhill from seed points. Object
  inputs (observer, second surface, line, points) are filled from the
  selection or the 3D cursor; presets that make lines create a new object in
  a "GeoTools Analysis" collection with their provenance and attributes ready
  for vector publishing. **Read Totals** reports volumes, walking time and
  obstructions summed in binary64. All use Geometry Nodes Raycast, measured
  faster than a bake for these ray counts; accuracy and timings at 1M and 10M
  triangles are in `docs/analysis-presets.md`.
- Analysis presets (M5): **fast (bake) paths** for sky-view factor, positive
  and negative openness and the cumulative viewshed. Add Analysis offers
  "(Fast Bake)" entries and an Engine choice (Live (Geometry Nodes) or Fast
  (bake)): the preset's own rays, inputs and formulas, cast by Cycles Raycast
  shader nodes in one EMIT bake (the sun-hours engine, now shared as
  `geotools/blender/ray_bake.py` by sun hours, the horizon profile and these
  paths), stored once as a layer with origin analysis and a provenance naming
  the preset version and the method. On 10M triangles: sky-view factor about
  40 s and openness about 50 s (live: 163 s and about 210 s), and a
  cumulative viewshed from 100 observers 49 to 65 s (live: 229 s). Identical
  to the live presets on the analytic tests and to batch 2's segment-box test;
  details in `docs/analysis-presets.md`.
- Analysis (M5): **3D surface hydrology**, in a Hydrology sub-panel. Flow
  routing, flow accumulation (upslope contributing area in m², plan area of
  upward faces by default) and ponding on the mesh's own connectivity: faces
  linked by shared edges, steepest descent by drop over 3D distance, so water
  runs down walls; faces pointing down drip, and faces on an open edge with no
  lower neighbour drop, straight down onto whatever Blender's Geometry Nodes
  Raycast finds below (or the ground above a sunken wall's foot), across
  objects. Depressions are filled to their spill level (priority-flood levels
  from a Borůvka spanning tree of the basins, vectorised) and flow is routed
  over the filled surface and across flats. Results are FACE layers (flow
  accumulation, shown on a log scale; ponded depth; depression as a category,
  largest volume first), an optional point object of the depressions at their
  pour points (level, volume, area, depth, catchment) and an optional line
  object of the main flow paths, both exportable with Export Vectors.
  Geometric only: no rainfall, infiltration or hydraulics. 20 s on 10 million
  triangles (`scripts/bench_hydrology.py`); a bowl's ponded volume is within
  0.01 % of the analytic one.
- Analysis (M5): **contours and sections**, in a Contours and Sections
  sub-panel. Contours at an interval from a base in the project's height
  datum (RL: local z plus the binary64 offset A), and sections along the edges
  of a line object (drawn, or an imported line layer), cut from the evaluated
  meshes by vectorised plane-triangle intersection and chained into open and
  closed polylines. Output is a line object in a `GeoTools Analysis`
  collection with `elevation`, `index`, `major` and `closed` per contour, and
  `chainage` per point and `chainage_from`/`chainage_to` per section piece
  (every piece of a multi-level surface is kept). Export Vectors writes them
  as GeoPackage or GeoJSON lines with absolute Z, and a Vectors build profile
  publishes them. 6 s for 1 m contours of 10 million triangles.
- Layer display: a **Scale** of Linear or Log for ramps (Log spaces the
  colours by decades, for values such as flow accumulation; its automatic
  range runs from the 2nd percentile of the positive values to the largest).
  The variant record in `build.json` gains `scale`.
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
- A searchable EPSG picker for the project CRS (Project panel), backed by
  rusty-tiles 0.4.1's GDAL-verified allow-list: every code it resolves (3,211)
  is listed as `EPSG:7856 — GDA2020 / MGA zone 56`, with names and areas of use
  from the bundled pyproj (codes only without it). Typing filters by code or
  name words; with a project location (a verified placement, the CRS and
  offset, or an anchor) the codes whose area of use holds the site come first,
  and "Near This Site" lists only those. The plain `EPSG:nnnn` is stored and
  passed to rusty-tiles; WKT and PROJ text still work. The panel and Run
  Checks show `resolve_source_crs()`'s verdict before any bake: the EPSG datum
  operation and its stated accuracy, or the refusal as an error (British
  National Grid, NAD27 and other grid-based or dynamic CRSs). CRS Details shows
  the area of use and the definition rusty-tiles uses. Scenes saved by
  development builds with a GDA2020 MGA or NZTM2000 preset PROJ string are
  moved to its EPSG code on load, only when rusty-tiles resolves that code to
  the same definition, so placement is unchanged (a test compares the ECEF of
  probe points within 1 mm).
- An import (GeoTIFF, GeoPackage, GeoJSON) into a Source CRS project with no
  CRS yet offers the data's CRS as the project CRS, with the same one-click
  button as for Local projects: the CRS only when the project has an offset,
  else also the data's centre.
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
  saved `.blend` (null if unsaved or modified), the placement's EPSG code, and
  `placement.crs_operation`: rusty-tiles' `crsOperation` (the EPSG datum
  operation and stated accuracy) for mesh, GLB and vector builds.
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
  (ballpark operation, points outside the area of use, a missing grid).
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
    bundled pyproj, before the offset is applied. The target is the project's
    EPSG code (EPSG:7856 for MGA zone 56), so GDA94 data moves the real ~1.5 m
    into GDA2020. The import
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
    data's CRS (its `EPSG:nnnn`), with the
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
    explicitly.
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

- The sidebar is reorganised for a first-time user
  ([docs/design/ui-structure.md](docs/design/ui-structure.md)). It follows
  the workflow: Project, Source, Linked Data, **Analysis** (moved up from near
  the bottom), Layers, Textures, Checks, Publish, Inspect Output, About.
  Analysis has six sub-panels instead of eight side by side: Measure,
  Presets, Hydrology, Contours and Sections, Sun (now with **Sun Hours** and
  **Shadow Study** inside it, as they use its date, time zone and site) and
  Archaeoastronomy. Sun starts closed.
- Renamed labels, with no property or `bl_idname` changes, so saved files and
  scripts are unaffected: the Coordinates sub-panel is **Measure**; Ingest
  Source is **Import Source Mesh**; Make Contours and Make Sections are
  **Create Contours** and **Create Sections**; the inspect **Open** button is
  **Open Tiles**; Frames per Step is **Frame Step**; the Create Attributes and
  Write Presentation operators take their buttons' names, **Create Empty
  Attributes** and **Write Colours**.
- No control is drawn twice: Archaeoastronomy no longer repeats Sun Hours'
  **Use Horizon Profile** (it says whether Sun Hours uses the profile), and
  Shadow Study no longer repeats Sun's **First Frame** (it says which frame
  the keys start at).
- Less cut-off text in the sidebar: the Checks list cuts a long issue at its
  end rather than its middle, so the subject stays readable (the box under the
  list shows it whole); the CRS name under the picker wraps; a layer's
  **Description** gets the full width; Sections' checkbox reads **Edges:
  Selected Only**; Balance's empty **Reference** reads "Automatic".
- In Publish, **Format** comes before **Output**, each labelled above its
  field, so `.3tz .glb Vectors` is no longer cut off. Linked Data's import
  buttons, Inspect Output's Quality buttons and the long hints in Sun Hours,
  Shadow Study, Archaeoastronomy and Presets no longer get cut off either.
- Every property and operator the sidebar draws has a tooltip (about 40
  properties and 18 operator options had none, for example the sun's date and time, the layer domain and type,
  and the Inspect Output options), and so does every option of every GeoTools
  operator. `tests/blender/test_ui.py` keeps it that way.

- New CRS projects start with offset A = 0, because GeoScene tools read
  Blender Z as absolute height. "Use data origin" from a local or anchor
  project sets A to 0 as before, and now leaves a CRS project's A alone.
  Existing scenes keep their A. When A is not 0, the Project panel and Check
  warn that other GIS tools will see heights shifted by A. For scripts, the
  project CRS and offset E/N are now `scene.geotools.crs`, `origin_e` and
  `origin_n`. These read and write the GeoScene keys in a CRS project.
  `source_crs`, `source_offset_e` and `source_offset_n` are GeoTools' own text,
  used only when the scene has no keys.

- Export Vectors writes FLOAT attributes (float32 in Blender) as the shortest
  decimal that reads back as the same value, so 123.4 is written as 123.4
  rather than 123.40000152587891.
- GeoTools needs rusty-tiles 0.4.1 or later (`supported_epsg_codes` and
  `resolve_source_crs`). A wheel without them is reported as not available, in
  About and Check Dependencies. The pin in `pyproject.toml` stays at 0.4.0
  until 0.4.1 is released (the develop wheel still reports 0.4.0).
- GeoTools also needs rusty-tiles' `list_tiles` and `decode_tile` (0.4.1,
  decoded tile views) for output inspection; a wheel without them is reported
  as needing rusty-tiles 0.4.1 or later.
- Blender 5.2.0 is now the minimum version (was 5.1). Only the latest Blender
  release is supported.
- The sidebar is redesigned around the workflow: Project, Source, Layers,
  Checks, Publish, About. Each panel header shows whether the step is done,
  still to set up, or has a warning or error. It fits the default sidebar width
  without cut-off labels, and advanced settings are in closed sub-panels.
  - Project shows the CRS's name and rusty-tiles' verdict under the EPSG
    field; the area of use and definition are in CRS Details. Verify
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

[Unreleased]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/BenDyson-Arch/blender_geotools/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/BenDyson-Arch/blender_geotools/releases/tag/v0.1.0
