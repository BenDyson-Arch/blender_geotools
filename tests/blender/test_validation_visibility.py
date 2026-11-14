"""Validation: visibility, distance, volume and path presets against independent references.

The inputs are the synthetic cases of ``scripts/validation/visibility/cases.py``
(a ridge, a dome, Gaussian hills, 20 km of level ground), built here as
triangulated grids. The references in ``tests/data/validation/visibility`` were
made by the scripts beside ``cases.py``: an exact binary64 line of sight over
the same TIN, GDAL ``gdal_viewshed`` and GRASS ``r.viewshed``, trimesh,
shapely and networkx (see docs/validation/visibility.md). Each test checks
GeoTools against an analytic answer and against those references, and prints
the comparison statistics the validation doc quotes.
"""

from __future__ import annotations

import importlib.util
import json
import math
import re
import sys
from pathlib import Path

import bpy
import numpy as np
from test_analysis_presets import _add, _ap, _clear_trees, _reset, _values
from test_analysis_presets_2 import _attr, _co, _edges, _empty, _evaluated, _new_object, _select

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "tests" / "data" / "validation" / "visibility"


def _cases():
    name = "geotools_validation_cases"
    if name not in sys.modules:
        path = REPO_ROOT / "scripts" / "validation" / "visibility" / "cases.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _fixture(name: str):
    return np.load(DATA / f"{name}.npz")


def _report(case: str, **stats) -> None:
    print(f"VALIDATION {case} {json.dumps(stats, default=float)}")


