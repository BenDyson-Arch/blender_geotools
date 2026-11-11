""" "Use this data's centre as the project origin": the follow-up to a LOCAL/ANCHOR refusal.

Projected data with large coordinates cannot go into a LOCAL or ANCHOR project
(``core.data_origin``). The import operators then call :func:`offer`, which

- remembers the refused import on the window manager (not saved in the
  ``.blend``), so the Linked Data panel shows the refusal with a button, and
- pops up the refusal with the same button under the mouse, when there is a
  window (File > Import and the Linked Data panel's Import buttons both end
  here).

The button is :class:`GEOTOOLS_OT_use_data_origin`. It switches the project to
SOURCE_CRS with the data's CRS and the centre of its extent, then runs the
import again. When the project already has a source collection with objects,
its invoke asks for confirmation first, because the switch changes how the
existing scene is placed on Earth. Run from a script (EXEC), it does not ask.
"""

from __future__ import annotations

import json
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from ..core.data_origin import DataOrigin, NeedsProjectOrigin
from .common import redraw_ui
from .properties import write_number
from .ui import wrapped

KIND_ITEMS = (
    ("GEOJSON", "GeoJSON", "A GeoJSON file"),
    ("GPKG", "GeoPackage", "GeoPackage feature tables"),
    ("GEOTIFF", "GeoTIFF", "A GeoTIFF DEM or orthophoto"),
)
RASTER_MODE_ITEMS = (
    ("AUTO", "Automatic", "A DEM for one band of floats or 16/32-bit integers"),
    ("DEM", "DEM", "A height mesh from band 1"),
    ("ORTHO", "Orthophoto", "The image on a plane over its footprint"),
)
REQUEST_KEYS = (
    "kind",
    "filepath",
    "layer",
    "where",
    "within_site",
    "allow_grid_downloads",
    "mode",
    "step",
)


# --- the pending offer -------------------------------------------------------------


def offer(
    context: bpy.types.Context, request: dict[str, object], refusal: NeedsProjectOrigin
) -> None:
    """Remember the refused import and show the refusal with the one-click fix."""
    wm = context.window_manager
    if wm is None:
        return
    pending = {
        "request": {k: v for k, v in request.items() if k in REQUEST_KEYS},
        "message": str(refusal),
        "origin": refusal.origin.to_dict(),
    }
    wm.geotools_origin_offer = json.dumps(pending, sort_keys=True)
    if getattr(context, "window", None) is not None and not bpy.app.background:
        wm.popup_menu(_draw_popup, title="Import Refused", icon="ERROR")
    redraw_ui(context)


def pending_offer(context: bpy.types.Context) -> dict[str, object] | None:
    """The refused import the Linked Data panel offers to fix, if any."""
    wm = context.window_manager
    text = getattr(wm, "geotools_origin_offer", "") if wm is not None else ""
    if not text:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    return raw if isinstance(raw, dict) and isinstance(raw.get("request"), dict) else None


def clear_offer(context: bpy.types.Context) -> None:
    wm = context.window_manager
    if wm is not None and getattr(wm, "geotools_origin_offer", ""):
        wm.geotools_origin_offer = ""


def fill(op_props: object, request: dict[str, object]) -> None:
    """Set a :class:`GEOTOOLS_OT_use_data_origin` button's properties from a request."""
    for key in REQUEST_KEYS:
        if key in request:
            setattr(op_props, key, request[key])


def draw_offer(layout: bpy.types.UILayout, context: bpy.types.Context, pending: dict) -> None:
    """The refusal and its buttons, for the popup and the Linked Data panel."""
    box = layout.box()
    col = box.column(align=True)
    col.alert = True
    wrapped(col, context, str(pending.get("message", "")), "ERROR", max_lines=12)
    row = box.row(align=True)
    op = row.operator(
        GEOTOOLS_OT_use_data_origin.bl_idname,
        text="Use This Data's Centre as the Project Origin",
        icon="ORIENTATION_GLOBAL",
    )
    fill(op, pending["request"])
    row.operator(GEOTOOLS_OT_dismiss_origin_offer.bl_idname, text="", icon="X")


def _draw_popup(menu: bpy.types.UIPopupMenu, context: bpy.types.Context) -> None:
    pending = pending_offer(context)
    if pending is not None:
        draw_offer(menu.layout, context, pending)


# --- applying an origin ---------------------------------------------------------------


def source_object_count(scene: bpy.types.Scene) -> int:
    """Objects in the project's source collection (0 when there is none)."""
    collection = scene.geotools.source_collection
    return len(collection.all_objects) if collection is not None else 0


def needs_confirmation(scene: bpy.types.Scene) -> bool:
    """True when switching the origin would move an existing scene: a source with objects."""
    return source_object_count(scene) > 0


def apply_origin(props: bpy.types.PropertyGroup, origin: DataOrigin) -> None:
    """Switch the project to SOURCE_CRS with ``origin``'s CRS and offset (A is 0)."""
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = origin.source_crs
    for attr, value in zip(
        ("source_offset_e", "source_offset_n", "source_offset_a"), origin.offset, strict=True
    ):
        write_number(props, attr, value)
    props.offset_file = ""


_PLACEMENT_ATTRS = (
    "placement_mode",
    "source_crs",
    "source_offset_e",
    "source_offset_n",
    "source_offset_a",
    "offset_file",
)


