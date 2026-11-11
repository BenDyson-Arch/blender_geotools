"""Layer presentation and variant tilesets, the ``bpy``-free half (design section 4).

A layer is made visible by *presenting* it: its values go through a colour ramp
(or one colour per category) into COLOR_0, or into an 8-bit base-colour or
emissive texture. A *variant tileset* is an ordinary 3D Tiles archive of the
source with one layer presented, written next to the plain one as
``<stem>.<layer>.3tz``.

This module holds the parts that need no Blender: the targets and blend modes,
the variant output name, the static checks of a presentation and the
``"variant"`` record of build.json. The colour maths is in ``core.ramps``; the
Blender side is ``geotools.blender.presentation``.

What each target and blend mode publishes (glTF core PBR only):

========== ================================================ =============================================
target     REPLACE                                          MULTIPLY
========== ================================================ =============================================
COLOR_0    base colour white, no base-colour texture;       the material is kept; COLOR_0 multiplies its
           COLOR_0 is the ramp                              base colour, as glTF defines
BASE_COLOR base-colour texture is the ramp, factor white,   base-colour texture is the ramp; the factor
           COLOR_0 dropped                                  and COLOR_0 are kept and multiply it
EMISSIVE   emissive texture is the ramp (factor 1), base    emissive texture is the ramp (factor 1),
           colour black with no texture and COLOR_0         added on top of the unchanged material
           dropped, so the ramp shows unlit
========== ================================================ =============================================
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

import numpy as np

from .issues import Issue, Severity
from .layers import ATTRIBUTE_PREFIX, DataType, Domain, LayerSpec, json_safe
from .ramps import PALETTES, ramp_stops

__all__ = [
    "PRESENT_ATTRIBUTE",
    "Blend",
    "Target",
    "presentation_issues",
    "targets_for",
    "variant_output",
    "variant_record",
]

PRESENT_ATTRIBUTE = ATTRIBUTE_PREFIX + "present"
"""The byte colour attribute (CORNER domain) that holds a COLOR_0 presentation."""

LEGEND_STOPS = 11
"""Entries in a variant record's ``ramp_stops``."""

# Issue codes of presentation_issues (the Blender side adds its own).
VARIANT_TARGET_INVALID = "variant-target-invalid"
VARIANT_PALETTE_INVALID = "variant-palette-invalid"
VARIANT_RANGE_INVALID = "variant-range-invalid"


class Target(StrEnum):
    COLOR_0 = "COLOR_0"  # FACE and CORNER layers: the gt_present colour attribute
    BASE_COLOR = "BASE_COLOR"  # TEXTURE layers: an 8-bit base-colour texture
    EMISSIVE = "EMISSIVE"  # TEXTURE layers: an 8-bit emissive texture


class Blend(StrEnum):
    REPLACE = "REPLACE"
    MULTIPLY = "MULTIPLY"


def targets_for(domain: Domain) -> tuple[Target, ...]:
    """The targets a layer of ``domain`` can be presented in."""
    if Domain(domain) is Domain.TEXTURE:
        return (Target.BASE_COLOR, Target.EMISSIVE)
    return (Target.COLOR_0,)


def variant_output(output: Path, layer: str) -> Path:
    """``<dir>/<stem>.<layer>.3tz`` for the plain output ``<dir>/<stem>.3tz``.

    Its staging directory is then ``<stem>.<layer>.geotools``, separate from the
    plain tileset's. Raises ``ValueError`` unless ``output`` is a ``.3tz`` path
    and ``layer`` a non-empty name without path separators or dots.
    """
    output = Path(output)
    if output.suffix != ".3tz" or not output.stem:
        raise ValueError(f"the output must be a .3tz file, got {str(output)!r}")
    if not layer or any(c in layer for c in "/\\.") or layer != layer.strip():
        raise ValueError(f"{layer!r} cannot be part of a file name")
    return output.with_name(f"{output.stem}.{layer}.3tz")


