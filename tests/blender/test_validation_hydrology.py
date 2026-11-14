"""Hydrology, contours and sections validation, the Blender half (docs/validation/hydrology.md).

* **Compute Hydrology** on the two-bowl surface gives exactly the core's answer
  (drops cast by Blender's Raycast, layers written to the mesh), and its
  Depressions object holds the core's depression table.
* **Flow Path (Quick Look)** follows the lines of steepest descent of two
  Gaussian hills, integrated by SciPy 1.18.1 ``solve_ivp`` (DOP853) into the
  committed references.
* **Export Vectors** writes the Depressions, Flow Paths, Contours and Sections
  objects of a CRS project as GeoPackage and GeoJSON; they are read back here
  without GeoTools (sqlite3 and a few lines of WKB, and json): CRS, attribute
  names, values and absolute coordinates.

Set ``GEOTOOLS_HYDROLOGY_REPORT=/path/report.json`` to write the measured
statistics (the doc's tables come from it).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from collections.abc import Iterator
from pathlib import Path

import _harness as h
import _hydrology_surfaces as hs
import bpy
import numpy as np

REFERENCES = (
    Path(__file__).resolve().parents[1] / "data" / "validation" / "hydrology" / "references.npz"
)
E0, N0, A0 = 500_000.0, 6_960_000.0, 100.0
REPORT: dict[str, object] = {}


def _report(key: str, value: object) -> None:
    REPORT[key] = value
    path = os.environ.get("GEOTOOLS_HYDROLOGY_REPORT")
    if path:
        report = json.loads(Path(path).read_text()) if Path(path).is_file() else {}
        report.update(REPORT)
        Path(path).write_text(json.dumps(report, indent=1, sort_keys=True))


@contextlib.contextmanager
def _tmpdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-hydro-val-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _reset() -> None:
    h.reset_scene()
    for name in ("geotools_hydrology", "geotools_contours"):
        group = getattr(bpy.context.scene, name)
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"}:
                group.property_unset(prop.identifier)
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _mesh_object(name, pts, tris, collection) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(pts))
    mesh.vertices.foreach_set("co", np.asarray(pts, np.float32).ravel())
    tris = np.asarray(tris, np.int32)
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _two_bowls(collection) -> tuple[bpy.types.Object, np.ndarray, np.ndarray]:
    z, x, y = hs.dem(*hs.HYDRO["two_bowls"])
    pts, tris = hs.grid_mesh(z, x, y)
    pts = pts.astype(np.float32).astype(np.float64)  # what Blender holds
    return _mesh_object("Bowls", pts, tris, collection), pts, tris


def _crs_project() -> None:
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    props.origin_e = repr(E0)
    props.origin_n = repr(N0)
    props.source_offset_a = repr(A0)


def _core(pts, tris, **kwargs):
    """The core's analysis of a single-sheet mesh: every drop leaves the model."""
    hy = h.submodule("analysis.hydrology")
    starts = np.arange(0, 3 * len(tris) + 1, 3)
    corner_verts = tris.ravel()
    edges, n_edges = hy.corner_edges(starts, corner_verts)
    surface = hy.surface_from_mesh(pts, starts, corner_verts, edges, n_edges)
    queries = hy.drop_queries(surface, 1e-6)
    miss = np.full(len(queries.face), -1)
    drops = hy.resolve_drops(queries, surface, miss, queries.origin, miss, queries.origin)
    return surface, hy.analyse(surface, drops, **kwargs)


def _values(obj, name, domain="FACE", dtype=np.float32) -> np.ndarray:
    attr = obj.data.attributes[name]
    assert attr.domain == domain, (name, attr.domain)
    out = np.empty(len(attr.data), dtype)
    attr.data.foreach_get("value", out)
    return out


def _co(obj) -> np.ndarray:
    co = np.empty(3 * len(obj.data.vertices), np.float32)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3).astype(np.float64)


# --- Compute Hydrology against the core ---------------------------------------------------------


