"""MkDocs hook: make the docs' GitHub-relative links work on the site too.

The Markdown under docs/ is written to read well on GitHub, so some relative
links leave docs/ (``../../examples/drape-contexts/``,
``../../CONTRIBUTING.md#building``) or point at a folder (``../design/``).
On GitHub those work; MkDocs cannot resolve them. Before each page is
rendered, this hook rewrites such links, and only those:

- a root file the site includes as a page (CONTRIBUTING.md, CHANGELOG.md,
  LICENSE) becomes a link to that page, keeping the anchor;
- any other file or folder outside docs/, or a folder inside docs/ with no
  index page, becomes a link to it on GitHub (``SOURCE_BRANCH``).

Links that resolve to a page are left alone, so MkDocs still validates them
(and their anchors) under ``mkdocs build --strict``. A link this hook rewrites
to GitHub is checked here instead: if its target is not in the repository, the
hook logs a warning, which fails the strict build. The files on disk never
change. It also points the "Edit this page" button of the stub pages at the
root file they include.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re

# The site is deployed from main, so source links point at main.
SOURCE_BRANCH = "main"
# Edits go to develop, like edit_uri in mkdocs.yml.
EDIT_BRANCH = "develop"

# Repository root file -> the docs page that includes it.
ROOT_PAGES = {
    "CONTRIBUTING.md": "contributing.md",
    "CHANGELOG.md": "changelog.md",
    "LICENSE": "license.md",
}

_FENCE = re.compile(r"^\s*(```|~~~)")
# ](target) or ](target "title"); not images of remote URLs, not autolinks.
_LINK = re.compile(r"\]\((?P<target>[^)\s]+)(?P<title>\s+\"[^\"]*\")?\)")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")

# A warning here fails `mkdocs build --strict`, like MkDocs' own link warnings.
log = logging.getLogger("mkdocs.hooks.site_links")


def _rewrite(target: str, page_dir: str, files, repo_url: str, repo_root: str) -> str | None:
    """The link to use instead of ``target``, or None to leave it to MkDocs.

    Raises FileNotFoundError when ``target`` leaves docs/ (or is a folder) and
    is not in the repository.
    """
    if _SCHEME.match(target) or target.startswith(("#", "/")):
        return None
    path, sep, anchor = target.partition("#")
    if not path:
        return None
    is_dir = path.endswith("/")
    in_docs = posixpath.normpath(posixpath.join(page_dir, path))
    if in_docs.startswith("../") or in_docs == "..":
        repo_path = posixpath.normpath(posixpath.join("docs", page_dir, path))
        if repo_path.startswith("../"):
            return None  # outside the repository: leave it for MkDocs to report
        site_page = ROOT_PAGES.get(repo_path)
        if site_page is not None:
            rel = posixpath.relpath(site_page, page_dir or ".")
            return rel + sep + anchor
    else:
        if not is_dir:
            return None  # a file in docs/: MkDocs resolves and validates it
        for index in ("index.md", "README.md"):
            if files.get_file_from_path(posixpath.join(in_docs, index)):
                return None
        repo_path = posixpath.join("docs", in_docs)
    if not os.path.exists(os.path.join(repo_root, *repo_path.split("/"))):
        raise FileNotFoundError(repo_path)
    kind = "tree" if is_dir else "blob"
    url = f"{repo_url.rstrip('/')}/{kind}/{SOURCE_BRANCH}/{repo_path}"
    if is_dir:
        url += "/"
    return url + sep + anchor


def on_page_markdown(markdown, page, config, files):
    repo_url = config.get("repo_url")
    if not repo_url:
        return markdown
    src = page.file.src_uri
    page_dir = posixpath.dirname(src)
    # mkdocs.yml sits at the repository root.
    repo_root = os.path.dirname(os.path.abspath(config.config_file_path))

    for root_file, site_page in ROOT_PAGES.items():
        if src == site_page:
            page.edit_url = f"{repo_url.rstrip('/')}/edit/{EDIT_BRANCH}/{root_file}"

    out = []
    fence = None
    for line in markdown.split("\n"):
        m = _FENCE.match(line)
        if m:
            if fence is None:
                fence = m.group(1)
            elif m.group(1) == fence:
                fence = None
            out.append(line)
            continue
        if fence is None and "](" in line:
            # Skip inline code spans: rewrite only the parts outside backticks.
            parts = line.split("`")
            for i in range(0, len(parts), 2):

                def sub(match):
                    target = match.group("target")
                    try:
                        new = _rewrite(target, page_dir, files, repo_url, repo_root)
                    except FileNotFoundError as missing:
                        log.warning(
                            "%s: link '%s' points to %s, which does not exist",
                            src,
                            target,
                            missing,
                        )
                        return match.group(0)
                    if new is None:
                        return match.group(0)
                    return f"]({new}{match.group('title') or ''})"

                parts[i] = _LINK.sub(sub, parts[i])
            line = "`".join(parts)
        out.append(line)
    return "\n".join(out)
