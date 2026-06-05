from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .contracts import AddonSpec

_ADDON_SPECS: dict[str, AddonSpec] = {}
_DISCOVERED = False


def register_addon(spec: AddonSpec) -> AddonSpec:
    _ADDON_SPECS[spec.id] = spec
    return spec


def discover_addons() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return

    package_root = Path(__file__).resolve().parents[1] / "addons"
    root_package = __package__.rsplit(".", 1)[0]

    for group in ("data", "analysis"):
        group_root = package_root / group
        prefix = f"{root_package}.addons.{group}."
        for modinfo in pkgutil.iter_modules([str(group_root)], prefix=prefix):
            if modinfo.ispkg:
                continue
            importlib.import_module(modinfo.name)

    _DISCOVERED = True


def iter_addons(
    *, kind: str | None = None, group: str | None = None
) -> list[AddonSpec]:
    discover_addons()
    specs = list(_ADDON_SPECS.values())
    if kind is not None:
        specs = [spec for spec in specs if spec.kind == kind]
    if group is not None:
        specs = [spec for spec in specs if spec.group == group]
    return sorted(specs, key=lambda spec: (spec.order, spec.label.lower()))


def get_addon(addon_id: str | None) -> AddonSpec | None:
    if not addon_id:
        return None
    discover_addons()
    return _ADDON_SPECS.get(addon_id)


def get_addon_for_format(fmt: str | None) -> AddonSpec | None:
    if not fmt:
        return None
    for spec in iter_addons(kind="source"):
        if spec.matches_format(fmt):
            return spec
    return None
