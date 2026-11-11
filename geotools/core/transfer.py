"""Domain conversions and transfer reductions for the vertex-space bake (numpy only).

The ``bpy`` half is ``geotools.blender.bake_vertex``: it reads the evaluated
mesh and the Geometry Nodes sampling results with ``foreach_get`` and hands the
arrays to the functions here. See docs/design/m3-layers.md, section 2.

Values travel as a pair: a float64 array and a boolean ``valid`` mask. An
element is invalid when it is NaN, inf, equal to the layer's no-data, or when
nothing reached it. Reductions skip invalid elements, so one no-data corner or
evaluated face does not poison the mean. :func:`to_layer_values` turns a pair
into what the layer stores: the layer's numpy type, with the fill value
(``core.layers.fill_value``) where the mask is False.

Two kinds of reduction exist (:func:`reduction_kind`):

- ``"mean"``: weighted mean, for FLOAT layers and for INT layers that do not
  aggregate by MODE (the mean is rounded when stored);
- ``"mode"``: the value with the largest total weight (ties go to the smallest
  value), for BOOLEAN and CATEGORY layers and for INT layers that aggregate by
  MODE. Averaging category indices would invent categories.

Mesh arrays use Blender's names: ``loop_start`` and ``loop_total`` per face,
``corner_vert`` (``loops.vertex_index``) and ``corner_edge``
(``loops.edge_index``) per corner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from .layers import Aggregation, DataType, LayerSpec, effective_aggregation, fill_value, numpy_dtype

__all__ = [
    "PATHS",
    "Kind",
    "MeshArrays",
    "TransferCounts",
    "area_reduce",
    "choose_nearer_hit",
    "corner_to_face",
    "corner_to_face_ids",
    "face_to_corner",
    "gather",
    "merge_interpolated",
    "point_to_corner",
    "reduction_kind",
    "segment_mode",
    "to_corners",
    "to_faces",
    "to_layer_values",
    "valid_mask",
]

Kind = Literal["mean", "mode"]

PATHS = ("copied", "area_mean", "raycast", "nearest", "fallback")
"""How a bake can fill an element, in report order. ``fallback`` is NEAREST
used where RAYCAST missed or AREA_MEAN gave a face nothing."""


def reduction_kind(spec: LayerSpec) -> Kind:
    """``"mode"`` for BOOLEAN and CATEGORY layers and INT layers aggregated by MODE."""
    data_type = DataType(spec.data_type)
    if data_type in (DataType.BOOLEAN, DataType.CATEGORY):
        return "mode"
    if data_type is DataType.INT and effective_aggregation(spec) is Aggregation.MODE:
        return "mode"
    return "mean"


def valid_mask(values: np.ndarray, nodata: float | None = None) -> np.ndarray:
    """True where ``values`` holds data: finite, and not equal to ``nodata``.

    ``nodata`` is compared in the precision of ``values``, so a float32
    attribute matches ``float32(nodata)``. An integer array only matches a
    whole-number ``nodata``; booleans never match.
    """
    values = np.asarray(values)
    if values.dtype.kind == "f":
        keep = np.isfinite(values)
        if nodata is not None and math.isfinite(nodata):
            keep &= values != values.dtype.type(nodata)
        return keep
    keep = np.ones(values.shape, dtype=bool)
    if (
        values.dtype.kind in "iu"
        and nodata is not None
        and math.isfinite(nodata)
        and float(nodata).is_integer()
    ):
        keep &= values != int(nodata)
    return keep


# --- domain conversions ---------------------------------------------------------------------


def point_to_corner(values: np.ndarray, corner_vert: np.ndarray) -> np.ndarray:
    """One value per corner: the value of the corner's vertex."""
    return np.asarray(values)[np.asarray(corner_vert, dtype=np.intp)]


def gather(values: np.ndarray, index: np.ndarray) -> np.ndarray:
    """``values[index]``; also edge to corner (``index`` = ``corner_edge``)."""
    return np.asarray(values)[np.asarray(index, dtype=np.intp)]


def face_to_corner(values: np.ndarray, loop_total: np.ndarray) -> np.ndarray:
    """One value per corner: the value of the corner's face (``np.repeat``)."""
    return np.repeat(np.asarray(values), np.asarray(loop_total, dtype=np.intp))


