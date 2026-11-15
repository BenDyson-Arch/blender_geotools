"""The external validator pins and their judge (``tests/extvalidate.py``).

The validators themselves run in CI's wheel job; these checks keep the pins
exact and hashed, the allowlist reasoned, and the judging honest, without them.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

import extvalidate
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATORS = REPO_ROOT / "tests" / "validators"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


# --- pins -------------------------------------------------------------------------------------


def test_the_npm_pin_is_exact_and_every_package_is_hashed() -> None:
    package = json.loads((VALIDATORS / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"] == {"3d-tiles-validator": "0.6.1"}
    lock = json.loads((VALIDATORS / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] == 3
    assert lock["packages"][""]["dependencies"] == package["dependencies"]
    installed = {k: v for k, v in lock["packages"].items() if k}
    assert installed["node_modules/3d-tiles-validator"]["version"] == "0.6.1"
    unhashed = [
        name
        for name, entry in installed.items()
        if not entry.get("link")
        and not (
            str(entry.get("resolved", "")).startswith("https://registry.npmjs.org/")
            and str(entry.get("integrity", "")).startswith("sha512-")
        )
    ]
    assert not unhashed, unhashed


def _job(text: str, name: str) -> str:
    """One job's block of ci.yml: from ``  name:`` to the next top-level job."""
    start = text.index(f"\n  {name}:\n")
    following = re.search(r"\n  [\w-]+:\n", text[start + 1 :])
    return text[start : start + 1 + following.start()] if following else text[start:]


def test_the_wheel_job_requires_both_validators_at_exact_pins() -> None:
    text = CI.read_text(encoding="utf-8")
    assert re.search(r'^  NODE_VERSION: "\d+\.\d+\.\d+"$', text, re.MULTILINE), "pin Node exactly"
    assert re.search(r'^  GLTF_VALIDATOR_VERSION: "2\.0\.0-dev\.3\.10"$', text, re.MULTILINE)
    assert re.search(r'^  GLTF_VALIDATOR_SHA256: "[0-9a-f]{64}"$', text, re.MULTILINE)
    job = _job(text, "blender-wheel")
    assert 'GEOTOOLS_TEST_REQUIRE_VALIDATORS: "1"' in job
    assert "npm ci" in job and "tests/validators/package-lock.json" in job
    assert "sha256sum -c" in job
    for variable in ("GEOTOOLS_TEST_3D_TILES_VALIDATOR", "GEOTOOLS_TEST_GLTF_VALIDATOR"):
        assert f"{variable}=" in job, variable


# --- the allowlist ----------------------------------------------------------------------------


def test_the_allowlist_loads() -> None:
    allowed = extvalidate.load_allowed()
    assert set(allowed) == {extvalidate.TILES, extvalidate.GLTF}


