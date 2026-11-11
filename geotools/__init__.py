"""GeoTools: prepare, bake and publish OGC 3D Tiles from Blender.

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
