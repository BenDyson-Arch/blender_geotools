"""Tests for geotools.core.probe, including the M1 exit check against rusty-tiles."""

from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pytest

from geotools.core import probe as probe_module
from geotools.core.metashape import find_offset_file, read_offset_file
from geotools.core.placement import Placement, PlacementMode, blender_to_gltf
from geotools.core.preflight import ObjectSummary, SceneSummary, check
from geotools.core.probe import PlacementProbe, probe_placement, write_probe_glb

UTM = Placement(
    PlacementMode.SOURCE_CRS, source_crs="EPSG:32756", source_offset=(502000.0, 6961000.0, 10.0)
)
ANCHOR = Placement(PlacementMode.ANCHOR, anchor=(153.02, -27.47, 25.0))
BOUNDS = ((-50.0, -80.0, 0.0), (120.0, 40.0, 35.0))


def read_glb(path: Path) -> tuple[dict, np.ndarray, np.ndarray]:
    data = path.read_bytes()
    magic, version, length = struct.unpack_from("<III", data, 0)
    assert (magic, version, length) == (0x46546C67, 2, len(data))
    json_length, json_type = struct.unpack_from("<II", data, 12)
    assert json_type == 0x4E4F534A and json_length % 4 == 0
    document = json.loads(data[20 : 20 + json_length])
    bin_offset = 20 + json_length
    bin_length, bin_type = struct.unpack_from("<II", data, bin_offset)
    assert bin_type == 0x004E4942 and bin_length % 4 == 0
    assert bin_offset + 8 + bin_length == len(data)
    binary = data[bin_offset + 8 :]
    views = document["bufferViews"]
    positions = np.frombuffer(
        binary, "<f4", count=3 * document["accessors"][0]["count"], offset=views[0]["byteOffset"]
    ).reshape(-1, 3)
    indices = np.frombuffer(
        binary, "<u4", count=document["accessors"][1]["count"], offset=views[1]["byteOffset"]
    )
    return document, positions, indices


def gltf_to_blender(points: np.ndarray) -> np.ndarray:
    """What Blender's glTF importer does: glTF (x, y, z) to Blender (x, -z, y)."""
    return np.stack([points[:, 0], -points[:, 2], points[:, 1]], axis=1)


def enu_basis(lon: float, lat: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lam, phi = math.radians(lon), math.radians(lat)
    east = np.array([-math.sin(lam), math.cos(lam), 0.0])
    north = np.array(
        [-math.sin(phi) * math.cos(lam), -math.sin(phi) * math.sin(lam), math.cos(phi)]
    )
    up = np.array([math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)])
    return east, north, up


def wgs84_ecef(lon: float, lat: float, height: float) -> np.ndarray:
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    lam, phi = math.radians(lon), math.radians(lat)
    n = a / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    return np.array(
        [
            (n + height) * math.cos(phi) * math.cos(lam),
            (n + height) * math.cos(phi) * math.sin(lam),
            (n * (1 - e2) + height) * math.sin(phi),
        ]
    )


def columns(root: tuple[float, ...]) -> list[np.ndarray]:
    """Column-major 4x4 to its four columns (xyz only)."""
    return [np.array(root[4 * i : 4 * i + 3]) for i in range(4)]


# --- the probe GLB -------------------------------------------------------------------


def test_probe_glb_is_one_minimal_triangle(tmp_path: Path) -> None:
    path = tmp_path / "p.glb"
    write_probe_glb(path, *BOUNDS)
    document, positions, indices = read_glb(path)
    assert document["asset"] == {"version": "2.0"}
    assert document["scene"] == 0
    assert list(document["meshes"][0]["primitives"][0]["attributes"]) == ["POSITION"]
    assert document["meshes"][0]["primitives"][0]["mode"] == 4
    assert document["accessors"][0]["componentType"] == 5126
    assert document["accessors"][1]["componentType"] == 5125
    assert "extras" not in json.dumps(document) and "extensions" not in json.dumps(document)
    assert indices.tolist() == [0, 1, 2]
    assert positions.shape == (3, 3)


def test_probe_glb_box_is_the_bounds_in_gltf_y_up(tmp_path: Path) -> None:
    path = tmp_path / "p.glb"
    (x0, y0, z0), (x1, y1, z1) = BOUNDS
    write_probe_glb(path, *BOUNDS)
    document, positions, _ = read_glb(path)
    # Blender (x, y, z) -> glTF (x, z, -y): north flips into -Z.
    assert positions.min(axis=0).tolist() == [x0, z0, -y1]
    assert positions.max(axis=0).tolist() == [x1, z1, -y0]
    assert document["accessors"][0]["min"] == [x0, z0, -y1]
    assert document["accessors"][0]["max"] == [x1, z1, -y0]
    back = gltf_to_blender(positions.astype(np.float64))
    assert back.min(axis=0).tolist() == list(BOUNDS[0])
    assert back.max(axis=0).tolist() == list(BOUNDS[1])


