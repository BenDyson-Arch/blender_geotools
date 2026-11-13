"""The metadata GLB (M4): object ids, layer values, georeference, and File > Export.

A standard glTF 2.0 GLB of the source collection with object identity and
layers as ``EXT_mesh_features`` / ``EXT_structural_metadata`` and the
georeference as ``KHR_xmp_json_ld`` (docs/design/m4-glb-metadata.md). It is
separate from the plain ``source.glb`` rusty-tiles tiles.

* :func:`prepare_glb` does the main-thread work: refreshes the layers, reads
  FACE and CORNER values from the meshes the export publishes
  (``layers.layer_meshes``), extracts the scene with ``export.Metadata``, packs
  TEXTURE layers (each object's float image) into PNG property textures,
  measures the placement with the probe (``core.georef``) and builds the
  extension. Writing (``gltf_writer.write_glb(..., extension=...)``) can then
  run anywhere: the one-off export writes straight away, a ``.glb`` build
  profile hands it to the publish job's worker thread.
* :func:`object_ids` gives every exported object a persistent UUID
  (``Object.geotools_id``), made on first export and kept across renames and
  saves. A duplicated object (which copies the property) gets a new id; the
  first in name order keeps it. Linked objects, which cannot be written, get a
  UUID derived from their library and name.
* ``geotools.export_glb`` is File > Export > GeoTools GLB with metadata.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ExportHelper

from ..core import crs_catalog, deps, georef
from ..core import gltf_metadata as gm
from ..core.gltf_writer import ExportError, GlbWriteResult, SceneExport, write_glb
from ..core.issues import Issue, Severity
from ..core.layers import DataType, Domain, LayerSpec, fill_value, validate
from ..core.metadata_schema import SchemaError, Storage, property_type
from ..core.placement import PlacementMode, blender_to_gltf
from . import blend_paths, export
from .common import extension_version, redraw_ui
from .layers import (
    attribute_problem,
    find_layer,
    layer_image,
    layer_meshes,
    other_layer_names,
    read_layer,
    refresh_layers,
    source_objects,
    spec_from_props,
)
from .properties import (
    GLB_EARTH_PLACED_DESCRIPTION,
    GLB_FACE_TABLE_DESCRIPTION,
    FieldError,
    placement_from_props,
)

log = logging.getLogger(__name__)

OBJECT_ID_PROP = "geotools_id"
EARTH_PLACED_DESCRIPTION = GLB_EARTH_PLACED_DESCRIPTION
FACE_TABLE_DESCRIPTION = GLB_FACE_TABLE_DESCRIPTION
_ID_NAMESPACE = uuid.UUID("6f1d3c52-6a1f-4d7e-9a43-0a1c9b8b6e21")


# --- options and object ids --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GlbOptions:
    face_table: bool = False
    earth_placed: bool = False
    apply_modifiers: bool = True
    variant: str = ""  # a layer to present (COLOR_0 or textures), as the variant tilesets do

    def record(self) -> dict[str, object]:
        return {
            "face_table": self.face_table,
            "earth_placed": self.earth_placed,
            "apply_modifiers": self.apply_modifiers,
            "variant": self.variant or None,
        }


def _editable(obj: bpy.types.Object) -> bool:
    return obj.library is None and obj.override_library is None


def object_ids(objects: list[bpy.types.Object], *, assign: bool = True) -> tuple[list[str], int]:
    """Each object's persistent id, making missing or duplicated ones; and how many
    were made. ``assign`` False reads without writing (a made id is not stored)."""
    ids: list[str] = []
    seen: set[str] = set()
    made = 0
    for obj in objects:
        value = getattr(obj, OBJECT_ID_PROP, "")
        if not _editable(obj):
            library = obj.library.filepath if obj.library is not None else ""
            value = str(uuid.uuid5(_ID_NAMESPACE, f"{library}\0{obj.name}"))
        elif not value or value in seen:
            value = str(uuid.uuid4())
            made += 1
            if assign:
                setattr(obj, OBJECT_ID_PROP, value)
        seen.add(value)
        ids.append(value)
    return ids, made


# --- checks ------------------------------------------------------------------------------


def _layer_specs(scene: bpy.types.Scene) -> tuple[list[LayerSpec], list[str]]:
    """Valid layers (registry order) and the names of invalid ones."""
    specs, invalid = [], []
    for item in scene.geotools.layers:
        spec = spec_from_props(item)
        errors = [
            i
            for i in validate(spec, other_layer_names(scene, item))
            if i.severity is Severity.ERROR
        ]
        (invalid.append(item.name) if errors else specs.append(spec))
    return specs, invalid


def glb_issues(scene: bpy.types.Scene, options: GlbOptions) -> list[Issue]:
    """Problems that stop or degrade a metadata GLB, beyond Run Checks'."""
    issues: list[Issue] = []
    objects = source_objects(scene)
    for obj in objects:
        if obj.data.is_editmode:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "glb-edit-mode",
                    f"{obj.name} is in Edit Mode; switch to Object Mode to export its layers.",
                    obj.name,
                )
            )
    specs, invalid = _layer_specs(scene)
    for name in invalid:
        issues.append(
            Issue(
                Severity.WARNING,
                "glb-layer-invalid",
                f"Layer {name} is invalid and is left out of the GLB; fix it in the Layers panel.",
                name,
            )
        )
    for spec in specs:
        domain = Domain(spec.domain)
        storage = (
            Storage.TEXTURE
            if domain is Domain.TEXTURE
            else Storage.TABLE
            if domain is Domain.FACE and options.face_table
            else Storage.ATTRIBUTE
        )
        try:
            property_type(spec, storage)
        except SchemaError as ex:
            issues.append(Issue(Severity.ERROR, "glb-layer-type", str(ex), spec.name))
        if domain is Domain.TEXTURE:
            item = find_layer(scene, spec.name)
            missing = [o.name for o in objects if layer_image(item, o) is None]
            if missing:
                issues.append(
                    Issue(
                        Severity.WARNING,
                        "glb-image-missing",
                        f"Texture layer {spec.name} has no image for {', '.join(missing[:3])}"
                        f"{' ...' if len(missing) > 3 else ''}; those objects get no values.",
                        spec.name,
                    )
                )
    if options.face_table and not any(Domain(s.domain) is Domain.FACE for s in specs):
        issues.append(
            Issue(
                Severity.INFO,
                "glb-face-table-empty",
                "The face table has no face layers; it holds only each face's object and index.",
            )
        )
    if options.earth_placed:
        try:
            mode = placement_from_props(scene.geotools).mode
        except FieldError:
            mode = None  # Run Checks reports the field
        if mode is PlacementMode.LOCAL:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "glb-earth-placed-local",
                    "Earth-placed needs a placement on Earth (Anchor or CRS); the project "
                    "is local.",
                )
            )
        if not deps.probe_rusty_tiles().available:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "glb-earth-placed-rusty-tiles",
                    "Earth-placed needs rusty-tiles to work out the placement (see About).",
                )
            )
    return issues


