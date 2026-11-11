# Contributing to GeoTools

GeoTools is a Blender 5.2+ extension that bundles rusty-tiles. Read
[docs/ROADMAP.md](docs/ROADMAP.md) first. It holds the plan and the decisions
behind it.

## Repository layout

| Path | What it holds |
|---|---|
| `geotools/` | The extension. Blender installs this directory. |
| `geotools/blender_manifest.toml` | The extension manifest. `platforms` and `wheels` are added at build time; do not add them here. |
| `geotools/core/` | Pure Python and numpy. Never imports `bpy`, so it can be tested with plain pytest. |
| `geotools/blender/` | The `bpy` glue: operators, panels and preferences. |
| `geotools/publish/` | The publish job (bpy-free): staging layout and lock, the worker thread that writes the GLB and runs rusty-tiles, the build record, and the preview helper. |
| `geotools/analysis/`, `geotools/bake/` | Empty, reserved for later milestones. |
| `geotools/wheels/` | Fetched rusty-tiles wheels. Ignored by git, never committed. |
| `scripts/` | Wheel fetching, extension build and the Blender test runner. |
| `tests/core/` | pytest tests for `geotools/core`. They run without Blender. |
| `tests/blender/` | Tests that run inside Blender. |

`geotools/__init__.py` imports `bpy` only inside `register()` and
`unregister()`. Keep it that way. `tests/core/test_no_bpy.py` fails if
`geotools` or `geotools.core` imports `bpy` or the Blender glue.

## Performance rule

No Python loops over faces, vertices, loops or other mesh data. Use
`foreach_get` and `foreach_set` with numpy arrays, Geometry Nodes, or Cycles
bakes. Sources can have tens of millions of triangles, and a per-element Python
loop is far too slow at that size.

## Development setup

You need Python 3.11 or later for the scripts. Blender 5.2 runs Python 3.13,
and ruff targets 3.13. You also need Blender 5.2 to build and to run the
Blender tests.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install pytest numpy ruff
```

Lint and format:

```sh
ruff check .
ruff format --check .
```

## Fetching the rusty-tiles wheels

The pin is `rusty-tiles` under `[tool.geotools]` in `pyproject.toml`, and the
target platforms are listed beside it. `scripts/fetch_wheels.py` puts one wheel
per platform in `geotools/wheels/`. It empties that directory first, and checks
each wheel's name, version and Python tags against the pin and Blender's Python.

Once rusty-tiles 0.4.0 is on PyPI, download the wheels for every platform:

```sh
python scripts/fetch_wheels.py
```

This uses `pip download`, so pip settings such as `PIP_INDEX_URL` apply.

Until then, copy wheels from a local directory, for example a rusty-tiles
`dist/` folder:

```sh
python scripts/fetch_wheels.py --from-dir /path/to/rusty-tiles/dist --platform linux-x64
```

`--platform` takes any of `linux-x64`, `windows-x64`, `macos-arm64` and
`macos-x64`. Repeat it or separate names with commas. Without it the script
fetches every platform in `pyproject.toml`.

A locally built Linux wheel is often tagged `linux_x86_64` rather than
`manylinux`. The script warns about this. Such a wheel is fine for local builds,
but do not ship it.

## Building

```sh
python scripts/build_extension.py --blender /path/to/blender
```

The script:

1. copies `geotools/` to `.build/staging/geotools`;
2. adds `platforms` and `wheels` to the staged manifest from the wheels in
   `geotools/wheels/`;
3. runs `blender --command extension validate`;
4. runs `blender --command extension build --split-platforms`;
5. validates each zip.

It writes one zip per platform to `dist/`, named
`geotools-<version>-<platform>.zip`, for example
`geotools-0.3.0-dev-linux_x64.zip`. If only some platforms have wheels, it
builds only those and says so. Use `--out DIR` to write elsewhere. Use
`--no-wheels` for a single zip without rusty-tiles. The repository manifest is
never changed.

`--blender` defaults to `blender` on `PATH`.

## Running the tests

Core tests, from the repository root:

```sh
python -m pytest
```

`pyproject.toml` points pytest at `tests/core`.

Blender tests run inside Blender against the installed extension. One command
builds the zip, installs it into a temporary profile, runs `tests/blender/` and
cleans up:

```sh
# Without rusty-tiles bundled (builds with --no-wheels)
python scripts/run_blender_tests.py --expect missing

