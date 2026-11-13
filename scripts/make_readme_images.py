#!/usr/bin/env python3
"""Make the README and docs images from the synthetic demo, reproducibly (a dev tool).

One command regenerates everything in ``docs/assets/readme/``:

    python3 scripts/make_readme_images.py --out docs/assets/readme

What it does, in three stages (the same file runs in each):

1. **Blender** (host side, stdlib only): builds the extension with the bundled
   wheels, installs it into a throwaway profile, copies the synthetic demo
   (``examples/drape-contexts/``) into ``.scratch/readme-images/work`` and opens
   it in GUI Blender (``--inner``). A Blender window is on your screen for
   about a minute; ``--enable-event-simulate`` makes Blender ignore your mouse
   and keyboard meanwhile. Inside, the demo is prepared the way a user would:
   a synthetic orthophoto texture, the Slope analysis preset, Verify
   Placement, Run Checks, contours and hydrology flow paths, a synthetic DEM
   GeoTIFF linked beside the GeoJSON, Fill Holes and Publish (``site.3tz`` and
   the slope variant ``site.slope.3tz``), then solstice and lunar sightlines from
   the mound (Archaeoastronomy) and a viewshed from the field's low corner. Each
   shot shows only the GeoTools panels it is about, in a fresh workspace (a
   region keeps each panel's open or closed state once it has drawn it); the
   analysis shots also take a centred view for the hero's montage.
2. **Cesium**: the ``cesium@CESIUM`` package tarball (``npm pack``, checked
   against its pinned integrity; only its prebuilt ``Build/Cesium`` is used, so
   no dependency is resolved) unpacked into the scratch folder, the slope
   variant extracted beside a small page, rendered in headless Chromium with
   Playwright (offline: no ion token, Cesium's bundled NaturalEarthII imagery
   and the default ellipsoid). The page waits until the tileset, the globe and
   every request have finished, and fails if the site is not wholly in frame.
3. **Compose** (``--compose``, run by ``uv run`` with pinned Python,
   Playwright, Pillow and pyoxipng, and ``--exclude-newer`` for their
   dependencies): the hero and the social previews are HTML/CSS templates in
   ``docs/assets/readme/templates/`` rendered with Chromium; the feature
   thumbnails are crops of the Blender shots; every PNG is optimised with
   oxipng.

Every image is checked: a blank or flat render (for a thumbnail, also its
viewport part alone), a missing tileset or a failed operator stops the run
with an error. The scratch folder is deleted at the end (``--keep`` keeps it,
and reuses its unpacked Cesium on the next run).

Outputs (in ``--out``):

* ``hero-a.png``: "Analyse in Blender", a montage of four analyses of the demo
  (slope and contours, a viewshed, sightlines, flow paths), beside "Share", the
  published slope variant in CesiumJS (transparent, for light and dark pages);
* ``hero-b.png``, ``hero-b-light.png``: hero option B, the designed banner
  (the wordmark, tagline and both shots inset), dark and light, 1600x640;
* ``social-a.png`` (the hero with the wordmark), ``social-b.png`` (the
  wordmark banner): 1280x640 social previews for the repository's settings
  (under 1 MB);
* ``feature-*.png``: thumbnails of the main features, the GeoTools sidebar
  with a strip of the 3D Viewport (the publish one with the Cesium view), so
  the sidebar text stays readable at half size.

Needs: Blender 5.2 (``--blender`` or ``$BLENDER``), the rusty-tiles wheels in
``geotools/wheels/`` (``scripts/fetch_wheels.py``), npm, ``uv``
(Playwright's Chromium is installed with ``playwright install chromium`` if
missing), network access for npm and PyPI on the first run. Synthetic data
only: never point it at real survey data.

Notes:
* On a Wayland desktop Blender is pushed onto Xwayland (``WAYLAND_DISPLAY=``),
  as in ``scripts/ui_screenshots.py``, whose approach this follows.
* Blender's temporary folder is the scratch folder (``TMPDIR``), so its
  ``quit.blend`` session-recovery file never lands in ``/tmp``.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import bpy  # only inside Blender

    INSIDE_BLENDER = True
except ImportError:
    INSIDE_BLENDER = False

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "examples" / "drape-contexts"
TEMPLATES_REL = Path("templates")  # inside --out

# Pinned tools (each released at least two weeks before it was pinned).
CESIUM = "1.146.0"  # npm, 2026-10-01
CESIUM_INTEGRITY = (  # the tarball's npm dist.integrity
    "sha512-hvKkURnmc8OZ8qKNrwawl/YijwAItaWLbpAdIVNAUXODCLMjpod4PbdLuwo8Ab6ETL915MUGV2NocHFCRCeqqA=="
)
PLAYWRIGHT = "1.64.1"  # PyPI, 2026-10-22 (Chromium build 1248)
PILLOW = "12.2.0"  # PyPI, 2026-04-01
PYOXIPNG = "9.1.1"  # PyPI, 2025-08-21 (oxipng 9.1)
UV_PYTHON = "3.13.14"
# Their dependencies (greenlet, pyee, typing-extensions) resolve as of this date.
UV_EXCLUDE_NEWER = "2026-10-30T00:00:00Z"

WINDOW = (1600, 1000)
GUI_TIMEOUT = 600  # seconds
# The feature thumbnails: the top-right of the 3D Viewport area, header included: the
# sidebar and a 200 px strip of viewport. Small, so the README can show them two to a
# row (about 380 px wide) with the sidebar text still readable. 536 px tall ends the
# Publish panel just after Folder and Record.
THUMB = (560, 536)
SIDEBAR_WIDTH = 360  # px; the sidebar is dragged wider, as a user would, so labels fit
SOCIAL_LIMIT = 1_000_000  # bytes, GitHub's social preview limit
FOLDER_BUDGET = 8_000_000  # bytes, the whole --out folder

# Shots taken in Blender: name -> what the sidebar shows.
SHOTS = (
    "textures",
    "hero",
    "georef",
    "layers",
    "water",
    "publish",
    "formats",
    "sky",
    "terrain",
)


class MakeError(RuntimeError):
    pass


# --- host side ---------------------------------------------------------------------------------


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    check = kwargs.pop("check", False)
    return subprocess.run(cmd, check=check, **kwargs)


def host_main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_blender_tests import (
        RunError,
        blender_python,
        build,
        find_blender,
        install,
        isolated_env,
        zip_manifest,
    )
    from run_blender_tests import remove_tree as _remove_tree

    def remove_tree(path: Path) -> None:
        if path.exists():
            _remove_tree(path)

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="output folder for the PNGs")
    parser.add_argument(
        "--blender",
        default=os.environ.get("BLENDER", "blender"),
        help="Blender executable (default: $BLENDER, else blender on PATH)",
    )
    parser.add_argument(
        "--scratch",
        type=Path,
        default=REPO_ROOT / ".scratch" / "readme-images",
        help="scratch folder (default: .scratch/readme-images; never /tmp, which is small)",
    )
    parser.add_argument(
        "--skip-blender",
        action="store_true",
        help="reuse the Blender shots already in the scratch folder (needs --keep before)",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="keep WAYLAND_DISPLAY (by default Blender is pushed onto Xwayland)",
    )
    parser.add_argument(
        "--skip-compose", action="store_true", help="stop after the Blender shots (with --keep)"
    )
    parser.add_argument("--keep", action="store_true", help="keep the scratch folder")
    args = parser.parse_args(argv)

    out = args.out.expanduser().resolve()
    scratch = args.scratch.expanduser().resolve()
    raw = scratch / "raw"
    work = scratch / "work"
    started = time.time()
    try:
        for tool in ("npm", "uv"):
            if shutil.which(tool) is None:
                raise MakeError(f"{tool} is not on PATH")
        if not (out / TEMPLATES_REL / "hero-a.html").is_file() and not args.skip_compose:
            raise MakeError(f"{out / TEMPLATES_REL} has no templates (wrong --out?)")
        if not args.skip_blender:
            for sub in ("raw", "work", "profile", "tmp", "dist"):
                remove_tree(scratch / sub)
            for sub in ("raw", "work", "profile", "tmp"):
                (scratch / sub).mkdir(parents=True)
            blender = find_blender(args.blender)
            env = isolated_env(scratch / "profile")
            env["TMPDIR"] = str(scratch / "tmp")  # Blender's temp dir: quit.blend lands here
            if not any((REPO_ROOT / "geotools" / "wheels").glob("rusty_tiles-*.whl")):
                raise MakeError("no rusty-tiles wheel in geotools/wheels (scripts/fetch_wheels.py)")
            zip_path = build(blender, scratch / "dist", True, env, blender_python(blender, env))
            ext_id = str(zip_manifest(zip_path)["id"])
            install(blender, zip_path, ext_id, env, scratch / "profile")
            for name in ("drape-contexts.blend", "contexts.geojson", "terrain.ply"):
                shutil.copy2(DEMO / name, work / name)
            shutil.copy2(DEMO / "terrain_offset.txt", work / "terrain_offset.txt")
            if not args.native:
                env["WAYLAND_DISPLAY"] = ""
            cmd = [
                blender,
                "--enable-event-simulate",
                "--window-geometry",
                "0",
                "0",
                str(WINDOW[0]),
                str(WINDOW[1]),
                str(work / "drape-contexts.blend"),
                "--python",
                str(Path(__file__).resolve()),
                "--",
                "--inner",
                "--package",
                f"bl_ext.user_default.{ext_id}",
                "--raw",
                str(raw),
            ]
            result = _run(cmd, env=env, timeout=GUI_TIMEOUT, check=False)
            _remove_quit_blend(started)
            done = raw / "shots.json"
            if result.returncode != 0 or not done.is_file():
                raise MakeError(
                    f"Blender exited with {result.returncode}"
                    + ("" if done.is_file() else " and wrote no shots.json (see its log above)")
                )
            meta = json.loads(done.read_text())
            if meta.get("errors"):
                raise MakeError("in Blender: " + "; ".join(meta["errors"]))
        for name in ("site.3tz", "site.slope.3tz"):
            if not (work / name).is_file():
                raise MakeError(f"the publish wrote no {name}")
        if args.skip_compose:
            return 0

        web = scratch / "web"
        remove_tree(web)
        web.mkdir(parents=True)
        _fetch_cesium(scratch / "cesium")
        _extract_3tz(work / "site.slope.3tz", web / "site.slope")  # the hero: the slope layer
        _extract_3tz(work / "site.3tz", web / "site")  # Publish's Last Build: the textured site

        cmd = [
            "uv",
            "run",
            "--no-project",
            "--python",
            UV_PYTHON,
            "--exclude-newer",
            UV_EXCLUDE_NEWER,
            "--with",
            f"playwright=={PLAYWRIGHT}",
            "--with",
            f"pillow=={PILLOW}",
            "--with",
            f"pyoxipng=={PYOXIPNG}",
            "python",
            "-I",
            str(Path(__file__).resolve()),
            "--compose",
            "--scratch",
            str(scratch),
            "--out",
            str(out),
        ]
        if _run(cmd, check=False).returncode != 0:
            raise MakeError("the compose stage failed")
        total = 0
        for path in sorted(out.rglob("*")):
            if path.is_file():
                total += path.stat().st_size
                if path.suffix == ".png":
                    print(f"{path.stat().st_size / 1024:8.1f} KiB  {path.relative_to(out)}")
        print(f"{total / 1e6:.2f} MB in {out}")
        if total > FOLDER_BUDGET:
            raise MakeError(f"{out} is {total / 1e6:.1f} MB, over {FOLDER_BUDGET / 1e6:.0f} MB")
        return 0
    except (MakeError, RunError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    finally:
        _remove_quit_blend(started)
        if args.keep:
            print(f"kept {scratch}")
        else:
            remove_tree(scratch)


def _remove_quit_blend(since: float) -> None:
    """Blender's session-recovery file, if this run wrote it to /tmp after all."""
    for path in (Path("/tmp/quit.blend"),):
        try:
            if path.stat().st_mtime >= since:
                path.unlink()
        except OSError:
            pass


