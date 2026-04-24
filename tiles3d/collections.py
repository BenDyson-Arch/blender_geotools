"""
collections.py — Blender collection hierarchy management for tile objects.
"""

from pathlib import Path

import bpy


def get_or_create_collection(
    name: str,
    parent: bpy.types.Collection = None,
) -> bpy.types.Collection:
    """Return the named collection, creating and linking it if absent.

    When *parent* is ``None`` the collection is linked directly under the
    scene's root collection.
    """
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    if parent is not None:
        parent.children.link(col)
    else:
        bpy.context.scene.collection.children.link(col)
    return col


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
    scene_root = bpy.context.scene.collection
    for obj in objects:
        if obj.name not in collection.objects:
            collection.objects.link(obj)
        if obj.name in scene_root.objects:
            scene_root.objects.unlink(obj)
