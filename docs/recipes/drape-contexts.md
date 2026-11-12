# Recipe: drape excavation contexts on a site mesh

This recipe takes the context plan of an excavation from a GIS and turns it into
3D:

- each context draped on the site mesh at its top level;
- a closed volume for each context, between its top and bottom levels;
- the draped contexts exported back to a GeoPackage with real heights;
- everything re-draped by Blender after you edit the plan in QGIS.

You need the Blender basics: selecting objects, the Properties editor, adding a
modifier, and adding and linking nodes in the Geometry Nodes editor.

All of this uses stock Blender nodes. GeoTools reads and writes the geo formats
and keeps the GeoJSON linked to the object. It ships no draping node groups,
because Blender already does draping well (the Scope decision in
[ROADMAP.md](../ROADMAP.md)). Every node below is in Blender's own Add menu.

## The example file

[`examples/drape-contexts/`](../../examples/drape-contexts/) holds a finished
version of this recipe. The site is invented. It sits in open country at round
coordinates in WGS 84 / UTM zone 31N (EPSG:32631), and every number in it is
synthetic.

| File | What it is |
|---|---|
| `terrain.ply` and `terrain_offset.txt` | A 60 m square synthetic terrain in local metres, with its E/N/A offset file, as a photogrammetry export with a coordinate shift arrives. |
| `contexts.geojson` | Nine contexts as 2D polygons in EPSG:32631: pits, postholes, a ditch, a spread and a ring ditch with a hole. Each has `context`, `type`, `phase`, `depth_top` and `depth_bottom`. |
| `drape-contexts.blend` | The finished scene: the project and the terrain set up, `contexts` linked to the GeoJSON and draped, `contexts.volumes`, and two build profiles. |

Open `drape-contexts.blend`. Everything below is already set up in it. The
GeoTools panels are in the sidebar (**N** key, GeoTools tab).

To make the files again (for example after changing the script):

```sh
blender --background --factory-startup --python scripts/make_example_drape.py
```

The script needs only Blender, and writes into `examples/drape-contexts/` (or
`--out DIR`, given after a `--` separator).

## Before you start

1. **Set the project CRS.** In the Project panel, pick the CRS your plan is
   drawn in and set the offset. The example uses EPSG:32631, with E 500000,
   N 5400000 and A 100.
2. **Bring in the site mesh** with **Import Source Mesh** in the Source panel. GeoTools
   reads the offset file beside it. In the example the mesh is called
   `terrain`.
