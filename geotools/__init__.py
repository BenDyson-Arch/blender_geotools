"""GeoTools: GIS for Blender. Georeference a site, bring in geodata, analyse the 3D mesh, publish 3D Tiles.

Packages: :mod:`.core` (pure Python and numpy, never ``bpy``), :mod:`.analysis`
(the analysis maths, also bpy-free), :mod:`.publish` (the publish job,
bpy-free), and :mod:`.blender` (operators, panels and properties: the only
package that imports Blender). import-linter checks the layering
(``[tool.importlinter]`` in ``pyproject.toml``).

This is a Blender extension (see ``blender_manifest.toml``), so there is no
``bl_info``. Blender imports it as ``bl_ext.<repo>.geotools``; nothing here may
assume the package name.

``bpy`` is only imported lazily inside :func:`register`/:func:`unregister`, so
``geotools`` and ``geotools.core`` stay importable from plain Python (pytest).
"""

from __future__ import annotations


def register() -> None:
    """Register the Blender side of the extension."""
    from . import blender

    blender.register()


def unregister() -> None:
    """Unregister the Blender side of the extension."""
    from . import blender

    blender.unregister()
