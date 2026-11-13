# GeoTools

<!-- Hero option B (designed banner): docs/assets/readme/hero-b.png (dark) or hero-b-light.png. Swap the src below to use it. -->
<img src="docs/assets/readme/hero-a.png" alt="A synthetic terrain coloured by slope in Blender's GeoTools sidebar, and the same terrain published as 3D Tiles and streamed in CesiumJS" width="100%">

GeoTools is a Blender 5.2 extension that places a site on Earth, analyses it,
and publishes it as OGC 3D Tiles with
[rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles).

[![CI](https://github.com/BenDyson-Arch/blender_geotools/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/BenDyson-Arch/blender_geotools/actions/workflows/ci.yml?query=branch%3Adevelop)
[![Latest release](https://img.shields.io/github/v/release/BenDyson-Arch/blender_geotools)](https://github.com/BenDyson-Arch/blender_geotools/releases/latest)
[![Blender 5.2 LTS](https://img.shields.io/badge/Blender-5.2%20LTS-e87d0d?logo=blender&logoColor=white)](https://www.blender.org/download/lts/)
[![Licence: GPL-3.0-or-later](https://img.shields.io/badge/licence-GPL--3.0--or--later-blue)](LICENSE)

## What it does

<table>
<tr>
<td width="50%" valign="top">
<a href="docs/guide/projects.md"><img src="docs/assets/readme/feature-georeference.png" alt="The Project panel of a CRS project in EPSG:32631, WGS 84 / UTM zone 31N, with its offsets and where Verify Placement puts the site" width="380"></a><br>
<b>Georeference</b><br>
Local, Anchor or CRS projects, with a searchable EPSG picker and Verify
Placement to see where the site lands.
<a href="docs/guide/projects.md">Guide</a>
</td>
<td width="50%" valign="top">
<a href="docs/guide/geo-formats.md"><img src="docs/assets/readme/feature-formats.png" alt="The Linked Data panel with a linked GeoJSON and GeoTIFF over the terrain" width="380"></a><br>
<b>Geo formats</b><br>
GeoJSON, GeoPackage and GeoTIFF in, vectors out, all in the project's frame
and kept linked to their files.
<a href="docs/guide/geo-formats.md">Guide</a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="docs/guide/layers.md"><img src="docs/assets/readme/feature-layers.png" alt="The Layers panel with the terrain coloured by slope" width="380"></a><br>
<b>Layers</b><br>
Values per face, corner or texel, shown as colours and published as variant
tilesets.
<a href="docs/guide/layers.md">Guide</a>
</td>
<td width="50%" valign="top">
<a href="docs/guide/analysis.md"><img src="docs/assets/readme/feature-contours.png" alt="The Contours and Sections panel with contour lines on the terrain" width="380"></a><br>
<b>Analysis</b><br>
Sun and shadow studies, archaeoastronomy, hydrology, contours, sections and
editable Geometry Nodes presets.
<a href="docs/guide/analysis.md">Guide</a>
</td>
</tr>
<tr>
<td width="50%" valign="top">
<a href="docs/guide/textures.md"><img src="docs/assets/readme/feature-textures.png" alt="The Textures panel's Cleanup tools next to holes in a terrain texture" width="380"></a><br>
<b>Texture cleanup</b><br>
Fill photogrammetry holes, remove masked objects, pad UV islands and balance
atlas pages.
<a href="docs/guide/textures.md">Guide</a>
</td>
<td width="50%" valign="top">
<a href="docs/guide/publish.md"><img src="docs/assets/readme/feature-publish.png" alt="The Publish panel's Last Build showing a validated site.3tz next to the tileset in CesiumJS" width="380"></a><br>
<b>Publish</b><br>
Checks first, then a validated <code>.3tz</code> archive of 3D Tiles to
extract and serve to CesiumJS or any 3D Tiles viewer, or a GLB with metadata.
<a href="docs/guide/publish.md">Guide</a>
</td>
</tr>
</table>

You can also open published tiles back in Blender and trace them to their
source faces with
[Inspect Output](docs/guide/inspect-output.md).

## Install

You need Blender 5.2 LTS on Linux x64, Windows x64 or macOS arm64 (macOS 15 or
later). rusty-tiles and pyproj are bundled, so there is nothing else to
install.

1. Download the zip for your platform from the
   [releases](https://github.com/BenDyson-Arch/blender_geotools/releases):
   `geotools-<version>-linux_x64.zip`, `-windows_x64.zip` or
   `-macos_arm64.zip`.
2. In Blender, open **Edit > Preferences > Get Extensions** and choose
   **Install from Disk** (or drag the zip into the Blender window).
3. In the 3D Viewport, press **N** and select the **GeoTools** tab.

To build the zip yourself, see
[CONTRIBUTING.md](CONTRIBUTING.md#building).

The old 0.2 add-on lives on the
[`legacy-0.2`](https://github.com/BenDyson-Arch/blender_geotools/tree/legacy-0.2)
branch and is no longer developed.

## Documentation

Read the [user guide](docs/guide/README.md) here or on the docs site,
<https://bendyson-arch.github.io/blender_geotools/>. Start with
[Getting started](docs/guide/getting-started.md): it takes a synthetic demo
site from an empty Blender to a published tileset in about ten minutes.

## Working with BlenderGIS

GeoTools shares BlenderGIS's scene georeference keys, so both agree on where
the scene is, and it does not need BlenderGIS:
[Working alongside BlenderGIS](docs/guide/blendergis.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the plan in
[docs/ROADMAP.md](docs/ROADMAP.md).

## Licence

GPL-3.0-or-later. See [LICENSE](LICENSE).
