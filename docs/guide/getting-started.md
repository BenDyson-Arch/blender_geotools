# Getting started

This page takes you from an empty Blender to a published, georeferenced 3D
Tiles archive. It uses a small synthetic site that ships with GeoTools, so you
can follow it without data of your own. It takes about ten minutes.

You will:

1. install the extension;
2. check the bundled libraries;
3. get the demo data;
4. place the project on Earth;
5. bring in a mesh;
6. run the checks;
7. publish `site.3tz`;
8. view the result.

You need the Blender basics: the 3D Viewport, the sidebar, and saving a file.

## 1. Install the extension

You need:

- Blender 5.2. Only the latest Blender release is supported.
- Linux x64, Windows x64 or macOS arm64 (Apple silicon). On macOS, macOS 15 or
  later.

Then:

1. Download the zip for your platform from the
   [GeoTools releases](https://github.com/BenDyson-Arch/blender_geotools/releases),
   for example `geotools-0.3.0-linux_x64.zip`. The other platforms end in
   `windows_x64` and `macos_arm64`.
2. In Blender, open **Edit > Preferences > Get Extensions** and choose
   **Install from Disk**. Or drag the zip into the Blender window.
3. In the 3D Viewport, press **N** to open the sidebar and select the
   **GeoTools** tab.

To build the zip from source instead, see
[CONTRIBUTING.md](../../CONTRIBUTING.md#building).

## 2. Check the bundled libraries

The zip carries everything GeoTools needs. There is nothing to install with
pip and nothing is downloaded at install.

| Library | What GeoTools uses it for |
|---|---|
| rusty-tiles | Tiling and validating 3D Tiles, checking a CRS, Verify Placement, and opening published tiles again. |
| pyproj (with PROJ, no GDAL) | Reprojecting imported vector data, latitude and longitude of a CRS project, and the geodesy of the analysis tools. |
| certifi | pyproj's only dependency. |

Open the **About** panel at the bottom of the GeoTools tab. It is closed by
default. Its header shows how the libraries loaded:

- a tick: rusty-tiles and pyproj both loaded;
- a warning: pyproj did not load. Imports cannot reproject, and the analysis
  tools cannot relate a CRS project to the Earth. Everything else works;
- an error: rusty-tiles did not load. Nothing can be published.

Inside, About shows the GeoTools version and one line per library, such as
`rusty-tiles 0.4.1` and `pyproj 3.8.0 (PROJ 9.8.1)`. A library that failed
says `not available` and why. **Check Dependencies** loads them again. The
GeoTools preferences (**Edit > Preferences > Add-ons**, GeoTools) show the
same, plus the PROJ data folder.

GeoTools needs rusty-tiles 0.4.1 or later. An older rusty-tiles is reported as
not available, with the functions it lacks.

Blender shares Python packages between extensions. If another extension
bundles a newer pyproj, GeoTools runs with that one. About shows the version
that actually loaded.

## 3. Get the demo data

The demo is a synthetic site. Download `geotools-demo-data-<version>.zip` (for
example `geotools-demo-data-0.3.0.zip`) from the same
[release](https://github.com/BenDyson-Arch/blender_geotools/releases) as the
extension and unzip it anywhere. It holds a `drape-contexts` folder, the
same as [`examples/drape-contexts/`](../../examples/drape-contexts/) in the
repository. You need three files from it:

| File | What it is |
|---|---|
| `terrain.ply` | A 60 m square terrain in local metres: 14,400 faces, no texture. |
| `terrain_offset.txt` | The shift that was taken off the terrain's coordinates: E 500000, N 5400000, A 100. |
| `contexts.geojson` | Nine invented excavation contexts, used in the [geo formats](geo-formats.md) page. |

The site is invented. It sits in open country at round coordinates in
WGS 84 / UTM zone 31N (EPSG:32631). Every number in it is synthetic.

## 4. Place the project on Earth

The **Project** panel says where the scene's coordinates are on Earth. Its
top row picks the placement:

- **Local**: local metres, not placed on Earth;
- **Anchor**: local metres placed at a WGS84 longitude, latitude and height,
  turned by a heading;
- **CRS**: coordinates in a projected CRS, minus an offset. Photogrammetry and
  survey data usually need this one.

For the demo:

1. Choose **CRS**.
2. Click the **CRS** field and type `32631`, or words of the name such as
   `UTM 31N`.
3. Pick `EPSG:32631 — WGS 84 / UTM zone 31N`.

Under the field, GeoTools shows the CRS's name and rusty-tiles' verdict: a
tick and the datum operation it will use (here `WGS 84, no datum change`), or
an error that says why rusty-tiles refuses the CRS.

Leave the offsets at 0 for now. The next step reads them from the offset file.
[Georeference and projects](projects.md) explains every field.

## 5. Bring in the mesh

1. In the **Source** panel, press **Import Source Mesh...**.
2. Pick `terrain.ply`. **Up Axis** is **Z Up**, the usual for photogrammetry
   OBJ and PLY exports. glTF files are always Y-up and converted for you.
3. Press **Import Source Mesh**.

GeoTools runs Blender's own importer and puts the result in a new collection,
**GeoTools Source**. This is the source collection: what GeoTools publishes.
The panel's **Summary** shows 1 object, 14,400 faces, 14,641 vertices and a
size of about 60 × 60 m.

GeoTools also looks for an offset file beside the mesh: `terrain_offset.txt`,
`terrain.offset.txt` or `offset.txt`, in that order. It finds
`terrain_offset.txt`, so the Project panel now shows **Offset E** 500000,
**N** 5400000 and **A** 100, and "From terrain_offset.txt".

The Project panel also shows a warning: offset A is 100 m, so BlenderGIS and
other GeoScene tools would see every height 100 m lower than GeoTools
publishes it. That only matters if you share the scene with those tools. See
[Working alongside BlenderGIS](blendergis.md).

To use objects you already have, set **Collection** in the Source panel to the
collection that holds them, instead of importing.

### Verify the placement

Press **Verify Placement** at the bottom of the Project panel. rusty-tiles
places one triangle that spans the source and reports where it lands:

- Longitude about 3.0000000°;
- Latitude about 48.7530130°;
- Height about 102.217 m.

That is the middle of the terrain, a little above 100 m. If you change the
project later, the panel says "Placement changed; verify again".

## 6. Run the checks

Save the `.blend` now, in a new folder. The publish output goes next to it.

In the **Checks** panel, press **Run Checks**. GeoTools checks the source
against rusty-tiles' input rules and the placement. For the demo it finds:

- 1 warning, `georef-height-shift`: the offset A warning from above;
- 1 note, `crs-operation`: "Source CRS: EPSG:32631: WGS 84, no datum change".

There are no errors. An error blocks publishing; warnings and notes do not.
Select an issue in the list to read it in full. Issues with a fix, such as an
image that is too large, show their fix buttons there. See
[Checks and publishing](publish.md#checks).

## 7. Publish

1. In the **Publish** panel, press **Add Build Profile**. A build profile says
   where and how to publish.
2. Set **Output** to `//site.3tz`. `//` means the folder of the `.blend`.
3. Leave **Format** on **.3tz**, **Variant** empty and **Validate** on.
4. Press **Publish site.3tz**.

The panel shows progress (writing the source GLB, tiling, validating) and a
**Cancel** button. The demo takes a few seconds. When it is done, **Last
Build** shows:

- the file name, when it was built and how long it took;
- **Triangles** 28,800 and **Tiles** 5;
- **Validation** Passed;
- **Folder** and **Record** buttons, which open the output folder and the
  build record.

Next to the `.blend` you now have:

```
site.3tz                the 3D Tiles archive
site.geotools/
  build.json            the build record: placement, settings, layers, hashes
  source.glb            exactly what rusty-tiles tiled
  facemap.npz           output triangles back to Blender faces
  report.json           rusty-tiles' report
  validation.json       rusty-tiles' validation report
  diagnostics.json      rusty-tiles' runtime diagnostics
```

To publish again over the same file, turn on **Overwrite**. Without it the
publish stops with "already exists". The old archive is kept if a new build
fails or is cancelled.

## 8. View the result

`site.3tz` is a 3D Tiles archive: one zip file that holds `tileset.json` and
its tiles. Viewers that stream 3D Tiles from a web server, such as CesiumJS,
cannot open a `.3tz` directly. Extract it and serve the folder:

```sh
python3 -m zipfile -e site.3tz site/
python3 -m http.server 8000
```

Then load `http://localhost:8000/site/tileset.json` in your viewer. In
CesiumJS:

```js
const tileset = await Cesium.Cesium3DTileset.fromUrl(
  "http://localhost:8000/site/tileset.json"
);
viewer.scene.primitives.add(tileset);
viewer.zoomTo(tileset);
```

The tiles use meshopt geometry compression (`EXT_meshopt_compression`), and
the lossy quality profiles use KTX2 textures. Use a viewer that supports both,
such as a current CesiumJS.

To preview from Blender, set the optional **rusty-tiles CLI** and a Cesium
1.146 **Cesium Runtime** folder in the GeoTools preferences. Neither is bundled.
**Last Build** then offers **Preview in Browser**, which extracts the archive
and serves it locally.

To look at the tiles inside Blender, see [Inspecting output](inspect-output.md).

## Next steps

- Add an analysis layer and publish it as a variant, such as `site.slope.3tz`:
  [Layers and bakes](layers.md) and [Analysis](analysis.md).
- Import and drape `contexts.geojson`: [Geo formats](geo-formats.md) and the
  [draping recipe](../recipes/drape-contexts.md).
- Export one GLB with the objects and layers as metadata:
  [GLB with metadata](glb-metadata.md).