# --- preparing ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class PreparedGlb:
    """A metadata GLB ready to write (``write_glb(scene, path, extension=extension)``)."""

    scene: SceneExport
    extension: gm.MetadataExtension
    topology: dict[str, str]
    remedies: list[dict]
    record: dict[str, object] = field(default_factory=dict)
    """For build.json: options, extensions used, layers, objects, face table cost."""
    warnings: list[str] = field(default_factory=list)
    ids_made: int = 0
    variant: dict[str, object] | None = None
    """The presented layer's record (``core.presentation.variant_record``), if any."""


def _finite_range(values: list[np.ndarray], nodata: float | None) -> tuple[float, float] | None:
    lo, hi = math.inf, -math.inf
    for array in values:
        data = np.asarray(array, dtype=np.float64).ravel()
        mask = np.isfinite(data)
        if nodata is not None:
            mask &= data != float(np.float32(nodata))
        if mask.any():
            lo = min(lo, float(data[mask].min()))
            hi = max(hi, float(data[mask].max()))
    return None if lo > hi else (lo, hi)


def _with_range(spec: LayerSpec, values: list[np.ndarray]) -> LayerSpec:
    """``spec`` with ``value_min``/``value_max`` of exactly the values written."""
    if DataType(spec.data_type) is DataType.CATEGORY:
        return spec
    found = _finite_range(values, spec.nodata)
    if found is None:
        return replace(spec, value_min=None, value_max=None)
    return replace(spec, value_min=found[0], value_max=found[1])


