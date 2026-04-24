"""
ui.py — Unified GeoTools N-panel (View3D > Sidebar > GeoTools tab).

Panels: Scene / CRS, Import, Layers, Attributes (+ Dependencies).
"""

import os

import bpy
from bpy.types import Panel, PropertyGroup

from .geoscene import georef_manager_layout
from .core.checkdeps import HAS_PYPROJ, HAS_GDAL, HAS_NUMPY
from .core import provenance as _prov


def _read_mesh_attributes(mesh):
    """Lazy wrapper to avoid importing geojson.attributes at module load."""
    from .geojson.attributes import read_mesh_attributes
    return read_mesh_attributes(mesh)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _prefs():
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return None


def _mod_enabled(flag: str) -> bool:
    p = _prefs()
    return getattr(p, flag, True) if p else True


# ── Dynamic enum: linked layers ─────────────────────────────────────────────

_LAYER_NONE = [("__none__", "— no linked layers —", "")]


def _linked_layer_items(self, context):
    """Build an enum of all collections with geo-provenance."""
    items = []
    for col in bpy.data.collections:
        if _prov.GEO_SOURCE not in col:
            continue
        fmt    = col.get(_prov.GEO_FORMAT, "?").upper()
        layer  = col.get(_prov.GEO_LAYER, "")
        source = os.path.basename(col.get(_prov.GEO_SOURCE, ""))
        label  = f"{col.name}"
        desc   = f"{fmt} · {layer} · {source}" if layer else f"{fmt} · {source}"
        items.append((col.name, label, desc))
    return items or _LAYER_NONE


# ── Scene-level UI settings ─────────────────────────────────────────────────

class GeoToolsUISettings(PropertyGroup):
    import_format: bpy.props.EnumProperty(
        name="Format",
        items=[
            ("GEOJSON",  "GeoJSON",    "Import a GeoJSON file"),
            ("GPKG",     "GeoPackage", "Import a GeoPackage layer"),
            ("TILES3D",  "3D Tiles",   "Import an OGC 3D Tileset"),
            ("DEM",      "DEM",        "Import a Digital Elevation Model"),
        ],
        default="GPKG",
    )

    active_layer: bpy.props.EnumProperty(
        name="Layer",
        description="Select a linked layer to manage",
        items=_linked_layer_items,
    )


# ── Scene / CRS panel ────────────────────────────────────────────────────────

class GEOTOOLS_PT_scene(Panel):
    bl_label       = "Scene / CRS"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "GeoTools"
    bl_order       = 0

    def draw(self, context):
        georef_manager_layout(self, context)


# ── Import panel ─────────────────────────────────────────────────────────────

class GEOTOOLS_PT_import(Panel):
    bl_label       = "Import"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "GeoTools"
    bl_order       = 10

    def draw(self, context):
        layout = self.layout
        ui = context.scene.geotools_ui

        layout.prop(ui, "import_format", text="")

        fmt = ui.import_format

        # ── GeoJSON ───────────────────────────────────────────────────
        if fmt == "GEOJSON" and _mod_enabled("mod_geojson"):
            layout.operator("geotools.import_geojson",
                            text="Import GeoJSON…", icon="IMPORT")

        # ── GeoPackage ────────────────────────────────────────────────
        elif fmt == "GPKG" and _mod_enabled("mod_gpkg"):
            s = (context.scene.gpkg_import
                 if hasattr(context.scene, "gpkg_import") else None)
            if s is not None:
                col = layout.column(align=True)
                col.prop(s, "filepath", text="File")
                col.prop(s, "layer_name", text="Layer")
                layout.separator(factor=0.5)
                layer_ready = (s.filepath
                               and s.layer_name not in ("", "__none__"))
                row = layout.row()
                row.enabled = bool(layer_ready)
                op = row.operator("geotools.import_gpkg",
                                  text="Import GPKG", icon="IMPORT")
                if layer_ready:
                    op.filepath           = s.filepath
                    op.layer_name         = s.layer_name
                    op.mode               = s.mode
                    op.offset_origin      = s.offset_origin
                    op.store_custom_props = s.store_custom_props
                    op.import_holes       = s.import_holes
            else:
                layout.operator("geotools.import_gpkg",
                                text="Import GPKG…", icon="IMPORT")

        # ── 3D Tiles ──────────────────────────────────────────────────
        elif fmt == "TILES3D" and _mod_enabled("mod_tiles3d"):
            props = context.scene.tiles3d
            layout.prop(props, "last_filepath", text="Tileset")

            if props.total_content_nodes > 0:
                info = layout.column(align=True)
                info.label(
                    text=f"Tree nodes: {props.total_content_nodes}",
                    icon="MESH_DATA",
                )
                info.label(
                    text=(f"\u03b5  {props.error_min:.4f}"
                          f" \u2013 {props.error_max:.4f}"),
                    icon="MOD_SMOOTH",
                )

                layout.separator(factor=0.5)
                col = layout.column(align=True)
                col.label(text="LOD Threshold (geometricError \u2264):")
                col.prop(props, "error_threshold", text="", slider=False)

                layout.separator(factor=0.5)
                col = layout.column(align=True)
                col.prop(props, "apply_transforms")
                col.prop(props, "join_mesh")

                layout.separator(factor=0.5)
                op = layout.operator(
                    "geotools.import_tileset",
                    text="Import Tiles", icon="IMPORT",
                )
                op.filepath                  = props.last_filepath
                op.geometric_error_threshold = props.error_threshold
                op.apply_transforms          = props.apply_transforms
                op.join_mesh                 = props.join_mesh
            else:
                layout.label(text="Select a tileset.json to begin.",
                             icon="INFO")

        # ── DEM ───────────────────────────────────────────────────────
        elif fmt == "DEM" and _mod_enabled("mod_dem"):
            layout.operator("geotools.import_dem",
                            text="Import DEM…", icon="IMPORT")

        # ── Module not enabled ────────────────────────────────────────
        else:
            layout.label(text="Module not enabled in preferences.",
                         icon="INFO")


