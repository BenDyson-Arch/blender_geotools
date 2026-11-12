"""Output inspection (M7): open published tiles in Blender and trace them to the source.

* **List**: the tiles of the last published archive (the active build
  profile's) or a chosen ``.3tz``, parent before children, with role, geometric
  error and triangle count (read cheaply from each tile's GLB header).
* **Open**: a tile becomes a read-only mesh object in the "GeoTools Output"
  collection, built from rusty-tiles' ``"arrays"`` view (``core.output_tiles``)
  with ``foreach_set``: per-face ``source_primitive`` and ``source_triangle``
  FACE attributes, its texture pages as packed images, its materials, and
  positions placed by the build's recorded placement
  (``core.output_frame``), so it overlays the source.
* **Trace**: Select Source Faces (output faces -> source polygons) and Select
  Output Faces (source polygons -> faces of the opened tiles), through the face
  map beside the archive (``core.inspect``). Refused when the face map no
  longer fits the scene.
* **Quality**: a distance heatmap to the source (the batch 2 Distance to
  Surface preset, against the source collection), a texel-density view, and a
  comparison of levels (one depth at a time, or side by side).

Settings and results live on ``scene.geotools_inspect``; panels are in
``inspect_ui``. See docs/design/m7-output-inspection.md.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import bpy
import numpy as np
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)

from ..core import output_frame, output_tiles, ramps
from ..core.facemap import FaceMap, topology_digest
from ..core.inspect import (
    InspectError,
    OutputRecord,
    assemble,
    depth_counts,
    placement_differences,
    selected_rows,
    source_faces,
    summarise_density,
    texel_density,
    tile_face_mask,
    topology_problems,
)
from ..core.output_frame import FrameError, SceneFrame
from ..core.output_tiles import OutputTilesError, TileEntry
from ..core.placement import PlacementMode
from .common import redraw_ui
from .properties import FieldError, placement_from_props

log = logging.getLogger(__name__)

OUTPUT_COLLECTION = "GeoTools Output"
TAG = "geotools_output"
"""Custom property on opened tiles (JSON) and on their materials and images (1)."""
DISTANCE_GROUP = "GeoTools Output Distance"
DISTANCE_ATTRIBUTE = "surface_distance"
DENSITY_ATTRIBUTE = "texel_density"
DISTANCE_COLOURS = "gt_inspect_distance"
DENSITY_COLOURS = "gt_inspect_density"
_NO_DATA = (80, 80, 80, 255)

SOURCE_ITEMS = (
    ("LAST", "Last Publish", "The archive the active build profile published last"),
    ("FILE", "File", "A .3tz archive chosen on disk"),
)
SCOPE_ITEMS = (
    ("SELECTED", "Selected Tile", "Open the tile selected in the list"),
    ("DEPTH", "One Depth", "Open every tile at one depth"),
    ("LEAVES", "All Leaves", "Open every leaf tile"),
)
VIEW_ITEMS = (
    ("ALL", "All", "Show every opened tile in place"),
    ("DEPTH", "One Depth", "Show only the opened tiles at one depth, in place (same framing)"),
    ("SIDE", "Side by Side", "Offset each depth along +X so levels sit next to each other"),
)


class InspectRunError(RuntimeError):
    """The operator cannot go on; the message is for the user."""


# --- properties ------------------------------------------------------------------------------


class GeoToolsOutputTile(bpy.types.PropertyGroup):
    uri: StringProperty(name="URI")
    depth: IntProperty(name="Depth")
    role: StringProperty(name="Role")
    geometric_error: FloatProperty(name="Geometric Error", unit="LENGTH")
    triangles: IntProperty(name="Triangles", default=-1)


def _apply_view(self, context: bpy.types.Context) -> None:
    try:
        apply_view(context.scene)
    except Exception:  # an update callback must never raise
        log.exception("inspect: applying the view failed")


class GeoToolsInspectSettings(bpy.types.PropertyGroup):
    source: EnumProperty(name="Archive", items=SOURCE_ITEMS, default="LAST")
    filepath: StringProperty(name="File", subtype="FILE_PATH", description="A .3tz archive")
    archive: StringProperty(name="Listed Archive", description="The archive the list shows")
    tiles: CollectionProperty(type=GeoToolsOutputTile)
    tiles_index: IntProperty(name="Tile", default=0)
    notes: StringProperty(name="Notes", description="What listing the archive found")
    scope: EnumProperty(name="Open", items=SCOPE_ITEMS, default="SELECTED")
    open_depth: IntProperty(name="Depth", min=0, default=0)
    limit: IntProperty(
        name="Limit",
        min=1,
        default=64,
        description="Open at most this many tiles at once",
    )
    max_distance: FloatProperty(
        name="Max Distance",
        min=0.0,
        default=0.0,
        unit="LENGTH",
        description="Farther than this from the source is no data; 0: four times the "
        "tile's geometric error",
    )
    view: EnumProperty(name="Levels", items=VIEW_ITEMS, default="ALL", update=_apply_view)
    view_depth: IntProperty(name="Depth", min=0, default=0, update=_apply_view)
    gap: FloatProperty(name="Gap", min=0.0, default=10.0, unit="LENGTH", update=_apply_view)
    result: StringProperty(name="Result", description="The last trace or quality result")


def settings(context: bpy.types.Context) -> GeoToolsInspectSettings:
    return context.scene.geotools_inspect


# --- the archive and its frame ----------------------------------------------------------------


def last_output(scene: bpy.types.Scene) -> Path | None:
    props = scene.geotools
    if not 0 <= props.build_profile_index < len(props.build_profiles):
        return None
    profile = props.build_profiles[props.build_profile_index]
    if not profile.last_output or not profile.last_output.endswith(".3tz"):
        return None
    return Path(profile.last_output)


def chosen_archive(context: bpy.types.Context) -> Path | None:
    props = settings(context)
    if props.source == "LAST":
        return last_output(context.scene)
    if not props.filepath.strip():
        return None
    return Path(bpy.path.abspath(props.filepath.strip()))


_frames: dict[str, tuple[tuple, SceneFrame, list[str]]] = {}


def _stamp(path: Path | None) -> float | None:
    try:
        return path.stat().st_mtime if path is not None else None
    except OSError:
        return None


def frame_for(
    scene: bpy.types.Scene, record: OutputRecord, root: TileEntry | None
) -> tuple[SceneFrame, list[str]]:
    """The scene frame for ``record``'s archive and the notes about it (cached)."""
    record_path = record.staging / "build.json" if record.staging else None
    key = (_stamp(record.archive), _stamp(record_path), _current_kwargs_key(scene))
    cached = _frames.get(str(record.archive))
    if cached is not None and cached[0] == key:
        return cached[1], list(cached[2])
    notes: list[str] = []
    current = _current_kwargs(scene)
    if record.record is None:
        if current is None:
            raise InspectRunError(
                f"{record.archive.name} has no build record, and the scene's placement "
                "cannot be read"
            )
        frame = output_frame.scene_frame({"placement": current})
        notes.append(
            "No build record beside the archive: placed with the scene's current placement"
        )
    else:
        try:
            frame = output_frame.scene_frame(record.record)
        except FrameError as exc:
            raise InspectRunError(str(exc)) from exc
        recorded = record.placement_record or {}
        if current is not None:
            changed = placement_differences(recorded, current)
            if changed:
                notes.append(
                    "The scene's placement changed since this publish ("
                    + ", ".join(changed)
                    + "): tiles are placed with the recorded one"
                )
    if root is not None:
        mismatch = frame.root_mismatch(root.transform)
        if mismatch is not None and mismatch > 1e-3:
            notes.append(
                f"The archive's root transform differs from the record by {mismatch:.3g} m: "
                "the record may not belong to this archive"
            )
        bounds = output_frame.source_bounds(record.source_glb_path) if record.record else None
        if frame.mode is PlacementMode.SOURCE_CRS and bounds is not None:
            centre = tuple((np.asarray(bounds[0]) + np.asarray(bounds[1])) / 2.0)
            try:
                residual = frame.centre_residual(root.transform, centre)
            except FrameError as exc:
                raise InspectRunError(str(exc)) from exc
            size = float(np.linalg.norm(residual))
            if size > output_frame.CORRECT_ABOVE_M:
                frame = frame.with_correction(-residual)
                notes.append(
                    f"pyproj and rusty-tiles differ by {size * 1000:.1f} mm at the source "
                    "centre; corrected as a shift"
                )
            note = frame.operation_note()
            if note:
                notes.append(note)
    _frames[str(record.archive)] = (key, frame, notes)
    return frame, list(notes)


