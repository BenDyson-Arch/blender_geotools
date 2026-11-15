# AGENTS.md

GeoTools is GIS for Blender: georeference a site, bring in geodata, analyse the
full 3D mesh, and publish OGC 3D Tiles through the bundled rusty-tiles wheel.
Learn the code from its module docstrings, tests and lint rules. Plans and
open work are GitHub issues and milestones, not files.

## Commands

```sh
ruff check . && ruff format --check .
uvx --from import-linter==2.15 lint-imports
python -m pytest                                        # tests/core, no Blender
python scripts/run_blender_tests.py --expect missing    # Blender suite, no wheels
python scripts/fetch_wheels.py --platform linux-x64
python scripts/run_blender_tests.py --expect available  # with the pinned wheels
uvx --with-requirements requirements-docs.txt mkdocs build --strict
```

Use `-k NAME` to run part of the Blender suite. Setup, building and releasing
are in CONTRIBUTING.md.

## The Blender lock

The Blender is pinned: the official Blender 5.2 LTS (CPython 3.13) whose
checksums are in `.github/workflows/ci.yml`. Run the Blender suite with that
build (`--blender PATH` or `$BLENDER`), never a distribution package on
another Python. Run one Blender process at a time per machine; when agents
share a machine, wrap Blender runs in
`flock ~/.cache/geotools-blender-tests.lock …`.

## Judgement rules (no check can enforce these)

- **Don't redo Blender.** GeoTools reads and writes geo formats, handles the
  georeference, analyses what needs the georeference, and publishes.
  Modelling, draping, displacement, painting and anything Apply Modifier or
  Geometry Nodes already does stay Blender's, documented as a recipe in the
  guide, not as GeoTools code.
- **Standards only.** Output is plain glTF 2.0 and 3D Tiles 1.1 metadata. No
  consumer-specific sidecars, extras or export variants. `build.json` and the
  staging folder are GeoTools' own record, never a published format.
- **No Python loops over mesh data.** Faces, vertices, loops and texels go
  through `foreach_get`/`foreach_set` and numpy, Geometry Nodes or a Cycles
  bake. Sources reach tens of millions of triangles. A loop over objects,
  tiles or features (where a format forces it) is fine.
- **Panels stay cheap.** `draw()` never evaluates a mesh, hashes a file,
  decodes an image or starts a subprocess. Cache the result on a property
  and refresh it from an operator.
- **One word per thing in the UI.** The rules are in the `blender.panels`
  docstring. Panel and button names in the guide match the sidebar exactly.
- **Synthetic data only.** Fixtures, examples and screenshots are generated.
  Never commit or attach real site meshes, textures, coordinates or community
  data.
- **No fallbacks for old dependencies.** Require the pinned rusty-tiles API;
  an older wheel is reported as not available, never served a degraded path.
- **Markdown is for users.** Change the guide page for any change a user can
  see. Put developer knowledge in a docstring, a test or a lint rule, and
  plans in an issue. Don't add design notes, roadmaps or status lines to
  markdown, and don't repeat a fact that already has an authority: link to
  it. `tests/core/test_docs_policy.py` checks the rest.
- **Turn corrections into checks.** When a review finds a rule broken, add a
  test or lint rule that would have caught it, not more prose.
- **Validated maths stays validated.** A change to an analysis result updates
  its validation page and the reference test in the same pull request.

## Branches

Work lands on `develop` through pull requests; `main` holds releases only.
Don't push to `main`, `develop` or `release/*`, and never move a tag.
