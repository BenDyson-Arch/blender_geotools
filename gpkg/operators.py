"""gpkg/operators.py — GPKG import, reload, and write-back operators.

Three operators:

GEOTOOLS_OT_import_gpkg
    File-browser operator.  Reads a GPKG layer and imports it into Blender as
    mesh objects.  Writes provenance custom properties (geo_source, geo_layer,
    geo_fid, geo_geom_type, geo_scene_srid) on every imported object so that
    the reload and write-back operators can identify the source row later.

GEOTOOLS_OT_reload_gpkg
    Re-reads the source layer from disk, matches features by geo_fid, replaces
    mesh data and custom properties in-place.  New features that appear in the
    file become new objects; objects whose fid is no longer in the file are
    flagged with a "geo_orphan" custom property.

GEOTOOLS_OT_write_gpkg
    Synchronises the Blender state back to the .gpkg file:
      - attribute write-back  (changed custom properties → UPDATE column values)
      - geometry write-back   (moved vertices → UPDATE geometry blob)
      - new feature insertion (objects without geo_fid → INSERT + assign new fid)
      - feature deletion      (fids tracked at import time that are now missing
                               from the collection → DELETE, with confirmation)
    All writes for one layer run inside a single transaction.

Geometry reconstruction (write-back):
    The original geometry type is read from geo_geom_type on the object.
      Point       — single vertex position
      LineString  — vertices in edge order (DFS from lowest-index vertex)
      Polygon     — exterior ring from the first face loop; each disconnected
                    face island becomes an additional interior ring
      Multi*      — not supported in this release; a warning is emitted and the
                    feature is skipped
"""

from pathlib import Path

import bpy

from ..geoscene import GeoScene
from ..core.proj.reproj import Reproj
from ..core.proj.srs import SRS
from ..core import import_utils
from ..geojson import geometry as _geometry_mod
from ..geojson import attributes as _attributes_mod
from ..core import provenance as _prov
from . import reader as _reader
from . import writer as _writer


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UNSUPPORTED_IMPORT_TYPES = {"GeometryCollection"}


# ---------------------------------------------------------------------------
# Shared import pipeline
# ---------------------------------------------------------------------------

