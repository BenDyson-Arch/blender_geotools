"""Synthetic surfaces and comparison measures of the hydrology, contour and section validation.

Shared by ``scripts/validation/hydrology/make_references.py`` (which runs the
reference tools), ``tests/core/test_validation_hydrology.py`` and
``tests/blender/test_validation_hydrology.py``; see
docs/validation/hydrology.md. numpy only: it never imports GeoTools, so the
Blender suite can use it next to the installed extension.

A DEM here is ``z[i, j]`` at ``(x[i], y[j])``, x east and y north, on cell
centres; GeoTools sees it as a mesh with a vertex at every cell centre and two
triangles per square of four (:func:`grid_mesh`).
"""

from __future__ import annotations

import math

import numpy as np

# --- the surfaces ----------------------------------------------------------------------------


def axes(x0: float, x1: float, y0: float, y1: float, h: float) -> tuple[np.ndarray, np.ndarray]:
    nx = round((x1 - x0) / h) + 1
    ny = round((y1 - y0) / h) + 1
    return x0 + h * np.arange(nx), y0 + h * np.arange(ny)


def dem(height, x0, x1, y0, y1, h) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Heights at the cell centres, and the axes."""
    x, y = axes(x0, x1, y0, y1, h)
    xx, yy = np.meshgrid(x, y, indexing="ij")
    return np.asarray(height(xx, yy), dtype=np.float64), x, y


def grid_mesh(z: np.ndarray, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vertex positions ``(nx * ny, 3)`` (vertex ``i * ny + j``) and triangles: each square
    split along its south-west to north-east diagonal."""
    nx, ny = z.shape
    xx, yy = np.meshgrid(x, y, indexing="ij")
    pts = np.stack([xx.ravel(), yy.ravel(), z.ravel()], axis=1)
    i, j = np.meshgrid(np.arange(nx - 1), np.arange(ny - 1), indexing="ij")
    v0 = (i * ny + j).ravel()
    v1 = v0 + ny
    tris = np.concatenate([np.stack([v0, v1, v1 + 1], 1), np.stack([v0, v1 + 1, v0 + 1], 1)])
    return pts, tris


def two_bowls(x, y):
    """Two paraboloid pits: the shallow east one (bottom -0.3) spills over the saddle into
    the deep west one (bottom -1.0), which spills over the west edge."""
    return np.minimum(0.5 * ((x + 1.5) ** 2 + y**2) - 1.0, 0.5 * ((x - 1.2) ** 2 + y**2) - 0.3)


TWO_BOWLS_SADDLE_X = (1.4 - 0.81) / 5.4
"""Where the two paraboloids meet on y = 0: (x + 1.5)² - (x - 1.2)² = 1.4."""
TWO_BOWLS_SADDLE_Z = 0.5 * (TWO_BOWLS_SADDLE_X + 1.5) ** 2 - 1.0
TWO_BOWLS_WEST_SPILL = -0.5  # the west edge x = -2.5 on y = 0


def two_bowls_volumes() -> tuple[float, float]:
    """Water of each bowl filled to its spill level, closed form: a paraboloid
    0.5 r² holds pi d² below depth d. Both lakes are discs that just touch the
    saddle (east) or the west edge (west), so nothing is cut off."""
    east = TWO_BOWLS_SADDLE_Z - (-0.3)
    west = TWO_BOWLS_WEST_SPILL - (-1.0)
    return math.pi * east**2, math.pi * west**2


C30, S30 = math.cos(math.radians(30)), math.sin(math.radians(30))


def plane(x, y):
    return -0.1 * x + 0 * y


def plane30(x, y):
    """Falls at 0.1 towards 30° north of east (across the triangulation)."""
    return -0.1 * (C30 * x + S30 * y)


def cone(x, y):
    return 32.0 - np.hypot(x - 32.0, y - 32.0)


def inverted_cone(x, y):
    return np.hypot(x, y)


def valley(x, y):
    """V-shaped valley along y = 0, sides 0.3, falling 0.05 towards x = 0."""
    return 0.3 * np.abs(y) + 0.05 * x


