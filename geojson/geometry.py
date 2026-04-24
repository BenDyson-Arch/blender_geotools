"""
geometry.py — Blender mesh construction per GeoJSON geometry type.
"""

import bpy
import bmesh


def _coord(c):
    """Return a 3-tuple (x, y, z) from a 2- or 3-element coordinate."""
    return (c[0], c[1], c[2] if len(c) > 2 else 0.0)


def _signed_area_2d(ring):
    """Shoelace formula over XY. Positive → CCW, negative → CW."""
    n = len(ring)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += ring[i][0] * ring[j][1]
        area -= ring[j][0] * ring[i][1]
    return area / 2.0


def _open_ring(ring):
    """Drop the GeoJSON closing duplicate and return a list copy."""
    if len(ring) > 1 and ring[0][0] == ring[-1][0] and ring[0][1] == ring[-1][1]:
        return list(ring[:-1])
    return list(ring)


def _ensure_winding(ring, ccw=True):
    """Return ``ring`` (open, no closing dup) in the requested winding order.

    ``ccw=True`` → counter-clockwise (positive signed area, exterior rings).
    ``ccw=False`` → clockwise (negative signed area, interior/hole rings).
    """
    ring = _open_ring(ring)
    area = _signed_area_2d(ring)
    if ccw and area < 0:
        ring.reverse()
    elif not ccw and area > 0:
        ring.reverse()
    return ring


def _sanitize_ring(ring, ccw=True):
    """Return a face-safe open ring, or ``[]`` if it cannot form a polygon."""
    ring = _ensure_winding(ring, ccw=ccw)
    if not ring:
        return []

    cleaned = []
    for point in ring:
        if not cleaned or cleaned[-1][0] != point[0] or cleaned[-1][1] != point[1]:
            cleaned.append(point)

    distinct_xy = {(point[0], point[1]) for point in cleaned}
    if len(cleaned) < 3 or len(distinct_xy) < 3:
        return []
    return cleaned


def _new_edge_loop(bm, ring):
    """Create one closed edge loop for ``ring`` and return its edges."""
    ring_verts = [bm.verts.new(_coord(point)) for point in ring]
    edges = []
    for index in range(len(ring_verts)):
        edges.append(
            bm.edges.new((ring_verts[index], ring_verts[(index + 1) % len(ring_verts)]))
        )
    return ring_verts, edges


def _add_polygon_to_bmesh(bm, exterior, holes=None, ignore_holes=False):
    """Add one polygon to ``bm`` and return ``True`` when geometry was created."""
    holes = holes or []
    ext = _sanitize_ring(exterior, ccw=True)
    if len(ext) < 3:
        return False

    if ignore_holes or not holes:
        ext_verts = [bm.verts.new(_coord(point)) for point in ext]
        bm.faces.new(ext_verts)
        return True

    _, all_edges = _new_edge_loop(bm, ext)
    for hole in holes:
        hole_ring = _sanitize_ring(hole, ccw=False)
        if len(hole_ring) < 3:
            continue
        _, hole_edges = _new_edge_loop(bm, hole_ring)
        all_edges.extend(hole_edges)

    bmesh.ops.triangle_fill(bm, use_beauty=True, use_dissolve=True, edges=all_edges)
    return True


# ---------------------------------------------------------------------------
# Point
# ---------------------------------------------------------------------------

def make_point(coords):
    """Single-vertex mesh from one coordinate."""
    mesh = bpy.data.meshes.new("Point")
    bm = bmesh.new()
    bm.verts.new(_coord(coords))
    bm.to_mesh(mesh)
    bm.free()
    return mesh


def make_multipoint(coords_list):
    """Multi-vertex mesh, one vertex per point."""
    mesh = bpy.data.meshes.new("MultiPoint")
    bm = bmesh.new()
    for c in coords_list:
        bm.verts.new(_coord(c))
    bm.to_mesh(mesh)
    bm.free()
    return mesh


# ---------------------------------------------------------------------------
# LineString
# ---------------------------------------------------------------------------

def make_linestring(coords):
    """Edge-only mesh from a sequence of coordinates."""
    mesh = bpy.data.meshes.new("LineString")
    bm = bmesh.new()
    verts = [bm.verts.new(_coord(c)) for c in coords]
    for i in range(len(verts) - 1):
        bm.edges.new((verts[i], verts[i + 1]))
    bm.to_mesh(mesh)
    bm.free()
    return mesh


def make_multilinestring(coords_list):
    """Edge-only mesh with disconnected edge chains, one per LineString."""
    mesh = bpy.data.meshes.new("MultiLineString")
    bm = bmesh.new()
    for chain in coords_list:
        verts = [bm.verts.new(_coord(c)) for c in chain]
        for i in range(len(verts) - 1):
            bm.edges.new((verts[i], verts[i + 1]))
    bm.to_mesh(mesh)
    bm.free()
    return mesh


