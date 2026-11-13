"""M6 texture cleanup: paint setup, cleanup tools, dirty tracking, saving, the publish loop.

All images are synthetic (gradients, flat colours, checkers), never site imagery.
Background Blender runs no timers, so publishing waits with ``job.join()``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

import _harness as h
import bpy
import numpy as np

JOIN_SECONDS = 300.0


def _available() -> bool:
    return h.CONFIG.expect == "available"


def _cleanup():
    return h.submodule("blender.texture_cleanup")


def _state():
    return h.submodule("blender.texture_state")


def _settings():
    return bpy.context.scene.geotools_texture


@contextmanager
def _tmpdir():
    path = Path(tempfile.mkdtemp(prefix="geotools-texture-test-"))
    try:
        yield path
    finally:
        h.submodule("blender.publish").shutdown_job()
        shutil.rmtree(path, ignore_errors=True)


# --- fixtures ------------------------------------------------------------------------------


def _source_collection() -> bpy.types.Collection:
    collection = bpy.data.collections.get("Source")
    if collection is None:
        collection = bpy.data.collections.new("Source")
        bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    return collection


def _image(name: str, pixels: np.ndarray, *, pack: bool = True) -> bpy.types.Image:
    """A byte image holding ``pixels`` ``(H, W, 4)``, packed so Checks accept it."""
    height, width = pixels.shape[:2]
    image = bpy.data.images.new(name, width, height, alpha=True)
    image.pixels.foreach_set(np.ascontiguousarray(pixels, np.float32).ravel())
    if pack:
        image.pack()
    return image


def _gradient(size: int, *, tint=(1.0, 1.0, 1.0)) -> np.ndarray:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    rgba = np.ones((size, size, 4), np.float32)
    rgba[..., 0] = (0.15 + 0.7 * x / size) * tint[0]
    rgba[..., 1] = (0.15 + 0.7 * y / size) * tint[1]
    rgba[..., 2] = 0.4 * tint[2]
    return rgba


def _material(name: str, image: bpy.types.Image) -> bpy.types.Material:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    return material


def _grid(
    name: str,
    n: int,
    material: bpy.types.Material,
    *,
    origin=(0.0, 0.0),
    size=1.0,
    uv_box=(0.1, 0.1, 0.9, 0.9),
    height=0.0,
) -> bpy.types.Object:
    """An n x n grid of quads, ``size`` across, with UVs spread over ``uv_box``."""
    xs, ys = np.meshgrid(np.linspace(0, 1, n + 1), np.linspace(0, 1, n + 1))
    co = np.stack(
        [origin[0] + size * xs.ravel(), origin[1] + size * ys.ravel(), height * np.sin(xs.ravel())],
        axis=1,
    ).astype(np.float32)
    i = np.arange(n)
    a = (i[None, :] + (n + 1) * i[:, None]).ravel()
    quads = np.stack([a, a + 1, a + n + 2, a + n + 1], axis=1).astype(np.int32)
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(co))
    mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(quads.size)
    mesh.loops.foreach_set("vertex_index", quads.ravel())
    mesh.polygons.add(len(quads))
    mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
    u0, v0, u1, v1 = uv_box
    uv = np.stack([xs.ravel() * (u1 - u0) + u0, ys.ravel() * (v1 - v0) + v0], axis=1)
    layer = mesh.uv_layers.new(name="UVMap")
    layer.data.foreach_set("uv", uv[quads.ravel()].astype(np.float32).ravel())
    mesh.update()
    mesh.validate()
    mesh.materials.append(material)
    obj = bpy.data.objects.new(name, mesh)
    _source_collection().objects.link(obj)
    return obj


def _activate(obj: bpy.types.Object) -> None:
    for other in list(bpy.context.view_layer.objects):
        if other is not None:
            other.select_set(False)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)


def _pixels(image: bpy.types.Image) -> np.ndarray:
    return _cleanup().read_pixels(image)


def _anchor() -> None:
    props = h.project()
    props.placement_mode = "ANCHOR"
    p = h.submodule("blender.properties")
    p.write_number(props, "anchor_lon", 153.02)
    p.write_number(props, "anchor_lat", -27.47)
    p.write_number(props, "anchor_height", 10.0)


def _object_mode() -> None:
    obj = bpy.context.view_layer.objects.active
    if obj is not None and obj.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")


def _reset() -> None:
    _object_mode()
    h.reset_scene()
    _cleanup().begin_undo("")
    settings = _settings()
    for prop in settings.bl_rna.properties:
        name = prop.identifier
        if name in {"rna_type", "name"}:
            continue
        if prop.type == "COLLECTION":
            getattr(settings, name).clear()
        elif not prop.is_readonly:
            settings.property_unset(name)
    paint = bpy.context.scene.tool_settings.image_paint
    paint.canvas = None
    paint.stencil_image = None
    paint.use_stencil_layer = False


# --- registration --------------------------------------------------------------------------


def test_texture_registered() -> None:
    for idname in (
        "geotools.texture_paint_setup",
        "geotools.texture_fill_holes",
        "geotools.texture_remove_masked",
        "geotools.texture_new_mask",
        "geotools.texture_pad_islands",
        "geotools.texture_cleanup_undo",
        "geotools.texture_balance_fit",
        "geotools.texture_balance_apply",
        "geotools.texture_balance_clear",
    ):
        assert h.operator_available(idname), idname
    assert hasattr(bpy.context.scene, "geotools_texture")


# --- paint setup ---------------------------------------------------------------------------


def _uv_texel(image: bpy.types.Image, u: float, v: float) -> tuple[int, int]:
    """(row, column) of the texel at UV (u, v)."""
    width, height = image.size
    return int(v * height), int(u * width)


def test_paint_setup_masks_overlaps() -> None:
    _reset()
    image = _image("Albedo", _gradient(64))
    material = _material("Albedo", image)
    left = _grid("Left", 4, material, uv_box=(0.1, 0.1, 0.6, 0.6))
    _grid("Right", 4, material, origin=(2.0, 0.0), uv_box=(0.4, 0.4, 0.9, 0.9))
    _activate(left)
    assert bpy.ops.geotools.texture_paint_setup() == {"FINISHED"}
    paint = bpy.context.scene.tool_settings.image_paint
    assert left.mode == "TEXTURE_PAINT"
    assert paint.mode == "IMAGE" and paint.canvas == image
    assert paint.use_occlude and paint.use_backface_culling and paint.use_normal_falloff
    settings = _settings()
    # The two objects share texels where their UV boxes meet: [0.4, 0.6] squared.
    assert 100 < settings.overlap_texels < 250, settings.overlap_texels
    assert "painting one place changes another" in settings.overlap_note
    mask = paint.stencil_image
    assert mask is not None and paint.use_stencil_layer
    assert mask.name == "GeoTools overlaps Albedo"
    assert left.data.uv_layer_stencil.name == "UVMap"
    px = _pixels(mask)
    assert px[_uv_texel(mask, 0.5, 0.5)][0] == 0.0  # overlapping: protected
    assert px[_uv_texel(mask, 0.2, 0.2)][0] == 1.0  # used once: paintable
    assert px[_uv_texel(mask, 0.95, 0.05)][0] == 1.0  # unused
    panels = h.submodule("blender.texture_ui")
    labels = h.label_text(h.draw(panels.GEOTOOLS_PT_textures_paint))
    assert "painting one place changes another" in labels, labels

    # Without the overlap the stencil and its mask go away.
    _object_mode()
    bpy.data.objects.remove(bpy.data.objects["Right"])
    _activate(left)
    assert bpy.ops.geotools.texture_paint_setup() == {"FINISHED"}
    assert settings.overlap_texels == 0
    assert paint.stencil_image is None and not paint.use_stencil_layer
    assert bpy.data.images.get("GeoTools overlaps Albedo") is None
    _object_mode()


def test_paint_setup_without_protection_keeps_mask_visible_only() -> None:
    _reset()
    image = _image("Albedo", _gradient(32))
    material = _material("Albedo", image)
    left = _grid("Left", 2, material, uv_box=(0.1, 0.1, 0.6, 0.6))
    _grid("Right", 2, material, origin=(2.0, 0.0), uv_box=(0.4, 0.4, 0.9, 0.9))
    _activate(left)
    assert bpy.ops.geotools.texture_paint_setup(protect_overlaps=False) == {"FINISHED"}
    paint = bpy.context.scene.tool_settings.image_paint
    assert not paint.use_stencil_layer
    assert _settings().overlap_texels > 0
    assert bpy.data.images.get("GeoTools overlaps Albedo") is not None
    _object_mode()


# --- cleanup tools -------------------------------------------------------------------------


def _textured(size: int = 64, uv_box=(0.1, 0.1, 0.9, 0.9), n: int = 8, background=True):
    """A grid whose image is a gradient inside its UV box and black outside it."""
    rgba = _gradient(size)
    image = _image("Albedo", rgba, pack=False)
    material = _material("Albedo", image)
    obj = _grid("Site", n, material, uv_box=uv_box)
    coverage, _ = _cleanup().image_coverage(bpy.context, image)
    inside = coverage.inside
    if background:
        rgba[~inside, :3] = 0.0
    image.pixels.foreach_set(rgba.ravel())
    image.pack()
    _activate(obj)
    return obj, image, _gradient(size), inside


def test_fill_holes_inside_islands_only() -> None:
    _reset()
    _obj, image, truth, inside = _textured()
    px = _pixels(image)
    y, x = np.mgrid[0:64, 0:64]
    hole = (x - 32) ** 2 + (y - 30) ** 2 <= 36
    px[hole, :3] = 0.0
    image.pixels.foreach_set(px.ravel())
    image.pack()  # clean, so the fill must make it dirty
    assert not image.is_dirty
    assert bpy.ops.geotools.texture_fill_holes() == {"FINISHED"}
    assert image.is_dirty
    after = _pixels(image)
    err = np.abs(after[hole, :3] - truth[hole, :3]).max()
    assert err < 0.04, err
    # The background outside the islands is never read or written.
    assert (after[~inside, :3] == 0.0).all()
    grown = h.submodule("core.texture_cleanup").grow_mask(hole, _settings().grow)
    assert np.array_equal(after[inside & ~grown], px[inside & ~grown])
    panels = h.submodule("blender.texture_ui")
    calls = h.draw(panels.GEOTOOLS_PT_textures)
    assert ("operator", "geotools.texture_cleanup_undo", "LOOP_BACK") in calls, calls
    assert bpy.ops.geotools.texture_cleanup_undo() == {"FINISHED"}
    assert np.array_equal(_pixels(image), px)
    assert not bpy.ops.geotools.texture_cleanup_undo.poll()
    assert bpy.ops.geotools.texture_fill_holes() == {"FINISHED"}
    # Nothing left to fill.
    result, error = h.call_op(bpy.ops.geotools.texture_fill_holes)
    assert result == {"CANCELLED"}, (result, error)


def test_loading_a_file_drops_the_undo_copies() -> None:
    """Undo Cleanup never writes one file's pixels over a same-named image in another."""
    cleanup = _cleanup()
    assert cleanup._on_load_pre in bpy.app.handlers.load_pre
    _reset()
    _obj, image, _truth, _inside = _textured()
    px = _pixels(image)
    px[28:34, 28:34, :3] = 0.0
    image.pixels.foreach_set(px.ravel())
    assert bpy.ops.geotools.texture_fill_holes() == {"FINISHED"}
    assert bpy.ops.geotools.texture_cleanup_undo.poll()
    assert cleanup._coverage
    try:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()
        other = bpy.data.images.new("Albedo", 64, 64, alpha=True)
        before = _pixels(other)
        assert not bpy.ops.geotools.texture_cleanup_undo.poll()
        assert cleanup.undo_label() == "" and not cleanup._coverage
        assert cleanup.undo_cleanup() == []
        assert np.array_equal(_pixels(other), before) and not other.is_dirty
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()


