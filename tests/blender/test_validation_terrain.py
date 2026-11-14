"""Terrain validation: the terrain-shape and archaeological-visualisation presets.

Each preset runs (as a Geometry Nodes modifier, or its fast bake path) on
synthetic meshes and is compared with closed-form answers and with GDAL 3.12,
WhiteboxTools 2.4 and rvt-py 2.2.1 run on the same DEM
(``scripts/validation/terrain/make_references.py`` wrote the references in
``tests/data/validation/terrain/references.npz``). The surfaces are in
``_terrain_surfaces.py``; the method, tolerances and results are in
docs/validation/terrain.md.

Set ``GEOTOOLS_TERRAIN_REPORT=/path/report.json`` to write every statistic
measured here (the doc's tables come from that report).
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

import _harness as h
import _terrain_surfaces as ts
import bpy
import numpy as np
from test_analysis_presets import _clear_trees, _reset

REFERENCES = (
    Path(__file__).resolve().parents[1] / "data" / "validation" / "terrain" / "references.npz"
)
REPORT: dict[str, object] = {}
TIMES: dict[str, list[float]] = {}
FULL = bool(os.environ.get("GEOTOOLS_TERRAIN_REPORT"))
"""Also run the report-only measurements (slower; the doc's tables need them)."""
RESOLUTION = 90.0 / 2 ** (8 - 1)  # horizon bisection bracket at Steps 8, degrees


def _ap():
    return h.submodule("blender.analysis_presets")


def _catalog():
    return h.submodule("analysis.presets")


def _report(key: str, value: object) -> None:
    REPORT[key] = value
    REPORT["seconds"] = {k: [round(t, 3) for t in v] for k, v in TIMES.items()}
    path = os.environ.get("GEOTOOLS_TERRAIN_REPORT")
    if path:
        report = json.loads(Path(path).read_text()) if Path(path).is_file() else {}
        report.update(REPORT)
        Path(path).write_text(json.dumps(report, indent=1, sort_keys=True))


def _stats(error, circular: bool = False) -> dict[str, float]:
    e = np.asarray(error, np.float64).ravel()
    if circular:
        e = ts.angle_difference(e, 0.0)
    e = e[np.isfinite(e)]
    if not len(e):
        return {"n": 0}
    return {
        "n": len(e),
        "max": float(np.abs(e).max()),
        "median": float(np.median(np.abs(e))),
        "rms": float(np.sqrt(np.mean(e * e))),
        "bias": float(e.mean()),
    }


# --- meshes and presets --------------------------------------------------------------------------


def _mesh(name: str, verts: np.ndarray, tris: np.ndarray) -> bpy.types.Object:
    return _mesh_faces(name, verts, tris)


def _mesh_faces(name: str, verts: np.ndarray, faces: np.ndarray) -> bpy.types.Object:
    """A mesh object of equal-sided faces (triangles or quads) in the source collection."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", np.asarray(verts, np.float32).ravel())
    mesh.loops.add(faces.size)
    mesh.loops.foreach_set("vertex_index", faces.astype(np.int32).ravel())
    mesh.polygons.add(len(faces))
    sides = faces.shape[1]
    mesh.polygons.foreach_set("loop_start", np.arange(0, faces.size, sides, dtype=np.int32))
    mesh.update()
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    h.project().source_collection.objects.link(obj)
    return obj


def _dem(name: str, z: np.ndarray, spacing: float) -> bpy.types.Object:
    return _mesh(name, ts.grid_vertices(z, spacing), ts.triangles(z.shape[0]))


def _evaluated(obj: bpy.types.Object, attribute: str, domain: str) -> np.ndarray:
    """An attribute of the evaluated mesh; CORNER values are returned per vertex."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    mesh = obj.evaluated_get(depsgraph).data
    data = mesh.attributes[attribute]
    assert data.domain == domain, (attribute, data.domain)
    values = np.empty(len(data.data), np.float32)
    data.data.foreach_get("value", values)
    if domain == "FACE":
        return values.astype(np.float64)
    loops = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", loops)
    per_vertex = np.full(len(mesh.vertices), np.nan)
    per_vertex[loops] = values
    return per_vertex


def _preset(obj: bpy.types.Object, preset_id: str, **inputs) -> np.ndarray:
    """Evaluate one preset on ``obj`` with ``inputs`` (the rest at their defaults)."""
    preset = _catalog().get(preset_id)
    group = _ap().ensure_group(preset)
    attribute = f"gt_val_{preset_id}"
    modifier = obj.modifiers.new(attribute, "NODES")
    modifier.node_group = group
    _ap().set_input(modifier, _catalog().ATTRIBUTE_INPUT, attribute)
    for name, value in inputs.items():
        _ap().set_input(modifier, name, value)
    began = time.perf_counter()
    try:
        return _evaluated(obj, attribute, preset.domain.value)
    finally:
        TIMES.setdefault(f"{obj.name}/{preset_id}", []).append(time.perf_counter() - began)
        obj.modifiers.remove(modifier)


def _fast(obj: bpy.types.Object, preset_id: str, **inputs) -> np.ndarray:
    """The preset's fast (bake) path on ``obj``: per-vertex values."""
    layer = f"val_fast_{preset_id}"
    began = time.perf_counter()
    h.submodule("blender.preset_bake").compute(
        bpy.context, preset_id, [obj], name=layer, inputs=inputs
    )
    TIMES.setdefault(f"{obj.name}/{preset_id}/fast", []).append(time.perf_counter() - began)
    return _evaluated(obj, f"gt_{layer}", "CORNER")


def _face_centres(obj: bpy.types.Object) -> np.ndarray:
    centres = np.empty(len(obj.data.polygons) * 3, np.float32)
    obj.data.polygons.foreach_get("center", centres)
    return centres.reshape(-1, 3).astype(np.float64)


def _vertices(obj: bpy.types.Object) -> np.ndarray:
    co = np.empty(len(obj.data.vertices) * 3, np.float32)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3).astype(np.float64)


def _references() -> dict[str, np.ndarray]:
    with np.load(REFERENCES) as data:
        refs = {k: data[k] for k in data.files}
    refs["meta"] = json.loads(str(refs["meta"]))
    return refs


