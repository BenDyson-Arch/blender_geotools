"""
analysis/operators.py — Terrain analysis operators.
"""

import bpy
import mathutils
from bpy.types import Operator

from ....core.raycast import SceneRaycaster
from . import attr_transfer as _attr_transfer
from . import attr_vis as _attr_vis
from . import los as _los
from . import shadow as _shadow
from . import surface_tag as _surface_tag
from . import terrain as _terrain
from .registry import iter_operator_classes

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _run(operator, context, *, slope=False, aspect=False, roughness=False):
    """Run compute_terrain_attributes on selected meshes with the given flags."""
    targets = [obj for obj in context.selected_objects if obj.type == "MESH"]
    if not targets:
        operator.report({"ERROR"}, "No mesh objects selected")
        return {"CANCELLED"}

    processed = 0
    for obj in targets:
        try:
            s_min, s_max, a_min, a_max, r_min, r_max = (
                _terrain.compute_terrain_attributes(
                    obj,
                    compute_slope=slope,
                    compute_aspect=aspect,
                    compute_roughness=roughness,
                )
            )
            processed += 1
            parts = []
            if slope:
                parts.append(f"slope {s_min:.1f}°-{s_max:.1f}°")
            if aspect:
                parts.append(f"aspect {a_min:.1f}°-{a_max:.1f}°")
            if roughness:
                parts.append(f"roughness {r_min:.3f}-{r_max:.3f}")
            operator.report({"INFO"}, f"{obj.name}: {', '.join(parts)}")
        except TypeError as exc:
            operator.report({"WARNING"}, str(exc))

    if processed == 0:
        operator.report({"ERROR"}, "No valid mesh objects processed")
        return {"CANCELLED"}

    operator.report({"INFO"}, f"Attribute written to {processed} object(s)")
    return {"FINISHED"}


# ---------------------------------------------------------------------------
# Individual operators
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_compute_slope(Operator):
    """Write slope_deg face attribute (0 = flat, 90 = vertical)."""

    bl_idname = "geotools.compute_slope"
    bl_label = "Compute Slope"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        return _run(self, context, slope=True)


class GEOTOOLS_OT_compute_aspect(Operator):
    """Write aspect_deg face attribute (compass bearing of downslope direction)."""

    bl_idname = "geotools.compute_aspect"
    bl_label = "Compute Aspect"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        return _run(self, context, aspect=True)


class GEOTOOLS_OT_compute_roughness(Operator):
    """Write roughness face attribute (scale-independent local relief)."""

    bl_idname = "geotools.compute_roughness"
    bl_label = "Compute Roughness"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        return _run(self, context, roughness=True)


