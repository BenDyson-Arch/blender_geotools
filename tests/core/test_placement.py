"""Tests for geotools.core.placement."""

from __future__ import annotations

import math

import pytest

from geotools.core.issues import Severity
from geotools.core.placement import (
    Placement,
    PlacementMode,
    blender_to_gltf,
    heading_quaternion_xyzw,
)

NAN = float("nan")
INF = float("inf")


def codes(placement: Placement) -> list[str]:
    return [issue.code for issue in placement.validate()]


def rotate(q: tuple[float, float, float, float], v: tuple[float, float, float]):
    """Rotate ``v`` by the unit quaternion ``q`` (x, y, z, w): q v q*."""
    x, y, z, w = q
    vx, vy, vz = v
    # t = 2 (q.xyz x v); v' = v + w t + q.xyz x t
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (
        vx + w * tx + (y * tz - z * ty),
        vy + w * ty + (z * tx - x * tz),
        vz + w * tz + (x * ty - y * tx),
    )


# --- axes -----------------------------------------------------------------------


def test_blender_to_gltf_maps_z_up_to_y_up() -> None:
    assert blender_to_gltf((1.0, 2.0, 3.0)) == (1.0, 3.0, -2.0)
    east, north, up = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)
    assert blender_to_gltf(east) == (1.0, 0.0, 0.0)  # glTF +X is east
    assert blender_to_gltf(up) == (0.0, 1.0, 0.0)  # glTF +Y is up
    assert blender_to_gltf(north) == (0.0, 0.0, -1.0)  # glTF -Z is north


def test_blender_to_gltf_returns_floats_and_no_negative_zero() -> None:
    out = blender_to_gltf((1, 0, 2))
    assert all(type(v) is float for v in out)
    assert math.copysign(1.0, out[2]) == 1.0


def test_blender_to_gltf_is_exact_for_large_values() -> None:
    v = (502_123.456789012, 6_961_987.654321098, 1e-17)
    assert blender_to_gltf(v) == (v[0], v[2], -v[1])


# --- heading ---------------------------------------------------------------------


def test_heading_zero_is_identity() -> None:
    assert heading_quaternion_xyzw(0.0) == (0.0, 0.0, 0.0, 1.0)


@pytest.mark.parametrize("heading", [0.0, 12.5, 90.0, -45.0, 180.0, 359.0, 3600.0 + 30.0])
def test_heading_quaternion_is_unit_and_about_up(heading: float) -> None:
    q = heading_quaternion_xyzw(heading)
    assert q[0] == 0.0 and q[1] == 0.0
    assert abs(math.hypot(*q) - 1.0) < 1e-15  # rusty-tiles allows 1e-12


def test_heading_turns_counter_clockwise_seen_from_above() -> None:
    # +90 degrees turns east onto north, and north onto west.
    q = heading_quaternion_xyzw(90.0)
    assert rotate(q, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0), abs=1e-15)
    assert rotate(q, (0.0, 1.0, 0.0)) == pytest.approx((-1.0, 0.0, 0.0), abs=1e-15)
    assert rotate(q, (0.0, 0.0, 1.0)) == pytest.approx((0.0, 0.0, 1.0), abs=1e-15)


def test_heading_wraps_full_turns() -> None:
    assert heading_quaternion_xyzw(370.0) == pytest.approx(heading_quaternion_xyzw(10.0))
    assert heading_quaternion_xyzw(-350.0) == pytest.approx(heading_quaternion_xyzw(10.0))


# --- validate: local and anchor ---------------------------------------------------


def test_local_has_no_issues_and_ignores_other_fields() -> None:
    placement = Placement(
        PlacementMode.LOCAL,
        anchor=(NAN, 500.0, INF),
        heading_degrees=NAN,
        source_crs="auto",
        source_offset=(NAN, 0.0, 0.0),
        height_offset=INF,
    )
    assert placement.validate() == []
    assert placement.mesh_to_tileset_kwargs() == {}


def test_default_placement_is_local() -> None:
    assert Placement().mode is PlacementMode.LOCAL
    assert Placement().mesh_to_tileset_kwargs() == {}


