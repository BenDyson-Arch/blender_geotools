# M3 design: layers and the bake engine

Status: agreed design for M3 (see [ROADMAP.md](../ROADMAP.md#m3-layers-and-bake-engine)).
The interfaces below are shared by every M3 work stream. Change them here
first, then in code.

## Goal

A **layer** is a named, typed value that lives on the source: one value per
face, per face corner, or per texel. Layers come from Geometry Nodes, from
analysis tools (M5) or from the user. M3 provides:

1. the **layer registry**, stored in the `.blend`;
2. **layers from Geometry Nodes**: a FACE or CORNER layer reads its attribute
   from the evaluated mesh, so a Store Named Attribute output is a layer
   without any bake (the vertex-space bake was removed; section 2 says why);
3. **texture-space bakes**: a Cycles bake of an attribute into an image on the
   source UVs, after a UV-overlap check;
4. a **presentation** step that makes a layer visible (a colour ramp into
   COLOR_0, or an 8-bit texture in the base-colour or emissive slot) and
   publishes it as a **variant tileset** such as `site.slope.3tz`.

rusty-tiles 0.4.0 publishes no custom attributes, so until M9 a layer reaches a
tileset only through presentation. The registry still records everything the
0.5 annotate pass will need (units, no-data, aggregation).

## Rules that apply to every work stream

- The performance rule holds: no Python loops over faces, corners, vertices or
  texels. Use `foreach_get`/`foreach_set` with numpy, Geometry Nodes or Cycles.
  Building a node tree or a material from Python is fine.
- `geotools/core` never imports `bpy`. Put maths (ramps, colour conversion,
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
- FACE and CORNER layers live in a mesh attribute of every source object,
  with `FACE` or `CORNER` domain and `FLOAT`, `INT` or `BOOLEAN` attribute
  type (CATEGORY is `INT`). The attribute is the layer's `source_attribute`
  (`LayerSpec.source_attribute`, `GeoToolsLayer.source_attribute`, shown as
  **Attribute** in the Layers panel), or `gt_<name>` when that is empty:
  `core.layers.effective_attribute_name(spec)` resolves it, and
  `attribute_name(spec)` stays the default. So a Geometry Nodes output named
  `slope` can back a layer directly. `validate` refuses a source attribute
  with surrounding spaces, a leading dot (Blender's internal names), the
  reserved `gt_present` or more than 63 bytes (`layer-attribute-invalid`).
- Values are **read from the evaluated mesh**, the one the exporter publishes:
  `blender.layers.layer_meshes(scene)` yields each source object with
  `obj.evaluated_get(depsgraph).data` (its modifiers and Geometry Nodes
  applied), or its original mesh when the active build profile's **Apply
  Modifiers** is off. `update_value_range`, `layer_status`, the presentation
  and the export all read this way. Values are **written to the original
  mesh** (`write_layer`, `ensure_attribute`, Create Empty Attributes), for
  manual layers and tools; modifiers carry the attribute on like any other.
- Panels never evaluate a mesh. A layer's data checks are cached on it
  (`status_issues`, `status_key`): `refresh_layer(scene, item)` updates the
  range and the cached status, and the layer operators, Run Checks and
  Publish (through Run Checks, `refresh_layers`) call it. `cached_status`
  is what panels draw: validation now plus the cached data issues, or one
  `layer-not-checked` info when the layer was never checked or its
  `status_key` (domain, type, attribute, UV map, source collection, Apply
  Modifiers) changed since.
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
  - `layer_status` codes: `layer-attribute-missing` (warning, on the evaluated
    mesh; the message says to add a Geometry Nodes Store Named Attribute or
    create the attribute), `layer-attribute-mismatch` (error),
    `layer-edit-mode` (warning: an object in Edit Mode is not checked),
    `layer-image-missing` (warning), `layer-image-unsaved` (warning),
    `layer-uv-map-missing` (error), `layer-no-source` (info), plus the
    validation codes. `cached_status` adds `layer-not-checked` (info).
  - Operators: `geotools.layer_add`, `layer_remove` (`delete_data`; image files on
    disk are kept), `layer_rename` (renames the `gt_<name>` attributes too; a
    layer with a source attribute keeps it), `layer_create_attributes`,
    `layer_set_categories`, `layer_update_range` (reads the evaluated values
    and refreshes the cached status).
  - Publish passes the records in `BuildRequest.layers`; invalid layers are left
    out with a warning.
- UI: a **Layers** panel (`GEOTOOLS_PT_layers`) after Source, with a UIList,
  add, remove and rename operators, the selected layer's label, units,
  attribute (FACE and CORNER), value range and cached status
  (`cached_status`), and three sub-panels in workflow order: **Bake**
  (`GEOTOOLS_PT_layers_bake`, TEXTURE layers only, section 3), **Display**
  (`GEOTOOLS_PT_layers_display`, section 4) and **Details**
  (`GEOTOOLS_PT_layers_details`, closed by default: domain, type, no-data,
  origin, aggregation, description, provenance and Create Attributes).
  Removing a layer asks whether to delete its attributes and images too.
  `update_value_range` also caches the automatic ramp range (`auto_min`,
  `auto_max`) for the Display sub-panel.
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
    `"EPSG:nnnn"` when `source_crs` is `EPSG:nnnn` (`crs_catalog.epsg_code`),
    else null. `source_crs` stays as the fallback. `"crs_operation"` (added
    with the EPSG picker) is rusty-tiles' `crsOperation` from the conversion
    report: the EPSG datum operation and its stated accuracy, or null.
  - `rusty_tiles_version` as before (the wheel exposes no commit).

## 2. Layers from Geometry Nodes (no vertex-space bake)

M3 first had a vertex-space bake, `geotools.bake_geonodes_layer`, which froze
a Geometry Nodes output into the layer's `gt_<name>` attribute on the
original mesh and, when the node tree changed the topology, transferred the
values back to the source faces (nearest, raycast or area mean). It was
removed: it redid what Blender already does (the "Scope" row of the
roadmap's decisions). Freezing a result is Apply Modifier; moving values
between topologies is a Geometry Nodes sampling setup (Sample Nearest,
Raycast, Sample Index). And the export already reads every object through
the evaluated depsgraph, which is applying the modifiers at publish time.

To compute a layer with Geometry Nodes:

1. Add a Geometry Nodes modifier to the source objects and end the tree with
   a **Store Named Attribute** node of the layer's domain (Face or Face
   Corner) and type (Float, Integer, Boolean; Integer for a category layer).
2. Name the attribute `gt_<name>`, or type the node's name in the layer's
   **Attribute** field (e.g. `slope`).
3. **Update** the layer's values (or Run Checks): the range and status are
   read from the evaluated mesh. Publish applies the modifier, as the export
   always has.

The tree may change the topology (Subdivide Mesh, Delete Geometry): the layer
then lives on the evaluated faces, which is what is published, and the face
map records those objects with an `evaluated:` topology (see
`export.py`). With the build profile's **Apply Modifiers** off, the export and
every layer read use the original mesh data instead, as Blender's own glTF
exporter does, so only attributes stored on the mesh count.

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
  rusty-tiles records overlap but never refuses it). For a TEXTURE layer the
  **Bake** sub-panel draws `bake_texture.draw_bake`: the UV map, resolution
  and margin, **Bake...** (the operator's dialog) and **Check UV Overlap**
  (`geotools.check_layer_uvs`).
- Operator properties: `layer` (empty for the selected one; must be TEXTURE),
  `source_layer` (a FACE or CORNER layer, baked from its attribute,
  `effective_attribute_name`), `attribute`
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

- **COLOR_0**: for FACE and CORNER layers, the ramp of the values on the
  exported (evaluated) meshes, computed at publish time and handed to the
  exporter in memory (see "Building" below).
- **BASE_COLOR and EMISSIVE**: for TEXTURE layers, ramp the float images into
  8-bit sRGB PNG images (numpy over `pixels.foreach_get`).
- Export: `extract_scene(..., presentation=Presentation(...))`, a small
  dataclass in `geotools/blender/export.py`. It overrides what the exporter
  writes (the COLOR_0 colours, base-colour or emissive textures, and the base
  colour factor for `REPLACE`) without changing the user's materials
  or render colour attribute.
- Variant publish: the build profile gets `variant_layer` (a layer name, empty
  meaning the plain tileset). The output name gets `.<layer>` before `.3tz`
  (`site.3tz` becomes `site.slope.3tz`), with its own staging directory. Same
  placement, same checks, and the variant is recorded in `build.json`.

A variant is an ordinary 3D Tiles archive: no values sidecar or other
non-standard output (the values themselves wait for the M9 metadata).

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

**Building** (`geotools/blender/presentation.py`): `build_presentation(scene,
layer_name) -> (Presentation, variant_record)`, called by the variant publish.

- COLOR_0: the layer's values are read from the mesh each source object
  exports (`layers.layer_meshes`: evaluated, unless Apply Modifiers is off)
  and turned into one 8-bit sRGB colour per face (FACE layer) or per corner
  (CORNER layer), kept in memory in `Presentation.colors` (object name ->
  `(N, 4) uint8`). The exporter converts them to linear floats
  (`core.ramps.srgb_bytes_to_linear`, Blender's own byte-colour conversion)
  and indexes them by each exported corner's polygon or corner. No colour
  attribute is written, so nothing has to survive the modifiers: a layer
  computed entirely by a Geometry Nodes modifier, even one that changes the
  topology, presents like a stored one. An object without the layer's
  attribute gets the no-data colour (a `variant-data-missing` warning); the
  automatic range is taken over the evaluated values of every object.
- Viewport preview: `write_preview(scene, layer_name) -> (variant_record,
  skipped)` (the Display sub-panel's **Write Colours**) paints the same
  colours into `gt_present` (`BYTE_COLOR`, CORNER) on each original mesh,
  with `foreach_set("color_srgb")`, a FACE layer repeated over each face's
  corners. It can only do so where the evaluated topology is the original's
  (`export.same_topology`) or Apply Modifiers is off; other objects are
  skipped and named in a warning, since no original corner matches each
  exported one. Adding a colour attribute makes Blender set it active and
  render, so the previous active and render names are put back. An existing
  `gt_present` of another type or domain is replaced. The export never reads
  `gt_present`: a stale, missing or modifier-dropped preview changes no
  tileset.
- TEXTURE: one 8-bit sRGB image per object (`gt_present.<layer>.<object>`, the
  size of the float layer image, alpha 255), from the first channel of the
  float image. They are generated datablocks, not files: the exporter writes
  them as PNG like any other unsaved image, and each variant publish writes
  them again.
- The user's materials, images and colour attributes are never changed. The
  plain export never publishes `gt_present`, even when it is the render or
  active colour attribute: COLOR_0 then comes from the next one.
- Operators: `geotools.layer_write_presentation` (`write_preview`, to look at
  it in the viewport) and `geotools.layer_presentation_reset_colors`.

**Export** (`blender/export.py`):

```python
@dataclass(frozen=True, slots=True, eq=False)
class Presentation:
    target: Target
    blend: Blend
    colors: Mapping[str, np.ndarray] = {}             # COLOR_0: object name -> (N, 4) uint8 sRGB
    domain: str = "FACE"                              # COLOR_0: rows per FACE or per CORNER
    nodata_rgba: tuple[int, int, int, int] = (0, 0, 0, 255)  # objects without colours
    images: Mapping[str, bpy.types.Image] = {}        # object name -> 8-bit image
    uv_map: str = ""                                  # "" = each object's render UV map

def extract_scene(context, collection, image_dir, *, remedies=None,
                  presentation: Presentation | None = None,
                  apply_modifiers: bool = True) -> tuple[SceneExport, dict[str, str]]
```

- COLOR_0 comes from `Presentation.colors`, never from a mesh attribute.
  Colours whose count is not the exported mesh's faces or corners raise
  `ExportError` (the mesh changed after the presentation was built); the
  publish builds the presentation just before extracting, from the same
  depsgraph, so this only guards against misuse.
- `apply_modifiers=False` (the build profile's **Apply Modifiers**, default
  on, in Build Settings) exports `obj.to_mesh()` of the original object, its
  mesh data without modifiers, with the evaluated transform. Every face map
  entry is then the plain topology digest. `build.json` records the choice as
  `geotools.apply_modifiers`.
- Texture targets give each object its own copy of each of its materials
  (named `<material> [<object>]`), with the object's image in the slot and its
  texture coordinates renumbered (still at most two UV maps); an empty slot
  gets a plain white material.

**Variant publish** (`blender/publish.py`): `geotools.publish` takes a
`layer` property (empty: the build profile's `variant_layer`). In the UI the
variant is chosen in one place, the **Variant** field of the selected build
profile in the Publish panel, whose **Publish** button names the file it
writes; the `layer` property stays for scripts. The Display sub-panel says
when its layer is the profile's variant. The variant runs
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
 "palette": "viridis", "scale": "LINEAR",
 "range": {"min": 0.0, "max": 45.0, "auto_min": true, "auto_max": false},
 "nodata_rgba": [128, 128, 128, 255],
 "ramp_stops": [{"value": 0.0, "rgba": [68, 1, 84, 255]}, ...]}
```

`scale` is the presentation's **Scale**, `LINEAR` or `LOG` (added with M5
hydrology): a log ramp spaces colours by `log10` between positive ends, and its
automatic range runs from the 2nd percentile of the positive values to the
largest; `ramp_stops` are then evenly spaced in `log10`. For a CATEGORY layer
`palette`, `scale` and `range` are null and `ramp_stops` is
replaced by `"categories": [{"value": 0, "label": "bare", "rgba": [...]}, ...]`.
`range` is the range actually used; `auto_min`/`auto_max` say which ends came
from `auto_range`. Colours are 8-bit sRGB RGBA. `ramp_stops` has 11 entries.

## Exit (from the roadmap)

- A Geometry Nodes result reaches a tileset: as a FACE layer read from the
  evaluated mesh (Blender test: Store Named Attribute after Subdivide Mesh,
  its range, status, COLOR_0 in the GLB and a validated variant publish), and
  baked into a texture layer directly and selected-to-active.
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
2. **Vertex bake**: built, then removed (section 2); layers read the
   evaluated mesh instead.
3. **Texture bake**: `blender/bake_texture.py`, `core/uv_overlap.py`, the
   `uv-overlap` check.
4. **Presentation and variants**: `core/ramps.py`, `Presentation` in export,
   the variant publish.

Streams 2 to 4 run in parallel once stream 1 has merged into `develop`.
