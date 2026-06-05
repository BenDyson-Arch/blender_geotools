"""
collections.py — Blender collection hierarchy management for tile objects.
"""

from pathlib import Path

import bpy

from ....core import import_utils


def get_or_create_collection(
    name: str,
    parent: bpy.types.Collection = None,
) -> bpy.types.Collection:
    """Return the named collection, creating and linking it if absent.

    When *parent* is ``None`` the collection is linked directly under the
    scene's root collection.
    """
    return import_utils.get_or_create_collection(bpy.context, name, parent=parent)


def collection_name_for_tile(tile, tileset_name: str) -> str:
    """Return a deterministic, human-readable collection name for *tile*.

    Format: ``<tileset_name>/<stem>_d<depth>``
    e.g. ``tileset/a_d0``, ``tileset/d000_d3``.

    The tileset prefix keeps collections from different tilesets separate.
    """
    stem = Path(tile.uri).stem
    return f"{tileset_name}/{stem}_d{tile.depth}"


def link_objects_to_collection(
    objects,
    collection: bpy.types.Collection,
) -> None:
    """Link *objects* into *collection* and unlink them from the scene root.

    Safe to call multiple times — skips objects already in the collection.
    """
    for obj in objects:
        import_utils.link_to_collection(bpy.context, obj, collection)
