"""Rasters in the project frame: the DEM grid mesh, the orthophoto footprint
and the CRS check. Pure numpy, no ``bpy``; ``blender/raster_io.py`` only
copies these arrays into Blender with ``foreach_set``.

Coordinates stay binary64 until they become float32 positions in the project
frame: ``local = (x - offset_E, y - offset_N, z - offset_A)``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .crs_match import DataCrs, matches, project_crs
from .data_origin import Bounds, check_local_project, offer_project_crs
from .geotiff import GeoTiffInfo
from .placement import Placement, PlacementMode, Vec3

QGIS_HINT = "Reproject it to the project CRS in QGIS (Raster > Projections > Warp) first"


class RasterError(ValueError):
    """The raster cannot be imported as asked; the message says why."""


# --- CRS ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CrsCheck:
    offset: Vec3
    """The project offset to subtract (zero for LOCAL and ANCHOR projects)."""
    warnings: tuple[str, ...]


def check_crs(
    placement: Placement,
    data: DataCrs | None,
    name: str,
    *,
    confirmed: bool = False,
    bounds: Bounds | None = None,
) -> CrsCheck:
    """The offset to apply, or :class:`RasterError` if the CRS does not match.

    Rasters are never reprojected. ``confirmed`` is the user's "data is in the
    project CRS", used only when the CRS cannot be determined.

    In a LOCAL or ANCHOR project the coordinates are used as local metres, with
    a warning, unless the raster is in a known projected CRS and ``bounds`` (its
    extent) reach far from the origin: then
    :class:`~geotools.core.data_origin.NeedsProjectOrigin` (a ValueError) is
    raised, carrying the raster's CRS and centre as a project origin. A
    SOURCE_CRS project with no CRS is offered the raster's the same way
    (``data_origin.offer_project_crs``).
    """
    mode = PlacementMode(placement.mode)
    data_label = data.label() if data is not None else "no CRS"
    if mode is not PlacementMode.SOURCE_CRS:
        warning = check_local_project(mode, data, bounds, name)
        return CrsCheck((0.0, 0.0, 0.0), (warning,))
    project = project_crs(placement)
    if project is None:
        offer_project_crs(data, bounds, placement.source_offset, name)
        raise RasterError("Set the project CRS first (Project panel)")
    result = matches(project, data) if data is not None else None
    offset = tuple(float(v) for v in placement.source_offset)
    if result is True:
        return CrsCheck(offset, ())
    if result is False:
        raise RasterError(
            f"{name} is in {data.label()} but the project CRS is {project.label()}. "
            f"GeoTools does not reproject rasters. {QGIS_HINT}."
        )
    if confirmed:
        warning = (
            f"Could not tell {name}'s CRS ({data_label}); treated as the project CRS "
            f"{project.label()}, as confirmed."
        )
        return CrsCheck(offset, (warning,))
    raise RasterError(
        f"Cannot tell whether {name} ({data_label}) is in the project CRS "
        f'{project.label()}. Tick "Data is in the project CRS" if it is; otherwise '
        f"{QGIS_HINT[0].lower() + QGIS_HINT[1:]}."
    )


# --- DEM grid -----------------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class GridMesh:
    """A DEM as quads, ready for ``foreach_set``."""

    positions: np.ndarray
    """(vertices, 3) float32 in the project frame."""
    corners: np.ndarray
    """(faces * 4,) int32 vertex index of each face corner (loop)."""
    uvs: np.ndarray
    """(faces * 4, 2) float32 UV of each corner: 0..1 over the raster's footprint."""
    grid_shape: tuple[int, int]
    """(rows, columns) of sampled pixels, before nodata is removed."""
    spacing: tuple[float, float]
    """Ground spacing between vertices (x, y), in CRS units."""
    nodata_vertices: int
    """Sampled pixels dropped as nodata (or non-finite)."""
    z_range: tuple[float, float]
    """Lowest and highest vertex z in the project frame."""

    @property
    def vertex_count(self) -> int:
        return len(self.positions)

    @property
    def face_count(self) -> int:
        return len(self.corners) // 4