def _current_kwargs(scene: bpy.types.Scene) -> dict | None:
    try:
        return placement_from_props(scene.geotools).mesh_to_tileset_kwargs()
    except (FieldError, ValueError):
        return None


def _current_kwargs_key(scene: bpy.types.Scene) -> str:
    return json.dumps(_current_kwargs(scene), sort_keys=True, default=str)


def _load_record(archive: Path) -> OutputRecord:
    try:
        return OutputRecord.load(archive)
    except InspectError as exc:
        raise InspectRunError(str(exc)) from exc


def _list(archive: Path) -> list[TileEntry]:
    try:
        return output_tiles.list_tiles(archive)
    except OutputTilesError as exc:
        raise InspectRunError(str(exc)) from exc


def _root(tiles: list[TileEntry]) -> TileEntry | None:
    """The root tile; rusty-tiles' mesh output transforms the root only, so with no
    root content any tile carries the root transform."""
    return next((t for t in tiles if t.depth == 0), tiles[0] if tiles else None)


def list_archive(context: bpy.types.Context, archive: Path) -> list[str]:
    """Fill the tile list; returns notes."""
    props = settings(context)
    record = _load_record(archive)
    tiles = _list(archive)
    counts = output_tiles.triangle_counts(archive, [t.uri for t in tiles])
    root = _root(tiles)
    _frame, notes = frame_for(context.scene, record, root)
    if record.record is not None and record.face_map_path is None:
        notes.append("No face map beside the archive: faces cannot be traced")
    props.tiles.clear()
    for t in tiles:
        item = props.tiles.add()
        item.uri = t.uri
        item.depth = t.depth
        item.role = t.role
        item.geometric_error = t.geometric_error
        item.triangles = counts.get(t.uri, -1)
    props.tiles_index = 0
    props.archive = str(archive)
    props.notes = "\n".join(notes)
    return notes


# --- opening tiles ---------------------------------------------------------------------------


