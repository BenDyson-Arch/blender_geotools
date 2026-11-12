"""Generate the drape-contexts example: a synthetic site, its contexts, and a .blend.

Runs inside Blender, against the source checkout (nothing is installed):

    blender --background --factory-startup --python scripts/make_example_drape.py -- \\
        [--out examples/drape-contexts]

It writes, into ``--out``:

- ``terrain.ply`` and ``terrain_offset.txt``: a synthetic terrain mesh (a 60 m
  square at 0.5 m spacing: a gentle slope, a low mound and some undulation),
  in local metres with an E/N/A offset file, the way a photogrammetry export
  with a coordinate shift arrives;
- ``contexts.geojson``: nine invented excavation contexts (pits, postholes, a
  ditch, a spread and a ring ditch with a hole) as 2D polygons in WGS 84 / UTM
  zone 31N (EPSG:32631, with the legacy ``crs`` member), each with
  ``context``, ``type``, ``phase``, ``depth_top`` and ``depth_bottom`` (metres
  below the ground surface);
- ``drape-contexts.blend``: the project placed in EPSG:32631 with the offset,
  the terrain in the Site source collection, ``contexts`` imported from the
  GeoJSON as a linked object, a Geometry Nodes modifier that drapes it on the
  terrain with stock nodes (Subdivide Mesh, Raycast, Set Position, Named
  Attribute), ``contexts.volumes`` extruding each context between its top and
  bottom levels (Object Info, Extrude Mesh, Flip Faces, Join Geometry, Merge by
  Distance), and build profiles for the site mesh and for the contexts as
  vector tiles.

Everything is synthetic: the site is invented, in open country, at round
numbers. Never put real site data in this example (see CONTRIBUTING.md).

The node groups are ordinary user node groups in the example file, built here
from stock nodes; GeoTools ships no node groups (the Scope decision in
docs/ROADMAP.md). docs/recipes/drape-contexts.md explains them.

The script needs only Blender: it registers GeoTools from the checkout for the
import and the project properties, and pyproj is not needed because the data
is already in the project CRS.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import numpy as np

# The invented site: WGS 84 / UTM zone 31N, round numbers in open farmland.
CRS = "EPSG:32631"
E0, N0, A0 = 500_000.0, 5_400_000.0, 100.0  # the project offset: the site's centre
SIZE = 60.0  # metres, terrain side
SPACING = 0.5  # metres between terrain vertices

DRAPE_GROUP = "Drape on Terrain"
VOLUME_GROUP = "Context Volumes"


# --- synthetic data (numpy only) -----------------------------------------------------


def terrain_height(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Local height (metres above A0) at local x, y: slope, a mound and undulation."""
    slope = 0.03 * x + 0.015 * y
    mound = 1.6 * np.exp(-((x - 8.0) ** 2 + (y + 6.0) ** 2) / (2 * 7.0**2))
    ripple = 0.12 * np.sin(x / 2.7) * np.cos(y / 3.9)
    return 2.0 + slope + mound + ripple


def terrain_grid() -> tuple[np.ndarray, np.ndarray]:
    """Vertices (n, 3) float32 and quads (m, 4) int32 of the terrain, centred on 0, 0."""
    n = round(SIZE / SPACING) + 1
    axis = np.linspace(-SIZE / 2, SIZE / 2, n)
    xs, ys = np.meshgrid(axis, axis)
    zs = terrain_height(xs, ys)
    co = np.stack([xs.ravel(), ys.ravel(), zs.ravel()], axis=1).astype(np.float32)
    i = np.arange(n - 1)
    a = (i[None, :] + n * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + n + 1, a + n], axis=1).astype(np.int32)
    return co, quads


def _wound(ring: list, counter_clockwise: bool) -> list:
    """``ring`` (closed) wound as RFC 7946 asks: exteriors anticlockwise, holes clockwise."""
    a = np.asarray(ring, dtype=np.float64)
    area = float(np.sum(a[:-1, 0] * a[1:, 1] - a[1:, 0] * a[:-1, 1]))
    return ring if (area > 0) == counter_clockwise else ring[::-1]


