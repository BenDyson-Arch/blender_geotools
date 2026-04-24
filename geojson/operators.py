"""
operators.py — ImportGeoJSON operator.

BlenderGIS interop replaced by GeoScene from blender_geotools.geoscene.
No UI registration here — the unified UI (Phase 3) provides entry points.
"""

import json
from pathlib import Path

import bpy

from . import parser, geometry, attributes
from ..geoscene import GeoScene
from ..core.proj.srs import SRS
from ..core.settings import settings as core_settings
from ..core import import_utils
from ..core import provenance as _prov


# ---------------------------------------------------------------------------
# GeoJSON-specific CRS helper
# ---------------------------------------------------------------------------

def _resolve_crs(data):
    """Extract a CRS string from the GeoJSON data, or None if absent."""
    crs_dict = parser.get_crs(data)
    crs_str = parser.extract_crs_string(crs_dict)
    return crs_str or None


# ---------------------------------------------------------------------------
# Module-local aliases for shared helpers (keeps call sites readable)
# ---------------------------------------------------------------------------

_reproject_features      = import_utils.reproject_features
_get_or_create_collection = import_utils.get_or_create_collection
_link_to_collection      = import_utils.link_to_collection


def _build_mesh_for_feature(feat, settings):
    return import_utils.build_mesh_for_feature(feat, settings.import_holes, geometry)


def _geometry_type(feat):
    return import_utils.geometry_type(feat)


def _apply_scene_offset(context, feats, srid, operator):
    return import_utils.apply_scene_offset(
        context, feats, srid, operator,
        parser.compute_bbox_centroid,
        parser.apply_offset,
    )


# ---------------------------------------------------------------------------
# Object naming
# ---------------------------------------------------------------------------

def _object_name(layer, feat, index):
    feat_id = feat.get("id")
    props = feat.get("properties") or {}
    label = props.get("name") or props.get("label")
    suffix = str(feat_id) if feat_id is not None else (label or str(index))
    return f"{layer}_{suffix}"


# ---------------------------------------------------------------------------
# Main import pipeline
# ---------------------------------------------------------------------------

