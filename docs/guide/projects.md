# Georeference and projects

Each Blender scene is one GeoTools project. The project says where the scene's
coordinates are on Earth. Publishing, the geo formats and the analysis tools
all read it. You set it in the **Project** panel, the first panel of the
GeoTools tab.

## Pick a placement

The top row of the Project panel has three placements:

| Placement | Use it for | The tileset |
|---|---|---|
| **Local** | Objects with no real-world position, such as a museum object. | Is not placed on Earth. |
| **Anchor** | A model in local metres that you want to put at one known point. | Is placed rigidly at that point. |
| **CRS** | Survey, photogrammetry and GIS data with projected coordinates. | Is placed by its CRS and offset. |

The panel header shows a tick when the project is complete, and an error when
it cannot be used, for example a CRS project with no CRS or a CRS rusty-tiles
refuses.

## A CRS project

A CRS project reads each Blender position as coordinates in a projected CRS,
minus an offset:

- E = Blender X + **Offset E**;
- N = Blender Y + **Offset N**;
- height = Blender Z + **A**.

+Y is grid north. There is no rotation. The offset keeps Blender's coordinates
small: Blender stores positions in single precision, which is only good to
about half a metre at a northing of 6,961,000 m.

### Choose the CRS

Click the **CRS** field and type an EPSG code or words of its name, such as
`7856` or `MGA 56`. Pick an entry such as `EPSG:7856 — GDA2020 / MGA zone 56`.

- The list holds every EPSG code the bundled rusty-tiles can publish, more
  than 3,000 of them. Names and areas of use come from the bundled pyproj; without pyproj,
  the list has codes only.
- The globe button next to the field is **Near This Site**. When the project
  already has a location (a verified placement, a CRS and offset, or an
  anchor), it lists only the codes whose area of use holds the site. Turn it
  off to list every code, the ones near the site first.
- You can also type WKT or a PROJ definition.

Under the field, GeoTools shows the CRS's name and rusty-tiles' verdict before
anything is built:

- a tick with the datum operation rusty-tiles will use and its stated
  accuracy, for example "GDA2020 to WGS 84 (2)", 3 m;
- or an error saying why rusty-tiles refuses the CRS.

rusty-tiles refuses CRSs whose datum operation needs a grid (British National
Grid, EPSG:27700; NAD27; the original NAD83), dynamic frames, coordinate
epochs, and compound and 3D CRSs (for example EPSG:7856+5711 or EPSG:4979).
Use the horizontal CRS and set the heights with A and the height offset
(below).

The **CRS Details** sub-panel shows the CRS's area of use, how rusty-tiles
checked the datum operation, and the definition it uses.

### Set the offset

The offset is the shift that was taken off the coordinates. Photogrammetry
tools such as Metashape ask for it when you export with a local shift.

- Type it in **Offset E**, **N** and **A**. The fields keep every digit you
  type (they are stored as text, at full double precision).
- Or press **Load Offset File** and pick an offset file.
- Or put the offset file next to the mesh before you import it. **Import
  Source Mesh...** in the Source panel looks for `<name>_offset.txt`,
  `<name>.offset.txt` or `offset.txt` beside the mesh, in that order, and
  reads the first it finds. Reading an offset file switches the project to
  CRS, but does not set the CRS: the file does not say which CRS it is in.

Import Source Mesh uses the offset file only while the project has no origin
yet (a Local project with no BlenderGIS keys and E, N, A all 0, or a CRS
project with E, N, A all 0). A file with exactly the project's E, N and A
changes nothing, so you can import a chunk again, or several chunks that
share one offset file. When the project already has another origin (other E,
N or A, a BlenderGIS georeference, an Anchor project), the mesh is imported
unshifted, the origin stays as it is, and the report and the Source panel show
the file's E/N/A next to the project's. The Source panel then offers:

- **Use This Offset File as the Project Origin**: a CRS project at the file's
  E, N and A. It asks first, because the objects already in the source
  collection keep their Blender positions and so move on Earth.
- **Place the Imported Objects at the Project Origin** (CRS projects): moves
  the imported objects by the difference between the two offsets, computed in
  double precision. Blender keeps object locations in single precision, so it
  warns when the objects end up more than about 16 km from the scene origin,
  where that rounds them by more than a millimetre.
