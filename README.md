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
   heading, or a source CRS with an E/N/A offset and a height offset. GDA2020
   MGA and NZTM are presets, shown by name; the PROJ definition and the datum
   note are in **CRS Details** and the tooltip. Offsets are stored at full
   double precision. **Verify Placement** asks rusty-tiles where the source
   lands on Earth, or why it refuses the CRS, before any build.
2. **Source:** pick the source collection, or import a GLB, glTF, OBJ or PLY
   into one. A summary shows its objects, faces and size.
3. **Layers:** named values per face, face corner or texel. The panel shows the
   selected layer at a glance (label, units, attribute, value range, what is
   missing). A face or corner layer reads its attribute (`gt_<name>`, or the
   name typed in **Attribute**) from the evaluated mesh, so a Geometry Nodes
   **Store Named Attribute** computes it with no bake step. **Bake** fills a
   texture layer with a Cycles bake; **Display** sets its colour ramp or
   category colours for a variant tileset; **Details** holds no-data, origin,
   aggregation and notes.
4. **Checks:** run the checks against rusty-tiles' mesh input rules, with
   one-click, opt-in fixes for oversize or 16-bit images.
5. **Publish:** pick a build profile and its output, optionally a **Variant**
   layer, and press **Publish**. It writes `site.3tz`, or `site.<layer>.3tz`
   for a variant, with the source collection's modifiers and Geometry Nodes
   applied (unless **Apply Modifiers** is off), with progress and cancel. Each build also writes the source GLB, a
   face map from output triangles back to Blender faces, and a build record
   next to the archive. **Build Settings** holds Apply Modifiers and the
   rusty-tiles quality, threads and memory; **Last Build** summarises the last publish and offers a
   local **Preview** if the rusty-tiles CLI and a Cesium runtime are set in
   preferences. A 10M-triangle textured scene publishes in about 20 minutes on
   a 20-core desktop.
6. **About** (closed by default): the extension and rusty-tiles versions.

## Coming next

The plan is in [docs/ROADMAP.md](docs/ROADMAP.md).

- M3: layers computed by Geometry Nodes or stored on the mesh, and baking
  attributes to texture space.
- M4: GLB export with object and layer metadata (`EXT_mesh_features`,
  `EXT_structural_metadata`).
- M5: analysis tools rebuilt on the bake engine.
- M6: texture painting and cleanup.
- M7: open output tiles in Blender and check their quality.
- M8: GeoJSON, GeoPackage and GeoTIFF import and export.
- M9: queryable metadata without retiling (needs rusty-tiles 0.5).
- M10: large sites and texture-only rebuilds.

## Requirements

- Blender 5.2 LTS. Only the latest Blender release is supported.
- Linux x64, Windows x64, macOS arm64 or macOS x64.
- On macOS, macOS 15 or later. The rusty-tiles wheels are built for that.

Nothing else. rusty-tiles is bundled in the extension zip, so there is no
pyproj, GDAL or other package to install.

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
python3 scripts/fetch_wheels.py --from-dir DIR   # DIR holds rusty-tiles 0.4.0 wheels
python3 scripts/build_extension.py --blender /path/to/blender
```

The zips are written to `dist/`, one per platform. Once rusty-tiles 0.4.0 is on
PyPI, `fetch_wheels.py` will download the wheels without `--from-dir`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
