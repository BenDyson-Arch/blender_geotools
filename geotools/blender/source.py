"""Source collection: ingest a mesh, read an offset file, summarise.

Import Source Mesh reads an offset file found beside the mesh only when the
project has no origin yet, or already uses the same offset
(``core.data_origin.check_offset_file``). Otherwise the mesh is imported
unshifted, the project origin stays as it is, and the offer is kept on the
window manager (``geotools_offset_offer``, not saved) for the Source panel:
**Use This Offset File as the Project Origin** (which asks first, since the
source collection then has objects) and, in a CRS project, **Place the
Imported Objects at the Project Origin**. The offer reuses ``data_origin``'s
offer machinery.
"""

import json
import os
from pathlib import Path

import bpy
from bpy.props import EnumProperty, StringProperty

from ..core import geoscene, offsets, paths
from ..core.data_origin import (
    FAR_LOCATION_STEP_M,
    OffsetAction,
    OffsetFileCheck,
    check_offset_file,
    float32_step,
)
from ..core.preflight import ImageSummary, SceneSummary
from . import blend_paths, data_origin, scene_georef
from .common import redraw_ui
from .properties import FieldError, apply_source_offset, format_vec, parse_vec, read_number
from .summarise import summarise_collection
from .ui import wrapped

OFFSET_OFFER = "geotools_offset_offer"

SOURCE_COLLECTION_NAME = "GeoTools Source"
SOURCE_EXTENSIONS = (".glb", ".gltf", ".obj", ".ply")

UP_AXIS_ITEMS = (
    ("Z", "Z Up", "The file is Z-up, as photogrammetry OBJ and PLY exports usually are"),
    ("Y", "Y Up", "The file is Y-up (OBJ convention): Y becomes Blender Z"),
)

# Importer axis keywords for OBJ and PLY. "forward" is the file axis that becomes
# Blender +Y and "up" the one that becomes Blender +Z.
#   Z: identity, file (x, y, z) -> Blender (x, y, z)
#   Y: file (x, y, z) -> Blender (x, -z, y), the usual OBJ Y-up conversion
AXIS_KEYWORDS: dict[str, dict[str, str]] = {
    "Z": {"forward_axis": "Y", "up_axis": "Z"},
    "Y": {"forward_axis": "NEGATIVE_Z", "up_axis": "Y"},
}


# --- collection --------------------------------------------------------------------


def ensure_source_collection(scene: bpy.types.Scene) -> bpy.types.Collection:
    """The scene's source collection, created and linked to the scene if needed."""
    props = scene.geotools
    collection = props.source_collection
    if collection is None:
        collection = bpy.data.collections.new(SOURCE_COLLECTION_NAME)
        props.source_collection = collection
    if collection not in scene.collection.children_recursive:
        scene.collection.children.link(collection)
    return collection


def move_into(objects: list[bpy.types.Object], collection: bpy.types.Collection) -> None:
    """Link ``objects`` to ``collection`` only."""
    for obj in objects:
        if collection not in obj.users_collection:
            collection.objects.link(obj)
        for other in list(obj.users_collection):
            if other != collection:
                other.objects.unlink(obj)


# --- summary cache ------------------------------------------------------------------


def store_summary(props: bpy.types.PropertyGroup, summary: SceneSummary) -> None:
    meshes = [o for o in summary.objects if o.type == "MESH"]
    props.summary_objects = len(summary.objects)
    props.summary_meshes = len(meshes)
    props.summary_faces = sum(o.faces for o in meshes)
    props.summary_vertices = sum(o.vertices for o in meshes)
    bounds = summary.bounds()
    props.summary_bounds_min = format_vec(bounds[0]) if bounds else ""
    props.summary_bounds_max = format_vec(bounds[1]) if bounds else ""
    store_images(props, source_images(summary))
    props.summary_valid = True


def source_images(summary: SceneSummary) -> list[ImageSummary]:
    """Each image the source meshes' materials use, once, sorted by name."""
    found: dict[str, ImageSummary] = {}
    for obj in summary.objects:
        if obj.type != "MESH":
            continue
        for material in obj.materials:
            for image in material.images:
                found.setdefault(image.name, image)
    return [found[name] for name in sorted(found)]