# ── Layers panel ─────────────────────────────────────────────────────────────

def _get_active_layer_col(context):
    """Return the Blender collection for the active layer enum, or None."""
    ui = getattr(context.scene, "geotools_ui", None)
    if ui is None:
        return None
    name = ui.active_layer
    if not name or name == "__none__":
        return None
    return bpy.data.collections.get(name)


class GEOTOOLS_PT_layers(Panel):
    bl_label       = "Layers"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "GeoTools"
    bl_order       = 15

    def draw(self, context):
        layout = self.layout
        ui = context.scene.geotools_ui

        layout.prop(ui, "active_layer", text="", icon="OUTLINER_COLLECTION")

        col_obj = _get_active_layer_col(context)
        if col_obj is None:
            layout.label(text="Import data to create a layer.", icon="INFO")
            return

        fmt    = col_obj.get(_prov.GEO_FORMAT, "")
        source = col_obj.get(_prov.GEO_SOURCE, "")
        layer  = col_obj.get(_prov.GEO_LAYER, "")

        # ── Layer info ────────────────────────────────────────────────
        info = layout.column(align=True)
        info.label(text=f"Format: {fmt.upper()}", icon="FILE_BLANK")
        info.label(text=f"Source: {os.path.basename(source)}")
        if layer:
            info.label(text=f"Table: {layer}")
        info.label(text=f"Objects: {len(col_obj.objects)}")

        layout.separator()

        # ── GPKG operations (reload / write-back) ────────────────────
        if fmt == "gpkg":
            col = layout.column(align=True)
            col.operator("geotools.reload_gpkg",
                         text="Reload from File", icon="FILE_REFRESH")
            col.operator("geotools.write_gpkg",
                         text="Write to File",    icon="EXPORT")
            layout.separator()

        # ── Add / Remove Feature ──────────────────────────────────────
        row = layout.row(align=True)
        row.operator("geotools.add_feature",
                     text="Add Feature",    icon="ADD")
        row.operator("geotools.remove_feature",
                     text="Remove Feature", icon="REMOVE")


# ── Attribute skip list ──────────────────────────────────────────────────────

_BUILTIN_MESH_ATTRS = frozenset({
    "position", "shade_smooth", "sharp_face", "sharp_edge",
    "crease_edge", "crease_vert", "material_index",
})


# ── Edit Attribute operator ──────────────────────────────────────────────────

