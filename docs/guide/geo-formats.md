# Geo formats: GeoJSON, GeoPackage and GeoTIFF

GeoTools reads and writes the geo formats Blender cannot, in the project's
frame. What you then do with the data (drape it, offset it, extrude it) uses
Blender's own tools. The [draping recipe](../recipes/drape-contexts.md) shows
how.

| Format | Import | Export | Publish |
|---|---|---|---|
| GeoJSON | Points, lines and polygons, with attributes | Yes, with Z | As vector 3D Tiles |
| GeoPackage | Feature tables, with attributes and an SQL filter | Yes, with Z | As vector 3D Tiles |
| GeoTIFF | A DEM as a height mesh, or an orthophoto on a plane | No | With the source mesh |

Import from **File > Import** (**GeoJSON (.geojson)**, **GeoPackage (.gpkg)**,
**GeoTIFF (.tif, .tiff)**) or from the **Import** row of the **Linked Data**
panel. New objects go into the active collection. Make sure that is not the
source collection, unless you want them published with the site mesh.

## Coordinates and CRS

Imports are placed in the project frame: GeoTools subtracts the project offset
in double precision, and only then stores the result in Blender's single
precision. Z is kept when the data has it.

- **Data in the project CRS** is placed exactly.
- **Vector data in another CRS** is reprojected into the project CRS with the
  bundled pyproj. The report names the operation and its accuracy. GeoTools
  targets the project's EPSG code, so, for example, GDA94 data moves the real
  1.5 m or so into a GDA2020 project. Grid files are not downloaded unless you
  tick **Allow Grid Downloads**. Without pyproj, such data is refused, with
  both CRSs named.
- **Data with no stated CRS** can be taken as being in the project CRS: tick
  **Data Is in Project CRS** when you import.
- **Rasters in another CRS** are refused. Reproject them in QGIS first.

If the project has no CRS yet, or is Local or Anchor, an import can set the
project from the data. See
[Georeference and projects](projects.md#data-that-brings-its-own-crs).

## GeoJSON and GeoPackage

### Import

1. **File > Import > GeoJSON (.geojson)** or **GeoPackage (.gpkg)**.
2. For a GeoPackage, choose the **Layer** (empty imports every feature table)
   and an optional **Filter**: an SQL `WHERE` clause such as
   `phase = 2 AND depth > 0.5`.
3. Optionally tick **Within Site Extent**: only features whose bounds overlap
   the source collection.
4. Import.

You get one mesh object per layer and geometry kind:

- points become loose vertices, lines loose edges, polygons faces. A polygon
  with holes is cut into triangles;
- every property becomes an attribute of the same name on the point, edge or
  face domain (float, integer, boolean or string), plus an integer `fid`, the
  feature id.

To see the attributes, open a Spreadsheet editor and select the object.

In the demo, import `contexts.geojson` into the EPSG:32631 project from
[Getting started](getting-started.md). You get `contexts`: nine faces, flat at
height 0 since the file has no Z, with `context`, `type`, `phase`,
`depth_top`, `depth_bottom` and `fid` attributes. The
[draping recipe](../recipes/drape-contexts.md) takes it from there.

### Linked files

Each imported object stays linked to its file. The **Linked Data** panel lists
the linked objects with their state: **Up to Date**, **Changed on Disk**,
**Missing** or **Error**. The panel header warns when a file changed.

For the selected link:

- **Refresh** reads the file again and replaces only the mesh data. Modifiers,
  materials, transform and parent stay, so a draping modifier re-drapes the
  new geometry. The report lists features added, removed and changed by fid.
- **Auto** refreshes by itself: GeoTools checks the file every 2 seconds.
- **Filters** changes the import filters and refreshes.
- **Write Back** writes the attributes you tick back to the file: a GeoPackage
  by `UPDATE` on each feature's fid (only those columns), a GeoJSON by
  rewriting it, keeping members GeoTools does not know. Geometry is never
  written back. It is refused if the file changed since the last sync; refresh
  first.
- **Export** writes the object to a new file (below).

The button beside the list (**Check Linked Files**) checks every link against
its file. Links are also checked when you open the `.blend`.

Edit geometry in QGIS, not in Blender: Refresh picks up the change.

### Export

**File > Export > GeoPackage (.gpkg)** or **GeoJSON (.geojson)** writes the
selected meshes, as evaluated (modifiers applied), into the project CRS with
the offset added back:

- faces become polygons, loose edges lines, loose vertices points;
- Z is the absolute height (Blender Z plus offset A);
- attributes are written as columns.

Options: **Format**, the **Layer** (table) name, and **Replace Table** for a
GeoPackage table that already exists.

A GeoJSON in a projected CRS is outside RFC 7946, which allows only WGS84.
GeoTools writes the legacy `crs` member, which QGIS and GDAL read.

The analysis tools' lines and points (contours, sections, sightlines, flow
paths, depressions) export the same way.

## GeoTIFF

1. **File > Import > GeoTIFF (.tif, .tiff)**.
2. Choose **As**:
   - **Automatic**: a DEM for one band of floats or 16 or 32-bit integers,
     otherwise an orthophoto;
   - **DEM**: a height mesh from band 1, one vertex per pixel centre;
   - **Orthophoto**: the image on a plane over its footprint.
3. For a DEM, set **Decimation**: use every *n*th pixel. The dialog shows the
   resulting spacing and vertex count.
4. Import.

A DEM drops cells with no data, and gets UVs over the raster's extent, so an
orthophoto of the same extent maps straight onto it. An orthophoto's image
stays linked to its file. Both stay linked like vectors: **Refresh** re-reads
a DEM, and reloads and moves an orthophoto.
If you painted or cleaned the orthophoto, Publish saves the edits to a copy
in `textures/` and the image uses it. **Refresh** then moves the plane but
keeps the copy, and warns; if the file's width or height in pixels changed,
it refuses instead, so the copy is never stretched. Point the Image Texture
back at the `.tif` to reload the file. See [Texture cleanup](textures.md).

GeoTools reads the georeference, the GeoKeys and the nodata value itself.
Pixels go through Blender's image loader where it is exact (float32, int16,
uint16, uint8), and through GeoTools' own reader for other sample types in
uncompressed and deflate files. A 4096 × 4096 DEM imports in about 3.5 seconds.

**Limits.** Rasters are never reprojected; reproject them in QGIS first.
Rotated or sheared rasters are refused (warp them north-up in QGIS). LZW files
with sample types Blender cannot read exactly are refused; re-save them as
deflate or float32. A GeoTIFF is not published as terrain; publish the DEM mesh
as part of the source.

## Publish vectors

A build profile with **Format** set to **Vectors** publishes the meshes of a
collection as vector 3D Tiles: faces as polygons, loose edges as lines and
loose vertices as points, with their attributes.

1. Put the vector objects (imported, linked, draped or not) in a collection
   of their own.
2. In the **Publish** panel, add a build profile, set **Format** to
   **Vectors**, and set **Vectors** to that collection.
3. Set the **Output**, such as `//contexts.3tz`, and press **Publish**.

**Build Settings** for vectors:

- **Apply Modifiers** (on): publish the draped, evaluated geometry;
- **Incremental** (on): a republish passes the previous archive to rusty-tiles,
  which re-encodes only the features that changed;
- **Skip Invalid**: leave out features rusty-tiles cannot convert, rather than
  fail;
- **Threads**.

Vectors need a CRS or a Local project. An Anchor project is refused.
