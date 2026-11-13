"""Source textures: which changed since the last publish, and saving them before one.

Painting or cleaning a texture changes an image datablock in memory. Publish
must never lose those edits or publish stale pixels, so before it writes the
source GLB, :func:`save_edited_images` saves every source image with unsaved
edits:

- an image packed into the ``.blend`` is packed again from its current pixels;
- an image with a file is saved to that file (Blender's Image > Save);
- an image with neither (generated, then painted) is saved as a PNG in
  ``//textures/`` next to a saved ``.blend``, or packed into an unsaved one.

Then :func:`image_records` hashes each source image's bytes (file or packed)
for build.json ``source.images`` (``core.texture_state``). The Publish panel
compares the scene with the active build profile's last record, so it can say
"N textures changed since last publish" (:func:`changed_textures`), and Run
Checks flags images with unsaved edits (``image-unsaved``, through
``summarise.image_summary``) and adds a note listing the changed textures.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import bpy

from ..core.issues import Issue, Severity
from ..core.texture_state import (
    REASON_TEXT,
    Change,
    HashCache,
    ImageState,
    Origin,
    compare,
    recorded_images,
    same_file,
    sha256_bytes,
    summary_text,
)
from . import blend_paths
from .nodes import output_node, upstream

log = logging.getLogger(__name__)

TEXTURE_DIR = "//textures"
"""Where an edited image with no file is saved, next to a saved ``.blend``."""

PREVIEW_NODE = "GeoTools balance preview"
"""Name of the shader nodes the colour balance preview inserts (``texture_cleanup``)."""

DRAW_TTL = 1.0
"""Seconds the panel reuses its last comparison, so drawing stays cheap."""

_hashes = HashCache()
_records: dict[str, tuple[int, list[ImageState] | None]] = {}  # build.json path -> (mtime, images)
_draw: dict[str, object] = {"key": None, "time": 0.0, "changes": None}


# --- source images -------------------------------------------------------------------------


def material_images(material: bpy.types.Material | None) -> list[bpy.types.Image]:
    """Images of the Image Texture nodes feeding ``material``'s output."""
    if material is None or material.node_tree is None:
        return []
    output = output_node(material.node_tree)
    if output is None:
        return []
    return [
        n.image
        for n in upstream(output)
        if n.bl_idname == "ShaderNodeTexImage" and n.image is not None
    ]


def source_images(scene: bpy.types.Scene) -> list[bpy.types.Image]:
    """Every image the source collection's mesh materials use, once, sorted by name."""
    collection = scene.geotools.source_collection
    if collection is None:
        return []
    found: dict[str, bpy.types.Image] = {}
    seen: set[str] = set()
    for obj in collection.all_objects:
        if obj.type != "MESH":
            continue
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.name_full in seen:
                continue
            seen.add(material.name_full)
            for image in material_images(material):
                if image.source in {"FILE", "GENERATED"}:
                    found.setdefault(image.name_full, image)
    return [found[name] for name in sorted(found)]


def has_unsaved_edits(image: bpy.types.Image) -> bool:
    """Whether ``image`` holds pixels that are saved nowhere (painted, cleaned, filled)."""
    return bool(image.is_dirty) and image.library is None


def _file_path(image: bpy.types.Image) -> str:
    if image.source != "FILE" or not image.filepath:
        return ""
    return blend_paths.image_path(image)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "image"


def _new_file(image: bpy.types.Image) -> str:
    """``//textures/<image>.png``, numbered so it never overwrites another file."""
    stem = f"{TEXTURE_DIR}/{_safe_name(image.name)}"
    rel, n = f"{stem}.png", 1
    while os.path.exists(blend_paths.abspath(rel)):
        rel = f"{stem}_{n}.png"
        n += 1
    return rel


def save_target(image: bpy.types.Image) -> str | None:
    """Where Publish saves ``image``'s unsaved edits, in words; None if it has none."""
    if not has_unsaved_edits(image):
        return None
    if image.packed_file is not None:
        return "the .blend (packed)"
    path = _file_path(image)
    if path and os.path.isdir(os.path.dirname(path)):
        return f"its file {os.path.basename(path)}"
    if bpy.data.is_saved:
        return _new_file(image)
    return "the .blend (packed)"