def _run_import(operator, context, settings):
    """Core import logic shared by the file-browser and N-panel operators."""
    gs = GeoScene(context.scene)
    if gs.is_broken:
        operator.report({"ERROR"}, "Scene georeferencing is broken. Fix Scene / CRS first.")
        return {"CANCELLED"}

    filepath = settings.filepath
    layer_name = settings.layer_name.strip()

    # ── Layer selection ───────────────────────────────────────────────────

    try:
        layers = _reader.list_layers(filepath)
    except Exception as exc:
        operator.report({"ERROR"}, f"Cannot read GPKG file: {exc}")
        return {"CANCELLED"}

    if not layers:
        operator.report({"ERROR"}, "No feature layers found in this GeoPackage.")
        return {"CANCELLED"}

    layer_names = [l.name for l in layers]
    if layer_name and layer_name not in layer_names:
        operator.report(
            {"ERROR"},
            f"Layer {layer_name!r} not found. Available: {', '.join(layer_names)}"
        )
        return {"CANCELLED"}
    if not layer_name:
        layer_name = layer_names[0]
        if len(layers) > 1:
            operator.report(
                {"INFO"},
                f"Multiple layers found; importing first: {layer_name!r}. "
                f"Use the 'Layer' field to pick a different one."
            )

    # ── Read layer data ───────────────────────────────────────────────────

    try:
        layer_data = _reader.read_layer(filepath, layer_name)
    except Exception as exc:
        operator.report({"ERROR"}, f"Failed to read layer {layer_name!r}: {exc}")
        return {"CANCELLED"}

    feats_raw = layer_data.features
    if not feats_raw:
        operator.report({"INFO"}, f"Layer {layer_name!r} is empty.")
        return {"FINISHED"}

    # ── Convert to GeoJSON-style feature dicts (expected by import_utils) ─

    feats = [
        {
            "type": "Feature",
            "id": f["fid"],
            "geometry": f["geometry"],
            "properties": f["properties"],
            "_gpkg_fid": f["fid"],
        }
        for f in feats_raw
    ]

    # ── CRS resolution ────────────────────────────────────────────────────

    file_srid_str = _reader.read_crs(filepath, layer_name)
    if file_srid_str:
        srid = file_srid_str
    elif layer_data.srid:
        srid = f"EPSG:{layer_data.srid}"
    else:
        srid = "EPSG:4326"
        operator.report({"INFO"}, "No CRS found in GPKG; assuming EPSG:4326.")

    scene_srid = gs.srid or srid
    need_reproject = scene_srid != srid

    if need_reproject:
        try:
            import_utils.reproject_features(feats, srid, scene_srid)
        except Exception as exc:
            operator.report(
                {"ERROR"},
                f"Failed to reproject from {srid} to {scene_srid}: {exc}"
            )
            return {"CANCELLED"}
        srid = scene_srid

    if SRS(srid).isGeo and feats:
        srid, ok = import_utils.auto_reproject_geographic(
            feats, srid, operator, _compute_gpkg_centroid
        )
        if not ok:
            return {"CANCELLED"}

    if not gs.has_crs or gs.srid != srid:
        try:
            gs.srid = srid
        except Exception as exc:
            operator.report({"ERROR"}, f"Invalid scene CRS {srid}: {exc}")
            return {"CANCELLED"}

    if feats and settings.offset_origin:
        import_utils.apply_scene_offset(
            context, feats, srid, operator,
            _compute_gpkg_centroid, _apply_gpkg_offset,
        )

    # ── Build collection ──────────────────────────────────────────────────

    file_stem = Path(filepath).stem
    col_name = f"{file_stem}:{layer_name}"
    root_col = import_utils.get_or_create_collection(context, col_name)

    # ── Import features ───────────────────────────────────────────────────

    imported_fids = []

    for feat in feats:
        fid = feat["_gpkg_fid"]
        geom = feat.get("geometry")
        if geom is None:
            continue

        gtype = (geom.get("type") or "")

        if gtype in _UNSUPPORTED_IMPORT_TYPES:
            operator.report(
                {"WARNING"},
                f"Skipping fid={fid}: GeometryCollection is not supported "
                f"for mesh import in this release."
            )
            continue

        mesh = import_utils.build_mesh_for_feature(
            feat, settings.import_holes, _geometry_mod
        )
        if mesh is None:
            continue

        name = f"{layer_name}_{fid}"
        obj = bpy.data.objects.new(name, mesh)

        # ── Provenance ────────────────────────────────────────────────
        _prov.stamp(obj, source=filepath, layer=layer_name, fmt="gpkg",
                    fid=fid, geom_type=gtype, scene_srid=srid)

        # ── Feature attributes ────────────────────────────────────────
        props = feat.get("properties") or {}
        _attributes_mod.write_feature_properties(
            obj, mesh, props,
            store_custom_props=settings.store_custom_props,
            write_attributes=True,
            auto_normal=False,
            normal_prop="",
        )

        import_utils.link_to_collection(context, obj, root_col)
        imported_fids.append(fid)

    # Store provenance + tracked fids + column schema on the collection
    _prov.stamp_collection(root_col, source=filepath, layer=layer_name,
                           fmt="gpkg", fids=imported_fids)

    # Read and store the column schema so "Add Feature" can pre-populate attrs
    try:
        col_schema = _reader.read_column_schema(filepath, layer_name)
        layer_geom_type = next(
            (l.geom_type for l in layers if l.name == layer_name), "")
        _prov.set_schema(root_col, col_schema, geom_type=layer_geom_type)
    except Exception:
        pass  # non-fatal — schema is nice to have, not required

    operator.report(
        {"INFO"},
        f"Imported {len(imported_fids)} features from {layer_name!r}."
    )
    return {"FINISHED"}


