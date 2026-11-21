"""Normal orientation: do a mesh's normals point out of the surface, into the air?

Hydrology, sun hours, the sky presets (sky-view factor, openness) and
slope's overhangs read each face's normal as pointing out of the rock. A scan whose
normals point inward, or flip from face to face, gives wrong values without
any error. These checks find that, from the mesh as numpy arrays (Blender's
evaluated mesh through ``foreach_get``), with no loop over faces:

* **Downward share**: the share of the area whose normal points down (below
  :data:`DOWN_NZ`). Most of a terrain faces up; a closed box has a sixth
  facing down; a rock shelter can have half. Above :data:`DOWN_SHARE` the
  normals may point inward, or the surface is mostly overhang.
* **Winding** per connected part (faces linked by shared edges): two faces
  that share an edge must traverse it in opposite directions, or their
  normals point to opposite sides. Parts never cross unwelded seams, so
  each tile of a tiled scan is checked on its own. Blender's Recalculate
  Outside (Mesh > Normals) makes a part consistent.
* **Sky test** (Blender only, it casts rays; :func:`sky_sample` and
  :func:`sky_votes`): a face whose straight-up ray escapes the surface is
  open to the sky, so on a surface seen from the air its normal must point
  up. A sample of faces, area-weighted and not near vertical, votes per
  part; a consistently wound part whose open faces mostly face down
  (:data:`INWARD_SHARE`) points inward, and Flip (or Recalculate Outside,
  on a closed mesh) fixes it. It is a heuristic: a scan made from inside a
  cave, or a tile holding only a ceiling whose rock above was not scanned,
  is read as inward. That is why these are warnings that never stop an
  analysis.

:func:`check` runs the first two; the Blender side
(``geotools.blender.orientation``) reads the meshes, casts the sky rays and
turns :func:`issues` into Checks issues and analysis report notes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from ..core.issues import Issue, Severity

DOWN_NZ = -math.sin(math.radians(1.0))
"""A face faces down when its unit normal's z is below this (1° below horizontal),
so vertical walls and their float noise do not count."""
DOWN_SHARE = 0.5
"""Above this share of the area facing down, warn (unless the sky test cleared it)."""
SKY_NZ = 0.1
"""Faces within about 6° of vertical (``|nz|`` below this) do not vote in the sky test."""
SKY_SAMPLES = 1024
"""Area-weighted draws per object for the sky test; a face drawn twice votes twice."""
SKY_MIN_VOTES = 8
"""A part needs this many votes from faces open to the sky to be judged."""
INWARD_SHARE = 0.75
"""A judged part is inward when this share of its votes face down. Not a bare
majority: under a large overhang whose rock above was not scanned, the
ceiling is open to the sky too."""
CHUNK = 1 << 20
"""Faces per block when computing areas, to bound the temporary arrays."""

INCONSISTENT = "normals-inconsistent"
INWARD = "normals-inward"
DOWN = "normals-down"
CODES = (INCONSISTENT, INWARD, DOWN)

RECALCULATE = "in Edit Mode, select all and use Mesh > Normals > Recalculate Outside"
FLIP = (
    "in Edit Mode, select all and use Mesh > Normals > Flip (Recalculate Outside on a closed mesh)"
)


@dataclass(frozen=True)
class Winding:
    """Connected parts of a mesh and the edges whose two faces disagree."""

    labels: np.ndarray
    """``(F,)`` the part of each face: the smallest face id in it."""
    parts: int
    inconsistent_edges: int
    inconsistent: np.ndarray
    """Labels of the parts with at least one inconsistent edge, sorted."""


@dataclass(frozen=True)
class Orientation:
    """The orientation facts of one mesh (one object)."""

    faces: int
    area: float
    down_area: float
    parts: int
    inconsistent_edges: int
    inconsistent_parts: int
    sky_parts: int = 0
    """Parts with enough sky votes to be judged (0 when the sky test did not run)."""
    inward_parts: int = 0
    """Consistently wound parts whose faces open to the sky mostly face down."""

    @property
    def down_share(self) -> float:
        return self.down_area / self.area if self.area > 0 else 0.0


def area_normals(
    positions: np.ndarray,
    face_starts: np.ndarray,
    corner_verts: np.ndarray,
    *,
    flip: bool = False,
    chunk: int = CHUNK,
) -> tuple[np.ndarray, np.ndarray]:
    """Each face's area and the z of its unit normal: ``(area (F,), nz (F,))``.

    ``positions`` ``(V, 3)`` in world space, ``face_starts`` ``(F + 1,)`` corner
    offsets, ``corner_verts`` ``(C,)``. The vector area is Newell's sum about
    the face's first corner, so a polygon needs no triangulation and large
    coordinates lose no precision. ``flip`` reverses every normal: an object
    whose transform mirrors keeps its normals on the side Blender shows.
    Computed ``chunk`` faces at a time (a loop over blocks, not faces).
    """
    positions = np.asarray(positions, dtype=np.float64)
    face_starts = np.asarray(face_starts, dtype=np.int64)
    corner_verts = np.asarray(corner_verts)
    n_faces = len(face_starts) - 1
    area = np.empty(n_faces, dtype=np.float64)
    nz = np.empty(n_faces, dtype=np.float32)
    for f0 in range(0, n_faces, chunk):
        f1 = min(f0 + chunk, n_faces)
        starts = face_starts[f0 : f1 + 1]
        c0, c1 = int(starts[0]), int(starts[-1])
        sizes = np.diff(starts)
        local = starts[:-1] - c0
        p = positions[corner_verts[c0:c1]]
        if (sizes == 3).all():
            t = p.reshape(-1, 3, 3)
            vec = 0.5 * np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        else:
            first = np.repeat(local, sizes)
            q = p - p[first]
            nxt = np.arange(1, c1 - c0 + 1)
            nxt[local + sizes - 1] = local
            vec = 0.5 * np.add.reduceat(np.cross(q, q[nxt]), local, axis=0)
        a = np.sqrt(np.einsum("ij,ij->i", vec, vec))
        area[f0:f1] = a
        with np.errstate(invalid="ignore", divide="ignore"):
            z = np.where(a > 0, vec[:, 2] / a, 0.0)
        nz[f0:f1] = -z if flip else z
    return area, nz


def components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Connected component label (smallest node id) of each of ``n`` nodes linked by
    ``a[i] - b[i]``: hook each root to the smallest root it links to, then jump
    pointers until every node points at its root; repeat on the links that still
    cross. Keeps the dtype of ``a`` (int32 halves the memory traffic)."""
    dtype = np.asarray(a).dtype
    parent = np.arange(n, dtype=dtype)
    while len(a):
        ra, rb = parent[a], parent[b]
        cross = ra != rb
        if not cross.any():
            break
        a, b, ra, rb = a[cross], b[cross], ra[cross], rb[cross]
        np.minimum.at(parent, np.maximum(ra, rb), np.minimum(ra, rb))
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    return parent


