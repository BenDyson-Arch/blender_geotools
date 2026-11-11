"""Linked sources: ``obj.geotools_link`` (docs/design/m8-geo-formats.md, "Linked sources").

An object imported from a geo file keeps a link to it, so Refresh can re-read
the file with the same options and replace the object's data in place. This
module holds only the property group and small helpers shared by the
importers; the Linked Data panel, Refresh, stale detection and auto-refresh
live with the vector work stream.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty

from ..core import fingerprint as core_fingerprint

KIND_ITEMS = (
    ("GEOJSON", "GeoJSON", "A GeoJSON file", "FILE_TEXT", 0),
    ("GPKG", "GeoPackage", "A GeoPackage feature table", "FILE_BLEND", 1),
    ("GEOTIFF", "GeoTIFF", "A GeoTIFF DEM or orthophoto", "FILE_IMAGE", 2),
)

STATUS_ITEMS = (
    ("NONE", "Not Linked", "Not linked to a file"),
    ("OK", "Up to Date", "Matches the file as last read"),
    ("CHANGED", "Changed on Disk", "The file changed since it was last read"),
    ("MISSING", "Missing", "The file cannot be found"),
    ("ERROR", "Error", "The last refresh failed"),
)


class GeoToolsLink(bpy.types.PropertyGroup):
    """The file an object was imported from, and how (``obj.geotools_link``)."""

    kind: EnumProperty(name="Kind", items=KIND_ITEMS, default="GEOTIFF")
    filepath: StringProperty(name="File", subtype="FILE_PATH")
    layer: StringProperty(name="Layer", description="Table or GeoJSON path; empty for one")
    fid_field: StringProperty(name="Feature ID Field")
    crs: StringProperty(name="CRS", description="The data's CRS when it was last read")
    options: StringProperty(name="Options", description="Import options, as JSON")
    fingerprint: StringProperty(
        name="Fingerprint", description="mtime_ns, size and sha256 of the file, as JSON"
    )
    last_sync: StringProperty(name="Last Sync", description="UTC time of the last read")
    auto_refresh: BoolProperty(
        name="Auto-Refresh", description="Refresh when the file changes on disk", default=False
    )
    status: EnumProperty(name="Status", items=STATUS_ITEMS, default="NONE")


def link_options(link: GeoToolsLink) -> dict[str, object]:
    """``link.options`` as a dict ({} if empty or not JSON)."""
    try:
        value = json.loads(link.options) if link.options else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def mark_synced(link: GeoToolsLink, path: str | os.PathLike[str]) -> None:
    """Record the file's fingerprint and the time, and mark the link up to date."""
    link.fingerprint = core_fingerprint.to_text(core_fingerprint.fingerprint(path))
    link.last_sync = datetime.now(UTC).isoformat(timespec="seconds")
    link.status = "OK"


classes = (GeoToolsLink,)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.Object.geotools_link = PointerProperty(type=GeoToolsLink)


def unregister() -> None:
    if hasattr(bpy.types.Object, "geotools_link"):
        del bpy.types.Object.geotools_link
    _unregister_classes()
