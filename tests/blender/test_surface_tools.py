"""Hydrology, contours and sections in Blender (``blender.hydrology``, ``blender.contours``).

Synthetic scenes only: a sloping terrain with a pit, a roof sheet above it and
a floating slab, for hydrology; a cone and a sphere for contours and sections.
"""

from __future__ import annotations

import contextlib
import json
import math
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import _harness as h
import bpy
import numpy as np

E0, N0, A0 = 500_000.0, 6_960_000.0, 100.0
JOIN_SECONDS = 300


@contextlib.contextmanager
def _tmpdir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-surface-"))
    try:
        yield path
    finally:
        with contextlib.suppress(Exception):
            h.submodule("blender.publish").shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


def _reset() -> None:
    h.reset_scene()
    for name in ("geotools_hydrology", "geotools_contours"):
        group = getattr(bpy.context.scene, name)
        for prop in group.bl_rna.properties:
            if prop.identifier not in {"rna_type", "name"}:
                group.property_unset(prop.identifier)
    for tree in list(bpy.data.node_groups):
        bpy.data.node_groups.remove(tree)


def _source() -> bpy.types.Collection:
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _mesh_object(name: str, pts, tris, collection=None) -> bpy.types.Object:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(np.asarray(pts).tolist(), [], np.asarray(tris).tolist())
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    (collection or bpy.context.scene.collection).objects.link(obj)
    return obj


def _grid(nx: int, ny: int, x0, x1, y0, y1, height):
    x = np.linspace(x0, x1, nx + 1)
    y = np.linspace(y0, y1, ny + 1)
    xx, yy = np.meshgrid(x, y, indexing="ij")
    pts = np.stack([xx.ravel(), yy.ravel(), height(xx.ravel(), yy.ravel())], axis=1)
    i, j = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    v0 = (i * (ny + 1) + j).ravel()
    v1, v3 = v0 + ny + 1, v0 + 1
    v2 = v1 + 1
    return pts, np.concatenate([np.stack([v0, v1, v2], 1), np.stack([v0, v2, v3], 1)])


def _box(lo, hi):
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    pts = [
        (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
        (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
    ]  # fmt: skip
    quads = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    tris = [t for q in quads for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))]
    return np.array(pts, float), np.array(tris)


PIT = (8.0, 22.0)


def _terrain_scene() -> dict[str, bpy.types.Object]:
    """A 30 x 30 m terrain sloping to x = 0 with a 1 m deep pit, an open roof sheet over
    it and a floating slab, each its own object in the source collection."""
    source = _source()

    def height(x, y):
        return 0.05 * x - 1.0 * np.exp(-((x - PIT[0]) ** 2 + (y - PIT[1]) ** 2) / 4.0)

    terrain = _mesh_object("Terrain", *_grid(30, 30, 0, 30, 0, 30, height), source)
    roof = _mesh_object(
        "Roof", *_grid(6, 6, 12, 18, 10, 16, lambda x, y: 8 + 0.3 * (y - 10)), source
    )
    slab = _mesh_object("Slab", *_box((20, 4, 4), (24, 8, 5)), source)
    return {"terrain": terrain, "roof": roof, "slab": slab}


def _faces(obj: bpy.types.Object, name: str, dtype=np.float32) -> np.ndarray:
    values = np.empty(len(obj.data.polygons), dtype)
    obj.data.attributes[name].data.foreach_get("value", values)
    return values


def _centres(obj: bpy.types.Object) -> np.ndarray:
    c = np.empty(3 * len(obj.data.polygons))
    obj.data.polygons.foreach_get("center", c)
    m = np.array(obj.matrix_world)
    return c.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3]


def _plan(obj: bpy.types.Object) -> np.ndarray:
    n = np.empty(3 * len(obj.data.polygons))
    obj.data.polygons.foreach_get("normal", n)
    a = np.empty(len(obj.data.polygons))
    obj.data.polygons.foreach_get("area", a)
    return np.maximum(n.reshape(-1, 3)[:, 2], 0) * a


def acc_item_max(acc: dict[str, np.ndarray]) -> float:
    return float(max(values.max() for values in acc.values()))