def circle(cx: float, cy: float, r: float, n: int = 16, squash: float = 1.0) -> list:
    t = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
    ring = [[cx + r * math.cos(a), cy + squash * r * math.sin(a)] for a in t]
    return [*ring, ring[0]]


def contexts_features() -> list[dict]:
    """Invented contexts, local metres from the site centre."""
    ditch = [[-24.0, 14.0], [-4.0, 20.0], [-3.2, 17.6], [-23.2, 11.6], [-24.0, 14.0]]
    spread = [
        [10.0, 12.0],
        [17.0, 10.5],
        [21.0, 15.0],
        [18.5, 21.0],
        [12.5, 21.5],
        [9.0, 17.0],
        [10.0, 12.0],
    ]
    ring_outer = circle(8.0, -6.0, 9.0, 32)
    ring_inner = circle(8.0, -6.0, 7.6, 32)
    rows = [
        ("C101", "pit", 1, 0.25, 1.10, [circle(-12.0, -4.0, 1.6)]),
        ("C102", "pit", 1, 0.30, 0.85, [circle(-7.5, -11.0, 1.2, squash=0.8)]),
        ("C103", "posthole", 2, 0.20, 0.55, [circle(-15.0, 4.0, 0.35, 12)]),
        ("C104", "posthole", 2, 0.20, 0.60, [circle(-13.0, 4.2, 0.35, 12)]),
        ("C105", "posthole", 2, 0.20, 0.50, [circle(-11.0, 4.4, 0.35, 12)]),
        ("C106", "ditch", 1, 0.30, 1.40, [ditch]),
        ("C107", "spread", 3, 0.05, 0.25, [spread]),
        ("C108", "ring ditch", 1, 0.35, 1.20, [ring_outer, ring_inner]),
        ("C109", "pit", 3, 0.25, 0.95, [circle(20.0, -18.0, 2.1, 20)]),
    ]
    features = []
    for fid, (name, kind, phase, top, bottom, rings) in enumerate(rows, start=1):
        rings = [_wound(ring, counter_clockwise=k == 0) for k, ring in enumerate(rings)]
        coords = [[[round(E0 + x, 3), round(N0 + y, 3)] for x, y in ring] for ring in rings]
        features.append(
            {
                "type": "Feature",
                "id": fid,
                "properties": {
                    "context": name,
                    "type": kind,
                    "phase": phase,
                    "depth_top": top,
                    "depth_bottom": bottom,
                },
                "geometry": {"type": "Polygon", "coordinates": coords},
            }
        )
    return features


def write_geojson(path: Path) -> None:
    data = {
        "type": "FeatureCollection",
        "name": "contexts",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::32631"}},
        "features": contexts_features(),
    }
    path.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")


def write_offset_file(path: Path) -> None:
    path.write_text(f"E: {E0:.3f}\nN: {N0:.3f}\nA: {A0:.3f}\n", encoding="utf-8")


# --- Blender ----------------------------------------------------------------------------


def _mesh(name: str, co: np.ndarray, faces: np.ndarray):
    import bpy

    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(faces.size)
    mesh.loops.foreach_set("vertex_index", faces.ravel())
    mesh.polygons.add(len(faces))
    sides = faces.shape[1]
    mesh.polygons.foreach_set("loop_start", np.arange(0, faces.size, sides, dtype=np.int32))
    mesh.update()
    mesh.validate()
    return mesh


def _material(name: str, rgba: tuple[float, float, float, float]):
    import bpy

    material = bpy.data.materials.new(name)  # node-based by default in Blender 5
    bsdf = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    bsdf.inputs["Base Color"].default_value = rgba
    bsdf.inputs["Roughness"].default_value = 1.0
    material.diffuse_color = rgba
    return material


def _socket(tree, name: str, in_out: str, socket_type: str, description: str = ""):
    socket = tree.interface.new_socket(name, in_out=in_out, socket_type=socket_type)
    socket.description = description
    return socket


