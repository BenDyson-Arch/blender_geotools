#!/usr/bin/env python3
"""Screenshot the GeoTools sidebar in a real Blender window (a dev tool).

Builds the extension, installs it into a throwaway Blender profile
(BLENDER_USER_RESOURCES), opens GUI Blender on an empty .blend, builds a demo
scene, screenshots the GeoTools sidebar and quits. A Blender window is open on
your screen for about 20 seconds.

It writes, in ``--out``:

* ``montage.png``: every panel open (DEFAULT_CLOSED ignored), the demo scene
  published once (when rusty-tiles is bundled, so Last Build shows), the
  sidebar scrolled top to bottom and the strips put side by side;
* ``montage_texture.png``: the same with the texture layer selected (after
  ``montage``, so it needs it);
* ``montage_glb.png``: the same with the second build profile selected, a
  ``.glb`` profile (the metadata GLB, with the face table), exported once
  (after ``montage``, so it needs it);
* ``state_empty.png``, ``state_mid.png``, ``state_ready.png``: what a user
  sees with the default open and closed panels, for an empty project, one
  half set up (placed, a source, an empty layer) and the full demo scene.

The demo scene is a 200 m terrain grid in GDA2020 / MGA zone 56 with a FACE
layer (``slope``, computed by a Geometry Nodes modifier), a CATEGORY layer
(``landcover``) and a TEXTURE layer (``shade``, baked from ``slope`` with
Cycles), a build profile whose variant is ``slope``, checks run, and two
linked sources in the Linked Data panel: a synthetic DEM GeoTIFF of the same
terrain (written with the test fixture writer, ``tests/blender/_geotiff.py``)
and a GeoJSON of four contexts.

After each shot it checks for clipped text (see "Clipped text" below) and
exits 1 if it finds any at the sidebar's default width; clipped text at the
narrow width is reported as a warning. The result is in ``clipping.json``;
each shot with clipped text also gets ``clipped_<shot>_<width>.png``, the
check's strips with the marks boxed in red.

Usage:
    python3 scripts/ui_screenshots.py --out /tmp/shots
    python3 scripts/ui_screenshots.py --out /tmp/shots --states montage
    xvfb-run -a -s "-screen 0 1920x1440x24 -dpi 96" \
        python3 scripts/ui_screenshots.py --out shots --gpu-backend vulkan

Clipped text:
* Blender clips text that does not fit its button with an ellipsis, in the
  middle of a label or at the end, and Python cannot see button sizes (the
  layout is resolved after ``draw()``, and ``UILayout.introspect()`` is empty
  there). So the check looks at pixels: it shoots the sidebar again with the
  text theme colours set to magenta, which makes a clean text mask, and finds
  ellipses in it (:func:`find_ellipses`). Text fields (``wcol_text``: paths,
  names, search fields) keep their colour: Blender clips the user's own
  values there by design.
* GeoTools' own ellipses ("Import Source Mesh...", ``ui.clipped``) would look
  the same, so during the check every text GeoTools passes to the layout has
  them swapped for glyphs of exactly the same width (:func:`neutral_text`,
  measured with ``blf``), which leaves the layout and Blender's clipping as
  they were.
* It checks at the sidebar's default width, which gates, and at
  ``--narrow`` (unscaled pixels), which only warns. Python cannot set a region's width, so the edge is dragged with
  simulated mouse events (Blender runs with ``--enable-event-simulate``,
  which also ignores your own mouse and keyboard while it runs).
* It first checks a calibration panel with two clipped and three
  intentional ellipses, and fails if it does not find exactly the two.

Notes:
* Only one sidebar tab can be shown and Python cannot pick the active tab, so
  the other View3D sidebar panels are unregistered to leave GeoTools alone.
* For the montage the GeoTools panels are registered again, in the add-on's
  own order, without DEFAULT_CLOSED, and shot in the Modeling workspace,
  whose sidebar has not drawn them yet (a region keeps each panel's open or
  closed state once it has drawn it).
* On a Wayland desktop without Xvfb Blender runs on Xwayland
  (``WAYLAND_DISPLAY=``); pass ``--native`` to leave the environment alone.
* The script is also the in-Blender part: Blender runs it with ``--inner``.

Stdlib only on the host side (Python 3.12+).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

try:
    import bpy  # only inside Blender

    INSIDE_BLENDER = True
except ImportError:
    INSIDE_BLENDER = False

STATES = ("empty", "mid", "ready", "montage", "texture", "glb")
WINDOW = (1800, 1350)
GUI_TIMEOUT = 900  # seconds
MONTAGE_WORKSPACE = "Modeling"  # a factory workspace with a 3D Viewport
NARROW = 240  # unscaled pixels; Blender's default sidebar is 280
REPORT = "clipping.json"


# --- clipped text (pure functions, tested in tests/core/test_ui_clipping.py) ------------------

ELLIPSES = ("...", "…")
# Same-width stand-ins for an intentional ellipsis, tried in order: three
# periods, then the ellipsis character. None reads as three lone dots (not
# "'''": apostrophes are dots at 11 px).
SUBSTITUTES = (
    ("!!!", "m"),
    ("iii", "M"),
    ("lll", "m"),
    ("|||", "—"),
)


def neutral_text(text: str, width) -> str:
    """``text`` with its ellipses swapped for stand-ins of exactly the same width.

    ``width(str) -> float`` measures with the UI font. Raises ``ValueError``
    when no stand-in matches, so a new string cannot slip past the check.
    """
    if not any(mark in text for mark in ELLIPSES):
        return text
    want = width(text)
    for dots, char in SUBSTITUTES:
        candidate = text.replace(ELLIPSES[0], dots).replace(ELLIPSES[1], char)
        if width(candidate) == want:
            return candidate
    raise ValueError(f"no same-width stand-in for the ellipsis in {text!r}")


def text_mask(pixels):
    """Where text drawn in magenta is: ``pixels`` (h, w, >=3) in 0..1 -> bool (h, w).

    Over a grey background ``min(r, b) - g`` is the text's coverage, so
    anti-aliased edges count from about a third.
    """
    import numpy as np

    return (np.minimum(pixels[..., 0], pixels[..., 2]) - pixels[..., 1]) > 0.35


def _components(mask) -> list[tuple[int, int, int, int]]:
    """Boxes ``(x0, y0, x1, y1)``, inclusive, of the 8-connected parts of ``mask``."""
    import numpy as np

    height, width = mask.shape
    grid = mask.tolist()
    boxes = []
    for y, x in zip(*(a.tolist() for a in np.nonzero(mask)), strict=True):
        if not grid[y][x]:
            continue
        grid[y][x] = False
        stack = [(y, x)]
        x0 = x1 = x
        y0 = y1 = y
        while stack:
            cy, cx = stack.pop()
            x0, x1, y0, y1 = min(x0, cx), max(x1, cx), min(y0, cy), max(y1, cy)
            for ny in (cy - 1, cy, cy + 1):
                if 0 <= ny < height:
                    row = grid[ny]
                    for nx in (cx - 1, cx, cx + 1):
                        if 0 <= nx < width and row[nx]:
                            row[nx] = False
                            stack.append((ny, nx))
        boxes.append((x0, y0, x1, y1))
    return boxes


def find_ellipses(mask, em: float) -> list[tuple[int, int, int, int]]:
    """Boxes ``(x0, y0, x1, y1)`` of ellipses in a text mask, top row first.

    ``em`` is the UI font size in pixels. An ellipsis is three dots (parts at
    most 0.3 em each way) on the same rows (within 1 px), evenly spaced
    (within 1 px) 0.15 to 0.5 em apart, with no text in the 0.5 em above
    them (a colon, the dots of "iii") or the 0.25 em below. Dots too near the
    top or bottom edge for those rows to be seen are skipped.
    """
    big = max(2, round(0.3 * em))
    dots = sorted(b for b in _components(mask) if b[2] - b[0] < big and b[3] - b[1] < big)
    found: list[tuple[int, int, int, int]] = []
    used: set[tuple[int, int, int, int]] = set()

    def level(a, b) -> bool:
        return abs(a[1] - b[1]) <= 1 and abs(a[3] - b[3]) <= 1

    for i, a in enumerate(dots):
        if a in used:
            continue
        for j in range(i + 1, len(dots)):
            b = dots[j]
            step = b[0] - a[0]
            if step > 0.5 * em:
                break
            if step < 0.15 * em or not level(a, b):
                continue
            c = next(
                (
                    c
                    for c in dots[j + 1 :]
                    if abs(c[0] - b[0] - step) <= 1 and c[0] > b[0] and level(a, c)
                ),
                None,
            )
            if c is None:
                continue
            x0, x1 = a[0], c[2]
            top, bottom = min(a[1], b[1], c[1]), max(a[3], b[3], c[3])
            if top - round(0.5 * em) < 0 or bottom + 2 + round(0.25 * em) > mask.shape[0]:
                break  # cut off by the edge: the rows around it are not all there
            above = mask[top - round(0.5 * em) : top - 1, x0 : x1 + 1]
            below = mask[bottom + 2 : bottom + 2 + round(0.25 * em), x0 : x1 + 1]
            if not above.any() and not below.any():
                found.append((x0, top, x1, bottom))
                used.update((a, b, c))
                break
    return found


# --- host side ---------------------------------------------------------------------------------


def host_main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from run_blender_tests import (
        RunError,
        blender_python,
        build,
        find_blender,
        install,
        isolated_env,
        remove_tree,
        zip_manifest,
    )

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="directory for the PNGs")
    parser.add_argument(
        "--blender",
        default=os.environ.get("BLENDER", "blender"),
        help="Blender executable (default: $BLENDER, else blender on PATH)",
    )
    parser.add_argument(
        "--states",
        default=",".join(STATES),
        help=f"comma-separated shots to take, from {', '.join(STATES)} (default: all)",
    )
    parser.add_argument("--zip", type=Path, help="install this extension zip instead of building")
    parser.add_argument(
        "--no-wheels", action="store_true", help="build without the rusty-tiles wheels"
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="keep WAYLAND_DISPLAY (by default Blender is pushed onto Xwayland)",
    )
    parser.add_argument(
        "--narrow",
        type=int,
        default=NARROW,
        help=f"the narrow sidebar width the clip check also uses, unscaled px (default {NARROW})",
    )
    parser.add_argument(
        "--gpu-backend",
        choices=("opengl", "vulkan"),
        help="Blender's GPU backend (default: Blender's). On Xvfb use vulkan (Mesa's "
        "lavapipe): with OpenGL (llvmpipe) Blender's screenshots come back black",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary profile")
    args = parser.parse_args(argv)
    states = [s.strip() for s in args.states.split(",") if s.strip()]
    unknown = sorted(set(states) - set(STATES))
    if unknown:
        parser.error(f"unknown states: {', '.join(unknown)}")
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / REPORT).unlink(missing_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="geotools-ui-shots-")).resolve()
    user_resources = tmp / "user"
    user_resources.mkdir()
    try:
        blender = find_blender(args.blender)
        env = isolated_env(user_resources)
        if args.zip is not None:
            zip_path = args.zip.expanduser().resolve()
        else:
            wheels = any(
                (Path(__file__).resolve().parents[1] / "geotools" / "wheels").glob("*.whl")
            )
            with_wheels = wheels and not args.no_wheels
            # Wheels fetched with --python for this Blender's CPython build only for it.
            python = blender_python(blender, env) if with_wheels else None
            zip_path = build(blender, tmp / "dist", with_wheels, env, python)
        ext_id = str(zip_manifest(zip_path)["id"])
        install(blender, zip_path, ext_id, env, user_resources)

        # An empty .blend to open: no splash screen, and a saved file for the
        # texture bake's images.
        work = tmp / "work"
        work.mkdir()
        blend = work / "demo.blend"
        subprocess.run(
            [
                blender,
                "--background",
                "--factory-startup",
                "--python-expr",
                f"import bpy; bpy.ops.wm.save_as_mainfile(filepath={str(blend)!r})",
            ],
            env=env,
            check=True,
            capture_output=True,
        )
        if not args.native:
            env["WAYLAND_DISPLAY"] = ""
        cmd = [
            blender,
            *(["--gpu-backend", args.gpu_backend] if args.gpu_backend else []),
            "--enable-event-simulate",  # the clip check drags the sidebar edge
            "--window-geometry",
            "0",
            "0",
            str(WINDOW[0]),
            str(WINDOW[1]),
            str(blend),
            "--python",
            str(Path(__file__).resolve()),
            "--",
            "--inner",
            "--package",
            f"bl_ext.user_default.{ext_id}",
            "--out",
            str(out),
            "--states",
            ",".join(states),
            "--narrow",
            str(args.narrow),
        ]
        print("$ " + " ".join(cmd), flush=True)
        result = subprocess.run(cmd, env=env, timeout=GUI_TIMEOUT, check=False)
        written = sorted(p.name for p in out.glob("*.png"))
        print(f"exit code {result.returncode}; {out}: {', '.join(written)}")
        return 1 if result.returncode != 0 else report_clipping(out / REPORT)
    except (RunError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 1
    finally:
        if args.keep:
            print(f"kept {tmp}")
        else:
            remove_tree(tmp)


def report_clipping(path: Path) -> int:
    """Print the clip check's result from ``path``.

    0 when it ran and found no clipped text at the default width. Clips at the
    narrow width are printed as warnings (GitHub annotations in Actions).
    """
    import json

    if not path.is_file():
        print(f"error: the run wrote no {path.name}", file=sys.stderr)
        return 1
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("error"):
        print(f"error: the run failed:\n{report['error']}", file=sys.stderr)
        return 1
    clipped = report["clipped"]
    failing = [c for c in clipped if c["fails"]]
    checked = ", ".join(f"{c['shot']} at {c['width']} px" for c in report["checked"])
    print(
        f"clip check: {len(failing)} clipped text(s) at the default width, "
        f"{len(clipped) - len(failing)} warning(s) at the narrow width; checked {checked}"
    )
    actions = os.environ.get("GITHUB_ACTIONS") == "true"
    for item in clipped:
        kind = "clipped" if item["fails"] else "warning, narrow width only"
        where = (
            f"{item['shot']} at {item['width']} px, box {item['box']} "
            f"(px from the sidebar's top left; see {item['image']})"
        )
        print(f"  {kind}: {where}")
        if actions:
            print(f"::{'error' if item['fails'] else 'warning'}::Clipped sidebar text: {where}")
    return 1 if failing else 0


# --- inside Blender -------------------------------------------------------------------------------


def inner_main(argv: list[str]) -> None:
    import importlib
    import logging

    import bpy
    import numpy as np

    parser = argparse.ArgumentParser()
    parser.add_argument("--inner", action="store_true")
    parser.add_argument("--package", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--states", required=True)
    parser.add_argument("--narrow", type=int, default=NARROW)
    args = parser.parse_args(argv)
    out: Path = args.out
    states = args.states.split(",")
    pkg = args.package

    def mod(name: str):
        return importlib.import_module(f"{pkg}.{name}")

    # -- the sidebar -------------------------------------------------------------------

    def ui_region():
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    region = next(r for r in area.regions if r.type == "UI")
                    return window, area, region
        raise RuntimeError("no 3D Viewport")

    def geotools_panels() -> list[type]:
        """GeoTools panel classes in registration order (the sidebar order)."""
        ordered: list[type] = []
        for module in mod("blender").MODULES:
            for cls in getattr(module, "classes", ()):
                if issubclass(cls, bpy.types.Panel) and cls not in ordered:
                    ordered.append(cls)
        return ordered

    def hide_other_tabs() -> None:
        ours = set(geotools_panels())
        # Children first, so parents unregister cleanly.
        panels = sorted(
            bpy.types.Panel.__subclasses__(), key=lambda c: not getattr(c, "bl_parent_id", "")
        )
        for cls in panels:
            if (
                getattr(cls, "bl_space_type", "") == "VIEW_3D"
                and getattr(cls, "bl_region_type", "") == "UI"
                and cls not in ours
            ):
                with contextlib.suppress(RuntimeError):
                    bpy.utils.unregister_class(cls)

    def open_all_panels() -> None:
        ordered = geotools_panels()
        for cls in reversed(ordered):
            bpy.utils.unregister_class(cls)
        for cls in ordered:
            cls.bl_options = set(getattr(cls, "bl_options", set())) - {"DEFAULT_CLOSED"}
            bpy.utils.register_class(cls)

    def prepare_view() -> None:
        window, area, _region = ui_region()
        space = area.spaces.active
        space.show_region_ui = True
        space.shading.type = "SOLID"
        space.shading.color_type = "VERTEX"
        space.overlay.show_floor = False
        main = next(r for r in area.regions if r.type == "WINDOW")
        with bpy.context.temp_override(window=window, area=area, region=main):
            bpy.ops.view3d.view_axis(type="TOP")
            bpy.ops.view3d.view_orbit(angle=0.9, type="ORBITDOWN")
            bpy.ops.view3d.view_all()

    # -- pixels --------------------------------------------------------------------------

    def screenshot_sidebar(tag: str) -> np.ndarray:
        """The UI region's pixels, as an (h, w, 4) float array, bottom row first."""
        window, _area, region = ui_region()
        path = out / f".window_{tag}.png"
        with bpy.context.temp_override(window=window):
            bpy.ops.screen.screenshot(filepath=str(path), check_existing=False)
        image = bpy.data.images.load(str(path))
        try:
            width, height = image.size
            pixels = np.empty(width * height * 4, np.float32)
            image.pixels.foreach_get(pixels)
        finally:
            bpy.data.images.remove(image)
            path.unlink(missing_ok=True)
        pixels = pixels.reshape(height, width, 4)
        # region.x and region.y are window-relative pixels; the screenshot may
        # be scaled from the window (HiDPI), so scale them into its pixels.
        sx, sy = width / window.width, height / window.height
        x0, y0 = round(region.x * sx), round(region.y * sy)
        x1, y1 = round((region.x + region.width) * sx), round((region.y + region.height) * sy)
        return pixels[y0:y1, x0:x1].copy()

    def save_png(pixels: np.ndarray, path: Path) -> None:
        height, width = pixels.shape[:2]
        image = bpy.data.images.new(path.stem, width, height, alpha=True)
        try:
            image.pixels.foreach_set(pixels.ravel())
            image.filepath_raw = str(path)
            image.file_format = "PNG"
            image.save()
        finally:
            bpy.data.images.remove(image)

    def side_by_side(strips: list[np.ndarray]) -> np.ndarray:
        height = max(s.shape[0] for s in strips)
        gap = np.ones((height, 8, 4), np.float32)
        parts = []
        for strip in strips:
            if strip.shape[0] < height:
                pad = np.zeros((height - strip.shape[0], *strip.shape[1:]), np.float32)
                pad[..., 3] = 1.0
                parts += [np.concatenate([pad, strip]), gap]
            else:
                parts += [strip, gap]
        return np.concatenate(parts[:-1], axis=1)

    def scroll(page_down: bool | None) -> None:
        window, area, region = ui_region()
        with bpy.context.temp_override(window=window, area=area, region=region):
            if page_down is None:
                for _ in range(400):
                    bpy.ops.view2d.scroll_up()
            else:
                bpy.ops.view2d.scroll_down(page=True)

    def scrolled() -> int:
        """How far down the sidebar is scrolled, in pixels."""
        _window, _area, region = ui_region()
        return round(-region.view2d.region_to_view(0, region.height)[1])

    def strips_of(tag: str):
        """Shoot the sidebar top to bottom (generator, yields delays).

        Returns the strips and how far down each one starts.
        """
        scroll(None)
        yield 0.6
        strips: list[np.ndarray] = []
        tops: list[int] = []
        for index in range(16):
            strip = screenshot_sidebar(f"{tag}_{index}")
            # The end is reached when a page scroll no longer moves the view. Live
            # text (free memory) may still change a few pixels between shots.
            if strips and strip.shape == strips[-1].shape:
                changed = np.any(np.abs(strip - strips[-1]) > 0.02, axis=-1).mean()
                if changed < 0.01:
                    break
            strips.append(strip)
            tops.append(scrolled())
            scroll(True)
            yield 0.5
        return strips, tops

    def capture(name: str):
        """Shoot the sidebar top to bottom, save the strips side by side, check for clipping."""
        strips, _tops = yield from strips_of(name)
        save_png(side_by_side(strips), out / f"{name}.png")
        print(f"UI-SHOTS {name}: {len(strips)} strip(s)", flush=True)
        yield from check_clipping(name)

    # -- clipped text (see the module docstring) -------------------------------------------

    report: dict = {"checked": [], "clipped": [], "calibration": None, "error": None}
    # Widget text colours the check turns magenta. Not wcol_text (the user's own
    # values in text, path and search fields) or wcol_tab (the category tabs).
    widgets = (
        "wcol_regular",
        "wcol_tool",
        "wcol_toolbar_item",
        "wcol_radio",
        "wcol_option",
        "wcol_toggle",
        "wcol_num",
        "wcol_numslider",
        "wcol_box",
        "wcol_menu",
        "wcol_pulldown",
        "wcol_list_item",
        "wcol_progress",
    )
    patched: list[tuple[type, str, object]] = []
    saved_theme: list[tuple[object, str, tuple]] = []

    def em() -> float:
        prefs = bpy.context.preferences
        return prefs.ui_styles[0].widget.points * prefs.system.ui_scale

    def text_width(text: str) -> float:
        import blf

        blf.size(0, em())
        return blf.dimensions(0, text)[0]

    class CheckLayout:
        """A UILayout whose texts have their ellipses neutralised (:func:`neutral_text`)."""

        def __init__(self, layout) -> None:
            object.__setattr__(self, "_layout", layout)

        def __getattr__(self, name: str):
            attr = getattr(self._layout, name)
            if not callable(attr):
                return attr

            def call(*args, **kwargs):
                for key in ("text", "heading"):
                    if isinstance(kwargs.get(key), str):
                        kwargs[key] = neutral_text(kwargs[key], text_width)
                return wrap(attr(*args, **kwargs))

            return call

        def __setattr__(self, name: str, value) -> None:
            setattr(self._layout, name, value)

    def wrap(value):
        if isinstance(value, bpy.types.UILayout):
            return CheckLayout(value)
        if isinstance(value, tuple):
            return tuple(wrap(v) for v in value)
        return value

    class PanelView:
        """A panel as its draw functions see it, with ``layout`` a :class:`CheckLayout`."""

        def __init__(self, panel, layout) -> None:
            object.__setattr__(self, "_panel", panel)
            object.__setattr__(self, "layout", layout)

        def __getattr__(self, name: str):
            attr = getattr(type(self._panel), name, None)
            if isinstance(attr, types.FunctionType):
                return types.MethodType(attr, self)  # helpers see self.layout too
            return getattr(self._panel, name)

    def patch(cls: type, name: str, make) -> None:
        original = getattr(cls, name, None)
        if original is None:
            return
        patched.append((cls, name, cls.__dict__.get(name)))
        setattr(cls, name, make(original))

    def panel_draw(original):
        def draw(self, context):
            return original(PanelView(self, CheckLayout(self.layout)), context)

        return draw

    def list_draw(original):
        # Blender passes as many arguments as the function names, so name them all.
        count = original.__code__.co_argcount - 3  # after self, context and layout

        def draw_item(
            self,
            context,
            layout,
            data,
            item,
            icon,
            active_data,
            active_property,
            index=0,
            flt_flag=0,
        ):
            rest = (data, item, icon, active_data, active_property, index, flt_flag)
            return original(self, context, CheckLayout(layout), *rest[:count])

        return draw_item

    def check_mode(classes: list[type]) -> None:
        """Magenta text, and neutralised ellipses in everything ``classes`` draw."""
        theme = bpy.context.preferences.themes[0]
        ui_theme = theme.user_interface
        targets = [(getattr(ui_theme, w), k) for w in widgets for k in ("text", "text_sel")]
        targets += [(ui_theme, "panel_title"), (ui_theme, "panel_text")]
        targets += [(theme.view_3d.space, k) for k in ("title", "text", "text_hi")]
        for owner, key in targets:
            value = tuple(getattr(owner, key))
            saved_theme.append((owner, key, value))
            setattr(owner, key, (1.0, 0.0, 1.0, 1.0)[: len(value)])
        for cls in classes:
            if issubclass(cls, bpy.types.Panel):
                patch(cls, "draw", panel_draw)
                patch(cls, "draw_header", panel_draw)
            elif issubclass(cls, bpy.types.UIList):
                patch(cls, "draw_item", list_draw)

    def normal_mode() -> None:
        while saved_theme:
            owner, key, value = saved_theme.pop()
            setattr(owner, key, value)
        while patched:
            cls, name, original = patched.pop()
            if original is None:
                delattr(cls, name)
            else:
                setattr(cls, name, original)

    def geotools_classes() -> list[type]:
        return [cls for module in mod("blender").MODULES for cls in getattr(module, "classes", ())]

    edge = {"offset": None}

    def set_sidebar_width(target: int):
        """Drag the sidebar's edge until it is ``target`` px wide (generator)."""
        window, area, region = ui_region()
        main = next(r for r in area.regions if r.type == "WINDOW")
        scale = bpy.context.preferences.system.ui_scale
        # The resize zone sits just left of the region (3 px at UI scale 1).
        offsets = [edge["offset"]] if edge["offset"] is not None else []
        offsets += [round(k * scale) for k in (-3, -2, -4)]
        for offset in offsets:
            if region.width == target:
                break
            before = region.width
            x, y = region.x + offset, region.y + region.height // 2
            to = x + before - target
            events = [("MOUSEMOVE", "NOTHING", x), ("LEFTMOUSE", "PRESS", x)]
            events += [("MOUSEMOVE", "NOTHING", x + (to - x) * k // 6) for k in range(1, 7)]
            events += [("LEFTMOUSE", "RELEASE", to)]
            for kind, value, ex in events:
                window.event_simulate(type=kind, value=value, x=ex, y=y)
                yield 0.05
            yield 0.4
            window, area, region = ui_region()
            if region.width != before:
                edge["offset"] = offset
        # Park the pointer over the viewport, away from any button.
        window.event_simulate(
            type="MOUSEMOVE", value="NOTHING", x=main.x + main.width // 3, y=main.y + 40
        )
        yield 0.3
        window, area, region = ui_region()
        if region.width != target:
            raise RuntimeError(f"the sidebar is {region.width} px wide, not {target}")

    def assemble(strips: list[np.ndarray], tops: list[int]) -> np.ndarray:
        """The strips placed where they were scrolled to: one tall image, top row first."""
        base = min(tops)
        height = max(t - base + s.shape[0] for s, t in zip(strips, tops, strict=True))
        canvas = np.zeros((height, strips[0].shape[1], 4), np.float32)
        canvas[..., 3] = 1.0
        for strip, top in zip(strips, tops, strict=True):
            canvas[top - base : top - base + strip.shape[0]] = strip[::-1]
        return canvas

    def marked(canvas: np.ndarray, boxes: list, page: int) -> np.ndarray:
        """``canvas`` with red boxes round ``boxes``, cut into pages side by side."""
        canvas = canvas.copy()
        height, width = canvas.shape[:2]
        red = np.array([1.0, 0.0, 0.0, 1.0], np.float32)
        for box in boxes:
            x0, x1 = max(box[0] - 2, 0), min(box[2] + 2, width - 1)
            y0, y1 = max(box[1] - 2, 0), min(box[3] + 2, height - 1)
            canvas[y0 : y1 + 1, [x0, x1]] = red
            canvas[[y0, y1], x0 : x1 + 1] = red
        return side_by_side([canvas[i : i + page][::-1] for i in range(0, height, page)])

    def check_clipping(name: str):
        """Shoot the sidebar again in check mode at both widths; record clipped text."""
        _window, _area, region = ui_region()
        default = region.width
        narrow = round(args.narrow * bpy.context.preferences.system.ui_scale)
        check_mode(geotools_classes())
        try:
            for width in (default, narrow):
                yield from set_sidebar_width(width)
                strips, tops = yield from strips_of(f"check_{name}_{width}")
                canvas = assemble(strips, tops)
                found = find_ellipses(text_mask(canvas), em())
                report["checked"].append({"shot": name, "width": width})
                if found:
                    image = f"clipped_{name}_{width}.png"
                    save_png(marked(canvas, found, strips[0].shape[0]), out / image)
                    report["clipped"] += [
                        {
                            "shot": name,
                            "width": width,
                            "fails": width == default,  # narrow-width clips only warn
                            "box": list(b),
                            "image": image,
                        }
                        for b in found
                    ]
                print(f"UI-SHOTS clip check {name} at {width} px: {len(found)}", flush=True)
        finally:
            normal_mode()
        yield from set_sidebar_width(default)

    class VIEW3D_PT_geotools_clip_calibration(bpy.types.Panel):
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Clip check"
        bl_label = "Clip check"

        def draw(self, context):
            layout = self.layout
            layout.label(text="Calibration: a label far too long for any sidebar, so it clips")
            layout.label(text="Ends on purpose\u2026", icon="INFO")
            layout.label(text="Three periods ... then more")
            layout.operator("wm.save_mainfile", text="Save As...")
            layout.label(text="Short, with 1.25 m, a:b and iii")
            row = layout.row()
            row.label(text="Left")
            row.operator("wm.save_mainfile", text="A button far too long for its half")

    calibration_found = 2  # the long label and the long button

    def calibrate():
        """Check the calibration panel; the run fails unless exactly its two clips are found."""
        cls = VIEW3D_PT_geotools_clip_calibration
        bpy.utils.register_class(cls)
        check_mode([cls])
        try:
            _window, area, region = ui_region()
            area.tag_redraw()
            yield 0.5  # the region lists a category once it has drawn it
            region.active_panel_category = cls.bl_category
            area.tag_redraw()
            yield 1.0
            strip = screenshot_sidebar("calibration")
        finally:
            normal_mode()
            bpy.utils.unregister_class(cls)
        found = find_ellipses(text_mask(strip[::-1]), em())
        report["calibration"] = {"expected": calibration_found, "found": len(found)}
        if len(found) != calibration_found:
            save_png(marked(strip[::-1], found, strip.shape[0]), out / "clipped_calibration.png")
            raise RuntimeError(
                f"the clip check found {len(found)} clipped texts in its calibration panel, "
                f"not {calibration_found} (see clipped_calibration.png)"
            )
        area.tag_redraw()
        yield 0.5

    # -- the demo scene ------------------------------------------------------------------

    def reset() -> None:
        scene = bpy.context.scene
        for collection in (bpy.data.objects, bpy.data.meshes, bpy.data.collections):
            for block in list(collection):
                collection.remove(block)
        for group in [g for g in bpy.data.node_groups if g.name.startswith("Demo")]:
            bpy.data.node_groups.remove(group)
        props = scene.geotools
        for prop in props.bl_rna.properties:
            name = prop.identifier
            if name in {"rna_type", "name"} or (prop.is_readonly and prop.type != "COLLECTION"):
                continue
            if prop.type == "COLLECTION":
                getattr(props, name).clear()
            else:
                props.property_unset(name)
        props.source_collection = None

    def terrain() -> bpy.types.Object:
        scene = bpy.context.scene
        site = bpy.data.collections.new("Site")
        scene.collection.children.link(site)
        mesh = bpy.data.meshes.new("Terrain")
        n = 121
        xs = np.linspace(-100.0, 100.0, n)
        x, y = np.meshgrid(xs, xs)
        z = 9 * np.sin(x / 23) * np.cos(y / 31) + 4 * np.sin((x + y) / 11)
        verts = np.stack([x, y, z], -1).reshape(-1, 3)
        i, j = np.meshgrid(np.arange(n - 1), np.arange(n - 1))
        a = (j * n + i).ravel()
        quads = np.stack([a, a + 1, a + n + 1, a + n], -1)
        mesh.vertices.add(len(verts))
        mesh.vertices.foreach_set("co", verts.astype(np.float32).ravel())
        mesh.loops.add(quads.size)
        mesh.loops.foreach_set("vertex_index", quads.astype(np.int32).ravel())
        mesh.polygons.add(len(quads))
        mesh.polygons.foreach_set("loop_start", np.arange(0, quads.size, 4, dtype=np.int32))
        mesh.polygons.foreach_set("loop_total", np.full(len(quads), 4, np.int32))
        uv = mesh.uv_layers.new(name="UVMap")
        uvs = ((verts[quads.ravel(), :2] + 100.0) / 200.0).astype(np.float32)
        uv.data.foreach_set("uv", uvs.ravel())
        mesh.update()
        mesh.validate()
        mesh.materials.append(bpy.data.materials.new("Ground"))
        obj = bpy.data.objects.new("Terrain", mesh)
        site.objects.link(obj)
        return obj

    def slope_modifier(obj: bpy.types.Object) -> None:
        """A Geometry Nodes modifier that stores the face slope in degrees as "slope"."""
        group = bpy.data.node_groups.new("Demo Slope", "GeometryNodeTree")
        group.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
        group.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
        nodes, links = group.nodes, group.links
        gin = nodes.new("NodeGroupInput")
        gout = nodes.new("NodeGroupOutput")
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type = "FLOAT"
        store.domain = "FACE"
        store.inputs["Name"].default_value = "slope"
        normal = nodes.new("GeometryNodeInputNormal")
        xyz = nodes.new("ShaderNodeSeparateXYZ")
        acos = nodes.new("ShaderNodeMath")
        acos.operation = "ARCCOSINE"
        deg = nodes.new("ShaderNodeMath")
        deg.operation = "MULTIPLY"
        deg.inputs[1].default_value = 57.29577951308232
        links.new(gin.outputs[0], store.inputs["Geometry"])
        links.new(normal.outputs[0], xyz.inputs[0])
        links.new(xyz.outputs[2], acos.inputs[0])
        links.new(acos.outputs[0], deg.inputs[0])
        links.new(deg.outputs[0], store.inputs["Value"])
        links.new(store.outputs[0], gout.inputs[0])
        modifier = obj.modifiers.new("Slope", "NODES")
        modifier.node_group = group

    def place(props) -> None:
        props.placement_mode = "SOURCE_CRS"
        props.crs = "EPSG:7856"
        props.origin_e = "502000.0"
        props.origin_n = "6961000.0"
        props.source_offset_a = "0.0"

    def run_op(op, **kwargs) -> None:
        try:
            op(**kwargs)
        except RuntimeError as ex:  # an operator ERROR report
            print(f"UI-SHOTS {op.idname_py()}: {ex}", flush=True)

    def build_mid() -> None:
        reset()
        scene = bpy.context.scene
        props = scene.geotools
        obj = terrain()
        props.source_collection = obj.users_collection[0]
        place(props)
        run_op(bpy.ops.geotools.refresh_source_summary)
        bpy.ops.geotools.layer_add(name="slope", domain="FACE", data_type="FLOAT")
        item = props.layers["slope"]
        item.label, item.units = "Slope", "deg"

    def build_ready() -> None:
        build_mid()
        scene = bpy.context.scene
        props = scene.geotools
        layers = mod("blender.layers")
        core = mod("core.layers")
        obj = props.source_collection.objects[0]
        mesh = obj.data
        slope_modifier(obj)
        item = props.layers["slope"]
        item.origin, item.description = "GEONODES", "Surface slope from face normals"
        item.source_attribute = "slope"  # the modifier's Store Named Attribute

        cover = core.LayerSpec(
            name="landcover",
            label="Land cover",
            domain=core.Domain.FACE,
            data_type=core.DataType.CATEGORY,
            origin=core.Origin.MANUAL,
            categories=("grass", "bare", "water"),
        )
        layers.ensure_layer(scene, cover)
        count = len(mesh.polygons)
        centres = np.empty(count * 3, np.float32)
        mesh.polygons.foreach_get("center", centres)
        z = centres.reshape(count, 3)[:, 2]
        layers.write_layer(mesh, cover, np.where(z < -6, 2, np.where(z > 8, 1, 0)).astype(np.int32))

        shade = core.LayerSpec(
            name="shade",
            label="Shade",
            domain=core.Domain.TEXTURE,
            data_type=core.DataType.FLOAT,
            units="deg",
        )
        shade_item = layers.ensure_layer(scene, shade)
        shade_item.resolution = 256
        shade_item.presentation.target = "BASE_COLOR"
        run_op(bpy.ops.geotools.bake_texture_layer, layer="shade", source_layer="slope")

        props.layers_index = list(props.layers.keys()).index("landcover")
        run_op(bpy.ops.geotools.layer_presentation_reset_colors)
        props.layers_index = 0
        for entry in props.layers:
            layers.refresh_layer(scene, entry)
        run_op(bpy.ops.geotools.layer_write_presentation)

        bpy.ops.geotools.build_profile_add()
        profile = props.build_profiles[0]
        profile.name = "Site"
        profile.output_path = bpy.path.abspath("//site.3tz")  # beside the temporary demo.blend
        profile.variant_layer = "slope"
        bpy.ops.geotools.build_profile_add()
        glb = props.build_profiles[1]
        glb.name = "Site GLB"
        glb.output_path = bpy.path.abspath("//site.glb")
        glb.output_type = "GLB"
        glb.glb_face_table = True
        props.build_profile_index = 0
        run_op(bpy.ops.geotools.run_checks)
        linked_dem()
        linked_contexts()
        if "gt_present" in mesh.color_attributes:
            mesh.color_attributes.active_color = mesh.color_attributes["gt_present"]
        bpy.ops.object.select_all(action="DESELECT")
        bpy.context.view_layer.objects.active = None

    def linked_dem() -> None:
        """The terrain as a 4 m DEM GeoTIFF in MGA zone 56, imported as linked data."""
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "blender"))
        import _geotiff

        cols = np.arange(50)
        x, y = np.meshgrid(-100.0 + 4.0 * (cols + 0.5), 100.0 - 4.0 * (cols + 0.5))
        z = 12.0 + 9 * np.sin(x / 23) * np.cos(y / 31) + 4 * np.sin((x + y) / 11)
        spec = _geotiff.GeoTiffSpec(origin=(501900.0, 6961100.0), pixel_size=(4.0, 4.0))
        path = Path(bpy.path.abspath("//terrain_dem.tif"))
        _geotiff.write_geotiff(path, z.astype(np.float32), spec, compression="deflate")
        run_op(bpy.ops.geotools.import_geotiff, filepath=str(path))
        dem = bpy.data.objects.get("terrain_dem")
        if dem is not None:
            dem.hide_set(True)  # the source terrain stays in view

    def linked_contexts() -> None:
        """A small synthetic GeoJSON of excavation contexts, imported as linked data."""
        import json

        features = []
        for k, (x, y) in enumerate(((-60, -40), (-20, 10), (30, -25), (45, 40))):
            e, n = 502000.0 + x, 6961000.0 + y
            ring = [[e, n], [e + 18, n], [e + 18, n + 12], [e, n + 12], [e, n]]
            features.append(
                {
                    "type": "Feature",
                    "id": k + 1,
                    "geometry": {"type": "Polygon", "coordinates": [ring]},
                    "properties": {"context": f"C{k + 1:03d}", "depth": 0.4 * (k + 1)},
                }
            )
        path = Path(bpy.path.abspath("//contexts.geojson"))
        path.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "crs": {"type": "name", "properties": {"name": "EPSG:7856"}},
                    "features": features,
                }
            )
        )
        run_op(bpy.ops.geotools.import_geojson, filepath=str(path))

    def build_empty() -> None:
        reset()

    builders = {"empty": build_empty, "mid": build_mid, "ready": build_ready}

    def publish_demo():
        """Publish the plain tileset, so Last Build has something to show."""
        if not mod("core.deps").probe_rusty_tiles().available:
            print("UI-SHOTS publish skipped: rusty-tiles is not bundled", flush=True)
            return
        props = bpy.context.scene.geotools
        profile = props.build_profiles[0]
        variant, profile.variant_layer = profile.variant_layer, ""
        profile.overwrite = True
        run_op(bpy.ops.geotools.publish)
        publish = mod("blender.publish")
        for _ in range(240):
            yield 0.25
            if not publish.is_running():
                break
        profile.variant_layer = variant

    # -- the run ---------------------------------------------------------------------------

    def run():
        hide_other_tabs()
        prepare_view()
        yield 1.0
        yield from calibrate()
        window, _area, region = ui_region()
        print(
            f"UI-SHOTS window {window.width}x{window.height}, sidebar at "
            f"({region.x}, {region.y}) {region.width}x{region.height}",
            flush=True,
        )
        for state in ("empty", "mid", "ready"):
            if state in states:
                builders[state]()
                prepare_view()
                yield 1.0
                yield from capture(f"state_{state}")
        if "montage" in states:
            # Panels keep their open or closed state per region, so the shots
            # above would leave DEFAULT_CLOSED panels closed. Another
            # workspace has its own 3D Viewport sidebar that has not drawn
            # GeoTools yet.
            build_ready()
            open_all_panels()
            window = bpy.context.window_manager.windows[0]
            window.workspace = bpy.data.workspaces[MONTAGE_WORKSPACE]
            yield 1.0
            hide_other_tabs()
            prepare_view()
            yield from publish_demo()
            yield 1.0
            yield from capture("montage")
        if "texture" in states and "montage" in states:
            props = bpy.context.scene.geotools
            props.layers_index = list(props.layers.keys()).index("shade")
            yield 1.0
            yield from capture("montage_texture")
        if "glb" in states and "montage" in states:
            props = bpy.context.scene.geotools
            props.layers_index = 0
            props.build_profile_index = 1
            props.build_profiles[1].overwrite = True
            run_op(bpy.ops.geotools.publish)
            publish = mod("blender.publish")
            for _ in range(240):
                yield 0.25
                if not publish.is_running():
                    break
            yield 1.0
            yield from capture("montage_glb")

    steps = run()

    def tick():
        import json
        import traceback

        try:
            return next(steps)
        except StopIteration:
            pass
        except Exception:
            logging.getLogger("ui_screenshots").exception("the screenshot run failed")
            report["error"] = traceback.format_exc()
        (out / REPORT).write_text(json.dumps(report, indent=1), encoding="utf-8")
        bpy.ops.wm.quit_blender()
        return None

    bpy.app.timers.register(tick, first_interval=1.5)


if __name__ == "__main__":
    if INSIDE_BLENDER:
        inner_main(sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else [])
    else:
        sys.exit(host_main())
