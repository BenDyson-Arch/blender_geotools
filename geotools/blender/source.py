"""Source collection: ingest a mesh, read an offset file, summarise."""

from pathlib import Path

import bpy
from bpy.props import EnumProperty, StringProperty

from ..core import offsets
from ..core.preflight import SceneSummary
from .common import redraw_ui
from .properties import apply_source_offset, format_vec
from .summarise import summarise_collection

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
    props.summary_valid = True


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


def detect_offset(props: bpy.types.PropertyGroup, source: Path) -> tuple[set[str], str]:
    """Look for an offset file beside ``source`` and apply it.

    Returns the report level and a sentence for the operator report ("" if no
    file was found).
    """
    found = offsets.find_offset_file(source)
    if found is None:
        return {"INFO"}, ""
    try:
        offset = offsets.read_offset_file(found)
    except (offsets.OffsetFileError, OSError) as ex:
        return {"WARNING"}, f"Found {found.name} but could not read it: {ex}"
    apply_source_offset(props, offset, str(found))
    message = f"Offset read from {found.name}; placement set to Source CRS."
    if not props.source_crs.strip():
        return {"WARNING"}, message + " Set the source CRS."
    return {"INFO"}, message


class GEOTOOLS_OT_ingest_source(bpy.types.Operator):
    """Import a source mesh (glTF, OBJ or PLY) into the source collection"""

    bl_idname = "geotools.ingest_source"
    bl_label = "Ingest Source"
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
        path = Path(bpy.path.abspath(self.filepath))
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
        props.source_filepath = str(path)
        refresh_summary(context)

        level, offset_message = detect_offset(props, path)
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
                source_path = Path(bpy.path.abspath(source))
                found = offsets.find_offset_file(source_path)
                self.filepath = str(found) if found else str(source_path.parent) + "/"
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}
        path = Path(bpy.path.abspath(self.filepath))
        try:
            offset = offsets.read_offset_file(path)
        except offsets.OffsetFileError as ex:
            self.report({"ERROR"}, f"{path.name} is not a valid offset file: {ex}")
            return {"CANCELLED"}
        except OSError as ex:
            self.report({"ERROR"}, f"Cannot read {path}: {ex.strerror or ex}")
            return {"CANCELLED"}
        props = context.scene.geotools
        apply_source_offset(props, offset, str(path))
        message = f"Offset read from {path.name}; placement set to Source CRS."
        if props.source_crs.strip():
            self.report({"INFO"}, message)
        else:
            self.report({"WARNING"}, message + " Set the source CRS.")
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
    GEOTOOLS_OT_refresh_source_summary,
)

register, unregister = bpy.utils.register_classes_factory(classes)