def _waves_object(lo: int = 0) -> tuple[bpy.types.Object, np.ndarray]:
    """The waves DEM's cells [lo, N - lo)^2 as a mesh (a centred crop, so the same coordinates)."""
    z = ts.waves(*ts.grid_xy())[lo : ts.N - lo, lo : ts.N - lo]
    return _dem("Waves", z, ts.H), z


def _ids(rows: np.ndarray, lo: int = 0) -> np.ndarray:
    """Vertex ids, in the crop from ``lo``, of the full DEM's cells rows x rows (row-major)."""
    m = ts.N - 2 * lo
    r = np.asarray(rows) - lo
    return (r[:, None] * m + r[None, :]).ravel()


# --- slope, slope (percent) and aspect ----------------------------------------------------------------


SLOPES = (0.6, 5.0, 30.0, 60.0, 85.0)
ASPECTS = tuple(22.5 * k for k in range(16)) + (1.0, 359.0)


def _plane_patches() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """One mesh of separate 3 x 3 patches, one per (slope, aspect): verts, tris, truth per face."""
    verts, tris, truth = [], [], []
    patch = ts.triangles(4)
    for row, slope in enumerate((0.0, 0.4, *SLOPES)):
        for col, aspect in enumerate(ASPECTS):
            x, y = np.meshgrid(np.linspace(0, 0.3, 4), np.linspace(0.3, 0, 4))
            a = math.radians(aspect)
            # Falls towards the aspect: z = -tan(slope) (x sin a + y cos a).
            z = -math.tan(math.radians(slope)) * (x * math.sin(a) + y * math.cos(a))
            base = len(verts) * 16
            v = np.stack([x.ravel() + col * 0.5, y.ravel() + row * 0.5, z.ravel()], 1)
            verts.append(v)
            tris.append(patch + base)
            truth += [(slope, aspect)] * len(patch)
    return np.concatenate(verts), np.concatenate(tris), np.array(truth)


def test_validation_slope_aspect_planes() -> None:
    _reset()
    verts, tris, truth = _plane_patches()
    obj = _mesh("Planes", verts, tris)
    slope = _preset(obj, "slope")
    percent = _preset(obj, "slope_percent")
    aspect = _preset(obj, "aspect")
    expect_slope, expect_aspect = truth[:, 0], truth[:, 1]
    tilted = expect_slope >= 0.5  # above the default Flat Slope of 0.5 deg
    stats = {
        "slope_deg": _stats(slope - expect_slope),
        "slope_percent_rel": _stats(
            percent[expect_slope > 0] / (100 * np.tan(np.radians(expect_slope[expect_slope > 0])))
            - 1
        ),
        "aspect_deg": _stats(ts.angle_difference(aspect[tilted], expect_aspect[tilted])),
        "faces": len(slope),
    }
    _report("planes", stats)
    # float32 vertex positions: normals good to ~1e-7, so ~1e-5 deg except where
    # acos is ill-conditioned (slope 0.4-0.6 deg: 6e-8 / sin(0.5 deg) rad).
    assert stats["slope_deg"]["max"] < 2e-3, stats
    assert stats["aspect_deg"]["max"] < 2e-3, stats
    # tan amplifies a normal's float32 error by 1 / (sin cos): 11.5 at 85 deg.
    assert stats["slope_percent_rel"]["max"] < 1e-4, stats
    assert np.all((aspect[tilted] >= 0) & (aspect[tilted] < 360)), "aspect outside [0, 360)"
    np.testing.assert_array_equal(percent[expect_slope == 0], 0.0)
    assert np.isnan(aspect[expect_slope < 0.5]).all(), "level and 0.4 deg faces have no aspect"

    # Convergence: true = grid + convergence, wrapped into [0, 360).
    north = (expect_aspect == 1.0) & tilted
    for convergence, expected in ((-10.0, 351.0), (10.0, 11.0), (180.0, 181.0)):
        values = _preset(obj, "aspect", Convergence=convergence)[north]
        np.testing.assert_allclose(ts.angle_difference(values, expected), 0, atol=2e-3)
        assert np.all((values >= 0) & (values < 360))
    # Flat Slope 0: every non-level face has an aspect, a level one none.
    values = _preset(obj, "aspect", **{"Flat Slope": 0.0})
    assert np.isfinite(values[expect_slope > 0]).all() and np.isnan(values[expect_slope == 0]).all()

    # Far from the origin, float32 vertex positions round the 0.1 m facets: a
    # georeferenced scene keeps meshes near its origin (the GeoScene offset).
    far = {}
    for offset in (1e3, 1e4, 1e5):
        moved = _mesh(f"Planes {offset:g}", verts + [offset, offset, 0.0], tris)
        slope_far = _preset(moved, "slope")
        aspect_far = _preset(moved, "aspect")
        far[f"{offset:g} m"] = {
            "slope_deg": _stats(slope_far - expect_slope),
            "aspect_deg": _stats(ts.angle_difference(aspect_far[tilted], expect_aspect[tilted])),
        }
        bpy.data.objects.remove(moved)
    _report("planes_far_from_origin", far)
    # 1 km out a coordinate is held to 6e-5 m: ~6e-4 rad on a 0.1 m facet.
    assert far["1000 m"]["slope_deg"]["max"] < 0.15, far
    _clear_trees()


