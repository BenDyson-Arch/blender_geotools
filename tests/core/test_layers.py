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
    "attribute",
    "domain",
    "data_type",
    "units",
    "nodata",
    "origin",
    "aggregation",
    "description",
    "categories",
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
    )
    record = L.to_record(s)
    assert list(record) == RECORD_KEYS
    assert record == {
        "name": "land_cover",
        "attribute": "gt_land_cover",
        "domain": "CORNER",
        "data_type": "CATEGORY",
        "units": "",
        "nodata": -1.0,
        "origin": "ANALYSIS",
        "aggregation": "MODE",  # the default, written out
        "description": "Classified cover",
        "categories": ["ground", "grass", "trees"],
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