def test_probe_glb_flat_bounds(tmp_path: Path) -> None:
    path = tmp_path / "p.glb"
    write_probe_glb(path, (0.0, 0.0, 5.0), (10.0, 20.0, 5.0))
    _, positions, _ = read_glb(path)
    assert positions[:, 1].tolist() == [5.0, 5.0, 5.0]


# --- probe_placement without rusty-tiles ----------------------------------------------


def test_missing_rusty_tiles_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "rusty_tiles", None)  # import raises ImportError
    result = probe_placement(UTM, *BOUNDS, workdir=tmp_path)
    assert result.ok is False
    assert result.origin_lon_lat_height is None
    assert "rusty-tiles is not available" in (result.error or "")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "placement",
    [
        Placement(PlacementMode.SOURCE_CRS, source_crs="32756"),
        Placement(PlacementMode.SOURCE_CRS, source_crs=""),
        Placement(PlacementMode.ANCHOR, anchor=(0.0, 95.0, 0.0)),
    ],
)
def test_invalid_placement_is_reported_not_raised(placement: Placement) -> None:
    result = probe_placement(placement, *BOUNDS)
    assert result.ok is False
    assert (result.error or "").startswith("ValueError: ")


@pytest.mark.parametrize(
    "bounds",
    [
        ((0.0, 0.0, float("nan")), (1.0, 1.0, 1.0)),
        ((0.0, 0.0, 0.0), (1.0, float("inf"), 1.0)),
        ((2.0, 0.0, 0.0), (1.0, 1.0, 1.0)),
        ((0.0, 0.0), (1.0, 1.0, 1.0)),
        (None, (1.0, 1.0, 1.0)),
    ],
)
def test_invalid_bounds_are_reported_not_raised(bounds) -> None:
    result = probe_placement(ANCHOR, *bounds)
    assert result.ok is False
    assert (result.error or "").startswith("ValueError: ")


class FakeTiles:
    """A stand-in rusty_tiles module recording each call."""

    def __init__(self, report: dict | None = None, raises: type | None = None) -> None:
        self.module = types.ModuleType("rusty_tiles")
        self.module.TilesError = type("TilesError", (Exception,), {})
        self.module.UnsupportedError = type("UnsupportedError", (self.module.TilesError,), {})
        self.module.mesh_to_tileset = self.mesh_to_tileset
        self.report = report or {"root_transform": None}
        self.raises = raises
        self.calls: list[tuple[Path, Path, dict]] = []

    def mesh_to_tileset(self, source, output, **kwargs):
        assert Path(source).is_file() and not Path(output).exists()
        self.calls.append((Path(source), Path(output), kwargs))
        if self.raises is not None:
            raise getattr(self.module, self.raises.__name__)("boom: no such operation")
        return types.SimpleNamespace(report=self.report)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch):
    def install(**kwargs) -> FakeTiles:
        tiles = FakeTiles(**kwargs)
        monkeypatch.setitem(sys.modules, "rusty_tiles", tiles.module)
        return tiles

    return install


def test_probe_calls_rusty_tiles_in_its_own_temp_dir(fake, tmp_path: Path) -> None:
    tiles = fake(
        report={
            "root_transform": list(range(16)),
            "source_crs": {"origin_degrees_metres": [153.0, -27.0, 12.0], "operation": "portable"},
            "placement": {"anchor_degrees_metres": [153.0, -27.0, 12.0]},
        }
    )
    (tmp_path / "keep.3tz").write_bytes(b"user file")
    result = probe_placement(UTM, *BOUNDS, workdir=tmp_path)
    assert result == PlacementProbe(
        True,
        origin_lon_lat_height=(153.0, -27.0, 12.0),
        operation="portable",
        root_transform=tuple(float(v) for v in range(16)),
    )
    [(source, output, kwargs)] = tiles.calls
    assert source.parent == output.parent
    assert source.parent.parent == tmp_path and source.parent.name.startswith("geotools-probe-")
    assert kwargs.pop("force") is True
    assert kwargs.pop("jobs") == 1
    assert kwargs == UTM.mesh_to_tileset_kwargs()
    # Cleaned up, and the user's files untouched.
    assert [p.name for p in tmp_path.iterdir()] == ["keep.3tz"]
    assert (tmp_path / "keep.3tz").read_bytes() == b"user file"


def test_probe_uses_system_temp_without_workdir(fake, monkeypatch, tmp_path: Path) -> None:
    tiles = fake()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    result = probe_placement(Placement(), *BOUNDS)
    assert result == PlacementProbe(True)
    assert tiles.calls[0][0].parent.parent == tmp_path
    assert list(tmp_path.iterdir()) == []