def test_validation_slope_aspect_walls() -> None:
    """Vertical and overhanging faces: slope 90 and above, no percent, an aspect."""
    _reset()
    # A wall facing east (+X) and an overhang tilted 30 deg past vertical, facing south.
    verts = np.array(
        [
            [0, 0, 0],
            [0, 1, 0],
            [0, 1, 1],
            [0, 0, 1],  # x = 0 wall, normal +X
            [2, 0, 0],
            [3, 0, 0],
            [3, 0, 0],
            [2, 0, 0],
        ],
        np.float64,
    )
    lean = math.radians(30.0)  # the top leans out over the foot, to -Y
    verts[6] = [3, -math.sin(lean), math.cos(lean)]
    verts[7] = [2, -math.sin(lean), math.cos(lean)]
    tris = np.array([[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7]], np.int32)
    obj = _mesh("Walls", verts, tris)
    slope = _preset(obj, "slope")
    percent = _preset(obj, "slope_percent")
    aspect = _preset(obj, "aspect")
    normals = np.empty(12, np.float32)
    obj.data.polygons.foreach_get("normal", normals)
    np.testing.assert_allclose(normals.reshape(-1, 3)[:2], [[1, 0, 0]] * 2, atol=1e-6)
    np.testing.assert_allclose(slope, [90, 90, 120, 120], atol=1e-4)
    assert np.isnan(percent).all()
    np.testing.assert_allclose(aspect, [90, 90, 180, 180], atol=1e-4)
    _report("walls", {"slope": slope.tolist(), "aspect": aspect.tolist()})
    _clear_trees()


def test_validation_slope_aspect_cone() -> None:
    """A cone of 30 deg: constant slope, radial aspect; error of the facets near the apex."""
    _reset()
    n, spacing = 61, 0.1
    x, y = ts.grid_xy(n, spacing)
    obj = _dem("Cone", -np.hypot(x, y) * math.tan(math.radians(30.0)), spacing)
    slope = _preset(obj, "slope")
    aspect = _preset(obj, "aspect")
    c = _face_centres(obj)
    rho = np.hypot(c[:, 0], c[:, 1])
    expected_aspect = np.degrees(np.arctan2(c[:, 0], c[:, 1])) % 360
    stats = {}
    for label, band in (
        ("rho<0.3", rho < 0.3),
        ("0.3-1", (rho >= 0.3) & (rho < 1)),
        (">1", rho >= 1),
    ):
        stats[label] = {
            "slope_deg": _stats(slope[band] - 30.0),
            "aspect_deg": _stats(ts.angle_difference(aspect[band], expected_aspect[band])),
        }
    _report("cone", stats)
    # A facet of a sampled cone is the plane through three of its points, whose
    # gradient turns by up to h / rho across it: its slope and aspect differ
    # from the cone's at the facet centre by O(h / rho), alternating in sign
    # (unbiased over a ring).
    assert stats[">1"]["slope_deg"]["median"] < 0.2, stats
    assert abs(stats[">1"]["slope_deg"]["bias"]) < 0.02, stats
    assert stats[">1"]["aspect_deg"]["median"] < 0.5, stats
    _clear_trees()


def _gradient(slope_deg, aspect_deg) -> tuple[np.ndarray, np.ndarray]:
    """(-p, -q), the downslope gradient, from slope and aspect in degrees."""
    t = np.tan(np.radians(slope_deg))
    a = np.radians(aspect_deg)
    return t * np.sin(a), t * np.cos(a)


def test_validation_slope_aspect_vs_raster_tools() -> None:
    """Face slope and aspect on the waves DEM against GDAL, WhiteboxTools and the truth.

    Each facet is compared with the true tangent plane at its centre. Then, so
    that the mesh and the rasters are compared at the same place, the two
    facets of each square of four cell centres are averaged (as gradient
    vectors) and compared with the raster tools' gradients averaged over the
    four cells, at the square's centre.
    """
    _reset()
    refs = _references()
    start, stop, _ = refs["meta"]["local_crop"]
    m = stop - start
    crop = start - 2
    obj, z = _waves_object(crop)
    slope = _preset(obj, "slope")
    percent = _preset(obj, "slope_percent")
    aspect = _preset(obj, "aspect", **{"Flat Slope": 0.0})
    c = _face_centres(obj)
    side = ts.N - 2 * crop - 1  # squares per row of the crop
    lo, hi = start + 3 - crop, stop - 4 - crop  # squares whose four cells have 5 x 5 tool values
    squares = (np.arange(lo, hi)[:, None] * side + np.arange(lo, hi)[None, :]).ravel()
    faces = np.concatenate([squares, squares + side**2])
    cx, cy = c[faces, 0], c[faces, 1]
    d = ts.waves_derivatives(cx, cy)
    true_slope, true_aspect = ts.slope_aspect(d["p"], d["q"])
    steep = true_slope > 5.0  # aspect is ill-conditioned on near-level ground
    stats = {
        "facets": len(faces),
        "facet_vs_truth": {
            "slope": _stats(slope[faces] - true_slope),
            "slope_percent": _stats(percent[faces] - 100 * np.tan(np.radians(true_slope))),
            "aspect_slope_gt_5": _stats(ts.angle_difference(aspect[faces], true_aspect)[steep]),
        },
    }
    # Squares: GeoTools' two facets, averaged.
    n_sq = len(squares)
    gx, gy = _gradient(slope[faces], aspect[faces])
    gx, gy = 0.5 * (gx[:n_sq] + gx[n_sq:]), 0.5 * (gy[:n_sq] + gy[n_sq:])
    sq_slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    sq_aspect = np.degrees(np.arctan2(gx, gy)) % 360
    rows, cols = np.divmod(squares, side)
    rows, cols = rows + crop, cols + crop  # in the full DEM
    half = (ts.N - 1) * ts.H / 2
    sx, sy = -half + (cols + 0.5) * ts.H, half - (rows + 0.5) * ts.H
    d = ts.waves_derivatives(sx, sy)
    t_slope, t_aspect = ts.slope_aspect(d["p"], d["q"])
    sq_steep = t_slope > 5.0
    stats["square_vs_truth"] = {
        "slope": _stats(sq_slope - t_slope),
        "aspect_slope_gt_5": _stats(ts.angle_difference(sq_aspect, t_aspect)[sq_steep]),
    }
    r, cc = rows - start, cols - start  # the square's north-west cell in the crop
    for name, (skey, akey) in {
        "gdal_horn": ("gdal_slope_horn", "gdal_aspect_horn"),
        "gdal_zt": ("gdal_slope_zt", "gdal_aspect_zt"),
        "wbt": ("wbt_slope", "wbt_aspect"),
    }.items():
        rx, ry = _gradient(refs[skey].astype(np.float64), refs[akey].astype(np.float64))
        rx = np.nan_to_num(rx)  # level cells (no aspect) have no gradient
        ry = np.nan_to_num(ry)
        ax = 0.25 * (rx[r, cc] + rx[r + 1, cc] + rx[r, cc + 1] + rx[r + 1, cc + 1])
        ay = 0.25 * (ry[r, cc] + ry[r + 1, cc] + ry[r, cc + 1] + ry[r + 1, cc + 1])
        ref_slope = np.degrees(np.arctan(np.hypot(ax, ay)))
        ref_aspect = np.degrees(np.arctan2(ax, ay)) % 360
        inner = (slice(3, m - 3),) * 2
        cell_slope, cell_aspect = ts.slope_aspect(
            *(ts.waves_derivatives(*ts.grid_xy())[k][start:stop, start:stop] for k in "pq")
        )
        stats[name] = {
            "cells_vs_truth_slope": _stats((refs[skey] - cell_slope)[inner]),
            "cells_vs_truth_aspect_slope_gt_5": _stats(
                ts.angle_difference(refs[akey], cell_aspect)[inner][cell_slope[inner] > 5]
            ),
            "square_vs_truth_slope": _stats(ref_slope - t_slope),
            "geotools_square_vs_square_slope": _stats(sq_slope - ref_slope),
            "geotools_square_vs_square_aspect_slope_gt_5": _stats(
                ts.angle_difference(sq_aspect, ref_aspect)[sq_steep]
            ),
        }
    # A quad grid (as GeoTools imports a GeoTIFF DEM): the quad's normal is the
    # cross product of its diagonals, so its gradient is the two facets' mean.
    quads = ts.quads(z.shape[0])
    quad_obj = _mesh_faces("Waves Quads", ts.grid_vertices(z, ts.H), quads)
    q_slope = _preset(quad_obj, "slope")[squares]
    q_aspect = _preset(quad_obj, "aspect", **{"Flat Slope": 0.0})[squares]
    stats["quad_vs_square_mean"] = {
        "slope": _stats(q_slope - sq_slope),
        "aspect": _stats(ts.angle_difference(q_aspect, sq_aspect)),
    }
    assert stats["quad_vs_square_mean"]["slope"]["max"] < 1e-3, stats["quad_vs_square_mean"]
    _report("waves_slope_aspect", stats)
    # A facet's gradient is first-order accurate: about h |curvature| / 3 rad
    # here (h = 0.1 m, curvatures to ~5 1/m).
    assert stats["facet_vs_truth"]["slope"]["median"] < 1.5, stats["facet_vs_truth"]
    assert abs(stats["facet_vs_truth"]["slope"]["bias"]) < 0.2, stats["facet_vs_truth"]
    # Square means: second order, like the raster tools; Horn and Zevenbergen-
    # Thorne smooth more (their own bias here is -0.4 and -0.3 deg at the cells).
    assert stats["square_vs_truth"]["slope"]["median"] < 0.3, stats["square_vs_truth"]
    assert stats["wbt"]["geotools_square_vs_square_slope"]["median"] < 0.3, stats["wbt"]
    for name in ("gdal_horn", "gdal_zt"):
        assert stats[name]["geotools_square_vs_square_slope"]["median"] < 1.0, stats[name]
    _clear_trees()


