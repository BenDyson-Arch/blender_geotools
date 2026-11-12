# GeoTools

GeoTools is being rebuilt as a Blender extension for creating, analysing and
publishing OGC 3D Tiles with
[rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles).

The old 0.2 add-on imported GeoJSON, GeoPackage, DEM and 3D Tiles data. It is
kept on the `legacy-0.2` branch and the `v0.2.0` tag. It is not developed any
more.

## State

The rebuild has reached milestone M2 (publish), and work on M3 (layers and
bakes) is under way. The extension lives in the 3D Viewport sidebar (N),
**GeoTools** tab. Its panels follow the workflow from top to bottom. Each
panel header shows a tick when that step is done, a circle when it still needs
setting up, or a warning or error icon:

1. **Project:** place the project on Earth: local metres, a WGS84 anchor with a
   heading, or a source CRS with an E/N/A offset and a height offset. The CRS
   field searches every EPSG code the bundled rusty-tiles resolves, by code or
   name, near the site first, and shows the datum operation and its stated
   accuracy, or why rusty-tiles refuses the CRS (WKT and PROJ text work too).
   Offsets are stored at full double precision. **Verify Placement** asks
   rusty-tiles where the source lands on Earth before any build.
2. **Source:** pick the source collection, or import a GLB, glTF, OBJ or PLY
   into one. A summary shows its objects, faces and size.
3. **Linked Data:** import GeoJSON, GeoPackage and GeoTIFF files into the
   project frame (also in File > Import), and export meshes back to GeoJSON or
   GeoPackage with Z (File > Export). Vectors become points, edges and faces
   with their attributes; a GeoTIFF DEM becomes a height mesh and an
   orthophoto an image on a plane. Vector data in another CRS is reprojected
   with the bundled pyproj and the report says how; rasters must already be in
   the project CRS. Each object stays linked to its file: **Refresh** re-reads
   it in place (keeping modifiers and materials), changed files are flagged,
   and GeoPackage and GeoJSON attributes can be written back. Projected data
   imported into a local or anchor project offers to use the data's centre as
   the project origin.
4. **Layers:** named values per face, face corner or texel. The panel shows the
   selected layer at a glance (label, units, attribute, value range, what is
   missing). A face or corner layer reads its attribute (`gt_<name>`, or the
   name typed in **Attribute**) from the evaluated mesh, so a Geometry Nodes
   **Store Named Attribute** computes it with no bake step. **Bake** fills a
   texture layer with a Cycles bake; **Display** sets its colour ramp or
   category colours for a variant tileset; **Details** holds no-data, origin,
   aggregation and notes.
5. **Checks:** run the checks against rusty-tiles' mesh input rules, with
   one-click, opt-in fixes for oversize or 16-bit images.
6. **Publish:** pick a build profile and its output, optionally a **Variant**
   layer, and press **Publish**. It writes `site.3tz`, or `site.<layer>.3tz`
   for a variant, with the source collection's modifiers and Geometry Nodes
   applied (unless **Apply Modifiers** is off), with progress and cancel. Each build also writes the source GLB, a
   face map from output triangles back to Blender faces, and a build record
   next to the archive. **Build Settings** holds Apply Modifiers and the
   rusty-tiles quality, threads and memory; **Last Build** summarises the last publish and offers a
   local **Preview** if the rusty-tiles CLI and a Cesium runtime are set in
   preferences. A 10M-triangle textured scene publishes in about 20 minutes on
   a 20-core desktop. A profile whose **Format** is `.glb` exports the
   metadata GLB instead (below), through the same checks, with **Export
   site.glb**. A **Vectors** profile publishes the GeoJSON and GeoPackage
   objects of a collection (draped by modifiers or not) as vector 3D Tiles,
   and republishes only what changed. The
   [draping recipe](docs/recipes/drape-contexts.md) drapes excavation contexts
   on a site mesh with stock Geometry Nodes, with an example file.
7. **About** (closed by default): the extension, rusty-tiles and pyproj versions.

### GLB with metadata

Besides the tileset, GeoTools writes one standard glTF 2.0 GLB of the source
collection that any glTF tool opens, with the objects and layers as glTF
metadata, the same metadata 3D Tiles 1.1 uses: a `.glb` build profile, or
**File > Export > GeoTools GLB with metadata**.

- Every object is a feature (`EXT_mesh_features`) with a row in a property
  table: its name and a persistent id that survives renames and saves.