def store_images(props: bpy.types.PropertyGroup, images: list[ImageSummary]) -> None:
    props.summary_images.clear()
    for image in images:
        item = props.summary_images.add()
        item.name = image.name
        item.width, item.height = image.width, image.height
        item.file_format = image.file_format
        item.file_bytes = -1.0 if image.file_bytes is None else float(image.file_bytes)
        item.bit_depth = image.bit_depth or 0
    props.summary_images_index = min(props.summary_images_index, max(len(images) - 1, 0))


def refresh_summary(context: bpy.types.Context) -> SceneSummary | None:
    """Summarise the source collection and cache the counts for the panel."""
    props = context.scene.geotools
    if props.source_collection is None:
        props.summary_valid = False
        return None
    summary = summarise_collection(props.source_collection, context)
    store_summary(props, summary)
    return summary


# --- import ------------------------------------------------------------------------


def import_file(filepath: Path, up_axis: str) -> None:
    """Run Blender's own importer for ``filepath``. Raises RuntimeError on failure."""
    suffix = filepath.suffix.lower()
    if suffix in {".glb", ".gltf"}:
        # glTF is Y-up by definition; the importer converts it to Blender Z-up.
        bpy.ops.import_scene.gltf(filepath=str(filepath), import_scene_as_collection=False)
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(filepath), **AXIS_KEYWORDS[up_axis])
    elif suffix == ".ply":
        bpy.ops.wm.ply_import(filepath=str(filepath), **AXIS_KEYWORDS[up_axis])
    else:
        raise RuntimeError(f"unsupported file type {suffix!r}; use .glb, .gltf, .obj or .ply")


def _object_mode_poll(cls: type, context: bpy.types.Context) -> bool:
    if context.scene is None:
        return False
    if context.mode != "OBJECT":
        cls.poll_message_set("Switch to Object Mode first")
        return False
    return True


def project_origin(props: bpy.types.PropertyGroup) -> tuple[float | None, ...]:
    """The project's E, N and A as numbers; None where a field holds none."""
    found: list[float | None] = []
    for attr in ("origin_e", "origin_n", "source_offset_a"):
        try:
            found.append(read_number(props, attr))
        except FieldError:
            found.append(None)
    return tuple(found)


def check_offset(props: bpy.types.PropertyGroup, offset: tuple[float, float, float]):
    """``core.data_origin.check_offset_file`` for this project and scene."""
    scene = props.id_data
    return check_offset_file(
        props.placement_mode, scene_georef.read(scene), project_origin(props), offset
    )


def root_objects(objects: list[bpy.types.Object]) -> list[bpy.types.Object]:
    """The objects of ``objects`` whose parent is not among them."""
    members = set(objects)
    return [obj for obj in objects if obj.parent is None or obj.parent not in members]


def detect_offset(
    props: bpy.types.PropertyGroup,
    source: Path,
    context: bpy.types.Context | None = None,
    objects: list[bpy.types.Object] = (),  # type: ignore[assignment]
) -> tuple[set[str], str]:
    """Look for an offset file beside ``source`` and apply it when it may set the origin.

    It is applied when the project has no origin yet; a file that matches the
    project's origin exactly changes nothing. Otherwise the project is left as
    it is and, with ``context``, the file is offered in the Source panel for
    the imported ``objects``. Returns the report level and a sentence for the
    operator report ("" if no file was found).
    """
    found = offsets.find_offset_file(source)
    if found is None:
        return {"INFO"}, ""
    try:
        offset = offsets.read_offset_file(found)
    except (offsets.OffsetFileError, OSError) as ex:
        return {"WARNING"}, f"Found {found.name} but could not read it: {ex}"
    portable = blend_paths.portable(found)
    check = check_offset(props, offset)
    if context is not None:
        clear_offset_offer(context)
    if check.action is OffsetAction.OFFER:
        if context is not None:
            offer_offset_file(context, check, portable, objects)
        return {"WARNING"}, check.message(found.name)
    if check.action is OffsetAction.MATCH:
        if not props.offset_file:
            props.offset_file = portable
        message = f"The offset in {found.name} is the project's origin already."
    else:
        apply_source_offset(props, offset, portable)
        message = f"Offset read from {found.name}; placement set to Source CRS."
    if not props.crs.strip():
        return {"WARNING"}, message + " Set the source CRS."
    return {"INFO"}, message


