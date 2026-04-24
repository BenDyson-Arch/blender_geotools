"""
tile_import.py — GLB and B3DM tile loading, temp file management, join.

BlenderGIS interop removed; ECEF reprojection uses blender_geotools.core.proj.reproj.
Scene origin is managed via GeoScene from blender_geotools.geoscene.
"""

import json
import math
import os
import struct
import tempfile
from pathlib import Path
from typing import List

import bmesh
import bpy


# ── Per-object metadata ──────────────────────────────────────────────────────

def write_tile_metadata(obj: bpy.types.Object, tile) -> None:
    """Write tile provenance as custom properties on *obj*.

    Properties written:

    ========================  =======  ======================================
    Property                  Type     Description
    ========================  =======  ======================================
    ``tile_uri``              str      Absolute path to the source tile file
    ``tile_geometric_error``  float    geometricError of this tile node
    ``tile_depth``            int      Depth in the tileset tree (0 = root)
    ``tile_refine``           str      ``'ADD'`` or ``'REPLACE'``
    ``tile_bounding_volume``  str      JSON-encoded bounding volume dict
    ========================  =======  ======================================

    Visible in Properties → Object → Custom Properties.
    """
    obj["tile_uri"]             = str(tile.uri)
    obj["tile_geometric_error"] = float(tile.geometric_error)
    obj["tile_depth"]           = int(tile.depth)
    obj["tile_refine"]          = str(tile.refine)
    obj["tile_bounding_volume"] = json.dumps(tile.bounding_volume)


def bake_face_attributes(obj: bpy.types.Object, tile) -> None:
    """Write tile_depth and tile_geometric_error as FLOAT face attributes.

    Called before joining so the values survive the bmesh merge.
    Existing attributes of the same name are overwritten.
    """
    mesh = obj.data
    face_count = len(mesh.polygons)
    if face_count == 0:
        return

    for attr_name, value in (
        ("tile_depth",           float(tile.depth)),
        ("tile_geometric_error", float(tile.geometric_error)),
    ):
        if attr_name in mesh.attributes:
            mesh.attributes.remove(mesh.attributes[attr_name])
        attr = mesh.attributes.new(attr_name, "FLOAT", "FACE")
        for i in range(face_count):
            attr.data[i].value = value


def join_tiles(
    objects_and_tiles,
    joined_name: str,
    tileset_name: str,
    collection: bpy.types.Collection = None,
) -> bpy.types.Object:
    """Merge all tile objects into one mesh via ``bpy.ops.object.join``.

    Uses Blender's native join which preserves materials, UV maps, vertex
    colours, and all mesh attributes.  Tile provenance face attributes
    (``tile_depth``, ``tile_geometric_error``) are baked beforehand via
    :func:`bake_face_attributes`.

    Parameters
    ----------
    objects_and_tiles:
        Iterable of ``(bpy.types.Object, TileNode)`` pairs.
    joined_name:
        Name for the resulting merged object.
    tileset_name:
        Used as the key in the metadata text datablock name.
    collection:
        If given, the joined object is linked here; otherwise it goes to the
        scene root collection.

    Returns
    -------
    The new joined ``bpy.types.Object``.

    Side-effects
    ------------
    - All source objects (except the join target) are consumed by the join.
    - A JSON text datablock ``<tileset_name>_tile_metadata`` is written
      mapping face-index ranges to tile provenance.
    """
    pairs = list(objects_and_tiles)
    if not pairs:
        raise ValueError("No objects to join")

    scene_col = bpy.context.scene.collection

    # Ensure all objects are linked to the scene so bpy.ops.object.join works.
    for obj, _tile in pairs:
        if obj.name not in scene_col.objects:
            scene_col.objects.link(obj)

    # Apply world transforms into mesh data so the join sees correct positions.
    import mathutils
    for obj, _tile in pairs:
        obj.data.transform(obj.matrix_world)
        obj.matrix_world = mathutils.Matrix.Identity(4)

    # Build metadata from face counts before join reorders anything.
    metadata = []
    face_cursor = 0
    for obj, tile in pairs:
        fc = len(obj.data.polygons)
        if fc > 0:
            metadata.append({
                "face_start":           face_cursor,
                "face_end":             face_cursor + fc,
                "tile_uri":             str(tile.uri),
                "tile_depth":           int(tile.depth),
                "tile_geometric_error": float(tile.geometric_error),
                "tile_refine":          str(tile.refine),
                "tile_bounding_volume": tile.bounding_volume,
            })
        face_cursor += fc

    # Pick the first object as the join target.
    target = pairs[0][0]

    # Deselect all, select source objects, set target active.
    bpy.ops.object.select_all(action="DESELECT")
    for obj, _tile in pairs:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = target

    bpy.ops.object.join()

    # The target now contains the joined mesh.
    joined_obj = target
    joined_obj.name = joined_name
    joined_obj.data.name = joined_name

    # Move to the requested collection.
    if collection is not None:
        if joined_obj.name not in collection.objects:
            collection.objects.link(joined_obj)
        if joined_obj.name in scene_col.objects:
            scene_col.objects.unlink(joined_obj)

    # Write metadata text datablock.
    text_name = f"{tileset_name}_tile_metadata"
    if text_name in bpy.data.texts:
        bpy.data.texts.remove(bpy.data.texts[text_name])
    txt = bpy.data.texts.new(text_name)
    txt.write(json.dumps(metadata, indent=2))

    return joined_obj