# --- curvature --------------------------------------------------------------------------------------


def test_validation_curvature_analytic() -> None:
    """Sphere and paraboloid caps: the closed forms, at windows of 1, 2 and 2.5 spacings."""
    _reset()
    n, spacing = 81, 0.05
    x, y = ts.grid_xy(n, spacing)
    radius, a = 5.0, 0.25
    surfaces = {
        "sphere": (np.sqrt(radius**2 - x**2 - y**2), None),
        "bowl": (-np.sqrt(radius**2 - x**2 - y**2), None),
        "paraboloid_dome": (-a * (x**2 + y**2), -a),
        "paraboloid_bowl": (a * (x**2 + y**2), a),
    }
    stats = {}
    for name, (z, coef) in surfaces.items():
        obj = _dem(name, z, spacing)
        v = _vertices(obj)
        rho2 = v[:, 0] ** 2 + v[:, 1] ** 2
        if coef is None:  # sphere of radius R: every curvature is +-1/R
            sign = 1.0 if name == "sphere" else -1.0
            truth = dict.fromkeys(("profile", "plan", "mean"), np.full(len(v), sign / radius))
        else:  # z = c rho^2: k_v = -2c/(1+g)^1.5, k_h = -2c/(1+g)^0.5, g = 4 c^2 rho^2
            g = 4 * coef * coef * rho2
            truth = {
                "profile": -2 * coef / (1 + g) ** 1.5,
                "plan": -2 * coef / np.sqrt(1 + g),
                "mean": -coef * (2 + g) / (1 + g) ** 1.5,
            }
        inner = (np.abs(v[:, 0]) < 1.6) & (np.abs(v[:, 1]) < 1.6) & (rho2 > 0.05**2)
        for kind in ("profile", "plan", "mean"):
            for steps in (1, 2, 2.5):
                values = _preset(obj, f"curvature_{kind}", Window=steps * spacing)
                rel = values[inner] / truth[kind][inner] - 1
                stats[f"{name}/{kind}/w={steps}h"] = _stats(rel)
        bpy.data.objects.remove(obj)
    _report("curvature_analytic_relative", stats)
    for key, s in stats.items():
        # 2.5 spacings: the window samples mid-facet, where a chord misses the
        # surface by about c h^2 / 4 in height: +(h / w)^2 / 4 = +4 % in curvature.
        limit = 0.055 if key.endswith("2.5h") else 0.005
        assert s["max"] < limit, (key, s)
    # The level apex of the paraboloid: profile and plan undefined, mean defined.
    obj = _dem("apex", a * (x**2 + y**2), spacing)
    apex = int(np.argmin(np.hypot(*_vertices(obj)[:, :2].T)))
    assert np.isnan(_preset(obj, "curvature_profile", Window=spacing)[apex])
    assert np.isnan(_preset(obj, "curvature_plan", Window=spacing)[apex])
    assert abs(_preset(obj, "curvature_mean", Window=spacing)[apex] + 2 * a) < 1e-3
    _clear_trees()