# --- the offer: an offset file that was not applied -----------------------------------


def offer_offset_file(
    context: bpy.types.Context,
    check: OffsetFileCheck,
    path: str,
    objects: list[bpy.types.Object],
) -> None:
    """Keep the offset file the import did not apply, for the Source panel's buttons."""
    pending = {
        "path": path,
        "message": check.message(paths.name(path)),
        "offset": list(check.offset),
        "shift": list(check.shift) if check.shift is not None else None,
        "objects": [obj.name for obj in root_objects(list(objects))],
    }
    data_origin.store_offer(context, OFFSET_OFFER, pending, _draw_popup, "Offset File Not Applied")


def pending_offset_offer(context: bpy.types.Context) -> dict[str, object] | None:
    """The offset file the Source panel offers, if any."""
    return data_origin.load_offer(context, OFFSET_OFFER, "path")


def clear_offset_offer(context: bpy.types.Context) -> None:
    data_origin.drop_offer(context, OFFSET_OFFER)


def draw_offset_offer(layout: bpy.types.UILayout, context: bpy.types.Context) -> None:
    """The offset file that was not applied, with its buttons (the Source panel)."""
    pending = pending_offset_offer(context)
    if pending is None:
        return
    box = layout.box()
    col = box.column(align=True)
    col.alert = True
    wrapped(col, context, str(pending.get("message", "")), "ERROR", max_lines=12)
    col = box.column(align=True)
    row = col.row(align=True)
    op = row.operator(GEOTOOLS_OT_use_offset_file_origin.bl_idname, icon="ORIENTATION_GLOBAL")
    op.filepath = str(pending["path"])
    row.operator(GEOTOOLS_OT_dismiss_offset_offer.bl_idname, text="", icon="X")
    shift = pending.get("shift")
    if isinstance(shift, list) and len(shift) == 3:
        op = col.operator(GEOTOOLS_OT_place_at_project_origin.bl_idname, icon="OBJECT_ORIGIN")
        op.objects = json.dumps(pending.get("objects") or [])
        op.shift = format_vec(tuple(float(v) for v in shift))  # type: ignore[arg-type]


def _draw_popup(menu: bpy.types.UIPopupMenu, context: bpy.types.Context) -> None:
    draw_offset_offer(menu.layout, context)


def use_offset_file(props: bpy.types.PropertyGroup, offset, path: str) -> None:
    """Make the offset file the project origin: a CRS project with its E, N and A.

    A Local or Anchor project's own E/N text from before is dropped first: the
    file replaces it, and it must not be reported as overruled by the scene's
    keys when the project becomes a CRS project.
    """
    if props.placement_mode != "SOURCE_CRS":
        for name in (geoscene.STORE_E, geoscene.STORE_N):
            props.property_unset(name)
    apply_source_offset(props, offset, path)


