"""Linked sources: ``GeoToolsLink`` on objects, stale detection, refresh and auto-refresh.

An object made from a geo file keeps a link to it (``obj.geotools_link``):
the file, the layer, the fid field, the CRS, the import options and a
fingerprint of the file at the last sync. See docs/design/m8-geo-formats.md
("Linked sources").

- :func:`check_link` compares the file with the fingerprint: stat first, and
  hash only if the stat changed. It runs from the ``load_post`` handler, from
  ``geotools.link_check`` and from the auto-refresh timer.
- :func:`refresh_link` re-reads the file with the stored options through the
  refresher registered for the link's kind (:data:`REFRESHERS`). Refreshers
  replace only the object's mesh data (:func:`replace_mesh`), so modifiers,
  materials, transform, parent and collections survive.
- One ``bpy.app.timers`` timer, :func:`auto_refresh_tick`, polls the files of
  links with Auto-Refresh on and refreshes those that changed, unless the
  object is in Edit Mode.

Vectors (``vectors``) and GeoTIFF (``raster_io``) register their refreshers
with :func:`register_refresher`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import bpy
from bpy.app.handlers import persistent
from bpy.props import BoolProperty, EnumProperty, IntProperty, PointerProperty, StringProperty

from ..core import fingerprint
from ..core.fingerprint import FileState, Fingerprint
from .common import redraw_ui

log = logging.getLogger(__name__)

LINK_KIND_ITEMS = (
    ("NONE", "None", "Not linked to a file"),
    ("GEOJSON", "GeoJSON", "Linked to a GeoJSON file"),
    ("GPKG", "GeoPackage", "Linked to a GeoPackage feature table"),
    ("GEOTIFF", "GeoTIFF", "Linked to a GeoTIFF"),
)

LINK_STATUS_ITEMS = (
    ("OK", "Up to Date", "The object matches the file as last read"),
    ("CHANGED", "Changed on Disk", "The file changed since the last sync; refresh to update"),
    ("MISSING", "Missing", "The file is not there"),
    ("ERROR", "Error", "The last refresh or check failed"),
)

KIND_ICONS = {"GEOJSON": "FILE_TEXT", "GPKG": "FILE_CACHE", "GEOTIFF": "IMAGE_DATA"}
STATUS_ICONS = {
    "OK": "CHECKMARK",
    "CHANGED": "FILE_REFRESH",
    "MISSING": "LIBRARY_DATA_BROKEN",
    "ERROR": "CANCEL",
}

AUTO_REFRESH_SECONDS = 2.0


class GeoToolsLink(bpy.types.PropertyGroup):
    """The file an object was made from (``obj.geotools_link``)."""

    kind: EnumProperty(name="Kind", items=LINK_KIND_ITEMS, default="NONE")
    filepath: StringProperty(name="File", subtype="FILE_PATH")
    layer: StringProperty(
        name="Layer",
        description="GeoPackage table; for GeoJSON the file's stem (a file holds one layer)",
    )
    fid_field: StringProperty(
        name="Feature Id",
        description='Column that identifies features: the GeoPackage primary key, GeoJSON "id", '
        'or "@index" for the position in the file',
    )
    crs: StringProperty(name="CRS", description="The data's CRS (crs_match.to_text JSON)")
    options: StringProperty(
        name="Options", description="Import options as JSON: filters, geometry kind, columns"
    )
    fingerprint: StringProperty(
        name="Fingerprint", description="mtime_ns, size and SHA-256 at the last sync, as JSON"
    )
    last_sync: StringProperty(name="Last Sync", description="UTC time of the last sync (ISO)")
    auto_refresh: BoolProperty(
        name="Auto-Refresh",
        description="Refresh when the file changes on disk (checked every few seconds), "
        "except in Edit Mode",
        default=False,
    )
    status: EnumProperty(name="Status", items=LINK_STATUS_ITEMS, default="OK")
    message: StringProperty(name="Message", description="Detail of the last status")


# --- helpers ------------------------------------------------------------------------


def is_linked(obj: bpy.types.Object | None) -> bool:
    link = getattr(obj, "geotools_link", None)
    return link is not None and link.kind != "NONE" and bool(link.filepath)


def linked_objects() -> list[bpy.types.Object]:
    return [obj for obj in bpy.data.objects if is_linked(obj)]


def link_path(link: GeoToolsLink) -> Path:
    return Path(bpy.path.abspath(link.filepath))


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def mark_synced(link: GeoToolsLink, print_: Fingerprint | None = None) -> None:
    """Record that the object matches the file now."""
    if print_ is None:
        print_ = fingerprint.take(link_path(link))
    link.fingerprint = print_.to_text()
    link.last_sync = utc_now()
    link.status = "OK"
    link.message = ""


def set_status(link: GeoToolsLink, status: str, message: str = "") -> None:
    if link.status != status:
        link.status = status
    if link.message != message:
        link.message = message


def check_link(obj: bpy.types.Object) -> str:
    """Compare the linked file with the stored fingerprint; set and return the status.

    An ERROR status stays until a refresh succeeds, unless the file went missing.
    """
    link = obj.geotools_link
    state, current = fingerprint.check(link_path(link), Fingerprint.from_text(link.fingerprint))
    if state is FileState.MISSING:
        set_status(link, "MISSING", f"{link_path(link)} is missing")
    elif state is FileState.CHANGED:
        if link.status != "ERROR":
            set_status(link, "CHANGED", "The file changed on disk")
    elif state is FileState.TOUCHED:
        # Same bytes, new mtime: remember the new stat so it is not hashed again.
        assert current is not None
        link.fingerprint = current.to_text()
        if link.status in {"CHANGED", "MISSING"}:
            set_status(link, "OK")
    elif link.status in {"CHANGED", "MISSING"}:
        set_status(link, "OK")
    return link.status


def links_state(objects: list[bpy.types.Object]) -> str | None:
    """Worst status over ``objects``: ERROR, CHANGED, OK, or None for no links."""
    statuses = {obj.geotools_link.status for obj in objects}
    if not statuses:
        return None
    if statuses & {"ERROR", "MISSING"}:
        return "ERROR"
    return "CHANGED" if "CHANGED" in statuses else "OK"


# --- refresh --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What a refresh changed.

    Vector refreshers count features by fid (``by_feature``); a raster refresher
    has no features and sets ``by_feature=False``, so :meth:`summary` gives
    only its ``message``. ``warnings`` are reported beside the summary.
    """

    added: int = 0
    removed: int = 0
    changed: int = 0
    message: str = ""
    warnings: tuple[str, ...] = ()
    by_feature: bool = True

    def summary(self, name: str) -> str:
        if not self.by_feature:
            return f"{name}: {self.message}" if self.message else f"{name}: refreshed"
        text = f"{name}: {self.added} added, {self.removed} removed, {self.changed} changed"
        return f"{text}. {self.message}" if self.message else text