def _edge_attr(obj, name, dtype=np.float32) -> np.ndarray:
    values = np.empty(len(obj.data.edges), dtype)
    obj.data.attributes[name].data.foreach_get("value", values)
    return values


# --- registration --------------------------------------------------------------------


def test_surface_tools_registered() -> None:
    h.ensure_enabled()
    for idname in ("geotools.hydrology", "geotools.contours", "geotools.sections"):
        assert h.operator_available(idname), idname
    scene = bpy.context.scene
    assert scene.geotools_hydrology.accumulation_layer == "flow_accumulation"
    assert scene.geotools_contours.interval == "1"


# --- hydrology -----------------------------------------------------------------------


def test_hydrology_layers_drops_and_ponding() -> None:
    _reset()
    objs = _terrain_scene()
    props = bpy.context.scene.geotools_hydrology
    props.flow_lines = True
    props.channel_area = 50.0
    props.min_depth = 0.05
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None and result == {"FINISHED"}, error
    out = json.loads(props.result)
    assert out["faces"] == sum(len(o.data.polygons) for o in objs.values())
    assert out["stats"]["unrouted"] == 0

    layers = h.project().layers
    acc_item = layers["flow_accumulation"]
    assert (acc_item.domain, acc_item.data_type, acc_item.units) == ("FACE", "FLOAT", "m²")
    assert acc_item.presentation.scale == "LOG"
    assert acc_item.origin == "ANALYSIS" and json.loads(acc_item.provenance)["tool"] == "hydrology"
    assert layers["ponded_depth"].units == "m"
    dep_item = layers["depression"]
    assert dep_item.data_type == "CATEGORY"
    assert dep_item.categories.splitlines()[:2] == ["None", "Depression 1"]

    terrain, roof, slab = objs["terrain"], objs["roof"], objs["slab"]
    acc = {k: _faces(o, "gt_flow_accumulation") for k, o in objs.items()}
    # Every face drains at least its own plan area.
    for key, obj in objs.items():
        assert (acc[key] >= _plan(obj) * (1 - 1e-5) - 1e-6).all(), key
    # The roof's and the slab's water ends up on the terrain: its largest
    # accumulation (the outlet side) holds all of the plan area.
    total = sum(_plan(o).sum() for o in objs.values())
    assert acc["terrain"].max() > _plan(roof).sum() + _plan(slab).sum()
    assert out["max_accumulation"] <= total * (1 + 1e-6)

    # The pit ponds: depth and depression 1 there, none far from it.
    depth = _faces(terrain, "gt_ponded_depth")
    label = _faces(terrain, "gt_depression", np.int32)
    centres = _centres(terrain)
    near = np.hypot(centres[:, 0] - PIT[0], centres[:, 1] - PIT[1]) < 0.8
    far = np.hypot(centres[:, 0] - PIT[0], centres[:, 1] - PIT[1]) > 6
    assert (depth[near] > 0.1).all() and (label[near] == 1).all()
    assert not depth[far].any() and not label[far].any()
    assert out["depressions"] >= 1 and out["top"][0]["volume"] > 0

    # The depression points and flow paths, in the analysis collection.
    points = bpy.data.objects["Depressions"]
    lines = bpy.data.objects["Flow Paths"]
    collection = bpy.data.collections["GeoTools Analysis"]
    assert points.name in collection.objects and lines.name in collection.objects
    assert len(points.data.vertices) == out["depressions"]
    volume = np.empty(len(points.data.vertices), np.float32)
    points.data.attributes["volume"].data.foreach_get("value", volume)
    assert volume[0] == np.float32(out["top"][0]["volume"])
    assert len(lines.data.edges) > 0 and len(lines.data.polygons) == 0
    fid = _edge_attr(lines, "fid", np.int32)
    assert fid.min() == 1 and out["flow_lines"] == fid.max()

    # The accumulation presents on a log scale, from small faces to the largest.
    present = h.submodule("blender.presentation")
    presentation, record = present.build_presentation(bpy.context.scene, "flow_accumulation")
    assert record["scale"] == "LOG" and record["range"]["max"] == acc_item_max(acc)
    stops = [s["value"] for s in record["ramp_stops"]]
    assert np.allclose(np.diff(np.log10(stops)), np.log10(stops[1] / stops[0]))
    colours = presentation.colors["Terrain"]
    assert len(np.unique(colours[:, :3], axis=0)) > 5

    # Run again: the same objects are reused, not duplicated.
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None, error
    assert "Depressions.001" not in bpy.data.objects and "Flow Paths.001" not in bpy.data.objects