def test_fill_holes_transparent() -> None:
    _reset()
    _obj, image, truth, _inside = _textured(background=False)
    px = _pixels(image)
    px[20:24, 40:44, 3] = 0.0
    px[20:24, 40:44, :3] = 0.9  # colour without alpha is no data
    image.pixels.foreach_set(px.ravel())
    settings = _settings()
    settings.holes_black = False
    settings.grow = 0
    assert bpy.ops.geotools.texture_fill_holes() == {"FINISHED"}
    after = _pixels(image)
    assert (after[20:24, 40:44, 3] == 1.0).all()
    assert np.abs(after[20:24, 40:44, :3] - truth[20:24, 40:44, :3]).max() < 0.04


def test_remove_masked_key_colour_and_mask_image() -> None:
    _reset()
    _obj, image, truth, _inside = _textured(background=False)
    px = _pixels(image)
    px[20:28, 30:40, :3] = (1.0, 0.0, 1.0)  # painted over in the key colour
    image.pixels.foreach_set(px.ravel())
    settings = _settings()
    assert settings.mask_source == "KEY"
    assert bpy.ops.geotools.texture_remove_masked() == {"FINISHED"}
    after = _pixels(image)
    key = _cleanup().key_mask(after, (1.0, 0.0, 1.0), settings.key_tolerance)
    assert not key.any()
    assert np.abs(after[20:28, 30:40, :3] - truth[20:28, 30:40, :3]).max() < 0.05

    # A separate mask image, painted white where to remove.
    assert bpy.ops.geotools.texture_new_mask() == {"FINISHED"}
    mask = settings.mask_image
    assert mask is not None and settings.mask_source == "IMAGE"
    assert tuple(mask.size) == tuple(image.size)
    assert bpy.context.scene.tool_settings.image_paint.canvas == mask
    settings.image = image  # the tools keep working on the texture
    m = np.zeros((64, 64, 4), np.float32)
    m[..., 3] = 1.0
    m[40:46, 12:20, :3] = 1.0
    mask.pixels.foreach_set(m.ravel())
    before = _pixels(image)
    before[40:46, 12:20, :3] = (0.0, 1.0, 0.0)  # something to remove
    image.pixels.foreach_set(before.ravel())
    assert bpy.ops.geotools.texture_remove_masked() == {"FINISHED"}
    after = _pixels(image)
    assert np.abs(after[40:46, 12:20, :3] - truth[40:46, 12:20, :3]).max() < 0.05
    grown = np.zeros((64, 64), bool)
    grown[38:48, 10:22] = True
    assert np.array_equal(after[~grown], before[~grown])
    # The wrong size is refused.
    settings.mask_image = _image("Small", np.ones((8, 8, 4), np.float32))
    result, error = h.call_op(bpy.ops.geotools.texture_remove_masked)
    assert error and "not 64x64" in error, (result, error)


