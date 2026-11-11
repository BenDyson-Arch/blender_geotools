"""GeoTIFF import: a DEM becomes a grid mesh, an orthophoto an image on a plane.

Both are placed in the project frame and linked to their file
(``obj.geotools_link``, kind GEOTIFF), so :func:`refresh` can rebuild them in
place. The georeference is parsed by ``core.geotiff``; the arrays come from
``core.raster``; this module only reads pixels and copies arrays into Blender.

- **DEM pixels** go through Blender's image loader for the sample types it
  reads exactly (float32, uint8, uint16, int16; see ``core.geotiff``), and
  through ``core.geotiff.read_band`` for the rest (uncompressed or deflate).
  The temporary image is removed straight away.
- **Orthophotos** are loaded as a Blender image *linked* to the file (not
  packed), so the ``.blend`` stays small and Refresh is ``Image.reload``. Keep
  the file beside the ``.blend``, or pack it yourself (File > External Data).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from ..core import geotiff as core_geotiff
from ..core import raster as core_raster
from ..core.geotiff import GeoTiffError, GeoTiffInfo
from ..core.raster import RasterError
from .linked import link_options, mark_synced
from .properties import FieldError, placement_from_scene

MODE_ITEMS = (
    (
        "AUTO",
        "Automatic",
        "A DEM for one band of floats or 16/32-bit integers; otherwise an orthophoto",
    ),
    ("DEM", "DEM", "A height mesh from band 1, one vertex per pixel centre"),
    ("ORTHO", "Orthophoto", "The image on a plane over its footprint"),
)

UV_MAP_NAME = "UVMap"


@dataclass(frozen=True)
class RefreshReport:
    """What an import or refresh did, for the operator report."""

    message: str
    warnings: tuple[str, ...] = field(default=())
    seconds: float = 0.0

    def __str__(self) -> str:
        return self.message


# --- reading ----------------------------------------------------------------------------


def resolve_mode(info: GeoTiffInfo, mode: str) -> str:
    """``"DEM"`` or ``"ORTHO"`` for ``mode`` (``AUTO`` decides from the band layout)."""
    if mode in {"DEM", "ORTHO"}:
        return mode
    if info.photometric == core_geotiff.PHOTOMETRIC_PALETTE:
        return "ORTHO"
    one_band = info.samples_per_pixel == 1 or (info.samples_per_pixel == 2 and info.has_alpha)
    if one_band and (info.sample_format == 3 or info.bits_per_sample >= 16):
        return "DEM"
    return "ORTHO"


def read_dem_band(info: GeoTiffInfo) -> np.ndarray:
    """Band 1 as a (height, width) array of the file's sample type, top row first."""
    route = core_geotiff.dem_route(info)
    if route == "numpy":
        return core_geotiff.read_band(info, 0)
    try:
        image = bpy.data.images.load(info.path, check_existing=False)
    except RuntimeError as ex:
        raise GeoTiffError(f"Blender cannot read {info.name}: {ex}") from ex
    try:
        # Raw values: no colour management, no alpha premultiplication.
        image.colorspace_settings.is_data = True
        image.alpha_mode = "CHANNEL_PACKED"
        width, height = image.size
        channels = image.channels
        if (width, height) != (info.width, info.height) or channels < 1:
            raise GeoTiffError(
                f"Blender read {info.name} as {width} x {height}, "
                f"expected {info.width} x {info.height}"
            )
        pixels = np.empty(width * height * channels, np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    # Image rows run bottom to top.
    band = pixels.reshape(height, width, channels)[::-1, :, 0]
    return core_geotiff.denormalise(np.ascontiguousarray(band), info.sample_type)


def _placement_offset(
    scene: bpy.types.Scene, info: GeoTiffInfo, confirmed: bool
) -> core_raster.CrsCheck:
    try:
        placement = placement_from_scene(scene)
    except FieldError as ex:
        raise RasterError(f"Fix the project placement first: {ex}") from ex
    return core_raster.check_crs(placement, info.crs, info.name, confirmed=confirmed)


def _crs_text(info: GeoTiffInfo) -> str:
    if info.crs is None:
        return ""
    return info.crs.epsg or info.crs.definition or info.crs_citation or "unknown"


def _units(info: GeoTiffInfo) -> str:
    return "°" if info.geographic else " m"


# --- building -----------------------------------------------------------------------


def _set_mesh(
    mesh: bpy.types.Mesh, positions: np.ndarray, corners: np.ndarray, uvs: np.ndarray
) -> None:
    """Fill an empty mesh with quads, entirely with ``foreach_set``.

    ``position`` and ``.corner_vert`` are written as attributes: Blender copies
    a matching contiguous array in one go there (0.01 s for 16.7 million
    vertices), where ``vertices.foreach_set("co")`` and
    ``loops.foreach_set("vertex_index")`` take seconds.
    """
    loops = len(corners)
    mesh.vertices.add(len(positions))
    mesh.loops.add(loops)
    mesh.polygons.add(loops // 4)
    attributes = mesh.attributes
    flat = np.ascontiguousarray(positions, np.float32).reshape(-1)
    corners = np.ascontiguousarray(corners, np.int32)
    if "position" in attributes and attributes["position"].data_type == "FLOAT_VECTOR":
        attributes["position"].data.foreach_set("vector", flat)
    else:
        mesh.vertices.foreach_set("co", flat)
    if ".corner_vert" in attributes and attributes[".corner_vert"].data_type == "INT":
        attributes[".corner_vert"].data.foreach_set("value", corners)
    else:
        mesh.loops.foreach_set("vertex_index", corners)
    mesh.polygons.foreach_set("loop_start", np.arange(0, loops, 4, dtype=np.int32))
    mesh.update(calc_edges=True)
    uv = mesh.uv_layers.new(name=UV_MAP_NAME)
    uv.uv.foreach_set("vector", np.ascontiguousarray(uvs, np.float32).reshape(-1))


def build_grid_mesh(name: str, grid: core_raster.GridMesh) -> bpy.types.Mesh:
    """A new mesh of the DEM's quads."""
    mesh = bpy.data.meshes.new(name)
    _set_mesh(mesh, grid.positions, grid.corners, grid.uvs)
    return mesh


def build_plane_mesh(name: str, quad: core_raster.Footprint) -> bpy.types.Mesh:
    """A new one-quad mesh over an orthophoto's footprint."""
    mesh = bpy.data.meshes.new(name)
    _set_mesh(mesh, quad.positions, np.arange(4, dtype=np.int32), quad.uvs)
    return mesh


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def ortho_image(path: str) -> bpy.types.Image:
    """The Blender image linked to ``path``, loaded or reloaded from disk."""
    for image in bpy.data.images:
        if (
            image.source == "FILE"
            and image.filepath
            and _same_file(bpy.path.abspath(image.filepath, library=image.library), path)
        ):
            image.reload()
            return image
    try:
        return bpy.data.images.load(path, check_existing=False)
    except RuntimeError as ex:
        raise GeoTiffError(f"Blender cannot read {Path(path).name}: {ex}") from ex


def ortho_material(name: str, image: bpy.types.Image, alpha: bool) -> bpy.types.Material:
    """A material showing ``image`` on the active UV map, unlit-looking (rough, no specular)."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    texture = tree.nodes.new("ShaderNodeTexImage")
    texture.image = image
    texture.extension = "CLIP"
    texture.location = (bsdf.location.x - 320, bsdf.location.y)
    tree.links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 1.0
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
    if alpha:
        tree.links.new(texture.outputs["Alpha"], bsdf.inputs["Alpha"])
    return material


def swap_mesh(obj: bpy.types.Object, mesh: bpy.types.Mesh) -> None:
    """Give ``obj`` the new mesh data, keeping its name and data materials.

    Modifiers, object materials, transform, parent and collections belong to
    the object and are untouched. The old mesh is removed if nothing else uses it.
    """
    old = obj.data
    for material in old.materials:
        mesh.materials.append(material)
    name = old.name
    obj.data = mesh
    if old.users == 0:
        bpy.data.meshes.remove(old)
    mesh.name = name


# --- import and refresh ---------------------------------------------------------------


def _dem_mesh(
    info: GeoTiffInfo, offset: tuple[float, float, float], step: int, name: str
) -> tuple[bpy.types.Mesh, str]:
    started = time.perf_counter()
    band = read_dem_band(info)
    read = time.perf_counter()
    grid = core_raster.dem_grid(info, band, offset, step)
    del band
    built = time.perf_counter()
    mesh = build_grid_mesh(name, grid)
    done = time.perf_counter()
    rows, cols = grid.grid_shape
    sx, sy = grid.spacing
    units = _units(info)
    dropped = f", {grid.nodata_vertices:,} nodata samples removed" if grid.nodata_vertices else ""
    message = (
        f"{info.name}: DEM {cols:,} x {rows:,} samples at {sx:g}{units} x {sy:g}{units} "
        f"spacing, {grid.vertex_count:,} vertices, {grid.face_count:,} faces{dropped} "
        f"(read {read - started:.2f} s, grid {built - read:.2f} s, "
        f"mesh {done - built:.2f} s)"
    )
    return mesh, message


def _ortho_mesh(
    info: GeoTiffInfo, offset: tuple[float, float, float], name: str
) -> tuple[bpy.types.Mesh, bpy.types.Image, str]:
    image = ortho_image(info.path)
    quad = core_raster.footprint(info, offset)
    mesh = build_plane_mesh(name, quad)
    units = _units(info)
    width, height = quad.size
    message = (
        f"{info.name}: orthophoto {info.width:,} x {info.height:,} px on a "
        f"{width:g}{units} x {height:g}{units} plane"
    )
    return mesh, image, message


def import_geotiff(
    context: bpy.types.Context,
    path: str,
    mode: str = "AUTO",
    step: int = 1,
    confirmed: bool = False,
) -> tuple[bpy.types.Object, RefreshReport]:
    """Import ``path`` into the active collection and link it. Raises ValueError."""
    started = time.perf_counter()
    info = core_geotiff.read_info(path)
    mode = resolve_mode(info, mode)
    check = _placement_offset(context.scene, info, confirmed)
    name = Path(path).stem
    if mode == "DEM":
        mesh, message = _dem_mesh(info, check.offset, step, name)
        image = None
    else:
        mesh, image, message = _ortho_mesh(info, check.offset, name)
    obj = bpy.data.objects.new(name, mesh)
    if image is not None:
        mesh.materials.append(ortho_material(name, image, info.has_alpha))
    collection = context.collection or context.scene.collection
    collection.objects.link(obj)

    link = obj.geotools_link
    link.kind = "GEOTIFF"
    link.filepath = path
    link.crs = _crs_text(info)
    link.options = json.dumps(
        {"mode": mode, "step": step if mode == "DEM" else 1, "assume_project_crs": confirmed},
        sort_keys=True,
    )
    mark_synced(link, path)
    seconds = time.perf_counter() - started
    return obj, RefreshReport(f"{message}; {seconds:.2f} s", check.warnings, seconds)


def refresh(obj: bpy.types.Object, link: object) -> RefreshReport:
    """Re-read a linked GeoTIFF with its stored options and rebuild ``obj`` in place.

    The DEM mesh or the plane is replaced (:func:`swap_mesh`) and an
    orthophoto's image is reloaded, so modifiers, materials, transform, parent
    and collections survive. Raises ValueError (``RasterError`` or
    ``GeoTiffError``) with a message; the link's status says what happened.
    """
    started = time.perf_counter()
    path = bpy.path.abspath(link.filepath)
    if not os.path.isfile(path):
        link.status = "MISSING"
        raise RasterError(f"{obj.name}: the linked file is missing: {path}")
    if obj.type != "MESH":
        link.status = "ERROR"
        raise RasterError(f"{obj.name} is not a mesh")
    if obj.mode != "OBJECT":
        raise RasterError(f"{obj.name}: leave Edit Mode before refreshing")
    options = link_options(link)
    mode = str(options.get("mode", "AUTO"))
    step = max(1, int(options.get("step", 1)))
    confirmed = bool(options.get("assume_project_crs", False))
    scene = obj.users_scene[0] if obj.users_scene else bpy.context.scene
    try:
        info = core_geotiff.read_info(path)
        mode = resolve_mode(info, mode)
        check = _placement_offset(scene, info, confirmed)
        if mode == "DEM":
            mesh, message = _dem_mesh(info, check.offset, step, obj.data.name)
        else:
            mesh, image, message = _ortho_mesh(info, check.offset, obj.data.name)
            if not obj.material_slots or not any(s.material for s in obj.material_slots):
                mesh.materials.append(ortho_material(obj.name, image, info.has_alpha))
    except ValueError:
        link.status = "ERROR"
        raise
    swap_mesh(obj, mesh)
    link.crs = _crs_text(info)
    mark_synced(link, path)
    seconds = time.perf_counter() - started
    return RefreshReport(f"Refreshed {message}; {seconds:.2f} s", check.warnings, seconds)


# --- operator ------------------------------------------------------------------------------

_INFO_CACHE: dict[str, tuple[int, GeoTiffInfo | str]] = {}


def _cached_info(path: str) -> GeoTiffInfo | str | None:
    """The header of the file selected in the file browser, or the error, cached by mtime."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        return None
    hit = _INFO_CACHE.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        value: GeoTiffInfo | str = core_geotiff.read_info(path)
    except GeoTiffError as ex:
        value = str(ex)
    _INFO_CACHE.clear()
    _INFO_CACHE[path] = (mtime, value)
    return value


class GEOTOOLS_OT_import_geotiff(bpy.types.Operator, ImportHelper):
    """Import a GeoTIFF in the project frame: a DEM as a height mesh, an orthophoto on a \
plane. Linked to the file, so it can be refreshed"""

    bl_idname = "geotools.import_geotiff"
    bl_label = "Import GeoTIFF"
    bl_options = {"REGISTER", "UNDO"}

    filename_ext = ".tif"
    filter_glob: StringProperty(default="*.tif;*.tiff", options={"HIDDEN"})
    mode: EnumProperty(name="As", items=MODE_ITEMS, default="AUTO")
    step: IntProperty(
        name="Decimation",
        description="Use every nth pixel of a DEM; the spacing grows n times",
        default=1,
        min=1,
        soft_max=64,
    )
    assume_project_crs: BoolProperty(
        name="Data Is in the Project CRS",
        description="Use when GeoTools cannot tell the file's CRS. Rasters are never "
        "reprojected: a file in another CRS must be reprojected in QGIS first",
        default=False,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None:
            return False
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode first")
            return False
        return True

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "mode")
        info = _cached_info(bpy.path.abspath(self.filepath)) if self.filepath else None
        if isinstance(info, str):
            col = layout.column()
            col.alert = True
            col.label(text=info[:80], icon="ERROR")
            return
        mode = resolve_mode(info, self.mode) if info is not None else self.mode
        dem = layout.column()
        dem.active = mode != "ORTHO"
        dem.prop(self, "step")
        if info is not None and mode == "DEM":
            rows, cols = core_raster.decimated_shape(info, self.step)
            sx, sy = core_raster.spacing(info, self.step)
            units = _units(info)
            dem.label(text=f"Spacing {sx:g}{units} x {sy:g}{units}", icon="MESH_GRID")
            dem.label(text=f"{cols:,} x {rows:,} vertices")
        if info is not None:
            layout.label(text=f"{info.band_layout}, {info.compression_name}", icon="IMAGE_DATA")
            layout.label(text=f"CRS {info.crs.label() if info.crs else 'not stated'}")
        layout.prop(self, "assume_project_crs")

    def execute(self, context: bpy.types.Context) -> set[str]:
        if not self.filepath:
            self.report({"ERROR"}, "No file selected")
            return {"CANCELLED"}
        path = bpy.path.abspath(self.filepath)
        if not os.path.isfile(path):
            self.report({"ERROR"}, f"File not found: {path}")
            return {"CANCELLED"}
        try:
            obj, report = import_geotiff(
                context, path, self.mode, self.step, self.assume_project_crs
            )
        except ValueError as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        for selected in context.selected_objects:
            selected.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        for warning in report.warnings:
            self.report({"WARNING"}, warning)
        self.report({"INFO"}, report.message)
        return {"FINISHED"}


def menu_func_import(self: bpy.types.Menu, context: bpy.types.Context) -> None:
    self.layout.operator(GEOTOOLS_OT_import_geotiff.bl_idname, text="GeoTIFF (.tif, .tiff)")


classes = (GEOTOOLS_OT_import_geotiff,)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    _INFO_CACHE.clear()
    _unregister_classes()
