"""Read published tiles back: rusty-tiles' decoded tile views (M7).

bpy-free. This is the ONE module that calls ``rusty_tiles.list_tiles`` and
``rusty_tiles.decode_tile`` (rusty-tiles 0.4.1, #329), so a change to that API
is a change here only. Everything else in GeoTools reads :class:`TileEntry` and
:class:`DecodedTile`.

What rusty-tiles gives (``decode_tile(..., format="arrays")``):

* positions as float64 in the Z-up tile frame, one ``transform`` (tile frame to
  the tileset's world, ECEF for placed output) and the ``root_transform``;
* indices (T, 3), normals, UVs (glTF convention: v runs down) and colours;
* feature IDs PER VERTEX, whose values are rows of a property table:
  ``source_primitive`` rows hold the source ``primitive_index``, and
  ``source_triangle`` rows hold its ``primitive_index`` and ``triangle_index``.
  Leaves carry both sets; coarse tiles only ``source_primitive``, a
  representative primitive per triangle. :func:`decode_tile` resolves them
  through the tables into per-FACE source IDs, the numbering of
  ``core.facemap``;
* RGBA8 pages (top row first), the glTF materials, textures and samplers.

The ``"arrays"`` view is used rather than the ``"glb"`` one: Blender's glTF
importer may merge vertices that differ only in a custom attribute, which would
smear the per-face IDs across neighbours.

:func:`triangle_counts` reads only each tile's GLB JSON chunk (the members of a
``.3tz`` are stored uncompressed), so the tile list can show counts cheaply.
"""

from __future__ import annotations

import importlib
import json
import struct
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np

from . import deps

ROLE_LEAF = "leaf"
ROLE_COARSE = "coarse"

SOURCE_PRIMITIVE = "source_primitive"
SOURCE_TRIANGLE = "source_triangle"
NO_ID = -1
"""A face without a source ID (no feature-ID set, or the set's null ID)."""

_GLB_MAGIC = 0x46546C67
_JSON_CHUNK = 0x4E4F534A
_MAX_JSON = 64 << 20


class OutputTilesError(RuntimeError):
    """The archive cannot be read: rusty-tiles missing, or its error."""


@dataclass(frozen=True, slots=True)
class TileEntry:
    """One content tile of a published archive, as ``list_tiles`` reports it."""

    uri: str
    depth: int
    role: str  # "leaf" or "coarse"
    geometric_error: float
    transform: np.ndarray  # (4, 4) float64, tile frame -> tileset world (ECEF if placed)

    @property
    def is_leaf(self) -> bool:
        return self.role == ROLE_LEAF


@dataclass(frozen=True, slots=True)
class DecodedPrimitive:
    """One glTF primitive of a decoded tile, with per-face source IDs."""

    material: int | None
    positions: np.ndarray  # (N, 3) float64, tile frame (Z-up)
    indices: np.ndarray  # (T, 3) uint32
    normals: np.ndarray | None  # (N, 3) float32
    uvs: tuple[np.ndarray, ...]  # (N, 2) float32 each, glTF convention (v down)
    colors: np.ndarray | None  # (N, 4) float32, linear
    source_primitive: np.ndarray  # (T,) int64, NO_ID where unknown
    source_triangle: np.ndarray | None  # (T,) int64; None on coarse tiles

    @property
    def triangles(self) -> int:
        return int(self.indices.shape[0])


@dataclass(frozen=True, slots=True)
class DecodedTile:
    """A decoded tile: geometry, pages and materials, in the tile frame."""

    uri: str
    depth: int
    role: str
    geometric_error: float
    transform: np.ndarray  # (4, 4) float64
    root_transform: np.ndarray  # (4, 4) float64
    primitives: tuple[DecodedPrimitive, ...]
    pages: tuple[np.ndarray, ...]  # (H, W, 4) uint8, top row first
    materials: tuple[dict[str, Any], ...]  # glTF material objects
    textures: tuple[dict[str, Any], ...]  # glTF textures; "source" indexes pages
    samplers: tuple[dict[str, Any], ...] = field(default=())

    @property
    def is_leaf(self) -> bool:
        return self.role == ROLE_LEAF

    @property
    def triangles(self) -> int:
        return sum(p.triangles for p in self.primitives)

    def base_color_page(self, material: int | None) -> int | None:
        """The page a material's base colour texture samples, or None."""
        if material is None or not 0 <= material < len(self.materials):
            return None
        pbr = self.materials[material].get("pbrMetallicRoughness") or {}
        ref = pbr.get("baseColorTexture")
        if not isinstance(ref, Mapping):
            return None
        index = ref.get("index")
        if not isinstance(index, int) or not 0 <= index < len(self.textures):
            return None
        source = self.textures[index].get("source")
        return source if isinstance(source, int) and 0 <= source < len(self.pages) else None


