"""Normal orientation in Checks and before the analyses that read normals.

The maths is bpy-free (``geotools.analysis.orientation``); this module reads
each object's evaluated mesh (``foreach_get``) and casts the sky test's rays.

* :func:`check_issues` (Run Checks, and so Publish): every source mesh, with
  the sky test, as ``normals-*`` issues.
* :func:`analysis_notes` (Hydrology, Sun Hours, and the sky presets: Sky-View
  Factor and Openness): the analysis's meshes, without rays, as report
  warnings. They never stop the analysis.

Neither runs in a panel's ``draw``: the Checks panel shows stored issues.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import bpy
import numpy as np

from ..analysis import hydrology as hy
from ..analysis import orientation as orient
from ..analysis.sun_hours import ray_offset
from ..core.issues import Issue, Severity
from .layers import apply_modifiers, layer_meshes, source_objects
from .surface import cast_vertical

log = logging.getLogger(__name__)


@dataclass
class _Mesh:
    positions: np.ndarray
    face_starts: np.ndarray
    corner_verts: np.ndarray
    corner_edges: np.ndarray
    n_edges: int
    mirrored: bool


def _get(collection, name: str, field: str, dtype, size: int) -> np.ndarray:
    values = np.empty(size, dtype)
    if size:
        collection[name].data.foreach_get(field, values)
    return values


def read_mesh(obj: bpy.types.Object, mesh: bpy.types.Mesh, *, edges: bool = True) -> _Mesh:
    """One evaluated mesh as world-space arrays (``foreach_get``)."""
    n_v, n_f, n_l = len(mesh.vertices), len(mesh.polygons), len(mesh.loops)
    co = _get(mesh.attributes, "position", "vector", np.float32, 3 * n_v).reshape(-1, 3)
    matrix = np.array(obj.matrix_world, dtype=np.float64)
    loop_start = np.empty(n_f, np.int32)
    mesh.polygons.foreach_get("loop_start", loop_start)
    return _Mesh(
        positions=co.astype(np.float64) @ matrix[:3, :3].T + matrix[:3, 3],
        face_starts=np.append(loop_start.astype(np.int64), n_l),
        corner_verts=_get(mesh.attributes, ".corner_vert", "value", np.int32, n_l),
        corner_edges=(
            _get(mesh.attributes, ".corner_edge", "value", np.int32, n_l)
            if edges
            else np.zeros(0, np.int32)
        ),
        n_edges=len(mesh.edges),
        mirrored=bool(np.linalg.det(matrix[:3, :3]) < 0),
    )


@dataclass
class _Sample:
    """One object's sky-test faces, kept until the rays are cast."""

    index: int
    faces: np.ndarray
    labels: np.ndarray
    inconsistent: np.ndarray
    draws: np.ndarray
    nz: np.ndarray
    centres: np.ndarray


def _meshes(context: bpy.types.Context, objects: list[bpy.types.Object]):
    """Each object with faces, not in Edit Mode, and its evaluated mesh."""
    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    for obj, mesh in layer_meshes(
        scene, objects, apply=apply_modifiers(scene), depsgraph=depsgraph
    ):
        if obj.data.is_editmode or not len(mesh.polygons):
            continue
        yield obj, mesh


