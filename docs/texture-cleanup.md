# Texture cleanup (M6)

Photogrammetry textures often need some cleaning before you publish them: black
holes where no photo saw the surface, a person or a vehicle in a trench, a scale
bar left on the ground, or pages of an atlas that came out at different
exposures. Blender 5.2 already has a full set of texture painting tools.
GeoTools only adds what Blender lacks and what publishing needs. Everything it
adds is in the **Textures** panel of the GeoTools sidebar, between Layers and
Checks.

| Task | Who does it |
|---|---|
| Painting, clone and patch, smear, soften, masks | Blender (recipes below) |
| One-click paint setup, with UV overlaps masked | GeoTools: **Paint Texture** |
| Fill black or transparent holes inside UV islands | GeoTools: **Fill Holes** |
| Remove a masked person, vehicle or scale bar | GeoTools: **Remove Masked** |
| Pad UV islands outwards (dilation) | GeoTools: **Pad Islands** |
| Match exposure across atlas pages | GeoTools: **Fit Balance**, then **Apply Balance** |
| Save edited textures, and say what changed since the last publish | GeoTools: **Publish** and **Run Checks** |

All the examples and test images in GeoTools are synthetic. Use your own data
to try the tools.

## Workflow

1. **Paint Texture.** Select the mesh, open **Textures > Paint** and click
   **Paint Texture**. GeoTools:
   - enters Texture Paint mode in single-image mode, with the base colour
     image of the active material as the canvas. It paints on the UV map that
     image uses, so it makes that map the active one;
   - turns on projection paint occlusion, backface culling and normal falloff,
     so a stroke never reaches through the model, and keeps at least 2 px of
     seam bleed;
   - sets every 3D view to Solid shading with Flat lighting and Texture colour,
     so you see the texture without any lighting on it.

   These are ordinary Blender settings. Change them as you like afterwards.
   To paint a different image, choose it in the **Image** field at the top of
   the panel.

2. **Watch for UV overlaps.** Overlapping UV islands share texels, so painting
   one place also changes another place on the model, or on another object
   that uses the same image. Paint Texture rasterises every face that uses the
   image, with the same rule as the `uv-overlap` check. If some texels are used
   twice, it warns you and writes the image `GeoTools overlaps <image>`: black
   where texels overlap, white elsewhere. With **Protect Overlaps** on (the
   default), that image becomes the paint stencil (Texture Paint > Masking >
   Stencil Mask). Strokes then cannot change the overlapping texels, and the
   viewport's mask overlay shows where they are. Turn the stencil off in
   Blender's Masking panel when you mean to paint there.

3. **Clean** with Blender's brushes (see the recipes) and the GeoTools tools
   (see below). Each GeoTools tool marks the image as edited.

4. **Run Checks.** Run Checks now also reports:
   - `image-unsaved` (warning): an image has edits that are not saved
     anywhere. The message says where Publish will save them.
   - `textures-changed` (note): which textures changed since the selected
     build profile's last publish.
   - `texture-balance-preview` (error): a material still shows the colour
     balance preview. Apply or Clear it first, otherwise the published
     material would lose its texture.

5. **Publish.** The Publish panel shows **N textures changed since last
   publish**, with the first few names and the reason for each: unsaved edits,
   changed, new, or no longer used. Publish saves every edited source image
   before it writes the source GLB, so a painted texture is never lost or
   published stale:
   - an image packed into the `.blend` is packed again from its current pixels;
   - an image with a file is saved to that file, as Blender's Image > Save
     does. A JPEG file is saved as JPEG again, which is lossy, so paint on PNG
     textures where you can;
   - an image with neither (a generated image you painted) is saved as
     `//textures/<image>.png` next to a saved `.blend`. If the `.blend` has
     never been saved, the image is packed into it instead.

   `build.json` then records, under `source.images`, each source image's name,
   whether it came from a file or was packed, and the SHA-256 of those bytes.
   The next comparison starts from this record.

**Publishing means a full retile.** rusty-tiles 0.4 has no incremental update
for meshes, so a texture change rebuilds every tile. That takes as long as the
first publish did. Coarse levels pick up the change as well as leaves. The
GeoTools tests check this: a quarter of the texture is repainted, then
republished, and both leaf tiles and coarse tiles get new texture bytes, while
leaves away from the painted area keep theirs. A rebuild that changes only the
appearance waits for rusty-tiles' appearance-only rebuild ("B",
[rusty-tiles #331](https://github.com/BenDyson-Arch/rusty-tiles/issues/331),
GeoTools M10).