# ---------------------------------------------------------------------------
# Polygon
# ---------------------------------------------------------------------------

def make_polygon(exterior, holes=None, ignore_holes=False):
    """Face mesh from an exterior ring and optional interior rings (holes).

    ``exterior`` and each entry in ``holes`` are lists of coordinate arrays.
    GeoJSON rings close back to the first vertex; the closing duplicate is
    dropped automatically.

    Winding order is normalised before mesh construction:
    - Exterior ring → counter-clockwise (CCW, positive signed area in XY)
    - Hole rings    → clockwise (CW, negative signed area in XY)

    When holes are present, all ring edges are passed to
    ``bmesh.ops.triangle_fill`` which triangulates the region between the
    loops and then dissolves interior edges into ngons. This produces a
    correct topology where the hole boundary is present but the hole area
    has no faces.

    Set ``ignore_holes=True`` to skip all hole processing and produce a
    simple flat face from the exterior ring only.
    """
    mesh = bpy.data.meshes.new("Polygon")
    bm = bmesh.new()

    _add_polygon_to_bmesh(bm, exterior, holes=holes, ignore_holes=ignore_holes)

    bm.to_mesh(mesh)
    bm.free()
    return mesh


def make_multipolygon(polygon_list, ignore_holes=False):
    """Single mesh with one disconnected face per polygon in the list.

    ``polygon_list`` is a list of polygon coordinate arrays, where each
    entry is ``[exterior_ring, *hole_rings]``.
    """
    mesh = bpy.data.meshes.new("MultiPolygon")
    bm = bmesh.new()

    for poly_rings in polygon_list:
        if not poly_rings:
            continue
        exterior = poly_rings[0]
        holes = poly_rings[1:] if len(poly_rings) > 1 else []
        _add_polygon_to_bmesh(bm, exterior, holes=holes, ignore_holes=ignore_holes)

    bm.to_mesh(mesh)
    bm.free()
    return mesh


# ---------------------------------------------------------------------------
# Extrusion
# ---------------------------------------------------------------------------

def extrude_mesh_by_value(mesh, value):
    """Extrude all faces of ``mesh`` along world Z by ``value`` in-place.

    Only operates when ``value`` is a positive finite number. Silently
    returns without modification for zero, negative, non-numeric, or
    absent values — allowing flat polygons to remain flat without errors.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return

    if not (value > 0):
        return

    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.faces.ensure_lookup_table()

    faces = list(bm.faces)
    if not faces:
        bm.free()
        return

    result = bmesh.ops.extrude_face_region(bm, geom=faces)
    extruded_verts = [v for v in result["geom"] if isinstance(v, bmesh.types.BMVert)]
    bmesh.ops.translate(bm, verts=extruded_verts, vec=(0.0, 0.0, value))

    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


# ---------------------------------------------------------------------------
# Mesh joining
# ---------------------------------------------------------------------------

def join_meshes(mesh_list, name="Joined", operator=None):
    """Merge a list of bpy.types.Mesh objects into a single new mesh.

    Each source mesh's geometry is appended in sequence. The source meshes
    are not modified or freed — the caller is responsible for removing them
    from ``bpy.data.meshes`` if no longer needed.

    The ``feature_index`` attribute is stored on faces only. For Point and
    LineString geometries that have no faces the index is silently lost;
    if ``operator`` is provided a warning is emitted for those meshes.

    Returns a new ``bpy.types.Mesh`` named ``name``.
    """
    out_mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()

    feat_index_layer = bm.faces.layers.int.new("feature_index")

    for feat_index, src_mesh in enumerate(mesh_list):
        tmp_bm = bmesh.new()
        tmp_bm.from_mesh(src_mesh)
        if not tmp_bm.faces and operator is not None:
            operator.report(
                {"WARNING"},
                f"Feature {feat_index} ({src_mesh.name}) has no faces — "
                "feature_index attribute will not be stored for this geometry.",
            )
        vert_offset = len(bm.verts)
        for v in tmp_bm.verts:
            bm.verts.new(v.co)
        bm.verts.ensure_lookup_table()
        for e in tmp_bm.edges:
            try:
                bm.edges.new((bm.verts[vert_offset + e.verts[0].index],
                               bm.verts[vert_offset + e.verts[1].index]))
            except ValueError:
                pass  # edge already exists (shared boundary)
        for f in tmp_bm.faces:
            try:
                face_verts = [bm.verts[vert_offset + v.index] for v in f.verts]
                new_face = bm.faces.new(face_verts)
                new_face[feat_index_layer] = feat_index
            except ValueError:
                pass
        tmp_bm.free()

    bm.to_mesh(out_mesh)
    bm.free()
    return out_mesh

