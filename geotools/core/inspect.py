"""Output inspection (M7), bpy-free: the build record, tracing and quality numbers.

* :class:`OutputRecord`: an archive and what its build recorded beside it
  (``build.json``, ``facemap.npz``, ``source.glb``; see ``publish.staging``).
* :func:`assemble`: a decoded tile as flat arrays ready for ``foreach_set``,
  placed in the scene by a ``core.output_frame.SceneFrame``.
* Tracing, both ways, through the face map (``core.facemap``):
  :func:`source_faces` (output faces -> source polygons) and
  :func:`tile_face_mask` with :func:`selected_rows` (source polygons -> output
  faces). Leaves carry exact (source_primitive, source_triangle) pairs; a coarse
  face carries only a representative source primitive, so it traces to that
  whole primitive.
* :func:`topology_problems`: why a face map no longer fits the scene.
* :func:`texel_density`: texels per square metre per face.

Nothing here loops over faces or vertices in Python.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .facemap import FaceMap, load_face_map
from .output_frame import SceneFrame
from .output_tiles import DecodedTile

EVALUATED_PREFIX = "evaluated:"
"""A face-map topology entry for an object exported with topology-changing modifiers."""


class InspectError(ValueError):
    """The output cannot be inspected or traced as asked; the message says why."""


# --- the build record ------------------------------------------------------------------------


@dataclass(frozen=True)
class OutputRecord:
    """A published archive and the files its build left beside it."""

    archive: Path
    record: Mapping[str, Any] | None  # build.json, or None when there is none
    staging: Path | None

    @classmethod
    def load(cls, archive: Path) -> OutputRecord:
        """The archive's build record, if any. Raises InspectError for a missing
        archive or an unreadable build.json."""
        from ..publish import staging as st

        archive = Path(archive)
        if not archive.exists():
            raise InspectError(f"{archive} does not exist")
        if archive.is_dir() or archive.suffix != ".3tz":
            return cls(archive, None, None)
        staging = st.staging_dir_for(archive)
        path = staging / st.RECORD_NAME
        if not path.is_file():
            return cls(archive, None, staging)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InspectError(f"Cannot read {path}: {exc}") from exc
        if not isinstance(record, Mapping):
            raise InspectError(f"{path} is not a GeoTools build record")
        if record.get("output_type", "3tz") != "3tz":
            raise InspectError(f"{path} records a {record.get('output_type')} build, not a .3tz")
        return cls(archive, record, staging)

    @property
    def placement_record(self) -> Mapping[str, Any] | None:
        if self.record is None:
            return None
        value = self.record.get("placement")
        return value if isinstance(value, Mapping) else None

    def _file(self, key: str, default: str) -> Path | None:
        if self.staging is None or self.record is None:
            return None
        name = self.record.get(key, default)
        if isinstance(name, Mapping):
            name = name.get("path", default)
        path = self.staging / str(name)
        return path if path.is_file() else None

    @property
    def face_map_path(self) -> Path | None:
        return self._file("face_map", "facemap.npz")

    @property
    def source_glb_path(self) -> Path | None:
        return self._file("source_glb", "source.glb")

    def face_map(self) -> FaceMap:
        path = self.face_map_path
        if path is None:
            raise InspectError(
                f"{self.archive.name} has no face map beside it (publish it from GeoTools to "
                "trace its faces)"
            )
        try:
            return load_face_map(path)
        except (OSError, ValueError) as exc:
            raise InspectError(f"Cannot read the face map {path}: {exc}") from exc

    @property
    def source_collection(self) -> str | None:
        geotools = (self.record or {}).get("geotools")
        name = geotools.get("source_collection") if isinstance(geotools, Mapping) else None
        return str(name) if name else None

    @property
    def profile(self) -> str | None:
        settings = (self.record or {}).get("settings")
        value = settings.get("profile") if isinstance(settings, Mapping) else None
        return str(value) if value else None

    @property
    def leaf_error(self) -> float | None:
        """rusty-tiles' maximum leaf geometry error (the position quantisation), metres."""
        summary = (self.record or {}).get("report_summary")
        value = summary.get("maximum_leaf_geometry_error") if isinstance(summary, Mapping) else None
        return float(value) if isinstance(value, int | float) else None


