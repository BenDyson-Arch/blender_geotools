# Texture cleanup

Photogrammetry textures often need cleaning before you publish: black holes
where no photo saw the surface, a person or a vehicle in a trench, a scale bar
on the ground, or atlas pages at different exposures.

Painting stays Blender's: its brushes, clone, smear, soften and masks. GeoTools
adds what Blender lacks, in the **Textures** panel between Layers and Checks.
The full page is [Texture cleanup](../texture-cleanup.md). In short:

1. Select the textured mesh. The panel works on the base colour image of its
   active material, or on the image you choose at the top of the panel.
2. **Paint** sub-panel: **Paint Texture** sets up Blender's Texture Paint mode
   for the source texture. With **Protect Overlaps** on, texels that two places
   on the model share become a stencil, so one stroke cannot change two places.
3. **Cleanup** sub-panel:
   - **Fill Holes** fills black or transparent texels inside the UV islands
     from their surroundings;
   - **Remove Masked** fills an area you marked with a key colour or a mask
     image (**New Mask Image** makes one), such as a person;
   - **Pad Islands** extends each UV island's colours outwards, so coarse
     levels and mipmaps do not bleed in the background.
4. **Balance** sub-panel: **Fit Balance** matches the exposure of atlas pages
   across the seams between them, as a preview. **Apply Balance** writes it to
   the pixels; **Clear Balance** drops it.
5. After a GeoTools tool runs, the top of the panel offers to undo it (the
   button reads, for example, **Undo Fill Holes**). It puts back the images
   that tool changed. There is one level of undo.
6. Run Checks and publish. Publish saves every edited image first, and the
   Publish panel says how many textures changed since the last publish.

**Limits.**

- Fill Holes and Remove Masked are smooth fills: they blur, and they do not
  invent texture. Use Blender's Clone brush for detail.
- Every publish is a full retile, so a texture change rebuilds every tile.
- A JPEG texture saved again is re-compressed. Paint on PNG textures where you
  can.
