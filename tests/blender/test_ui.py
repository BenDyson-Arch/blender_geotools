"""The sidebar as a whole: tooltips on everything it draws, and on every operator.

Each panel is drawn against the stub layout in several scene states (each
placement mode, a source with face, category and texture layers, every build
profile format, results present), and every property and operator it draws
must have a description, which Blender shows as the tooltip.
"""

from __future__ import annotations

import _harness as h
import bpy


def _source() -> bpy.types.Object:
    """A one-object source collection with a UV map."""
    mesh = bpy.data.meshes.new("ui_mesh")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.uv_layers.new(name="UVMap")
    obj = bpy.data.objects.new("ui_obj", mesh)
    collection = bpy.data.collections.new("ui_source")
    bpy.context.scene.collection.children.link(collection)
    collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    h.project().source_collection = collection
    return obj


def _layers(obj: bpy.types.Object) -> None:
    layers = h.submodule("blender.layers")
    core = h.submodule("core.layers")
    scene = bpy.context.scene
    for name, domain, data_type in (
        ("slope", "FACE", "FLOAT"),
        ("cover", "CORNER", "CATEGORY"),
        ("shade", "TEXTURE", "FLOAT"),
    ):
        categories = ("bare", "grass") if data_type == "CATEGORY" else ()
        spec = core.LayerSpec(
            name, core.Domain(domain), core.DataType(data_type), categories=categories
        )
        item = layers.ensure_layer(scene, spec)
        if domain == "TEXTURE":
            item.resolution = 4
            layers.ensure_layer_image(item, obj)


def _states():
    """Set up each state in turn; yield its name once the scene is ready."""
    for mode in ("LOCAL", "ANCHOR", "SOURCE_CRS"):
        h.reset_scene()
        h.project().placement_mode = mode
        yield f"empty {mode}"
    h.reset_scene()
    obj = _source()
    _layers(obj)
    project = h.project()
    for _ in range(3):
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    analysis = bpy.context.scene.geotools_analysis
    analysis.sun_hours.insolation = True
    inspect = bpy.context.scene.geotools_inspect
    inspect.tiles.add()
    for index, output_type in enumerate(("3TZ", "GLB", "VECTOR")):
        project.build_profiles[index].output_type = output_type
    for index in range(len(project.layers)):
        project.layers_index = index
        for profile in range(len(project.build_profiles)):
            project.build_profile_index = profile
            yield f"layer {index}, profile {profile}"
    # A last build, so Last Build (Extract for Web, Preview) draws for each format.
    for profile in range(len(project.build_profiles)):
        project.build_profile_index = profile
        item = project.build_profiles[profile]
        item.last_output = "//ui_site.glb" if item.output_type == "GLB" else "//ui_site.3tz"
        yield f"last build, profile {profile}"
    for tz_mode, period, scope, view, mask, source in (
        ("OFFSET", "RANGE", "DEPTH", "DEPTH", "IMAGE", "FILE"),
        ("ZONE", "YEAR", "LEAVES", "SIDE", "KEY", "LAST"),
    ):
        analysis.sun.tz_mode = tz_mode
        analysis.sun_hours.period = period
        inspect.scope, inspect.view, inspect.source = scope, view, source
        bpy.context.scene.geotools_texture.mask_source = mask
        yield f"options {tz_mode}"


def _description(data: object, prop: str) -> str:
    return data.bl_rna.properties[prop].description.strip()


def test_every_drawn_property_and_operator_has_a_tooltip() -> None:
    panels = h.submodule("blender.panels").classes
    missing_props: dict[str, str] = {}
    operators: set[str] = set()
    drawn = 0
    for state in _states():
        for cls in panels:
            if "poll" in cls.__dict__ and not cls.poll(bpy.context):
                continue
            calls = h.draw(cls)
            for data, prop in h.PROPS_DRAWN:
                drawn += 1
                if not _description(data, prop):
                    key = f"{data.bl_rna.identifier}.{prop}"
                    missing_props.setdefault(key, f"{key} ({cls.bl_label}, {state})")
            operators.update(idname for kind, idname, _icon in calls if kind == "operator")
    assert drawn > 150, drawn
    assert not missing_props, "\n".join(sorted(missing_props.values()))
    no_tooltip = []
    for idname in sorted(operators):
        module, name = idname.split(".")
        if not getattr(getattr(bpy.ops, module), name).get_rna_type().description.strip():
            no_tooltip.append(idname)
    assert not no_tooltip, no_tooltip
    h.reset_scene()


def test_every_operator_and_its_options_have_tooltips() -> None:
    """Every GeoTools operator, and every option it shows in a dialog or redo panel."""
    missing = []
    names = [n for n in dir(bpy.ops.geotools) if not n.startswith("_")]
    assert len(names) > 60, names
    for name in names:
        rna = getattr(bpy.ops.geotools, name).get_rna_type()
        if not rna.description.strip():
            missing.append(f"geotools.{name}")
        for prop in rna.properties:
            if prop.identifier == "rna_type" or prop.is_hidden:
                continue
            if prop.identifier in {"filepath", "directory", "files", "filter_glob"}:
                continue  # the file browser's own
            if not prop.description.strip():
                missing.append(f"geotools.{name}: {prop.identifier}")
    assert not missing, "\n".join(missing)


def test_operator_labels_are_title_case() -> None:
    """Buttons are in Title Case: no lower-case first letter in any word but the small ones."""
    small = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to"}
    wrong = []
    for name in dir(bpy.ops.geotools):
        if name.startswith("_"):
            continue
        label = getattr(bpy.ops.geotools, name).get_rna_type().name
        words = label.replace("/", " ").split()
        for i, word in enumerate(words):
            if word[0].isalpha() and word[0].islower() and (i == 0 or word not in small):
                wrong.append(label)
                break
    assert not wrong, wrong
