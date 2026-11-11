"""Vector imports in another CRS (reprojection) and into LOCAL and ANCHOR projects (M8)."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import _vector_fixtures as vf
import bpy
import numpy as np

E0, N0, A0 = vf.E0, vf.N0, 10.0
COLUMNS = {"context": "TEXT", "depth": "REAL", "phase": "INTEGER", "sealed": "BOOLEAN"}


@contextmanager
def _tmpdir() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="geotools-vector-crs-") as tmp:
        yield Path(tmp)


@contextmanager
def _without_pyproj() -> Iterator[None]:
    """Make ``core.reproject.available()`` say no, as in a Blender without pyproj."""
    module = h.submodule("core.reproject")
    original = module.available
    module.available = lambda: False
    try:
        yield
    finally:
        module.available = original


def _has_pyproj() -> bool:
    return h.submodule("core.reproject").available()


def _preset():
    return h.submodule("core.crs_presets").get("MGA2020_56")


def _project(mode: str = "SOURCE_CRS") -> None:
    h.reset_scene()
    h.submodule("blender.data_origin").clear_offer(bpy.context)
    props = h.project()
    props.placement_mode = mode
    props.source_crs = _preset().definition
    props.source_offset_e = repr(E0)
    props.source_offset_n = repr(N0)
    props.source_offset_a = repr(A0)


def _positions(mesh: bpy.types.Mesh) -> np.ndarray:
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def _transform(points: np.ndarray, source: str, target: str = "EPSG:7856") -> np.ndarray:
    """The reference, computed here with pyproj against the EPSG code."""
    import pyproj

    transformer = pyproj.Transformer.from_crs(source, target, always_xy=True)
    x, y = transformer.transform(points[:, 0], points[:, 1])
    return np.stack([x, y], axis=1)


# Three synthetic finds near (E0, N0), in WGS84 longitude and latitude.
LONLAT = np.array([[153.02035, -27.47466], [153.02092, -27.47488], [153.01995, -27.47436]])


def _wgs84_geojson(path: Path, extra: int = 0) -> tuple[Path, np.ndarray]:
    points = np.concatenate([LONLAT, LONLAT[:1] + 1e-4 * np.arange(1, extra + 1)[:, None]])
    features = [
        vf.feature({"type": "Point", "coordinates": [float(x), float(y)]}, {"k": i}, i + 1)
        for i, (x, y) in enumerate(points)
    ]
    return vf.write_geojson(path, features, crs=None), points


def _bump(path: Path) -> None:
    import os

    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns - 10_000_000_000))


# --- reprojection ---------------------------------------------------------------------------


def test_wgs84_geojson_into_an_mga56_project_is_reprojected() -> None:
    _project()
    with _tmpdir() as tmp:
        path, lonlat = _wgs84_geojson(tmp / "finds.geojson")
        result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(path))
        if not _has_pyproj():
            assert result == {"ERROR_REPORTED"}, result
            assert "Reprojection is not available" in error, error
            return
        assert result == {"FINISHED"}, error
        obj = bpy.data.objects["finds"]
        expected = _transform(lonlat, "EPSG:4326") - (E0, N0)
        got = _positions(obj.data).astype(np.float64)
        np.testing.assert_allclose(got[:, :2], expected, atol=1e-3)  # within a millimetre
        assert np.all(got[:, 2] == 0.0)  # 2D data: z = 0
        link = obj.geotools_link
        assert json.loads(link.crs)["epsg"] == "EPSG:4326"  # the data's CRS, as read
        options = json.loads(link.options)
        report = options["reprojection"]
        assert report["source"] == "EPSG:4326" and report["target"] == "EPSG:7856", report
        assert report["operation"] and "network" in report
        assert any("preset" in note for note in report["notes"]), report
        assert options["allow_grid_downloads"] is False


def test_gda94_gpkg_shifts_about_1_5_m() -> None:
    if not _has_pyproj():
        return
    _project()
    with _tmpdir() as tmp:
        ring = vf.square(E0, N0, 10.0)
        path = vf.write_gpkg(
            tmp / "gda94.gpkg",
            "old",
            [(1, {"type": "Polygon", "coordinates": [ring]}, {"context": "C1"})],
            {"context": "TEXT"},
            srs_id=28356,
            definition="undefined",
        )
        result, error = h.call_op(bpy.ops.geotools.import_gpkg, filepath=str(path))
        assert result == {"FINISHED"}, error
        got = _positions(bpy.data.objects["old"].data).astype(np.float64)[:, :2]
        corners = np.array(ring[:-1], dtype=np.float64)
        expected = _transform(corners, "EPSG:28356") - (E0, N0)
        order = np.lexsort(got.T[::-1])
        np.testing.assert_allclose(got[order], expected[np.lexsort(expected.T[::-1])], atol=1e-3)
        shift = np.hypot(*(expected[0] - (corners[0] - (E0, N0))))
        assert 1.4 < shift < 1.6, shift  # GDA94 to GDA2020 near Brisbane
        options = json.loads(bpy.data.objects["old"].geotools_link.options)
        assert options["reprojection"]["accuracy_m"] == 0.01, options


def test_reprojection_report_is_stored_and_reused_on_refresh() -> None:
    if not _has_pyproj():
        return
    _project()
    links = h.submodule("blender.links")
    with _tmpdir() as tmp:
        path, lonlat = _wgs84_geojson(tmp / "finds.geojson")
        _bump(path)
        assert bpy.ops.geotools.import_geojson(filepath=str(path)) == {"FINISHED"}
        obj = bpy.data.objects["finds"]
        before = _positions(obj.data).copy()
        stored = json.loads(obj.geotools_link.options)["reprojection"]

        _path, lonlat = _wgs84_geojson(path, extra=2)  # two more finds
        result = links.refresh_link(obj)
        assert result.added == 2 and result.removed == 0, result
        assert f"Reprojected as at import: {stored['operation']}" in result.message, result
        after = _positions(obj.data)
        np.testing.assert_array_equal(after[:3], before)  # the same features, the same place
        expected = _transform(lonlat, "EPSG:4326") - (E0, N0)
        np.testing.assert_allclose(after[:, :2].astype(np.float64), expected, atol=1e-3)
        assert json.loads(obj.geotools_link.options)["reprojection"] == stored


def test_without_pyproj_another_crs_is_refused() -> None:
    _project()
    with _tmpdir() as tmp, _without_pyproj():
        path, _lonlat = _wgs84_geojson(tmp / "finds.geojson")
        result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(path))
        assert result == {"ERROR_REPORTED"}
        assert "EPSG:4326" in error and "EPSG:7856" in error, error
        assert "Reprojection is not available" in error and "QGIS" in error, error
        assert not bpy.data.objects and not bpy.data.meshes
        # Data in the project CRS needs no pyproj.
        same = vf.write_geojson(tmp / "same.geojson", vf.contexts(1))
        result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(same))
        assert result == {"FINISHED"}, error


def test_grid_downloads_option() -> None:
    for op in (bpy.ops.geotools.import_geojson, bpy.ops.geotools.import_gpkg):
        prop = op.get_rna_type().properties["allow_grid_downloads"]
        assert prop.default is False, op


# --- LOCAL and ANCHOR projects -------------------------------------------------------------


def test_projected_geojson_in_a_local_project_offers_its_centre() -> None:
    data_origin = h.submodule("blender.data_origin")
    for mode in ("LOCAL", "ANCHOR"):
        _project(mode)
        with _tmpdir() as tmp:
            path = vf.write_geojson(tmp / "dig.geojson", vf.contexts(2))  # EPSG:7856
            result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(path))
            assert result == {"ERROR_REPORTED"}, mode
            assert "project origin" in error and "local metres" in error, error
            assert not bpy.data.objects
            pending = data_origin.pending_offer(bpy.context)
            assert pending["request"]["kind"] == "GEOJSON", pending
            assert pending["origin"]["offset"] == [E0 + 15.0, N0 + 15.0, 0.0], pending

            # The Linked Data panel shows the refusal with the one-click fix.
            calls = h.draw(h.submodule("blender.links_ui").GEOTOOLS_PT_linked_data)
            assert ("operator", "geotools.use_data_origin", "ORIENTATION_GLOBAL") in calls
            assert "project origin" in h.label_text(calls)

            assert not data_origin.needs_confirmation(bpy.context.scene)
            result, error = h.call_op(bpy.ops.geotools.use_data_origin, **pending["request"])
            assert result == {"FINISHED"}, error
            props = h.project()
            assert props.placement_mode == "SOURCE_CRS"
            assert props.source_crs == _preset().definition  # the preset for EPSG:7856
            read = h.submodule("blender.properties").read_number
            centre = (E0 + 15.0, N0 + 15.0, 0.0)  # the extent is E0..E0+30, N0..N0+30
            assert tuple(read(props, f"source_offset_{k}") for k in "ena") == centre
            obj = bpy.data.objects["dig"]
            got = _positions(obj.data)
            assert got[:, 0].min() == -15.0 and got[:, 0].max() == 15.0, got
            assert got[:, 1].min() == -15.0 and got[:, 1].max() == 15.0, got
            assert json.loads(obj.geotools_link.crs)["epsg"] == "EPSG:7856"
            assert data_origin.pending_offer(bpy.context) is None


def test_projected_gpkg_without_a_preset_and_the_confirmation() -> None:
    data_origin = h.submodule("blender.data_origin")
    _project("ANCHOR")
    collection = bpy.data.collections.new("Site")
    bpy.context.scene.collection.children.link(collection)
    for name in ("Ground", "Wall"):
        collection.objects.link(bpy.data.objects.new(name, bpy.data.meshes.new(name)))
    h.project().source_collection = collection
    with _tmpdir() as tmp:
        rows = [(f["id"], f["geometry"], f["properties"]) for f in vf.contexts(1)]
        path = vf.write_gpkg(tmp / "utm.gpkg", "contexts", rows, COLUMNS, srs_id=32756)
        result, error = h.call_op(bpy.ops.geotools.import_gpkg, filepath=str(path))
        assert result == {"ERROR_REPORTED"} and "an Anchor project" in error, error
        pending = data_origin.pending_offer(bpy.context)
        assert pending["request"]["layer"] == "contexts", pending

        assert data_origin.needs_confirmation(bpy.context.scene)
        text = h.label_text(h.draw(data_origin.GEOTOOLS_OT_use_data_origin))
        assert "changes how the existing scene is placed on Earth" in text, text
        assert "'Site' (2 objects)" in text and "from Anchor to EPSG:32756" in text, text

        result, error = h.call_op(bpy.ops.geotools.use_data_origin, **pending["request"])
        assert result == {"FINISHED"}, error
        props = h.project()
        assert props.placement_mode == "SOURCE_CRS" and props.source_crs == "EPSG:32756"
        assert "contexts" in bpy.data.objects
        # The project checks then say whether rusty-tiles takes EPSG:32756.


def test_local_project_keeps_small_or_unknown_data() -> None:
    _project("LOCAL")
    with _tmpdir() as tmp:
        small = vf.write_geojson(
            tmp / "small.geojson",
            [vf.feature({"type": "Point", "coordinates": [30.0, -40.0, 2.0]}, {}, 1)],
        )
        result, error = h.call_op(bpy.ops.geotools.import_geojson, filepath=str(small))
        assert result == {"FINISHED"}, error
        rows = [(f["id"], f["geometry"], f["properties"]) for f in vf.contexts(1)]
        unknown = vf.write_gpkg(
            tmp / "unknown.gpkg",
            "far",
            rows,
            COLUMNS,
            srs_id=-1,
            organization="NONE",
            definition="undefined",
        )
        result, error = h.call_op(bpy.ops.geotools.import_gpkg, filepath=str(unknown))
        assert result == {"FINISHED"}, error
        assert _positions(bpy.data.objects["far"].data)[:, 0].min() == np.float32(E0)
    assert h.project().placement_mode == "LOCAL"
    assert h.submodule("blender.data_origin").pending_offer(bpy.context) is None
