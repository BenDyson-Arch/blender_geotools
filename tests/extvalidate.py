"""Run the official external validators on GeoTools output and judge their reports (tests only).

Shared by the core tests (pytest) and the Blender suite (its own runner), so it
needs only the standard library. The validators are commands in environment
variables; CI's wheel job installs the pins and sets them:

- ``GEOTOOLS_TEST_3D_TILES_VALIDATOR``: the 3d-tiles-validator CLI, pinned with
  its whole dependency tree in ``tests/validators/package-lock.json``. Run as
  ``<cli> --tilesetFile FILE`` (a ``tileset.json`` or a ``.3tz``) or
  ``<cli> --tileContentFile FILE`` (a GLB), with ``--reportFile REPORT``.
- ``GEOTOOLS_TEST_GLTF_VALIDATOR``: the Khronos glTF-Validator CLI (a release
  binary, pinned by checksum in ``.github/workflows/ci.yml``), run as
  ``<cli> -o FILE`` with the JSON report on stdout.

Unset, :func:`tiles` and :func:`gltf` return ``None`` and callers skip the
check, unless ``GEOTOOLS_TEST_REQUIRE_VALIDATORS=1`` (set in CI), when a
missing command fails instead, so a broken install cannot pass as a skip.

Errors always fail. A warning fails unless ``tests/validators/allowed.toml``
allows it (:func:`load_allowed`); infos and hints are ignored. GeoTools' own
warnings are fixed, never allowed: an allowance names the rusty-tiles or
validator issue that tracks it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import tomllib
from pathlib import Path

ALLOWED_TOML = Path(__file__).resolve().parent / "validators" / "allowed.toml"
TILES = "3d-tiles-validator"
GLTF = "gltf-validator"
_ENV = {TILES: "GEOTOOLS_TEST_3D_TILES_VALIDATOR", GLTF: "GEOTOOLS_TEST_GLTF_VALIDATOR"}
REQUIRE_ENV = "GEOTOOLS_TEST_REQUIRE_VALIDATORS"
TIMEOUT = 300


# An allowance names the issue that tracks it, in one of these repositories.
KNOWN_ISSUE_REPOSITORIES = (
    "BenDyson-Arch/rusty-tiles",
    "KhronosGroup/glTF-Validator",
    "CesiumGS/3d-tiles-validator",
)
_KNOWN_ISSUE = re.compile(
    r"https://github\.com/(" + "|".join(map(re.escape, KNOWN_ISSUE_REPOSITORIES)) + r")/issues/\d+"
)
# 3d-tiles-validator wraps any inner glTF warning in these: allowing one needs its message.
_WRAPPERS = frozenset({"CONTENT_VALIDATION_WARNING", "CONTENT_VALIDATION_ERROR"})


def load_allowed(path: Path = ALLOWED_TOML) -> dict[str, list[dict]]:
    """``{validator: [entry, ...]}`` from the allowlist, each entry checked.

    Each ``[[warning]]`` has ``validator`` (``3d-tiles-validator`` or
    ``gltf-validator``), ``code`` (the glTF-Validator code or the
    3d-tiles-validator type), ``reason``, and ``known_issue``: the rusty-tiles
    or validator issue that tracks it. ``message``, when given, must match the
    issue's message exactly; a content wrapper type needs one.
    """
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("warning", [])
    allowed: dict[str, list[dict]] = {TILES: [], GLTF: []}
    seen = set()
    for entry in entries:
        where = f"{path.name}: {entry}"
        unknown = set(entry) - {"validator", "code", "message", "reason", "known_issue"}
        assert not unknown, f"unknown keys {sorted(unknown)} in {where}"
        assert entry.get("validator") in allowed, f"bad validator in {where}"
        assert str(entry.get("code", "")).strip(), f"no code in {where}"
        assert len(str(entry.get("reason", "")).split()) >= 5, f"no reason in {where}"
        assert _KNOWN_ISSUE.fullmatch(str(entry.get("known_issue", ""))), (
            f"known_issue must link an issue in {', '.join(KNOWN_ISSUE_REPOSITORIES)}: {where}"
        )
        assert entry["code"] not in _WRAPPERS or entry.get("message"), (
            f"{entry['code']} wraps any content warning: give its message in {where}"
        )
        key = (entry["validator"], entry["code"], entry.get("message"))
        assert key not in seen, f"repeated {where}"
        seen.add(key)
        allowed[entry["validator"]].append(entry)
    return allowed


def _allows(entries: list[dict], code: object, message: object) -> bool:
    return any(e["code"] == code and e.get("message", message) == message for e in entries)


def command(validator: str) -> list[str] | None:
    """The validator's command from its environment variable, or ``None`` when unset."""
    value = os.environ.get(_ENV[validator], "").strip()
    if not value:
        assert os.environ.get(REQUIRE_ENV) != "1", f"{REQUIRE_ENV}=1 but {_ENV[validator]} is unset"
        return None
    return shlex.split(value)