## Recipes: Blender's own tools

### Clone and patch across UV islands

Use Blender's **Clone** brush in the 3D view. In projection painting it samples
the surface through the view, not the UV layout. So it copies across UV
islands, seams and even pages, wherever the two places sit in the atlas.

1. Click **Paint Texture**, then pick the **Clone** brush (Brush asset
   "Clone", brush type `CLONE`).
2. Leave the checkbox in the header of the brush's **Clone** panel (Clone
   Map) off. The source is then the 3D cursor. **Ctrl+click** on good surface
   to place it there.
3. Paint over the damage. Blender copies from the source, keeping the offset
   between where you first click and the cursor. View the surface square-on,
   so the reprojection does not stretch it.
4. To patch from another photo or capture of the same model, turn on
   **Clone Map**: choose the other image as the clone image and its UV map as
   the clone source map. Blender then copies texel by texel between the two
   UV layouts.

Use the Clone brush for anything with structure: stone courses, brick, tool
marks, section drawings. The GeoTools fill does not invent texture.

### Smear, soften and seams

- **Soften** blurs a hard edge, for example where two photos meet.
  **Smear** drags colour along a stroke.
- **Bleed** (Texture Paint > Options, in pixels) carries strokes past UV
  island edges, so seams painted in 3D do not show a line.
- To limit painting to selected faces, select them in Edit Mode, then turn on
  the face selection mask (Paint Mask) in Texture Paint.

### Masks

- **Stencil Mask** (Texture Paint > Masking): an image that limits painting, white paints
  and black protects. The GeoTools overlap mask uses this.
- **Mask** brush and Paint Mask: these are also Blender's own tools.

### Heavy retouching in another editor

**Image > Quick Edit** (`paint.image_from_view`, then `paint.project_image`)
renders the current view to an image, opens it in your image editor (GIMP,
Krita, Photoshop) and projects the edited result back onto the texture.

## GeoTools tools

All of them work on the image named at the top of the Textures panel. If that
field is empty, they use the base colour image of the active object's active
material. Where the tools need UV islands, they rasterise the faces that use
the image, in every source object, on the UV map each image node reads.

### Fill Holes

This fills texels that hold no data: black in every colour channel (at or below
**Black Level**, in 8-bit levels), or with zero alpha, or both. It only fills
texels inside UV islands. The background between islands is left alone, and it
is never read. **Grow** first widens each hole by a few texels, to cover the
soft rim many photogrammetry tools leave.

The fill is **push-pull** (pyramid) interpolation:

1. The known texels are averaged down an image pyramid, using only texels
   inside islands that are not holes.
2. Each hole texel then takes a smooth blend of the known texels around it, at
   the finest level that has any.

The cost is about one pass over the image, whatever the size of the hole, and
only the area around the holes is processed.

Limits:

- It is a diffusion fill. It is smooth and blurs: a hole comes back as a soft
  blend of its rim, with no texture detail in it.
- It is exact for a flat colour and close for a gradient. In the tests, a
  44-texel hole in a ramp comes back within 4% of the ramp's range at worst,
  and within 1% on average.
- At an island border, a hole is filled from one side only, so a gradient does
  not continue across it.
- Holes are filled from whatever island texels are near in texture space. A
  large hole at an island border can borrow colour from a neighbouring island
  that is not its neighbour on the model.

### Remove Masked

Removing people, vehicles or scale bars is the same fill over a mask that you
paint. There are two ways to make the mask:

- **Key Colour** (the default): paint over the object in the key colour
  (magenta unless you change it) directly on the texture, in Texture Paint.
  Use a hard brush (Falloff: Constant) so the edges are clean. The texels
  within **Tolerance** of the key colour are filled. **Grow** (2 texels by
  default) covers the brush's soft edge.
- **Mask Image**: **New Mask Image** makes a black image the size of the
  texture and sets it as the paint canvas. Paint white over what to remove,
  then click **Paint Texture** to go back to the texture.

Limits: the fill is diffusion, **not content-aware**. It does not search for
matching patches, as PatchMatch or Photoshop's Content-Aware Fill do. A removed
person leaves a smooth, blurred patch. That works on even ground such as
grass, sand or a trench floor. On structured surfaces it looks wrong, so use
the Clone recipe there, or remove the person with Quick Edit in an editor
that has a content-aware fill.

### Pad Islands

This pads UV islands outwards with their own edge colours, **Margin** texels
deep (16 by default). Each ring of background texels copies the colour of its
nearest island texel, so each padding texel continues its nearest island.
Where the paddings of two islands meet, each texel goes to the nearer island.
Texels inside islands are **never** changed.