# ---------------------------------------------------------------------------
# Geometry centroid / offset helpers (mirrors parser.compute_bbox_centroid /
# parser.apply_offset for GeoJSON — adapted for the GPKG feature format)
# ---------------------------------------------------------------------------

def _compute_gpkg_centroid(feats, operator=None):
    """Return (cx, cy, 0.0) for the bounding box of all feature geometries."""
    xs, ys = [], []
    for feat in feats:
        geom = feat.get("geometry")
        if geom is None:
            continue
        for pt in _flatten_coords(geom):
            xs.append(pt[0])
            ys.append(pt[1])
    if not xs:
        return 0.0, 0.0, 0.0
    return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, 0.0


def _apply_gpkg_offset(coordinates, offset):
    """Subtract offset from a single geometry's coordinate array, in-place.

    Matches the ``apply_offset_fn(coordinates, offset)`` contract expected by
    ``core.import_utils.apply_scene_offset``.
    """
    ox, oy = offset[0], offset[1]
    import_utils.reproject_node(coordinates, _OffsetRprj(ox, oy))


class _OffsetRprj:
    """Minimal duck-type for reproject_node that subtracts a fixed offset."""
    def __init__(self, cx, cy):
        self._cx = cx
        self._cy = cy
    def pt(self, x, y):
        return x - self._cx, y - self._cy


def _flatten_coords(geom):
    """Yield leaf [x, y, ...] positions from a GeoJSON geometry dict."""
    t = geom.get("type")
    c = geom.get("coordinates")
    if t == "Point":
        yield c
    elif t in ("LineString", "MultiPoint"):
        yield from c
    elif t in ("Polygon", "MultiLineString"):
        for ring in c:
            yield from ring
    elif t == "MultiPolygon":
        for poly in c:
            for ring in poly:
                yield from ring
    elif t == "GeometryCollection":
        for g in geom.get("geometries", []):
            yield from _flatten_coords(g)


# ---------------------------------------------------------------------------
# Geometry reconstruction helpers (Blender mesh → GeoJSON dict, for write-back)
# ---------------------------------------------------------------------------

def _mesh_to_geojson(obj) -> dict | None:
    """Reconstruct a GeoJSON geometry dict from a Blender mesh object.

    The geometry type is read from the ``geo_geom_type`` custom property.
    Returns ``None`` and emits a warning when the type cannot be reconstructed.
    """
    gtype = obj.get(_prov.GEO_GEOM_TYPE, "")
    mesh = obj.data
    verts = [list(v.co) for v in mesh.vertices]  # [[x,y,z], ...]

    if not gtype:
        gtype = _infer_geom_type_from_mesh(mesh)
        if gtype:
            obj[_prov.GEO_GEOM_TYPE] = gtype

    if gtype == "Point":
        if not verts:
            return None
        pt = verts[0]
        return {"type": "Point", "coordinates": [pt[0], pt[1], pt[2]]}

    elif gtype == "LineString":
        ordered = _ordered_linestring_verts(mesh)
        return {"type": "LineString", "coordinates": [[v[0], v[1]] for v in ordered]}

    elif gtype == "Polygon":
        rings = _polygon_rings(mesh)
        if not rings:
            return None
        return {"type": "Polygon", "coordinates": rings}

    else:
        return None


def _infer_geom_type_from_mesh(mesh) -> str:
    """Infer a GeoJSON geometry type from mesh topology."""
    if len(mesh.polygons) > 0:
        return "Polygon"
    if len(mesh.edges) > 0:
        return "LineString"
    if len(mesh.vertices) > 0:
        return "Point"
    return ""


