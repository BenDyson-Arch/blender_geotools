"""Ask rusty-tiles where a placement lands on Earth. CONTRACT (M1).

Writes one triangle spanning the source bounds as a glTF Y-up GLB, runs
``rusty_tiles.mesh_to_tileset`` with the placement keywords in a temporary
directory, and reads the placement back from the report. This is the real
rusty-tiles CRS operation, so an unsupported CRS fails here, before any build.
"""

from __future__ import annotations

import importlib
import json
import logging
import math
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .placement import Placement, PlacementMode, Vec3, blender_to_gltf

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PlacementProbe:
    ok: bool
    origin_lon_lat_height: Vec3 | None = None  # WGS84 deg, deg, ellipsoidal m
    operation: str | None = None  # "portable" or "native" (SOURCE_CRS only)
    root_transform: tuple[float, ...] | None = None  # 16 floats, column-major
    error: str | None = None  # rusty-tiles error class and message on failure


def write_probe_glb(path: Path, bounds_min: Vec3, bounds_max: Vec3) -> None:
    """One triangle across the Blender-space bounds, written glTF Y-up."""
    (x0, y0, z0), (x1, y1, z1) = bounds_min, bounds_max
    # Two opposite corners plus a third, so the triangle's box is exactly the
    # bounds and rusty-tiles' origin (the centre of the used bounds) is theirs.
    # rusty-tiles accepts a degenerate triangle, so flat bounds are fine.
    corners = [(x0, y0, z0), (x1, y1, z1), (x1, y0, z0)]
    positions = np.array([blender_to_gltf(c) for c in corners], dtype=np.float32)
    Path(path).write_bytes(_glb_bytes(positions, np.arange(3, dtype=np.uint32)))


def probe_placement(
    placement: Placement, bounds_min: Vec3, bounds_max: Vec3, workdir: Path | None = None
) -> PlacementProbe:
    """Run the probe. Never raises for rusty-tiles errors; returns ok=False."""
    try:
        kwargs = placement.mesh_to_tileset_kwargs()
        _check_bounds(bounds_min, bounds_max)
    except ValueError as exc:
        return PlacementProbe(False, error=f"ValueError: {exc}")
    try:
        rusty_tiles = importlib.import_module("rusty_tiles")
    except Exception as exc:  # missing, or a broken native wheel can raise anything
        log.debug("rusty-tiles import failed", exc_info=True)
        return PlacementProbe(
            False, error=f"rusty-tiles is not available: {type(exc).__name__}: {exc}"
        )
    try:
        # Always a fresh directory of our own, so force=True never overwrites
        # anything the user owns; it is removed whatever happens.
        with tempfile.TemporaryDirectory(
            prefix="geotools-probe-", dir=workdir, ignore_cleanup_errors=True
        ) as tmp:
            source = Path(tmp) / "probe.glb"
            write_probe_glb(source, bounds_min, bounds_max)
            result = rusty_tiles.mesh_to_tileset(
                source, Path(tmp) / "probe.3tz", jobs=1, force=True, **kwargs
            )
            report = result.report
    except rusty_tiles.TilesError as exc:
        return PlacementProbe(False, error=f"{type(exc).__name__}: {exc}")
    except OSError as exc:  # the temporary directory itself
        return PlacementProbe(False, error=f"{type(exc).__name__}: {exc}")
    return _read_report(PlacementMode(placement.mode), report)


def _read_report(mode: PlacementMode, report: dict) -> PlacementProbe:
    root = report.get("root_transform")
    root_transform = tuple(float(v) for v in root) if root is not None else None
    origin = None
    operation = None
    if mode is PlacementMode.SOURCE_CRS:
        crs = report["source_crs"]
        origin = crs["origin_degrees_metres"]
        operation = crs["operation"]
    elif mode is PlacementMode.ANCHOR:
        origin = report["placement"]["anchor_degrees_metres"]
    return PlacementProbe(
        True,
        origin_lon_lat_height=None if origin is None else _vec3(origin),
        operation=operation,
        root_transform=root_transform,
    )


def _check_bounds(bounds_min: Vec3, bounds_max: Vec3) -> None:
    try:
        lo = [float(v) for v in bounds_min]
        hi = [float(v) for v in bounds_max]
    except TypeError:
        raise ValueError("The bounds must be three finite numbers each.") from None
    if len(lo) != 3 or len(hi) != 3 or not all(math.isfinite(v) for v in lo + hi):
        raise ValueError("The bounds must be three finite numbers each.")
    if any(a > b for a, b in zip(lo, hi, strict=True)):
        raise ValueError("The bounds minimum is above the maximum.")


def _vec3(v: list[float]) -> Vec3:
    return (float(v[0]), float(v[1]), float(v[2]))


def _glb_bytes(positions: np.ndarray, indices: np.ndarray) -> bytes:
    """A minimal GLB: one TRIANGLES primitive, POSITION and uint32 indices.

    Private to the probe and its tests; the real writer arrives in M2.
    """
    positions = np.ascontiguousarray(positions, dtype="<f4").reshape(-1, 3)
    indices = np.ascontiguousarray(indices, dtype="<u4").reshape(-1)
    position_bytes = positions.tobytes()
    index_bytes = indices.tobytes()
    binary = position_bytes + index_bytes
    binary += b"\0" * (-len(binary) % 4)
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(position_bytes), "target": 34962},
            {
                "buffer": 0,
                "byteOffset": len(position_bytes),
                "byteLength": len(index_bytes),
                "target": 34963,
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,  # FLOAT
                "count": len(positions),
                "type": "VEC3",
                # float32 values widen to float exactly, so min/max match the data.
                "min": positions.min(axis=0).tolist(),
                "max": positions.max(axis=0).tolist(),
            },
            {
                "bufferView": 1,
                "componentType": 5125,  # UNSIGNED_INT
                "count": len(indices),
                "type": "SCALAR",
            },
        ],
    }
    json_bytes = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * (-len(json_bytes) % 4)
    length = 12 + 8 + len(json_bytes) + 8 + len(binary)
    return b"".join(
        (
            struct.pack("<III", 0x46546C67, 2, length),  # "glTF", version 2
            struct.pack("<II", len(json_bytes), 0x4E4F534A),  # JSON
            json_bytes,
            struct.pack("<II", len(binary), 0x004E4942),  # BIN
            binary,
        )
    )
