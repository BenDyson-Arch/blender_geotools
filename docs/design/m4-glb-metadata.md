# M4 design: GLB export with metadata

Status: built for M4 (see [ROADMAP.md](../ROADMAP.md#m4-glb-export-with-metadata)).
This page is also the definition of the `geotools:` XMP namespace (section 5).

## Goal

One standard glTF 2.0 GLB of the source collection that any glTF tool opens,
with object identity and layer values carried as glTF metadata, the same
metadata 3D Tiles 1.1 uses. It is a separate file from the plain `source.glb`
that rusty-tiles tiles: rusty-tiles 0.4 refuses input with extensions, and the
plain GLB stays byte-identical to what M2 wrote.

Two ways to write it:

- a build profile whose **Format** is `.glb`: the Publish button reads
  **Export site.glb**, and the build goes through the same checks, staging
  directory and `build.json` as a `.3tz`, without rusty-tiles;
- **File > Export > GeoTools GLB with metadata (.glb)**, a one-off export with
  its own Apply Modifiers, Face Table and Earth-Placed options.

## Modules

| Module | What it holds |
|---|---|
| `core/metadata_schema.py` | LayerSpecs to an `EXT_structural_metadata` schema: classes, properties, types per storage, enums, ids. bpy-free and independent of any writer, so M9 can reuse it for tilesets. |
| `core/gltf_metadata.py` | Property tables (values, string offsets, boolean bits), attribute values, texture packing and a deterministic PNG encoder, the XMP packet, and `MetadataExtension`, which the writer calls. |
| `core/georef.py` | The local-to-ECEF frame from the placement probe, and the glTF node matrix. |
| `core/gltf_writer.py` | `write_glb(..., extension=...)`: the hook. Without an extension nothing changes and custom attributes are refused. |
| `blender/export.py` | `extract_scene(..., metadata=Metadata(...))`: feature ID and layer vertex attributes, welded with the geometry. |
| `blender/glb_export.py` | Object ids, reading layer values, `prepare_glb`, the checks, the File > Export operator. |
| `publish/job.py`, `publish/staging.py` | The `.glb` build: write, face map, record. |
| `tests/glbcheck.py` | A structural validator and decoder used by both test suites. |

## 1. File layout

Extensions, all optional (in `extensionsUsed`, never `extensionsRequired`):
`EXT_mesh_features`, `EXT_structural_metadata`, `KHR_xmp_json_ld`. Every
buffer view starts on 8 bytes and the BIN chunk's data starts on 8 bytes in
the file, as `EXT_structural_metadata` asks.

A trimmed example (two objects, a FACE float layer, a CATEGORY layer and a
TEXTURE layer, MGA zone 56, Earth-placed; accessors, buffer views and the
material are left out):

```json
{
  "extensionsUsed": ["EXT_mesh_features", "EXT_structural_metadata", "KHR_xmp_json_ld"],
  "asset": {"version": "2.0", "generator": "GeoTools",
            "extensions": {"KHR_xmp_json_ld": {"packet": 0}}},
  "nodes": [
    {"name": "GeoTools ECEF", "matrix": [-0.45392, 0.00015, 0.89150, 0, "...",
                                         -5046557.137, -2924993.614, -2569103.444, 1],
     "children": [1]},
    {"name": "GeoTools source", "mesh": 0}
  ],
  "meshes": [{"primitives": [{
    "attributes": {"POSITION": 1, "NORMAL": 2, "TEXCOORD_0": 3,
                   "_FEATURE_ID_0": 4, "_SLOPE": 5, "_LANDCOVER": 6},
    "indices": 0, "mode": 4,
    "extensions": {
      "EXT_mesh_features": {"featureIds": [
        {"featureCount": 1, "attribute": 0, "propertyTable": 0, "label": "object"}]},
      "EXT_structural_metadata": {"propertyTextures": [0], "propertyAttributes": [0]}
    }}, "..."]}],
  "extensions": {
    "EXT_structural_metadata": {
      "schema": {
        "id": "geotools", "name": "GeoTools", "version": "0.3.0-dev",
        "classes": {
          "object": {"properties": {
            "name": {"type": "STRING", "required": true},
            "object_id": {"type": "STRING", "required": true}}},
          "vertex": {"properties": {
            "slope": {"name": "Slope",
                      "description": "Steepness of the face. Units: deg. GeoTools layer slope (face float).",
                      "type": "SCALAR", "componentType": "FLOAT32",
                      "noData": -9999.0, "min": 0.0, "max": 7.5},
            "landcover": {"type": "ENUM", "enumType": "landcover_category",
                          "noData": "no data"}}},
          "texel": {"properties": {
            "shade": {"name": "Sun hours", "type": "SCALAR", "componentType": "FLOAT32",
                      "min": 0.0, "max": 9.0}}}
        },
        "enums": {"landcover_category": {"valueType": "INT8", "values": [
          {"name": "bare", "value": 0}, {"name": "grass", "value": 1},
          {"name": "no data", "value": -1}]}}
      },
      "propertyTables": [{"class": "object", "count": 2, "properties": {
        "name": {"values": 14, "stringOffsets": 15, "stringOffsetType": "UINT32"},
        "object_id": {"values": 16, "stringOffsets": 17, "stringOffsetType": "UINT32"}}}],
      "propertyTextures": [{"class": "texel", "properties": {
        "shade": {"index": 0, "texCoord": 0, "channels": [0, 1, 2, 3]}}}, "..."],
      "propertyAttributes": [{"class": "vertex", "properties": {
        "slope": {"attribute": "_SLOPE"}, "landcover": {"attribute": "_LANDCOVER"}}}]
    },
    "KHR_xmp_json_ld": {"packets": [{"@context": {"...": "section 5"}, "@id": "", "...": "..."}]}
  },
  "samplers": [{"magFilter": 9728, "minFilter": 9728, "wrapS": 10497, "wrapT": 10497}]
}
```

The mesh is the plain export's (M2): one mesh, one primitive per material and
attribute layout, positions in the local frame converted to glTF Y-up,
triangles welded per object. Only the metadata is added.

## 2. Schema from layers (`core.metadata_schema`)

Ids are stable and come from layer names: property id = layer name, enum id =
`<name>_category`, vertex attribute = `_<NAME>` (`_GT_FEATURE_ID_0` for a layer
named `feature_id_0`, which would clash with a feature ID attribute). Every
property has `name` (the layer's label, or its name), `description` (its
description, `Units: ...`, and which GeoTools layer it is), `noData` (the
layer's no-data; never on BOOLEAN), and `min`/`max` for SCALAR properties.

`min` and `max` are the range of exactly the values written (no-data and NaN
left out), worked out at export time from the same arrays; the layer's cached
`value_min`/`value_max` could be stale, or read with another Apply Modifiers
setting, and the 3d-tiles-validator refuses values outside the class range.

The storage decides the types, because glTF vertex attributes and textures
cannot hold every metadata type:

| Layer | Property table | Property attribute | Property texture |
|---|---|---|---|
| FLOAT | FLOAT32 | FLOAT32 | FLOAT32, 4 channels |
| INT | INT32 | FLOAT32 (exact to 2^24, refused beyond) | INT32, 4 channels |
| BOOLEAN | BOOLEAN | UINT8 (1 true, 0 false) | UINT8, 1 channel |
| CATEGORY | ENUM | ENUM | ENUM |

glTF vertex attributes have no 32-bit integers (UNSIGNED_INT is for indices
only), and property attributes cannot be BOOLEAN or STRING. Attribute
elements must be 4-byte aligned, so 1- and 2-byte attributes (enums, booleans)
use a buffer view with `byteStride` 4.

A CATEGORY layer's enum value type is the smallest of UINT8, INT8, UINT16,
INT16 and INT32 that holds `0..n-1` and the no-data value; property attributes
allow at most 16 bits. Value names are the category labels (a repeated label
gets ` (value i)`). Enum no-data is a name, so a no-data value adds an enum
value `"no data"`, which the property's `noData` names; validators then accept
the no-data value as a member of the enum.

## 3. Objects and face layers

**Object identity.** Feature ID set `object` on every primitive:
`_FEATURE_ID_0` holds the object's row (its index in name order), FLOAT (the
`EXT_mesh_features` advice: exact to 2^24), `featureCount` the number of
distinct ids in the primitive, `propertyTable` 0. Table `object` has `name`
(the object name when written) and `object_id`, a UUID kept on the Blender
object (`Object.geotools_id`, made on the first export). A rename or a save
keeps it. A duplicate copies the property, so when two exported objects share
an id the first in name order keeps it and the other gets a new one. Linked
objects, which cannot be written, get a UUID derived from their library path
and name. The export operator is undoable so a later undo keeps the ids it
made; the build profile's publish pushes an undo step when it makes ids.

**FACE and CORNER layers, default: property attributes.** One property
attribute (class `vertex`) on every primitive. FACE values repeat on the
face's corners. Every layer attribute is a weld source, compared bit for bit,
so two corners become one vertex only when every layer agrees too. An object
without the layer's attribute gets the fill value (no-data, else NaN for
FLOAT, 0 for INT). Values are read from the meshes the export publishes
(`layers.layer_meshes`, evaluated unless Apply Modifiers is off), so a layer
computed only by a Geometry Nodes modifier, even one that changes the
topology, exports like a stored one.

**Face Table option.** Feature ID set `face` (`_FEATURE_ID_1`, property table
1): one row per exported face, objects in order and each object's evaluated
faces in order, with properties `object` (UINT32, the object row), `polygon`
(UINT32, the face index in the exported mesh) and every FACE layer (full table
types: INT32, BOOLEAN bits, ENUM). The face id is a weld source, so vertices
are shared only within a face; FACE layers then are not vertex attributes,
CORNER layers still are. Refused over 2^24 faces (FLOAT feature IDs). Each
face is pickable and queryable.

Its cost depends on the data, because face layers as vertex attributes already
split vertices wherever neighbouring faces differ. Measured on a 128 x 128
grid (`build.json` records `glb.face_table_cost` for each build):

| Mesh | Face layers | Default | Face Table |
|---|---|---|---|
| 16,384 quads | none | 16,641 vertices, 995 KB | 65,536 vertices (3.9x), 3.15 MB |
| 16,384 quads | a float per face (all different) | 65,536, 3.02 MB | 65,536, 3.22 MB (+6.5%) |
| 16,384 quads | a category in four blocks | 16,770, 1.07 MB | 65,536, 3.17 MB |
| 32,768 triangles | none | 16,641, 995 KB | 98,304 (5.9x), 4.59 MB |
| 32,768 triangles | a float per face | 98,304, 4.33 MB | 98,304, 4.72 MB (+9%) |

## 4. Texture layers: property textures

Each object has its own float image per texture layer, and a primitive refers
to one property texture, so with texture layers the export splits primitives
by object too (`Metadata.per_object`). Each textured primitive gets one
property texture (class `texel`) with one image per layer, sampled with the
layer's UV map: it is added to the primitive's texcoord sets if the material
does not already use it (refused if that would make a third). An object
without an image for a layer has no value for it (the property is left out of
its property texture).

**Packing, chosen: exact bytes, no quantisation.** A value is its
little-endian bytes in the channels of an 8-bit RGBA PNG: FLOAT32 and INT32 in
R, G, B, A (`channels [0, 1, 2, 3]`), UINT8 and 8-bit enums in R (G and B 0, A
255), 16-bit enums in R and G. The PNG is lossless, so the values round-trip
bit for bit, NaN included, and no range has to be known. The alternative,
UINT8 or UINT16 with `offset`/`scale` on one or two channels, would halve or
quarter the bytes for smooth data but quantise it, and needs a range that
fits; it can be added later as an option. Rows are flipped from Blender's
bottom-up pixel order, so glTF `v = 1 - v_blender` samples the same texel. The
sampler is NEAREST (packed bytes must never be filtered). The PNG writer is
GeoTools' own (`gltf_metadata.encode_png`: filter None, zlib level 6), so the
output is deterministic.

## 5. Placement and the `geotools:` XMP terms

Vertex positions always stay in the local frame (float32 near the origin, glTF
Y-up), so the file opens in the right place in any glTF tool.

The georeference is one `KHR_xmp_json_ld` packet, referenced from `asset`. Its
`@context`:

```json
{"dc": "http://purl.org/dc/elements/1.1/",
 "geotools": "https://github.com/BenDyson-Arch/blender_geotools/blob/main/docs/design/m4-glb-metadata.md#"}
```

`"@id": ""` (the file itself), Dublin Core `dc:format` (`model/gltf-binary`),
`dc:source` (the `.blend` file name, when saved) and `dc:description`, and
these `geotools:` terms. Numbers are JSON numbers (binary64, written exactly,
so a northing keeps its millimetres); arrays are `{"@list": [...]}`.

| Term | Value |
|---|---|
| `geotools:version` | GeoTools version that wrote the file |
| `geotools:placement` | `local`, `anchor` or `source_crs` |
| `geotools:axes`, `geotools:units` | `y-up`, `metre`: the model coordinates |
| `geotools:crs` | `EPSG:nnnn` for an EPSG code (`crs_catalog.epsg_code`) |
| `geotools:crsDefinition` | the definition rusty-tiles was given (EPSG code, PROJ or WKT) |
| `geotools:sourceOffset` | E, N, A of the source offset |
| `geotools:heightOffset` | metres added to height plus A for the ellipsoidal height |
| `geotools:anchor`, `geotools:sceneOffset` | anchor placement: lon, lat, height; E, N, U |
| `geotools:originLongitude`, `geotools:originLatitude`, `geotools:originHeight` | WGS84 degrees and ellipsoidal metres of the probe's origin |
| `geotools:originModel` | that origin in model (glTF) coordinates |
| `geotools:heading` | degrees counter-clockwise from true east to model +X at the origin (the anchor heading, or the grid convergence) |
| `geotools:gridScale` | true metres per model metre along +X (source CRS: the grid scale factor and height) |
| `geotools:crsOperation` | rusty-tiles' operation (`portable` or `native`) |
| `geotools:modelToEcef` | the 16 numbers, column-major, of the Earth-placed node matrix |
| `geotools:earthPlaced` | whether the root node carries that matrix |
| `geotools:originUnavailable` | why the origin is missing (rusty-tiles not bundled, or it refused the CRS) |

The origin terms come from the placement probe, so they need rusty-tiles; a
local placement has none.

**Earth-Placed option** (off by default). A root node `GeoTools ECEF` with
`matrix` = `C M C^-1`, where `C` is Blender (x, y, z) to glTF (x, z, -y) and `M`
maps Blender world coordinates to ECEF, and the mesh node as its child.
Viewers that turn glTF Y-up into Z-up (CesiumJS, 3D Tiles content) then get
ECEF. General tools apply the matrix too, in float32, and put the model about
6,400 km from their origin, which is why it is off by default and the tooltip
says so.

`M` comes from rusty-tiles (`core.georef.measure_frame`):

- Anchor placement: the tileset root transform as it is. The tiles keep the
  local frame, so it maps local coordinates to ECEF exactly; the test checks
  `C^-1 N C` equals the probe's root transform.
- Source CRS: rusty-tiles reprojects each vertex into an east-north-up frame
  at the centre of the source bounds and puts that frame in the root
  transform, so copying it would ignore grid convergence and the scale factor.
  200 km from the UTM 56 central meridian at 27 S that is 0.93 degrees, 1.63 m
  over 100 m. Instead four more probes (the centre moved half the extent
  east, west, north and south, about 10 ms each) measure where grid east and
  grid north land in that frame, as central differences
  ((P₊ − P₋) / 2d, the tangents at the centre), and `M` = root transform x
  that measured frame at the centre. Exact at the centre, first order
  elsewhere: a corner of a 200 m site lands within 1 cm of where rusty-tiles
  puts it, and every corner of a 10 km site at the MGA zone 56 edge 3.93 m
  from it, the Earth's curvature over 7 km that no single matrix can follow
  (core tests). Until 0.3.0 the probes were one-sided ((P₊ − P₀) / d,
  secants): the axes dipped 4×10⁻⁴ rad below the tangent plane on that site,
  the scale was 18 ppm high, and the corners were 0.18 m to 7.84 m off.

## 6. Output and records

A `.glb` profile writes `<dir>/<name>.glb` (`<name>.<layer>.glb` with a
Variant, which also presents that layer as the variant tilesets do). Its
staging directory is `<name>.glb.geotools`, so a `.3tz` and a `.glb` of the
same name keep separate records. The GLB is written into `work/` on the worker
thread and moved onto the output once complete (a failed export keeps the old
file), then `facemap.npz` (GLB triangle to Blender face) and `build.json`.
Toggling the Format rewrites the output path's suffix; a path whose suffix does
not match is refused. Build Settings shows only Apply Modifiers for a `.glb`
profile; Last Build has no Preview.

`build.json` keeps schema 2, with `"output_type": "glb"` (`"3tz"` for
tilesets) and a `"glb"` object: `path`, `sha256`, `bytes`, `primitives`,
`triangles`, `options` (`face_table`, `earth_placed`, `apply_modifiers`,
`variant`), `extensions_used`, `objects`, `vertex_layers`, `face_layers`,
`texture_layers`, `face_table_rows`, `property_textures`, `vertices`,
`placement`, `origin_lon_lat_height` and, with the face table,
`face_table_cost` (`vertices`, `vertices_without_face_table`, `vertex_ratio`,
`extra_vertex_bytes`). `rusty_tiles_version` and `report_summary` are empty.

Checks: Run Checks, plus `glb_issues`: `glb-edit-mode` (error),
`glb-layer-invalid` (warning; the layer is left out), `glb-layer-type`
(error, e.g. an integer beyond 2^24 as an attribute), `glb-image-missing`
(warning), `glb-face-table-empty` (info), `glb-earth-placed-local` and
`glb-earth-placed-rusty-tiles` (errors).

## 7. Validation

- `tests/glbcheck.py` checks the container, accessors, extension use, feature
  ID ranges and counts, the schema, property table lengths and string offsets
  (UTF-8 object names), property attribute types, enum values and ranges, and
  property texture channels and samplers; both suites use it.
- `GEOTOOLS_TEST_3D_TILES_VALIDATOR` (the 3d-tiles-validator CLI, run as
  `<cli> --tileContentFile FILE --reportFile REPORT`) and
  `GEOTOOLS_TEST_GLTF_VALIDATOR` (the Khronos glTF-Validator CLI, run as
  `<cli> -o FILE`) run the external validators in the core and Blender tests
  when set. 3d-tiles-validator 0.6.1 validates `EXT_mesh_features` and
  `EXT_structural_metadata` itself (feature counts, ranges, enum values) and
  reports 0 errors and 0 warnings on these files; its only infos are the
  glTF-Validator's "extension not supported" and "may be unused" for the
  property textures.
- Blender's glTF importer opens the file: the geometry comes in whole and the
  extensions are ignored.

## Limits

- At most two texcoord sets per primitive (the writer's rusty-tiles limit),
  including the texture layers' UV maps.
- The face table holds at most 2^24 faces; INT layers as vertex attributes at
  most +-2^24.
- A FLOAT layer without a no-data value writes NaN where it has no value, and
  declares no `noData`; set one to make it explicit.
