"""Where decoded output tiles go in the scene: the publish placement, inverted (M7).

bpy-free. A decoded tile (``core.output_tiles``) has positions in its Z-up tile
frame and a ``transform`` to the tileset's world. The publish placement put the
source there; this module undoes it so a tile overlays its source in Blender.
The placement is the one ``build.json`` recorded (``placement``: the exact
``mesh_to_tileset`` keywords), never the scene's current settings: the source
sits in Blender where it was published, whatever has been typed since.

* LOCAL: the tileset world is the source frame (glTF Y-up turned back to Z-up
  by rusty-tiles), so ``scene = transform @ tile``.
* ANCHOR: rusty-tiles keeps the local frame and puts the rigid placement in
  the root transform. :func:`anchor_matrix` rebuilds that matrix from the
  recorded anchor, orientation and scene offset (the same frame ``core.georef``
  and ``analysis.site`` use), and ``scene = anchor^-1 @ transform @ tile``.
  :meth:`SceneFrame.root_mismatch` compares it with the archive's root
  transform, so a record that does not belong to the archive shows.
* SOURCE_CRS: rusty-tiles mapped every vertex through the CRS to WGS84 ECEF,
  then into an east-north-up frame (the root transform). Here ``transform @
  tile`` gives ECEF in binary64, pyproj maps it to the project CRS with
  ellipsoidal heights in one vectorised call (EPSG:4978 to the CRS made 3D),
  and the recorded offsets come off: ``x = E - offset E``, ``y = N - offset N``
  and ``z = h - height offset - offset A``.

pyproj picks its own datum operation; rusty-tiles uses the one its allow-list
names (``crs_operation`` in build.json, for example GDA2020 to WGS 84 (2),
EPSG:8450). The two agree to the verification tolerance of the allow-list (1
mm) when they are the same operation, and :meth:`SceneFrame.operation_note`
says when pyproj's description does not name the recorded one. With the source
bounds (``source.glb`` beside the record; :func:`source_bounds`),
:meth:`SceneFrame.centre_residual` measures the whole chain at one point:
rusty-tiles' ENU origin is the mapped centre of the source bounds, so taking it
back through this frame must land on that centre. A residual over
:data:`CORRECT_ABOVE_M` is removed as a translation (datum operations differ by
a near-constant shift over a site) and reported.

Precision: every step is binary64. Output positions are quantised by
rusty-tiles' lossy profiles; see docs/design/m7-output-inspection.md for the
measured tolerance.
"""

from __future__ import annotations

import json
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from . import deps
from .georef import ecef_from_lon_lat_height
from .placement import Placement, PlacementMode, Vec3

__all__ = [
    "CORRECT_ABOVE_M",
    "FrameError",
    "SceneFrame",
    "anchor_matrix",
    "placement_from_record",
    "scene_frame",
    "source_bounds",
]

CORRECT_ABOVE_M = 0.001
"""A centre residual larger than this (metres) is corrected as a translation."""

_CHUNK = 1 << 20


class FrameError(ValueError):
    """The recorded placement cannot be inverted (no pyproj, an unknown CRS, bad record)."""


def _vec3(value: object, what: str) -> Vec3:
    try:
        x, y, z = (float(v) for v in value)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        raise FrameError(f"build.json has a malformed {what}: {value!r}") from None
    if not all(math.isfinite(v) for v in (x, y, z)):
        raise FrameError(f"build.json has a non-finite {what}")
    return (x, y, z)


def placement_from_record(placement: Mapping[str, Any] | None) -> Placement:
    """The :class:`Placement` behind build.json's ``placement`` (the exact
    ``mesh_to_tileset`` keywords, plus ``crs_epsg`` and ``crs_operation``)."""
    if not placement:
        return Placement()
    if placement.get("source_crs"):
        axes = placement.get("source_axes", "y-up")
        if axes != "y-up":
            raise FrameError(f"build.json records source axes {axes!r}; GeoTools writes y-up")
        return Placement(
            mode=PlacementMode.SOURCE_CRS,
            source_crs=str(placement["source_crs"]),
            source_offset=_vec3(placement.get("source_offset", (0, 0, 0)), "source offset"),
            height_offset=float(placement.get("height_offset", 0.0)),
        )
    if "anchor" in placement:
        x, y, z, w = (float(v) for v in placement.get("orientation_xyzw", (0, 0, 0, 1)))
        if abs(x) > 1e-12 or abs(y) > 1e-12:
            raise FrameError("build.json records a tilted orientation; GeoTools writes headings")
        gx, gy, gz = _vec3(placement.get("scene_offset", (0, 0, 0)), "scene offset")
        return Placement(
            mode=PlacementMode.ANCHOR,
            anchor=_vec3(placement["anchor"], "anchor"),
            heading_degrees=math.degrees(2.0 * math.atan2(z, w)),
            scene_offset=(gx, -gz, gy),  # glTF Y-up back to Blender Z-up
        )
    return Placement()