def winding(
    face_starts: np.ndarray, corner_verts: np.ndarray, corner_edges: np.ndarray, n_edges: int
) -> Winding:
    """Parts (faces linked by shared edges) and the edges traversed the same way twice.

    ``corner_edges`` is Blender's ``.corner_edge``: the edge from each corner
    to the next. An edge with exactly two corners, in two different faces, is
    consistent when they start at different vertices. Edges with more than
    two faces link the parts but are not judged.
    """
    face_starts = np.asarray(face_starts, dtype=np.int64)
    corner_verts = np.asarray(corner_verts)
    n_faces = len(face_starts) - 1
    n_corners = len(corner_edges)
    index = np.int32 if max(n_corners, n_faces, n_edges) < 2**31 else np.int64
    corner_edges = np.asarray(corner_edges, dtype=index)
    sizes = np.diff(face_starts)
    triangles = bool((sizes == 3).all())
    corner_face = None if triangles else np.repeat(np.arange(n_faces, dtype=index), sizes)

    def face_of(corners: np.ndarray) -> np.ndarray:
        return corners // 3 if corner_face is None else corner_face[corners]

    # One corner of each edge (the last written); every other corner links to it.
    corners = np.arange(n_corners, dtype=index)
    first = np.full(n_edges, -1, dtype=index)
    first[corner_edges] = corners
    mate = first[corner_edges]
    del first
    others = np.flatnonzero(mate != corners).astype(index)
    del corners
    mate = mate[others]
    face, mate_face = face_of(others), face_of(mate)
    labels = components(n_faces, face, mate_face)

    # Edges with exactly two corners: ``others`` holds one of them, ``mate`` the other.
    count = np.bincount(corner_edges, minlength=n_edges)
    pair = count[corner_edges[others]] == 2
    bad = pair & (face != mate_face) & (corner_verts[others] == corner_verts[mate])
    inconsistent = np.unique(labels[face[bad]])
    return Winding(
        labels=labels,
        parts=int(np.count_nonzero(labels == np.arange(n_faces, dtype=labels.dtype))),
        inconsistent_edges=int(np.count_nonzero(bad)),
        inconsistent=inconsistent,
    )