class GEOTOOLS_OT_edit_attribute(bpy.types.Operator):
    """Edit the value of a single mesh attribute on the active object."""
    bl_idname  = "geotools.edit_attribute"
    bl_label   = "Edit Attribute"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    attr_name: bpy.props.StringProperty(name="Attribute", options={"HIDDEN"})
    attr_type: bpy.props.StringProperty(name="Type", options={"HIDDEN"})

    val_float:  bpy.props.FloatProperty(name="Value")
    val_int:    bpy.props.IntProperty(name="Value")
    val_bool:   bpy.props.BoolProperty(name="Value")
    val_string: bpy.props.StringProperty(name="Value")
    val_vector: bpy.props.FloatVectorProperty(name="Value", size=3)

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None or obj.data is None:
            return {"CANCELLED"}
        mesh = obj.data
        attr = mesh.attributes.get(self.attr_name)
        if attr is None or len(attr.data) == 0:
            return {"CANCELLED"}
        # Pre-fill with current value
        if attr.data_type == "FLOAT":
            self.val_float = attr.data[0].value
        elif attr.data_type == "INT":
            self.val_int = attr.data[0].value
        elif attr.data_type == "BOOLEAN":
            self.val_bool = bool(attr.data[0].value)
        elif attr.data_type == "STRING":
            raw = attr.data[0].value
            self.val_string = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        elif attr.data_type == "FLOAT_VECTOR":
            self.val_vector = attr.data[0].vector[:3]
        self.attr_type = attr.data_type
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.label(text=self.attr_name)
        if self.attr_type == "FLOAT":
            layout.prop(self, "val_float", text="")
        elif self.attr_type == "INT":
            layout.prop(self, "val_int", text="")
        elif self.attr_type == "BOOLEAN":
            layout.prop(self, "val_bool", text="")
        elif self.attr_type == "STRING":
            layout.prop(self, "val_string", text="")
        elif self.attr_type == "FLOAT_VECTOR":
            layout.prop(self, "val_vector", text="")

    def execute(self, context):
        obj = context.active_object
        if obj is None or obj.data is None:
            return {"CANCELLED"}
        mesh = obj.data
        attr = mesh.attributes.get(self.attr_name)
        if attr is None:
            return {"CANCELLED"}
        if attr.data_type == "FLOAT":
            for el in attr.data:
                el.value = self.val_float
        elif attr.data_type == "INT":
            for el in attr.data:
                el.value = self.val_int
        elif attr.data_type == "BOOLEAN":
            for el in attr.data:
                el.value = self.val_bool
        elif attr.data_type == "STRING":
            encoded = self.val_string.encode("utf-8")
            for el in attr.data:
                el.value = encoded
        elif attr.data_type == "FLOAT_VECTOR":
            for el in attr.data:
                el.vector = self.val_vector
        return {"FINISHED"}


# ── Edit Custom Property operator ────────────────────────────────────────────

class GEOTOOLS_OT_edit_custom_prop(bpy.types.Operator):
    """Edit the value of a custom property on the active object."""
    bl_idname  = "geotools.edit_custom_prop"
    bl_label   = "Edit Property"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    prop_name:  bpy.props.StringProperty(name="Property", options={"HIDDEN"})
    val_string: bpy.props.StringProperty(name="Value")

    def invoke(self, context, event):
        obj = context.active_object
        if obj is None:
            return {"CANCELLED"}
        val = obj.get(self.prop_name)
        self.val_string = str(val) if val is not None else ""
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        layout = self.layout
        layout.label(text=self.prop_name)
        layout.prop(self, "val_string", text="")

    def execute(self, context):
        obj = context.active_object
        if obj is None:
            return {"CANCELLED"}
        old = obj.get(self.prop_name)
        # Try to preserve the original type
        if isinstance(old, int):
            try:
                obj[self.prop_name] = int(self.val_string)
            except ValueError:
                obj[self.prop_name] = self.val_string
        elif isinstance(old, float):
            try:
                obj[self.prop_name] = float(self.val_string)
            except ValueError:
                obj[self.prop_name] = self.val_string
        elif isinstance(old, bool):
            obj[self.prop_name] = self.val_string.lower() in ("true", "1", "yes")
        else:
            obj[self.prop_name] = self.val_string
        return {"FINISHED"}


# ── Attributes panel ─────────────────────────────────────────────────────────

_TYPE_ICONS = {
    "FLOAT": "CON_TRANSLIKE",
    "INT": "SEQUENCE",
    "BOOLEAN": "CHECKBOX_HLT",
    "STRING": "FILE_TEXT",
    "FLOAT_VECTOR": "EMPTY_ARROWS",
}


