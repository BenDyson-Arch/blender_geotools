"""The scene georeference in Blender: GeoTools' CRS and E/N fields over the GeoScene keys.

The rules live in ``core.geoscene`` (bpy-free, tested without Blender); this
module only connects them to the scene:

- ``scene.geotools.crs``, ``origin_e`` and ``origin_n`` are ``StringProperty``
  fields with get/set callbacks. In a SOURCE_CRS project whose scene has a
  ``SRID`` key they read and write the scene's ``SRID``, ``crs x`` and
  ``crs y`` ID properties (the BlenderGIS GeoScene keys), keeping
  ``latitude``/``longitude`` in sync. Otherwise they read and write GeoTools'
  own text, ``source_crs``, ``source_offset_e`` and ``source_offset_n`` (where
  earlier GeoTools versions kept the project), which then is the georeference.
  Every reader of the project CRS and origin (``properties.placement_from_props``,
  the panels, the EPSG picker, the importers) goes through these fields.
- :func:`reconcile_scene` moves GeoTools' own CRS and E/N into the keys on file
  load (migration from earlier GeoTools versions, with a one-time notice in the
  Project panel), when a project becomes SOURCE_CRS, and when a CRS GeoScene
  can hold is chosen. The keys win where both exist.
- LOCAL and ANCHOR projects write no keys. If the scene has some, the Project
  panel and Check warn, and :class:`GEOTOOLS_OT_use_blendergis_georef` switches
  to a CRS project that uses them.

GeoTools never imports BlenderGIS and works the same without it. See
docs/design/georeference-interop.md.
"""

from __future__ import annotations

import logging
from typing import Any

import bpy

from ..core import geoscene
from ..core.issues import Issue
from .common import redraw_ui
from .ui import wrapped

log = logging.getLogger(__name__)

STORE_NAMES = (geoscene.STORE_CRS, geoscene.STORE_E, geoscene.STORE_N)


class _Store:
    """GeoTools' own CRS and E/N text on ``scene.geotools`` as a mapping.

    A name is "in" the store when it was set (``is_property_set``); deleting it
    unsets it, so the default ("" or "0") applies again.
    """

    __slots__ = ("props",)

    def __init__(self, props: Any) -> None:
        self.props = props

    def __contains__(self, name: str) -> bool:
        return bool(self.props.is_property_set(name))

    def get(self, name: str, default: object = None) -> object:
        return getattr(self.props, name) if name in self else default

    def __getitem__(self, name: str) -> object:
        if name not in self:
            raise KeyError(name)
        return getattr(self.props, name)

    def __setitem__(self, name: str, value: object) -> None:
        setattr(self.props, name, str(value))

    def __delitem__(self, name: str) -> None:
        self.props.property_unset(name)


def _scene(props: Any) -> bpy.types.Scene:
    return props.id_data


def store(props: Any) -> _Store:
    return _Store(props)


# --- the property callbacks (used by properties.GeoToolsProject) --------------------------


def get_crs(props: Any) -> str:
    try:
        return geoscene.get_crs(_scene(props), _Store(props), props.placement_mode)
    except Exception:  # a getter must never raise into the UI
        log.exception("reading the scene CRS failed")
        return ""


def set_crs(props: Any, value: str) -> None:
    geoscene.set_crs(_scene(props), _Store(props), props.placement_mode, value)


def _origin_getter(attr: str):
    def get(props: Any) -> str:
        try:
            return geoscene.get_origin(_scene(props), _Store(props), props.placement_mode, attr)
        except Exception:
            log.exception("reading the scene origin failed")
            return ""

    return get


def _origin_setter(attr: str):
    def set_(props: Any, value: str) -> None:
        if not geoscene.set_origin(_scene(props), _Store(props), props.placement_mode, attr, value):
            log.warning("%r is not a number; the scene origin is unchanged", value)

    return set_