def placement_differences(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Which ``mesh_to_tileset`` placement keywords differ (recorded vs the scene's now).

    Numbers are compared exactly: they are the binary64 values typed into the
    project, and any change moves the source on Earth."""
    keys = ("anchor", "orientation_xyzw", "scene_offset", "source_crs", "source_offset")
    keys += ("height_offset", "source_axes")
    changed = []
    for key in keys:
        a, b = recorded.get(key), current.get(key)
        if isinstance(a, list | tuple) and isinstance(b, list | tuple):
            same = len(a) == len(b) and all(float(x) == float(y) for x, y in zip(a, b, strict=True))
        elif isinstance(a, int | float) and isinstance(b, int | float):
            same = float(a) == float(b)
        else:
            same = a == b
        if not same:
            changed.append(key.replace("_", " "))
    return changed


# --- a decoded tile as mesh arrays -----------------------------------------------------------


@dataclass(frozen=True)
class TileMesh:
    """A decoded tile as flat mesh arrays (one triangle per face), in the scene."""

    positions: np.ndarray  # (V, 3) float64, Blender world
    corner_verts: np.ndarray  # (3F,) int32
    material: np.ndarray  # (F,) int32, index into ``materials`` (-1: none)
    materials: tuple[int | None, ...]  # glTF material index of each mesh slot
    source_primitive: np.ndarray  # (F,) int32, NO_ID where unknown
    source_triangle: np.ndarray | None  # (F,) int32; None on coarse tiles
    uvs: np.ndarray | None  # (3F, 2) float32 TEXCOORD_0, Blender convention (v up)
    colors: np.ndarray | None  # (V, 4) float32, linear
    normals: np.ndarray | None  # (V, 3) float32

    @property
    def faces(self) -> int:
        return int(self.material.shape[0])


def _int32(values: np.ndarray, what: str) -> np.ndarray:
    if values.size and int(values.max()) > np.iinfo(np.int32).max:
        raise InspectError(f"{what} does not fit a 32-bit attribute")
    return values.astype(np.int32)


def assemble(tile: DecodedTile, frame: SceneFrame) -> TileMesh:
    """Join a tile's primitives into one mesh, placed with ``frame``."""
    prims = tile.primitives
    if not prims or not sum(p.triangles for p in prims):
        raise InspectError(f"{tile.uri} has no triangles")
    counts = np.array([p.positions.shape[0] for p in prims], dtype=np.int64)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    tile_positions = np.concatenate([p.positions for p in prims])
    positions = frame.to_scene(tile_positions, tile.transform)
    corner_verts = np.concatenate(
        [p.indices.astype(np.int64) + s for p, s in zip(prims, starts, strict=True)]
    ).ravel()
    slots: list[int | None] = []
    for p in prims:
        if p.material not in slots:
            slots.append(p.material)
    material = np.concatenate(
        [np.full(p.triangles, slots.index(p.material), dtype=np.int32) for p in prims]
    )
    source_primitive = _int32(np.concatenate([p.source_primitive for p in prims]), "An ID")
    source_triangle = None
    if all(p.source_triangle is not None for p in prims):
        source_triangle = _int32(
            np.concatenate([p.source_triangle for p in prims]),  # type: ignore[misc]
            "A source triangle",
        )
    uvs = None
    if any(p.uvs for p in prims):
        corner_uv = []
        for p in prims:
            uv = p.uvs[0] if p.uvs else np.zeros((p.positions.shape[0], 2), np.float32)
            corner_uv.append(uv[p.indices.ravel()])
        uvs = np.concatenate(corner_uv).astype(np.float32)
        uvs[:, 1] = 1.0 - uvs[:, 1]  # glTF v runs down; Blender's runs up
    colors = None
    if any(p.colors is not None for p in prims):
        colors = np.concatenate(
            [
                p.colors if p.colors is not None else np.ones((p.positions.shape[0], 4), np.float32)
                for p in prims
            ]
        ).astype(np.float32)
    normals = None
    if all(p.normals is not None for p in prims):
        normals = np.concatenate([p.normals for p in prims]).astype(np.float32)  # type: ignore[misc]
        if tile.root_transform is not None:
            # Tile-frame normals to the scene: the rotation of the same placement.
            rotation = _rotation(frame, tile)
            normals = (normals.astype(np.float64) @ rotation.T).astype(np.float32)
    return TileMesh(
        positions=positions,
        corner_verts=_int32(corner_verts, "A vertex index"),
        material=material,
        materials=tuple(slots),
        source_primitive=source_primitive,
        source_triangle=source_triangle,
        uvs=uvs,
        colors=colors,
        normals=normals,
    )


def _rotation(frame: SceneFrame, tile: DecodedTile) -> np.ndarray:
    """The linear part of tile frame -> scene, measured at the tile's centre."""
    centre = np.mean(np.concatenate([p.positions for p in tile.primitives]), axis=0)
    probe = centre + np.vstack([np.zeros(3), np.eye(3)])
    mapped = frame.to_scene(probe, tile.transform)
    linear = (mapped[1:] - mapped[0]).T
    u, _s, vt = np.linalg.svd(linear)
    return u @ vt


# --- tracing ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceFaces:
    """Source polygons an output selection traces to."""

    polygons: dict[str, np.ndarray]  # object name -> sorted unique polygon indices
    primitives: np.ndarray  # sorted unique source primitives touched
    whole_primitives: bool  # True for coarse faces: each primitive is selected whole
    unknown: int  # output faces without a usable source ID

    @property
    def count(self) -> int:
        return sum(int(p.size) for p in self.polygons.values())


def _primitive_count(face_map: FaceMap) -> int:
    return int(face_map.primitive_offsets.shape[0] - 1)


def _rows(
    face_map: FaceMap, primitive: np.ndarray, triangle: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Face-map rows of (primitive, triangle) pairs, and which pairs are valid."""
    primitive = np.asarray(primitive, dtype=np.int64)
    triangle = np.asarray(triangle, dtype=np.int64)
    count = _primitive_count(face_map)
    valid = (primitive >= 0) & (primitive < count) & (triangle >= 0)
    p = np.where(valid, primitive, 0)
    starts = face_map.primitive_offsets[p]
    sizes = face_map.primitive_offsets[p + 1] - starts
    valid &= triangle < sizes
    return np.where(valid, starts + triangle, 0), valid


def source_faces(
    face_map: FaceMap, primitive: np.ndarray, triangle: np.ndarray | None
) -> SourceFaces:
    """The source polygons of output faces. ``triangle`` None (a coarse tile) selects
    every polygon of each representative primitive."""
    primitive = np.asarray(primitive, dtype=np.int64)
    count = _primitive_count(face_map)
    if triangle is None:
        valid = (primitive >= 0) & (primitive < count)
        chosen = np.zeros(count, dtype=bool)
        chosen[primitive[valid]] = True
        row_mask = np.repeat(chosen, np.diff(face_map.primitive_offsets))
        rows = np.flatnonzero(row_mask)
        whole = True
    else:
        rows, valid = _rows(face_map, primitive, triangle)
        rows = rows[valid]
        whole = False
    objects = face_map.source_object[rows]
    polygons = face_map.source_polygon[rows]
    out: dict[str, np.ndarray] = {}
    for index in np.unique(objects):
        out[face_map.object_names[int(index)]] = np.unique(polygons[objects == index])
    return SourceFaces(
        polygons=out,
        primitives=np.unique(primitive[valid]),
        whole_primitives=whole,
        unknown=int((~valid).sum()),
    )


def selected_rows(face_map: FaceMap, selection: Mapping[str, np.ndarray]) -> np.ndarray:
    """(T,) bool: face-map rows whose source polygon is selected.

    ``selection`` maps object names to a bool mask over that object's polygons;
    objects not in the face map are ignored."""
    rows = np.zeros(face_map.source_object.shape[0], dtype=bool)
    for name, mask in selection.items():
        if name not in face_map.object_names:
            continue
        mask = np.asarray(mask, dtype=bool)
        index = face_map.object_names.index(name)
        mine = face_map.source_object == index
        polygons = face_map.source_polygon[mine]
        inside = polygons < mask.shape[0]
        hit = np.zeros(polygons.shape[0], dtype=bool)
        hit[inside] = mask[polygons[inside]]
        rows[mine] = hit
    return rows


def tile_face_mask(
    face_map: FaceMap,
    rows: np.ndarray,
    primitive: np.ndarray,
    triangle: np.ndarray | None,
) -> np.ndarray:
    """(F,) bool: a tile's faces that trace to a selected face-map row.

    A leaf face is selected when its own row is; a coarse face (``triangle``
    None) when any row of its representative primitive is."""
    primitive = np.asarray(primitive, dtype=np.int64)
    if triangle is not None:
        index, valid = _rows(face_map, primitive, triangle)
        return valid & rows[index]
    count = _primitive_count(face_map)
    offsets = face_map.primitive_offsets
    chosen = np.logical_or.reduceat(rows, offsets[:-1]) if rows.size else np.zeros(count, bool)
    valid = (primitive >= 0) & (primitive < count)
    return valid & chosen[np.where(valid, primitive, 0)]


def topology_problems(face_map: FaceMap, current: Mapping[str, str | None]) -> list[str]:
    """Why the face map does not fit the scene, one message per object, or [].

    ``current`` maps each face-map object name to its plain topology digest now
    (``core.facemap.topology_digest`` of the original mesh), or None if there is
    no such mesh object any more."""
    problems = []
    for name in face_map.object_names:
        recorded = face_map.topology.get(name)
        now = current.get(name)
        if now is None:
            problems.append(f"{name!r} is not in the scene any more")
        elif recorded is None:
            problems.append(f"{name!r} has no topology hash in the face map")
        elif recorded.startswith(EVALUATED_PREFIX):
            problems.append(
                f"{name!r} was published with modifiers that change its topology, so the "
                "face map numbers evaluated faces, which cannot be selected"
            )
        elif recorded != now:
            problems.append(f"{name!r} has been edited since it was published")
    return problems


# --- texel density ---------------------------------------------------------------------------


def triangle_areas(positions: np.ndarray, corner_verts: np.ndarray) -> np.ndarray:
    """(F,) float64 area of each triangle, in the positions' units squared."""
    tri = np.asarray(positions, dtype=np.float64)[np.asarray(corner_verts).reshape(-1, 3)]
    return 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)


def texel_density(
    positions: np.ndarray,
    corner_verts: np.ndarray,
    corner_uvs: np.ndarray,
    page_texels: np.ndarray,
) -> np.ndarray:
    """(F,) texels per square metre: UV area x page texels / 3D area.

    ``corner_uvs`` (3F, 2) and ``page_texels`` (F,) the width x height of the page
    each face samples (0 or NaN: untextured). Faces without area or texture are NaN."""
    uv = np.asarray(corner_uvs, dtype=np.float64).reshape(-1, 3, 2)
    du, dv = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    uv_area = 0.5 * np.abs(du[:, 0] * dv[:, 1] - du[:, 1] * dv[:, 0])
    area = triangle_areas(positions, corner_verts)
    texels = np.asarray(page_texels, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        density = uv_area * texels / area
    density[~(area > 0) | ~(texels > 0) | ~np.isfinite(density)] = np.nan
    return density


@dataclass(frozen=True)
class DensitySummary:
    faces: int  # textured faces with an area
    median: float  # texels per m²
    low: float  # 5th percentile
    high: float  # 95th percentile

    @property
    def spacing_m(self) -> float:
        """Ground size of one texel at the median density, metres."""
        return 1.0 / math.sqrt(self.median) if self.median > 0 else math.nan

    def as_text(self) -> str:
        return (
            f"{self.median:,.0f} texels/m² (one texel ≈ {self.spacing_m * 1000:,.1f} mm); "
            f"5–95%: {self.low:,.0f}–{self.high:,.0f}"
        )


def summarise_density(density: np.ndarray, weights: np.ndarray | None = None) -> DensitySummary:
    """Percentiles of a density array, ignoring NaN. Raises InspectError if all NaN."""
    density = np.asarray(density, dtype=np.float64)
    kept = np.isfinite(density)
    if not kept.any():
        raise InspectError("No face is textured, so there is no texel density")
    low, median, high = np.percentile(density[kept], (5, 50, 95))
    return DensitySummary(int(kept.sum()), float(median), float(low), float(high))


def depth_counts(depths: Sequence[int]) -> dict[int, int]:
    """Tiles per depth, in depth order."""
    values, counts = np.unique(np.asarray(depths, dtype=np.int64), return_counts=True)
    return {int(v): int(c) for v, c in zip(values, counts, strict=True)}
