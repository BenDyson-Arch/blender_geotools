# GeoTools

GeoTools is being rebuilt as a Blender extension for creating, analysing and
publishing OGC 3D Tiles with
[rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles).

The old 0.2 add-on imported GeoJSON, GeoPackage, DEM and 3D Tiles data. It is
kept on the `legacy-0.2` branch and the `v0.2.0` tag. It is not developed any
more.

## State

The rebuild has reached milestone M2 (publish). In the 3D Viewport sidebar
(**GeoTools** tab) the extension can:

- **Place the project:** local metres, a WGS84 anchor with a heading, or a
  source CRS with an E/N/A offset and a height offset. GDA2020 MGA and NZTM are
  available as presets. Offsets are stored at full double precision.
- **Ingest a source mesh:** GLB, glTF, OBJ or PLY goes into a source collection.
- **Run checks** against rusty-tiles' mesh input rules, with one-click,
  opt-in fixes for oversize or 16-bit images.
- **Verify placement:** rusty-tiles reports where the source lands on Earth, or
  why it refuses the CRS, before any build.
- **Publish** the source collection (modifiers and Geometry Nodes applied) to a
  validated `.3tz` with progress and cancel. Each build writes the source GLB,
  a face map from output triangles back to Blender faces, and a build record
  next to the archive. A 10M-triangle textured scene publishes in about 20
  minutes on a 20-core desktop.
- **Preview** the output locally if the rusty-tiles CLI and a Cesium runtime are
  set in preferences.

## Coming next

The plan is in [docs/ROADMAP.md](docs/ROADMAP.md).

- M3: layers, and baking Geometry Nodes and attributes to vertex or texture
  space.
- M4: GLB export with object and layer metadata (`EXT_mesh_features`,
  `EXT_structural_metadata`).
- M5: analysis tools rebuilt on the bake engine.
- M6: texture painting and cleanup.
- M7: open output tiles in Blender and check their quality.
- M8: vector layers from GeoPackage.
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
3. Open the 3D Viewport sidebar (N) and select the **GeoTools** tab. It should
   show the rusty-tiles version.

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
