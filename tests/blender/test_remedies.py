"""Image remedies: checks, the Checks panel buttons, export and an end-to-end publish.

The oversize fixture is a real PNG just over rusty-tiles' 32 MiB limit (3400 x
3400 noise, 33 to 35 MiB depending on the zlib build), written in under a
second, so the limit itself is not lowered for the main cases. Two cases do
lower ``core.gltf_writer.MAX_IMAGE_BYTES`` through :func:`_limit`, a test-only
patch of the module constant: a JPEG still over the limit, and a downscale
whose PNG is still over it and falls back to JPEG. Building those at the real
limit would need far larger fixtures.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import _fixtures
import _harness as h
import bpy
import numpy as np

MIB = 1 << 20
NOISE_EDGE = 3400  # 33 to 35 MiB as noise PNG: just over the limit


# --- helpers -----------------------------------------------------------------------


@contextmanager
def _files() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="geotools-remedy-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _writer():
    return h.submodule("core.gltf_writer")


@contextmanager
def _limit(max_bytes: int) -> Iterator[None]:
    """Test-only: lower rusty-tiles' encoded image limit as GeoTools sees it."""
    writer = _writer()
    original = writer.MAX_IMAGE_BYTES
    writer.MAX_IMAGE_BYTES = max_bytes
    try:
        yield
    finally:
        writer.MAX_IMAGE_BYTES = original


def _textured_source(image: bpy.types.Image) -> bpy.types.Object:
    """A UV-mapped quad whose base colour is ``image``, as the project source."""
    collection = bpy.data.collections.new("Source")
    bpy.context.scene.collection.children.link(collection)
    h.project().source_collection = collection
    material = bpy.data.materials.new("Textured")
    if material.node_tree is None:
        material.use_nodes = True
    tree = material.node_tree
    principled = next(n for n in tree.nodes if n.bl_idname == "ShaderNodeBsdfPrincipled")
    tex = tree.nodes.new("ShaderNodeTexImage")
    tex.image = image
    tree.links.new(tex.outputs["Color"], principled.inputs["Base Color"])
    mesh = bpy.data.meshes.new("Quad")
    mesh.from_pydata([(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)], [], [(0, 1, 2, 3)])
    mesh.uv_layers.new(name="UVMap")
    mesh.materials.append(material)
    obj = bpy.data.objects.new("Quad", mesh)
    collection.objects.link(obj)
    return obj


def _issues() -> list[tuple[str, str]]:
    return [(i.code, i.severity) for i in h.project().issues]


def _issue(code: str):
    return next(i for i in h.project().issues if i.code == code)


def _run_checks() -> None:
    assert bpy.ops.geotools.run_checks() == {"FINISHED"}


def _set_remedy(image: bpy.types.Image, remedy: str) -> None:
    assert bpy.ops.geotools.set_image_remedy(image=image.name, remedy=remedy) == {"FINISHED"}


def _pixels_sha(image: bpy.types.Image) -> str:
    px = np.empty(len(image.pixels), dtype=np.float32)
    image.pixels.foreach_get(px)
    return hashlib.sha256(px.tobytes()).hexdigest()


def _state(image: bpy.types.Image) -> tuple:
    """Everything about the user's image that remediation must leave alone."""
    path = Path(bpy.path.abspath(image.filepath)) if image.filepath else None
    return (
        image.name,
        image.filepath,
        image.filepath_raw,
        image.source,
        image.file_format,
        image.packed_file is not None,
        image.is_dirty,
        image.colorspace_settings.name,
        tuple(image.size),
        _pixels_sha(image),
        hashlib.sha256(path.read_bytes()).hexdigest() if path and path.is_file() else None,
    )


@contextmanager
def _extracted(collection: bpy.types.Collection, remedies: list | None = None):
    with _files() as tmp:
        bpy.context.view_layer.update()
        export = h.submodule("blender.export")
        scene, _topology = export.extract_scene(
            bpy.context, collection, tmp / "images", remedies=remedies
        )
        yield scene


