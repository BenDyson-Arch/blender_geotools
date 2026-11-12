"""Opt-in acceptance test against the real BlenderGIS.

Skipped unless ``GEOTOOLS_BLENDERGIS_ZIP`` points to a BlenderGIS zip (for
example GitHub's ``domlysz/BlenderGIS`` master archive). It installs BlenderGIS
into the test profile, then checks that

- BlenderGIS's own ``GeoScene`` reads a georeference GeoTools set, exactly
  (CRS, projected origin, and lat/lon equal to BlenderGIS's own reprojection);
- GeoTools reads a georeference BlenderGIS set;
- a GeoTIFF DEM imported with BlenderGIS's georaster importer lands on the same
  coordinates as GeoTools' GeoTIFF import of the same file, both when GeoTools
  set the scene origin and when BlenderGIS did.

GeoTools never imports BlenderGIS itself; only this test does. BlenderGIS's
imageio plugin downloads the FreeImage library on first use, into the test
profile here (``IMAGEIO_USERDIR``), so the test needs the network once.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import traceback
from pathlib import Path

import _geotiff as gw
import _harness as h
import addon_utils
import bpy
import numpy as np

ZIP_ENV = "GEOTOOLS_BLENDERGIS_ZIP"
E0, N0 = 512_000.25, 6_961_000.75
ORIGIN = (512_345.0, 6_961_789.0)  # top-left corner of the raster


def _zip() -> Path | None:
    text = os.environ.get(ZIP_ENV, "")
    return Path(text) if text else None


def _install() -> str:
    """Install and enable BlenderGIS from the zip; return its module name."""
    path = _zip()
    assert path is not None and path.is_file(), f"{ZIP_ENV}={path} is not a file"
    user = Path(bpy.utils.resource_path("USER"))
    os.environ.setdefault("IMAGEIO_USERDIR", str(user / "imageio"))
    before = {m.__name__ for m in addon_utils.modules()}
    assert bpy.ops.preferences.addon_install(filepath=str(path), overwrite=True) == {"FINISHED"}
    names = [m.__name__ for m in addon_utils.modules(refresh=True) if m.__name__ not in before]
    names = names or [
        m.__name__ for m in addon_utils.modules() if "blendergis" in m.__name__.lower()
    ]
    assert names, "BlenderGIS was not installed"
    name = names[0]
    errors: list[str] = []
    addon_utils.enable(
        name, default_set=True, handle_error=lambda _ex: errors.append(traceback.format_exc())
    )
    assert not errors, errors
    return name


def _geoscene(name: str):
    return importlib.import_module(f"{name}.geoscene")


def _reproj(name: str):
    return importlib.import_module(f"{name}.core.proj.reproj")


def _world_vertices(obj: bpy.types.Object) -> np.ndarray:
    """World-space vertex positions, sorted by x, y (float64)."""
    mesh = obj.data
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3).astype(np.float64)
    m = np.array(obj.matrix_world, dtype=np.float64)
    world = co @ m[:3, :3].T + m[:3, 3]
    order = np.lexsort((world[:, 1], world[:, 0]))
    return world[order]


def _dem(tmp: Path) -> tuple[Path, np.ndarray]:
    z = (100.0 + np.arange(5 * 7, dtype=np.float32).reshape(5, 7) * 0.5).astype(np.float32)
    spec = gw.GeoTiffSpec(origin=ORIGIN, pixel_size=(2.0, 2.0), epsg=7856)
    return gw.write_geotiff(tmp / "dem.tif", z, spec), z


def _geotools_import(path: Path) -> bpy.types.Object:
    before = set(bpy.data.objects)
    result, error = h.call_op(bpy.ops.geotools.import_geotiff, filepath=str(path), mode="DEM")
    assert result == {"FINISHED"}, error
    (obj,) = [o for o in bpy.data.objects if o not in before]
    return obj


def _blendergis_import(path: Path, **kwargs: object) -> bpy.types.Object:
    before = set(bpy.data.objects)
    result = bpy.ops.importgis.georaster(
        "EXEC_DEFAULT", filepath=str(path), importMode="DEM_RAW", step=1, **kwargs
    )
    assert result == {"FINISHED"}, result
    (obj,) = [o for o in bpy.data.objects if o not in before]
    return obj


def _clear_blendergis_scene_data() -> None:
    scene = bpy.context.scene
    for key in ("_RNA_UI", "scale", "zoom"):
        if key in scene:
            del scene[key]


def _check_geoscene_reads_geotools(name: str) -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    props.origin_e, props.origin_n = repr(E0), repr(N0)
    geoscn = _geoscene(name).GeoScene(bpy.context.scene)
    assert geoscn.hasValidCRS and geoscn.isGeoref and not geoscn.isBroken
    assert geoscn.crs == "EPSG:7856"
    assert geoscn.getOriginPrj() == (E0, N0)  # bit for bit
    lon_lat = h.submodule("core.geoscene").origin_lon_lat("EPSG:7856", E0, N0)
    if lon_lat is None:  # no pyproj in this build: GeoTools wrote no lat/lon
        assert not geoscn.hasOriginGeo
    else:
        assert geoscn.isFullyGeoref
        lon, lat = geoscn.getOriginGeo()
        assert (lon, lat) == lon_lat
        # BlenderGIS's own reprojection of the same origin (it uses pyproj when it can
        # import it, as it can in a GeoTools profile with the bundled wheels).
        theirs = _reproj(name).reprojPt("EPSG:7856", 4326, E0, N0)
        assert abs(theirs[0] - lon) < 1e-9 and abs(theirs[1] - lat) < 1e-9, (theirs, lon_lat)
    print(f"    BlenderGIS GeoScene reads {geoscn.crs}, {geoscn.getOriginPrj()}")

    # And the reverse: BlenderGIS moves the origin (no object move), GeoTools follows.
    geoscn.setOriginPrj(E0 + 10.0, N0 - 10.0)
    assert props.origin_e == repr(E0 + 10.0) and props.origin_n == repr(N0 - 10.0)
    placement = h.submodule("blender.properties").placement_from_props(props)
    assert placement.source_offset == (E0 + 10.0, N0 - 10.0, 0.0)


def _check_dem_on_geotools_origin(name: str, tmp: Path) -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = "EPSG:7856"
    props.origin_e, props.origin_n = repr(E0), repr(N0)
    path, _z = _dem(tmp)
    ours = _world_vertices(_geotools_import(path))
    theirs = _world_vertices(_blendergis_import(path))
    assert ours.shape == theirs.shape, (ours.shape, theirs.shape)
    np.testing.assert_allclose(theirs, ours, rtol=0, atol=1e-3)
    print(
        f"    {len(ours)} DEM vertices match on the GeoTools origin (max diff "
        f"{np.abs(theirs - ours).max():.2e} m)"
    )


def _check_dem_on_blendergis_origin(name: str, tmp: Path) -> None:
    h.reset_scene()
    _clear_blendergis_scene_data()
    prefs = bpy.context.preferences.addons[name].preferences
    import json

    crs_list = json.loads(prefs.predefCrsJson)
    if not any(item[0] == "EPSG:7856" for item in crs_list):
        crs_list.append(["EPSG:7856", "GDA2020 / MGA zone 56", "Test"])
        prefs.predefCrsJson = json.dumps(crs_list)
    path, _z = _dem(tmp)
    theirs_obj = _blendergis_import(path, rastCRS="EPSG:7856")
    scene = bpy.context.scene
    assert scene["SRID"] == "EPSG:7856" and "crs x" in scene, dict(scene.items())
    assert bpy.ops.geotools.use_blendergis_georef() == {"FINISHED"}
    props = h.project()
    assert props.crs == "EPSG:7856"
    assert float(props.origin_e) == scene["crs x"] and float(props.origin_n) == scene["crs y"]
    ours = _world_vertices(_geotools_import(path))
    theirs = _world_vertices(theirs_obj)
    np.testing.assert_allclose(theirs, ours, rtol=0, atol=1e-3)
    print(
        f"    {len(ours)} DEM vertices match on the BlenderGIS origin "
        f"({scene['crs x']}, {scene['crs y']})"
    )


def test_blendergis_acceptance() -> None:
    if _zip() is None:
        print(f"    skipped: set {ZIP_ENV} to a BlenderGIS zip")
        return
    name = _install()
    try:
        _check_geoscene_reads_geotools(name)
        with tempfile.TemporaryDirectory(prefix="geotools-bgis-") as tmp:
            _check_dem_on_geotools_origin(name, Path(tmp))
            _check_dem_on_blendergis_origin(name, Path(tmp))
    finally:
        addon_utils.disable(name, default_set=True)
        _clear_blendergis_scene_data()
        h.reset_scene()