get_origin_e = _origin_getter(geoscene.STORE_E)
set_origin_e = _origin_setter(geoscene.STORE_E)
get_origin_n = _origin_getter(geoscene.STORE_N)
set_origin_n = _origin_setter(geoscene.STORE_N)


def placement_mode_changed(props: Any, _context: bpy.types.Context | None) -> None:
    """A project that becomes SOURCE_CRS takes the scene's keys (or gives them its own)."""
    reconcile_scene(_scene(props), announce=False)


# --- reading -------------------------------------------------------------------------------


def read(scene: bpy.types.Scene) -> geoscene.GeoScene:
    """The scene's GeoScene keys."""
    return geoscene.read(scene)


def keys_live(scene: bpy.types.Scene) -> bool:
    """True when the keys are the project's CRS and E/N."""
    return geoscene.keys_live(scene, scene.geotools.placement_mode)


def stored_crs(props: Any) -> str:
    """GeoTools' own CRS text (the preset migration's input), whatever the mode."""
    return props.source_crs


def write_stored_crs(props: Any, value: str) -> None:
    props.source_crs = value


def offset_a(props: Any) -> float | None:
    from .properties import FieldError, read_number

    try:
        return read_number(props, "source_offset_a")
    except FieldError:
        return None


def issues(scene: bpy.types.Scene) -> list[Issue]:
    """How GeoScene tools (BlenderGIS) will see this scene: the panel's and Check's warnings."""
    props = scene.geotools
    return geoscene.interop_issues(scene, props.placement_mode, offset_a(props))


def start_crs_project(props: Any) -> bool:
    """Make a LOCAL or ANCHOR project a CRS project with offset A = 0; True if it switched.

    New CRS projects start with A = 0, because GeoScene tools (BlenderGIS) read
    Blender Z as absolute height. A was not used by the LOCAL or ANCHOR project
    (it is not even shown), so nothing the user set is lost. A project that is
    already SOURCE_CRS keeps its A. The switch reconciles: existing keys win.
    """
    if props.placement_mode == "SOURCE_CRS":
        return False
    props.property_unset("source_offset_a")
    props.placement_mode = "SOURCE_CRS"
    return True


# --- migration ---------------------------------------------------------------------------


def reconcile_scene(scene: bpy.types.Scene, announce: bool) -> geoscene.Reconciled | None:
    """Make the keys the georeference of a SOURCE_CRS scene (``core.geoscene.reconcile``).

    With ``announce`` (file load: migration from an earlier GeoTools) any
    change leaves a notice in the Project panel; otherwise only a conflict,
    where the keys won over GeoTools' own different values, does.
    """
    props = getattr(scene, "geotools", None)
    if props is None:
        return None
    result = geoscene.reconcile(scene, _Store(props), props.placement_mode)
    if result is None or not result.changed:
        return result
    if announce or result.dropped:
        props.georef_notice = result.notice()
    log.info("Scene %s georeference: %s", scene.name, result.notice())
    return result


def reconcile_all() -> list[str]:
    """Reconcile every scene of the open file; return the names of those that changed."""
    changed = []
    for scene in bpy.data.scenes:
        try:
            result = reconcile_scene(scene, announce=True)
        except Exception:  # never break file loading
            log.exception("Georeference migration of scene %s failed", scene.name)
            continue
        if result is not None and result.changed:
            changed.append(scene.name)
    return changed


# --- snapshot (undoing a failed import's origin switch) -----------------------------------


def snapshot(scene: bpy.types.Scene) -> dict[str, object]:
    """Everything that says where the scene is: mode, keys, GeoTools' own text, A."""
    props = scene.geotools
    return {
        "placement_mode": props.placement_mode,
        "keys": {k: scene[k] for k in geoscene.GEOSCENE_KEYS if k in scene},
        "store": {n: getattr(props, n) for n in STORE_NAMES if props.is_property_set(n)},
        "source_offset_a": props.source_offset_a,
        "offset_file": props.offset_file,
        "georef_notice": props.georef_notice,
    }