def test_hydrology_operator_equals_the_core() -> None:
    _reset()
    bowls, pts, tris = _two_bowls(_source())
    props = bpy.context.scene.geotools_hydrology
    props.min_depth = 0.01
    props.depression_points = True
    props.flow_lines = True
    props.channel_area = 0.5
    props.channel_count = 0
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None and result == {"FINISHED"}, error
    out = json.loads(props.result)
    assert out["stats"]["drips"] == 0 and out["stats"]["drops"] == out["stats"]["drops_out"]
    surface, core = _core(pts, tris, min_depth=0.01)
    np.testing.assert_allclose(
        _values(bowls, "gt_flow_accumulation"), core.accumulation.astype(np.float32), rtol=1e-6
    )
    np.testing.assert_allclose(_values(bowls, "gt_ponded_depth"), core.depth, atol=1e-6)
    np.testing.assert_array_equal(_values(bowls, "gt_depression", dtype=np.int32), core.depression)
    table = core.depressions
    assert out["depressions"] == table.count == 2
    points = bpy.data.objects["Depressions"]
    np.testing.assert_allclose(_co(points), table.pour_point, atol=1e-5)
    np.testing.assert_allclose(_values(points, "level", "POINT"), table.level, atol=1e-6)
    np.testing.assert_allclose(_values(points, "volume", "POINT"), table.volume, rtol=1e-6)
    np.testing.assert_allclose(_values(points, "max_depth", "POINT"), table.max_depth, atol=1e-6)
    lines = bpy.data.objects["Flow Paths"]
    assert len(lines.data.edges) > 0 and out["flow_lines"] > 0
    # Every flow-path vertex is a face centre on a channel (no drops on a single sheet).
    channel = core.accumulation >= 0.5
    centres = surface.centroids[channel]
    co = _co(lines)
    gap = np.min(np.linalg.norm(co[:, None, :2] - centres[None, :, :2], axis=2), axis=1)
    assert gap.max() < 1e-5
    _report("operator_two_bowls", {"faces": surface.n, "depressions": table.count})


# --- Flow Path (Quick Look) against SciPy ----------------------------------------------------------


def test_flow_path_quick_look_follows_steepest_descent() -> None:
    _reset()
    source = _source()
    cell = 0.025
    half = hs.HILLS_HALF
    z, x, y = hs.dem(hs.hills, -half, half, -half, half, cell)
    pts, tris = hs.grid_mesh(z, x, y)
    hills = _mesh_object("Hills", pts, tris, source)
    seeds = bpy.data.meshes.new("Seeds")
    seeds.from_pydata([(sx, sy, 0.0) for sx, sy in hs.HILLS_SEEDS], [], [])
    seed_obj = bpy.data.objects.new("Seeds", seeds)
    bpy.context.scene.collection.objects.link(seed_obj)
    bpy.context.view_layer.update()
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj in (hills, seed_obj))
    bpy.context.view_layer.objects.active = hills
    assert bpy.ops.geotools.analysis_add(preset="flow_path") == {"FINISHED"}
    obj = bpy.context.view_layer.objects.active
    ap = h.submodule("blender.analysis_presets")
    with np.load(REFERENCES) as refs:
        ref_points, ref_curve = refs["fp_points"], refs["fp_curve"]
    report = {}
    # A 2 cm step, and the default 10 cm: the path is explicit Euler on the facets, so its
    # error grows with the step (and the facets' own departure from the surface).
    for step, max_off, median_off in ((0.02, 0.02, 0.01), (0.1, 0.1, 0.05)):
        ap.set_input(obj.modifiers[0], "Step", step)
        mesh = obj.evaluated_get(bpy.context.evaluated_depsgraph_get()).data
        co = np.empty(3 * len(mesh.vertices), np.float32)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3).astype(np.float64)
        edges = np.empty(2 * len(mesh.edges), np.int32)
        mesh.edges.foreach_get("vertices", edges)
        edges = edges.reshape(-1, 2)
        seed_of_edge = np.empty(len(mesh.edges), np.int32)
        mesh.attributes["seed"].data.foreach_get("value", seed_of_edge)
        stats = {}
        for k in range(len(hs.HILLS_SEEDS)):
            path = co[np.unique(edges[seed_of_edge == k])]
            curve = ref_points[ref_curve == k]
            a3 = np.column_stack([curve[:-1], np.zeros(len(curve) - 1)])
            b3 = np.column_stack([curve[1:], np.zeros(len(curve) - 1)])
            # Up to where the reference stops, 0.05 from the edge; the path runs on to it.
            reach = np.abs(path[:, :2]).max(axis=1).max()
            inside = np.abs(path[:, :2]).max(axis=1) <= half - 0.05
            off = hs.point_segment_distance(path[inside], a3, b3)
            # Heights are the surface's: each step is draped onto the mesh.
            zs = hs.hills(path[:, 0], path[:, 1])
            stats[k] = {
                "points": len(path),
                "max_off": float(off.max()),
                "median_off": float(np.median(off)),
                "reach": float(reach),
                "z_error": float(np.abs(zs - path[:, 2]).max()),
            }
            assert off.max() < max_off, (step, k, stats[k])
            assert np.median(off) < median_off, (step, k, stats[k])
            assert reach > half - step - 0.01, (step, k, stats[k])
            assert np.abs(zs - path[:, 2]).max() < 1e-3
        report[str(step)] = stats
    _report("flow_path_hills", {"cell": cell, "steps": report})