# --- saving ---------------------------------------------------------------------------------


class SaveError(RuntimeError):
    """An edited image could not be saved, so publishing would lose or skip its edits."""


@dataclass
class SaveReport:
    saved: list[str] = field(default_factory=list)  # "name -> where"
    blend_changed: bool = False  # the .blend now differs from the file on disk

    @property
    def count(self) -> int:
        return len(self.saved)


def save_edited_images(images: list[bpy.types.Image]) -> SaveReport:
    """Save or pack every image with unsaved edits; raise :class:`SaveError` on failure."""
    report = SaveReport()
    for image in images:
        if not has_unsaved_edits(image):
            continue
        name = image.name
        try:
            where, changes_blend = _save_one(image)
        except (RuntimeError, OSError) as ex:
            raise SaveError(f"Could not save the edits of image {name}: {ex}") from ex
        finally:
            forget(image)
        if has_unsaved_edits(image):
            raise SaveError(f"Image {name} still has unsaved edits after saving it")
        report.saved.append(f"{name} -> {where}")
        report.blend_changed |= changes_blend
    return report


def _save_one(image: bpy.types.Image) -> tuple[str, bool]:
    """Save one edited image; return where to, and whether the ``.blend`` data changed."""
    if image.packed_file is not None:
        image.pack()
        return "packed", True
    path = _file_path(image)
    if path and os.path.isdir(os.path.dirname(path)):
        image.save()
        return path, False
    if not bpy.data.is_saved:
        image.pack()
        return "packed", True
    rel = _new_file(image)
    Path(blend_paths.abspath(rel)).parent.mkdir(parents=True, exist_ok=True)
    image.filepath_raw = rel
    image.file_format = "PNG"
    image.save()
    return rel, True


# --- state and records ----------------------------------------------------------------------


def forget(image: bpy.types.Image) -> None:
    """Drop cached hashes of ``image`` (after it was saved, packed or edited)."""
    key = image.name_full
    _hashes.forget(lambda k: len(k) > 1 and k[0] == "blob" and k[1] == key)
    _draw["key"] = None


def image_state(image: bpy.types.Image, old: ImageState | None = None) -> ImageState:
    """The state build.json records for ``image``; hashes only when it must.

    With ``old`` (the recorded state), a file with the same path, size and
    modification time keeps the recorded hash without being read.
    """
    name = image.name_full
    width, height = (int(v) for v in image.size[:2])
    if has_unsaved_edits(image):
        return ImageState(name, Origin.UNSAVED, width=width, height=height)
    packed = image.packed_file
    if packed is not None:
        size = int(packed.size)
        sha = _hashes.blob((name, size, packed.as_pointer()), lambda: packed.data)
        return ImageState(name, Origin.PACKED, sha, bytes=size, width=width, height=height)
    path = _file_path(image)
    if path:
        try:
            stat = os.stat(path)
        except OSError:
            return ImageState(name, Origin.FILE, None, path=path, width=width, height=height)
        state = ImageState(
            name,
            Origin.FILE,
            None,
            path=path,
            bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            width=width,
            height=height,
        )
        if same_file(state, old):
            sha = old.sha256
        else:
            sha = _hashes.file(path, stat.st_size, stat.st_mtime_ns)
        return ImageState(
            name, Origin.FILE, sha, path, stat.st_size, stat.st_mtime_ns, width, height
        )
    if image.source == "GENERATED":
        colour = ",".join(f"{c:.6g}" for c in image.generated_color)
        params = (
            f"generated:{image.generated_type}:{image.generated_width}x"
            f"{image.generated_height}:{colour}:{image.use_generated_float}"
        )
        sha = sha256_bytes(params.encode())
        return ImageState(name, Origin.GENERATED, sha, width=width, height=height)
    return ImageState(name, Origin.FILE, None, width=width, height=height)


def image_records(scene: bpy.types.Scene) -> list[dict[str, object]]:
    """build.json ``source.images``: one record per source image, by name."""
    return [image_state(image).to_record() for image in source_images(scene)]