def _ordered_linestring_verts(mesh):
    """Return mesh vertices in edge-connectivity order for a LineString."""
    if not mesh.edges:
        return [list(v.co) for v in mesh.vertices]
    # Build adjacency
    adj = {}
    for e in mesh.edges:
        adj.setdefault(e.vertices[0], []).append(e.vertices[1])
        adj.setdefault(e.vertices[1], []).append(e.vertices[0])
    # Find an endpoint (degree 1) or just start at vertex 0
    start = next((i for i, nbrs in adj.items() if len(nbrs) == 1), 0)
    visited = []
    cur, prev = start, -1
    while True:
        visited.append(cur)
        nxt = next((n for n in adj.get(cur, []) if n != prev), None)
        if nxt is None:
            break
        prev, cur = cur, nxt
    return [list(mesh.vertices[i].co) for i in visited]


def _polygon_rings(mesh):
    """Return [[exterior ring], ...interior rings] from a polygon mesh.

    Each disconnected face island becomes one ring.  The exterior ring is
    the island with the largest absolute area.

    Rings are extracted by finding boundary edges (edges that belong to
    only one face within the island), then walking them in order.
    """
    if not mesh.polygons:
        return []

    # ── 1.  Union-Find to group faces into connected islands ──────────
    n_verts = len(mesh.vertices)
    uf = list(range(n_verts))

    def find(x):
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def union(a, b):
        uf[find(a)] = find(b)

    for face in mesh.polygons:
        v0 = face.vertices[0]
        for v in face.vertices[1:]:
            union(v0, v)

    # Map root → set of vertex indices in that island
    island_verts: dict[int, set[int]] = {}
    for face in mesh.polygons:
        root = find(face.vertices[0])
        s = island_verts.setdefault(root, set())
        for vi in face.vertices:
            s.add(vi)

    # ── 2.  For each island, find the boundary edges ──────────────────
    # A boundary edge is one that borders only a single face.
    # Build a global edge → face-count map, but only for faces in the mesh.
    edge_face_count: dict[tuple[int, int], int] = {}
    for face in mesh.polygons:
        fv = list(face.vertices)
        for i in range(len(fv)):
            e = tuple(sorted((fv[i], fv[(i + 1) % len(fv)])))
            edge_face_count[e] = edge_face_count.get(e, 0) + 1

    # Also include mesh.edges that are *not* part of any face (bare edges).
    for e in mesh.edges:
        ek = tuple(sorted((e.vertices[0], e.vertices[1])))
        if ek not in edge_face_count:
            edge_face_count[ek] = 0  # bare edge — treat as boundary

    rings = []
    for root, vset in island_verts.items():
        # Boundary edges for this island: edge count == 1 (or 0 for bare)
        boundary_adj: dict[int, list[int]] = {}
        for (a, b), cnt in edge_face_count.items():
            if cnt <= 1 and a in vset:
                boundary_adj.setdefault(a, []).append(b)
                boundary_adj.setdefault(b, []).append(a)

        if not boundary_adj:
            # Fully closed solid (no boundary) — fall back to face perimeter
            # This shouldn't happen for GIS polygons, but be safe.
            continue

        # Walk boundary edges starting from any vertex
        used_verts: set[int] = set()
        start = next(iter(boundary_adj))
        ring_vi: list[int] = []
        cur, prev = start, -1
        while True:
            ring_vi.append(cur)
            used_verts.add(cur)
            nxt = None
            for n in boundary_adj.get(cur, []):
                if n != prev and n not in used_verts:
                    nxt = n
                    break
            if nxt is None:
                break
            prev, cur = cur, nxt

        ring = [[mesh.vertices[vi].co.x, mesh.vertices[vi].co.y] for vi in ring_vi]
        # Close the ring — must be a COPY, not a reference to ring[0],
        # because reproject_node iterates in-place and would transform
        # the shared object twice.
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0][:])
        rings.append((abs(_ring_area(ring)), ring))

    # Sort largest area first (exterior ring)
    rings.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r in rings]


def _ring_area(ring):
    """Shoelace formula for signed polygon area."""
    n = len(ring)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += ring[i][0] * ring[j][1]
        area -= ring[j][0] * ring[i][1]
    return area / 2.0


