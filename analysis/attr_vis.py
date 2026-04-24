"""
analysis/attr_vis.py — Map face attributes to colour attributes for viewport display.

Two mapping modes:

  RAMP        — Normalises a numeric (FLOAT / INT / INT8 / BOOLEAN) FACE
                attribute to [0, 1] then samples a built-in colour ramp.
                The value range can be set manually or auto-detected from
                the data.

  CATEGORICAL — Treats each distinct integer or float value as a labelled
                category and assigns it a visually distinct hue.  Useful for
                feature-type attributes (land-use codes, surface classes)
                where the raw numbers are labels, not measurements.

                AUTO mode chooses RAMP for FLOAT / BOOLEAN attributes and
                CATEGORICAL for INT / INT8 attributes with ≤ 16 distinct
                values; otherwise falls back to RAMP.

Output is a CORNER-domain BYTE_COLOR attribute (Blender's native "Color
Attribute" type).  Writing to CORNER domain means every face corner is given
the same colour as its parent face, producing flat per-face shading.  The
new attribute is set as the active Color Attribute so the 3D Viewport
immediately shows it under Solid → Color → Attribute.
"""

from __future__ import annotations

import colorsys
import logging
from typing import Dict, List, Optional, Tuple

import bpy

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Built-in colour ramps
# Each ramp is a list of (t, r, g, b) control points, t in [0, 1].
# ---------------------------------------------------------------------------

RAMP_NAMES = ["VIRIDIS", "PLASMA", "INFERNO", "COOL_WARM", "GREYSCALE"]

_RAMPS: Dict[str, List[Tuple[float, float, float, float]]] = {
    "VIRIDIS": [
        (0.000, 0.267, 0.004, 0.329),
        (0.125, 0.279, 0.175, 0.483),
        (0.250, 0.230, 0.322, 0.545),
        (0.375, 0.173, 0.455, 0.558),
        (0.500, 0.128, 0.568, 0.551),
        (0.625, 0.157, 0.684, 0.498),
        (0.750, 0.365, 0.788, 0.384),
        (0.875, 0.643, 0.862, 0.196),
        (1.000, 0.993, 0.906, 0.144),
    ],
    "PLASMA": [
        (0.000, 0.050, 0.030, 0.527),
        (0.125, 0.292, 0.016, 0.570),
        (0.250, 0.488, 0.012, 0.580),
        (0.375, 0.648, 0.061, 0.543),
        (0.500, 0.798, 0.178, 0.470),
        (0.625, 0.908, 0.318, 0.372),
        (0.750, 0.970, 0.482, 0.253),
        (0.875, 0.990, 0.659, 0.133),
        (1.000, 0.940, 0.975, 0.131),
    ],
    "INFERNO": [
        (0.000, 0.000, 0.000, 0.016),
        (0.125, 0.130, 0.025, 0.200),
        (0.250, 0.330, 0.035, 0.360),
        (0.375, 0.524, 0.058, 0.380),
        (0.500, 0.711, 0.163, 0.273),
        (0.625, 0.854, 0.334, 0.162),
        (0.750, 0.948, 0.556, 0.106),
        (0.875, 0.985, 0.800, 0.294),
        (1.000, 0.988, 0.998, 0.644),
    ],
    "COOL_WARM": [
        (0.000, 0.231, 0.298, 0.752),
        (0.250, 0.552, 0.690, 0.996),
        (0.500, 0.865, 0.865, 0.865),
        (0.750, 0.957, 0.603, 0.486),
        (1.000, 0.706, 0.016, 0.149),
    ],
    "GREYSCALE": [
        (0.000, 0.000, 0.000, 0.000),
        (1.000, 1.000, 1.000, 1.000),
    ],
}

_NUMERIC_TYPES = {"FLOAT", "INT", "INT8", "BOOLEAN"}

# INT/INT8 attributes with more distinct values than this are ramp-mapped
CATEGORICAL_AUTO_THRESHOLD = 16


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sample_ramp(
    points: List[Tuple[float, float, float, float]],
    t: float,
) -> Tuple[float, float, float]:
    """Piecewise-linear colour ramp sample. t is clamped to [0, 1]."""
    t = max(0.0, min(1.0, t))
    if t <= points[0][0]:
        return points[0][1], points[0][2], points[0][3]
    if t >= points[-1][0]:
        return points[-1][1], points[-1][2], points[-1][3]
    for i in range(len(points) - 1):
        t0, r0, g0, b0 = points[i]
        t1, r1, g1, b1 = points[i + 1]
        if t0 <= t <= t1:
            a = (t - t0) / (t1 - t0)
            return r0 + a * (r1 - r0), g0 + a * (g1 - g0), b0 + a * (b1 - b0)
    return points[-1][1], points[-1][2], points[-1][3]


