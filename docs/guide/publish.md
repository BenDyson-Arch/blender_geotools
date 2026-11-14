# Checks and publishing

Publishing turns the source collection into a `.3tz`, an OGC 3D Tiles archive
written by rusty-tiles. A build profile says where and how. Before every
publish, GeoTools checks the scene against rusty-tiles' input rules, so a
problem shows up in seconds rather than after a long build.

## Checks

Press **Run Checks** in the **Checks** panel. Publishing runs the same checks,
so this step is optional; it lets you fix things early.

The panel header shows the worst result: a tick when there are no errors or
warnings, else a warning or an error icon. The panel lists every issue with its severity:

- **Error**: publishing would fail or be wrong. Publish refuses to start.
- **Warning**: the publish works, but something may not be what you meant.
- **Info**: a note, such as the datum operation rusty-tiles will use.

Select an issue to read its full message and its code. Some issues have fix
buttons under the message.

Run Checks also refreshes every layer's value range and status.

### Issues you may meet

| Code | What it means | What to do |
|---|---|---|
| `no-mesh-objects` | The source collection has no meshes. | Import a mesh or choose another collection in the Source panel. |
| `coordinate-range` | Blender coordinates beyond ±1,000,000. | Move the geometry towards the origin and put the shift in the offset. |
| `float-precision` | Coordinates so large that single precision is coarser than 1 mm. | As above. |
| `unit-scale` | The scene's unit scale is not 1. | Set it to 1 in Scene properties > Units. |
| `crs-refused` | rusty-tiles cannot publish this CRS. | Choose another CRS. See [Georeference and projects](projects.md). |
| `crs-operation` | The datum operation rusty-tiles will use, and its accuracy. | Nothing; an info note. |
| `georef-height-shift` | Offset A is not 0, so BlenderGIS sees heights shifted. | Nothing, unless you share the scene. See [BlenderGIS](blendergis.md). |
| `georef-origin-lat-lon-only` | The scene has a latitude and longitude but no projected origin (E, N). | **Origin from Latitude/Longitude** in the Project panel or Checks. See [BlenderGIS](blendergis.md). |
| `georef-origin-lat-lon-differ` | The scene's latitude and longitude lie some metres from E and N. | Nothing, if E and N are right; else **Origin from Latitude/Longitude**. |
| `georef-keys-ignored` | The scene has a BlenderGIS georeference, but the project is Local or Anchor. | **Use the Scene's BlenderGIS Georeference** in the Project panel, or ignore it. |
| `uv-overlap` | UV triangles share texels on an object with texture layers. | Fix the UVs, or bake anyway. See [Layers](layers.md#texture-layers-and-the-bake-sub-panel). |
| `image-missing` | An image's file is missing and it is not packed. | Find the file, or pack the image. |
| `image-unsaved` | An image has unsaved edits. An error when its file is a source file and the `.blend` was never saved. | Nothing: Publish saves it first, to a `textures/` copy for a source file. For the error, save the `.blend`. |
| `textures-changed` | Source images changed since the last publish. | Nothing; an info note. See [Texture cleanup](textures.md). |
| `texture-balance-preview` | A colour-balance preview node is still in a material. | Apply or clear the balance. See [Texture cleanup](textures.md). |

### Images rusty-tiles refuses

rusty-tiles refuses source images over 16,384 pixels a side, over 1 GiB
encoded, or with 16 bits per channel. GeoTools reports them
(`image-too-large`, `image-file-too-large`, `image-png-size`, `image-16-bit`)
with fix buttons: **Re-encode as JPEG**, **Downscale** or **Convert to 8-bit**.
**Clear Remedy** takes the fix off again.

Below 1 GiB, an image of any encoded size is accepted as long as the build's
memory budget can hold it while it is decoded. If it can't, the publish fails
with a `ResourceLimitError` that names the bytes it needs: raise the build
profile's **Memory GiB**, or choose **Downscale** for the image.

A fix changes only the copy that is published. Your image, its file and its
pixels stay as they are. The **Images** sub-panel of Checks lists every source
image with its size and format, and lets you set the fix and its **JPEG
Quality** or **Max Edge**. A JPEG fix is lossy, so a **Lossless** build warns
about it.

## Build profiles

A build profile is one way to publish the scene. You can keep several, for
example one for the full-quality archive and one for a variant.

1. In the **Publish** panel, press **Add Build Profile** (or **+** beside the
   list).
2. Set **Output**: where the archive goes. `//site.3tz` means `site.3tz` next
   to the `.blend`, so save the `.blend` first. Other relative paths are
   refused; use an absolute path or `//`.
3. Choose the **Format**:
   - **.3tz**: a 3D Tiles archive of the source collection, tiled by
     rusty-tiles;
   - **.glb**: one GLB with the objects and layers as metadata, not tiled. See
     [GLB with metadata](glb-metadata.md);
   - **Vectors**: the meshes of a collection as vector 3D Tiles. See
     [Geo formats](geo-formats.md#publish-vectors).
4. Optionally choose a **Variant** layer. See
   [Layers](layers.md#publish-a-layer-as-a-variant).
5. **Overwrite**: replace an archive that already exists. Off by default.
6. **Validate**: run rusty-tiles' validator on the result. On by default.

The **Build Settings** sub-panel holds:

- **Apply Modifiers** (on): publish each object's evaluated mesh, with its
  modifiers and Geometry Nodes applied, as you see it in the viewport. Off
  publishes the original mesh data;
- **Quality**: rusty-tiles' profile. **Fast**, **Balanced** (the default),
  **High** or **Lossless**. The lossy profiles compress textures as KTX2;
- **Threads**: rusty-tiles' worker threads. 0 is automatic;
- **Memory GiB**: rusty-tiles' memory budget. 0 is automatic: 60% of the
  memory available when you publish, minus 4 GiB for Blender. The panel shows
  the number it will use.

## Publish

Press the big button: **Publish site.3tz** (or **Export site.glb** for a
`.glb` profile). GeoTools:

1. runs the checks, and stops on any error;
2. saves edited source images, so a painted texture is never lost or
   published stale;
3. writes the source collection to one GLB (`source.glb`) and a face map;
4. runs rusty-tiles on a background thread, so Blender stays usable;
5. validates the archive.

The panel shows the stage, a progress bar and **Cancel**. A failed or cancelled
build leaves the previous archive untouched, and the panel says why it
stopped. A 10-million-triangle textured site publishes in about 20 minutes on
a 20-core desktop.

### What a publish writes

For an output `site.3tz`:

```
site.3tz                the archive
site.geotools/          the staging folder, GeoTools' own
  build.json            the build record
  source.glb            the exact rusty-tiles input
  facemap.npz           each output triangle back to its Blender object and face
  report.json           rusty-tiles' report
  validation.json       rusty-tiles' validation report
  diagnostics.json      rusty-tiles' runtime diagnostics
```

`build.json` records the inputs and settings: the placement (CRS, offsets and
datum operation), the layers, the build settings, the SHA-256 of `source.glb`,
of the saved `.blend` and of each source image, and the validation result. A
variant writes `site.<layer>.3tz` and its own `site.<layer>.geotools/`.

The staging folder is GeoTools' record, not a published format. Publish only
the `.3tz`.

### Last Build

The **Last Build** sub-panel shows what the selected profile last published:
the file, when, how long it took, the triangles and tiles, and whether
validation passed. **Folder** opens the output folder and **Record** opens
`build.json`.

**Extract for Web** (for a `.3tz`) writes the archive as a folder a web server
can serve, next to it: `site.web/` for `site.3tz`, `site.slope.web/` for a
variant. It holds `tileset.json` and the tiles, an `index.html` CesiumJS page
(see [Viewing a .3tz elsewhere](#viewing-a-3tz-elsewhere)) and
`.geotools-web.json`, which marks the folder as GeoTools' own and lists the
files it wrote. Pressing it again replaces those files. If you edited or
replaced `index.html`, your page is kept. Any other file you added or changed
in the folder stops the extract, with an error that names it, until you move it
out, so nothing of yours is deleted. GeoTools never replaces a folder of that
name that it did not write: move or rename yours, or the button reports an
error. The folder button beside it opens the folder. After a new publish the
panel says the folder is older than the archive until you extract again.

**Preview in Browser** serves the archive with `rusty-tiles preview` and opens
it in your browser. It needs two things that are not bundled, set in the
GeoTools preferences: the **rusty-tiles CLI** and a Cesium 1.146
**Cesium Runtime** folder (`Build/Cesium`). Until they are set, the panel says
"Preview needs setup" with a button to the preferences.

## Viewing a .3tz elsewhere

A `.3tz` is a zip that holds `tileset.json` and its tiles. Web viewers such as
CesiumJS read tilesets from a server, not from a `.3tz`. Press **Extract for
Web** and serve the folder it writes.
[Getting started](getting-started.md#8-view-the-result) shows how, with
Python's built-in server.

The `index.html` it writes is this page. It loads CesiumJS 1.146.0 from the
jsDelivr CDN (with subresource integrity hashes), shows Natural Earth II
imagery, which ships inside CesiumJS, and turns off the geocoder and the
imagery picker, so it never asks Cesium ion for anything and needs no token.
Copy it into any folder that holds a `tileset.json` to view that tileset:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>3D Tiles viewer</title>
  <!-- CesiumJS 1.146.0 from the jsDelivr CDN. No Cesium ion token is needed. -->
  <script src="https://cdn.jsdelivr.net/npm/cesium@1.146.0/Build/Cesium/Cesium.js"
          integrity="sha384-1lWK5deN/B+lLaUw4lk6xKp/L19ii/98SZLk6K8YthDskgvc+GbdixLzbCnSS2Wn"
          crossorigin="anonymous"></script>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/cesium@1.146.0/Build/Cesium/Widgets/widgets.css"
        integrity="sha384-ghEeMdcWWzRv/BPeUcX835vcKDGrxvROXisl/Btpv3GeekBUXTSPVcFJpI1Tcrgp"
        crossorigin="anonymous">
  <style>
    html, body, #viewer { margin: 0; width: 100%; height: 100%; overflow: hidden; }
    #message { position: absolute; top: 8px; left: 8px; right: 8px; padding: 8px 12px;
               background: #fff; color: #900; font: 14px sans-serif; display: none; }
  </style>
</head>
<body>
  <div id="viewer"></div>
  <div id="message"></div>
  <script type="module">
    // Natural Earth II ships inside CesiumJS, so the globe has imagery without
    // a Cesium ion token. Use `baseLayer: false` for no imagery at all.
    const viewer = new Cesium.Viewer("viewer", {
      baseLayer: Cesium.ImageryLayer.fromProviderAsync(
        Cesium.TileMapServiceImageryProvider.fromUrl(
          Cesium.buildModuleUrl("Assets/Textures/NaturalEarthII")
        )
      ),
      baseLayerPicker: false,
      geocoder: false,
      animation: false,
      timeline: false,
    });
    try {
      const tileset = await Cesium.Cesium3DTileset.fromUrl("./tileset.json");
      viewer.scene.primitives.add(tileset);
      await viewer.zoomTo(tileset);
    } catch (error) {
      const message = document.getElementById("message");
      message.textContent =
        `Could not load tileset.json (${error}). Serve this folder from a web ` +
        "server, for example `python3 -m http.server 8000` in this folder " +
        "(`py -m http.server 8000` on Windows), and open http://localhost:8000/ " +
        "instead of opening this file directly.";
      message.style.display = "block";
    }
  </script>
</body>
</html>
```

For no imagery at all, replace the `baseLayer: ...` option with
`baseLayer: false`. The page is not part of the tileset: the tiles load in any
viewer without it. Edit `index.html` in the `.web` folder, or replace it with
your own page, and Extract for Web keeps it when you extract again.

Serve the page and the tiles from the same web server. A viewer on another
origin, such as [Sandcastle](https://sandcastle.cesium.com/) or a page on
another port, cannot read the tiles from `python3 -m http.server`: the browser
blocks the request (CORS) because that server sends no
`Access-Control-Allow-Origin` header. To host the tiles for viewers elsewhere,
copy the folder to a web server that sends that header.

The tiles use `EXT_meshopt_compression` geometry, and the lossy profiles use
KTX2 textures (`KHR_texture_basisu`). CesiumJS reads both. Checked on
2026-11-13 with CesiumJS 1.146.0 in Chromium: the synthetic demo site from
[Getting started](getting-started.md), with a test texture added, published at
**Balanced** (KTX2 textures) and at **Lossless** (PNG). In this page both
loaded their tiles and textures with no request to Cesium ion, and CesiumJS
placed them at 3° E, 48.753° N, where the offset file's E 500000, N 5400000
in EPSG:32631 is. Other viewers were not tested. Blender's own glTF importer
reads neither extension; open published tiles in Blender with
[Inspect Output](inspect-output.md) instead.

## Limits

- Every publish is a full retile. Changing one layer or one texture rebuilds
  the whole archive. Faster updates need later rusty-tiles releases.
- rusty-tiles takes positions, normals, tangents, two UV maps, one colour
  attribute and core PBR materials. Other material nodes, more UV maps or more
  colour attributes are reported by the checks.
- One publish runs at a time.