def _mesh_object(name: str, co: np.ndarray, tris: np.ndarray, *, source: bool = True):
    """A mesh of triangles from binary64 arrays (stored as float32, as Blender does)."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", np.asarray(co, np.float32).ravel())
    tris = np.asarray(tris, np.int32)
    mesh.loops.add(tris.size)
    mesh.loops.foreach_set("vertex_index", tris.ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, tris.size, 3, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    collection = bpy.context.scene.geotools.source_collection if source else None
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def _grid_object(g, name: str = "", *, source: bool = True):
    return _mesh_object(name or g.name, g.nodes(), g.triangles(), source=source)


def _viewshed(g, observer_xy, eye_height: float, **inputs) -> np.ndarray:
    """GeoTools' viewshed of the grid's faces from an Empty on the TIN at ``observer_xy``."""
    obj = _grid_object(g)
    x, y = observer_xy
    observer = _empty("Observer", (x, y, float(g.height(x, y))))
    _select(obj, observer)
    assert _add("viewshed") == {"FINISHED"}
    modifier = obj.modifiers[0]
    _ap().set_input(modifier, "Observer Height", eye_height)
    for key, value in inputs.items():
        _ap().set_input(modifier, key, value)
    values, _where = _values(obj, "viewshed")
    return values


def _against_exact(values: np.ndarray, exact: np.ndarray) -> dict:
    """GeoTools against the exact TIN viewshed (code 1 seen, 0 hidden, -1 grazing)."""
    sure = exact >= 0
    differ = sure & (values != exact)
    return {"faces": len(values), "grazing": int((~sure).sum()), "differ": int(differ.sum())}


def _nearest_nodes(g, centres: np.ndarray) -> np.ndarray:
    i = np.clip(np.rint((centres[:, 0] - g.x0) / g.h), 0, g.nx).astype(int)
    j = np.clip(np.rint((centres[:, 1] - g.y0) / g.h), 0, g.ny).astype(int)
    return j * (g.nx + 1) + i


def _against_raster(g, values: np.ndarray, raster: np.ndarray) -> float:
    """Agreement (%) of each face with the raster value at the node nearest its centroid."""
    centres, _, _ = g.faces()
    at = raster[_nearest_nodes(g, centres)].astype(float)
    keep = (at != 2) & np.isfinite(values)
    return round(100.0 * float((values[keep] == at[keep]).mean()), 3)


# --- visibility ---------------------------------------------------------------------------


def test_viewshed_ridge_against_similar_triangles_gdal_and_grass() -> None:
    _reset()
    C, ref = _cases(), _fixture("visibility")
    g = C.ridge()
    centres, normals, _ = g.faces()
    behind = (centres[:, 0] > C.RIDGE["D"] + C.RIDGE["W"]) & (normals[:, 2] > 0.999)
    for target, key in ((0.0, "ridge"), (0.5, "ridge_target")):
        _reset()
        values = _viewshed(g, C.RIDGE["observer"], C.EYE, **{"Target Height": target})
        stats = _against_exact(values, ref[f"{key}__exact_faces"])
        assert stats["differ"] == 0, stats
        # Similar triangles: the shadow on level ground ends where the line from the eye over
        # the crest comes down to the sample point, which is Ray Offset (1 cm) up.
        end = C.ridge_shadow_end(C.EYE, target + C.OFFSET)
        near = np.abs(centres[:, 0] - end) < 1e-3
        np.testing.assert_array_equal(values[behind & ~near], centres[behind & ~near, 0] > end)
        hidden = centres[behind & (values == 0), 0]
        stats.update(
            analytic_end=end,
            last_hidden_centroid=float(hidden.max()),
            first_seen_centroid=float(centres[behind & (values == 1), 0].min()),
            vs_gdal_pct=_against_raster(g, values, ref[f"{key}__gdal"]),
            vs_grass_pct=_against_raster(g, values, ref[f"{key}__grass"]),
        )
        _report(f"viewshed_{key}", **stats)
        assert stats["vs_gdal_pct"] > 97.0 and stats["vs_grass_pct"] > 95.0, stats
    _clear_trees()


def test_viewshed_dome_against_the_tangent_cone() -> None:
    _reset()
    C, ref = _cases(), _fixture("visibility")
    g = C.dome()
    values = _viewshed(g, C.DOME["observer"], C.DOME["eye"])
    stats = _against_exact(values, ref["dome__exact_faces"])
    assert stats["differ"] == 0, stats
    # The true hemisphere: away from its rim (where the TIN cuts the vertical tangent) and
    # 0.1 m from the analytic shadow edge or horizon. The TIN's face centres lie within
    # 1.2 cm of the sphere, but on the steep flank a facet's normal differs from the
    # sphere's by several degrees, which moves the facing test's horizon by up to 0.1 m.
    points, _, centres = C.face_view_points(g)
    analytic, margin = C.dome_visible(points)
    r = np.hypot(centres[:, 0] - C.DOME["C"][0], centres[:, 1] - C.DOME["C"][1])
    clear = (np.abs(r - C.DOME["R"]) > 2 * g.h) & (np.abs(margin) > 0.1)
    differ = clear & (values != analytic)
    stats.update(
        analytic_compared=int(clear.sum()),
        analytic_differ=int(differ.sum()),
        vs_gdal_pct=_against_raster(g, values, ref["dome__gdal"]),
        vs_grass_pct=_against_raster(g, values, ref["dome__grass"]),
    )
    _report("viewshed_dome", **stats)
    assert stats["analytic_differ"] == 0, stats
    assert stats["vs_gdal_pct"] > 98.0 and stats["vs_grass_pct"] > 98.0, stats
    _clear_trees()


def test_viewshed_hills_against_gdal_and_grass() -> None:
    _reset()
    C, ref = _cases(), _fixture("visibility")
    g = C.hills()
    values = _viewshed(g, C.HILLS_OBSERVER, C.EYE)
    stats = _against_exact(values, ref["hills__exact_faces"])
    stats.update(
        vs_gdal_pct=_against_raster(g, values, ref["hills__gdal"]),
        vs_grass_pct=_against_raster(g, values, ref["hills__grass"]),
        seen_pct=round(100.0 * float(values.mean()), 2),
    )
    _report("viewshed_hills", **stats)
    assert stats["differ"] == 0, stats
    assert stats["vs_gdal_pct"] > 97.0 and stats["vs_grass_pct"] > 97.0, stats
    _clear_trees()


def test_viewshed_long_range_has_no_earth_curvature() -> None:
    _reset()
    C, ref = _cases(), _fixture("visibility")
    g = C.long_range()
    values = _viewshed(
        g, C.LONG["observer"], C.LONG["eye"], **{"Max Distance": C.LONG["max_distance"]}
    )
    stats = _against_exact(values, ref["long__exact_faces"])
    # Level ground 10 km in every direction: every face is seen (no curvature), as GDAL
    # and GRASS without curvature; with it, both hide everything past the ~12.2 km horizon.
    centres, _, _ = g.faces()
    r = np.hypot(*centres[:, :2].T)
    gdal_curved = ref["long__gdal_curved"][_nearest_nodes(g, centres)]
    stats.update(
        seen=int((values == 1).sum()),
        farthest_m=float(r.max()),
        gdal_curved_hidden_faces=int((gdal_curved == 0).sum()),
    )
    _report("viewshed_long", **stats)
    assert stats["differ"] == 0 and stats["seen"] == len(values), stats
    assert stats["gdal_curved_hidden_faces"] > 0
    _clear_trees()


def test_viewshed_hills_far_from_the_origin() -> None:
    # The hills 10 km east and 20 km north of the origin (a site in a projected CRS without
    # an origin shift), where float32 keeps 2 mm: the faces' normals and centres are coarse.
    # The reference is the exact viewshed of the same float32 vertices, with sight lines
    # within 1 cm of the TIN left out (either answer is right there).
    _reset()
    C = _cases()
    near = C.hills()
    x0, y0, z0 = C.FAR
    nodes = np.asarray(near.nodes() + (x0, y0, z0), np.float32).astype(np.float64)
    g = C.Grid(near.x0 + x0, near.y0 + y0, near.h, nodes[:, 2].reshape(near.z.shape), "far")
    g.x0, g.y0 = float(nodes[0, 0]), float(nodes[0, 1])
    observer = (C.HILLS_OBSERVER[0] + x0, C.HILLS_OBSERVER[1] + y0)
    values = _viewshed(g, observer, C.EYE)
    seen, unsure = C.viewshed_faces(g, C.node_eye(g, observer, C.EYE), grazing=1e-2)
    differ = ~unsure & (values != seen)
    stats = {"faces": len(values), "grazing_1cm": int(unsure.sum()), "differ": int(differ.sum())}
    _report("viewshed_far", **stats)
    assert stats["differ"] <= 0.001 * len(values), stats
    _clear_trees()


def _observers_mesh(g, points, name: str):
    co = [(x, y, float(g.height(x, y))) for x, y in points]
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(co, [], [])
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def test_cumulative_viewshed_hills_live_and_fast() -> None:
    _reset()
    C, ref = _cases(), _fixture("visibility")
    g = C.hills()
    obj = _grid_object(g)
    observers = _observers_mesh(g, C.HILLS_OBSERVERS, "Observers")
    _select(obj, observers)
    assert _add("viewshed_cumulative") == {"FINISHED"}
    live, _ = _values(obj, "viewshed_cumulative")
    exact = ref["cumulative__exact_faces"]
    sure = exact >= 0
    _select(obj, observers)
    assert _add("viewshed_cumulative", engine="BAKE", name="cumulative_fast") == {"FINISHED"}
    fast, _ = _values(obj, "cumulative_fast")
    stats = {
        "faces": len(live),
        "grazing": int((~sure).sum()),
        "live_differ": int((sure & (live != exact)).sum()),
        "fast_differ": int((sure & (fast != exact)).sum()),
        "fast_vs_live_differ": int((fast != live).sum()),
        "vs_gdal_sum_pct": _against_raster(g, live, ref["cumulative__gdal"]),
        "vs_grass_sum_pct": _against_raster(g, live, ref["cumulative__grass"]),
    }
    _report("viewshed_cumulative", **stats)
    assert stats["live_differ"] == 0 and stats["fast_differ"] == 0, stats
    assert stats["vs_gdal_sum_pct"] > 95.0 and stats["vs_grass_sum_pct"] > 95.0, stats
    _clear_trees()


def test_intervisibility_hills_against_exact_gdal_and_grass() -> None:
    _reset()
    C, ref = _cases(), _fixture("visibility")
    g = C.hills()
    surface = _grid_object(g)
    points = _observers_mesh(g, C.HILLS_POINTS, "Points")
    _select(surface, points)
    assert _add("intervisibility") == {"FINISHED"}
    lines = _new_object("intervisibility")
    mesh = _evaluated(lines)
    ends = _co(mesh)[_edges(mesh)]
    visible = _attr(mesh, "visible")
    # Match each line to its pair (i < j) by its end points.
    where = np.array([(x, y) for x, y in C.HILLS_POINTS])
    index = [np.argmin(np.hypot(*(where - e[:2]).T)) for e in ends.reshape(-1, 3)]
    index = np.array(index).reshape(-1, 2)
    pairs = [(i, j) for i in range(len(where)) for j in range(i + 1, len(where))]
    order = [pairs.index(tuple(sorted(p))) for p in index.tolist()]
    exact = ref["intervisibility__exact"][order]
    sure = exact >= 0
    stats = {
        "pairs": len(visible),
        "visible": int(visible.sum()),
        "differ": int((sure & (visible != exact)).sum()),
        "gdal_differ": int((visible != ref["intervisibility__gdal"][order]).sum()),
        "grass_differ": int((visible != ref["intervisibility__grass"][order]).sum()),
    }
    _report("intervisibility", **stats)
    assert stats["pairs"] == 15 and stats["differ"] == 0, stats
    # The eyes are on the TIN (draped) plus Height.
    eyes = np.array([C.node_eye(g, p, C.EYE) for p in C.HILLS_POINTS])
    got = ends.reshape(-1, 3)
    np.testing.assert_allclose(got, eyes[index.ravel()], atol=2e-3)
    _clear_trees()


def test_line_of_sight_over_the_ridge() -> None:
    _reset()
    C = _cases()
    g = C.ridge()
    surface = _grid_object(g)
    target_x = 20.0
    line = bpy.data.meshes.new("Line")
    line.from_pydata([(0.0, 0.0, 0.0), (target_x, 0.0, 0.0)], [(0, 1)], [])
    line_obj = bpy.data.objects.new("Line", line)
    bpy.context.scene.collection.objects.link(line_obj)
    _select(surface, line_obj)
    assert _add("line_of_sight") == {"FINISHED"}
    obj = _new_object("line_of_sight")
    mesh = _evaluated(obj)
    co, ends = _co(mesh), _edges(mesh)
    # The sight line z = 1.6 (1 - x / 20) meets the ridge's front slope z = (x - 8) / 2 at
    # x = 5.6 / 0.58 = 9.655 (H = 1, W = 2, D = 10).
    D, H, W = C.RIDGE["D"], C.RIDGE["H"], C.RIDGE["W"]
    slope = H / W
    x_hit = (C.EYE + slope * (D - W)) / (slope + C.EYE / target_x)
    (obstruction,) = co[_attr(mesh, "obstruction") > 0]
    np.testing.assert_allclose(obstruction, (x_hit, 0.0, slope * (x_hit - D + W)), atol=1e-4)
    # The profile: seen up to the crest and from the shadow end on, hidden between.
    part, visible = _attr(mesh, "part"), _attr(mesh, "visible")
    middle = co[ends[part == 0]].mean(axis=1)[:, 0]
    seen = visible[part == 0]
    end = C.ridge_shadow_end(C.EYE, C.OFFSET)
    assert end > target_x  # the target is in the shadow
    assert (seen[middle < D - 0.3] == 1).all() and (seen[(middle > D + 0.3)] == 0).all()
    _report(
        "line_of_sight",
        obstruction=obstruction.tolist(),
        analytic=[x_hit, 0.0, slope * (x_hit - D + W)],
    )
    _clear_trees()


# --- change and distance ------------------------------------------------------------------


def _vertex_values(obj, layer: str) -> np.ndarray:
    """A CORNER layer's values per vertex (every corner of a vertex holds its value)."""
    values, _ = _values(obj, layer)
    mesh = obj.data
    loops = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", loops)
    out = np.full(len(mesh.vertices), np.nan)
    out[loops] = values
    return out