def _categorical_palette(n: int) -> List[Tuple[float, float, float]]:
    """Generate *n* visually-distinct RGB colours using evenly-spaced HSV hues."""
    if n == 0:
        return []
    if n == 1:
        return [(0.12, 0.47, 0.71)]  # single category → steel blue
    return [colorsys.hsv_to_rgb(i / n, 0.75, 0.90) for i in range(n)]


def _read_numeric_face_values(
    attr: bpy.types.Attribute,
    n_faces: int,
) -> List[float]:
    """Return a list of float values from a numeric FACE attribute."""
    data_type = attr.data_type
    if data_type == "BOOLEAN":
        return [1.0 if attr.data[i].value else 0.0 for i in range(n_faces)]
    return [float(attr.data[i].value) for i in range(n_faces)]


def _ensure_color_attr(mesh: bpy.types.Mesh, name: str) -> bpy.types.Attribute:
    """Return a CORNER BYTE_COLOR attribute, recreating it if the type differs."""
    if name in mesh.attributes:
        existing = mesh.attributes[name]
        if existing.domain == "CORNER" and existing.data_type == "BYTE_COLOR":
            return existing
        mesh.attributes.remove(existing)
    return mesh.attributes.new(name=name, type="BYTE_COLOR", domain="CORNER")


def _set_active_color(mesh: bpy.types.Mesh, attr: bpy.types.Attribute) -> None:
    """Set *attr* as the active Color Attribute, ignoring errors silently."""
    try:
        mesh.color_attributes.active_color = attr
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def detect_mode(obj: bpy.types.Object, attr_name: str) -> str:
    """Return ``"RAMP"`` or ``"CATEGORICAL"`` for AUTO mode resolution.

    Rules:
    - FLOAT / BOOLEAN → ``"RAMP"``
    - INT / INT8 with ≤ ``CATEGORICAL_AUTO_THRESHOLD`` distinct values → ``"CATEGORICAL"``
    - INT / INT8 with more → ``"RAMP"``
    """
    mesh = obj.data
    if attr_name not in mesh.attributes:
        return "RAMP"
    attr = mesh.attributes[attr_name]
    if attr.data_type in ("FLOAT", "BOOLEAN"):
        return "RAMP"
    if attr.data_type in ("INT", "INT8"):
        n = len(mesh.polygons)
        distinct = {attr.data[i].value for i in range(n)}
        return "CATEGORICAL" if len(distinct) <= CATEGORICAL_AUTO_THRESHOLD else "RAMP"
    return "RAMP"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def colourise_ramp(
    obj: bpy.types.Object,
    src_attr_name: str,
    dst_attr_name: str,
    ramp_name: str = "VIRIDIS",
    invert: bool = False,
    range_min: Optional[float] = None,
    range_max: Optional[float] = None,
) -> Tuple[int, float, float]:
    """Map a numeric FACE attribute through a colour ramp.

    Parameters
    ----------
    obj:
        Target mesh object.
    src_attr_name:
        FLOAT / INT / INT8 / BOOLEAN FACE attribute to read.
    dst_attr_name:
        Name for the output BYTE_COLOR CORNER attribute.
    ramp_name:
        One of: ``VIRIDIS``, ``PLASMA``, ``INFERNO``, ``COOL_WARM``, ``GREYSCALE``.
    invert:
        When True, flip the ramp so high values map to the dark/cool end.
    range_min / range_max:
        Explicit clamp range.  Either or both may be ``None`` to auto-detect
        from the data.

    Returns
    -------
    (face_count, effective_min, effective_max)
    """
    mesh = obj.data
    if src_attr_name not in mesh.attributes:
        raise ValueError(f"'{obj.name}' has no attribute '{src_attr_name}'")
    src_attr = mesh.attributes[src_attr_name]
    if src_attr.domain != "FACE":
        raise ValueError(f"Attribute '{src_attr_name}' is not FACE-domain")
    if src_attr.data_type not in _NUMERIC_TYPES:
        raise ValueError(
            f"Attribute '{src_attr_name}' type '{src_attr.data_type}' is not numeric; "
            "expected FLOAT, INT, INT8, or BOOLEAN"
        )

    n_faces = len(mesh.polygons)
    if n_faces == 0:
        return 0, 0.0, 0.0

    values = _read_numeric_face_values(src_attr, n_faces)

    v_min = range_min if range_min is not None else min(values)
    v_max = range_max if range_max is not None else max(values)
    span  = v_max - v_min
    if abs(span) < 1e-12:
        # All identical — show midpoint colour for all faces
        span = 1.0
        v_min -= 0.5

    ramp_points = _RAMPS.get(ramp_name, _RAMPS["VIRIDIS"])
    dst_attr = _ensure_color_attr(mesh, dst_attr_name)

    for i, face in enumerate(mesh.polygons):
        t = (values[i] - v_min) / span
        t = max(0.0, min(1.0, t))
        if invert:
            t = 1.0 - t
        r, g, b = _sample_ramp(ramp_points, t)
        for loop_idx in range(face.loop_start, face.loop_start + face.loop_total):
            dst_attr.data[loop_idx].color = (r, g, b, 1.0)

    _set_active_color(mesh, dst_attr)
    # Restore the real v_min in case we nudged it
    v_min_real = range_min if range_min is not None else min(values)
    return n_faces, v_min_real, v_max