def test_anchor_valid() -> None:
    placement = Placement(PlacementMode.ANCHOR, anchor=(153.02, -27.47, 25.0))
    assert placement.validate() == []


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        ((180.0, 90.0, 0.0), []),
        ((-180.0, -90.0, -100.0), []),
        ((180.0001, 0.0, 0.0), ["anchor-longitude-range"]),
        ((-181.0, 0.0, 0.0), ["anchor-longitude-range"]),
        ((0.0, 90.5, 0.0), ["anchor-latitude-range"]),
        ((0.0, -91.0, 0.0), ["anchor-latitude-range"]),
        ((200.0, 100.0, 0.0), ["anchor-longitude-range", "anchor-latitude-range"]),
        ((NAN, 0.0, 0.0), ["placement-not-finite"]),
        ((0.0, 0.0, INF), ["placement-not-finite"]),
    ],
)
def test_anchor_ranges(anchor, expected) -> None:
    assert codes(Placement(PlacementMode.ANCHOR, anchor=anchor)) == expected


def test_anchor_heading_and_offset_must_be_finite() -> None:
    placement = Placement(
        PlacementMode.ANCHOR,
        anchor=(10.0, 10.0, 0.0),
        heading_degrees=INF,
        scene_offset=(0.0, NAN, 0.0),
    )
    issues = placement.validate()
    assert [i.code for i in issues] == ["placement-not-finite", "placement-not-finite"]
    assert all(i.severity is Severity.ERROR for i in issues)
    assert "heading" in issues[0].message and "scene offset" in issues[1].message


def test_anchor_ignores_crs_fields() -> None:
    placement = Placement(
        PlacementMode.ANCHOR,
        anchor=(10.0, 10.0, 0.0),
        source_crs="auto",
        source_offset=(NAN, NAN, NAN),
        height_offset=NAN,
    )
    assert placement.validate() == []


def test_anchor_wrong_length_is_an_error_not_a_crash() -> None:
    placement = Placement(PlacementMode.ANCHOR, anchor=(1.0, 2.0))  # type: ignore[arg-type]
    assert codes(placement) == ["placement-not-finite"]


def test_anchor_kwargs() -> None:
    placement = Placement(
        PlacementMode.ANCHOR,
        anchor=(153.02, -27.47, 25.0),
        heading_degrees=90.0,
        scene_offset=(10.0, 20.0, 5.0),
        source_crs="EPSG:32756",  # ignored
    )
    kwargs = placement.mesh_to_tileset_kwargs()
    assert kwargs == {
        "anchor": (153.02, -27.47, 25.0),
        "orientation_xyzw": heading_quaternion_xyzw(90.0),
        "scene_offset": (10.0, 5.0, -20.0),  # Blender E, N, U to glTF Y-up
    }


def test_kwargs_raise_on_errors() -> None:
    with pytest.raises(ValueError, match="longitude"):
        Placement(PlacementMode.ANCHOR, anchor=(190.0, 0.0, 0.0)).mesh_to_tileset_kwargs()


# --- validate: source CRS --------------------------------------------------------


@pytest.mark.parametrize(
    "crs", ["EPSG:32756", "epsg:3857", "  EPSG:4326 ", "+proj=utm +zone=56 +south +datum=WGS84"]
)
def test_source_crs_valid(crs: str) -> None:
    placement = Placement(PlacementMode.SOURCE_CRS, source_crs=crs)
    assert placement.validate() == []


@pytest.mark.parametrize(
    ("crs", "code", "hint"),
    [
        ("", "crs-missing", "EPSG:32756"),
        ("   ", "crs-missing", "EPSG:32756"),
        ("local", "crs-missing", "local placement"),
        ("LOCAL", "crs-missing", "local placement"),
        ("32756", "crs-bare-code", '"EPSG:32756"'),
        (" 4326 ", "crs-bare-code", '"EPSG:4326"'),
        ("3857", "crs-bare-code", '"EPSG:3857"'),
        ("auto", "crs-keyword", "explicitly"),
        ("AUTO", "crs-keyword", "explicitly"),
        ("geographic", "crs-keyword", "EPSG:4326"),
        ("WGS84", "crs-keyword", "EPSG:4326"),
        ("webmercator", "crs-keyword", "EPSG:3857"),
        ("web-mercator", "crs-keyword", "EPSG:3857"),
        ("Mercator", "crs-keyword", "EPSG:3857"),
        ("pseudo-mercator", "crs-keyword", "EPSG:3857"),
        ("pseudomercator", "crs-keyword", "EPSG:3857"),
    ],
)
def test_source_crs_refused(crs: str, code: str, hint: str) -> None:
    issues = Placement(PlacementMode.SOURCE_CRS, source_crs=crs).validate()
    assert [i.code for i in issues] == [code]
    assert issues[0].severity is Severity.ERROR
    assert hint in issues[0].message
    assert issues[0].message.endswith(".")


