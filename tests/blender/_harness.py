"""Shared state and helpers for the in-Blender suite.

Only importable inside Blender. ``run_suite.py`` fills in :data:`CONFIG` and
:data:`STARTUP` before it imports any test module.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import addon_utils
import bpy


@dataclass(frozen=True)
class Config:
    """What the suite expects, from the command line of ``run_suite.py``."""

    package: str
    """Installed package, e.g. ``bl_ext.user_default.geotools``."""
    repo: str
    """Extension repository it was installed into, e.g. ``user_default``."""
    expect: str
    """``available`` or ``missing``: the expected rusty-tiles status."""
    expect_version: str | None
    """Expected rusty-tiles version when ``expect`` is ``available``."""
    extension_version: str | None
    """Version in the installed manifest, if known."""

    @property
    def extension_id(self) -> str:
        return self.package.rpartition(".")[2]


@dataclass(frozen=True)
class Startup:
    """Add-on state as Blender left it after loading the preferences."""

    loaded: bool
    default: bool
    in_preferences: bool


# Set by run_suite.py before any test module is imported.
CONFIG: Config = None  # type: ignore[assignment]
STARTUP: Startup = None  # type: ignore[assignment]


def snapshot_startup(package: str) -> Startup:
    default, loaded = addon_utils.check(package)
    return Startup(
        loaded=loaded,
        default=default,
        in_preferences=package in bpy.context.preferences.addons,
    )


# --- extension state ---------------------------------------------------------


def package() -> ModuleType:
    """The installed extension's root package."""
    return importlib.import_module(CONFIG.package)


def submodule(name: str) -> ModuleType:
    """A submodule of the installed extension, e.g. ``core.deps``."""
    return importlib.import_module(f"{CONFIG.package}.{name}")


def is_enabled() -> bool:
    _default, loaded = addon_utils.check(CONFIG.package)
    return loaded and CONFIG.package in bpy.context.preferences.addons


def enable() -> ModuleType:
    """Enable the extension; raise whatever its ``register()`` raised."""
    errors: list[BaseException] = []
    mod = addon_utils.enable(CONFIG.package, default_set=True, handle_error=errors.append)
    if errors:
        raise errors[0]
    if mod is None:
        raise RuntimeError(f"addon_utils.enable({CONFIG.package!r}) returned None")
    return mod


def disable() -> None:
    """Disable the extension; raise whatever its ``unregister()`` raised."""
    errors: list[BaseException] = []
    addon_utils.disable(CONFIG.package, default_set=True, handle_error=errors.append)
    if errors:
        raise errors[0]


def ensure_enabled() -> None:
    if not is_enabled():
        enable()


def extension_classes() -> list[type]:
    """Every bpy class the extension registers, from each module's ``classes``."""
    blender = submodule("blender")
    result: list[type] = []
    for module in blender.MODULES:
        result.extend(module.classes)
    assert result, "the extension declares no classes"
    return result


def preferences_class() -> type:
    # AddonPreferences subclasses are not exposed on bpy.types.
    return submodule("blender.preferences").GEOTOOLS_AddonPreferences


def operator_class() -> type:
    return submodule("blender.operators").GEOTOOLS_OT_check_dependencies


def panel_class() -> type:
    return submodule("blender.panels").GEOTOOLS_PT_about


def operator_available(idname: str) -> bool:
    """True if ``bpy.ops`` can find the operator.

    ``hasattr(bpy.ops.<mod>, name)`` is always true, so ask for its RNA type.
    """
    module, _, name = idname.partition(".")
    try:
        getattr(getattr(bpy.ops, module), name).get_rna_type()
    except (KeyError, AttributeError, RuntimeError):
        return False
    return True


# --- dependency expectation ------------------------------------------------------


def extensions_root() -> Path:
    return Path(bpy.utils.user_resource("EXTENSIONS")).resolve()


def is_within(path: str | Path, root: Path) -> bool:
    return Path(path).resolve().is_relative_to(root)


# --- drawing in background mode ----------------------------------------------------


_ICONS: frozenset[str] | None = None


def valid_icons() -> frozenset[str]:
    global _ICONS
    if _ICONS is None:
        param = bpy.types.UILayout.bl_rna.functions["label"].parameters["icon"]
        _ICONS = frozenset(param.enum_items.keys())
    return _ICONS


class StubOperatorProperties:
    """Stands in for the ``OperatorProperties`` that ``layout.operator`` returns."""