# --- Export Vectors read back without GeoTools ------------------------------------------------------


def _wkb_coords(blob: bytes) -> list[np.ndarray]:
    """Line strings (or one point) of a GeoPackage geometry blob, parsed by hand."""
    assert blob[:2] == b"GP"
    flags = blob[3]
    order = "<" if flags & 1 else ">"
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}[(flags >> 1) & 7]
    wkb = memoryview(blob)[8 + envelope :]

    def geom(at: int) -> tuple[list[np.ndarray], int]:
        bo = "<" if wkb[at] == 1 else ">"
        (kind,) = struct.unpack_from(bo + "I", wkb, at + 1)
        has_z = kind > 1000 or bool(kind & 0x80000000)
        base = (kind & 0x0FFFFFFF) % 1000
        dims = 3 if has_z else 2
        at += 5
        if base == 1:
            point = np.array(struct.unpack_from(bo + "d" * dims, wkb, at))
            return [point[None, :]], at + 8 * dims
        if base == 2:
            (n,) = struct.unpack_from(bo + "I", wkb, at)
            values = struct.unpack_from(bo + "d" * dims * n, wkb, at + 4)
            return [np.array(values).reshape(n, dims)], at + 4 + 8 * dims * n
        assert base in (4, 5), kind  # multi point / multi line string
        (n,) = struct.unpack_from(bo + "I", wkb, at)
        at += 4
        out = []
        for _ in range(n):
            parts, at = geom(at)
            out += parts
        return out, at

    del order
    return geom(0)[0]


def _read_gpkg(path: Path, table: str) -> dict:
    with contextlib.closing(sqlite3.connect(path)) as db:
        srs = db.execute(
            "SELECT s.organization, s.organization_coordsys_id FROM gpkg_contents c "
            "JOIN gpkg_spatial_ref_sys s ON s.srs_id = c.srs_id WHERE c.table_name = ?",
            (table,),
        ).fetchone()
        geom_col, z = db.execute(
            "SELECT column_name, z FROM gpkg_geometry_columns WHERE table_name = ?", (table,)
        ).fetchone()
        cursor = db.execute(f'SELECT * FROM "{table}"')
        names = [d[0] for d in cursor.description]
        rows = cursor.fetchall()
    g = names.index(geom_col)
    return {
        "srs": srs,
        "z": z,
        "columns": [n for n in names if n != geom_col],
        "rows": [dict(zip(names, r, strict=True)) for r in rows],
        "geoms": [_wkb_coords(r[g]) for r in rows],
    }


def _read_geojson(path: Path) -> dict:
    data = json.loads(path.read_text())
    geoms = []
    for f in data["features"]:
        g = f["geometry"]
        if g["type"] == "Point":
            geoms.append([np.array([g["coordinates"]])])
        elif g["type"] == "LineString":
            geoms.append([np.array(g["coordinates"])])
        else:
            geoms.append([np.array(p) for p in g["coordinates"]])
    return {
        "crs": data.get("crs", {}).get("properties", {}).get("name", ""),
        "rows": [f["properties"] for f in data["features"]],
        "geoms": geoms,
    }


def _export(tmp: Path, name: str, fmt: str) -> Path:
    path = tmp / f"{name.replace(' ', '_')}.{'gpkg' if fmt == 'GPKG' else 'geojson'}"
    result, error = h.call_op(
        bpy.ops.geotools.export_vector,
        filepath=str(path),
        file_format=fmt,
        object_name=name,
        layer="out",
    )
    assert error is None and result == {"FINISHED"}, error
    return path


