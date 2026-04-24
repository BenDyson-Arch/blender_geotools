"""
geoscene.py - Scene georeferencing manager for blender_geotools.

Portions of the interaction model and UI flow are derived from BlenderGIS
(https://github.com/domlysz/BlenderGIS).
Copyright (C) domlysz - GNU General Public License v3.0.

Scene CRS and origin are the single source of truth for all imports.
Imports may initialize scene georef if it is missing, but they must not carry
their own target CRS once the scene is georeferenced.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Tuple

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, PointerProperty
from bpy.types import Operator, PropertyGroup

from .core.proj.reproj import reproj_pt
from .core.proj.srs import SRS

log = logging.getLogger(__name__)

_K_SRID = "gt_srid"
_K_ORIGIN_X = "gt_origin_x"
_K_ORIGIN_Y = "gt_origin_y"
_K_ORIGIN_LON = "gt_origin_lon"
_K_ORIGIN_LAT = "gt_origin_lat"

_BGIS_SRID = "SRID"
_BGIS_CRS_X = "crs x"
_BGIS_CRS_Y = "crs y"


class GeoScene:
    """Scene georeferencing manager with BlenderGIS-style state semantics."""

    def __init__(self, scene=None):
        self._scene = scene if scene is not None else bpy.context.scene

    @property
    def has_crs(self) -> bool:
        return _K_SRID in self._scene or _BGIS_SRID in self._scene

    @property
    def has_valid_crs(self) -> bool:
        return self.has_crs and SRS.validate(self.srid)

    @property
    def has_origin_geo(self) -> bool:
        return _K_ORIGIN_LON in self._scene and _K_ORIGIN_LAT in self._scene

    @property
    def has_origin_proj(self) -> bool:
        has_gt = _K_ORIGIN_X in self._scene and _K_ORIGIN_Y in self._scene
        has_bgis = _BGIS_CRS_X in self._scene and _BGIS_CRS_Y in self._scene
        return has_gt or has_bgis

    @property
    def is_georef(self) -> bool:
        return self.has_valid_crs and self.has_origin_proj

    @property
    def is_partially_georef(self) -> bool:
        return self.has_crs or self.has_origin_proj or self.has_origin_geo

    @property
    def is_broken(self) -> bool:
        return (
            (self.has_crs and not self.has_valid_crs)
            or (not self.has_crs and (self.has_origin_proj or self.has_origin_geo))
            or (self.has_crs and self.has_origin_geo and not self.has_origin_proj)
        )

    @property
    def srid(self) -> Optional[str]:
        if _K_SRID in self._scene:
            return str(self._scene[_K_SRID])
        if _BGIS_SRID in self._scene:
            return str(self._scene[_BGIS_SRID])
        return None

    @srid.setter
    def srid(self, value: str):
        new_srid = str(SRS(value))
        old_srid = self.srid

        if self.has_origin_geo:
            x, y = reproj_pt("EPSG:4326", new_srid, self.origin_lon, self.origin_lat)
            self._set_origin_proj_raw(x, y)
        elif self.has_origin_proj and old_srid and old_srid != new_srid:
            x, y = reproj_pt(old_srid, new_srid, self.origin_x, self.origin_y)
            self._set_origin_proj_raw(x, y)

        self._scene[_K_SRID] = new_srid
        self._scene[_BGIS_SRID] = new_srid

        if self.has_origin_proj:
            self._sync_geo_from_proj()

    @property
    def origin_x(self) -> Optional[float]:
        if _K_ORIGIN_X in self._scene:
            return float(self._scene[_K_ORIGIN_X])
        if _BGIS_CRS_X in self._scene:
            return float(self._scene[_BGIS_CRS_X])
        return None

    @property
    def origin_y(self) -> Optional[float]:
        if _K_ORIGIN_Y in self._scene:
            return float(self._scene[_K_ORIGIN_Y])
        if _BGIS_CRS_Y in self._scene:
            return float(self._scene[_BGIS_CRS_Y])
        return None

    @property
    def origin_lon(self) -> Optional[float]:
        if _K_ORIGIN_LON in self._scene:
            return float(self._scene[_K_ORIGIN_LON])
        return None

    @property
    def origin_lat(self) -> Optional[float]:
        if _K_ORIGIN_LAT in self._scene:
            return float(self._scene[_K_ORIGIN_LAT])
        return None

    def _set_origin_proj_raw(self, x: float, y: float) -> None:
        self._scene[_K_ORIGIN_X] = float(x)
        self._scene[_K_ORIGIN_Y] = float(y)
        self._scene[_BGIS_CRS_X] = float(x)
        self._scene[_BGIS_CRS_Y] = float(y)

    def _sync_geo_from_proj(self) -> None:
        if not self.has_crs or not self.has_origin_proj:
            return
        try:
            lon, lat = reproj_pt(self.srid, "EPSG:4326", self.origin_x, self.origin_y)
            self._scene[_K_ORIGIN_LON] = float(lon)
            self._scene[_K_ORIGIN_LAT] = float(lat)
        except Exception:
            if _K_ORIGIN_LON in self._scene:
                del self._scene[_K_ORIGIN_LON]
            if _K_ORIGIN_LAT in self._scene:
                del self._scene[_K_ORIGIN_LAT]
            log.debug("Could not compute geographic origin from projected coords", exc_info=True)

    def _sync_proj_from_geo(self) -> None:
        if not self.has_crs or not self.has_origin_geo:
            return
        x, y = reproj_pt("EPSG:4326", self.srid, self.origin_lon, self.origin_lat)
        self._set_origin_proj_raw(x, y)

    def set_origin_proj(self, x: float, y: float, srid: Optional[str] = None) -> None:
        if srid is not None:
            self._scene[_K_SRID] = str(SRS(srid))
            self._scene[_BGIS_SRID] = str(SRS(srid))
        elif not self.has_crs:
            raise ValueError("Scene CRS must be set before setting projected origin")
        self._set_origin_proj_raw(x, y)
        self._sync_geo_from_proj()

    def set_origin_geo(self, lon: float, lat: float, srid: Optional[str] = None) -> None:
        if srid is None and not self.has_crs:
            raise ValueError("Scene CRS must be set before setting geographic origin")
        if srid is not None:
            new_srid = str(SRS(srid))
            self._scene[_K_SRID] = new_srid
            self._scene[_BGIS_SRID] = new_srid
        self._scene[_K_ORIGIN_LON] = float(lon)
        self._scene[_K_ORIGIN_LAT] = float(lat)
        self._sync_proj_from_geo()

    def get_origin_proj(self) -> Tuple[float, float]:
        return self.origin_x, self.origin_y

    def get_origin_geo(self) -> Tuple[float, float]:
        return self.origin_lon, self.origin_lat

    def _move_top_level_objects(self, dx: float, dy: float) -> None:
        for obj in [obj for obj in self._scene.objects if not obj.parent]:
            obj.location.x -= dx
            obj.location.y -= dy

    def upd_origin_proj(self, x: float, y: float, upd_obj_loc: bool = True) -> None:
        if not self.has_origin_proj:
            raise ValueError("Cannot update an unset projected origin")
        dx = float(x) - self.origin_x
        dy = float(y) - self.origin_y
        self.set_origin_proj(x, y)
        if upd_obj_loc:
            self._move_top_level_objects(dx, dy)

    def upd_origin_geo(self, lon: float, lat: float, upd_obj_loc: bool = True) -> None:
        if not self.is_georef:
            raise ValueError("Cannot update geographic origin of an ungeoref scene")
        x, y = reproj_pt("EPSG:4326", self.srid, lon, lat)
        self.upd_origin_proj(x, y, upd_obj_loc=upd_obj_loc)

    def clear_origin(self) -> None:
        for key in (_K_ORIGIN_X, _K_ORIGIN_Y, _K_ORIGIN_LON, _K_ORIGIN_LAT, _BGIS_CRS_X, _BGIS_CRS_Y):
            if key in self._scene:
                del self._scene[key]

    def clear(self) -> None:
        self.clear_origin()
        for key in (_K_SRID, _BGIS_SRID):
            if key in self._scene:
                del self._scene[key]

    def proj_to_blender(self, x: float, y: float) -> Tuple[float, float]:
        if not self.has_origin_proj:
            raise RuntimeError("Scene has no georeferencing origin")
        return x - self.origin_x, y - self.origin_y

    def blender_to_proj(self, bx: float, by: float) -> Tuple[float, float]:
        if not self.has_origin_proj:
            raise RuntimeError("Scene has no georeferencing origin")
        return bx + self.origin_x, by + self.origin_y

    def __repr__(self):
        return (
            f"GeoScene(srid={self.srid!r}, origin=({self.origin_x}, {self.origin_y}), "
            f"geo=({self.origin_lon}, {self.origin_lat}))"
        )


def _crs_enum_items(self, context):
    try:
        prefs = context.preferences.addons[__package__].preferences
        return [(item[0], item[1], item[2]) for item in json.loads(prefs.crs_presets_json)]
    except Exception:
        return [("EPSG:4326", "WGS84 Geographic", "")]


class GEOTOOLS_OT_set_scene_crs(Operator):
    bl_idname = "geotools.set_scene_crs"
    bl_label = "Set Scene CRS"
    bl_options = {"INTERNAL", "UNDO"}

    def invoke(self, context, event):
        prefs = context.preferences.addons[__package__].preferences
        self.crs = prefs.crs_presets
        return context.window_manager.invoke_props_dialog(self, width=320)

    crs: EnumProperty(name="CRS", items=_crs_enum_items)

    def draw(self, context):
        layout = self.layout
        row = layout.row(align=True)
        row.prop(self, "crs", text="")
        row.operator("geotools.add_crs_preset", text="", icon="ADD")

    def execute(self, context):
        geoscn = GeoScene(context.scene)
        try:
            geoscn.srid = self.crs
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot update scene CRS: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GEOTOOLS_OT_init_origin(Operator):
    bl_idname = "geotools.init_origin"
    bl_label = "Init Origin"
    bl_options = {"INTERNAL", "UNDO"}

    lonlat: BoolProperty(name="As lon/lat", default=False)
    x: FloatProperty(name="X")
    y: FloatProperty(name="Y")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=260)

    def execute(self, context):
        geoscn = GeoScene(context.scene)
        if geoscn.has_origin_geo or geoscn.has_origin_proj:
            self.report({"ERROR"}, "Scene origin already exists")
            return {"CANCELLED"}
        if not geoscn.has_crs:
            self.report({"ERROR"}, "Set the scene CRS before initializing the origin")
            return {"CANCELLED"}
        if self.lonlat:
            geoscn.set_origin_geo(self.x, self.y)
        else:
            geoscn.set_origin_proj(self.x, self.y)
        return {"FINISHED"}


class GEOTOOLS_OT_link_origin_geo(Operator):
    bl_idname = "geotools.link_origin_geo"
    bl_label = "Link Geo Origin"
    bl_options = {"INTERNAL", "UNDO"}

    def execute(self, context):
        geoscn = GeoScene(context.scene)
        if not (geoscn.has_origin_proj and geoscn.has_crs):
            self.report({"ERROR"}, "Projected origin and scene CRS are required")
            return {"CANCELLED"}
        try:
            geoscn._sync_geo_from_proj()
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot compute lon/lat: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GEOTOOLS_OT_link_origin_proj(Operator):
    bl_idname = "geotools.link_origin_proj"
    bl_label = "Link Projected Origin"
    bl_options = {"INTERNAL", "UNDO"}

    def execute(self, context):
        geoscn = GeoScene(context.scene)
        if not (geoscn.has_origin_geo and geoscn.has_crs):
            self.report({"ERROR"}, "Lon/lat origin and scene CRS are required")
            return {"CANCELLED"}
        try:
            geoscn._sync_proj_from_geo()
        except Exception as exc:
            self.report({"ERROR"}, f"Cannot compute projected origin: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class GEOTOOLS_OT_clear_origin(Operator):
    bl_idname = "geotools.clear_origin"
    bl_label = "Clear Origin"
    bl_options = {"INTERNAL", "UNDO"}

    def execute(self, context):
        GeoScene(context.scene).clear_origin()
        return {"FINISHED"}


class GEOTOOLS_OT_clear_georef(Operator):
    bl_idname = "geotools.clear_georef"
    bl_label = "Clear Georef"
    bl_options = {"INTERNAL", "UNDO"}

    def execute(self, context):
        GeoScene(context.scene).clear()
        return {"FINISHED"}


def _get_lon(self):
    return GeoScene().origin_lon or 0.0


def _set_lon(self, lon):
    geoscn = GeoScene()
    if geoscn.has_origin_geo:
        geoscn.upd_origin_geo(lon, geoscn.origin_lat)
    else:
        geoscn.set_origin_geo(lon, geoscn.origin_lat or 0.0)


def _get_lat(self):
    return GeoScene().origin_lat or 0.0


def _set_lat(self, lat):
    geoscn = GeoScene()
    if geoscn.has_origin_geo:
        geoscn.upd_origin_geo(geoscn.origin_lon, lat)
    else:
        geoscn.set_origin_geo(geoscn.origin_lon or 0.0, lat)


def _get_crsx(self):
    return GeoScene().origin_x or 0.0


def _set_crsx(self, x):
    geoscn = GeoScene()
    if geoscn.has_origin_proj:
        geoscn.upd_origin_proj(x, geoscn.origin_y)
    else:
        geoscn.set_origin_proj(x, geoscn.origin_y or 0.0)


def _get_crsy(self):
    return GeoScene().origin_y or 0.0


def _set_crsy(self, y):
    geoscn = GeoScene()
    if geoscn.has_origin_proj:
        geoscn.upd_origin_proj(geoscn.origin_x, y)
    else:
        geoscn.set_origin_proj(geoscn.origin_x or 0.0, y)


class GEOTOOLS_GEOREF_PROPS(PropertyGroup):
    display_origin_geo: BoolProperty(name="Geo", default=False)
    display_origin_proj: BoolProperty(name="Proj", default=True)
    lon: FloatProperty(name="Lon", min=-180.0, max=180.0, get=_get_lon, set=_set_lon)
    lat: FloatProperty(name="Lat", min=-90.0, max=90.0, get=_get_lat, set=_set_lat)
    crsx: FloatProperty(name="X", get=_get_crsx, set=_set_crsx)
    crsy: FloatProperty(name="Y", get=_get_crsy, set=_set_crsy)


def georef_manager_layout(panel, context):
    layout = panel.layout
    geoscn = GeoScene(context.scene)
    wm = context.window_manager

    if geoscn.is_broken:
        layout.alert = True

    row = layout.row(align=True)
    row.label(text="Scene georeferencing:")
    if geoscn.has_crs:
        row.operator("geotools.clear_georef", text="", icon="CANCEL")

    row = layout.row(align=True)
    split = row.split(factor=0.25)
    if geoscn.has_crs:
        split.label(icon="PROP_ON", text="CRS:")
    elif geoscn.has_origin_geo or geoscn.has_origin_proj:
        split.label(icon="ERROR", text="CRS:")
    else:
        split.label(icon="PROP_OFF", text="CRS:")
    split.label(text=geoscn.srid or "Not set")
    row.operator("geotools.set_scene_crs", text="", icon="PREFERENCES")

    row = layout.row(align=True)
    split = row.split(factor=0.25, align=True)
    if not geoscn.has_origin_geo and not geoscn.has_origin_proj:
        split.label(icon="PROP_OFF", text="Origin:")
    elif not geoscn.has_origin_geo and geoscn.has_origin_proj:
        split.label(icon="PROP_CON", text="Origin:")
    elif geoscn.has_origin_geo and geoscn.has_origin_proj:
        split.label(icon="PROP_ON", text="Origin:")
    else:
        split.label(icon="ERROR", text="Origin:")

    col = split.column(align=True)
    col.enabled = geoscn.has_origin_geo
    col.prop(wm.geotools_georef, "display_origin_geo", toggle=True)
    col = split.column(align=True)
    col.enabled = geoscn.has_origin_proj
    col.prop(wm.geotools_georef, "display_origin_proj", toggle=True)

    if geoscn.has_origin_geo or geoscn.has_origin_proj:
        if geoscn.has_crs and not geoscn.has_origin_proj:
            row.operator("geotools.link_origin_proj", text="", icon="CONSTRAINT")
        if geoscn.has_crs and not geoscn.has_origin_geo:
            row.operator("geotools.link_origin_geo", text="", icon="CONSTRAINT")
        row.operator("geotools.clear_origin", text="", icon="REMOVE")
    else:
        init_op = row.operator("geotools.init_origin", text="", icon="ADD")
        init_op.lonlat = False

    if geoscn.has_origin_geo and wm.geotools_georef.display_origin_geo:
        row = layout.row(align=True)
        row.prop(wm.geotools_georef, "lon", text="Lon")
        row.prop(wm.geotools_georef, "lat", text="Lat")

    if geoscn.has_origin_proj and wm.geotools_georef.display_origin_proj:
        row = layout.row(align=True)
        row.prop(wm.geotools_georef, "crsx", text="X")
        row.prop(wm.geotools_georef, "crsy", text="Y")

    layout.alert = False


_classes = (
    GEOTOOLS_OT_set_scene_crs,
    GEOTOOLS_OT_init_origin,
    GEOTOOLS_OT_link_origin_geo,
    GEOTOOLS_OT_link_origin_proj,
    GEOTOOLS_OT_clear_origin,
    GEOTOOLS_OT_clear_georef,
    GEOTOOLS_GEOREF_PROPS,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.WindowManager.geotools_georef = PointerProperty(type=GEOTOOLS_GEOREF_PROPS)


def unregister():
    if hasattr(bpy.types.WindowManager, "geotools_georef"):
        del bpy.types.WindowManager.geotools_georef
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