def test_hydrology_drops_land_where_blender_casts_them() -> None:
    _reset()
    objs = _terrain_scene()
    surface = h.submodule("blender.surface")
    hy = h.submodule("analysis.hydrology")
    arrays = surface.read_surface(bpy.context, list(objs.values()))
    graph = hy.surface_from_mesh(
        arrays.positions,
        arrays.face_starts,
        arrays.corner_verts,
        arrays.corner_edges,
        arrays.n_edges,
        flip=arrays.flip,
    )
    queries = hy.drop_queries(graph, 1e-3)
    lo, hi = hy.face_bounds(arrays.positions, arrays.face_starts, arrays.corner_verts)
    target = hy.faces_under(queries.origin, lo, hi)
    assert len(target) < graph.n
    down, down_p, up, _up_p = surface.cast_vertical(
        bpy.context,
        arrays.positions,
        arrays.face_starts,
        arrays.corner_verts,
        target,
        queries.origin,
    )
    parts = {p.obj.name: p for p in arrays.parts}
    terrain = parts["Terrain"]
    in_terrain = (down >= terrain.face_start) & (down < terrain.face_start + terrain.face_count)
    # The slab's underside drips onto the terrain straight below its centres.
    slab = parts["Slab"]
    from_slab = (queries.face >= slab.face_start) & queries.drip
    assert from_slab.sum() == 2
    assert in_terrain[from_slab].all()
    assert np.allclose(down_p[from_slab, :2], queries.origin[from_slab, :2], atol=1e-4)
    # The roof's open edges drop onto the terrain; the terrain's own edge drops miss.
    roof = parts["Roof"]
    from_roof = (queries.face >= roof.face_start) & (
        queries.face < roof.face_start + roof.face_count
    )
    assert in_terrain[from_roof].all()
    from_terrain = queries.face < terrain.face_count
    assert (down[from_terrain] < 0).all() and (up[from_terrain] < 0).all()
    # Nothing temporary is left behind.
    assert not [o for o in bpy.data.objects if o.name.startswith(surface.TEMP_PREFIX)]
    assert not [t for t in bpy.data.node_groups if t.name.startswith(surface.TEMP_PREFIX)]


def test_hydrology_skips_writing_where_modifiers_change_topology() -> None:
    _reset()
    objs = _terrain_scene()
    objs["roof"].modifiers.new("Subdivide", "SUBSURF").levels = 1
    result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is None and result == {"FINISHED"}, error
    out = json.loads(bpy.context.scene.geotools_hydrology.result)
    assert any("Roof" in note and "topology" in note for note in out["notes"])
    assert "gt_flow_accumulation" not in objs["roof"].data.attributes
    assert "gt_flow_accumulation" in objs["terrain"].data.attributes


def test_hydrology_errors() -> None:
    _reset()
    props = bpy.context.scene.geotools_hydrology
    _result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is not None and props.error and not props.result
    _terrain_scene()
    props.depth_layer = props.accumulation_layer
    _result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is not None and "different" in props.error
    props.property_unset("depth_layer")
    bpy.ops.geotools.layer_add()
    item = h.project().layers[h.project().layers_index]
    item.name = "ponded_depth"
    item.domain = "CORNER"
    _result, error = h.call_op(bpy.ops.geotools.hydrology)
    assert error is not None and "ponded_depth" in props.error


# --- contours and sections -----------------------------------------------------------


def _cone_scene() -> bpy.types.Object:
    source = _source()
    return _mesh_object(
        "Cone", *_grid(80, 80, -10, 10, -10, 10, lambda x, y: 10 - np.hypot(x, y)), source
    )


