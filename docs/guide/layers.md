# Layers and bakes

A **layer** is a named value on the source: one per face, one per face corner,
or one per texel of an image on the source UVs. Slope, sun hours, a phase
number or a material class are all layers. GeoTools keeps the list of layers
in the `.blend`, with each layer's units, no-data value and value range.

What a layer does today:

- it can be shown as colours and published as a **variant tileset**, such as
  `site.slope.3tz`;
- it is written as glTF metadata in the [GLB with metadata](glb-metadata.md).

rusty-tiles 0.4 cannot carry metadata into tiles, so a variant is the way to
see a layer in a 3D Tiles viewer. Queryable layers in the tiles themselves come
with rusty-tiles 0.5 (see the [roadmap](../ROADMAP.md)).

## Add a layer

1. In the **Layers** panel, press **+** (**Add Layer**).
2. Give it a **Name**: lowercase letters, digits and underscores, such as
   `phase` or `slope`.
3. Choose its **Domain**: **Face**, **Corner** or **Texture**.
4. Choose its **Type**: **Float**, **Integer**, **Boolean** or **Category**.
5. Press **Add**.

The analysis tools and presets add their own layers, so you often skip this.

Under the list, the selected layer shows:

- **Label** and **Units**: the label is the name shown in the list, and both
  are written into the [GLB metadata](glb-metadata.md);
- **Attribute** (face and corner layers): the mesh attribute the layer reads,
  `gt_<name>` unless you type another;
- **Values**: the range of values present. The refresh button (**Update
  Range**) reads it again from the evaluated meshes;
- **Categories** (category layers): the labels of the values 0, 1, 2 and so
  on. The pencil button (**Set Categories**) edits them;
- what is missing, for example an object without the attribute.

**-** removes the layer. Its dialog can also delete the layer's attributes and
images (**Delete Attributes and Images**). The pencil next to the list renames
it.

## Face and corner layers

A face or corner layer reads its attribute from the **evaluated** mesh: the
mesh with its modifiers and Geometry Nodes applied, as publishing exports it.
So a Geometry Nodes tree that ends in **Store Named Attribute** computes a
layer with no bake step, even if it changes the topology.

1. Add a Geometry Nodes modifier to the source mesh.
2. Store the value with **Store Named Attribute**, named `gt_<layer name>`,
   on the Face or Face Corner domain.
3. Or store it under any name, and type that name in the layer's
   **Attribute** field.

You can also paint or set the values by hand. **Create Empty Attributes** in
the **Details** sub-panel adds the attribute, filled with the no-data value,
to every source object that lacks it.

The [analysis presets](analysis.md#presets) are ready-made Geometry Nodes
trees of this kind.

## Texture layers and the Bake sub-panel

A texture layer holds one value per texel, in a float image per object. The
images are OpenEXR files in `geotools_layers/` next to the `.blend`, so save
the `.blend` first.

The **Bake** sub-panel appears for a texture layer:

- **UV Map**: the UV map to bake onto. Empty means the active render UV map;
- **Resolution**: the image size in pixels (1024 by default);
- **Margin**: how many pixels the bake extends past each UV island (8 by
  default), so coarse levels and mipmaps do not pull in empty texels.

Press **Bake...** and choose what to bake:

- **Source Layer**: a face or corner layer, or
- **Attribute**: any named attribute of the evaluated mesh, such as a Geometry
  Nodes output.

**Method** is **Automatic** by default: each object is baked directly when its
evaluated mesh has the same topology, and from a copy of the result
(**Selected to Active**) when Geometry Nodes changed the topology. The bake is
a one-sample Cycles emission bake. Your materials, modifiers, render settings
and selection are put back afterwards, also when the bake fails. Texels that
no triangle covers, and NaN or infinite values, get the layer's no-data value.

Overlapping UVs would bake two places into one texel. **Check UV Overlap**
finds them with the same rule rusty-tiles uses for texture ownership. A bake
refuses overlapping UVs unless you tick **Bake Anyway**; the last triangle
baked then wins.

## Show a layer: the Display sub-panel

The **Display** sub-panel sets how a layer looks in its variant tileset. It
never changes your materials, images or colour attributes.

For numbers:

- **Palette**: Viridis, Magma, Cividis, Turbo, Diverging Red-Blue or Greys;
- **Scale**: **Linear**, or **Log** for values that span decades, such as flow
  accumulation;
- **Minimum** and **Maximum**: the ramp's range. Empty means automatic: the
  2nd to the 98th percentile of the values;
- **No-Data Colour**.

For a category layer, one colour per category, under **Category Colours**.

Where the colours go:

- Face and corner layers go into the tileset's vertex colours.
- For a texture layer, **Target** picks **Base Colour** or **Emission**: the
  colours become an 8-bit texture in that slot.
- **Blend**: **Replace** shows the colours alone. **Multiply** keeps the
  material and tints it.

**Write Colours** previews the result in Blender, in a `gt_present` colour
attribute or 8-bit images. Publishing works the colours out again, so this is
only a preview.

## Publish a layer as a variant

1. Select a build profile in the **Publish** panel.
2. Set **Variant** to the layer.
3. Press **Publish**.

The variant is written beside the main archive, as `<output>.<layer>.3tz`,
for example `site.slope.3tz`. It is an ordinary 3D Tiles archive with the same
placement and checks, and its own `site.slope.geotools/build.json`, which
records the palette, range and legend stops, or the category colours. See
[Checks and publishing](publish.md).

## Details

The **Details** sub-panel holds the fields you rarely change:

- **Identifier**: the layer's name;
- **Domain** and **Type**;
- **No-Data**: the value that means "no value";
- **Origin**: **Geometry Nodes**, **Analysis** or **Manual**;
- **Aggregation**: how coarse levels of detail will combine values (**Default**
  is the area-weighted mean for numbers and the most common value for booleans
  and categories). It is stored now for rusty-tiles 0.5, which will use it;
- **Description**;
- which tool made the layer, from its provenance.

## Limits

- Layer values reach 3D Tiles only as colours (variants) until rusty-tiles 0.5.
  The [GLB with metadata](glb-metadata.md) carries the values themselves.
- A texture layer needs UVs, and its images live next to the saved `.blend`.
- A float layer without a no-data value has no explicit "no value". Set one.

Design details: [design/m3-layers.md](../design/m3-layers.md).
