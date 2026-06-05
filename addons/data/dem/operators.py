"""
dem/operators.py — DEM import operator.
"""

import dataclasses
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import Operator

from ....core import import_utils
from ....core import provenance as _prov
from ....core.proj.reproj import reproj_pt
from ....geoscene import GeoScene
from . import reader as _reader


class GEOTOOLS_OT_import_dem(Operator):
    """Import a GeoTIFF DEM as a georeferenced terrain mesh."""

    bl_idname = "geotools.import_dem"
    bl_label = "Import DEM"
    bl_options = {"REGISTER", "UNDO"}

    # File browser
    filepath: StringProperty(subtype="FILE_PATH")
    filter_glob: StringProperty(default="*.tif;*.tiff;*.geotiff", options={"HIDDEN"})

    downsample: EnumProperty(
        name="Downsample",
        items=[
            ("1", "1× — Full resolution", ""),
            ("2", "2× — Half resolution", ""),
            ("4", "4× — Quarter resolution", ""),
            ("8", "8× — Eighth resolution", ""),
        ],
        default="4",
    )
    apply_offset: BoolProperty(
        name="Apply scene offset",
        default=True,
    )
    add_subdivision: BoolProperty(
        name="Add Subdivision modifier",
        default=False,
    )

    def invoke(self, context, event):
        if hasattr(context.scene, "dem_import"):
            s = context.scene.dem_import
            self.downsample = s.downsample
            self.apply_offset = s.apply_offset
            self.add_subdivision = s.add_subdivision
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.prop(self, "downsample")
        col.separator()
        col.prop(self, "apply_offset")
        col.prop(self, "add_subdivision")

    def execute(self, context):
        if not self.filepath:
            self.report({"ERROR"}, "No filepath provided")
            return {"CANCELLED"}

        gs = GeoScene(context.scene)
        if gs.is_broken:
            self.report({"ERROR"}, "Scene georeferencing is broken. Fix CRS first.")
            return {"CANCELLED"}

        # ── Read DEM ─────────────────────────────────────────────────
        try:
            dem_data = _reader.read_dem(self.filepath)
        except FileNotFoundError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        except ValueError as exc:
            self.report({"ERROR"}, f"Cannot read DEM: {exc}")
            return {"CANCELLED"}

        # ── Resolve / validate CRS ────────────────────────────────────
        file_srid = dem_data.srid

        if file_srid is None and gs.has_valid_crs:
            file_srid = gs.srid
            self.report(
                {"INFO"},
                f"DEM has no embedded CRS — assuming scene CRS ({file_srid}).",
            )
        elif file_srid is None:
            self.report(
                {"ERROR"},
                "DEM has no embedded CRS and the scene has no CRS set. "
                "Set the scene CRS before importing.",
            )
            return {"CANCELLED"}

        # Reproject to scene CRS when they differ
        scene_srid = gs.srid or file_srid
        if gs.has_valid_crs and scene_srid != file_srid:
            try:
                dem_data = _reproject_dem(dem_data, file_srid, scene_srid)
            except Exception as exc:
                self.report(
                    {"ERROR"},
                    f"Cannot reproject DEM from {file_srid} to {scene_srid}: {exc}",
                )
                return {"CANCELLED"}
            self.report({"INFO"}, f"Reprojected DEM {file_srid} → {scene_srid}")
            file_srid = scene_srid

        # ── Set scene CRS / origin ────────────────────────────────────
        if not gs.has_crs:
            try:
                gs.srid = file_srid
            except Exception as exc:
                self.report({"ERROR"}, f"Invalid CRS {file_srid}: {exc}")
                return {"CANCELLED"}

        x_min, y_min, x_max, y_max = _reader.dem_bounds(dem_data)
        cx = (x_min + x_max) / 2.0
        cy = (y_min + y_max) / 2.0

        if not gs.has_origin_proj:
            gs.set_origin_proj(cx, cy)

        # ── Build mesh ────────────────────────────────────────────────
        stem = Path(self.filepath).stem
        step = int(self.downsample)

        try:
            mesh = _reader.build_terrain_mesh(dem_data, name=stem, downsample=step)
        except Exception as exc:
            self.report({"ERROR"}, f"Mesh build failed: {exc}")
            return {"CANCELLED"}

        # ── Apply scene offset ────────────────────────────────────────
        if self.apply_offset:
            ox = gs.origin_x
            oy = gs.origin_y
            for v in mesh.vertices:
                v.co.x -= ox
                v.co.y -= oy
            mesh.update()

        # ── Create object / collection ───────────────────────────────
        source_path = str(Path(self.filepath).resolve())
        root_col = import_utils.get_or_create_collection(context, stem)
        _prov.stamp_collection(
            root_col,
            source=source_path,
            layer=stem,
            fmt="dem",
            settings={
                "downsample": step,
                "apply_offset": bool(self.apply_offset),
                "add_subdivision": bool(self.add_subdivision),
            },
        )

        obj = bpy.data.objects.new(stem, mesh)
        _prov.stamp(
            obj,
            source=source_path,
            layer=stem,
            fmt="dem",
            geom_type="Mesh",
            scene_srid=file_srid,
        )
        obj["dem_source"] = source_path
        obj["dem_srid"] = file_srid
        obj["dem_pixel_w"] = dem_data.pixel_width
        obj["dem_pixel_h"] = dem_data.pixel_height
        obj["dem_downsample"] = step

        import_utils.link_to_collection(context, obj, root_col)
        context.view_layer.objects.active = obj
        obj.select_set(True)

        if self.add_subdivision:
            mod = obj.modifiers.new(name="Subdivision", type="SUBSURF")
            mod.levels = 1
            mod.render_levels = 2

        if hasattr(context.scene, "dem_import"):
            s = context.scene.dem_import
            s.downsample = self.downsample
            s.apply_offset = self.apply_offset
            s.add_subdivision = self.add_subdivision

        vert_count = len(mesh.vertices)
        face_count = len(mesh.polygons)
        self.report(
            {"INFO"},
            f"Imported '{stem}': {dem_data.cols}×{dem_data.rows} grid → "
            f"{vert_count} verts, {face_count} faces (downsample {step}×, CRS {file_srid})",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# DEM reprojection helper
# ---------------------------------------------------------------------------


def _reproject_dem(
    dem: "_reader.DEMData", src_srid: str, dst_srid: str
) -> "_reader.DEMData":
    """Return a new DEMData with corner coordinates reprojected to *dst_srid*."""
    x0, y0 = dem.x_origin, dem.y_origin
    x1 = dem.x_origin + dem.cols * dem.pixel_width
    y1 = dem.y_origin + dem.rows * dem.pixel_height

    x0r, y0r = reproj_pt(src_srid, dst_srid, x0, y0)
    x1r, y1r = reproj_pt(src_srid, dst_srid, x1, y1)

    new_pw = (x1r - x0r) / dem.cols
    new_ph = (y1r - y0r) / dem.rows

    return dataclasses.replace(
        dem,
        x_origin=x0r,
        y_origin=y0r,
        pixel_width=new_pw,
        pixel_height=new_ph,
        srid=dst_srid,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (GEOTOOLS_OT_import_dem,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
