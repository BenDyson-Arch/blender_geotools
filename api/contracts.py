from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AddonCapabilities:
    """Capability flags exposed by a format/feature addon."""

    supports_import: bool = False
    supports_linked_layers: bool = False
    supports_feature_editing: bool = False
    supports_reload: bool = False
    supports_writeback: bool = False


@dataclass(frozen=True)
class AddonSpec:
    """Manifest for a repo package that plugs into the shared addon API."""

    id: str
    label: str
    description: str
    pref_flag: str
    module_path: str
    kind: str = "source"
    group: str = "data"
    order: int = 100
    format_id: str = ""
    provenance_formats: tuple[str, ...] = ()
    capabilities: AddonCapabilities = field(default_factory=AddonCapabilities)
    import_panel_draw: str | None = None
    layer_panel_draw: str | None = None

    def matches_format(self, fmt: str | None) -> bool:
        if not fmt:
            return False
        token = fmt.lower()
        if self.format_id and token == self.format_id.lower():
            return True
        if token == self.id.lower():
            return True
        return token in {value.lower() for value in self.provenance_formats}


@dataclass(frozen=True)
class AnalysisToolSpec:
    """Single-source-of-truth manifest for one analysis tool."""

    addon_id: str
    id: str
    label: str
    description: str
    icon: str
    draw_path: str
    operator_paths: tuple[str, ...]
    settings: tuple[tuple[str, Any], ...] = ()
    order: int = 100