GOOD = 'validator = "gltf-validator"\ncode = "X"\nreason = "a b c d e"\n'
ISSUE = 'known_issue = "https://github.com/BenDyson-Arch/rusty-tiles/issues/1"\n'


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ('validator = "gltf-validator"\ncode = "X"\n' + ISSUE, "no reason"),
        ('validator = "gltf-validator"\ncode = "X"\nreason = "fine"\n' + ISSUE, "no reason"),
        ('validator = "cesium"\ncode = "X"\nreason = "a b c d e"\n' + ISSUE, "bad validator"),
        ('validator = "gltf-validator"\nreason = "a b c d e"\n' + ISSUE, "no code"),
        (GOOD, "known_issue must link"),
        (GOOD + 'known_issue = "see the tracker"', "known_issue must link"),
        # GeoTools' own warnings are fixed, never allowed.
        (
            GOOD + 'known_issue = "https://github.com/BenDyson-Arch/blender_geotools/issues/1"',
            "known_issue must link",
        ),
        (GOOD + ISSUE + 'scope = "all"', "unknown"),
        (
            GOOD.replace('"X"', '"CONTENT_VALIDATION_WARNING"') + ISSUE,
            "wraps any content warning",
        ),
        (GOOD + ISSUE + "[[warning]]\n" + GOOD + ISSUE, "repeated"),
    ],
)
def test_the_allowlist_refuses_unreasoned_entries(tmp_path: Path, entry: str, message: str) -> None:
    path = tmp_path / "allowed.toml"
    path.write_text(f"[[warning]]\n{entry}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=message):
        extvalidate.load_allowed(path)


def test_an_allowance_names_its_issue(tmp_path: Path) -> None:
    path = tmp_path / "allowed.toml"
    path.write_text(f"[[warning]]\n{GOOD}{ISSUE}", encoding="utf-8")
    assert [e["code"] for e in extvalidate.load_allowed(path)[extvalidate.GLTF]] == ["X"]


# --- judging ----------------------------------------------------------------------------------

ALLOWED = [
    {"code": "ALLOWED_WARNING"},
    {"code": "CONTENT_VALIDATION_WARNING", "message": "this one only"},
]


def _gltf_report(*messages: tuple[str, int]) -> dict:
    return {
        "issues": {
            "numErrors": sum(s == 0 for _c, s in messages),
            "numWarnings": sum(s == 1 for _c, s in messages),
            "messages": [{"code": c, "severity": s, "message": c} for c, s in messages],
        }
    }


def test_gltf_errors_and_unlisted_warnings_fail() -> None:
    judge = extvalidate.gltf_problems
    assert judge(_gltf_report(("ALLOWED_WARNING", 1), ("SOME_INFO", 2), ("HINT", 3)), ALLOWED) == []
    assert [m["code"] for m in judge(_gltf_report(("OTHER", 1)), ALLOWED)] == ["OTHER"]
    # An allowance is for warnings only: the same code as an error still fails.
    assert [m["code"] for m in judge(_gltf_report(("ALLOWED_WARNING", 0)), ALLOWED)] == [
        "ALLOWED_WARNING"
    ]
    # A count with no message (truncated report) still fails.
    assert judge({"issues": {"numErrors": 1, "messages": []}}, ALLOWED)


def _issue(kind: str, severity: str, *causes: dict) -> dict:
    return {"type": kind, "severity": severity, "message": kind, "causes": list(causes)}


def test_tiles_errors_and_unlisted_warnings_fail_through_causes() -> None:
    judge = extvalidate.tiles_problems

    def report(*issues: dict) -> dict:
        return {"numErrors": sum(i["severity"] == "ERROR" for i in issues), "issues": list(issues)}

    assert judge(report(_issue("ALLOWED_WARNING", "WARNING"), _issue("X", "INFO")), ALLOWED) == []
    assert judge(report(_issue("OTHER", "WARNING")), ALLOWED)
    assert judge(report(_issue("ALLOWED_WARNING", "ERROR")), ALLOWED)
    # A content warning wrapping the inner validator's warnings: allowed only if all are.
    wrapped = _issue("CONTENT_VALIDATION_WARNING", "WARNING", _issue("ALLOWED_WARNING", "WARNING"))
    assert judge(report(wrapped), ALLOWED) == []
    mixed = _issue(
        "CONTENT_VALIDATION_WARNING",
        "WARNING",
        _issue("ALLOWED_WARNING", "WARNING"),
        _issue("OTHER", "WARNING"),
    )
    assert judge(report(mixed), ALLOWED)
    assert judge({"numErrors": 2, "issues": []}, ALLOWED)
    # A wrapper type is allowed only with the listed message.
    listed = {
        "type": "CONTENT_VALIDATION_WARNING",
        "severity": "WARNING",
        "message": "this one only",
    }
    other = {**listed, "message": "something else"}
    assert judge(report(_issue("W", "WARNING", listed)), ALLOWED) == []
    assert judge(report(_issue("W", "WARNING", other)), ALLOWED)
    # A failure lists what failed, not the infos beside it.
    info = _issue("SOME_INFO", "INFO")
    assert judge(report(_issue("W", "WARNING", other, info)), ALLOWED) == [
        {"type": "W", "severity": "WARNING", "message": "W", "causes": [other]}
    ]


# --- commands ---------------------------------------------------------------------------------


def test_a_required_validator_cannot_be_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEOTOOLS_TEST_GLTF_VALIDATOR", raising=False)
    monkeypatch.delenv(extvalidate.REQUIRE_ENV, raising=False)
    assert extvalidate.gltf() is None
    assert extvalidate.check_gltf(Path("missing.glb")) is None
    monkeypatch.setenv(extvalidate.REQUIRE_ENV, "1")
    with pytest.raises(AssertionError, match="GEOTOOLS_TEST_GLTF_VALIDATOR is unset"):
        extvalidate.gltf()


FAKE_GLTF = """
import json, sys
from pathlib import Path
code = Path(sys.argv[-1]).stem.upper()
severity = {"CLEAN": None, "WARNED": 1, "BROKEN": 0}[code]
messages = [] if severity is None else [{"code": code, "severity": severity, "message": "m"}]
print(json.dumps({"issues": {"numErrors": int(severity == 0), "messages": messages}}))
print("stderr detail", file=sys.stderr)
"""


def test_check_gltf_reports_the_validator_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake_gltf_validator.py"
    fake.write_text(FAKE_GLTF, encoding="utf-8")
    monkeypatch.setenv("GEOTOOLS_TEST_GLTF_VALIDATOR", shlex.join([sys.executable, str(fake)]))
    assert extvalidate.check_gltf(tmp_path / "clean.glb") is not None
    for name in ("warned", "broken"):
        with pytest.raises(AssertionError, match=r"(?s)gltf-validator on .*stderr detail"):
            extvalidate.check_gltf(tmp_path / f"{name}.glb")