def colourise_categorical(
    obj: bpy.types.Object,
    src_attr_name: str,
    dst_attr_name: str,
) -> Tuple[int, Dict]:
    """Assign a distinct hue to each distinct value in a numeric FACE attribute.

    Parameters
    ----------
    obj:
        Target mesh object.
    src_attr_name:
        Numeric FACE attribute whose values are treated as category labels.
    dst_attr_name:
        Name for the output BYTE_COLOR CORNER attribute.

    Returns
    -------
    (face_count, value_to_color)
        ``value_to_color`` maps each distinct value (as ``float``) to an
        ``(r, g, b)`` tuple — useful for building a legend.
    """
    mesh = obj.data
    if src_attr_name not in mesh.attributes:
        raise ValueError(f"'{obj.name}' has no attribute '{src_attr_name}'")
    src_attr = mesh.attributes[src_attr_name]
    if src_attr.domain != "FACE":
        raise ValueError(f"Attribute '{src_attr_name}' is not FACE-domain")
    if src_attr.data_type not in _NUMERIC_TYPES:
        raise ValueError(
            f"Attribute '{src_attr_name}' type '{src_attr.data_type}' is not numeric"
        )

    n_faces = len(mesh.polygons)
    if n_faces == 0:
        return 0, {}

    values = _read_numeric_face_values(src_attr, n_faces)
    unique  = sorted(set(values))
    palette = _categorical_palette(len(unique))
    value_to_color: Dict[float, Tuple[float, float, float]] = {
        v: palette[i] for i, v in enumerate(unique)
    }

    dst_attr = _ensure_color_attr(mesh, dst_attr_name)

    for i, face in enumerate(mesh.polygons):
        r, g, b = value_to_color[values[i]]
        for loop_idx in range(face.loop_start, face.loop_start + face.loop_total):
            dst_attr.data[loop_idx].color = (r, g, b, 1.0)

    _set_active_color(mesh, dst_attr)
    return n_faces, value_to_color


# ---------------------------------------------------------------------------
# Object custom-property colouring
# ---------------------------------------------------------------------------

def _coerce_value(raw) -> Optional[float]:
    """Convert a Blender custom-property value to float, or None if unsupported."""
    if isinstance(raw, bool):
        return 1.0 if raw else 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def detect_mode_from_values(values: List[float]) -> str:
    """Like :func:`detect_mode` but operates on an already-collected value list."""
    distinct = set(values)
    # If every value is exactly 0 or 1 it could be boolean — use ramp
    if distinct <= {0.0, 1.0}:
        return "RAMP"
    # All values are integers and ≤ threshold distinct values → categorical
    if all(v == int(v) for v in values) and len(distinct) <= CATEGORICAL_AUTO_THRESHOLD:
        return "CATEGORICAL"
    return "RAMP"


def _paint_uniform(
    obj: bpy.types.Object,
    dst_attr_name: str,
    color: Tuple[float, float, float],
) -> None:
    """Paint every loop of *obj*'s mesh a single colour."""
    mesh = obj.data
    n_faces = len(mesh.polygons)
    if n_faces == 0:
        return
    dst_attr = _ensure_color_attr(mesh, dst_attr_name)
    r, g, b = color
    for face in mesh.polygons:
        for loop_idx in range(face.loop_start, face.loop_start + face.loop_total):
            dst_attr.data[loop_idx].color = (r, g, b, 1.0)
    _set_active_color(mesh, dst_attr)


