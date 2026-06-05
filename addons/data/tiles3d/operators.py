"""
operators.py — ScanTileset and ImportTileset operators.

No UI registration here — the unified UI (Phase 3) provides entry points.
"""

from pathlib import Path
from types import SimpleNamespace

import bpy
from bpy.props import BoolProperty, FloatProperty, StringProperty
from bpy.types import Operator

from ....core import provenance as _prov
from ....geoscene import GeoScene
from . import collections as col_utils
from . import tile_import, traversal


class GEOTOOLS_OT_scan_tileset(Operator):
    """Scan a tileset.json and populate the LOD slider range."""

    bl_idname = "geotools.scan_tileset"
    bl_label = "Scan Tileset"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.tiles3d
        fp = Path(props.last_filepath).resolve()
        if not fp.is_file():
            self.report({"ERROR"}, f"File not found: {fp}")
            return {"CANCELLED"}

        try:
            data = traversal.load_tileset(fp)
        except (ValueError, FileNotFoundError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        min_e, max_e, total = traversal.scan_tileset(data["root"], base_path=fp.parent)

        props.error_min = min_e
        props.error_max = max_e
        props.total_content_nodes = total
        props.last_filepath = str(fp)

        self.report(
            {"INFO"},
            f"Scanned {total} content nodes  —  "
            f"geometricError {min_e:.4f} – {max_e:.4f}",
        )
        return {"FINISHED"}


class GEOTOOLS_OT_import_tileset(Operator):
    """Import an OGC 3D Tiles tileset from local disk."""

    bl_idname = "geotools.import_tileset"
    bl_label = "Import 3D Tiles"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(
        name="File Path",
        description="Path to tileset.json",
        subtype="FILE_PATH",
    )
    geometric_error_threshold: FloatProperty(
        name="geometricError Threshold",
        description="Import tiles at or below this geometricError value",
        default=20.0,
        min=0.0,
    )
    apply_transforms: BoolProperty(
        name="Apply Transforms",
        description="Apply tile world transforms (ENU decomposition + scene origin offset)",
        default=True,
    )
    join_mesh: BoolProperty(
        name="Join Mesh",
        description="Merge all tile objects into a single mesh after import",
        default=False,
    )

    def execute(self, context):
        geoscn = GeoScene(context.scene)
        if geoscn.is_broken:
            self.report(
                {"ERROR"}, "Scene georeferencing is broken. Fix Scene / CRS first."
            )
            return {"CANCELLED"}

        fp = Path(self.filepath).resolve()
        if not fp.is_file():
            self.report({"ERROR"}, f"File not found: {fp}")
            return {"CANCELLED"}

        try:
            data = traversal.load_tileset(fp)
        except (ValueError, FileNotFoundError) as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        settings = SimpleNamespace(
            filepath=fp,
            base_path=fp.parent,
            tileset_name=fp.parent.name,
            threshold=self.geometric_error_threshold,
            apply_transforms=self.apply_transforms,
            join_mesh=self.join_mesh,
        )

        # ─ Transforms & scene origin ─────────────────────────────
        # root_tf is a 64-bit Python nested list; all origin/offset
        # computations use it directly to avoid 32-bit precision loss.
        root_tf = traversal.accumulate_transform(data["root"], None)
        if settings.apply_transforms:
            ox, oy, srid = tile_import.get_or_set_scene_origin(root_tf)
            offset = tile_import.blender_offset_for_root(root_tf, ox, oy, srid)
        else:
            offset = (0.0, 0.0, 0.0)

        # ─ Collection root ──────────────────────────────────
        root_col = col_utils.get_or_create_collection(settings.tileset_name)
        _prov.stamp_collection(
            root_col,
            source=str(fp),
            layer=settings.tileset_name,
            fmt="tiles3d",
            settings={
                "threshold": settings.threshold,
                "apply_transforms": bool(settings.apply_transforms),
                "join_mesh": bool(settings.join_mesh),
            },
        )

        # ─ Import loop ─────────────────────────────────────
        all_objects = []  # [(obj, tile), ...]
        tiles = list(
            traversal.traverse(
                data["root"],
                threshold=settings.threshold,
                base_path=settings.base_path,
            )
        )

        for tile in tiles:
            uri = tile.uri
            ext = Path(uri).suffix.lower()
            try:
                if ext == ".b3dm":
                    objs = tile_import.import_b3dm(
                        uri, f"{settings.tileset_name}_d{tile.depth}"
                    )
                elif ext in (".glb", ".gltf"):
                    objs = tile_import.import_glb(
                        uri, f"{settings.tileset_name}_d{tile.depth}"
                    )
                else:
                    self.report({"WARNING"}, f"Skipping unsupported tile format: {uri}")
                    continue
            except Exception as exc:
                self.report({"WARNING"}, f"Failed to import {uri}: {exc}")
                continue

            for obj in objs:
                if settings.apply_transforms:
                    tile_import.apply_transform_enu(
                        obj,
                        tile.world_transform,
                        root_tf,
                        blender_offset=offset,
                        scene_origin=(ox, oy, srid),
                    )
                tile_import.write_tile_metadata(obj, tile)
                _prov.stamp(
                    obj,
                    source=str(fp),
                    layer=settings.tileset_name,
                    fmt="tiles3d",
                    geom_type="Mesh",
                    scene_srid=geoscn.srid or "",
                )
                all_objects.append((obj, tile))

            # Only build the per-tile collection hierarchy when not joining.
            if not settings.join_mesh:
                tile_col = col_utils.get_or_create_collection(
                    col_utils.collection_name_for_tile(tile, settings.tileset_name),
                    parent=root_col,
                )
                col_utils.link_objects_to_collection(objs, tile_col)

        total_objs = len(all_objects)

        # ─ Optional join ───────────────────────────────────────────
        if settings.join_mesh and all_objects:
            for obj, tile in all_objects:
                tile_import.bake_face_attributes(obj, tile)

            joined_name = f"{settings.tileset_name}_joined"
            joined_obj = tile_import.join_tiles(
                all_objects,
                joined_name=joined_name,
                tileset_name=settings.tileset_name,
                collection=root_col,
            )
            _prov.stamp(
                joined_obj,
                source=str(fp),
                layer=settings.tileset_name,
                fmt="tiles3d",
                geom_type="Mesh",
                scene_srid=geoscn.srid or "",
            )
            self.report(
                {"INFO"},
                f"Joined {total_objs} object(s) from {len(tiles)} tile(s) "
                f"into '{joined_obj.name}' [ε ≤ {settings.threshold}]",
            )
        else:
            self.report(
                {"INFO"},
                f"Imported {total_objs} object(s) from {len(tiles)} tile(s) "
                f"[ε ≤ {settings.threshold}]",
            )
        return {"FINISHED"}


_classes = (
    GEOTOOLS_OT_scan_tileset,
    GEOTOOLS_OT_import_tileset,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
