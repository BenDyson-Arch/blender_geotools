"""Which source textures changed since the last publish (core.texture_state)."""

from __future__ import annotations

import hashlib
import os

from geotools.core import preflight
from geotools.core.issues import Severity
from geotools.core.texture_state import (
    Change,
    HashCache,
    ImageState,
    Origin,
    Reason,
    compare,
    recorded_images,
    same_file,
    sha256_file,
    summary_text,
)


def _file(name: str, sha: str = "a" * 64, **kw) -> ImageState:
    return ImageState(name, Origin.FILE, sha, path=f"/t/{name}.png", bytes=10, mtime_ns=5, **kw)


def test_record_round_trip() -> None:
    state = _file("albedo", width=8192, height=8192)
    record = state.to_record()
    assert record == {
        "name": "albedo",
        "origin": "file",
        "sha256": "a" * 64,
        "path": "/t/albedo.png",
        "bytes": 10,
        "mtime_ns": 5,
        "width": 8192,
        "height": 8192,
    }
    assert ImageState.from_record(record) == state
    odd = ImageState.from_record({"name": "x", "origin": "?", "bytes": True, "sha256": ""})
    assert odd == ImageState("x", Origin.FILE)


def test_recorded_images_of_older_builds_is_unknown() -> None:
    assert recorded_images(None) is None
    assert recorded_images({"source": {"glb_sha256": "x"}}) is None
    assert recorded_images({"source": {"images": []}}) == []
    images = recorded_images({"source": {"images": [_file("a").to_record(), "junk"]}})
    assert images == [_file("a")]


def test_compare() -> None:
    recorded = [_file("a"), _file("b"), _file("c"), _file("gone")]
    current = [
        _file("a"),  # unchanged
        _file("b", "b" * 64),  # saved with new pixels
        ImageState("c", Origin.UNSAVED),  # painted, not saved yet
        _file("new"),
        ImageState("lost", Origin.FILE, None),  # file missing: hash unknown
    ]
    recorded.append(_file("lost"))
    changes = compare(current, recorded)
    assert [(c.name, c.reason) for c in changes] == [
        ("b", Reason.CHANGED),
        ("c", Reason.EDITED),
        ("gone", Reason.REMOVED),
        ("lost", Reason.CHANGED),
        ("new", Reason.NEW),
    ]
    assert changes[1].text == "c: unsaved edits"
    assert compare(current, None) is None
    assert compare([_file("a")], [_file("a")]) == []


def test_unsaved_counts_even_when_new() -> None:
    assert compare([ImageState("p", Origin.UNSAVED)], []) == [Change("p", Reason.EDITED)]


def test_same_file_needs_path_size_time_and_a_hash() -> None:
    old = _file("a")
    assert same_file(_file("a", None), old)
    assert not same_file(ImageState("a", Origin.FILE, None, "/t/a.png", 11, 5), old)
    assert not same_file(ImageState("a", Origin.FILE, None, "/t/a.png", 10, 6), old)
    assert not same_file(_file("a"), None)
    assert not same_file(_file("a"), ImageState("a", Origin.FILE, None, "/t/a.png", 10, 5))


def test_hash_cache(tmp_path) -> None:
    path = tmp_path / "t.png"
    path.write_bytes(b"pixels")
    stat = os.stat(path)
    cache = HashCache(limit=2)
    expected = hashlib.sha256(b"pixels").hexdigest()
    assert sha256_file(path) == expected
    assert cache.file(str(path), stat.st_size, stat.st_mtime_ns) == expected
    path.write_bytes(b"other!")  # same key (size and time faked): the cache answers
    assert cache.file(str(path), stat.st_size, stat.st_mtime_ns) == expected
    reads = []
    assert (
        cache.blob(("img", 1), lambda: reads.append(1) or b"x") == hashlib.sha256(b"x").hexdigest()
    )
    cache.blob(("img", 1), lambda: reads.append(1) or b"x")
    assert reads == [1]
    cache.forget(lambda k: k[0] == "blob")
    cache.blob(("img", 1), lambda: reads.append(1) or b"x")
    assert reads == [1, 1]
    cache.blob(("img", 2), lambda: b"y")
    cache.blob(("img", 3), lambda: b"z")  # over the limit: the oldest goes
    assert len(cache._hashes) == 2
    assert sha256_file(tmp_path / "missing.png") is None


def test_summary_text() -> None:
    assert summary_text(None) == "" and summary_text([]) == ""
    assert summary_text([Change("a", Reason.NEW)]) == "1 texture changed since last publish"
    assert summary_text([Change("a", Reason.NEW)] * 3) == "3 textures changed since last publish"


def test_preflight_flags_unsaved_images() -> None:
    def scene(image: preflight.ImageSummary) -> preflight.SceneSummary:
        material = preflight.MaterialSummary("m", True, images=(image,))
        obj = preflight.ObjectSummary(
            "o", "MESH", 1, 3, (0, 0, 0), (1, 1, 1), uv_layers=1, materials=(material,)
        )
        return preflight.SceneSummary(objects=(obj,))

    from geotools.core.placement import Placement

    painted = preflight.ImageSummary(
        "albedo", "/t/albedo.png", "PNG", False, True, 64, 64, unsaved_target="its file albedo.png"
    )
    issues = preflight.check(scene(painted), Placement())
    unsaved = [i for i in issues if i.code == "image-unsaved"]
    assert len(unsaved) == 1 and unsaved[0].subject == "albedo"
    assert unsaved[0].message == (
        "Image albedo has unsaved edits; Publish saves them to its file albedo.png first."
    )
    clean = preflight.ImageSummary("albedo", "/t/albedo.png", "PNG", False, True, 64, 64)
    assert not [i for i in preflight.check(scene(clean), Placement()) if i.code == "image-unsaved"]
    # Alongside other image rules: an EXR is still converted to PNG.
    exr = preflight.ImageSummary(
        "height", "", "OPEN_EXR", True, True, 64, 64, unsaved_target="the .blend (packed)"
    )
    codes = [i.code for i in preflight.check(scene(exr), Placement())]
    assert "image-format" in codes and "image-unsaved" in codes
    # A source file with an unsaved .blend: nowhere to put the copy, so an error.
    blocked = preflight.ImageSummary(
        "ortho",
        "/t/ortho.tif",
        "TIFF",
        False,
        True,
        64,
        64,
        unsaved_target="//textures/ once the .blend is saved",
        unsaved_blocked="save the .blend first",
    )
    (issue,) = [
        i for i in preflight.check(scene(blocked), Placement()) if i.code == "image-unsaved"
    ]
    assert issue.severity is Severity.ERROR and issue.subject == "ortho"
    assert issue.message == "Image ortho has unsaved edits, but save the .blend first."
