"""Portable paths for the file paths GeoTools stores in its own properties.

A project folder (the ``.blend`` and its data) should open on Linux, macOS
and Windows and still find its files. Blender reads its own ``//`` relative
paths with either separator on every OS, but ``bpy.path.abspath`` does not
convert backslashes, so GeoTools resolves the paths it stores itself here.

The rule (also in CONTRIBUTING.md, "File paths"):

- Writing (:func:`portable`): a path under the ``.blend``'s folder is stored
  Blender-relative, ``//sub/dir/file``, always with forward slashes. Anything
  else stays absolute, as the OS spells it (Windows drive and UNC paths too).
- Reading (:func:`resolve`): a ``//`` path, with ``/`` or ``\\`` separators,
  resolves against the ``.blend``'s folder on every OS. Any other path is
  returned as is. Nothing is searched for: a file that is not at the resolved
  path is reported missing by the caller.
- Comparing (:func:`same_file`): resolved paths (symlinks such as macOS
  ``/var`` -> ``/private/var``, Windows 8.3 short names), never the text.

Only ``//`` (two forward slashes) is relative, as in Blender; ``\\\\server``
is a Windows UNC path. The functions take the OS path module as ``pathmod``
(``os.path`` by default) so tests can pass ``ntpath`` or ``posixpath`` and
check Windows inputs on Linux.
"""

from __future__ import annotations

import os
import re
from types import ModuleType

BLEND_PREFIX = "//"
"""Blender's prefix for paths relative to the ``.blend``'s folder."""

_SEPARATORS = re.compile(r"[\\/]+")

StrPath = str | os.PathLike[str]


class NotSavedError(ValueError):
    """A ``//`` path was resolved for a ``.blend`` that has not been saved."""


def is_blend_relative(text: str) -> bool:
    """Whether ``text`` is a Blender ``//`` path (relative to the ``.blend``'s folder)."""
    return text.startswith(BLEND_PREFIX)


def relative_parts(text: str) -> list[str]:
    """The components of a ``//`` path, split at either separator (``.`` dropped)."""
    rest = text[len(BLEND_PREFIX) :] if is_blend_relative(text) else text
    return [part for part in _SEPARATORS.split(rest) if part and part != "."]


def name(text: str) -> str:
    """The file name of a stored path, split at either separator (for display)."""
    parts = [part for part in _SEPARATORS.split(text) if part]
    return parts[-1] if parts else ""


def resolve(text: str, blend_dir: StrPath | None, *, pathmod: ModuleType = os.path) -> str:
    """The path ``text`` names: a ``//`` path joined to ``blend_dir``, any other as is.

    ``//`` paths may use either separator and are normalised (``..`` collapsed)
    in ``pathmod``'s spelling. Raises :class:`NotSavedError` for a ``//`` path
    when ``blend_dir`` is None (the ``.blend`` was never saved).
    """
    if not is_blend_relative(text):
        return text
    if blend_dir is None:
        raise NotSavedError(f"{text} is relative to the .blend (//), which is not saved")
    return pathmod.normpath(pathmod.join(os.fspath(blend_dir), *relative_parts(text)))


def portable(path: StrPath, blend_dir: StrPath | None, *, pathmod: ModuleType = os.path) -> str:
    """The text GeoTools stores for ``path``.

    A ``//`` path is kept relative, with forward slashes. An absolute path
    under ``blend_dir`` becomes ``//sub/file``; anything else (another
    folder, another drive, a UNC share, no saved ``.blend``) is returned as is.
    The test is on the text, after ``normpath`` and ``normcase``, and, for
    the running OS's ``os.path``, on the resolved paths too (so a ``.blend``
    opened through a symlinked folder still counts its own files).
    """
    text = os.fspath(path)
    if not text:
        return ""
    if is_blend_relative(text):
        return BLEND_PREFIX + "/".join(relative_parts(text))
    if blend_dir is None or not pathmod.isabs(text):
        return text
    parts = relative_to(text, blend_dir, pathmod=pathmod)
    if parts is None and pathmod is os.path:
        parts = relative_to(os.path.realpath(text), os.path.realpath(blend_dir))
    return text if parts is None else BLEND_PREFIX + "/".join(parts)


def relative_to(
    path: StrPath, folder: StrPath, *, pathmod: ModuleType = os.path
) -> list[str] | None:
    """The components of absolute ``path`` below ``folder``, or None if it is not below it.

    Lexical: both are normalised with ``pathmod.normpath`` and compared
    component by component with ``pathmod.normcase`` (case-insensitive on
    Windows). ``folder`` itself is not below itself.
    """
    file_parts = _components(os.fspath(path), pathmod)
    dir_parts = _components(os.fspath(folder), pathmod)
    if file_parts is None or dir_parts is None or len(file_parts) <= len(dir_parts):
        return None
    for a, b in zip(file_parts, dir_parts, strict=False):
        if pathmod.normcase(a) != pathmod.normcase(b):
            return None
    return file_parts[len(dir_parts) :]


def _components(text: str, pathmod: ModuleType) -> list[str] | None:
    """Normalised components of an absolute path; the anchor (drive, root, share) first."""
    if not text or not pathmod.isabs(text):
        return None
    norm = pathmod.normpath(text)
    drive, rest = pathmod.splitdrive(norm)
    seps = "\\/" if pathmod.sep == "\\" else pathmod.sep
    parts = [part for part in re.split(f"[{re.escape(seps)}]+", rest) if part]
    return [drive + pathmod.sep, *parts]


def same_file(a: StrPath, b: StrPath) -> bool:
    """Whether ``a`` and ``b`` are the same file, comparing resolved paths.

    ``os.path.samefile`` when both exist; otherwise their ``realpath``,
    ``normcase`` spellings, so symlinked folders (macOS ``/var``) and Windows
    8.3 short names (``RUNNER~1``) still match.
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return real(a) == real(b)


def real(path: StrPath) -> str:
    """``path`` resolved for comparison: absolute, symlinks followed, case-normalised."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))