def _crs_project() -> None:
    props = h.project()
    props.placement_mode = "SOURCE_CRS"
    props.source_crs = "EPSG:7856"
    props.source_offset_e = repr(E0)
    props.source_offset_n = repr(N0)
    props.source_offset_a = repr(A0)


def test_contours_in_the_project_datum_and_exported() -> None:
    _reset()
    _crs_project()
    _cone_scene()
    props = bpy.context.scene.geotools_contours
    props.interval = "2.5"
    props.base = "0.25"  # levels at 100.25 + 2.5 k in RL
    result, error = h.call_op(bpy.ops.geotools.contours)
    assert error is None and result == {"FINISHED"}, error
    out = json.loads(props.contour_result)
    obj = bpy.data.objects["Contours"]
    assert obj.name in bpy.data.collections["GeoTools Analysis"].objects
    assert len(obj.data.polygons) == 0 and len(obj.data.edges) > 0
    elevation = _edge_attr(obj, "elevation")
    levels = np.unique(elevation)
    assert np.allclose((levels - 0.25) / 2.5, np.round((levels - 0.25) / 2.5))
    assert out["lowest"] >= A0 - 4.2 and out["highest"] <= A0 + 10 and out["datum"] == "RL"
    closed = _edge_attr(obj, "closed", bool)
    # Each closed contour is a circle at radius 110 - RL about the cone's axis.
    co = np.empty(3 * len(obj.data.vertices), np.float32)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    edges = np.empty(2 * len(obj.data.edges), np.int32)
    obj.data.edges.foreach_get("vertices", edges)
    edges = edges.reshape(-1, 2)
    for level in np.unique(elevation[closed]):
        verts = np.unique(edges[closed & (elevation == level)])
        r = np.hypot(co[verts, 0], co[verts, 1])
        assert np.abs(r - (A0 + 10 - level)).max() < 0.02, level
        assert np.allclose(co[verts, 2] + A0, level, atol=1e-4)

    vectors = h.submodule("core.vector.gpkg")
    kinds = h.submodule("core.vector.model").Kind
    with _tmpdir() as tmp:
        path = tmp / "contours.gpkg"
        result, error = h.call_op(
            bpy.ops.geotools.export_vector,
            filepath=str(path),
            file_format="GPKG",
            object_name="Contours",
            layer="contours",
        )
        assert error is None and result == {"FINISHED"}, error
        data = vectors.read_layer(path, "contours")
        assert data.n_rows == out["lines"]
        lines = data.geometries[kinds.LINES]
        z = lines.coords[:, 2]
        assert z.min() >= A0 - 4.2 and z.max() <= A0 + 10
        # Elevations are the exact decimal levels, and every vertex of a line is on it.
        column = data.columns["elevation"].values
        assert set(np.round((column - 0.25) / 2.5, 9)) <= set(np.arange(38.0, 45.0))
        # Lines: one ring per part; vertex -> ring -> part -> feature -> row.
        ring_part = np.repeat(np.arange(lines.n_parts), np.diff(lines.part_offsets))
        vertex_ring = np.repeat(np.arange(lines.n_rings), lines.ring_lengths())
        row = lines.rows[lines.part_feature()[ring_part[vertex_ring]]]
        assert np.allclose(z, column[row], atol=1e-4)
        assert lines.coords[:, 0].min() > E0 - 11 and lines.coords[:, 0].max() < E0 + 11