def _add_with(preset: str, obj, other, **inputs):
    _select(obj, other)
    assert _add(preset) == {"FINISHED"}
    modifier = obj.modifiers[-1]
    for key, value in inputs.items():
        _ap().set_input(modifier, key, value)
    return modifier


def test_distance_to_surface_is_perpendicular_not_vertical() -> None:
    _reset()
    C = _cases()
    measured = _grid_object(C.tilted(0.0), "Later")
    reference = _grid_object(C.tilted(C.TILT_RISE, half=15.0), "Earlier", source=False)
    _add_with("surface_distance", measured, reference)
    distance = _vertex_values(measured, "surface_distance")
    # The reference is 0.25 m above along z, so 0.25 cos 30° away along its normal, and
    # the measured surface is behind it: negative.
    expected = -C.TILT_RISE * math.cos(math.radians(30.0))
    _report(
        "distance_tilted", expected=expected, max_error=float(np.abs(distance - expected).max())
    )
    np.testing.assert_allclose(distance, expected, atol=2e-5)
    # Cut and fill on the same pair measures vertically: -0.25 on every face.
    _add_with("cut_fill", measured, reference)
    depth, _ = _values(measured, "cut_fill")
    np.testing.assert_allclose(depth, -C.TILT_RISE, atol=2e-5)
    _clear_trees()