# ---------------------------------------------------------------------------
# Import operator
# ---------------------------------------------------------------------------

class GEOTOOLS_OT_import_gpkg(bpy.types.Operator):
    """Import a GeoPackage (.gpkg) vector layer as Blender mesh objects."""

    bl_idname = "geotools.import_gpkg"
    bl_label  = "Import GeoPackage"
    bl_options = {"REGISTER", "UNDO"}

    filepath:    bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.gpkg", options={"HIDDEN"})
    filter_folder: bpy.props.BoolProperty(default=True, options={"HIDDEN"})

    layer_name:        bpy.props.StringProperty(name="Layer", default="")
    mode:              bpy.props.EnumProperty(
        name="Import Mode",
        items=[
            ("PER_FEATURE", "One object per feature", "Required for reload / write-back"),
            ("BY_TYPE",     "One object per type",    ""),
            ("JOINED",      "Single joined mesh",     ""),
        ],
        default="PER_FEATURE",
    )
    offset_origin:     bpy.props.BoolProperty(name="Offset to world origin", default=True)
    store_custom_props: bpy.props.BoolProperty(name="Duplicate as custom properties", default=False)
    import_holes:      bpy.props.BoolProperty(name="Import holes", default=True)

    def invoke(self, context, event):
        # If file + layer are already set (e.g. from the sidebar), import directly
        if self.filepath and self.layer_name:
            return self.execute(context)
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "layer_name")
        layout.prop(self, "mode")
        layout.separator()
        col = layout.column(align=True)
        col.prop(self, "offset_origin")
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Feature Data:")
        col.prop(self, "store_custom_props")
        layout.separator()
        col = layout.column(align=True)
        col.label(text="Geometry:")
        col.prop(self, "import_holes")

    def execute(self, context):
        if not self.filepath:
            self.report({"ERROR"}, "No filepath provided.")
            return {"CANCELLED"}
        return _run_import(self, context, self)


# ---------------------------------------------------------------------------
# Reload operator
# ---------------------------------------------------------------------------