Why you need it: coarse tiles and mipmaps average neighbouring texels, so
background colour (often black) bleeds into the island edges and shows up as
dark seams at a distance. Blender's bake **margin** does the same thing, but
only for images Blender bakes. It is a filter on the bake result, and it does
nothing for a texture that came from photogrammetry software.

**Fill Background** then fills every texel still outside the islands, with
the same push-pull fill. After that, even the coarsest mipmap averages no
background at all. 16 texels covers four mip levels. Use more for textures
that are seen mostly from far away.

### Balance pages

Atlas pages, or separate textures of one model, often come out at different
exposures, so a seam shows wherever two pages meet on the model. GeoTools
fits a **gain and offset per page and per colour channel**:
`colour' = gain × colour + offset`, in linear light.

- **Seams.** It finds every 3D edge where a face on one page meets a face on
  another. Edges are matched by position, so this works whether the mesh is
  welded or split at UV seams, and across separate objects.
- **Samples.** It takes 5 colour samples along each seam edge, on both sides,
  each moved 1.5 texels into its face so that no gutter texel is read. At
  most 200,000 samples are used.
- **Fit.** One page is the **Reference** and keeps its colours. By default
  this is the page with the most seam samples. The others are fitted to it
  by least squares. Seams that really do not match, for example where
  something was occluded, are down-weighted (Huber, re-weighted a few times).
  A page with no chain of seams to the reference is left as it is, and the
  fit says so.
- **Preview.** **Fit Balance** shows the correction in each page's materials
  with a shader node (`GeoTools balance preview`): `colour × gain + offset`.
  View it in Material Preview or Rendered shading. The panel lists each page's
  gain and offset, and the seam error before and after, in 8-bit levels of
  linear light.
- **Apply.** **Apply Balance** writes the correction into the pixels and
  removes the preview. **Clear Balance** removes the preview without touching
  any pixels.

Limits:

- It makes one correction for each whole page. It does not fix vignetting or
  exposure that varies within a page, and it is not a histogram match.
- It needs seams between pages. Pages that never meet on the model cannot be
  matched.
- A gain far from 1 on 8-bit pages stretches or crushes levels, so expect
  some banding in smooth areas.

### Undo

Every GeoTools tool keeps a copy of the pixels it changes. **Undo Cleanup**
(it appears at the top of the Textures panel as **Undo <tool>**) puts the
images back as they were before the last tool ran. There is one level of undo.

- Blender's Ctrl+Z does not undo pixel changes made outside paint strokes, so
  use Undo Cleanup instead.
- **Image > Reload** goes back to the saved file.
- The copy lasts for the session only, and costs memory: 256 MiB for an 8K
  8-bit image, and four times that for a float image.

## Performance

These timings come from `scripts/bench_texture_cleanup.py` in Blender 5.2.4 on
Linux (16 threads, on a shared machine). The test scene is two synthetic
8192 × 8192 pages and a 1.8M-triangle site cut into 48 × 48 UV islands per
page, with 82% of texels inside islands. The timings include Blender's own
pixel reads and writes: about 0.3 s to read an 8K image, and under 1 s
to write one back.

| Tool | 8K page |
|---|---|
| Paint Texture (rasterises 0.9M triangles at 8K for the overlap stencil) | 8.2 s |
| Fill Holes (167k texels: scattered single texels and a 300-texel hole) | 3.7 s |
| Remove Masked (103k texels, key colour) | 2.2 s |
| Pad Islands (16 texels: 12M background texels written) | 4.9 s |
| Fit Balance (2 pages, 3,360 seam samples) | 1.8 s |
| Apply Balance (one page) | 2.0 s |
| Undo Cleanup | 0.6 s |

Paint Texture rasterises the UVs of the faces that use the image, and that
raster is cached. Fill Holes, Remove Masked and Pad Islands reuse it while the
UVs are unchanged; the times above already reuse it.

Fill accuracy in the same run: the filled texels are within 2.1 levels (of
255) of the synthetic field on average, which has 2.6 levels of noise that no
fill can recover. In the 300-texel hole the mean is also 2.1 levels and the
maximum is 12 levels, where the blur shows most.

Fit accuracy: the darker page was made with a linear gain of 0.8. The fit
returns gains of 1.18 to 1.24 per channel (1.25 would be exact) and moves part
of the difference into the offset. This takes the seam error from 15.6 to 3.0
levels.