def _rusty_tiles() -> ModuleType:
    status = deps.probe_rusty_tiles()
    if not status.available:
        raise OutputTilesError(f"Cannot read output tiles: {status.summary}")
    return importlib.import_module(deps.MODULE_NAME)


def _matrix(value: object) -> np.ndarray:
    matrix = np.asarray(value, dtype=np.float64)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
        raise OutputTilesError(f"rusty-tiles gave a malformed tile transform {matrix.shape}")
    return matrix


def list_tiles(archive: Path) -> list[TileEntry]:
    """Every content tile of ``archive`` (a ``.3tz`` or a tileset directory),
    parent before children. Raises :class:`OutputTilesError`."""
    rt = _rusty_tiles()
    try:
        raw = rt.list_tiles(Path(archive))
    except rt.TilesError as exc:
        raise OutputTilesError(f"{type(exc).__name__}: {exc}") from exc
    return [
        TileEntry(
            uri=str(t["uri"]),
            depth=int(t["depth"]),
            role=str(t["role"]),
            geometric_error=float(t["geometric_error"]),
            transform=_matrix(t["transform"]),
        )
        for t in raw
    ]


def decode_tile(archive: Path, uri: str) -> DecodedTile:
    """Decode one tile into arrays with per-face source IDs.

    Raises :class:`OutputTilesError` for a rusty-tiles error or IDs that do not
    resolve through the tile's property tables."""
    rt = _rusty_tiles()
    try:
        raw = rt.decode_tile(Path(archive), uri, format="arrays")
    except rt.TilesError as exc:
        raise OutputTilesError(f"{type(exc).__name__}: {exc}") from exc
    tables = list(raw.get("property_tables") or ())
    primitives = tuple(_primitive(p, tables, uri) for p in raw["primitives"])
    return DecodedTile(
        uri=str(raw["uri"]),
        depth=int(raw["depth"]),
        role=str(raw["role"]),
        geometric_error=float(raw["geometric_error"]),
        transform=_matrix(raw["transform"]),
        root_transform=_matrix(raw["root_transform"]),
        primitives=primitives,
        pages=tuple(np.asarray(page, dtype=np.uint8) for page in raw.get("pages") or ()),
        materials=tuple(dict(m) for m in raw.get("materials") or ()),
        textures=tuple(dict(t) for t in raw.get("textures") or ()),
        samplers=tuple(dict(s) for s in raw.get("samplers") or ()),
    )


def _primitive(
    raw: Mapping[str, Any], tables: list[Mapping[str, Any]], uri: str
) -> DecodedPrimitive:
    positions = np.asarray(raw["positions"], dtype=np.float64).reshape(-1, 3)
    indices = np.asarray(raw["indices"], dtype=np.uint32).reshape(-1, 3)
    if indices.size and int(indices.max()) >= positions.shape[0]:
        raise OutputTilesError(f"{uri}: an index is beyond the positions")
    sets = {str(s["label"]): s for s in raw.get("feature_ids") or ()}
    corner = indices[:, 0] if indices.size else np.zeros(0, dtype=np.uint32)
    source_primitive = np.full(indices.shape[0], NO_ID, dtype=np.int64)
    source_triangle = None
    if SOURCE_PRIMITIVE in sets:
        source_primitive = _resolve(sets[SOURCE_PRIMITIVE], corner, tables, "primitive_index", uri)
    if SOURCE_TRIANGLE in sets:
        source_triangle = _resolve(sets[SOURCE_TRIANGLE], corner, tables, "triangle_index", uri)
        of_triangle = _resolve(sets[SOURCE_TRIANGLE], corner, tables, "primitive_index", uri)
        known = of_triangle != NO_ID
        if (of_triangle[known] != source_primitive[known]).any():
            raise OutputTilesError(f"{uri}: source_triangle and source_primitive disagree")
    material = raw.get("material")
    return DecodedPrimitive(
        material=int(material) if material is not None else None,
        positions=positions,
        indices=indices,
        normals=None if raw.get("normals") is None else np.asarray(raw["normals"], np.float32),
        uvs=tuple(np.asarray(uv, dtype=np.float32).reshape(-1, 2) for uv in raw.get("uvs") or ()),
        colors=None if raw.get("colors") is None else np.asarray(raw["colors"], np.float32),
        source_primitive=source_primitive,
        source_triangle=source_triangle,
    )