# ── B3DM header ────────────────────────────────────────────────────────────
#
# Offset  Size  Field
#  0       4    magic              "b3dm"
#  4       4    version            uint32 (must be 1)
#  8       4    byteLength         uint32 total tile size
# 12       4    featureTableJSONByteLength  uint32
# 16       4    featureTableBinaryByteLength uint32
# 20       4    batchTableJSONByteLength    uint32
# 24       4    batchTableBinaryByteLength  uint32
#               --- header ends at 28 bytes ---
# 28+     var  featureTable JSON
# ...     var  featureTable binary
# ...     var  batchTable JSON
# ...     var  batchTable binary
# ...     var  GLB payload

_B3DM_HEADER_SIZE = 28
_B3DM_MAGIC = b"b3dm"


def extract_glb_from_b3dm(b3dm_path) -> bytes:
    """Return the raw GLB bytes embedded in a B3DM file.

    Raises ``ValueError`` on invalid magic or unsupported version.
    """
    path = Path(b3dm_path)
    with path.open("rb") as fh:
        header = fh.read(_B3DM_HEADER_SIZE)
        if len(header) < _B3DM_HEADER_SIZE:
            raise ValueError(f"{path}: file too short to contain a B3DM header")
        magic = header[0:4]
        if magic != _B3DM_MAGIC:
            raise ValueError(f"{path}: bad magic {magic!r}, expected b'b3dm'")
        (version, _byte_len,
         ft_json_len, ft_bin_len,
         bt_json_len, bt_bin_len) = struct.unpack_from("<IIIIII", header, 4)
        if version != 1:
            raise ValueError(f"{path}: unsupported B3DM version {version}")
        glb_offset = _B3DM_HEADER_SIZE + ft_json_len + ft_bin_len + bt_json_len + bt_bin_len
        fh.seek(glb_offset)
        glb_bytes = fh.read()
    return glb_bytes


