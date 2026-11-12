"""Build a ``core.preflight.SceneSummary`` from the source collection.

Cheap by design: counts come from ``len()`` of mesh data, bounds from the eight
``bound_box`` corners of each object times ``matrix_world`` in numpy. Nothing
here loops over vertices, faces or loops in Python.

Blend mode in Blender 5.2
-------------------------
EEVEE (5.x) has no per-material alpha mode any more. ``Material`` has
``surface_render_method`` (``DITHERED`` or ``BLENDED``), and the old
``blend_method`` (``OPAQUE``/``CLIP``/``HASHED``/``BLEND``) is a compatibility
property that EEVEE ignores (new materials report ``HASHED``). Alpha clipping is
now built in the node tree. Blender 5.2's own glTF exporter therefore reads the
alpha mode from the nodes feeding the Principled BSDF Alpha input, and its
importer only sets ``surface_render_method`` (``BLENDED`` for glTF BLEND,
``DITHERED`` for OPAQUE and MASK). We follow the exporter's rule:

* ``OPAQUE``: Alpha is unlinked and its value is 1 (whatever the render method).
* ``CLIP``: Alpha is driven through an alpha-clip pattern, the ones the glTF
  add-on writes and recognises: ``Math(ROUND)``, ``1 - Math(LESS_THAN, a, cutoff)``
  or the legacy ``Math(GREATER_THAN, a, cutoff)``.
* ``BLEND``: any other alpha (a linked texture alpha, or a constant below 1).
  With ``DITHERED`` this renders as hashed transparency, with ``BLENDED`` as
  sorted blending; both are partial transparency, so both are ``BLEND``.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

import bpy
import numpy as np

from ..core.images import encoded_bit_depth
from ..core.placement import Vec3
from ..core.preflight import ImageSummary, MaterialSummary, ObjectSummary, SceneSummary
from .nodes import alpha_clip_nodes, blend_mode, from_node, output_node, upstream
from .properties import image_remedy
from .texture_state import save_target

# Shader nodes the GLB writer can express when they sit upstream of the
# material output: core PBR through one Principled BSDF, image textures on a UV
# map, normal maps, and the glTF importer's own helper nodes.
SUPPORTED_NODES: frozenset[str] = frozenset(
    {
        "ShaderNodeOutputMaterial",
        "ShaderNodeBsdfPrincipled",
        "ShaderNodeTexImage",
        "ShaderNodeNormalMap",
        "ShaderNodeUVMap",
        "ShaderNodeTexCoord",
        "ShaderNodeMapping",  # KHR_texture_transform
        "ShaderNodeSeparateColor",  # metallic/roughness from one texture
        "ShaderNodeVertexColor",  # Color Attribute node, COLOR_0
        "NodeReroute",
    }
)

# Object types whose bound_box describes geometry.
_GEOMETRY_TYPES = frozenset(
    {"MESH", "CURVE", "SURFACE", "META", "FONT", "CURVES", "POINTCLOUD", "VOLUME", "GREASEPENCIL"}
)


# --- bounds ------------------------------------------------------------------------


_BOUNDS_CHUNK = 1 << 20  # vertices transformed per numpy step, bounds memory use


def world_bounds(obj: bpy.types.Object) -> tuple[Vec3, Vec3]:
    """World-space AABB as float64.

    Meshes use their vertices, so the box is exact even for rotated objects:
    rusty-tiles places its origin at the centre of the exact source bounds, and
    a loose box would move the verified origin. Other types use their
    ``bound_box`` corners.
    """
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    rotation, translation = matrix[:3, :3].T, matrix[:3, 3]
    if obj.type == "MESH" and len(obj.data.vertices) > 0:
        count = len(obj.data.vertices)
        local = np.empty(count * 3, dtype=np.float32)
        obj.data.vertices.foreach_get("co", local)
        local = local.reshape(count, 3)
        lo = np.full(3, np.inf)
        hi = np.full(3, -np.inf)
        for start in range(0, count, _BOUNDS_CHUNK):
            world = local[start : start + _BOUNDS_CHUNK].astype(np.float64) @ rotation + translation
            lo = np.minimum(lo, world.min(axis=0))
            hi = np.maximum(hi, world.max(axis=0))
    else:
        corners = np.array(obj.bound_box, dtype=np.float64).reshape(8, 3)
        world = corners @ rotation + translation
        lo, hi = world.min(axis=0), world.max(axis=0)
    return (float(lo[0]), float(lo[1]), float(lo[2])), (float(hi[0]), float(hi[1]), float(hi[2]))


# --- materials -----------------------------------------------------------------------


def image_summary(image: bpy.types.Image) -> ImageSummary:
    packed = image.packed_file is not None
    filepath = ""
    if image.source in {"FILE", "SEQUENCE", "TILED"} and image.filepath:
        filepath = os.path.normpath(bpy.path.abspath(image.filepath, library=image.library))
    on_disk = bool(filepath) and os.path.isfile(filepath)
    # Generated and painted images are exportable too: the exporter saves their
    # current pixels as PNG.
    in_memory = image.source == "GENERATED" or (image.is_dirty and image.has_data)
    exists = packed or on_disk or in_memory
    width, height = (int(v) for v in image.size[:2])
    file_bytes = bit_depth = None
    used_as_is = (
        image.file_format in {"PNG", "JPEG"} and not image.is_dirty and image.source == "FILE"
    )
    if used_as_is and packed:
        file_bytes = image.packed_file.size
        # Reading the header would copy the whole packed file; Blender decodes
        # 16-bit PNGs (and only those, of PNG and JPEG) into a float buffer.
        bit_depth = 16 if image.is_float else 8
    elif used_as_is and on_disk:
        file_bytes = os.path.getsize(filepath)
        bit_depth = encoded_bit_depth(filepath)
    return ImageSummary(
        name=image.name,
        filepath=filepath,
        file_format=image.file_format,
        packed=packed,
        exists=exists,
        width=width,
        height=height,
        file_bytes=file_bytes,
        bit_depth=bit_depth,
        remedy=image_remedy(image),
        unsaved_target=save_target(image),
    )


def summarise_material(material: bpy.types.Material) -> MaterialSummary:
    tree = material.node_tree
    if tree is None:
        return MaterialSummary(name=material.name, uses_principled=False)
    output = output_node(tree)
    if output is None:
        return MaterialSummary(name=material.name, uses_principled=False)
    surface = from_node(output.inputs["Surface"])
    principled = surface if surface and surface.bl_idname == "ShaderNodeBsdfPrincipled" else None

    nodes = upstream(output)
    allowed = {n.name for n in alpha_clip_nodes(principled)} if principled else set()
    unsupported = sorted(
        {n.bl_idname for n in nodes if n.bl_idname not in SUPPORTED_NODES and n.name not in allowed}
    )
    images = {
        n.image.name: n.image
        for n in nodes
        if n.bl_idname == "ShaderNodeTexImage" and n.image is not None
    }
    return MaterialSummary(
        name=material.name,
        uses_principled=principled is not None,
        unsupported_nodes=tuple(unsupported),
        blend_mode=blend_mode(principled),
        images=tuple(image_summary(images[name]) for name in sorted(images)),
    )


# --- objects ------------------------------------------------------------------------


def _is_animated(*ids: bpy.types.ID | None) -> bool:
    for id_ in ids:
        anim = getattr(id_, "animation_data", None) if id_ is not None else None
        if anim is None:
            continue
        if anim.action is not None or len(anim.drivers) or len(anim.nla_tracks):
            return True
    return False


def summarise_object(
    obj: bpy.types.Object,
    evaluated: bpy.types.Object,
    materials: dict[str, MaterialSummary],
) -> ObjectSummary:
    """Summarise one object.

    Counts, layers and shape keys come from the original data (what the writer
    exports). Bounds come from the evaluated object, whose ``bound_box`` is
    always current.
    """
    bounds_min = bounds_max = (0.0, 0.0, 0.0)
    if obj.type in _GEOMETRY_TYPES:
        bounds_min, bounds_max = world_bounds(evaluated)

    faces = vertices = uv_layers = color_attributes = 0
    has_shape_keys = False
    data = obj.data
    if obj.type == "MESH":
        faces = len(data.polygons)
        vertices = len(data.vertices)
        uv_layers = len(data.uv_layers)
        color_attributes = len(data.color_attributes)
        has_shape_keys = data.shape_keys is not None and len(data.shape_keys.key_blocks) > 0

    has_armature = any(m.type == "ARMATURE" for m in obj.modifiers) or (
        obj.parent is not None and obj.parent_type == "ARMATURE"
    )
    shape_keys = getattr(data, "shape_keys", None)
    is_animated = _is_animated(obj, data, shape_keys)

    summaries = []
    for slot in obj.material_slots:
        material = slot.material
        if material is None:
            continue
        if material.name not in materials:
            materials[material.name] = summarise_material(material)
        summaries.append(materials[material.name])

    return ObjectSummary(
        name=obj.name,
        type=obj.type,
        faces=faces,
        vertices=vertices,
        bounds_min=bounds_min,
        bounds_max=bounds_max,
        uv_layers=uv_layers,
        color_attributes=color_attributes,
        has_shape_keys=has_shape_keys,
        has_armature=has_armature,
        is_animated=is_animated,
        materials=tuple(summaries),
    )


def summarise_objects(
    objects: Iterable[bpy.types.Object],
    scene: bpy.types.Scene,
    depsgraph: bpy.types.Depsgraph,
) -> SceneSummary:
    materials: dict[str, MaterialSummary] = {}
    summaries = tuple(
        summarise_object(obj, obj.evaluated_get(depsgraph), materials)
        for obj in sorted(objects, key=lambda o: o.name)
    )
    return SceneSummary(unit_scale=float(scene.unit_settings.scale_length), objects=summaries)


def summarise_collection(
    collection: bpy.types.Collection, context: bpy.types.Context
) -> SceneSummary:
    """Summary of every object in ``collection`` and its child collections."""
    depsgraph = context.evaluated_depsgraph_get()
    return summarise_objects(collection.all_objects, context.scene, depsgraph)
