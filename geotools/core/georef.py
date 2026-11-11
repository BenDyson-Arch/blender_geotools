"""Where the local model frame sits on Earth, as one matrix (M4 metadata GLB).

bpy-free. Built from the placement probe (``core.probe``), so the numbers are
rusty-tiles' own CRS operation, never GeoTools maths beyond WGS84 ellipsoid to
ECEF.

``model_to_ecef`` maps Blender world coordinates (Z-up, metres, the frame the
GLB's positions are in before the axis change) to WGS84 ECEF:

* ANCHOR placement: rusty-tiles' tileset root transform as it is. Its tiles
  keep the local frame, so the root transform already maps local Z-up
  coordinates to ECEF (anchor, heading and scene offset included).
* SOURCE_CRS placement: rusty-tiles reprojects every vertex into an
  east-north-up frame at the centre of the source bounds and puts that frame in
  the root transform. One matrix cannot reproject, so the local axes are
  measured instead: two more probes, at the centre moved east and north by half
  the source extent, give where grid east and grid north land in that frame.
  That carries the grid convergence (about 0.9 degrees 200 km from a UTM
  central meridian at 27 S, so 1.6 m over 100 m) and the scale factor, which
  copying the root transform would drop. Exact at the centre, to first order
  elsewhere.
* LOCAL placement has no Earth frame.

:func:`gltf_node_matrix` turns it into a glTF node matrix for Y-up content:
``C @ M @ C^-1`` with ``C`` Blender (x, y, z) to glTF (x, z, -y), so a viewer
that converts glTF Y-up to Z-up (as CesiumJS and 3D Tiles do) gets ECEF.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .placement import Placement, PlacementMode, Vec3
from .probe import PlacementProbe, probe_placement

__all__ = [
    "EcefFrame",
    "GeorefError",
    "ecef_from_lon_lat_height",
    "frame_from_probes",
    "gltf_node_matrix",
    "measure_frame",
]

_WGS84_A = 6_378_137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)

# Blender Z-up (x, y, z) -> glTF Y-up (x, z, -y), as a 4x4.
_C = np.array(
    [[1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, -1.0, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]]
)


class GeorefError(RuntimeError):
    """The probe could not place the source (rusty-tiles missing or refusing)."""


@dataclass(frozen=True, slots=True)
class EcefFrame:
    """The local frame on Earth."""

    model_to_ecef: tuple[float, ...]
    """16 floats, column-major: Blender world (Z-up) -> ECEF."""
    origin_lon_lat_height: Vec3
    """The probe's origin (WGS84 degrees and ellipsoidal metres)."""
    origin_local: Vec3
    """Where the origin is in Blender world coordinates."""
    heading_degrees: float
    """Counter-clockwise angle from true east to the local +X axis at the origin."""
    scale: float
    """Length on Earth of one local metre along +X (the grid scale factor)."""
    operation: str | None
    """rusty-tiles' CRS operation ("portable" or "native"), SOURCE_CRS only."""

    @property
    def matrix(self) -> np.ndarray:
        return np.array(self.model_to_ecef, dtype=np.float64).reshape(4, 4).T


def ecef_from_lon_lat_height(lon: float, lat: float, height: float) -> np.ndarray:
    """WGS84 geodetic to ECEF metres."""
    lo, la = math.radians(lon), math.radians(lat)
    sin_la = math.sin(la)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_la * sin_la)
    return np.array(
        [
            (n + height) * math.cos(la) * math.cos(lo),
            (n + height) * math.cos(la) * math.sin(lo),
            (n * (1.0 - _WGS84_E2) + height) * sin_la,
        ]
    )


def _root(result: PlacementProbe) -> np.ndarray:
    if not result.ok or result.root_transform is None or result.origin_lon_lat_height is None:
        raise GeorefError(result.error or "rusty-tiles gave no root transform")
    return np.array(result.root_transform, dtype=np.float64).reshape(4, 4).T