# ---------------------------------------------------------------------------
# Line of Sight operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_compute_los(Operator):
    """Check line of sight from an observer to each selected target object.

    The active object (or 3D cursor) is the observer; all other selected
    mesh objects are targets.  A ``los_visible`` boolean custom property is
    written to each target object.
    """

    bl_idname = "geotools.compute_los"
    bl_label = "Check Line of Sight"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active = context.active_object
        settings = getattr(context.scene, "geotools_analysis", None)
        if settings and settings.los_observer == "ACTIVE" and active is None:
            return False
        return any(
            obj.type == "MESH" and obj is not active for obj in context.selected_objects
        )

    def execute(self, context):
        settings = context.scene.geotools_analysis

        # ── Resolve observer point ─────────────────────────────────────
        if settings.los_observer == "CURSOR":
            obs_base = context.scene.cursor.location.copy()
            observer_obj_name = None
        else:
            active = context.active_object
            if active is None:
                self.report({"ERROR"}, "No active object to use as observer")
                return {"CANCELLED"}
            obs_base = active.location.copy()
            observer_obj_name = active.name

        obs_pt = obs_base + mathutils.Vector((0.0, 0.0, settings.los_observer_height))

        # ── Collect targets ────────────────────────────────────────────
        active = context.active_object
        targets = [
            obj
            for obj in context.selected_objects
            if obj.type == "MESH" and obj is not active
        ]
        if not targets:
            self.report(
                {"ERROR"},
                "Select target mesh objects (make observer the active object)",
            )
            return {"CANCELLED"}

        # ── Build raycaster, excluding observer + targets ──────────────
        terrain_col = settings.los_terrain_collection.strip() or None
        exclude: list[str] = []
        if observer_obj_name:
            exclude.append(observer_obj_name)
        for t in targets:
            exclude.append(t.name)

        raycaster = SceneRaycaster(
            context,
            collection_names=[terrain_col] if terrain_col else None,
            exclude_names=exclude or None,
        )

        if raycaster.object_count == 0:
            self.report(
                {"WARNING"},
                "No blocking geometry found — check Terrain Collection name",
            )

        # ── Check each target ──────────────────────────────────────────
        visible_count = 0
        for obj in targets:
            tgt_pt = obj.location + mathutils.Vector(
                (0.0, 0.0, settings.los_target_height)
            )
            result = _los.check_los(raycaster, obs_pt, tgt_pt)
            obj["los_visible"] = result.visible
            if result.visible:
                visible_count += 1
                self.report({"INFO"}, f"{obj.name}: visible  ({result.distance:.1f} m)")
            else:
                blocker = (
                    result.obstructing_object.name
                    if result.obstructing_object
                    else "unknown"
                )
                self.report({"INFO"}, f"{obj.name}: BLOCKED by {blocker}")

        self.report(
            {"INFO"},
            f"LoS complete — {visible_count}/{len(targets)} target(s) visible",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Surface Tag operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_tag_surface(Operator):
    """Write a boolean face attribute tagging faces by slope range or Edit Mode selection."""

    bl_idname = "geotools.tag_surface"
    bl_label = "Tag Surface"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        settings = context.scene.geotools_analysis
        mode = settings.tag_mode
        attr_name = settings.tag_attribute_name.strip() or "walkable"
        targets = [obj for obj in context.selected_objects if obj.type == "MESH"]

        if not targets:
            self.report({"ERROR"}, "No mesh objects selected")
            return {"CANCELLED"}

        # Warn if any target is still in Edit Mode (selection won't be up-to-date)
        if mode == "SELECTION":
            edit_mode_objs = [o.name for o in targets if o.mode == "EDIT"]
            if edit_mode_objs:
                self.report(
                    {"WARNING"},
                    f"Exit Edit Mode first — {', '.join(edit_mode_objs)} still in Edit Mode",
                )
                return {"CANCELLED"}

        total_tagged = 0
        total_faces = 0

        for obj in targets:
            try:
                if mode == "SLOPE":
                    tagged, faces = _surface_tag.tag_faces_by_slope(
                        obj,
                        min_slope_deg=settings.tag_slope_min,
                        max_slope_deg=settings.tag_slope_max,
                        attr_name=attr_name,
                        auto_compute=settings.tag_auto_slope,
                    )
                else:  # SELECTION
                    tagged, faces = _surface_tag.tag_faces_by_selection(
                        obj, attr_name=attr_name
                    )
                total_tagged += tagged
                total_faces += faces
                pct = 100.0 * tagged / faces if faces else 0.0
                self.report(
                    {"INFO"},
                    f"{obj.name}: {tagged}/{faces} faces tagged '{attr_name}' ({pct:.1f}%)",
                )
            except ValueError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"Tagged {total_tagged}/{total_faces} faces as '{attr_name}' across "
            f"{len(targets)} object(s)",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Attribute Transfer operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_transfer_attributes(Operator):
    """Copy a face attribute from the active (source) mesh to selected target meshes.

    Active object = source (owns the attribute to copy).  All other selected
    mesh objects = targets (will receive the copied attribute).
    """

    bl_idname = "geotools.transfer_attributes"
    bl_label = "Transfer Attribute"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        active = context.active_object
        if active is None or active.type != "MESH":
            return False
        settings = getattr(context.scene, "geotools_analysis", None)
        if settings and not settings.xfer_source_attr.strip():
            return False
        return any(
            obj.type == "MESH" and obj is not active for obj in context.selected_objects
        )

    def execute(self, context):
        settings = context.scene.geotools_analysis
        src_obj = context.active_object
        src_attr = settings.xfer_source_attr.strip()
        dst_attr = settings.xfer_dest_attr.strip() or src_attr
        method = settings.xfer_method
        max_dist = settings.xfer_max_distance
        cast_dir = settings.xfer_ray_direction

        targets = [
            obj
            for obj in context.selected_objects
            if obj.type == "MESH" and obj is not src_obj
        ]
        if not targets:
            self.report(
                {"ERROR"}, "Select target mesh objects (make source the active object)"
            )
            return {"CANCELLED"}

        # Build raycaster scoped to only the source object
        raycaster = SceneRaycaster(
            context,
            include_names=[src_obj.name],
        )
        if raycaster.object_count == 0:
            self.report({"ERROR"}, f"Source object '{src_obj.name}' is not visible")
            return {"CANCELLED"}

        total_transferred = 0
        total_faces = 0

        for tgt in targets:
            try:
                if method == "RAYCAST":
                    transferred, faces = _attr_transfer.transfer_by_raycast(
                        raycaster,
                        src_obj,
                        tgt,
                        src_attr_name=src_attr,
                        dst_attr_name=dst_attr,
                        cast_direction=cast_dir,
                        max_distance=max_dist,
                    )
                else:  # NEAREST
                    transferred, faces = _attr_transfer.transfer_by_nearest(
                        raycaster,
                        src_obj,
                        tgt,
                        src_attr_name=src_attr,
                        dst_attr_name=dst_attr,
                        max_distance=max_dist,
                    )
                total_transferred += transferred
                total_faces += faces
                miss = faces - transferred
                pct = 100.0 * transferred / faces if faces else 0.0
                self.report(
                    {"INFO"},
                    f"{tgt.name}: {transferred}/{faces} faces transferred"
                    + (f" ({miss} missed)" if miss else ""),
                )
            except ValueError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"Transfer complete — '{src_attr}' → '{dst_attr}' on {len(targets)} object(s); "
            f"{total_transferred}/{total_faces} faces filled",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Attribute Visualisation operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_visualise_attribute(Operator):
    """Map a FACE attribute or object custom property to viewport colours."""

    bl_idname = "geotools.visualise_attribute"
    bl_label = "Visualise Attribute"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        settings = getattr(context.scene, "geotools_analysis", None)
        if not settings or not settings.vis_source_attr.strip():
            return False
        return any(obj.type == "MESH" for obj in context.selected_objects)

    def execute(self, context):
        settings = context.scene.geotools_analysis
        src_attr = settings.vis_source_attr.strip()
        dst_attr = settings.vis_output_attr.strip() or f"{src_attr}_color"
        mode = settings.vis_mode
        source = settings.vis_source
        targets = [obj for obj in context.selected_objects if obj.type == "MESH"]

        if not targets:
            self.report({"ERROR"}, "No mesh objects selected")
            return {"CANCELLED"}

        # ── Object custom-property mode ───────────────────────────────
        if source == "OBJECT_PROP":
            range_min = None if settings.vis_auto_range else settings.vis_range_min
            range_max = None if settings.vis_auto_range else settings.vis_range_max
            try:
                painted, info = _attr_vis.colourise_objects_by_property(
                    targets,
                    src_attr,
                    dst_attr,
                    mode=mode,
                    ramp_name=settings.vis_ramp,
                    invert=settings.vis_invert,
                    range_min=range_min,
                    range_max=range_max,
                )
            except ValueError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

            for name in info.get("skipped", []):
                self.report(
                    {"WARNING"}, f"{name}: no custom property '{src_attr}' — skipped"
                )

            if painted == 0:
                if info.get("skipped"):
                    self.report(
                        {"ERROR"},
                        f"No selected objects have a custom property '{src_attr}'",
                    )
                else:
                    self.report(
                        {"ERROR"},
                        f"Property '{src_attr}' exists but value type is not supported (string, list, etc.)",
                    )
                return {"CANCELLED"}

            if "range" in info:
                v_min, v_max = info["range"]
                self.report(
                    {"INFO"},
                    f"{painted} object(s) coloured (range {v_min:.4g}–{v_max:.4g})",
                )
            else:
                n_cats = len(info.get("legend", {}))
                self.report(
                    {"INFO"}, f"{painted} object(s) coloured ({n_cats} categories)"
                )

            self.report(
                {"INFO"},
                f"Visualised '{src_attr}' as '{dst_attr}' on {painted} object(s)",
            )
            return {"FINISHED"}

        # ── Face attribute mode ───────────────────────────────────────
        processed = 0
        for obj in targets:
            mesh = obj.data
            if src_attr not in mesh.attributes:
                self.report(
                    {"WARNING"}, f"{obj.name}: no attribute '{src_attr}' — skipped"
                )
                continue

            attr = mesh.attributes[src_attr]
            if attr.domain != "FACE":
                self.report(
                    {"WARNING"},
                    f"{obj.name}: '{src_attr}' is not FACE-domain — skipped",
                )
                continue
            if attr.data_type not in _attr_vis._NUMERIC_TYPES:
                self.report(
                    {"WARNING"},
                    f"{obj.name}: '{src_attr}' type '{attr.data_type}' is not numeric — skipped",
                )
                continue

            effective = _attr_vis.detect_mode(obj, src_attr) if mode == "AUTO" else mode

            try:
                if effective == "RAMP":
                    range_min = (
                        None if settings.vis_auto_range else settings.vis_range_min
                    )
                    range_max = (
                        None if settings.vis_auto_range else settings.vis_range_max
                    )
                    faces, v_min, v_max = _attr_vis.colourise_ramp(
                        obj,
                        src_attr,
                        dst_attr,
                        ramp_name=settings.vis_ramp,
                        invert=settings.vis_invert,
                        range_min=range_min,
                        range_max=range_max,
                    )
                    self.report(
                        {"INFO"},
                        f"{obj.name}: {faces} faces coloured "
                        f"(range {v_min:.4g}–{v_max:.4g}, ramp {settings.vis_ramp})",
                    )
                else:  # CATEGORICAL
                    faces, legend = _attr_vis.colourise_categorical(
                        obj, src_attr, dst_attr
                    )
                    self.report(
                        {"INFO"}, f"{obj.name}: {faces} faces, {len(legend)} categories"
                    )
                processed += 1
            except ValueError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

        if processed == 0:
            self.report(
                {"ERROR"}, f"No selected objects have a FACE attribute '{src_attr}'"
            )
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            f"Visualised '{src_attr}' as '{dst_attr}' on {processed} object(s)",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Shadow Analysis operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_compute_shadow(Operator):
    """Compute per-face shadow from a configurable sun position.

    Instant mode: writes a boolean ``in_shadow`` FACE attribute.
    Daily mode: samples solar positions across a UTC hour window and writes
    a float ``shadow_fraction`` FACE attribute (0.0 = fully lit, 1.0 = always
    in shadow).
    """

    bl_idname = "geotools.compute_shadow"
    bl_label = "Compute Shadow"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return any(obj.type == "MESH" for obj in context.selected_objects)

    # ------------------------------------------------------------------
    # Helpers

    @staticmethod
    def _parse_date(date_str: str) -> tuple[int, int, int]:
        """Return (year, month, day) from a YYYY-MM-DD string."""
        parts = date_str.strip().split("-")
        if len(parts) != 3:
            raise ValueError(f"Invalid date format '{date_str}' — expected YYYY-MM-DD")
        return int(parts[0]), int(parts[1]), int(parts[2])

    @staticmethod
    def _geoscene_latlon(context) -> tuple[float, float] | None:
        """Return (lat, lon) from the scene's GeoScene, or None."""
        try:
            from ....geoscene import GeoScene

            gs = GeoScene(context.scene)
            if gs.has_origin_geo:
                return gs.origin_lat, gs.origin_lon
        except Exception:
            pass
        return None

    def _resolve_sun_dirs(self, context) -> list[mathutils.Vector] | None:
        """Build the list of toward-sun unit vectors; returns None on error."""
        settings = context.scene.geotools_analysis

        if settings.shadow_mode == "INSTANT":
            src = settings.shadow_source

            if src == "MANUAL":
                return [
                    _shadow.sun_direction(
                        settings.shadow_azimuth, settings.shadow_elevation
                    )
                ]

            if src == "SUN_LAMP":
                lamp_name = settings.shadow_sun_object.strip()
                if not lamp_name:
                    self.report({"ERROR"}, "Enter the name of a Sun light object")
                    return None
                lamp = bpy.data.objects.get(lamp_name)
                if lamp is None:
                    self.report({"ERROR"}, f"Sun lamp object '{lamp_name}' not found")
                    return None
                if lamp.type != "LIGHT" or lamp.data.type != "SUN":
                    self.report({"ERROR"}, f"'{lamp_name}' is not a Sun light object")
                    return None
                return [_shadow.sun_direction_from_lamp(lamp)]

            # src == "SOLAR"
            return self._build_solar_dirs(
                context,
                settings,
                hours=[settings.shadow_hour_utc],
            )

        # DAILY mode — always uses solar position
        return self._build_solar_dirs(
            context,
            settings,
            hours=None,  # build from start/end/steps
        )

    def _build_solar_dirs(self, context, settings, hours):
        """Compute solar direction(s); filter to above-horizon only."""
        try:
            year, month, day = self._parse_date(settings.shadow_date)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return None

        if settings.shadow_use_geoscene:
            latlon = self._geoscene_latlon(context)
            if latlon is None:
                self.report(
                    {"ERROR"},
                    "Scene has no GeoScene origin — enter Latitude / Longitude manually",
                )
                return None
            lat, lon = latlon
        else:
            lat = settings.shadow_lat
            lon = settings.shadow_lon

        if hours is None:
            # Daily sampling — hours are local time
            n = max(2, settings.shadow_num_steps)
            start = settings.shadow_hour_start
            end = settings.shadow_hour_end
            if end <= start:
                self.report({"ERROR"}, "End Hour must be greater than Start Hour")
                return None
            step = (end - start) / (n - 1)
            hours = [start + i * step for i in range(n)]

        # Convert local hours → UTC by subtracting the offset
        utc_offset = settings.shadow_utc_offset
        utc_hours = [h - utc_offset for h in hours]

        dirs: list[mathutils.Vector] = []
        for h in utc_hours:
            az, el = _shadow.solar_position(lat, lon, year, month, day, h)
            if el > 0.0:
                dirs.append(_shadow.sun_direction(az, el))

        if not dirs:
            self.report(
                {"WARNING"},
                "Sun is below the horizon for all sampled times — no shadow data written",
            )
            return None

        return dirs

    # ------------------------------------------------------------------
    def execute(self, context):
        settings = context.scene.geotools_analysis
        targets = [obj for obj in context.selected_objects if obj.type == "MESH"]

        if not targets:
            self.report({"ERROR"}, "No mesh objects selected")
            return {"CANCELLED"}

        sun_dirs = self._resolve_sun_dirs(context)
        if sun_dirs is None:
            return {"CANCELLED"}

        # Build raycaster — terrain collection scopes the occluding geometry.
        # The target objects themselves are included so terrain self-shadows.
        terrain_col = settings.shadow_terrain_collection.strip() or None
        raycaster = SceneRaycaster(
            context,
            collection_names=[terrain_col] if terrain_col else None,
        )
        if raycaster.object_count == 0:
            self.report(
                {"WARNING"},
                "No blocking geometry found — check Terrain Collection name",
            )

        is_daily = settings.shadow_mode == "DAILY"
        attr_name = (
            settings.shadow_attr_daily.strip() or "shadow_fraction"
            if is_daily
            else settings.shadow_attr_instant.strip() or "in_shadow"
        )

        total_faces = 0
        processed = 0

        for obj in targets:
            try:
                if is_daily:
                    f_min, f_max, n = _shadow.compute_shadow_fraction(
                        obj, raycaster, sun_dirs, attr_name=attr_name
                    )
                    self.report(
                        {"INFO"},
                        f"{obj.name}: {n} faces — shadow fraction {f_min:.2f}–{f_max:.2f}",
                    )
                else:
                    shadowed, n = _shadow.compute_shadow_instant(
                        obj, raycaster, sun_dirs[0], attr_name=attr_name
                    )
                    pct = 100.0 * shadowed / n if n else 0.0
                    self.report(
                        {"INFO"},
                        f"{obj.name}: {shadowed}/{n} faces in shadow ({pct:.1f}%)",
                    )
                total_faces += n
                processed += 1
            except ValueError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}

        mode_label = "shadow fraction" if is_daily else "in_shadow"
        self.report(
            {"INFO"},
            f"Shadow '{attr_name}' written to {processed} object(s) ({total_faces} faces total)",
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Place Sun Lamp operator
# ---------------------------------------------------------------------------


class GEOTOOLS_OT_place_sun_lamp(Operator):
    """Create or update a Sun light oriented to the current shadow settings.

    Uses the same source / solar-position logic as Compute Shadow so the
    lamp always matches the shadow analysis.
    """

    bl_idname = "geotools.place_sun_lamp"
    bl_label = "Place Sun Lamp"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.geotools_analysis
        src = settings.shadow_source

        if src == "MANUAL":
            az = settings.shadow_azimuth
            el = settings.shadow_elevation

        elif src == "SUN_LAMP":
            self.report({"ERROR"}, "Source is already a Sun Lamp — nothing to place")
            return {"CANCELLED"}

        else:  # SOLAR
            try:
                parts = settings.shadow_date.strip().split("-")
                year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError):
                self.report(
                    {"ERROR"},
                    f"Invalid date '{settings.shadow_date}' — expected YYYY-MM-DD",
                )
                return {"CANCELLED"}

            if settings.shadow_use_geoscene:
                try:
                    from ....geoscene import GeoScene

                    gs = GeoScene(context.scene)
                    if not gs.has_origin_geo:
                        self.report(
                            {"ERROR"},
                            "Scene has no GeoScene origin — enter Lat/Lon manually",
                        )
                        return {"CANCELLED"}
                    lat, lon = gs.origin_lat, gs.origin_lon
                except Exception as exc:
                    self.report({"ERROR"}, f"GeoScene error: {exc}")
                    return {"CANCELLED"}
            else:
                lat = settings.shadow_lat
                lon = settings.shadow_lon

            az, el = _shadow.solar_position(
                lat,
                lon,
                year,
                month,
                day,
                settings.shadow_hour_utc - settings.shadow_utc_offset,
            )

        if el <= 0.0:
            self.report(
                {"WARNING"},
                f"Sun is below the horizon (elevation {el:.1f}°) — lamp placed anyway",
            )

        lamp = _shadow.place_sun_lamp(az, el)
        self.report(
            {"INFO"}, f"Sun lamp '{lamp.name}' placed (az {az:.1f}°, el {el:.1f}°)"
        )
        return {"FINISHED"}


_classes = tuple(iter_operator_classes())


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
