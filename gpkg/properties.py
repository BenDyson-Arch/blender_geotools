"""gpkg/properties.py — Blender PropertyGroup for GPKG import settings."""

import os

import bpy


# ---------------------------------------------------------------------------
# Dynamic layer-name enum helpers
# ---------------------------------------------------------------------------

# Keyed by absolute filepath → list[tuple[str,str,str]].
# Must be a module-level reference so Blender's enum machinery cannot GC it.
_layer_items_cache: dict[str, list[tuple]] = {}

_PLACEHOLDER_NOFILE  = [("__none__", "— select a file first —", "")]
_PLACEHOLDER_NOLAY   = [("__none__", "— no layers found —",    "")]
_PLACEHOLDER_ERR     = [("__none__", "— error reading file —",  "")]


def _layer_items(self, context) -> list[tuple]:
    """Dynamic items callback for layer_name EnumProperty."""
    fp = bpy.path.abspath(self.filepath) if self.filepath else ""
    if not fp or not os.path.isfile(fp):
        return _PLACEHOLDER_NOFILE
    if fp not in _layer_items_cache:
        try:
            from .reader import list_layers
            infos = list_layers(fp)
            if infos:
                _layer_items_cache[fp] = [
                    (info.name, info.name,
                     f"{info.geom_type}  ·  {info.feature_count} features")
                    for info in infos
                ]
            else:
                _layer_items_cache[fp] = _PLACEHOLDER_NOLAY
        except Exception:
            _layer_items_cache[fp] = _PLACEHOLDER_ERR
    return _layer_items_cache[fp]


def _on_filepath_update(self, context) -> None:
    """Invalidate the layer cache when the user changes the filepath."""
    _layer_items_cache.clear()


# ---------------------------------------------------------------------------
# Import settings PropertyGroup
# ---------------------------------------------------------------------------

class GPKGImportSettings(bpy.types.PropertyGroup):
    """Import settings exposed in the N-panel and file browser."""

    filepath: bpy.props.StringProperty(
        name="File",
        description="Path to the .gpkg file",
        subtype="FILE_PATH",
        default="",
        update=_on_filepath_update,
    )

    layer_name: bpy.props.EnumProperty(
        name="Layer",
        description="Feature table to import",
        items=_layer_items,
    )

    mode: bpy.props.EnumProperty(
        name="Import Mode",
        items=[
            ("PER_FEATURE", "One object per feature",
             "Each feature becomes a separate Blender object (required for reload/write-back)"),
            ("BY_TYPE", "One object per geometry type",
             "All features of the same type are merged (write-back not available)"),
            ("JOINED", "Single joined mesh",
             "All geometry joined into one object (write-back not available)"),
        ],
        default="PER_FEATURE",
    )

    offset_origin: bpy.props.BoolProperty(
        name="Offset to world origin",
        description="Subtract the dataset bounding box centroid from all coordinates",
        default=True,
    )

    store_custom_props: bpy.props.BoolProperty(
        name="Duplicate as custom properties",
        description="Also write numeric attributes as object custom properties (legacy)",
        default=False,
    )

    import_holes: bpy.props.BoolProperty(
        name="Import holes (interior rings)",
        description="Cut interior rings as holes in polygon faces",
        default=True,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register():
    bpy.utils.register_class(GPKGImportSettings)
    bpy.types.Scene.gpkg_import = bpy.props.PointerProperty(type=GPKGImportSettings)


def unregister():
    del bpy.types.Scene.gpkg_import
    bpy.utils.unregister_class(GPKGImportSettings)
