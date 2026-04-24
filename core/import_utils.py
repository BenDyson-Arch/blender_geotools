# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0
"""
core/import_utils.py — Shared import pipeline helpers.

Used by all vector-feature importers (GeoJSON, GPKG, and future formats).
Provides:
  - Coordinate reprojection helpers (reproject_node, reproject_features)
  - Auto-reproject geographic CRS to UTM / Web Mercator
  - Scene origin offset application
  - Blender collection management helpers
  - Geometry-type dispatch to geojson/geometry.py mesh builders
"""

import copy
import logging

import bpy

from ..geoscene import GeoScene
from .proj.reproj import Reproj
from .proj.srs import SRS
from . import settings as _settings_mod

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coordinate reprojection
# ---------------------------------------------------------------------------

def reproject_node(node, rprj):
    """Recursively reproject a GeoJSON coordinate node in-place.

    ``node`` is either a leaf position ``[x, y, ...]`` or a nested list of
    positions. The ``rprj`` object must expose a ``.pt(x, y)`` method that
    returns a ``(x', y')`` tuple.
    """
    if not node:
        return
    if isinstance(node[0], (int, float)):
        x, y = rprj.pt(node[0], node[1])
        node[0] = x
        node[1] = y
    else:
        for child in node:
            reproject_node(child, rprj)


def reproject_features(feats, src_srid, dst_srid):
    """Reproject GeoJSON-style features from *src_srid* to *dst_srid* in-place.

    Uses an atomic deep-copy strategy so that a mid-list reprojection failure
    never leaves ``feats`` in a mixed-CRS state.  If ``src_srid == dst_srid``
    the function returns immediately without touching the data.

    Raises whatever exception ``Reproj`` raises on failure (e.g.
    ``ReprojError``, ``ImportError`` if no backend is available).
    """
    if src_srid == dst_srid:
        return
    rprj = Reproj(src_srid, dst_srid)
    new_coords = []
    for feat in feats:
        geom = feat.get("geometry")
        if geom and geom.get("coordinates"):
            coords_copy = copy.deepcopy(geom["coordinates"])
            reproject_node(coords_copy, rprj)
            new_coords.append((geom, coords_copy))
        else:
            new_coords.append((None, None))
    # All features reprojected without error — apply atomically.
    for geom, coords_copy in new_coords:
        if geom is not None:
            geom["coordinates"] = coords_copy


def auto_reproject_geographic(feats, srid, operator, compute_centroid_fn):
    """Auto-reproject features that are in a geographic (degree) CRS.

    Geographic coordinates are unusable in Blender — a 10 km city spans
    ~0.1 degrees, which collapses to an invisible point.  This function:

    1. Detects a suitable UTM zone from the data centroid.
    2. Falls back to EPSG:3857 (Web Mercator) if UTM reprojection fails.
    3. Reports an error and returns ``(feats, None)`` if both fail.

    Parameters
    ----------
    feats:
        List of GeoJSON-style feature dicts.
    srid:
        Current CRS of the features (must satisfy ``SRS(srid).isGeo``).
    operator:
        Blender operator instance used for ``operator.report()``.
    compute_centroid_fn:
        Callable ``(feats) -> (cx, cy, cz)`` that returns the data centroid.
        Typically ``parser.compute_bbox_centroid`` from the calling module.

    Returns
    -------
    (srid, success): tuple of the new SRID string and a bool.
    """
    from .proj.utm import lonlat_to_epsg
    cx, cy, _ = compute_centroid_fn(feats)
    auto_srid = lonlat_to_epsg(cx, cy)
    try:
        reproject_features(feats, srid, auto_srid)
        operator.report(
            {"INFO"},
            f"Auto-reprojected {srid} → {auto_srid} "
            f"(degree coordinates are not usable in Blender)",
        )
        return auto_srid, True
    except Exception as exc:
        # UTM failed — try Web Mercator as last resort (always built-in)
        try:
            reproject_features(feats, srid, "EPSG:3857")
            operator.report({"INFO"}, f"Auto-reprojected {srid} → EPSG:3857")
            return "EPSG:3857", True
        except Exception:
            operator.report(
                {"ERROR"},
                f"Cannot reproject geographic coordinates ({srid}): {exc}. "
                f"Degree-based geometry would collapse to a point in Blender.",
            )
            return srid, False


