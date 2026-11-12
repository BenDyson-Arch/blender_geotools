"""Check a source scene against rusty-tiles' mesh input rules. CONTRACT (M1).

The Blender side builds a SceneSummary (cheaply, with numpy); this module only
applies rules, so it is testable without Blender.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from .images import NO_REMEDY, ImageRemedy, RemedyKind, is_lossless_profile
from .issues import Issue, Severity
from .placement import Placement, PlacementMode, Vec3

SOURCE_COORDINATE_LIMIT = 1_000_000.0
"""rusty-tiles refuses a source coordinate beyond this, in source units."""

PRECISION_LIMIT_METRES = 1e-3
"""Float32 spacing above this at the largest coordinate on an axis earns a warning."""

METRES_PER_DEGREE = 111_320.0
"""Rough size of one degree, to judge precision in a geographic CRS."""

MAX_UV_LAYERS = 2  # TEXCOORD_0 and TEXCOORD_1
MAX_COLOR_ATTRIBUTES = 1  # COLOR_0
IMAGE_FORMATS = frozenset({"PNG", "JPEG"})

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

# 2D geographic CRSs a user is likely to name. Only EPSG:4326 runs in the portable
# rusty-tiles build; the others need its native build, but are still degrees.
_GEOGRAPHIC_EPSG = frozenset(
    {4019, 4167, 4171, 4230, 4258, 4269, 4277, 4283, 4326, 4490, 4612, 4617, 4674, 6668, 7844}
)
_EPSG = re.compile(r"epsg:(\d+)", re.IGNORECASE)
_PROJ_GEOGRAPHIC = re.compile(r"\+proj=(?:longlat|latlong|lonlat|latlon)\b", re.IGNORECASE)
_WKT_GEOGRAPHIC = re.compile(r"(?:GEOGCS|GEOGCRS|GEOGRAPHICCRS)\s*\[", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ImageSummary:
    name: str
    filepath: str  # absolute, "" if none
    file_format: str  # Blender image.file_format, e.g. "PNG", "JPEG", "OPEN_EXR"
    packed: bool
    exists: bool  # pixels are available: file on disk, packed, generated or painted
    width: int = 0
    height: int = 0
    file_bytes: int | None = None  # size of the PNG/JPEG file used as is, if any
    # Bits per decoded channel of the PNG/JPEG file used as is, if known; None
    # when the exporter writes the pixels itself (always 8-bit).
    bit_depth: int | None = None
    remedy: ImageRemedy = NO_REMEDY  # the user's stored choice, never inferred
    # Unsaved edits (painted, cleaned or generated pixels): where Publish saves
    # them first, e.g. "its file albedo.png" or "the .blend (packed)"; None if
    # the image has no unsaved edits. See blender.texture_state.
    unsaved_target: str | None = None


@dataclass(frozen=True, slots=True)
class MaterialSummary:
    name: str
    uses_principled: bool  # output driven by one Principled BSDF
    unsupported_nodes: tuple[str, ...] = ()  # node bl_idnames we cannot express
    blend_mode: str = "OPAQUE"  # "OPAQUE", "CLIP" (alpha mask) or "BLEND"
    images: tuple[ImageSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class ObjectSummary:
    name: str
    type: str  # bpy object type, e.g. "MESH", "CURVE"
    faces: int = 0
    vertices: int = 0
    bounds_min: Vec3 = (0.0, 0.0, 0.0)  # world space, Blender Z-up
    bounds_max: Vec3 = (0.0, 0.0, 0.0)
    uv_layers: int = 0
    color_attributes: int = 0
    has_shape_keys: bool = False
    has_armature: bool = False
    is_animated: bool = False
    materials: tuple[MaterialSummary, ...] = ()


@dataclass(frozen=True, slots=True)
class SceneSummary:
    unit_scale: float = 1.0  # scene.unit_settings.scale_length
    objects: tuple[ObjectSummary, ...] = field(default_factory=tuple)

    def bounds(self) -> tuple[Vec3, Vec3] | None:
        """World bounds of all mesh objects, or None if there are none."""
        meshes = [o for o in self.objects if o.type == "MESH"]
        if not meshes:
            return None
        lo = np.min(np.array([o.bounds_min for o in meshes], dtype=np.float64), axis=0)
        hi = np.max(np.array([o.bounds_max for o in meshes], dtype=np.float64), axis=0)
        return (_vec(lo), _vec(hi))


def check(scene: SceneSummary, placement: Placement, *, profile: str | None = None) -> list[Issue]:
    """Every problem found, errors first. Includes ``placement.validate()``.

    ``profile`` is the rusty-tiles build profile ("fast", "balanced", "quality"
    or "lossless") the scene is about to be published with, if known. It only
    adds warnings, e.g. for a lossy image remedy in a lossless build.
    """
    issues = list(placement.validate())
    issues += _scene_issues(scene, placement)
    meshes = [o for o in scene.objects if o.type == "MESH"]
    for obj in scene.objects:
        if obj.type != "MESH":
            issues.append(
                _warn(
                    "non-mesh-object",
                    f"{obj.name} is not a mesh ({obj.type}), so it is ignored.",
                    obj.name,
                )
            )
    for obj in meshes:
        issues += _object_issues(obj)
    issues += _material_issues(meshes, profile)
    # sorted() is stable, so issues keep their discovery order within a severity.
    return sorted(issues, key=lambda i: _SEVERITY_ORDER[i.severity])


def is_geographic_crs(definition: str) -> bool:
    """Whether a CRS definition is geographic (degrees), as far as can be told cheaply."""
    text = (definition or "").strip()
    epsg = _EPSG.fullmatch(text)
    if epsg:
        return int(epsg.group(1)) in _GEOGRAPHIC_EPSG
    return bool(_PROJ_GEOGRAPHIC.search(text) or _WKT_GEOGRAPHIC.match(text))


def _scene_issues(scene: SceneSummary, placement: Placement) -> list[Issue]:
    issues = []
    geographic = PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS and is_geographic_crs(
        placement.source_crs
    )
    if geographic:
        issues.append(
            _warn(
                "crs-geographic",
                f"{placement.source_crs.strip()} is geographic, so Blender units are read as "
                "degrees of longitude and latitude, not metres.",
            )
        )
    scale = scene.unit_scale
    if not (math.isfinite(scale) and scale == 1.0):
        issues.append(
            _error(
                "unit-scale",
                f"The scene unit scale is {scale:g}; set it to 1 so one Blender unit is "
                "one source unit.",
            )
        )
    bounds = scene.bounds()
    if bounds is None:
        issues.append(_error("no-mesh-objects", "There are no mesh objects to publish."))
        return issues
    corners = np.array(bounds, dtype=np.float64)
    if not np.all(np.isfinite(corners)):
        issues.append(_error("coordinate-range", "Some mesh coordinates are not finite."))
        return issues
    largest = float(np.max(np.abs(corners)))
    if largest > SOURCE_COORDINATE_LIMIT:
        issues.append(
            _error(
                "coordinate-range",
                f"Mesh coordinates reach {largest:,.0f}, beyond the "
                f"±{SOURCE_COORDINATE_LIMIT:,.0f} rusty-tiles accepts; move the geometry "
                "towards the origin and record the shift as an offset.",
            )
        )
        return issues
    # Float32 spacing at the largest magnitude on each axis, in metres. In a
    # geographic CRS, X and Y are degrees but Z is still metres.
    per_axis = np.max(np.abs(corners), axis=0)
    units = np.array([METRES_PER_DEGREE, METRES_PER_DEGREE, 1.0]) if geographic else np.ones(3)
    spacing_metres = np.spacing(per_axis.astype(np.float32)).astype(np.float64) * units
    worst = int(np.argmax(spacing_metres))
    if spacing_metres[worst] > PRECISION_LIMIT_METRES:
        where = {
            PlacementMode.LOCAL: "a source CRS offset",
            PlacementMode.ANCHOR: "the scene offset",
            PlacementMode.SOURCE_CRS: "the source offset",
        }[PlacementMode(placement.mode)]
        issues.append(
            _warn(
                "float-precision",
                f"Mesh coordinates reach {float(per_axis[worst]):,.6g}, where single "
                f"precision is only good to {_length(float(spacing_metres[worst]))}; move "
                f"the geometry towards the origin and put the shift in {where}.",
            )
        )
    return issues


def _object_issues(obj: ObjectSummary) -> list[Issue]:
    issues = []
    name = obj.name
    if obj.has_shape_keys:
        issues.append(
            _error(
                "shape-keys",
                f"{name} has shape keys, which rusty-tiles refuses; apply or remove them.",
                name,
            )
        )
    if obj.has_armature:
        issues.append(
            _error(
                "armature",
                f"{name} is deformed by an armature, which rusty-tiles refuses; apply or "
                "remove it.",
                name,
            )
        )
    if obj.is_animated:
        issues.append(
            _warn(
                "animation",
                f"{name} is animated; only the current frame is published.",
                name,
            )
        )
    if obj.uv_layers > MAX_UV_LAYERS:
        issues.append(
            _warn(
                "uv-layers",
                f"{name} has {obj.uv_layers} UV maps; only the first {MAX_UV_LAYERS} are "
                "published.",
                name,
            )
        )
    if obj.color_attributes > MAX_COLOR_ATTRIBUTES:
        issues.append(
            _warn(
                "color-attributes",
                f"{name} has {obj.color_attributes} colour attributes; only the first is "
                "published.",
                name,
            )
        )
    return issues


def _material_issues(meshes: list[ObjectSummary], profile: str | None = None) -> list[Issue]:
    """Material and image rules, each material and image reported once."""
    issues = []
    seen_materials: set[str] = set()
    seen_images: set[str] = set()
    for obj in meshes:
        for material in obj.materials:
            if material.name in seen_materials:
                continue
            seen_materials.add(material.name)
            issues += _one_material(material)
            for image in material.images:
                if image.name not in seen_images:
                    seen_images.add(image.name)
                    issues += _one_image(image, profile)
    return issues


def _one_material(material: MaterialSummary) -> list[Issue]:
    issues = []
    name = material.name
    if material.blend_mode.upper() == "BLEND":
        issues.append(
            _error(
                "alpha-blend",
                f"Material {name} uses alpha blending, which rusty-tiles refuses; use alpha "
                "clip or opaque instead.",
                name,
            )
        )
    if not material.uses_principled:
        issues.append(
            _warn(
                "material-not-principled",
                f"Material {name} is not driven by a Principled BSDF, so it is published as "
                "a flat colour.",
                name,
            )
        )
    elif material.unsupported_nodes:
        nodes = ", ".join(sorted(set(material.unsupported_nodes)))
        issues.append(
            _warn(
                "material-unsupported-nodes",
                f"Material {name} uses nodes that cannot be published ({nodes}), so it falls "
                "back to a flat colour.",
                name,
            )
        )
    return issues


def _one_image(image: ImageSummary, profile: str | None = None) -> list[Issue]:
    issues = _image_rules(image, profile)
    if image.unsaved_target and (image.exists or image.packed):
        issues.append(
            _warn(
                "image-unsaved",
                f"Image {image.name} has unsaved edits; Publish saves them to "
                f"{image.unsaved_target} first.",
                image.name,
            )
        )
    return issues


def _image_rules(image: ImageSummary, profile: str | None = None) -> list[Issue]:
    name = image.name
    issues = _image_limits(image)
    if not image.exists and not image.packed:
        where = f" at {image.filepath}" if image.filepath else ""
        issues.append(
            _error(
                "image-missing",
                f"Image {name} is missing{where} and not packed.",
                name,
            )
        )
        return issues
    remedy = image.remedy
    if remedy.active:
        if not any(i.code in _REMEDIED_CODES and i.severity is Severity.WARNING for i in issues):
            issues.append(
                _warn(
                    "image-remedy",
                    f"Image {name} is published from a copy "
                    f"{remedy.describe(image.width, image.height)}.",
                    name,
                )
            )
        if remedy.lossy and is_lossless_profile(profile):
            issues.append(
                _warn(
                    "image-remedy-lossy",
                    f"Image {name} is {_lossy_phrase(remedy)}, so the lossless build "
                    "profile does not publish it losslessly.",
                    name,
                )
            )
        return issues
    if issues:
        return issues
    if image.file_format.upper() not in IMAGE_FORMATS:
        return [
            _warn(
                "image-format",
                f"Image {name} is {image.file_format}, not PNG or JPEG, so it is converted to PNG.",
                name,
            )
        ]
    return []


# Image limit codes a remedy can turn from an error into a warning.
_REMEDIED_CODES = frozenset(
    ("image-too-large", "image-16-bit", "image-file-too-large", "image-png-size")
)


def _lossy_phrase(remedy: ImageRemedy) -> str:
    if remedy.kind is RemedyKind.JPEG:
        return f"re-encoded as lossy JPEG quality {remedy.quality}"
    return f"downscaled, and written as lossy JPEG quality {remedy.quality} if still too big"


def _error(code: str, message: str, subject: str | None = None) -> Issue:
    return Issue(Severity.ERROR, code, message, subject)


def _warn(code: str, message: str, subject: str | None = None) -> Issue:
    return Issue(Severity.WARNING, code, message, subject)


def _length(metres: float) -> str:
    return f"{metres:.3g} m" if metres >= 1.0 else f"{metres * 1000:.3g} mm"


def _vec(a: np.ndarray) -> Vec3:
    return (float(a[0]), float(a[1]), float(a[2]))


def _image_limits(image: ImageSummary) -> list[Issue]:
    """rusty-tiles refuses images over 16384 px a side, 32 MiB encoded or 16-bit.

    Without a remedy these are errors whose message names the remedy to choose.
    A remedy that removes the cause turns its error into a warning that names
    the remedy; one that does not leaves the error.
    """
    from .gltf_writer import MAX_IMAGE_BYTES, MAX_IMAGE_EDGE

    name = image.name
    remedy = image.remedy
    kind = remedy.kind
    does = remedy.describe(image.width, image.height)
    limit_mib = MAX_IMAGE_BYTES / (1 << 20)
    issues = []

    if max(image.width, image.height) > MAX_IMAGE_EDGE:
        size = f"Image {name} is {image.width}x{image.height}"
        if kind is RemedyKind.DOWNSCALE and remedy.max_edge <= MAX_IMAGE_EDGE:
            issues.append(
                _warn(
                    "image-too-large",
                    f"{size}, over the {MAX_IMAGE_EDGE} pixels a side rusty-tiles accepts, "
                    f"so publishing uses a copy {does}.",
                    name,
                )
            )
        else:
            issues.append(
                _error(
                    "image-too-large",
                    f"{size}; rusty-tiles accepts at most {MAX_IMAGE_EDGE} pixels a side, "
                    "so choose Downscale for it in the Checks panel.",
                    name,
                )
            )

    sixteen_bit = image.bit_depth is not None and image.bit_depth != 8
    if sixteen_bit:
        depth = f"Image {name} is {image.bit_depth}-bit per channel"
        if remedy.active:
            issues.append(
                _warn(
                    "image-16-bit",
                    f"{depth}, which rusty-tiles refuses, so publishing uses a copy {does}"
                    f"{'' if kind is RemedyKind.CONVERT_8BIT else ' at 8 bits'}.",
                    name,
                )
            )
        else:
            issues.append(
                _error(
                    "image-16-bit",
                    f"{depth}; rusty-tiles accepts 8-bit images only, so choose Convert to "
                    "8-bit for it in the Checks panel.",
                    name,
                )
            )

    if image.file_bytes is not None and image.file_bytes > MAX_IMAGE_BYTES:
        mib = image.file_bytes / (1 << 20)
        size = f"Image {name} is {mib:.1f} MiB"
        # 16 to 8 bits halves the samples; the export checks the real size.
        halved = kind is RemedyKind.CONVERT_8BIT and sixteen_bit
        if kind in (RemedyKind.JPEG, RemedyKind.DOWNSCALE):
            issues.append(
                _warn(
                    "image-file-too-large",
                    f"{size}, over the {limit_mib:g} MiB per encoded image rusty-tiles "
                    f"accepts, so publishing uses a copy {does}.",
                    name,
                )
            )
        elif halved and image.file_bytes * 8 / image.bit_depth <= MAX_IMAGE_BYTES:
            issues.append(
                _warn(
                    "image-file-too-large",
                    f"{size}, over the {limit_mib:g} MiB rusty-tiles accepts, and publishing "
                    "uses a copy converted to an 8-bit PNG, expected to fit and checked then.",
                    name,
                )
            )
        else:
            issues.append(
                _error(
                    "image-file-too-large",
                    f"{size}; rusty-tiles accepts at most {limit_mib:g} MiB per encoded "
                    "image, so choose Re-encode as JPEG or Downscale for it in the Checks "
                    "panel.",
                    name,
                )
            )
    elif image.file_bytes is None and image.width * image.height * 4 > MAX_IMAGE_BYTES:
        size = f"{image.width}x{image.height}"
        if kind in (RemedyKind.JPEG, RemedyKind.DOWNSCALE):
            issues.append(
                _warn(
                    "image-png-size",
                    f"Image {name} is written by GeoTools at {size}, where a PNG can exceed "
                    f"rusty-tiles' {limit_mib:g} MiB limit, so publishing uses a copy {does}.",
                    name,
                )
            )
        else:
            issues.append(
                _warn(
                    "image-png-size",
                    f"Image {name} is converted to PNG at {size}, which can exceed "
                    f"rusty-tiles' {limit_mib:g} MiB limit per image; choose Re-encode as "
                    "JPEG or Downscale for it in the Checks panel to be sure.",
                    name,
                )
            )
    return issues
