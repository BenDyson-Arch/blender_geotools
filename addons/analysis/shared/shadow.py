"""
analysis/shadow.py — Shadow casting analysis.

Two modes:

* **Instant shadow** — given a single sun direction, writes a boolean FACE
  attribute ``in_shadow`` to each processed mesh.  True = face centroid is
  occluded from the sun by intervening geometry.

* **Daily shadow fraction** — samples solar positions across a configurable
  time window and writes a float FACE attribute ``shadow_fraction``
  (0.0 = always in sun, 1.0 = always in shadow).

Sun direction sources for instant mode:

* **Manual** — user supplies azimuth (° clockwise from N) and elevation (°
  above horizon) directly.
* **Sun Lamp** — derived from the orientation of a Blender Sun light object.
* **Solar** — computed from geographic coordinates + UTC date/hour using the
  simplified NOAA solar position algorithm.

Daily mode always uses the Solar source with a configurable UTC hour range
and step count.

All spatial operations use :class:`~core.raycast.SceneRaycaster` so shadow
queries benefit from BVH acceleration and can be scoped to a specific terrain
collection.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import bpy
import mathutils

from ....core.raycast import SceneRaycaster

# ---------------------------------------------------------------------------
# Solar position
# ---------------------------------------------------------------------------


def solar_position(
    lat_deg: float,
    lon_deg: float,
    year: int,
    month: int,
    day: int,
    hour_utc: float,
) -> tuple[float, float]:
    """Compute solar position using the simplified NOAA algorithm.

    Parameters
    ----------
    lat_deg, lon_deg:
        Geographic latitude and longitude in decimal degrees.
        Positive = North / East.
    year, month, day:
        Calendar date (UTC).
    hour_utc:
        UTC time as a decimal hour (e.g. 14.5 = 14:30 UTC).

    Returns
    -------
    (azimuth_deg, elevation_deg)
        azimuth: 0 = North, 90 = East, clockwise.
        elevation: degrees above horizon; negative = below horizon.
    """
    # Julian Date
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn + (hour_utc - 12.0) / 24.0

    # Julian centuries from J2000.0
    t = (jd - 2451545.0) / 36525.0

    # Geometric mean longitude of Sun (°)
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360.0

    # Geometric mean anomaly (°)
    m_anom = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    m_anom_r = math.radians(m_anom)

    # Equation of centre
    c = (
        math.sin(m_anom_r) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2.0 * m_anom_r) * (0.019993 - 0.000101 * t)
        + math.sin(3.0 * m_anom_r) * 0.000289
    )

    # Apparent longitude (correcting for nutation + aberration)
    omega = 125.04 - 1934.136 * t
    app_lon_r = math.radians(l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega)))

    # Mean obliquity + correction
    obliq_mean = (
        23.0
        + (26.0 + (21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    )
    obliq_r = math.radians(obliq_mean + 0.00256 * math.cos(math.radians(omega)))

    # Solar declination
    decl_r = math.asin(math.sin(obliq_r) * math.sin(app_lon_r))

    # Equation of time (minutes)
    l0_r = math.radians(l0)
    ecc = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    y_val = math.tan(obliq_r / 2.0) ** 2
    eot = 4.0 * math.degrees(
        y_val * math.sin(2.0 * l0_r)
        - 2.0 * ecc * math.sin(m_anom_r)
        + 4.0 * ecc * y_val * math.sin(m_anom_r) * math.cos(2.0 * l0_r)
        - 0.5 * y_val**2 * math.sin(4.0 * l0_r)
        - 1.25 * ecc**2 * math.sin(2.0 * m_anom_r)
    )

    # True solar time (minutes)
    tst = (hour_utc * 60.0 + lon_deg * 4.0 + eot) % 1440.0

    # Hour angle
    ha_r = math.radians(tst / 4.0 - 180.0)
    lat_r = math.radians(lat_deg)

    # Solar zenith angle
    cos_zen = max(
        -1.0,
        min(
            1.0,
            math.sin(lat_r) * math.sin(decl_r)
            + math.cos(lat_r) * math.cos(decl_r) * math.cos(ha_r),
        ),
    )
    zenith_r = math.acos(cos_zen)
    elevation = 90.0 - math.degrees(zenith_r)

    # Solar azimuth — 0 = North, clockwise
    sin_zen = math.sin(zenith_r)
    if sin_zen < 1e-9:
        return 0.0, elevation  # near zenith; azimuth undefined

    cos_az = max(
        -1.0,
        min(
            1.0,
            (math.sin(decl_r) - math.sin(lat_r) * cos_zen)
            / (math.cos(lat_r) * sin_zen),
        ),
    )
    az = math.degrees(math.acos(cos_az))
    # Afternoon: hour angle positive → azimuth > 180 °
    if ha_r > 0.0:
        az = 360.0 - az
    return az, elevation


# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------


def sun_direction(azimuth_deg: float, elevation_deg: float) -> mathutils.Vector:
    """Convert azimuth / elevation to a world-space unit vector pointing
    **from the scene toward the sun**.

    Blender coordinate system: +X = East, +Y = North, +Z = Up.
    Azimuth 0 = North, 90 = East (clockwise from North).
    """
    az_r = math.radians(azimuth_deg)
    el_r = math.radians(elevation_deg)
    cos_el = math.cos(el_r)
    return mathutils.Vector(
        (
            math.sin(az_r) * cos_el,  # East
            math.cos(az_r) * cos_el,  # North
            math.sin(el_r),  # Up
        )
    ).normalized()


def sun_direction_from_lamp(lamp_obj: bpy.types.Object) -> mathutils.Vector:
    """Derive the toward-sun unit vector from a Blender Sun light object.

    In Blender, a Sun lamp emits light along its local **−Z** axis.  The
    direction *from surfaces toward the sun* is therefore the lamp's local
    **+Z** axis expressed in world space.
    """
    rot = lamp_obj.matrix_world.to_3x3().normalized()
    return (rot @ mathutils.Vector((0.0, 0.0, 1.0))).normalized()


# ---------------------------------------------------------------------------
# Core shadow computation
# ---------------------------------------------------------------------------


def compute_shadow_instant(
    obj: bpy.types.Object,
    raycaster: SceneRaycaster,
    toward_sun: mathutils.Vector,
    attr_name: str = "in_shadow",
    epsilon: float = 0.05,
) -> tuple[int, int]:
    """Cast a shadow ray from each face centroid toward the sun.

    Writes a boolean FACE attribute *attr_name*:

    * ``True``  — face is in shadow (ray hits intervening geometry).
    * ``False`` — face is directly illuminated.

    Parameters
    ----------
    obj:
        The mesh object to process.  Its own geometry is included in the
        raycaster if the raycaster was built that way, so self-shadowing of
        concave terrain is captured.  The *epsilon* nudge prevents the cast
        from immediately re-hitting the same polygon.
    raycaster:
        Pre-built :class:`~core.raycast.SceneRaycaster` for the blocking
        geometry (terrain, buildings, …).
    toward_sun:
        Unit vector pointing from the scene toward the sun.
    attr_name:
        Name of the boolean FACE attribute to write.
    epsilon:
        Metres to offset the ray origin from each face centroid before
        casting.  Prevents false self-intersection at the source face.

    Returns
    -------
    (shadowed_faces, total_faces)
    """
    mesh = obj.data
    mat = obj.matrix_world
    rot_mat = mat.to_3x3().normalized()
    n_faces = len(mesh.polygons)

    # Precompute world-space face normals and centroids once.
    world_normals = [rot_mat @ poly.normal for poly in mesh.polygons]
    world_centroids = [mat @ poly.center for poly in mesh.polygons]

    shadowed: list[bool] = [False] * n_faces
    for i in range(n_faces):
        # Offset along the outward face normal so the ray origin is always
        # on the correct side of the surface.  Using toward_sun for the
        # offset pushed back-facing faces *inside* the mesh, causing false
        # occlusion hits on the interior.
        origin = world_centroids[i] + world_normals[i] * epsilon
        shadowed[i] = raycaster.cast(origin, toward_sun, max_distance=1e6).hit

    # Write / replace attribute
    attr = mesh.attributes.get(attr_name)
    if attr is not None and (attr.domain != "FACE" or attr.data_type != "BOOLEAN"):
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(name=attr_name, type="BOOLEAN", domain="FACE")
    for i, val in enumerate(shadowed):
        attr.data[i].value = val

    return sum(shadowed), n_faces


def compute_shadow_fraction(
    obj: bpy.types.Object,
    raycaster: SceneRaycaster,
    sun_dirs: Sequence[mathutils.Vector],
    attr_name: str = "shadow_fraction",
    epsilon: float = 0.05,
) -> tuple[float, float, int]:
    """Sample multiple sun directions and write a shadow fraction per face.

    Each direction in *sun_dirs* should be a toward-sun unit vector with
    positive Z (above the horizon).  Callers must filter out below-horizon
    positions before calling.

    Parameters
    ----------
    obj:
        The mesh object to process.
    raycaster:
        Pre-built raycaster for the blocking geometry.
    sun_dirs:
        Sequence of toward-sun unit vectors to sample.
    attr_name:
        Name of the float FACE attribute to write.
    epsilon:
        Self-intersection guard offset (metres).

    Returns
    -------
    (min_fraction, max_fraction, face_count)
        *min_fraction* / *max_fraction* are the per-face shadow fraction
        extremes across all processed faces.

    Raises
    ------
    ValueError
        When *sun_dirs* is empty.
    """
    if not sun_dirs:
        raise ValueError("sun_dirs is empty — no above-horizon sun positions found")

    mesh = obj.data
    mat = obj.matrix_world
    rot_mat = mat.to_3x3().normalized()
    n_faces = len(mesh.polygons)
    n_steps = len(sun_dirs)

    # Precompute world-space face normals and centroids once — reused for
    # every sun direction instead of recomputing inside the inner loop.
    world_normals = [rot_mat @ poly.normal for poly in mesh.polygons]
    world_centroids = [mat @ poly.center for poly in mesh.polygons]

    shadow_counts: list[int] = [0] * n_faces
    for toward_sun in sun_dirs:
        for i in range(n_faces):
            # Offset along the outward face normal to avoid self-intersection.
            # This produces pure terrain-occlusion shadow without penalising
            # faces simply for facing away from the sun at this time step.
            origin = world_centroids[i] + world_normals[i] * epsilon
            if raycaster.cast(origin, toward_sun, max_distance=1e6).hit:
                shadow_counts[i] += 1

    fractions = [c / n_steps for c in shadow_counts]

    # Write / replace attribute
    attr = mesh.attributes.get(attr_name)
    if attr is not None and (attr.domain != "FACE" or attr.data_type != "FLOAT"):
        mesh.attributes.remove(attr)
        attr = None
    if attr is None:
        attr = mesh.attributes.new(name=attr_name, type="FLOAT", domain="FACE")
    for i, f in enumerate(fractions):
        attr.data[i].value = f

    return min(fractions), max(fractions), n_faces


# ---------------------------------------------------------------------------
# Sun lamp placement
# ---------------------------------------------------------------------------


def place_sun_lamp(
    azimuth_deg: float,
    elevation_deg: float,
    name: str = "GT_Sun",
    energy: float = 5.0,
    angle_deg: float = 0.53,
) -> bpy.types.Object:
    """Create or update a Blender Sun light aimed at the given solar position.

    If an object named *name* already exists and is a Sun light, its rotation
    is updated in-place.  Otherwise a new Sun light is created at the world
    origin (position does not matter for Sun lamps — only rotation does).

    The lamp's local **−Z** axis is aligned to point **toward the ground**
    in the direction the sun's rays travel (i.e. the lamp shines from the
    sun toward the scene), matching Blender's Sun lamp convention.

    Parameters
    ----------
    azimuth_deg:
        Solar azimuth — 0 = North, 90 = East, clockwise.
    elevation_deg:
        Solar elevation above horizon in degrees.
    name:
        Name for the Sun light object.
    energy:
        Sun lamp strength in watts per m².
    angle_deg:
        Angular diameter of the sun disc in degrees (default ≈ real sun).

    Returns
    -------
    The created or updated :class:`bpy.types.Object`.
    """
    toward_sun = sun_direction(azimuth_deg, elevation_deg)
    # Sun lamp emits along local -Z; to point -Z at the scene we need
    # the lamp's local +Z to equal toward_sun.
    z_world = toward_sun  # lamp +Z = toward sun

    # Build an orthonormal frame with z_world as the Z axis.
    # Use world Y (+North) as the secondary axis, fall back to X if near-parallel.
    ref = mathutils.Vector((0.0, 1.0, 0.0))
    if abs(z_world.dot(ref)) > 0.99:
        ref = mathutils.Vector((1.0, 0.0, 0.0))
    x_world = z_world.cross(ref).normalized()
    y_world = z_world.cross(x_world).normalized()

    rot_mat = mathutils.Matrix(
        (
            (x_world.x, y_world.x, z_world.x),
            (x_world.y, y_world.y, z_world.y),
            (x_world.z, y_world.z, z_world.z),
        )
    )
    euler = rot_mat.to_euler("XYZ")

    # Find existing or create new
    existing = bpy.data.objects.get(name)
    if (
        existing is not None
        and existing.type == "LIGHT"
        and existing.data.type == "SUN"
    ):
        lamp_obj = existing
    else:
        lamp_data = bpy.data.lights.new(name=name, type="SUN")
        lamp_obj = bpy.data.objects.new(name=name, object_data=lamp_data)
        bpy.context.scene.collection.objects.link(lamp_obj)

    lamp_obj.rotation_euler = euler
    lamp_obj.data.energy = energy
    lamp_obj.data.angle = math.radians(angle_deg)

    return lamp_obj
