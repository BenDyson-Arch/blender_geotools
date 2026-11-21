"""Synthetic triangulated surfaces with known geometry, shared by the core tests.

Meshes are ``(positions, face_starts, corner_verts)``, Blender's polygon layout.
Generated, never real site data.
"""

from __future__ import annotations

import numpy as np


def surface(fn, ns: int, nt: int, s_range=(0.0, 1.0), t_range=(0.0, 1.0), *, flip=False):
    """A triangulated parametric surface ``fn(s, t) -> (..., 3)`` on an ns × nt grid:
    ``(positions, face_starts, corner_verts)``. With s along +X and t along +Y the
    normals point to +Z (``flip`` reverses them)."""
    s = np.linspace(*s_range, ns + 1)
    t = np.linspace(*t_range, nt + 1)
    ss, tt = np.meshgrid(s, t, indexing="ij")
    positions = np.asarray(fn(ss, tt), dtype=np.float64).reshape(-1, 3)
    idx = np.arange((ns + 1) * (nt + 1)).reshape(ns + 1, nt + 1)
    a, b, c, d = idx[:-1, :-1], idx[1:, :-1], idx[1:, 1:], idx[:-1, 1:]
    tris = np.concatenate(
        [np.stack([a, b, c], -1).reshape(-1, 3), np.stack([a, c, d], -1).reshape(-1, 3)]
    )
    if flip:
        tris = tris[:, ::-1]
    starts = np.arange(0, 3 * len(tris) + 1, 3, dtype=np.int64)
    return positions, starts, tris.ravel().astype(np.int64)


def plane_xy(z_of=lambda x, y: 0.0 * x):
    return lambda s, t: np.stack([s, t, z_of(s, t)], axis=-1)


def drop_faces(mesh, keep):
    positions, _, corners = mesh
    tris = corners.reshape(-1, 3)[keep]
    starts = np.arange(0, 3 * len(tris) + 1, 3, dtype=np.int64)
    return positions, starts, tris.ravel()


def join(*meshes):
    """Meshes side by side as one (unwelded: their seams are open edges)."""
    pos, starts, corners, offset, corner_offset = [], [np.zeros(1, np.int64)], [], 0, 0
    for p, s, c in meshes:
        pos.append(p)
        corners.append(c + offset)
        starts.append(s[1:] + corner_offset)
        offset += len(p)
        corner_offset += s[-1]
    return np.concatenate(pos), np.concatenate(starts), np.concatenate(corners)
