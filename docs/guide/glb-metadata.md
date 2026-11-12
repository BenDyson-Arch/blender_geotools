# GLB with metadata

Besides the tileset, GeoTools writes one standard glTF 2.0 GLB of the source
collection, with the objects and layers as glTF metadata. Any glTF tool opens
it. Viewers that understand 3D Tiles metadata, such as CesiumJS, can pick each
object and read its layer values.

Use it to hand a site to someone who works in another 3D tool, to keep layer
values with the geometry, or to check layers in a metadata-aware viewer.

It is a different file from `source.glb`, the plain GLB that rusty-tiles
tiles. rusty-tiles 0.4 refuses input with metadata, so the two stay apart.

## Export it

Either:

- **File > Export > GeoTools GLB with metadata (.glb)**, or
- a build profile with **Format** set to **.glb**, and its **Output** ending
  in `.glb`. Its button reads **Export site.glb**. This runs the same checks
  and keeps a `site.glb.geotools/build.json` record, like a publish.

The options:

- **Apply Modifiers** (on): export each object's evaluated mesh, with its
  modifiers and Geometry Nodes, and read layer values from it.
- **Face Table** (in the export dialog: **Face Values as a Feature Table**):
  see [Face layers](#face-layers) below. Off by default.
- **Earth-Placed**: see [Placement](#placement) below. Off by default.

Both routes run the checks first and stop on an error.

## What the file holds

- **Objects.** Every object is a feature with a row in a property table
  (`EXT_mesh_features`): its name and a persistent id. The id is a UUID kept on
  the Blender object, so it survives renames and saves.
- **Layers.** Layers become an `EXT_structural_metadata` schema with each
  layer's name, units, no-data value and value range. Category layers become
  enums.
- **Texture layers** become property textures, their float values packed
  losslessly into PNGs.
- **The georeference.** The CRS, the offsets, the height offset and the
  origin's longitude, latitude and height are recorded as XMP metadata
  (`KHR_xmp_json_ld`).

### Face layers

By default, face and corner layers are vertex attributes. They are compact
and can be styled.

With **Face Table** on, face layers become a per-face feature ID set with a
property table, so each face can be picked and its values queried. Vertices
are then shared only within a face, which can be up to about six times the
vertex data of a smooth mesh.

### Placement

Positions always stay in the local frame, near the origin. So the file opens
in the right place in any glTF tool, in single precision, with nothing lost.

**Earth-Placed** also puts the local-to-ECEF transform on the root node, so
geospatial viewers such as CesiumJS place the model on Earth with no extra
input. General glTF tools apply it too and then show the model about 6,400 km
from their origin, which is why it is off by default. Earth-Placed needs
rusty-tiles, which computes the transform.

## Limits

- At most two UV maps per mesh, including the texture layers' UV maps.
- The face table holds at most 2^24 faces. Integer layers written as vertex
  attributes must stay within ±2^24.
- A float layer without a no-data value writes NaN where it has no value. Set
  a no-data value to make it explicit.
- This GLB is not tiled. Tiling it, with its metadata, needs rusty-tiles 0.5.

Design details: [design/m4-glb-metadata.md](../design/m4-glb-metadata.md).
