"""The drape-contexts recipe and its example (M8 stream 3).

``scripts/make_example_drape.py`` runs in a separate factory-startup Blender
(the way the recipe tells people to run it), into a temporary directory. The
test then opens the .blend it wrote and checks the recipe's promises with stock
nodes only: the contexts follow the terrain, lowered by ``depth_top``; the
volumes are closed and as thick as ``depth_bottom - depth_top``; the draped
tops export to a GeoPackage with absolute Z; an edit to the GeoJSON re-drapes
after a refresh; and the contexts publish as vector tiles and republish
incrementally (with rusty-tiles bundled).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import bpy
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make_example_drape.py"
EXAMPLE = REPO_ROOT / "examples" / "drape-contexts"
E0, N0, A0 = 500_000.0, 5_400_000.0, 100.0  # as in the script
MAX_EXAMPLE_BYTES = 3 * 2**20
JOIN_SECONDS = 180.0


@contextmanager
def _generated() -> Iterator[Path]:
    """Run the example script into a temporary directory; yield that directory."""
    tmp = Path(tempfile.mkdtemp(prefix="geotools-drape-"))
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        done = subprocess.run(
            [
                bpy.app.binary_path,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                "1",
                "--python",
                str(SCRIPT),
                "--",
                "--out",
                str(tmp),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            check=False,
        )
        assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
        yield tmp
    finally:
        h.submodule("blender.publish").shutdown_job()
        bpy.ops.wm.read_homefile(use_empty=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _open(blend: Path) -> None:
    assert bpy.ops.wm.open_mainfile(filepath=str(blend), load_ui=False) == {"FINISHED"}


def _array(collection, attr: str, count: int, dtype, width: int = 1) -> np.ndarray:
    out = np.empty(count * width, dtype=dtype)
    collection.foreach_get(attr, out)
    return out.reshape(-1, width) if width > 1 else out


class Evaluated:
    """An object's evaluated mesh as numpy arrays (read, then the mesh is freed)."""

    def __init__(self, obj: bpy.types.Object) -> None:
        evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        try:
            self.co = _array(mesh.vertices, "co", len(mesh.vertices), np.float32, 3)
            n_faces, n_loops = len(mesh.polygons), len(mesh.loops)
            self.loop_vertex = _array(mesh.loops, "vertex_index", n_loops, np.int32)
            self.loop_edge = _array(mesh.loops, "edge_index", n_loops, np.int32)
            totals = _array(mesh.polygons, "loop_total", n_faces, np.int32)
            self.loop_face = np.repeat(np.arange(n_faces), totals)
            self.n_edges = len(mesh.edges)
            mesh.calc_loop_triangles()
            tris = mesh.loop_triangles
            self.tris = _array(tris, "vertices", len(tris), np.int32, 3)
            self.face_attrs = {}
            for name in ("depth_top", "depth_bottom", "fid"):
                attr = mesh.attributes.get(name)
                if attr is not None and attr.domain == "FACE":
                    dtype = np.int32 if attr.data_type == "INT" else np.float32
                    self.face_attrs[name] = _array(attr.data, "value", n_faces, dtype)
        finally:
            evaluated.to_mesh_clear()

    def vertex_values(self, name: str) -> np.ndarray:
        """A face attribute on the vertices (every vertex here has one feature)."""
        out = np.full(len(self.co), np.nan)
        out[self.loop_vertex] = self.face_attrs[name][self.loop_face]
        return out

    def signed_volume(self) -> float:
        v = self.co.astype(np.float64)[self.tris]
        return float(np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6.0)


def _terrain_height(terrain: bpy.types.Object, xy: np.ndarray) -> np.ndarray:
    """Terrain z at local x, y, bilinear on its regular grid (no per-vertex loop)."""
    mesh = terrain.data
    co = _array(mesh.vertices, "co", len(mesh.vertices), np.float32, 3).astype(np.float64)
    n = round(float(np.sqrt(len(co))))
    grid = co[:, 2].reshape(n, n)
    x0, y0 = co[0, 0], co[0, 1]
    step = co[1, 0] - co[0, 0]
    fx = np.clip((xy[:, 0] - x0) / step, 0, n - 1 - 1e-9)
    fy = np.clip((xy[:, 1] - y0) / step, 0, n - 1 - 1e-9)
    i, j = fy.astype(int), fx.astype(int)
    ty, tx = fy - i, fx - j
    return (
        grid[i, j] * (1 - tx) * (1 - ty)
        + grid[i, j + 1] * tx * (1 - ty)
        + grid[i + 1, j] * (1 - tx) * ty
        + grid[i + 1, j + 1] * tx * ty
    )