def test_distance_to_surface_of_a_sphere_against_trimesh() -> None:
    _reset()
    C, ref = _cases(), _fixture("distance_volume")
    above = C.grid(lambda x, y: 4.0 + 0.0 * x, -6.0, -6.0, 6.0, 6.0, 0.5, "above")
    measured = _grid_object(above, "Above")
    sphere = _mesh_object("Sphere", *C.uv_sphere(3.0, 24, 48), source=False)
    _add_with("surface_distance", measured, sphere, **{"Max Distance": 10.0})
    distance = _vertex_values(measured, "surface_distance")
    expected = ref["sphere__distance"] * ref["sphere__sign"]
    error = np.abs(distance - expected)
    co = np.asarray(above.nodes(), np.float32).astype(np.float64)
    analytic = np.abs(distance - (np.linalg.norm(co, axis=1) - 3.0))
    _report(
        "distance_sphere", vs_trimesh_max=float(error.max()), vs_sphere_max=float(analytic.max())
    )
    assert error.max() < 2e-5, error.max()  # float32 at a few metres
    assert analytic.max() < 0.012  # the faceted sphere is up to 1.1 cm inside the true one
    _clear_trees()


def _points_object(name: str, points: np.ndarray, size: float = 1e-3):
    """One tiny triangle per point (CORNER presets need faces); the point is its first vertex."""
    co = np.repeat(points, 3, axis=0)
    co[1::3, 0] += size
    co[2::3, 1] += size
    tris = np.arange(len(co)).reshape(-1, 3)
    return _mesh_object(name, co, tris)