def test_validation_curvature_vs_whitebox() -> None:
    """The waves DEM at Window = cell size: Evans-Young on the raster, WhiteboxTools, truth."""
    _reset()
    refs = _references()
    start, stop, _ = refs["meta"]["local_crop"]
    lo = start - 3
    obj, _z = _waves_object(lo)
    ids = _ids(np.arange(start, stop), lo)
    crop = (slice(start, stop),) * 2
    z = ts.waves(*ts.grid_xy())
    ey = ts.curvatures(**ts.evans_young(z.astype(np.float32), ts.H))
    d = ts.waves_derivatives(*ts.grid_xy())
    truth = ts.curvatures(**d)
    g = d["p"] ** 2 + d["q"] ** 2
    contour = truth["plan"] * np.sqrt(1 + g) / np.sqrt(g)  # k_h / sin(slope)
    inner = (slice(3, -3),) * 2
    wbt = {
        "profile": "wbt_profile_curvature",
        "plan": "wbt_tangential_curvature",
        "mean": "wbt_mean_curvature",
    }
    stats = {}
    for kind in ("profile", "plan", "mean"):
        gt = _preset(obj, f"curvature_{kind}", Window=ts.H)[ids].reshape(stop - start, -1)
        gt_c, ey_c, tr_c = gt[inner], ey[kind][crop][inner], truth[kind][crop][inner]
        ref = refs[wbt[kind]][inner]
        stats[kind] = {
            "geotools_vs_evans_young_raster": _stats(gt_c - ey_c),
            "geotools_vs_truth": _stats(gt_c - tr_c),
            "evans_young_vs_truth": _stats(ey_c - tr_c),
            f"{wbt[kind]}_vs_truth": _stats(ref - tr_c),
            f"geotools_vs_{wbt[kind]}": _stats(gt_c - ref),
            f"geotools_vs_minus_{wbt[kind]}": _stats(gt_c + ref),
            "truth_rms": float(np.sqrt(np.nanmean(tr_c**2))),
        }
        # GeoTools at Window = spacing is Evans-Young on the raster (the rays
        # land on vertices), to float32: heights ~0.5 m, so ~6e-8 m / (3 h^2).
        assert stats[kind]["geotools_vs_evans_young_raster"]["max"] < 2e-4, stats[kind]
    stats["wbt_plan_curvature_vs_contour_curvature"] = _stats(
        refs["wbt_plan_curvature"][inner] - contour[crop][inner]
    )
    _report("waves_curvature", stats)
    _clear_trees()


def test_validation_curvature_float32() -> None:
    """Window heights are float32: curvature noise grows with |z| / w^2."""
    _reset()
    n, spacing, radius = 41, 0.05, 5.0
    x, y = ts.grid_xy(n, spacing)
    stats = {}
    for offset in (0.0, 100.0, 1000.0):
        obj = _dem(f"Sphere{offset:g}", np.sqrt(radius**2 - x**2 - y**2) + offset, spacing)
        v = _vertices(obj)
        inner = (np.abs(v[:, 0]) < 0.8) & (np.abs(v[:, 1]) < 0.8)
        for window in (0.05, 0.2):
            values = _preset(obj, "curvature_mean", Window=window)[inner]
            stats[f"z+{offset:g}/w={window}"] = _stats(values * radius - 1)
        bpy.data.objects.remove(obj)
    _report("curvature_float32_relative", stats)
    assert stats["z+0/w=0.05"]["max"] < 0.01, stats
    _clear_trees()


# --- TRI, TPI, local relief, rugosity ----------------------------------------------------------------


def test_validation_tri() -> None:
    _reset()
    refs = _references()
    start, stop, _ = refs["meta"]["local_crop"]
    lo = start - 3
    obj, _z = _waves_object(lo)
    inner = (slice(3, -3),) * 2
    tri = _preset(obj, "tri", Window=ts.H)[_ids(np.arange(start, stop), lo)]
    tri = tri.reshape(stop - start, -1)[inner]
    riley = refs["gdal_tri_riley"][inner]
    stats = {
        "geotools_vs_gdal_riley": _stats(tri - riley),
        "geotools_vs_wbt_ruggedness_x_sqrt8": _stats(
            tri - math.sqrt(8) * refs["wbt_ruggedness_index"][inner]
        ),
        "geotools_over_gdal_wilson_median": float(
            np.nanmedian(tri / refs["gdal_tri_wilson"][inner])
        ),
        "riley_rms": float(np.sqrt(np.nanmean(riley.astype(np.float64) ** 2))),
    }
    # The rays land on the vertices: Riley on the raster, to float32.
    assert stats["geotools_vs_gdal_riley"]["max"] < 1e-5, stats
    assert stats["geotools_vs_wbt_ruggedness_x_sqrt8"]["max"] < 1e-5, stats
    # A plane in any direction: w |grad z| sqrt(6).
    planes = {}
    for aspect in (0.0, 22.5, 45.0, 200.0):
        x, y = ts.grid_xy(21, 0.05)
        a = math.radians(aspect)
        tan = math.tan(math.radians(30.0))
        plane = _dem(f"Plane{aspect}", -tan * (x * math.sin(a) + y * math.cos(a)), 0.05)
        values = _preset(plane, "tri")  # Window 0.05 (default)
        planes[aspect] = _stats(values / (0.05 * tan * math.sqrt(6)) - 1)
        assert planes[aspect]["max"] < 1e-4, (aspect, planes[aspect])
        assert np.isnan(values).sum() == 21 * 4 - 4  # the boundary ring: the window leaves the mesh
    stats["planes_relative"] = planes
    _report("tri", stats)
    _clear_trees()


def _mesh_disc_mean(z, spacing, px, py, dx, dy) -> np.ndarray:
    """Mean of the triangulated DEM over the sample offsets, ignoring samples off the mesh."""
    heights = ts.mesh_height(z, spacing, px[:, None] + dx[None, :], py[:, None] + dy[None, :])
    return np.nanmean(heights, axis=1)


