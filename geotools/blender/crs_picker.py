"""The Project panel's CRS: a searchable EPSG field, rusty-tiles' verdict, preset migration.

``scene.geotools.crs`` is a ``StringProperty`` with a search callback
(:func:`search_crs`): clicking it lists every EPSG code the bundled
rusty-tiles resolves as ``EPSG:7856 — GDA2020 / MGA zone 56`` (names and areas
of use from pyproj, ``core.crs_catalog``), and typing filters by code or name
words. When the project has a location (a verified placement, the current CRS
and offset, or an anchor), the codes whose area of use holds it come first,
and with "Near This Site" on they are the only ones listed. Choosing an entry
stores only ``EPSG:7856`` (the field's setter). Free text (WKT, PROJ) is kept
as typed. The field reads and writes the scene's GeoScene ``SRID`` key when it
can hold the CRS (``blender.scene_georef``).

Under the field the panel shows ``rusty_tiles.resolve_source_crs``'s verdict,
cached by definition: the datum operation and its EPSG-stated accuracy, or the
refusal as an error, before anything is baked. Without rusty-tiles (or with
one older than 0.4.1) the field still takes text, and the panel says why
nothing can be listed or checked.

Scenes saved with one of the old CRS presets' PROJ strings
(``core.crs_migration``) in GeoTools' own CRS text are moved to the preset's
EPSG code on file load (before that text moves into the GeoScene keys),
when the extension is enabled, and when Run Checks or Publish first reads the
placement (``properties.placement_from_props`` uses the migrated value even
before it is written). ``crs_catalog.migrated`` only does so when rusty-tiles
resolves the code to the very same definition, so the placement is unchanged.
"""

from __future__ import annotations

import logging
import math

import bpy
from bpy.app.handlers import persistent

from ..core import crs_catalog, deps
from ..core.crs_catalog import LonLat
from . import scene_georef
from .ui import info_wrapped, split_column, wrapped

log = logging.getLogger(__name__)


# --- the property callbacks (used by properties.GeoToolsProject) -------------------------