def _png_header(path: Path) -> tuple[int, int, int]:
    head = path.read_bytes()[:33]
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path
    width, height = int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
    return width, height, head[24]


def _assert_admitted(image_data) -> None:
    """The file passes the GLB writer's rusty-tiles admission checks."""
    size = _writer()._validate_image(0, image_data)
    assert 0 < size <= 32 * MIB, size


def _noise_image(files: Path) -> tuple[bpy.types.Image, Path]:
    path = files / "big.png"
    path.write_bytes(_fixtures.png_noise(NOISE_EDGE, NOISE_EDGE))
    assert path.stat().st_size > 32 * MIB
    return bpy.data.images.load(str(path)), path


# --- 16-bit ------------------------------------------------------------------------


def test_16_bit_png_is_refused_then_converted_to_8_bit() -> None:
    h.reset_scene()
    with _files() as files:
        path = files / "deep.png"
        path.write_bytes(_fixtures.png16(16, 8))
        image = bpy.data.images.load(str(path))
        obj = _textured_source(image)

        _run_checks()
        assert ("image-16-bit", "ERROR") in _issues(), _issues()
        assert "Convert to 8-bit" in _issue("image-16-bit").message
        assert h.project().summary_images[0].bit_depth == 16

        # A packed 16-bit PNG is caught too, without reading the packed bytes.
        packed = bpy.data.images.load(str(path), check_existing=False)
        packed.pack()
        summary = h.submodule("blender.summarise").image_summary(packed)
        assert summary.bit_depth == 16 and summary.packed, summary
        bpy.data.images.remove(packed)

        before = _state(image)
        _set_remedy(image, "CONVERT_8BIT")
        assert ("image-16-bit", "WARNING") in _issues(), _issues()
        assert not [c for c, s in _issues() if s == "ERROR"], _issues()
        assert "8-bit PNG" in _issue("image-16-bit").message

        count = len(bpy.data.images)
        with _extracted(obj.users_collection[0]) as scene:
            data = scene.images[0]
            assert data.path.name.endswith("-8bit.png") and data.mime_type == "image/png", data
            assert _png_header(data.path) == (16, 8, 8)
            _assert_admitted(data)
            # Blender holds 16-bit PNGs as linear float; the export writes sRGB
            # again, so each sample is the 16-bit one to within an 8-bit step.
            out = bpy.data.images.load(str(data.path), check_existing=False)
            px = np.empty(len(out.pixels), dtype=np.float32)
            out.pixels.foreach_get(px)
            bpy.data.images.remove(out)
            got = px.reshape(8, 16, 4)[::-1, :, :3]  # Blender stores the bottom row first
            want = _fixtures.samples16(16, 8) / 65535.0
            assert np.abs(got - want).max() <= 1.0 / 255 + 1e-6
        assert len(bpy.data.images) == count, "a temporary image was left behind"
        assert _state(image) == before


# --- oversize PNG ------------------------------------------------------------------


