"""Tests for geotools.core.output_tiles: rusty-tiles' decoded tile views."""

from __future__ import annotations

import json
import struct
import sys
import types
import zipfile
from pathlib import Path

import numpy as np
import pytest

from geotools.core import deps
from geotools.core.facemap import build_face_map
from geotools.core.gltf_writer import MaterialData, PrimitiveData, SceneExport, write_glb
from geotools.core.output_tiles import (
    NO_ID,
    OutputTilesError,
    decode_tile,
    glb_triangles,
    list_tiles,
    triangle_counts,
)


def _real() -> bool:
    deps.reset_cache()
    return deps.probe_rusty_tiles().available


real = pytest.mark.skipif(not _real(), reason="needs rusty-tiles 0.4.1 (decode_tile)")


def _glb(document: dict) -> bytes:
    data = json.dumps(document).encode()
    data += b" " * (-len(data) % 4)
    return struct.pack("<IIIII", 0x46546C67, 2, 20 + len(data), len(data), 0x4E4F534A) + data


TWO_PRIMS = {
    "accessors": [{"count": 30}, {"count": 12}, {"count": 9}],
    "meshes": [
        {"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]},
        {
            "primitives": [
                {"attributes": {"POSITION": 2}},
                {"attributes": {"POSITION": 0}, "mode": 1},
            ]
        },
    ],
}


def test_glb_triangles_from_the_json_chunk() -> None:
    assert glb_triangles(_glb(TWO_PRIMS)) == 4 + 3  # indices / 3, then POSITION / 3; lines skipped
    assert glb_triangles(_glb({})) == 0
    assert glb_triangles(b"not a glb") is None
    assert glb_triangles(_glb(TWO_PRIMS)[:30]) is None  # truncated JSON
    broken = {"meshes": [{"primitives": [{"indices": 5}]}], "accessors": []}
    assert glb_triangles(_glb(broken)) is None


def test_triangle_counts_in_an_archive_and_a_directory(tmp_path: Path) -> None:
    archive = tmp_path / "site.3tz"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("content/a.glb", _glb(TWO_PRIMS) + b"\0" * 64)
        zf.writestr("content/b.glb", b"junk")
    assert triangle_counts(archive, ["content/a.glb", "content/b.glb", "absent.glb"]) == {
        "content/a.glb": 7
    }
    folder = tmp_path / "unpacked"
    (folder / "content").mkdir(parents=True)
    (folder / "content" / "a.glb").write_bytes(_glb(TWO_PRIMS))
    assert triangle_counts(folder, ["content/a.glb", "content/missing.glb"]) == {"content/a.glb": 7}
    assert triangle_counts(tmp_path / "nothing.3tz", ["content/a.glb"]) == {}


# --- a fake rusty-tiles ----------------------------------------------------------------------


class FakeTilesError(Exception):
    pass


def _fake(monkeypatch: pytest.MonkeyPatch, arrays: dict, tiles: list[dict] | None = None):
    module = types.ModuleType("rusty_tiles")
    module.TilesError = FakeTilesError
    module.supported_epsg_codes = list
    module.resolve_source_crs = lambda definition: {}
    module.list_tiles = lambda archive: tiles or []
    calls = []

    def decode(archive, member, *, format="glb"):
        calls.append((Path(archive), member, format))
        if member == "bad":
            raise FakeTilesError("no such member")
        return arrays

    module.decode_tile = decode
    module.__version__ = "0.4.1"
    monkeypatch.setitem(sys.modules, "rusty_tiles", module)
    deps.reset_cache()
    return calls


@pytest.fixture(autouse=True)
def _fresh_deps():
    deps.reset_cache()
    yield
    deps.reset_cache()


def _arrays(*, leaf: bool = True, null: int | None = None, values=(0, 0, 0, 1, 1, 1)) -> dict:
    """Two triangles; the feature IDs are rows of the property tables, not IDs."""
    sets = [
        {
            "label": "source_primitive",
            "feature_count": 2,
            "null_feature_id": None,
            "property_table": 0,
            "values": np.array([1, 1, 1, 0, 0, 0], np.uint32),
        }
    ]
    tables = [
        {
            "class": "source_primitive",
            "count": 2,
            "properties": {"primitive_index": np.array([4, 7], np.uint32)},
        }
    ]
    if leaf:
        sets.append(
            {
                "label": "source_triangle",
                "feature_count": 2,
                "null_feature_id": null,
                "property_table": 1,
                "values": np.array(values, np.uint32),
            }
        )
        tables.append(
            {
                "class": "source_triangle",
                "count": 2,
                "properties": {
                    "primitive_index": np.array([7, 4], np.uint32),
                    "triangle_index": np.array([12, 3], np.uint64),
                },
            }
        )
    eye = np.eye(4)
    return {
        "uri": "content/t.glb",
        "depth": 2,
        "role": "leaf" if leaf else "coarse",
        "geometric_error": 0.5,
        "transform": eye,
        "root_transform": eye,
        "primitives": [
            {
                "node": 0,
                "mesh": 0,
                "primitive": 0,
                "material": 0,
                "positions": np.arange(18, dtype=np.float64).reshape(6, 3),
                "indices": np.arange(6, dtype=np.uint32).reshape(2, 3),
                "normals": None,
                "uvs": [np.zeros((6, 2), np.float32)],
                "colors": None,
                "feature_ids": sets,
            }
        ],
        "pages": [np.zeros((4, 8, 4), np.uint8)],
        "property_tables": tables,
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0, "sampler": 0}],
        "samplers": [{"magFilter": 9728}],
    }