def output_collection(scene: bpy.types.Scene) -> bpy.types.Collection:
    collection = bpy.data.collections.get(OUTPUT_COLLECTION)
    if collection is None:
        collection = bpy.data.collections.new(OUTPUT_COLLECTION)
    if collection.name not in scene.collection.children:
        scene.collection.children.link(collection)
    return collection


def tile_info(obj: bpy.types.Object | None) -> dict | None:
    """The opened-tile record of ``obj``, or None if it is not an opened tile."""
    if obj is None or obj.type != "MESH":
        return None
    raw = obj.get(TAG)
    if not isinstance(raw, str):
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    return info if isinstance(info, dict) else None


def opened_tiles(scene: bpy.types.Scene, archive: str | None = None) -> list[bpy.types.Object]:
    """Opened tile objects in the scene, optionally of one archive."""
    found = []
    for obj in scene.objects:
        info = tile_info(obj)
        if info is not None and (archive is None or info.get("archive") == archive):
            found.append(obj)
    return found


def _image(name: str, page: np.ndarray) -> bpy.types.Image:
    height, width = int(page.shape[0]), int(page.shape[1])
    image = bpy.data.images.new(name, width, height, alpha=True)
    pixels = np.flipud(page).astype(np.float32).ravel()  # Blender's rows run bottom up
    pixels *= np.float32(1.0 / 255.0)
    image.pixels.foreach_set(pixels)
    image.pack()
    image[TAG] = 1
    return image


_NEAREST = {9728, 9984, 9986}


def _material(name: str, gltf: dict, image: bpy.types.Image | None, nearest: bool):
    material = bpy.data.materials.new(name)
    material[TAG] = 1
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next(node for node in tree.nodes if node.type == "BSDF_PRINCIPLED")
    pbr = gltf.get("pbrMetallicRoughness") or {}
    factor = [float(v) for v in pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))]
    bsdf.inputs["Base Color"].default_value = factor
    bsdf.inputs["Metallic"].default_value = float(pbr.get("metallicFactor", 1.0))
    bsdf.inputs["Roughness"].default_value = float(pbr.get("roughnessFactor", 1.0))
    material.diffuse_color = factor
    if image is not None:
        texture = tree.nodes.new("ShaderNodeTexImage")
        texture.image = image
        texture.location = (-400, 200)
        if nearest:
            texture.interpolation = "Closest"
        tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
    return material


def build_object(
    scene: bpy.types.Scene,
    archive: Path,
    tile: output_tiles.DecodedTile,
    frame: SceneFrame,
) -> bpy.types.Object:
    """A read-only mesh object of one decoded tile, in the output collection."""
    try:
        arrays = assemble(tile, frame)
    except (InspectError, FrameError) as exc:
        raise InspectRunError(f"{tile.uri}: {exc}") from exc
    stem = f"{archive.stem} d{tile.depth} {Path(tile.uri).stem}"
    mesh = bpy.data.meshes.new(stem)
    faces = arrays.faces
    mesh.vertices.add(arrays.positions.shape[0])
    mesh.vertices.foreach_set("co", arrays.positions.astype(np.float32).ravel())
    mesh.loops.add(3 * faces)
    mesh.loops.foreach_set("vertex_index", arrays.corner_verts)
    mesh.polygons.add(faces)
    mesh.polygons.foreach_set("loop_start", np.arange(0, 3 * faces, 3, dtype=np.int32))
    mesh.polygons.foreach_set("material_index", arrays.material)
    mesh.attributes.new(output_tiles.SOURCE_PRIMITIVE, "INT", "FACE").data.foreach_set(
        "value", arrays.source_primitive
    )
    if arrays.source_triangle is not None:
        mesh.attributes.new(output_tiles.SOURCE_TRIANGLE, "INT", "FACE").data.foreach_set(
            "value", arrays.source_triangle
        )
    if arrays.uvs is not None:
        mesh.uv_layers.new(name="UVMap").data.foreach_set("uv", arrays.uvs.ravel())
    if arrays.colors is not None:
        colours = mesh.color_attributes.new("Col", "FLOAT_COLOR", "POINT")
        colours.data.foreach_set("color", arrays.colors.ravel())
    mesh.update()
    if arrays.normals is not None:
        mesh.normals_split_custom_set_from_vertices(arrays.normals)

    images: dict[int, bpy.types.Image] = {}
    slot_texels: list[int] = []
    for slot, index in enumerate(arrays.materials):
        gltf = tile.materials[index] if index is not None and index < len(tile.materials) else {}
        page = tile.base_color_page(index)
        image = None
        if page is not None:
            if page not in images:
                images[page] = _image(f"{stem} page {page}", tile.pages[page])
            image = images[page]
        nearest = _nearest(tile, index)
        mesh.materials.append(_material(f"{stem} {slot}", gltf, image, nearest))
        shape = tile.pages[page].shape if page is not None else (0, 0)
        slot_texels.append(int(shape[0]) * int(shape[1]))

    obj = bpy.data.objects.new(stem, mesh)
    output_collection(scene).objects.link(obj)
    obj.lock_location = obj.lock_rotation = obj.lock_scale = (True, True, True)
    obj[TAG] = json.dumps(
        {
            "archive": str(archive),
            "uri": tile.uri,
            "depth": tile.depth,
            "role": tile.role,
            "geometric_error": tile.geometric_error,
            "slot_texels": slot_texels,
        }
    )
    return obj