# ---------------------------------------------------------------------------
# Scene origin offset
# ---------------------------------------------------------------------------

def apply_scene_offset(context, feats, srid, operator, compute_centroid_fn, apply_offset_fn):
    """Apply the scene origin offset to all feature coordinates.

    If the scene does not yet have a georeferenced origin, one is computed
    from the data centroid and stored in ``GeoScene``.

    Parameters
    ----------
    context:
        Blender context.
    feats:
        List of GeoJSON-style feature dicts (modified in-place).
    srid:
        CRS string of the features after reprojection.
    operator:
        Blender operator for reporting.
    compute_centroid_fn:
        Callable ``(feats, operator=None) -> (cx, cy, cz)`` — typically
        ``parser.compute_bbox_centroid``.
    apply_offset_fn:
        Callable ``(coordinates, offset)`` that subtracts the offset from
        a coordinate array — typically ``parser.apply_offset``.

    Returns
    -------
    offset: tuple ``(ox, oy, 0.0)`` — the subtracted offset.
    """
    gs = GeoScene(context.scene)

    if gs.has_origin_proj:
        ox = gs.origin_x
        oy = gs.origin_y
    else:
        _is_geo_crs = (not srid) or bool(SRS(srid).isGeo)
        centroid = compute_centroid_fn(feats, operator if _is_geo_crs else None)
        ox, oy = centroid[0], centroid[1]
        gs.set_origin_proj(ox, oy, srid)

    offset = (ox, oy, 0.0)
    for feat in feats:
        geom = feat.get("geometry")
        if geom and geom.get("coordinates"):
            apply_offset_fn(geom["coordinates"], offset)

    return offset


# ---------------------------------------------------------------------------
# Blender collection helpers
# ---------------------------------------------------------------------------

def get_or_create_collection(context, name, parent=None):
    """Return the named collection, creating it if absent.

    If *parent* is given the new collection is linked under it; otherwise it
    is linked directly to the scene root collection.
    """
    if name in bpy.data.collections:
        return bpy.data.collections[name]
    col = bpy.data.collections.new(name)
    if parent is not None:
        parent.children.link(col)
    else:
        context.scene.collection.children.link(col)
    return col


def link_to_collection(context, obj, collection):
    """Link *obj* to *collection*, removing it from the scene root if present."""
    if obj.name not in collection.objects:
        collection.objects.link(obj)
    if obj.name in context.scene.collection.objects:
        context.scene.collection.objects.unlink(obj)


# ---------------------------------------------------------------------------
# Geometry dispatch
# ---------------------------------------------------------------------------

def build_mesh_for_feature(feat, import_holes, geometry_mod):
    """Return a ``bpy.types.Mesh`` for the feature's geometry, or ``None``.

    Parameters
    ----------
    feat:
        GeoJSON-style feature dict with a ``"geometry"`` key.
    import_holes:
        Whether interior rings (holes) should be cut into polygon faces.
    geometry_mod:
        The ``geojson.geometry`` module (or any module exposing the same
        ``make_*`` functions).  Passed explicitly so this function has no
        hard dependency on a specific submodule path.
    """
    geom = feat.get("geometry")
    if geom is None:
        return None

    gtype = geom.get("type")
    coords = geom.get("coordinates")

    if gtype == "Point":
        return geometry_mod.make_point(coords)
    elif gtype == "MultiPoint":
        return geometry_mod.make_multipoint(coords)
    elif gtype == "LineString":
        return geometry_mod.make_linestring(coords)
    elif gtype == "MultiLineString":
        return geometry_mod.make_multilinestring(coords)
    elif gtype == "Polygon":
        exterior = coords[0] if coords else []
        holes = coords[1:] if len(coords) > 1 else []
        return geometry_mod.make_polygon(exterior, holes=holes,
                                         ignore_holes=not import_holes)
    elif gtype == "MultiPolygon":
        return geometry_mod.make_multipolygon(coords, ignore_holes=not import_holes)
    elif gtype == "GeometryCollection":
        return None  # caller handles GeometryCollection separately
    return None


def geometry_type(feat):
    """Return the geometry type string for *feat*, or ``'Unknown'``."""
    geom = feat.get("geometry")
    return (geom.get("type") or "Unknown") if geom else "Unknown"