def drape_group():
    """Subdivide Mesh -> Raycast onto the terrain -> Set Position -> offset down by a depth."""
    import bpy

    tree = bpy.data.node_groups.new(DRAPE_GROUP, "GeometryNodeTree")
    tree.description = (
        "Drapes polygons on a terrain object with stock nodes, then lowers them by a depth "
        "attribute (metres below the surface). From the GeoTools drape-contexts recipe"
    )
    _socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    terrain = _socket(
        tree, "Terrain", "INPUT", "NodeSocketObject", "The surface to drape on (the site mesh)"
    )
    depth = _socket(
        tree,
        "Depth Attribute",
        "INPUT",
        "NodeSocketString",
        "Face attribute with the depth below the surface, in metres (positive down)",
    )
    depth.default_value = "depth_top"
    subdiv = _socket(
        tree,
        "Subdivisions",
        "INPUT",
        "NodeSocketInt",
        "Subdivide first, so the faces follow the terrain between their corners",
    )
    subdiv.default_value, subdiv.min_value, subdiv.max_value = 3, 0, 6
    _socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")
    _ = terrain

    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-1000, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (900, 0)

    subdivide = nodes.new("GeometryNodeSubdivideMesh")
    subdivide.location = (-750, 100)
    links.new(group_in.outputs["Geometry"], subdivide.inputs["Mesh"])
    links.new(group_in.outputs["Subdivisions"], subdivide.inputs["Level"])

    info = nodes.new("GeometryNodeObjectInfo")
    info.location = (-750, -150)
    info.transform_space = "RELATIVE"
    links.new(group_in.outputs["Terrain"], info.inputs["Object"])

    position = nodes.new("GeometryNodeInputPosition")
    position.location = (-950, -350)
    separate = nodes.new("ShaderNodeSeparateXYZ")
    separate.location = (-750, -350)
    links.new(position.outputs["Position"], separate.inputs["Vector"])
    start = nodes.new("ShaderNodeCombineXYZ")
    start.location = (-550, -350)
    start.label = "Ray Start (10 km up)"
    links.new(separate.outputs["X"], start.inputs["X"])
    links.new(separate.outputs["Y"], start.inputs["Y"])
    start.inputs["Z"].default_value = 10_000.0

    raycast = nodes.new("GeometryNodeRaycast")
    raycast.location = (-300, -150)
    links.new(info.outputs["Geometry"], raycast.inputs["Target Geometry"])
    links.new(start.outputs["Vector"], raycast.inputs["Source Position"])
    raycast.inputs["Ray Direction"].default_value = (0.0, 0.0, -1.0)
    raycast.inputs["Ray Length"].default_value = 20_000.0

    onto = nodes.new("GeometryNodeSetPosition")
    onto.location = (0, 100)
    onto.label = "Onto the Terrain"
    links.new(subdivide.outputs["Mesh"], onto.inputs["Geometry"])
    links.new(raycast.outputs["Is Hit"], onto.inputs["Selection"])
    links.new(raycast.outputs["Hit Position"], onto.inputs["Position"])

    named = nodes.new("GeometryNodeInputNamedAttribute")
    named.location = (0, -200)
    named.data_type = "FLOAT"
    links.new(group_in.outputs["Depth Attribute"], named.inputs["Name"])
    negate = nodes.new("ShaderNodeMath")
    negate.location = (200, -200)
    negate.operation = "MULTIPLY"
    negate.inputs[1].default_value = -1.0
    links.new(named.outputs["Attribute"], negate.inputs[0])
    down = nodes.new("ShaderNodeCombineXYZ")
    down.location = (400, -200)
    links.new(negate.outputs["Value"], down.inputs["Z"])

    lower = nodes.new("GeometryNodeSetPosition")
    lower.location = (600, 100)
    lower.label = "Down by the Depth"
    links.new(onto.outputs["Geometry"], lower.inputs["Geometry"])
    links.new(down.outputs["Vector"], lower.inputs["Offset"])
    links.new(lower.outputs["Geometry"], group_out.inputs["Geometry"])
    return tree


