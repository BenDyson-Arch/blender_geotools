from __future__ import annotations

from ...api import AddonSpec, AnalysisToolSpec, register_addon, register_analysis_tool


def setting(name, prop):
    return (name, prop)


def register_analysis_addon(
    *,
    tool_id: str,
    label: str,
    description: str,
    icon: str,
    draw_path: str,
    operator_paths: tuple[str, ...],
    settings: tuple[tuple[str, object], ...] = (),
    order: int = 100,
):
    addon_id = f"analysis.{tool_id}"
    register_addon(
        AddonSpec(
            id=addon_id,
            label=label,
            description=description,
            pref_flag=f"mod_analysis_{tool_id}",
            module_path="blender_geotools.addons.analysis.shared",
            kind="analysis_tool",
            group="analysis",
            order=order,
        )
    )
    return register_analysis_tool(
        AnalysisToolSpec(
            addon_id=addon_id,
            id=tool_id,
            label=label,
            description=description,
            icon=icon,
            draw_path=draw_path,
            operator_paths=operator_paths,
            settings=settings,
            order=order,
        )
    )