class GEOTOOLS_OT_reload_gpkg(bpy.types.Operator):
    """Re-read the source GPKG layer and update geometry + attributes in-place.

    Operates on all objects in the active collection that have a gpkg_source
    custom property.  If no collection is active, falls back to selected objects.
    """

    bl_idname = "geotools.reload_gpkg"
    bl_label  = "Reload from GeoPackage"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _gpkg_context(context) is not None

    def execute(self, context):
        ctx = _gpkg_context(context)
        if ctx is None:
            self.report({"ERROR"}, "No GPKG-linked objects found in selection or active collection.")
            return {"CANCELLED"}

        filepath, layer_name, objects = ctx

        # Re-read layer
        try:
            layer_data = _reader.read_layer(filepath, layer_name)
        except Exception as exc:
            self.report({"ERROR"}, f"Failed to read layer: {exc}")
            return {"CANCELLED"}

        # Build lookup by fid
        by_fid = {f["fid"]: f for f in layer_data.features}
        existing_fids = {obj[_prov.GEO_FID] for obj in objects if _prov.GEO_FID in obj}

        srid_str = _reader.read_crs(filepath, layer_name) or f"EPSG:{layer_data.srid or 4326}"
        gs = GeoScene(context.scene)
        scene_srid = gs.srid or srid_str

        updated = new_count = orphan_count = 0

        # Update existing objects
        for obj in objects:
            fid = obj.get(_prov.GEO_FID)
            if fid is None:
                continue
            feat_row = by_fid.get(fid)
            if feat_row is None:
                obj["geo_orphan"] = True
                orphan_count += 1
                continue

            geom = feat_row["geometry"]
            if geom is None:
                continue

            feat = {
                "type": "Feature",
                "id": fid,
                "geometry": geom,
                "properties": feat_row["properties"],
                "_gpkg_fid": fid,
            }

            # Reproject if needed
            if srid_str != scene_srid:
                import copy as _copy
                feat = _copy.deepcopy(feat)
                import_utils.reproject_features([feat], srid_str, scene_srid)

            # Apply scene offset
            if gs.has_crs:
                offset = (gs.origin_x, gs.origin_y, 0.0)
                geom = feat.get("geometry")
                if geom and geom.get("coordinates"):
                    _apply_gpkg_offset(geom["coordinates"], offset)

            # Rebuild mesh in-place
            new_mesh = import_utils.build_mesh_for_feature(
                feat, True, _geometry_mod
            )
            if new_mesh is None:
                continue

            old_mesh = obj.data
            obj.data = new_mesh
            if old_mesh and old_mesh.users == 0:
                bpy.data.meshes.remove(old_mesh)

            # Update feature attributes (mesh attributes + non-mesh custom props)
            props = feat_row["properties"] or {}
            mesh_keys = _attributes_mod.write_mesh_attributes(new_mesh, props)
            _attributes_mod.write_non_mesh_properties(obj, props, skip=mesh_keys)

            updated += 1

        # Add new features that didn't exist before
        col = _find_gpkg_collection(context, filepath, layer_name)
        if col is not None:
            for fid, feat_row in by_fid.items():
                if fid in existing_fids:
                    continue
                geom = feat_row.get("geometry")
                if geom is None:
                    continue
                gtype = geom.get("type", "")
                if gtype in _UNSUPPORTED_IMPORT_TYPES:
                    continue
                feat = {
                    "type": "Feature",
                    "id": fid,
                    "geometry": geom,
                    "properties": feat_row["properties"],
                    "_gpkg_fid": fid,
                }
                if srid_str != scene_srid:
                    import copy as _copy
                    feat = _copy.deepcopy(feat)
                    import_utils.reproject_features([feat], srid_str, scene_srid)
                if gs.has_crs:
                    offset = (gs.origin_x, gs.origin_y, 0.0)
                    fgeom = feat.get("geometry")
                    if fgeom and fgeom.get("coordinates"):
                        _apply_gpkg_offset(fgeom["coordinates"], offset)
                new_mesh = import_utils.build_mesh_for_feature(feat, True, _geometry_mod)
                if new_mesh is None:
                    continue
                obj = bpy.data.objects.new(f"{layer_name}_{fid}", new_mesh)
                _prov.stamp(obj, source=filepath, layer=layer_name, fmt="gpkg",
                            fid=fid, geom_type=gtype, scene_srid=scene_srid)
                import_utils.link_to_collection(context, obj, col)
                new_count += 1

        self.report(
            {"INFO"},
            f"Reload: {updated} updated, {new_count} added, {orphan_count} orphaned."
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Write-back operator
# ---------------------------------------------------------------------------

class GEOTOOLS_OT_write_gpkg(bpy.types.Operator):
    """Write Blender object changes back to the source .gpkg file.

    Performs attribute write-back, geometry write-back, new-feature insertion,
    and deletion of features that are no longer in the collection.

    Operates on all objects in the active collection that have a gpkg_source
    custom property (or the selected objects if no GPKG collection is active).
    """

    bl_idname = "geotools.write_gpkg"
    bl_label  = "Write to GeoPackage"
    bl_options = {"REGISTER", "UNDO"}

    confirm_deletions: bpy.props.BoolProperty(
        name="Confirm deletions",
        description="Delete features from the GPKG whose objects are missing from the collection",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return _gpkg_context(context) is not None

    def invoke(self, context, event):
        ctx = _gpkg_context(context)
        if ctx is None:
            return {"CANCELLED"}
        filepath, layer_name, objects = ctx

        # Check whether any deletions are pending before showing confirm dialog
        tracked = _tracked_fids(context, filepath, layer_name)
        present = {obj[_prov.GEO_FID] for obj in objects if _prov.GEO_FID in obj}
        pending = tracked - present
        if pending:
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        ctx = _gpkg_context(context)
        if ctx is None:
            return
        filepath, layer_name, objects = ctx
        tracked = _tracked_fids(context, filepath, layer_name)
        present = {obj[_prov.GEO_FID] for obj in objects if _prov.GEO_FID in obj}
        pending = tracked - present
        col = self.layout.column()
        col.label(text=f"{len(pending)} feature(s) will be DELETED from the GPKG:", icon="ERROR")
        col.label(text=f"Layer: {layer_name!r}  File: {Path(filepath).name}")
        col.prop(self, "confirm_deletions")

    def execute(self, context):
        ctx = _gpkg_context(context)
        if ctx is None:
            self.report({"ERROR"}, "No GPKG-linked objects found.")
            return {"CANCELLED"}

        filepath, layer_name, objects = ctx
        gs = GeoScene(context.scene)
        scene_srid = gs.srid

        # Read layer SRID for write-back reprojection
        file_srid = _reader.read_crs(filepath, layer_name) or scene_srid
        need_rereproject = file_srid != scene_srid

        written_attrs = written_geom = inserted = deleted = 0

        for obj in objects:
            fid = obj.get(_prov.GEO_FID)
            if fid is None:
                # New feature — insert
                geom_dict = _mesh_to_geojson(obj)
                if geom_dict is not None:
                    geom_dict = _reverse_offset(geom_dict, gs)
                    if need_rereproject:
                        geom_dict = _rereproject_geom(geom_dict, scene_srid, file_srid)
                props = _collect_attribute_props(obj)
                try:
                    new_fid = _writer.insert_feature(
                        filepath, layer_name, geom_dict, props,
                        srid=layer_data_srid_int(filepath, layer_name),
                    )
                    _prov.stamp(obj, source=filepath, layer=layer_name,
                                fmt="gpkg", fid=new_fid)
                    inserted += 1
                except Exception as exc:
                    self.report({"WARNING"}, f"Insert failed for {obj.name}: {exc}")
                continue

            # ── Geometry write-back ────────────────────────────────────────
            geom_dict = _mesh_to_geojson(obj)
            if geom_dict is not None:
                geom_dict = _reverse_offset(geom_dict, gs)
                if need_rereproject:
                    geom_dict = _rereproject_geom(geom_dict, scene_srid, file_srid)
                try:
                    _writer.update_feature_geometry(
                        filepath, layer_name, fid, geom_dict,
                        srid=layer_data_srid_int(filepath, layer_name),
                    )
                    written_geom += 1
                except _writer.GPKGWriteError as exc:
                    self.report({"WARNING"}, f"Geometry update failed fid={fid}: {exc}")

            # ── Attribute write-back ───────────────────────────────────────
            props = _collect_attribute_props(obj)
            if props:
                try:
                    _writer.update_feature_attributes(filepath, layer_name, fid, props)
                    written_attrs += 1
                except _writer.GPKGWriteError as exc:
                    self.report({"WARNING"}, f"Attribute update failed fid={fid}: {exc}")

        # ── Deletion ─────────────────────────────────────────────────────────
        if self.confirm_deletions:
            tracked = _tracked_fids(context, filepath, layer_name)
            present = {obj[_prov.GEO_FID] for obj in objects if _prov.GEO_FID in obj}
            for fid_del in (tracked - present):
                try:
                    _writer.delete_feature(filepath, layer_name, fid_del)
                    deleted += 1
                except _writer.GPKGWriteError as exc:
                    self.report({"WARNING"}, f"Delete failed fid={fid_del}: {exc}")

        # Update tracked fids after write
        col = _find_gpkg_collection(context, filepath, layer_name)
        if col is not None:
            present_after = {obj[_prov.GEO_FID] for obj in objects if _prov.GEO_FID in obj}
            _prov.set_tracked_fids(col, present_after)

        self.report(
            {"INFO"},
            f"Write-back: {written_geom} geometry, {written_attrs} attributes, "
            f"{inserted} inserted, {deleted} deleted."
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Write-back helpers
# ---------------------------------------------------------------------------

def _collect_attribute_props(obj) -> dict:
    """Return a dict of {col_name: value} for non-provenance custom properties."""
    return _prov.collect_attribute_props(obj)


def _reverse_offset(geom_dict: dict, gs) -> dict:
    """Add the scene origin offset back to all coordinates."""
    if not gs.has_crs:
        return geom_dict
    import copy as _copy
    geom_dict = _copy.deepcopy(geom_dict)

    class _AddOffset:
        def __init__(self, cx, cy):
            self._cx, self._cy = cx, cy
        def pt(self, x, y):
            return x + self._cx, y + self._cy

    import_utils.reproject_node(
        geom_dict.get("coordinates") or geom_dict.get("geometries", []),
        _AddOffset(gs.origin_x, gs.origin_y),
    )
    return geom_dict


def _rereproject_geom(geom_dict: dict, src_srid: str, dst_srid: str) -> dict:
    """Reproject all coordinates in a GeoJSON geometry dict from src to dst."""
    import copy as _copy
    geom_dict = _copy.deepcopy(geom_dict)
    feat = {"type": "Feature", "geometry": geom_dict, "properties": {}}
    import_utils.reproject_features([feat], src_srid, dst_srid)
    return feat["geometry"]


def layer_data_srid_int(filepath: str, layer_name: str) -> int:
    """Return the integer SRID for a layer, or 0 if unavailable."""
    try:
        layers = _reader.list_layers(filepath)
        for l in layers:
            if l.name == layer_name:
                return l.srid
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _gpkg_context(context):
    """Return (filepath, layer_name, [objects]) for the current GPKG context.

    Checks the Layers panel selector first, then the active collection,
    then falls back to selected objects.
    Returns None if no GPKG-linked objects are found.
    """
    # UI layer selector
    ui = getattr(context.scene, "geotools_ui", None)
    if ui is not None:
        name = ui.active_layer
        if name and name != "__none__":
            col = bpy.data.collections.get(name)
            if col and col.get(_prov.GEO_FORMAT) == "gpkg":
                objs = [
                    o for o in col.objects
                    if _prov.GEO_SOURCE in o
                ]
                if objs:
                    return col[_prov.GEO_SOURCE], col[_prov.GEO_LAYER], objs

    # Active collection
    col = getattr(context, "collection", None)
    if col and col.get(_prov.GEO_FORMAT) == "gpkg":
        objs = [
            o for o in col.objects
            if _prov.GEO_SOURCE in o and o.get(_prov.GEO_SOURCE) == col.get(_prov.GEO_SOURCE)
        ]
        if objs:
            return col[_prov.GEO_SOURCE], col[_prov.GEO_LAYER], objs

    # Selected objects
    sel = [o for o in (context.selected_objects or []) if o.get(_prov.GEO_FORMAT) == "gpkg"]
    if not sel:
        return None

    filepath = sel[0][_prov.GEO_SOURCE]
    layer_name = sel[0][_prov.GEO_LAYER]
    objs = [o for o in sel if o.get(_prov.GEO_SOURCE) == filepath and o.get(_prov.GEO_LAYER) == layer_name]
    return filepath, layer_name, objs


def _find_gpkg_collection(context, filepath: str, layer_name: str):
    """Return the Blender collection that stores the layer, or None."""
    for col in bpy.data.collections:
        if col.get(_prov.GEO_SOURCE) == filepath and col.get(_prov.GEO_LAYER) == layer_name:
            return col
    return None


def _tracked_fids(context, filepath: str, layer_name: str) -> set:
    """Return the set of fids that were present when the layer was last imported/reloaded."""
    col = _find_gpkg_collection(context, filepath, layer_name)
    if col is None:
        return set()
    return _prov.get_tracked_fids(col)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    GEOTOOLS_OT_import_gpkg,
    GEOTOOLS_OT_reload_gpkg,
    GEOTOOLS_OT_write_gpkg,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