def test_decode_resolves_ids_through_the_property_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake(monkeypatch, _arrays())
    tile = decode_tile(Path("site.3tz"), "content/t.glb")
    assert calls == [(Path("site.3tz"), "content/t.glb", "arrays")]
    prim = tile.primitives[0]
    np.testing.assert_array_equal(prim.source_primitive, [7, 4])
    np.testing.assert_array_equal(prim.source_triangle, [12, 3])
    assert tile.is_leaf and tile.triangles == 2 and tile.depth == 2
    assert tile.base_color_page(0) == 0 and tile.base_color_page(None) is None
    assert tile.base_color_page(5) is None


def test_decode_coarse_has_no_source_triangle(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, _arrays(leaf=False))
    prim = decode_tile(Path("site.3tz"), "content/t.glb").primitives[0]
    assert prim.source_triangle is None
    np.testing.assert_array_equal(prim.source_primitive, [7, 4])


def test_decode_null_ids_become_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, _arrays(null=9, values=(9, 9, 9, 1, 1, 1)))
    prim = decode_tile(Path("site.3tz"), "content/t.glb").primitives[0]
    np.testing.assert_array_equal(prim.source_triangle, [NO_ID, 3])


def test_decode_refuses_inconsistent_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, _arrays(values=(1, 1, 1, 0, 0, 0)))  # rows name the other primitive
    with pytest.raises(OutputTilesError, match="disagree"):
        decode_tile(Path("site.3tz"), "content/t.glb")
    _fake(monkeypatch, _arrays(values=(5, 5, 5, 0, 0, 0)))
    with pytest.raises(OutputTilesError, match="beyond their property table"):
        decode_tile(Path("site.3tz"), "content/t.glb")
    arrays = _arrays()
    arrays["property_tables"][1]["properties"].pop("triangle_index")
    _fake(monkeypatch, arrays)
    with pytest.raises(OutputTilesError, match="no 'triangle_index'"):
        decode_tile(Path("site.3tz"), "content/t.glb")


def test_rusty_tiles_errors_become_output_tiles_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, _arrays())
    with pytest.raises(OutputTilesError, match="FakeTilesError: no such member"):
        decode_tile(Path("site.3tz"), "bad")


def test_list_tiles(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [[1.0, 0, 0, 5.0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]
    _fake(
        monkeypatch,
        {},
        [{"uri": "a.glb", "depth": 0, "role": "coarse", "geometric_error": 2, "transform": rows}],
    )
    (entry,) = list_tiles(Path("x.3tz"))
    assert (entry.uri, entry.depth, entry.role, entry.is_leaf) == ("a.glb", 0, "coarse", False)
    assert entry.transform[0, 3] == 5.0 and entry.transform.dtype == np.float64


def test_without_decode_tile_nothing_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {})
    del sys.modules["rusty_tiles"].decode_tile
    deps.reset_cache()
    with pytest.raises(OutputTilesError, match=r"needs rusty-tiles 0\.4\.1 or later"):
        list_tiles(Path("x.3tz"))


# --- the real wheel --------------------------------------------------------------------------


@real
def test_real_leaf_ids_and_positions_match_the_source(tmp_path: Path) -> None:
    """A LOCAL lossless publish: every decoded leaf triangle is its source triangle."""
    import rusty_tiles

    n = 80  # 12,800 triangles: leaves and a coarse root
    xs, ys = np.meshgrid(np.arange(n + 1, dtype=np.float32), np.arange(n + 1, dtype=np.float32))
    co = np.stack([xs.ravel(), ys.ravel(), np.sin(xs.ravel() / 4)], 1).astype(np.float32)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    tris = np.concatenate(
        [np.stack([a, a + 1, a + n + 2], 1), np.stack([a, a + n + 2, a + n + 1], 1)]
    )
    tris = tris.astype(np.uint32)
    half = len(tris) // 2
    prims = tuple(
        PrimitiveData(
            material=m,
            positions=co,
            indices=part,
            source_object=np.zeros(len(part), np.int32),
            source_polygon=np.arange(len(part), dtype=np.int32),
        )
        for m, part in ((0, tris[:half]), (1, tris[half:]))
    )
    scene = SceneExport(
        ("Grid",), (MaterialData("A"), MaterialData("B", base_color_factor=(1, 0, 0, 1))), (), prims
    )
    write_glb(scene, tmp_path / "source.glb")
    face_map = build_face_map(scene, {"Grid": "x"})
    rusty_tiles.mesh_to_tileset(tmp_path / "source.glb", tmp_path / "site.3tz", profile="lossless")
    tiles = list_tiles(tmp_path / "site.3tz")
    assert tiles[0].depth == 0 and not tiles[0].is_leaf
    counts = triangle_counts(tmp_path / "site.3tz", [t.uri for t in tiles])
    assert sum(counts[t.uri] for t in tiles if t.is_leaf) == len(tris)
    seen = 0
    for entry in tiles:
        tile = decode_tile(tmp_path / "site.3tz", entry.uri)
        assert tile.triangles == counts[entry.uri]
        for prim in tile.primitives:
            if not entry.is_leaf:
                assert prim.source_triangle is None
                assert set(prim.source_primitive.tolist()) <= {0, 1}
                continue
            source = np.stack(
                [
                    prims[p].indices[t]
                    for p, t in zip(prim.source_primitive, prim.source_triangle, strict=True)
                ]
            )
            # Same corners, possibly rotated.
            got = prim.positions[prim.indices]
            want = co.astype(np.float64)[source]
            gaps = np.stack(
                [np.abs(np.roll(got, r, axis=1) - want).max(axis=(1, 2)) for r in range(3)]
            )
            assert gaps.min(axis=0).max() == 0.0
            rows = face_map.primitive_offsets[prim.source_primitive] + prim.source_triangle
            assert (face_map.source_polygon[rows] == prim.source_triangle).all()
            seen += prim.triangles
    assert seen == len(tris)