def frame_from_probes(
    mode: PlacementMode,
    centre: Vec3,
    centre_probe: PlacementProbe,
    east_probe: PlacementProbe | None = None,
    north_probe: PlacementProbe | None = None,
    step: tuple[float, float] = (1.0, 1.0),
) -> EcefFrame:
    """The frame from probe results (see the module docstring); ``centre`` is the
    bounds centre and ``step`` how far the east and north probes moved it."""
    mode = PlacementMode(mode)
    root = _root(centre_probe)
    if mode is PlacementMode.ANCHOR:
        matrix = root
    elif mode is PlacementMode.SOURCE_CRS:
        if east_probe is None or north_probe is None:
            raise GeorefError("a SOURCE_CRS frame needs the east and north probes")
        rotation = root[:3, :3]
        origin = root[:3, 3]
        axes = []
        for result, d in ((east_probe, step[0]), (north_probe, step[1])):
            _root(result)
            point = ecef_from_lon_lat_height(*result.origin_lon_lat_height)
            axes.append(rotation.T @ (point - origin) / d)  # in the ENU frame at the centre
        jacobian = np.column_stack([axes[0], axes[1], (0.0, 0.0, 1.0)])
        local = np.eye(4)
        local[:3, :3] = jacobian
        local[:3, 3] = -jacobian @ np.asarray(centre, dtype=np.float64)
        matrix = root @ local
    else:
        raise GeorefError("a local placement has no Earth frame")
    lon, lat, height = centre_probe.origin_lon_lat_height
    origin_ecef = ecef_from_lon_lat_height(lon, lat, height)
    origin_local = np.linalg.solve(matrix, np.append(origin_ecef, 1.0))[:3]
    # Local +X in the ENU frame at the origin.
    lo, la = math.radians(lon), math.radians(lat)
    east = np.array([-math.sin(lo), math.cos(lo), 0.0])
    north = np.array([-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)])
    x_axis = matrix[:3, 0]
    heading = math.degrees(math.atan2(float(x_axis @ north), float(x_axis @ east)))
    return EcefFrame(
        model_to_ecef=tuple(float(v) for v in matrix.T.ravel()),
        origin_lon_lat_height=(float(lon), float(lat), float(height)),
        origin_local=(float(origin_local[0]), float(origin_local[1]), float(origin_local[2])),
        heading_degrees=heading,
        scale=float(np.linalg.norm(x_axis)),
        operation=centre_probe.operation,
    )


def measure_frame(placement: Placement, bounds_min: Vec3, bounds_max: Vec3) -> EcefFrame:
    """Run the probes for ``placement`` over these Blender-space bounds.

    Raises :class:`GeorefError` if rusty-tiles is missing or refuses the
    placement, or the placement is LOCAL. Each probe is one triangle through
    ``mesh_to_tileset`` (about 10 ms)."""
    mode = PlacementMode(placement.mode)
    if mode is PlacementMode.LOCAL:
        raise GeorefError("a local placement has no Earth frame")
    lo = np.asarray(bounds_min, dtype=np.float64)
    hi = np.asarray(bounds_max, dtype=np.float64)
    centre = (lo + hi) / 2.0
    half = (hi - lo) / 2.0

    def at(c: np.ndarray) -> PlacementProbe:
        result = probe_placement(placement, tuple(c - half), tuple(c + half))
        if not result.ok:
            raise GeorefError(result.error or "the placement probe failed")
        return result

    centre_probe = at(centre)
    if mode is PlacementMode.ANCHOR:
        return frame_from_probes(mode, tuple(centre), centre_probe)
    step = (max(float(half[0]), 1.0), max(float(half[1]), 1.0))
    east = at(centre + (step[0], 0.0, 0.0))
    north = at(centre + (0.0, step[1], 0.0))
    return frame_from_probes(mode, tuple(centre), centre_probe, east, north, step)


def gltf_node_matrix(frame: EcefFrame) -> tuple[float, ...]:
    """The glTF node matrix (16 floats, column-major) placing Y-up content in ECEF."""
    node = _C @ frame.matrix @ np.linalg.inv(_C)
    return tuple(float(v) for v in node.T.ravel())