def _nearest(tile: output_tiles.DecodedTile, material: int | None) -> bool:
    if material is None or material >= len(tile.materials):
        return False
    ref = (tile.materials[material].get("pbrMetallicRoughness") or {}).get("baseColorTexture")
    try:
        texture = tile.textures[ref["index"]]
        sampler = tile.samplers[texture["sampler"]]
    except (TypeError, KeyError, IndexError):
        return False
    return sampler.get("magFilter") in _NEAREST


def tiles_to_open(props: GeoToolsInspectSettings) -> list[GeoToolsOutputTile]:
    if props.scope == "SELECTED":
        if not 0 <= props.tiles_index < len(props.tiles):
            return []
        return [props.tiles[props.tiles_index]]
    if props.scope == "DEPTH":
        return [t for t in props.tiles if t.depth == props.open_depth]
    return [t for t in props.tiles if t.role == output_tiles.ROLE_LEAF]


def open_tiles(context: bpy.types.Context, uris: list[str]) -> tuple[list[bpy.types.Object], str]:
    """Open ``uris`` of the listed archive; returns the new objects and a summary."""
    props = settings(context)
    archive = Path(props.archive)
    record = _load_record(archive)
    frame, _notes = frame_for(context.scene, record, _root(_list(archive)))
    already = {tile_info(o)["uri"] for o in opened_tiles(context.scene, str(archive))}
    started = time.perf_counter()
    made = []
    skipped = 0
    triangles = 0
    for uri in uris:
        if uri in already:
            skipped += 1
            continue
        try:
            tile = output_tiles.decode_tile(archive, uri)
        except OutputTilesError as exc:
            raise InspectRunError(str(exc)) from exc
        made.append(build_object(context.scene, archive, tile, frame))
        triangles += tile.triangles
    seconds = time.perf_counter() - started
    summary = f"Opened {len(made)} tile{'s' * (len(made) != 1)} ({triangles:,} triangles) "
    summary += f"in {seconds:.2f} s"
    if skipped:
        summary += f"; {skipped} already open"
    if made:
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in made:
            obj.select_set(True)
        context.view_layer.objects.active = made[-1]
    apply_view(context.scene)
    return made, summary


def remove_tiles(scene: bpy.types.Scene) -> int:
    objects = opened_tiles(scene)
    meshes = {o.data for o in objects}
    for obj in objects:
        bpy.data.objects.remove(obj)
    for mesh in meshes:
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    for material in [m for m in bpy.data.materials if m.get(TAG) and m.users == 0]:
        bpy.data.materials.remove(material)
    for image in [i for i in bpy.data.images if i.get(TAG) and i.users == 0]:
        bpy.data.images.remove(image)
    group = bpy.data.node_groups.get(DISTANCE_GROUP)
    if group is not None and group.users == 0:
        bpy.data.node_groups.remove(group)
    return len(objects)


# --- comparing levels ------------------------------------------------------------------------


def apply_view(scene: bpy.types.Scene) -> None:
    """Show the opened tiles as ``scene.geotools_inspect.view`` asks."""
    props = scene.geotools_inspect
    objects = opened_tiles(scene)
    if not objects:
        return
    depths = sorted({int(tile_info(o)["depth"]) for o in objects})
    width = 0.0
    if props.view == "SIDE":
        xs = [corner[0] for o in objects for corner in o.bound_box]  # local = world, unmoved
        width = max(xs) - min(xs)
    view_layer = bpy.context.view_layer
    for obj in objects:
        depth = int(tile_info(obj)["depth"])
        visible = props.view != "DEPTH" or depth == props.view_depth
        if props.view == "SIDE":
            obj.location = (depths.index(depth) * (width + props.gap), 0.0, 0.0)
        else:
            obj.location = (0.0, 0.0, 0.0)
        if obj.name in view_layer.objects:
            obj.hide_set(not visible)


# --- tracing ---------------------------------------------------------------------------------


def _ensure_object_mode(context: bpy.types.Context) -> None:
    if context.mode != "OBJECT" and context.view_layer.objects.active is not None:
        bpy.ops.object.mode_set(mode="OBJECT")


def _get(collection, attr: str, count: int, dtype) -> np.ndarray:
    out = np.empty(count, dtype=dtype)
    if count:
        collection.foreach_get(attr, out)
    return out


def face_attribute(mesh: bpy.types.Mesh, name: str) -> np.ndarray | None:
    attr = mesh.attributes.get(name)
    if attr is None or attr.domain != "FACE":
        return None
    return _get(attr.data, "value", len(attr.data), np.int32)


def face_selection(mesh: bpy.types.Mesh) -> np.ndarray:
    return _get(mesh.polygons, "select", len(mesh.polygons), bool)


def write_face_selection(mesh: bpy.types.Mesh, mask: np.ndarray) -> None:
    """Select exactly the faces in ``mask`` (and their vertices and edges)."""
    from .export import _corner_verts, _loop_totals

    mask = np.asarray(mask, dtype=bool)
    totals = _loop_totals(mesh)
    corner_selected = np.repeat(mask, totals)
    verts = np.zeros(len(mesh.vertices), dtype=bool)
    verts[_corner_verts(mesh)[corner_selected]] = True
    edges = np.zeros(len(mesh.edges), dtype=bool)
    corner_edges = _get(mesh.loops, "edge_index", len(mesh.loops), np.int32)
    edges[corner_edges[corner_selected]] = True
    mesh.polygons.foreach_set("select", mask)
    mesh.vertices.foreach_set("select", verts)
    mesh.edges.foreach_set("select", edges)
    mesh.update()