class RefreshError(RuntimeError):
    """A refresh was refused or failed; the object is unchanged."""


Refresher = Callable[[bpy.types.Object], RefreshResult]
"""Re-reads ``obj``'s linked file with its stored options and updates ``obj`` in place.

How it updates the object is up to the kind: vectors and DEMs build a new mesh
and swap it in with :func:`replace_mesh`; an orthophoto reloads its image and
moves its plane's four vertices in place. Either way modifiers, materials,
transform, parent and collections must survive. :func:`refresh_link` checks
that the file exists, catches failures (raise :class:`RefreshError` with a
message to refuse) and updates the fingerprint, last sync and status, so a
refresher does none of that.
"""

REFRESHERS: dict[str, Refresher] = {}


def register_refresher(kind: str, refresher: Refresher) -> None:
    """Make :func:`refresh_link` use ``refresher`` for links of ``kind``."""
    REFRESHERS[kind] = refresher


def unregister_refresher(kind: str) -> None:
    REFRESHERS.pop(kind, None)


def refresh_link(obj: bpy.types.Object) -> RefreshResult:
    """Re-read ``obj``'s file and replace its mesh data. Raises :class:`RefreshError`.

    On success the fingerprint and last sync are updated and the status is OK;
    on failure the object keeps its data and the status is ERROR (or MISSING).
    """
    link = obj.geotools_link
    if obj.mode == "EDIT":
        raise RefreshError(f"{obj.name} is in Edit Mode; leave it to refresh")
    refresher = REFRESHERS.get(link.kind)
    if refresher is None:
        raise RefreshError(f"No refresh for {link.kind} links in this version")
    path = link_path(link)
    try:
        before = fingerprint.take(path)
    except OSError:
        set_status(link, "MISSING", f"{path} is missing")
        raise RefreshError(f"{path.name} is missing") from None
    try:
        result = refresher(obj)
    except RefreshError as ex:
        set_status(link, "ERROR", str(ex))
        raise
    except Exception as ex:  # a broken file must not leave a half-made object
        log.exception("refresh of %s failed", obj.name)
        set_status(link, "ERROR", f"{type(ex).__name__}: {ex}")
        raise RefreshError(f"Refresh of {obj.name} failed: {ex}") from ex
    mark_synced(link, before)
    return result


