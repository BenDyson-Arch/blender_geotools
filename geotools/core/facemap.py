"""Face map: rusty-tiles (source_primitive, source_triangle) <-> Blender face.

CONTRACT (M2). Saved next to every build as ``facemap.npz``. rusty-tiles keeps
exact source IDs on every leaf triangle, so this map links output leaves back to
Blender polygons (M7 inspection, M9 annotation).

Numbering (checked against rusty-tiles 0.4.0 by ``tests/core/test_gltf_writer.py``):
the GLB has one node and one mesh, so a leaf's ``source_triangle`` row carries
``node_index = 0``, ``mesh_index = 0``, ``primitive_index`` = the index into
``SceneExport.primitives`` and ``triangle_index`` = the row of that primitive's
``indices``. ``write_glb`` refuses empty primitives, so the two numberings
never drift apart. Face-map row ``primitive_offsets[p] + t`` is triangle ``t``
of primitive ``p``.

The ``.npz`` holds plain arrays only (no pickles): ``version``,
``object_names``, ``primitive_offsets``, ``source_object``, ``source_polygon``,
``topology_names`` and ``topology_digests``.
"""

from __future__ import annotations

import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .gltf_writer import ExportError, PrimitiveData, SceneExport

FACEMAP_VERSION = 1

_TOPOLOGY_TAG = b"geotools-topology-v1\0"
_KEYS = (
    "version",
    "object_names",
    "primitive_offsets",
    "source_object",
    "source_polygon",
    "topology_names",
    "topology_digests",
)


