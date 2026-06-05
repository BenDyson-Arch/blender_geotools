"""
traversal.py — tileset.json parsing, LOD selection, and transform accumulation.

Pure-Python; no bpy or mathutils dependency. Safe to run outside Blender.
"""

import json
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple


# ── Type alias ─────────────────────────────────────────────────────────────

Matrix4x4 = List[List[float]]  # 4×4 row-major nested list


# ── Dataclass ──────────────────────────────────────────────────────────────

@dataclass
class TileNode:
    """A tile content node selected for import."""

    uri: str
    """Absolute path to the tile file (when base_path was given), else raw URI."""

    geometric_error: float
    depth: int
    refine: str
    """'REPLACE' or 'ADD'."""

    bounding_volume: dict
    world_transform: Optional[Matrix4x4]
    """4×4 row-major world transform, or None for identity."""


# ── Matrix helpers ─────────────────────────────────────────────────────────

def _mat_mul(a: Matrix4x4, b: Matrix4x4) -> Matrix4x4:
    """4×4 row-major matrix multiplication."""
    return [
        [sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)]
        for i in range(4)
    ]


def accumulate_transform(
    node_dict: dict,
    parent_matrix: Optional[Matrix4x4],
) -> Optional[Matrix4x4]:
    """Compose this node's local transform with *parent_matrix*.

    3D Tiles stores transforms as a flat column-major list of 16 floats.
    Converted to row-major (transposed) before composing with the parent.
    Returns None when the composition would be identity.
    """
    flat = node_dict.get("transform")
    if flat and len(flat) == 16:
        # Column-major (3D Tiles spec) → row-major
        local: Matrix4x4 = [
            [flat[0],  flat[4],  flat[8],  flat[12]],
            [flat[1],  flat[5],  flat[9],  flat[13]],
            [flat[2],  flat[6],  flat[10], flat[14]],
            [flat[3],  flat[7],  flat[11], flat[15]],
        ]
    else:
        local = None

    if parent_matrix is None and local is None:
        return None
    if parent_matrix is None:
        return local
    if local is None:
        return parent_matrix
    return _mat_mul(parent_matrix, local)


# ── Tileset loading ────────────────────────────────────────────────────────

