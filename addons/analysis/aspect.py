from __future__ import annotations

from .common import register_analysis_addon

TOOL_SPEC = register_analysis_addon(
    tool_id="aspect",
    label="Aspect",
    description="Compass bearing of the downslope direction (0/360=N, 90=E)",
    icon="ORIENTATION_GLOBAL",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_aspect",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_compute_aspect",
    ),
    order=20,
)