def anchor_matrix(placement: Placement) -> np.ndarray:
    """(4, 4) Blender world (Z-up) -> ECEF for an ANCHOR placement: the scene
    offset, then the heading about up, then the east-north-up frame at the anchor."""
    lon, lat, height = (float(v) for v in placement.anchor)
    lo, la = math.radians(lon), math.radians(lat)
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    enu = np.eye(4)
    enu[:3, :3] = np.column_stack([east, north, up])
    enu[:3, 3] = ecef_from_lon_lat_height(lon, lat, height)
    t = math.radians(float(placement.heading_degrees))
    turn = np.eye(4)
    turn[:2, :2] = [[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]]
    shift = np.eye(4)
    shift[:3, 3] = placement.scene_offset
    return enu @ turn @ shift


@lru_cache(maxsize=8)
def _to_crs(crs_text: str) -> Any:
    """pyproj Transformer: WGS84 ECEF -> the project CRS with ellipsoidal heights."""
    status = deps.probe_pyproj()
    if not status.available:
        raise FrameError(
            f"Placing CRS output needs pyproj, which is not available: {status.summary}"
        )
    import pyproj

    try:
        crs = pyproj.CRS.from_user_input(crs_text)
        if crs.is_compound:
            raise FrameError(f"The recorded CRS {crs_text} is compound; rusty-tiles refuses those")
        return pyproj.Transformer.from_crs("EPSG:4978", crs.to_3d(), always_xy=True)
    except pyproj.exceptions.CRSError as exc:
        raise FrameError(f"pyproj does not know the recorded CRS {crs_text}: {exc}") from exc
    except pyproj.exceptions.ProjError as exc:
        raise FrameError(f"PROJ cannot use the recorded CRS {crs_text}: {exc}") from exc


def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    return points @ matrix[:3, :3].T + matrix[:3, 3]


