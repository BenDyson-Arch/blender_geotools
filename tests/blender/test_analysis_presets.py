"""M5 analysis presets: the bundled Geometry Nodes library and Add Analysis.

Each preset is added with the operator (one click, as the menu does) to an
analytic surface, and its layer is read from the evaluated mesh: a plane
tilted 30° (slope, aspect, rugosity, TRI, TPI), a sphere and a bowl
(curvature sign and magnitude), a flat plane (sky-view factor and openness)
and a V-trench (sky-view factor and openness against the exact horizon).
The bundled .blend must match ``scripts/make_analysis_presets.py``.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import _harness as h
import bpy
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "make_analysis_presets.py"
LIBRARY = REPO_ROOT / "geotools" / "assets" / "analysis_presets.blend"
TAN30 = math.tan(math.radians(30.0))


def _ap():
    return h.submodule("blender.analysis_presets")


def _catalog():
    return h.submodule("analysis.presets")


def _layers():
    return h.submodule("blender.layers")


def _clear_trees() -> None:
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)
    for library in list(bpy.data.libraries):
        bpy.data.libraries.remove(library)


def _reset() -> None:
    h.reset_scene()
    _clear_trees()
    collection = bpy.data.collections.new("Site")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection


def _grid(name: str, n: int, size: float, height, *, select: bool = True) -> bpy.types.Object:
    """An n x n quad grid of side ``size`` centred on 0, z = height(x, y), in the source."""
    axis = np.linspace(-size / 2, size / 2, n + 1)
    xs, ys = np.meshgrid(axis, axis)
    co = np.stack([xs.ravel(), ys.ravel(), height(xs, ys).ravel()], axis=1)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.astype(np.float32).ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    h.project().source_collection.objects.link(obj)
    if select:
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
    return obj


def _select(*objects: bpy.types.Object) -> None:
    bpy.context.view_layer.update()
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj in objects)
    bpy.context.view_layer.objects.active = objects[0]


def _add(preset: str, **kwargs) -> set[str]:
    return bpy.ops.geotools.analysis_add(preset=preset, **kwargs)


def _values(obj: bpy.types.Object, layer: str) -> tuple[np.ndarray, np.ndarray]:
    """The layer's evaluated values on ``obj`` and the position of each (face centre or corner)."""
    L = _layers()
    scene = bpy.context.scene
    spec = L.spec_from_props(L.find_layer(scene, layer))
    ((_, mesh),) = list(L.layer_meshes(scene, [obj]))
    values = L.read_layer(mesh, spec)
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if spec.domain == "FACE":
        where = np.empty(len(mesh.polygons) * 3, np.float32)
        mesh.polygons.foreach_get("center", where)
        return values, where.reshape(-1, 3)
    loops = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", loops)
    return values, co[loops]


def _inside(where: np.ndarray, half: float) -> np.ndarray:
    return (np.abs(where[:, 0]) < half) & (np.abs(where[:, 1]) < half)


def _node_group_names() -> list[str]:
    return sorted(g.name for g in bpy.data.node_groups)


# --- the library --------------------------------------------------------------------------


def test_presets_registered() -> None:
    assert h.operator_available("geotools.analysis_add")
    assert hasattr(bpy.types, "GEOTOOLS_MT_analysis_presets")
    assert hasattr(bpy.types, "GEOTOOLS_PT_analysis_presets")