def test_probe_reports_rusty_tiles_errors_by_class(fake, tmp_path: Path) -> None:
    fake(raises=type("UnsupportedError", (), {}))
    result = probe_placement(UTM, *BOUNDS, workdir=tmp_path)
    assert result == PlacementProbe(False, error="UnsupportedError: boom: no such operation")
    assert list(tmp_path.iterdir()) == []


def test_probe_reads_anchor_origin(fake) -> None:
    fake(
        report={
            "root_transform": [0.0] * 16,
            "placement": {"anchor_degrees_metres": [153.02, -27.47, 25.0]},
        }
    )
    result = probe_placement(ANCHOR, *BOUNDS)
    assert result.ok and result.origin_lon_lat_height == (153.02, -27.47, 25.0)
    assert result.operation is None


def test_missing_workdir_is_reported(fake, tmp_path: Path) -> None:
    fake()
    result = probe_placement(ANCHOR, *BOUNDS, workdir=tmp_path / "missing")
    assert result.ok is False and (result.error or "").startswith("FileNotFoundError: ")


# --- against the real wheel: the M1 exit check -----------------------------------------


@pytest.fixture
def rusty_tiles():
    return pytest.importorskip("rusty_tiles")


def write_metashape_export(
    folder: Path, offset_text: str, size: tuple[float, float, float], seed: int
) -> Path:
    """A Metashape-style GLB: glTF Y-up, relative to the offset, plus its offset file.

    ``size`` is the (east, north, height) extent in source units.
    """
    rng = np.random.default_rng(seed)
    count = 300
    east = rng.uniform(-0.3, 0.7, count * 3) * size[0]
    north = rng.uniform(-0.6, 0.4, count * 3) * size[1]
    height = rng.uniform(0.05, 1.0, count * 3) * size[2]
    gltf = np.stack([east, height, -north], axis=1).astype(np.float32)
    indices = np.arange(len(gltf), dtype=np.uint32)
    source = folder / "site.glb"
    source.write_bytes(probe_module._glb_bytes(gltf, indices))
    (folder / "site_offset.txt").write_bytes(offset_text.encode("utf-8"))
    return source


EXPORTS = [
    pytest.param(
        "EPSG:32756", "E: 502000\nN: 6961000\nA: 10\n", (170.0, 120.0, 35.0), 0.0, id="utm-56s"
    ),
    pytest.param(
        "EPSG:4326",
        "E: 153.02\r\nN: -27.47\r\nA: 25.5\r\n",
        (0.002, 0.0015, 40.0),
        0.0,
        id="wgs84-degrees",
    ),
    pytest.param(
        "EPSG:3857",
        "E: 17033750\nN: -3183160\nA: 12.5\n",
        (400.0, 250.0, 60.0),
        39.25,
        id="web-mercator-height-offset",
    ),
]


@pytest.mark.parametrize(("crs", "offset_text", "size", "height_offset"), EXPORTS)
def test_m1_exit_metashape_export_is_placed_like_rusty_tiles(
    rusty_tiles, tmp_path: Path, crs: str, offset_text: str, size, height_offset: float
) -> None:
    source = write_metashape_export(tmp_path, offset_text, size, seed=len(crs))

    # The reference: rusty-tiles reading the export as its CLI would.
    offset_file = tmp_path / "site_offset.txt"
    reference = rusty_tiles.mesh_to_tileset(
        source,
        tmp_path / "reference.3tz",
        jobs=1,
        source_crs=crs,
        source_offset_file=offset_file,
        height_offset=height_offset,
    ).report

    # GeoTools: find and parse the offset, import into Blender's Z-up frame, check, probe.
    found = find_offset_file(source)
    assert found == offset_file
    offset = read_offset_file(found)
    assert tuple(reference["source_crs"]["source_offset"]) == offset
    _, gltf, _ = read_glb(source)
    blender = gltf_to_blender(gltf.astype(np.float64))
    scene = SceneSummary(
        objects=(
            ObjectSummary(
                "site",
                "MESH",
                faces=len(blender) // 3,
                vertices=len(blender),
                bounds_min=tuple(blender.min(axis=0).tolist()),
                bounds_max=tuple(blender.max(axis=0).tolist()),
            ),
        )
    )
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs=crs, source_offset=offset, height_offset=height_offset
    )
    issues = check(scene, placement)
    assert [i for i in issues if i.severity == "error"] == []
    work = tmp_path / "work"
    work.mkdir()
    result = probe_placement(placement, *scene.bounds(), workdir=work)
    assert list(work.iterdir()) == []

    assert result.ok, result.error
    assert result.operation == reference["source_crs"]["operation"] == "portable"
    ref_lon, ref_lat, ref_h = reference["source_crs"]["origin_degrees_metres"]
    lon, lat, h = result.origin_lon_lat_height
    assert abs(lon - ref_lon) <= 1e-9
    assert abs(lat - ref_lat) <= 1e-9
    assert abs(h - ref_h) <= 1e-6
    assert np.allclose(result.root_transform, reference["root_transform"], rtol=0, atol=1e-6)
    # And the origin is where the offset says: the export sits at its offset.
    if crs == "EPSG:4326":
        assert abs(lon - 153.02) < 0.01 and abs(lat + 27.47) < 0.01
    else:
        assert 153.0 < lon < 153.1 and -27.6 < lat < -27.4


