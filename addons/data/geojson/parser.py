"""
parser.py — GeoJSON parsing, geometry extraction, and property handling.
"""

import json
from pathlib import Path


# Geometry types defined by GeoJSON (RFC 7946)
_GEOMETRY_TYPES = {
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection",
}


def load_geojson(filepath):
    """Read a GeoJSON file and return the parsed dict.

    Validates that the top-level object has a recognised ``type`` field.
    Raises ``ValueError`` for unrecognised types and ``FileNotFoundError``
    if the path does not exist.
    """
    path = Path(filepath)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    top_type = data.get("type")
    valid_top = {"FeatureCollection", "Feature"} | _GEOMETRY_TYPES
    if top_type not in valid_top:
        raise ValueError(
            f"Unrecognised GeoJSON top-level type: {top_type!r}. "
            f"Expected one of: {', '.join(sorted(valid_top))}"
        )

    return data


def iter_features(geojson):
    """Yield normalised feature dicts from any valid GeoJSON object.

    Handles:
    - ``FeatureCollection`` — iterates ``features`` array
    - ``Feature`` — yields the single feature
    - Bare geometry — wraps in a synthetic feature with ``properties={}``

    Each yielded dict has the shape::

        {"geometry": <geometry dict or None>,
         "properties": <dict>,
         "id": <value or None>}
    """
    top_type = geojson.get("type")

    if top_type == "FeatureCollection":
        for feat in geojson.get("features", []):
            yield _normalise_feature(feat)

    elif top_type == "Feature":
        yield _normalise_feature(geojson)

    elif top_type in _GEOMETRY_TYPES:
        # Bare geometry — wrap it
        yield {
            "geometry": geojson,
            "properties": {},
            "id": None,
        }

    else:
        raise ValueError(f"Cannot iterate features from GeoJSON type: {top_type!r}")


def _normalise_feature(feat):
    """Return a consistent feature dict from a GeoJSON Feature object."""
    return {
        "geometry": feat.get("geometry"),
        "properties": feat.get("properties") or {},
        "id": feat.get("id"),
    }


def extract_coordinates(geometry):
    """Recursively extract coordinate arrays from a geometry dict.

    Returns a list whose structure mirrors the geometry type:

    - ``Point``            → ``[x, y]`` or ``[x, y, z]``
    - ``MultiPoint``       → ``[[x, y, z], ...]``
    - ``LineString``       → ``[[x, y, z], ...]``
    - ``MultiLineString``  → ``[[[x, y, z], ...], ...]``
    - ``Polygon``          → ``[exterior_ring, *hole_rings]``
    - ``MultiPolygon``     → ``[[exterior_ring, *hole_rings], ...]``
    - ``GeometryCollection`` → ``[extract_coordinates(g) for g in geometries]``

    Z coordinates are preserved when present.
    Returns an empty list if ``geometry`` is ``None``.
    """
    if geometry is None:
        return []

    gtype = geometry.get("type")

    if gtype == "GeometryCollection":
        return [extract_coordinates(g) for g in geometry.get("geometries", [])]

    coords = geometry.get("coordinates")
    if coords is None:
        return []

    return coords


def get_crs(geojson):
    """Return the CRS dict from a GeoJSON object, or ``None`` if absent.

    The ``crs`` member is deprecated in RFC 7946 but still common in
    older exports (e.g. GeoServer, QGIS). Returns the raw dict so callers
    can interpret the name/link CRS object as needed.
    """
    return geojson.get("crs")


def extract_crs_string(crs):
    """Extract a plain EPSG/authority string from a GeoJSON CRS object.

    Handles the two common forms:
    - Named CRS:  ``{"type": "name", "properties": {"name": "EPSG:4326"}}``
    - Linked CRS: ``{"type": "link", "properties": {"href": "...", "type": "..."}}``

    For named CRS the function normalises common URN forms::

        urn:ogc:def:crs:EPSG::4326  →  EPSG:4326
        urn:ogc:def:crs:OGC:1.3:CRS84  →  EPSG:4326

    Returns the raw name string if it cannot be normalised, and ``None``
    if ``crs`` is ``None`` or the structure is unrecognised.
    """
    if crs is None:
        return None

    crs_type = crs.get("type")
    props = crs.get("properties") or {}

    if crs_type == "name":
        name = props.get("name", "")
        # Normalise URN → EPSG:XXXX
        if "EPSG" in name.upper():
            # e.g. urn:ogc:def:crs:EPSG::4326 or EPSG:4326
            parts = name.replace(",", ":").replace("::", ":").split(":")
            epsg_idx = next((i for i, p in enumerate(parts) if p.upper() == "EPSG"), None)
            if epsg_idx is not None and epsg_idx + 1 < len(parts):
                code = parts[epsg_idx + 1]
                if code:
                    return f"EPSG:{code}"
        # OGC CRS84 is essentially EPSG:4326 with lon/lat axis order
        if "CRS84" in name.upper():
            return "EPSG:4326"
        return name or None

    if crs_type == "link":
        href = props.get("href", "")
        # Try to extract EPSG code from URL, e.g. .../EPSG/0/4326
        if "epsg" in href.lower():
            parts = href.rstrip("/").split("/")
            for i, p in enumerate(parts):
                if p.lower() == "epsg" and i + 2 < len(parts):
                    return f"EPSG:{parts[i + 2]}"
        return href or None

    return None