def replace_mesh(obj: bpy.types.Object, mesh: bpy.types.Mesh) -> None:
    """Give ``obj`` the new ``mesh`` in place of its current one.

    The old mesh's materials are copied over (slots linked to the data would
    otherwise be lost); modifiers, transform, parent and collections belong to
    the object and stay. The old mesh is removed if nothing else uses it, and
    the new one takes its name.
    """
    old = obj.data
    if isinstance(old, bpy.types.Mesh):
        for material in old.materials:
            mesh.materials.append(material)
    name = old.name if old is not None else obj.name
    obj.data = mesh
    if old is not None and old.users == 0:
        bpy.data.meshes.remove(old)
    mesh.name = name


# --- load_post and the auto-refresh timer -------------------------------------------

_failed: dict[str, str] = {}
"""Object name -> fingerprint text of a file state whose auto-refresh failed."""


@persistent
def _on_load_post(*_args: object) -> None:
    _failed.clear()
    for obj in linked_objects():
        try:
            check_link(obj)
        except Exception:  # never break file loading
            log.exception("checking the link of %s failed", obj.name)


def auto_refresh_tick() -> float:
    """One poll of the auto-refresh timer; returns the seconds to the next one.

    Links with Auto-Refresh on whose file stat changed are checked (hashing
    only then) and refreshed if the content changed. An object in Edit Mode is
    skipped until it leaves, and a file state that failed once is not retried
    until the file changes again.
    """
    refreshed = False
    for obj in linked_objects():
        link = obj.geotools_link
        if not link.auto_refresh or obj.mode == "EDIT":
            continue
        stored = Fingerprint.from_text(link.fingerprint)
        try:
            quick = fingerprint.stat(link_path(link))
        except OSError:
            set_status(link, "MISSING", f"{link_path(link)} is missing")
            continue
        if stored is not None and quick.same_stat(stored):
            continue
        if _failed.get(obj.name) == quick.to_text():
            continue
        status = check_link(obj)
        if status not in {"CHANGED", "ERROR"}:
            continue
        try:
            result = refresh_link(obj)
        except RefreshError as ex:
            _failed[obj.name] = quick.to_text()
            log.warning("auto-refresh of %s failed: %s", obj.name, ex)
            continue
        _failed.pop(obj.name, None)
        refreshed = True
        log.info("auto-refresh %s", result.summary(obj.name))
    if refreshed:
        try:
            bpy.ops.ed.undo_push(message="GeoTools Auto-Refresh")
        except RuntimeError:  # no window context (e.g. background mode)
            pass
        context = bpy.context
        if getattr(context, "window_manager", None) is not None:
            redraw_ui(context)
    return AUTO_REFRESH_SECONDS


def _timer() -> float:
    try:
        return auto_refresh_tick()
    except Exception:  # a failing timer is unregistered by Blender; keep polling
        log.exception("auto-refresh failed")
        return AUTO_REFRESH_SECONDS


# --- the Linked Data list ----------------------------------------------------------