def test_validation_tpi_and_local_relief() -> None:
    _reset()
    refs = _references()
    x, y = ts.grid_xy()
    stats = {}
    cases = {
        # preset, inputs, analytic annulus (inner, outer), samples
        "tpi_default": ("tpi", {}, (0.0, 0.5), 96),
        "tpi_annulus": ("tpi", {"Inner Radius": 0.3, "Outer Radius": 0.8}, (0.3, 0.8), 96),
        "local_relief_default": ("local_relief", {}, (0.0, 2.0), 48),
    }
    lo = 30
    obj, z = _waves_object(lo)
    z32 = z.astype(np.float32).astype(np.float64)
    rows = np.arange(50, 91, 4)  # more than 2 m (the LRM radius) inside the crop
    keep = _ids(rows, lo)
    px, py = x[np.ix_(rows, rows)].ravel(), y[np.ix_(rows, rows)].ravel()
    pz = z32.ravel()[keep]
    for label, (preset, inputs, (inner, outer), samples) in cases.items():
        values = _preset(obj, preset, **inputs)[keep]
        dx, dy = ts.sunflower(samples, inner, outer)
        discrete_mesh = pz - _mesh_disc_mean(z32, ts.H, px, py, dx, dy)
        discrete_true = ts.waves(px, py) - np.mean(ts.waves(px[:, None] + dx, py[:, None] + dy), 1)
        continuous = ts.waves(px, py) - ts.waves_disc_mean(px, py, inner, outer)
        stats[label] = {
            "geotools_vs_sunflower_on_mesh": _stats(values - discrete_mesh),
            "geotools_vs_weiss_continuous": _stats(values - continuous),
            "sunflower_on_surface_vs_continuous": _stats(discrete_true - continuous),
            "mesh_interpolation": _stats(discrete_mesh - discrete_true),
            "continuous_rms": float(np.sqrt(np.mean(continuous**2))),
        }
        # The preset is exactly its documented sampling of this mesh (float32).
        assert stats[label]["geotools_vs_sunflower_on_mesh"]["max"] < 2e-5, (label, stats[label])
        if preset == "tpi":
            # The default Samples gives Weiss's disc mean to about 2% RMS of the TPI
            # (sampling 1.7%; the rest is the mesh's linear interpolation).
            relative = (
                stats[label]["geotools_vs_weiss_continuous"]["rms"] / stats[label]["continuous_rms"]
            )
            assert relative < 0.03, (label, relative)
    bpy.data.objects.remove(obj)

    # Matched second moments: gdaldem TPI (8 neighbours, <x^2> = 0.75 h^2) and
    # RVT's simple LRM (21 x 21 cells, <x^2> = 110/3 h^2) against GeoTools discs
    # of the same <x^2>, 256 samples.
    start, stop, _ = refs["meta"]["local_crop"]
    lo = start - 3
    obj, _z = _waves_object(lo)
    r_gdal = ts.matched_disc_radius(ts.square_second_moment(1, ts.H, centre=False))
    tpi = _preset(obj, "tpi", **{"Outer Radius": r_gdal, "Samples": 256})
    tpi = tpi[_ids(np.arange(start, stop), lo)].reshape(stop - start, -1)
    inner = (slice(3, -3),) * 2
    gdal = refs["gdal_tpi"].astype(np.float64)
    xc, yc = x[start:stop, start:stop], y[start:stop, start:stop]
    stats["tpi_vs_gdal_matched"] = {
        "radius": r_gdal,
        "geotools_vs_gdal": _stats((tpi - gdal)[inner]),
        "gdal_rms": float(np.sqrt(np.mean(gdal[inner] ** 2))),
        "gdal_vs_weiss_disc": _stats(
            (gdal - (ts.waves(xc, yc) - ts.waves_disc_mean(xc, yc, 0, r_gdal)))[inner]
        ),
    }
    bpy.data.objects.remove(obj)

    hs, _he, hstep = refs["meta"]["horizon_crop"]
    rows = np.arange(40, 101, 2)
    fixture = (rows - hs) // hstep
    r_rvt = ts.matched_disc_radius(ts.square_second_moment(refs["meta"]["slrm_radius_cells"], ts.H))
    lo = 40 - math.ceil(r_rvt / ts.H) - 1
    obj, _z = _waves_object(lo)
    lrm = _preset(obj, "local_relief", Radius=r_rvt, Samples=256)[_ids(rows, lo)]
    slrm = refs["rvt_slrm"].astype(np.float64)[np.ix_(fixture, fixture)].ravel()
    pxh, pyh = x[np.ix_(rows, rows)].ravel(), y[np.ix_(rows, rows)].ravel()
    stats["local_relief_vs_rvt_slrm_matched"] = {
        "radius": r_rvt,
        "geotools_vs_rvt": _stats(lrm - slrm),
        "rvt_rms": float(np.sqrt(np.mean(slrm**2))),
        "geotools_vs_weiss_disc": _stats(
            lrm - (ts.waves(pxh, pyh) - ts.waves_disc_mean(pxh, pyh, 0, r_rvt))
        ),
    }
    _report("tpi_local_relief", stats)
    _clear_trees()


def _gaussian_face_blur(values: np.ndarray, side: int, sigma: float) -> np.ndarray:
    """Normalised Gaussian blur of per-facet values of a triangulated grid, by centroid distance."""
    n_sq = side * side
    grids = [values[:n_sq].reshape(side, side), values[n_sq:].reshape(side, side)]
    # Centroid offsets within a square (units of h, x east, y north): lower (sw, se, ne)
    # and upper (sw, ne, nw) triangles.
    offsets = [(2 / 3, 1 / 3), (1 / 3, 2 / 3)]
    k = math.ceil(3 * sigma / ts.H) + 1
    out = []
    for kind in range(2):
        total = np.zeros((side, side))
        weight = np.zeros((side, side))
        for other in range(2):
            for di in range(-k, k + 1):
                for dj in range(-k, k + 1):
                    dx = dj + offsets[other][0] - offsets[kind][0]
                    dy = -di + offsets[other][1] - offsets[kind][1]
                    w = math.exp(-(dx * dx + dy * dy) * ts.H**2 / (2 * sigma * sigma))
                    shifted = np.full((side, side), np.nan)
                    src = grids[other][
                        max(di, 0) : side + min(di, 0), max(dj, 0) : side + min(dj, 0)
                    ]
                    shifted[max(-di, 0) : side + min(-di, 0), max(-dj, 0) : side + min(-dj, 0)] = (
                        src
                    )
                    valid = np.isfinite(shifted)
                    total += np.where(valid, shifted * w, 0.0)
                    weight += np.where(valid, w, 0.0)
        out.append((total / weight).ravel())
    return np.concatenate(out)