def _read_values(
    scene: bpy.types.Scene, specs: list[LayerSpec], objects, apply: bool, depsgraph
) -> tuple[dict[str, dict[str, np.ndarray]], dict[str, list[np.ndarray]]]:
    """FACE/CORNER values per layer and object, from the meshes the export publishes;
    and per layer every array written (for the range), fill values included."""
    values: dict[str, dict[str, np.ndarray]] = {s.name: {} for s in specs}
    written: dict[str, list[np.ndarray]] = {s.name: [] for s in specs}
    cache: dict[tuple[int, str], np.ndarray | None] = {}
    for obj, mesh in layer_meshes(scene, objects, apply=apply, depsgraph=depsgraph):
        for spec in specs:
            key = (mesh.as_pointer(), spec.name)
            if key not in cache:
                problem = attribute_problem(mesh, spec)
                if problem == "mismatch":
                    raise ExportError(
                        f"Layer {spec.name}: the attribute on {obj.name} has the wrong domain "
                        "or type (see the Layers panel)."
                    )
                cache[key] = read_layer(mesh, spec) if problem is None else None
            array = cache[key]
            if array is None:
                written[spec.name].append(np.array([fill_value(spec)], dtype=np.float64))
            else:
                values[spec.name][obj.name] = array
                written[spec.name].append(array)
    return values, written


