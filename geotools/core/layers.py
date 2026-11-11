"""The layer registry: named, typed values on the source (see docs/design/m3-layers.md).

A layer holds one value per face, per face corner or per texel. This module is
the ``bpy``-free half: the :class:`LayerSpec` description, its validation, the
default coarse-level aggregation, and the JSON record written to build.json.
The Blender storage and the attribute helpers are in ``geotools.blender.layers``.

Text fields: Blender stores ``nodata`` as binary64 text and ``categories`` as a
newline-separated string. :func:`parse_nodata`, :func:`format_nodata`,
:func:`parse_categories` and :func:`format_categories` convert between those
and a :class:`LayerSpec`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from .issues import Issue, Severity

__all__ = [
    "ATTRIBUTE_PREFIX",
    "NAME_MAX_LENGTH",
    "RESERVED_NAMES",
    "VALID_AGGREGATIONS",
    "Aggregation",
    "DataType",
    "Domain",
    "LayerSpec",
    "Origin",
    "attribute_name",
    "default_aggregation",
    "effective_aggregation",
    "fill_value",
    "format_categories",
    "format_nodata",
    "from_record",
    "numpy_dtype",
    "parse_categories",
    "parse_nodata",
    "to_record",
    "validate",
]


class Domain(StrEnum):
    FACE = "FACE"  # one value per polygon
    CORNER = "CORNER"  # one value per face corner (loop)
    TEXTURE = "TEXTURE"  # one value per texel of a per-object image


class DataType(StrEnum):
    FLOAT = "FLOAT"
    INT = "INT"
    BOOLEAN = "BOOLEAN"
    CATEGORY = "CATEGORY"  # an INT index into LayerSpec.categories


class Origin(StrEnum):
    GEONODES = "GEONODES"
    ANALYSIS = "ANALYSIS"
    MANUAL = "MANUAL"


class Aggregation(StrEnum):
    """How coarse levels reduce a layer (recorded for the 0.5 annotate pass)."""

    AREA_MEAN = "AREA_MEAN"
    MIN = "MIN"
    MAX = "MAX"
    MODE = "MODE"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class LayerSpec:
    name: str  # identifier: [a-z][a-z0-9_]{0,47}, unique in the project
    domain: Domain
    data_type: DataType
    units: str = ""  # free text, e.g. "deg", "m", "h"
    nodata: float | None = None
    origin: Origin = Origin.MANUAL
    aggregation: Aggregation | None = None  # None -> default_aggregation(data_type)
    description: str = ""
    categories: tuple[str, ...] = ()  # CATEGORY only: label of value i


ATTRIBUTE_PREFIX = "gt_"
NAME_MAX_LENGTH = 48
_NAME_RE = re.compile(rf"[a-z][a-z0-9_]{{0,{NAME_MAX_LENGTH - 1}}}")

RESERVED_NAMES: frozenset[str] = frozenset({"present"})
"""Names whose ``gt_<name>`` attribute GeoTools uses itself (``gt_present`` is
the presentation colour attribute)."""

VALID_AGGREGATIONS: dict[DataType, frozenset[Aggregation]] = {
    DataType.FLOAT: frozenset(
        (Aggregation.AREA_MEAN, Aggregation.MIN, Aggregation.MAX, Aggregation.NONE)
    ),
    DataType.INT: frozenset(
        (
            Aggregation.AREA_MEAN,
            Aggregation.MIN,
            Aggregation.MAX,
            Aggregation.MODE,
            Aggregation.NONE,
        )
    ),
    # MIN is "all", MAX is "any".
    DataType.BOOLEAN: frozenset(
        (Aggregation.MODE, Aggregation.MIN, Aggregation.MAX, Aggregation.NONE)
    ),
    DataType.CATEGORY: frozenset((Aggregation.MODE, Aggregation.NONE)),
}

_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1


def attribute_name(spec_or_name: LayerSpec | str) -> str:
    """``"gt_" + name``: the mesh attribute that holds a FACE or CORNER layer."""
    name = spec_or_name.name if isinstance(spec_or_name, LayerSpec) else spec_or_name
    return ATTRIBUTE_PREFIX + name


def default_aggregation(data_type: DataType) -> Aggregation:
    """FLOAT and INT average by area; BOOLEAN and CATEGORY take the mode."""
    if DataType(data_type) in (DataType.FLOAT, DataType.INT):
        return Aggregation.AREA_MEAN
    return Aggregation.MODE


def effective_aggregation(spec: LayerSpec) -> Aggregation:
    """The spec's aggregation, or the default for its data type."""
    if spec.aggregation is None:
        return default_aggregation(spec.data_type)
    return Aggregation(spec.aggregation)