def test_distance_to_surface_sign_at_sharp_edges() -> None:
    C, ref = _cases(), _fixture("distance_volume")
    stats = {}
    for key, inward in (("gable", False), ("notch", True)):
        _reset()
        g = C.gable()
        if inward:
            g = C.Grid(g.x0, g.y0, g.h, 3.0 - g.z, "notch")
        reference = _grid_object(g, "Reference", source=False)
        points = ref[f"{key}__points"]
        measured = _points_object("Points", points)
        _add_with("surface_distance", measured, reference)
        distance = _vertex_values(measured, "surface_distance")[0::3]
        truth = -1.0 if inward else 1.0
        stats[key] = {
            "points": len(points),
            "wrong_sign": int((np.sign(distance) != truth).sum()),
            "trimesh_nearest_face_wrong": int((ref[f"{key}__face_sign"] != truth).sum()),
            "pseudonormal_wrong": int((ref[f"{key}__pseudo_sign"] != truth).sum()),
            "max_magnitude_error": float(np.abs(np.abs(distance) - 0.3).max()),
        }
        assert stats[key]["max_magnitude_error"] < 1e-5, stats
    _report("distance_sharp_edges", **stats)
    # Every point is 0.3 m from the crest, on the side its true sign says. The sign is
    # taken from the normal of one face at the crest: within the wedge of the two faces'
    # normals (here 120° apart) that face's plane may put the point on the wrong side.
    # Documented, not fixed: see docs/validation/visibility.md.
    assert stats["gable"]["pseudonormal_wrong"] == 0 and stats["notch"]["pseudonormal_wrong"] == 0
    _clear_trees()


