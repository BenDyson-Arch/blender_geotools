from __future__ import annotations

from .common import register_analysis_addon

TOOL_SPEC = register_analysis_addon(
    tool_id="roughness",
    label="Roughness",
    description="Scale-independent local relief relative to face size",
    icon="MESH_GRID",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_roughness",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_compute_roughness",
    ),
    order=30,
)