3. **Check the context attributes.** The recipe expects two numbers on each
   context:
   - `depth_top`: how far the top of the context is below the ground surface,
     in metres (positive is down);
   - `depth_bottom`: the same for its base.

   If your plan records levels (reduced levels, absolute heights) instead of
   depths, see [Levels instead of depths](#levels-instead-of-depths).

## 1. Import the contexts

**File > Import > GeoJSON (.geojson)**, or **Import: GeoJSON** in the Linked Data
panel. Pick `contexts.geojson`.

GeoTools makes one mesh object, `contexts`:

- one face per context, flat at height 0, since the file has no Z;
- a polygon with a hole (the ring ditch) is cut into triangles;
- every property becomes a **face attribute** with the same name, plus `fid`,
  the feature id.

To see the attributes, open a Spreadsheet editor, choose the **Face** domain
and select `contexts`.

The object stays **linked** to the file. Its row in the Linked Data panel shows
whether the file has changed since the import.

If the file is in another CRS, GeoTools reprojects it into the project CRS and
says how. Rasters are not reprojected; reproject those in QGIS first.

## 2. Drape the contexts on the site mesh

Select `contexts`. In the Properties editor, open Modifiers and add
**Geometry Nodes** (Add Modifier > Generate > Geometry Nodes). Click **New**.

Build this tree. Node names are those in the Add menu, and `.socket` names the
socket a link goes to or comes from.

```
Group Input.Geometry ──► Subdivide Mesh.Mesh            (Level ◄── Group Input.Subdivisions = 3)
Group Input.Terrain  ──► Object Info.Object             (Relative)

Position ──► Separate XYZ ──X──► Combine XYZ.X ┐
                          └─Y──► Combine XYZ.Y ├─► Raycast.Source Position
                                Combine XYZ.Z = 10000 ┘
Object Info.Geometry ──► Raycast.Target Geometry
                         Raycast.Ray Direction = (0, 0, -1)
                         Raycast.Ray Length    = 20000

Subdivide Mesh.Mesh ──► Set Position ("Onto the Terrain").Geometry
Raycast.Is Hit      ──► Set Position.Selection
Raycast.Hit Position ─► Set Position.Position
```

What each part does:

- **Subdivide Mesh** adds vertices inside each context. A face only bends at
  its vertices, so without this a large context would cut straight across a
  hollow. Level 3 is plenty for contexts of a few metres; raise it for long
  ditches on uneven ground.
- **Object Info** brings in the site mesh. Set its transform to **Relative**, so the
  terrain is seen in the contexts object's own space.
- **Raycast** shoots a ray straight down from 10 km above each vertex, and
  reports where it hits the terrain.
- **Set Position** moves each vertex to that hit. **Is Hit** as the
  Selection leaves any vertex that missed the terrain where it was, so the
  rest still drape.

In the modifier panel, set **Terrain** to `terrain`. The contexts now lie on
the ground.

Raycast hits the first surface below, so it also works on a mesh with walls and
overhangs. On a plain height field, **Sample Nearest Surface** works as well.

## 3. Lower them by their depth

Add a second **Set Position** after the first, and feed its **Offset**:

```
Group Input.Depth Attribute ("depth_top") ──► Named Attribute.Name   (type Float)
Named Attribute.Attribute ──► Math (Multiply, × -1) ──► Combine XYZ.Z
Combine XYZ.Vector ──► Set Position ("Down by the Depth").Offset

Set Position ("Onto the Terrain").Geometry ──► Set Position ("Down by the Depth").Geometry
Set Position ("Down by the Depth").Geometry ──► Group Output.Geometry
```

- **Named Attribute** reads `depth_top` from each face. Set Position works on
  vertices, so Blender carries each face's value to its vertices.
- **Multiply by −1** turns "metres below the surface" into a move down.

Make the attribute name a group input (drag the Name socket onto the Group
Input node). The same group then drapes at any depth you name in the modifier
panel, for example `depth_bottom` to see the base of each context.

```
           ┌──────────────┐   ┌────────┐   ┌──────────────┐   ┌──────────────┐
Geometry ─►│Subdivide Mesh├──►│        │   │ Set Position │   │ Set Position │
           └──────────────┘   │        ├──►│ onto terrain ├──►│ down by depth├─► Geometry
Terrain ─► Object Info ─────► │Raycast │   └──────▲───────┘   └──────▲───────┘
Position ─► (x, y, 10000) ──► │        ├─ Is Hit, Hit Position       │
                              └────────┘          Named Attribute ─► ×(-1) ─► (0, 0, z)
```

In the example, this group is called **Drape on Terrain**, with the inputs
Terrain, Depth Attribute and Subdivisions.

## 4. Extrude volumes between the top and bottom levels

Keep the draped tops as they are, so they can still be exported as polygons. Make
the volumes on a second object:

1. Add an empty mesh object: Add > Mesh > Plane, then delete all its vertices
   in Edit Mode. Name it `contexts.volumes`.
2. Give it a Geometry Nodes modifier with this tree:

```
Group Input.Tops (object) ──► Object Info.Object      (Relative)

Named Attribute "depth_bottom" ─┐
Named Attribute "depth_top"    ─┴─► Math (Subtract: bottom − top) ──► Extrude Mesh.Offset Scale
Combine XYZ (0, 0, -1) ─────────────────────────────────────────────► Extrude Mesh.Offset

Object Info.Geometry ──► Extrude Mesh.Mesh            (mode Faces, Individual off)
Extrude Mesh.Mesh ──► Flip Faces ──► Join Geometry ◄── Object Info.Geometry
Join Geometry ──► Merge by Distance (0.0005 m) ──► Group Output.Geometry
```

3. Set **Tops** to `contexts`.

- **Object Info** reads the *draped* contexts from the first object, already
  lowered to their tops. When `contexts` changes, the volumes follow.
- **Extrude Mesh** pushes each top straight down by its thickness,
  `depth_bottom − depth_top`, so the base keeps the shape of the ground.
- **Link a vector into Offset.** Offset is a field that defaults to the face
  normal, so a value typed into the socket is ignored. A Combine XYZ of
  (0, 0, −1) gives "straight down".
- **Flip Faces** turns the extruded walls and bases outwards. Extruding against
  the face normals leaves them facing in.
- **Join Geometry** adds the tops back as lids, and **Merge by Distance** welds
  the lids to the walls. Each volume is then closed, with outward normals, so
  its volume can be measured.

Merge by Distance welds any vertices closer than its distance. Where two
contexts share a boundary, their volumes are welded together along it. That
still renders correctly. To measure contexts one by one, use a smaller
distance, or separate them first.

A closed volume has no outline, so it does not export as a polygon. Export
the tops (step 5), and publish the volumes as a mesh.

## 5. Export back to a GeoPackage with Z

Select `contexts` and choose **File > Export > GeoPackage (.gpkg)** (or
**Export** in the Linked Data panel).

The export reads the evaluated mesh, with the modifiers applied:

- each context is one polygon, with its draped outline;
- Z is the real height: the local height plus the project's A offset;
- E and N are in the project CRS, with the offset added back at full (double)
  precision;
- every attribute is kept, and `fid` matches the source.

Subdivided faces of one context become one polygon again, so the result is one
row per context. Its boundary has the extra vertices from Subdivide Mesh, each
on the ground.

In QGIS, the layer shows as `PolygonZ`. To check the heights, use the Vertex
Editor, or the Field Calculator with `z(point_n($geometry, 1))`.

GeoJSON export works too. In a projected CRS it carries the legacy `crs`
member, which QGIS and GDAL read; that is outside RFC 7946.

## 6. Refresh after an edit in QGIS

QGIS stays the place to draw and edit the plan. Edit the original
`contexts.geojson` (or GeoPackage): move a vertex, add a context, or change a
`depth_top`. Then save the edits.

In Blender:

1. The `contexts` row in the Linked Data panel turns to *changed on disk*.
   GeoTools checks when the `.blend` is opened, and when you click **Check
   Linked Files** (the refresh icon beside the list).
2. Click **Refresh**. GeoTools re-reads the file and replaces only the mesh
   data. The modifiers, materials and transform stay.
3. The Drape modifier runs again on the new data, and `contexts.volumes`
   follows, since it reads `contexts` through Object Info.

Refresh reports how many contexts were added, removed and changed. With
**Auto** on, GeoTools checks the file every few seconds and refreshes by
itself (not while you are in Edit Mode).

Geometry is never written back from Blender to the source file; QGIS owns the
2D plan. Attribute columns can be written back with **Write Back**, for
example a context volume computed in Blender: a GeoPackage is updated by
feature id, and a GeoJSON file is rewritten.

## 7. Publish as 3D Tiles (optional)

- **The draped contexts as vector tiles.** Add a build profile in the Publish
  panel. Set Format to **Vectors**, choose the collection holding `contexts`,
  and set the output, for example `//contexts.3tz`. Then click **Publish**.
  - GeoTools exports the evaluated contexts to a GeoPackage, the same way as
    step 5, and tiles it with rusty-tiles.
  - The attributes become feature properties that a viewer such as CesiumJS can
    pick and style.
  - The next publish is **incremental**: only what changed is re-encoded.
  - Polygons, lines and points publish. Closed volumes do not.
- **The site mesh and the volumes** publish with an ordinary `.3tz` profile.
  Put `contexts.volumes` in the source collection to include it.

The example file has both profiles: **Site** and **Contexts**.

## Levels instead of depths

If each context records reduced levels (RL, metres above the height datum)
instead of depths, skip the Raycast. Set Z straight from the level:

```
Position ──► Separate XYZ ──X──► Combine XYZ.X ┐
                          └─Y──► Combine XYZ.Y ├─► Set Position.Position
Named Attribute "level_top" ─► Math (Subtract, A) ─► Combine XYZ.Z ┘
```

`A` is the project's height offset (100 in the example), because Blender's Z
is the height minus A. For the volumes, use `level_top − level_bottom` as the
thickness.

Keep depths or levels as ordinary numbers in the file. Blender stores
attributes in single precision, about 0.1 mm at heights of a few hundred
metres. Coordinates are different: GeoTools always keeps E and N in double
precision.

## Troubleshooting

- **Some vertices did not move.** Their rays missed the terrain. The context
  is outside the site mesh, or the terrain is more than 10 km above or below
  the contexts. Raise the ray start and length.
- **Contexts float or sink as a whole.** Set Object Info to **Relative**, and
  do not move either object after importing. Imports are already in the
  project frame.
- **A ditch cuts through a bank.** Raise Subdivisions.
- **The volumes go up, not down.** The Offset of Extrude Mesh is not linked.
  Link a Combine XYZ of (0, 0, −1).
- **The volumes look dark or inside out.** Flip Faces is missing, or comes
  after Join Geometry.
- **The export has fewer polygons than contexts.** A context whose faces form
  a closed shape has no outline to export. Export the draped tops, not the
  volumes.