def test_library_is_bundled_and_up_to_date() -> None:
    installed = _ap().LIBRARY_PATH
    assert installed.is_file(), installed
    assert installed.read_bytes() == LIBRARY.read_bytes(), "installed library is not the repo's"
    assert installed.stat().st_size < 512 * 1024, installed.stat().st_size
    assert (installed.parent / "blender_assets.cats.txt").is_file()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    done = subprocess.run(
        [
            bpy.app.binary_path,
            "--background",
            "--factory-startup",
            "--python-exit-code",
            "1",
            "--python",
            str(SCRIPT),
            "--",
            "--check",
            "--out",
            str(LIBRARY),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    assert "is up to date" in done.stdout


def test_check_catches_a_stale_library() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="geotools-presets-"))
    try:
        stale = tmp / "analysis_presets.blend"
        shutil.copy(LIBRARY, stale)
        shutil.copy(LIBRARY.parent / "blender_assets.cats.txt", tmp)
        # Change one default in a copy of the library.
        with bpy.data.libraries.load(str(stale), link=False) as (src, dst):
            dst.node_groups = list(src.node_groups)
        group = bpy.data.node_groups["GeoTools Slope"]
        group.interface.items_tree["Smoothing"].default_value = 3
        bpy.data.libraries.write(str(stale), set(bpy.data.node_groups), compress=True)
        _clear_trees()
        done = subprocess.run(
            [
                bpy.app.binary_path,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                "1",
                "--python",
                str(SCRIPT),
                "--",
                "--check",
                "--out",
                str(stale),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 1, done.stdout[-3000:]
        assert "out of date" in done.stdout and "GeoTools Slope" in done.stdout
    finally:
        _clear_trees()
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_preset_is_a_marked_asset_with_the_catalog_inputs() -> None:
    _clear_trees()
    catalog = _catalog()
    with bpy.data.libraries.load(str(_ap().LIBRARY_PATH), link=False) as (src, dst):
        dst.node_groups = list(src.node_groups)
    try:
        for preset in catalog.PRESETS:
            group = bpy.data.node_groups[preset.group]
            assert group.asset_data is not None and group.is_modifier, preset.id
            assert group[catalog.PRESET_KEY] == preset.id
            assert group[catalog.VERSION_KEY] == preset.version
            inputs = {
                i.name: i
                for i in group.interface.items_tree
                if i.item_type == "SOCKET" and i.in_out == "INPUT"
            }
            assert inputs[catalog.ATTRIBUTE_INPUT].default_value == preset.attribute
            for spec in preset.inputs:
                default = inputs[spec.name].default_value
                assert math.isclose(default, spec.default, rel_tol=1e-6), (preset.id, spec.name)
            # Stock nodes only: every group node is another group of the library.
            for node in group.nodes:
                if node.bl_idname == "GeometryNodeGroup":
                    assert node.node_tree.get(catalog.PRESET_KEY), node.node_tree.name
                else:
                    assert not node.bl_idname.startswith("GeometryNodeCustom"), node.bl_idname
    finally:
        _clear_trees()


# --- adding a preset ---------------------------------------------------------------------------


def test_slope_and_aspect_on_a_tilted_plane() -> None:
    _reset()
    scene = bpy.context.scene
    L = _layers()
    plane = _grid("Tilted", 20, 4.0, lambda x, y: y * TAN30)  # rises to +Y: faces south

    assert _add("slope") == {"FINISHED"}
    item = L.find_layer(scene, "slope")
    spec = L.spec_from_props(item)
    assert (spec.domain, spec.data_type, spec.units) == ("FACE", "FLOAT", "deg")
    assert (spec.origin, spec.aggregation, spec.label) == ("GEONODES", "AREA_MEAN", "Slope")
    assert spec.source_attribute == ""  # the default gt_slope
    parameters = spec.provenance["parameters"]
    assert spec.provenance["tool"] == "analysis_preset"
    assert (parameters["preset"], parameters["preset_version"]) == ("slope", 1)
    assert parameters["node_group"] == "GeoTools Slope"
    assert parameters["inputs"] == {"Smoothing": 0}
    assert parameters["objects"] == ["Tilted"]
    assert (item.presentation.palette, item.presentation.range_max) == ("viridis", "90.0")
    assert scene.geotools.layers[scene.geotools.layers_index].name == "slope"
    # The range was read from the evaluated mesh; nothing was written to the mesh.
    assert abs(float(item.value_min) - 30.0) < 1e-3 and abs(float(item.value_max) - 30.0) < 1e-3
    assert L.layer_status(scene, item) == []
    assert "gt_slope" not in plane.data.attributes
    values, _ = _values(plane, "slope")
    np.testing.assert_allclose(values, 30.0, atol=1e-3)

    assert _add("slope_percent") == {"FINISHED"}
    np.testing.assert_allclose(_values(plane, "slope_percent")[0], 100 * TAN30, rtol=1e-5)

    assert _add("aspect") == {"FINISHED"}
    np.testing.assert_allclose(_values(plane, "aspect")[0], 180.0, atol=1e-3)
    spec = L.spec_from_props(L.find_layer(scene, "aspect"))
    assert spec.aggregation == "NONE" and spec.provenance["parameters"]["north"] == "grid"

    # True north in an anchor project: +Y is the heading, so convergence = -heading.
    props = h.project()
    props.placement_mode = "ANCHOR"
    p_ = h.submodule("blender.properties")
    p_.write_number(props, "anchor_lon", 153.0)
    p_.write_number(props, "anchor_lat", -27.5)
    p_.write_number(props, "anchor_height", 10.0)
    p_.write_number(props, "heading", 10.0)
    assert _add("aspect", name="aspect_true", north="TRUE") == {"FINISHED"}
    np.testing.assert_allclose(_values(plane, "aspect_true")[0], 170.0, atol=1e-3)
    spec = L.spec_from_props(L.find_layer(scene, "aspect_true"))
    assert spec.provenance["parameters"]["north"] == "true"
    assert spec.provenance["parameters"]["inputs"]["Convergence"] == -10.0
    # Both aspect layers share one node group, on two modifiers.
    assert _node_group_names().count("GeoTools Aspect") == 1
    assert len(_ap().preset_modifiers(plane)) == 4

    # Flat faces have no aspect (NaN, the FLOAT no-data).
    flat = _grid("Flat", 4, 1.0, lambda x, y: 0.0 * x)
    assert _add("aspect") == {"FINISHED"}
    assert np.isnan(_values(flat, "aspect")[0]).all()
    _clear_trees()


def test_mga_true_north_uses_the_site_convergence() -> None:
    if h.CONFIG.expect != "available":
        return  # needs pyproj
    import pyproj

    _reset()
    east, north = pyproj.Transformer.from_crs("EPSG:7844", "EPSG:7856", always_xy=True).transform(
        150.0, -30.0
    )
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = h.submodule("core.crs_presets").get("MGA2020_56").definition
    props.source_offset_e = repr(east)
    props.source_offset_n = repr(north)
    props.source_offset_a = "250.0"
    plane = _grid("Tilted", 8, 2.0, lambda x, y: y * TAN30)
    assert _add("aspect", north="TRUE") == {"FINISHED"}
    expected = _ap().convergence(bpy.context.scene, plane)[0]
    assert 1.4 < expected < 1.6, expected  # +1.50° at the zone's west edge
    np.testing.assert_allclose(_values(plane, "aspect")[0], 180.0 + expected, atol=1e-3)
    _clear_trees()


def test_readding_never_duplicates() -> None:
    _reset()
    a = _grid("A", 6, 1.0, lambda x, y: 0.1 * x)
    b = _grid("B", 6, 1.0, lambda x, y: 0.1 * y, select=False)
    assert _add("curvature_profile") == {"FINISHED"}
    groups = _node_group_names()
    assert groups == [
        "GeoTools Profile Curvature",
        "GeoTools Surface Derivatives",
        "GeoTools Window Heights",
    ], groups
    # Again on the same object: same group, same modifier.
    assert _add("curvature_profile") == {"FINISHED"}
    assert _node_group_names() == groups and len(a.modifiers) == 1
    # Another preset sharing the helpers appends only its own group.
    assert _add("curvature_plan") == {"FINISHED"}
    assert _node_group_names() == sorted([*groups, "GeoTools Plan Curvature"])
    plan = bpy.data.node_groups["GeoTools Plan Curvature"]
    helper = next(n for n in plan.nodes if n.bl_idname == "GeometryNodeGroup")
    assert helper.node_tree == bpy.data.node_groups["GeoTools Surface Derivatives"]
    # A renamed (or edited) group is still the preset's group.
    bpy.data.node_groups["GeoTools Profile Curvature"].name = "My Curvature"
    _select(a, b)
    assert _add("curvature_profile") == {"FINISHED"}
    assert "GeoTools Profile Curvature" not in _node_group_names()
    assert b.modifiers[0].node_group.name == "My Curvature" and len(a.modifiers) == 2
    item = _layers().find_layer(bpy.context.scene, "curvature_profile")
    assert _layers().spec_from_props(item).provenance["parameters"]["objects"] == ["A", "B"]
    assert not bpy.data.libraries, list(bpy.data.libraries)
    _clear_trees()


def test_refuses_a_name_taken_by_another_layer() -> None:
    _reset()
    obj = _grid("A", 2, 1.0, lambda x, y: 0.0 * x)
    assert bpy.ops.geotools.layer_add(name="slope") == {"FINISHED"}
    result, error = h.call_op(bpy.ops.geotools.analysis_add, preset="slope")
    assert error is not None and "already exists" in error, (result, error)
    assert not obj.modifiers and not bpy.data.node_groups
    result, error = h.call_op(bpy.ops.geotools.analysis_add, preset="slope", name="Bad Name")
    assert error is not None and not obj.modifiers
    assert _add("slope", name="slope_2") == {"FINISHED"}
    assert _ap().get_input(obj.modifiers[0], "Attribute") == "gt_slope_2"
    _clear_trees()


def test_needs_a_selected_mesh() -> None:
    _reset()
    bpy.context.view_layer.objects.active = None
    assert not bpy.ops.geotools.analysis_add.poll()
    obj = _grid("A", 2, 1.0, lambda x, y: 0.0 * x)
    obj.select_set(False)
    bpy.context.view_layer.objects.active = obj
    assert bpy.ops.geotools.analysis_add.poll()  # the active mesh counts


# --- correctness on analytic surfaces ---------------------------------------------------


def test_neighbourhood_presets_on_a_tilted_plane() -> None:
    _reset()
    plane = _grid("Tilted", 60, 6.0, lambda x, y: y * TAN30)
    for preset in ("rugosity", "tri", "tpi", "local_relief", "curvature_mean"):
        assert _add(preset) == {"FINISHED"}, preset

    values, _ = _values(plane, "rugosity")
    np.testing.assert_allclose(values, 1.0 / math.cos(math.radians(30.0)), rtol=1e-5)

    values, where = _values(plane, "tri")  # Riley: six neighbours at +-W tan 30
    inside = _inside(where, 2.8)
    np.testing.assert_allclose(values[inside], 0.05 * TAN30 * math.sqrt(6.0), rtol=1e-4)
    assert np.isnan(values[~_inside(where, 2.96)]).all()  # the window leaves the mesh

    values, where = _values(plane, "tpi")  # Outer Radius 0.5
    np.testing.assert_allclose(values[_inside(where, 2.4)], 0.0, atol=1e-5)
    values, where = _values(plane, "local_relief")  # Radius 2
    np.testing.assert_allclose(values[_inside(where, 0.9)], 0.0, atol=1e-5)
    values, where = _values(plane, "curvature_mean")
    np.testing.assert_allclose(values[_inside(where, 2.8)], 0.0, atol=1e-3)
    _clear_trees()


def test_curvature_of_a_sphere_and_a_bowl() -> None:
    _reset()
    radius = 5.0
    dome = _grid("Dome", 200, 2.0, lambda x, y: np.sqrt(radius**2 - x**2 - y**2))
    bowl = _grid("Bowl", 200, 2.0, lambda x, y: -np.sqrt(radius**2 - x**2 - y**2), select=False)
    _select(dome, bowl)
    for kind in ("profile", "plan", "mean"):
        assert _add(f"curvature_{kind}") == {"FINISHED"}
        for obj, sign in ((dome, 1.0), (bowl, -1.0)):
            values, where = _values(obj, f"curvature_{kind}")
            inside = _inside(where, 0.85) & ~(np.hypot(where[:, 0], where[:, 1]) < 0.02)
            np.testing.assert_allclose(values[inside], sign / radius, rtol=0.01, err_msg=kind)
    spec = _layers().spec_from_props(_layers().find_layer(bpy.context.scene, "curvature_plan"))
    assert (spec.domain, spec.units) == ("CORNER", "1/m")
    _clear_trees()


def test_sky_view_factor_and_openness_on_a_plane() -> None:
    _reset()
    flat = _grid("Flat", 40, 8.0, lambda x, y: 0.0 * x)
    for preset in ("sky_view_factor", "openness_positive", "openness_negative"):
        assert _add(preset) == {"FINISHED"}
    svf, where = _values(flat, "sky_view_factor")
    inside = _inside(where, 1.9)  # Max Distance 2 from the 4 m edge
    np.testing.assert_allclose(svf, 1.0, atol=1e-6)  # never anything above level
    resolution = 90.0 / 2 ** (8 - 1)  # Steps 8
    for layer in ("openness_positive", "openness_negative"):
        values, _ = _values(flat, layer)
        assert np.all(np.abs(values[inside] - 90.0) <= resolution / 2 + 1e-3), layer
        assert values.max() > 120.0  # the edge: nothing within reach outwards
    _clear_trees()


def test_sky_view_factor_of_a_v_trench() -> None:
    _reset()
    alpha = math.radians(30.0)
    trench = _grid("Trench", 60, 6.0, lambda x, y: np.abs(x) * math.tan(alpha))
    for preset in ("sky_view_factor", "openness_positive", "openness_negative"):
        assert _add(preset) == {"FINISHED"}
    azimuths = np.arange(16) * 2 * math.pi / 16  # Directions 16, from +Y
    horizon = np.arctan(np.abs(np.sin(azimuths)) * math.tan(alpha))  # walls at alpha
    resolution = math.radians(90.0 / 2 ** (8 - 1))

    svf, where = _values(trench, "sky_view_factor")
    bottom = (np.abs(where[:, 0]) < 1e-4) & (np.abs(where[:, 1]) < 0.9)
    assert bottom.sum() > 50
    expected = 1.0 - np.mean(np.sin(horizon))
    assert np.all(np.abs(svf[bottom] - expected) < 0.006), (svf[bottom].min(), expected)
    # Higher up the wall more sky is seen.
    wall = (np.abs(where[:, 0] - 1.0) < 1e-4) & (np.abs(where[:, 1]) < 0.9)
    assert svf[wall].min() > svf[bottom].max()

    positive, _ = _values(trench, "openness_positive")
    negative, _ = _values(trench, "openness_negative")
    mean_horizon = math.degrees(np.mean(horizon))
    assert np.all(np.abs(positive[bottom] - (90.0 - mean_horizon)) < math.degrees(resolution))
    # From below, the lowest surface in each direction is the same wall line.
    assert np.all(np.abs(negative[bottom] - (90.0 + mean_horizon)) < math.degrees(resolution))
    _clear_trees()


# --- layers, export and the panel -------------------------------------------------------------


def test_preset_layer_survives_export() -> None:
    _reset()
    scene = bpy.context.scene
    a = _grid("A", 4, 2.0, lambda x, y: y * TAN30)
    b = _grid("B", 4, 2.0, lambda x, y: 0.5 * x, select=False)
    b.location.x = 5.0
    _select(a, b)
    assert _add("slope") == {"FINISHED"}
    bpy.context.view_layer.update()
    present = h.submodule("blender.presentation")
    ramps = h.submodule("core.ramps")
    presentation, record = present.build_presentation(scene, "slope")
    assert (record["range"]["min"], record["range"]["max"]) == (0.0, 90.0)
    slope_b = math.degrees(math.atan(0.5))
    for name, value in (("A", 30.0), ("B", slope_b)):
        expected = ramps.apply_ramp(np.full(16, value, np.float32), 0.0, 90.0, "viridis")
        np.testing.assert_array_equal(presentation.colors[name], expected)
    tmp = Path(tempfile.mkdtemp(prefix="geotools-presets-export-"))
    try:
        # The exporter checks the colours against each evaluated mesh (ExportError
        # on a mismatch), so this is the layer reaching COLOR_0 on export.
        export, topology = h.submodule("blender.export").extract_scene(
            bpy.context, h.project().source_collection, tmp, presentation=presentation
        )
        assert export.primitives and set(topology) == {"A", "B"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # build.json records the layer with its provenance.
    records, invalid = _layers().layer_records(scene)
    assert not invalid
    (record,) = records
    assert record["attribute"] == "gt_slope" and record["domain"] == "FACE"
    assert record["provenance"]["parameters"]["preset"] == "slope"
    if h.CONFIG.expect == "available":
        _publish_variant("slope")
    _clear_trees()


def _publish_variant(layer: str) -> None:
    """Publish ``site.<layer>.3tz`` (the roadmap exit: a preset publishes as a variant)."""
    import json

    import rusty_tiles

    props = h.project()
    props.placement_mode = "ANCHOR"
    p_ = h.submodule("blender.properties")
    p_.write_number(props, "anchor_lon", 153.02)
    p_.write_number(props, "anchor_lat", -27.47)
    p_.write_number(props, "anchor_height", 10.0)
    publish = h.submodule("blender.publish")
    tmp = Path(tempfile.mkdtemp(prefix="geotools-presets-variant-"))
    try:
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        profile = props.build_profiles[0]
        profile.output_path = str(tmp / "site.3tz")
        profile.profile = "LOSSLESS"
        assert bpy.ops.geotools.publish(layer=layer) == {"FINISHED"}
        job = publish.active_job()
        assert job is not None and job.join(180.0), "the publish job did not finish"
        assert publish.update_from_job() is None
        assert job.state.name == "DONE", job.error
        output = tmp / f"site.{layer}.3tz"
        assert rusty_tiles.validate(output)["ok"]
        build = json.loads((tmp / f"site.{layer}.geotools" / "build.json").read_text())
        assert build["variant"]["layer"] == layer
        assert build["geotools"]["apply_modifiers"] is True
        (record,) = build["layers"]
        assert record["provenance"]["parameters"]["preset"] == layer
    finally:
        publish.shutdown_job()
        shutil.rmtree(tmp, ignore_errors=True)


def test_panel_and_menu_draw() -> None:
    _reset()
    panel = bpy.types.GEOTOOLS_PT_analysis_presets
    calls = h.draw(panel)
    assert ("menu", "GEOTOOLS_MT_analysis_presets", "ADD") in calls
    assert "Adds a Geometry Nodes modifier" in h.label_text(calls)
    obj = _grid("A", 2, 1.0, lambda x, y: 0.0 * x)
    assert _add("slope") == {"FINISHED"}
    bpy.context.view_layer.objects.active = obj
    assert "Slope: gt_slope" in h.label_text(h.draw(panel))
    calls = h.draw(bpy.types.GEOTOOLS_MT_analysis_presets)
    operators = [c for c in calls if c[0] == "operator"]
    assert len(operators) == len(_catalog().PRESETS) + 1  # aspect twice: grid and true north
    assert "Terrain Shape" in h.label_text(calls)
    _clear_trees()
