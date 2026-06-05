from __future__ import annotations

import bpy

from ....api import get_addon, get_analysis_tool, iter_analysis_tools, resolve_path


def _prefs():
    try:
        root_pkg = __package__.split(".", 1)[0]
        return bpy.context.preferences.addons[root_pkg].preferences
    except Exception:
        return None


def _tool_enabled(spec) -> bool:
    addon = get_addon(spec.addon_id)
    if addon is None:
        return False
    prefs = _prefs()
    return getattr(prefs, addon.pref_flag, True) if prefs else True


def iter_all_tool_specs():
    return iter_analysis_tools()


def iter_tool_specs(*, enabled_only: bool = True):
    specs = iter_analysis_tools()
    if enabled_only:
        specs = [spec for spec in specs if _tool_enabled(spec)]
    return specs


def get_tool_spec(tool_id: str | None, *, enabled_only: bool = True):
    spec = get_analysis_tool(tool_id)
    if spec is None:
        return None
    if enabled_only and not _tool_enabled(spec):
        return None
    return spec


def first_enabled_tool_spec():
    specs = iter_tool_specs(enabled_only=True)
    return specs[0] if specs else None


def has_enabled_tools() -> bool:
    return bool(iter_tool_specs(enabled_only=True))


def tool_items(_self=None, _context=None):
    specs = iter_tool_specs(enabled_only=True)
    if not specs:
        return [
            (
                "__none__",
                "— no enabled analysis tools —",
                "Enable tools in preferences",
                "INFO",
                0,
            )
        ]
    return [
        (spec.id, spec.label, spec.description, spec.icon, index)
        for index, spec in enumerate(specs)
    ]


def apply_tool_settings(cls) -> None:
    annotations = dict(getattr(cls, "__annotations__", {}))
    for spec in iter_all_tool_specs():
        for name, prop in spec.settings:
            if name in annotations:
                continue
            annotations[name] = prop
            setattr(cls, name, prop)
    cls.__annotations__ = annotations


def resolve_drawer(tool_id: str | None):
    spec = get_tool_spec(tool_id)
    if spec is None:
        spec = first_enabled_tool_spec()
    if spec is None:
        return None
    return resolve_path(spec.draw_path)


def iter_operator_classes(*, enabled_only: bool = True):
    seen = set()
    classes = []
    for spec in iter_tool_specs(enabled_only=enabled_only):
        for path in spec.operator_paths:
            if path in seen:
                continue
            seen.add(path)
            classes.append(resolve_path(path))
    return classes