def _resolve(
    feature_set: Mapping[str, Any],
    corner: np.ndarray,
    tables: list[Mapping[str, Any]],
    prop: str,
    uri: str,
) -> np.ndarray:
    """Per-face values of ``prop`` in the set's property table (NO_ID for null IDs)."""
    values = np.asarray(feature_set["values"]).astype(np.int64, copy=False)
    ids = values[corner] if corner.size else np.zeros(0, dtype=np.int64)
    index = feature_set.get("property_table")
    label = feature_set.get("label")
    if not isinstance(index, int) or not 0 <= index < len(tables):
        raise OutputTilesError(f"{uri}: feature IDs {label!r} have no property table")
    column = (tables[index].get("properties") or {}).get(prop)
    if column is None:
        raise OutputTilesError(f"{uri}: property table {label!r} has no {prop!r}")
    column = np.asarray(column).astype(np.int64, copy=False)
    null = feature_set.get("null_feature_id")
    valid = (ids >= 0) & (ids < column.shape[0])
    if null is not None:
        valid &= ids != int(null)
    if not valid.all() and null is None:
        raise OutputTilesError(f"{uri}: feature IDs {label!r} are beyond their property table")
    out = np.full(ids.shape, NO_ID, dtype=np.int64)
    out[valid] = column[ids[valid]]
    return out


# --- cheap triangle counts -------------------------------------------------------------------


def glb_triangles(head: bytes | Iterable[bytes]) -> int | None:
    """Triangles of a GLB from its JSON chunk, or None if it cannot tell.

    ``head`` is the start of the file (at least the 20-byte header and the JSON
    chunk). Counts TRIANGLES primitives (mode 4): indices / 3, else POSITION / 3."""
    data = head if isinstance(head, bytes) else b"".join(head)
    if len(data) < 20:
        return None
    magic, _version, _length, chunk_length, chunk_type = struct.unpack_from("<IIIII", data)
    if magic != _GLB_MAGIC or chunk_type != _JSON_CHUNK or len(data) < 20 + chunk_length:
        return None
    try:
        document = json.loads(data[20 : 20 + chunk_length])
        accessors = document.get("accessors") or []
        total = 0
        for mesh in document.get("meshes") or []:
            for prim in mesh.get("primitives") or []:
                if prim.get("mode", 4) != 4:
                    continue
                accessor = prim.get("indices")
                if accessor is None:
                    accessor = (prim.get("attributes") or {}).get("POSITION")
                if accessor is None:
                    continue
                total += int(accessors[accessor]["count"]) // 3
        return total
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def _read_head(handle) -> bytes:
    head = handle.read(20)
    if len(head) < 20:
        return head
    chunk_length = struct.unpack_from("<I", head, 12)[0]
    if chunk_length > _MAX_JSON:
        return head
    return head + handle.read(chunk_length)


def triangle_counts(archive: Path, uris: Sequence[str]) -> dict[str, int]:
    """Triangle counts of ``uris`` from their GLB JSON chunks; tiles that cannot be
    read cheaply are left out. Never raises for a malformed tile."""
    archive = Path(archive)
    counts: dict[str, int] = {}
    try:
        if archive.is_dir():
            for uri in uris:
                path = archive / uri
                try:
                    with path.open("rb") as handle:
                        count = glb_triangles(_read_head(handle))
                except OSError:
                    continue
                if count is not None:
                    counts[uri] = count
            return counts
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            for uri in uris:
                if uri not in names:
                    continue
                with zf.open(uri) as handle:
                    count = glb_triangles(_read_head(handle))
                if count is not None:
                    counts[uri] = count
    except (OSError, zipfile.BadZipFile, NotImplementedError):
        return counts
    return counts
