"""The layer registry's bpy-free half: validation, aggregation and records."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from geotools.core import layers as L
from geotools.core.issues import Severity
from geotools.core.layers import Aggregation, DataType, Domain, LayerSpec, Origin


def spec(**kwargs: object) -> LayerSpec:
    base: dict[str, object] = {"name": "slope", "domain": Domain.FACE, "data_type": DataType.FLOAT}
    base.update(kwargs)
    return LayerSpec(**base)  # type: ignore[arg-type]


def codes(issues: list) -> list[str]:
    return [i.code for i in issues]


# --- names ------------------------------------------------------------------------------


def test_attribute_name() -> None:
    assert L.attribute_name("slope") == "gt_slope"
    assert L.attribute_name(spec(name="sun_hours")) == "gt_sun_hours"
    assert L.ATTRIBUTE_PREFIX == "gt_"


def test_effective_attribute_name() -> None:
    assert L.effective_attribute_name(spec()) == "gt_slope"
    assert L.effective_attribute_name(spec(source_attribute="   ")) == "gt_slope"
    assert L.effective_attribute_name(spec(source_attribute="slope")) == "slope"
    assert L.effective_attribute_name(spec(source_attribute="Slope (deg)")) == "Slope (deg)"


@pytest.mark.parametrize("source", ["slope", "Slope (deg)", "gt_other", "material_index"])
def test_valid_source_attributes(source: str) -> None:
    assert L.validate(spec(source_attribute=source)) == []


@pytest.mark.parametrize("source", [" slope", "slope ", ".corner_vert", "gt_present", "x" * 64])
def test_invalid_source_attributes(source: str) -> None:
    issues = L.validate(spec(source_attribute=source))
    assert codes(issues) == ["layer-attribute-invalid"], issues
    assert issues[0].severity is Severity.ERROR
    # A texture layer has no attribute, so the field is ignored.
    assert L.validate(spec(domain=Domain.TEXTURE, source_attribute=source)) == []


def test_source_attribute_record() -> None:
    record = L.to_record(spec(source_attribute="slope"))
    assert record["attribute"] == "slope"
    assert L.from_record(record).source_attribute == "slope"
    assert L.to_record(L.from_record(record)) == record
    # The default name round-trips as the default.
    assert L.from_record(L.to_record(spec())).source_attribute == ""
    assert L.from_record(dict(L.to_record(spec()), attribute=None)).source_attribute == ""


@pytest.mark.parametrize("name", ["a", "slope", "sun_hours_2", "x" * 48, "a_"])
def test_valid_names(name: str) -> None:
    assert L.validate(spec(name=name)) == []


@pytest.mark.parametrize(
    "name",
    ["", "Slope", "2slope", "_slope", "slope-deg", "slope deg", "x" * 49, "sløpe", "present"],
)
def test_invalid_names(name: str) -> None:
    issues = L.validate(spec(name=name))
    assert codes(issues) == ["layer-name-invalid"], issues
    assert issues[0].severity is Severity.ERROR


def test_duplicate_name() -> None:
    assert codes(L.validate(spec(), existing_names=["aspect", "slope"])) == ["layer-name-duplicate"]
    assert L.validate(spec(), existing_names={"aspect"}) == []
    # A generator works too.
    assert codes(L.validate(spec(), existing_names=(n for n in ["slope"]))) == [
        "layer-name-duplicate"
    ]


# --- aggregation --------------------------------------------------------------------------


def test_default_aggregation() -> None:
    assert L.default_aggregation(DataType.FLOAT) is Aggregation.AREA_MEAN
    assert L.default_aggregation(DataType.INT) is Aggregation.AREA_MEAN
    assert L.default_aggregation(DataType.BOOLEAN) is Aggregation.MODE
    assert L.default_aggregation(DataType.CATEGORY) is Aggregation.MODE
    assert L.default_aggregation("FLOAT") is Aggregation.AREA_MEAN  # type: ignore[arg-type]
    assert L.effective_aggregation(spec()) is Aggregation.AREA_MEAN
    assert L.effective_aggregation(spec(aggregation=Aggregation.MAX)) is Aggregation.MAX


def test_default_aggregations_are_valid() -> None:
    for data_type in DataType:
        assert L.default_aggregation(data_type) in L.VALID_AGGREGATIONS[data_type]
        assert Aggregation.NONE in L.VALID_AGGREGATIONS[data_type]


@pytest.mark.parametrize(
    ("data_type", "aggregation"),
    [
        (DataType.CATEGORY, Aggregation.AREA_MEAN),
        (DataType.CATEGORY, Aggregation.MIN),
        (DataType.BOOLEAN, Aggregation.AREA_MEAN),
        (DataType.FLOAT, Aggregation.MODE),
    ],
)
def test_invalid_aggregation(data_type: DataType, aggregation: Aggregation) -> None:
    s = spec(data_type=data_type, aggregation=aggregation, categories=("a",))
    assert codes(L.validate(s)) == ["layer-aggregation-invalid"]


# --- categories ---------------------------------------------------------------------------


def test_categories() -> None:
    assert codes(L.validate(spec(data_type=DataType.CATEGORY))) == ["layer-categories-missing"]
    blank = spec(data_type=DataType.CATEGORY, categories=("low", " ", "high"))
    issues = L.validate(blank)
    assert codes(issues) == ["layer-categories-missing"]
    assert "value 1" in issues[0].message
    assert L.validate(spec(data_type=DataType.CATEGORY, categories=("low", "high"))) == []
    # Categories on another type are ignored.
    assert L.validate(spec(categories=("a",))) == []


def test_category_text_round_trip() -> None:
    assert L.parse_categories("") == ()
    assert L.parse_categories(" \n") == ()
    assert L.parse_categories("low\nmid \n high\n") == ("low", "mid", "high")
    assert L.parse_categories("a\n\nb") == ("a", "", "b")  # a blank label is kept, and flagged
    assert L.format_categories(("low", "high")) == "low\nhigh"
    labels = ("bare ground", "grass", "trees")
    assert L.parse_categories(L.format_categories(labels)) == labels


# --- no-data ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "nodata", "categories"),
    [
        (DataType.FLOAT, -9999.0, ()),
        (DataType.FLOAT, 0.0, ()),
        (DataType.INT, -1.0, ()),
        (DataType.INT, float(2**31 - 1), ()),
        (DataType.CATEGORY, -1.0, ("a", "b")),
        (DataType.CATEGORY, 2.0, ("a", "b")),
    ],
)
def test_valid_nodata(data_type: DataType, nodata: float, categories: tuple[str, ...]) -> None:
    assert L.validate(spec(data_type=data_type, nodata=nodata, categories=categories)) == []


@pytest.mark.parametrize(
    ("data_type", "nodata", "categories"),
    [
        (DataType.FLOAT, math.nan, ()),
        (DataType.FLOAT, math.inf, ()),
        (DataType.FLOAT, 1e300, ()),
        (DataType.INT, 1.5, ()),
        (DataType.INT, float(2**31), ()),
        (DataType.BOOLEAN, 0.0, ()),
        (DataType.CATEGORY, 1.0, ("a", "b")),
    ],
)
def test_invalid_nodata(data_type: DataType, nodata: float, categories: tuple[str, ...]) -> None:
    issues = L.validate(spec(data_type=data_type, nodata=nodata, categories=categories))
    assert codes(issues) == ["layer-nodata-invalid"], issues


def test_nodata_text() -> None:
    assert L.parse_nodata("") is None
    assert L.parse_nodata("  ") is None
    assert L.parse_nodata("-9999") == -9999.0
    assert math.isnan(L.parse_nodata("abc"))  # type: ignore[arg-type]
    value = 0.1 + 0.2
    assert L.parse_nodata(L.format_nodata(value)) == value
    assert L.format_nodata(None) == ""
    assert codes(L.validate(spec(nodata=L.parse_nodata("abc")))) == ["layer-nodata-invalid"]


def test_several_issues_at_once() -> None:
    s = spec(name="Bad", data_type=DataType.CATEGORY, aggregation=Aggregation.MAX, nodata=0.5)
    assert codes(L.validate(s, ["Bad"])) == [
        "layer-name-invalid",
        "layer-name-duplicate",
        "layer-aggregation-invalid",
        "layer-categories-missing",
        "layer-nodata-invalid",
    ]


# --- values -------------------------------------------------------------------------------


def test_numpy_dtype_and_fill_value() -> None:
    assert L.numpy_dtype(DataType.FLOAT) == np.float32
    assert L.numpy_dtype(DataType.INT) == np.int32
    assert L.numpy_dtype(DataType.CATEGORY) == np.int32
    assert L.numpy_dtype(DataType.BOOLEAN) == np.bool_
    assert math.isnan(L.fill_value(spec()))  # type: ignore[arg-type]
    assert L.fill_value(spec(nodata=-9999.0)) == -9999.0
    assert L.fill_value(spec(data_type=DataType.INT)) == 0
    fill = L.fill_value(spec(data_type=DataType.INT, nodata=-1.0))
    assert fill == -1 and isinstance(fill, int)
    assert L.fill_value(spec(data_type=DataType.BOOLEAN)) is False


# --- records ------------------------------------------------------------------------------

RECORD_KEYS = [
    "name",
    "label",
    "attribute",
    "domain",
    "data_type",
    "units",
    "nodata",
    "value_min",
    "value_max",
    "origin",
    "aggregation",
    "description",
    "categories",
    "provenance",
]


def test_record_has_stable_keys_and_is_json() -> None:
    s = spec(
        name="land_cover",
        domain=Domain.CORNER,
        data_type=DataType.CATEGORY,
        units="",
        nodata=-1.0,
        origin=Origin.ANALYSIS,
        description="Classified cover",
        categories=("ground", "grass", "trees"),
        label="Land cover",
        value_min=0.0,
        value_max=2.0,
        provenance=L.make_provenance("classify", {"bands": (1, 2)}, "0.3.0"),
    )
    record = L.to_record(s)
    assert list(record) == RECORD_KEYS
    assert record == {
        "name": "land_cover",
        "label": "Land cover",
        "attribute": "gt_land_cover",
        "domain": "CORNER",
        "data_type": "CATEGORY",
        "units": "",
        "nodata": -1.0,
        "value_min": 0.0,
        "value_max": 2.0,
        "origin": "ANALYSIS",
        "aggregation": "MODE",  # the default, written out
        "description": "Classified cover",
        "categories": [
            {"value": 0, "label": "ground"},
            {"value": 1, "label": "grass"},
            {"value": 2, "label": "trees"},
        ],
        "provenance": {
            "tool": "classify",
            "parameters": {"bands": [1, 2]},
            "geotools_version": "0.3.0",
        },
    }
    text = json.dumps(record, allow_nan=False)
    assert L.from_record(json.loads(text)) == L.from_record(record)
    assert list(L.to_record(s)) == RECORD_KEYS  # the same order every time


def test_record_round_trip() -> None:
    specs = [
        spec(aggregation=Aggregation.AREA_MEAN, units="deg", nodata=-9999.0),
        spec(
            name="lit",
            domain=Domain.TEXTURE,
            data_type=DataType.BOOLEAN,
            aggregation=Aggregation.MAX,
            origin=Origin.GEONODES,
        ),
        spec(
            name="cover",
            data_type=DataType.CATEGORY,
            aggregation=Aggregation.NONE,
            categories=("a", "b"),
            nodata=-1.0,
        ),
        spec(nodata=0.1 + 0.2, aggregation=Aggregation.MIN, description="ünïcode"),
        spec(
            aggregation=Aggregation.AREA_MEAN,
            label="Slope (degrees)",
            value_min=0.1 + 0.2,
            value_max=89.5,
            provenance=L.make_provenance("slope", {"smooth": 2.5, "mode": "fast"}, "0.3.0"),
        ),
    ]
    for s in specs:
        record = L.to_record(s)
        assert L.from_record(record) == s
        assert L.from_record(json.loads(json.dumps(record))) == s
        assert L.to_record(L.from_record(record)) == record


def test_record_of_default_aggregation_is_explicit() -> None:
    s = spec()
    back = L.from_record(L.to_record(s))
    assert back.aggregation is Aggregation.AREA_MEAN
    assert L.effective_aggregation(back) == L.effective_aggregation(s)
    assert L.to_record(back) == L.to_record(s)


def test_categories_record_form() -> None:
    labels = ("ground", "grass", "trees")
    record = L.to_record(spec(data_type=DataType.CATEGORY, categories=labels))
    assert [c["value"] for c in record["categories"]] == [0, 1, 2]
    # Objects in any order, and plain labels, are read back.
    shuffled = dict(record, categories=list(reversed(record["categories"])))
    assert L.from_record(shuffled).categories == labels
    assert L.from_record(dict(record, categories=list(labels))).categories == labels
    assert L.from_record(dict(record, categories=[])).categories == ()
    gap = [{"value": 0, "label": "a"}, {"value": 2, "label": "c"}]
    with pytest.raises(ValueError):
        L.from_record(dict(record, categories=gap))


def test_label_and_display_name() -> None:
    assert L.display_name(spec()) == "slope"
    assert L.display_name(spec(label="  ")) == "slope"
    assert L.display_name(spec(label="Slope (deg)")) == "Slope (deg)"
    assert L.validate(spec(label="Anything Goes ✓")) == []


def test_value_range() -> None:
    assert L.validate(spec(value_min=-1.0, value_max=1.0)) == []
    assert L.validate(spec(value_min=3.0, value_max=3.0)) == []
    assert L.validate(spec(value_min=3.0)) == []
    for low, high in ((2.0, 1.0), (math.nan, None), (None, math.inf)):
        issues = L.validate(spec(value_min=low, value_max=high))
        assert codes(issues) == ["layer-range-invalid"], (low, high, issues)
    record = L.to_record(spec(value_min=math.nan, value_max=1.0))
    assert record["value_min"] is None and record["value_max"] == 1.0


def test_number_text() -> None:
    assert L.parse_nodata is L.parse_number and L.format_nodata is L.format_number
    assert L.parse_number("") is None
    assert L.parse_number(L.format_number(1e-300)) == 1e-300


def test_provenance() -> None:
    p = L.make_provenance("slope", {"radius": np.float32(2.5), "steps": np.int64(3)}, "0.3.0")
    assert p == {
        "tool": "slope",
        "parameters": {"radius": 2.5, "steps": 3},
        "geotools_version": "0.3.0",
    }
    assert type(p["parameters"]["steps"]) is int  # type: ignore[index]
    assert L.make_provenance("x") == {"tool": "x", "parameters": {}, "geotools_version": ""}
    assert L.validate(spec(provenance=p)) == []
    json.dumps(p, allow_nan=False)

    text = L.format_provenance(p)
    assert L.parse_provenance(text) == p
    assert L.format_provenance(None) == "" and L.parse_provenance(" ") is None
    for bad in ({"parameters": {}}, {"tool": ""}, {"tool": "x", "parameters": [1]}):
        assert codes(L.validate(spec(provenance=bad))) == ["layer-provenance-invalid"], bad
    for text in ("{not json", "[1, 2]"):
        parsed = L.parse_provenance(text)
        assert codes(L.validate(spec(provenance=parsed))) == ["layer-provenance-invalid"]

    # Non-JSON values are made safe in the record.
    record = L.to_record(spec(provenance={"tool": "t", "parameters": {"x": math.nan, 1: (2,)}}))
    assert record["provenance"] == {"tool": "t", "parameters": {"x": None, "1": [2]}}
    json.dumps(record, allow_nan=False)


def test_texture_record_has_no_attribute() -> None:
    record = L.to_record(spec(domain=Domain.TEXTURE))
    assert record["attribute"] is None
    assert record["nodata"] is None
    assert L.to_record(spec(nodata=math.nan))["nodata"] is None  # JSON-safe even if invalid


def test_from_record_rejects_bad_records() -> None:
    with pytest.raises(KeyError):
        L.from_record({"domain": "FACE", "data_type": "FLOAT"})
    with pytest.raises(ValueError):
        L.from_record({"name": "x", "domain": "VERTEX", "data_type": "FLOAT"})
    minimal = L.from_record({"name": "x", "domain": "FACE", "data_type": "INT"})
    assert minimal == LayerSpec("x", Domain.FACE, DataType.INT)
    assert minimal.label == "" and minimal.provenance is None and minimal.value_min is None
