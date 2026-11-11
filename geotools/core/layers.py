"""The layer registry: named, typed values on the source (see docs/design/m3-layers.md).

A layer holds one value per face, per face corner or per texel. This module is
the ``bpy``-free half: the :class:`LayerSpec` description, its validation, the
default coarse-level aggregation, and the JSON record written to build.json.
The Blender storage and the attribute helpers are in ``geotools.blender.layers``.

Text fields: Blender stores ``nodata``, ``value_min`` and ``value_max`` as
binary64 text, ``categories`` as a newline-separated string and ``provenance``
as JSON. :func:`parse_number`/:func:`format_number` (``parse_nodata``/
``format_nodata`` are the same functions), :func:`parse_categories`/
:func:`format_categories` and :func:`parse_provenance`/:func:`format_provenance`
convert between those and a :class:`LayerSpec`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
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
    "display_name",
    "effective_aggregation",
    "fill_value",
    "format_categories",
    "format_nodata",
    "format_number",
    "format_provenance",
    "from_record",
    "json_safe",
    "make_provenance",
    "numpy_dtype",
    "parse_categories",
    "parse_nodata",
    "parse_number",
    "parse_provenance",
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
    label: str = ""  # display name; the UI shows label, or name if label is empty
    value_min: float | None = None  # range actually present, set by whoever writes values
    value_max: float | None = None
    # {"tool": str, "parameters": {...}, "geotools_version": str}, set by bakes and
    # analysis tools (see make_provenance); None for manual layers. A dict makes
    # the spec unhashable; specs are compared, never hashed.
    provenance: Mapping[str, object] | None = None


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


def display_name(spec: LayerSpec) -> str:
    """What the UI shows: the label, or the name if the label is empty."""
    return spec.label.strip() or spec.name


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


def parse_number(text: str) -> float | None:
    """An optional number stored as binary64 text; empty text means none.

    Text that is not a number gives NaN, which :func:`validate` reports
    (``layer-nodata-invalid`` or ``layer-range-invalid``), so reading the
    registry never fails.
    """
    if not text.strip():
        return None
    try:
        return float(text)
    except ValueError:
        return math.nan


def format_number(value: float | None) -> str:
    """Exact text for ``value`` (``repr`` round-trips a binary64); "" for none."""
    return "" if value is None else repr(float(value))


parse_nodata = parse_number
format_nodata = format_number


def parse_categories(text: str) -> tuple[str, ...]:
    """Category labels from newline-separated text; blank text means none."""
    if not text.strip():
        return ()
    return tuple(label.strip() for label in text.splitlines())


def format_categories(categories: tuple[str, ...] | list[str]) -> str:
    return "\n".join(categories)


def json_safe(value: object) -> object:
    """Plain JSON data: mappings with str keys, lists, finite floats, str, int, bool, None.

    Tuples become lists, numpy scalars and arrays become Python values and lists,
    non-finite floats become None and anything else its ``str``.
    """
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def make_provenance(
    tool: str, parameters: Mapping[str, object] | None = None, geotools_version: str = ""
) -> dict[str, object]:
    """The ``provenance`` a bake or analysis tool records, made JSON-safe."""
    return {
        "tool": str(tool),
        "parameters": json_safe(dict(parameters or {})),
        "geotools_version": str(geotools_version),
    }


def parse_provenance(text: str) -> Mapping[str, object] | None:
    """Provenance stored as JSON text; empty text means none.

    Text that is not a JSON object gives ``{"invalid": text}``, which
    :func:`validate` reports as ``layer-provenance-invalid``.
    """
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return {"invalid": text}
    return value if isinstance(value, dict) else {"invalid": text}


def format_provenance(provenance: Mapping[str, object] | None) -> str:
    """Compact, key-sorted JSON for ``provenance``; "" for none."""
    if provenance is None:
        return ""
    return json.dumps(json_safe(provenance), sort_keys=True, allow_nan=False)


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
    issues.extend(_range_issues(spec, subject))
    issues.extend(_provenance_issues(spec, subject))
    return issues


def _range_issues(spec: LayerSpec, subject: str | None) -> list[Issue]:
    bounds = {"minimum": spec.value_min, "maximum": spec.value_max}
    for word, value in bounds.items():
        if value is not None and not math.isfinite(float(value)):
            return [
                Issue(
                    Severity.ERROR,
                    "layer-range-invalid",
                    f"Layer {spec.name!r}: the value {word} must be a finite number "
                    "(empty when not known).",
                    subject,
                )
            ]
    low, high = spec.value_min, spec.value_max
    if low is not None and high is not None and float(low) > float(high):
        return [
            Issue(
                Severity.ERROR,
                "layer-range-invalid",
                f"Layer {spec.name!r}: the value minimum {low!r} is above the maximum {high!r}.",
                subject,
            )
        ]
    return []


def _provenance_issues(spec: LayerSpec, subject: str | None) -> list[Issue]:
    provenance = spec.provenance
    if provenance is None:
        return []
    ok = (
        isinstance(provenance, Mapping)
        and isinstance(provenance.get("tool"), str)
        and bool(provenance["tool"].strip())  # type: ignore[union-attr]
        and isinstance(provenance.get("parameters", {}), Mapping)
        and isinstance(provenance.get("geotools_version", ""), str)
    )
    if ok:
        return []
    return [
        Issue(
            Severity.ERROR,
            "layer-provenance-invalid",
            f"Layer {spec.name!r}: provenance must be a JSON object with a tool name, "
            'e.g. {"tool": "slope", "parameters": {}, "geotools_version": "0.3.0"}.',
            subject,
        )
    ]


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
    a record states what applies. ``categories`` is a list of
    ``{"value": i, "label": label}``. Non-finite numbers (an invalid spec) are
    written as null.
    """
    return {
        "name": spec.name,
        "label": spec.label,
        "attribute": attribute_name(spec) if Domain(spec.domain) is not Domain.TEXTURE else None,
        "domain": Domain(spec.domain).value,
        "data_type": DataType(spec.data_type).value,
        "units": spec.units,
        "nodata": _finite_or_none(spec.nodata),
        "value_min": _finite_or_none(spec.value_min),
        "value_max": _finite_or_none(spec.value_max),
        "origin": Origin(spec.origin).value,
        "aggregation": effective_aggregation(spec).value,
        "description": spec.description,
        "categories": [{"value": i, "label": label} for i, label in enumerate(spec.categories)],
        "provenance": None if spec.provenance is None else json_safe(spec.provenance),
    }


