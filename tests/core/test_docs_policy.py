"""Markdown in this repository is for users, and says only what the code does.

The allowed files are the README, the changelog, the docs site's user pages and
the GitHub release notes, plus two capped developer files: CONTRIBUTING.md
(setup, tests, releasing) and AGENTS.md (commands and judgement rules).
Developer knowledge belongs in docstrings, tests and lint rules, and plans in
GitHub issues, so an agent never has to guess which copy of a fact is the
authority.

Checked here, without MkDocs (``mkdocs build --strict`` in the docs workflow
checks links and anchors):

- every ``.md`` file matches :data:`ALLOWED`, within its word cap;
- every page under ``docs/`` is in the ``mkdocs.yml`` nav, which has no
  developer section;
- the user pages hold no ``file.py:123`` line references, no milestone codes,
  no versions newer than what ships and no "planned"/"not yet" wording, and
  name only operators, panels, labels and issue codes the code has;
- nothing links to the removed developer docs, and no code cites a markdown
  file that is not an allowed one.
"""

from __future__ import annotations

import ast
import fnmatch
import re
import subprocess
import tomllib
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

ALLOWED = (
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".github/release-notes/*.md",
    "docs/index.md",
    "docs/changelog.md",
    "docs/license.md",
    "docs/guide/*.md",
    "docs/reference/*.md",
    "docs/recipes/*.md",
    "docs/validation/*.md",
    "docs/analysis-presets.md",
)

# Developer files: the only markdown not written for users.
DEVELOPER = ("CONTRIBUTING.md", "AGENTS.md", "CLAUDE.md")

WORD_CAPS = {"CONTRIBUTING.md": 1200, "AGENTS.md": 600}
CHANGELOG_SECTION_CAP = 800
RELEASE_NOTES_CAP = 1600
CLAUDE_MD_CAP = 25

# Blender's own UI and node names the guide refers to in bold, which GeoTools'
# code does not spell. Add a term here only if it is Blender's.
BLENDER_UI_TERMS = frozenset(
    s.lower()
    for s in (
        "Add-ons",
        "Allow Online Access",
        "Bleed",
        "Clone",
        "Ctrl+click",
        "Custom Properties",
        "Clone Map",
        "Edit",
        "Extrude Mesh",
        "Flip Faces",
        "Get Extensions",
        "Image",
        "Install from Disk",
        "Join Geometry",
        "Mask",
        "Merge by Distance",
        "Object Info",
        "Preferences",
        "Quick Edit",
        "Reload",
        "Sample Nearest Surface",
        "Set Position",
        "Smear",
        "Soften",
        "Stencil Mask",
        "Store Named Attribute",
        "Subdivide Mesh",
    )
)

# Labels GeoTools builds at draw time, which no string literal spells whole.
DYNAMIC_LABELS = (
    re.compile(r"^Undo .+$"),  # Undo <tool>
    re.compile(r"^Publish \S+\.3tz$"),  # Publish site.3tz
    re.compile(r"^Export \S+\.glb$"),  # Export site.glb
    re.compile(r"^.+ \((True North|Fast Bake)\)$"),  # Add Analysis menu entries
)


# --- the files ------------------------------------------------------------------------------


@cache
def markdown_files() -> tuple[str, ...]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        files = [line for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):  # not a git checkout
        files = [
            p.relative_to(REPO_ROOT).as_posix()
            for p in REPO_ROOT.rglob("*.md")
            if not any(part.startswith(".") and part != ".github" for part in p.parts)
        ]
    return tuple(sorted(f for f in files if (REPO_ROOT / f).is_file()))


def allowed(path: str) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in ALLOWED)


def user_pages() -> list[str]:
    return [f for f in markdown_files() if allowed(f) and f not in DEVELOPER]


def read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def words(text: str) -> int:
    return len(text.split())


_FENCE = re.compile(r"^\s*(```|~~~)")