def test_export_vectors_round_trips_hydrology_contours_and_sections() -> None:
    _reset()
    _crs_project()
    source = _source()
    _bowls, pts, tris = _two_bowls(source)
    props = bpy.context.scene.geotools_hydrology
    props.min_depth = 0.01
    props.depression_points = True
    props.flow_lines = True
    props.channel_area = 0.5
    props.channel_count = 3
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None and result == {"FINISHED"}, error
    contours = bpy.context.scene.geotools_contours
    contours.interval = "0.25"
    contours.base = "0"
    result, error = h.call_op(bpy.ops.geotools.contours)
    assert error is None and result == {"FINISHED"}, error
    line = bpy.data.meshes.new("Line")
    line.from_pydata([(-2.3, -0.4, 0.0), (0.1, 0.2, 0.0), (2.2, -0.3, 0.0)], [(0, 1), (1, 2)], [])
    line_obj = bpy.data.objects.new("Line", line)
    bpy.context.scene.collection.objects.link(line_obj)
    contours.line = line_obj
    result, error = h.call_op(bpy.ops.geotools.sections)
    assert error is None and result == {"FINISHED"}, error

    expect = {
        "Depressions": ("depression", "level", "volume", "area", "max_depth", "faces", "catchment"),
        "Flow Paths": ("accumulation", "network", "length"),
        "Contours": ("elevation", "index", "major", "closed", "length"),
        "Sections": ("section", "chainage_from", "chainage_to", "length"),
    }
    offset = np.array([E0, N0, A0])
    found = {}
    with _tmpdir() as tmp:
        for name, attrs in expect.items():
            obj = bpy.data.objects[name]
            local = _co(obj)
            for fmt in ("GPKG", "GEOJSON"):
                path = _export(tmp, name, fmt)
                data = _read_gpkg(path, "out") if fmt == "GPKG" else _read_geojson(path)
                if fmt == "GPKG":
                    assert data["srs"] == ("EPSG", 7856), data["srs"]
                    assert data["z"] in (1, 2)
                    columns = set(data["columns"])
                else:
                    assert "7856" in data["crs"], data["crs"]
                    columns = set(data["rows"][0])
                assert set(attrs) <= columns, (name, fmt, sorted(columns))
                # Coordinates: every exported vertex is a vertex of the object, plus E, N, A.
                coords = np.concatenate([p for g in data["geoms"] for p in g])
                assert coords.shape[1] == 3
                gap = np.min(
                    np.linalg.norm((coords - offset)[:, None, :] - local[None, :, :], axis=2),
                    axis=1,
                )
                assert gap.max() < 1e-6, (name, fmt, gap.max())
                found[(name, fmt)] = (data, coords)

    # Values. Depressions: level in RL (A + local level); volumes are the core's on the
    # ground: divided by the square of the combined scale factor (0.9996 on MGA's
    # central meridian, 100 m up) when pyproj is there, grid values without it.
    _surface, core = _core(pts, tris, min_depth=0.01)
    table = core.depressions
    for fmt in ("GPKG", "GEOJSON"):
        data, coords = found[("Depressions", fmt)]
        rows = sorted(data["rows"], key=lambda r: r["depression"])
        assert [r["depression"] for r in rows] == [1, 2]
        level = np.array([r["level"] for r in rows])
        np.testing.assert_allclose(level, table.level + A0, atol=1e-4)
        ratio = np.array([r["volume"] for r in rows]) / table.volume
        lo, hi = (1.0, 1.002) if h.submodule("analysis.site").available() else (0.99999, 1.00001)
        assert np.ptp(ratio) < 1e-6 and lo < ratio[0] < hi, ratio
        # Contours: each line's vertices are at its elevation (RL) and on a 0.25 m level.
        data, _coords = found[("Contours", fmt)]
        for row, geom in zip(data["rows"], data["geoms"], strict=True):
            for part in geom:
                np.testing.assert_allclose(part[:, 2], row["elevation"], atol=1e-4)
            assert (row["elevation"] / 0.25) == round(row["elevation"] / 0.25)
        # Sections: the pieces run from chainage_from to chainage_to along the line.
        data, _coords = found[("Sections", fmt)]
        for row in data["rows"]:
            assert 0.0 <= row["chainage_from"] < row["chainage_to"] <= 4.7
    # The per-point chainage stays in Blender: simple-feature lines carry no per-vertex
    # values (see the doc's recommendation to write it as M).
    assert "chainage" in bpy.data.objects["Sections"].data.attributes
    data, _coords = found[("Sections", "GPKG")]
    assert "chainage" not in data["columns"]
    _report("export_round_trip", {f"{n} {f}": len(d[0]["rows"]) for (n, f), d in found.items()})