def _run_import(context: bpy.types.Context, request: dict[str, object]) -> list[str]:
    """Run the refused import again; return the report lines. Raises like the importers."""
    kind = str(request["kind"])
    path = Path(bpy.path.abspath(str(request["filepath"])))
    if kind == "GEOTIFF":
        from . import raster_io

        obj, report = raster_io.import_geotiff(
            context, str(path), str(request.get("mode", "AUTO")), int(request.get("step", 1))
        )
        raster_io.select_imported(context, obj)
        return [*report.warnings, report.message]
    from . import vectors

    layers = [x for x in str(request.get("layer", "")).split(",") if x] if kind == "GPKG" else [""]
    if kind == "GPKG" and not layers:
        layers = [info.name for info in vectors.gpkg.list_layers(path)]
    result = vectors.import_vectors(
        context,
        kind,
        path,
        layers,
        where=str(request.get("where", "")),
        within_site=bool(request.get("within_site", False)),
        allow_grid_downloads=bool(request.get("allow_grid_downloads", False)),
    )
    vectors.select_imported(context, result.objects)
    noun = "object" if len(result.objects) == 1 else "objects"
    return [
        *result.notes,
        *result.warnings,
        f"Imported {len(result.objects)} linked {noun} from {path.name}",
    ]


def import_with_data_origin(
    context: bpy.types.Context, request: dict[str, object]
) -> tuple[DataOrigin | None, list[str]]:
    """Import ``request``; if it needs the data's origin, apply that first.

    Returns the origin applied (None when the import went through as the
    project was) and the report lines. On failure the placement is restored
    and the error is raised.
    """
    props = context.scene.geotools
    try:
        return None, _run_import(context, request)
    except NeedsProjectOrigin as ex:
        origin = ex.origin
    saved = {attr: getattr(props, attr) for attr in _PLACEMENT_ATTRS}
    apply_origin(props, origin)
    try:
        lines = _run_import(context, request)
    except Exception:
        for attr, value in saved.items():
            setattr(props, attr, value)
        raise
    return origin, lines


# --- operators -------------------------------------------------------------------------


class GEOTOOLS_OT_use_data_origin(bpy.types.Operator):
    """Switch the project to the data's CRS, with the centre of its extent (whole metres) as the offset and A 0, then import the file. Asks first when the project already has a source collection"""

    bl_idname = "geotools.use_data_origin"
    bl_label = "Use This Data's Centre as the Project Origin"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(name="Kind", items=KIND_ITEMS, default="GEOJSON")
    filepath: StringProperty(name="File", subtype="FILE_PATH")
    layer: StringProperty(
        name="Layer", description="GeoPackage tables, separated by commas; empty for every table"
    )
    where: StringProperty(name="Filter", description="GeoPackage SQL WHERE clause")
    within_site: BoolProperty(name="Within Site Extent", default=False)
    allow_grid_downloads: BoolProperty(name="Allow Grid Downloads", default=False)
    mode: EnumProperty(name="As", items=RASTER_MODE_ITEMS, default="AUTO")
    step: IntProperty(name="Decimation", default=1, min=1)

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        return True

    def _request(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in REQUEST_KEYS}

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if needs_confirmation(context.scene):
            return context.window_manager.invoke_props_dialog(
                self, width=440, title="Change the Project Origin?", confirm_text="Use Data Origin"
            )
        return self.execute(context)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        scene = context.scene
        props = scene.geotools
        mode = "Local" if props.placement_mode == "LOCAL" else "Anchor"
        if props.placement_mode == "SOURCE_CRS":
            mode = "CRS"
        collection = props.source_collection
        name = collection.name if collection is not None else ""
        count = source_object_count(scene)
        pending = pending_offer(context)
        origin = DataOrigin.from_dict(pending.get("origin")) if pending else None
        target = origin.describe() if origin is not None else "the data's CRS and centre"
        text = (
            f"This switches the project from {mode} to {target}. The source collection "
            f"'{name}' ({count} object{'s' if count != 1 else ''}) is then read as "
            "coordinates in that CRS relative to the offset, not as local metres, which "
            "changes how the existing scene is placed on Earth (Verify Placement and Publish "
            "use the new placement). Undo reverts it."
        )
        wrapped(layout, context, text, "ERROR", max_lines=16)

    def execute(self, context: bpy.types.Context) -> set[str]:
        request = self._request()
        if not request["filepath"]:
            self.report({"ERROR"}, "No file given")
            return {"CANCELLED"}
        try:
            origin, lines = import_with_data_origin(context, request)
        except (ValueError, RuntimeError, OSError) as ex:
            self.report({"ERROR"}, str(ex))
            redraw_ui(context)
            return {"CANCELLED"}
        clear_offer(context)
        if origin is not None:
            self.report({"INFO"}, f"Project origin set to {origin.describe()}")
        for line in lines:
            self.report({"INFO"}, line)
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_dismiss_origin_offer(bpy.types.Operator):
    """Hide the refused import from the Linked Data panel"""

    bl_idname = "geotools.dismiss_origin_offer"
    bl_label = "Dismiss"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        clear_offer(context)
        redraw_ui(context)
        return {"FINISHED"}


classes = (GEOTOOLS_OT_use_data_origin, GEOTOOLS_OT_dismiss_origin_offer)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.WindowManager.geotools_origin_offer = StringProperty(
        name="Refused Import", options={"SKIP_SAVE"}
    )


def unregister() -> None:
    if hasattr(bpy.types.WindowManager, "geotools_origin_offer"):
        del bpy.types.WindowManager.geotools_origin_offer
    _unregister_classes()
