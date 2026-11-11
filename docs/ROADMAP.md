# GeoTools rebuild roadmap

Status: agreed 2026-11-11. This replaces the 0.2 importer/analysis add-on.

## What GeoTools becomes

You prepare a site in Blender, bake analysis and cleaned textures onto it, and
publish it as OGC 3D Tiles with [rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles).
GeoTools creates tilesets; it no longer imports them.

- **Blender:** the latest release only (5.2 LTS at the time of writing). It ships
  as a Blender extension (`blender_manifest.toml`) with the rusty-tiles wheels
  bundled for each platform.
- **Source:** the full-detail mesh in Blender plus its **layers**.
  - An attribute layer is a named per-face or per-corner value with units, a
    no-data value and a rule for how coarse levels aggregate it.
  - A texture layer is an image on the source UVs.
- **Output:** a `.3tz` archive written by rusty-tiles.
- **Link between them:** a **face map**. When GeoTools writes the source GLB, it
  records which `(source_primitive, source_triangle)` each Blender face became.
  rusty-tiles keeps those IDs on every leaf triangle, so a source layer can be
  mapped exactly onto output leaves.
- **Updating output:** layers stay in Blender and change the output only when
  published. Until rusty-tiles 0.5, publishing means a retile; from 0.5 it
  means an annotate pass that adds metadata without rebuilding geometry.

## Decisions taken

| Decision | Choice | Revisit when |
|---|---|---|
| Blender support | Latest release only (5.2 LTS) | Each new Blender release |
| Where the source lives | The `.blend` plus image files on disk. Face maps and build records sit beside the outputs | Sources outgrow one Blender session (see M10) |
| Versioning | Continue the 0.x line. First rebuild release is 0.3.0; 1.0.0 at M9 | |
| CRS handling | rusty-tiles does all CRS maths at publish time (`source_crs`, `source_offset`, `height_offset`). Imports (M8) reproject vector points into the project CRS with a bundled pyproj, never GeoTools' own maths; rusty-tiles will not add general reprojection. Rasters are not reprojected (QGIS first). The source GLB is always standard glTF Y-up | |
| Precision | Offsets, anchors and other geodetic numbers are binary64 Python floats, never Blender `FloatProperty` (single precision) | |
| Performance rule | No Python loops over faces or vertices. Use `foreach_get`/`foreach_set` with numpy, Geometry Nodes or Cycles bakes | |
| Scope | Don't do what Blender already does. GeoTools reads and writes geo formats, handles georeference and publishes; modelling, draping, displacement and painting use Blender's own tools, with recipes in the docs | A task needs something Blender cannot do |

## rusty-tiles constraints this plan is built around (0.4.0)

1. Mesh input admits only POSITION, NORMAL, TANGENT, TEXCOORD_0/1 and COLOR_0
   (in [0, 1]) with core PBR materials, and refuses extras, extensions and
   metadata. Before 0.5, analysis can reach tiles only as colours or texels.
2. Meshes have no incremental update. Only vectors support `--reuse-tileset`.
3. Output geometry is always `EXT_meshopt_compression`, and lossy profiles use
   KTX2 textures. Blender's glTF importer reads neither.
4. Leaves keep exact `source_primitive`/`source_triangle` feature IDs. Coarse
   tiles keep only a representative `source_primitive`. Each leaf reports
   texture ownership as `unique` (editable in texture space) or `shared`
   (vertex space only).
5. CesiumJS cannot open `.3tz` directly; publish extracted directories for
   viewers.
