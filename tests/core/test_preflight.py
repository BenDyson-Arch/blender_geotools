"""Tests for geotools.core.preflight."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from geotools.core.issues import Severity
from geotools.core.placement import Placement, PlacementMode
from geotools.core.preflight import (
    ImageSummary,
    MaterialSummary,
    ObjectSummary,
    SceneSummary,
    check,
    is_geographic_crs,
)

LOCAL = Placement()
UTM = Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:32756", source_offset=(5e5, 7e6, 0.0))

PNG = ImageSummary("albedo", "/data/albedo.png", "PNG", packed=False, exists=True)
PRINCIPLED = MaterialSummary("ground", uses_principled=True, images=(PNG,))


def mesh(name: str = "site", **changes) -> ObjectSummary:
    base = ObjectSummary(
        name,
        "MESH",
        faces=2,
        vertices=4,
        bounds_min=(-10.0, -20.0, 0.0),
        bounds_max=(30.0, 40.0, 5.0),
        uv_layers=1,
        color_attributes=0,
        materials=(PRINCIPLED,),
    )
    return dataclasses.replace(base, **changes)


def scene(*objects: ObjectSummary, unit_scale: float = 1.0) -> SceneSummary:
    return SceneSummary(unit_scale=unit_scale, objects=objects)


def codes(issues) -> list[str]:
    return [i.code for i in issues]


def only(issues, code: str):
    found = [i for i in issues if i.code == code]
    assert len(found) == 1, issues
    return found[0]


# --- bounds -------------------------------------------------------------------------


def test_bounds_none_without_meshes() -> None:
    assert SceneSummary().bounds() is None
    assert scene(ObjectSummary("cam", "CAMERA", bounds_max=(100.0, 100.0, 100.0))).bounds() is None


def test_bounds_union_of_meshes_only() -> None:
    s = scene(
        mesh("a", bounds_min=(-1.0, 2.0, 3.0), bounds_max=(4.0, 5.0, 6.0)),
        mesh("b", bounds_min=(0.0, -7.0, 4.0), bounds_max=(1.0, 3.0, 9.5)),
        ObjectSummary("light", "LIGHT", bounds_min=(-1e9, -1e9, -1e9), bounds_max=(1e9,) * 3),
    )
    assert s.bounds() == ((-1.0, -7.0, 3.0), (4.0, 5.0, 9.5))
    lo, hi = s.bounds()
    assert all(type(v) is float for v in lo + hi)


def test_bounds_keep_double_precision() -> None:
    lo = (502_000.123456789, 6_961_000.987654321, 10.000000001)
    s = scene(mesh(bounds_min=lo, bounds_max=(502_100.0, 6_961_100.0, 20.0)))
    assert s.bounds()[0] == lo


# --- clean scenes ---------------------------------------------------------------------


def test_clean_scene_has_no_issues() -> None:
    assert check(scene(mesh()), LOCAL) == []
    assert check(scene(mesh()), UTM) == []


def test_minimal_summaries_have_no_issues() -> None:
    # The defaults the Blender side might leave alone must not raise anything.
    assert check(scene(ObjectSummary("m", "MESH")), LOCAL) == []


# --- scene rules -----------------------------------------------------------------------


def test_no_mesh_objects() -> None:
    issues = check(scene(), LOCAL)
    assert codes(issues) == ["no-mesh-objects"]
    assert issues[0].severity is Severity.ERROR


def test_non_mesh_objects_warn_and_are_ignored() -> None:
    issues = check(scene(mesh(), ObjectSummary("Camera", "CAMERA", has_armature=True)), LOCAL)
    issue = only(issues, "non-mesh-object")
    assert issue.severity is Severity.WARNING
    assert issue.subject == "Camera"
    assert codes(issues) == ["non-mesh-object"]  # its other flags are not checked


def test_only_non_mesh_objects() -> None:
    issues = check(scene(ObjectSummary("Curve", "CURVE")), LOCAL)
    assert codes(issues) == ["no-mesh-objects", "non-mesh-object"]


@pytest.mark.parametrize("scale", [0.001, 0.3048, 2.0, float("nan"), 0.0])
def test_unit_scale(scale: float) -> None:
    issue = only(check(scene(mesh(), unit_scale=scale), LOCAL), "unit-scale")
    assert issue.severity is Severity.ERROR


@pytest.mark.parametrize(
    ("lo", "hi", "error"),
    [
        ((-1e6, -1e6, -1e6), (1e6, 1e6, 1e6), False),
        ((0.0, 0.0, 0.0), (1_000_000.5, 1.0, 1.0), True),
        ((-1_000_001.0, 0.0, 0.0), (1.0, 1.0, 1.0), True),
        ((0.0, 0.0, -2e6), (1.0, 1.0, 1.0), True),
        ((5e5, 7e6, 0.0), (5.1e5, 7.1e6, 10.0), True),  # raw UTM, offset not taken out
    ],
)
def test_coordinate_range(lo, hi, error: bool) -> None:
    issues = check(scene(mesh(bounds_min=lo, bounds_max=hi)), UTM)
    assert ("coordinate-range" in codes(issues)) is error
    if error:
        assert only(issues, "coordinate-range").severity is Severity.ERROR
        assert "float-precision" not in codes(issues)  # one problem, reported once


def test_non_finite_bounds_are_a_coordinate_error() -> None:
    issues = check(scene(mesh(bounds_max=(float("nan"), 1.0, 1.0))), LOCAL)
    assert codes(issues) == ["coordinate-range"]


@pytest.mark.parametrize(
    ("largest", "warn"),
    [
        (8192.0, False),  # spacing 0.98 mm
        (16_383.0, False),
        (16_384.0, True),  # spacing 1.95 mm
        (-20_000.0, True),
        (999_999.0, True),
    ],
)
def test_float_precision(largest: float, warn: bool) -> None:
    lo, hi = (min(largest, 0.0), 0.0, 0.0), (max(largest, 0.0), 1.0, 1.0)
    issues = check(scene(mesh(bounds_min=lo, bounds_max=hi)), LOCAL)
    assert ("float-precision" in codes(issues)) is warn
    expected = float(np.spacing(np.float32(abs(largest)))) > 1e-3
    assert warn is expected
    if warn:
        issue = only(issues, "float-precision")
        assert issue.severity is Severity.WARNING
        assert "offset" in issue.message


@pytest.mark.parametrize(
    ("placement", "hint"),
    [
        (UTM, "the source offset"),
        (Placement(PlacementMode.ANCHOR, anchor=(1.0, 2.0, 3.0)), "the scene offset"),
        (LOCAL, "a source CRS offset"),
    ],
)
def test_float_precision_suggests_the_right_offset(placement: Placement, hint: str) -> None:
    issues = check(scene(mesh(bounds_max=(50_000.0, 1.0, 1.0))), placement)
    assert hint in only(issues, "float-precision").message


def test_float_precision_in_geographic_crs_judges_height_in_metres() -> None:
    wgs84 = Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:4326")
    issues = check(
        scene(mesh(bounds_min=(0.0, 0.0, 0.0), bounds_max=(0.01, 0.01, 20_000.0))), wgs84
    )
    assert "mm" in only(issues, "float-precision").message


def test_geographic_crs_warns_and_judges_precision_in_degrees() -> None:
    wgs84 = Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:4326")
    # Raw degrees: 153 has float32 spacing 1.5e-5 deg, about 1.7 m.
    raw = check(scene(mesh(bounds_min=(152.9, -27.5, 0.0), bounds_max=(153.1, -27.4, 30.0))), wgs84)
    assert codes(raw) == ["crs-geographic", "float-precision"]
    assert all(i.severity is Severity.WARNING for i in raw)
    assert "degrees" in raw[0].message
    # With the offset taken out, 0.01 deg has spacing far below a millimetre, and
    # heights are still metres, so 300 m is fine.
    small = check(
        scene(mesh(bounds_min=(-0.01, -0.01, 0.0), bounds_max=(0.01, 0.01, 300.0))), wgs84
    )
    assert codes(small) == ["crs-geographic"]


@pytest.mark.parametrize(
    ("crs", "geographic"),
    [
        ("EPSG:4326", True),
        ("epsg:4326", True),
        (" EPSG:7844 ", True),
        ("EPSG:4258", True),
        ("+proj=longlat +datum=WGS84 +no_defs", True),
        ('GEOGCRS["WGS 84", DATUM["World Geodetic System 1984"]]', True),
        ('GEOGCS["WGS 84"]', True),
        ("EPSG:32756", False),
        ("EPSG:3857", False),
        ("+proj=utm +zone=56 +south", False),
        ('PROJCRS["WGS 84 / UTM zone 56S", BASEGEOGCRS["WGS 84"]]', False),
        ("", False),
    ],
)
def test_is_geographic_crs(crs: str, geographic: bool) -> None:
    assert is_geographic_crs(crs) is geographic


def test_geographic_warning_only_in_source_crs_mode() -> None:
    anchor = Placement(PlacementMode.ANCHOR, anchor=(1.0, 2.0, 3.0), source_crs="EPSG:4326")
    assert check(scene(mesh()), anchor) == []


# --- object rules ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "code", "severity"),
    [
        ({"has_shape_keys": True}, "shape-keys", Severity.ERROR),
        ({"has_armature": True}, "armature", Severity.ERROR),
        ({"is_animated": True}, "animation", Severity.WARNING),
        ({"uv_layers": 3}, "uv-layers", Severity.WARNING),
        ({"color_attributes": 2}, "color-attributes", Severity.WARNING),
    ],
)
def test_object_rules(changes, code: str, severity: Severity) -> None:
    issues = check(scene(mesh("Tree", **changes)), LOCAL)
    assert codes(issues) == [code]
    assert issues[0].severity is severity
    assert issues[0].subject == "Tree"
    assert "Tree" in issues[0].message


def test_object_limits_are_inclusive() -> None:
    assert check(scene(mesh(uv_layers=2, color_attributes=1)), LOCAL) == []


# --- material and image rules -------------------------------------------------------


def with_material(material: MaterialSummary, name: str = "site") -> ObjectSummary:
    return mesh(name, materials=(material,))


@pytest.mark.parametrize(
    ("material", "expected"),
    [
        (MaterialSummary("m", uses_principled=False), ["material-not-principled"]),
        (
            MaterialSummary("m", True, unsupported_nodes=("ShaderNodeWireframe",)),
            ["material-unsupported-nodes"],
        ),
        (MaterialSummary("m", True, blend_mode="BLEND"), ["alpha-blend"]),
        (MaterialSummary("m", True, blend_mode="CLIP"), []),
        (MaterialSummary("m", True, blend_mode="OPAQUE"), []),
        (
            MaterialSummary("m", False, blend_mode="BLEND"),
            ["alpha-blend", "material-not-principled"],
        ),
    ],
)
def test_material_rules(material: MaterialSummary, expected: list[str]) -> None:
    issues = check(scene(with_material(material)), LOCAL)
    assert codes(issues) == expected
    assert all(i.subject == "m" for i in issues)


def test_alpha_blend_is_an_error() -> None:
    issues = check(scene(with_material(MaterialSummary("glass", True, blend_mode="BLEND"))), LOCAL)
    assert only(issues, "alpha-blend").severity is Severity.ERROR


def test_unsupported_nodes_named_in_message() -> None:
    material = MaterialSummary("m", True, unsupported_nodes=("ShaderNodeB", "ShaderNodeA"))
    message = only(check(scene(with_material(material)), LOCAL), "material-unsupported-nodes")
    assert "ShaderNodeA, ShaderNodeB" in message.message


@pytest.mark.parametrize(
    ("image", "expected", "severity"),
    [
        (ImageSummary("i", "/x/i.png", "PNG", False, True), [], None),
        (ImageSummary("i", "/x/i.jpg", "JPEG", False, True), [], None),
        (ImageSummary("i", "/x/i.exr", "OPEN_EXR", False, True), ["image-format"], "warning"),
        (ImageSummary("i", "/x/i.tif", "TIFF", True, True), ["image-format"], "warning"),
        (ImageSummary("i", "/x/i.png", "PNG", False, False), ["image-missing"], "error"),
        (ImageSummary("i", "", "PNG", False, False), ["image-missing"], "error"),
        (ImageSummary("i", "/x/i.png", "PNG", True, False), [], None),  # packed
        (ImageSummary("i", "/x/i.exr", "OPEN_EXR", False, False), ["image-missing"], "error"),
    ],
)
def test_image_rules(image: ImageSummary, expected: list[str], severity) -> None:
    material = MaterialSummary("m", True, images=(image,))
    issues = check(scene(with_material(material)), LOCAL)
    assert codes(issues) == expected
    if expected:
        assert issues[0].severity == severity
        assert issues[0].subject == "i"


def test_shared_materials_and_images_reported_once() -> None:
    exr = ImageSummary("height", "/x/h.exr", "OPEN_EXR", False, True)
    a = MaterialSummary("a", False, images=(exr,))
    b = MaterialSummary("b", True, images=(exr,))
    issues = check(scene(mesh("one", materials=(a, b)), mesh("two", materials=(a,))), LOCAL)
    assert codes(issues) == ["material-not-principled", "image-format"]


def test_materials_on_non_mesh_objects_are_ignored() -> None:
    curve = ObjectSummary("Curve", "CURVE", materials=(MaterialSummary("x", False),))
    assert codes(check(scene(mesh(), curve), LOCAL)) == ["non-mesh-object"]


# --- ordering and placement -----------------------------------------------------------


def test_errors_first_and_placement_included() -> None:
    bad_placement = Placement(PlacementMode.SOURCE_CRS, source_crs="32756")
    issues = check(
        scene(
            ObjectSummary("Lamp", "LIGHT"),
            mesh("a", is_animated=True, has_shape_keys=True),
            with_material(MaterialSummary("glass", True, blend_mode="BLEND"), "b"),
            unit_scale=0.01,
        ),
        bad_placement,
    )
    severities = [i.severity for i in issues]
    assert severities == sorted(severities, key=[Severity.ERROR, Severity.WARNING].index)
    assert codes(issues) == [
        "crs-bare-code",
        "unit-scale",
        "shape-keys",
        "alpha-blend",
        "non-mesh-object",
        "animation",
    ]


def test_every_message_is_one_plain_sentence() -> None:
    issues = check(
        scene(
            ObjectSummary("Lamp", "LIGHT"),
            mesh(
                "a",
                is_animated=True,
                has_shape_keys=True,
                has_armature=True,
                uv_layers=4,
                color_attributes=3,
                bounds_max=(20_000.0, 1.0, 1.0),
                materials=(
                    MaterialSummary(
                        "m",
                        False,
                        blend_mode="BLEND",
                        images=(
                            ImageSummary("x", "/x.exr", "OPEN_EXR", False, True),
                            ImageSummary("y", "/y.png", "PNG", False, False),
                        ),
                    ),
                    MaterialSummary("n", True, unsupported_nodes=("ShaderNodeToon",)),
                ),
            ),
            unit_scale=2.0,
        ),
        Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:4326"),
    )
    assert len({i.code for i in issues}) == len(issues) == 14
    for issue in issues:
        assert issue.message[0].isupper() or issue.message[0].isdigit() or issue.subject
        assert issue.message.endswith(".")
        assert ". " not in issue.message.rstrip(".")


def test_image_limits() -> None:
    from geotools.core.preflight import ImageSummary, _image_limits

    ok = ImageSummary("a", "/a.jpg", "JPEG", False, True, 8192, 8192, 20 << 20)
    assert _image_limits(ok) == []
    big_edge = ImageSummary("b", "/b.png", "PNG", False, True, 16385, 16, 1000)
    assert [i.code for i in _image_limits(big_edge)] == ["image-too-large"]
    big_file = ImageSummary("c", "/c.png", "PNG", False, True, 8192, 8192, 40 << 20)
    assert [i.code for i in _image_limits(big_file)] == ["image-file-too-large"]
    converted = ImageSummary("d", "", "OPEN_EXR", False, True, 8192, 8192, None)
    assert [i.code for i in _image_limits(converted)] == ["image-png-size"]
