# Working alongside BlenderGIS

GeoTools works on its own. You do not need
[BlenderGIS](https://github.com/domlysz/BlenderGIS) or any other Blender GIS
add-on, and GeoTools never loads them. But if you use them, GeoTools shares
the scene's georeference with them, so data from either tool lands on the same
coordinates.

## The shared keys

BlenderGIS keeps a scene's georeference in five custom properties on the
scene, the **GeoScene keys**. GeoTools reads and writes the same keys:

| Key | What it holds |
|---|---|
| `SRID` | The CRS, such as `EPSG:7856`, or a PROJ string. |
| `crs x`, `crs y` | The scene origin in CRS units: GeoTools' **Offset E** and **N**. |
| `latitude`, `longitude` | The same origin in WGS84 degrees. GeoTools rewrites them whenever it changes the CRS or the origin. |

The frame is the one a GeoTools CRS project uses with A = 0: Blender X is
E − `crs x`, Blender Y is N − `crs y`, +Y is grid north, and Blender Z is the
absolute height. You can see the keys in **Properties > Scene > Custom
Properties**.

GeoTools never touches BlenderGIS's own `scale` and `zoom` keys, and it never
adds keys of its own (there is no `crs z`).

## What is shared

In a CRS project the keys are the project's CRS and origin. GeoTools keeps no
second copy. So:

- **A scene georeferenced by BlenderGIS** opens in GeoTools as it is. Make it a
  CRS project and its CRS and Offset E and N are BlenderGIS's.
- **A scene set up in GeoTools** is georeferenced for BlenderGIS and for any
  other tool that reads these keys.
- **Data imported with either tool** lands on the same coordinates.
- **Tools that place things by the keys** see the same origin. For example,
  blender_ogc3dtiles can place a streamed 3D Tiles tileset from the same
  keys, so a published site and streamed context line up in one scene.

## What stays GeoTools-only

Some of a GeoTools project has no place in the GeoScene keys:

- **Local and Anchor projects.** There is no GeoScene for local metres or for
  an anchor with a heading. GeoTools writes no keys for them. If the scene has
  keys anyway, GeoTools leaves them alone, and the Project panel and Run Checks
  warn that the project ignores them.
- **Offset A and the height offset.** GeoScene's Z is absolute height, with no
  vertical offset and no vertical datum. Keep A at 0 in scenes you share. When
  A is not 0, the Project panel and Run Checks warn (`georef-height-shift`)
  that other tools will see every height shifted by A.
- **The vertical datum and compound CRSs.** GeoTools writes `SRID` only as a
  plain horizontal `EPSG:nnnn` code or a PROJ string, because BlenderGIS cannot
  read anything else. A compound code (such as `EPSG:7856+5711`), WKT or
  another authority's code stays in GeoTools' own settings, and the scene then
  has no keys. The Project panel says that other tools see the scene as not
  georeferenced. Choose a plain EPSG code again to write the keys back.

## Moving the origin

BlenderGIS and GeoTools treat a change of origin differently:

- In BlenderGIS, changing the origin moves the scene's objects the opposite
  way, so they stay on the same spot on Earth.
- In GeoTools, changing **Offset E** or **N**, or loading an offset file, keeps
  the objects where they are in Blender. The scene is read at the new origin,
  so the objects move on Earth.

GeoTools changes `crs x` and `crs y` only when you edit the origin yourself.
An import, a CRS change or a change of placement never moves the origin. When
an import offers the data's centre as the project origin and the scene already
has a complete BlenderGIS georeference, the project takes that georeference
instead, so objects another tool placed stay where they are.

Changing the CRS also differs. BlenderGIS reprojects the origin to keep it on
the same spot. GeoTools keeps the E and N numbers and reads them in the new
CRS, because the CRS names what the scene's coordinates are in.

## Common situations

**The scene has BlenderGIS keys, but the project is Local or Anchor.** The
Project panel warns and offers **Use the Scene's BlenderGIS Georeference**. It
makes the project a CRS project that uses the keys, with A = 0. Objects keep
their Blender positions.

**A file from an earlier GeoTools.** Its CRS and offset move into the keys when
you open it. A one-time notice in the Project panel names what was taken from
where; dismiss it with the X. If the scene already had keys that differ, the
keys win and the notice says what was dropped.

**Latitude and longitude disappeared.** GeoTools computes them with the bundled
pyproj. Without pyproj, or for a CRS pyproj cannot read, it deletes them rather
than leave stale values beside a changed origin. BlenderGIS does the same.

**A scene with only `latitude` and `longitude`.** GeoTools needs `crs x` and
`crs y`. BlenderGIS's own tools can compute them.

Design details:
[design/georeference-interop.md](../design/georeference-interop.md).
