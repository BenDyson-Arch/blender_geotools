from __future__ import annotations

from .contracts import AnalysisToolSpec
from .registry import discover_addons

_ANALYSIS_TOOL_SPECS: dict[str, AnalysisToolSpec] = {}


def register_analysis_tool(spec: AnalysisToolSpec) -> AnalysisToolSpec:
    _ANALYSIS_TOOL_SPECS[spec.id] = spec
    return spec


def iter_analysis_tools() -> list[AnalysisToolSpec]:
    discover_addons()
    return sorted(
        _ANALYSIS_TOOL_SPECS.values(), key=lambda spec: (spec.order, spec.label.lower())
    )


def get_analysis_tool(tool_id: str | None) -> AnalysisToolSpec | None:
    if not tool_id:
        return None
    discover_addons()
    return _ANALYSIS_TOOL_SPECS.get(tool_id)