def _polygon_areas(geojson: dict) -> dict[int, float]:
    """XY area of each feature (exterior minus holes), by id."""

    def ring_area(ring: list) -> float:
        a = np.asarray(ring, dtype=np.float64)[:, :2] - [E0, N0]
        return 0.5 * float(np.sum(a[:-1, 0] * a[1:, 1] - a[1:, 0] * a[:-1, 1]))

    out = {}
    for f in geojson["features"]:
        exterior, *holes = f["geometry"]["coordinates"]
        # RFC 7946 winding: exteriors anticlockwise, holes clockwise.
        assert ring_area(exterior) > 0 and all(ring_area(r) < 0 for r in holes)
        out[f["id"]] = ring_area(exterior) + sum(ring_area(r) for r in holes)
    return out


def _expected_volume(geojson: dict) -> float:
    areas = _polygon_areas(geojson)
    return sum(
        areas[f["id"]] * (f["properties"]["depth_bottom"] - f["properties"]["depth_top"])
        for f in geojson["features"]
    )


def _check_drape(contexts: bpy.types.Object, terrain: bpy.types.Object) -> Evaluated:
    ev = Evaluated(contexts)
    ground = _terrain_height(terrain, ev.co[:, :2].astype(np.float64))
    depth = ev.vertex_values("depth_top")
    assert not np.isnan(depth).any()
    error = np.abs(ev.co[:, 2] - (ground - depth))
    assert error.max() < 0.01, f"drape is off by up to {error.max():.4f} m"
    return ev


# --- the committed example -------------------------------------------------------------


def test_committed_example_is_small_and_synthetic() -> None:
    files = sorted(p.name for p in EXAMPLE.iterdir() if p.is_file())
    assert files == [
        "contexts.geojson",
        "drape-contexts.blend",
        "terrain.ply",
        "terrain_offset.txt",
    ], files
    assert sum((EXAMPLE / name).stat().st_size for name in files) < MAX_EXAMPLE_BYTES
    data = json.loads((EXAMPLE / "contexts.geojson").read_text(encoding="utf-8"))
    assert data["crs"]["properties"]["name"] == "urn:ogc:def:crs:EPSG::32631"
    # The invented site sits at round UTM numbers; every coordinate is within 30 m.
    coords = np.array(
        [p for f in data["features"] for r in f["geometry"]["coordinates"] for p in r]
    )
    assert np.abs(coords - [E0, N0]).max() <= 30.0
    lines = (EXAMPLE / "terrain_offset.txt").read_text(encoding="utf-8").splitlines()
    assert lines == ["E: 500000.000", "N: 5400000.000", "A: 100.000"]


# --- the script, its .blend and the recipe ----------------------------------------------