def test_distance_to_feature_against_shapely() -> None:
    _reset()
    C, ref = _cases(), _fixture("distance_volume")
    g = C.grid(lambda x, y: C.FEATURE_Z + 0.0 * x, -8.0, -8.0, 8.0, 8.0, 0.5, "plan")
    measured = _grid_object(g, "Site")
    co = [(x, y, 0.0) for x, y in C.FEATURE_POLYGON]
    n = len(co)
    co += [(x, y, 0.0) for x, y in C.FEATURE_LINE]
    edges = [(n + k, n + k + 1) for k in range(len(C.FEATURE_LINE) - 1)]
    m = len(co)
    co += [(x, y, 0.0) for x, y in C.FEATURE_POINTS]
    mesh = bpy.data.meshes.new("Features")
    mesh.from_pydata(co, edges, [list(range(n))])
    feature = bpy.data.objects.new("Features", mesh)
    bpy.context.scene.collection.objects.link(feature)
    assert len(mesh.vertices) == m + len(C.FEATURE_POINTS)
    modifier = _add_with("feature_distance", measured, feature)
    plan = _vertex_values(measured, "feature_distance")
    expected = ref["feature__plan"]
    _ap().set_input(modifier, "Horizontal", False)
    solid = _vertex_values(measured, "feature_distance")
    stats = {
        "nodes": len(plan),
        "plan_max_error": float(np.abs(plan - expected).max()),
        "inside_max": float(np.abs(plan[expected == 0]).max()),
        "3d_max_error": float(np.abs(solid - np.hypot(expected, C.FEATURE_Z)).max()),
    }
    _report("feature_distance", **stats)
    assert stats["plan_max_error"] < 1e-5 and stats["inside_max"] < 1e-6, stats
    assert stats["3d_max_error"] < 1e-5, stats
    _clear_trees()


# --- volumes -----------------------------------------------------------------------------------


def _numbers(line: str) -> list[float]:
    return [float(s.replace(",", "")) for s in re.findall(r"-?[\d,]+\.\d+", line)]


def _tetra_volume(co: np.ndarray, tris: np.ndarray) -> float:
    """The signed-tetrahedron mesh volume (binary64, about the vertices' mean)."""
    p = co[tris] - co.mean(axis=0)
    return float(np.einsum("ij,ij->i", p[:, 0], np.cross(p[:, 1], p[:, 2])).sum() / 6.0)


def test_closed_volume_of_solids_near_and_far() -> None:
    _reset()
    C, ref = _cases(), json.loads((DATA / "distance_volume.json").read_text())["closed_volume"]
    names = ("prism", "pyramid", "frustum", "hemisphere")
    stats = {}
    for where in ("near", "far"):
        _reset()
        parts = [C.solid(n, C.FAR if where == "far" else (0.0, 0.0, 0.0)) for n in names]
        co, tris, start, ids = [], [], 0, []
        for k, (c, t) in enumerate(parts):
            co.append(c)
            tris.append(t + start)
            ids += [k] * len(t)
            start += len(c)
        obj = _mesh_object("Solids", np.vstack(co), np.vstack(tris))
        _select(obj)
        assert _add("closed_volume") == {"FINISHED"}
        values, _ = _values(obj, "closed_volume")
        ids = np.array(ids)
        f32 = np.asarray(np.vstack(co), np.float32).astype(np.float64)
        for k, name in enumerate(names):
            part = values[ids == k]
            assert np.ptp(part) == 0.0  # one value per part
            expected = ref[name]["trimesh" if where == "near" else "trimesh_far"]
            mine = _tetra_volume(f32, np.vstack(tris)[ids == k])
            stats[f"{where}_{name}"] = {
                "geotools": float(part[0]),
                "trimesh": expected,
                "rel_error": abs(float(part[0]) - expected) / expected,
                "tetra_rel": abs(mine - expected) / expected,
            }
        (line,) = _ap().totals(bpy.context, obj)
        total64 = _numbers(line)[0]
        stats[f"{where}_total_binary64"] = total64
    _report("closed_volume", **stats)
    # float32: each part's faces are summed in order (Accumulate Field), 1e-5 on 4,096 faces.
    for key, s in stats.items():
        if isinstance(s, dict):
            assert s["rel_error"] < 1e-5 and s["tetra_rel"] < 1e-9, (key, s)
    # The faceted solids' exact volumes, at the origin.
    for name in ("prism", "pyramid", "frustum"):
        assert (
            abs(stats[f"near_{name}"]["geotools"] - ref[name]["faceted"])
            < 2e-6 * ref[name]["faceted"]
        )
    _clear_trees()