class GEOTOOLS_UL_links(bpy.types.UIList):
    """Objects linked to a geo file: kind, name and status."""

    bl_idname = "GEOTOOLS_UL_links"

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        link = item.geotools_link
        row = layout.row(align=True)
        row.label(text=item.name, icon=KIND_ICONS.get(link.kind, "FILE"))
        if link.auto_refresh:
            row.label(text="", icon="TIME")
        row.label(text="", icon=STATUS_ICONS[link.status])

    def filter_items(self, context, data, propname):
        objects = getattr(data, propname)
        flags = [self.bitflag_filter_item if is_linked(obj) else 0 for obj in objects]
        if self.filter_name:
            needle = self.filter_name.lower()
            flags = [f if needle in obj.name.lower() else 0 for f, obj in zip(flags, objects)]
        return flags, []


# --- operators ----------------------------------------------------------------------


def target_object(context: bpy.types.Context, name: str) -> bpy.types.Object | None:
    obj = bpy.data.objects.get(name) if name else active_link(context)
    return obj if is_linked(obj) else None


def active_link(context: bpy.types.Context) -> bpy.types.Object | None:
    """The link selected in the Linked Data list, if any."""
    scene = context.scene
    if scene is None:
        return None
    index = scene.geotools_link_index
    if 0 <= index < len(bpy.data.objects):
        obj = bpy.data.objects[index]
        if is_linked(obj):
            return obj
    return None


class GEOTOOLS_OT_link_refresh(bpy.types.Operator):
    """Re-read the linked file and replace the object's mesh data; modifiers, materials and transform stay"""

    bl_idname = "geotools.link_refresh"
    bl_label = "Refresh"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Object", description="Linked object; empty for the selected link")

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = target_object(context, self.name)
        if obj is None:
            self.report({"ERROR"}, "Select a linked object first")
            return {"CANCELLED"}
        try:
            result = refresh_link(obj)
        except RefreshError as ex:
            self.report({"ERROR"}, str(ex))
            redraw_ui(context)
            return {"CANCELLED"}
        for warning in result.warnings:
            self.report({"WARNING"}, warning)
        self.report({"INFO"}, result.summary(obj.name))
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_link_check(bpy.types.Operator):
    """Check every linked file for changes on disk (cheap: hashes only files whose date or size changed)"""

    bl_idname = "geotools.link_check"
    bl_label = "Check Linked Files"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return context.scene is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        objects = linked_objects()
        counts: dict[str, int] = {}
        for obj in objects:
            status = check_link(obj)
            counts[status] = counts.get(status, 0) + 1
        if not objects:
            self.report({"INFO"}, "No linked objects")
        else:
            text = ", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items()))
            level = {"INFO"} if set(counts) == {"OK"} else {"WARNING"}
            self.report(level, f"{len(objects)} linked: {text}")
        redraw_ui(context)
        return {"FINISHED"}


class GEOTOOLS_OT_link_unlink(bpy.types.Operator):
    """Forget the link to the file; the object and its mesh stay as they are"""

    bl_idname = "geotools.link_unlink"
    bl_label = "Unlink"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Object", description="Linked object; empty for the selected link")

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = target_object(context, self.name)
        if obj is None:
            self.report({"ERROR"}, "Select a linked object first")
            return {"CANCELLED"}
        obj.property_unset("geotools_link")
        self.report({"INFO"}, f"{obj.name} is no longer linked")
        redraw_ui(context)
        return {"FINISHED"}


classes = (
    GeoToolsLink,
    GEOTOOLS_UL_links,
    GEOTOOLS_OT_link_refresh,
    GEOTOOLS_OT_link_check,
    GEOTOOLS_OT_link_unlink,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Object.geotools_link = PointerProperty(type=GeoToolsLink)
    bpy.types.Scene.geotools_link_index = IntProperty(name="Active Link", default=-1)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if not bpy.app.timers.is_registered(_timer):
        bpy.app.timers.register(_timer, first_interval=AUTO_REFRESH_SECONDS, persistent=True)


def unregister() -> None:
    if bpy.app.timers.is_registered(_timer):
        bpy.app.timers.unregister(_timer)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
    _failed.clear()
    for attr, owner in (
        ("geotools_link_index", bpy.types.Scene),
        ("geotools_link", bpy.types.Object),
    ):
        if hasattr(owner, attr):
            delattr(owner, attr)
    _unregister_classes()