def _current_digests(face_map: FaceMap) -> dict[str, str | None]:
    from .export import _topology_arrays

    current: dict[str, str | None] = {}
    for name in face_map.object_names:
        obj = bpy.data.objects.get(name)
        current[name] = (
            topology_digest(*_topology_arrays(obj.data))
            if obj is not None and obj.type == "MESH"
            else None
        )
    return current


def checked_face_map(archive: Path) -> FaceMap:
    """The archive's face map, refused (InspectRunError) unless it fits the scene."""
    try:
        face_map = _load_record(archive).face_map()
    except InspectError as exc:
        raise InspectRunError(str(exc)) from exc
    problems = topology_problems(face_map, _current_digests(face_map))
    if problems:
        more = f" (and {len(problems) - 2} more)" if len(problems) > 2 else ""
        raise InspectRunError(
            "The face map no longer fits the scene: "
            + "; ".join(problems[:2])
            + more
            + ". Publish again to trace these faces."
        )
    return face_map


def select_source(context: bpy.types.Context, tile: bpy.types.Object, edit: bool) -> str:
    """Select the source polygons of ``tile``'s selected faces; returns a summary."""
    info = tile_info(tile)
    if info is None:
        raise InspectRunError("Make an opened output tile active first")
    _ensure_object_mode(context)
    mesh = tile.data
    chosen = face_selection(mesh)
    if not chosen.any():
        raise InspectRunError(f"Select faces of {tile.name} in Edit Mode first")
    face_map = checked_face_map(Path(info["archive"]))
    primitive = face_attribute(mesh, output_tiles.SOURCE_PRIMITIVE)
    if primitive is None:
        raise InspectRunError(f"{tile.name} has no source_primitive attribute")
    triangle = face_attribute(mesh, output_tiles.SOURCE_TRIANGLE)
    found = source_faces(
        face_map, primitive[chosen], None if triangle is None else triangle[chosen]
    )
    if not found.count:
        raise InspectRunError("The selected faces carry no source IDs")
    for obj in context.selected_objects:
        obj.select_set(False)
    targets = []
    for name, polygons in found.polygons.items():
        obj = bpy.data.objects[name]
        mask = np.zeros(len(obj.data.polygons), dtype=bool)
        mask[polygons] = True
        write_face_selection(obj.data, mask)
        obj.select_set(True)
        targets.append(obj)
    context.view_layer.objects.active = targets[0]
    summary = f"Selected {found.count:,} source face{'s' * (found.count != 1)}"
    summary += f" on {', '.join(found.polygons)}"
    if found.whole_primitives:
        listed = ", ".join(str(int(p)) for p in found.primitives[:5])
        summary += (
            f". Coarse tile: its faces carry only a representative source primitive, so "
            f"every face of primitive {listed} is selected"
        )
    if found.unknown:
        summary += f"; {found.unknown} face{'s' * (found.unknown != 1)} had no source ID"
    if edit:
        bpy.ops.object.mode_set(mode="EDIT")
    return summary


def select_output(context: bpy.types.Context, edit: bool) -> str:
    """Select the faces of opened tiles that trace to the selected source polygons."""
    props = settings(context)
    archive = props.archive
    if not archive:
        raise InspectRunError("List an archive first")
    tiles = opened_tiles(context.scene, archive)
    if not tiles:
        raise InspectRunError("Open tiles of the listed archive first")
    _ensure_object_mode(context)
    face_map = checked_face_map(Path(archive))
    selection = {}
    for name in face_map.object_names:
        obj = bpy.data.objects.get(name)
        if obj is not None and obj.type == "MESH":
            selection[name] = face_selection(obj.data)
    rows = selected_rows(face_map, selection)
    if not rows.any():
        raise InspectRunError("Select source faces in Edit Mode first")
    for obj in context.selected_objects:
        obj.select_set(False)
    total = 0
    coarse = 0
    hit = []
    for obj in tiles:
        mesh = obj.data
        primitive = face_attribute(mesh, output_tiles.SOURCE_PRIMITIVE)
        if primitive is None:
            continue
        triangle = face_attribute(mesh, output_tiles.SOURCE_TRIANGLE)
        mask = tile_face_mask(face_map, rows, primitive, triangle)
        write_face_selection(mesh, mask)
        if mask.any():
            obj.select_set(True)
            hit.append(obj)
            total += int(mask.sum())
            coarse += triangle is None
    if not hit:
        return "No opened tile holds the selected source faces"
    context.view_layer.objects.active = hit[0]
    summary = f"Selected {total:,} output face{'s' * (total != 1)} in {len(hit)} tile"
    summary += "s" * (len(hit) != 1)
    if coarse:
        summary += (
            f"; {coarse} coarse tile{'s' * (coarse != 1)} matched by representative source "
            "primitive only"
        )
    if edit:
        bpy.ops.object.mode_set(mode="EDIT")
    return summary


# --- quality ---------------------------------------------------------------------------------


