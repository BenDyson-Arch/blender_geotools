"""
prefs.py — AddonPreferences and CRS preset management operators.

Preferences are stored in Blender's addon preferences (auto-saved).
CRS presets are serialised as a JSON string (same pattern as BlenderGIS).
"""

import json
import logging

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import AddonPreferences, Operator

from .core.checkdeps import HAS_GDAL, HAS_NUMPY, HAS_PYPROJ, dep_versions
from .core import settings as _core_settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default CRS presets  [(srid, label, description), ...]
_DEFAULT_PRESETS_JSON = json.dumps([
    ["EPSG:4326", "WGS84 Geographic", "World Geodetic System 1984 (lon/lat)"],
    ["EPSG:3857", "Web Mercator", "Pseudo-Mercator — used by web maps"],
    ["EPSG:32634", "UTM 34N", "WGS84 / UTM zone 34N (central Europe, Greece)"],
    ["EPSG:2100", "Greek Grid (GGRS87)", "Official Greek national grid (EPSG:2100)"],
])


# ---------------------------------------------------------------------------
# Helpers

def _list_crs_presets(self, context):
    try:
        data = json.loads(self.crs_presets_json)
        return [tuple(item) for item in data]
    except Exception:
        return [("EPSG:4326", "WGS84", "")]


def _sync_core_settings(prefs, _context=None):
    _core_settings.sync_from_prefs(prefs)


def _crs_search_update(self, context):
    if len(self.query) < 2:
        self.results_json = "[]"
        return
    prefs = context.preferences.addons[__package__].preferences
    api_key = prefs.maptiler_api_key.strip()
    if not api_key:
        self.results_json = "[]"
        return
    try:
        from .core.proj.srv import MapTilerCoordinates
        results = MapTilerCoordinates(api_key).search(self.query)
        self.results_json = json.dumps(results)
        if results:
            code = results[0]["id"]["code"]
            self.selected_srid = f"EPSG:{code}"
            self.preset_label = results[0]["name"]
    except Exception as exc:
        log.warning("CRS search failed: %s", exc)
        self.results_json = "[]"


def _crs_result_items(self, context):
    try:
        results = json.loads(self.results_json)
        if not results:
            return [("", "No results - check API key or connection", "")]
        return [
            (f"EPSG:{r['id']['code']}", r["name"], f"EPSG:{r['id']['code']}")
            for r in results[:20]
        ]
    except Exception:
        return [("", "Error parsing results", "")]


def _crs_result_choice_update(self, context):
    self.selected_srid = self.result_choice
    try:
        results = json.loads(self.results_json)
        for r in results:
            if f"EPSG:{r['id']['code']}" == self.result_choice:
                self.preset_label = r["name"]
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Operators: add / remove CRS presets