def _run_import(operator, context, settings):
    gs = GeoScene(context.scene)
    if gs.is_broken:
        operator.report({"ERROR"}, "Scene georeferencing is broken. Fix Scene / CRS first.")
        return {"CANCELLED"}

    filepath = Path(settings.filepath)
    layer = filepath.stem

    try:
        data = parser.load_geojson(settings.filepath)
    except (ValueError, FileNotFoundError) as exc:
        operator.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    file_crs = _resolve_crs(data)  # None when file has no CRS member

    try:
        feats = list(parser.iter_features(data))
    except ValueError as exc:
        operator.report({"ERROR"}, str(exc))
        return {"CANCELLED"}

    # Resolve the source CRS for this file.
    #
    # RFC 7946 says GeoJSON without a CRS member is WGS84 (EPSG:4326).
    # We use a coordinate-range heuristic to distinguish genuinely
    # geographic data from projected data that simply lacks a header.
    if file_crs is not None:
        srid = file_crs
    elif feats and not parser.is_geographic_coordinates(feats):
        # Coordinates are outside ±180/±90 — clearly projected, not degrees.
        if gs.has_valid_crs:
            srid = gs.srid
            operator.report(
                {"INFO"},
                f"No CRS in file but coordinates look projected — "
                f"assuming scene CRS ({srid})."
            )
        else:
            cx, cy, _cz = parser.compute_bbox_centroid(feats)
            operator.report(
                {"ERROR"},
                f"No CRS declared and no scene CRS set. "
                f"Coordinates ({cx:.1f}, {cy:.1f}) look projected, not "
                f"geographic (EPSG:4326). Set the scene CRS before importing."
            )
            return {"CANCELLED"}
    else:
        # Default: EPSG:4326 per RFC 7946
        srid = "EPSG:4326"
        if gs.has_valid_crs and gs.srid != "EPSG:4326":
            operator.report(
                {"INFO"},
                f"No CRS in file — defaulting to EPSG:4326 (RFC 7946). "
                f"Will reproject to scene CRS ({gs.srid})."
            )

    scene_srid = gs.srid or srid
    need_reproject = scene_srid != srid

    if need_reproject:
        try:
            _reproject_features(feats, srid, scene_srid)
        except Exception as exc:
            operator.report(
                {"ERROR"},
                f"Failed to reproject GeoJSON from {srid} into scene CRS {scene_srid}: {exc}"
            )
            return {"CANCELLED"}
        operator.report(
            {"INFO"},
            f"Reprojected GeoJSON {srid} -> {scene_srid} via {core_settings.last_reproj_engine or 'UNKNOWN'}"
        )
        srid = scene_srid

    # Geographic (degree) coordinates are unusable in Blender — a 10 km city
    # spans ~0.1 degrees, which is invisible.  Auto-detect a UTM zone from
    # the data centroid and reproject before anything touches the scene.
    if SRS(srid).isGeo and feats:
        srid, ok = import_utils.auto_reproject_geographic(
            feats, srid, operator, parser.compute_bbox_centroid
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
        _apply_scene_offset(context, feats, srid, operator)

    # Flip Y/Z if requested
    if settings.flip_yz:
        for feat in feats:
            geom = feat.get("geometry")
            if geom and geom.get("coordinates"):
                parser.flip_yz_coords(geom["coordinates"])

    root_col = _get_or_create_collection(context, layer)

    if settings.mode == "PER_FEATURE":
        _import_per_feature(context, layer, feats, settings, root_col,
                            operator, str(filepath), srid)
    elif settings.mode == "BY_TYPE":
        _import_by_type(context, layer, feats, settings, root_col, operator)
    elif settings.mode == "JOINED":
        _import_joined(context, layer, feats, settings, root_col, operator)

    # Stamp collection provenance so the layer appears in the Layers panel
    _prov.stamp_collection(root_col, source=str(filepath), layer=layer,
                           fmt="geojson")

    operator.report({"INFO"}, f"Imported {len(feats)} features from {filepath.name!r}.")
    return {"FINISHED"}


# ---------------------------------------------------------------------------
# Import mode implementations
# ---------------------------------------------------------------------------

def _import_per_feature(context, layer, feats, settings, root_col,
                        operator, filepath="", srid=""):
    for index, feat in enumerate(feats):
        gtype = _geometry_type(feat)
        feat_id = feat.get("id")

        if gtype == "GeometryCollection":
            sub_col = _get_or_create_collection(context, f"{layer}_{index}_gc", parent=root_col)
            geom = feat.get("geometry") or {}
            for sub_index, sub_geom in enumerate(geom.get("geometries", [])):
                sub_feat = {"geometry": sub_geom, "properties": feat.get("properties") or {}, "id": None}
                sub_mesh = _build_mesh_for_feature(sub_feat, settings)
                if sub_mesh is None:
                    continue
                name = f"{layer}_{index}_gc_{sub_index}"
                obj = bpy.data.objects.new(name, sub_mesh)
                _prov.stamp(obj, source=filepath, layer=layer, fmt="geojson",
                            fid=index, geom_type=sub_geom.get("type", "Unknown"),
                            scene_srid=srid)
                attributes.write_feature_properties(
                    obj, sub_mesh, feat.get("properties") or {},
                    store_custom_props=settings.store_custom_props,
                    write_attributes=True,
                    auto_normal=settings.auto_normal,
                    normal_prop=settings.normal_property,
                )
                _link_to_collection(context, obj, sub_col)
            continue

        mesh = _build_mesh_for_feature(feat, settings)
        if mesh is None:
            continue

        if settings.extrude_prop and gtype in ("Polygon", "MultiPolygon"):
            extrude_val = (feat.get("properties") or {}).get(settings.extrude_prop)
            if extrude_val is not None:
                geometry.extrude_mesh_by_value(mesh, extrude_val)

        name = _object_name(layer, feat, index)
        obj = bpy.data.objects.new(name, mesh)
        _prov.stamp(obj, source=filepath, layer=layer, fmt="geojson",
                    fid=feat_id if feat_id is not None else index,
                    geom_type=gtype, scene_srid=srid)

        attributes.write_feature_properties(
            obj, mesh, feat.get("properties") or {},
            store_custom_props=settings.store_custom_props,
            write_attributes=True,
            auto_normal=settings.auto_normal,
            normal_prop=settings.normal_property,
        )

        _link_to_collection(context, obj, root_col)


def _import_by_type(context, layer, feats, settings, root_col, operator):
    by_type = {}
    for index, feat in enumerate(feats):
        gtype = _geometry_type(feat)
        if gtype == "GeometryCollection":
            continue
        mesh = _build_mesh_for_feature(feat, settings)
        if mesh is None:
            continue
        if settings.extrude_prop and gtype in ("Polygon", "MultiPolygon"):
            extrude_val = (feat.get("properties") or {}).get(settings.extrude_prop)
            if extrude_val is not None:
                geometry.extrude_mesh_by_value(mesh, extrude_val)
        by_type.setdefault(gtype, []).append((index, feat, mesh))

    for gtype, entries in by_type.items():
        mesh_list = [m for _, _, m in entries]
        try:
            joined = geometry.join_meshes(mesh_list, name=f"{layer}_{gtype}", operator=operator)
        finally:
            for m in mesh_list:
                if m.users == 0:
                    bpy.data.meshes.remove(m)

        obj = bpy.data.objects.new(f"{layer}_{gtype}", joined)
        obj["GeometryType"] = gtype

        prop_table = {
            str(i): {"_feat_index": orig_index, **(feat.get("properties") or {})}
            for i, (orig_index, feat, _) in enumerate(entries)
        }
        text_name = f"{layer}_{gtype}_properties"
        if text_name in bpy.data.texts:
            bpy.data.texts.remove(bpy.data.texts[text_name])
        text = bpy.data.texts.new(text_name)
        text.use_fake_user = True
        text.write(json.dumps(prop_table, indent=2))

        type_col = _get_or_create_collection(context, f"{layer}_{gtype}", parent=root_col)
        _link_to_collection(context, obj, type_col)


def _import_joined(context, layer, feats, settings, root_col, operator):
    all_meshes = []
    for index, feat in enumerate(feats):
        gtype = _geometry_type(feat)
        if gtype == "GeometryCollection":
            continue
        mesh = _build_mesh_for_feature(feat, settings)
        if mesh is None:
            continue
        if settings.extrude_prop and gtype in ("Polygon", "MultiPolygon"):
            extrude_val = (feat.get("properties") or {}).get(settings.extrude_prop)
            if extrude_val is not None:
                geometry.extrude_mesh_by_value(mesh, extrude_val)
        all_meshes.append((index, feat, mesh))

    if not all_meshes:
        return

    mesh_list = [m for _, _, m in all_meshes]
    try:
        joined = geometry.join_meshes(mesh_list, name=layer, operator=operator)
    finally:
        for m in mesh_list:
            if m.users == 0:
                bpy.data.meshes.remove(m)

    obj = bpy.data.objects.new(layer, joined)
    obj["GeometryType"] = "JOINED"

    prop_table = {
        str(i): {"_feat_index": orig_index, **(feat.get("properties") or {})}
        for i, (orig_index, feat, _) in enumerate(all_meshes)
    }
    text_name = f"{layer}_properties"
    if text_name in bpy.data.texts:
        bpy.data.texts.remove(bpy.data.texts[text_name])
    text = bpy.data.texts.new(text_name)
    text.use_fake_user = True
    text.write(json.dumps(prop_table, indent=2))

    _link_to_collection(context, obj, root_col)


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class GEOTOOLS_OT_import_geojson(bpy.types.Operator):
    """Import a GeoJSON file as Blender mesh objects."""

    bl_idname = "geotools.import_geojson"
    bl_label = "Import GeoJSON"
    bl_options = {"REGISTER", "UNDO"}

    # File browser properties
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.geojson;*.json", options={"HIDDEN"})
    filter_folder: bpy.props.BoolProperty(default=True, options={"HIDDEN"})

    # Import settings
    mode: bpy.props.EnumProperty(
        name="Import Mode",
        items=[
            ("PER_FEATURE", "One object per feature", ""),
            ("BY_TYPE",     "One object per geometry type", ""),
            ("JOINED",      "Single joined mesh", ""),
        ],
        default="PER_FEATURE",
    )
    offset_origin: bpy.props.BoolProperty(name="Offset to world origin", default=True)
    flip_yz: bpy.props.BoolProperty(name="Flip Y/Z axes", default=False)
    store_custom_props: bpy.props.BoolProperty(name="Duplicate as custom properties", default=False)
    auto_normal: bpy.props.BoolProperty(name="Auto-detect normal property", default=True)
    normal_property: bpy.props.StringProperty(name="Normal property name", default="normal")
    import_holes: bpy.props.BoolProperty(name="Import holes", default=True)
    extrude_prop: bpy.props.StringProperty(name="Extrude by property", default="")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def draw(self, context):
        layout = self.layout

        layout.prop(self, "mode")

        layout.separator()

        col = layout.column(align=True)
        col.prop(self, "offset_origin")
        col.prop(self, "flip_yz")

        layout.separator()

        col = layout.column(align=True)
        col.label(text="Feature Data:")
        col.prop(self, "store_custom_props")

        layout.separator()

        col = layout.column(align=True)
        col.label(text="Geometry:")
        col.prop(self, "import_holes")
        row = col.row(align=True)
        row.prop(self, "extrude_prop")

    def execute(self, context):
        if not self.filepath:
            self.report({"ERROR"}, "No filepath provided")
            return {"CANCELLED"}
        return _run_import(self, context, self)


_classes = (GEOTOOLS_OT_import_geojson,)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
