# GLB format reference

This page describes the file that **GLB with metadata** writes (see
[GLB with metadata](../guide/glb-metadata.md) for how to export it), for anyone
who reads it in their own tools. It is also the definition of the `geotools:`
XMP terms: their namespace IRI is this page,
`https://bendyson-arch.github.io/blender_geotools/reference/glb-format/#`.

## File layout

The file is standard glTF 2.0 binary. Its extensions are all optional: they
are listed in `extensionsUsed`, never in `extensionsRequired`, so any glTF
tool opens the geometry.

| Extension | What it holds |
|---|---|
| `EXT_mesh_features` | Feature IDs: which object (and, with the face table, which face) each vertex belongs to |
| `EXT_structural_metadata` | The schema, a property table per feature ID set, layer values as property attributes and property textures |
| `KHR_xmp_json_ld` | One XMP packet, referenced from `asset`, with the georeference |

Every buffer view starts on 8 bytes, and the binary chunk's data starts on 8
bytes in the file, as `EXT_structural_metadata` asks.

The mesh is the same as in the plain export: one mesh, one primitive per
material and attribute layout, positions in local metres converted to glTF
Y-up, and vertices welded per object. With texture layers, primitives are
also split per object, because each object has its own layer images.

A trimmed example, with two objects, a face float layer, a category layer and
a texture layer, Earth-placed (accessors, buffer views and the material left
out):

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
        "id": "geotools", "name": "GeoTools", "version": "0.4.0",
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
    "KHR_xmp_json_ld": {"packets": [{"@context": {"...": "see below"}, "@id": "", "...": "..."}]}
  },
  "samplers": [{"magFilter": 9728, "minFilter": 9728, "wrapS": 10497, "wrapT": 10497}]
}
```

### Objects

Every primitive has the feature ID set `object`: the vertex attribute
`_FEATURE_ID_0` holds the object's row in property table 0 (objects in name
order). The table's properties are `name`, the object's name when the file was
written, and `object_id`, a UUID kept on the Blender object. The id survives
renames and saves; a duplicated object gets a new one.

### Face layers and corner layers

By default each face or corner layer is a property attribute of class
`vertex`. A face's value is repeated on its corners. Vertices are shared only
where every layer agrees, so the values stay exact.

With the **Face Table** option, there is a second feature ID set, `face`
(`_FEATURE_ID_1`, property table 1), with one row per exported face: `object`
(the object's row), `polygon` (the face's index in the exported mesh) and
every face layer. Each face can then be picked and queried. Corner layers stay
property attributes.

### Texture layers

Each texture layer is a property of class `texel` in a property texture,
sampled with the layer's UV map. Values are stored exactly, as their
little-endian bytes in the channels of an 8-bit RGBA PNG:

- 32-bit floats and integers in R, G, B and A (`channels [0, 1, 2, 3]`);
- 8-bit values and enums in R;
- 16-bit enums in R and G.

The PNGs are lossless, so values come back bit for bit, NaN included. The
sampler is NEAREST: never filter these textures. Rows are in glTF order
(`v = 1 − v_blender`).

## Schema types

Property ids are the layer names. An enum is called `<name>_category`, and a
vertex attribute `_<NAME>` (`_GT_FEATURE_ID_0` for a layer named
`feature_id_0`, which would clash with a feature ID attribute). Every property
has a `name` (the layer's label), a `description` (its description, units and
which GeoTools layer it is), `noData` (the layer's no-data value; never on
booleans) and, for numbers, `min` and `max`: the range of exactly the values
written, without no-data and NaN.

Where the values are stored decides their type, because glTF vertex
attributes and textures cannot hold every metadata type:

| Layer type | Property table | Property attribute | Property texture |
|---|---|---|---|
| Float | FLOAT32 | FLOAT32 | FLOAT32, 4 channels |
| Integer | INT32 | FLOAT32 (exact to ±2^24) | INT32, 4 channels |
| Boolean | BOOLEAN | UINT8 (1 true, 0 false) | UINT8, 1 channel |
| Category | ENUM | ENUM | ENUM |

A category layer's enum uses the smallest of UINT8, INT8, UINT16, INT16 and
INT32 that holds every category and the no-data value (property attributes
allow at most 16 bits). Enum names are the category labels; a repeated label
gets ` (value i)`. Enum no-data is a name, so a no-data value adds an enum
value `"no data"`.

## Placement and the geotools: terms

Vertex positions are always local metres near the origin (glTF Y-up), so the
file opens in the right place in any glTF tool.

The georeference is one `KHR_xmp_json_ld` packet, referenced from `asset`.
Its `@context`:

```json
{"dc": "http://purl.org/dc/elements/1.1/",
 "geotools": "https://bendyson-arch.github.io/blender_geotools/reference/glb-format/#"}
