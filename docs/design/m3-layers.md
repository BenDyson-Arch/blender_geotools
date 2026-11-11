# M3 design: layers and the bake engine

Status: agreed design for M3 (see [ROADMAP.md](../ROADMAP.md#m3-layers-and-bake-engine)).
The interfaces below are shared by every M3 work stream. Change them here
first, then in code.

## Goal

A **layer** is a named, typed value that lives on the source: one value per
face, per face corner, or per texel. Layers come from Geometry Nodes, from
analysis tools (M4) or from the user. M3 provides:

1. the **layer registry**, stored in the `.blend`;
2. **vertex-space bakes**: freeze a Geometry Nodes output into a stored source
   attribute, transferring values when the node tree changes topology;
3. **texture-space bakes**: a Cycles bake of an attribute into an image on the
   source UVs, after a UV-overlap check;
4. a **presentation** step that makes a layer visible (a colour ramp into
   COLOR_0, or an 8-bit texture in the base-colour or emissive slot) and
   publishes it as a **variant tileset** such as `site.slope.3tz`.

rusty-tiles 0.4.0 publishes no custom attributes, so until M8 a layer reaches a
tileset only through presentation. The registry still records everything the
0.5 annotate pass will need (units, no-data, aggregation).

## Rules that apply to every work stream

- The performance rule holds: no Python loops over faces, corners, vertices or
  texels. Use `foreach_get`/`foreach_set` with numpy, Geometry Nodes or Cycles.
  Building a node tree or a material from Python is fine.
- `geotools/core` never imports `bpy`. Put maths (ramps, transfer reductions,
  UV rasterisation, validation) in `core` with plain pytest tests, and keep the
  `bpy` glue thin.
- Every bake is undoable as one step or restores what it touched in a
  `try/finally`: temporary materials, modifiers, objects, selection, active
  object, render engine and bake settings.
- Values are float32 in Blender attributes. No-data and range numbers in the
  registry are binary64 strings, like the placement fields.
- Tests: `tests/core` for core modules, `tests/blender` for operators,
  persistence and bakes. Run both suites with `--expect missing` and
  `--expect available`.

## 1. Layer registry

### Core: `geotools/core/layers.py`

```python
class Domain(StrEnum):      FACE = "FACE"; CORNER = "CORNER"; TEXTURE = "TEXTURE"
class DataType(StrEnum):    FLOAT = "FLOAT"; INT = "INT"; BOOLEAN = "BOOLEAN"; CATEGORY = "CATEGORY"
class Origin(StrEnum):      GEONODES = "GEONODES"; ANALYSIS = "ANALYSIS"; MANUAL = "MANUAL"
class Aggregation(StrEnum): AREA_MEAN = "AREA_MEAN"; MIN = "MIN"; MAX = "MAX"; MODE = "MODE"; NONE = "NONE"

@dataclass(frozen=True, slots=True)
class LayerSpec:
    name: str                 # identifier: [a-z][a-z0-9_]{0,47}, unique in the project
    domain: Domain
    data_type: DataType
    units: str = ""           # free text, e.g. "deg", "m", "h"
    nodata: float | None = None
    origin: Origin = Origin.MANUAL
    aggregation: Aggregation | None = None   # None -> default_aggregation(data_type)
    description: str = ""
    categories: tuple[str, ...] = ()         # CATEGORY only: label of value i
    label: str = ""                          # display name; the UI shows label, or name if empty
    value_min: float | None = None           # range actually present, ignoring no-data and NaN;
    value_max: float | None = None           #   set by whoever writes the values
    provenance: Mapping[str, object] | None = None
        # {"tool": str, "parameters": {...}, "geotools_version": str}, JSON-safe;
        # set by bakes and analysis tools (make_provenance), None for manual layers

ATTRIBUTE_PREFIX = "gt_"
def attribute_name(spec_or_name) -> str      # "gt_" + name: the mesh attribute that holds a FACE/CORNER layer
def validate(spec, existing_names=()) -> list[Issue]
def default_aggregation(data_type) -> Aggregation   # FLOAT/INT -> AREA_MEAN, BOOLEAN/CATEGORY -> MODE
def to_record(spec) -> dict                  # JSON-safe, stable key order; for build.json
def from_record(d) -> LayerSpec
def make_provenance(tool, parameters=None, geotools_version="") -> dict
def display_name(spec) -> str                # label, or name if the label is empty
```

`to_record` keys, in order: `name`, `label`, `attribute` (`gt_<name>`, null for
TEXTURE), `domain`, `data_type`, `units`, `nodata`, `value_min`, `value_max`,
`origin`, `aggregation` (the effective one), `description`, `categories`,
`provenance`. `categories` is a list of `{"value": int, "label": str}` objects
(the spec keeps the tuple: value i is label i); `from_record` accepts that form
in any order, or plain labels. Non-finite numbers are written as null.

Validation codes (Issue codes, kebab-case): `layer-name-invalid`,
`layer-name-duplicate`, `layer-aggregation-invalid` (for example AREA_MEAN on a
CATEGORY), `layer-categories-missing`, `layer-nodata-invalid`,
`layer-range-invalid` (non-finite, or minimum above maximum),
`layer-provenance-invalid` (not an object with a non-empty `tool`).

### Storage in Blender

- `scene.geotools.layers`: `CollectionProperty(type=GeoToolsLayer)` and
  `layers_index`. `GeoToolsLayer` mirrors `LayerSpec`. `nodata`, `value_min`
  and `value_max` are string fields parsed as binary64, empty meaning none.
  `categories` is a newline-separated string. `provenance` is a JSON string,
  empty meaning none.
- FACE and CORNER layers live in the mesh attribute `gt_<name>` on every mesh
  of the source collection, with `FACE` or `CORNER` domain and `FLOAT`, `INT`
  or `BOOLEAN` attribute type (CATEGORY is `INT`).
- TEXTURE layers live in images, one per object, as
  `GeoToolsLayerImage(object: PointerProperty(Object), image:
  PointerProperty(Image))` items in `GeoToolsLayer.images`. The layer also
  stores `uv_map` (name, empty meaning the active render UV map), `resolution`
  and `margin` (pixels). Images are float (`float_buffer=True`, Non-Color) and
  are saved as OpenEXR next to the `.blend` in `//geotools_layers/`, so the
  source stays "the `.blend` plus image files".
- Helpers in `geotools/blender/layers.py`, used by every bake and tool:

  ```python
  def spec_from_props(item) -> LayerSpec
  def find_layer(scene, name) -> GeoToolsLayer | None
  def ensure_layer(scene, spec) -> GeoToolsLayer          # create or update the registry entry
  def ensure_attribute(mesh, spec) -> bpy.types.Attribute # create gt_<name> with the right domain/type
  def read_layer(mesh, spec) -> np.ndarray                # foreach_get into a numpy array
  def write_layer(mesh, spec, values) -> None             # foreach_set, checks the length
  def layer_status(scene, item) -> list[Issue]            # missing attributes or images, wrong domain/type
  def update_value_range(scene, item) -> tuple[float | None, float | None]
      # numpy min/max over the gt_<name> attributes (TEXTURE: the images' first
      # channel), ignoring no-data, NaN and inf; stored in value_min/value_max
  ```

- Additions made while building stream 1 (all compatible with the above):
  - `core.layers` also has `effective_aggregation(spec)`, `numpy_dtype(data_type)`,
    `fill_value(spec)` (no-data, else NaN for FLOAT, 0 for INT/CATEGORY, False for
    BOOLEAN), `VALID_AGGREGATIONS`, `RESERVED_NAMES` (`present`, for
    `gt_present`), `json_safe`, and `parse_number`/`format_number` (alias
    `parse_nodata`/`format_nodata`), `parse_categories`/`format_categories` and
    `parse_provenance`/`format_provenance` for the text fields. `validate`'s `existing_names` are
    the *other* layers' names.
  - `to_record` writes the effective aggregation and an `"attribute"` key
    (`gt_<name>`, or null for TEXTURE); `from_record(to_record(s)) == s` when
    `s.aggregation` is set.
  - `GeoToolsLayer.aggregation` has a `DEFAULT` item for `None`; the layer name is
    the PropertyGroup `name`.
  - `spec_from_props` never raises: bad number text becomes NaN, bad provenance
    JSON `{"invalid": text}`, both reported by `validate`. `ensure_layer` raises
    `ValueError` on an invalid spec. Adding a layer can move other entries in
    memory, so look a `GeoToolsLayer` up again rather than keeping it.
  - `ensure_attribute(mesh, spec, *, issues=None)` and
    `write_layer(mesh, spec, values, *, issues=None)`: a new attribute is filled
    with `fill_value`; a replaced one is logged and added to `issues` as
    `layer-attribute-replaced`. `write_layer` creates the attribute if needed.
    `read_layer` raises `KeyError` (missing) or `ValueError` (wrong domain/type).
    All need Object Mode.
  - More helpers in `blender.layers`: `source_objects`, `source_meshes`,
    `domain_size`, `attribute_type`, `attribute_problem`, `remove_attribute`,
    `layer_image`, `ensure_layer_image`, `save_layer_image` (full-float EXR at
    `//geotools_layers/<layer>__<object>.exr`), `image_is_saved`,
    `remove_layer_data`, `layer_records`, `write_props`, `other_layer_names`.
  - `layer_status` codes: `layer-attribute-missing` (warning),
    `layer-attribute-mismatch` (error), `layer-image-missing` (warning),
    `layer-image-unsaved` (warning), `layer-uv-map-missing` (error),
    `layer-no-source` (info), plus the validation codes.
  - Operators: `geotools.layer_add`, `layer_remove` (`delete_data`; image files on
    disk are kept), `layer_rename` (renames the attributes too),
    `layer_create_attributes`, `layer_set_categories`, `layer_update_range`.
  - Publish passes the records in `BuildRequest.layers`; invalid layers are left
    out with a warning.
- UI: a **Layers** panel (`GEOTOOLS_PT_layers`) after Source, with a UIList,
  add/remove operators, the fields of the selected layer, and its status
  (`layer_status`). Removing a layer asks whether to delete its attributes and
  images too.
- The registry and the layer attributes survive saving and reopening the
  `.blend`. Images survive because they are saved files.
- `build.json` gets a `"layers"` list (`to_record` of each layer) and, for a
  variant, `"variant": {"layer": name, ...}` (section 4; null for the plain
  tileset). Bump `RECORD_SCHEMA` to 2.
  Schema 2 also has:
  - `"source": {"glb_sha256": <hex of source.glb>, "blend_sha256": <hex of the
    saved .blend, or null>}`. The operator passes `BuildRequest.blend_path`
    (`publish.blend_path_for_record()`: None if the file is unsaved or
    `bpy.data.is_dirty`), and the worker thread hashes the file. An unreadable
    file is recorded as null.
  - `"placement"` keeps the rusty-tiles keywords and gains `"crs_epsg"`:
    `"EPSG:nnnn"` when `source_crs` is a `core.crs_presets` preset's definition
    (`crs_presets.epsg_code`, from `for_definition(...).epsg`) or is itself
    `EPSG:nnnn`, else null. `source_crs` stays as the fallback.
  - `rusty_tiles_version` as before (the wheel exposes no commit).

## 2. Vertex-space bake: `geotools/blender/bake_vertex.py`

Operator `geotools.bake_geonodes_layer`: for a layer and a Geometry Nodes
modifier, read the named attribute that the modifier outputs ("Store Named
Attribute") from the evaluated mesh, and store it as the layer's `gt_<name>`
attribute on the original mesh.

- **Same topology** (the evaluated mesh has the same polygon count and the same
  topology digest as the original): copy directly with `foreach_get` and
  `foreach_set`. Convert domains with numpy: point to corner through
  `loops.vertex_index`; corner to face as the mean of each face's corners
  (`np.add.reduceat` over `polygons.loop_start`); face to corner by
  `np.repeat` over `loop_total`.
- **Changed topology**: transfer values to the source faces, with the method
  chosen in the operator:
  - `NEAREST`: the value at the nearest point of the evaluated surface to
    each source face centre. Use a Geometry Nodes evaluation with Sample
    Nearest Surface, not `mathutils.bvhtree` in a Python loop.
  - `RAYCAST`: cast along the source face normal (both directions, within a
    maximum distance) with the Raycast node, falling back to NEAREST on a miss.
  - `AREA_MEAN`: for each evaluated face, find the nearest source face (one
    Geometry Nodes evaluation that writes an index attribute), then reduce in
    numpy: `np.bincount(index, weights=value * area) / np.bincount(index,
    weights=area)`. Source faces that receive nothing fall back to NEAREST.
  Put the reductions in `geotools/core/transfer.py` with pytest tests.
- Option: after a successful bake, turn the modifier off in the viewport and
  render, so the export uses the frozen values on the source topology.
- Report how many faces were filled by each path, and how many were left as
  no-data (written as the layer's `nodata`, or NaN for FLOAT when none is set).

Settled while building stream 2 (all compatible with the above):

- Operator properties: `layer` (empty for the selected one), `modifier`,
  `attribute`, `domain` (`LAYER` keeps the layer's domain; `FACE` or `CORNER`
  changes it, which also lets a TEXTURE entry become an attribute layer),
  `method` (`NEAREST`, `RAYCAST`, `AREA_MEAN`), `max_distance` (raycast, each
  way, default 1) and `disable_modifier` (default off). Every source object
  with a Geometry Nodes modifier of that name is baked; objects without it are
  skipped and reported, and a mesh shared by several objects is baked once.
- The mesh is evaluated up to and including the modifier: later modifiers are
  turned off in the viewport for the evaluation, and the modifier itself on
  (so a modifier turned off by an earlier bake can be baked again). Their
  visibility is restored afterwards.
- The attribute may be on the POINT, EDGE, FACE or CORNER domain, with type
  FLOAT, INT, INT8 or BOOLEAN. Vectors and colours are refused.
- Transfers use one temporary object that shares the original mesh and reads
  the evaluated mesh with an Object Info node (original space). NEAREST uses
  Sample Nearest (FACE) for the evaluated face and Sample Nearest Surface for
  the interpolated value. A CORNER layer samples each corner moved 1%
  (`CORNER_INSET`) towards its face centre, so a corner takes its own face's
  side of a value boundary. RAYCAST casts two rays (normal and reverse) from
  the same points; the nearer hit wins. AREA_MEAN gives a CORNER layer each
  face's value on all its corners.
- `core.transfer.reduction_kind`: BOOLEAN, CATEGORY and INT layers aggregated
  by MODE never interpolate. They take the value of the evaluated face found
  (the mode of its corners), and AREA_MEAN takes the value with the largest
  total area (`segment_mode`). Other INT layers are averaged and rounded.
- No-data and NaN never mix into a mean: reductions skip them, and the
  sampling tree maps the layer's `nodata` to NaN before Sample Nearest Surface
  and Raycast interpolate. Where the interpolated value is NaN, the evaluated
  face's own value (the mean of its valid corners) is used. An element whose
  value is still no-data is counted as no-data, not under its path.
- Every object's values are computed before anything is written, so a failure
  leaves the source and the registry unchanged. The layer then becomes
  `origin=GEONODES` with provenance `make_provenance("bake_geonodes", {...})`.
  The parameters are `modifier`, `node_group`, `attribute`, `method`,
  `domain`, `source_domains`, `same_topology`, `objects`, `counts` (per path:
  `copied`, `area_mean`, `raycast`, `nearest`, `fallback`, plus `nodata`),
  `modifier_disabled` and, for RAYCAST, `max_distance`.
- The panel's settings are kept per layer in `GeoToolsLayer.vertex_bake`
  (`GeoToolsVertexBake`: the operator's settings plus `last_report`).
  `bake_vertex.register()` adds this pointer to the registered class, so
  `properties.py` is unchanged. The UI is the **Bake from Geometry Nodes**
  sub-panel of Layers (`GEOTOOLS_PT_layers_bake_vertex`), shown for FACE and
  CORNER layers.

## 3. Texture-space bake: `geotools/blender/bake_texture.py`

Operator `geotools.bake_texture_layer`: bake a layer, or a named attribute of
the evaluated mesh, into the layer's per-object images on the source UVs, with
Cycles.

- Use Cycles `EMIT`. For the bake only, replace each material slot's material
  with a bake material: Attribute node, then Emission, then Material Output,
  plus an active Image Texture node with the target image. Restore the
  original materials, the render engine, the device and the bake settings
  afterwards, even when the bake fails or is cancelled.
- Use `margin` pixels of gutter (`margin_type="EXTEND"`).
- **Selected to active**: when the attribute exists only on a
  topology-changing Geometry Nodes result, bake from a temporary object made
  from the evaluated mesh onto the original mesh with the modifier off, with
  `use_selected_to_active` and a cage or ray distance.
- **UV-overlap check** (`geotools/core/uv_overlap.py`, numpy): flag UV
  triangles that share texels, using the same rule as rusty-tiles' texture
  ownership ("one triangle per texel" over decoded-UV footprints; see
  rusty-tiles `src/mesh_large/batching.rs` and
  `docs/architecture/h28-tile-batching-contract.md`). Report it as Issues (code
  `uv-overlap`, with the object and the number of overlapping texels and
  triangles), as a check in the Checks panel and before a texture bake. A
  texture bake refuses overlapping UVs unless the user chooses to bake anyway.
  It must run on a 10M-triangle mesh in seconds, at a coarse rasterisation if
  needed, with the resolution reported.

Settled while building stream 3 (all compatible with the above):

- **Ownership rule.** rusty-tiles does not rasterise. `shared_footprints` and
  `sweep` in `src/mesh_large/batching.rs` (contract invariant 5) call two
  triangles shared when their decoded-UV *interiors* share area, tested
  exactly on a 2^-56 grid: shared edges and corners never count, a zero-area
  triangle is never shared, and a coordinate that is not finite or not below
  8 in magnitude makes its triangle shared. On a REPEAT (period 1) or
  MIRRORED_REPEAT (period 2) axis, `period_copies` compares the copies under
  the axis' symmetry (`x - 1`; `2 - x` and `x - 2`), so a triangle crossing a
  mirror line shares with itself, and a footprint wider than 1 on a period axis
  makes every triangle of the page shared. CLAMP compares footprints where
  they lie, also outside `[0, 1]`.
- `core.uv_overlap.find_overlaps(uv, triangles=None, *, resolution, wrap,
  max_samples, max_grid, max_examples)` rasterises that rule. A triangle owns
  texel `(i, j)` when the centre `((i + 0.5) / W, (j + 0.5) / H)`, nudged by an
  infinitesimal `(e, e^2)`, is in its open interior: each edge function is
  positive, or zero with an inward normal pointing +x (or +y on a vertical
  edge). Two triangles with disjoint interiors never own the same centre.
  Edge functions are binary64 from a canonical endpoint order, so the two sides
  of a shared edge get exactly opposite values. REPEAT folds the texel index
  `mod W`, MIRRORED `mod 2W` mirrored. A texel owned twice (by two triangles,
  or by one and its mirror image) is overlapping. The raster never flags what
  rusty-tiles calls unique; it misses overlaps thinner than a texel. The
  out-of-range and wide cases are flagged per triangle, as in rusty-tiles.
- It returns `OverlapResult`: `width`, `height` (the grid used), `requested`,
  `triangles`, `overlapping_texels`, `overlapping_triangles` (rusty-tiles'
  count: every triangle when one is wide), `examples`, `out_of_range`, `wide`,
  and `coarsened`/`ok`. The grid is halved while more than `max_samples`
  (2^25) candidate texels or `max_grid` (2^26) texels are needed. On a
  synthetic 10M-triangle layout it takes about 1.7 s at 1024 and 3.8 s at
  4096 (`scripts/bench_uv_overlap.py`); 8192 and above coarsen to 4096.
- The check uses the evaluated mesh, its loop triangles and the layer's UV map
  (empty: the active render UV map), at the layer's `resolution`, with REPEAT
  wrap. Issue code `uv-overlap`, subject the object; the message names the UV
  map, the counts, the grid (and what it was coarsened from) and example face
  indices. The Checks panel adds it as a warning for each source object and
  each UV map a TEXTURE layer uses (nothing without TEXTURE layers, since
  rusty-tiles records overlap but never refuses it). The **Texture Bake**
  sub-panel also has **Check UV Overlap** (`geotools.check_layer_uvs`).
- Operator properties: `layer` (empty for the selected one; must be TEXTURE),
  `source_layer` (a FACE or CORNER layer, baked from `gt_<name>`), `attribute`
  (used when `source_layer` is empty), `method` (`AUTO`, `DIRECT`,
  `SELECTED_TO_ACTIVE`), `cage_extrusion` (0: 1% of the object's bounding-box
  diagonal), `max_ray_distance` (0: twice the extrusion) and `bake_anyway`.
  The operator opens a dialog with these; nothing is added to `properties.py`.
- `AUTO` bakes an object directly when its evaluated mesh has the attribute and
  the same face and corner counts as the original, selected-to-active when the
  evaluated mesh has it on a different topology, and directly with all
  modifiers off when only the original mesh has it. Selected-to-active bakes
  from a temporary object holding `new_from_object` of the evaluated mesh onto
  the object with all its modifiers off, so the target is the stored mesh.
- Cycles writes 0 for NaN and inf, so the bake material emits
  `(v, (v - v) == 0, 0)`: the value and a finite mask. Afterwards a texel with
  mask `G > 0` gets `R / G` (exactly the value inside a face, a weighted mean
  in the EXTEND gutter) and the others `fill_value` (no-data, or NaN). INT,
  CATEGORY and BOOLEAN layers are rounded. The value is written to R, G and B,
  alpha 1. The Attribute node's Color output is used, since Fac can be an ulp
  off. One sample, CPU device, no denoising.
- Material slots are switched to object links for the bake, so a mesh shared by
  several objects gets each object's own image. A mesh without slots gets one
  temporarily. Hidden objects are shown for the bake. A failed bake removes
  the images it made and reloads the others from their files; a step that
  cannot be restored is reported as an error.
- Provenance: `make_provenance("bake_texture", {...})` with `attribute`,
  `source_layer`, `method`, `methods` (per object: `DIRECT`, `ORIGINAL` or
  `SELECTED_TO_ACTIVE`), `uv_map`, `resolution`, `margin`, `margin_type` and
  `bake_anyway`. The layer's origin becomes the source layer's, or `GEONODES`
  for a named attribute.

## 4. Presentation and variant tilesets

`geotools/core/ramps.py` (numpy, pytest):

```python
PALETTES: dict[str, np.ndarray]   # "viridis", "magma", "cividis", "turbo", "diverging_rdbu", "greys"
def apply_ramp(values, vmin, vmax, palette, *, nodata=None, nodata_rgba=(0,0,0,0)) -> np.ndarray  # (N,4) uint8, sRGB
def apply_categories(values, colours, *, nodata=None) -> np.ndarray
def auto_range(values, nodata=None, percentiles=(2, 98)) -> tuple[float, float]
```

A layer's presentation is stored on the layer as `GeoToolsLayerPresentation`:
`target` (`COLOR_0`, `BASE_COLOR` or `EMISSIVE`), `blend` (`REPLACE`, which
sets the base colour to white with no texture, or `MULTIPLY`), `palette`,
`range_min` and `range_max` (binary64 strings; empty means `auto_range`), and,
for CATEGORY, one colour per category.

- **COLOR_0**: for FACE and CORNER layers, write the ramp to a byte colour
  attribute `gt_present` (CORNER domain).
- **BASE_COLOR and EMISSIVE**: for TEXTURE layers, ramp the float images into
  8-bit sRGB PNG images (numpy over `pixels.foreach_get`).
- Export: `extract_scene(..., presentation=Presentation(...))`, a small
  dataclass in `geotools/blender/export.py`. It overrides what the exporter
  writes (the COLOR_0 source attribute, base-colour or emissive textures, and
  the base colour factor for `REPLACE`) without changing the user's materials
  or render colour attribute.
- Variant publish: the build profile gets `variant_layer` (a layer name, empty
  meaning the plain tileset). The output name gets `.<layer>` before `.3tz`
  (`site.3tz` becomes `site.slope.3tz`), with its own staging directory. Same
  placement, same checks, and the variant is recorded in `build.json`.

A variant is an ordinary 3D Tiles archive: no values sidecar or other
non-standard output (the values themselves wait for the M8 metadata).

### Settled while building stream 4

**Ramps** (`core.ramps`, also `PALETTES`' companions):

- Each palette is a compact table of sRGB stops, low end first, evenly spaced,
  sampled from the published palette (matplotlib viridis, magma and cividis at
  10 points; Google Turbo at 17 points from its polynomial with the lookup
  table's end points; ColorBrewer RdBu with 11 classes, **red low, blue high**;
  `greys` **black low, white high**). Values are interpolated linearly in sRGB
  between neighbouring stops, as matplotlib does with listed colormaps.
- `apply_ramp` clamps values outside `[vmin, vmax]` to the ends. NaN, ±inf and
  `nodata` (compared in the values' own precision, so a float32 attribute
  matches `float32(nodata)`) get `nodata_rgba`; other values are opaque. With
  `vmin == vmax` values below get the low end, above the high end and equal
  ones the middle. `palette` may also be a `(K, 3)` stop table.
- `apply_categories(values, colours, *, nodata=None, nodata_rgba=(0,0,0,0))`:
  value `i` gets `colours[i]` (`(C, 3)` or `(C, 4)` bytes); no-data,
  non-integral and out-of-range values get `nodata_rgba`.
- `auto_range(..., *, max_samples=1 << 22)` subsamples longer inputs with an
  even stride (deterministic) and raises `ValueError` when no value is left.
- `ramp_stops(palette, vmin, vmax, n=11)` gives `n` legend entries
  `{"value", "rgba"}` whose colours are exactly `apply_ramp`'s.
- `default_category_colours(count)`: Tableau 10, repeated.

**`geotools/core/presentation.py`** (new, bpy-free): `Target` and `Blend`
(`StrEnum`s), `PRESENT_ATTRIBUTE = "gt_present"`, `targets_for(domain)` (FACE
and CORNER: COLOR_0; TEXTURE: BASE_COLOR or EMISSIVE),
`variant_output(output, layer)` (`site.3tz` -> `site.slope.3tz`, so the staging
directory is `site.slope.geotools`), `presentation_issues(spec, target,
palette, range_min, range_max)` (`variant-target-invalid`,
`variant-palette-invalid`, `variant-range-invalid`) and `variant_record(...)`.

**Targets and blend modes**, as published (glTF core PBR only):

| target | REPLACE | MULTIPLY |
|---|---|---|
| COLOR_0 | base colour factor white, no base-colour texture; COLOR_0 is the ramp | the material is kept; COLOR_0 multiplies its base colour |
| BASE_COLOR | base-colour texture is the ramp, factor white, COLOR_0 dropped | base-colour texture is the ramp; the factor and COLOR_0 are kept and multiply it |
| EMISSIVE | emissive texture is the ramp (factor 1); base colour **black** with no texture, metallic 0 and no metallic-roughness texture; COLOR_0 dropped, so the ramp shows unlit | emissive texture is the ramp (factor 1), added on top of the unchanged material |

EMISSIVE + REPLACE uses a black base rather than white: glTF adds emission to
the lit base colour, so a white base would wash the ramp out. glTF has one
base-colour texture slot, so BASE_COLOR + MULTIPLY replaces the user's
texture and keeps only its factor.

**Storage** (`properties.py`): `GeoToolsLayerPresentation` also has
`nodata_color` (sRGB, default mid grey; presentations are always opaque),
`category_colors` (a collection of `GeoToolsCategoryColor.color`, item `i` for
value `i`; missing items use `default_category_colours`) and `images` (the
8-bit presentation images, `GeoToolsLayerImage` items, one per object). It is
`GeoToolsLayer.presentation`. A BOOLEAN layer ramps over `(0, 1)` when its
range is automatic.

**Writing** (`geotools/blender/presentation.py`): `write_presentation(scene,
layer_name) -> (Presentation, variant_record)`.

- COLOR_0: `gt_present` (`BYTE_COLOR`, CORNER) on every source mesh, written
  with `foreach_set("color_srgb")`; a FACE layer is repeated over each face's
  corners. A mesh without the layer's attribute is painted with the no-data
  colour (a `variant-data-missing` warning). Adding a colour attribute makes
  Blender set it active and render, so the previous active and render names
  are put back. An existing `gt_present` of another type or domain is replaced.
- TEXTURE: one 8-bit sRGB image per object (`gt_present.<layer>.<object>`, the
  size of the float layer image, alpha 255), from the first channel of the
  float image. They are generated datablocks, not files: the exporter writes
  them as PNG like any other unsaved image, and each variant publish writes
  them again.
- The user's materials, images and colour attributes are never changed. The
  plain export never publishes `gt_present`, even when it is the render or
  active colour attribute: COLOR_0 then comes from the next one.
- Operators: `geotools.layer_write_presentation` (write now, to look at it in
  the viewport) and `geotools.layer_presentation_reset_colors`.

**Export** (`blender/export.py`):

```python
@dataclass(frozen=True, slots=True, eq=False)
class Presentation:
    target: Target
    blend: Blend
    attribute: str = "gt_present"                     # COLOR_0 source
    images: Mapping[str, bpy.types.Image] = {}        # object name -> 8-bit image
    uv_map: str = ""                                  # "" = each object's render UV map

def extract_scene(context, collection, image_dir, *, remedies=None,
                  presentation: Presentation | None = None) -> tuple[SceneExport, dict[str, str]]
```

- COLOR_0 is read from `gt_present` on the **evaluated** mesh, so modifiers
  carry it like any corner attribute (Subdivision interpolates it). If a
  modifier drops it and the evaluated topology equals the original's (the
  topology digest is not `evaluated:`), it is read from the original mesh,
  whose corners are then the same. If it is dropped **and** the topology
  changed, `extract_scene` raises `ExportError` naming the object: apply the
  modifiers or bake the layer on the evaluated topology first.
- Texture targets give each object its own copy of each of its materials
  (named `<material> [<object>]`), with the object's image in the slot and its
  texture coordinates renumbered (still at most two UV maps); an empty slot
  gets a plain white material.

**Variant publish** (`blender/publish.py`): `geotools.publish` takes a
`layer` property (empty: the build profile's `variant_layer`). The Presentation
panel's **Publish Variant** button calls it with the selected layer and the
selected build profile, and shows the file name it writes. The variant runs
the same checks plus `variant_issues(scene, layer)`: `variant-layer-missing`,
`variant-layer-invalid`, the `presentation_issues` codes, `variant-edit-mode`,
`variant-data-mismatch`, `variant-no-data` (no source mesh holds the
attribute) and `variant-data-missing` (warning) for FACE and CORNER layers, and
`variant-image-missing` and `variant-uv-map-missing` for TEXTURE layers. An
automatic range over no values fails the publish (`PresentationError`). The
last-build summary on the profile then describes the variant.

**`build.json`**: schema stays 2, with a `"variant"` key: null for the plain
tileset, else (in this order)

```json
{"layer": "slope", "target": "COLOR_0", "blend": "REPLACE",
 "palette": "viridis",
 "range": {"min": 0.0, "max": 45.0, "auto_min": true, "auto_max": false},
 "nodata_rgba": [128, 128, 128, 255],
 "ramp_stops": [{"value": 0.0, "rgba": [68, 1, 84, 255]}, ...]}
```

For a CATEGORY layer `palette` and `range` are null and `ramp_stops` is
replaced by `"categories": [{"value": 0, "label": "bare", "rgba": [...]}, ...]`.
`range` is the range actually used; `auto_min`/`auto_max` say which ends came
from `auto_range`. Colours are 8-bit sRGB RGBA. `ramp_stops` has 11 entries.

## Exit (from the roadmap)

- A Geometry Nodes result is baked in both spaces (Blender tests: same
  topology, changed topology with each transfer method, and texture bake
  directly and selected-to-active).
- The layer survives saving and reopening the `.blend` (Blender test: save to
  a temporary file, `open_mainfile`, check the registry, attributes and
  images).
- The variant tileset shows the analysis in Cesium (an end-to-end Blender test
  that publishes `site.<layer>.3tz` and decodes COLOR_0 or the texture from a
  leaf, plus a visual check by the user).

## Work streams

1. **Registry** (first; the others build on it): `core/layers.py`,
   `blender/layers.py`, properties, Layers panel, persistence test,
   `build.json` schema 2.
2. **Vertex bake**: `blender/bake_vertex.py`, `core/transfer.py`.
3. **Texture bake**: `blender/bake_texture.py`, `core/uv_overlap.py`, the
   `uv-overlap` check.
4. **Presentation and variants**: `core/ramps.py`, `Presentation` in export,
   the variant publish.

Streams 2 to 4 run in parallel once stream 1 has merged into `develop`.