class GEOTOOLS_OT_ingest_source(bpy.types.Operator):
    """Import a source mesh (glTF, OBJ or PLY) into the source collection"""

    bl_idname = "geotools.ingest_source"
    bl_label = "Import Source Mesh"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.glb;*.gltf;*.obj;*.ply", options={"HIDDEN"})
    up_axis: EnumProperty(
        name="Up Axis",
        items=UP_AXIS_ITEMS,
        default="Z",
        description="Up axis of OBJ and PLY files; glTF is always Y-up and converted for you",
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _object_mode_poll(cls, context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "up_axis")
        layout.label(text="OBJ and PLY only; glTF is Y-up", icon="INFO")

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}
        path = Path(blend_paths.abspath(self.filepath))
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            self.report({"ERROR"}, f"{path.name}: choose a .glb, .gltf, .obj or .ply file")
            return {"CANCELLED"}
        if not path.is_file():
            self.report({"ERROR"}, f"File not found: {path}")
            return {"CANCELLED"}

        scene = context.scene
        props = scene.geotools
        collection = ensure_source_collection(scene)
        before = set(bpy.data.objects)
        try:
            import_file(path, self.up_axis)
        except RuntimeError as ex:
            self.report({"ERROR"}, f"Import of {path.name} failed: {ex}")
            return {"CANCELLED"}
        new = [obj for obj in bpy.data.objects if obj not in before]
        if not new:
            self.report({"ERROR"}, f"{path.name} contained nothing to import")
            return {"CANCELLED"}
        move_into(new, collection)
        props.source_filepath = blend_paths.portable(path)
        refresh_summary(context)

        level, offset_message = detect_offset(props, path, context, new)
        noun = "object" if len(new) == 1 else "objects"
        message = f"Imported {len(new)} {noun} from {path.name} into '{collection.name}'."
        self.report(level, f"{message} {offset_message}".strip())
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_load_offset_file(bpy.types.Operator):
    """Read an E/N/A offset file into the source offset"""

    bl_idname = "geotools.load_offset_file"
    bl_label = "Load Offset File"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.txt", options={"HIDDEN"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene is not None

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if not self.filepath:
            source = context.scene.geotools.source_filepath
            if source:
                source_path = Path(blend_paths.abspath(source))
                found = offsets.find_offset_file(source_path)
                self.filepath = str(found) if found else os.path.join(source_path.parent, "")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}
        path = Path(blend_paths.abspath(self.filepath))
        try:
            offset = offsets.read_offset_file(path)
        except offsets.OffsetFileError as ex:
            self.report({"ERROR"}, f"{path.name} is not a valid offset file: {ex}")
            return {"CANCELLED"}
        except OSError as ex:
            self.report({"ERROR"}, f"Cannot read {path}: {ex.strerror or ex}")
            return {"CANCELLED"}
        props = context.scene.geotools
        apply_source_offset(props, offset, blend_paths.portable(path))
        message = f"Offset read from {path.name}; placement set to Source CRS."
        if props.crs.strip():
            self.report({"INFO"}, message)
        else:
            self.report({"WARNING"}, message + " Set the source CRS.")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_use_offset_file_origin(bpy.types.Operator):
    """Make this offset file the project origin: a CRS project with its E, N and A. Objects keep their Blender positions, so the source collection's existing objects move on Earth. Asks first when the source collection has objects"""

    bl_idname = "geotools.use_offset_file_origin"
    bl_label = "Use This Offset File as the Project Origin"
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(name="Offset File", subtype="FILE_PATH")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _object_mode_poll(cls, context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        if data_origin.needs_confirmation(context.scene):
            return context.window_manager.invoke_props_dialog(
                self, width=440, title="Change the Project Origin?", confirm_text="Use Offset File"
            )
        return self.execute(context)

    def draw(self, context: bpy.types.Context) -> None:
        scene = context.scene
        collection = scene.geotools.source_collection
        count = data_origin.source_object_count(scene)
        name = collection.name if collection is not None else ""
        text = (
            f"This makes {paths.name(self.filepath)} the project origin. The source collection "
            f"'{name}' ({count} object{'s' if count != 1 else ''}) keeps its Blender positions, "
            "so objects placed against the current origin move on Earth (Verify Placement and "
            "Publish use the new origin). Undo reverts it."
        )
        if scene_georef.read(scene).complete:
            text += " The scene's BlenderGIS georeference (crs x, crs y) changes with it."
        wrapped(self.layout, context, text, "ERROR", max_lines=16)

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.filepath:
            self.report({"ERROR"}, "No offset file given")
            return {"CANCELLED"}
        path = Path(blend_paths.abspath(self.filepath))
        try:
            offset = offsets.read_offset_file(path)
        except offsets.OffsetFileError as ex:
            self.report({"ERROR"}, f"{path.name} is not a valid offset file: {ex}")
            return {"CANCELLED"}
        except OSError as ex:
            self.report({"ERROR"}, f"Cannot read {path}: {ex.strerror or ex}")
            return {"CANCELLED"}
        props = context.scene.geotools
        use_offset_file(props, offset, blend_paths.portable(path))
        clear_offset_offer(context)
        e, n, a = offset
        message = f"Project origin set from {path.name}: E {e!r}, N {n!r}, A {a!r}."
        if props.crs.strip():
            self.report({"INFO"}, message)
        else:
            self.report({"WARNING"}, message + " Set the source CRS.")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_place_at_project_origin(bpy.types.Operator):
    """Move the imported objects by the difference between their offset file and the project origin (computed in double precision), so they sit at the project origin. The project origin does not change"""

    bl_idname = "geotools.place_at_project_origin"
    bl_label = "Place the Imported Objects at the Project Origin"
    bl_options = {"REGISTER", "UNDO"}

    objects: StringProperty(name="Objects", description="The imported root objects, as JSON")
    shift: StringProperty(
        name="Shift", description="Offset file minus project origin: E, N, A, as exact text"
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _object_mode_poll(cls, context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            names = json.loads(self.objects or "[]")
            shift = parse_vec(self.shift)
        except ValueError:
            names, shift = [], None
        if shift is None or not isinstance(names, list):
            self.report({"ERROR"}, "No shift to apply")
            return {"CANCELLED"}
        objects = [bpy.data.objects[n] for n in names if n in bpy.data.objects]
        if not objects:
            self.report({"ERROR"}, "The imported objects are gone")
            return {"CANCELLED"}
        for obj in objects:
            # float64 sum, rounded once to Blender's single-precision location.
            obj.location = tuple(float(c) + d for c, d in zip(obj.location, shift, strict=True))
        clear_offset_offer(context)
        refresh_summary(context)
        de, dn, da = shift
        noun = "object" if len(objects) == 1 else "objects"
        self.report(
            {"INFO"},
            f"Moved {len(objects)} imported {noun} by E {de!r}, N {dn!r}, A {da!r} to the "
            "project origin",
        )
        far = max(abs(c) for obj in objects for c in obj.location)
        step = float32_step(far)
        if step > FAR_LOCATION_STEP_M:
            self.report(
                {"WARNING"},
                f"The objects now sit up to {far / 1000.0:,.1f} km from the scene origin. "
                f"Blender keeps object locations in single precision, so their positions are "
                f"rounded to about {step * 1000.0:g} mm. Use the offset file as the project "
                "origin instead to keep full precision.",
            )
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_dismiss_offset_offer(bpy.types.Operator):
    """Hide the offset file that was not applied from the Source panel"""

    bl_idname = "geotools.dismiss_offset_offer"
    bl_label = "Dismiss"
    bl_options = {"INTERNAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        clear_offset_offer(context)
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_refresh_source_summary(bpy.types.Operator):
    """Recount the objects, faces and bounds of the source collection"""

    bl_idname = "geotools.refresh_source_summary"
    bl_label = "Refresh Summary"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None or context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a source collection first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        summary = refresh_summary(context)
        props = context.scene.geotools
        if summary is None or not props.summary_meshes:
            self.report({"WARNING"}, "The source collection has no meshes")
        else:
            self.report(
                {"INFO"},
                f"{props.summary_meshes} meshes, {props.summary_faces:,} faces, "
                f"{props.summary_vertices:,} vertices",
            )
        redraw_ui(context)
        return {"FINISHED"}


classes = (
    GEOTOOLS_OT_ingest_source,
    GEOTOOLS_OT_load_offset_file,
    GEOTOOLS_OT_use_offset_file_origin,
    GEOTOOLS_OT_place_at_project_origin,
    GEOTOOLS_OT_dismiss_offset_offer,
    GEOTOOLS_OT_refresh_source_summary,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    setattr(
        bpy.types.WindowManager,
        OFFSET_OFFER,
        StringProperty(name="Offset File Not Applied", options={"SKIP_SAVE"}),
    )


def unregister() -> None:
    if hasattr(bpy.types.WindowManager, OFFSET_OFFER):
        delattr(bpy.types.WindowManager, OFFSET_OFFER)
    _unregister_classes()