def numpy_dtype(data_type: DataType) -> np.dtype:
    """The numpy type of a layer's values in a Blender attribute."""
    data_type = DataType(data_type)
    if data_type is DataType.FLOAT:
        return np.dtype(np.float32)
    if data_type is DataType.BOOLEAN:
        return np.dtype(np.bool_)
    return np.dtype(np.int32)  # INT and CATEGORY


def fill_value(spec: LayerSpec) -> float | int | bool:
    """The value written where a layer has no data.

    ``nodata`` if set; otherwise NaN for FLOAT, 0 for INT and CATEGORY and
    False for BOOLEAN.
    """
    data_type = DataType(spec.data_type)
    if data_type is DataType.BOOLEAN:
        return False
    if spec.nodata is None:
        return math.nan if data_type is DataType.FLOAT else 0
    if data_type is DataType.FLOAT:
        return float(spec.nodata)
    return int(spec.nodata)


# --- text fields ----------------------------------------------------------------------


def parse_nodata(text: str) -> float | None:
    """The no-data value stored as binary64 text; empty text means none.

    Text that is not a number gives NaN, which :func:`validate` reports as
    ``layer-nodata-invalid``, so reading the registry never fails.
    """
    if not text.strip():
        return None
    try:
        return float(text)
    except ValueError:
        return math.nan


def format_nodata(value: float | None) -> str:
    """Exact text for ``value`` (``repr`` round-trips a binary64); "" for none."""
    return "" if value is None else repr(float(value))


def parse_categories(text: str) -> tuple[str, ...]:
    """Category labels from newline-separated text; blank text means none."""
    if not text.strip():
        return ()
    return tuple(label.strip() for label in text.splitlines())


def format_categories(categories: tuple[str, ...] | list[str]) -> str:
    return "\n".join(categories)


# --- validation -----------------------------------------------------------------------


def validate(spec: LayerSpec, existing_names: object = ()) -> list[Issue]:
    """Problems with ``spec``; ``existing_names`` are the names of the *other* layers."""
    issues: list[Issue] = []
    name = spec.name
    subject = name or None
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        issues.append(
            Issue(
                Severity.ERROR,
                "layer-name-invalid",
                f"Layer name {name!r} must start with a lowercase letter and use only "
                f"lowercase letters, digits and underscores, at most {NAME_MAX_LENGTH} "
                "characters.",
                subject,
            )
        )
    elif name in RESERVED_NAMES:
        issues.append(
            Issue(
                Severity.ERROR,
                "layer-name-invalid",
                f"Layer name {name!r} is reserved: GeoTools uses {attribute_name(name)!r} itself.",
                subject,
            )
        )
    if name in set(existing_names):  # type: ignore[call-overload]
        issues.append(
            Issue(
                Severity.ERROR,
                "layer-name-duplicate",
                f"Another layer is already named {name!r}.",
                subject,
            )
        )

    data_type = DataType(spec.data_type)
    aggregation = effective_aggregation(spec)
    if aggregation not in VALID_AGGREGATIONS[data_type]:
        allowed = ", ".join(sorted(a.value for a in VALID_AGGREGATIONS[data_type]))
        issues.append(
            Issue(
                Severity.ERROR,
                "layer-aggregation-invalid",
                f"Layer {name!r}: {aggregation.value} does not apply to a "
                f"{data_type.value} layer (use one of {allowed}).",
                subject,
            )
        )

    if data_type is DataType.CATEGORY:
        if not spec.categories:
            issues.append(
                Issue(
                    Severity.ERROR,
                    "layer-categories-missing",
                    f"Category layer {name!r} has no categories; give a label for each value.",
                    subject,
                )
            )
        else:
            blank = [i for i, label in enumerate(spec.categories) if not label.strip()]
            if blank:
                issues.append(
                    Issue(
                        Severity.ERROR,
                        "layer-categories-missing",
                        f"Category layer {name!r}: value {blank[0]} has no label.",
                        subject,
                    )
                )

    issues.extend(_nodata_issues(spec, data_type, subject))
    return issues


