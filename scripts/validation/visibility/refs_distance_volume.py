# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy==2.5.4", "trimesh==5.2.1", "rtree==1.4.1", "scipy==1.18.1", "shapely==2.2.0"]
# ///
"""Reference values for Distance to Surface, Distance to Feature, Cut and Fill and Closed Volume.

* Distance to Surface: trimesh's closest point on the reference (distance and
  triangle) at every measured vertex, with two signs: the normal of the
  triangle trimesh returns (CloudCompare's C2M convention) and the
  angle-weighted pseudonormal of the closest feature (Bærentzen and Aanæs 2005,
  correct at edges and vertices).
* Distance to Feature: shapely (GEOS) plan distances to a polygon, a line and points.
* Closed Volume: trimesh's mesh volume of each solid (from the float32
  vertices Blender stores) and the analytic volume of the faceted solid.
* Cut and Fill: the raster method (depth at every node x cell area, numpy)
  at the mesh's spacing, and a fine numerical integral (1 cm) of the analytic surfaces.

Run from the repository root:

    uv run scripts/validation/visibility/refs_distance_volume.py

It writes ``tests/data/validation/visibility/distance_volume.npz`` and ``.json``.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import shapely
import trimesh

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cases as C

OUT = HERE.parents[2] / "tests" / "data" / "validation" / "visibility"


def f32(co: np.ndarray) -> np.ndarray:
    """Vertex positions as Blender stores them."""
    return np.asarray(co, np.float32).astype(np.float64)


def pseudonormal_sign(mesh: trimesh.Trimesh, points, closest, tri) -> np.ndarray:
    """Sign of (p - q) . n, with n the angle-weighted pseudonormal of q's feature.

    q on a face's interior: the face normal; on an edge: the sum of its two faces' normals;
    at a vertex: the angle-weighted sum of its faces' normals (Bærentzen and Aanæs 2005).
    """
    v = mesh.vertices[mesh.faces[tri]]  # (N, 3, 3)
    bary = trimesh.triangles.points_to_barycentric(v, closest)
    eps = 1e-7
    zero = bary < eps
    normals = mesh.face_normals[tri].copy()
    angles = mesh.face_angles  # per face, per corner
    vertex_normals = np.zeros((len(mesh.vertices), 3))
    for k in range(3):
        np.add.at(vertex_normals, mesh.faces[:, k], mesh.face_normals * angles[:, [k]])
    edge_normal = {}
    for face, adj in zip(mesh.face_adjacency, mesh.face_adjacency_edges, strict=True):
        edge_normal[tuple(sorted(adj))] = mesh.face_normals[face].sum(axis=0)
    for n in range(len(points)):
        count = int(zero[n].sum())
        corners = mesh.faces[tri[n]]
        if count == 2:  # at a vertex
            normals[n] = vertex_normals[corners[np.argmax(bary[n])]]
        elif count == 1:  # on the edge opposite the zero coordinate
            k = int(np.argmax(zero[n]))
            edge = tuple(sorted(np.delete(corners, k)))
            normals[n] = edge_normal.get(edge, normals[n])
    return np.sign(np.einsum("ij,ij->i", points - closest, normals))


def surface_distance(reference: trimesh.Trimesh, points: np.ndarray) -> dict:
    closest, distance, tri = trimesh.proximity.closest_point(reference, points)
    face_sign = np.sign(np.einsum("ij,ij->i", points - closest, reference.face_normals[tri]))
    pseudo = pseudonormal_sign(reference, points, closest, tri)
    return {"distance": distance, "face_sign": face_sign, "pseudo_sign": pseudo, "closest": closest}


def grid_mesh(g: C.Grid) -> trimesh.Trimesh:
    return trimesh.Trimesh(f32(g.nodes()), g.triangles(), process=False)


def wedge_points(apex_z: float, inward: bool) -> np.ndarray:
    """Points whose nearest point on the gable (or notch) is its crest: within the normals' wedge.

    The crest is along y at x = 0; the faces' normals are at ±60° from vertical, so the
    points at angles up to ±55° from vertical, 0.3 m from the crest, all project onto it.
    """
    phi = np.radians(np.linspace(-55, 55, 23))
    ys = np.linspace(-3, 3, 7)
    P, Y = np.meshgrid(phi, ys, indexing="ij")
    s = -1.0 if inward else 1.0
    return np.stack([0.3 * np.sin(P).ravel(), Y.ravel(), apex_z + s * 0.3 * np.cos(P).ravel()], 1)


def main() -> None:
    data: dict[str, np.ndarray] = {}
    summary: dict = {
        "versions": {
            "trimesh": trimesh.__version__,
            "shapely": shapely.__version__,
            "numpy": np.__version__,
        },
    }

    # Tilted planes: the perpendicular distance, not the vertical one.
    measured = C.tilted(0.0)
    reference = grid_mesh(C.tilted(C.TILT_RISE, half=15.0, name="tilted_ref"))
    d = surface_distance(reference, f32(measured.nodes()))
    analytic = -C.TILT_RISE * math.cos(math.radians(30.0))
    summary["tilted"] = {
        "analytic": analytic,
        "trimesh_max_error": float(np.abs(d["distance"] * d["face_sign"] - analytic).max()),
    }

    # A sphere of radius 3 under a level grid 4 m up.
    sphere = trimesh.Trimesh(*C.uv_sphere(3.0, 24, 48), process=False)
    sphere = trimesh.Trimesh(f32(sphere.vertices), sphere.faces, process=False)
    above = C.grid(lambda x, y: 4.0 + 0.0 * x, -6.0, -6.0, 6.0, 6.0, 0.5, "above")
    pts = f32(above.nodes())
    d = surface_distance(sphere, pts)
    data["sphere__distance"] = d["distance"]
    data["sphere__sign"] = d["pseudo_sign"]
    summary["sphere"] = {
        "volume_ok": bool(sphere.volume > 0),
        "analytic_max_error": float(
            np.abs(d["distance"] - (np.linalg.norm(pts, axis=1) - 3)).max()
        ),
        "chord_sagitta": 3.0 * (1 - math.cos(math.pi / 48)),
    }

    # Sharp edges: a gable (convex crest) and a notch (concave), faces 60° steep.
    for key, g, inward in (("gable", C.gable(), False), ("notch", None, True)):
        if g is None:
            gg = C.gable()
            g = C.Grid(gg.x0, gg.y0, gg.h, 3.0 - gg.z, "notch")  # z = |x| tan 60
        apex = 3.0 if not inward else 0.0
        pts = wedge_points(apex, inward)
        d = surface_distance(grid_mesh(g), pts)
        data[f"{key}__points"] = pts
        data[f"{key}__distance"] = d["distance"]
        data[f"{key}__face_sign"] = d["face_sign"]
        data[f"{key}__pseudo_sign"] = d["pseudo_sign"]
        truth = -1.0 if inward else 1.0
        summary[key] = {
            "points": len(pts),
            "true_sign": truth,
            "pseudonormal_wrong": int((d["pseudo_sign"] != truth).sum()),
            "nearest_face_wrong": int((d["face_sign"] != truth).sum()),
            "distance_max_error": float(np.abs(d["distance"] - 0.3).max()),
        }

    # Features in plan (shapely).
    g = C.grid(lambda x, y: C.FEATURE_Z + 0.0 * x, -8.0, -8.0, 8.0, 8.0, 0.5, "plan")
    nodes = f32(g.nodes())
    where = shapely.points(nodes[:, 0], nodes[:, 1])
    features = shapely.GeometryCollection(
        [
            shapely.Polygon(C.FEATURE_POLYGON),
            shapely.LineString(C.FEATURE_LINE),
            *[shapely.Point(p) for p in C.FEATURE_POINTS],
        ]
    )
    plan = shapely.distance(where, features)
    data["feature__plan"] = plan
    summary["feature"] = {"nodes": len(plan), "inside_polygon": int((plan == 0).sum())}

    # Closed volumes: at the origin against the faceted solid's exact volume, and 10 km
    # away (as a site in a projected CRS without an origin shift) against trimesh on the
    # same float32 vertices, which Blender rounds to 1-2 mm there.
    vols = {}
    for name, faceted, smooth in (
        ("prism", 15.0, 15.0),
        ("pyramid", C.frustum_volume(2.0, 0.0, 3.0, 64), math.pi * 4 * 3 / 3),
        ("frustum", C.frustum_volume(2.0, 1.0, 2.0, 64), math.pi * 2 / 3 * (4 + 1 + 2)),
        ("hemisphere", None, 2 / 3 * math.pi * 8),
    ):
        near = C.solid(name)
        far = C.solid(name, C.FAR)
        mesh = trimesh.Trimesh(f32(near[0]), near[1], process=False)
        far_mesh = trimesh.Trimesh(f32(far[0]), far[1], process=False)
        vols[name] = {
            "trimesh": float(mesh.volume),
            "trimesh_far": float(far_mesh.volume),
            "faceted": faceted,
            "smooth": smooth,
            "faces": len(near[1]),
            "watertight": bool(mesh.is_watertight),
            "winding_consistent": bool(mesh.is_winding_consistent),
        }
    summary["closed_volume"] = vols

    # Cut and fill: the raster method (depth at every node x h²) and a fine integral.
    def raster(design, ref, h, x0, y0, x1, y1):
        xs = np.arange(x0, x1 + h / 2, h)
        ys = np.arange(y0, y1 + h / 2, h)
        X, Y = np.meshgrid(xs, ys)
        w = np.ones_like(X)  # node area: h² inside, half on edges, a quarter at corners
        w[0, :] *= 0.5
        w[-1, :] *= 0.5
        w[:, 0] *= 0.5
        w[:, -1] *= 0.5
        dz = (design(X, Y) - ref(X, Y)) * w * h * h
        return float(dz[dz > 0].sum()), float(-dz[dz < 0].sum())

    def flat(x, y):
        return 0.0 * x

    cf = {}
    for h in (0.25, 0.01):
        fill, cut = raster(C.mound_pit, flat, h, -12, -6, 12, 6)
        cf[f"mound_pit_raster_{h:g}"] = {"fill": fill, "cut": cut}
        fill, cut = raster(C.wave, flat, h, -8, -8, 8, 8)
        cf[f"wave_flat_raster_{h:g}"] = {"fill": fill, "cut": cut}
        fill, cut = raster(C.wave, C.wave_plane, h, -8, -8, 8, 8)
        cf[f"wave_plane_raster_{h:g}"] = {"fill": fill, "cut": cut}
    cf["analytic"] = {
        "mound": C.mound_volume(),
        "pit": C.pit_volume(),
        "wave_flat": C.wave_fill(),
        "wave_plane_net": -0.1 * 16 * 16,  # the wave integrates to 0; the plane to 0.1 x area
    }
    summary["cut_fill"] = cf

    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / "distance_volume.npz", **data)
    (OUT / "distance_volume.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