def volume_group():
    """Object Info (the draped tops) -> Extrude Mesh down to the bottom level -> closed."""
    import bpy

    tree = bpy.data.node_groups.new(VOLUME_GROUP, "GeometryNodeTree")
    tree.description = (
        "Closed volumes between each context's top and bottom levels, from the draped tops "
        "of another object. From the GeoTools drape-contexts recipe"
    )
    _socket(tree, "Geometry", "INPUT", "NodeSocketGeometry")
    _socket(tree, "Tops", "INPUT", "NodeSocketObject", "The object with the draped tops")
    top = _socket(tree, "Top Attribute", "INPUT", "NodeSocketString", "Depth of the top")
    top.default_value = "depth_top"
    bottom = _socket(tree, "Bottom Attribute", "INPUT", "NodeSocketString", "Depth of the bottom")
    bottom.default_value = "depth_bottom"
    _socket(tree, "Geometry", "OUTPUT", "NodeSocketGeometry")

    nodes, links = tree.nodes, tree.links
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-900, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (900, 0)

    info = nodes.new("GeometryNodeObjectInfo")
    info.location = (-650, 150)
    info.transform_space = "RELATIVE"
    links.new(group_in.outputs["Tops"], info.inputs["Object"])

    named_top = nodes.new("GeometryNodeInputNamedAttribute")
    named_top.location = (-650, -150)
    named_top.data_type = "FLOAT"
    links.new(group_in.outputs["Top Attribute"], named_top.inputs["Name"])
    named_bottom = nodes.new("GeometryNodeInputNamedAttribute")
    named_bottom.location = (-650, -350)
    named_bottom.data_type = "FLOAT"
    links.new(group_in.outputs["Bottom Attribute"], named_bottom.inputs["Name"])
    thickness = nodes.new("ShaderNodeMath")
    thickness.location = (-400, -250)
    thickness.operation = "SUBTRACT"
    thickness.label = "Thickness"
    links.new(named_bottom.outputs["Attribute"], thickness.inputs[0])
    links.new(named_top.outputs["Attribute"], thickness.inputs[1])

    # Offset is a field that defaults to the face normal, so the direction must be
    # linked in: a value typed into the socket is not used.
    down = nodes.new("ShaderNodeCombineXYZ")
    down.location = (-400, 0)
    down.label = "Straight Down"
    down.inputs["Z"].default_value = -1.0
    extrude = nodes.new("GeometryNodeExtrudeMesh")
    extrude.location = (-150, 100)
    extrude.mode = "FACES"
    links.new(info.outputs["Geometry"], extrude.inputs["Mesh"])
    links.new(down.outputs["Vector"], extrude.inputs["Offset"])
    links.new(thickness.outputs["Value"], extrude.inputs["Offset Scale"])
    extrude.inputs["Individual"].default_value = False

    # Extruding against the face normals leaves the walls and the moved faces
    # facing inwards: flip them, then close the top with the original tops.
    flip = nodes.new("GeometryNodeFlipFaces")
    flip.location = (100, 100)
    links.new(extrude.outputs["Mesh"], flip.inputs["Mesh"])
    join = nodes.new("GeometryNodeJoinGeometry")
    join.location = (350, 50)
    links.new(info.outputs["Geometry"], join.inputs["Geometry"])
    links.new(flip.outputs["Mesh"], join.inputs["Geometry"])
    merge = nodes.new("GeometryNodeMergeByDistance")
    merge.location = (600, 50)
    merge.inputs["Distance"].default_value = 0.0005
    links.new(join.outputs["Geometry"], merge.inputs["Geometry"])
    links.new(merge.outputs["Geometry"], group_out.inputs["Geometry"])
    return tree


def _set_modifier_input(modifier, tree, name: str, value) -> None:
    """Set a Geometry Nodes modifier input by its socket name (Blender 5.2 API)."""
    item = next(i for i in tree.interface.items_tree if i.in_out == "INPUT" and i.name == name)
    getattr(modifier.properties.inputs, item.identifier).value = value