def test_oversize_png_remedies_and_lossless_warning() -> None:
    h.reset_scene()
    with _files() as files:
        image, path = _noise_image(files)
        obj = _textured_source(image)
        collection = obj.users_collection[0]
        assert bpy.ops.geotools.build_profile_add() == {"FINISHED"}
        profile = h.project().build_profiles[0]
        profile.profile = "LOSSLESS"

        _run_checks()
        assert ("image-file-too-large", "ERROR") in _issues(), _issues()
        message = _issue("image-file-too-large").message
        assert "Re-encode as JPEG or Downscale" in message, message

        # The selected issue offers both remedies; the Images panel lists the image.
        props = h.project()
        props.issues_index = [i.code for i in props.issues].index("image-file-too-large")
        panels = h.submodule("blender.panels")
        calls = h.draw(panels.GEOTOOLS_PT_checks)
        buttons = [icon for kind, idname, icon in calls if idname == "geotools.set_image_remedy"]
        assert buttons == ["IMAGE_RGB", "FULLSCREEN_EXIT"], calls
        calls = h.draw(panels.GEOTOOLS_PT_checks_images)
        # The encoded size depends on the zlib build, so read it from the file.
        size = f"{path.stat().st_size / MIB:.1f} MiB"
        assert ("label", f"{NOISE_EDGE}x{NOISE_EDGE} PNG, {size}", "NONE") in calls, calls
        assert ("template_list", "GEOTOOLS_UL_images", "NONE") in calls

        before = _state(image)
        count = len(bpy.data.images)

        # JPEG under the lossless profile: a warning naming the remedy, plus the
        # lossless warning. Nothing is applied until export.
        _set_remedy(image, "JPEG")
        issues = _issues()
        assert ("image-file-too-large", "WARNING") in issues, issues
        assert ("image-remedy-lossy", "WARNING") in issues, issues
        assert not [c for c, s in issues if s == "ERROR"], issues
        assert "JPEG quality 95" in _issue("image-file-too-large").message
        assert "lossless" in _issue("image-remedy-lossy").message
        assert _state(image) == before
        calls = h.draw(panels.GEOTOOLS_PT_checks)
        buttons = [icon for kind, idname, icon in calls if idname == "geotools.set_image_remedy"]
        assert buttons == ["FULLSCREEN_EXIT", "X"], calls  # Downscale, Clear Remedy
        item = props.summary_images[0]
        assert ("label", "JPEG 95", "MODIFIER") in h.draw_list_item(
            h.submodule("blender.remedies").GEOTOOLS_UL_images, props, item
        )

        profile.profile = "FAST"
        _run_checks()
        assert "image-remedy-lossy" not in [c for c, _s in _issues()], _issues()

        remedies: list[dict] = []
        with _extracted(collection, remedies) as scene:
            data = scene.images[0]
            assert data.mime_type == "image/jpeg", data
            assert data.path.name.endswith("-jpeg-q95.jpg"), data.path.name
            _assert_admitted(data)
            first = data.path.read_bytes()
            with _extracted(collection) as again:
                assert again.images[0].path.name == data.path.name
                assert again.images[0].path.read_bytes() == first, "JPEG is not deterministic"
        assert [(r["remedy"], r["mime_type"]) for r in remedies] == [("jpeg-q95", "image/jpeg")]

        # Downscale: the largest power of two expected to fit, here 2048.
        _set_remedy(image, "DOWNSCALE")
        assert image.geotools_remedy.max_edge == 2048
        assert ("image-file-too-large", "WARNING") in _issues(), _issues()
        assert "2048x2048" in _issue("image-file-too-large").message
        with _extracted(collection) as scene:
            data = scene.images[0]
            assert data.path.name.endswith("-downscale-2048.png"), data.path.name
            assert _png_header(data.path) == (2048, 2048, 8)
            _assert_admitted(data)
            first = data.path.read_bytes()
            with _extracted(collection) as again:
                assert again.images[0].path.read_bytes() == first, "PNG is not deterministic"

        # A downscaled PNG still over the limit falls back to JPEG (limit lowered).
        with _limit(8 * MIB), _extracted(collection) as scene:
            data = scene.images[0]
            assert data.path.name.endswith("-downscale-2048-q95.jpg"), data.path.name
            assert data.mime_type == "image/jpeg"
            assert data.path.stat().st_size <= 8 * MIB

        # A JPEG still over the limit fails clearly, without retrying (limit lowered).
        _set_remedy(image, "JPEG")
        image.geotools_remedy.quality = 100
        export_error = _writer().ExportError
        with _limit(4 * MIB):
            try:
                with _extracted(collection):
                    pass
            except export_error as ex:
                assert "still" in str(ex) and "lower its JPEG quality" in str(ex), ex
            else:
                raise AssertionError("a JPEG over the limit was accepted")

        _set_remedy(image, "NONE")
        assert image.geotools_remedy.kind == "NONE"
        assert ("image-file-too-large", "ERROR") in _issues(), _issues()
        assert len(bpy.data.images) == count, "a temporary image was left behind"
        assert _state(image) == before
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_jpeg_remedy_refuses_transparency() -> None:
    h.reset_scene()
    image = bpy.data.images.new("Cutout", 8, 8, alpha=True)
    image.pixels.foreach_set(np.tile(np.array([1, 0, 0, 0.5], np.float32), 64))
    obj = _textured_source(image)
    image.geotools_remedy.kind = "JPEG"
    try:
        with _extracted(obj.users_collection[0]):
            pass
    except _writer().ExportError as ex:
        assert "transparency" in str(ex), ex
    else:
        raise AssertionError("JPEG remedy dropped an alpha channel silently")


