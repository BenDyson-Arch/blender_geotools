"""Portable paths: what GeoTools stores for linked files and outputs, across save, move and reopen.

The rule is in ``geotools/core/paths.py``: under the ``.blend``'s folder a path
is stored as ``//sub/file`` (forward slashes), anything else stays absolute;
``//`` paths with either separator resolve against the ``.blend``'s folder.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import _geotiff as gw
import _harness as h
import _vector_fixtures as vf
import bpy

OFFSET = (vf.E0, vf.N0, 10.0)


@contextmanager
def _tmpdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="geotools-paths-") as tmp:
        try:
            yield Path(tmp)
        finally:
            bpy.ops.wm.read_homefile(use_empty=True)


def _project() -> None:
    h.reset_scene()
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.crs = vf.MGA56
    write = h.submodule("blender.properties").write_number
    for attr, value in zip(("origin_e", "origin_n", "source_offset_a"), OFFSET, strict=True):
        write(props, attr, value)
    props.build_profiles.clear()
    props.build_profiles.add().name = "Site"
    props.build_profile_index = 0


def _links():
    return h.submodule("blender.links")


def _publish():
    return h.submodule("blender.publish")


def _write_data(folder: Path) -> tuple[Path, Path]:
    """A GeoJSON and a GeoTIFF DEM, in MGA zone 56 near the project origin."""
    folder.mkdir(parents=True, exist_ok=True)
    geojson = vf.write_geojson(folder / "dig.geojson", vf.contexts(1))
    spec = gw.GeoTiffSpec(origin=(vf.E0, vf.N0 + 20.0), pixel_size=(2.0, 2.0), epsg=7856)
    dem = gw.write_geotiff(folder / "dem.tif", gw.dem(6, 5), spec)
    return geojson, dem


def _import_both(geojson: Path, dem: Path) -> tuple[bpy.types.Object, bpy.types.Object]:
    result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(geojson))
    assert result == {"FINISHED"}, error
    result, error = h.call_op(bpy.ops.geotools.import_geotiff, filepath=str(dem))
    assert result == {"FINISHED"}, error
    return bpy.data.objects["dig"], bpy.data.objects["dem"]


def _save(path: Path, copy: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=copy) == {"FINISHED"}


def _open(path: Path) -> None:
    assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}


def _same(a: str | Path, b: str | Path) -> bool:
    return h.submodule("core.paths").same_file(a, b)


# --- writing -----------------------------------------------------------------------------------


def test_links_and_output_under_the_blend_folder_are_stored_relative() -> None:
    _project()
    with _tmpdir() as tmp:
        site = tmp / "site"
        geojson, dem = _write_data(site / "data")
        _save(site / "site.blend")
        dig, dem_obj = _import_both(geojson, dem)
        assert dig.geotools_link.filepath == "//data/dig.geojson"
        assert dem_obj.geotools_link.filepath == "//data/dem.tif"
        assert _links().check_link(dig) == "OK" and _links().check_link(dem_obj) == "OK"

        profile = h.project().build_profiles[0]
        profile.output_path = str(site / "out" / "site.3tz")  # as the file browser gives it
        assert profile.output_path == "//out/site.3tz"
        profile.output_path = "//out\\sub\\site.3tz"  # typed or browsed on Windows
        assert profile.output_path == "//out/sub/site.3tz"
        elsewhere = str(tmp / "elsewhere" / "site.3tz")
        profile.output_path = elsewhere
        assert profile.output_path == elsewhere  # outside the folder: absolute
        profile.output_type = "GLB"  # the suffix follows the format, still absolute
        assert profile.output_path == elsewhere[: -len(".3tz")] + ".glb"


def test_unsaved_blend_keeps_absolute_paths_until_the_first_save() -> None:
    _project()
    with _tmpdir() as tmp:
        geojson, dem = _write_data(tmp / "site" / "data")
        dig, dem_obj = _import_both(geojson, dem)
        assert _same(dig.geotools_link.filepath, geojson)  # nothing to be relative to yet
        profile = h.project().build_profiles[0]
        profile.output_path = str(tmp / "site" / "out" / "site.3tz")
        assert Path(profile.output_path).is_absolute()

        _save(tmp / "site" / "site.blend")  # GeoTools stores them for the new folder
        assert dig.geotools_link.filepath == "//data/dig.geojson"
        assert dem_obj.geotools_link.filepath == "//data/dem.tif"
        assert profile.output_path == "//out/site.3tz"


def test_store_result_records_last_output_relative() -> None:
    _project()
    with _tmpdir() as tmp:
        _save(tmp / "site.blend")
        publish = _publish()
        output = tmp / "out" / "site.3tz"
        record = tmp / "out" / "site.geotools" / "build.json"
        record.parent.mkdir(parents=True)
        record.write_text("{}")
        result = SimpleNamespace(
            output=output,
            record_path=record,
            report_summary={"triangles": 2, "tiles": 1},
            triangles=2,
            seconds=1.0,
            validation_ok=None,
        )
        session = SimpleNamespace(
            job=SimpleNamespace(result=result),
            detached=False,
            scene_name=bpy.context.scene.name,
            profile_name="Site",
        )
        publish._store_result(session)
        profile = h.project().build_profiles[0]
        assert profile.last_output == "//out/site.3tz"
        assert profile.last_record == "//out/site.geotools/build.json"
        assert bpy.ops.geotools.open_build_record.poll()
        assert _same(publish._last_output(bpy.context), output)
        assert _same(h.submodule("blender.inspect").last_output(bpy.context.scene), output)


# --- reading -----------------------------------------------------------------------------------


def test_project_folder_moves_and_reopens() -> None:
    """The .blend and its data copied to another folder find their files there."""
    _project()
    with _tmpdir() as tmp:
        site = tmp / "site"
        geojson, dem = _write_data(site / "data")
        _save(site / "site.blend")
        _import_both(geojson, dem)
        props = h.project()
        props.build_profiles[0].output_path = "//out/site.3tz"
        offset = site / "data" / "offset.txt"
        offset.write_text(f"E: {OFFSET[0]}\nN: {OFFSET[1]}\nA: {OFFSET[2]}\n")
        assert bpy.ops.geotools.load_offset_file(filepath=str(offset)) == {"FINISHED"}
        assert props.offset_file == "//data/offset.txt"
        _save(site / "site.blend")

        moved = tmp / "moved"
        shutil.copytree(site, moved)
        shutil.rmtree(site)
        _open(moved / "site.blend")

        links = _links()
        dig, dem_obj = bpy.data.objects["dig"], bpy.data.objects["dem"]
        for obj, name in ((dig, "dig.geojson"), (dem_obj, "dem.tif")):
            assert obj.geotools_link.status == "OK", (obj.name, obj.geotools_link.message)
            assert _same(links.link_path(obj.geotools_link), moved / "data" / name)
        result, error = h.call_op(bpy.ops.geotools.link_check)
        assert result == {"FINISHED"}, error
        assert {dig.geotools_link.status, dem_obj.geotools_link.status} == {"OK"}
        refreshed = links.refresh_link(dem_obj)
        assert dem_obj.geotools_link.status == "OK", refreshed

        publish = _publish()
        profile = h.project().build_profiles[0]
        assert profile.output_path == "//out/site.3tz"  # loading never rewrites it
        assert _same(publish.resolve_output(profile.output_path), moved / "out" / "site.3tz")
        assert publish.output_name(bpy.context.scene, profile) == "site.3tz"

        (moved / "data" / "dig.geojson").unlink()
        bpy.ops.geotools.link_check()
        assert dig.geotools_link.status == "MISSING"
        assert dem_obj.geotools_link.status == "OK"


def test_windows_and_absolute_paths_from_older_files_keep_working() -> None:
    """Backslash // paths (saved on Windows) and absolute paths (older GeoTools) both resolve,
    and loading leaves them as they are."""
    _project()
    with _tmpdir() as tmp:
        site = tmp / "site"
        geojson, dem = _write_data(site / "data")
        blend = site / "site.blend"
        _save(blend)
        dig, dem_obj = _import_both(geojson, dem)
        # Stored as a Windows build or an older GeoTools would have, bypassing GeoTools'
        # update callbacks: what a .blend from there holds.
        dig.geotools_link.filepath = "//data\\dig.geojson"
        dem_obj.geotools_link.filepath = str(dem)
        profile = h.project().build_profiles[0]
        profile["output_path"] = "//out\\site.3tz"
        h.project().offset_file = "//data\\offset.txt"
        _save(blend)  # same folder: nothing is re-expressed
        _open(blend)

        dig, dem_obj = bpy.data.objects["dig"], bpy.data.objects["dem"]
        assert dig.geotools_link.filepath == "//data\\dig.geojson"
        assert dem_obj.geotools_link.filepath == str(dem)
        profile = h.project().build_profiles[0]
        assert profile.output_path == "//out\\site.3tz"
        links = _links()
        for obj in (dig, dem_obj):
            assert obj.geotools_link.status == "OK", (obj.name, obj.geotools_link.message)
        assert _same(links.link_path(dig.geotools_link), geojson)
        assert _same(links.link_path(dem_obj.geotools_link), dem)

        publish = _publish()
        output = publish.resolve_output(profile.output_path)
        assert _same(output, site / "out" / "site.3tz")
        assert "\\" not in str(output) or os.sep == "\\"
        assert publish.output_name(bpy.context.scene, profile) == "site.3tz"
        presentation = h.submodule("blender.presentation")
        assert presentation.variant_output_name(bpy.context.scene, "slope") == "site.slope.3tz"
        paths = h.submodule("core.paths")
        assert paths.name(h.project().offset_file) == "offset.txt"

        # GeoTools writes them in the portable form the next time it sets them.
        profile.output_path = profile.output_path
        assert profile.output_path == "//out/site.3tz"
        links.store_path(dig.geotools_link, links.link_path(dig.geotools_link))
        assert dig.geotools_link.filepath == "//data/dig.geojson"


def test_save_as_elsewhere_and_save_copy() -> None:
    """Saving to another folder re-expresses // paths for it; a copy leaves the open file alone."""
    _project()
    with _tmpdir() as tmp:
        site = tmp / "site"
        geojson, dem = _write_data(site / "data")
        _save(site / "site.blend")
        dig, _dem_obj = _import_both(geojson, dem)
        profile = h.project().build_profiles[0]
        profile.output_path = "//out/site.3tz"
        assert dig.geotools_link.filepath == "//data/dig.geojson"

        # A copy into a sub-folder: the copy points at the same files, the open file is unchanged.
        _save(site / "backup" / "copy.blend", copy=True)
        assert dig.geotools_link.filepath == "//data/dig.geojson"
        assert profile.output_path == "//out/site.3tz"

        # Save As into the data folder: the GeoJSON is now beside the .blend.
        _save(site / "data" / "site.blend")
        assert dig.geotools_link.filepath == "//dig.geojson"
        assert Path(profile.output_path).is_absolute()  # outside the new folder: absolute
        assert _same(profile.output_path, site / "out" / "site.3tz")
        assert _links().check_link(dig) == "OK"

        _open(site / "backup" / "copy.blend")
        dig = bpy.data.objects["dig"]
        assert _same(dig.geotools_link.filepath, geojson)
        assert dig.geotools_link.status == "OK"
        assert _same(h.project().build_profiles[0].output_path, site / "out" / "site.3tz")


def test_image_paths_with_backslashes_resolve() -> None:
    """GeoTools' own checks of an image file read // paths with either separator."""
    with _tmpdir() as tmp:
        _save(tmp / "site.blend")
        (tmp / "textures").mkdir()
        image = bpy.data.images.new("Painted", 4, 4)
        image.filepath_raw = "//textures/painted.png"
        image.file_format = "PNG"
        image.save()
        image.filepath_raw = "//textures\\painted.png"
        blend_paths = h.submodule("blender.blend_paths")
        assert _same(blend_paths.image_path(image), tmp / "textures" / "painted.png")
        layers = h.submodule("blender.layers")
        assert layers.image_is_saved(image)
        state = h.submodule("blender.texture_state")
        assert state.image_state(image).sha256 is not None  # found and hashed
