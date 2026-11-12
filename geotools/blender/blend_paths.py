"""The paths GeoTools stores in its own properties, against the open ``.blend``.

Thin Blender glue over :mod:`geotools.core.paths` (the rule is documented
there and in CONTRIBUTING.md, "File paths"):

- :func:`abspath` reads a stored path: ``//`` with either separator against
  the ``.blend``'s folder (or a linked library's), anything else as is. Use it
  in place of ``bpy.path.abspath``, which keeps backslashes on Linux and macOS.
- :func:`portable` is the text to store: ``//sub/file`` under the ``.blend``'s
  folder, absolute otherwise.
- :func:`image_path` is an image's file, for GeoTools' own checks of it.

Stored paths are rewritten only when GeoTools writes them: when it sets the
property, when the user edits one that has :func:`portable_update`, and on
saving to another folder (:func:`_on_save_pre`), where ``//`` paths are
re-expressed against the new folder as Blender does for its own data. Loading
a file never changes them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator

import bpy
from bpy.app.handlers import persistent

from ..core import paths

log = logging.getLogger(__name__)

PathRef = tuple[bpy.types.bpy_struct, str]


def blend_dir(library: bpy.types.Library | None = None) -> str | None:
    """Folder of the open ``.blend`` (or of ``library``'s file); None if unsaved."""
    if library is not None:
        try:
            return os.path.dirname(paths.resolve(library.filepath, blend_dir()))
        except paths.NotSavedError:
            return None
    filepath = bpy.data.filepath
    return os.path.dirname(filepath) if filepath else None


def abspath(text: str, library: bpy.types.Library | None = None) -> str:
    """The absolute path a stored path names (``text`` itself if it cannot say).

    A ``//`` path of an unsaved ``.blend`` comes back unchanged, so the file
    is reported missing rather than looked for somewhere else.
    """
    try:
        return paths.resolve(text, blend_dir(library))
    except paths.NotSavedError:
        return text


def portable(path: str | os.PathLike[str]) -> str:
    """The text to store for ``path`` in the open ``.blend``."""
    return paths.portable(path, blend_dir())


def image_path(image: bpy.types.Image) -> str:
    """An image's file, normalised, for GeoTools' own reading or checks of it."""
    return os.path.normpath(abspath(image.filepath, image.library))


def same_file(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return paths.same_file(a, b)


def portable_update(attr: str) -> Callable[[bpy.types.bpy_struct, bpy.types.Context], None]:
    """An ``update`` callback that stores a user-edited path property portably."""

    def update(self: bpy.types.bpy_struct, _context: bpy.types.Context) -> None:
        if _pending["saving"]:  # remap() and restore() store exactly what they mean
            return
        text = getattr(self, attr)
        stored = portable(text)
        if stored != text:  # the second call (from this assignment) changes nothing
            setattr(self, attr, stored)

    return update


# --- save: re-express // paths against the new folder ------------------------------------------


def stored_paths() -> Iterator[PathRef]:
    """Every path property GeoTools stores in the open ``.blend`` (local data only)."""
    for obj in bpy.data.objects:
        link = getattr(obj, "geotools_link", None)
        if obj.library is None and link is not None and link.filepath:
            yield link, "filepath"
    for scene in bpy.data.scenes:
        if scene.library is not None:
            continue
        props = getattr(scene, "geotools", None)
        if props is not None:
            for attr in ("source_filepath", "offset_file"):
                yield props, attr
            for profile in props.build_profiles:
                for attr in ("output_path", "last_output", "last_record"):
                    yield profile, attr
        inspect = getattr(scene, "geotools_inspect", None)
        if inspect is not None:
            yield inspect, "filepath"


def remap(old_dir: str | None, new_dir: str) -> list[tuple[PathRef, str]]:
    """Re-express the stored paths for a ``.blend`` moving from ``old_dir`` to ``new_dir``.

    ``//`` paths resolve against ``old_dir`` and are stored again for
    ``new_dir`` (relative if under it, absolute if not); absolute paths become
    relative if they are under ``new_dir``. Returns ``((owner, attr), old
    text)`` of each change, for :func:`restore`.
    """
    changed: list[tuple[PathRef, str]] = []
    _pending["saving"] = True
    try:
        for owner, attr in stored_paths():
            text = getattr(owner, attr)
            if not text:
                continue
            try:
                absolute = paths.resolve(text, old_dir)
            except paths.NotSavedError:
                continue  # nothing to resolve it against; leave it as it is
            stored = paths.portable(absolute, new_dir)
            if stored != text:
                setattr(owner, attr, stored)
                changed.append(((owner, attr), text))
    finally:
        _pending["saving"] = False
    return changed


def restore(changed: list[tuple[PathRef, str]]) -> None:
    _pending["saving"] = True
    try:
        for (owner, attr), text in changed:
            try:
                setattr(owner, attr, text)
            except ReferenceError:  # removed meanwhile
                pass
    finally:
        _pending["saving"] = False


_pending: dict[str, object] = {"old": None, "changed": [], "saving": False}


@persistent
def _on_save_pre(filepath: str = "", *_args: object) -> None:
    """Before saving to another folder (or a first save), store the paths for it."""
    _pending["changed"] = []
    _pending["old"] = None
    if not filepath:
        return
    old_dir = blend_dir()
    new_dir = os.path.dirname(filepath)
    if old_dir is not None and paths.real(old_dir) == paths.real(new_dir):
        return
    try:
        _pending["changed"] = remap(old_dir, new_dir)
        _pending["old"] = bpy.data.filepath
    except Exception:  # never break saving
        log.exception("re-expressing GeoTools paths for %s failed", filepath)


@persistent
def _on_save_post(*_args: object) -> None:
    """After a copy (the open file stays where it was), put the paths back."""
    changed = _pending["changed"]
    if changed and bpy.data.filepath == _pending["old"]:
        restore(changed)  # type: ignore[arg-type]
    _pending["changed"] = []
    _pending["old"] = None


@persistent
def _on_save_post_fail(*_args: object) -> None:
    restore(_pending["changed"])  # type: ignore[arg-type]
    _pending["changed"] = []
    _pending["old"] = None


classes: tuple[type, ...] = ()  # handlers only

_HANDLERS = (
    ("save_pre", _on_save_pre),
    ("save_post", _on_save_post),
    ("save_post_fail", _on_save_post_fail),
)


def register() -> None:
    for name, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler not in handlers:
            handlers.append(handler)


def unregister() -> None:
    for name, handler in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        if handler in handlers:
            handlers.remove(handler)
    _pending["changed"] = []
    _pending["old"] = None