def terrain(x, y):
    """Seeded hills and pits on a gentle slope: many depressions of every size."""
    rng = np.random.default_rng(20261114)
    z = 0.02 * x + 0.5 * np.sin(x / 7.0) * np.cos(y / 5.0)
    for _ in range(40):
        cx, cy = rng.uniform(0, 95, 2)
        r, d = rng.uniform(2, 8), rng.uniform(-1.5, 1.0)
        z = z + d * np.exp(-((x - cx) ** 2 + (y - cy) ** 2) / r**2)
    return z + rng.normal(0.0, 0.02, x.shape)


HYDRO = {
    # name: (height, x0, x1, y0, y1, cell)
    "plane": (plane, 0, 64, 0, 32, 1.0),
    "plane30": (plane30, 0, 64, 0, 64, 1.0),
    "cone": (cone, 0, 64, 0, 64, 1.0),
    "bowl": (inverted_cone, -32, 32, -32, 32, 1.0),
    "two_bowls": (two_bowls, -2.5, 2.5, -2.0, 2.0, 0.05),
    "valley": (valley, 0, 80, -20, 20, 1.0),
    "terrain": (terrain, 0, 95, 0, 95, 1.0),
}


def monkey(x, y):
    return x**3 - 3 * x * y**2


def contour_plane(x, y):
    return x + 0 * y


def contour_cone(x, y):
    return 10.0 - np.hypot(x, y)


CONTOURS = {
    # name: (height, x0, x1, y0, y1, cell, interval, base)
    "plane": (contour_plane, 0, 20, 0, 10, 1.0, 1.0, 0.0),
    "cone": (contour_cone, -10, 10, -10, 10, 0.25, 1.0, 0.25),
    "monkey": (monkey, -1, 1, -1, 1, 0.05, 0.1, 0.0),
    "terrain": (terrain, 0, 95, 0, 95, 1.0, 0.25, 0.0),
}


def paraboloid(x, y):
    return 0.05 * (x**2 + y**2)


SECTIONS = {
    # name: (height, x0, x1, y0, y1, cell, path)
    "paraboloid": (paraboloid, -10, 10, -10, 10, 0.5, [(-9.3, -2.1), (2.2, 3.7), (8.9, -6.4)]),
    "cone": (contour_cone, -10, 10, -10, 10, 0.5, [(-9.1, 0.7), (9.2, 0.7)]),
    "terrain": (terrain, 0, 95, 0, 95, 1.0, [(3.3, 5.1), (90.2, 40.7), (20.5, 91.6)]),
}


def hills(x, y):
    """Two Gaussian hills on a plane falling east: no pits, curved lines of steepest descent."""
    return (
        np.exp(-((x - 1.0) ** 2 + y**2) / 2.0)
        + 0.6 * np.exp(-((x + 1.5) ** 2 + (y - 1.0) ** 2) / 1.5)
        - 0.05 * x
    )


def hills_gradient(x, y):
    g1 = np.exp(-((x - 1.0) ** 2 + y**2) / 2.0)
    g2 = 0.6 * np.exp(-((x + 1.5) ** 2 + (y - 1.0) ** 2) / 1.5)
    gx = -g1 * (x - 1.0) - g2 * 2 * (x + 1.5) / 1.5 - 0.05
    gy = -g1 * y - g2 * 2 * (y - 1.0) / 1.5
    return gx, gy


HILLS_HALF = 4.0  # the hills mesh is [-4, 4]²
HILLS_SEEDS = ((1.4, 0.6), (0.2, -0.8), (-1.2, 1.6), (-2.0, 0.4), (0.9, 1.5))


# --- measures --------------------------------------------------------------------------------


def stats(e) -> dict | None:
    e = np.abs(np.asarray(e, dtype=np.float64))
    if not e.size:
        return None
    return {
        "n": int(e.size),
        "max": float(e.max()),
        "median": float(np.median(e)),
        "rms": float(np.sqrt((e**2).mean())),
    }


def vertex_max(values: np.ndarray, tris: np.ndarray, n: int) -> np.ndarray:
    """The largest of the values of the faces round each vertex."""
    out = np.full(n, -np.inf)
    for k in range(3):
        np.maximum.at(out, tris[:, k], values)
    return out


def point_segment_distance(p, a, b, chunk: int = 1024) -> np.ndarray:
    """Plan distance from each point to the nearest of the segments ``a``-``b``."""
    p, a, b = p[:, :2], a[:, :2], b[:, :2]
    d = b - a
    dd = np.maximum((d * d).sum(1), 1e-300)
    best = np.full(len(p), np.inf)
    for s in range(0, len(p), chunk):
        q = p[s : s + chunk, None, :]
        t = np.clip(((q - a) * d).sum(2) / dd, 0, 1)
        best[s : s + chunk] = np.linalg.norm(q - (a + t[..., None] * d), axis=2).min(1)
    return best


