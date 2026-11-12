"""Run Checks (preflight) and Verify Placement (the rusty-tiles probe)."""

import bpy

from ..core import crs_catalog, deps, preflight, probe
from ..core.issues import Issue, Severity
from ..core.placement import Placement
from . import scene_georef
from .bake_texture import uv_overlap_issues
from .common import redraw_ui
from .crs_picker import migrate_scene
from .layers import refresh_layers
from .properties import SEVERITY_ICONS, FieldError, field_issues, placement_from_props
from .source import refresh_summary
from .texture_state import check_issues as texture_issues

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _source_poll(cls: type, context: bpy.types.Context) -> bool:
    if context.scene is None or context.scene.geotools.source_collection is None:
        cls.poll_message_set("Set or ingest a source collection first")
        return False
    return True


def store_issues(props: bpy.types.PropertyGroup, issues: list[Issue]) -> None:
    props.issues.clear()
    for issue in issues:
        item = props.issues.add()
        item.severity = Severity(issue.severity).name
        item.code = issue.code
        item.message = issue.message
        item.subject = issue.subject or ""
    props.issues_index = 0
    props.checks_run = True


def count_issues(issues: list[Issue]) -> dict[Severity, int]:
    counts = dict.fromkeys(Severity, 0)
    for issue in issues:
        counts[Severity(issue.severity)] += 1
    return counts


def describe_counts(counts: dict[Severity, int]) -> str:
    def plural(n: int, word: str) -> str:
        return f"{n} {word}{'' if n == 1 else 's'}"

    return ", ".join(
        (
            plural(counts[Severity.ERROR], "error"),
            plural(counts[Severity.WARNING], "warning"),
            plural(counts[Severity.INFO], "note"),
        )
    )


def active_rusty_tiles_profile(props: bpy.types.PropertyGroup) -> str | None:
    """The rusty-tiles profile ("fast" ... "lossless") of the active build profile."""
    index = props.build_profile_index
    if 0 <= index < len(props.build_profiles):
        return props.build_profiles[index].profile.lower()
    return None


def run_checks(context: bpy.types.Context) -> list[Issue]:
    """Summarise the source collection and check it against the placement.

    Image rules see the active build profile, so a lossy image remedy warns in a
    lossless build. Also refreshes every layer's cached range and status
    (``layers.refresh_layers``), read from the evaluated meshes.
    """
    props = context.scene.geotools
    migrate_scene(context.scene)
    summary = refresh_summary(context)
    refresh_layers(context.scene)
    assert summary is not None
    profile = active_rusty_tiles_profile(props)
    bad_fields = field_issues(props)
    if bad_fields:
        # Without valid numbers there is no Placement to check; still check the
        # scene itself against a local placement.
        issues = bad_fields + [
            Issue(
                Severity.INFO,
                "placement-not-checked",
                "Placement was not checked; fix the numbers above first.",
            )
        ]
        issues += [
            i for i in preflight.check(summary, Placement(), profile=profile) if i not in issues
        ]
    else:
        placement = placement_from_props(props)
        issues = preflight.check(summary, placement, profile=profile)
        issues += crs_issues(placement, issues)
    issues += uv_overlap_issues(context)
    issues += texture_issues(context)
    issues += scene_georef.issues(context.scene)
    issues.sort(key=lambda i: _SEVERITY_ORDER[Severity(i.severity)])
    return issues


def crs_issues(placement: Placement, found: list[Issue]) -> list[Issue]:
    """rusty-tiles' verdict on the source CRS (``crs_catalog.check_issues``).

    Skipped when preflight already rejects the CRS text (missing, a bare code).
    """
    if placement.mode != "source_crs" or not placement.source_crs:
        return []
    if any(i.code.startswith("crs-") and i.severity == Severity.ERROR for i in found):
        return []
    return crs_catalog.check_issues(placement.source_crs)