def _rugosity_sigma(obj: bpy.types.Object, inside: np.ndarray) -> dict[str, float]:
    """The Gaussian sigma that best reproduces Rugosity at Radius 0.2 and 0.4 (report only).

    On a grid every facet has the same plan area, so the preset's ratio is the
    blur of the facets' own ratios 1 / cos(slope); fit a normalised Gaussian
    blur of those (by centroid distance) to it.
    """
    side = round(math.sqrt(len(obj.data.polygons) / 2))
    facet = 1 / np.cos(np.radians(_preset(obj, "slope")))
    fitted = {}
    for radius in (0.2, 0.4):
        values = _preset(obj, "rugosity", Radius=radius)
        best = min(
            (
                float(
                    np.sqrt(
                        np.nanmean((_gaussian_face_blur(facet, side, sg) - values)[inside] ** 2)
                    )
                ),
                sg,
            )
            for sg in np.arange(0.02, 0.41, 0.01)
        )
        fitted[f"radius={radius}"] = {"sigma": round(best[1], 3), "rms": best[0]}
    return fitted


def test_validation_rugosity() -> None:
    _reset()
    refs = _references()
    obj, _z = _waves_object(refs["meta"]["local_crop"][0] - 2)
    c = _face_centres(obj)
    d = ts.waves_derivatives(c[:, 0], c[:, 1])
    truth = np.sqrt(1 + d["p"] ** 2 + d["q"] ** 2)
    start, stop, _ = refs["meta"]["local_crop"]
    half = (ts.N - 1) * ts.H / 2
    x0, y0 = -half + start * ts.H, half - start * ts.H
    margin = 3 * ts.H
    inside = (
        (c[:, 0] > x0 + margin)
        & (c[:, 0] < x0 + (stop - start - 1) * ts.H - margin)
        & (c[:, 1] < y0 - margin)
        & (c[:, 1] > y0 - (stop - start - 1) * ts.H + margin)
    )
    sar = ts.bilinear(refs["wbt_surface_area_ratio"], x0, y0, ts.H, c[inside, 0], c[inside, 1])
    facet = 1 / np.cos(np.radians(_preset(obj, "slope")[inside]))  # each facet's own ratio
    stats = {"wbt_sar_vs_local_truth": _stats(sar - truth[inside])}
    for radius in (0.0, 0.1, 0.3):
        values = _preset(obj, "rugosity", Radius=radius)[inside]
        stats[f"radius={radius}"] = {
            "geotools_vs_facet": _stats(values - facet),
            "geotools_vs_local_truth": _stats(values - truth[inside]),
            "geotools_vs_wbt_sar": _stats(values - sar),
        }
    if FULL:
        stats["effective_sigma"] = _rugosity_sigma(obj, inside)
    _report("rugosity", stats)
    # Radius 0 (no blur): each facet's own area ratio, 1 / cos(slope).
    assert stats["radius=0.0"]["geotools_vs_facet"]["max"] < 1e-5, stats
    _clear_trees()


# --- sky-view factor and openness --------------------------------------------------------------------


HORIZON = ("sky_view_factor", "openness_positive", "openness_negative")
RVT_KEY = {
    "sky_view_factor": "svf",
    "openness_positive": "openness_positive",
    "openness_negative": "openness_negative",
}


