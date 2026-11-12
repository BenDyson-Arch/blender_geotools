"""GeoTIFF import (DEM and orthophoto), CRS checks, linked refresh and File > Import."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _geotiff as gw
import _harness as h
import bpy
import numpy as np

ORIGIN = (512_345.0, 6_961_789.0)
OFFSET = (512_000.0, 6_961_000.0, 100.0)


@contextmanager
def _tmpdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="geotools-test-") as tmp:
        yield Path(tmp)


def _project(crs: str = "EPSG:7856", mode: str = "SOURCE_CRS") -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = mode
    props.crs = crs
    write = h.submodule("blender.properties").write_number
    for attr, value in zip(("origin_e", "origin_n", "source_offset_a"), OFFSET, strict=True):
        write(props, attr, value)


def _write(path: Path, data: np.ndarray, **kw) -> Path:
    spec = gw.GeoTiffSpec(
        origin=kw.pop("origin", ORIGIN),
        pixel_size=kw.pop("pixel_size", (2.0, 2.0)),
        epsg=kw.pop("epsg", 7856),
        nodata=kw.pop("nodata", None),
        pixel_is_point=kw.pop("pixel_is_point", False),
        geokeys=kw.pop("geokeys", True),
    )
    return gw.write_geotiff(path, data, spec, **kw)


def _import(path: Path, **kw) -> tuple[set[str], str | None, bpy.types.Object | None]:
    before = set(bpy.data.objects)
    result, error = h.call_op(bpy.ops.geotools.import_geotiff, filepath=str(path), **kw)
    new = [o for o in bpy.data.objects if o not in before]
    assert len(new) <= 1, new
    return result, error, new[0] if new else None


def _positions(obj: bpy.types.Object) -> np.ndarray:
    mesh = obj.data
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def _expected(z: np.ndarray, pixel: float = 2.0, step: int = 1, point: bool = False) -> np.ndarray:
    """Pixel centres in binary64, minus the offset, then float32."""
    rows, cols = np.arange(0, z.shape[0], step), np.arange(0, z.shape[1], step)
    half = 0.0 if point else 0.5
    xx, yy = np.meshgrid(ORIGIN[0] + (cols + half) * pixel, ORIGIN[1] - (rows + half) * pixel)
    zz = z[::step, ::step].astype(np.float64)
    pts = np.stack([xx - OFFSET[0], yy - OFFSET[1], zz - OFFSET[2]], axis=-1)
    return pts.reshape(-1, 3).astype(np.float32)


def _raster_io():
    return h.submodule("blender.raster_io")


def _links():
    return h.submodule("blender.links")


def _refresh(obj: bpy.types.Object):
    """Refresh through the shared registry, as the Linked Data panel does."""
    return _links().refresh_link(obj)


# --- DEM --------------------------------------------------------------------------------


def test_dem_import_places_vertices_in_the_project_frame() -> None:
    _project()
    z = gw.dem(12, 17)
    with _tmpdir() as tmp:
        path = _write(tmp / "site_dem.tif", z, compression="deflate", predictor=3, tile=(16, 16))
        result, error, obj = _import(path)
        assert result == {"FINISHED"}, error
        assert obj.name == "site_dem" and obj.type == "MESH"
        assert tuple(obj.matrix_world.translation) == (0.0, 0.0, 0.0)
        np.testing.assert_array_equal(_positions(obj), _expected(z))
        mesh = obj.data
        assert len(mesh.polygons) == 11 * 16
        assert mesh.uv_layers.active is not None and mesh.uv_layers.active.name == "UVMap"
        assert not mesh.validate(verbose=False)
        link = obj.geotools_link
        assert link.kind == "GEOTIFF"
        assert Path(link.filepath) == path
        assert json.loads(link.crs)["epsg"] == "EPSG:7856"
        assert link.layer == "site_dem"
        assert link.status == "OK" and link.last_sync
        assert json.loads(link.fingerprint)["size"] == path.stat().st_size
        assert json.loads(link.options) == {"assume_project_crs": False, "mode": "DEM", "step": 1}
    assert obj in bpy.context.selected_objects
    assert bpy.context.view_layer.objects.active == obj


def test_dem_pixel_is_point() -> None:
    _project()
    z = gw.dem(5, 6)
    with _tmpdir() as tmp:
        path = _write(tmp / "p.tif", z, pixel_is_point=True)
        result, error, obj = _import(path)
    assert result == {"FINISHED"}, error
    np.testing.assert_array_equal(_positions(obj), _expected(z, point=True))


def test_dem_lzw_int16_through_blenders_loader() -> None:
    _project()
    z = (gw.dem(9, 11, "int16") - 150).astype(np.int16)
    z[0, 0] = -32767
    with _tmpdir() as tmp:
        path = _write(tmp / "i16.tif", z, compression="lzw", predictor=2)
        info = h.submodule("core.geotiff").read_info(path)
        assert h.submodule("core.geotiff").dem_route(info) == "blender"
        band = _raster_io().read_dem_band(info)
        assert band.dtype == np.int16
        np.testing.assert_array_equal(band, z)
        result, error, obj = _import(path)
    assert result == {"FINISHED"}, error
    np.testing.assert_array_equal(_positions(obj), _expected(z))
    assert not any(img.filepath.endswith("i16.tif") for img in bpy.data.images)


def test_dem_uint16_bigtiff_through_blenders_loader() -> None:
    _project()
    z = (gw.dem(9, 11) * 300).astype(np.uint16)
    with _tmpdir() as tmp:
        path = _write(tmp / "u16.tif", z, bigtiff=True, compression="lzw", tile=(16, 16))
        info = h.submodule("core.geotiff").read_info(path)
        np.testing.assert_array_equal(_raster_io().read_dem_band(info), z)


def test_dem_float64_through_the_numpy_reader() -> None:
    _project()
    z = gw.dem(7, 8, "float64") + 0.123456789
    with _tmpdir() as tmp:
        path = _write(tmp / "f64.tif", z, compression="deflate", predictor=3)
        result, error, obj = _import(path)
    assert result == {"FINISHED"}, error
    np.testing.assert_array_equal(_positions(obj), _expected(z))


def test_dem_nodata_removed_and_decimated() -> None:
    _project()
    z = gw.dem(21, 21)
    z[10, 10] = -9999.0
    with _tmpdir() as tmp:
        path = _write(tmp / "nd.tif", z, nodata="-9999")
        result, error, obj = _import(path, step=2)
        assert result == {"FINISHED"}, error
        # Step 2 samples rows and columns 0, 2, ... 20: 11 x 11, and (10, 10) is one.
        assert len(obj.data.vertices) == 121 - 1
        assert len(obj.data.polygons) == 100 - 4
        expected = _expected(z, step=2)
        expected = expected[expected[:, 2] > -9000]
        np.testing.assert_array_equal(_positions(obj), expected)
        assert json.loads(obj.geotools_link.options)["step"] == 2

        result, error, full = _import(path)
        assert result == {"FINISHED"}, error
        assert len(full.data.vertices) == 441 - 1
        assert len(full.data.polygons) == 400 - 4


def test_dem_import_reports_spacing_and_time() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "r.tif", gw.dem(9, 9))
        obj, report = _raster_io().import_geotiff(bpy.context, str(path), "DEM", 2)
    assert "5 x 5 samples at 4 m x 4 m spacing" in report.message, report.message
    assert report.seconds > 0 and report.warnings == ()
    assert obj.geotools_link.kind == "GEOTIFF"


# --- orthophoto ---------------------------------------------------------------------------


def test_orthophoto_plane_placement_and_size() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "ortho.tif", gw.rgb(30, 40), pixel_size=(0.25, 0.5))
        result, error, obj = _import(path)
        assert result == {"FINISHED"}, error
        positions = _positions(obj)
        expected = [(345.0, 789.0, 0.0), (345.0, 774.0, 0.0), (355.0, 774.0, 0.0)]
        expected.append((355.0, 789.0, 0.0))
        np.testing.assert_array_equal(positions, np.array(expected, np.float32))
        bpy.context.view_layer.update()
        assert tuple(obj.dimensions[:2]) == (10.0, 15.0)
        assert len(obj.data.polygons) == 1
        assert obj.data.polygons[0].normal.z > 0.99
        material = obj.active_material
        assert material is not None
        images = [n.image for n in material.node_tree.nodes if n.type == "TEX_IMAGE"]
        assert len(images) == 1
        image = images[0]
        assert Path(bpy.path.abspath(image.filepath)) == path
        assert image.packed_file is None  # linked to the file, not packed
        assert tuple(image.size) == (40, 30)
        assert json.loads(obj.geotools_link.options)["mode"] == "ORTHO"


def test_auto_mode() -> None:
    _project()
    resolve = _raster_io().resolve_mode
    geotiff = h.submodule("core.geotiff")
    with _tmpdir() as tmp:
        cases = {
            "f32": (gw.dem(4, 4), "DEM"),
            "i16": (gw.dem(4, 4, "int16"), "DEM"),
            "u8": (gw.dem(4, 4, "uint8"), "ORTHO"),
            "rgb": (gw.rgb(4, 4), "ORTHO"),
        }
        for name, (data, mode) in cases.items():
            info = geotiff.read_info(_write(tmp / f"{name}.tif", data))
            assert resolve(info, "AUTO") == mode, name
            assert resolve(info, "DEM") == "DEM"


# --- CRS ----------------------------------------------------------------------------------


def test_crs_mismatch_is_refused() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "other.tif", gw.dem(4, 4), epsg=28356)
        result, error, obj = _import(path)
    assert result == {"ERROR_REPORTED"} and obj is None
    assert "EPSG:28356" in error and "EPSG:7856" in error and "QGIS" in error, error


def test_a_saved_crs_preset_matches_its_epsg_code() -> None:
    codes = h.submodule("core.crs_migration").PRESET_CODES
    _project(next(text for text, code in codes.items() if code == "EPSG:7856"))
    with _tmpdir() as tmp:
        result, error, _obj = _import(_write(tmp / "a.tif", gw.dem(4, 4)))
    assert result == {"FINISHED"}, error


def test_a_project_without_a_crs_is_offered_the_rasters() -> None:
    data_origin = h.submodule("blender.data_origin")
    _project(crs="")  # the offset is set: only the CRS is offered
    with _tmpdir() as tmp:
        path = _write(tmp / "a.tif", gw.dem(4, 4))
        result, error, obj = _import(path)
        assert result == {"ERROR_REPORTED"} and obj is None
        assert "no CRS yet" in error and "EPSG:7856" in error, error
        pending = data_origin.pending_offer(bpy.context)
        assert pending is not None and pending["origin"]["set_offset"] is False
        result, error = h.call_op(bpy.ops.geotools.use_data_origin, **pending["request"])
        assert result == {"FINISHED"}, error
    props = h.project()
    assert props.placement_mode == "SOURCE_CRS" and props.crs == "EPSG:7856"
    read = h.submodule("blender.properties").read_number
    assert (
        tuple(read(props, k) for k in ("origin_e", "origin_n", "source_offset_a")) == OFFSET
    )  # kept


def test_unknown_crs_needs_confirming() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "nocrs.tif", gw.dem(4, 4), geokeys=False)
        result, error, obj = _import(path)
        assert result == {"ERROR_REPORTED"} and obj is None
        assert "Data is in the project CRS" in error, error
        result, error, obj = _import(path, assume_project_crs=True)
        assert result == {"FINISHED"}, error
        assert json.loads(obj.geotools_link.options)["assume_project_crs"] is True


def test_local_project_imports_unshifted_with_a_warning() -> None:
    _project(mode="LOCAL")
    z = gw.dem(3, 3)
    with _tmpdir() as tmp:
        path = _write(tmp / "l.tif", z, origin=(100.0, 200.0), pixel_size=(1.0, 1.0))
        obj, report = _raster_io().import_geotiff(bpy.context, str(path))
    assert any("local metres" in w for w in report.warnings), report.warnings
    assert tuple(_positions(obj)[0]) == (100.5, 199.5, np.float32(z[0, 0]))


def test_rotated_transform_is_refused() -> None:
    _project()
    m = (0.9, -0.1, 0.0, 512_345.0, 0.1, 0.9, 0.0, 6_961_789.0, 0, 0, 0, 0, 0, 0, 0, 1)
    with _tmpdir() as tmp:
        path = gw.write_geotiff(tmp / "rot.tif", gw.dem(4, 4), gw.GeoTiffSpec(transformation=m))
        result, error, obj = _import(path)
    assert result == {"ERROR_REPORTED"} and obj is None
    assert "rotated" in error, error


# --- linked refresh ------------------------------------------------------------------------


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def test_dem_refresh_keeps_modifiers_materials_and_transform() -> None:
    _project()
    z = gw.dem(6, 7)
    with _tmpdir() as tmp:
        path = _write(tmp / "live.tif", z, compression="deflate")
        result, error, obj = _import(path, step=1)
        assert result == {"FINISHED"}, error
        modifier = obj.modifiers.new("Displace", "DISPLACE")
        modifier.strength = 0.5
        material = bpy.data.materials.new("Ground")
        obj.data.materials.append(material)
        obj.location = (1.0, 2.0, 3.0)
        mesh_name = obj.data.name
        old_print = obj.geotools_link.fingerprint

        z2 = z + 10.0
        z2[0, 0] = -9999.0
        _write(path, z2, compression="deflate", nodata="-9999")
        _bump_mtime(path)
        result = _refresh(obj)

        assert "refreshed" in result.summary(obj.name) and not result.by_feature
        assert [m.name for m in obj.modifiers] == ["Displace"]
        assert obj.modifiers["Displace"].strength == 0.5
        assert obj.data.materials[:] == [material]
        assert tuple(obj.location) == (1.0, 2.0, 3.0)
        assert obj.data.name == mesh_name
        assert len(bpy.data.meshes) == 1  # the old mesh was removed
        expected = _expected(z2)
        np.testing.assert_array_equal(_positions(obj), expected[expected[:, 2] > -9000])
        link = obj.geotools_link
        assert link.status == "OK" and link.fingerprint != old_print


def test_ortho_refresh_reloads_the_image_and_moves_the_plane() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "o.tif", gw.rgb(8, 8, seed=1), pixel_size=(1.0, 1.0))
        result, error, obj = _import(path)
        assert result == {"FINISHED"}, error
        material = obj.active_material
        _write(path, gw.rgb(16, 8, seed=2), pixel_size=(1.0, 1.0))
        _bump_mtime(path)
        mesh = obj.data
        _refresh(obj)
        assert obj.data == mesh  # the plane moved in place; no new mesh
        assert obj.active_material == material
        image = next(n.image for n in material.node_tree.nodes if n.type == "TEX_IMAGE")
        assert tuple(image.size) == (8, 16)
        assert len([i for i in bpy.data.images if i.name.startswith("o.tif")]) == 1
        bpy.context.view_layer.update()  # dimensions are the evaluated bounds
        assert tuple(obj.dimensions[:2]) == (8.0, 16.0)


def test_refresh_of_a_missing_file() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "gone.tif", gw.dem(4, 4))
        _result, _error, obj = _import(path)
    try:
        _refresh(obj)
    except _links().RefreshError as ex:
        assert "missing" in str(ex)
    else:
        raise AssertionError("refresh of a missing file did not raise")
    assert obj.geotools_link.status == "MISSING"


def test_refresh_refuses_a_crs_change() -> None:
    _project()
    with _tmpdir() as tmp:
        path = _write(tmp / "c.tif", gw.dem(4, 4))
        _result, _error, obj = _import(path)
        _write(path, gw.dem(4, 4), epsg=4326)
        _bump_mtime(path)
        try:
            _refresh(obj)
        except _links().RefreshError as ex:
            assert "QGIS" in str(ex)
        else:
            raise AssertionError("refresh accepted a CRS change")
    assert obj.geotools_link.status == "ERROR"
    assert len(obj.data.vertices) == 16  # untouched


def test_geotiff_refresher_is_registered_and_used_by_the_panel_operator() -> None:
    _project()
    assert _links().REFRESHERS["GEOTIFF"] is _raster_io().refresh
    z = gw.dem(5, 5)
    with _tmpdir() as tmp:
        path = _write(tmp / "reg.tif", z)
        _result, _error, obj = _import(path)
        assert bpy.context.scene.geotools_link_index == bpy.data.objects.find(obj.name)
        _write(path, z + 1.0)
        _bump_mtime(path)
        result, error = h.call_op(bpy.ops.geotools.link_refresh, name=obj.name)
        assert result == {"FINISHED"}, error
        np.testing.assert_array_equal(_positions(obj), _expected(z + 1.0))
        assert obj.geotools_link.status == "OK"


def test_geotiff_stale_detection_and_auto_refresh() -> None:
    _project()
    links = _links()
    z = gw.dem(4, 6)
    with _tmpdir() as tmp:
        path = _write(tmp / "stale.tif", z)
        _result, _error, obj = _import(path)
        assert links.check_link(obj) == "OK"
        os.utime(path)  # touched, same bytes
        assert links.check_link(obj) == "OK"
        _write(path, z + 5.0)
        _bump_mtime(path)
        assert links.check_link(obj) == "CHANGED"

        links.auto_refresh_tick()  # Auto-Refresh is off
        np.testing.assert_array_equal(_positions(obj), _expected(z))
        obj.geotools_link.auto_refresh = True
        links.auto_refresh_tick()
        np.testing.assert_array_equal(_positions(obj), _expected(z + 5.0))
        assert obj.geotools_link.status == "OK"

        path.unlink()
        assert links.check_link(obj) == "MISSING"


# --- LOCAL and ANCHOR projects -------------------------------------------------------------


def test_projected_raster_in_a_local_or_anchor_project_offers_its_centre() -> None:
    data_origin = h.submodule("blender.data_origin")
    for mode in ("LOCAL", "ANCHOR"):
        _project(mode=mode)
        z = gw.dem(12, 17)
        with _tmpdir() as tmp:
            path = _write(tmp / "far.tif", z)
            result, error, obj = _import(path, step=2)
            assert result == {"ERROR_REPORTED"} and obj is None, mode
            assert "project origin" in error and "EPSG:7856" in error, error
            pending = data_origin.pending_offer(bpy.context)
            assert pending["request"] == {
                "kind": "GEOTIFF",
                "filepath": str(path),
                "mode": "AUTO",
                "step": 2,
            }, pending
            assert not data_origin.needs_confirmation(bpy.context.scene)

            result, error = h.call_op(bpy.ops.geotools.use_data_origin, **pending["request"])
            assert result == {"FINISHED"}, error
            props = h.project()
            assert props.placement_mode == "SOURCE_CRS"
            assert props.crs == "EPSG:7856"
            # Pixel edges: 512345 to 512379 E, 6961765 to 6961789 N.
            centre = (512_362.0, 6_961_777.0, 0.0)
            read = h.submodule("blender.properties").read_number
            offset = tuple(read(props, k) for k in ("origin_e", "origin_n", "source_offset_a"))
            assert offset == centre, offset
            assert data_origin.pending_offer(bpy.context) is None
            obj = bpy.data.objects["far"]
            assert obj.geotools_link.kind == "GEOTIFF"
            rows, cols = np.arange(0, 12, 2), np.arange(0, 17, 2)
            xx, yy = np.meshgrid(ORIGIN[0] + (cols + 0.5) * 2.0, ORIGIN[1] - (rows + 0.5) * 2.0)
            zz = z[::2, ::2].astype(np.float64)
            expected = np.stack([xx - centre[0], yy - centre[1], zz], axis=-1).reshape(-1, 3)
            np.testing.assert_array_equal(_positions(obj), expected.astype(np.float32))


def test_use_data_origin_asks_first_when_there_is_a_source() -> None:
    data_origin = h.submodule("blender.data_origin")
    _project(mode="LOCAL")
    collection = bpy.data.collections.new("Site")
    bpy.context.scene.collection.children.link(collection)
    collection.objects.link(bpy.data.objects.new("Ground", bpy.data.meshes.new("Ground")))
    h.project().source_collection = collection
    with _tmpdir() as tmp:
        path = _write(tmp / "far.tif", gw.dem(4, 4))
        result, _error, _obj = _import(path)
        assert result == {"ERROR_REPORTED"}
        assert data_origin.needs_confirmation(bpy.context.scene)
        calls = h.draw(data_origin.GEOTOOLS_OT_use_data_origin)
        text = h.label_text(calls)
        assert "changes how the existing scene is placed on Earth" in text, text
        assert "'Site' (1 object)" in text and "from Local" in text, text
        # The CRS's name comes from the EPSG catalogue, which needs rusty-tiles and pyproj.
        named = h.CONFIG.expect == "available"
        assert ("EPSG:7856 (GDA2020 / MGA zone 56)" if named else "to EPSG:7856,") in text, text
        # Confirmed (EXEC, as the dialog's OK button runs it): the import goes ahead.
        result, error = h.call_op(
            bpy.ops.geotools.use_data_origin, **data_origin.pending_offer(bpy.context)["request"]
        )
        assert result == {"FINISHED"}, error
        assert h.project().placement_mode == "SOURCE_CRS" and "far" in bpy.data.objects


def test_small_local_raster_in_a_local_project_still_imports() -> None:
    _project(mode="ANCHOR")
    with _tmpdir() as tmp:
        path = _write(tmp / "near.tif", gw.dem(3, 3), origin=(-20.0, 30.0))
        result, error, _obj = _import(path)
    assert result == {"FINISHED"}, error
    assert h.project().placement_mode == "ANCHOR"


# --- File > Import ---------------------------------------------------------------------------


def test_file_import_menu_entry() -> None:
    raster_io = _raster_io()
    menu = bpy.types.TOPBAR_MT_file_import
    assert raster_io.menu_func_import in menu._dyn_ui_initialize()
    calls: list[tuple[str, str, str]] = []
    fake_menu = type("Menu", (), {"layout": h.StubLayout(calls)})()
    raster_io.menu_func_import(fake_menu, bpy.context)
    assert calls == [("operator", "geotools.import_geotiff", "NONE")], calls
    assert h.operator_available("geotools.import_geotiff")


def test_menu_entry_removed_on_disable() -> None:
    raster_io = _raster_io()
    h.disable()
    try:
        assert (
            raster_io.menu_func_import not in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize()
        )
        assert not hasattr(bpy.types.Object, "geotools_link")
    finally:
        h.enable()
    assert _raster_io().menu_func_import in bpy.types.TOPBAR_MT_file_import._dyn_ui_initialize()