def prose_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) outside fenced code blocks and HTML comments."""
    out, fenced, comment = [], False, False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        if comment or "<!--" in line:
            comment = "-->" not in line.split("<!--")[-1] if "<!--" in line else "-->" not in line
            continue
        out.append((number, line))
    return out


def test_the_policy_sees_the_files() -> None:
    files = markdown_files()
    assert "README.md" in files and "docs/guide/getting-started.md" in files


def test_every_markdown_file_is_allowed() -> None:
    stray = [f for f in markdown_files() if not allowed(f)]
    assert not stray, (
        "Markdown here is for users (README, CHANGELOG, the docs site's user pages, release "
        "notes), plus CONTRIBUTING.md and AGENTS.md. Put developer knowledge in a docstring, "
        "a test or a lint rule, and plans in a GitHub issue.\n" + "\n".join(stray)
    )


# --- word caps ------------------------------------------------------------------------------


@pytest.mark.parametrize(("path", "cap"), sorted(WORD_CAPS.items()))
def test_developer_files_stay_short(path: str, cap: int) -> None:
    count = words(read(path))
    assert count <= cap, (
        f"{path} has {count} words (cap {cap}). Move what a check can enforce into a test "
        "or lint rule, and the rest into docstrings."
    )


def test_claude_md_is_one_line_pointing_to_agents_md() -> None:
    lines = [line for line in read("CLAUDE.md").splitlines() if line.strip()]
    assert len(lines) == 1 and "AGENTS.md" in lines[0]
    assert words(lines[0]) <= CLAUDE_MD_CAP


def changelog_sections(text: str) -> dict[str, str]:
    sections, name = {}, None
    for line in text.splitlines():
        if line.startswith("## ["):
            name = line[3:]
            sections[name] = ""
        elif name is not None:
            sections[name] += line + "\n"
    return sections


def test_changelog_sections_are_concise() -> None:
    sections = changelog_sections(read("CHANGELOG.md"))
    assert "[Unreleased]" in sections
    long = {name: words(body) for name, body in sections.items()}
    long = {name: n for name, n in long.items() if n > CHANGELOG_SECTION_CAP}
    assert not long, (
        f"Changelog sections over {CHANGELOG_SECTION_CAP} words: {long}. Say what changed "
        "for users; the release notes and git hold the story."
    )


def test_release_notes_are_concise() -> None:
    pages = [f for f in markdown_files() if f.startswith(".github/release-notes/")]
    long = {f: words(read(f)) for f in pages if words(read(f)) > RELEASE_NOTES_CAP}
    assert not long, long


# --- the docs site --------------------------------------------------------------------------


def mkdocs_block(key: str) -> list[str]:
    """The lines of a top-level mkdocs.yml key's block, without the key's line."""
    lines = read("mkdocs.yml").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{key}:"))
    block = []
    for line in lines[start + 1 :]:
        if line and not line[0].isspace() and not line.startswith("#"):
            break
        block.append(line)
    return block


_NAV_PAGE = re.compile(r"^\s*-\s+(?:(?:\"[^\"]*\"|[^:\"]+):\s+)?([\w./-]+\.md)\s*$")


def nav_pages() -> list[str]:
    return [m.group(1) for line in mkdocs_block("nav") if (m := _NAV_PAGE.match(line))]


def site_pages() -> list[str]:
    return [
        f.removeprefix("docs/")
        for f in markdown_files()
        if f.startswith("docs/") and not f.startswith(("docs/hooks/", "docs/assets/"))
    ]


def test_every_docs_page_is_in_the_nav() -> None:
    nav = set(nav_pages())
    missing = sorted(set(site_pages()) - nav)
    assert not missing, f"Pages under docs/ missing from the mkdocs.yml nav: {missing}"
    gone = sorted(p for p in nav if not (REPO_ROOT / "docs" / p).is_file())
    assert not gone, f"nav entries with no page: {gone}"


def test_the_nav_is_for_users() -> None:
    nav = nav_pages()
    assert nav, "no pages found in the mkdocs.yml nav"
    assert not [p for p in nav if not allowed(f"docs/{p}")], nav
    text = "\n".join(mkdocs_block("nav"))
    assert not re.search(r"(?i)^\s*-\s+(developer|design|roadmap|backlog)\b", text, re.MULTILINE)
    leftover = [
        line
        for line in mkdocs_block("not_in_nav")
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not leftover, f"not_in_nav must stay empty: {leftover}"


# --- lint rule 1: no line references ---------------------------------------------------------

LINE_REFERENCE = re.compile(r"\b[\w./-]+\.(?:py|md|ya?ml|toml|rs|js|html|sh)(?::|#L)\d+")


def line_references(path: str) -> list[str]:
    return [
        f"{path}:{n}: {m.group(0)}"
        for n, line in enumerate(read(path).splitlines(), 1)
        for m in LINE_REFERENCE.finditer(line)
    ]


def test_no_line_references() -> None:
    found = [hit for f in markdown_files() if allowed(f) for hit in line_references(f)]
    assert not found, (
        "Line numbers drift with every change; name the function, class or test.\n"
        + "\n".join(found)
    )


# --- lint rule 2: no milestone codes ---------------------------------------------------------

MILESTONE = re.compile(r"(?<![\w-])M(?:10|[0-9])(?![\w])")


def milestone_codes(path: str) -> list[str]:
    return [
        f"{path}:{n}: {m.group(0)} in {line.strip()[:70]!r}"
        for n, line in prose_lines(read(path))
        for m in MILESTONE.finditer(line)
    ]


def test_no_milestone_codes() -> None:
    found = [hit for f in user_pages() for hit in milestone_codes(f)]
    assert not found, (
        "Users don't know the milestone codes: name the feature (Geo formats, Analysis), and "
        "keep plans in GitHub issues and milestones.\n" + "\n".join(found)
    )


# --- lint rule 3: no waiting for future versions ---------------------------------------------

# GeoTools versions have three parts ("GeoTools 16.50 h" is a reading, not a version).
_VERSIONED = re.compile(
    r"\b(?:(rusty-tiles|Blender|pyproj)\s+(\d+)\.(\d+)|(GeoTools)\s+(\d+)\.(\d+)\.\d+)"
)
WAITING = re.compile(
    r"(?i)\b(not (?:built )?yet|planned|next batch|later releases?|coming soon|"
    r"will be added|in the backlog|on the roadmap)\b"
)
# Release notes are frozen at their tag: they describe that release's world.
FROZEN = ".github/release-notes/"


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


@cache
def shipped_versions() -> dict[str, tuple[int, ...]]:
    pyproject = tomllib.loads(read("pyproject.toml"))
    wheels = pyproject["tool"]["geotools"]["wheels"]
    manifest = tomllib.loads(read("geotools/blender_manifest.toml"))
    return {
        "rusty-tiles": _version(wheels["rusty-tiles"]),
        "pyproj": _version(wheels["pyproj"]),
        "GeoTools": _version(manifest["version"]),
        "Blender": _version(manifest["blender_version_min"]),
    }


def waiting_words(path: str) -> list[str]:
    shipped = shipped_versions()
    found = []
    for n, line in prose_lines(read(path)):
        for m in _VERSIONED.finditer(line):
            name, major, minor = [g for g in m.groups() if g is not None]
            # Major and minor only: a patch release of what ships is what ships.
            if (int(major), int(minor)) > shipped[name][:2]:
                found.append(f"{path}:{n}: {m.group(0)} is newer than what ships")
        found.extend(f"{path}:{n}: {m.group(0)!r}" for m in WAITING.finditer(line))
    return found


def test_no_version_waiting_wording() -> None:
    found = [hit for f in user_pages() if not f.startswith(FROZEN) for hit in waiting_words(f)]
    assert not found, (
        "User pages describe what this version does. State a limit as it is today, and keep "
        'plans ("until rusty-tiles 0.5", "planned") in GitHub issues.\n' + "\n".join(found)
    )


# --- lint rule 4: names the docs use exist in the code ---------------------------------------


@cache
def code_inventory() -> tuple[frozenset[str], frozenset[str]]:
    """(bl_idnames, every string literal) of geotools/ and the preset generator."""
    idnames, strings = set(), set()
    sources = [*sorted((REPO_ROOT / "geotools").rglob("*.py"))]
    sources.append(REPO_ROOT / "scripts" / "make_analysis_presets.py")
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.add(node.value.strip())
            if (
                isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "bl_idname" for t in node.targets)
                and isinstance(node.value, ast.Constant)
            ):
                idnames.add(str(node.value.value))
    return frozenset(idnames), frozenset(strings)


_SUBPACKAGES = frozenset(p.stem for p in (REPO_ROOT / "geotools").iterdir())
_OPERATOR = re.compile(r"(?<![\w.])geotools\.([a-z_]+)\b(?![.\w])")
_CLASS_ID = re.compile(r"\bGEOTOOLS_(?:PT|OT|MT|UL)_\w+")


def unknown_ids(path: str) -> list[str]:
    idnames, _ = code_inventory()
    found = []
    for n, line in enumerate(read(path).splitlines(), 1):
        for m in _OPERATOR.finditer(line):
            name = m.group(0)
            if m.group(1) not in _SUBPACKAGES and name not in idnames:
                found.append(f"{path}:{n}: operator {name}")
        for m in _CLASS_ID.finditer(line):
            if m.group(0) not in idnames and m.group(0) not in _class_names():
                found.append(f"{path}:{n}: {m.group(0)}")
    return found


@cache
def _class_names() -> frozenset[str]:
    names = set()
    for path in (REPO_ROOT / "geotools").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names.update(n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef))
    return frozenset(names)


_BOLD = re.compile(r"\*\*([^*`]+?)\*\*")
# Pages that name UI. The README and the site's home page title their feature
# cards in bold ("Terrain and visibility"), and the recipes name Geometry Nodes
# and the example file's own objects; neither are GeoTools labels.
UI_PAGES = ("docs/guide/", "docs/reference/", "docs/analysis-presets.md")
_SMALL_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
    ]
)


def title_case(text: str) -> bool:
    """Labels are in Title Case; bold sentence-case phrases are emphasis."""
    words_ = re.findall(r"[A-Za-z][\w'-]*", text)
    return bool(words_) and all(w[0].isupper() or w in _SMALL_WORDS for w in words_)


def unknown_labels(path: str) -> list[str]:
    _, strings = code_inventory()
    known = {s.lower() for s in strings} | {
        s.removeprefix("GeoTools ").lower() for s in strings if s.startswith("GeoTools ")
    }
    found = []
    # Bold can wrap over a line break, so look at whole paragraphs.
    lines = prose_lines(read(path))
    paragraphs: list[tuple[int, str, int]] = []
    for n, line in lines:
        if paragraphs and line.strip() and paragraphs[-1][1] and n == paragraphs[-1][2] + 1:
            first, text, _ = paragraphs[-1]
            paragraphs[-1] = (first, f"{text} {line.strip()}", n)
        else:
            paragraphs.append((n, line.strip(), n))
    for n, line, _ in paragraphs:
        for m in _BOLD.finditer(line):
            text = m.group(1).strip()
            # Sentences and lead-ins ("**Limits.**", "**Fit.**") are emphasis, not labels.
            if text.endswith((".", ":", "?")) or not title_case(text) or len(text.split()) > 6:
                continue
            parts = re.split(r" > |: ", text)
            for part in (p.strip().rstrip("…").removesuffix("...") for p in parts):
                if (
                    part.lower() in known
                    or part.lower() in BLENDER_UI_TERMS
                    or any(rx.match(part) for rx in DYNAMIC_LABELS)
                ):
                    continue
                found.append(f"{path}:{n}: **{text}** ({part!r})")
    return found


def label_pages() -> list[str]:
    return [f for f in user_pages() if f.startswith(UI_PAGES)]


def test_operators_and_panels_named_in_the_docs_exist() -> None:
    found = [hit for f in markdown_files() if allowed(f) for hit in unknown_ids(f)]
    assert not found, "No such operator or class in geotools/:\n" + "\n".join(found)


def test_bold_ui_labels_in_the_docs_exist() -> None:
    found = [hit for f in label_pages() for hit in unknown_labels(f)]
    assert not found, (
        "A bold Title Case name in the guide is a UI label: it must be spelled as GeoTools "
        "draws it (bl_label, a property name, an enum item or a text= literal). If it is "
        "Blender's own UI, add it to BLENDER_UI_TERMS.\n" + "\n".join(found)
    )


_ISSUE_CODE_ROW = re.compile(r"^\|\s*`([a-z0-9]+(?:-[a-z0-9]+)+)`\s*\|")


def test_issue_codes_in_the_publish_guide_exist() -> None:
    _, strings = code_inventory()
    text = read("docs/guide/publish.md")
    codes = [m.group(1) for line in text.splitlines() if (m := _ISSUE_CODE_ROW.match(line))]
    codes += re.findall(r"`(image-[a-z0-9-]+)`", text)
    assert len(codes) > 10, "the issue-code table moved; update this test"
    unknown = sorted({c for c in codes if c not in strings})
    assert not unknown, f"Issue codes in guide/publish.md that geotools/ never emits: {unknown}"


# --- lint rule 5: no links into the removed developer docs ----------------------------------

_LINK_TARGET = re.compile(r"\]\(([^)\s]+)")
DEVELOPER_DOCS = re.compile(r"(^|/)(design/|ROADMAP\.md|BACKLOG\.md|contributing\.md)")


def developer_links(path: str) -> list[str]:
    return [
        f"{path}:{n}: {m.group(1)}"
        for n, line in enumerate(read(path).splitlines(), 1)
        for m in _LINK_TARGET.finditer(line)
        if DEVELOPER_DOCS.search(m.group(1).split("#")[0])
    ]


def test_no_links_to_developer_docs() -> None:
    found = [hit for f in markdown_files() if allowed(f) for hit in developer_links(f)]
    assert not found, (
        "Design notes, the roadmap and the backlog are gone: link the code, a user page or a "
        "GitHub issue instead.\n" + "\n".join(found)
    )


# --- lint rule 6: code cites only allowed markdown -------------------------------------------

_CITED = re.compile(r"\b(docs/[\w./-]+\.md|[A-Z]+\.md)\b")
# Their own fixtures name markdown files that do not exist, on purpose.
CITATION_EXEMPT = frozenset({"tests/core/test_release_notes.py", "tests/core/test_docs_policy.py"})
# Markdown in other repositories, cited by path.
OTHER_REPOSITORIES = frozenset({"docs/architecture/h28-tile-batching-contract.md"})  # rusty-tiles


def code_files() -> list[Path]:
    roots = ("geotools", "scripts", "tests", ".github/workflows", ".github/actions")
    files = [p for root in roots for p in (REPO_ROOT / root).rglob("*") if p.is_file()]
    files.append(REPO_ROOT / "pyproject.toml")
    files.append(REPO_ROOT / "mkdocs.yml")
    return [
        p
        for p in files
        if p.suffix in (".py", ".toml", ".yml", ".yaml")
        and p.relative_to(REPO_ROOT).as_posix() not in CITATION_EXEMPT
    ]


def bad_citations(path: Path) -> list[str]:
    rel = path.relative_to(REPO_ROOT).as_posix()
    found = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        for m in _CITED.finditer(line):
            cited = m.group(1)
            # A URL (such as the 0.3 namespace IRI) is an identifier, not a citation.
            in_url = re.search(r"https?://\S*$", line[: m.start()])
            if in_url or cited in OTHER_REPOSITORIES:
                continue
            if not ((REPO_ROOT / cited).is_file() and allowed(cited)):
                found.append(f"{rel}:{n}: {cited}")
    return found


def test_code_cites_only_allowed_markdown() -> None:
    found = [hit for path in code_files() for hit in bad_citations(path)]
    assert not found, (
        "Code may point at a user page or CONTRIBUTING.md/AGENTS.md, never at a markdown file "
        "that is gone or not allowed. Put the fact in the docstring instead.\n" + "\n".join(found)
    )