def test_closed_volume_totals_float32_and_binary64() -> None:
    _reset()
    C = _cases()
    co, tris = C.hemisphere(2.0, 256, 512, C.FAR)
    obj = _mesh_object("Fine", co, tris)
    _select(obj)
    assert _add("closed_volume") == {"FINISHED"}
    values, _ = _values(obj, "closed_volume")
    (line,) = _ap().totals(bpy.context, obj)
    (shown,) = [w.message for m in obj.modifiers for w in m.node_warnings]
    exact = _tetra_volume(np.asarray(co, np.float32).astype(np.float64), tris)
    # Read Totals' own binary64 sum, unrounded.
    centres, area_vectors = _ap()._face_vectors(_evaluated(obj))
    binary64 = float(np.einsum("ij,ij->i", centres, area_vectors).sum() / 3.0)
    stats = {
        "faces": len(tris),
        "binary64_tetra": exact,
        "read_totals": _numbers(line)[0],
        "read_totals_unrounded": binary64,
        "per_face_value_float32": float(values[0]),
        "modifier_text": _numbers(shown)[0],
    }
    stats["read_totals_rel"] = abs(binary64 - exact) / exact
    stats["value_rel"] = abs(stats["per_face_value_float32"] - exact) / exact
    stats["modifier_rel"] = abs(stats["modifier_text"] - exact) / exact
    _report("closed_volume_totals", **stats)
    # 262,144 faces 10 km from the origin. Read Totals is exact; the float32 sums are
    # within 2e-4 (before version 2 of the preset, 0.27 % out: see the CHANGELOG).
    assert stats["read_totals_rel"] < 1e-12, stats
    assert abs(stats["read_totals"] - exact) <= 5e-4, stats  # shown to 3 decimals
    assert stats["value_rel"] < 2e-4 and stats["modifier_rel"] < 2e-4, stats
    _clear_trees()


def _cut_fill(design_grid, reference_fn, extent) -> tuple[float, float, float]:
    C = _cases()
    _reset()
    design = _grid_object(design_grid, "Design")
    x0, y0, x1, y1 = extent
    ref_grid = C.grid(reference_fn, x0 - 1, y0 - 1, x1 + 1, y1 + 1, 0.25, "Ground")
    reference = _grid_object(ref_grid, "Ground", source=False)
    _add_with("cut_fill", design, reference)
    (line,) = _ap().totals(bpy.context, design)
    fill, cut, net = _numbers(line)[:3]
    return fill, cut, net


def test_cut_and_fill_against_analytic_and_raster_volumes() -> None:
    C, ref = _cases(), json.loads((DATA / "distance_volume.json").read_text())["cut_fill"]
    stats = {}

    def flat(x, y):
        return 0.0 * x

    fill, cut, net = _cut_fill(C.mound_pit_grid(), flat, (-12, -6, 12, 6))
    stats["mound_pit"] = {
        "fill": fill,
        "cut": cut,
        "analytic_fill": ref["analytic"]["mound"],
        "analytic_cut": ref["analytic"]["pit"],
        "raster_fill": ref["mound_pit_raster_0.25"]["fill"],
        "raster_cut": ref["mound_pit_raster_0.25"]["cut"],
    }
    fill, cut, net = _cut_fill(C.wave_grid(), flat, (-8, -8, 8, 8))
    stats["wave_flat"] = {
        "fill": fill,
        "cut": cut,
        "net": net,
        "analytic": ref["analytic"]["wave_flat"],
        "raster_fill": ref["wave_flat_raster_0.25"]["fill"],
    }
    fill, cut, net = _cut_fill(C.wave_grid(), C.wave_plane, (-8, -8, 8, 8))
    stats["wave_plane"] = {
        "fill": fill,
        "cut": cut,
        "net": net,
        "analytic_net": ref["analytic"]["wave_plane_net"],
        "fine_fill": ref["wave_plane_raster_0.01"]["fill"],
        "fine_cut": ref["wave_plane_raster_0.01"]["cut"],
        "raster_fill": ref["wave_plane_raster_0.25"]["fill"],
        "raster_cut": ref["wave_plane_raster_0.25"]["cut"],
    }
    _report("cut_fill", **stats)
    s = stats["mound_pit"]
    # The cone's TIN is exact but for its apex fan and rim (h²): within 0.2 %; the pit's
    # walls are planar but the grid's diagonals cut its north-west and south-east hips.
    assert abs(s["fill"] - s["analytic_fill"]) < 2e-3 * s["analytic_fill"], s
    assert abs(s["cut"] - s["analytic_cut"]) < 2e-3 * s["analytic_cut"], s
    s = stats["wave_flat"]
    assert abs(s["fill"] - s["analytic"]) < 2e-3 * s["analytic"] and abs(s["net"]) < 1e-3, s
    assert abs(s["cut"] - s["analytic"]) < 2e-3 * s["analytic"], s
    s = stats["wave_plane"]
    assert abs(s["net"] - s["analytic_net"]) < 2e-3 * abs(s["analytic_net"]), s
    assert abs(s["fill"] - s["fine_fill"]) < 1e-2 * s["fine_fill"], s
    assert abs(s["cut"] - s["fine_cut"]) < 1e-2 * s["fine_cut"], s
    _clear_trees()