def _fetch_cesium(dest: Path) -> None:
    """Cesium's prebuilt ``Build/Cesium`` into ``dest``, from the pinned npm tarball.

    ``npm pack`` fetches the one tarball and resolves no dependency (an
    ``npm install`` would resolve cesium's dependency ranges afresh each time).
    """
    import base64
    import hashlib
    import tarfile

    stamp = dest / "VERSION"
    if stamp.is_file() and stamp.read_text().strip() == CESIUM:
        return
    if dest.exists():
        shutil.rmtree(dest)
    download = dest.parent / "cesium-download"
    if download.exists():
        shutil.rmtree(download)
    download.mkdir(parents=True)
    code = _run(
        ["npm", "pack", "--silent", "--pack-destination", str(download), f"cesium@{CESIUM}"],
        check=False,
        cwd=download,
    ).returncode
    tarballs = list(download.glob("cesium-*.tgz"))
    if code != 0 or len(tarballs) != 1:
        raise MakeError(f"npm pack cesium@{CESIUM} failed")
    algorithm, _, expected = CESIUM_INTEGRITY.partition("-")
    digest = hashlib.new(algorithm, tarballs[0].read_bytes()).digest()
    if base64.b64encode(digest).decode() != expected:
        raise MakeError(f"cesium-{CESIUM}.tgz does not match its pinned integrity")
    prefix = "package/Build/Cesium/"
    with tarfile.open(tarballs[0]) as tar:
        members = []
        for member in tar.getmembers():
            if member.name.startswith(prefix) and (member.isfile() or member.isdir()):
                member.name = member.name[len(prefix) :]
                if member.name:
                    members.append(member)
        tar.extractall(dest, members=members, filter="data")
    shutil.rmtree(download)
    if not (dest / "Cesium.js").is_file():
        raise MakeError(f"cesium-{CESIUM}.tgz has no Build/Cesium/Cesium.js")
    stamp.write_text(CESIUM + "\n")