def _finite_or_none(value: float | None) -> float | None:
    if value is None or not math.isfinite(float(value)):
        return None
    return float(value)


def _categories_from_record(items: object) -> tuple[str, ...]:
    """Labels from ``[{"value": i, "label": ...}]`` (any order) or a list of labels."""
    items = list(items or ())  # type: ignore[call-overload]
    if not items or not isinstance(items[0], Mapping):
        return tuple(str(label) for label in items)
    by_value = {int(item["value"]): str(item["label"]) for item in items}
    if sorted(by_value) != list(range(len(items))):
        raise ValueError(f"category values must be 0 to {len(items) - 1}, got {sorted(by_value)}")
    return tuple(by_value[i] for i in range(len(items)))


def from_record(d: dict[str, object]) -> LayerSpec:
    """The :class:`LayerSpec` of a :func:`to_record` record.

    ``attribute`` is derived from the name and ignored. ``categories`` may be the
    ``{"value", "label"}`` objects or plain labels. Raises ``KeyError`` or
    ``ValueError`` for a record that is not a layer record.
    """

    def number(key: str) -> float | None:
        value = d.get(key)
        return None if value is None else float(value)  # type: ignore[arg-type]

    aggregation = d.get("aggregation")
    provenance = d.get("provenance")
    return LayerSpec(
        name=str(d["name"]),
        domain=Domain(d["domain"]),
        data_type=DataType(d["data_type"]),
        units=str(d.get("units", "")),
        nodata=number("nodata"),
        origin=Origin(d.get("origin", Origin.MANUAL)),
        aggregation=None if aggregation is None else Aggregation(aggregation),
        description=str(d.get("description", "")),
        categories=_categories_from_record(d.get("categories", ())),
        label=str(d.get("label", "")),
        value_min=number("value_min"),
        value_max=number("value_max"),
        provenance=None if provenance is None else dict(provenance),  # type: ignore[call-overload]
    )
