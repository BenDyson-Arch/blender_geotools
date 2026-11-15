"""Publishing, bpy-free: everything after the Blender scene has been read.

- :mod:`.staging`: the staging folder beside an output (``site.geotools/``), its
  lock and file names. The folder is GeoTools' own record, never a published
  format.
- :mod:`.job`: the worker thread that writes ``source.glb`` and the face map,
  runs rusty-tiles (or writes the GLB with metadata) and validates, and the
  ``build.json`` record.
- :mod:`.vector`: vector 3D Tiles from line and point objects.
- :mod:`.web`: Extract for Web, a servable folder with ``viewer.html``.
- :mod:`.preview`: ``rusty-tiles preview`` on an extracted copy.

The Blender side (operators, the progress timer, the panels) is
``geotools.blender.publish``.
"""