def corner_to_face_ids(loop_total: np.ndarray) -> np.ndarray:
    """The face index of every corner, assuming corners are stored face by face."""
    totals = np.asarray(loop_total, dtype=np.intp)
    return np.repeat(np.arange(totals.size, dtype=np.intp), totals)


def corner_to_face(
    values: np.ndarray,
    valid: np.ndarray,
    loop_start: np.ndarray,
    loop_total: np.ndarray,
    kind: Kind = "mean",
) -> tuple[np.ndarray, np.ndarray]:
    """Reduce corner values to faces: the mean (or mode) of each face's valid corners.

    The mean uses ``np.add.reduceat`` over ``loop_start``. A face with no valid
    corner is invalid. Returns float64 values and the face mask.
    """
    starts = np.asarray(loop_start, dtype=np.intp)
    totals = np.asarray(loop_total, dtype=np.intp)
    values = np.asarray(values, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    faces = starts.size
    if faces == 0:
        return np.zeros(0, np.float64), np.zeros(0, bool)
    if values.shape != valid.shape or values.shape[0] != int(totals.sum()):
        raise ValueError(
            f"{values.shape[0]} corner values and {valid.shape[0]} flags for faces with "
            f"{int(totals.sum())} corners"
        )
    if (totals <= 0).any():
        raise ValueError("every face needs at least one corner")
    if kind == "mode":
        return segment_mode(corner_to_face_ids(totals), values, valid, faces)
    weights = valid.astype(np.float64)
    sums = np.add.reduceat(np.where(valid, values, 0.0), starts)
    counts = np.add.reduceat(weights, starts)
    face_valid = counts > 0
    out = np.zeros(faces, np.float64)
    np.divide(sums, counts, out=out, where=face_valid)
    return out, face_valid


def segment_mode(
    ids: np.ndarray,
    values: np.ndarray,
    valid: np.ndarray,
    count: int,
    weights: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Per segment ``0..count-1``: the valid value with the largest total weight.

    ``ids[i]`` is the segment of element ``i``. Ties go to the smallest value. A
    segment with no valid element is invalid. Sorting-based; no Python loop.
    """
    ids = np.asarray(ids, dtype=np.intp)
    values = np.asarray(values, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    w = np.ones(ids.shape, np.float64) if weights is None else np.asarray(weights, np.float64)
    keep = valid & (ids >= 0)
    ids, values, w = ids[keep], values[keep], w[keep]
    out = np.zeros(count, np.float64)
    found = np.zeros(count, bool)
    if ids.size == 0:
        return out, found
    order = np.lexsort((values, ids))
    ids, values, w = ids[order], values[order], w[order]
    starts = np.flatnonzero(np.r_[True, (ids[1:] != ids[:-1]) | (values[1:] != values[:-1])])
    run_id, run_value = ids[starts], values[starts]
    run_weight = np.add.reduceat(w, starts)
    # Best run per segment: sort by segment, then weight descending, then value.
    best = np.lexsort((run_value, -run_weight, run_id))
    first = np.r_[True, run_id[best][1:] != run_id[best][:-1]]
    chosen = best[first]
    out[run_id[chosen]] = run_value[chosen]
    found[run_id[chosen]] = True
    return out, found


def _domain_arrays(
    values: np.ndarray, valid: np.ndarray, domain: str, mesh: MeshArrays
) -> tuple[np.ndarray, np.ndarray]:
    """Values of a POINT, EDGE, FACE or CORNER attribute, moved to the corners."""
    if domain == "CORNER":
        return values, valid
    if domain == "POINT":
        return gather(values, mesh.corner_vert), gather(valid, mesh.corner_vert)
    if domain == "EDGE":
        if mesh.corner_edge is None:
            raise ValueError("an EDGE attribute needs corner_edge")
        return gather(values, mesh.corner_edge), gather(valid, mesh.corner_edge)
    if domain == "FACE":
        return face_to_corner(values, mesh.loop_total), face_to_corner(valid, mesh.loop_total)
    raise ValueError(f"cannot transfer a {domain} attribute; use POINT, EDGE, FACE or CORNER")


@dataclass(frozen=True)
class MeshArrays:
    """The topology arrays the conversions need, read once per mesh."""

    loop_start: np.ndarray
    loop_total: np.ndarray
    corner_vert: np.ndarray
    corner_edge: np.ndarray | None = None

    @property
    def faces(self) -> int:
        return int(self.loop_start.size)

    @property
    def corners(self) -> int:
        return int(self.corner_vert.size)


def to_corners(
    values: np.ndarray, valid: np.ndarray, domain: str, mesh: MeshArrays
) -> tuple[np.ndarray, np.ndarray]:
    """An attribute of any mesh domain as one value (and flag) per corner."""
    values, valid = _domain_arrays(
        np.asarray(values, np.float64), np.asarray(valid, bool), domain, mesh
    )
    return np.asarray(values, np.float64), np.asarray(valid, bool)


def to_faces(
    values: np.ndarray, valid: np.ndarray, domain: str, mesh: MeshArrays, kind: Kind = "mean"
) -> tuple[np.ndarray, np.ndarray]:
    """An attribute of any mesh domain as one value (and flag) per face.

    A FACE attribute is copied; the others are moved to the corners and reduced
    with :func:`corner_to_face`.
    """
    if domain == "FACE":
        return np.asarray(values, np.float64), np.asarray(valid, bool)
    corner_values, corner_valid = to_corners(values, valid, domain, mesh)
    return corner_to_face(corner_values, corner_valid, mesh.loop_start, mesh.loop_total, kind)


# --- transfer -----------------------------------------------------------------------------------


def area_reduce(
    index: np.ndarray,
    values: np.ndarray,
    valid: np.ndarray,
    area: np.ndarray,
    count: int,
    kind: Kind = "mean",
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """AREA_MEAN: reduce evaluated faces onto the target faces they are nearest to.

    ``index[i]`` is the target face (``0..count-1``, or negative for none) that
    evaluated face ``i`` belongs to, ``values``/``valid`` its value and ``area``
    its area. The mean is ``bincount(index, value * area) / bincount(index,
    area)`` over valid faces; ``"mode"`` takes the value with the largest total
    area. Where every valid face that reached a target has zero area, the plain
    mean (or mode) of them is used.

    Returns ``(values, valid, received)``: ``received`` is False for targets
    that no evaluated face reached; they need a fallback. A target reached only
    by invalid faces is received but invalid (no-data).
    """
    index = np.asarray(index, dtype=np.intp)
    values = np.asarray(values, dtype=np.float64)
    valid = np.asarray(valid, dtype=bool)
    area = np.asarray(area, dtype=np.float64)
    if not (index.shape == values.shape == valid.shape == area.shape):
        raise ValueError("index, values, valid and area must have the same shape")
    if index.size and int(index.max(initial=-1)) >= count:
        raise ValueError(f"index refers to a face outside 0..{count - 1}")
    inside = index >= 0
    received = np.bincount(index[inside], minlength=count) > 0
    use = valid & inside
    area = np.where(np.isfinite(area) & (area > 0), area, 0.0)
    if kind == "mode":
        out, found = segment_mode(index, values, use, count, area)
        weight = np.bincount(index[use], weights=area[use], minlength=count)
        flat = found & ~(weight > 0)  # only zero-area faces: count them instead
        if flat.any():
            plain, _ = segment_mode(index, values, use, count)
            out[flat] = plain[flat]
        return out, found, received
    ids = index[use]
    v, a = values[use], area[use]
    weight = np.bincount(ids, weights=a, minlength=count)
    total = np.bincount(ids, weights=v * a, minlength=count)
    number = np.bincount(ids, minlength=count)
    plain = np.bincount(ids, weights=v, minlength=count)
    out = np.zeros(count, np.float64)
    weighted = weight > 0
    np.divide(total, weight, out=out, where=weighted)
    flat = ~weighted & (number > 0)
    np.divide(plain, number, out=out, where=flat)
    return out, weighted | flat, received


def choose_nearer_hit(
    hit_a: np.ndarray, dist_a: np.ndarray, hit_b: np.ndarray, dist_b: np.ndarray
) -> np.ndarray:
    """For two rays per sample: 0 if ray A's hit is used, 1 for ray B, -1 for a miss.

    The nearer hit wins; equal distances go to A.
    """
    hit_a = np.asarray(hit_a, bool)
    hit_b = np.asarray(hit_b, bool)
    dist_a = np.asarray(dist_a, np.float64)
    dist_b = np.asarray(dist_b, np.float64)
    choice = np.full(hit_a.shape, -1, np.int8)
    choice[hit_a] = 0
    take_b = hit_b & (~hit_a | (dist_b < dist_a))
    choice[take_b] = 1
    return choice


def merge_interpolated(
    interpolated: np.ndarray | None,
    face_values: np.ndarray,
    face_valid: np.ndarray,
    index: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """The values sampled at evaluated faces ``index`` (negative: nothing found).

    ``interpolated`` is Geometry Nodes' value at the sample point (no-data mapped
    to NaN) or None for ``"mode"`` layers. Where it is finite it is used; else
    the evaluated face's own value (``face_values[index]``), which ignores
    no-data corners. Returns float64 values and the mask.
    """
    index = np.asarray(index, dtype=np.intp)
    found = (index >= 0) & (index < face_values.size)
    safe = np.where(found, index, 0)
    if face_values.size == 0:
        return np.zeros(index.shape, np.float64), np.zeros(index.shape, bool)
    values = np.asarray(face_values, np.float64)[safe]
    valid = np.asarray(face_valid, bool)[safe] & found
    if interpolated is not None:
        interpolated = np.asarray(interpolated, np.float64)
        good = found & np.isfinite(interpolated)
        values = np.where(good, interpolated, values)
        valid = valid | good
    return values, valid


def to_layer_values(values: np.ndarray, valid: np.ndarray, spec: LayerSpec) -> np.ndarray:
    """What the layer stores: its numpy type, rounded for integers, and the fill value
    (``core.layers.fill_value``) where ``valid`` is False.

    BOOLEAN is ``value >= 0.5``; INT and CATEGORY round to the nearest integer
    and clip to int32.
    """
    values = np.asarray(values, np.float64)
    valid = np.asarray(valid, bool) & np.isfinite(values)
    data_type = DataType(spec.data_type)
    dtype = numpy_dtype(data_type)
    fill = fill_value(spec)
    if data_type is DataType.BOOLEAN:
        return np.where(valid, values >= 0.5, bool(fill))
    if dtype.kind == "i":
        info = np.iinfo(np.int32)
        whole = np.clip(np.rint(np.where(valid, values, 0.0)), info.min, info.max)
        return np.where(valid, whole, fill).astype(np.int32)
    return np.where(valid, values, fill).astype(np.float32)


@dataclass
class TransferCounts:
    """How many target elements each path filled, and how many were left as no-data.

    An element a path reached whose value is no-data counts as ``nodata``, not
    under the path.
    """

    unit: str = "faces"  # "faces" or "corners"
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(PATHS, 0))
    nodata: int = 0

    def add(self, path: str, mask: np.ndarray, valid: np.ndarray) -> None:
        """Count the elements in ``mask`` as filled by ``path`` (or no-data)."""
        if path not in self.counts:
            raise ValueError(f"unknown transfer path {path!r}")
        mask = np.asarray(mask, bool)
        valid = np.asarray(valid, bool)
        self.counts[path] += int(np.count_nonzero(mask & valid))
        self.nodata += int(np.count_nonzero(mask & ~valid))

    def merge(self, other: TransferCounts) -> None:
        for path, n in other.counts.items():
            self.counts[path] += n
        self.nodata += other.nodata

    @property
    def total(self) -> int:
        return sum(self.counts.values()) + self.nodata

    def summary(self) -> str:
        """e.g. ``"120 faces: 100 copied, 15 nearest, 5 no-data"``."""
        parts = [f"{n} {path.replace('_', ' ')}" for path, n in self.counts.items() if n]
        parts.append(f"{self.nodata} no-data")
        return f"{self.total} {self.unit}: " + ", ".join(parts)

    def as_dict(self) -> dict[str, int]:
        return {**self.counts, "nodata": self.nodata}