```

Files written by GeoTools 0.3 use the namespace IRI
`https://github.com/BenDyson-Arch/blender_geotools/blob/main/docs/design/m4-glb-metadata.md#`
for the same terms. Match terms by IRI, not by prefix, and accept both.

The packet has `"@id": ""` (the file itself), Dublin Core `dc:format`
(`model/gltf-binary`), `dc:source` (the `.blend` file name, when saved) and
`dc:description`, and these terms. Numbers are JSON numbers, written exactly in
double precision, so a northing keeps its millimetres. Lists are
`{"@list": [...]}`.

| Term | Value |
|---|---|
| `geotools:version` | The GeoTools version that wrote the file |
| `geotools:placement` | `local`, `anchor` or `source_crs` |
| `geotools:axes`, `geotools:units` | `y-up` and `metre`: the model coordinates |
| `geotools:crs` | `EPSG:nnnn`, for a CRS with an EPSG code |
| `geotools:crsDefinition` | The CRS definition the tiles were placed with (EPSG code, PROJ or WKT) |
| `geotools:sourceOffset` | E, N and A of the offset: model coordinates plus this are CRS coordinates |
| `geotools:heightOffset` | Metres added to height plus A to give the ellipsoidal height |
| `geotools:anchor`, `geotools:sceneOffset` | Anchor placement: longitude, latitude, height; and E, N, U |
| `geotools:originLongitude`, `geotools:originLatitude`, `geotools:originHeight` | WGS84 degrees and ellipsoidal metres of the model's origin |
| `geotools:originModel` | That origin in model (glTF) coordinates |
| `geotools:heading` | Degrees counter-clockwise from true east to model +X at the origin |
| `geotools:groundMetresPerModelMetre` | Source CRS only: metres on the ground (on the ellipsoid at the origin's height) per model metre along +X, at the origin. About 1 / the combined scale factor, so 1.0004 where the grid scale factor is 0.9996 |
| `geotools:crsOperation` | How the CRS was converted: `portable` or `native` |
| `geotools:modelToEcef` | The 16 numbers, column-major, of the Earth-placed node matrix |
| `geotools:earthPlaced` | Whether the root node carries that matrix |
| `geotools:originUnavailable` | Why the origin terms are missing |

The origin terms need the bundled rusty-tiles, which computes them; a local
placement has none.

### Earth-placed files

With **Earth-Placed** on, a root node `GeoTools ECEF` carries a matrix that
takes model coordinates to Earth-centred, Earth-fixed (ECEF) metres, with the
mesh node as its child. Viewers that turn glTF Y-up into Z-up, such as
CesiumJS and 3D Tiles content, then place the model on Earth with no extra
input. General glTF tools apply the matrix too, in single precision, and show
the model about 6,400 km from their origin, which is why the option is off by
default.

For a source CRS, the matrix is measured at the centre of the site, so it
includes grid convergence and the scale factor. It is exact at the centre and
first order elsewhere: a corner of a 200 m site lands within 1 cm of where the
tiles put it. Over several kilometres, Earth's curvature makes any single
matrix drift (about 4 m at the corners of a 10 km site).

## Limits

- At most two UV maps per primitive, including the texture layers' UV maps.
- The face table holds at most 2^24 faces. Integer layers written as vertex
  attributes must stay within ±2^24.
- A float layer without a no-data value writes NaN where it has no value, and
  declares no `noData`. Set a no-data value to make it explicit.
- The file is not tiled: tilesets carry layers only as colours, in variants.
