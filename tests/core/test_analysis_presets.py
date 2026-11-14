"""The analysis preset catalog (``geotools.analysis.presets``), without Blender."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from geotools.analysis import presets
from geotools.core import layers
from geotools.core.issues import Severity
from geotools.core.ramps import PALETTES

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_ids_groups_and_layers_are_unique() -> None:
    for key in ("id", "group", "layer"):
        values = [getattr(p, key) for p in presets.PRESETS]
        assert len(values) == len(set(values)), key
    assert all(p.group.startswith("GeoTools ") for p in presets.PRESETS)
    assert presets.get("slope").group == "GeoTools Slope"
    with pytest.raises(KeyError):
        presets.get("nope")


def test_the_first_batch_is_complete() -> None:
    assert {p.id for p in presets.by_category(presets.Category.TERRAIN)} == {
        "slope",
        "slope_percent",
        "aspect",
        "curvature_profile",
        "curvature_plan",
        "curvature_mean",
        "rugosity",
        "tpi",
        "tri",
    }
    assert {p.id for p in presets.by_category(presets.Category.ARCHAEOLOGY)} == {
        "local_relief",
        "sky_view_factor",
        "openness_positive",
        "openness_negative",
    }


def test_the_second_batch_is_complete() -> None:
    by = {c: {p.id for p in presets.by_category(c)} for c in presets.Category}
    assert by[presets.Category.VISIBILITY] == {
        "viewshed",
        "viewshed_cumulative",
        "intervisibility",
        "line_of_sight",
    }
    assert by[presets.Category.CHANGE] == {"surface_distance", "feature_distance"}
    assert by[presets.Category.VOLUMES] == {"cut_fill", "closed_volume"}
    assert by[presets.Category.PATHS] == {"least_cost_path", "flow_path"}
    assert set(presets.CATEGORY_LABELS) == set(presets.Category)
    objects = {p.id for p in presets.PRESETS if p.output is presets.Output.OBJECT}
    assert objects == {"intervisibility", "line_of_sight", "least_cost_path", "flow_path"}


LAYER_PRESETS = tuple(p for p in presets.PRESETS if p.output is presets.Output.LAYER)
OBJECT_PRESETS = tuple(p for p in presets.PRESETS if p.output is presets.Output.OBJECT)


@pytest.mark.parametrize("preset", LAYER_PRESETS, ids=lambda p: p.id)
def test_each_preset_makes_a_valid_layer(preset: presets.Preset) -> None:
    spec = presets.layer_spec(preset, inputs={"Window": 0.1}, objects=("Site",))
    assert [i for i in layers.validate(spec) if i.severity is Severity.ERROR] == []
    assert spec.domain in (layers.Domain.FACE, layers.Domain.CORNER)
    assert spec.data_type is layers.DataType.FLOAT
    assert spec.origin is layers.Origin.GEONODES
    assert spec.units and spec.label == preset.label
    assert layers.attribute_name(spec) == preset.attribute == f"gt_{preset.layer}"
    assert presets.preset_of(spec.provenance) == preset.id
    parameters = spec.provenance["parameters"]
    assert parameters["preset_version"] == preset.version
    assert parameters["node_group"] == preset.group
    assert parameters["library"] == presets.LIBRARY_FILE
    assert preset.palette in PALETTES
    if preset.display_range is not None:
        assert preset.display_range[0] < preset.display_range[1]
    # The record round-trips through build.json.
    assert layers.from_record(layers.to_record(spec)) == spec


@pytest.mark.parametrize("preset", OBJECT_PRESETS, ids=lambda p: p.id)
def test_object_presets_make_lines_with_provenance(preset: presets.Preset) -> None:
    assert preset.domain is None and preset.active_only
    with pytest.raises(ValueError):
        presets.layer_spec(preset)
    names = [a.name for a in preset.attributes]
    assert names and len(names) == len(set(names))
    assert preset.attribute == names[0]
    assert all(a.domain in {"POINT", "EDGE"} and a.description for a in preset.attributes)
    # The surface is the active mesh: exactly one SURFACE input.
    surfaces = [i for i in preset.object_inputs if i.fill is presets.Fill.SURFACE]
    assert [i.name for i in surfaces] == ["Surface"]
    record = presets.object_provenance(preset, inputs={"Surface": "Site"}, surface="Site")
    assert record["tool"] == presets.TOOL and presets.preset_of(record) == preset.id
    assert record["parameters"]["objects"] == ["Site"]
    assert set(record["parameters"]["attributes"]) == set(names)


def test_which_presets_go_on_the_active_object_only() -> None:
    # An Empty observer leaves every selected mesh a target; a second mesh does not.
    assert not presets.get("viewshed").active_only
    assert not presets.get("slope").active_only
    for preset_id in ("viewshed_cumulative", "surface_distance", "feature_distance", "cut_fill"):
        assert presets.get(preset_id).active_only, preset_id
    assert not presets.get("closed_volume").active_only


@pytest.mark.parametrize("preset", presets.PRESETS, ids=lambda p: p.id)
def test_inputs_are_well_formed(preset: presets.Preset) -> None:
    names = [i.name for i in preset.inputs]
    assert len(names) == len(set(names))
    assert presets.ATTRIBUTE_INPUT not in names and "Geometry" not in names
    assert (presets.CONVERGENCE_INPUT in names) == preset.north
    for spec in preset.inputs:
        assert spec.kind in {"FLOAT", "INT", "BOOLEAN", "STRING", "OBJECT", "COLLECTION"}
        assert spec.description.strip(), spec.name
        if spec.kind in {"OBJECT", "COLLECTION"}:
            assert spec.default is None
        assert (spec.fill is not presets.Fill.NONE) <= (spec.kind == "OBJECT"), spec.name
        if spec.kind == "OBJECT":
            assert spec.fill is presets.Fill.NONE or spec.fill in presets.FILL_LABELS
        if spec.kind == "STRING":
            assert isinstance(spec.default, str)
        if spec.min is not None:
            assert spec.default >= spec.min, spec.name
        if spec.max is not None:
            assert spec.default <= spec.max, spec.name
        if spec.kind == "INT":
            assert isinstance(spec.default, int)


def test_north_in_the_provenance() -> None:
    aspect = presets.get("aspect")
    assert presets.layer_spec(aspect).provenance["parameters"]["north"] == "grid"
    spec = presets.layer_spec(aspect, "aspect_true", north="true")
    assert spec.name == "aspect_true" and spec.provenance["parameters"]["north"] == "true"
    assert "north" not in presets.layer_spec(presets.get("slope")).provenance["parameters"]


def test_preset_of_other_layers() -> None:
    assert presets.preset_of(None) is None
    assert presets.preset_of({"tool": "bake_texture", "parameters": {}}) is None
    assert presets.preset_of({"tool": presets.TOOL, "parameters": "x"}) is None


def test_docs_describe_every_preset() -> None:
    text = (REPO_ROOT / "docs" / "analysis-presets.md").read_text(encoding="utf-8")
    for preset in presets.PRESETS:
        assert f"`{preset.layer}`" in text, preset.layer
        for spec in preset.inputs:
            assert re.search(rf"\b{re.escape(spec.name)}\b", text), (preset.id, spec.name)