class GEOTOOLS_PT_attributes(Panel):
    bl_label       = "Attributes"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "GeoTools"
    bl_order       = 20

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj is not None and _prov.has_provenance(obj)

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        mesh = obj.data

        # ── Mesh attributes ───────────────────────────────────────────
        if mesh is not None and hasattr(mesh, "attributes"):
            for attr in mesh.attributes:
                name = attr.name
                if name.startswith(".") or name in _BUILTIN_MESH_ATTRS:
                    continue
                if len(attr.data) == 0:
                    continue

                icon = _TYPE_ICONS.get(attr.data_type, "DOT")

                # Read current value for display
                if attr.data_type == "FLOAT":
                    val_str = f"{attr.data[0].value:.6g}"
                elif attr.data_type == "INT":
                    val_str = str(attr.data[0].value)
                elif attr.data_type == "BOOLEAN":
                    val_str = "True" if attr.data[0].value else "False"
                elif attr.data_type == "STRING":
                    raw = attr.data[0].value
                    val_str = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
                elif attr.data_type == "FLOAT_VECTOR":
                    v = attr.data[0].vector
                    val_str = f"({v[0]:.4g}, {v[1]:.4g}, {v[2]:.4g})"
                else:
                    continue

                row = layout.row(align=True)
                row.label(text=name, icon=icon)
                row.label(text=val_str)
                op = row.operator("geotools.edit_attribute",
                                  text="", icon="GREASEPENCIL")
                op.attr_name = name

        # ── Non-mesh custom properties (strings not on mesh) ──────────
        has_custom = False
        for k, v in obj.items():
            if k in _prov.ALL_PROVENANCE_KEYS or k.startswith("_"):
                continue
            # Check if this key already exists as a mesh attribute
            if mesh is not None and hasattr(mesh, "attributes"):
                if mesh.attributes.get(k) is not None:
                    continue
            if not has_custom:
                layout.separator()
                layout.label(text="Custom Properties:", icon="PROPERTIES")
                has_custom = True
            row = layout.row(align=True)
            row.label(text=k, icon="FILE_TEXT")
            row.label(text=str(v))
            op = row.operator("geotools.edit_custom_prop",
                              text="", icon="GREASEPENCIL")
            op.prop_name = k


# ── Dependencies panel ────────────────────────────────────────────────────────

class GEOTOOLS_PT_dependencies(Panel):
    bl_label       = "Dependencies"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "GeoTools"
    bl_order       = 90
    bl_options     = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        from .core.checkdeps import dep_versions
        from .core.settings import settings as core_settings
        prefs = _prefs()

        versions = dep_versions()

        def row_for(pkg, has, label):
            row = layout.row(align=True)
            icon = "CHECKMARK" if has else "CANCEL"
            ver  = versions.get(pkg)
            text = f"{label}  {ver}" if ver else label
            row.label(text=text, icon=icon)

        row_for("pyproj", HAS_PYPROJ, "PyProj")
        row_for("gdal",   HAS_GDAL,   "GDAL")
        row_for("numpy",  HAS_NUMPY,  "NumPy")

        row = layout.row(align=True)
        has_maptiler = bool(getattr(prefs, "maptiler_api_key", "").strip()) if prefs else False
        row.label(text="MapTiler" + (" configured" if has_maptiler else " not configured"), icon="CHECKMARK" if has_maptiler else "CANCEL")

        if core_settings.last_reproj_engine:
            layout.separator()
            layout.label(text=f"Last engine: {core_settings.last_reproj_engine}", icon="INFO")
            if core_settings.last_reproj_summary:
                layout.label(text=core_settings.last_reproj_summary)

        if not HAS_PYPROJ and not HAS_GDAL:
            layout.separator()
            layout.label(text="No local reprojection backend.", icon="INFO")
            if has_maptiler:
                layout.label(text="AUTO will fall back to MapTiler when built-in transforms do not apply.")
            else:
                layout.label(text="Install pyproj or configure MapTiler fallback.")


# ── Registration ─────────────────────────────────────────────────────────────

_classes = (
    GeoToolsUISettings,
    GEOTOOLS_OT_edit_attribute,
    GEOTOOLS_OT_edit_custom_prop,
    GEOTOOLS_PT_scene,
    GEOTOOLS_PT_import,
    GEOTOOLS_PT_layers,
    GEOTOOLS_PT_attributes,
    GEOTOOLS_PT_dependencies,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.geotools_ui = bpy.props.PointerProperty(type=GeoToolsUISettings)


def unregister():
    del bpy.types.Scene.geotools_ui
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
