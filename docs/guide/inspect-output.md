# Inspecting output

The **Inspect Output** panel, after Publish, opens published tiles back in
Blender, over the source they were made from. Use it to see what a level of
detail looks like, to find the source faces behind an output face, and to
check how far a coarse tile strays from the source.

Blender's own glTF importer cannot read published tiles (they use meshopt
compression and KTX2 textures). Inspect Output reads them through rusty-tiles
instead. It needs the bundled rusty-tiles 0.4.1 or later; without it the panel
says why tiles cannot be opened.

## Open tiles

1. Choose the **Archive**: **Last Publish** (what the selected build profile
   published last) or **File** (any `.3tz`, chosen on disk).
2. Press **List Tiles**. The list shows every tile by depth, with its role,
   geometric error and triangle count.
3. Choose what to **Open**:
   - **Selected Tile**: the tile selected in the list;
   - **One Depth**: every tile at one **Depth**;
   - **All Leaves**: every leaf tile.
   **Limit** (64) caps how many tiles open at once.
4. Press **Open Tiles**.

Each tile becomes a read-only mesh object in a **GeoTools Output** collection,
with its textures and materials. Each face carries `source_primitive` and
`source_triangle` attributes. Tiles are placed with the placement `build.json`
recorded, so they sit over the source. If the project has changed since the
publish, the panel says so.

Leaves land on the source within rusty-tiles' reported leaf error: 0 to
0.005 mm for the **High** and **Lossless** quality profiles, and the 16-bit
position step for **Fast** and **Balanced**.

**Remove Opened Tiles** deletes them again.

## Trace to Source

Trace goes both ways, through the face map that publishing wrote beside the
archive (`facemap.npz`):

- Select faces of an opened tile in Edit Mode, then press **Select Source
  Faces**. The source faces they came from are selected.
- Select source faces, then press **Select Output Faces**. The faces of the
  opened tiles made from them are selected.

A leaf traces to exact source triangles. A coarse tile keeps only a
representative source primitive per face, so it selects that whole primitive,
and says so.

If you edited the source mesh after publishing, the face map no longer fits,
and tracing is refused. Publish again.

## Quality

- **Distance to Source** colours each face of the opened tiles by the distance
  from its centre to the source. **Max Distance** sets where the colour scale
  ends; 0 means four times the tile's geometric error.
- **Texel Density** colours each face by its texels per m².
- **Levels** compares levels of detail: **All** shows every opened tile in
  place, **One Depth** shows only one **Depth**, and **Side by Side** moves
  each depth along +X by **Gap** metres.

## Limits

- Tracing needs the face map beside the archive, so it works only for
  archives GeoTools published. Any `.3tz` can be opened; without a
  `build.json` beside it, its tiles are placed with the scene's current
  placement, and the panel says so.
- Opening many tiles makes many objects. Raise **Limit** with care.

Design details:
[design/m7-output-inspection.md](../design/m7-output-inspection.md).
