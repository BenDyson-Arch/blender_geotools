"""
Module-level settings singleton for blender_geotools core.

Defaults are set here. blender_geotools/prefs.py calls sync_from_prefs()
after the addon preferences are loaded to push user choices into this object.
All core/ modules import from here rather than from bpy.
"""

USER_AGENT = "blender_geotools/0.2.0"


class _CoreSettings:
    # Reprojection backend: AUTO | PYPROJ | GDAL | BUILTIN | MAPTILER
    proj_engine: str = "AUTO"
    maptiler_api_key: str = ""
    user_agent: str = USER_AGENT
    last_reproj_engine: str = ""
    last_reproj_summary: str = ""


settings = _CoreSettings()


def sync_from_prefs(prefs) -> None:
    """Called by prefs.py on load and on preference change to keep core in sync."""
    settings.proj_engine = prefs.reproj_backend
    settings.maptiler_api_key = prefs.maptiler_api_key


def set_last_reproj(engine: str, summary: str = "") -> None:
    settings.last_reproj_engine = engine
    settings.last_reproj_summary = summary