def ponded_vertex_levels(z, tris, filled, depth) -> np.ndarray:
    """GeoTools' water level at each vertex: the highest filled level of a ponded face round
    it, or the vertex's own height where none is ponded (the raster tools' filled DEM)."""
    level = vertex_max(np.where(depth > 0, filled, -np.inf), tris, z.size)
    return np.maximum(level.reshape(z.shape), z)


def lake_comparison(z, x, y, centroids, depth, filled, labels, raster_depth, h):
    """Each GeoTools depression against the raster lake under its deepest face:
    ``(level difference, raster lake volume, label)``, the difference NaN where that cell
    is dry in the raster. ``labels`` are GeoTools' depression labels per face (0 none);
    raster lakes are 8-connected ponded cells."""
    nx, ny = z.shape
    lake = _label8(raster_depth > 0)
    rows = []
    for k in range(1, int(labels.max(initial=0)) + 1):
        faces = np.flatnonzero(labels == k)
        if not len(faces):
            continue
        deepest = faces[np.argmax(depth[faces])]
        i = int(np.clip(round((centroids[deepest, 0] - x[0]) / h), 0, nx - 1))
        j = int(np.clip(round((centroids[deepest, 1] - y[0]) / h), 0, ny - 1))
        if lake[i, j] == 0:
            rows.append((np.nan, 0.0, k))
            continue
        volume = float(raster_depth[lake == lake[i, j]].sum() * h * h)
        rows.append((float(filled[deepest] - (z + raster_depth)[i, j]), volume, k))
    return rows


def neighbourhood_max(face_values, tris, shape) -> np.ndarray:
    """At each vertex, the largest value of the faces touching it or any of its 8 neighbours:
    a channel one cell to the side still counts."""
    v = vertex_max(face_values, tris, shape[0] * shape[1]).reshape(shape)
    p = np.pad(v, 1, constant_values=-np.inf)
    out = v.copy()
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            out = np.maximum(out, p[1 + di : 1 + di + shape[0], 1 + dj : 1 + dj + shape[1]])
    return out


def hydrology_measures(
    z, x, y, tris, centroids, plan_areas, result, wbt_depth, richdem_depth, wbt_acc
) -> dict:
    """GeoTools' filling and accumulation against priority-flood rasters and D8/D-inf.

    ``result`` is GeoTools' hydrology result; ``*_depth`` the reference fills minus the
    DEM; ``wbt_acc`` a dict of ``d8`` and ``dinf`` accumulations in cells.
    """
    h = float(x[1] - x[0])
    area = h * h
    out = {
        "faces": len(centroids),
        "geotools_volume": float((result.depth * plan_areas).sum()),
        "geotools_depressions_1cm": int(result.depressions.count),
    }
    for label, depth in (("wbt", wbt_depth), ("richdem", richdem_depth)):
        out[f"{label}_volume"] = float(depth.sum() * area)
        lakes = _label8(depth > 0)
        deep = np.zeros(lakes.max() + 1)
        np.maximum.at(deep, lakes.ravel(), depth.ravel())
        out[f"{label}_lakes_1cm"] = int((deep[1:] >= 0.01).sum())
    level = ponded_vertex_levels(z, tris, result.filled, result.depth)
    wet = (level > z) | (richdem_depth > 0)
    out["vertex_level_vs_richdem"] = stats((level - (z + richdem_depth))[wet])
    rows = lake_comparison(
        z, x, y, centroids, result.depth, result.filled, result.depression, richdem_depth, h
    )
    if rows:
        diff = np.array([r[0] for r in rows])
        matched = np.isfinite(diff)
        out["lakes_matched"] = int(matched.sum())
        out["lakes_unmatched"] = int((~matched).sum())
        out["lake_level_vs_richdem"] = stats(diff[matched])
        ratio = [
            result.depressions.volume[k - 1] / v for d, v, k in rows if np.isfinite(d) and v > 0
        ]
        out["lake_volume_ratio"] = [round(float(r), 4) for r in ratio]
    gt = neighbourhood_max(result.accumulation, tris, z.shape)
    dry = (level <= z) & (richdem_depth <= 0)
    dry[[0, -1], :] = False
    dry[:, [0, -1]] = False
    for kind, cells in wbt_acc.items():
        channel = dry & (cells >= 100)
        if channel.any():
            ratio = gt[channel] / (cells[channel] * area)
            out[f"channel_ratio_wbt_{kind}"] = {
                "cells": int(channel.sum()),
                "p10": round(float(np.percentile(ratio, 10)), 4),
                "median": round(float(np.median(ratio)), 4),
                "p90": round(float(np.percentile(ratio, 90)), 4),
            }
    return out