def restore(scene: bpy.types.Scene, saved: dict[str, object]) -> None:
    """Put back what :func:`snapshot` saved, exactly (no sync, no reconcile)."""
    props = scene.geotools
    props.placement_mode = saved["placement_mode"]  # its update may write keys: undone next
    for key in geoscene.GEOSCENE_KEYS:
        if key in scene:
            del scene[key]
    for key, value in dict(saved["keys"]).items():  # type: ignore[arg-type]
        scene[key] = value
    for name in STORE_NAMES:
        props.property_unset(name)
    for name, value in dict(saved["store"]).items():  # type: ignore[arg-type]
        setattr(props, name, value)
    props.source_offset_a = saved["source_offset_a"]
    props.offset_file = saved["offset_file"]
    props.georef_notice = saved["georef_notice"]


# --- drawing -------------------------------------------------------------------------------


def draw_notes(
    layout: bpy.types.UILayout, context: bpy.types.Context, props: bpy.types.PropertyGroup
) -> None:
    """The Project panel's interoperability notes: notice, ignored keys, A not 0."""
    scene = _scene(props)
    if props.georef_notice:
        box = layout.box()
        row = box.row()
        col = row.column()
        wrapped(col, context, props.georef_notice, icon="FILE_REFRESH", max_lines=10)
        row.operator(GEOTOOLS_OT_dismiss_georef_notice.bl_idname, text="", icon="X")
    for issue in issues(scene):
        box = layout.box()
        wrapped(box, context, issue.message, icon="ERROR", max_lines=8)
        if issue.code == "georef-keys-ignored" and read(scene).crs:
            box.operator(GEOTOOLS_OT_use_blendergis_georef.bl_idname, icon="ORIENTATION_GLOBAL")


def draw_storage(
    layout: bpy.types.UILayout, context: bpy.types.Context, props: bpy.types.PropertyGroup
) -> None:
    """Under the CRS and E/N fields: where they are kept."""
    scene = _scene(props)
    if keys_live(scene):
        if not read(scene).parses:
            wrapped(
                layout,
                context,
                "The scene's SRID is not one BlenderGIS can read; GeoTools reads it and "
                "leaves it as it is.",
                icon="INFO",
            )
        return
    if props.crs.strip() and geoscene.srid_for(props.crs) is None:
        wrapped(
            layout,
            context,
            "BlenderGIS cannot hold this CRS (only a plain EPSG code or a PROJ string), so "
            "the scene has no GeoScene keys and other GIS tools see it as not georeferenced.",
            icon="INFO",
            max_lines=5,
        )


# --- operators -----------------------------------------------------------------------------


class GEOTOOLS_OT_use_blendergis_georef(bpy.types.Operator):
    """Make this a CRS project that uses the scene's BlenderGIS georeference (SRID and origin), with offset A 0. Objects keep their Blender positions, so they are then read as coordinates relative to that origin, with Z as absolute height"""

    bl_idname = "geotools.use_blendergis_georef"
    bl_label = "Use the Scene's BlenderGIS Georeference"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        scene = context.scene
        if scene is None or not read(scene).crs:
            cls.poll_message_set("The scene has no BlenderGIS CRS (SRID)")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        start_crs_project(scene.geotools)  # A = 0; reconciles: the keys win
        self.report({"INFO"}, f"Project uses the scene's georeference: {read(scene).describe()}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_dismiss_georef_notice(bpy.types.Operator):
    """Hide the georeference migration notice"""

    bl_idname = "geotools.dismiss_georef_notice"
    bl_label = "Dismiss"
    bl_options = {"INTERNAL", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        context.scene.geotools.georef_notice = ""
        redraw_ui(context)
        return {"FINISHED"}


classes = (GEOTOOLS_OT_use_blendergis_georef, GEOTOOLS_OT_dismiss_georef_notice)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    # File load and enabling run reconcile_all from crs_picker's handler, after the
    # preset migration of GeoTools' own CRS text.
    _register_classes()


def unregister() -> None:
    _unregister_classes()
