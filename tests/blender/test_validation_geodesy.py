"""Geodesy validation in Blender: Measure's operators and publishing placement against
independent references (docs/validation/geodesy.md, tests/data/validation/geodesy/).

The core suite (tests/core/test_validation_geodesy.py) checks the maths; these
check the Blender path: float32 cursor positions and world-space mesh triangles
through the operators, and the bundled rusty-tiles placing a published mesh.
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import _harness as h
import bpy
import numpy as np

DATA = Path(__file__).resolve().parents[1] / "data" / "validation" / "geodesy"


def _available() -> bool:
    return h.CONFIG.expect == "available"


def _site(name: str) -> dict:
    data = json.loads((DATA / "measure.json").read_text())
    return next(s for s in data["sites"] if s["name"] == name)


def _reset() -> None:
    h.reset_scene()
    props = bpy.context.scene.geotools_analysis.measure
    for prop in props.bl_rna.properties:
        if prop.identifier not in {"rna_type", "name"}:
            props.property_unset(prop.identifier)


def _project(entry: dict, height_offset: float = 0.0) -> None:
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = entry["crs"]
    props.origin_e = repr(entry["origin"][0])
    props.origin_n = repr(entry["origin"][1])
    props.source_offset_a = "0.0"
    props.height_offset = repr(height_offset)


def _analysis():
    return h.submodule("blender.analysis")


def test_measure_operator_against_geodesics() -> None:
    """Point A and B from the 3D cursor, in a zone-edge MGA project and an Albers one
    (not conformal): the ground distance and true bearing of 10 km lines against
    GeographicLib, as in the core suite (0.05 ppm, 0.05")."""
    if not _available():
        return
    for name in ("mga56-edge", "albers"):
        entry = _site(name)
        for line in entry["lines"]:
            length = math.dist(line["start"][:2], line["end"][:2])
            if not 9_000 < length < 11_000:
                continue
            _reset()
            _project(entry, line["start_height"])
            scene = bpy.context.scene
            for which, point in (("A", line["start"]), ("B", line["end"])):
                scene.cursor.location = point
                result, error = h.call_op(bpy.ops.geotools.point_from_cursor, point=which)
                assert result == {"FINISHED"}, error
            m = _analysis().load_measurement(scene.geotools_analysis.measure.distance)
            assert abs(m.ground_distance / line["ground_gaussian"] - 1.0) < 0.05e-6, (name, m)
            bearing = (m.true_bearing - line["azimuth_middle"] + 180.0) % 360.0 - 180.0
            assert abs(bearing) * 3600.0 < 0.05, (name, bearing)


def test_area_operator_against_a_geodesic_polygon() -> None:
    """The selected faces of a 1 km square mesh (16 x 16 cells, float32 vertices as
    Blender holds them) against GeographicLib's polygon area, to 1e-8."""
    if not _available():
        return
    for name in ("mga56-edge", "albers", "webmercator"):
        entry = _site(name)
        polygon = entry["polygons"][0]
        _reset()
        _project(entry, polygon["height"])
        c = np.asarray(polygon["corners"])
        s = np.linspace(0.0, 1.0, 17)
        u, v = (w[..., None] for w in np.meshgrid(s, s, indexing="ij"))
        xy = (1 - u) * (1 - v) * c[0] + u * (1 - v) * c[1] + u * v * c[2] + (1 - u) * v * c[3]
        verts = [(float(x), float(y), 0.0) for x, y in xy.reshape(-1, 2)]
        n = 17
        faces = [
            (i * n + j, (i + 1) * n + j, (i + 1) * n + j + 1, i * n + j + 1)
            for i in range(n - 1)
            for j in range(n - 1)
        ]
        mesh = bpy.data.meshes.new("square")
        mesh.from_pydata(verts, [], faces)
        obj = bpy.data.objects.new("square", mesh)
        bpy.context.scene.collection.objects.link(obj)
        bpy.context.view_layer.objects.active = obj
        mesh.polygons.foreach_set("select", np.ones(len(faces), dtype=bool))
        result, error = h.call_op(bpy.ops.geotools.measure_area)
        assert result == {"FINISHED"}, error
        area = _analysis().load_area(bpy.context.scene.geotools_analysis.measure.area)
        assert abs(area.ground_plan / polygon["ground_area"] - 1.0) < 1e-8, (name, area)


def test_published_placement_against_the_closed_form() -> None:
    """With the bundled rusty-tiles: a mesh published (lossless) in MGA zone 56 and in
    Australian Albers lands within 1 mm of the closed-form ECEF of every vertex."""
    if not _available():
        return
    import rusty_tiles

    gw = h.submodule("core.gltf_writer")
    tiles = h.submodule("core.output_tiles")
    placement_mod = h.submodule("core.placement")
    data = json.loads((DATA / "placement.json").read_text())
    co = np.asarray(data["vertices"], dtype=np.float32)
    n = 5
    a = (np.arange(n - 1)[None, :] + n * np.arange(n - 1)[:, None]).ravel()
    tris = np.concatenate([np.stack([a, a + 1, a + n + 1], 1), np.stack([a, a + n + 1, a + n], 1)])
    prim = gw.PrimitiveData(
        material=0,
        positions=co,
        indices=tris.astype(np.uint32),
        source_object=np.zeros(len(tris), np.int32),
        source_polygon=np.arange(len(tris), dtype=np.int32),
    )
    for entry in data["source_crs"]:
        if entry["name"] not in ("mga56-edge", "albers-gda94"):
            continue
        placement = placement_mod.Placement(
            placement_mod.PlacementMode.SOURCE_CRS,
            source_crs=entry["crs"],
            source_offset=tuple(entry["source_offset"]),
            height_offset=entry["height_offset"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            source, archive = Path(tmp) / "source.glb", Path(tmp) / "site.3tz"
            gw.write_glb(gw.SceneExport(("G",), (gw.MaterialData("m"),), (), (prim,)), source)
            rusty_tiles.mesh_to_tileset(
                source, archive, profile="lossless", **placement.mesh_to_tileset_kwargs()
            )
            got = np.full((len(co), 3), np.nan)
            for tile_entry in tiles.list_tiles(archive):
                if not tile_entry.is_leaf:
                    continue
                tile = tiles.decode_tile(archive, tile_entry.uri)
                for p in tile.primitives:
                    world = (np.c_[p.positions, np.ones(len(p.positions))] @ tile.transform.T)[
                        :, :3
                    ]
                    got[tris[p.source_triangle].ravel()] = world[p.indices.ravel()]
        error = np.linalg.norm(got - np.asarray(entry["ecef"]), axis=1)
        assert error.max() < 1e-3, (entry["name"], error.max())