def parse_batch_table(b3dm_path) -> dict:
    """Return the JSON batch table from a B3DM file, or an empty dict if absent."""
    path = Path(b3dm_path)
    with path.open("rb") as fh:
        header = fh.read(_B3DM_HEADER_SIZE)
        if len(header) < _B3DM_HEADER_SIZE or header[0:4] != _B3DM_MAGIC:
            return {}
        (_version, _byte_len,
         ft_json_len, ft_bin_len,
         bt_json_len, _bt_bin_len) = struct.unpack_from("<IIIIII", header, 4)
        if bt_json_len == 0:
            return {}
        bt_offset = _B3DM_HEADER_SIZE + ft_json_len + ft_bin_len
        fh.seek(bt_offset)
        bt_raw = fh.read(bt_json_len)
    try:
        return json.loads(bt_raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


# ── GLB import ─────────────────────────────────────────────────────────────

def import_glb(filepath, name: str) -> List[bpy.types.Object]:
    """Import a GLB file into Blender; return the newly created objects.

    Uses Blender's built-in glTF importer.  Objects are renamed with *name*
    as a prefix so they can be identified later.

    Any collections auto-created by the glTF importer are dissolved so that
    imported objects are not duplicated in the outliner.
    """
    filepath = str(Path(filepath).resolve())
    if not os.path.isfile(filepath):
        raise FileNotFoundError(f"GLB file not found: {filepath}")
    before_objs = set(bpy.data.objects.keys())
    before_cols = set(bpy.data.collections.keys())
    bpy.ops.import_scene.gltf("EXEC_DEFAULT", filepath=filepath)
    new_objs = [bpy.data.objects[k] for k in bpy.data.objects.keys() if k not in before_objs]

    # Remove collections auto-created by the glTF importer so objects don't
    # appear in both the glTF collection and the tile collection.
    # First ensure every object is in the scene root so nothing gets orphaned.
    scene_root = bpy.context.scene.collection
    gltf_cols = [bpy.data.collections[k] for k in bpy.data.collections.keys() if k not in before_cols]
    for col in gltf_cols:
        for obj in list(col.objects):
            if obj.name not in scene_root.objects:
                scene_root.objects.link(obj)
        bpy.data.collections.remove(col)

    # Rename for traceability
    stem = Path(name).stem
    if len(new_objs) == 1:
        new_objs[0].name = stem
    else:
        for i, obj in enumerate(new_objs):
            obj.name = f"{stem}_{i}"
    return new_objs


# ── ECEF ↔ geographic conversions (pure math, no external deps) ────────────

def _ecef_to_lonlat(ex: float, ey: float, ez: float):
    """Convert ECEF XYZ (metres) to (lon°, lat°, height m) using WGS84.

    Pure-math implementation — no external dependencies required.
    Uses Bowring's iterative method (converges in ~5 iterations).
    """
    # WGS84 constants
    a  = 6378137.0           # semi-major axis (m)
    f  = 1.0 / 298.257223563
    b  = a * (1.0 - f)       # semi-minor axis
    e2 = 1.0 - (b / a) ** 2  # first eccentricity squared

    lon = math.atan2(ey, ex)
    p   = math.sqrt(ex ** 2 + ey ** 2)
    lat = math.atan2(ez, p * (1.0 - e2))  # initial estimate
    for _ in range(5):
        sin_lat = math.sin(lat)
        N   = a / math.sqrt(1.0 - e2 * sin_lat ** 2)
        lat = math.atan2(ez + e2 * N * sin_lat, p)
    sin_lat = math.sin(lat)
    N = a / math.sqrt(1.0 - e2 * sin_lat ** 2)
    cos_lat = math.cos(lat)
    h = (p / cos_lat - N) if abs(cos_lat) > 1e-10 else (abs(ez) / abs(sin_lat) - N * (1.0 - e2))
    return math.degrees(lon), math.degrees(lat), h


def _utm_srid_from_lonlat(lon: float, lat: float) -> str:
    """Return the EPSG code string for the UTM zone covering (lon, lat)."""
    zone = int((lon + 180.0) / 6.0) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return f"EPSG:{epsg}"


def _utm_grid_convergence(lon_deg: float, lat_deg: float, srid: str) -> float:
    """Return the UTM grid convergence in radians at (lon, lat).

    Grid convergence γ is the angle from true north to grid north,
    measured clockwise (positive east of central meridian in NH).
    Returns 0.0 for non-UTM projections (e.g. Web Mercator).
    """
    try:
        code = int(srid.split(":")[1]) if ":" in srid else int(srid)
    except (ValueError, IndexError):
        return 0.0
    if 32601 <= code <= 32660:
        zone = code - 32600
    elif 32701 <= code <= 32760:
        zone = code - 32700
    else:
        return 0.0  # not UTM
    central_meridian = zone * 6 - 183
    delta_lon_rad = math.radians(lon_deg - central_meridian)
    lat_rad = math.radians(lat_deg)
    return math.atan(math.tan(delta_lon_rad) * math.sin(lat_rad))


# ── CRS reprojection ───────────────────────────────────────────────────────

def _ecef_from_transform(tf):
    """Extract ECEF XYZ translation from a 4×4 row-major transform.

    *tf* can be a Python nested list (64-bit) or a ``mathutils.Matrix``
    (32-bit — avoid for origin computation). Returns ``(0.0, 0.0, 0.0)``
    for ``None`` (identity).
    """
    if tf is None:
        return 0.0, 0.0, 0.0
    return float(tf[0][3]), float(tf[1][3]), float(tf[2][3])


def ecef_origin_to_projected(root_world_tf, scene_srid: str = None):
    """Reproject the root tile's ECEF origin to a projected CRS.

    *root_world_tf* should be a 64-bit Python nested list (not a
    ``mathutils.Matrix``) to preserve sub-metre precision at ECEF
    magnitudes (~6.4 M metres).

    Uses pure-math ECEF → lon/lat, then GeoTools core reproj for
    lon/lat (EPSG:4326) -> *scene_srid*. Auto-detects the UTM zone when
    *scene_srid* is ``None``.

    Raises ``RuntimeError`` if the shared reprojection engine cannot handle the request.
    """
    from ..core.proj.reproj import reproj_pt as _reproj_pt
    from ..core.errors import ReprojError

    ex, ey, ez = _ecef_from_transform(root_world_tf)

    lon, lat, _h = _ecef_to_lonlat(ex, ey, ez)

    if scene_srid is None:
        scene_srid = _utm_srid_from_lonlat(lon, lat)

    try:
        ox, oy = _reproj_pt("EPSG:4326", scene_srid, lon, lat)
    except (ReprojError, Exception) as exc:
        raise RuntimeError(
            f"Cannot reproject ECEF origin to {scene_srid}: {exc}. "
            "Install PyProj/GDAL, use a built-in supported CRS pair, or configure MapTiler fallback."
        ) from exc

    return float(ox), float(oy), scene_srid


def get_or_set_scene_origin(root_world_tf):
    """Return (ox, oy, srid) for the scene origin, creating it if absent.

    *root_world_tf* should be a 64-bit Python nested list (row-major 4×4).

    If the scene already has a GeoTools georef origin (set by a previous
    import or a CRS selector), that origin is reused so all layers stay
    aligned.

    When creating a new origin:
    - ECEF → lon/lat is handled with a pure-math WGS84 conversion.
    - lon/lat -> scene CRS uses the GeoTools reproj backend.
    - Auto-detects the UTM zone when the scene CRS is not set.
    - Falls back to raw ECEF XY + ``EPSG:4978`` when reprojection fails.
    """
    from ..geoscene import GeoScene
    gs = GeoScene(bpy.context.scene)

    if gs.has_origin_proj and gs.has_crs:
        return gs.origin_x, gs.origin_y, gs.srid

    try:
        ox, oy, srid = ecef_origin_to_projected(root_world_tf, gs.srid)
    except RuntimeError:
        if gs.has_crs:
            raise
        # No reprojection backend — store raw ECEF XY as fallback
        ex, ey, _ez = _ecef_from_transform(root_world_tf)
        ox   = ex
        oy   = ey
        srid = "EPSG:4978"

    gs.set_origin_proj(ox, oy, srid)
    return ox, oy, srid


def blender_offset_for_root(
    root_world_tf,
    scene_ox: float,
    scene_oy: float,
    scene_srid: str,
):
    """Return the (dx, dy, dz) offset to align the root tile with the scene origin.

    *root_world_tf* should be a 64-bit Python nested list (row-major 4×4).

    The ENU decomposition (``enu_mat @ tile_mat``) yields coordinates
    *relative to the root tile's own ECEF position*, so the root tile itself
    lands at (0, 0, 0) in ENU space regardless of its real-world elevation.

    dx, dy:
        Difference between the root tile's projected XY and the scene origin.
        Zero when the origin was freshly created from this tileset.
    dz:
        Ellipsoidal height of the root tile above the WGS84 ellipsoid, so
        Z in Blender equals real-world height rather than root-relative metres.

    Returns ``(0.0, 0.0, 0.0)`` as fallback when reprojection is unavailable.
    """
    ex, ey, ez = _ecef_from_transform(root_world_tf)
    _lon, _lat, h_root = _ecef_to_lonlat(ex, ey, ez)

    if scene_srid == "EPSG:4978":
        # Raw ECEF fallback — no projected XY offset, but still correct Z
        return 0.0, 0.0, h_root

    try:
        root_px, root_py, _ = ecef_origin_to_projected(root_world_tf, scene_srid)
    except RuntimeError:
        return 0.0, 0.0, h_root

    return root_px - scene_ox, root_py - scene_oy, h_root


# ── Transform application ─────────────────────────────────────────────────

def apply_transform(obj: bpy.types.Object, matrix) -> None:
    """Set *obj*'s world transform to *matrix* (a ``mathutils.Matrix``)."""
    obj.matrix_world = matrix


def get_enu_matrix(root_world_matrix):
    """Return the ECEF → local-ENU transform (inverse of the root tile matrix).

    Applying ``enu_mat @ tile_mat`` yields tile coordinates in the root tile's
    local East-North-Up frame: X = East (m), Y = North (m), Z = Up (m).
    """
    return root_world_matrix.inverted()


def apply_transform_enu(
    obj: bpy.types.Object,
    tile_world_tf,
    root_world_tf,
    blender_offset=(0.0, 0.0, 0.0),
    scene_origin=None,
) -> None:
    """Place *obj* in Blender world space aligned to the scene CRS origin.

    Parameters
    ----------
    tile_world_tf : list[list[float]] or None
        4×4 row-major tile world transform as a Python nested list (64-bit).
    root_world_tf : list[list[float]] or None
        4×4 row-major root tile world transform as a Python nested list (64-bit).
    blender_offset : tuple
        ``(dx, dy, dz)`` from :func:`blender_offset_for_root`.
    scene_origin : tuple or None
        ``(ox, oy, srid)`` for direct ECEF → projected translation.
        When provided, the tile's Blender XY position is computed by
        projecting the tile's ECEF origin directly into the scene CRS
        and subtracting the scene origin.  This eliminates the grid-
        convergence rotation error between the ENU tangent plane and
        the projected CRS (e.g. UTM), which can be tens of centimetres
        even for small sites.  The *rotation* is still taken from the
        ENU decomposition (accurate to sub-mm for local mesh extents).
        Falls back to ENU translation when reprojection fails.

    The translation component is computed in 64-bit Python to avoid
    precision loss from large ECEF coordinates (~6.4 M metres) that
    exceed 32-bit float resolution (~0.5 m at that magnitude).
    Rotation values are in the [-1, 1] range and are fine at 32-bit.
    """
    import mathutils  # noqa: PLC0415

    # Build 32-bit matrices for the rotation part (small values, no risk).
    tile_mat = mathutils.Matrix(tile_world_tf) if tile_world_tf else mathutils.Matrix.Identity(4)
    root_mat = mathutils.Matrix(root_world_tf) if root_world_tf else mathutils.Matrix.Identity(4)
    enu_mat = root_mat.inverted()

    # Rotation of (enu @ tile) — 32-bit is fine for [-1, 1] values.
    result = enu_mat @ tile_mat

    # ── Translation ────────────────────────────────────────────────
    # Prefer direct ECEF → projected CRS conversion when scene_origin
    # is available.  This places tiles in the exact same coordinate
    # frame as GeoJSON features (both go through the same map projection),
    # eliminating ENU-vs-UTM grid convergence / scale-factor drift.
    projected = False
    if scene_origin is not None:
        ox, oy, srid = scene_origin
        if srid != "EPSG:4978":  # skip raw-ECEF fallback CRS
            try:
                from ..core.proj.reproj import reproj_pt as _reproj_pt
                tex, tey, tez = _ecef_from_transform(tile_world_tf)
                tlon, tlat, th = _ecef_to_lonlat(tex, tey, tez)
                tpx, tpy = _reproj_pt("EPSG:4326", srid, tlon, tlat)

                # Rotate mesh from ENU orientation (true north) to
                # projected-CRS orientation (grid north).  For UTM the
                # angle between them is the grid convergence γ; for Web
                # Mercator γ = 0.
                gamma = _utm_grid_convergence(tlon, tlat, srid)
                if gamma != 0.0:
                    R_conv = mathutils.Matrix.Rotation(gamma, 4, 'Z')
                    result = R_conv @ result

                result[0][3] = tpx - ox
                result[1][3] = tpy - oy
                result[2][3] = th
                projected = True
            except Exception:
                pass  # fall through to ENU method

    if not projected:
        # ENU fallback: R_enu @ (t_tile - t_root) + offset.
        # The subtraction of two ~6.4 M-metre ECEF vectors is the step
        # that wrecks 32-bit precision; doing it in 64-bit keeps sub-mm
        # accuracy.
        t_tile = [tile_world_tf[i][3] for i in range(3)] if tile_world_tf else [0.0, 0.0, 0.0]
        t_root = [root_world_tf[i][3] for i in range(3)] if root_world_tf else [0.0, 0.0, 0.0]
        dt = [t_tile[i] - t_root[i] for i in range(3)]

        R = enu_mat.to_3x3()
        dx, dy, dz = blender_offset
        result[0][3] = float(R[0][0]) * dt[0] + float(R[0][1]) * dt[1] + float(R[0][2]) * dt[2] + dx
        result[1][3] = float(R[1][0]) * dt[0] + float(R[1][1]) * dt[1] + float(R[1][2]) * dt[2] + dy
        result[2][3] = float(R[2][0]) * dt[0] + float(R[2][1]) * dt[1] + float(R[2][2]) * dt[2] + dz

    obj.matrix_world = result


# ── B3DM import ────────────────────────────────────────────────────────────

def import_b3dm(filepath, name: str) -> List[bpy.types.Object]:
    """Extract the GLB from a B3DM file and import it into Blender.

    Writes GLB bytes to a temporary file, calls :func:`import_glb`, then
    deletes the temporary file.
    """
    glb_bytes = extract_glb_from_b3dm(filepath)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".glb")
    try:
        os.write(tmp_fd, glb_bytes)
        os.close(tmp_fd)
        return import_glb(tmp_path, name)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