class GEOTOOLS_OT_add_crs_preset(Operator):
    """Search for a CRS by name or EPSG code and add it to your presets."""

    bl_idname = "geotools.add_crs_preset"
    bl_label = "Add CRS Preset"
    bl_options = {"REGISTER", "INTERNAL"}

    query: StringProperty(
        name="Search",
        description="Name or EPSG code to search (requires MapTiler API key)",
        default="",
        update=_crs_search_update,
    )
    results_json: StringProperty(default="[]", options={"HIDDEN"})
    selected_srid: StringProperty(name="EPSG / SRID", default="")
    preset_label: StringProperty(name="Label", default="")

    result_choice: EnumProperty(
        name="Result",
        items=_crs_result_items,
        update=_crs_result_choice_update,
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        layout = self.layout
        prefs = context.preferences.addons[__package__].preferences
        if not prefs.maptiler_api_key:
            layout.label(text="No MapTiler API key set — manual entry only.", icon="ERROR")
        else:
            layout.prop(self, "query", icon="VIEWZOOM")
            layout.prop(self, "result_choice")
        layout.separator()
        layout.prop(self, "selected_srid")
        layout.prop(self, "preset_label")

    def execute(self, context):
        srid = self.selected_srid.strip()
        label = self.preset_label.strip() or srid
        if not srid:
            self.report({"ERROR"}, "No CRS selected")
            return {"CANCELLED"}

        # Validate
        from .core.proj.srs import SRS
        if not SRS.validate(srid):
            self.report({"ERROR"}, f"Invalid CRS: {srid}")
            return {"CANCELLED"}

        prefs = context.preferences.addons[__package__].preferences
        try:
            data = json.loads(prefs.crs_presets_json)
        except Exception:
            data = []

        # Avoid duplicates
        if any(item[0] == srid for item in data):
            self.report({"WARNING"}, f"{srid} is already in presets")
            return {"CANCELLED"}

        data.append([srid, label, ""])
        prefs.crs_presets_json = json.dumps(data)
        self.report({"INFO"}, f"Added {srid} ({label})")
        return {"FINISHED"}


class GEOTOOLS_OT_remove_crs_preset(Operator):
    """Remove the selected CRS preset."""

    bl_idname = "geotools.remove_crs_preset"
    bl_label = "Remove CRS Preset"
    bl_options = {"REGISTER", "INTERNAL"}

    srid: StringProperty(options={"HIDDEN"})

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        try:
            data = json.loads(prefs.crs_presets_json)
        except Exception:
            return {"CANCELLED"}
        data = [item for item in data if item[0] != self.srid]
        prefs.crs_presets_json = json.dumps(data)
        return {"FINISHED"}


class GEOTOOLS_OT_reset_crs_presets(Operator):
    """Reset CRS presets to the built-in defaults."""

    bl_idname = "geotools.reset_crs_presets"
    bl_label = "Reset to Defaults"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        prefs.crs_presets_json = _DEFAULT_PRESETS_JSON
        return {"FINISHED"}


class GEOTOOLS_OT_test_maptiler_api(Operator):
    """Test the configured MapTiler API key against search and transform endpoints."""

    bl_idname = "geotools.test_maptiler_api"
    bl_label = "Test MapTiler"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        api_key = prefs.maptiler_api_key.strip()
        if not api_key:
            self.report({"ERROR"}, "No MapTiler API key configured")
            return {"CANCELLED"}

        try:
            from .core.proj.srv import MapTilerCoordinates
            result = MapTilerCoordinates(api_key).test()
        except Exception as exc:
            self.report({"ERROR"}, f"MapTiler test failed: {exc}")
            return {"CANCELLED"}

        x, y = result["reproj_result"]
        self.report(
            {"INFO"},
            f"MapTiler OK: {result['search_count']} search result(s), 0,0 -> {x:.3f},{y:.3f} in EPSG:3857"
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# AddonPreferences

class GeoToolsPrefs(AddonPreferences):
    bl_idname = __package__

    # -- Reprojection backend -------------------------------------------------
    reproj_backend: EnumProperty(
        name="Reprojection Backend",
        description="Engine used to reproject coordinates between CRS",
        items=[
            ("AUTO", "Auto", "Use the best available backend (PyProj → GDAL → Built-in → MapTiler)"),
            ("PYPROJ", "PyProj", "Use PyProj library (recommended)"),
            ("GDAL", "GDAL", "Use GDAL/OGR library"),
            ("BUILTIN", "Built-in", "No external dependency; supports UTM and Web Mercator only"),
            ("MAPTILER", "MapTiler API", "Reproject via MapTiler web API (requires key + internet)"),
        ],
        default="AUTO",
        update=_sync_core_settings,
    )

    maptiler_api_key: StringProperty(
        name="MapTiler API Key",
        description=(
            "Free API key from maptiler.com. Used for live CRS search "
            "and (optionally) point reprojection."
        ),
        default="",
        subtype="PASSWORD",
        update=_sync_core_settings,
    )

    # -- Enabled modules -------------------------------------------------------
    mod_geojson: BoolProperty(name="GeoJSON", default=True)
    mod_gpkg: BoolProperty(name="GeoPackage", default=True)
    mod_tiles3d: BoolProperty(name="3D Tiles", default=True)
    mod_dem: BoolProperty(name="DEM", default=True)
    mod_analysis: BoolProperty(name="Analysis Tools", default=True)

    # -- CRS presets -----------------------------------------------------------
    crs_presets_json: StringProperty(
        name="CRS Presets (JSON)",
        default=_DEFAULT_PRESETS_JSON,
    )
    crs_presets: EnumProperty(
        name="CRS",
        description="Predefined coordinate reference systems",
        items=_list_crs_presets,
    )

    # -------------------------------------------------------------------------

    def draw(self, context):
        layout = self.layout

        # -- Backend & API key -------------------------------------------------
        box = layout.box()
        box.label(text="Reprojection", icon="ORIENTATION_GLOBAL")
        box.prop(self, "reproj_backend")
        row = box.row()
        row.prop(self, "maptiler_api_key")
        row.operator("geotools.test_maptiler_api", text="Test", icon="CHECKMARK")
        row.operator("wm.url_open", text="Get free key", icon="URL").url = (
            "https://cloud.maptiler.com/account/keys/"
        )

        # -- Dependencies status -----------------------------------------------
        box = layout.box()
        box.label(text="Dependencies", icon="PACKAGE")
        versions = dep_versions()
        for pkg, ver in versions.items():
            row = box.row()
            if ver:
                row.label(text=f"{pkg}  {ver}", icon="CHECKMARK")
            else:
                row.label(text=f"{pkg}  not found", icon="X")

        # -- Enabled modules ---------------------------------------------------
        box = layout.box()
        box.label(text="Enabled Modules", icon="MODIFIER")
        row = box.row()
        row.prop(self, "mod_geojson", toggle=True)
        row.prop(self, "mod_gpkg", toggle=True)
        row.prop(self, "mod_tiles3d", toggle=True)
        row.prop(self, "mod_dem", toggle=True)
        row.prop(self, "mod_analysis", toggle=True)

        # -- CRS presets -------------------------------------------------------
        box = layout.box()
        box.label(text="CRS Presets", icon="WORLD")
        try:
            presets = json.loads(self.crs_presets_json)
        except Exception:
            presets = []

        for item in presets:
            row = box.row(align=True)
            row.label(text=f"{item[1]}  ({item[0]})")
            op = row.operator("geotools.remove_crs_preset", text="", icon="X")
            op.srid = item[0]

        row = box.row(align=True)
        row.operator("geotools.add_crs_preset", text="Add CRS", icon="ADD")
        row.operator("geotools.reset_crs_presets", text="Reset", icon="LOOP_BACK")


# ---------------------------------------------------------------------------

_classes = (
    GEOTOOLS_OT_add_crs_preset,
    GEOTOOLS_OT_remove_crs_preset,
    GEOTOOLS_OT_reset_crs_presets,
    GEOTOOLS_OT_test_maptiler_api,
    GeoToolsPrefs,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)

    # Sync core settings immediately on load
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        _sync_core_settings(prefs)
    except Exception:
        pass  # prefs not yet available during first-time load


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