def _float(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def site_lon_lat(props: bpy.types.PropertyGroup) -> LonLat | None:
    """Roughly where the project is, for ordering the picker; None if it can't tell.

    In order: the last verified placement (rusty-tiles' own answer), the
    current CRS and offset (with pyproj), then a non-zero anchor.
    """
    if props.probe_status == "OK":
        lon, lat = _float(props.probe_lon), _float(props.probe_lat)
        if lon is not None and lat is not None:
            return lon, lat
    easting, northing = _float(props.origin_e), _float(props.origin_n)
    if props.crs.strip() and easting and northing:
        found = crs_catalog.lon_lat_of(props.crs.strip(), easting, northing)
        if found is not None:
            return found
    lon, lat = _float(props.anchor_lon), _float(props.anchor_lat)
    if lon is not None and lat is not None and (lon, lat) != (0.0, 0.0) and abs(lat) <= 90.0:
        return lon, lat
    return None


def search_crs(self: bpy.types.PropertyGroup, _context: bpy.types.Context, edit_text: str):
    """The picker's candidates for ``edit_text``: ``(label, area of use)`` pairs.

    The field's own value (just clicked, not edited yet) lists everything, near
    the site first.
    """
    try:
        current = self.crs.strip()
        query = edit_text or ""
        if query.strip() == current or crs_catalog.choice_value(query).strip() == current:
            query = ""
        near = site_lon_lat(self)
        entries = crs_catalog.search(query, near=near, near_only=bool(self.crs_near_site))
        if near is not None and self.crs_near_site and not entries:
            entries = crs_catalog.search(query, near=near)  # nothing holds the site
        return [(e.label, e.area_name) for e in entries]
    except Exception:  # a search callback must never raise into the UI
        log.exception("CRS search failed")
        return []


def crs_changed(self: bpy.types.PropertyGroup, _context: bpy.types.Context) -> None:
    """Forget a stale migration note.

    A chosen entry is already stored as its plain ``EPSG:nnnn`` by the field's
    setter (``scene_georef.set_crs``).
    """
    value = self.crs.strip()
    if self.crs_migrated_from and crs_catalog.migrated(self.crs_migrated_from) != value:
        self.crs_migrated_from = ""


# --- migration -------------------------------------------------------------------------


def migrate_scene(scene: bpy.types.Scene) -> str | None:
    """Replace GeoTools' saved preset PROJ string by its EPSG code; return the code.

    Only GeoTools' own CRS text (``source_crs``) is migrated, before it moves into
    the GeoScene keys (``scene_georef.reconcile_scene``). A ``SRID`` key is never
    rewritten, whatever it holds: other tools own it too.
    """
    props = getattr(scene, "geotools", None)
    if props is None:
        return None
    old = scene_georef.stored_crs(props).strip()
    code = crs_catalog.migrated(old)
    if code is None:
        return None
    props.crs_migrated_from = old
    scene_georef.write_stored_crs(props, code)
    log.info("Scene %s: CRS preset migrated to %s (same definition)", scene.name, code)
    return code


def migrate_all() -> list[str]:
    """Migrate every scene of the open file; return the names of those that changed."""
    changed = []
    for scene in bpy.data.scenes:
        try:
            if migrate_scene(scene) is not None:
                changed.append(scene.name)
        except Exception:  # never break file loading
            log.exception("CRS migration of scene %s failed", scene.name)
    return changed


def migrate_and_reconcile() -> None:
    """On load: migrate GeoTools' preset text, then move the georeference into the keys."""
    migrate_all()
    scene_georef.reconcile_all()


@persistent
def _on_load_post(*_args: object) -> None:
    migrate_and_reconcile()


def _after_register() -> None:
    # bpy.data is restricted during register(); the file already open is migrated here.
    migrate_and_reconcile()


# --- drawing ---------------------------------------------------------------------------


def draw_source_crs(
    layout: bpy.types.UILayout, context: bpy.types.Context, props: bpy.types.PropertyGroup
) -> None:
    """The CRS row of the Project panel: the search field, its name and rusty-tiles' verdict."""
    definition = crs_catalog.current_definition(props.crs.strip())
    result = crs_catalog.resolve(definition)
    col = split_column(layout)
    row = col.row(align=True)
    row.alert = not definition or (result is not None and not result.accepted)
    row.prop(props, "crs", placeholder="Search EPSG code or name")
    row.prop(props, "crs_near_site", text="", icon="ORIENTATION_GLOBAL")
    if not crs_catalog.available():
        status = deps.probe_rusty_tiles()
        wrapped(
            layout,
            context,
            f"No EPSG list or CRS check: {status.error or 'rusty-tiles is not available'} "
            "(see About).",
            icon="ERROR",
            max_lines=4,
        )
        return
    if not definition:
        layout.label(text="Search for an EPSG code, or type WKT or PROJ", icon="ERROR")
        return
    name = crs_catalog.name(definition)
    if name:
        info_wrapped(col, context, "", name)
    if result is None:
        return
    box = layout.column(align=True)
    if result.accepted:
        wrapped(box, context, result.message(), icon="CHECKMARK")
    else:
        box.alert = True
        wrapped(box, context, result.message(), icon="ERROR", max_lines=6)


def details_poll(props: bpy.types.PropertyGroup) -> bool:
    """Whether the CRS Details sub-panel has anything to show."""
    return props.placement_mode == "SOURCE_CRS" and bool(props.crs.strip())


def draw_details(
    layout: bpy.types.UILayout, context: bpy.types.Context, props: bpy.types.PropertyGroup
) -> None:
    """CRS Details: area of use, the definition rusty-tiles uses, the operation, the migration."""
    definition = crs_catalog.current_definition(props.crs.strip())
    found = crs_catalog.entry(definition)
    if found is not None and found.area_name:
        wrapped(layout, context, f"Area of use: {found.area_name}.", icon="WORLD")
    result = crs_catalog.resolve(definition)
    if result is not None and result.accepted:
        operation = result.operation or {}
        if operation.get("verification"):
            wrapped(layout, context, str(operation["verification"]), icon="INFO", max_lines=4)
        if result.resolved and result.resolved != definition:
            layout.label(text="rusty-tiles uses")
            wrapped(layout, context, result.resolved, max_lines=6)
    if props.crs_migrated_from:
        wrapped(
            layout,
            context,
            "Migrated from an old GeoTools CRS preset (a PROJ string with a null +towgs84 "
            "shift). rusty-tiles resolves this code to the same definition, so the placement "
            "is unchanged.",
            icon="FILE_REFRESH",
        )


# --- registration ----------------------------------------------------------------------

classes: tuple[type, ...] = ()
"""No Blender classes: the callbacks live on ``properties.GeoToolsProject``."""


def register() -> None:
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)
    if not bpy.app.timers.is_registered(_after_register):
        bpy.app.timers.register(_after_register, first_interval=0.0)


def unregister() -> None:
    if bpy.app.timers.is_registered(_after_register):
        bpy.app.timers.unregister(_after_register)
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)