def check(area: np.ndarray, nz: np.ndarray, wound: Winding) -> Orientation:
    """The orientation of one mesh from :func:`area_normals` and :func:`winding`."""
    area = np.asarray(area, dtype=np.float64)
    return Orientation(
        faces=len(area),
        area=float(area.sum()),
        down_area=float(area[np.asarray(nz) < DOWN_NZ].sum()),
        parts=wound.parts,
        inconsistent_edges=wound.inconsistent_edges,
        inconsistent_parts=len(wound.inconsistent),
    )


def sky_sample(area: np.ndarray, nz: np.ndarray, n: int = SKY_SAMPLES, seed: int = 0):
    """Faces for the sky test: ``(faces, draws)``, the distinct faces and how often each
    was drawn. ``n`` area-weighted draws among faces not near vertical; fixed seed."""
    nz = np.asarray(nz)
    eligible = np.flatnonzero(np.abs(nz) >= SKY_NZ)
    weight = np.asarray(area, dtype=np.float64)[eligible]
    total = weight.sum()
    if not len(eligible) or total <= 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    rng = np.random.default_rng(seed)
    picked = eligible[rng.choice(len(eligible), size=n, p=weight / total)]
    faces, draws = np.unique(picked, return_counts=True)
    return faces.astype(np.int64), draws