def test_validation_horizon_cone_and_pit() -> None:
    """Analytic horizons: the apex of a cone peak and pit, and a vertical-walled pit."""
    _reset()
    beta = math.radians(30.0)
    n, spacing = 61, 0.1  # 3 m from the apex: more than Max Distance (2 m)
    x, y = ts.grid_xy(n, spacing)
    rho = np.hypot(x, y)
    expected = {
        # peak: every horizon is -beta (down the flank); pit: +beta
        "peak": {
            "sky_view_factor": 1.0,
            "openness_positive": 90 + 30,
            "openness_negative": 90 - 30,
        },
        "pit": {
            "sky_view_factor": 1 - math.sin(beta),
            "openness_positive": 90 - 30,
            "openness_negative": 90 + 30,
        },
    }
    stats: dict[str, object] = {}
    # The apex's answer is the same in every direction: 4 directions in CI.
    directions = {} if FULL else {"Directions": 4}
    for name, sign in (("peak", -1.0), ("pit", 1.0)):
        # A polar cone (rays from the apex) has the cone's slope in every
        # direction; the grid cone's apex facets are up to 2 deg steeper
        # between the grid axes, which shows in the lowest (peak) or highest
        # (pit) horizon: reported, not asserted.
        polar = _mesh(f"{name} polar", *ts.polar_cone(30.0, 2.5, 25, 128, sign))
        for preset in HORIZON:
            live = _preset(polar, preset, **directions)[0]
            fast = _fast(polar, preset, **directions)[0]
            want = expected[name][preset]
            stats[f"{name}/{preset}"] = {"live": live, "fast": fast, "exact": want}
            tolerance = 0.006 if preset == "sky_view_factor" else RESOLUTION / 2 + 0.02
            assert abs(live - want) <= tolerance, (name, preset, live, want)
            assert abs(fast - want) <= tolerance, (name, preset, fast, want)
        bpy.data.objects.remove(polar)
        if FULL:
            grid = _dem(f"{name} grid", sign * rho * math.tan(beta), spacing)
            apex = (n // 2) * n + n // 2
            for preset in HORIZON:
                stats[f"{name}/{preset}"]["grid_live"] = _preset(grid, preset)[apex]
            bpy.data.objects.remove(grid)

    # A cylindrical pit with vertical walls (not a height field): radius 1 m,
    # depth 0.5 m. Floor points against the exact horizon through the bisection.
    radius, depth = 1.0, 0.5
    verts, tris = ts.polar_pit(radius, depth, 3.0, 12, 256)
    obj = _mesh("Cylinder Pit", verts, tris)
    floor = (np.abs(verts[:, 2] + depth) < 1e-9) & (np.hypot(verts[:, 0], verts[:, 1]) < 0.9)
    core = h.submodule("analysis.preset_bake")
    count = 16 if FULL else 8
    azimuths = np.arange(count) * 2 * math.pi / count
    # Rays start Ray Offset (2 mm) above the floor.
    exact = np.array(
        [ts.pit_horizon(px, py, radius, depth - 0.002, azimuths) for px, py in verts[floor, :2]]
    )
    svf_exact = 1 - np.mean(np.sin(exact), axis=1)
    search = core.horizon_search("sky_view_factor", {"Directions": count})
    angle_sum, sine_sum = core.bisect_horizon(
        search, lambda d, theta: np.radians(theta) <= exact[:, d], len(exact)
    )
    svf_bisected = core.horizon_values("sky_view_factor", angle_sum, sine_sum, count)
    live = _preset(obj, "sky_view_factor", Directions=count)[floor]
    fast = _fast(obj, "sky_view_factor", Directions=count)[floor]
    centre = int(np.argmin(np.hypot(verts[floor, 0], verts[floor, 1])))
    stats["cylinder_pit"] = {
        "centre_exact": 1 - (depth - 0.002) / math.hypot(radius, depth - 0.002),
        "centre_live": float(live[centre]),
        "live_vs_exact": _stats(live - svf_exact),
        "live_vs_bisected_exact": _stats(live - svf_bisected),
        "fast_vs_live": _stats(fast - live),
        "floor_points": int(floor.sum()),
    }
    _report("horizon_analytic", stats)
    # The wall is a 256-gon: horizons within ~0.01 deg of the circle's.
    # (A floor point's ray may graze a vertex of the 256-gon: a few differ by 1e-3.)
    assert stats["cylinder_pit"]["live_vs_bisected_exact"]["median"] < 1e-5, stats["cylinder_pit"]
    assert stats["cylinder_pit"]["live_vs_bisected_exact"]["max"] < 3e-3, stats["cylinder_pit"]
    assert stats["cylinder_pit"]["fast_vs_live"]["max"] < 1e-4, stats["cylinder_pit"]
    assert abs(live[centre] - stats["cylinder_pit"]["centre_exact"]) < 0.006
    _clear_trees()


def test_validation_horizon_vs_rvt() -> None:
    """The waves DEM: live and fast presets, RVT, and exact horizons.

    Two exact references: the continuous waves surface (in the fixture) and
    the mesh itself (computed here from the vertices and Blender's vertex
    normals), both with GeoTools' definition (Max Distance from the ray
    origin, Ray Offset along the normal).
    """
    _reset()
    refs = _references()
    first, last = (40, 100) if FULL else (50, 90)  # cells of the full DEM compared
    lo = first - 20  # Max Distance (2 m) more
    obj, z = _waves_object(lo)
    hs, _he, hstep = refs["meta"]["horizon_crop"]
    rows = np.arange(first, last + 1, 2)
    ids = _ids(rows, lo)
    fixture = (rows - hs) // hstep
    pick = np.ix_(fixture, fixture)
    sub = np.arange(first, last + 1, 4)  # the mesh-exact subset
    sub_ids = _ids(sub, lo)
    in_sub = np.isin(ids, sub_ids)
    co = _vertices(obj)[sub_ids]
    normals = np.empty(len(obj.data.vertices) * 3, np.float32)
    obj.data.vertex_normals.foreach_get("vector", normals)
    normals = normals.reshape(-1, 3).astype(np.float64)[sub_ids]
    z32 = z.astype(np.float32).astype(np.float64)
    mesh_up = ts.horizon_values(ts.mesh_horizon(z32, ts.H, co + 0.002 * normals, 16, 2.0))
    mesh_down = ts.horizon_values(
        ts.mesh_horizon(z32, ts.H, co - 0.002 * normals, 16, 2.0, below=True)
    )
    mesh_exact = {
        "sky_view_factor": mesh_up["svf"],
        "openness_positive": mesh_up["openness_positive"],
        "openness_negative": mesh_down["openness_negative"],
    }
    stats = {}
    for preset in HORIZON:
        key = RVT_KEY[preset]
        live = _preset(obj, preset)[ids]
        fast = _fast(obj, preset)[ids]
        rvt = refs[f"rvt_{key}"].astype(np.float64)[pick].ravel()
        exact = refs[f"exact_geotools_{key}"].astype(np.float64)[pick].ravel()
        exact_rvt = refs[f"exact_rvt_{key}"].astype(np.float64)[pick].ravel()
        stats[preset] = {
            "fast_vs_live": _stats(fast - live),
            "live_vs_mesh_exact": _stats(live[in_sub] - mesh_exact[preset]),
            "mesh_exact_vs_continuous_exact": _stats(mesh_exact[preset] - exact[in_sub]),
            "live_vs_continuous_exact": _stats(live - exact),
            "rvt_vs_exact_rvt_definition": _stats(rvt - exact_rvt),
            "live_vs_rvt": _stats(live - rvt),
            "continuous_exact_vs_exact_rvt_definition": _stats(exact - exact_rvt),
            "spread": float(np.std(exact)),
        }
    _report("horizon_waves", stats)
    for preset, s in stats.items():
        # Fast and live cast the same rays; a ray grazing the surface within
        # float32 may count as a hit in one engine only (docs/analysis-presets.md:
        # up to 0.02 SVF or 7 deg openness, on a few in 10^4 corners).
        assert s["fast_vs_live"]["median"] == 0.0, s
        assert s["fast_vs_live"]["rms"] < (2e-3 if preset == "sky_view_factor" else 0.2), s
        # Against the mesh's own horizon: within the bisection's half bracket per
        # direction (0.35 deg; SVF: sin changes by at most that much).
        limit = math.sin(math.radians(RESOLUTION / 2)) if preset == "sky_view_factor" else 0.36
        assert s["live_vs_mesh_exact"]["max"] < limit, (preset, s)
    _clear_trees()