# --- least-cost paths --------------------------------------------------------------------------


def _route(g, start, end, **inputs):
    _reset()
    surface = _grid_object(g)
    a = _empty("A Start", (*start, float(g.height(*start))))
    b = _empty("B End", (*end, float(g.height(*end))))
    _select(surface, a, b)
    assert _add("least_cost_path") == {"FINISHED"}
    obj = _new_object("least_cost_path")
    for key, value in inputs.items():
        _ap().set_input(obj.modifiers[0], key, value)
    mesh = _evaluated(obj)
    elapsed = _attr(mesh, "elapsed")
    order = np.argsort(elapsed)
    co = _co(mesh)[order]
    i = np.rint((co[:, 0] - g.x0) / g.h).astype(int)
    j = np.rint((co[:, 1] - g.y0) / g.h).astype(int)
    return j * (g.nx + 1) + i, elapsed[order]


def test_least_cost_path_against_networkx_and_r_walk() -> None:
    C, ref = _cases(), _fixture("paths")
    summary = json.loads((DATA / "paths.json").read_text())
    g = C.hills()
    route, elapsed = _route(g, C.LCP["start"], C.LCP["end"])
    co = np.asarray(g.nodes(), np.float32).astype(np.float64)
    expected = ref["hills__route"]
    d = np.diff(co[route], axis=0)
    run, rise = np.hypot(d[:, 0], d[:, 1]), d[:, 2]
    symmetric = float((0.5 * (C.tobler_time(run, rise) + C.tobler_time(run, -rise))).sum())
    s = summary["hills"]
    stats = {
        "vertices": len(route),
        "same_route": bool(len(route) == len(expected) and (route == expected).all()),
        "symmetric_cost": symmetric,
        "networkx_symmetric_cost": s["symmetric_cost"],
        "elapsed": float(elapsed[-1]),
        "networkx_walked": s["route_walked_time"],
        "directed_best": s["directed_best_time"],
        "excess_over_directed_pct": 100 * (float(elapsed[-1]) / s["directed_best_time"] - 1),
        "r_walk": s["r_walk_time"],
        "r_walk_knight": s["r_walk_knight_time"],
    }
    _report("least_cost_path_hills", **stats)
    assert abs(symmetric - s["symmetric_cost"]) < 1e-5 * symmetric, stats
    assert abs(stats["elapsed"] - s["route_walked_time"]) < 1e-4 * stats["elapsed"], stats
    assert stats["elapsed"] >= s["directed_best_time"] * (1 - 1e-5), stats

    # Level ground: the edge graph's bias with bearing (a triangulated grid has edges in
    # six directions; r.walk has eight, or sixteen with the knight's move).
    flat = C.grid(lambda x, y: 0.0 * x, -12.0, -12.0, 12.0, 12.0, 1.0, "flat")
    v0 = float(C.tobler_time(1.0, 0.0))
    ratios = []
    for bearing in summary["bearings"]:
        _, t = _route(flat, (0.0, 0.0), tuple(bearing["end"]))
        ratios.append(float(t[-1]) / (bearing["straight_m"] * v0))
        assert abs(ratios[-1] - bearing["graph_ratio"]) < 1e-5, (bearing, ratios[-1])
    _report("least_cost_path_bearings", ratios=ratios)
    assert abs(max(ratios) - math.sqrt(2.0)) < 1e-5
    _clear_trees()
