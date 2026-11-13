# GeoTools user guide

GeoTools is a Blender 5.2 extension. You prepare a site in Blender, analyse it,
clean its textures, and publish it as OGC 3D Tiles with
[rusty-tiles](https://github.com/BenDyson-Arch/rusty-tiles). GeoTools also
reads and writes GeoJSON and GeoPackage, and reads GeoTIFF, in the site's
coordinates.

Everything is in the 3D Viewport sidebar (press **N**), on the **GeoTools**
tab. The panels follow the workflow from top to bottom. Each panel header
shows a tick when its step is done, a circle when it still needs setting up,
or a warning or error icon.

## Start here

[Getting started](getting-started.md): install GeoTools, place a synthetic
demo site on Earth, run the checks, publish a `.3tz` and view it. About ten
minutes.

## By task

| Page | Panels | What it covers |
|---|---|---|
| [Georeference and projects](projects.md) | Project, Source | Local, Anchor and CRS projects; the EPSG picker; offsets and offset files; heights; Verify Placement. |
| [Layers and bakes](layers.md) | Layers | Values per face, corner or texel; layers from Geometry Nodes; texture bakes; colours; variant tilesets. |
| [Checks and publishing](publish.md) | Checks, Publish | The checks and their fixes; build profiles; what a publish writes; preview. |
| [GLB with metadata](glb-metadata.md) | Publish, File > Export | One glTF file with objects, layers and the georeference as metadata. |
| [Analysis](analysis.md) | Analysis | Sun, sun hours, shadow studies, archaeoastronomy, coordinates and measurement, presets, hydrology, contours and sections. |
| [Texture cleanup](textures.md) | Textures | Painting setup, filling holes, removing masked objects, padding, balancing pages. |
| [Inspecting output](inspect-output.md) | Inspect Output | Opening published tiles in Blender and tracing them to the source. |
| [Geo formats](geo-formats.md) | Linked Data, File > Import and Export | GeoJSON and GeoPackage in and out, GeoTIFF in; linked files; vector publishing. |
| [Working alongside BlenderGIS](blendergis.md) | Project | The shared scene georeference, and what stays GeoTools-only. |

## Reference pages

- [Analysis presets](../analysis-presets.md): every Geometry Nodes preset, its
  formula, inputs, accuracy and timing.
- [Texture cleanup](../texture-cleanup.md): the full cleanup workflow and
  Blender recipes.
- [Recipe: drape excavation contexts](../recipes/drape-contexts.md): drape a
  GIS plan on a site mesh with stock Geometry Nodes, with an example file.
- The design notes in [docs/design/](../design/) explain how each part works.
- [ROADMAP.md](../ROADMAP.md): what comes next.

## The demo data

The guide uses the synthetic site in
[`examples/drape-contexts/`](../../examples/drape-contexts/): a 60 m terrain
mesh with its offset file, and nine invented excavation contexts, at round
coordinates in WGS 84 / UTM zone 31N (EPSG:32631). Every number in it is
made up. `scripts/make_example_drape.py` makes the files again.

## What GeoTools does not do

GeoTools reads and writes geo formats (GeoTIFF is read only), handles the
georeference, analyses and publishes. Modelling, draping, displacement and
painting use Blender's own tools; the guide points to recipes where that
helps. It does not import 3D Tiles for editing (only for
[inspection](inspect-output.md)), and it does not reproject rasters.