def load_tileset(path) -> dict:
    """Read and parse *path* as a tileset.json.

    Validates ``asset.version`` and the presence of ``root``.
    Raises ``ValueError`` on invalid structure, ``FileNotFoundError`` if absent.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    asset = data.get("asset")
    if not asset:
        raise ValueError(f"{path}: missing 'asset' key")
    version = asset.get("version")
    if version not in ("1.0", "1.1"):
        raise ValueError(f"{path}: unsupported asset.version {version!r}")
    if "root" not in data:
        raise ValueError(f"{path}: missing 'root' key")

    return data


# ── Internal traversal helpers ─────────────────────────────────────────────

def _resolve_content(
    content: dict,
    ge: float,
    depth: int,
    refine: str,
    node: dict,
    world_tf: Optional[Matrix4x4],
    base_path: Optional[Path],
    threshold: float,
) -> Iterator[TileNode]:
    """Yield TileNode(s) for *content*, recursing into sub-tilesets when needed."""
    raw_uri = content.get("uri", "")
    if base_path is not None and raw_uri.lower().endswith(".json"):
        sub_path = (base_path / raw_uri).resolve()
        try:
            sub_data = load_tileset(sub_path)
        except (FileNotFoundError, ValueError) as exc:
            warnings.warn(f"Could not load sub-tileset {sub_path}: {exc}", stacklevel=4)
            return
        yield from _traverse_node(
            sub_data["root"],
            threshold,
            sub_path.parent,
            world_tf,
            depth,
            refine,
        )
    else:
        uri = str((base_path / raw_uri).resolve()) if base_path is not None else raw_uri
        yield TileNode(
            uri=uri,
            geometric_error=ge,
            depth=depth,
            refine=refine,
            bounding_volume=node.get("boundingVolume", {}),
            world_transform=world_tf,
        )


def _traverse_node(
    node: dict,
    threshold: float,
    base_path: Optional[Path],
    world_transform: Optional[Matrix4x4],
    depth: int,
    parent_refine: str,
) -> Iterator[TileNode]:
    refine = node.get("refine", parent_refine).upper()
    world_tf = accumulate_transform(node, world_transform)
    ge = float(node.get("geometricError", 0.0))
    children = node.get("children") or []
    content = node.get("content")

    if refine == "ADD":
        warnings.warn(
            f"ADD refinement encountered at depth {depth}; "
            "parent and child geometry will overlap in Blender.",
            stacklevel=4,
        )
        # ADD: collect this node's content at every level we visit
        if content:
            yield from _resolve_content(
                content, ge, depth, refine, node, world_tf, base_path, threshold
            )
        # Recurse while finer detail is still wanted
        if ge > threshold and children:
            for child in children:
                yield from _traverse_node(
                    child, threshold, base_path, world_tf, depth + 1, refine
                )
    else:
        # REPLACE: collect at leaf / fine-enough node; otherwise recurse only
        if ge <= threshold or not children:
            if content:
                yield from _resolve_content(
                    content, ge, depth, refine, node, world_tf, base_path, threshold
                )
        else:
            for child in children:
                yield from _traverse_node(
                    child, threshold, base_path, world_tf, depth + 1, refine
                )


# ── Blender matrix helpers (require mathutils — Blender only) ────────────────

def matrix_from_tile(transform_list: Optional[Matrix4x4]):
    """Convert a tile world transform to a ``mathutils.Matrix``.

    *transform_list* is the row-major 4×4 nested list stored on
    :attr:`TileNode.world_transform` (already transposed from 3D Tiles
    column-major during :func:`accumulate_transform`).  Returns an identity
    matrix when *transform_list* is ``None``.

    Requires ``mathutils`` (available inside Blender only).
    """
    import mathutils  # noqa: PLC0415 — intentional lazy import
    if transform_list is None:
        return mathutils.Matrix.Identity(4)
    return mathutils.Matrix(transform_list)


# ── Public API ─────────────────────────────────────────────────────────────

def traverse(
    root_node: dict,
    threshold: float,
    base_path=None,
    parent_transform: Optional[Matrix4x4] = None,
) -> Iterator[TileNode]:
    """Yield :class:`TileNode` instances for tiles selected by *threshold*.

    Parameters
    ----------
    root_node:
        The ``root`` dict from a parsed tileset (``load_tileset(...)["root"]``).
    threshold:
        Collect tiles whose ``geometricError`` is at or below this value.
        Lower threshold → finer geometry, more tiles.
    base_path:
        Directory containing the tileset.json.  When supplied, content URIs
        are resolved to absolute paths and sub-tileset references are
        automatically loaded and traversed.
    parent_transform:
        Accumulated 4×4 row-major world transform from an enclosing context.
    """
    bp = Path(base_path) if base_path is not None else None
    yield from _traverse_node(root_node, threshold, bp, parent_transform, 0, "REPLACE")


def scan_tileset(
    root_node: dict,
    base_path=None,
) -> Tuple[float, float, int]:
    """Scan the entire tileset tree without importing geometry.

    Traverses every branch, loading sub-tilesets when *base_path* is given.

    Returns
    -------
    (min_error, max_error, total_content_nodes)
        ``min_error`` and ``max_error`` are the ``geometricError`` range across
        all content-bearing nodes; ``total_content_nodes`` is the count.
        Used to populate the UI slider range.
    """
    bp = Path(base_path) if base_path is not None else None
    errors: List[float] = []
    _scan_node(root_node, bp, "REPLACE", errors)
    if not errors:
        return 0.0, 0.0, 0
    return min(errors), max(errors), len(errors)


def _scan_node(
    node: dict,
    base_path: Optional[Path],
    parent_refine: str,
    errors: List[float],
) -> None:
    """Recursively collect geometricError values for all content nodes."""
    refine = node.get("refine", parent_refine).upper()
    ge = float(node.get("geometricError", 0.0))
    children = node.get("children") or []
    content = node.get("content")

    if content:
        raw_uri = content.get("uri", "")
        if base_path is not None and raw_uri.lower().endswith(".json"):
            sub_path = (base_path / raw_uri).resolve()
            try:
                sub_data = load_tileset(sub_path)
                _scan_node(sub_data["root"], sub_path.parent, refine, errors)
            except (FileNotFoundError, ValueError):
                errors.append(ge)  # count the reference itself on failure
        else:
            errors.append(ge)

    for child in children:
        _scan_node(child, base_path, refine, errors)