def test_offset_round_trips_into_the_report(rusty_tiles) -> None:
    text = "E: 502000.125\nN: 6961000.0625\nA: -3.5\n"
    from geotools.core.metashape import parse_offset_text

    offset = parse_offset_text(text)
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:32756", source_offset=offset, height_offset=1.5
    )
    result = probe_placement(placement, *BOUNDS)
    assert result.ok, result.error
    # Origin height: source A plus the centre height of the bounds plus the height offset.
    assert result.origin_lon_lat_height[2] == pytest.approx(-3.5 + 17.5 + 1.5, abs=1e-6)


def test_unsupported_crs_fails_in_the_probe(rusty_tiles, tmp_path: Path) -> None:
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:2193", source_offset=(1.7e6, 5.9e6, 0.0)
    )
    result = probe_placement(placement, *BOUNDS, workdir=tmp_path)
    assert result.ok is False
    assert (result.error or "").startswith("UnsupportedError: ")
    assert result.origin_lon_lat_height is None and result.root_transform is None
    assert list(tmp_path.iterdir()) == []


def test_source_outside_crs_domain_fails_in_the_probe(rusty_tiles) -> None:
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:4326", source_offset=(0, 89.9, 0)
    )
    result = probe_placement(placement, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    assert result.ok is False
    assert (result.error or "").startswith("DataError: ")


def test_local_probe(rusty_tiles) -> None:
    assert probe_placement(Placement(), *BOUNDS) == PlacementProbe(True)


def test_anchor_with_heading_and_scene_offset(rusty_tiles, tmp_path: Path) -> None:
    lon, lat, height = 153.02, -27.47, 25.0
    placement = Placement(
        PlacementMode.ANCHOR,
        anchor=(lon, lat, height),
        heading_degrees=90.0,
        scene_offset=(10.0, 20.0, 5.0),  # Blender: 10 m east, 20 m north, 5 m up
    )
    result = probe_placement(placement, *BOUNDS)
    assert result.ok, result.error
    assert result.origin_lon_lat_height == (lon, lat, height)

    east, north, up = enu_basis(lon, lat)
    x_axis, y_axis, z_axis, translation = columns(result.root_transform)
    # Counter-clockwise from above: +90 turns the scene's east (X) onto north,
    # and its north (Y) onto west.
    assert np.allclose(x_axis, north, atol=1e-12)
    assert np.allclose(y_axis, -east, atol=1e-12)
    assert np.allclose(z_axis, up, atol=1e-12)
    # The offset moves the scene before the heading turns it: (10, 20, 5) ENU
    # becomes (-20, 10, 5).
    expected = wgs84_ecef(lon, lat, height) + (-20.0) * east + 10.0 * north + 5.0 * up
    assert np.allclose(translation, expected, rtol=0, atol=1e-6)

    # The same request made directly gives the same transform.
    source = tmp_path / "s.glb"
    write_probe_glb(source, *BOUNDS)
    direct = rusty_tiles.mesh_to_tileset(
        source,
        tmp_path / "d.3tz",
        anchor=(lon, lat, height),
        orientation_xyzw=(0.0, 0.0, math.sqrt(0.5), math.sqrt(0.5)),
        scene_offset=blender_to_gltf((10.0, 20.0, 5.0)),
    ).report
    assert np.allclose(result.root_transform, direct["root_transform"], rtol=0, atol=1e-9)


@pytest.mark.parametrize("heading", [0.0, 30.0, -45.0, 180.0, 271.0])
def test_heading_rotates_east_counter_clockwise(rusty_tiles, heading: float) -> None:
    lon, lat = -71.06, 42.36
    placement = Placement(PlacementMode.ANCHOR, anchor=(lon, lat, 0.0), heading_degrees=heading)
    result = probe_placement(placement, *BOUNDS)
    assert result.ok, result.error
    east, north, _ = enu_basis(lon, lat)
    x_axis = columns(result.root_transform)[0]
    t = math.radians(heading)
    assert np.allclose(x_axis, math.cos(t) * east + math.sin(t) * north, atol=1e-12)
