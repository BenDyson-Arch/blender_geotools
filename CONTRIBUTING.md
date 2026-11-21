# Contributing to GeoTools

GeoTools is a Blender 5.2+ extension that bundles rusty-tiles and pyproj. What
it does for users is in the [user guide](docs/guide/README.md); how the code
works is in its module docstrings, tests and lint rules; planned work is in
the [issues](https://github.com/BenDyson-Arch/blender_geotools/issues). Agents
start at [AGENTS.md](AGENTS.md).

Markdown in this repository is for users only. The developer exceptions are
this file (setup, tests and releasing) and AGENTS.md;
`tests/core/test_docs_policy.py` enforces the allowlist and word caps. Put
anything else in a docstring, a test, a lint rule or an issue.

## Setup

You need Python 3.11 or later for the scripts, and the official Blender 5.2
(CPython 3.13) to build and to run the Blender tests.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install pytest numpy ruff pyproj==3.8.0 tzdata==2026.6 hypothesis==6.170.14  # pins: pyproject.toml, ci.yml
```

Without pyproj the reprojection tests are skipped, and without tzdata the test
of the bundled time zone database. Lint, format and import layering:

```sh
ruff check .
ruff format --check .
uvx --from import-linter==2.15 lint-imports
```

## Wheels

The extension bundles one exact version of each pin under
`[tool.geotools.wheels]` in `pyproject.toml` (rusty-tiles, pyproj, certifi, tzdata),
for each platform in `[tool.geotools] platforms`.
`scripts/fetch_wheels.py` fetches them into `geotools/wheels/` (never
committed) and checks names, versions and Python tags:

```sh
python scripts/fetch_wheels.py                          # every platform
python scripts/fetch_wheels.py --platform linux-x64
python scripts/fetch_wheels.py --from-dir rusty-tiles=/path/to/dist --platform linux-x64
```

`--from-dir NAME=DIR` takes one pin from a local folder, for testing an
unreleased rusty-tiles; `build_extension.py` refuses a wheel that does not
match its pin, so a release still needs the pinned one. A Blender built
against another Python (a distribution package on 3.14) needs
`--python 3.14` for both scripts; such a build is for local use only.

## Building

```sh
python scripts/build_extension.py --blender /path/to/blender
```

It stages `geotools/`, adds `platforms` and `wheels` to the staged manifest,
runs Blender's `extension validate` and `extension build --split-platforms`,
and writes `dist/geotools-<version>-<platform>.zip`. `--no-wheels` builds one
zip without the bundled libraries, and `--out DIR` writes elsewhere. The
repository manifest is never changed. The script's docstring explains how
Blender picks wheels.

To try a build without touching your own Blender profile:

```sh
export BLENDER_USER_RESOURCES="$(mktemp -d)"
blender --command extension install-file -r user_default -e dist/geotools-<version>-linux_x64.zip
```

## Tests

```sh
python -m pytest                                        # tests/core, no Blender
python scripts/run_blender_tests.py --expect missing    # built with --no-wheels
python scripts/run_blender_tests.py --expect available  # with the fetched wheels
```

The hypothesis property tests (`tests/core/test_properties.py`) run 100
fixed examples each; `GEOTOOLS_HYPOTHESIS_PROFILE=thorough` runs 1,000
random ones.

`run_blender_tests.py` builds the zip, installs it into a temporary profile,
runs `tests/blender/` and cleans up. Use `--blender PATH` (or `$BLENDER`), `-k
NAME` to run some tests, and `--keep` to keep the profile. The Preview tests
need a rusty-tiles CLI and a Cesium `Build/Cesium` folder, given in
`GEOTOOLS_TEST_RUSTY_TILES_CLI` and `GEOTOOLS_TEST_CESIUM`; without them they
are skipped.

For UI work, `python3 scripts/ui_screenshots.py --out DIR` opens Blender on
your display, writes screenshots of every sidebar panel and fails if text is
clipped at the default sidebar width, warning at a narrow one (its docstring
says how);
panel tests draw into a stub layout and cannot see that. CI runs it under
Xvfb on pull requests that touch `geotools/blender/` and uploads the
`ui-screenshots` artifact.

CI (`.github/workflows/ci.yml`) runs on pull requests and on pushes to
`main`, `develop` and `release/**`. Required checks: `core (pytest, ruff)`
and the Blender suites, with and without the wheels, on 5.2 LTS and on the
newest Blender in the `blender-range` jobs (5.3.0). The 5.2.0 range jobs, the
macOS and Windows jobs and the UI screenshot job are not required, but must
pass before a release, because every platform zip ships. When Blender ships a
release, add it to `blender-range` with its checksum and to the rulesets.

## Docs site

The site is built with MkDocs from `docs/`, `CHANGELOG.md` and `LICENSE`:

```sh
uvx --with-requirements requirements-docs.txt mkdocs serve
uvx --with-requirements requirements-docs.txt mkdocs build --strict
```

The strict build must pass with no warnings. Every page under `docs/` is in
the `nav` of `mkdocs.yml`. Write links relative to the file, as on GitHub;
`docs/hooks/site_links.py` rewrites links that leave `docs/`. Pushes to
`main` deploy the site.

## Branches and pull requests

- `develop` is where work lands: branch from it and open pull requests
  against it (`gh pr create --base develop`).
- `main` holds released code only and moves when a release is merged.
- `release/X.Y.Z` is cut from `develop` when a release's scope is complete.
  Work for the release still lands on `develop` first and is brought over by
  merging `develop` into the release branch (no cherry-picks); a fix only the
  release needs goes straight to the release branch, which is then merged
  back into `develop`.

`main`, `develop` and `release/*` accept changes only through pull requests
with review threads resolved and the required checks passing; on `main` the
branch must be up to date and a code owner must approve (admins can bypass).
Release tags `v*` cannot be deleted or moved.

## Releasing

`.github/workflows/release.yml` runs when a tag is pushed to `main`.

1. When the scope is complete, move the changelog's `## [Unreleased]` entries
   under `## [X.Y.Z] - Unreleased` on `develop`, cut `release/X.Y.Z`, then set
   `develop` to the next `-dev` version.
2. On a branch from `release/X.Y.Z`, set `version` in
   `geotools/blender_manifest.toml` (a suffix such as `-rc.1` makes a
   prerelease) and replace `Unreleased` in its changelog heading with the date.
   The release body is `.github/release-notes/X.Y.Z.md` when it exists,
   otherwise the changelog section; `python scripts/release_notes.py X.Y.Z`
   prints it and fails if it links to a file the branch lacks.
3. Merge that into `release/X.Y.Z`, then merge `release/X.Y.Z` into `main`
   with a pull request. Check that the `docs (deploy to GitHub Pages)` job
   passes.
4. Tag the merge commit on `main`:
   `git tag -a vX.Y.Z -m "GeoTools X.Y.Z" && git push origin vX.Y.Z`.
5. The workflow checks the tag against the manifest and the changelog,
   fetches the pinned wheels, builds and checks the three zips, tests the
   Linux zip in Blender, and publishes the release with the zips, the demo
   data and `SHA256SUMS`.
6. Merge `main` back into `develop` and delete the release branch.

If the workflow fails after creating the draft, fix the cause and re-run the
failed jobs. A published version needs a new number.

## Bumping a pin or Blender

1. Change the version under `[tool.geotools.wheels]`. For pyproj, check that
   the new version has a `cp313` wheel for every platform, and its macOS tags
   (Blender ignores them; see `PIP_PLATFORM_TAGS` in `fetch_wheels.py`).
2. Fetch the wheels again, build, and check the version in the About panel.
3. Add a changelog line.

To update the pinned Blender, take the three checksums from the release's
`.sha256` file into `ci.yml` and `release.yml`. If its Python version
changes, update `BLENDER_PYTHON` in `fetch_wheels.py` and `PYTHON_VERSION` in
the workflows.