def read_record_images(record_path: str) -> list[ImageState] | None:
    """The images a build.json recorded, cached by its modification time."""
    if not record_path:
        return None
    try:
        mtime = os.stat(record_path).st_mtime_ns
    except OSError:
        return None
    cached = _records.get(record_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(record_path, encoding="utf-8") as f:
            images = recorded_images(json.load(f))
    except (OSError, ValueError):
        images = None
    _records[record_path] = (mtime, images)
    return images


def changes_since(scene: bpy.types.Scene, record_path: str) -> list[Change] | None:
    """Source textures changed since the build recorded at ``record_path``; None if unknown."""
    recorded = read_record_images(blend_paths.abspath(record_path) if record_path else "")
    if recorded is None:
        return None
    before = {r.name: r for r in recorded}
    current = [image_state(i, before.get(i.name_full)) for i in source_images(scene)]
    return compare(current, recorded)


def changed_textures(
    context: bpy.types.Context, profile: bpy.types.PropertyGroup | None
) -> list[Change] | None:
    """:func:`changes_since` the profile's last build, reused for ``DRAW_TTL`` seconds."""
    if profile is None or context.scene is None or not profile.last_record:
        return None
    key = (context.scene.name, profile.name, profile.last_record)
    now = time.monotonic()
    if _draw["key"] == key and now - float(_draw["time"]) < DRAW_TTL:
        return _draw["changes"]  # type: ignore[return-value]
    changes = changes_since(context.scene, profile.last_record)
    _draw.update(key=key, time=now, changes=changes)
    return changes


def invalidate() -> None:
    """Make the next panel draw compare again (after a publish, a save or a cleanup)."""
    _draw["key"] = None


# --- checks and UI -------------------------------------------------------------------------


def check_issues(context: bpy.types.Context) -> list[Issue]:
    """Run Checks: the colour-balance preview (an error) and the changed textures (a note)."""
    issues = preview_issues(context.scene)
    props = context.scene.geotools
    index = props.build_profile_index
    if 0 <= index < len(props.build_profiles):
        profile = props.build_profiles[index]
        invalidate()
        changes = changes_since(context.scene, profile.last_record) if profile.last_record else None
        if changes:
            listed = ", ".join(c.text for c in changes[:6])
            more = f" and {len(changes) - 6} more" if len(changes) > 6 else ""
            issues.append(
                Issue(
                    Severity.INFO,
                    "textures-changed",
                    f"{summary_text(changes)} with {profile.name}: {listed}{more}.",
                )
            )
    return issues


def preview_issues(scene: bpy.types.Scene) -> list[Issue]:
    """``texture-balance-preview`` errors: a preview node would publish a flat colour."""
    issues = []
    seen: set[str] = set()
    collection = scene.geotools.source_collection
    objects = collection.all_objects if collection is not None else []
    for obj in objects:
        for slot in getattr(obj, "material_slots", ()):
            material = slot.material
            if material is None or material.name_full in seen or material.node_tree is None:
                continue
            seen.add(material.name_full)
            if any(n.name.startswith(PREVIEW_NODE) for n in material.node_tree.nodes):
                issues.append(
                    Issue(
                        Severity.ERROR,
                        "texture-balance-preview",
                        f"Material {material.name} shows the colour balance preview; Apply "
                        "or Clear it in the Textures panel before publishing.",
                        material.name,
                    )
                )
    return issues


def draw_changes(
    layout: bpy.types.UILayout,
    context: bpy.types.Context,
    profile: bpy.types.PropertyGroup | None,
) -> None:
    """The Publish panel line: "N textures changed since last publish"."""
    changes = changed_textures(context, profile)
    if not changes:
        return
    col = layout.column(align=True)
    col.label(text=summary_text(changes), icon="IMAGE_DATA")
    for change in changes[:3]:
        col.label(text=f"    {change.name}: {REASON_TEXT[change.reason]}")
    if len(changes) > 3:
        col.label(text=f"    and {len(changes) - 3} more")
