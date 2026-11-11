# GeoTools

GeoTools is being rebuilt as a Blender extension for creating, analysing and
publishing OGC 3D Tiles with
[rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles).

The old 0.2 add-on imported GeoJSON, GeoPackage, DEM and 3D Tiles data. It is
kept on the `legacy-0.2` branch and the `v0.2.0` tag. It is not developed any
more.

## State

The rebuild is at milestone M0 (foundation). Today the extension:

- installs and enables in Blender 5.2;
- shows its version and the bundled rusty-tiles version in the 3D Viewport
  sidebar (**GeoTools** tab) and in its preferences;
- has a **Check Dependencies** button that loads rusty-tiles again and reports
  the result.

It does not import, analyse or publish anything yet.

## Coming next

The plan is in [docs/ROADMAP.md](docs/ROADMAP.md).

- M1: project georeference (CRS, offsets) and source mesh ingest.
- M2: publish from Blender to a validated `.3tz`. First release, 0.3.0.
- M3: layers, and baking Geometry Nodes and attributes to vertex or texture
  space.
- M4: analysis tools rebuilt on the bake engine.
- M5: texture painting and cleanup.
- M6: open output tiles in Blender and check their quality.
- M7: vector layers from GeoPackage.
- M8: queryable metadata without retiling (needs rusty-tiles 0.5).
- M9: large sites and texture-only rebuilds.

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