def colourise_objects_by_property(
    objects: List[bpy.types.Object],
    prop_name: str,
    dst_attr_name: str,
    mode: str = "AUTO",
    ramp_name: str = "VIRIDIS",
    invert: bool = False,
    range_min: Optional[float] = None,
    range_max: Optional[float] = None,
) -> Tuple[int, Dict[str, object]]:
    """Paint each object's mesh a flat colour derived from an object custom property.

    For each object in *objects*, reads ``obj[prop_name]`` (or
    ``obj.get(prop_name)``) and maps the value to a colour.  The colour is
    applied uniformly to every loop of the object's mesh so the entire object
    appears as a single flat colour in the viewport.

    This is the recommended way to visualise per-object results such as the
    ``los_visible`` boolean written by the Line-of-Sight operator.

    Parameters
    ----------
    objects:
        Mesh objects to process.  Objects missing the property are skipped
        and reported in the returned ``skipped`` list.
    prop_name:
        Name of the custom property to read (e.g. ``"los_visible"``).
    dst_attr_name:
        Output BYTE_COLOR CORNER attribute name.
    mode:
        ``"AUTO"``, ``"RAMP"``, or ``"CATEGORICAL"``.  AUTO: follow
        :func:`detect_mode_from_values`.
    ramp_name:
        Ramp to use in RAMP mode.
    invert:
        Flip the ramp direction.
    range_min / range_max:
        Manual value range for RAMP mode.  ``None`` = auto-detect.

    Returns
    -------
    (painted_count, info)
        ``info`` is a dict with keys:

        * ``"skipped"`` — list of object names that lacked the property
        * ``"legend"``  — for CATEGORICAL / string mode: dict mapping value → (r,g,b)
        * ``"range"``   — for RAMP mode: ``(v_min, v_max)``
    """
    # Collect raw values — check existence via .keys() so we distinguish
    # "property is missing" from "property has a falsy value (0, False, '')"
    obj_raws: List[Tuple[bpy.types.Object, object]] = []
    skipped: List[str] = []
    for obj in objects:
        if prop_name in obj.keys():
            obj_raws.append((obj, obj[prop_name]))
        else:
            skipped.append(obj.name)

    if not obj_raws:
        return 0, {"skipped": skipped}

    raws = [v for _, v in obj_raws]

    # ── String properties → always categorical ────────────────────────
    if all(isinstance(v, str) for v in raws):
        unique_strs = sorted(set(raws))
        palette     = _categorical_palette(len(unique_strs))
        str_to_color: Dict[str, Tuple[float, float, float]] = {
            s: palette[i] for i, s in enumerate(unique_strs)
        }
        for obj, val in obj_raws:
            _paint_uniform(obj, dst_attr_name, str_to_color[str(val)])
        return len(obj_raws), {"skipped": skipped, "legend": str_to_color}

    # ── Numeric properties ────────────────────────────────────────────
    obj_values: List[Tuple[bpy.types.Object, float]] = []
    for obj, raw in obj_raws:
        val = _coerce_value(raw)
        if val is None:
            skipped.append(obj.name)
        else:
            obj_values.append((obj, val))

    if not obj_values:
        return 0, {"skipped": skipped}

    values_only = [v for _, v in obj_values]

    # Resolve mode
    effective = detect_mode_from_values(values_only) if mode == "AUTO" else mode

    painted = 0

    if effective == "CATEGORICAL":
        unique  = sorted(set(values_only))
        palette = _categorical_palette(len(unique))
        value_to_color: Dict[float, Tuple[float, float, float]] = {
            v: palette[i] for i, v in enumerate(unique)
        }
        for obj, val in obj_values:
            _paint_uniform(obj, dst_attr_name, value_to_color[val])
            painted += 1
        return painted, {"skipped": skipped, "legend": value_to_color}

    else:  # RAMP
        v_min = range_min if range_min is not None else min(values_only)
        v_max = range_max if range_max is not None else max(values_only)
        span  = v_max - v_min
        if abs(span) < 1e-12:
            span = 1.0
            v_min -= 0.5

        ramp_points = _RAMPS.get(ramp_name, _RAMPS["VIRIDIS"])

        for obj, val in obj_values:
            t = (val - v_min) / span
            t = max(0.0, min(1.0, t))
            if invert:
                t = 1.0 - t
            color = _sample_ramp(ramp_points, t)
            _paint_uniform(obj, dst_attr_name, color)
            painted += 1

        v_min_real = range_min if range_min is not None else min(values_only)
        return painted, {"skipped": skipped, "range": (v_min_real, v_max)}