- The **X** dismisses the offer and leaves everything as it is.

**Load Offset File** is your explicit choice, and always sets the origin.

An offset file is rusty-tiles' `--source-offset-file` format: exactly three
lines, at most 4 KiB.

```
E: 500000.000
N: 5400000.000
A: 100.000
```

Changing Offset E or N moves the scene on Earth. Objects keep their Blender
positions; they are now read at the new origin. Changing the CRS keeps the E
and N numbers and reads them in the new CRS.

### Heights: A and the height offset

- **A** is added to Blender Z to give heights in the source's own height
  system, for example metres above a local datum.
- **Height Offset** is added to that to give the ellipsoidal height, which
  3D Tiles uses. If your heights are above the geoid (for example AHD or
  EGM96), set it to the geoid separation at the site. If your heights are
  already ellipsoidal, leave it at 0.

Keep A at 0 if you share the scene with BlenderGIS or another tool that reads
the scene's GeoScene keys: they read Blender Z as absolute height. When A is
not 0, the Project panel and Run Checks warn. See
[Working alongside BlenderGIS](blendergis.md).

## An Anchor project

An Anchor project places local metres at one point on Earth:

- **Longitude**, **Latitude**: the anchor in WGS84 degrees;
- **Height**: the anchor's ellipsoidal height in metres;
- **Heading**: a turn about the local up axis in degrees, counter-clockwise
  seen from above. 90 turns the scene's +X onto north;
- **Offset E**, **N**, **U**: a further shift in metres.

## A Local project

A Local project has no fields. The tileset is in local metres and is not
placed on Earth. Viewers show it at their own origin.

## Verify Placement

**Verify Placement**, at the bottom of the Project panel, asks rusty-tiles to
place one triangle that spans the source bounds. It shows the longitude,
latitude and height where the source lands, or why the placement fails. Use
it after any change to the project, before a long publish. When the project
changes afterwards, the panel says "Placement changed; verify again".

It needs rusty-tiles (see the About panel).

## Data that brings its own CRS

When you import a GeoJSON, GeoPackage or GeoTIFF, GeoTools can take the
project's CRS and origin from the data:

- **A CRS project with no CRS yet:** the import offers the data's CRS. If the
  offset is still 0, it also offers the centre of the data as the origin.
- **A Local or Anchor project:** small coordinates (within about 100 km of the
  origin) are taken as local metres, with a warning. Projected data with
  larger coordinates is refused, with an offer to switch.

The offer is a button, **Use This Data's Centre as the Project Origin**, in a
popup and in the **Linked Data** panel. It makes the project a CRS project in
the data's CRS (its `EPSG:nnnn` code), with the centre of the data's extent in
whole metres as Offset E and N, and A 0. Then it runs the import again. If the
source collection already has objects, it asks first, because their place on
Earth changes.

If the scene already has a BlenderGIS georeference, the offer uses it and does
not move the scene. See [Working alongside BlenderGIS](blendergis.md).

## Limits

- Keep every Blender coordinate within ±1,000,000. rusty-tiles refuses larger
  source coordinates, and Run Checks reports `coordinate-range`. Move the
  geometry towards the origin and put the shift in the offset.
- Run Checks warns (`float-precision`) when single precision is coarser than
  1 mm at the largest coordinate.
- The scene's unit scale must be 1 (`unit-scale`), so one Blender unit is one
  source unit.
- A geographic CRS (such as EPSG:4326) reads Blender units as degrees, which is
  rarely what you want. Run Checks warns (`crs-geographic`).
- Without rusty-tiles there is no EPSG list and no CRS check.

## Files from earlier GeoTools versions

A `.blend` saved by an earlier GeoTools keeps its CRS and offset in GeoTools'
own settings. When you open it, GeoTools moves them into the scene's GeoScene
keys and shows a one-time notice that names what it took from where. Offset A
and the height offset stay GeoTools settings. Placement does not change. Old
GeoTools CRS presets (PROJ strings for MGA and NZTM) are replaced by their EPSG
codes, only when rusty-tiles resolves the code to the same definition; CRS
Details then says so.

Design details: [design/georeference-interop.md](../design/georeference-interop.md).