def test_pad_islands_never_writes_inside() -> None:
    _reset()
    _obj, image, _truth, inside = _textured(uv_box=(0.25, 0.25, 0.75, 0.75), n=4)
    before = _pixels(image)
    settings = _settings()
    settings.pad_margin = 4
    assert bpy.ops.geotools.texture_pad_islands() == {"FINISHED"}
    after = _pixels(image)
    assert np.array_equal(after[inside], before[inside])
    rows = np.flatnonzero(inside.any(axis=1))
    cols = np.flatnonzero(inside.any(axis=0))
    r0, c0 = int(rows[0]), int(cols[0])
    mid = int(cols.mean())
    # Below the island, the padding repeats the island's edge colour, 4 texels deep.
    for d in (1, 4):
        assert np.abs(after[r0 - d, mid, :3] - before[r0, mid, :3]).max() < 0.02
    assert (after[r0 - 6, mid, :3] == 0.0).all()  # beyond the margin: untouched
    assert (after[r0 - 6, c0 - 6, :3] == 0.0).all()
    settings.pad_fill_rest = True
    assert bpy.ops.geotools.texture_pad_islands() == {"FINISHED"}
    after = _pixels(image)
    assert not (after[..., :3] == 0.0).all(axis=2).any()  # no background left
    assert np.array_equal(after[inside], before[inside])


# --- balance -------------------------------------------------------------------------------