# With the fetched wheels bundled; checks the pinned version loads
python scripts/run_blender_tests.py --expect available
```

Use `--blender PATH` (or `$BLENDER`) to pick a Blender, `-k NAME` to run some
tests, and `--keep` to keep the temporary profile for debugging.

The preview tests (core and Blender) need a rusty-tiles CLI and a Cesium 1.146
`Build/Cesium` folder, which are not bundled. Point
`GEOTOOLS_TEST_RUSTY_TILES_CLI` and `GEOTOOLS_TEST_CESIUM` at them to run those
tests; without them they are skipped. Blender's
Python has no pytest, so `tests/blender/run_suite.py` is a small runner of its
own.

CI (`.github/workflows/ci.yml`) runs on pull requests and on pushes to `main`
and `develop`. It has three jobs:

- `core`: ruff and pytest.
- `blender`: an official, checksum-pinned Blender 5.2 LTS runs the Blender
  tests with `--expect missing`, then uploads the zip.
- `blender-wheel`: the same with the PyPI wheel. It doesn't gate until
  rusty-tiles 0.4.0 is on PyPI; to make it gate, set the repository variable
  `RUSTY_TILES_WHEEL_GATING` to `true` and add it to the required checks of
  the `main` and `develop` rulesets.

When you update the pinned Blender in `ci.yml`, update `release.yml` to match.

## Isolating the Blender profile

Installing the extension by hand changes your Blender profile. To keep your own
profile clean, point `BLENDER_USER_RESOURCES` at a scratch directory. Blender
then keeps its config and extensions there.

```sh
export BLENDER_USER_RESOURCES="$(mktemp -d)"
blender --command extension install-file -r user_default -e dist/geotools-0.3.0-dev-linux_x64.zip
blender
```

`-e` enables the extension after installing it. Delete the directory when you
are done.

## Branches and pull requests

- `main` holds released code only. It moves when a release is merged.
- `develop` is where work lands. Branch from it and open pull requests against
  it (`gh pr create --base develop`).
- `legacy-0.2` keeps the 0.2 importer add-on. It is not developed.

Both `main` and `develop` are protected by rulesets: no deletion or force
push, changes only through a pull request with review threads resolved, merge
or squash only, and the `core (pytest, ruff)` and
`blender (official 5.2 LTS, no wheels)` checks must pass. On `main` the branch
must also be up to date with its base, and a code-owner review is required
(repository admins can bypass it). Release tags `v*` cannot be deleted or
moved.

## Releasing

Releases are cut from `main` by pushing a tag. `.github/workflows/release.yml`
does the rest.

1. On a branch from `develop`, set `version` in
   `geotools/blender_manifest.toml`, for example from `0.3.0-dev` to `0.3.0`.
   Use a suffix such as `0.3.0-rc.1` for a prerelease.
2. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [0.3.0] - YYYY-MM-DD`,
   add a new empty `## [Unreleased]` above it, and update the links at the
   bottom. The release notes are this section, so it must not be empty.
3. Merge that into `develop`, then open a pull request from `develop` to
   `main` and merge it.
4. Tag the merge commit on `main` and push the tag:
   `git tag -a v0.3.0 -m "GeoTools 0.3.0" && git push origin v0.3.0`.
5. The release workflow checks that the tag matches the manifest, is on
   `main` and has a changelog section. It then fetches the pinned wheels for
   every platform from PyPI, builds the four split zips, tests the Linux zip in
   official Blender, and publishes a GitHub release with the zips and
   `SHA256SUMS`. A plain version becomes the latest release; a suffixed one is
   a prerelease.
6. On `develop`, set the version to the next `-dev` version, for example
   `0.4.0-dev`.

If the workflow fails after creating the draft release, fix the cause and
re-run the failed jobs: it reuses the draft. A version that was published needs
a new version number, since release tags cannot be moved.

## Bumping the rusty-tiles pin

1. Change `rusty-tiles` under `[tool.geotools]` in `pyproject.toml`.
2. Run `python scripts/fetch_wheels.py` again. `build_extension.py` refuses to
   build with wheels that do not match the pin.
3. Build, install, and check that the GeoTools panel reports the new version.
4. Add a line to the changelog.

If a new Blender release changes its Python version, also update
`BLENDER_PYTHON` in `scripts/fetch_wheels.py`.
