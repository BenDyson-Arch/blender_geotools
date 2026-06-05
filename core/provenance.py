"""core/provenance.py — Generic provenance system for imported geo features.

Every Blender object created by a geo-format importer (GeoJSON, GPKG, …)
gets a small set of standard custom properties that record where it came
from.  These are the keys and helpers for reading/writing them.

Collection-level properties
---------------------------
GEO_SOURCE         Absolute filepath of the source file.
GEO_LAYER          Layer / table name (or empty string for single-layer formats).
GEO_FORMAT         Format identifier: ``"gpkg"``, ``"geojson"``, …
GEO_TRACKED_FIDS   JSON-encoded list of integer fids at last import/sync.
GEO_SCHEMA         JSON-encoded column schema (see ``set_schema`` / ``get_schema``).

Object-level properties
-----------------------
GEO_SOURCE         Same filepath as on the collection.
GEO_LAYER          Same layer name.
GEO_FORMAT         Format identifier.
GEO_FID            Integer primary-key of the source row (absent on new features).
GEO_GEOM_TYPE      GeoJSON geometry type string (``"Point"``, ``"Polygon"``, …).
GEO_SCENE_SRID     Scene CRS at import time (used by write-back reprojection).
"""

import json

# ---------------------------------------------------------------------------
# Key constants
# ---------------------------------------------------------------------------

GEO_SOURCE = "geo_source"  # str — absolute filepath
GEO_LAYER = "geo_layer"  # str — layer / table name
GEO_FORMAT = "geo_format"  # str — "gpkg", "geojson", …
GEO_FID = "geo_fid"  # int — feature primary key
GEO_GEOM_TYPE = "geo_geom_type"  # str — "Point", "Polygon", …
GEO_SCENE_SRID = "geo_scene_srid"  # str — CRS string at import time
GEO_TRACKED_FIDS = "geo_tracked_fids"  # str (JSON list) — on collection
GEO_SCHEMA = "geo_schema"  # str (JSON dict) — on collection
GEO_IMPORT_SETTINGS = "geo_import_settings"  # str (JSON dict) — import config snapshot

# Internal keys that should be excluded from attribute write-back
ALL_PROVENANCE_KEYS = frozenset(
    {
        GEO_SOURCE,
        GEO_LAYER,
        GEO_FORMAT,
        GEO_FID,
        GEO_GEOM_TYPE,
        GEO_SCENE_SRID,
        GEO_TRACKED_FIDS,
        GEO_SCHEMA,
        GEO_IMPORT_SETTINGS,
        "geo_orphan",
        # Legacy gpkg_* keys (kept for one-version migration tolerance)
        "gpkg_source",
        "gpkg_layer",
        "gpkg_fid",
        "gpkg_geom_type",
        "gpkg_scene_srid",
        "gpkg_tracked_fids_json",
        "gpkg_orphan",
        "gpkg_import_holes",
        # Blender internals
        "cycles",
        "_RNA_UI",
    }
)


# ---------------------------------------------------------------------------
# Object helpers
# ---------------------------------------------------------------------------


def stamp(
    obj,
    *,
    source: str,
    layer: str,
    fmt: str,
    fid: int | None = None,
    geom_type: str = "",
    scene_srid: str = "",
    settings: dict | None = None,
):
    """Write provenance custom properties onto a Blender object."""
    obj[GEO_SOURCE] = source
    obj[GEO_LAYER] = layer
    obj[GEO_FORMAT] = fmt
    if fid is not None:
        obj[GEO_FID] = fid
    if geom_type:
        obj[GEO_GEOM_TYPE] = geom_type
    if scene_srid:
        obj[GEO_SCENE_SRID] = scene_srid
    if settings is not None:
        obj[GEO_IMPORT_SETTINGS] = json.dumps(settings)


def read_source(obj) -> tuple[str, str, str] | None:
    """Return ``(source, layer, format)`` or ``None`` if not a geo object."""
    src = obj.get(GEO_SOURCE)
    if src is None:
        return None
    return src, obj.get(GEO_LAYER, ""), obj.get(GEO_FORMAT, "")


def has_provenance(obj) -> bool:
    """True if the object has provenance properties (any format)."""
    return GEO_SOURCE in obj


def is_new_feature(obj) -> bool:
    """True if the object has provenance but no fid (pending insertion)."""
    return has_provenance(obj) and GEO_FID not in obj


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def stamp_collection(
    col,
    *,
    source: str,
    layer: str,
    fmt: str,
    fids: list[int] | None = None,
    schema: dict | None = None,
    settings: dict | None = None,
):
    """Write provenance + tracked fids + schema on a Blender collection."""
    col[GEO_SOURCE] = source
    col[GEO_LAYER] = layer
    col[GEO_FORMAT] = fmt
    if fids is not None:
        col[GEO_TRACKED_FIDS] = json.dumps(fids)
    if schema is not None:
        col[GEO_SCHEMA] = json.dumps(schema)
    if settings is not None:
        col[GEO_IMPORT_SETTINGS] = json.dumps(settings)


def get_tracked_fids(col) -> set[int]:
    """Return the set of tracked feature ids from the collection."""
    raw = col.get(GEO_TRACKED_FIDS, "[]")
    try:
        return set(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return set()


def set_tracked_fids(col, fids):
    """Store *fids* as the tracked fid set on the collection."""
    col[GEO_TRACKED_FIDS] = json.dumps(list(fids))


def set_schema(col, columns: dict, geom_type: str = ""):
    """Store the layer column schema on the collection.

    ``columns`` maps column name → SQL type string (e.g. ``{"name": "TEXT"}``).
    ``geom_type`` is the layer's default geometry type.
    """
    col[GEO_SCHEMA] = json.dumps({"columns": columns, "geom_type": geom_type})


def get_schema(col) -> dict | None:
    """Return ``{"columns": {…}, "geom_type": "…"}`` or ``None``."""
    raw = col.get(GEO_SCHEMA)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def get_import_settings(owner) -> dict | None:
    """Return the stored import-settings snapshot from an object or collection."""
    raw = owner.get(GEO_IMPORT_SETTINGS)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def collect_attribute_props(obj) -> dict:
    """Return ``{col: value}`` for feature attributes on *obj*.

    Reads mesh attributes first (the authoritative source for numeric/bool
    data), then supplements with non-provenance custom properties (strings
    and other types that cannot be mesh attributes).
    """
    result = {}

    # 1) Mesh attributes — authoritative for numeric/bool/vector values
    mesh = getattr(obj, "data", None)
    if mesh is not None and hasattr(mesh, "attributes"):
        from ..addons.data.geojson.attributes import read_mesh_attributes

        result.update(read_mesh_attributes(mesh))

    # 2) Custom properties — strings and other non-mesh types
    for k, v in obj.items():
        if k in ALL_PROVENANCE_KEYS or k.startswith("_"):
            continue
        if k in result:
            continue  # mesh attribute takes precedence
        result[k] = v

    return result