def _source_collection(scene: bpy.types.Scene, info: dict) -> bpy.types.Collection:
    record = _load_record(Path(info["archive"]))
    name = record.source_collection
    collection = bpy.data.collections.get(name) if name else None
    if collection is None:
        collection = scene.geotools.source_collection
    if collection is None:
        raise InspectRunError("The source collection of this publish is not in the file")
    return collection


def distance_group() -> bpy.types.NodeTree:
    """Stock Geometry Nodes: each face's signed distance from its centre to the
    nearest face of the Source collection (Geometry Proximity), stored as a FACE
    attribute. Positive on the side the source faces (above, for terrain)."""
    group = bpy.data.node_groups.get(DISTANCE_GROUP)
    if group is not None:
        return group
    group = bpy.data.node_groups.new(DISTANCE_GROUP, "GeometryNodeTree")
    group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    group.interface.new_socket("Source", in_out="INPUT", socket_type="NodeSocketCollection")
    group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes, links = group.nodes, group.links

    def node(kind: str, **settings):
        made = nodes.new(kind)
        for key, value in settings.items():
            setattr(made, key, value)
        return made

    inputs = node("NodeGroupInput")
    outputs = node("NodeGroupOutput")
    info = node("GeometryNodeCollectionInfo", transform_space="RELATIVE")
    links.new(inputs.outputs["Source"], info.inputs["Collection"])
    realize = node("GeometryNodeRealizeInstances")
    links.new(info.outputs["Instances"], realize.inputs["Geometry"])
    reference = realize.outputs["Geometry"]
    position = node("GeometryNodeInputPosition").outputs["Position"]
    nearest = node("GeometryNodeProximity", target_element="FACES")
    links.new(reference, nearest.inputs["Geometry"])
    links.new(position, nearest.inputs["Sample Position"])
    normal = node("GeometryNodeFieldOnDomain", domain="FACE", data_type="FLOAT_VECTOR")
    links.new(node("GeometryNodeInputNormal").outputs["Normal"], normal.inputs["Value"])
    sample = node("GeometryNodeSampleNearestSurface", data_type="FLOAT_VECTOR")
    links.new(reference, sample.inputs["Mesh"])
    links.new(normal.outputs["Value"], sample.inputs["Value"])
    links.new(position, sample.inputs["Sample Position"])
    offset = node("ShaderNodeVectorMath", operation="SUBTRACT")
    links.new(position, offset.inputs[0])
    links.new(nearest.outputs["Position"], offset.inputs[1])
    dot = node("ShaderNodeVectorMath", operation="DOT_PRODUCT")
    links.new(offset.outputs["Vector"], dot.inputs[0])
    links.new(sample.outputs["Value"], dot.inputs[1])
    sign = node("ShaderNodeMath", operation="SIGN")
    links.new(dot.outputs["Value"], sign.inputs[0])
    signed = node("ShaderNodeMath", operation="MULTIPLY")
    links.new(sign.outputs["Value"], signed.inputs[0])
    links.new(nearest.outputs["Distance"], signed.inputs[1])
    store = node("GeometryNodeStoreNamedAttribute", data_type="FLOAT", domain="FACE")
    store.inputs["Name"].default_value = DISTANCE_ATTRIBUTE
    links.new(inputs.outputs["Geometry"], store.inputs["Geometry"])
    links.new(signed.outputs["Value"], store.inputs["Value"])
    links.new(store.outputs["Geometry"], outputs.inputs["Geometry"])
    return group


def _write_colours(mesh: bpy.types.Mesh, name: str, rgba_faces: np.ndarray) -> None:
    """Per-face sRGB colours into a BYTE_COLOR corner attribute, made active."""
    attr = mesh.attributes.get(name)
    if attr is not None:
        mesh.attributes.remove(attr)
    attr = mesh.color_attributes.new(name, "BYTE_COLOR", "CORNER")
    from .export import _loop_totals

    floats = np.repeat(rgba_faces, _loop_totals(mesh), axis=0).astype(np.float32) / 255.0
    attr.data.foreach_set("color_srgb", floats.ravel())
    mesh.color_attributes.active_color_name = name
    mesh.update_tag()


def _write_float(mesh: bpy.types.Mesh, name: str, domain: str, values: np.ndarray) -> None:
    attr = mesh.attributes.get(name)
    if attr is not None:
        mesh.attributes.remove(attr)
    mesh.attributes.new(name, "FLOAT", domain).data.foreach_set(
        "value", np.asarray(values, dtype=np.float32)
    )


def show_attribute_colours(context: bpy.types.Context) -> None:
    """Viewport solid shading by colour attribute, where there is a 3D view."""
    screen = context.screen
    if screen is None:
        return
    for area in screen.areas:
        if area.type == "VIEW_3D":
            for space in area.spaces:
                if space.type == "VIEW_3D" and space.shading.type == "SOLID":
                    space.shading.color_type = "VERTEX"