def _true_linear(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """A smooth linear-light colour field over the world, varying in every channel."""
    return np.stack(
        [0.05 + 0.15 * x + 0.25 * y, 0.1 + 0.1 * x + 0.4 * (1 - y), 0.1 + 0.1 * x + 0.3 * y],
        axis=-1,
    )


def _page(name: str, x0: float, gain, size: int = 64) -> bpy.types.Image:
    """A page showing the world field over x in [x0, x0 + 1] on UVs [0.1, 0.9], times gain."""
    tc = h.submodule("core.texture_cleanup")
    j, i = np.mgrid[0:size, 0:size].astype(np.float64)
    u, v = (i + 0.5) / size, (j + 0.5) / size
    x = x0 + (u - 0.1) / 0.8
    y = (v - 0.1) / 0.8
    rgba = np.ones((size, size, 4), np.float32)
    rgba[..., :3] = tc.linear_to_srgb(_true_linear(x, y) * np.asarray(gain))
    return _image(name, rgba)


def test_balance_fit_preview_apply_undo() -> None:
    _reset()
    gain_b = np.array([1.25, 1.0, 0.8])
    page_a = _page("PageA", 0.0, (1.0, 1.0, 1.0))
    page_b = _page("PageB", 1.0, gain_b)
    _grid("A", 8, _material("A", page_a), origin=(0.0, 0.0))
    _grid("B", 8, _material("B", page_b), origin=(1.0, 0.0))
    _anchor()
    settings = _settings()
    settings.balance_reference = page_a
    assert bpy.ops.geotools.texture_balance_fit() == {"FINISHED"}
    pages = {item.image.name: item for item in settings.balance_pages}
    assert set(pages) == {"PageA", "PageB"}
    assert pages["PageA"].reference
    gain = np.array(pages["PageB"].gain)
    assert np.abs(gain - 1 / gain_b).max() < 0.05, gain
    offset = np.array(pages["PageB"].offset)
    assert np.abs(offset).max() < 0.01, (gain, offset)
    assert settings.balance_rms_after < 0.25 * settings.balance_rms_before
    tree = bpy.data.materials["B"].node_tree
    assert any(n.name.startswith("GeoTools balance preview") for n in tree.nodes)
    assert not any(
        n.name.startswith("GeoTools balance preview")
        for n in bpy.data.materials["A"].node_tree.nodes
    )
    # The preview must not reach a tileset.
    issues = h.submodule("blender.checks").run_checks(bpy.context)
    assert "texture-balance-preview" in {i.code for i in issues}, issues
    panels = h.submodule("blender.texture_ui")
    labels = h.label_text(h.draw(panels.GEOTOOLS_PT_textures_balance))
    assert "PageB: gain" in labels and "PageA: reference" in labels, labels

    before = _pixels(page_b)
    assert bpy.ops.geotools.texture_balance_apply() == {"FINISHED"}
    assert not any(n.name.startswith("GeoTools balance preview") for n in tree.nodes)
    tc = h.submodule("core.texture_cleanup")
    expected = _page("Expected", 1.0, (1.0, 1.0, 1.0))
    after = _pixels(page_b)
    assert page_b.is_dirty
    assert np.abs(after[..., :3] - _pixels(expected)[..., :3]).max() < 0.03
    assert np.array_equal(_pixels(page_a), _pixels(_page("A2", 0.0, (1.0, 1.0, 1.0))))
    issues = h.submodule("blender.checks").run_checks(bpy.context)
    assert "texture-balance-preview" not in {i.code for i in issues}
    assert bpy.ops.geotools.texture_cleanup_undo() == {"FINISHED"}
    assert np.array_equal(_pixels(page_b), before)
    del tc

    # Clear removes a preview without touching pixels.
    assert bpy.ops.geotools.texture_balance_fit() == {"FINISHED"}
    assert bpy.ops.geotools.texture_balance_clear() == {"FINISHED"}
    assert not len(settings.balance_pages)
    assert not any(n.name.startswith("GeoTools balance preview") for n in tree.nodes)
    assert np.array_equal(_pixels(page_b), before)


def _three_pages():
    """Pages A (the reference), B and C side by side, B and C off by a gain; fitted."""
    pages = [
        _page("PageA", 0.0, (1.0, 1.0, 1.0)),
        _page("PageB", 1.0, (1.25, 1.0, 0.8)),
        _page("PageC", 2.0, (0.8, 1.0, 1.2)),
    ]
    for x, page in enumerate(pages):
        _grid(page.name[-1], 8, _material(page.name[-1], page), origin=(float(x), 0.0))
    _anchor()
    _settings().balance_reference = pages[0]
    assert bpy.ops.geotools.texture_balance_fit() == {"FINISHED"}
    assert [i.image.name for i in _settings().balance_pages] == ["PageA", "PageB", "PageC"]
    return pages


def _has_preview(name: str) -> bool:
    tree = bpy.data.materials[name].node_tree
    return any(n.name.startswith("GeoTools balance preview") for n in tree.nodes)


def test_balance_apply_checks_every_page_first() -> None:
    """A page with no pixels stops Apply before any page changes."""
    _reset()
    _a, page_b, page_c = _three_pages()
    before = _pixels(page_b)
    missing = bpy.data.images.new("Missing", 8, 8)
    missing.source = "FILE"
    missing.filepath = "/nonexistent/geotools-test/missing.png"
    assert tuple(missing.size) == (0, 0)
    _settings().balance_pages[2].image = missing
    cleanup = _cleanup()
    cleanup.begin_undo("Earlier Tool")
    cleanup.keep_for_undo(page_c, _pixels(page_c))
    result, error = h.call_op(bpy.ops.geotools.texture_balance_apply)
    assert result != {"FINISHED"} and "no page was balanced" in error, (result, error)
    assert np.array_equal(_pixels(page_b), before) and not page_b.is_dirty
    assert _has_preview("B") and len(_settings().balance_pages) == 3
    assert bpy.ops.geotools.texture_balance_apply.poll()
    assert cleanup.undo_label() == "Earlier Tool"  # the last tool's undo is kept


def test_balance_apply_failure_puts_back_the_pages_done() -> None:
    """A failure part-way (here a MemoryError) never leaves pages half balanced."""
    _reset()
    _a, page_b, page_c = _three_pages()
    before_b, before_c = _pixels(page_b), _pixels(page_c)
    cleanup = _cleanup()
    original = cleanup.apply_encoded
    calls = []

    def failing(pixels, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise MemoryError
        return original(pixels, *args, **kwargs)

    cleanup.apply_encoded = failing
    try:
        result, error = h.call_op(bpy.ops.geotools.texture_balance_apply)
    finally:
        cleanup.apply_encoded = original
    assert result != {"FINISHED"} and error, (result, error)
    assert "failed on PageC: MemoryError; put back PageB" in error, error
    assert np.array_equal(_pixels(page_b), before_b)
    assert np.array_equal(_pixels(page_c), before_c)
    assert _has_preview("B") and _has_preview("C") and len(_settings().balance_pages) == 3
    assert cleanup.undo_label() == ""

    # Apply again balances each page once.
    assert bpy.ops.geotools.texture_balance_apply() == {"FINISHED"}
    for page, x0 in ((page_b, 1.0), (page_c, 2.0)):
        expected = _pixels(_page(f"Expected{x0}", x0, (1.0, 1.0, 1.0)))
        assert np.abs(_pixels(page)[..., :3] - expected[..., :3]).max() < 0.03, page.name
    assert bpy.ops.geotools.texture_cleanup_undo() == {"FINISHED"}
    assert np.array_equal(_pixels(page_b), before_b)
    assert np.array_equal(_pixels(page_c), before_c)


def test_balance_apply_puts_back_a_page_whose_write_failed_half_way() -> None:
    """Pixels set but the update after it failing: that page is put back too."""
    _reset()
    _a, page_b, page_c = _three_pages()
    before_b, before_c = _pixels(page_b), _pixels(page_c)
    cleanup = _cleanup()
    original = cleanup.forget
    calls = []

    def failing(image):
        calls.append(image.name)
        if calls == ["PageB", "PageC"]:  # page C's pixels are already written
            raise RuntimeError("simulated update failure")
        return original(image)

    cleanup.forget = failing
    try:
        result, error = h.call_op(bpy.ops.geotools.texture_balance_apply)
    finally:
        cleanup.forget = original
    assert result != {"FINISHED"}, (result, error)
    assert "failed on PageC: simulated update failure; put back PageB, PageC" in error, error
    assert np.array_equal(_pixels(page_b), before_b)
    assert np.array_equal(_pixels(page_c), before_c)
    assert len(_settings().balance_pages) == 3


def test_balance_apply_keeps_undo_if_putting_back_fails() -> None:
    """If the rollback fails too, Undo Cleanup still has the copies and Apply cannot rerun."""
    _reset()
    _a, page_b, page_c = _three_pages()
    before_b, before_c = _pixels(page_b), _pixels(page_c)
    cleanup = _cleanup()
    original_apply, original_put_back = cleanup.apply_encoded, cleanup._put_back
    calls = []

    def failing(pixels, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise MemoryError
        return original_apply(pixels, *args, **kwargs)

    def failing_put_back(_names):
        raise MemoryError

    cleanup.apply_encoded, cleanup._put_back = failing, failing_put_back
    try:
        result, error = h.call_op(bpy.ops.geotools.texture_balance_apply)
    finally:
        cleanup.apply_encoded, cleanup._put_back = original_apply, original_put_back
    assert result != {"FINISHED"}, (result, error)
    assert "Press Undo Cleanup" in error, error
    assert not np.array_equal(_pixels(page_b), before_b)  # balanced, not put back
    assert len(_settings().balance_pages) == 0 and not _settings().balance_preview
    assert not _has_preview("B") and not _has_preview("C")
    assert not bpy.ops.geotools.texture_balance_apply.poll()
    assert cleanup.undo_label() == "Apply Balance"
    assert bpy.ops.geotools.texture_cleanup_undo() == {"FINISHED"}
    assert np.array_equal(_pixels(page_b), before_b)
    assert np.array_equal(_pixels(page_c), before_c)


def test_balance_needs_two_pages() -> None:
    _reset()
    _grid("A", 2, _material("A", _page("PageA", 0.0, (1, 1, 1), size=16)))
    result, error = h.call_op(bpy.ops.geotools.texture_balance_fit)
    assert error and "two pages" in error, (result, error)


# --- dirty tracking and saving -------------------------------------------------------------


def _profile(output: str) -> bpy.types.PropertyGroup:
    assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
    profile = h.project().build_profiles[h.project().build_profile_index]
    profile.output_path = output
    profile.profile = "FAST"
    return profile


def _file_image(path: Path, size: int = 32) -> bpy.types.Image:
    image = _image("FileAlbedo", _gradient(size), pack=False)
    image.filepath_raw = str(path)
    image.file_format = "PNG"
    image.save()
    assert not image.is_dirty and image.source == "FILE"
    return image


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _paint(image: bpy.types.Image, value: float = 0.9) -> None:
    px = _pixels(image)
    px[:4, :4, :3] = value
    image.pixels.foreach_set(px.ravel())
    assert image.is_dirty


@contextmanager
def _saved_blend():
    """A temporary folder with the open ``.blend`` saved in it as ``site.blend``."""
    _reset()
    assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
    h.reset_scene()
    try:
        with _tmpdir() as tmp:
            assert bpy.ops.wm.save_as_mainfile(filepath=str(tmp / "site.blend")) == {"FINISHED"}
            yield tmp
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()


def test_dirty_tracking_and_checks() -> None:
    state = _state()
    with _saved_blend() as tmp:
        file_image = _file_image(tmp / "albedo.png")
        packed = _image("PackedAlbedo", _gradient(16, tint=(0.5, 1, 1)))
        _grid("A", 2, _material("A", file_image))
        _grid("B", 2, _material("B", packed), origin=(1.0, 0.0))
        _anchor()
        records = state.image_records(bpy.context.scene)
        assert [(r["name"], r["origin"]) for r in records] == [
            ("FileAlbedo", "file"),
            ("PackedAlbedo", "packed"),
        ]
        file_sha = _sha(tmp / "albedo.png")
        assert records[0]["sha256"] == file_sha
        assert records[1]["sha256"] == hashlib.sha256(packed.packed_file.data).hexdigest()
        # A build record of this state: nothing changed since.
        record_path = tmp / "build.json"
        record_path.write_text(json.dumps({"source": {"images": records}}))
        profile = _profile(str(tmp / "site.3tz"))
        profile.last_record = str(record_path)
        profile.last_output = str(tmp / "site.3tz")
        assert state.changes_since(bpy.context.scene, str(record_path)) == []
        panels = h.submodule("blender.panels")
        labels = h.label_text(h.draw(panels.GEOTOOLS_PT_publish))
        assert "changed since last publish" not in labels, labels

        # Paint: both images get unsaved edits.
        for image in (file_image, packed):
            _paint(image)
        changes = state.changes_since(bpy.context.scene, str(record_path))
        assert [(c.name, c.reason.value) for c in changes] == [
            ("FileAlbedo", "edited"),
            ("PackedAlbedo", "edited"),
        ]
        state.invalidate()
        labels = h.label_text(h.draw(panels.GEOTOOLS_PT_publish))
        assert "2 textures changed since last publish" in labels, labels
        issues = h.submodule("blender.checks").run_checks(bpy.context)
        unsaved = {i.subject: i for i in issues if i.code == "image-unsaved"}
        assert set(unsaved) == {"FileAlbedo", "PackedAlbedo"}, issues
        assert unsaved["FileAlbedo"].message == (
            "Image FileAlbedo has unsaved edits; Publish saves them to "
            "//textures/FileAlbedo.png (a copy; albedo.png is left as it is) first."
        )
        assert unsaved["FileAlbedo"].severity.value == "warning"
        assert "the .blend (packed)" in unsaved["PackedAlbedo"].message
        assert any(i.code == "textures-changed" for i in issues), issues

        # Saving flushes both. The image's own file is kept: the edits go to a
        # copy GeoTools writes, and the image now uses it.
        report = state.save_edited_images(state.source_images(bpy.context.scene))
        assert report.count == 2 and report.blend_changed
        assert report.saved[0] == (
            "FileAlbedo -> //textures/FileAlbedo.png (a copy; albedo.png is left as it is)"
        )
        assert not file_image.is_dirty and not packed.is_dirty
        assert packed.packed_file is not None
        assert _sha(tmp / "albedo.png") == file_sha
        copy = tmp / "textures" / "FileAlbedo.png"
        assert file_image.filepath == "//textures/FileAlbedo.png", file_image.filepath
        assert copy.read_bytes()[:4] == b"\x89PNG"
        assert state.owns_file(file_image)
        changes = state.changes_since(bpy.context.scene, str(record_path))
        assert [(c.name, c.reason.value) for c in changes] == [
            ("FileAlbedo", "changed"),
            ("PackedAlbedo", "changed"),
        ]
        issues = h.submodule("blender.checks").run_checks(bpy.context)
        assert not [i for i in issues if i.code == "image-unsaved"]

        # Later edits are saved over that copy, which is GeoTools' own.
        _paint(file_image, 0.1)
        assert state.save_target(file_image) == "its file FileAlbedo.png"
        copy_sha = _sha(copy)
        report = state.save_edited_images([file_image])
        assert report.count == 1 and not report.blend_changed
        assert file_image.filepath == "//textures/FileAlbedo.png"
        assert _sha(copy) != copy_sha and _sha(tmp / "albedo.png") == file_sha
        assert sorted(p.name for p in copy.parent.iterdir()) == ["FileAlbedo.png"]

        # A new image in the source, and an image no longer used.
        generated = bpy.data.images.new("Painted", 8, 8)
        generated.pixels.foreach_set(np.full(256, 0.5, np.float32))
        assert state.save_target(generated) == "//textures/Painted.png"
        state.save_edited_images([generated])
        bpy.data.materials["B"].node_tree.nodes["Image Texture"].image = generated
        changes = state.changes_since(bpy.context.scene, str(record_path))
        assert [(c.name, c.reason.value) for c in changes] == [
            ("FileAlbedo", "changed"),
            ("PackedAlbedo", "removed"),
            ("Painted", "new"),
        ]


def test_a_jpeg_source_is_never_saved_over() -> None:
    """A Metashape-style JPEG, even one in //textures/, gets a PNG copy of the edits."""
    state = _state()
    with _saved_blend() as tmp:
        (tmp / "textures").mkdir()
        jpeg = tmp / "textures" / "scan.jpg"
        image = _image("scan.jpg", _gradient(32), pack=False)
        image.filepath_raw = str(jpeg)
        image.file_format = "JPEG"
        image.save()
        original = jpeg.read_bytes()
        _grid("A", 2, _material("A", image))
        _paint(image)
        # In the textures folder, but GeoTools did not write it.
        assert not state.owns_file(image)
        target = "//textures/scan.png (a copy; scan.jpg is left as it is)"
        assert state.save_target(image) == target
        report = state.save_edited_images(state.source_images(bpy.context.scene))
        assert report.saved == [f"scan.jpg -> {target}"] and report.blend_changed
        assert jpeg.read_bytes() == original
        assert image.filepath == "//textures/scan.png" and image.file_format == "PNG"
        copy = tmp / "textures" / "scan.png"
        saved = bpy.data.images.load(str(copy), check_existing=False)
        assert np.abs(_pixels(saved) - _pixels(image)).max() <= 1.0 / 255.0 + 1e-6
        (record,) = state.image_records(bpy.context.scene)
        assert record["path"].endswith("scan.png") and record["sha256"] == _sha(copy)


def test_an_unsaved_blend_never_saves_over_a_source_file() -> None:
    """With no folder for the copy, Checks and Publish refuse; nothing is packed."""
    _reset()
    assert not bpy.data.is_saved
    state = _state()
    with _tmpdir() as tmp:
        path = tmp / "albedo.png"
        image = _file_image(path)
        _grid("A", 2, _material("A", image))
        _anchor()
        before = _sha(path)
        _paint(image)
        assert state.save_target(image) == "//textures/ once the .blend is saved"
        issues = h.submodule("blender.checks").run_checks(bpy.context)
        (issue,) = [i for i in issues if i.code == "image-unsaved"]
        assert issue.severity.value == "error", issue
        assert issue.message == (
            "Image FileAlbedo has unsaved edits, but GeoTools never saves over its file "
            "albedo.png. Save the .blend first, so Publish can save the edits to a copy "
            "in //textures/."
        )
        try:
            state.save_edited_images(state.source_images(bpy.context.scene))
        except state.SaveError as ex:
            assert "Save the .blend first" in str(ex), ex
        else:
            raise AssertionError("an unsaved .blend saved over a source file")
        assert image.is_dirty and image.packed_file is None
        assert h.submodule("blender.blend_paths").same_file(image.filepath, path)
        assert _sha(path) == before
        _profile(str(tmp / "site.3tz"))
        result, error = h.call_op(bpy.ops.geotools.publish)
        assert result != {"FINISHED"} and error and "checks found" in error, (result, error)
        assert _sha(path) == before and image.is_dirty

        # A painted generated image has no source file, so it is still packed.
        generated = bpy.data.images.new("Painted", 8, 8)
        generated.pixels.foreach_set(np.full(256, 0.5, np.float32))
        assert state.save_target(generated) == "the .blend (packed)"
        state.save_edited_images([generated])
        assert generated.packed_file is not None and not generated.is_dirty


def test_a_failed_copy_leaves_the_image_on_its_source() -> None:
    """A copy that cannot be written leaves the image on its file, still edited."""
    state = _state()
    paths = h.submodule("blender.blend_paths")
    with _saved_blend() as tmp:
        jpeg = tmp / "scan.jpg"
        image = _image("scan.jpg", _gradient(32), pack=False)
        image.filepath_raw = str(jpeg)
        image.file_format = "JPEG"
        image.save()
        _grid("A", 2, _material("A", image))
        _paint(image)
        locked = tmp / "locked"
        locked.mkdir()
        locked.chmod(0o555)
        original = state._new_file
        state._new_file = lambda _image: str(locked / "scan.png")
        try:
            state.save_edited_images([image])
        except state.SaveError as ex:
            assert "Could not save the edits of image scan.jpg" in str(ex), ex
        else:
            raise AssertionError("saving into a read-only folder succeeded")
        finally:
            state._new_file = original
            locked.chmod(0o755)
        assert paths.same_file(paths.image_path(image), jpeg), image.filepath
        assert image.file_format == "JPEG" and image.is_dirty
        assert image.geotools_texture_file == "" and not state.owns_file(image)
        assert not (locked / "scan.png").exists()

        # Once the folder can be written, the next Publish saves the copy.
        report = state.save_edited_images([image])
        assert report.count == 1 and image.filepath == "//textures/scan.png"
        assert state.owns_file(image) and not image.is_dirty


def test_save_as_keeps_the_textures_copy_geotools_own() -> None:
    """The ownership marker is a stored path: Save As re-expresses it with the image's."""
    state = _state()
    with _saved_blend() as tmp:
        image = _file_image(tmp / "albedo.png")
        _grid("A", 2, _material("A", image))
        _paint(image)
        state.save_edited_images([image])
        assert image.geotools_texture_file == "//textures/FileAlbedo.png"
        assert state.owns_file(image)
        (tmp / "sub").mkdir()
        blend = tmp / "sub" / "site.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(blend)) == {"FINISHED"}
        copy = tmp / "textures" / "FileAlbedo.png"
        assert state.owns_file(image), (image.filepath, image.geotools_texture_file)
        _paint(image, 0.2)
        assert state.save_target(image) == "its file FileAlbedo.png"
        before = _sha(copy)
        report = state.save_edited_images([image])
        assert report.count == 1 and not report.blend_changed
        assert _sha(copy) != before
        assert not (tmp / "sub" / "textures").exists()


# --- the publish loop ----------------------------------------------------------------------


def _finish():
    publish = h.submodule("blender.publish")
    job = publish.active_job()
    assert job is not None
    assert job.join(JOIN_SECONDS), "the publish job did not finish"
    publish.update_from_job()
    assert job.error is None and job.state.value == "done", (job.state, job.error)
    return job


def _glb_images(data: bytes) -> list[str]:
    """SHA-256 of each image's bytes in a GLB tile (KTX2 or PNG, not decoded)."""
    import struct

    length = struct.unpack_from("<I", data, 12)[0]
    gltf = json.loads(data[20 : 20 + length])
    offset = 20 + length
    binary = b""
    if offset < len(data):
        bin_length = struct.unpack_from("<I", data, offset)[0]
        binary = data[offset + 8 : offset + 8 + bin_length]
    views = gltf.get("bufferViews", [])
    found = []
    for image in gltf.get("images", []):
        view = views[image["bufferView"]]
        start = view.get("byteOffset", 0)
        found.append(hashlib.sha256(binary[start : start + view["byteLength"]]).hexdigest())
    return found


def _tiles(archive: Path) -> dict[str, tuple[bool, list[str]]]:
    """Each tile content: (is a leaf, hashes of its texture images)."""
    with zipfile.ZipFile(archive) as z:
        tileset = json.loads(z.read("tileset.json"))
        found: dict[str, tuple[bool, list[str]]] = {}
        stack = [tileset["root"]]
        while stack:
            tile = stack.pop()
            children = tile.get("children", [])
            stack += children
            uri = tile.get("content", {}).get("uri")
            if uri:
                found[uri] = (not children, _glb_images(z.read(uri)))
    return found


def test_painted_source_republishes_to_leaves_and_coarse_levels() -> None:
    """The M6 exit: a painted source republishes, and coarse levels pick up the change."""
    if not _available():
        return
    with _saved_blend() as tmp:
        image = _file_image(tmp / "site_albedo.png", size=512)
        _grid("Site", 160, _material("Albedo", image), size=40.0, height=2.0)
        _anchor()
        output = tmp / "site.3tz"
        profile = _profile(str(output))
        profile.overwrite = True
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        _finish()
        before = _tiles(output)
        record = json.loads((tmp / "site.geotools" / "build.json").read_text())
        (first,) = record["source"]["images"]
        assert first["name"] == "FileAlbedo" and first["origin"] == "file"
        assert first["sha256"] == hashlib.sha256((tmp / "site_albedo.png").read_bytes()).hexdigest()
        state = _state()
        assert state.changes_since(bpy.context.scene, profile.last_record) == []
        assert any(leaf for leaf, _ in before.values()) and any(
            not leaf for leaf, _ in before.values()
        ), before

        # "Paint" a quarter of the texture: the UV quarter u, v < 0.5.
        px = _pixels(image)
        px[:256, :256, :3] = (0.9, 0.1, 0.1)
        image.pixels.foreach_set(px.ravel())
        assert image.is_dirty
        state.invalidate()
        panels = h.submodule("blender.panels")
        labels = h.label_text(h.draw(panels.GEOTOOLS_PT_publish))
        assert "1 texture changed since last publish" in labels, labels

        assert bpy.ops.geotools.publish() == {"FINISHED"}
        # Saved before the export read it: never lost, never stale, and never
        # over the source file.
        assert not image.is_dirty
        assert image.filepath == "//textures/FileAlbedo.png"
        assert _sha(tmp / "site_albedo.png") == first["sha256"]
        _finish()
        after = _tiles(output)
        record = json.loads((tmp / "site.geotools" / "build.json").read_text())
        (second,) = record["source"]["images"]
        on_disk = _sha(tmp / "textures" / "FileAlbedo.png")
        assert second["sha256"] == on_disk != first["sha256"]
        assert state.changes_since(bpy.context.scene, profile.last_record) == []
        state.invalidate()
        labels = h.label_text(h.draw(panels.GEOTOOLS_PT_publish))
        assert "changed since last publish" not in labels, labels

        assert set(after) == set(before)
        changed = {uri for uri in after if after[uri][1] != before[uri][1]}
        leaves = {uri for uri in changed if after[uri][0]}
        coarse = changed - leaves
        print(
            f"    {len(changed)} of {len(after)} tiles have new texture bytes: "
            f"{len(leaves)} leaves, {len(coarse)} coarse ({sorted(coarse)})"
        )
        assert leaves, "no leaf picked up the painted texture"
        assert coarse, "no coarse tile picked up the painted texture"
        # Leaves away from the painted quarter keep their texture bytes.
        assert len(leaves) < sum(1 for leaf, _ in after.values() if leaf)


def test_painted_generated_image_is_saved_next_to_a_saved_blend() -> None:
    _reset()
    assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
    h.reset_scene()
    state = _state()
    try:
        with tempfile.TemporaryDirectory(prefix="geotools-texture-blend-") as tmp:
            blend = Path(tmp) / "site.blend"
            assert bpy.ops.wm.save_as_mainfile(filepath=str(blend)) == {"FINISHED"}
            (Path(tmp) / "textures").mkdir()
            (Path(tmp) / "textures" / "Painted.png").write_bytes(b"someone else's file")
            image = bpy.data.images.new("Painted", 8, 8)
            image.pixels.foreach_set(np.full(256, 0.25, np.float32))
            _grid("A", 1, _material("A", image))
            assert state.save_target(image) == "//textures/Painted_1.png"
            report = state.save_edited_images(state.source_images(bpy.context.scene))
            assert report.count == 1 and report.blend_changed
            assert image.source == "FILE" and not image.is_dirty
            assert image.filepath == "//textures/Painted_1.png", image.filepath
            assert (Path(tmp) / "textures" / "Painted_1.png").read_bytes()[:4] == b"\x89PNG"
            assert (Path(tmp) / "textures" / "Painted.png").read_bytes() == b"someone else's file"
            (record,) = state.image_records(bpy.context.scene)
            assert record["origin"] == "file" and record["path"].endswith("Painted_1.png")
    finally:
        assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
        h.reset_scene()