@dataclass(frozen=True)
class FaceMap:
    object_names: tuple[str, ...]
    primitive_offsets: np.ndarray  # (P + 1,) int64; primitive p owns rows [o[p], o[p+1])
    source_object: np.ndarray  # (T,) int32, index into object_names
    source_polygon: np.ndarray  # (T,) int32, Blender polygon index
    topology: dict[str, str]  # object name -> topology digest at export time

    def lookup(self, primitive: int, triangle: int) -> tuple[str, int]:
        """(object name, polygon index) for one rusty-tiles source triangle."""
        objects, polygons = self.lookup_many(np.asarray([primitive]), np.asarray([triangle]))
        return self.object_names[int(objects[0])], int(polygons[0])

    def lookup_many(
        self, primitive: np.ndarray, triangle: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Vectorised lookup: (object index array, polygon index array)."""
        primitive = np.asarray(primitive)
        triangle = np.asarray(triangle)
        if primitive.shape != triangle.shape:
            raise ValueError(
                f"primitive and triangle must have the same shape, got {primitive.shape} "
                f"and {triangle.shape}."
            )
        for name, values in (("primitive", primitive), ("triangle", triangle)):
            if values.size and not np.issubdtype(values.dtype, np.integer):
                raise TypeError(f"{name} must be integers, got {values.dtype}.")
        primitive = primitive.astype(np.int64, copy=False)
        triangle = triangle.astype(np.int64, copy=False)
        count = self.primitive_offsets.shape[0] - 1
        if primitive.size and (primitive.min() < 0 or primitive.max() >= count):
            raise IndexError(f"Primitive index out of range; the face map has {count} primitives.")
        starts = self.primitive_offsets[primitive]
        sizes = self.primitive_offsets[primitive + 1] - starts
        if triangle.size and ((triangle < 0) | (triangle >= sizes)).any():
            raise IndexError("Triangle index out of range for its primitive.")
        rows = starts + triangle
        return self.source_object[rows], self.source_polygon[rows]


def build_face_map(scene: SceneExport, topology: dict[str, str]) -> FaceMap:
    """Rows in primitive order, then ``indices`` row order: exactly rusty-tiles'
    (source_primitive, source_triangle) numbering for a GLB from ``write_glb``.

    Raises ``ExportError`` if a primitive is empty or lacks provenance."""
    if not scene.primitives:
        raise ExportError("The scene has no primitives, so there is no face map.")
    counts = np.zeros(len(scene.primitives) + 1, dtype=np.int64)
    for p, prim in enumerate(scene.primitives):
        counts[p + 1] = _checked_rows(p, prim, len(scene.object_names))
    offsets = np.cumsum(counts)
    source_object = np.empty(int(offsets[-1]), dtype=np.int32)
    source_polygon = np.empty(int(offsets[-1]), dtype=np.int32)
    for p, prim in enumerate(scene.primitives):
        source_object[offsets[p] : offsets[p + 1]] = prim.source_object
        source_polygon[offsets[p] : offsets[p + 1]] = prim.source_polygon
    return FaceMap(
        object_names=tuple(scene.object_names),
        primitive_offsets=offsets,
        source_object=source_object,
        source_polygon=source_polygon,
        topology={str(k): str(v) for k, v in sorted(topology.items())},
    )


def _checked_rows(p: int, prim: PrimitiveData, object_count: int) -> int:
    where = f"Primitive {p}"
    triangles = int(prim.indices.shape[0]) if prim.indices.ndim == 2 else 0
    if triangles == 0:
        raise ExportError(f"{where} has no triangles; empty primitives are refused.")
    if prim.source_object is None or prim.source_polygon is None:
        raise ExportError(f"{where} has no provenance (source_object and source_polygon).")
    for name in ("source_object", "source_polygon"):
        values = getattr(prim, name)
        if values.dtype != np.int32 or values.shape != (triangles,):
            raise ExportError(
                f"{where}: {name} must be int32 with shape ({triangles},), got "
                f"{values.dtype} {values.shape}."
            )
    if prim.source_object.min() < 0 or prim.source_object.max() >= object_count:
        raise ExportError(f"{where}: source_object is out of range.")
    if prim.source_polygon.min() < 0:
        raise ExportError(f"{where}: source_polygon has a negative polygon index.")
    return triangles


def topology_digest(loop_totals: np.ndarray, loop_vertices: np.ndarray, vertex_count: int) -> str:
    """sha256 hex over a mesh's polygon sizes, loop vertex indices and vertex count.

    Detects topology edits that would invalidate a face map; vertex moves don't."""
    totals = _int32_view("loop_totals", loop_totals)
    loops = _int32_view("loop_vertices", loop_vertices)
    vertex_count = int(vertex_count)
    if vertex_count < 0:
        raise ValueError("vertex_count must not be negative.")
    if totals.size and int(totals.min()) < 0:
        raise ValueError("loop_totals must not be negative.")
    if int(totals.sum(dtype=np.int64)) != loops.size:
        raise ValueError(
            f"loop_totals sum to {int(totals.sum(dtype=np.int64))}, but there are "
            f"{loops.size} loop vertices."
        )
    if loops.size and (int(loops.min()) < 0 or int(loops.max()) >= vertex_count):
        raise ValueError("loop_vertices refer to a vertex outside vertex_count.")
    h = hashlib.sha256(_TOPOLOGY_TAG)
    h.update(struct.pack("<QQQ", totals.size, loops.size, vertex_count))
    h.update(memoryview(totals).cast("B"))
    h.update(memoryview(loops).cast("B"))
    return h.hexdigest()


def _int32_view(name: str, values: np.ndarray) -> np.ndarray:
    """``values`` as contiguous little-endian int32, copying only if needed."""
    array = np.asarray(values)
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional, got shape {array.shape}.")
    if array.size and not np.issubdtype(array.dtype, np.integer):
        raise ValueError(f"{name} must be integers, got {array.dtype}.")
    limits = np.iinfo(np.int32)
    if (
        array.size
        and array.dtype != np.dtype("<i4")
        and (int(array.min()) < limits.min or int(array.max()) > limits.max)
    ):
        raise ValueError(f"{name} does not fit in int32.")
    return np.ascontiguousarray(array, dtype="<i4")


def save_face_map(face_map: FaceMap, path: Path) -> None:
    """Write ``face_map`` as a compressed .npz (atomically: temp then rename)."""
    path = Path(path)
    names = sorted(face_map.topology)
    arrays = {
        "version": np.asarray(FACEMAP_VERSION, dtype=np.int64),
        "object_names": np.asarray(face_map.object_names, dtype=np.str_).reshape(-1),
        "primitive_offsets": np.asarray(face_map.primitive_offsets, dtype=np.int64),
        "source_object": np.asarray(face_map.source_object, dtype=np.int32),
        "source_polygon": np.asarray(face_map.source_polygon, dtype=np.int32),
        "topology_names": np.asarray(names, dtype=np.str_).reshape(-1),
        "topology_digests": np.asarray(
            [face_map.topology[n] for n in names], dtype=np.str_
        ).reshape(-1),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            np.savez_compressed(f, **arrays)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes it owner-only
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def load_face_map(path: Path) -> FaceMap:
    """Read a face map written by ``save_face_map``. Raises ``ValueError`` for an
    unknown version or inconsistent contents."""
    with np.load(Path(path), allow_pickle=False) as data:
        if "version" not in data.files:
            raise ValueError(f"{path} is not a GeoTools face map (no version).")
        version = data["version"]
        if version.shape != () or not np.issubdtype(version.dtype, np.integer):
            raise ValueError(f"{path} has a malformed face map version.")
        if int(version) != FACEMAP_VERSION:
            raise ValueError(
                f"{path} is face map version {int(version)}; this GeoTools reads version "
                f"{FACEMAP_VERSION}. Publish again to rebuild it."
            )
        missing = [k for k in _KEYS if k not in data.files]
        if missing:
            raise ValueError(f"{path} lacks {', '.join(missing)}.")
        arrays = {k: data[k] for k in _KEYS}
    offsets = arrays["primitive_offsets"].astype(np.int64, copy=False)
    source_object = arrays["source_object"]
    source_polygon = arrays["source_polygon"]
    names = arrays["object_names"]
    rows = source_object.shape[0] if source_object.ndim == 1 else -1
    if (
        offsets.ndim != 1
        or offsets.size < 2
        or offsets[0] != 0
        or (np.diff(offsets) <= 0).any()
        or source_object.dtype != np.int32
        or source_polygon.dtype != np.int32
        or source_polygon.shape != (rows,)
        or offsets[-1] != rows
        or (rows and (source_object.min() < 0 or source_object.max() >= names.size))
        or arrays["topology_names"].shape != arrays["topology_digests"].shape
    ):
        raise ValueError(f"{path} is an inconsistent face map.")
    return FaceMap(
        object_names=tuple(str(n) for n in names),
        primitive_offsets=offsets,
        source_object=source_object,
        source_polygon=source_polygon,
        topology={
            str(k): str(v)
            for k, v in zip(arrays["topology_names"], arrays["topology_digests"], strict=True)
        },
    )
