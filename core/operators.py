"""core/operators.py — Generic geo-feature operators (Add / Remove).

These work on any geo-linked collection regardless of the source format
(GPKG, GeoJSON, …).  They read the layer schema stored on the collection
by the importer and create/remove objects accordingly.
"""

import bpy

from . import provenance as _prov

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _active_geo_collection(context):
    """Return the geo-linked collection for the current context.

    Checks the Layers panel selector first, then falls back to the
    active outliner collection.
    """
    # UI layer selector
    ui = getattr(context.scene, "geotools_ui", None)
    if ui is not None:
        name = ui.active_layer
        if name and name != "__none__":
            col = bpy.data.collections.get(name)
            if col and _prov.GEO_SOURCE in col:
                return col

    # Fallback: active outliner collection
    col = getattr(context, "collection", None)
    if col and _prov.GEO_SOURCE in col:
        return col
    return None


def _default_geom_type(col) -> str:
    """Return the geometry type for new features in *col*.

    Reads from the stored schema; falls back to inspecting existing objects.
    """
    schema = _prov.get_schema(col)
    if schema and schema.get("geom_type"):
        raw = schema["geom_type"].upper()
        # Normalise GPKG type names → base GeoJSON type names.
        # Multi* variants map to the single form because new empty
        # features are always created as single geometries.
        _MAP = {
            "POINT": "Point",
            "MULTIPOINT": "Point",
            "LINESTRING": "LineString",
            "MULTILINESTRING": "LineString",
            "POLYGON": "Polygon",
            "MULTIPOLYGON": "Polygon",
            "GEOMETRY": "",  # mixed — let user decide
        }
        return _MAP.get(raw, raw.title())

    # Fall back: look at existing objects
    for obj in col.objects:
        gt = obj.get(_prov.GEO_GEOM_TYPE)
        if gt:
            return gt
    return ""


def _default_attributes(col) -> dict:
    """Return ``{col_name: default_value}`` from the stored schema."""
    schema = _prov.get_schema(col)
    if not schema or not schema.get("columns"):
        # Fall back: grab keys from the first object that has provenance
        for obj in col.objects:
            if _prov.has_provenance(obj):
                return {
                    k: _default_for_value(v)
                    for k, v in _prov.collect_attribute_props(obj).items()
                }
        return {}

    _TYPE_DEFAULTS = {
        "TEXT": "",
        "INTEGER": 0,
        "REAL": 0.0,
        "FLOAT": 0.0,
        "DOUBLE": 0.0,
        "BOOLEAN": False,
        "BLOB": b"",
        "DATE": "",
        "DATETIME": "",
    }
    return {
        name: _TYPE_DEFAULTS.get(sql_type.upper(), "")
        for name, sql_type in schema["columns"].items()
    }


def _default_for_value(v):
    """Return a sensible default of the same type as *v*."""
    if isinstance(v, int):
        return 0
    if isinstance(v, float):
        return 0.0
    if isinstance(v, bool):
        return False
    return ""


# ---------------------------------------------------------------------------
# Add Feature operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_add_feature(bpy.types.Operator):
    """Add a new empty feature to the active geo-linked collection.

    Creates a minimal mesh of the correct geometry type, stamps it with
    provenance (no fid → pending insertion), and pre-populates attribute
    custom properties with default/null values from the stored schema.
    """

    bl_idname = "geotools.add_feature"
    bl_label = "Add Feature"
    bl_options = {"REGISTER", "UNDO"}

    geom_type: bpy.props.EnumProperty(
        name="Geometry Type",
        items=[
            ("Point", "Point", "Single point"),
            ("LineString", "LineString", "Two-vertex line segment"),
            ("Polygon", "Polygon", "Triangular placeholder polygon"),
        ],
        default="Point",
    )

    @classmethod
    def poll(cls, context):
        return _active_geo_collection(context) is not None

    def invoke(self, context, event):
        col = _active_geo_collection(context)
        gt = _default_geom_type(col)
        if gt in ("Point", "LineString", "Polygon"):
            self.geom_type = gt
            return self.execute(context)
        # Unknown or mixed layer type — let the user pick
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        col = _active_geo_collection(context)
        if col is None:
            self.report({"ERROR"}, "No geo-linked collection active.")
            return {"CANCELLED"}

        source = col.get(_prov.GEO_SOURCE, "")
        layer = col.get(_prov.GEO_LAYER, "")
        fmt = col.get(_prov.GEO_FORMAT, "")

        # Create an empty mesh of the right type
        import bmesh

        mesh = bpy.data.meshes.new("NewFeature")
        bm = bmesh.new()

        if self.geom_type == "Point":
            bm.verts.new((0, 0, 0))
        elif self.geom_type == "LineString":
            v1 = bm.verts.new((0, 0, 0))
            v2 = bm.verts.new((1, 0, 0))
            bm.edges.new((v1, v2))
        elif self.geom_type == "Polygon":
            v1 = bm.verts.new((0, 0, 0))
            v2 = bm.verts.new((1, 0, 0))
            v3 = bm.verts.new((0.5, 1, 0))
            bm.faces.new((v1, v2, v3))

        bm.to_mesh(mesh)
        bm.free()

        # Name
        idx = len(col.objects) + 1
        name = f"{layer}_new_{idx}" if layer else f"feature_new_{idx}"
        obj = bpy.data.objects.new(name, mesh)

        # Provenance — no fid (signals "new / pending insert")
        _prov.stamp(obj, source=source, layer=layer, fmt=fmt, geom_type=self.geom_type)

        # Pre-populate feature attributes as mesh attributes + custom props
        defaults = _default_attributes(col)
        from ..addons.data.geojson.attributes import (
            write_mesh_attributes,
            write_non_mesh_properties,
        )

        mesh_keys = write_mesh_attributes(mesh, defaults)
        write_non_mesh_properties(obj, defaults, skip=mesh_keys)

        # Link to collection
        col.objects.link(obj)
        if obj.name in context.scene.collection.objects:
            context.scene.collection.objects.unlink(obj)

        # Select the new object
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        context.view_layer.objects.active = obj

        self.report({"INFO"}, f"Added new {self.geom_type} feature: {obj.name}")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Remove Feature operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_remove_feature(bpy.types.Operator):
    """Remove the selected geo-linked object(s) from the collection.

    The object is deleted from Blender.  On next write-back, the missing fid
    will trigger a DELETE in the source file (with confirmation).

    Objects without a fid (not yet saved) are simply discarded.
    """

    bl_idname = "geotools.remove_feature"
    bl_label = "Remove Feature"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(_prov.has_provenance(o) for o in (context.selected_objects or []))

    def execute(self, context):
        removed = 0
        for obj in list(context.selected_objects or []):
            if not _prov.has_provenance(obj):
                continue
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
            removed += 1

        self.report({"INFO"}, f"Removed {removed} feature(s).")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    GEOTOOLS_OT_add_feature,
    GEOTOOLS_OT_remove_feature,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
