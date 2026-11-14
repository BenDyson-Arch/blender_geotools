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

Unlike BlenderGIS's own origin edits, which translate the scene's objects to
keep them on the same spot, this never moves objects: it defines where the
scene's coordinates are. So a scene that already has a complete GeoScene
georeference (``SRID``, ``crs x``, ``crs y``) is never given a new origin here;
the project adopts the scene's georeference instead, and the data is imported
against it (see docs/design/georeference-interop.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import bpy
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty

from ..core.data_origin import DataOrigin, NeedsProjectOrigin
from . import blend_paths, scene_georef
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


def store_offer(
    context: bpy.types.Context,
    prop: str,
    pending: dict[str, object],
    popup: object = None,
    title: str = "",
) -> None:
    """Keep an offer as JSON on the window manager's ``prop`` (not saved in the ``.blend``)
    and, with a window, pop it up under the mouse with ``popup`` drawing it."""
    wm = context.window_manager
    if wm is None:
        return
    setattr(wm, prop, json.dumps(pending, sort_keys=True))
    windowed = getattr(context, "window", None) is not None and not bpy.app.background
    if popup is not None and windowed:
        wm.popup_menu(popup, title=title, icon="ERROR")
    redraw_ui(context)


def load_offer(context: bpy.types.Context, prop: str, required: str) -> dict[str, object] | None:
    """The offer kept on ``prop``, if any (a dict that has ``required``)."""
    wm = context.window_manager
    text = getattr(wm, prop, "") if wm is not None else ""
    if not text:
        return None
    try:
        raw = json.loads(text)
    except ValueError:
        return None
    return raw if isinstance(raw, dict) and raw.get(required) is not None else None


def drop_offer(context: bpy.types.Context, prop: str) -> None:
    wm = context.window_manager
    if wm is not None and getattr(wm, prop, ""):
        setattr(wm, prop, "")


def offer(
    context: bpy.types.Context, request: dict[str, object], refusal: NeedsProjectOrigin
) -> None:
    """Remember the refused import and show the refusal with the one-click fix."""
    pending = {
        "request": {k: v for k, v in request.items() if k in REQUEST_KEYS},
        "message": str(refusal),
        "origin": refusal.origin.to_dict(),
    }
    store_offer(context, "geotools_origin_offer", pending, _draw_popup, "Import Refused")


def pending_offer(context: bpy.types.Context) -> dict[str, object] | None:
    """The refused import the Linked Data panel offers to fix, if any."""
    raw = load_offer(context, "geotools_origin_offer", "request")
    return raw if raw is not None and isinstance(raw.get("request"), dict) else None


def clear_offer(context: bpy.types.Context) -> None:
    drop_offer(context, "geotools_origin_offer")


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
    origin = DataOrigin.from_dict(pending.get("origin"))
    text = "Use This Data's Centre as the Project Origin"
    if context.scene is not None and scene_georef_held(context.scene):
        text = "Use the Scene's BlenderGIS Georeference"
    elif origin is not None and not origin.set_offset:
        text = f"Use {origin.epsg or 'the Data CRS'} as the Project CRS"
    op = row.operator(GEOTOOLS_OT_use_data_origin.bl_idname, text=text, icon="ORIENTATION_GLOBAL")
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


def scene_georef_held(scene: bpy.types.Scene) -> bool:
    """True when the scene has a complete GeoScene georeference (SRID and crs x/y).

    "Use data origin" then adopts it instead of moving the origin: objects
    another GIS tool (BlenderGIS) placed against that origin stay where they are
    on Earth. GeoTools never moves the origin of such a scene behind its back.
    """
    return scene_georef.read(scene).complete


def apply_origin(props: bpy.types.PropertyGroup, origin: DataOrigin) -> bool:
    """Switch the project to SOURCE_CRS with ``origin``'s CRS and E/N offset.

    A LOCAL or ANCHOR project becomes a new CRS project, with offset A = 0
    (``scene_georef.start_crs_project``): the data's centre says nothing about
    heights, and GeoScene tools read Z as absolute height. A SOURCE_CRS project
    keeps its A. An origin that only offers its CRS (``set_offset`` False) keeps
    the offset. When the scene already holds a GeoScene georeference
    (:func:`scene_georef_held`), the project adopts it instead, and nothing
    moves. Returns True when it did.
    """
    scene = props.id_data
    adopt = scene_georef_held(scene)
    scene_georef.start_crs_project(props)  # reconciles: existing keys win
    if adopt:
        return True
    props.crs = origin.source_crs
    if not origin.set_offset:
        return False
    write_number(props, "origin_e", origin.offset[0])
    write_number(props, "origin_n", origin.offset[1])
    props.offset_file = ""
    return False


def _run_import(context: bpy.types.Context, request: dict[str, object]) -> list[str]:
    """Run the refused import again; return the report lines. Raises like the importers."""
    kind = str(request["kind"])
    path = Path(blend_paths.abspath(str(request["filepath"])))
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
    project was, or when the scene already held a GeoScene georeference that the
    project adopted instead, :func:`apply_origin`; the first report line then
    says so) and the report lines. On failure the placement (mode, GeoScene keys,
    GeoTools' own text) is restored exactly and the error is raised.
    """
    scene = context.scene
    try:
        return None, _run_import(context, request)
    except NeedsProjectOrigin as ex:
        origin = ex.origin
    saved = scene_georef.snapshot(scene)
    adopted = apply_origin(scene.geotools, origin)
    try:
        lines = _run_import(context, request)
    except Exception:
        scene_georef.restore(scene, saved)
        raise
    if adopted:
        lines.insert(
            0,
            "Used the scene's existing BlenderGIS georeference "
            f"({scene_georef.read(scene).describe()}) rather than the data's centre, so "
            "objects already placed against it stay put",
        )
        return None, lines
    return origin, lines


# --- operators -------------------------------------------------------------------------


class GEOTOOLS_OT_use_data_origin(bpy.types.Operator):
    """Switch the project to the data's CRS, with the centre of its extent (whole metres) as the E/N offset and A 0 (a CRS project keeps its A), then import the file. A scene that already has a BlenderGIS (GeoScene) georeference keeps it instead. Asks first when the project already has a source collection"""

    bl_idname = "geotools.use_data_origin"
    bl_label = "Use This Data's Centre as the Project Origin"
    bl_options = {"REGISTER", "UNDO"}

    kind: EnumProperty(
        name="Kind", description="The kind of file to import", items=KIND_ITEMS, default="GEOJSON"
    )
    filepath: StringProperty(name="File", description="The file to import", subtype="FILE_PATH")
    layer: StringProperty(
        name="Layer", description="GeoPackage tables, separated by commas; empty for every table"
    )
    where: StringProperty(name="Filter", description="GeoPackage SQL WHERE clause")
    within_site: BoolProperty(
        name="Within Site Extent",
        description="Only features whose bounds overlap the source collection's extent",
        default=False,
    )
    allow_grid_downloads: BoolProperty(
        name="Allow Grid Downloads",
        description="Let PROJ download a transformation grid from cdn.proj.org if a more "
        "accurate operation needs one",
        default=False,
    )
    mode: EnumProperty(
        name="As",
        description="Import a GeoTIFF as a DEM height mesh or an orthophoto on a plane",
        items=RASTER_MODE_ITEMS,
        default="AUTO",
    )
    step: IntProperty(
        name="Decimation",
        description="Use every nth pixel of a DEM; the spacing grows n times",
        default=1,
        min=1,
    )

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
        if scene_georef_held(scene):
            target = f"the scene's BlenderGIS georeference ({scene_georef.read(scene).describe()})"
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