def test_remedy_operator_refuses_unknown_image() -> None:
    h.reset_scene()
    result, error = h.call_op(bpy.ops.geotools.set_image_remedy, image="Nope", remedy="JPEG")
    assert result == {"ERROR_REPORTED"} and "Nope" in error, (result, error)


def test_remedy_survives_rename_save_and_reload() -> None:
    h.reset_scene()
    image = bpy.data.images.new("Albedo", 4, 4)
    image.use_fake_user = True
    remedy = image.geotools_remedy
    remedy.kind = "DOWNSCALE"
    remedy.max_edge = 1024
    remedy.quality = 92
    image.name = "Albedo renamed"
    with _files() as tmp:
        path = tmp / "remedy.blend"
        assert bpy.ops.wm.save_as_mainfile(filepath=str(path), copy=True) == {"FINISHED"}
        h.reset_scene()
        assert bpy.ops.wm.open_mainfile(filepath=str(path), load_ui=False) == {"FINISHED"}
        image = bpy.data.images["Albedo renamed"]
        remedy = image.geotools_remedy
        assert (remedy.kind, remedy.max_edge, remedy.quality) == ("DOWNSCALE", 1024, 92)
        core = h.submodule("blender.properties").image_remedy(image)
        assert (core.kind, core.max_edge, core.quality) == ("DOWNSCALE", 1024, 92)
        assert not image.is_dirty
    assert bpy.ops.wm.read_homefile(use_empty=True) == {"FINISHED"}
    h.reset_scene()


# --- end to end (needs the wheel) ----------------------------------------------------


def test_publish_with_remedied_image_end_to_end() -> None:
    if h.CONFIG.expect != "available":
        return
    import test_publish as tp

    h.reset_scene()
    with _files() as files, tp._tmpdir() as tmp:
        grid = tp._textured_grid(6)
        image, _path = _noise_image(files)
        tex = next(
            n for n in grid.data.materials[0].node_tree.nodes if n.bl_idname == "ShaderNodeTexImage"
        )
        tex.image = image
        bpy.data.images.remove(bpy.data.images["Checker"])
        output = tmp / "remedied.3tz"
        tp._add_profile(str(output))

        result, error = h.call_op(bpy.ops.geotools.publish)
        assert result == {"ERROR_REPORTED"} and "fix the errors" in error, (result, error)

        before = _state(image)
        _set_remedy(image, "JPEG")
        assert bpy.ops.geotools.publish() == {"FINISHED"}
        job = tp._finish()
        status = tp._status()
        assert status.state == "DONE", (status.state, status.message, job.error)
        rusty_tiles = __import__("rusty_tiles")
        assert rusty_tiles.validate(output)["ok"]
        record = json.loads((tmp / "remedied.geotools" / "build.json").read_text())
        assert record["validation"]["ok"] is True
        (applied,) = record["geotools"]["image_remedies"]
        assert applied["image"] == image.name and applied["remedy"] == "jpeg-q95", applied
        assert applied["mime_type"] == "image/jpeg" and 0 < applied["bytes"] <= 32 * MIB
        assert (applied["width"], applied["height"]) == (NOISE_EDGE, NOISE_EDGE)
        assert _state(image) == before