def test_sections_along_a_line_through_a_sphere() -> None:
    _reset()
    source = _source()
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=5.0)
    sphere = bpy.context.active_object
    for c in sphere.users_collection:
        c.objects.unlink(sphere)
    source.objects.link(sphere)
    line = _mesh_object("Line", [(-8, 0.5, 0), (0, 0.5, 0), (8, 0.5, 0)], [])
    line.data.edges.add(2)
    line.data.edges.foreach_set("vertices", [0, 1, 1, 2])
    line.data.update()
    props = bpy.context.scene.geotools_contours
    props.line = line
    result, error = h.call_op(bpy.ops.geotools.sections)
    assert error is None and result == {"FINISHED"}, error
    out = json.loads(props.section_result)
    assert out["pieces"] == 1 and out["sections"] == 1
    obj = bpy.data.objects["Sections"]
    co = np.empty(3 * len(obj.data.vertices), np.float32)
    obj.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    expected = math.sqrt(25 - 0.25)
    assert np.abs(np.hypot(co[:, 0], co[:, 2]) - expected).max() < 0.03
    chainage = np.empty(len(obj.data.vertices), np.float32)
    obj.data.attributes["chainage"].data.foreach_get("value", chainage)
    assert np.allclose(chainage, co[:, 0] + 8.0, atol=1e-4)
    start = _edge_attr(obj, "chainage_from")
    assert np.allclose(start, 8.0 - expected, atol=0.05)
    # Without a chosen line, the active object is the line; selected edges only.
    props.line = None
    bpy.context.view_layer.objects.active = line
    line.data.edges[1].select = False
    line.data.edges[0].select = True
    props.selected_edges = True
    result, error = h.call_op(bpy.ops.geotools.sections)
    assert error is None, error
    out = json.loads(props.section_result)
    # The west half of the ring: one open arc over the west side, cut at x = 0.
    assert out["pieces"] == 1, out
    assert abs(out["length"] - math.pi * expected) < 0.05
    assert "Sections.001" not in bpy.data.objects


def test_contour_and_section_errors() -> None:
    _reset()
    props = bpy.context.scene.geotools_contours
    _result, error = h.call_op(bpy.ops.geotools.contours)
    assert error is not None and props.contour_error
    _cone_scene()
    props.interval = "zero"
    _result, error = h.call_op(bpy.ops.geotools.contours)
    assert error is not None and "Interval" in props.contour_error
    props.interval = "0.0001"
    _result, error = h.call_op(bpy.ops.geotools.contours)
    assert error is not None and "levels" in props.contour_error
    bpy.context.view_layer.objects.active = None
    _result, error = h.call_op(bpy.ops.geotools.sections)
    assert error is not None and props.section_error


def test_contours_publish_as_vectors() -> None:
    if h.CONFIG.expect != "available":
        return
    _reset()
    _crs_project()
    _cone_scene()
    bpy.context.scene.geotools_contours.interval = "2"
    assert h.call_op(bpy.ops.geotools.contours) == ({"FINISHED"}, None)
    publish = h.submodule("blender.publish")
    with _tmpdir() as tmp:
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        props = h.project()
        profile = props.build_profiles[props.build_profile_index]
        profile.output_type = "VECTOR"
        profile.output_path = str(tmp / "contours.3tz")
        profile.vector_collection = bpy.data.collections["GeoTools Analysis"]
        profile.overwrite = True
        result, error = h.call_op(bpy.ops.geotools.publish)
        assert error is None and result == {"FINISHED"}, error
        job = publish.active_job()
        assert job.join(JOIN_SECONDS), "the publish job did not finish"
        publish.update_from_job()
        assert job.state.name == "DONE", job.error
        record = json.loads((tmp / "contours.geotools" / "build.json").read_text())
        assert record["validation"]["ok"] is True
        tables = {t["table"]: t for t in record["vectors"]["tables"]}
        assert tables["Contours_lines"]["kind"] == "lines", tables


# --- panels --------------------------------------------------------------------------


def test_surface_panels_draw() -> None:
    _reset()
    ui = h.submodule("blender.surface_ui")
    for cls in ui.classes:
        h.draw(cls)
    _terrain_scene()
    bpy.context.scene.geotools_hydrology.flow_lines = True
    assert h.call_op(bpy.ops.geotools.hydrology)[1] is None
    calls = h.draw(ui.GEOTOOLS_PT_analysis_hydrology)
    text = h.label_text(calls)
    assert "Geometric only" in text and "Depressions" in text
    bpy.context.scene.geotools_contours.interval = "0.5"
    assert h.call_op(bpy.ops.geotools.contours)[1] is None
    calls = h.draw(ui.GEOTOOLS_PT_analysis_contours)
    assert "closed" in h.label_text(calls)