def distance_heatmap(context: bpy.types.Context, tile: bpy.types.Object) -> str:
    """Each face's distance from its centre to the source surface, as a heatmap.

    Measured at face centres, where a flat coarse triangle departs most from a
    curved source: simplification keeps vertices on the source, so distances at
    vertices (the Distance to Surface preset's) are close to zero on every level."""
    info = tile_info(tile)
    if info is None:
        raise InspectRunError("Make an opened output tile active first")
    _ensure_object_mode(context)
    props = settings(context)
    from .analysis_presets import set_input

    limit = props.max_distance or max(4.0 * float(info["geometric_error"]), 0.01)
    source = _source_collection(context.scene, info)
    modifier = tile.modifiers.new(DISTANCE_GROUP, "NODES")
    try:
        modifier.node_group = distance_group()
        set_input(modifier, "Source", source)
        depsgraph = context.evaluated_depsgraph_get()
        depsgraph.update()
        evaluated = tile.evaluated_get(depsgraph).data
        attr = evaluated.attributes.get(DISTANCE_ATTRIBUTE)
        if attr is None or len(attr.data) != len(tile.data.polygons):
            raise InspectRunError("Geometry Proximity gave no distances")
        values = _get(attr.data, "value", len(attr.data), np.float32).astype(np.float64)
    finally:
        tile.modifiers.remove(modifier)
    values[~(np.abs(values) <= limit)] = np.nan  # beyond the source's edge, or no overlap
    mesh = tile.data
    _write_float(mesh, DISTANCE_ATTRIBUTE, "FACE", values)
    kept = values[np.isfinite(values)]
    span = max(float(np.percentile(np.abs(kept), 98)) if kept.size else limit, 1e-6)
    rgba = ramps.apply_ramp(values, -span, span, "diverging_rdbu", nodata_rgba=_NO_DATA)
    _write_colours(mesh, DISTANCE_COLOURS, rgba)
    show_attribute_colours(context)
    if not kept.size:
        return f"No face of {tile.name} is within {limit:g} m of the source"
    rms = float(np.sqrt(np.mean(kept**2)))
    worst = float(np.abs(kept).max())
    text = (
        f"{tile.name}: face centres to source RMS {rms:.3g} m, max {worst:.3g} m "
        f"(geometric error {float(info['geometric_error']):.3g} m)"
    )
    missing = values.size - kept.size
    if missing:
        text += f"; {missing:,} faces beyond {limit:g} m"
    tile["geotools_distance"] = text
    return text


def texel_view(context: bpy.types.Context, tile: bpy.types.Object) -> str:
    """Texels per m² per face of ``tile``, as a FACE layer and a heatmap."""
    info = tile_info(tile)
    if info is None:
        raise InspectRunError("Make an opened output tile active first")
    _ensure_object_mode(context)
    mesh = tile.data
    if not mesh.uv_layers:
        raise InspectRunError(f"{tile.name} has no UVs, so no texel density")
    from .export import _corner_verts

    positions = _get(mesh.vertices, "co", 3 * len(mesh.vertices), np.float32).reshape(-1, 3)
    uvs = _get(mesh.uv_layers[0].data, "uv", 2 * len(mesh.loops), np.float32).reshape(-1, 2)
    slots = np.asarray(info.get("slot_texels") or [0], dtype=np.float64)
    material = _get(mesh.polygons, "material_index", len(mesh.polygons), np.int32)
    texels = slots[np.clip(material, 0, slots.size - 1)]
    density = texel_density(positions, _corner_verts(mesh), uvs, texels)
    _write_float(mesh, DENSITY_ATTRIBUTE, "FACE", density)
    try:
        summary = summarise_density(density)
    except InspectError as exc:
        raise InspectRunError(str(exc)) from exc
    low, high = max(summary.low, 1e-9), max(summary.high, summary.low * 1.0001, 1e-9)
    rgba = ramps.apply_ramp(density, low, high, "viridis", log=True, nodata_rgba=_NO_DATA)
    _write_colours(mesh, DENSITY_COLOURS, rgba)
    show_attribute_colours(context)
    text = f"{tile.name}: {summary.as_text()}"
    tile["geotools_texel_density"] = text
    return text


# --- operators -------------------------------------------------------------------------------


def _fail(op: bpy.types.Operator, context: bpy.types.Context, message: str) -> set[str]:
    settings(context).result = message[:1024]
    op.report({"ERROR"}, message)
    redraw_ui(context)
    return {"CANCELLED"}


def _done(op: bpy.types.Operator, context: bpy.types.Context, message: str) -> set[str]:
    settings(context).result = message[:1024]
    op.report({"INFO"}, message)
    redraw_ui(context)
    return {"FINISHED"}