def tiles() -> list[str] | None:
    return command(TILES)


def gltf() -> list[str] | None:
    return command(GLTF)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=TIMEOUT)


def _failure(validator: str, path: Path, done: subprocess.CompletedProcess, why: str) -> str:
    return (
        f"{validator} on {path.name}: {why}\n"
        f"exit code {done.returncode}\nstdout:\n{done.stdout[-4000:]}\nstderr:\n{done.stderr[-4000:]}"
    )


def _tiles_unexpected(issue: dict, allowed: list[dict]) -> bool:
    """Whether a 3d-tiles-validator issue (with its causes) holds anything not allowed.

    A content warning wraps the inner validator's issues as ``causes``: it is
    allowed when it is itself, or when every cause is.
    """
    severity = issue.get("severity")
    if severity == "ERROR":
        return True
    if severity != "WARNING" or _allows(allowed, issue.get("type"), issue.get("message")):
        return False
    causes = issue.get("causes") or []
    return not causes or any(_tiles_unexpected(c, allowed) for c in causes)


def _without_infos(issue: dict) -> dict:
    """The issue with its INFO causes dropped, so a failure shows only what failed."""
    causes = [_without_infos(c) for c in issue.get("causes") or [] if c.get("severity") != "INFO"]
    return (
        {**issue, "causes": causes} if causes else {k: v for k, v in issue.items() if k != "causes"}
    )


def tiles_problems(report: dict, allowed: list[dict]) -> list[dict]:
    """The 3d-tiles-validator issues that fail (any error, warnings not allowed), without infos."""
    bad = [_without_infos(i) for i in report.get("issues", []) if _tiles_unexpected(i, allowed)]
    if report.get("numErrors") and not bad:
        bad = [{"numErrors": report["numErrors"]}]
    return bad


def gltf_problems(report: dict, allowed: list[dict]) -> list[dict]:
    """The glTF-Validator messages that fail: any error, and warnings not allowed."""
    issues = report["issues"]
    # Severity: 0 error, 1 warning, 2 info, 3 hint.
    bad = [
        m
        for m in issues["messages"]
        if m["severity"] == 0
        or (m["severity"] == 1 and not _allows(allowed, m["code"], m.get("message")))
    ]
    if issues["numErrors"] and not bad:
        bad = [{"numErrors": issues["numErrors"]}]
    return bad


def check_3d_tiles(path: Path, *, content: bool = False) -> dict | None:
    """Validate a tileset (``tileset.json`` or ``.3tz``) or, with ``content``, a GLB tile.

    Returns the report, or ``None`` when the validator is not configured.
    """
    cmd = tiles()
    if cmd is None:
        return None
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix="geotools-3dtv-") as tmp:
        report_path = Path(tmp) / "report.json"
        flag = "--tileContentFile" if content else "--tilesetFile"
        done = _run([*cmd, flag, str(path), "--reportFile", str(report_path)])
        assert report_path.is_file(), _failure(TILES, path, done, "no report written")
        report = json.loads(report_path.read_text(encoding="utf-8"))
    bad = tiles_problems(report, load_allowed()[TILES])
    assert not bad, _failure(TILES, path, done, json.dumps(bad, indent=1)[:8000])
    return report


def check_gltf(path: Path) -> dict | None:
    """Validate a GLB with glTF-Validator; returns the report, or ``None`` when not configured."""
    cmd = gltf()
    if cmd is None:
        return None
    path = Path(path)
    done = _run([*cmd, "-o", str(path)])
    try:
        report = json.loads(done.stdout)
    except json.JSONDecodeError:
        raise AssertionError(_failure(GLTF, path, done, "no JSON report")) from None
    bad = gltf_problems(report, load_allowed()[GLTF])
    assert not bad, _failure(GLTF, path, done, json.dumps(bad, indent=1)[:8000])
    return report


def summary(report: dict | None) -> str:
    """A one-line count for the Blender suite's log."""
    if report is None:
        return "not configured"
    # glTF-Validator nests its counts under "issues"; 3d-tiles-validator lists issues there.
    counts = report["issues"] if isinstance(report.get("issues"), dict) else report
    return f"{counts.get('numErrors')} errors, {counts.get('numWarnings')} warnings"