6. The wheel resolves EPSG codes only for WGS84 geographic, WGS84 UTM and
   Mercator. Other projected CRSs (GDA2020/GDA94 MGA, NZTM) work as PROJ
   definitions with an explicit `+towgs84` shift; GeoTools ships these as
   presets with their datum assumption stated. A verified EPSG allow-list is
   planned for rusty-tiles 0.4.1 (#337); the presets switch to plain EPSG codes when
   it ships. Coordinate epochs and grid-based datums (e.g. OSTN15) are refused.

## Upstream dependencies

| rusty-tiles item | Release | Tracking | Gates |
|---|---|---|---|
| 0.4.0 release (wheel on PyPI and GitHub) | 0.4.0 | release in progress | M0 wheel pin, M2 |
| E. Blender 5.2 LTS in wheel acceptance | 0.4.x | [#330](https://github.com/BenDyson-Arch/rusty-tiles/issues/330) | M2 release |
| C. Decoded tile export (`decode_tile` to plain GLB or arrays) | 0.4.x | [#329](https://github.com/BenDyson-Arch/rusty-tiles/issues/329) | M7 |
| A. Annotate pass from `(source_primitive, source_triangle)` tables | 0.5 | addendum in the local metadata-tiling design (no GitHub issue yet) | M9 |
| D. Custom vertex attributes in mesh input | 0.5 | same design; needs a decision on the strict-admission policy | M9 (alternative to A) |
| B. Appearance-only rebuild | post-0.4 | [#331](https://github.com/BenDyson-Arch/rusty-tiles/issues/331) | M10 |
| CRS allow-list: GDAL-verified EPSG codes (MGA, NZTM, ETRS89 ...) in the wheel | 0.4.1 | [#337](https://github.com/BenDyson-Arch/rusty-tiles/issues/337) | Replaces the PROJ-string CRS presets |
| Encoded images admitted by memory budget, not a fixed 32 MiB | 0.4.1 | [#338](https://github.com/BenDyson-Arch/rusty-tiles/issues/338) | Publishing 8K PNG photogrammetry without re-encoding |

## Milestones

| Milestone | Delivers | Needs from rusty-tiles | Release |
|---|---|---|---|
| M0 Foundation | Clean scaffold, packaging, CI | 0.4.0 wheel | none |
| M1 Project and georeference | CRS, offsets, source ingest | 0.4.0 | none |
| M2 Publish | Blender to a validated `.3tz` | 0.4.0, plus E to release | **0.3.0** |
| M3 Layers and bake engine | Layers read from Geometry Nodes outputs or stored attributes, and texture-space bakes | none | 0.4.0 |
| M4 GLB export with metadata | A standard glTF 2.0 GLB carrying objects and layers as `EXT_mesh_features` / `EXT_structural_metadata` | none | 0.4.0 |
| M5 Analysis library | Fast analysis rebuilt on the bake engine | none | 0.4.0 |
| M6 Texture cleanup | Paint, clean, publish | none | 0.5.0 |
| M7 Output inspection | Open output tiles in Blender, quality checks | C | 0.6.0 |
| M8 Geo formats | GeoJSON, GeoPackage and GeoTIFF in and out of Blender, in the project frame | none | any time after M2 |
| M9 Queryable metadata without retiling | Annotate pass from layers | A or D (rusty-tiles 0.5) | 1.0.0 |
| M10 Large sites and texture-only rebuilds | Chunked working mode, texture-only republish | B | later |

### M0 Foundation

- Keep the 0.2 code on the `legacy-0.2` branch and the `v0.2.0` tag, then
  start fresh on `main`.
- Packaging:
  - a Blender extension for Blender 5.2+, with the add-on in `geotools/`;
  - rusty-tiles wheels bundled for Linux x64, Windows x64 and macOS
    arm64/x64, pinned to one version and fetched by a script (never
    committed); pyproj (with PROJ) and certifi are bundled the same way for
    M8 imports;
  - no `bl_info`, and no pyproj or GDAL install steps.
- Layout:
  - `geotools/core/`: pure Python and numpy, no `bpy`;
  - `geotools/blender/`: operators, panels and properties;
  - `geotools/analysis/`, `geotools/bake/` and `geotools/publish/` for later
    milestones.
- CI:
  - pytest for `core/`;
  - extension build and `blender --command extension validate`;
  - headless tests inside an official, checksum-pinned Blender 5.2 LTS.
- **Exit:** the extension installs and enables in Blender 5.2, reports the
  bundled rusty-tiles version, and CI is green.

### M1 Project and georeference

- One project per scene:
  - placement in rusty-tiles' terms: `source_crs`, `source_offset` E/N/A,
    and `height_offset`. The GLB writer always writes standard glTF Y-up, so
    rusty-tiles is called with `source_axes = y-up`;
  - or a local anchor and orientation for unreferenced scenes.
- Ingest a source mesh with Blender's GLB, OBJ or PLY importers, and read a
  hand-written E/N/A offset file (rusty-tiles' `--source-offset-file` format).
  Photogrammetry tools don't export one; the shift entered at export time is
  typed in or kept in such a file.
- A source collection, build profiles, and a check of the scene against
  rusty-tiles' input rules.
- **Exit:** a Metashape export is placed correctly, and its CRS and offsets
  round-trip to the values rusty-tiles expects.

### M2 Publish (first release, 0.3.0)

- **Deterministic numpy GLB writer:**
  - triangulates and splits into one primitive per material;
  - writes only the allowed attributes and core PBR materials, with PNG or
    JPEG images;
  - writes no extras;
  - produces byte-identical output for the same scene.
- **Face map sidecar:** Blender face to `(primitive, triangle)`, with a
  topology hash so a stale map is detected.
- **Build operator:**
  - runs `mesh_to_tileset` on a worker thread (rusty-tiles releases the GIL);
  - queues progress events for a main-thread timer;
  - cancels by raising from the callback;
  - offers profile, job and memory-budget settings;
  - runs `validate` afterwards.
- **Build record:** inputs hash, settings, a report summary (escalated pages,
  texture ownership, errors) and the output path.
- **Preview:** a one-click launch of `rusty-tiles preview` on an extracted copy
  when the CLI is installed.
- **Exit:** a 10M-triangle photogrammetry scene publishes from Blender to a
  validated `.3tz` that displays in Cesium. Release once E is green.

### M3 Layers and bake engine

- **Layer registry:** name, domain (face, corner or texture), type, units,
  no-data value, origin (Geometry Nodes, analysis or manual) and a
  coarse-level aggregation rule, stored now for the 0.5 annotate pass.
- **Layers from Geometry Nodes (no vertex-space bake):** a face or corner
  layer reads its attribute ("Store Named Attribute", `gt_<name>` or a name
  set on the layer) from the evaluated mesh, as the export does, so a node
  tree computes a layer with no bake step, even when it changes the
  topology. A vertex-space bake that froze outputs into source attributes and
  transferred values between topologies was built and then removed: Apply
  Modifier and Geometry Nodes sampling already do that (the Scope decision).
  The build profile's Apply Modifiers option, on by default, can export and
  read the original mesh data instead.
- **Texture-space bake:**
  - a Cycles bake of an Attribute node into an image on the source UVs, with
    gutters;
  - a UV-overlap check that uses the same rule as rusty-tiles' texture
    ownership, so overlapping UVs are flagged before baking.
- **Presentation step (until M9):** turn a layer into something visible,
  either a colour ramp into COLOR_0 or a texture in the base-colour or
  emissive slot. Publish it as a variant tileset such as `site.slope.3tz`.
- **Exit:**
  - a Geometry Nodes result reaches a tileset, read from the evaluated mesh
    and baked into texture space;
  - the layer survives saving and reopening the `.blend`;
  - the variant tileset shows the analysis in Cesium.

### M4 GLB export with metadata

A standard GLB of the source collection that any glTF tool can open, with
object identity and layer values carried as glTF metadata, the same metadata
3D Tiles 1.1 uses. It is a separate file from the plain `source.glb` that
rusty-tiles tiles, because rusty-tiles 0.4 refuses input with extensions.

- **Schema from layers:** a bpy-free module turns `LayerSpec`s into an
  `EXT_structural_metadata` schema (classes, properties, `min`/`max`,
  `noData`, enums for CATEGORY layers; label and units in descriptions). M9
  reuses it for tilesets.
- **Object identity:** an `EXT_mesh_features` feature ID set "object" with a
  property table (name and a persistent object id stored on the Blender
  object).
- **FACE and CORNER layers:** property attributes (vertex attributes) by
  default, which keep the mesh compact and can be styled. Optionally, FACE
  layers as a per-face feature ID set with property tables, which can be
  picked and queried but need unshared vertices (about six times the vertex
  data).
- **TEXTURE layers:** property textures, with float32 values packed into
  lossless PNG channels.
- **Placement:** vertex positions always stay in the local frame (float32
  near the origin), so the file opens correctly in any glTF tool.
  - Default: the georeference is recorded in `KHR_xmp_json_ld` (CRS as EPSG
    code and definition, E/N/A offset, height offset, origin longitude,
    latitude and height, heading). Nothing is lost, and nothing moves the
    model away from the origin.
  - Option "Earth-placed": the root node also carries the local-to-ECEF
    transform (the same matrix as the tileset root transform, from the
    placement probe), so geospatial viewers such as CesiumJS place it with no
    extra input. General tools apply it in float32 and put the model about
    6,400 km from their origin, so it is off by default.
- **Output:** a build profile output type of `.3tz` or `.glb`, through the
  same checks and staging.
- **Exit:** the 3d-tiles-validator accepts the GLB; CesiumJS shows it, picks
  objects and reads layer values; Blender's glTF importer still opens it.

### M5 Analysis library

Each tool writes a layer (M3).

- **Surface tools** (slope, aspect, roughness, curvature): numpy over normals
  and neighbours, or bundled Geometry Nodes groups.
- **Sun hours and shadow:** reuse the 0.2 solar-position maths, and bake direct
  sunlight with Cycles over a series of time steps.
- **Viewshed and line of sight:** bake the shadow of a point light placed at
  the observer; a lit surface is a visible one. Keep a raycast line-of-sight
  check between two points.
- **Distance tools:** distance to a feature or a reference surface, and change
  detection between two sources.
- **Display:** colour ramps, categorical palettes and legends.
- **Exit:** each tool finishes in under a minute on a 10M-triangle mesh, or as
  one bake.

### M6 Texture cleanup

- Blender 5.2 texture painting on source images, with UV-overlap warnings.
- Cleanup tools: fill holes from neighbouring pixels, clone and patch across
  UV islands, colour and exposure balance, and removal of masked objects such
  as people or vehicles.
- Changed images are marked dirty in the project. Publishing still means a
  full retile until B lands.
- **Exit:** a painted source republishes, and coarse levels pick up the change.

### M7 Output inspection (needs C)

- Load output leaves or coarse tiles back into Blender as read-only objects
  through `decode_tile`.
- Pick an output triangle to select its source face, and the reverse, using
  the source IDs and the face map.
- Quality checks: a heatmap of distance from coarse tiles to the source, a
  texel-density view, and a side-by-side comparison of levels of detail.
- **Exit:** you can open any tile of a published archive and trace it back to
  its source faces.

### M8 Geo formats: vectors and rasters (can run in parallel after M2)

GeoTools reads and writes the geo formats Blender cannot, and stops there.
What users do with the data inside Blender (draping, offsetting, extruding,
displacing) uses Blender's own tools, documented as recipes, not GeoTools code.

- **GeoJSON and GeoPackage import:** features become an ordinary Blender mesh
  in the project frame. Points become vertices, lines become edges, polygons
  become faces; polygons with holes are triangulated. Feature properties
  become mesh attributes (float, int, string) on the matching domain, plus a
  feature id. Coordinates are shifted by the project offset in binary64; Z is
  kept when present. Bring back the legacy 0.2 readers, vectorised to follow
  the performance rule.
- **GeoJSON and GeoPackage export:** Blender geometry, with Z and its
  attributes, written back into the project CRS. GeoPackage write-back to the
  source table by feature id, as 0.2 had.
- **Linked sources (semi-live):**
  - Each imported object keeps a link: file, layer or table, feature id
    field, CRS, import options (including filters) and a fingerprint
    (mtime, size, hash for small files). It is stored in the `.blend`.
  - **Refresh** re-reads the file and replaces only the mesh data in place,
    so modifiers (for example a draping recipe), materials, transform and
    layer links survive and re-evaluate. One undo step.
  - Stale detection on file load and on demand, shown in panel headers; an
    optional **auto-refresh** polls the file's mtime every few seconds.
  - Import filters: an attribute filter (GeoPackage SQL `WHERE`) and "within
    the site extent".
  - **Attribute write-back:** changed or new attribute columns go back to the
    GeoPackage by feature id (`UPDATE` of those columns only; GeoJSON is
    rewritten whole, keeping unknown members). Refused if the file changed
    since the last sync, with Refresh offered.
  - Geometry write-back is out of scope: QGIS stays the 2D editor, and the 3D
    result is exported as a separate layer.
- **GeoTIFF import:** read the GeoTIFF georeference (tie point, pixel scale,
  GeoKeys) and nodata. A DEM becomes a height mesh in the project frame, with
  an optional decimation step; an orthophoto becomes an image on a plane
  placed and sized to its footprint. Pixels are read through Blender's image
  loader where it can; only the georeference tags are parsed by GeoTools.
- **CRS:** data in the project CRS is placed exactly. Vector data in another
  CRS is reprojected into the project CRS with a bundled pyproj (offline, no
  grid downloads unless the user opts in), and the import reports the
  operation and its accuracy. Rasters in another CRS are refused: reproject
  them in QGIS first.
- **Recipes (docs and an example `.blend`):** drape archaeological contexts on
  a site mesh with Raycast and Set Position, offset them by a depth or level
  attribute, and extrude volumes between top and bottom levels.
- **Publish vectors** as 3D Tiles with `vector_to_3tz` and `reuse_tileset`,
  which already updates incrementally.
- **Exit:** a GeoJSON of contexts is imported, draped and offset with the
  recipe, exported back to GeoPackage with Z values a GIS reads correctly; an
  edit saved in QGIS re-drapes in Blender after a refresh; and
  a GeoTIFF DEM and orthophoto line up with the site mesh.

### M9 Queryable metadata without retiling (needs A or D)

- Publish layers as metadata keyed by `(source_primitive, source_triangle)`,
  through the annotate pass or custom vertex attributes, whichever the
  rusty-tiles 0.5 design picks.
- Texture layers become property textures.
- Standard output only: 3D Tiles 1.1 metadata (`EXT_mesh_features` feature IDs,
  `EXT_structural_metadata` property tables and property textures). Object
  identity is published the same way, as a layer, so any viewer can pick
  objects and read values. No consumer-specific sidecars or export variants;
  `source.glb` and `build.json` are GeoTools' own staging and build record,
  not a published format.
- Incremental publishing: hash each layer and re-annotate only the layers that
  changed. Geometry or texture edits still mean a retile.
- **Exit:** changing one analysis layer updates the archive in seconds without
  a retile, and the values can be queried and styled in Cesium.

### M10 Large sites and texture-only rebuilds (later)

- A chunked working mode for sources too big for one Blender session: one
  `.3tz` per chunk under a parent tileset.
  - Viewing only: coarse levels crack at chunk borders.
  - A final full publish fixes the seams.
- A texture-only republish once B lands.
