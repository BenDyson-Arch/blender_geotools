"""Validation of the horizon bake (Cycles) against independent and closed-form skylines.

* A synthetic DEM (a valley ringed by hills, a mountain beyond, a near
  knoll; 401 x 401 nodes 100 m apart) built as the triangulated grid that
  scripts/validation/archaeoastronomy/horizon_reference.py marches exactly,
  and also run through GRASS GIS r.horizon (tests/data/validation/
  archaeoastronomy/horizon_reference.json).
* A cone seen from beside it, against its closed form.
* The dip of the horizon over a level plain: √(2 e (1 - k) / R).
* A near crest just below a farther range that curvature lowers beneath it.

See docs/validation/archaeoastronomy.md.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import _harness as h
import bpy
import numpy as np

DATA = Path(__file__).resolve().parents[1] / "data" / "validation" / "archaeoastronomy"
EYE = 1.6
SPACING, HALF = 100.0, 20_000.0


def _horizon():
    return h.submodule("analysis.horizon")


def _reset() -> None:
    h.reset_scene()
    analysis = bpy.context.scene.geotools_analysis
    for prop in analysis.archaeo.bl_rna.properties:
        if prop.identifier not in {"rna_type", "name"}:
            analysis.archaeo.property_unset(prop.identifier)
    props = h.project()
    props.placement_mode = "LOCAL"
    props.anchor_lon, props.anchor_lat = "0.0", "0.0"


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _mesh(name: str, verts: np.ndarray, tris: np.ndarray, collection) -> bpy.types.Object:
    """A triangle mesh from (n, 3) vertices and (m, 3) triangles, without Python loops."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", np.asarray(verts, dtype=np.float32).ravel())
    mesh.loops.add(3 * len(tris))
    mesh.loops.foreach_set("vertex_index", np.asarray(tris, dtype=np.int32).ravel())
    mesh.polygons.add(len(tris))
    mesh.polygons.foreach_set("loop_start", np.arange(0, 3 * len(tris), 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    mesh.validate()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    return obj


def _grid_tris(n: int) -> np.ndarray:
    """Each cell of an n x n node grid (row-major, y up) split along its SW-NE diagonal."""
    j, i = np.mgrid[0 : n - 1, 0 : n - 1]
    v00 = (j * n + i).ravel()
    v10, v01, v11 = v00 + 1, v00 + n, v00 + n + 1
    return np.concatenate([np.stack([v00, v10, v11], 1), np.stack([v00, v11, v01], 1)])


def _dem() -> np.ndarray:
    """The reference script's DEM, exactly (float32 heights, z[j, i] at (x[i], y[j]))."""
    x = np.arange(-HALF, HALF + 0.5 * SPACING, SPACING)
    xx, yy = np.meshgrid(x, x)
    r = np.hypot(xx, yy)
    theta = np.arctan2(xx, yy)
    z = (
        200.0
        + 350.0 * np.exp(-(((r - 7000.0) / 2000.0) ** 2)) * (1.0 + 0.6 * np.sin(3.0 * theta + 0.5))
        + 900.0 * np.exp(-((xx - 13000.0) ** 2 + (yy - 9000.0) ** 2) / 2500.0**2)
        + 120.0 * np.exp(-((xx + 1500.0) ** 2 + (yy - 1800.0) ** 2) / 500.0**2)
        + 15.0 * np.sin(xx / 650.0) * np.cos(yy / 810.0)
    )
    return z.astype(np.float32)


def _observer(point) -> None:
    bpy.context.scene.cursor.location = point
    assert bpy.ops.geotools.archaeo_observer_cursor() == {"FINISHED"}


def _profile(**settings):
    props = bpy.context.scene.geotools_analysis.archaeo
    for key, value in settings.items():
        setattr(props, key, value)
    result, error = h.call_op(bpy.ops.geotools.archaeo_horizon)
    assert result == {"FINISHED"}, error or props.profile_error
    return _horizon().HorizonProfile.from_json(props.profile)


def test_dem_horizon_against_tin_march_and_grass() -> None:
    """GeoTools on the DEM mesh against the exact TIN skyline and GRASS r.horizon.

    Against the exact skyline of the same triangles: within 0.003° at every
    degree (the fine step, 0.0016°, biased low, plus float32 positions).
    Against r.horizon (raster sampling, curvature, no refraction): median
    within 0.002° and RMS under 0.02° where the skyline is the far hills;
    the near knoll, 2.3 km off and steep, differs by up to 0.25° there
    because r.horizon samples the raster cells, not the triangles.
    """
    ref = json.loads((DATA / "horizon_reference.json").read_text())
    z = _dem()
    assert hashlib.sha256(z.tobytes()).hexdigest() == ref["dem_sha256"]
    _reset()
    n = z.shape[0]
    x = np.arange(-HALF, HALF + 0.5 * SPACING, SPACING)
    xx, yy = np.meshgrid(x, x)
    verts = np.stack([xx.ravel(), yy.ravel(), z.ravel().astype(np.float64)], axis=1)
    _mesh("DEM", verts, _grid_tris(n), _source())
    c = n // 2
    _observer((0.0, 0.0, float(z[c, c])))

    tin = ref["tin"]
    default = _profile(azimuth_step=1.0, eye_height=EYE, curvature=True, refraction=True)
    assert np.allclose(default.azimuth, ref["azimuth"])
    error = default.elevation - np.array(tin["k013"])
    assert np.abs(error).max() < 0.003, (np.abs(error).max(), int(np.argmax(np.abs(error))))
    assert not default.edge.any() and not default.open.any()
    reach = default.distance * np.cos(np.radians(default.geometric))
    # The grazing ray meets a broad crest's near slope: the distance is the crest's to a cell.
    assert np.median(np.abs(reach - np.array(tin["distance_k013"]))) < 50.0
    assert np.abs(reach - np.array(tin["distance_k013"])).max() < 300.0

    no_refraction = _profile(curvature=True, refraction=False)
    error = no_refraction.elevation - np.array(tin["k0"])
    assert np.abs(error).max() < 0.003
    flat = _profile(curvature=False, refraction=False)
    error = flat.elevation - np.array(tin["flat"])
    assert np.abs(error).max() < 0.003

    grass = np.array(ref["grass"]["k0"])
    far = np.array(tin["distance_k013"]) > 4000.0
    diff = no_refraction.elevation - grass
    for name, ours, theirs in (
        ("k=0.13 vs TIN", default.elevation, tin["k013"]),
        ("k=0 vs TIN", no_refraction.elevation, tin["k0"]),
        ("flat vs TIN", flat.elevation, tin["flat"]),
        ("k=0 vs GRASS", no_refraction.elevation, grass),
        ("k=0 vs GRASS, far", no_refraction.elevation[far], grass[far]),
    ):
        e = np.asarray(ours) - np.asarray(theirs)
        print(
            f"{name}: max {np.abs(e).max():.5f} rms {np.sqrt(np.mean(e**2)):.5f} "
            f"median {np.median(e):+.5f} n {len(e)}"
        )
    print("rays", default.info["rays"], "fine brackets", default.info["fine_brackets"])
    assert abs(np.median(diff)) < 0.002
    assert np.sqrt(np.mean(diff[far] ** 2)) < 0.02
    assert np.abs(diff).max() < 0.25


def _cone_skyline(azimuth: float, apex_y: float, radius: float, height: float) -> float:
    """The skyline of a right circular cone on a plain, by a fine 1-D search (k = 0.13)."""
    s = np.arange(1.0, apex_y + radius + 1.0, 0.25)
    a = math.radians(azimuth)
    rho = np.hypot(s * math.sin(a), s * math.cos(a) - apex_y)
    z = np.where(rho < radius, height * (1.0 - rho / radius), 0.0)
    drop = (1.0 - 0.13) * s / (2.0 * 6_371_000.0)
    return float(np.degrees(np.arctan2(z - EYE, s) - drop).max())


def test_cone_against_closed_form() -> None:
    """A 512-sided cone 400 m high, 1.5 km in radius, 3 km north: its silhouette.

    The facets sit at most r (1 - cos(π / 512)) inside the true cone, under
    1×10⁻⁴°; the fine step biases low by up to 0.0016°: 0.0025° allowed.
    """
    _reset()
    collection = _source()
    _mesh(
        "Plain",
        np.array([(-6000, -6000, 0), (6000, -6000, 0), (6000, 6000, 0), (-6000, 6000, 0)]),
        np.array([(0, 1, 2), (0, 2, 3)]),
        collection,
    )
    sides, radius, height, apex_y = 512, 1500.0, 400.0, 3000.0
    t = 2.0 * np.pi * np.arange(sides) / sides
    ring = np.stack([radius * np.sin(t), apex_y + radius * np.cos(t), np.full(sides, 0.01)], 1)
    verts = np.concatenate([ring, [(0.0, apex_y, height)]])
    tris = np.stack([np.arange(sides), (np.arange(sides) + 1) % sides, np.full(sides, sides)], 1)
    _mesh("Cone", verts, tris, collection)
    _observer((0.0, 0.0, 0.0))
    profile = _profile(azimuth_step=1.0, eye_height=EYE, curvature=True, refraction=True)
    for az in [*range(29), *range(332, 360)]:
        expected = _cone_skyline(float(az), apex_y, radius, height)
        got = float(profile.elevation[profile.nearest(az)])
        assert abs(got - expected) < 0.0025, (az, got, expected)


def test_dip_of_the_horizon() -> None:
    """A level plain 100 km across, eye 20 m up: the dip is √(2 e (1 - k) / R) = 0.1339°."""
    _reset()
    _mesh(
        "Plain",
        np.array([(-50e3, -50e3, 0), (50e3, -50e3, 0), (50e3, 50e3, 0), (-50e3, 50e3, 0)]),
        np.array([(0, 1, 2), (0, 2, 3)]),
        _source(),
    )
    _observer((0.0, 0.0, 0.0))
    eye = 20.0
    profile = _profile(azimuth_step=30.0, eye_height=eye, curvature=True, refraction=True)
    k, radius = _horizon().TERRESTRIAL_K, _horizon().EARTH_RADIUS
    dip = math.degrees(math.sqrt(2.0 * eye * (1.0 - k) / radius))
    reach = math.sqrt(2.0 * eye * radius / (1.0 - k))  # 17.1 km
    assert np.abs(profile.elevation + dip).max() < 0.002, profile.elevation
    distance = profile.distance * np.cos(np.radians(profile.geometric))
    # The maximum is broad: the fine step (0.0016°) leaves the distance to about 30 %.
    assert np.abs(distance - reach).max() < 0.35 * reach
    # With curvature off the farthest ray to hit the plain (46 to 58 km) is the skyline.
    flat = _profile(curvature=False)
    assert np.all((flat.elevation < 0.0) & (flat.elevation > -0.03)), flat.elevation


def test_near_crest_below_a_farther_range() -> None:
    """A crest 1 km off at 1.099° and a range 26 km off at 1.101° (0.998° after curvature).

    The coarse ray at 1.1° passes over the crest and hits the range; the
    crest must still be found (1.095°), to the fine step.
    """
    _reset()
    collection = _source()

    def wall(name: str, d: float, elevation: float) -> None:
        top = EYE + d * math.tan(math.radians(elevation))
        half = 20_000.0
        verts = np.array(
            [
                (-half, d, 0),
                (half, d, 0),
                (half, d, top),
                (-half, d, top),
                (-half, d + 50, top),
                (half, d + 50, top),
            ]
        )
        tris = np.array([(0, 1, 2), (0, 2, 3), (3, 2, 5), (3, 5, 4)])
        _mesh(name, verts, tris, collection)

    wall("Crest", 1000.0, 1.099)
    wall("Range", 26_000.0, 1.101)
    _observer((0.0, 0.0, 0.0))
    profile = _profile(azimuth_step=10.0, eye_height=EYE, curvature=True, refraction=True)
    drop = float(_horizon().curvature_drop(1000.0))
    expected = 1.099 - drop
    got = float(profile.at(0.0))
    assert expected - 0.002 < got <= expected + 1e-6, (got, expected)
    assert profile.info["fine_brackets"] > int((~profile.open).sum())  # the crest's too
