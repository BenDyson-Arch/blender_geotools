from __future__ import annotations

from .common import register_analysis_addon

TOOL_SPEC = register_analysis_addon(
    tool_id="slope",
    label="Slope",
    description="Angle between face normal and world Z (0=flat, 90=vertical)",
    icon="RNDCURVE",
    draw_path="blender_geotools.addons.analysis.shared.panel:_draw_slope",
    operator_paths=(
        "blender_geotools.addons.analysis.shared.operators:GEOTOOLS_OT_compute_slope",
    ),
    order=10,
)