class GEOTOOLS_OT_run_checks(bpy.types.Operator):
    """Check the source collection and placement against rusty-tiles' input rules"""

    bl_idname = "geotools.run_checks"
    bl_label = "Run Checks"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _source_poll(cls, context)

    def execute(self, context: bpy.types.Context) -> set[str]:
        issues = run_checks(context)
        store_issues(context.scene.geotools, issues)
        counts = count_issues(issues)
        if counts[Severity.ERROR] or counts[Severity.WARNING]:
            # WARNING, not ERROR: the operator worked, the scene has problems.
            self.report({"WARNING"}, f"Checks found {describe_counts(counts)}")
        else:
            self.report({"INFO"}, f"Checks passed ({describe_counts(counts)})")
        redraw_ui(context)
        return {"FINISHED"}


def placement_key(placement: Placement) -> str:
    """A string that changes whenever the placement changes."""
    return repr(placement)


def store_probe(props: bpy.types.PropertyGroup, result: probe.PlacementProbe, key: str) -> None:
    props.probe_key = key
    if result.ok and result.origin_lon_lat_height is not None:
        lon, lat, height = result.origin_lon_lat_height
        props.probe_status = "OK"
        props.probe_lon, props.probe_lat, props.probe_height = repr(lon), repr(lat), repr(height)
        props.probe_operation = result.operation or ""
        props.probe_error = ""
    else:
        props.probe_status = "FAILED"
        props.probe_lon = props.probe_lat = props.probe_height = ""
        props.probe_operation = ""
        props.probe_error = result.error or "rusty-tiles gave no placement and no error"


def store_probe_error(props: bpy.types.PropertyGroup, message: str, key: str = "") -> None:
    store_probe(props, probe.PlacementProbe(ok=False, error=message), key)


class GEOTOOLS_OT_verify_placement(bpy.types.Operator):
    """Ask rusty-tiles where the source lands on Earth with the current placement"""

    bl_idname = "geotools.verify_placement"
    bl_label = "Verify Placement"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        if not _source_poll(cls, context):
            return False
        if not deps.probe_rusty_tiles().available:
            cls.poll_message_set("rusty-tiles is not available")
            return False
        return True

    def execute(self, context: bpy.types.Context) -> set[str]:
        props = context.scene.geotools
        try:
            placement = placement_from_props(props)
        except FieldError as ex:
            store_probe_error(props, f"{ex}.")
            self.report({"ERROR"}, f"Cannot verify: {ex}")
            return {"CANCELLED"}
        key = placement_key(placement)

        summary = refresh_summary(context)
        bounds = summary.bounds() if summary is not None else None
        if bounds is None:
            store_probe_error(props, "The source collection has no meshes.", key)
            self.report({"ERROR"}, "Cannot verify: the source collection has no meshes")
            return {"CANCELLED"}

        try:
            result = probe.probe_placement(placement, bounds[0], bounds[1])
        except ValueError as ex:
            # The placement itself is invalid (Placement.validate errors).
            result = probe.PlacementProbe(ok=False, error=str(ex))
        store_probe(props, result, key)
        redraw_ui(context)
        if props.probe_status == "OK":
            op = f" ({props.probe_operation})" if props.probe_operation else ""
            lon, lat, height = result.origin_lon_lat_height
            self.report({"INFO"}, f"Origin lon {lon:.9f}, lat {lat:.9f}, height {height:.4f} m{op}")
        else:
            self.report({"ERROR"}, f"Placement failed: {props.probe_error}")
        # FINISHED either way: the result (or error) is now stored for the panel.
        return {"FINISHED"}


class GEOTOOLS_UL_issues(bpy.types.UIList):
    """Issues found by Run Checks, with a severity icon."""

    bl_idname = "GEOTOOLS_UL_issues"

    def draw_item(
        self,
        context: bpy.types.Context,
        layout: bpy.types.UILayout,
        data: bpy.types.AnyType,
        item: bpy.types.PropertyGroup,
        icon: int,
        active_data: bpy.types.AnyType,
        active_property: str,
        index: int = 0,
        flt_flag: int = 0,
    ) -> None:
        # Messages already name their subject.
        layout.label(text=item.message, icon=SEVERITY_ICONS.get(item.severity, "INFO"))


classes = (
    GEOTOOLS_OT_run_checks,
    GEOTOOLS_OT_verify_placement,
    GEOTOOLS_UL_issues,
)

register, unregister = bpy.utils.register_classes_factory(classes)