def face_corners(face_starts: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The corners of ``faces``, face after face, and each face's corner count."""
    face_starts = np.asarray(face_starts, dtype=np.int64)
    faces = np.asarray(faces, dtype=np.int64)
    sizes = face_starts[faces + 1] - face_starts[faces]
    first = np.cumsum(sizes) - sizes
    corners = np.repeat(face_starts[faces] - first, sizes) + np.arange(int(sizes.sum()))
    return corners, sizes


def centres(
    positions: np.ndarray, face_starts: np.ndarray, corner_verts: np.ndarray, faces: np.ndarray
) -> np.ndarray:
    """``(len(faces), 3)`` the mean corner position of each of ``faces``."""
    corners, sizes = face_corners(face_starts, faces)
    if not len(sizes):
        return np.zeros((0, 3))
    p = np.asarray(positions, dtype=np.float64)[np.asarray(corner_verts)[corners]]
    return np.add.reduceat(p, np.cumsum(sizes) - sizes, axis=0) / sizes[:, None]


def submesh(
    positions: np.ndarray, face_starts: np.ndarray, corner_verts: np.ndarray, faces: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """``faces`` alone as a compact mesh: ``(positions, face_starts, corner_verts)``,
    with only the vertices they use."""
    corners, sizes = face_corners(face_starts, faces)
    used, local = np.unique(np.asarray(corner_verts)[corners], return_inverse=True)
    starts = np.concatenate([[0], np.cumsum(sizes)]).astype(np.int64)
    return np.asarray(positions, dtype=np.float64)[used], starts, local.astype(np.int64)


def sky_origins(points: np.ndarray, nz: np.ndarray, offset: float) -> np.ndarray:
    """Ray origins above face centres ``points``, ``offset`` off each face's plane.

    Straight up by ``offset / |nz|``, so a steep face's origin is as far from
    its plane as a flat one's and its own ray cannot hit it.
    """
    lift = offset / np.maximum(np.abs(np.asarray(nz, dtype=np.float64)), SKY_NZ)
    origins = np.array(points, dtype=np.float64)
    origins[:, 2] += lift
    return origins


def sky_votes(
    found: Orientation,
    labels: np.ndarray,
    inconsistent: np.ndarray,
    draws: np.ndarray,
    nz: np.ndarray,
    open_sky: np.ndarray,
) -> Orientation:
    """``found`` with the sky verdict of the faces :func:`sky_sample` drew.

    Per sampled face: its part (``labels``), how often it was drawn, its
    normal's z, and ``open_sky``, whether its straight-up ray (from
    :func:`sky_origins`) hit nothing. Per part, the draws of open faces facing
    up vote against those facing down; a part with :data:`SKY_MIN_VOTES`
    votes is judged, and inward when :data:`INWARD_SHARE` of them face down.
    Parts in ``inconsistent`` are left to the winding issue.
    """
    open_sky = np.asarray(open_sky, dtype=bool)
    labels = np.asarray(labels)[open_sky]
    draws = np.asarray(draws, dtype=np.float64)[open_sky]
    if not len(labels):
        return replace(found, sky_parts=0, inward_parts=0)
    parts, which = np.unique(labels, return_inverse=True)
    down = np.asarray(nz)[open_sky] < 0
    votes = np.bincount(which, weights=draws, minlength=len(parts))
    against = np.bincount(which, weights=draws * down, minlength=len(parts))
    judged = (votes >= SKY_MIN_VOTES) & ~np.isin(parts, inconsistent)
    inward = judged & (against >= INWARD_SHARE * votes)
    return replace(
        found,
        sky_parts=int(np.count_nonzero(judged)),
        inward_parts=int(np.count_nonzero(inward)),
    )


def _in_parts(k: int, n: int) -> str:
    """ "" for a one-part mesh, else "in 2 of its 5 parts, "."""
    return "" if n == 1 else f"in {k:,} of its {n:,} parts, "


def issues(name: str, found: Orientation, *, sky: bool) -> list[Issue]:
    """Checks issues of one object, subject ``name``; ``sky`` when the sky test ran.

    ``normals-inconsistent`` and ``normals-inward`` are warnings with the
    Blender fix in the message. ``normals-down`` is a warning when nothing
    else explains a mostly downward surface, and a note when the sky test
    judged every part outward (so it is overhang).
    """
    found_issues: list[Issue] = []
    if found.inconsistent_parts:
        found_issues.append(
            Issue(
                Severity.WARNING,
                INCONSISTENT,
                f"{name}: {_in_parts(found.inconsistent_parts, found.parts)}"
                f"{found.inconsistent_edges:,} edges join faces wound in opposite directions, "
                f"so the normals flip from face to face there. To fix it, {RECALCULATE}.",
                name,
            )
        )
    if found.inward_parts:
        found_issues.append(
            Issue(
                Severity.WARNING,
                INWARD,
                f"{name}: {_in_parts(found.inward_parts, found.parts)}the normals point "
                "into the surface (most faces open to the sky face down). To fix it, "
                f"{FLIP}.",
                name,
            )
        )
    if found.down_share > DOWN_SHARE and not (found.inward_parts or found.inconsistent_parts):
        share = f"{found.down_share:.0%}"
        if sky and found.sky_parts:
            found_issues.append(
                Issue(
                    Severity.INFO,
                    DOWN,
                    f"{name}: {share} of the area faces down, but the faces open to the "
                    "sky face up, so it is overhang.",
                    name,
                )
            )
        else:
            found_issues.append(
                Issue(
                    Severity.WARNING,
                    DOWN,
                    f"{name}: {share} of the area faces down. Unless it is mostly overhang, "
                    f"the normals point inward: to fix it, {FLIP}.",
                    name,
                )
            )
    return found_issues