def contour_measures(lines, gp, gline, gelev, x, y):
    """Plan distances: GDAL vertices to GeoTools segments of their level; GeoTools vertices on
    the grid's edges and on its diagonals to GDAL segments of theirs."""
    a = lines.positions[lines.edges[:, 0]]
    b = lines.positions[lines.edges[:, 1]]
    seg_level = lines.line_values["elevation"][lines.edge_line]
    point_level = gelev[gline]
    inside = (gp[:, 0] >= x[0]) & (gp[:, 0] <= x[-1]) & (gp[:, 1] >= y[0]) & (gp[:, 1] <= y[-1])
    gdal_to_gt = np.full(len(gp), np.inf)
    for level in np.unique(point_level):
        on = np.isclose(point_level, level, rtol=0, atol=1e-9) & inside
        mine = np.isclose(seg_level, level, rtol=0, atol=1e-9)
        if mine.any() and on.any():
            gdal_to_gt[on] = point_segment_distance(gp[on], a[mine], b[mine])
    h = float(x[1] - x[0])
    fx = (lines.positions[:, 0] - x[0]) / h
    fy = (lines.positions[:, 1] - y[0]) / h
    on_edge = (np.abs(fx - np.round(fx)) < 1e-9) | (np.abs(fy - np.round(fy)) < 1e-9)
    same = gline[:-1] == gline[1:]
    ga, gb = gp[:-1][same], gp[1:][same]
    g_level = point_level[:-1][same]
    gt_level = np.empty(len(lines.positions))
    gt_level[lines.edges.ravel()] = np.repeat(seg_level, 2)
    gt_to_gdal = np.full(len(lines.positions), np.inf)
    for level in np.unique(gt_level):
        on = np.isclose(gt_level, level, rtol=0, atol=1e-9)
        theirs = np.isclose(g_level, level, rtol=0, atol=1e-9)
        if theirs.any():
            gt_to_gdal[on] = point_segment_distance(lines.positions[on], ga[theirs], gb[theirs])
    matched = np.isfinite(gdal_to_gt)
    return {
        "gdal_vertex_to_geotools": gdal_to_gt[matched],
        "gdal_vertices_without_geotools_level": int((inside & ~matched).sum()),
        "geotools_edge_vertex_to_gdal": gt_to_gdal[on_edge & np.isfinite(gt_to_gdal)],
        "geotools_diagonal_vertex_to_gdal": gt_to_gdal[~on_edge & np.isfinite(gt_to_gdal)],
        "geotools_vertices_without_gdal_level": int((~np.isfinite(gt_to_gdal)).sum()),
    }


def _label8(mask: np.ndarray) -> np.ndarray:
    """8-connected component labels (1, 2 ...) of ``mask``, 0 outside: hooking and jumping."""
    nx, ny = mask.shape
    i, j = np.nonzero(mask)
    pairs = []
    for di, dj in ((1, 0), (0, 1), (1, 1), (1, -1)):
        ti, tj = i + di, j + dj
        ok = (ti < nx) & (tj >= 0) & (tj < ny)
        ok[ok] = mask[ti[ok], tj[ok]]
        pairs.append(np.stack([i[ok] * ny + j[ok], ti[ok] * ny + tj[ok]], 1))
    e = np.concatenate(pairs)
    parent = np.arange(mask.size)
    while len(e):
        ra, rb = parent[e[:, 0]], parent[e[:, 1]]
        differ = ra != rb
        if not differ.any():
            break
        np.minimum.at(parent, np.maximum(ra, rb)[differ], np.minimum(ra, rb)[differ])
        while not np.array_equal(parent[parent], parent):
            parent = parent[parent]
    root = parent.reshape(mask.shape)
    out = np.zeros(mask.shape, np.int64)
    out[mask] = np.searchsorted(np.unique(root[mask]), root[mask]) + 1
    return out