- Face and corner layers are vertex attributes (`EXT_structural_metadata`
  property attributes), or with **Face Table** a per-face feature ID set and
  property table, so each face can be picked and queried. Texture layers are
  property textures, the float values packed losslessly into PNGs. Category
  layers become enums, and units, no-data and value ranges are in the schema.
- Positions stay local, so the file opens in the right place in any tool. The
  CRS, offsets and the origin on Earth are recorded as XMP metadata
  (`KHR_xmp_json_ld`). **Earth-Placed** also puts the local-to-ECEF transform
  on the root node, for geospatial viewers such as CesiumJS.

It is a separate file from the plain source GLB rusty-tiles tiles. See
[docs/design/m4-glb-metadata.md](docs/design/m4-glb-metadata.md).

## Interoperability

GeoTools keeps a CRS project's georeference in the scene's **GeoScene keys**,
the convention [BlenderGIS](https://github.com/domlysz/BlenderGIS) uses: the
CRS in `SRID`, the scene origin in `crs x` and `crs y`, and the same origin in
`latitude` and `longitude`. So:

- A scene georeferenced by BlenderGIS opens in GeoTools as a CRS project. Data
  imported with either tool lands on the same coordinates.
- A scene set up in GeoTools is georeferenced for BlenderGIS and other tools
  that read these keys.
- GeoTools does not need BlenderGIS and works the same without it.
- Files saved by earlier GeoTools versions are moved to the keys when you open
  them. A notice says what was taken from where.

Some things GeoScene cannot hold:

- Local and anchor projects, and a CRS given as WKT or as a compound code,
  have no keys. If a scene has keys while the project is local or anchor, the
  Project panel warns and offers **Use the Scene's BlenderGIS Georeference**.
- Offset A and the height offset stay GeoTools settings. GeoScene tools read
  Blender Z as absolute height, so new CRS projects start with A = 0. When A
  is not 0, the Project panel and Check warn that other tools will see heights
  shifted by A.
- Changing the origin in GeoTools moves the scene on Earth and leaves objects
  where they are in Blender. BlenderGIS's own origin edits move the objects
  instead.

Details:
[docs/design/georeference-interop.md](docs/design/georeference-interop.md).

## Coming next

The plan is in [docs/ROADMAP.md](docs/ROADMAP.md).

- M3: layers computed by Geometry Nodes or stored on the mesh, and baking
  attributes to texture space.
- M5: analysis: georeferenced sun, archaeoastronomy, 3D hydrology,
  measurement, and a library of Geometry Nodes analysis presets.
- M6: texture painting and cleanup.
- M7: open output tiles in Blender and check their quality.
- M8: GeoJSON, GeoPackage and GeoTIFF import and export, vector publishing,
  and the [draping recipe](docs/recipes/drape-contexts.md) are in.
- M9: queryable metadata without retiling (needs rusty-tiles 0.5).
- M10: large sites and texture-only rebuilds.

## Requirements

- Blender 5.2 LTS. Only the latest Blender release is supported.
- Linux x64, Windows x64 or macOS arm64 (Apple silicon; Blender 5.x has no
  Intel macOS build).
- On macOS, macOS 15 or later. The rusty-tiles wheels are built for that.

Nothing else. rusty-tiles and pyproj (with PROJ, no GDAL) are bundled in the
extension zip, so there is no package to install.

## Install

1. Get the extension zip for your platform, for example
   `geotools-0.3.0-dev-linux_x64.zip`.
2. In Blender, open **Edit > Preferences > Get Extensions** and choose
   **Install from Disk**. Or drag the zip into the Blender window.
3. Open the 3D Viewport sidebar (N) and select the **GeoTools** tab. The
   **About** panel header shows a tick when the bundled rusty-tiles loads; open
   it to see the version.

There are no releases yet, so you have to build the zip yourself. You need
Python 3.11 or later and Blender 5.2:

```sh
python3 scripts/fetch_wheels.py --from-dir rusty-tiles=DIR   # DIR holds a rusty-tiles wheel; pyproj comes from PyPI
python3 scripts/build_extension.py --blender /path/to/blender
```

GeoTools needs rusty-tiles 0.4.1 or later (its EPSG allow-list). Until 0.4.1
is released, build the wheel from rusty-tiles' `develop` branch (it still
reports version 0.4.0, the pin). The zips are written to `dist/`, one per
platform. Once rusty-tiles 0.4.1 is on PyPI, `fetch_wheels.py` will download every wheel without `--from-dir`. A
Blender that doesn't run Python 3.13 (some Linux distribution builds) needs
`--python` on both commands. See
[CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