def build_blend(out: Path) -> Path:
    import bpy

    import geotools
    from geotools.blender import properties

    bpy.ops.wm.read_homefile(use_empty=True)
    bpy.context.preferences.filepaths.save_version = 0  # no drape-contexts.blend1
    try:
        geotools.register()
    except ValueError:  # already registered (run twice in one session)
        pass
    scene = bpy.context.scene
    props = scene.geotools
    props.placement_mode = "SOURCE_CRS"
    props.crs = CRS
    for attr, value in (("origin_e", E0), ("origin_n", N0), ("source_offset_a", A0)):
        properties.write_number(props, attr, value)
    scene.unit_settings.system = "METRIC"

    # The site mesh: the source collection, as Ingest Source would leave it.
    site = bpy.data.collections.new("Site")
    scene.collection.children.link(site)
    props.source_collection = site
    co, quads = terrain_grid()
    terrain = bpy.data.objects.new("terrain", _mesh("terrain", co, quads))
    terrain.data.materials.append(_material("Ground", (0.42, 0.45, 0.33, 1.0)))
    site.objects.link(terrain)
    bpy.context.view_layer.objects.active = terrain
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj is terrain)
    bpy.ops.wm.ply_export(
        filepath=str(out / "terrain.ply"),
        export_selected_objects=True,
        apply_modifiers=False,
        export_normals=False,
        export_uv=False,
        export_colors="NONE",
        ascii_format=False,
        forward_axis="Y",
        up_axis="Z",
    )
    write_offset_file(out / "terrain_offset.txt")

    # The contexts: imported from the GeoJSON as a linked object.
    contexts_collection = bpy.data.collections.new("Contexts")
    scene.collection.children.link(contexts_collection)
    layer = bpy.context.view_layer.layer_collection.children[contexts_collection.name]
    bpy.context.view_layer.active_layer_collection = layer
    geojson_path = out / "contexts.geojson"
    result = bpy.ops.geotools.import_geojson(filepath=str(geojson_path))
    if result != {"FINISHED"}:
        raise RuntimeError(f"the GeoJSON import failed: {result}")
    contexts = bpy.data.objects["contexts"]
    contexts.data.materials.append(_material("Context", (0.85, 0.42, 0.12, 1.0)))
    drape = contexts.modifiers.new("Drape", "NODES")
    drape.node_group = drape_group()
    _set_modifier_input(drape, drape.node_group, "Terrain", terrain)

    # The volumes: their own object, so the draped tops above still export as polygons.
    volumes_collection = bpy.data.collections.new("Volumes")
    scene.collection.children.link(volumes_collection)
    volumes = bpy.data.objects.new("contexts.volumes", bpy.data.meshes.new("contexts.volumes"))
    volumes.data.materials.append(_material("Fill", (0.45, 0.27, 0.15, 1.0)))
    volumes_collection.objects.link(volumes)
    extrude = volumes.modifiers.new("Volumes", "NODES")
    extrude.node_group = volume_group()
    _set_modifier_input(extrude, extrude.node_group, "Tops", contexts)

    # Build profiles: the site mesh, and the draped contexts as vector tiles.
    bpy.ops.geotools.build_profile_add()
    site_profile = props.build_profiles[-1]
    site_profile.name = "Site"
    site_profile.output_path = "//site.3tz"
    site_profile.overwrite = True
    bpy.ops.geotools.build_profile_add()
    vector_profile = props.build_profiles[-1]
    vector_profile.name = "Contexts"
    vector_profile.output_type = "VECTOR"
    vector_profile.output_path = "//contexts.3tz"
    vector_profile.vector_collection = contexts_collection
    vector_profile.overwrite = True
    props.build_profile_index = len(props.build_profiles) - 1

    # Frame the site in the viewport for whoever opens the file.
    for obj in bpy.context.view_layer.objects:
        obj.select_set(obj is contexts)
    bpy.context.view_layer.objects.active = contexts

    blend = out / "drape-contexts.blend"
    # Saving stores the link relative to the .blend (//contexts.geojson, see
    # CONTRIBUTING.md "File paths"), so the example works wherever it is checked out.
    bpy.ops.wm.save_as_mainfile(filepath=str(blend), compress=True, relative_remap=True)
    link = contexts.geotools_link.filepath
    assert link == "//contexts.geojson", f"the link was stored as {link!r}"
    return blend


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "examples" / "drape-contexts",
        help="output directory (default: examples/drape-contexts)",
    )
    args = parser.parse_args(argv)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    write_geojson(out / "contexts.geojson")
    blend = build_blend(out)
    for path in sorted(out.iterdir()):
        if path.is_file():
            print(f"{path.stat().st_size / 1024:8.1f} KiB  {path.name}")
    print(f"Wrote {blend}")


if __name__ == "__main__":
    main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