def valid_mask(z: np.ndarray, nodata: float | None) -> np.ndarray:
    """True where ``z`` holds a height: finite and not the nodata value."""
    valid = np.isfinite(z) if z.dtype.kind == "f" else np.ones(z.shape, bool)
    if nodata is not None and np.isfinite(nodata):
        if z.dtype.kind in "iu":
            if float(nodata).is_integer():
                valid &= z != np.asarray(nodata).astype(z.dtype)
        else:
            valid &= z != np.asarray(nodata, dtype=z.dtype)
    return valid


def decimated_shape(info: GeoTiffInfo, step: int) -> tuple[int, int]:
    """(rows, columns) of the grid sampled every ``step`` pixels."""
    return -(-info.height // step), -(-info.width // step)


def spacing(info: GeoTiffInfo, step: int) -> tuple[float, float]:
    """Ground spacing (x, y) of the grid sampled every ``step`` pixels."""
    dx, dy = info.pixel_size
    return abs(dx) * step, abs(dy) * step


# (row, column) offsets of a quad's corners from its top-left sample, in
# counter-clockwise order for a north-up raster: top-left, bottom-left,
# bottom-right, top-right.
_QUAD_CORNERS = ((0, 0), (1, 0), (1, 1), (0, 1))


def dem_grid(info: GeoTiffInfo, band: np.ndarray, offset: Vec3, step: int = 1) -> GridMesh:
    """Build the DEM mesh from band 1 (``(height, width)``, heights in metres).

    One vertex per sampled pixel centre, every ``step`` pixels from the top-left
    pixel. Faces whose corners touch a nodata vertex are dropped, and so are
    vertices no face uses. Raises :class:`RasterError` if nothing is left.
    """
    if step < 1:
        raise RasterError("The decimation step must be 1 or more")
    if band.shape != (info.height, info.width):
        raise RasterError(
            f"{info.name}: band is {band.shape[1]} x {band.shape[0]}, "
            f"expected {info.width} x {info.height}"
        )
    z = band[::step, ::step]
    rows_n, cols_n = z.shape
    if rows_n < 2 or cols_n < 2:
        raise RasterError(
            f"{info.name}: a step of {step} leaves {cols_n} x {rows_n} samples; "
            "a DEM mesh needs at least 2 x 2"
        )
    valid = valid_mask(z, info.nodata)
    if rows_n * cols_n >= 2**31:
        raise RasterError(
            f"{info.name}: {cols_n:,} x {rows_n:,} samples is too many for one mesh; "
            "use a larger decimation step"
        )

    # Faces: one quad per 2 x 2 block of valid samples, mirrored when the
    # raster is (dx * dy > 0) so the normals always face +z.
    order = (3, 2, 1, 0) if info.pixel_size[0] * info.pixel_size[1] > 0 else (0, 1, 2, 3)
    all_valid = bool(valid.all())
    if all_valid:
        flat = None
        index = np.arange(rows_n * cols_n, dtype=np.int32).reshape(rows_n, cols_n)
        quad = np.empty((rows_n - 1, cols_n - 1, 4), np.int32)
        for k, (dr, dc) in zip(order, _QUAD_CORNERS, strict=True):
            quad[:, :, k] = index[dr : rows_n - 1 + dr, dc : cols_n - 1 + dc]
        corners = quad.reshape(-1)
    else:
        face_ok = valid[:-1, :-1] & valid[1:, :-1] & valid[1:, 1:] & valid[:-1, 1:]
        faces = int(np.count_nonzero(face_ok))
        if faces == 0:
            raise RasterError(f"{info.name}: no 2 x 2 block of valid heights to build faces from")
        # A vertex is kept when one of the (up to) four faces around it is.
        used = np.zeros((rows_n, cols_n), bool)
        for dr, dc in _QUAD_CORNERS:
            used[dr : rows_n - 1 + dr, dc : cols_n - 1 + dc] |= face_ok
        flat = np.flatnonzero(used).astype(np.int32)
        remap = (np.cumsum(used, dtype=np.int32) - 1).reshape(rows_n, cols_n)
        quad = np.empty((faces, 4), np.int32)
        for k, (dr, dc) in zip(order, _QUAD_CORNERS, strict=True):
            quad[:, k] = remap[dr : rows_n - 1 + dr, dc : cols_n - 1 + dc][face_ok]
        corners = quad.reshape(-1)
        del used, remap, face_ok

    ox, oy, oz = (float(v) for v in offset)
    xs = (info.centres_x(np.arange(0, info.width, step)) - ox).astype(np.float32)
    ys = (info.centres_y(np.arange(0, info.height, step)) - oy).astype(np.float32)
    us = ((np.arange(0, info.width, step) + 0.5) / info.width).astype(np.float32)
    vs = (1.0 - (np.arange(0, info.height, step) + 0.5) / info.height).astype(np.float32)
    zf = z.astype(np.float64) - oz if oz != 0.0 or z.dtype != np.float32 else z
    if all_valid:
        count = rows_n * cols_n
        positions = np.empty((rows_n, cols_n, 3), np.float32)
        positions[:, :, 0] = xs[None, :]
        positions[:, :, 1] = ys[:, None]
        positions[:, :, 2] = zf
        positions = positions.reshape(count, 3)
        uv_vertex = np.empty((rows_n, cols_n, 2), np.float32)
        uv_vertex[:, :, 0] = us[None, :]
        uv_vertex[:, :, 1] = vs[:, None]
        uv_vertex = uv_vertex.reshape(count, 2)
    else:
        vr, vc = np.divmod(flat, np.int32(cols_n))
        positions = np.empty((len(flat), 3), np.float32)
        positions[:, 0] = xs[vc]
        positions[:, 1] = ys[vr]
        positions[:, 2] = zf.reshape(-1)[flat]
        uv_vertex = np.empty((len(flat), 2), np.float32)
        uv_vertex[:, 0] = us[vc]
        uv_vertex[:, 1] = vs[vr]
    # UVs over the full footprint, so an orthophoto with the same extent maps
    # exactly: u = 0 at column 0's left edge, v = 1 at row 0's top edge.
    uvs = uv_vertex[corners]
    heights = positions[:, 2]

    return GridMesh(
        positions=positions,
        corners=corners,
        uvs=uvs,
        grid_shape=(rows_n, cols_n),
        spacing=spacing(info, step),
        nodata_vertices=int(valid.size - np.count_nonzero(valid)),
        z_range=(float(heights.min()), float(heights.max())),
    )


# --- orthophoto footprint -------------------------------------------------------------


@dataclass(frozen=True, eq=False)
class Footprint:
    """A quad over the raster's footprint, in the project frame."""

    positions: np.ndarray
    """(4, 3) float32, counter-clockwise seen from +z."""
    uvs: np.ndarray
    """(4, 2) float32, the image's corners."""
    size: tuple[float, float]
    """Width and height on the ground, in CRS units."""


def footprint(info: GeoTiffInfo, offset: Vec3, z: float = 0.0) -> Footprint:
    """The raster's footprint (pixel edges) as a quad at local height ``z``."""
    ox, oy, _oz = (float(v) for v in offset)
    raster = [(0.0, 0.0), (0.0, info.height), (info.width, info.height), (info.width, 0.0)]
    uv = [(0.0, 1.0), (0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
    dx, dy = info.pixel_size
    if dx * dy > 0:
        raster, uv = raster[::-1], uv[::-1]
    positions = np.empty((4, 3), np.float32)
    for k, (col, row) in enumerate(raster):
        x, y = info.corner(col, row)
        positions[k] = (x - ox, y - oy, z)
    xmin, ymin, xmax, ymax = info.bounds()
    return Footprint(positions, np.array(uv, np.float32), (xmax - xmin, ymax - ymin))