# ---------------------------------------------------------------------------
# Coordinate offset utilities
# ---------------------------------------------------------------------------

def _iter_all_positions(geometry):
    """Yield every leaf coordinate list ``[x, y]`` or ``[x, y, z]`` in a geometry."""
    if geometry is None:
        return
    gtype = geometry.get("type")
    if gtype == "GeometryCollection":
        for g in geometry.get("geometries", []):
            yield from _iter_all_positions(g)
        return
    coords = geometry.get("coordinates")
    if coords is None:
        return
    yield from _iter_leaf_coords(coords, gtype)


def _iter_leaf_coords(coords, gtype):
    if gtype == "Point":
        yield coords
    elif gtype in ("MultiPoint", "LineString"):
        yield from coords
    elif gtype in ("MultiLineString", "Polygon"):
        for ring in coords:
            yield from ring
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                yield from ring


def is_geographic_coordinates(features):
    """Return True if the dataset appears to use geographic (degree) coordinates.

    Samples up to 20 positions and checks whether all X values fall within
    [-180, 180] and all Y values within [-90, 90]. This is a heuristic —
    projected datasets that happen to fall in those ranges will be mis-detected,
    but that is an acceptable trade-off given the distortion geographic coordinates
    would cause in Blender.

    Returns False if no positions are found.
    """
    sampled = 0
    for feat in features:
        for pos in _iter_all_positions(feat.get("geometry")):
            x, y = pos[0], pos[1]
            if not (-180.0 <= x <= 180.0 and -90.0 <= y <= 90.0):
                return False
            sampled += 1
            if sampled >= 20:
                return True
    return sampled > 0


def compute_bbox_centroid(features, operator=None):
    """Return ``(cx, cy, cz)`` — the centroid of the bounding box of all features.

    ``features`` is an iterable of normalised feature dicts as returned by
    ``iter_features``. Iterates coordinates once.

    Emits a warning via ``operator.report`` if the centroid appears to be in
    geographic degrees (``|cx| <= 180`` and ``|cy| <= 90``).

    Returns ``(0.0, 0.0, 0.0)`` if no coordinates are found.
    """
    min_x = min_y = min_z = float("inf")
    max_x = max_y = max_z = float("-inf")
    found = False

    for feat in features:
        for pos in _iter_all_positions(feat.get("geometry")):
            found = True
            x, y = pos[0], pos[1]
            z = pos[2] if len(pos) > 2 else 0.0
            if x < min_x: min_x = x
            if x > max_x: max_x = x
            if y < min_y: min_y = y
            if y > max_y: max_y = y
            if z < min_z: min_z = z
            if z > max_z: max_z = z

    if not found:
        return (0.0, 0.0, 0.0)

    cx = (min_x + max_x) / 2.0
    cy = (min_y + max_y) / 2.0
    cz = (min_z + max_z) / 2.0

    if abs(cx) <= 180.0 and abs(cy) <= 90.0:
        msg = (
            f"Centroid ({cx:.4f}, {cy:.4f}) appears to be in geographic degrees "
            "(EPSG:4326). Large-extent imports may suffer floating-point jitter. "
            "Enable 'Offset to world origin' to mitigate this."
        )
        if operator is not None:
            operator.report({"WARNING"}, msg)
    return (cx, cy, cz)


def apply_offset(coords, offset):
    """Subtract the XY components of ``offset`` from every coordinate in ``coords`` in-place.

    Only X and Y are offset. Z is never modified — elevation values are
    preserved as-is in Blender world space.

    ``coords`` may be a single position ``[x, y]`` / ``[x, y, z]``, or any
    nested list of positions (as returned by ``geometry["coordinates"]``).
    Modifies the list in-place and also returns it.

    ``offset`` is a 2- or 3-tuple ``(ox, oy[, oz])``. Only ox and oy are used.
    """
    ox, oy = offset[0], offset[1]
    _apply_offset_recursive(coords, ox, oy)
    return coords


def _apply_offset_recursive(node, ox, oy):
    """Recursively walk a coordinate tree and subtract the XY offset."""
    if not node:
        return
    # Leaf: a list of numbers
    if isinstance(node[0], (int, float)):
        node[0] -= ox
        node[1] -= oy
        # Z intentionally not modified
    else:
        for child in node:
            _apply_offset_recursive(child, ox, oy)


def flip_yz_coords(coords):
    """Swap Y and Z in every coordinate in ``coords`` in-place.

    Used for Y-up source data that should be imported with Z-up orientation.
    ``coords`` may be a single position or any nested list of positions.
    """
    _flip_yz_recursive(coords)
    return coords


def _flip_yz_recursive(node):
    if not node:
        return
    if isinstance(node[0], (int, float)):
        y = node[1] if len(node) > 1 else 0.0
        z = node[2] if len(node) > 2 else 0.0
        if len(node) > 2:
            node[1] = z
            node[2] = y
        else:
            node.append(y)
            node[1] = 0.0
    else:
        for child in node:
            _flip_yz_recursive(child)