@dataclass(frozen=True)
class SceneFrame:
    """Decoded tile frame -> Blender world, for one recorded placement."""

    placement: Placement
    crs_operation: Mapping[str, Any] | None = None
    correction: np.ndarray = field(default_factory=lambda: np.zeros(3))
    """A translation added after the inverse (SOURCE_CRS centre residual), metres."""

    @property
    def mode(self) -> PlacementMode:
        return PlacementMode(self.placement.mode)

    def to_scene(self, points: np.ndarray, transform: np.ndarray) -> np.ndarray:
        """(N, 3) tile-frame positions -> (N, 3) float64 Blender world positions."""
        points = np.asarray(points, dtype=np.float64).reshape(-1, 3)
        transform = np.asarray(transform, dtype=np.float64)
        mode = self.mode
        if mode is PlacementMode.LOCAL:
            return _apply(transform, points) + self.correction
        if mode is PlacementMode.ANCHOR:
            matrix = np.linalg.solve(anchor_matrix(self.placement), transform)
            return _apply(matrix, points) + self.correction
        return self._from_ecef(points, transform) + self.correction

    def _from_ecef(self, points: np.ndarray, transform: np.ndarray) -> np.ndarray:
        transformer = _to_crs(self.placement.source_crs.strip())
        offset = np.asarray(self.placement.source_offset, dtype=np.float64)
        out = np.empty_like(points)
        for start in range(0, points.shape[0], _CHUNK):
            ecef = _apply(transform, points[start : start + _CHUNK])
            e, n, h = transformer.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2], errcheck=False)
            block = out[start : start + _CHUNK]
            block[:, 0] = np.asarray(e, dtype=np.float64) - offset[0]
            block[:, 1] = np.asarray(n, dtype=np.float64) - offset[1]
            block[:, 2] = np.asarray(h, dtype=np.float64) - self.placement.height_offset - offset[2]
        if not np.isfinite(out).all():
            raise FrameError(
                "Some tile positions are outside the recorded CRS (PROJ cannot map them back)."
            )
        return out

    def root_mismatch(self, root_transform: np.ndarray) -> float | None:
        """ANCHOR: the largest distance (metres, over a 1 km box) between the
        recorded placement and the archive's root transform; None otherwise."""
        if self.mode is not PlacementMode.ANCHOR:
            return None
        corners = np.array(
            [[x, y, z, 1.0] for x in (-500, 500) for y in (-500, 500) for z in (-500, 500)]
        )
        ours = corners @ anchor_matrix(self.placement).T
        theirs = corners @ np.asarray(root_transform, dtype=np.float64).T
        return float(np.abs(ours - theirs)[:, :3].max())

    def centre_residual(self, root_transform: np.ndarray, centre: Vec3) -> np.ndarray | None:
        """SOURCE_CRS: where the archive's ENU origin lands minus ``centre`` (the
        source bounds centre, Blender world), metres; None for other modes."""
        if self.mode is not PlacementMode.SOURCE_CRS:
            return None
        origin = np.zeros((1, 3))
        landed = SceneFrame(self.placement, self.crs_operation).to_scene(origin, root_transform)
        return landed[0] - np.asarray(centre, dtype=np.float64)

    def with_correction(self, correction: np.ndarray) -> SceneFrame:
        return SceneFrame(self.placement, self.crs_operation, np.asarray(correction, np.float64))

    def operation_note(self) -> str | None:
        """SOURCE_CRS: a note when pyproj's operation does not name the recorded one."""
        if self.mode is not PlacementMode.SOURCE_CRS or not self.crs_operation:
            return None
        wanted = str(self.crs_operation.get("operationId") or "")
        name = str(self.crs_operation.get("operation") or "")
        try:
            transformer = _to_crs(self.placement.source_crs.strip())
        except FrameError:
            return None
        description = str(transformer.description or "")
        text = description + str(transformer.to_json() or "")
        if not text or "unavailable" in description:
            return None  # PROJ names the operation only once it has run; the centre check holds
        if (wanted and wanted.split(":")[-1] in text) or (name and name in text):
            return None
        return (
            f"pyproj's operation ({description or 'unnamed'}) is not the recorded "
            f"{name or wanted}; the centre check measures the difference"
        )


def scene_frame(record: Mapping[str, Any]) -> SceneFrame:
    """The frame for a build record (the parsed build.json)."""
    placement_record = record.get("placement")
    if placement_record is not None and not isinstance(placement_record, Mapping):
        raise FrameError("build.json has a malformed placement")
    placement = placement_from_record(placement_record)
    operation = (placement_record or {}).get("crs_operation")
    return SceneFrame(placement, operation if isinstance(operation, Mapping) else None)


def source_bounds(glb: Path) -> tuple[Vec3, Vec3] | None:
    """Blender-world bounds of a source GLB from its POSITION accessors' min/max
    (glTF requires them), reading only the JSON chunk. None if unreadable."""
    try:
        with Path(glb).open("rb") as handle:
            head = handle.read(20)
            if len(head) < 20:
                return None
            magic, _v, _l, length, kind = struct.unpack("<IIIII", head)
            if magic != 0x46546C67 or kind != 0x4E4F534A:
                return None
            document = json.loads(handle.read(length))
        lows, highs = [], []
        accessors = document["accessors"]
        for mesh in document.get("meshes") or []:
            for prim in mesh.get("primitives") or []:
                accessor = accessors[prim["attributes"]["POSITION"]]
                lows.append(accessor["min"])
                highs.append(accessor["max"])
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    if not lows:
        return None
    lo = np.min(np.asarray(lows, dtype=np.float64), axis=0)
    hi = np.max(np.asarray(highs, dtype=np.float64), axis=0)
    # glTF (x, y, z) Y-up -> Blender (x, -z, y): the y range flips.
    return (
        (float(lo[0]), float(-hi[2]), float(lo[1])),
        (float(hi[0]), float(-lo[2]), float(hi[1])),
    )