class GEOTOOLS_OT_inspect_list(bpy.types.Operator):
    """List the tiles of the archive: the last publish or the chosen .3tz"""

    bl_idname = "geotools.inspect_list"
    bl_label = "List Tiles"
    bl_options = {"REGISTER"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        archive = chosen_archive(context)
        if archive is None:
            what = "Publish a .3tz first" if settings(context).source == "LAST" else "Choose a .3tz"
            return _fail(self, context, what)
        try:
            notes = list_archive(context, archive)
        except InspectRunError as exc:
            return _fail(self, context, f"Cannot list {archive.name}: {exc}")
        count = len(settings(context).tiles)
        message = f"{archive.name}: {count} tiles"
        if notes:
            self.report({"WARNING"}, "; ".join(notes))
        return _done(self, context, message)


class GEOTOOLS_OT_inspect_open(bpy.types.Operator):
    """Open tiles of the listed archive as read-only objects placed over the source"""

    bl_idname = "geotools.inspect_open"
    bl_label = "Open"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not settings(context).tiles:
            cls.poll_message_set("List an archive's tiles first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = settings(context)
        chosen = tiles_to_open(props)
        if not chosen:
            return _fail(self, context, "No tile matches; pick a tile or another depth")
        if len(chosen) > props.limit:
            return _fail(
                self,
                context,
                f"That is {len(chosen)} tiles, over the limit of {props.limit}; "
                "raise the limit or open one depth",
            )
        try:
            _made, summary = open_tiles(context, [t.uri for t in chosen])
        except InspectRunError as exc:
            return _fail(self, context, f"Cannot open: {exc}")
        return _done(self, context, summary)


class GEOTOOLS_OT_inspect_remove(bpy.types.Operator):
    """Remove every opened output tile, with its materials and images"""

    bl_idname = "geotools.inspect_remove"
    bl_label = "Remove Opened Tiles"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not opened_tiles(context.scene):
            cls.poll_message_set("No output tile is open")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        _ensure_object_mode(context)
        count = remove_tiles(context.scene)
        return _done(self, context, f"Removed {count} tile{'s' * (count != 1)}")


class GEOTOOLS_OT_inspect_select_source(bpy.types.Operator):
    """Select the source faces of the active output tile's selected faces"""

    bl_idname = "geotools.inspect_select_source"
    bl_label = "Select Source Faces"
    bl_options = {"REGISTER", "UNDO"}

    edit: BoolProperty(name="Edit Mode", default=True, description="Enter Edit Mode after")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if tile_info(context.active_object) is None:
            cls.poll_message_set("Make an opened output tile active")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            summary = select_source(context, context.active_object, self.edit)
        except InspectRunError as exc:
            return _fail(self, context, str(exc))
        return _done(self, context, summary)


class GEOTOOLS_OT_inspect_select_output(bpy.types.Operator):
    """Select the faces of the opened tiles that come from the selected source faces"""

    bl_idname = "geotools.inspect_select_output"
    bl_label = "Select Output Faces"
    bl_options = {"REGISTER", "UNDO"}

    edit: BoolProperty(name="Edit Mode", default=True, description="Enter Edit Mode after")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not opened_tiles(context.scene):
            cls.poll_message_set("Open output tiles first")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            summary = select_output(context, self.edit)
        except InspectRunError as exc:
            return _fail(self, context, str(exc))
        return _done(self, context, summary)


class _ActiveTileOperator:
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if tile_info(context.active_object) is None:
            cls.poll_message_set("Make an opened output tile active")
            return False
        return True


class GEOTOOLS_OT_inspect_distance(_ActiveTileOperator, bpy.types.Operator):
    """Colour the active output tile by its distance to the source surface (Distance to Surface)"""

    bl_idname = "geotools.inspect_distance"
    bl_label = "Distance to Source"

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            text = distance_heatmap(context, context.active_object)
        except (InspectRunError, KeyError, RuntimeError) as exc:
            return _fail(self, context, f"Cannot measure: {exc}")
        return _done(self, context, text)


class GEOTOOLS_OT_inspect_texel_density(_ActiveTileOperator, bpy.types.Operator):
    """Colour the active output tile by texels per square metre on each face"""

    bl_idname = "geotools.inspect_texel_density"
    bl_label = "Texel Density"

    def execute(self, context: bpy.types.Context) -> set[str]:
        try:
            text = texel_view(context, context.active_object)
        except InspectRunError as exc:
            return _fail(self, context, f"Cannot measure: {exc}")
        return _done(self, context, text)


def depth_summary(props: GeoToolsInspectSettings) -> str:
    counts = depth_counts([t.depth for t in props.tiles])
    return ", ".join(f"d{d}: {c}" for d, c in counts.items())


def tile_label(item: GeoToolsOutputTile) -> str:
    error = f"{item.geometric_error:.3g} m"
    tris = f", {item.triangles:,} tris" if item.triangles >= 0 else ""
    return f"{Path(item.uri).stem}  {item.role}  {error}{tris}"


class GEOTOOLS_UL_output_tiles(bpy.types.UIList):
    """Output tiles, parent before children, indented by depth."""

    bl_idname = "GEOTOOLS_UL_output_tiles"

    def draw_item(
        self,
        context: bpy.types.Context,
        layout: bpy.types.UILayout,
        data: bpy.types.AnyType,
        item: bpy.types.PropertyGroup,
        icon: int,
        active_data: bpy.types.AnyType,
        active_property: str,
        index: int = 0,
        flt_flag: int = 0,
    ) -> None:
        row = layout.row(align=True)
        for _ in range(min(item.depth, 8)):
            row.separator(factor=1.2)
        leaf = item.role == output_tiles.ROLE_LEAF
        row.label(text=tile_label(item), icon="MESH_DATA" if leaf else "MOD_DECIM")


classes = (
    GeoToolsOutputTile,
    GeoToolsInspectSettings,
    GEOTOOLS_OT_inspect_list,
    GEOTOOLS_OT_inspect_open,
    GEOTOOLS_OT_inspect_remove,
    GEOTOOLS_OT_inspect_select_source,
    GEOTOOLS_OT_inspect_select_output,
    GEOTOOLS_OT_inspect_distance,
    GEOTOOLS_OT_inspect_texel_density,
    GEOTOOLS_UL_output_tiles,
)

_register, _unregister = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register()
    bpy.types.Scene.geotools_inspect = PointerProperty(type=GeoToolsInspectSettings)


def unregister() -> None:
    del bpy.types.Scene.geotools_inspect
    _frames.clear()
    _unregister()