class StubLayout:
    """Records the calls panel and preference ``draw()`` code makes.

    Panels cannot draw in ``blender -b``, so ``draw()`` is called with this in
    place of a ``UILayout``. ``prop`` checks the property exists, and every
    property drawn in an ``alert`` layout is recorded in :attr:`alerts`.
    """

    def __init__(self, calls: list[tuple[str, str, str]], alerts: list[str] | None = None) -> None:
        self.calls = calls
        self.alerts = [] if alerts is None else alerts
        self.alert = False
        self.enabled = True
        self.active = True
        self.use_property_split = False
        self.use_property_decorate = True

    def _child(self, **_kwargs: object) -> StubLayout:
        child = StubLayout(self.calls, self.alerts)
        child.alert = self.alert
        return child

    column = row = box = split = grid_flow = column_flow = _child

    def label(self, *, text: str = "", icon: str = "NONE", **_kwargs: object) -> None:
        self.calls.append(("label", text, icon))

    def operator(
        self, idname: str, *, text: str = "", icon: str = "NONE", **_kwargs: object
    ) -> StubOperatorProperties:
        assert operator_available(idname), f"draws unknown operator {idname!r}"
        self.calls.append(("operator", idname, icon))
        return StubOperatorProperties()

    def prop(
        self, data: object, prop: str, *, text: str = "", icon: str = "NONE", **_kwargs: object
    ) -> None:
        assert prop in data.bl_rna.properties, f"draws unknown property {prop!r} of {data!r}"
        self.calls.append(("prop", prop, icon))
        if self.alert:
            self.alerts.append(prop)

    def template_list(
        self,
        listtype_name: str,
        list_id: str,
        dataptr: object,
        propname: str,
        active_dataptr: object,
        active_propname: str,
        **_kwargs: object,
    ) -> None:
        assert hasattr(bpy.types, listtype_name), f"unknown UIList {listtype_name!r}"
        assert propname in dataptr.bl_rna.properties, propname
        assert active_propname in active_dataptr.bl_rna.properties, active_propname
        self.calls.append(("template_list", listtype_name, "NONE"))

    def separator(self, **_kwargs: object) -> None:
        pass


def _check_icons(name: str, calls: list[tuple[str, str, str]]) -> None:
    icons = valid_icons()
    for kind, text, icon in calls:
        assert icon in icons, f"{name} draws {kind} {text!r} with unknown icon {icon!r}"


def draw(cls: type, alerts: list[str] | None = None) -> list[tuple[str, str, str]]:
    """Run ``cls.draw`` against a :class:`StubLayout` and check every icon exists."""
    calls: list[tuple[str, str, str]] = []
    fake_self = type("DrawSelf", (), {"layout": StubLayout(calls, alerts)})()
    cls.draw(fake_self, bpy.context)
    _check_icons(cls.__name__, calls)
    return calls


def draw_list_item(cls: type, data: object, item: object) -> list[tuple[str, str, str]]:
    """Run a UIList's ``draw_item`` for one item against a :class:`StubLayout`."""
    calls: list[tuple[str, str, str]] = []
    cls.draw_item(None, bpy.context, StubLayout(calls), data, item, 0, data, "", 0)
    _check_icons(cls.__name__, calls)
    return calls


class ReportRecorder:
    """Stands in for an operator's ``self`` so ``execute`` reports can be read back."""

    def __init__(self) -> None:
        self.reports: list[tuple[frozenset[str], str]] = []

    def report(self, levels: set[str], message: str) -> None:
        self.reports.append((frozenset(levels), message))


def labels(calls: list[tuple[str, str, str]]) -> Iterator[tuple[str, str]]:
    for kind, text, icon in calls:
        if kind == "label":
            yield text, icon


def props_drawn(calls: list[tuple[str, str, str]]) -> list[str]:
    return [text for kind, text, _icon in calls if kind == "prop"]


# --- scene state ------------------------------------------------------------------


def project() -> bpy.types.PropertyGroup:
    return bpy.context.scene.geotools


def reset_scene() -> None:
    """Remove every object and data block the tests make, and reset the project."""
    for collection in (
        bpy.data.objects,
        bpy.data.meshes,
        bpy.data.materials,
        bpy.data.images,
        bpy.data.actions,
        bpy.data.cameras,
        bpy.data.lights,
        bpy.data.collections,
    ):
        for block in list(collection):
            collection.remove(block)
    scene = bpy.context.scene
    scene.unit_settings.scale_length = 1.0
    props = scene.geotools
    for prop in props.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"} or prop.is_readonly and prop.type != "COLLECTION":
            continue
        if prop.type == "COLLECTION":
            getattr(props, name).clear()
        else:
            props.property_unset(name)
    props.source_collection = None


def call_op(op: object, **kwargs: object) -> tuple[set[str], str | None]:
    """Call an operator; return its result and the error it reported, if any.

    ``bpy.ops`` raises RuntimeError when an operator reports an ERROR, even
    when it returns FINISHED.
    """
    try:
        return op(**kwargs), None
    except RuntimeError as ex:
        return {"ERROR_REPORTED"}, str(ex)
