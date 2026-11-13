---
title: GeoTools
description: >-
  GIS for Blender: georeference a site, bring in geodata, analyse the full 3D
  mesh, and publish it as OGC 3D Tiles with rusty-tiles.
hide:
  - toc
---

# GeoTools

<!-- Hero option B (designed banner): assets/readme/hero-b.png (dark) or hero-b-light.png. -->
![Four analyses of a synthetic site in Blender (slope and contours, a viewshed, solstice and lunar sightlines, flow paths) beside the site published as 3D Tiles in CesiumJS](assets/readme/hero-a.png)

GeoTools brings GIS into Blender. Place a site in a real coordinate system,
bring in GeoJSON, GeoPackage and GeoTIFF, and analyse the full 3D mesh, not a
2.5D raster: terrain shape, archaeological visualisation, visibility, sun and
shadow, archaeoastronomy, hydrology, measurement, contours and sections.
Results are layers you can view, export as vectors or GLB metadata, and
publish as OGC 3D Tiles with
[rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles). Everything is on
the **GeoTools** tab of the 3D Viewport sidebar.

[Get started](guide/getting-started.md){ .md-button .md-button--primary }
[Read the user guide](guide/README.md){ .md-button }

## What it does

<div class="grid cards features" markdown>

-   ![The Project panel of a CRS project in EPSG:32631, WGS 84 / UTM zone 31N, with its offsets and where Verify Placement puts the site](assets/readme/feature-georeference.png)

    **Georeference**

    Local, Anchor or CRS projects, with a searchable EPSG picker and Verify
    Placement to see where the site lands.

    [:octicons-arrow-right-24: Georeference and projects](guide/projects.md)

-   ![The Linked Data panel with a linked GeoJSON and GeoTIFF over the terrain](assets/readme/feature-formats.png)

    **Geo formats**

    GeoJSON, GeoPackage and GeoTIFF in, vectors out, all in the project's frame
    and kept linked to their files.

    [:octicons-arrow-right-24: Geo formats](guide/geo-formats.md)

-   ![The Presets and Layers panels with a viewshed on the terrain: the mound hides the ground behind it](assets/readme/feature-terrain.png)

    **Terrain and visibility**

    Slope, aspect, curvature, TPI and TRI; local relief, sky-view factor and
    openness; viewsheds, intervisibility and line of sight; volumes and
    least-cost paths. Editable Geometry Nodes presets.

    [:octicons-arrow-right-24: Analysis presets](guide/analysis.md#presets)

-   ![The Archaeoastronomy panel with solstice and lunar standstill sightlines from the top of a mound](assets/readme/feature-sky.png)

    **Sun and sky**

    Place the Sun for a date and place, map hours of sun, key a shadow study,
    and find solstice, equinox and lunar standstill events on the real horizon.

    [:octicons-arrow-right-24: Sun and archaeoastronomy](guide/analysis.md#archaeoastronomy)

-   ![The Hydrology panel with flow paths and contour lines on the terrain](assets/readme/feature-hydrology.png)

    **Hydrology and measurement**

    Where water runs and ponds on the 3D surface, contours and sections, and
    points, distances and areas in real coordinates.

    [:octicons-arrow-right-24: Hydrology, contours and measurement](guide/analysis.md#hydrology)

-   ![The Layers panel with the terrain coloured by slope](assets/readme/feature-layers.png)

    **Layers**

    Values per face, corner or texel, shown as colours and published as variant
    tilesets.

    [:octicons-arrow-right-24: Layers and bakes](guide/layers.md)

-   ![The Textures panel's Cleanup tools next to holes in a terrain texture](assets/readme/feature-textures.png)

    **Texture cleanup**

    Fill photogrammetry holes, remove masked objects, pad UV islands and balance
    atlas pages.

    [:octicons-arrow-right-24: Texture cleanup](guide/textures.md)

-   ![The Publish panel's Last Build showing a validated site.3tz next to the tileset in CesiumJS](assets/readme/feature-publish.png)

    **Publish**

    Checks first, then a validated `.3tz` archive of 3D Tiles to
    extract and serve to CesiumJS or any 3D Tiles viewer, or a GLB with metadata.

    [:octicons-arrow-right-24: Checks and publishing](guide/publish.md)

</div>

## Also in GeoTools

<div class="grid cards" markdown>

-   **Inspect output**

    Open published tiles back in Blender and trace them to their source
    faces.

    [:octicons-arrow-right-24: Inspecting output](guide/inspect-output.md)

-   **GLB with metadata**

    One standard glTF file with the objects, layers and georeference as
    metadata.

    [:octicons-arrow-right-24: GLB with metadata](guide/glb-metadata.md)

-   **Working with BlenderGIS**

    GeoTools shares BlenderGIS's scene georeference keys, and does not need
    BlenderGIS.

    [:octicons-arrow-right-24: Working alongside BlenderGIS](guide/blendergis.md)

-   **Analysis presets**

    Every Geometry Nodes preset with its formula, inputs, accuracy and
    timing.

    [:octicons-arrow-right-24: Analysis presets](analysis-presets.md)

</div>

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

Then follow [Getting started](guide/getting-started.md): it takes a synthetic
demo site from an empty Blender to a published tileset in about ten minutes.

## Project

- [Contributing](../CONTRIBUTING.md), the [changelog](../CHANGELOG.md) and the
  [roadmap](ROADMAP.md).
- GeoTools is licensed under GPL-3.0-or-later ([licence](../LICENSE)).
- The old 0.2 add-on lives on the
  [`legacy-0.2`](https://github.com/BenDyson-Arch/blender_geotools/tree/legacy-0.2)
  branch and is no longer developed.