def _nodata_issues(spec: LayerSpec, data_type: DataType, subject: str | None) -> list[Issue]:
    nodata = spec.nodata
    if nodata is None:
        return []
    name = spec.name

    def issue(reason: str) -> list[Issue]:
        return [
            Issue(
                Severity.ERROR,
                "layer-nodata-invalid",
                f"Layer {name!r}: no-data {reason}.",
                subject,
            )
        ]

    try:
        value = float(nodata)
    except (TypeError, ValueError):
        return issue("is not a number")
    if not math.isfinite(value):
        return issue("must be a finite number (leave it empty for none)")
    if data_type is DataType.BOOLEAN:
        return issue("cannot be set on a BOOLEAN layer")
    if data_type in (DataType.INT, DataType.CATEGORY):
        if not value.is_integer():
            return issue(f"must be a whole number for a {data_type.value} layer, got {value!r}")
        if not _INT32_MIN <= value <= _INT32_MAX:
            return issue("must fit a 32-bit integer")
        if data_type is DataType.CATEGORY and 0 <= value < len(spec.categories):
            return issue(
                f"{int(value)} is a category value; use one outside 0 to "
                f"{len(spec.categories) - 1}, e.g. -1"
            )
    if data_type is DataType.FLOAT and abs(value) > float(np.finfo(np.float32).max):
        return issue("does not fit a 32-bit float")
    return []


# --- records ----------------------------------------------------------------------------


def to_record(spec: LayerSpec) -> dict[str, object]:
    """JSON-safe description of a layer for build.json, in a stable key order.

    ``aggregation`` is the effective one (the default when the spec has none), so
    a record states what applies. A non-finite ``nodata`` (an invalid spec) is
    written as null.
    """
    nodata = None
    if spec.nodata is not None and math.isfinite(float(spec.nodata)):
        nodata = float(spec.nodata)
    return {
        "name": spec.name,
        "attribute": attribute_name(spec) if Domain(spec.domain) is not Domain.TEXTURE else None,
        "domain": Domain(spec.domain).value,
        "data_type": DataType(spec.data_type).value,
        "units": spec.units,
        "nodata": nodata,
        "origin": Origin(spec.origin).value,
        "aggregation": effective_aggregation(spec).value,
        "description": spec.description,
        "categories": list(spec.categories),
    }


def from_record(d: dict[str, object]) -> LayerSpec:
    """The :class:`LayerSpec` of a :func:`to_record` record.

    ``attribute`` is derived from the name and ignored. Raises ``KeyError`` or
    ``ValueError`` for a record that is not a layer record.
    """
    nodata = d.get("nodata")
    aggregation = d.get("aggregation")
    return LayerSpec(
        name=str(d["name"]),
        domain=Domain(d["domain"]),
        data_type=DataType(d["data_type"]),
        units=str(d.get("units", "")),
        nodata=None if nodata is None else float(nodata),  # type: ignore[arg-type]
        origin=Origin(d.get("origin", Origin.MANUAL)),
        aggregation=None if aggregation is None else Aggregation(aggregation),
        description=str(d.get("description", "")),
        categories=tuple(str(c) for c in d.get("categories", ()) or ()),  # type: ignore[union-attr]
    )