def test_source_crs_offsets_must_be_finite() -> None:
    placement = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs="EPSG:32756",
        source_offset=(INF, 0.0, 0.0),
        height_offset=NAN,
    )
    assert codes(placement) == ["placement-not-finite", "placement-not-finite"]


def test_source_crs_ignores_anchor_fields() -> None:
    placement = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs="EPSG:32756",
        anchor=(500.0, NAN, 0.0),
        heading_degrees=INF,
        scene_offset=(NAN, 0.0, 0.0),
    )
    assert placement.validate() == []


def test_source_crs_kwargs() -> None:
    placement = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs=" EPSG:32756 ",
        source_offset=(502000, 6961000.25, 10),
        height_offset=-42.5,
        anchor=(153.0, -27.0, 0.0),  # ignored
    )
    kwargs = placement.mesh_to_tileset_kwargs()
    assert kwargs == {
        "source_crs": "EPSG:32756",
        "source_axes": "y-up",
        "source_offset": (502000.0, 6961000.25, 10.0),
        "height_offset": -42.5,
    }
    assert all(type(v) is float for v in kwargs["source_offset"])  # type: ignore[union-attr]


def test_source_crs_kwargs_keep_full_precision() -> None:
    offset = (502_000.123456789, 6_961_000.987654321, 10.000000001)
    placement = Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:32756", source_offset=offset)
    assert placement.mesh_to_tileset_kwargs()["source_offset"] == offset


def test_source_crs_kwargs_raise_on_keyword() -> None:
    with pytest.raises(ValueError, match="explicitly"):
        Placement(PlacementMode.SOURCE_CRS, source_crs="auto").mesh_to_tileset_kwargs()


def test_mode_accepts_plain_strings() -> None:
    # Blender EnumProperty values arrive as str.
    placement = Placement("source_crs", source_crs="EPSG:32756")  # type: ignore[arg-type]
    assert placement.mesh_to_tileset_kwargs()["source_axes"] == "y-up"


# --- against the real wheel ------------------------------------------------------


def test_kwargs_are_accepted_by_rusty_tiles(tmp_path) -> None:
    """Each mode's keywords run as-is, and the report echoes our numbers."""
    rusty_tiles = pytest.importorskip("rusty_tiles")
    from geotools.core.probe import write_probe_glb

    source = tmp_path / "s.glb"
    write_probe_glb(source, (-5.0, -5.0, 0.0), (5.0, 5.0, 2.0))

    anchor = Placement(
        PlacementMode.ANCHOR,
        anchor=(153.02, -27.47, 25.0),
        heading_degrees=30.0,
        scene_offset=(10.0, 20.0, 5.0),
    )
    report = rusty_tiles.mesh_to_tileset(
        source, tmp_path / "a.3tz", jobs=1, **anchor.mesh_to_tileset_kwargs()
    ).report
    assert report["placement"]["anchor_degrees_metres"] == [153.02, -27.47, 25.0]
    assert report["placement"]["scene_offset_metres"] == [10.0, 5.0, -20.0]
    assert report["placement"]["orientation_xyzw"] == pytest.approx(
        heading_quaternion_xyzw(30.0), abs=1e-15
    )

    crs = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs="EPSG:32756",
        source_offset=(502000.0, 6961000.0, 10.0),
        height_offset=40.0,
    )
    report = rusty_tiles.mesh_to_tileset(
        source, tmp_path / "c.3tz", jobs=1, **crs.mesh_to_tileset_kwargs()
    ).report
    assert report["source_crs"]["axes"] == "y-up"
    assert report["source_crs"]["source_offset"] == [502000.0, 6961000.0, 10.0]
    assert report["source_crs"]["height_offset_metres"] == 40.0

    local = rusty_tiles.mesh_to_tileset(
        source, tmp_path / "l.3tz", jobs=1, **Placement().mesh_to_tileset_kwargs()
    ).report
    assert local["root_transform"] is None


@pytest.mark.parametrize("crs", ["32756", "auto", "geographic", "wgs84", "mercator"])
def test_refused_crs_is_refused_by_rusty_tiles_too(tmp_path, crs: str) -> None:
    rusty_tiles = pytest.importorskip("rusty_tiles")
    from geotools.core.probe import write_probe_glb

    source = tmp_path / "s.glb"
    write_probe_glb(source, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    with pytest.raises((rusty_tiles.InvalidRequestError, rusty_tiles.UnsupportedError)):
        rusty_tiles.mesh_to_tileset(source, tmp_path / "o.3tz", jobs=1, source_crs=crs)