def _image_values(image: bpy.types.Image) -> np.ndarray:
    """The first channel of a float layer image as ``(H, W)``, row 0 at the bottom."""
    width, height = (int(v) for v in image.size[:2])
    channels = image.channels
    if not width or not height or not channels:
        raise ExportError(f"Image {image.name} has no pixels (is its file missing?).")
    pixels = np.empty(width * height * channels, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    return pixels[::channels].reshape(height, width)


def _bounds(scene: SceneExport) -> tuple[tuple[float, ...], tuple[float, ...]]:
    lo = np.min([p.positions.min(axis=0) for p in scene.primitives], axis=0)
    hi = np.max([p.positions.max(axis=0) for p in scene.primitives], axis=0)
    return tuple(float(v) for v in lo), tuple(float(v) for v in hi)


def _georeference(
    scene: bpy.types.Scene, export_scene: SceneExport, options: GlbOptions
) -> tuple[gm.Georeference, tuple[float, ...] | None]:
    """The XMP georeference and, Earth-placed, the root node matrix."""
    placement = placement_from_props(scene.geotools)
    mode = PlacementMode(placement.mode)
    fields: dict[str, object] = {
        "mode": mode.value,
        "geotools_version": extension_version(),
        "earth_placed": options.earth_placed,
        "source": Path(bpy.data.filepath).name if bpy.data.filepath else "",
    }
    if mode is PlacementMode.SOURCE_CRS:
        fields |= {
            "crs_epsg": crs_catalog.epsg_code(placement.source_crs),
            "crs_definition": placement.source_crs,
            "source_offset": tuple(float(v) for v in placement.source_offset),
            "height_offset": float(placement.height_offset),
        }
    elif mode is PlacementMode.ANCHOR:
        fields |= {
            "anchor": tuple(float(v) for v in placement.anchor),
            "heading_degrees": float(placement.heading_degrees),
            "scene_offset": tuple(float(v) for v in placement.scene_offset),
        }
    matrix = None
    if mode is not PlacementMode.LOCAL:
        try:
            if not deps.probe_rusty_tiles().available:
                raise georef.GeorefError("rusty-tiles is not available")
            frame = georef.measure_frame(placement, *_bounds(export_scene))
        except georef.GeorefError as ex:
            if options.earth_placed:
                raise ExportError(f"Cannot place the GLB on Earth: {ex}") from None
            fields["probe_error"] = str(ex)
        else:
            node = georef.gltf_node_matrix(frame)
            fields |= {
                "origin_lon_lat_height": frame.origin_lon_lat_height,
                "origin_model": blender_to_gltf(frame.origin_local),
                "heading_degrees": frame.heading_degrees,
                "grid_scale": frame.scale if mode is PlacementMode.SOURCE_CRS else None,
                "crs_operation": frame.operation,
                "model_to_ecef": node,
            }
            matrix = node if options.earth_placed else None
    return gm.Georeference(**fields), matrix  # type: ignore[arg-type]


def prepare_glb(
    context: bpy.types.Context, image_dir: Path, options: GlbOptions, *, assign_ids: bool = True
) -> PreparedGlb:
    """Everything up to writing; see the module docstring. Raises ``ExportError``."""
    scene = context.scene
    props = scene.geotools
    collection = props.source_collection
    if collection is None:
        raise ExportError("Set or ingest a source collection first.")
    objects = source_objects(scene)
    if any(o.data.is_editmode for o in objects):
        raise ExportError("A source object is in Edit Mode; switch to Object Mode first.")
    depsgraph = context.evaluated_depsgraph_get()
    refresh_layers(scene, depsgraph=depsgraph)
    specs, invalid = _layer_specs(scene)
    warnings = [f"layer {name} is invalid and was left out" for name in invalid]
    vertex_specs = [
        s
        for s in specs
        if Domain(s.domain) is Domain.CORNER
        or (Domain(s.domain) is Domain.FACE and not options.face_table)
    ]
    face_specs = [s for s in specs if Domain(s.domain) is Domain.FACE and options.face_table]
    texture_specs = [s for s in specs if Domain(s.domain) is Domain.TEXTURE]

    read_specs = vertex_specs + face_specs
    values, written = _read_values(scene, read_specs, objects, options.apply_modifiers, depsgraph)
    vertex_specs = [_with_range(s, written[s.name]) for s in vertex_specs]
    face_specs = [_with_range(s, written[s.name]) for s in face_specs]

    # Texture layers: each object's float image, and the UV map it uses.
    images: dict[str, dict[str, np.ndarray]] = {}
    texture_uv: dict[str, list[str]] = {}
    for spec in texture_specs:
        item = find_layer(scene, spec.name)
        images[spec.name] = {}
        for obj in objects:
            image = layer_image(item, obj)
            if image is None:
                warnings.append(f"texture layer {spec.name} has no image for {obj.name}")
                continue
            images[spec.name][obj.name] = _image_values(image)
            uvs = texture_uv.setdefault(obj.name, [])
            if item.uv_map not in uvs:
                uvs.append(item.uv_map)
    texture_specs = [_with_range(s, list(images[s.name].values())) for s in texture_specs]

    presentation, variant_record = None, None
    if options.variant:
        from .presentation import build_presentation

        presentation, variant_record = build_presentation(
            scene, options.variant, depsgraph=depsgraph
        )

    metadata = export.Metadata(
        vertex_layers=tuple(vertex_specs),
        values=values,
        face_table=options.face_table,
        per_object=bool(texture_specs),
        texture_uv={k: tuple(v) for k, v in texture_uv.items()},
    )
    remedies: list[dict] = []
    export_scene, topology = export.extract_scene(
        context,
        collection,
        image_dir,
        remedies=remedies,
        presentation=presentation,
        apply_modifiers=options.apply_modifiers,
        metadata=metadata,
    )
    ids, made = object_ids(objects, assign=assign_ids)

    # Property textures, per primitive (each holds one object; see export.Metadata).
    textures: list[tuple[gm.PackedTexture, ...]] = []
    render_uv = _render_uvs(objects, depsgraph, options.apply_modifiers) if texture_specs else {}
    for prim in export_scene.primitives:
        packed = []
        if texture_specs:
            obj_name = export_scene.object_names[int(prim.source_object[0])]
            for spec in texture_specs:
                pixels = images[spec.name].get(obj_name)
                if pixels is None:
                    continue
                uv = find_layer(scene, spec.name).uv_map or render_uv.get(obj_name)
                rgba, channels = gm.pack_texture(pixels, spec)
                packed.append(
                    gm.PackedTexture(
                        spec.name,
                        f"{spec.name}.{obj_name}",
                        gm.encode_png(rgba),
                        tuple(channels),
                        prim.texcoord_names.index(uv),
                    )
                )
        textures.append(tuple(packed))

    georeference, matrix = _georeference(scene, export_scene, options)
    face_counts = metadata.stats["face_counts"] if options.face_table else None
    face_values = None
    if options.face_table:
        face_values = {
            s.name: [values[s.name].get(n) for n in export_scene.object_names] for s in face_specs
        }
    extension = gm.build_extension(
        export_scene,
        object_ids=ids,
        vertex_layers=vertex_specs,
        face_layers=face_specs,
        face_values=face_values,
        face_counts=face_counts,
        texture_layers=texture_specs,
        textures=textures,
        georeference=georeference,
        root_matrix=matrix,
        version=extension_version(),
    )
    record = {
        "options": options.record(),
        **extension.summary,
        "vertices": metadata.stats["vertices"],
        "placement": georeference.mode,
        "origin_lon_lat_height": (
            list(georeference.origin_lon_lat_height)
            if georeference.origin_lon_lat_height is not None
            else None
        ),
    }
    if options.face_table:
        plain = metadata.stats["vertices_without_face_table"]
        record["face_table_cost"] = _face_table_cost(
            export_scene, metadata.stats["vertices"], plain
        )
    return PreparedGlb(
        scene=export_scene,
        extension=extension,
        topology=topology,
        remedies=remedies,
        record=record,
        warnings=warnings,
        ids_made=made,
        variant=variant_record,
    )


def _render_uvs(objects, depsgraph, apply: bool) -> dict[str, str | None]:
    out = {}
    for obj in objects:
        mesh = obj.evaluated_get(depsgraph).data if apply else obj.data
        out[obj.name] = export.render_uv_name(mesh)
    return out


def _face_table_cost(scene: SceneExport, vertices: int, plain: int) -> dict[str, object]:
    """How much the face table's unshared vertices cost: counts and vertex bytes."""
    vertex_bytes = 0
    for prim in scene.primitives:
        per_vertex = 12 + (12 if prim.normals is not None else 0)
        per_vertex += 16 if prim.tangents is not None else 0
        per_vertex += 8 * len(prim.texcoords) + (16 if prim.color is not None else 0)
        per_vertex += 4 * len(prim.custom_attributes)
        vertex_bytes += per_vertex * prim.positions.shape[0]
    per = vertex_bytes / vertices if vertices else 0.0
    return {
        "vertices": vertices,
        "vertices_without_face_table": plain,
        "vertex_ratio": round(vertices / plain, 3) if plain else None,
        "extra_vertex_bytes": round((vertices - plain) * per),
    }


def write_prepared(prepared: PreparedGlb, path: Path) -> GlbWriteResult:
    return write_glb(prepared.scene, path, extension=prepared.extension)


# --- File > Export --------------------------------------------------------------------------


class GEOTOOLS_OT_export_glb(bpy.types.Operator, ExportHelper):
    """Export the source collection as a standard GLB with its objects, layers and georeference as glTF metadata (EXT_mesh_features, EXT_structural_metadata, KHR_xmp_json_ld)"""

    bl_idname = "geotools.export_glb"
    bl_label = "Export GeoTools GLB"
    bl_options = {"REGISTER", "UNDO"}  # undo keeps the object ids it makes

    filename_ext = ".glb"
    filter_glob: StringProperty(default="*.glb", options={"HIDDEN"})
    face_table: BoolProperty(
        name="Face Values as a Feature Table", description=FACE_TABLE_DESCRIPTION, default=False
    )
    earth_placed: BoolProperty(
        name="Earth-Placed", description=EARTH_PLACED_DESCRIPTION, default=False
    )
    apply_modifiers: BoolProperty(
        name="Apply Modifiers",
        description="Export each object's evaluated mesh, with its modifiers and Geometry "
        "Nodes applied, and read layer values from it",
        default=True,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if context.scene is None or context.scene.geotools.source_collection is None:
            cls.poll_message_set("Set or ingest a GeoTools source collection first")
            return False
        return True

    def invoke(self, context, event):
        if not self.filepath:
            stem = Path(bpy.data.filepath).stem if bpy.data.filepath else "site"
            self.filepath = f"{stem}.glb"
        return ExportHelper.invoke(self, context, event)

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(self, "apply_modifiers")
        layout.prop(self, "face_table")
        layout.prop(self, "earth_placed")

    def execute(self, context: bpy.types.Context) -> set[str]:
        from .checks import count_issues, describe_counts, run_checks, store_issues

        options = GlbOptions(
            face_table=self.face_table,
            earth_placed=self.earth_placed,
            apply_modifiers=self.apply_modifiers,
        )
        issues = run_checks(context) + glb_issues(context.scene, options)
        store_issues(context.scene.geotools, issues)
        counts = count_issues(issues)
        if counts[Severity.ERROR]:
            first = next(i for i in issues if i.severity is Severity.ERROR)
            self.report(
                {"ERROR"},
                f"Cannot export: checks found {describe_counts(counts)} ({first.message}). "
                "See the Checks panel",
            )
            redraw_ui(context)
            return {"CANCELLED"}
        path = Path(blend_paths.abspath(self.filepath))
        image_dir = Path(tempfile.mkdtemp(prefix="geotools-glb-"))
        try:
            prepared = prepare_glb(context, image_dir, options)
            result = write_prepared(prepared, path)
        except (ExportError, ValueError, OSError, RuntimeError) as ex:  # ValueError: FieldError,
            # SchemaError (a range that changed since the checks)
            self.report({"ERROR"}, f"Cannot export: {ex}")
            return {"CANCELLED"}
        finally:
            shutil.rmtree(image_dir, ignore_errors=True)
        notes = list(prepared.warnings)
        cost = prepared.record.get("face_table_cost")
        if cost and cost.get("vertex_ratio"):
            notes.append(f"face table: {cost['vertex_ratio']}x the vertices")
        note = f" ({'; '.join(notes)})" if notes else ""
        self.report(
            {"WARNING" if prepared.warnings else "INFO"},
            f"Exported {path.name}: {result.triangles:,} triangles, {result.bytes:,} bytes{note}",
        )
        redraw_ui(context)
        return {"FINISHED"}


def menu_export(self, context: bpy.types.Context) -> None:
    self.layout.operator(GEOTOOLS_OT_export_glb.bl_idname, text="GeoTools GLB with metadata (.glb)")


classes = (GEOTOOLS_OT_export_glb,)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Object.geotools_id = StringProperty(
        name="GeoTools Object Id",
        description="Persistent id of the object in GeoTools metadata GLBs, made on first "
        "export; it survives renames and saves",
        default="",
        options=set(),
    )
    bpy.types.TOPBAR_MT_file_export.append(menu_export)


def unregister() -> None:
    bpy.types.TOPBAR_MT_file_export.remove(menu_export)
    if hasattr(bpy.types.Object, OBJECT_ID_PROP):
        del bpy.types.Object.geotools_id
    _unregister_classes()