def presentation_issues(
    spec: LayerSpec,
    target: Target | str,
    palette: str,
    range_min: float | None,
    range_max: float | None,
) -> list[Issue]:
    """Problems with presenting ``spec`` this way that need no data to find.

    The range is the parsed ``range_min``/``range_max`` text (None for empty,
    which means auto). CATEGORY layers ignore the palette and range.
    """
    issues: list[Issue] = []
    subject = spec.name
    target = Target(target)
    allowed = targets_for(spec.domain)
    if target not in allowed:
        names = " or ".join(t.value for t in allowed)
        issues.append(
            Issue(
                Severity.ERROR,
                VARIANT_TARGET_INVALID,
                f"Layer {spec.name!r} is a {Domain(spec.domain).value} layer; present it as "
                f"{names}, not {target.value}.",
                subject,
            )
        )
    if DataType(spec.data_type) is DataType.CATEGORY:
        return issues
    if palette not in PALETTES:
        issues.append(
            Issue(
                Severity.ERROR,
                VARIANT_PALETTE_INVALID,
                f"Layer {spec.name!r}: unknown palette {palette!r}.",
                subject,
            )
        )
    for word, value in (("minimum", range_min), ("maximum", range_max)):
        if value is not None and not math.isfinite(value):
            issues.append(
                Issue(
                    Severity.ERROR,
                    VARIANT_RANGE_INVALID,
                    f"Layer {spec.name!r}: the ramp {word} must be a number (empty for auto).",
                    subject,
                )
            )
            return issues
    if range_min is not None and range_max is not None and range_min > range_max:
        issues.append(
            Issue(
                Severity.ERROR,
                VARIANT_RANGE_INVALID,
                f"Layer {spec.name!r}: the ramp minimum {range_min!r} is above the maximum "
                f"{range_max!r}.",
                subject,
            )
        )
    return issues


def _rgba_list(rgba: Sequence[int] | np.ndarray) -> list[int]:
    return [int(c) for c in np.asarray(rgba).ravel()[:4]]


def variant_record(
    spec: LayerSpec,
    target: Target | str,
    blend: Blend | str,
    *,
    palette: str | None = None,
    value_range: tuple[float, float] | None = None,
    range_auto: tuple[bool, bool] = (False, False),
    category_rgba: np.ndarray | None = None,
    nodata_rgba: Sequence[int] = (0, 0, 0, 0),
    stops: int = LEGEND_STOPS,
) -> dict[str, object]:
    """The ``"variant"`` object of build.json, JSON-safe, in a stable key order.

    Keys: ``layer``, ``target``, ``blend``, ``palette`` (null for CATEGORY),
    ``range`` (``{"min", "max", "auto_min", "auto_max"}``, null for CATEGORY),
    ``nodata_rgba``, and either ``ramp_stops`` (legend entries
    ``{"value", "rgba"}`` from ``core.ramps.ramp_stops``) or ``categories``
    (``{"value", "label", "rgba"}``). Colours are 8-bit sRGB RGBA lists.
    """
    record: dict[str, object] = {
        "layer": spec.name,
        "target": Target(target).value,
        "blend": Blend(blend).value,
    }
    if DataType(spec.data_type) is DataType.CATEGORY:
        if category_rgba is None:
            raise ValueError("a CATEGORY variant needs category_rgba")
        record["palette"] = None
        record["range"] = None
        record["nodata_rgba"] = _rgba_list(nodata_rgba)
        record["categories"] = [
            {"value": i, "label": label, "rgba": _rgba_list(category_rgba[i])}
            for i, label in enumerate(spec.categories)
        ]
        return record
    if palette is None or value_range is None:
        raise ValueError("a ramp variant needs a palette and a value range")
    low, high = (float(v) for v in value_range)
    record["palette"] = palette
    record["range"] = {
        "min": low,
        "max": high,
        "auto_min": bool(range_auto[0]),
        "auto_max": bool(range_auto[1]),
    }
    record["nodata_rgba"] = _rgba_list(nodata_rgba)
    record["ramp_stops"] = ramp_stops(palette, low, high, stops)
    return json_safe(record)  # type: ignore[return-value]