def test_example_script_runs_and_the_recipe_holds() -> None:
    links = h.submodule("blender.links")
    vectors = h.submodule("blender.vectors")
    gpkg = h.submodule("core.vector.gpkg")
    kind = h.submodule("core.vector.model").Kind
    with _generated() as out:
        names = sorted(p.name for p in out.iterdir())
        assert names == sorted(p.name for p in EXAMPLE.iterdir() if p.is_file()), names
        geojson_path = out / "contexts.geojson"
        assert geojson_path.read_bytes() == (EXAMPLE / "contexts.geojson").read_bytes()
        geojson = json.loads(geojson_path.read_text(encoding="utf-8"))
        _open(out / "drape-contexts.blend")

        scene = bpy.context.scene
        props = scene.geotools
        assert props.placement_mode == "SOURCE_CRS" and props.crs == "EPSG:32631"
        assert float(props.origin_e) == E0 and float(props.source_offset_a) == A0
        terrain = bpy.data.objects["terrain"]
        contexts = bpy.data.objects["contexts"]
        volumes = bpy.data.objects["contexts.volumes"]
        assert props.source_collection.name == "Site"
        assert contexts.geotools_link.filepath == "//contexts.geojson"
        assert links.check_link(contexts) == "OK"

        # Stock nodes only: GeoTools ships no node groups.
        allowed_prefixes = (
            "GeometryNode",
            "ShaderNode",
            "FunctionNode",
            "NodeGroup",
            "NodeReroute",
        )
        for tree in bpy.data.node_groups:
            for node in tree.nodes:
                assert node.bl_idname.startswith(allowed_prefixes), node.bl_idname
                assert node.bl_idname != "GeometryNodeGroup", "no nested groups"

        # Draped on the terrain, lowered by depth_top.
        ev = _check_drape(contexts, terrain)
        assert sorted(set(ev.face_attrs["fid"].tolist())) == list(range(1, 10))

        # Volumes: closed, outward-facing, depth_bottom - depth_top thick.
        vol = Evaluated(volumes)
        assert np.all(np.bincount(vol.loop_edge, minlength=vol.n_edges) == 2), "not closed"
        expected = _expected_volume(geojson)
        assert abs(vol.signed_volume() - expected) < 0.005 * expected, (
            vol.signed_volume(),
            expected,
        )

        # Back to a GeoPackage with absolute Z, in the project CRS.
        gpkg_path = out / "draped.gpkg"
        counts, _warnings = vectors.export_vectors(
            bpy.context, [contexts], gpkg_path, "GPKG", "contexts", True
        )
        assert counts == {kind.POLYGONS: 9}
        data = gpkg.read_layer(gpkg_path, "contexts")
        assert data.crs.epsg == "EPSG:32631"
        assert sorted(data.fids.tolist()) == list(range(1, 10))
        coords = data.geometries[kind.POLYGONS].coords
        local = np.column_stack([coords[:, 0] - E0, coords[:, 1] - N0])
        ground = _terrain_height(terrain, local) + A0
        depth_by_fid = {f["id"]: f["properties"]["depth_top"] for f in geojson["features"]}
        geoms = data.geometries[kind.POLYGONS]
        feature_of_coord = geoms.coord_feature()
        depth = np.array([depth_by_fid[int(data.fids[r])] for r in geoms.rows])[feature_of_coord]
        assert np.abs(coords[:, 2] - (ground - depth)).max() < 0.01
        assert coords[:, 2].min() > A0  # absolute heights, not local

        # A QGIS edit: C101 is deeper, C109 moved 1 m east. Refresh re-drapes.
        before = Evaluated(volumes).signed_volume()
        for f in geojson["features"]:
            if f["properties"]["context"] == "C101":
                f["properties"]["depth_top"] = 0.55
            if f["properties"]["context"] == "C109":
                f["geometry"]["coordinates"] = [
                    [[x + 1.0, y] for x, y in ring] for ring in f["geometry"]["coordinates"]
                ]
        geojson_path.write_text(json.dumps(geojson, indent=1), encoding="utf-8")
        st = geojson_path.stat()
        os.utime(geojson_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        assert links.check_link(contexts) == "CHANGED"
        result = links.refresh_link(contexts)
        assert (result.added, result.removed, result.changed) == (0, 0, 2), result
        assert contexts.modifiers["Drape"].node_group.name == "Drape on Terrain"
        ev = _check_drape(contexts, terrain)
        after = Evaluated(volumes).signed_volume()
        area_c101 = _polygon_areas(geojson)[1]
        assert abs((before - after) - area_c101 * 0.30) < 0.01 * area_c101
        moved = ev.co[ev.vertex_values("fid") == 9]
        assert abs(moved[:, 0].mean() - 21.0) < 0.05  # the pit's centre was at x = 20

        # Publish the draped contexts as vector tiles, then incrementally.
        profile = props.build_profiles[props.build_profile_index]
        assert profile.name == "Contexts" and profile.output_type == "VECTOR"
        assert profile.vector_collection.name == "Contexts"
        publish = h.submodule("blender.publish")
        result_, error = h.call_op(bpy.ops.geotools.publish)
        if h.CONFIG.expect != "available":
            assert error is not None and "rusty" in error.lower(), error
            return
        assert error is None and result_ == {"FINISHED"}, error
        _finish(publish)
        record = json.loads((out / "contexts.geotools" / "build.json").read_text())
        assert record["output_type"] == "vector"
        assert record["placement"]["source_crs"] == "EPSG:32631"
        assert record["validation"]["ok"] is True
        assert record["vectors"]["tables"] == [
            {"table": "contexts", "object": "contexts", "kind": "polygons", "features": 9}
        ]
        assert profile.last_features == "9" and profile.last_validation == "OK"

        # Another edit, then republish: rusty-tiles reuses the previous archive.
        geojson["features"][2]["properties"]["depth_top"] = 0.3
        geojson_path.write_text(json.dumps(geojson, indent=1), encoding="utf-8")
        os.utime(geojson_path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
        links.refresh_link(contexts)
        assert h.call_op(bpy.ops.geotools.publish) == ({"FINISHED"}, None)
        _finish(publish)
        record = json.loads((out / "contexts.geotools" / "build.json").read_text())
        reuse = record["vectors"]["reuse"]
        assert reuse["used"] is True and reuse["report"]["previousTileset"] is True
        assert record["validation"]["ok"] is True


def _finish(publish) -> None:
    job = publish.active_job()
    assert job is not None, "no publish job is running"
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    assert publish.update_from_job() is None
    assert job.state.name == "DONE", job.error