def orientations(
    context: bpy.types.Context, objects: list[bpy.types.Object], *, sky: bool
) -> list[tuple[str, orient.Orientation]]:
    """The orientation of each of ``objects`` (skipping those in Edit Mode).

    ``sky`` adds the sky test: up to ``SKY_SAMPLES`` faces per object, all
    cast in one Geometry Nodes Raycast (``surface.cast_vertical``) against
    the faces of every object that could lie above them.
    """
    began = time.perf_counter()
    found: list[tuple[str, orient.Orientation]] = []
    samples: list[_Sample] = []
    for obj, mesh in _meshes(context, objects):
        arrays = read_mesh(obj, mesh)
        area, nz = orient.area_normals(
            arrays.positions, arrays.face_starts, arrays.corner_verts, flip=arrays.mirrored
        )
        wound = orient.winding(
            arrays.face_starts, arrays.corner_verts, arrays.corner_edges, arrays.n_edges
        )
        found.append((obj.name, orient.check(area, nz, wound)))
        if sky:
            faces, draws = orient.sky_sample(area, nz)
            samples.append(
                _Sample(
                    index=len(found) - 1,
                    faces=faces,
                    labels=wound.labels[faces],
                    inconsistent=wound.inconsistent,
                    draws=draws,
                    nz=nz[faces],
                    centres=orient.centres(
                        arrays.positions, arrays.face_starts, arrays.corner_verts, faces
                    ),
                )
            )
    if samples and any(len(s.faces) for s in samples):
        open_sky = _open_sky(context, objects, samples)
        start = 0
        for s in samples:
            name, result = found[s.index]
            stop = start + len(s.faces)
            found[s.index] = (
                name,
                orient.sky_votes(
                    result, s.labels, s.inconsistent, s.draws, s.nz, open_sky[start:stop]
                ),
            )
            start = stop
    log.debug(
        "Normal orientation of %d objects (%s faces, sky %s): %.2f s",
        len(found),
        f"{sum(o.faces for _, o in found):,}",
        sky,
        time.perf_counter() - began,
    )
    return found


def _open_sky(
    context: bpy.types.Context, objects: list[bpy.types.Object], samples: list[_Sample]
) -> np.ndarray:
    """Whether the straight-up ray from each sampled face hits nothing, in sample order."""
    centres = np.concatenate([s.centres for s in samples])
    nz = np.concatenate([s.nz for s in samples])
    origins = orient.sky_origins(centres, nz, ray_offset(centres))
    # Every object's faces whose plan box covers an origin: a second read, since
    # the origins of all objects are known only now.
    positions, starts, corners = [], [], []
    v_base = c_base = 0
    for obj, mesh in _meshes(context, objects):
        arrays = read_mesh(obj, mesh, edges=False)
        lo, hi = hy.face_bounds(arrays.positions, arrays.face_starts, arrays.corner_verts)
        under = hy.faces_under(origins, lo, hi)
        if not len(under):
            continue
        p, s, c = orient.submesh(arrays.positions, arrays.face_starts, arrays.corner_verts, under)
        positions.append(p)
        starts.append(s[:-1] + c_base)
        corners.append(c + v_base)
        v_base += len(p)
        c_base += len(c)
    if not positions:
        return np.ones(len(origins), dtype=bool)
    face_starts = np.append(np.concatenate(starts), c_base)
    _, _, up_face, _ = cast_vertical(
        context,
        np.concatenate(positions),
        face_starts,
        np.concatenate(corners),
        np.arange(len(face_starts) - 1),
        origins,
    )
    return up_face < 0


_FAILURES = (RuntimeError, ValueError, KeyError, IndexError, MemoryError)
"""What a failed read or ray cast raises; logged, never passed on: these checks
only warn, so they must not stop Run Checks, Publish or an analysis."""


def check_issues(context: bpy.types.Context) -> list[Issue]:
    """The ``normals-*`` issues of the source collection's meshes, with the sky test."""
    objects = source_objects(context.scene)
    if not objects:
        return []
    try:
        found = orientations(context, objects, sky=True)
    except _FAILURES:
        log.exception("Normal orientation check failed")
        return []
    return [issue for name, result in found for issue in orient.issues(name, result, sky=True)]


def analysis_notes(context: bpy.types.Context, objects: list[bpy.types.Object]) -> list[str]:
    """The warnings about ``objects``' normals for an analysis's report, without rays.

    Run at the start of Hydrology, Sun Hours and the sky presets. A failure to
    read a mesh is logged and gives no note: the check never stops an analysis.
    """
    try:
        found = orientations(context, objects, sky=False)
    except _FAILURES:
        log.exception("Normal orientation check failed")
        return []
    return [
        issue.message
        for name, result in found
        for issue in orient.issues(name, result, sky=False)
        if issue.severity is Severity.WARNING
    ]
