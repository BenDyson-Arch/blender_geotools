# GeoTools

GeoTools is a Blender extension for creating, analysing and publishing OGC 3D
Tiles with [rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles).

You prepare a site in Blender, place it on Earth, analyse it, clean its
textures, and publish it as a `.3tz` archive that any 3D Tiles viewer can
stream. GeoTools also reads and writes GeoJSON, GeoPackage and GeoTIFF in the
site's coordinates.

**Read the [user guide](docs/guide/README.md).** Start with
[Getting started](docs/guide/getting-started.md): it takes a synthetic demo
site from an empty Blender to a published tileset in about ten minutes.

## What it does

- **Georeference:** local, anchored or CRS projects, with a searchable EPSG
  picker and offsets kept at full double precision.
  [Guide](docs/guide/projects.md)
- **Publish:** the source collection to a validated `.3tz`, with checks first
  and a build record and face map beside it. [Guide](docs/guide/publish.md)
- **Layers:** values per face, corner or texel, from Geometry Nodes or
  bakes, published as coloured variant tilesets. [Guide](docs/guide/layers.md)
- **GLB with metadata:** one standard glTF file with the objects, layers and
  georeference as glTF metadata. [Guide](docs/guide/glb-metadata.md)
- **Analysis:** sun and shadow studies, sun hours and insolation,
  archaeoastronomy, measurement, 3D hydrology, contours and sections, and a
  library of Geometry Nodes presets. [Guide](docs/guide/analysis.md)
- **Texture cleanup:** paint setup, hole filling, masked object removal,
  island padding and page balancing. [Guide](docs/guide/textures.md)
- **Inspect output:** open published tiles in Blender and trace them back to
  their source faces. [Guide](docs/guide/inspect-output.md)
- **Geo formats:** GeoJSON, GeoPackage and GeoTIFF in and out, kept linked to
  their files, and vector publishing. [Guide](docs/guide/geo-formats.md)
- **BlenderGIS interop:** the scene georeference uses BlenderGIS's GeoScene
  keys, so both tools agree. BlenderGIS is not required.
  [Guide](docs/guide/blendergis.md)

The old 0.2 add-on, which imported GeoJSON, GeoPackage, DEM and 3D Tiles data,
is kept on the `legacy-0.2` branch and the `v0.2.0` tag. It is not developed
any more.

## Requirements

- Blender 5.2 LTS. Only the latest Blender release is supported.
- Linux x64, Windows x64 or macOS arm64 (Apple silicon; Blender 5.x has no
  Intel macOS build).
- On macOS, macOS 15 or later. The rusty-tiles wheels are built for that.

Nothing else. rusty-tiles and pyproj (with PROJ, no GDAL) are bundled in the
extension zip, so there is no package to install.

## Install

1. Get the extension zip for your platform from the
   [releases](https://github.com/BenDyson-Arch/blender_geotools/releases), for
   example `geotools-0.3.0-linux_x64.zip`.
2. In Blender, open **Edit > Preferences > Get Extensions** and choose
   **Install from Disk**. Or drag the zip into the Blender window.
3. Open the 3D Viewport sidebar (N) and select the **GeoTools** tab. The
   **About** panel shows the bundled rusty-tiles and pyproj versions.

To build the zip yourself (Python 3.11 or later and Blender 5.2):

```sh
python3 scripts/fetch_wheels.py --from-dir rusty-tiles=DIR   # DIR holds a rusty-tiles wheel; pyproj comes from PyPI
python3 scripts/build_extension.py --blender /path/to/blender
```

GeoTools needs rusty-tiles 0.4.1 or later. Until 0.4.1 is released, build the
wheel from rusty-tiles' `develop` branch (it still reports version 0.4.0, the
pin). The zips are written to `dist/`, one per platform. Once rusty-tiles
0.4.1 is on PyPI, `fetch_wheels.py` will download every wheel without
`--from-dir`. A Blender that doesn't run Python 3.13 (some Linux distribution
builds) needs `--python` on both commands. See
[CONTRIBUTING.md](CONTRIBUTING.md) for details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the plan in
[docs/ROADMAP.md](docs/ROADMAP.md).

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