def _extract_3tz(archive: Path, dest: Path) -> None:
    """A .3tz is a ZIP of a 3D Tiles tileset with tileset.json at its root."""
    import zipfile

    dest.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise MakeError(f"{archive} has an unsafe member {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(info)
            # Some 3D Tiles archives gzip their members; Cesium fetches them plainly.
            if data[:2] == b"\x1f\x8b":
                import gzip

                data = gzip.decompress(data)
            target.write_bytes(data)
    if not (dest / "tileset.json").is_file():
        raise MakeError(f"{archive} has no tileset.json at its root")


# --- inside Blender -------------------------------------------------------------------------------


def inner_main(argv: list[str]) -> None:
    import importlib
    import logging
    import math

    import bpy
    import numpy as np
    from mathutils import Euler, Vector

    parser = argparse.ArgumentParser()
    parser.add_argument("--inner", action="store_true")
    parser.add_argument("--package", required=True)
    parser.add_argument("--raw", type=Path, required=True)
    args = parser.parse_args(argv)
    raw: Path = args.raw
    raw.mkdir(parents=True, exist_ok=True)
    pkg = args.package
    meta: dict = {"window": None, "shots": {}, "errors": [], "notes": []}

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    sys.path.insert(0, str(REPO_ROOT / "tests" / "blender"))
    import make_example_drape as demo  # the demo's own synthetic numbers

    log = logging.getLogger("make_readme_images")

    def mod(name: str):
        return importlib.import_module(f"{pkg}.{name}")

    def fail(message: str) -> None:
        meta["errors"].append(message)
        raise MakeError(message)

    def run_op(op, **kwargs) -> None:
        """Run an operator in the 3D Viewport's context; any error report fails the run."""
        window, area, region = view3d()
        try:
            with bpy.context.temp_override(window=window, area=area, region=region):
                result = op(**kwargs)
        except RuntimeError as ex:  # an operator ERROR report
            fail(f"{op.idname_py()}: {ex}")
        if "FINISHED" not in result:
            fail(f"{op.idname_py()} returned {sorted(result)}")

    # -- the window -------------------------------------------------------------------

    def view3d():
        window = bpy.context.window_manager.windows[0]
        areas = [a for a in window.screen.areas if a.type == "VIEW_3D"]
        if not areas:
            raise MakeError("no 3D Viewport")
        area = max(areas, key=lambda a: a.width * a.height)
        main = next(r for r in area.regions if r.type == "WINDOW")
        return window, area, main

    def sidebar():
        _window, area, _main = view3d()
        return next(r for r in area.regions if r.type == "UI")

    # -- the sidebar ------------------------------------------------------------------

    def geotools_panels() -> list[type]:
        """GeoTools panel classes in registration order (the sidebar order)."""
        ordered: list[type] = []
        for module in mod("blender").MODULES:
            for cls in getattr(module, "classes", ()):
                if issubclass(cls, bpy.types.Panel) and cls not in ordered:
                    ordered.append(cls)
        return ordered

    original_options: dict[type, set] = {}

    def hide_other_tabs() -> None:
        ours = set(geotools_panels())
        panels = sorted(
            bpy.types.Panel.__subclasses__(), key=lambda c: not getattr(c, "bl_parent_id", "")
        )
        for cls in panels:
            if (
                getattr(cls, "bl_space_type", "") == "VIEW_3D"
                and getattr(cls, "bl_region_type", "") == "UI"
                and cls not in ours
            ):
                try:
                    bpy.utils.unregister_class(cls)
                except RuntimeError:
                    pass

    def show_panels(spec: dict[str, bool]) -> None:
        """Register only the panels in ``spec`` (idname -> open), in the add-on's order."""
        ordered = geotools_panels()
        known = {cls.bl_idname for cls in ordered}
        unknown = set(spec) - known
        if unknown:
            fail(f"unknown panels {sorted(unknown)}")
        for cls in reversed(ordered):
            original_options.setdefault(cls, set(getattr(cls, "bl_options", set())))
            if getattr(cls, "is_registered", False):
                bpy.utils.unregister_class(cls)
        for cls in ordered:
            if cls.bl_idname not in spec:
                continue
            options = original_options[cls] - {"DEFAULT_CLOSED"}
            if not spec[cls.bl_idname]:
                options |= {"DEFAULT_CLOSED"}
            cls.bl_options = options
            bpy.utils.register_class(cls)

    def restore_panels() -> None:
        ordered = geotools_panels()
        for cls in reversed(ordered):
            if getattr(cls, "is_registered", False):
                bpy.utils.unregister_class(cls)
        for cls in ordered:
            if cls in original_options:
                cls.bl_options = original_options[cls]
            bpy.utils.register_class(cls)

    # -- the scene ----------------------------------------------------------------------

    scene = bpy.context.scene
    props = scene.geotools
    terrain = bpy.data.objects["terrain"]
    work = Path(bpy.path.abspath("//"))

    def select(*objects) -> None:
        for obj in bpy.context.view_layer.objects:
            obj.select_set(obj in objects)
        bpy.context.view_layer.objects.active = objects[0] if objects else None

    def fbm(shape: tuple[int, int], seed: int, octaves: int = 6, base: int = 4) -> np.ndarray:
        """Smooth fractal noise in [0, 1] (bilinear upsampled random grids)."""
        rng = np.random.default_rng(seed)
        h, w = shape
        total = np.zeros(shape, np.float64)
        amp, norm = 1.0, 0.0
        for k in range(octaves):
            n = base * 2**k + 1
            grid = rng.random((n, n))
            ys = np.linspace(0, n - 1, h)
            xs = np.linspace(0, n - 1, w)
            y0 = np.floor(ys).astype(int).clip(0, n - 2)
            x0 = np.floor(xs).astype(int).clip(0, n - 2)
            fy = (ys - y0)[:, None]
            fx = (xs - x0)[None, :]
            fy, fx = fy * fy * (3 - 2 * fy), fx * fx * (3 - 2 * fx)
            a = grid[y0][:, x0]
            b = grid[y0][:, x0 + 1]
            c = grid[y0 + 1][:, x0]
            d = grid[y0 + 1][:, x0 + 1]
            total += amp * (
                a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy
            )
            norm += amp
            amp *= 0.55
        return total / norm

    def polygon_mask(xs, ys, ring) -> np.ndarray:
        """Even-odd point-in-polygon on a grid (vectorised over the grid, looped over edges)."""
        inside = np.zeros(xs.shape, bool)
        ring = np.asarray(ring, float)
        for (x1, y1), (x2, y2) in itertools.pairwise(ring):
            crosses = (y1 > ys) != (y2 > ys)
            with np.errstate(divide="ignore", invalid="ignore"):
                xcross = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
            inside ^= crosses & (xs < xcross)
        return inside

    def orthophoto(size: int = 1024) -> np.ndarray:
        """A synthetic 'drone orthophoto' of the demo field: grass, dry patches, crop marks.

        Rows top to bottom are north to south; returns (size, size, 4) float sRGB.
        """
        half = demo.SIZE / 2
        axis = (np.arange(size) + 0.5) / size * demo.SIZE - half
        xs, ys = np.meshgrid(axis, -axis)  # image row 0 is north
        z = demo.terrain_height(xs, ys)
        n1 = fbm((size, size), 11)
        n2 = fbm((size, size), 23, octaves=7, base=8)
        grass = np.array([0.36, 0.43, 0.22])
        dry = np.array([0.62, 0.57, 0.36])
        soil = np.array([0.45, 0.35, 0.24])
        t = np.clip((n1 - 0.45) * 2.2 + (z - 2.6) * 0.35, 0, 1)[..., None]
        rgb = grass * (1 - t) + dry * t
        # Mowing stripes across the field, faint.
        stripes = 0.5 + 0.5 * np.sin((xs * 0.8 + ys * 0.35) * 1.4)
        rgb *= (0.94 + 0.06 * stripes)[..., None]
        # Crop marks: the buried features grow greener and darker.
        marks = np.zeros(xs.shape, float)
        for feature in demo.contexts_features():
            rings = feature["geometry"]["coordinates"]
            inside = np.zeros(xs.shape, bool)
            for ring in rings:
                local = [[e - demo.E0, n - demo.N0] for e, n in ring]
                inside ^= polygon_mask(xs, ys, local)
            marks = np.maximum(marks, inside.astype(float))
        blur = marks.copy()
        for _ in range(3):  # a cheap blur, so the marks have soft edges
            blur = (
                blur
                + np.roll(blur, 2, 0)
                + np.roll(blur, -2, 0)
                + np.roll(blur, 2, 1)
                + np.roll(blur, -2, 1)
            ) / 5
        rgb = (
            rgb * (1 - 0.35 * blur[..., None])
            + np.array([0.20, 0.33, 0.14]) * 0.35 * blur[..., None]
        )
        # An open trench of bare soil over the ditch's west end.
        trench = (np.abs(xs + 19.0) < 2.2) & (np.abs(ys - 13.4) < 4.5)
        rgb[trench] = soil * (0.85 + 0.3 * n2[trench, None])
        # Texture grain.
        rgb *= (0.86 + 0.28 * n2)[..., None]
        # Photogrammetry holes: black where no photo saw the surface.
        for cx, cy, r, squash in HOLES:
            hole = (xs - cx) ** 2 + ((ys - cy) * squash) ** 2 < r * r
            rgb[hole] = 0.0
        rgba = np.concatenate([np.clip(rgb, 0, 1), np.ones((size, size, 1))], axis=2)
        return rgba

    # Photogrammetry holes in the synthetic photo (local x, y, radius, squash): a cluster
    # west of the ring ditch for the Textures close-up, and a few more about the field.
    HOLES = (
        (-15.5, 8.6, 1.0, 1.3),
        (-12.4, 6.2, 0.65, 0.8),
        (-17.2, 5.0, 0.45, 1.0),
        (-10.8, 9.9, 0.35, 1.0),
        (14.0, -21.0, 0.9, 1.2),
        (23.0, 8.0, 1.1, 0.7),
        (-4.0, -23.0, 0.7, 1.0),
    )
    state_hole = (-14.0, 7.4, float(demo.terrain_height(np.array(-14.0), np.array(7.4))))

    def add_photo_texture() -> bpy.types.Image:
        mesh = terrain.data
        uv = mesh.uv_layers.get("UVMap") or mesh.uv_layers.new(name="UVMap")
        count = len(mesh.loops)
        vidx = np.empty(count, np.int32)
        mesh.loops.foreach_get("vertex_index", vidx)
        co = np.empty(len(mesh.vertices) * 3, np.float32)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        half = demo.SIZE / 2
        uvs = (co[vidx, :2] + half) / demo.SIZE
        uv.data.foreach_set("uv", uvs.astype(np.float32).ravel())
        size = 1024
        image = bpy.data.images.new("ground_photo", size, size, alpha=False)
        pixels = orthophoto(size)[::-1]  # Blender's rows run bottom to top
        image.pixels.foreach_set(pixels.astype(np.float32).ravel())
        image.filepath_raw = str(work / "ground_photo.png")
        image.file_format = "PNG"
        image.save()
        material = terrain.data.materials[0]
        tree = material.node_tree
        bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
        node = tree.nodes.new("ShaderNodeTexImage")
        node.image = image
        node.location = (bsdf.location.x - 350, bsdf.location.y)
        tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
        return image

    def display_material(name: str, attribute: str, alpha: float = 1.0) -> bpy.types.Material:
        """A viewing material for Material Preview: the colour attribute GeoTools wrote."""
        material = bpy.data.materials.new(name)
        tree = material.node_tree
        bsdf = next(n for n in tree.nodes if n.type == "BSDF_PRINCIPLED")
        bsdf.inputs["Roughness"].default_value = 0.85
        node = tree.nodes.new("ShaderNodeVertexColor")
        node.layer_name = attribute
        node.location = (bsdf.location.x - 300, bsdf.location.y)
        tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
        if alpha < 1.0:
            bsdf.inputs["Alpha"].default_value = alpha
            material.surface_render_method = "BLENDED"
        return material

    def ghost_material(photo: bpy.types.Material) -> bpy.types.Material:
        """The photo material, see-through, to show what lies under the ground."""
        material = photo.copy()
        material.name = "View Ghost"
        bsdf = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        bsdf.inputs["Alpha"].default_value = 0.55
        material.surface_render_method = "BLENDED"
        return material

    def colour_material(name: str, rgba, emission: float = 0.0) -> bpy.types.Material:
        material = bpy.data.materials.new(name)
        bsdf = next(n for n in material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        bsdf.inputs["Base Color"].default_value = rgba
        bsdf.inputs["Roughness"].default_value = 1.0
        bsdf.inputs["Specular IOR Level"].default_value = 0.0
        if emission:
            bsdf.inputs["Emission Color"].default_value = rgba
            bsdf.inputs["Emission Strength"].default_value = emission
        material.diffuse_color = rgba
        return material

    def tube_modifier(obj: bpy.types.Object, radius: float, material) -> None:
        """Viewport-only display of a line object as thin tubes (lines are 1 px otherwise)."""
        group = bpy.data.node_groups.new(f"Display {obj.name}", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        nodes, links = group.nodes, group.links
        gin = nodes.new("NodeGroupInput")
        gout = nodes.new("NodeGroupOutput")
        to_curve = nodes.new("GeometryNodeMeshToCurve")
        circle = nodes.new("GeometryNodeCurvePrimitiveCircle")
        circle.inputs["Resolution"].default_value = 6
        circle.inputs["Radius"].default_value = radius
        to_mesh = nodes.new("GeometryNodeCurveToMesh")
        lift = nodes.new("GeometryNodeTransform")
        lift.inputs["Translation"].default_value = (0.0, 0.0, radius * 0.6)
        set_mat = nodes.new("GeometryNodeSetMaterial")
        set_mat.inputs["Material"].default_value = material
        links.new(gin.outputs[0], to_curve.inputs["Mesh"])
        links.new(to_curve.outputs["Curve"], to_mesh.inputs["Curve"])
        links.new(circle.outputs["Curve"], to_mesh.inputs["Profile Curve"])
        links.new(to_mesh.outputs["Mesh"], lift.inputs["Geometry"])
        links.new(lift.outputs["Geometry"], set_mat.inputs["Geometry"])
        links.new(set_mat.outputs["Geometry"], gout.inputs[0])
        modifier = obj.modifiers.new("Display Tubes", "NODES")
        modifier.node_group = group
        modifier.show_render = False

    def synthetic_dem() -> None:
        """The demo terrain as a 1 m DEM GeoTIFF in EPSG:32631, linked like a user's."""
        import _geotiff

        half = demo.SIZE / 2
        cols = np.arange(int(demo.SIZE))
        xs, ys = np.meshgrid(-half + cols + 0.5, half - cols - 0.5)
        z = demo.A0 + demo.terrain_height(xs, ys)
        spec = _geotiff.GeoTiffSpec(
            origin=(demo.E0 - half, demo.N0 + half), pixel_size=(1.0, 1.0), epsg=32631
        )
        path = work / "terrain_dem.tif"
        _geotiff.write_geotiff(path, z.astype(np.float32), spec, compression="deflate")
        run_op(bpy.ops.geotools.import_geotiff, filepath=str(path))
        dem = bpy.data.objects.get("terrain_dem")
        if dem is None:
            fail("the GeoTIFF import made no terrain_dem object")
        dem.hide_set(True)

    def wait_publish():
        publish = mod("blender.publish")
        for _ in range(1200):
            yield 0.25
            if not publish.is_running():
                break
        else:
            fail("the publish did not finish in 5 minutes")
        job = publish.active_job()
        if job is not None and job.state.name != "DONE":
            fail(f"the publish ended {job.state.name}: {job.error}")

    state: dict = {}

    def prepare():
        """Everything a user would have done on the demo before these shots."""
        image = add_photo_texture()
        state["image"] = image
        select(terrain)
        run_op(bpy.ops.geotools.analysis_add, preset="slope")
        layers = mod("blender.layers")
        for item in props.layers:
            layers.refresh_layer(scene, item)
        props.layers_index = list(props.layers.keys()).index("slope")
        slope = props.layers["slope"]
        slope.presentation.range_min = "0"  # the demo is gentle: 0 to 11 degrees
        slope.presentation.range_max = "10"
        run_op(bpy.ops.geotools.layer_write_presentation)
        if "gt_present" not in terrain.data.color_attributes:
            fail("Write Colours left no gt_present attribute")
        yield 0.2
        try:  # the "taken into the GeoScene keys" notice: a user reads it and closes it
            run_op(bpy.ops.geotools.dismiss_georef_notice)
        except MakeError:
            meta["errors"].pop()
        run_op(bpy.ops.geotools.verify_placement)
        run_op(bpy.ops.geotools.run_checks)

        contour = scene.geotools_contours
        contour.interval = "0.25"
        contour.major_every = 4
        run_op(bpy.ops.geotools.contours)
        hydro = scene.geotools_hydrology
        hydro.flow_lines = True
        hydro.channel_area = 60.0
        hydro.channel_count = 12
        hydro.depression_points = False
        run_op(bpy.ops.geotools.hydrology)
        analysis = bpy.data.collections.get("GeoTools Analysis")
        if analysis is None:
            fail("contours and hydrology made no GeoTools Analysis collection")
        state["contours"] = bpy.data.objects.get(contour.contour_name)
        state["flow"] = next(
            (o for o in analysis.objects if o is not state["contours"] and o.type == "MESH"), None
        )
        if state["contours"] is None or state["flow"] is None:
            fail(f"expected contours and flow paths in {sorted(o.name for o in analysis.objects)}")
        ink = colour_material("Contour Ink", (0.10, 0.07, 0.05, 1.0))
        water = colour_material("Flow Blue", (0.05, 0.35, 0.95, 1.0), emission=0.6)
        tube_modifier(state["contours"], 0.06, ink)
        tube_modifier(state["flow"], 0.11, water)
        for layer in props.layers:
            layers.refresh_layer(scene, layer)
        props.layers_index = list(props.layers.keys()).index("slope")

        layer_collection = bpy.context.view_layer.layer_collection.children["Contexts"]
        bpy.context.view_layer.active_layer_collection = layer_collection
        synthetic_dem()

        state["photo_material"] = terrain.data.materials[0]
        # How far Cesium lowers the site to sit it on the bare ellipsoid (its lowest point
        # 5 cm above it): the offset's A plus the terrain's lowest local height.
        zs = np.empty(len(terrain.data.vertices) * 3, np.float32)
        terrain.data.vertices.foreach_get("co", zs)
        meta["site_dz"] = round(demo.A0 + float(zs[2::3].min()) - 0.05, 3)
        state["slope_material"] = display_material("View Slope", "gt_present")
        state["ghost_material"] = ghost_material(state["photo_material"])
        bpy.context.view_layer.update()

    def publish_all():
        select(terrain)
        run_op(bpy.ops.geotools.texture_fill_holes)
        profile = props.build_profiles[0]  # "Site"
        props.build_profile_index = 0
        profile.overwrite = True
        profile.variant_layer = "slope"
        run_op(bpy.ops.geotools.publish)
        yield from wait_publish()
        profile.variant_layer = ""
        run_op(bpy.ops.geotools.publish)
        yield from wait_publish()
        for name in ("site.3tz", "site.slope.3tz"):
            if not (work / name).is_file():
                fail(f"the publish wrote no {name}")
        run_op(bpy.ops.geotools.run_checks)

    # -- viewport ------------------------------------------------------------------------

    def set_visible(show: set[str]) -> None:
        """Show only these objects (by name)."""
        for obj in bpy.context.view_layer.objects:
            obj.hide_set(obj.name not in show)

    def frame(rotation_deg, target, distance, lens=50.0, shift_px=(0.0, 0.0)) -> None:
        """An oblique perspective view; ``shift_px`` moves the target on screen (right, up).

        The shift is measured, not assumed: the target is projected into the region and
        the view moved until it lands where asked."""
        from bpy_extras.view3d_utils import location_3d_to_region_2d

        _window, area, main = view3d()
        space = area.spaces.active
        r3d = space.region_3d
        r3d.view_perspective = "PERSP"
        space.lens = lens
        space.clip_start = 0.1
        space.clip_end = 2000.0
        rot = Euler([math.radians(a) for a in rotation_deg], "XYZ").to_quaternion()
        r3d.view_rotation = rot
        r3d.view_distance = distance
        r3d.view_location = Vector(target)
        right = rot @ Vector((1.0, 0.0, 0.0))
        up = rot @ Vector((0.0, 1.0, 0.0))
        goal = Vector((main.width / 2 + shift_px[0], main.height / 2 + shift_px[1]))
        for _ in range(4):
            r3d.update()
            at = location_3d_to_region_2d(main, r3d, Vector(target))
            step = location_3d_to_region_2d(main, r3d, Vector(target) + right)
            step_up = location_3d_to_region_2d(main, r3d, Vector(target) + up)
            if at is None or step is None or step_up is None:
                fail("the framing target is behind the view")
            ppm_x = (step - at).length
            ppm_y = (step_up - at).length
            error = goal - at
            # Moving the view right moves the target left on screen.
            r3d.view_location = (
                r3d.view_location - right * (error.x / ppm_x) - up * (error.y / ppm_y)
            )

    def tidy_viewport(shading: str = "MATERIAL") -> None:
        _window, area, _main = view3d()
        space = area.spaces.active
        space.show_region_ui = True
        space.show_region_toolbar = False
        space.show_region_tool_header = False
        space.show_gizmo_navigate = False
        space.show_gizmo_tool = False
        overlay = space.overlay
        overlay.show_floor = False
        overlay.show_axis_x = overlay.show_axis_y = overlay.show_axis_z = False
        overlay.show_cursor = False
        overlay.show_object_origins = False
        overlay.show_relationship_lines = False
        overlay.show_text = False
        overlay.show_stats = False
        overlay.show_outline_selected = False
        overlay.show_extras = False
        overlay.show_bones = False
        shade = space.shading
        shade.type = shading
        if shading == "MATERIAL":
            shade.use_scene_world = False
            shade.use_scene_lights = False
            shade.studio_light = "forest.exr"
            shade.studiolight_rotate_z = math.radians(120)
            shade.studiolight_intensity = 1.15
            shade.studiolight_background_alpha = 0.0
            shade.studiolight_background_blur = 0.5

    def grab(path: Path) -> np.ndarray:
        """The window's pixels via screen.screenshot, (h, w, 4) float, top row first."""
        window = bpy.context.window_manager.windows[0]
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.screenshot(filepath=str(path), check_existing=False)
        image = bpy.data.images.load(str(path))
        width, height = image.size
        pixels = np.empty(width * height * 4, np.float32)
        image.pixels.foreach_get(pixels)
        bpy.data.images.remove(image)
        return pixels.reshape(height, width, 4)[::-1]

    def settle(min_spread: float = 0.008):
        """Wait until the viewport has drawn its objects (EEVEE compiles shaders lazily)
        and two grabs in a row agree."""
        window, _area, main = view3d()
        probe = raw / ".probe.png"
        previous = None
        for _ in range(80):
            yield 0.5
            pixels = grab(probe)
            sx = pixels.shape[1] / window.width
            sy = pixels.shape[0] / window.height
            y1 = pixels.shape[0] - round(main.y * sy)
            y0 = y1 - round(main.height * sy)
            x0 = round(main.x * sx)
            view = pixels[y0:y1, x0 : x0 + round(main.width * sx), :3]
            spread = float(view.std())
            if (
                previous is not None
                and spread > min_spread
                and float(np.abs(view - previous).max(axis=-1).mean()) < 0.002
            ):
                break
            previous = view
        else:
            fail("the viewport did not settle in 40 s")
        probe.unlink(missing_ok=True)

    def screenshot(name: str) -> dict:
        """The whole window as raw/<name>.png, and the rectangles that matter (top-left px)."""
        window, area, main = view3d()
        ui = sidebar()
        header = next((r for r in area.regions if r.type == "HEADER"), None)
        path = raw / f"{name}.png"
        pixels = grab(path)
        height, width = pixels.shape[:2]
        sx, sy = width / window.width, height / window.height

        def rect(r) -> list[int]:
            x0 = round(r.x * sx)
            y1 = height - round(r.y * sy)
            return [x0, y1 - round(r.height * sy), round(r.width * sx), round(r.height * sy)]

        info = {
            "file": path.name,
            "size": [width, height],
            "area": rect(area),
            "main": rect(main),
            "sidebar": rect(ui),
            "header": rect(header) if header else None,
            "ui_scale": bpy.context.preferences.view.ui_scale,
        }
        spread = float(pixels.reshape(-1, 4)[:, :3].std())
        if spread < 0.02:
            fail(f"the {name} screenshot is flat (std {spread:.4f})")
        meta["shots"][name] = info
        print(f"README-IMAGES shot {name}: {info}", flush=True)
        return info

    # -- shots ------------------------------------------------------------------------------

    P = "GEOTOOLS_PT_"
    fresh: list = []

    def use_fresh_workspace():
        window = bpy.context.window_manager.windows[0]
        if window.screen.show_fullscreen or window.screen.name.endswith("-nonnormal"):
            _w, area, main = view3d()
            with bpy.context.temp_override(window=window, area=area, region=main):
                bpy.ops.screen.back_to_previous()
            yield 0.5
        if not fresh:
            fail("ran out of fresh workspaces")
        window.workspace = fresh.pop(0)
        yield 0.6
        _w, area, main = view3d()
        area.spaces.active.show_region_ui = True
        tidy_viewport()
        yield 0.3
        with bpy.context.temp_override(window=window, area=area, region=main):
            bpy.ops.screen.screen_full_area()
        yield 0.8
        tidy_viewport()
        yield from widen_sidebar(SIDEBAR_WIDTH)
        sidebar().active_panel_category = "GeoTools"
        yield 0.3

    def widen_sidebar(width: int):
        """Drag the sidebar's left edge, as a user would (Python cannot set its width)."""
        window, _area, _main = view3d()
        ui = sidebar()
        grow = width - ui.width
        if grow <= 2:
            return
        x0, y = ui.x + 1, ui.y + ui.height // 2
        window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x0, y=y)
        yield 0.1
        window.event_simulate(type="LEFTMOUSE", value="PRESS", x=x0, y=y)
        yield 0.1
        for step in range(1, 7):
            window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=x0 - grow * step // 6, y=y)
            yield 0.05
        window.event_simulate(type="LEFTMOUSE", value="RELEASE", x=x0 - grow, y=y)
        yield 0.3
        window.event_simulate(type="MOUSEMOVE", value="NOTHING", x=20, y=20)  # off the UI
        yield 0.2
        if abs(sidebar().width - width) > 8:
            fail(f"dragging the sidebar to {width} px left it at {sidebar().width} px")

    def thumb_shift() -> tuple[float, float]:
        """Screen offset (right, up) that centres the target in the viewport part of a
        thumbnail: the crop is the top-right THUMB of the area, sidebar and header included."""
        _window, area, main = view3d()
        ui = sidebar()
        header = next((r.height for r in area.regions if r.type == "HEADER"), 0)
        visible_w = THUMB[0] - ui.width
        visible_h = THUMB[1] - header
        return main.width / 2 - visible_w / 2, main.height / 2 - visible_h / 2

    def shoot(name: str, rotation, target, distance, thumb: bool = True, wide=None):
        """``wide``: (rotation, target, distance) of a second, centred view of the same
        scene, ``<name>_wide``, for the hero's montage (its 3D Viewport region only)."""
        if thumb:
            dx, dy = thumb_shift()
            frame(rotation, target, distance, shift_px=(dx, dy))
        else:
            frame(rotation, target, distance)
        yield from settle()
        screenshot(name)
        if wide is not None:
            frame(*wide)
            yield from settle()
            screenshot(f"{name}_wide")

    # The ring ditch around the mound: what a thumbnail's narrow viewport strip shows,
    # looked at steeply enough that the terrain fills the strip.
    ring = (8.0, -6.0, 3.0)

    def shot_textures():
        terrain.data.materials[0] = state["photo_material"]
        set_visible({"terrain"})
        select()
        mod("blender.texture_cleanup").settings(bpy.context).image = state["image"]
        show_panels(
            {
                P + "textures": True,
                P + "textures_cleanup": True,
            }
        )
        texture_settings = mod("blender.texture_cleanup").settings(bpy.context)
        texture_settings.mask_source = "IMAGE"  # two rows shorter than a key colour
        yield from use_fresh_workspace()
        # Close on the holes, north-west of the ring ditch.
        yield from shoot("textures", (40, 0, 24), state_hole, 40.0)

    def shot_hero():
        terrain.data.materials[0] = state["slope_material"]
        set_visible({"terrain", state["contours"].name})
        select()
        show_panels(
            {
                P + "project": False,
                P + "source": False,
                P + "layers": True,
                P + "layers_bake": False,
                P + "layers_display": True,
                P + "layers_details": False,
                P + "textures": False,
                P + "checks": False,
                P + "publish": True,
                P + "publish_settings": False,
                P + "publish_last": True,
                P + "inspect": False,
                P + "linked_data": False,
                P + "analysis": False,
                P + "about": False,
            }
        )
        yield from use_fresh_workspace()
        yield from shoot("hero", (58, 0, 36), (0.5, -1.0, 2.0), 78.0, thumb=False)

    def shot_georef():
        terrain.data.materials[0] = state["photo_material"]
        set_visible({"terrain"})
        select()
        # The demo keeps the offset's A = 100 m (its heights are RL); the panel then
        # warns that GeoScene tools read Z as absolute height. Shown with A = 0, the
        # BlenderGIS-compatible default, after the publish, so the tilesets keep A.
        properties = mod("blender.properties")
        properties.write_number(props, "source_offset_a", 0.0)
        run_op(bpy.ops.geotools.verify_placement)
        try:  # a notice about the change, if any: a user reads it and closes it
            run_op(bpy.ops.geotools.dismiss_georef_notice)
        except MakeError:
            meta["errors"].pop()
        show_panels({P + "project": True, P + "project_crs": False})
        yield from use_fresh_workspace()
        # The resolved CRS, the offsets and Verify Placement's result, not the CRS
        # picker: its open list would hide them.
        yield from shoot("georef", (40, 0, 38), ring, 90.0)
        properties.write_number(props, "source_offset_a", demo.A0)

    def shot_layers():
        terrain.data.materials[0] = state["slope_material"]
        set_visible({"terrain"})
        select()
        show_panels(
            {
                P + "layers": True,
                P + "layers_bake": False,
                P + "layers_display": True,
                P + "layers_details": False,
            }
        )
        yield from use_fresh_workspace()
        yield from shoot("layers", (40, 0, 32), ring, 90.0)

    def shot_water():
        terrain.data.materials[0] = state["photo_material"]
        set_visible({"terrain", state["contours"].name, state["flow"].name})
        select()
        show_panels(
            {
                P + "analysis": True,
                P + "analysis_hydrology": True,
                P + "analysis_contours": False,
            }
        )
        yield from use_fresh_workspace()
        yield from shoot(
            "water", (40, 0, 40), ring, 100.0, wide=((55, 0, 30), (0.0, -2.0, 2.0), 82.0)
        )

    def ground(x: float, y: float) -> tuple[float, float, float]:
        return (x, y, float(demo.terrain_height(np.array(x), np.array(y))))

    def shot_sky():
        """Solstice, equinox and lunar standstill sightlines from the top of the mound."""
        terrain.data.materials[0] = state["photo_material"]
        archaeo = mod("blender.analysis").settings(bpy.context).archaeo
        scene.cursor.location = ground(8.0, -6.0)
        run_op(bpy.ops.geotools.archaeo_observer_cursor)
        run_op(bpy.ops.geotools.archaeo_events)
        archaeo.sightline_length = 26.0  # inside the 60 m demo
        before = {o.name for o in bpy.data.objects}
        run_op(bpy.ops.geotools.archaeo_sightlines)
        made = [o for o in bpy.data.objects if o.name not in before and o.type == "MESH"]
        if len(made) != 1:
            fail(f"Create Sightlines made {sorted(o.name for o in made)}, not one mesh")
        lines = made[0]
        gold = colour_material("Sightline Gold", (1.0, 0.42, 0.02, 1.0), emission=0.5)
        tube_modifier(lines, 0.12, gold)
        set_visible({"terrain", lines.name})
        select()
        show_panels(
            {
                P + "analysis": True,
                P + "analysis_sun": False,
                P + "analysis_archaeo": True,
            }
        )
        yield from use_fresh_workspace()
        yield from shoot(
            "sky", (48, 0, 30), (8.0, -6.0, 3.5), 95.0, wide=((50, 0, 30), (6.0, -5.0, 3.0), 80.0)
        )

    def shot_terrain():
        """A viewshed from the field's low south-west corner: the mound hides its far side.
        Last, as Write Colours replaces the slope colours in gt_present."""
        scene.cursor.location = ground(-24.0, -18.0)
        select(terrain)
        run_op(bpy.ops.geotools.analysis_add, preset="viewshed")
        layers = mod("blender.layers")
        for item in props.layers:
            layers.refresh_layer(scene, item)
        if "viewshed" not in props.layers:
            fail(f"Add Analysis made no viewshed layer: {sorted(props.layers.keys())}")
        props.layers_index = list(props.layers.keys()).index("viewshed")
        props.layers["viewshed"].presentation.palette = "viridis"
        run_op(bpy.ops.geotools.layer_write_presentation)
        terrain.data.materials[0] = state["slope_material"]  # it shows gt_present
        set_visible({"terrain"})
        select(terrain)  # Presets lists the active object's analyses
        show_panels(
            {
                P + "analysis": True,
                P + "analysis_presets": True,
                P + "layers": True,
                P + "layers_bake": False,
                P + "layers_display": True,
                P + "layers_details": False,
            }
        )
        yield from use_fresh_workspace()
        yield from shoot(
            "terrain",
            (48, 0, 28),
            (10.0, -4.0, 3.0),
            105.0,
            wide=((52, 0, 28), (0.0, -2.0, 2.0), 92.0),
        )

    def shot_publish():
        terrain.data.materials[0] = state["photo_material"]
        set_visible({"terrain"})
        select()
        show_panels({P + "publish": True, P + "publish_settings": False, P + "publish_last": True})
        yield from use_fresh_workspace()
        yield from shoot("publish", (58, 0, 38), ring, 165.0)  # the Cesium view covers it

    def shot_formats():
        terrain.data.materials[0] = state["ghost_material"]
        set_visible({"terrain", "contexts.volumes"})
        select()
        show_panels({P + "linked_data": True})
        yield from use_fresh_workspace()
        # The ring ditch and, behind it, the spread to its north.
        yield from shoot("formats", (45, 0, 28), (9.0, 2.0, 3.0), 100.0)

    # -- the run -----------------------------------------------------------------------------

    def run():
        window = bpy.context.window_manager.windows[0]
        prefs = bpy.context.preferences
        prefs.system.use_region_overlap = False
        prefs.filepaths.use_auto_save_temporary_files = False
        prefs.view.show_tooltips = False
        meta["window"] = [window.width, window.height]
        # Fresh copies of the Layout workspace, before any sidebar has drawn GeoTools.
        layout = bpy.data.workspaces["Layout"]
        window.workspace = layout
        yield 0.5
        before = set(bpy.data.workspaces)
        for _ in range(len(SHOTS) + 1):
            with bpy.context.temp_override(window=window):
                bpy.ops.workspace.duplicate()
            yield 0.2
        fresh.extend(sorted(set(bpy.data.workspaces) - before, key=lambda w: w.name))
        for ws in fresh:
            ws.object_mode = "OBJECT"
        window.workspace = layout
        yield 0.5
        hide_other_tabs()
        yield from prepare()
        yield from shot_textures()
        yield from publish_all()
        yield from shot_hero()
        yield from shot_georef()
        yield from shot_layers()
        yield from shot_water()
        yield from shot_publish()
        yield from shot_formats()
        yield from shot_sky()
        yield from shot_terrain()
        restore_panels()

    steps = run()

    def finish() -> None:
        (raw / "shots.json").write_text(json.dumps(meta, indent=1))
        bpy.ops.wm.quit_blender()

    def tick():
        try:
            return next(steps)
        except StopIteration:
            pass
        except MakeError:
            log.error("the run stopped: %s", meta["errors"][-1] if meta["errors"] else "")
        except Exception as ex:
            log.exception("the image run failed")
            meta["errors"].append(f"{type(ex).__name__}: {ex}")
        with contextlib.suppress(Exception):
            restore_panels()  # so the add-on unregisters cleanly on quit
        finish()
        return None

    bpy.app.timers.register(tick, first_interval=2.0)


# --- compose (inside `uv run` with Playwright, Pillow and pyoxipng) ------------------------------


# Cesium cameras: about the hero's Blender view (58 degrees from straight down), the
# site filling most of the frame; the hero's is checked to have the whole site in frame.
# The publish thumbnail's is a narrow strip: the site's middle, under a band of sky.
CESIUM_VIEW = {"heading": 324, "pitch": -35, "range": 100, "up": -4, "fit": 1}
CESIUM_THUMB_VIEW = {"heading": 324, "pitch": -26, "range": 75, "up": 10}
THUMBS = {  # thumbnail -> Blender shot
    "feature-georeference.png": "georef",
    "feature-layers.png": "layers",
    "feature-terrain.png": "terrain",
    "feature-sky.png": "sky",
    "feature-hydrology.png": "water",
    "feature-textures.png": "textures",
    "feature-formats.png": "formats",
    "feature-publish.png": "publish",  # with the Cesium view on the left
}
# The hero's montage: tile -> the Blender shot whose 3D Viewport region it shows, and
# how much of that region's middle it keeps (a centre crop, 16:10, then scaled).
MONTAGE = {
    "tile-slope.png": ("hero", 0.86),
    "tile-viewshed.png": ("terrain_wide", 0.9),
    "tile-sky.png": ("sky_wide", 0.9),
    "tile-water.png": ("water_wide", 0.9),
}
MONTAGE_SIZE = (800, 500)
CHROMIUM_ARGS = [
    "--use-angle=swiftshader",  # software WebGL: works headless, same pixels everywhere
    "--enable-unsafe-swiftshader",
    "--ignore-gpu-blocklist",
    "--font-render-hinting=none",
]


def compose_main(argv: list[str]) -> int:
    import functools
    import threading

    from PIL import Image, ImageStat
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser()
    parser.add_argument("--compose", action="store_true")
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    scratch, out = args.scratch, args.out
    raw, web = scratch / "raw", scratch / "web"
    meta = json.loads((raw / "shots.json").read_text())
    for note in meta.get("notes", []):
        print(f"note: {note}")
    shots_dir = web / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(out / TEMPLATES_REL, web / "templates", dirs_exist_ok=True)
    cesium_build = scratch / "cesium"
    if not (cesium_build / "Cesium.js").is_file():
        raise MakeError(f"no Cesium build in {cesium_build}")
    (web / "cesium").symlink_to(cesium_build, target_is_directory=True)

    def check(image: Image.Image, name: str, min_std: float = 12.0) -> None:
        """Fail on a blank or flat render."""
        std = ImageStat.Stat(image.convert("RGB")).stddev
        if max(std) < min_std:
            raise MakeError(
                f"{name} looks blank (channel std {', '.join(f'{v:.1f}' for v in std)})"
            )

    def area_crop(name: str) -> Image.Image:
        shot = meta["shots"][name]
        x, y, w, h = shot["area"]
        return Image.open(raw / shot["file"]).convert("RGB").crop((x, y, x + w, y + h))

    # The Blender shot: the 3D Viewport area (header, viewport and sidebar).
    blender = area_crop("hero")
    check(blender, "blender.png")
    blender.save(shots_dir / "blender.png")
    size = blender.size

    for filename, (name, keep) in MONTAGE.items():
        shot = meta["shots"][name]
        x, y, w, h = shot["main"]
        region = Image.open(raw / shot["file"]).convert("RGB").crop((x, y, x + w, y + h))
        aspect = MONTAGE_SIZE[0] / MONTAGE_SIZE[1]
        cw = min(w * keep, h * keep * aspect)
        ch = cw / aspect
        left, top = (w - cw) / 2, (h - ch) / 2
        tile = region.crop((round(left), round(top), round(left + cw), round(top + ch)))
        tile = tile.resize(MONTAGE_SIZE, Image.Resampling.LANCZOS)
        check(tile, filename)
        tile.save(shots_dir / filename)

    handler = functools.partial(_QuietHandler, directory=str(web))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(args=CHROMIUM_ARGS)
            except PlaywrightError:
                code = subprocess.run(
                    [sys.executable, "-m", "playwright", "install", "chromium"], check=False
                ).returncode
                if code != 0:
                    raise MakeError("playwright install chromium failed")
                browser = pw.chromium.launch(args=CHROMIUM_ARGS)

            def page_for(width: int, height: int):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.on("pageerror", lambda ex: print(f"page error: {ex}", file=sys.stderr))
                return page

            def cesium(
                width: int, height: int, view: dict, path: Path, tileset: str
            ) -> Image.Image:
                """Load at a quarter of the size (software WebGL draws a few frames a
                second), then draw the full size once everything has loaded."""
                page = page_for(max(width // 2, 320), max(height // 2, 200))
                query = "&".join(
                    f"{k}={v}"
                    for k, v in {**view, "dz": meta["site_dz"], "tileset": tileset}.items()
                )
                page.goto(f"{base}/templates/cesium.html?{query}")
                ready = "window.render && (window.render.ready || window.render.errors.length)"
                try:
                    page.wait_for_function(ready, timeout=300_000, polling=500)
                    page.set_viewport_size({"width": width, "height": height})
                    page.evaluate("window.render.final()")
                    page.wait_for_function(ready, timeout=300_000, polling=500)
                except PlaywrightError as ex:
                    raise MakeError(f"Cesium did not finish loading: {ex}")
                state = page.evaluate(
                    "({errors: window.render.errors, visible: window.render.visible,"
                    " outside: window.render.outside()})"
                )
                if state["errors"]:
                    raise MakeError(f"Cesium: {'; '.join(state['errors'])}")
                if state["visible"] == 0:
                    raise MakeError("Cesium drew no tile of the tileset")
                if view.get("fit") and state["outside"]:
                    raise MakeError(
                        f"{path.name}: {state['outside']} of the site's 8 bounding-box corners"
                        " are outside the frame (change its view)"
                    )
                page.evaluate("window.render.freeze()")
                page.screenshot(path=str(path), timeout=180_000)
                page.close()
                image = Image.open(path).convert("RGB")
                check(image, path.name)
                return image

            cesium(size[0], size[1], CESIUM_VIEW, shots_dir / "cesium.png", "site.slope")
            cesium_thumb = cesium(
                THUMB[0] - meta["shots"]["publish"]["sidebar"][2],
                THUMB[1],
                CESIUM_THUMB_VIEW,
                shots_dir / "cesium_thumb.png",
                "site",
            )

            def template(
                name: str,
                query: str,
                width: int,
                height: int,
                path: Path,
                *,
                element: str | None = None,
                transparent: bool = False,
            ) -> None:
                page = page_for(width, height)
                page.goto(f"{base}/templates/{name}?{query}")
                page.wait_for_function(
                    "document.documentElement.dataset.contours === 'done' && "
                    "Array.from(document.images).every(i => i.complete && i.naturalWidth > 0)",
                    timeout=30_000,
                )
                page.evaluate("document.fonts.ready")
                if not page.evaluate("document.fonts.check('800 40px Inter')"):
                    raise MakeError(f"{name}: the Inter font did not load")
                target = page.locator(element) if element else page
                target.screenshot(path=str(path), omit_background=transparent)
                page.close()
                check(Image.open(path), path.name)

            template(
                "hero-a.html",
                "mode=hero",
                1600,
                1000,
                out / "hero-a.png",
                element=".frame",
                transparent=True,
            )
            template("hero-a.html", "mode=social", 1280, 640, out / "social-a.png")
            template("banner.html", "theme=dark", 1280, 640, out / "social-b.png")
            # Hero option B: the designed banner, for the README and the site's home page.
            template("banner.html", "theme=dark", 1600, 640, out / "hero-b.png")
            template("banner.html", "theme=light", 1600, 640, out / "hero-b-light.png")
            browser.close()
    finally:
        server.shutdown()

    # Feature thumbnails: the top-right THUMB of the 3D Viewport area, sidebar included.
    for filename, name in THUMBS.items():
        shot = meta["shots"][name]
        x, y, w, _h = shot["area"]
        image = Image.open(raw / shot["file"]).convert("RGB")
        thumb = image.crop((x + w - THUMB[0], y, x + w, y + THUMB[1]))
        if name == "publish":
            thumb.paste(cesium_thumb, (0, 0))
        check(thumb, filename)
        # The sidebar alone is busy enough to pass; the viewport strip must not be empty.
        header = shot["header"][3] if shot["header"] else 0
        strip = thumb.crop((0, header, THUMB[0] - shot["sidebar"][2], THUMB[1]))
        check(strip, f"{filename} (its viewport)", min_std=6.0)
        thumb.save(out / filename)

    _optimise(out)
    return 0


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def _optimise(out: Path) -> None:
    """oxipng every PNG; quantise the social previews if they are still over GitHub's 1 MB."""
    import oxipng
    from PIL import Image

    for path in sorted(out.glob("*.png")):
        oxipng.optimize(path, level=4, strip=oxipng.StripChunks.safe())
        if path.name.startswith("social-") and path.stat().st_size > SOCIAL_LIMIT:
            image = Image.open(path).convert("RGB")
            image.quantize(
                colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG
            ).save(path)
            oxipng.optimize(path, level=4, strip=oxipng.StripChunks.safe())
            if path.stat().st_size > SOCIAL_LIMIT:
                raise MakeError(f"{path.name} is {path.stat().st_size} bytes, over 1 MB")


if __name__ == "__main__":
    if INSIDE_BLENDER:
        inner_main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
    elif "--compose" in sys.argv:
        sys.exit(compose_main(sys.argv[1:]))
    else:
        sys.exit(host_main())
