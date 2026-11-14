"""Presentation targets, variant naming and the variant record (geotools.core.presentation)."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from geotools.core.issues import Severity
from geotools.core.layers import DataType, Domain, LayerSpec
from geotools.core.presentation import (
    PRESENT_ATTRIBUTE,
    Blend,
    Target,
    presentation_issues,
    targets_for,
    variant_output,
    variant_record,
)
from geotools.core.ramps import apply_ramp, default_category_colours
from geotools.publish.staging import staging_dir_for

SLOPE = LayerSpec("slope", Domain.FACE, DataType.FLOAT, units="deg")
COVER = LayerSpec("cover", Domain.CORNER, DataType.CATEGORY, categories=("bare", "grass"))
SHADE = LayerSpec("shade", Domain.TEXTURE, DataType.FLOAT)


def test_present_attribute_is_reserved() -> None:
    assert PRESENT_ATTRIBUTE == "gt_present"


def test_targets_for_domain() -> None:
    assert targets_for(Domain.FACE) == (Target.COLOR_0,)
    assert targets_for(Domain.CORNER) == (Target.COLOR_0,)
    assert targets_for(Domain.TEXTURE) == (Target.BASE_COLOR, Target.EMISSIVE)


def test_variant_output_and_its_staging_dir(tmp_path: Path) -> None:
    output = tmp_path / "site.3tz"
    variant = variant_output(output, "slope")
    assert variant == tmp_path / "site.slope.3tz"
    assert staging_dir_for(variant) == tmp_path / "site.slope.geotools"
    assert staging_dir_for(variant) != staging_dir_for(output)
    for bad in ("", "a/b", "a.b", " slope"):
        with pytest.raises(ValueError):
            variant_output(output, bad)
    assert variant_output(tmp_path / "site.glb", "slope") == tmp_path / "site.slope.glb"
    with pytest.raises(ValueError):
        variant_output(tmp_path / "site.obj", "slope")


def test_presentation_issues() -> None:
    assert presentation_issues(SLOPE, Target.COLOR_0, "viridis", None, None) == []
    assert presentation_issues(SHADE, "EMISSIVE", "magma", 0.0, 1.0) == []

    def codes(*args) -> list[str]:
        issues = presentation_issues(*args)
        assert all(i.severity is Severity.ERROR for i in issues)
        return [i.code for i in issues]

    assert codes(SLOPE, Target.BASE_COLOR, "viridis", None, None) == ["variant-target-invalid"]
    assert codes(SHADE, Target.COLOR_0, "viridis", None, None) == ["variant-target-invalid"]
    assert codes(SLOPE, Target.COLOR_0, "jet", None, None) == ["variant-palette-invalid"]
    assert codes(SLOPE, Target.COLOR_0, "viridis", 2.0, 1.0) == ["variant-range-invalid"]
    assert codes(SLOPE, Target.COLOR_0, "viridis", math.nan, None) == ["variant-range-invalid"]
    # Categories ignore the palette and range.
    assert codes(COVER, Target.COLOR_0, "jet", 2.0, 1.0) == []


def test_variant_record_for_a_ramp() -> None:
    record = variant_record(
        SLOPE,
        Target.COLOR_0,
        Blend.MULTIPLY,
        palette="viridis",
        value_range=(0.0, 45.0),
        range_auto=(True, False),
        nodata_rgba=(128, 128, 128, 255),
    )
    assert list(record) == [
        "layer",
        "target",
        "blend",
        "palette",
        "scale",
        "range",
        "nodata_rgba",
        "ramp_stops",
    ]
    assert record["layer"] == "slope"
    assert record["target"] == "COLOR_0" and record["blend"] == "MULTIPLY"
    assert record["range"] == {"min": 0.0, "max": 45.0, "auto_min": True, "auto_max": False}
    assert record["nodata_rgba"] == [128, 128, 128, 255]
    stops = record["ramp_stops"]
    assert len(stops) == 11 and stops[0]["value"] == 0.0 and stops[-1]["value"] == 45.0
    assert stops[-1]["rgba"] == apply_ramp([45.0], 0.0, 45.0, "viridis")[0].tolist()
    assert json.loads(json.dumps(record, allow_nan=False)) == record


def test_variant_record_for_categories() -> None:
    colours = default_category_colours(2)
    record = variant_record(COVER, "COLOR_0", "REPLACE", category_rgba=colours)
    assert record["palette"] is None and record["range"] is None
    assert "ramp_stops" not in record
    assert record["categories"] == [
        {"value": 0, "label": "bare", "rgba": colours[0].tolist()},
        {"value": 1, "label": "grass", "rgba": colours[1].tolist()},
    ]
    assert record["nodata_rgba"] == [0, 0, 0, 0]
    with pytest.raises(ValueError):
        variant_record(COVER, "COLOR_0", "REPLACE")
    with pytest.raises(ValueError):
        variant_record(SLOPE, "COLOR_0", "REPLACE", palette="viridis")
    assert isinstance(record["categories"][0]["rgba"][0], int)
    assert np.asarray(record["categories"][1]["rgba"]).dtype.kind == "i"


def test_log_scale_needs_a_positive_range_and_is_recorded() -> None:
    assert presentation_issues(SLOPE, "COLOR_0", "viridis", None, None, "LOG") == []
    issues = presentation_issues(SLOPE, "COLOR_0", "viridis", 0.0, 10.0, "LOG")
    assert [i.code for i in issues] == ["variant-range-invalid"]
    record = variant_record(
        SLOPE, "COLOR_0", "REPLACE", palette="viridis", value_range=(1.0, 1000.0), scale="LOG"
    )
    assert record["scale"] == "LOG"
    assert [s["value"] for s in record["ramp_stops"]][::5] == pytest.approx(
        [1.0, 31.6227766, 1000.0]
    )
