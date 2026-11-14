# Backlog

Status: written 2026-11-13. A reference list for work after 0.3.0, not a plan.

## Where this list comes from

Before 0.3.0, `develop` at `ba9dd05` was reviewed from eight perspectives,
each proposing candidate work. Every candidate was then vetted through two
lenses: a value reviewer (is it worth doing, and when?) and a feasibility
reviewer (is the claim true, what does it cost, what could break?). Each
reviewer gave a score from 1 to 5.

The items that came out as **do now** were taken up for 0.3.0:
`sync-release-branch`, `manifest-permissions-online-access`,
`publish-never-overwrite-source-images`, `import-no-silent-reorigin`,
`texture-undo-clear-on-load`, `balance-apply-atomic`,
`docs-stale-contradictions`, `user-facing-release-notes` and
`web-viewing-extract-and-viewer-page`. This page was written before they
merged. If any of them did not ship in 0.3.0, move it into the worth-doing
tier here.

This page keeps the rest: the **worth doing** tier (both reviewers kept it,
but it was not needed for the release) and the **later** tier (the reviewers
split, or both put it after 0.3.0). [Deferred parts of 0.3.0
items](#deferred-parts-of-030-items) lists the pieces of do-now items that
were put off rather than rejected. The short list at the end records ideas
that were considered and rejected, so they are not proposed again without new
evidence.

**Check against the code before picking an item up.** File and line
references point at `develop` around `ba9dd05` and will drift. Some items may
have been done in passing, made moot, or changed by 0.3.0 work. Each item
keeps its review id (in backticks) as a stable label. The review notes
themselves are not kept in the repository, so anything an item needs from
them is written here.

Each item gives its tier, effort (S, M, L) and the two reviewers' scores,
then what and why, the reviewers' caveats, and the key evidence.

## Index

| Id | Item | Tier | Effort |
|---|---|---|---|
| [REL-1](#rel-1-an-update-channel-for-the-extension) | An update channel for the extension | Worth doing | S |
| [REL-2](#rel-2-rehearse-the-three-platform-release) | Rehearse the three-platform release | Worth doing | M-L |
| [REL-3](#rel-3-licence-and-third-party-notices-in-every-zip) | Licence and third-party notices in every zip | Later | S / M |
| [REL-4](#rel-4-harden-the-release-workflow) | Harden the release workflow | Later | S |
| [REL-5](#rel-5-wheel-hash-lock-and-reproducible-zips) | Wheel hash lock and reproducible zips | Later | M |
| [REL-6](#rel-6-detect-the-legacy-02-add-on) | Detect the legacy 0.2 add-on | Later | M |
| [ROB-1](#rob-1-refresh-refuses-unsafe-states) | Refresh refuses unsafe states | Worth doing | S |
| [ROB-2](#rob-2-plain-reports-for-sqlite-and-zip-errors) | Plain reports for sqlite and zip errors | Worth doing | S |
| [ROB-3](#rob-3-confirm-heavy-live-presets-on-large-meshes) | Confirm heavy live presets on large meshes | Worth doing | S |
| [ROB-4](#rob-4-memory-and-size-guard-for-dem-import) | Memory and size guard for DEM import | Worth doing | S-M |
| [ROB-5](#rob-5-bundle-tzdata-for-windows) | Bundle tzdata for Windows | Worth doing | S |
| [ROB-6](#rob-6-large-coordinate-objply-imports) | Large-coordinate OBJ/PLY imports | Worth doing | S, then M |
| [FMT-1](#fmt-1-a-stable-glb-metadata-namespace-uri) | A stable GLB metadata namespace URI | Worth doing | S |
| [FMT-2](#fmt-2-rfc-7946-wgs84-geojson-export) | RFC 7946 (WGS84) GeoJSON export | Worth doing | S (M) |
| [FMT-3](#fmt-3-lossless-attribute-round-trips) | Lossless attribute round-trips | Worth doing | M |
| [FMT-4](#fmt-4-svg-legends-for-variant-tilesets) | SVG legends for variant tilesets | Worth doing | S |
| [FMT-5](#fmt-5-geotiff-export) | GeoTIFF export | Later | M-L |
| [FMT-6](#fmt-6-height-system-and-geoid-separation) | Height system and geoid separation | Later | L |
| [FMT-7](#fmt-7-provenance-and-units-on-exported-vectors) | Provenance and units on exported vectors | Later | M |
| [FMT-8](#fmt-8-dataset-metadata-and-stac) | Dataset metadata and STAC | Later | M-L |
| [FMT-9](#fmt-9-surface-reading-presets) | Surface-reading presets | Later | L |
| [UX-1](#ux-1-make-preview-setup-findable) | Make Preview setup findable | Worth doing | S |
| [UX-2](#ux-2-support-entry-points) | Support entry points | Worth doing | S |
| [UX-3](#ux-3-say-why-the-last-build-failed) | Say why the last build failed | Worth doing | S |
| [UX-4](#ux-4-language-sweep-and-lint) | Language sweep and lint | Worth doing | S |
| [UX-5](#ux-5-issue-code-reference-page) | Issue-code reference page | Later | M |
| [PERF-1](#perf-1-measure-publishs-main-thread-work-at-10m-triangles) | Measure Publish's main-thread work at 10M triangles | Worth doing | M |
| [PERF-2](#perf-2-read-image-size-without-decoding) | Read image size without decoding | Worth doing | M |
| [PERF-3](#perf-3-lower-16k-texture-cleanup-peak-memory) | Lower 16K texture-cleanup peak memory | Later | M |
| [PERF-4](#perf-4-cache-the-macos-memory-read-in-draw) | Cache the macOS memory read in draw | Later | S |
| [PERF-5](#perf-5-dont-pin-full-rgba-buffers) | Don't pin full RGBA buffers | Later | S |
| [PERF-6](#perf-6-keep-texture-hashing-out-of-draw) | Keep texture hashing out of draw | Later | S |
| [PERF-7](#perf-7-refresh-layers-once-per-publish) | Refresh layers once per publish | Later | S |
| [PERF-8](#perf-8-redraw-only-the-sidebar-during-publish) | Redraw only the sidebar during publish | Later | S |
| [TEST-1](#test-1-ci-against-a-source-built-rusty-tiles-wheel) | CI against a source-built rusty-tiles wheel | Worth doing | M |
| [TEST-2](#test-2-external-validators-in-ci) | External validators in CI | Worth doing | S |
| [TEST-3](#test-3-getting-started-acceptance-test) | Getting-started acceptance test | Worth doing | M |
| [TEST-4](#test-4-cover-the-blender-version-range) | Cover the Blender version range | Worth doing | S |
| [TEST-5](#test-5-a-real-skip-outcome-in-the-blender-runner) | A real SKIP outcome in the Blender runner | Later | S |
| [TEST-6](#test-6-panel-draw-budget-test) | Panel-draw budget test | Later | M-L |
| [TEST-7](#test-7-upgrade-removal-and-unregister-hygiene) | Upgrade, removal and unregister hygiene | Later | M |
| [TEST-8](#test-8-paths-with-spaces-and-non-ascii-characters) | Paths with spaces and non-ASCII characters | Later | S |
| [TEST-9](#test-9-real-blendergis-acceptance-test-in-ci) | Real-BlenderGIS acceptance test in CI | Later | S |

Two worth-doing items are cheapest before a tag:
[FMT-1](#fmt-1-a-stable-glb-metadata-namespace-uri) (the namespace URI is
written into every metadata GLB) and
[REL-1](#rel-1-an-update-channel-for-the-extension) (users who install from
disk never see updates).

## Release engineering

### REL-1 An update channel for the extension

`extension-update-index` · Worth doing · Effort S · Scores 3, 4

**What and why.** Publish a Blender remote-repository `index.json` with each
release. In `release.yml`, run `blender --command extension server-generate
--repo-dir dist`, rewrite each relative `archive_url` to the tagged
`releases/download/vX.Y.Z/` asset, and upload `index.json`. Then document
adding `.../releases/latest/download/index.json` under Get Extensions >
Repositories. A zip installed from disk never shows that an update exists and
cannot later be switched to a repository install, so early adopters stay on
whatever they first installed.

**Caveats.** Allow Online Access must be on. A copy installed from disk must be
uninstalled and reinstalled from the repository. Prereleases never appear as
"latest". Check once that Blender follows GitHub's 302 redirect. `release.yml`
runs only on a tag push, so exercise the step in `ci.yml` or locally first.
Listing on extensions.blender.org is a separate, optional decision for the
maintainer, and needs the wheels to come from PyPI.

**Evidence.** `.github/workflows/release.yml:144` (uploads only zips and
`SHA256SUMS`); `README.md:53-61` and `docs/guide/getting-started.md:28-34`
(manual install, no update path); `geotools/blender_manifest.toml:3`.

### REL-2 Rehearse the three-platform release

`release-rehearsal-all-platforms` · Worth doing · Effort M-L · Scores 4, 3

**What and why.** The macOS and Windows zips, with their pyproj libraries and
native rusty-tiles module, have never been loaded in Blender, and
`release.yml` has never run. Tags can't be moved, so a broken zip costs a
version number. The rescoped plan: (1) get the multi-OS `setup-blender` action
onto the release branch; (2) add a rehearse workflow triggered by a push to
`rehearse/**` that pulls `wheel-<target>` artifacts from a rusty-tiles
wheel-acceptance run; (3) build all three zips and `SHA256SUMS` as artifacts,
never as a release; (4) install-test on Ubuntu, macOS and Windows with
`--expect available` and make the publish job depend on that matrix;
(5) add tag-only readiness checks (no "Unreleased" heading, pin at or above
`deps.MINIMUM_RUSTY_TILES`); (6) fix `release.yml:52`.

**Caveats.** `workflow_dispatch` can't work because `main` has no workflow
files. The existing wheel-acceptance run (42992725222) lacks the 0.4.1 API, so
it can't be the wheel source. Needs three maintainer actions: start the
upstream run, create a secret with `actions:read`, and approve a temporary
pin.

**Evidence.** `.github/workflows/release.yml:84-102` (only `linux_x64`
installed and tested), `:8-10` (tag trigger only); `.github/workflows/ci.yml:113-156`
(macOS and Windows run without wheels); `scripts/run_blender_tests.py:66-73`;
`release.yml:52` against `CONTRIBUTING.md:307-340`.

### REL-3 Licence and third-party notices in every zip

`licence-and-third-party-notices` · Later · Effort S (salvage) / M (full) · Scores 2, 4

**What and why.** The zips carry no `LICENSE` or third-party notices.
`build_extension.py` stages only `geotools/`. The cheap part: copy `LICENSE`
into the staged extension and add one `release.yml` check, plus optionally a
manifest `copyright` field. The full version generates `THIRD_PARTY_NOTICES.md`
from the bundled wheels' metadata and keeps a table of the native libraries
pyproj vendors on each platform.

**Caveats.** The author's own code doesn't need the file, Blender installs the
dist-info licence texts, and the gap in vendored libraries matches the upstream
pyproj wheel. The copyright holder string is the maintainer's call. Check the
Windows DLL set before writing the table. Worth doing eventually for a public
redistribution.

**Evidence.** `scripts/build_extension.py:64-72`;
`geotools/blender_manifest.toml:1-21` (no copyright); certifi is MPL-2.0;
the rusty-tiles wheel already ships `THIRD_PARTY_LICENSES-python.md`.

### REL-4 Harden the release workflow

`release-workflow-hardening` · Later · Effort S · Scores 2, 4

**What and why.** Add build-provenance attestations for `dist/*.zip` (with
job-scoped `id-token` and `attestations` permissions), pin actions to commit
SHAs as rusty-tiles does, and put the publish step behind an approval gate.
Today the publish job creates the draft and immediately makes it public and
latest.

**Caveats.** All the actions are GitHub-owned, and the tag push is already the
maintainer's explicit approval. SHA pins rot without a `dependabot.yml`.
Publishing the draft by hand is a zero-settings alternative to a protected
environment. The changes run only on a tag, so mistakes would show up only at
release. If any of it is taken up, take the attestation step.

**Evidence.** `.github/workflows/release.yml:34-123` (actions pinned by tag),
`:109-149` (draft then `--draft=false --latest`).

### REL-5 Wheel hash lock and reproducible zips

`wheel-hash-lock-reproducible-zips` · Later · Effort M · Scores 2, 3

**What and why.** (1) Record a SHA-256 per bundled wheel and verify it in
`fetch_wheels.check_wheel` and `build_extension.bundled_wheels`; today wheels
are checked by name, version and tags only. (2) Make zips byte-reproducible by
fixing mtimes and zip timestamps around Blender's build. The hash lock is the
cheaper and more useful half.

**Caveats.** PyPI filenames can't be replaced, and the hashes can only be
recorded from the fetch they are meant to guard. Reproducible zips need sorted
entries, normalised attributes and fixed timestamps after Blender writes its
per-platform manifest, and would only match on the same runner image. Nobody
promises reproducible builds.

**Evidence.** `scripts/fetch_wheels.py:380-404` (`pip download` without
hashes), `:251-273`; `scripts/build_extension.py:64-72`.

### REL-6 Detect the legacy 0.2 add-on

`legacy-0.2-detection-and-upgrade` · Later · Effort M (S for docs only) · Scores 2, 4

**What and why.** The 0.2 legacy add-on registers the same operator and panel
ids as the extension (`geotools.import_geojson`, `geotools.import_gpkg`,
`GEOTOOLS_PT_layers`, `GEOTOOLS_PT_analysis`), and its unregister deletes
`Scene.geotools_analysis`. Detect an enabled 0.2 add-on and tell the user to
remove it, and test coexistence. The docs-only part (remove 0.2 first; the
pip-installed pyproj and GDAL are no longer needed) was scheduled for the
0.3.0 release notes.

**Caveats.** The install base is close to zero (the v0.2.0 release has no
assets). Wait until real 0.2 users turn up. If it is built: no popup at
register (there is no context at startup), scan only when someone looks (About,
Check Dependencies, Run Checks). A test asserting that `Scene.geotools_analysis`
survives would fail by design.

**Evidence.** `v0.2.0:addons/data/geojson/operators.py:402` and
`gpkg/operators.py:526` against `geotools/blender/vectors.py:916, :928`;
`v0.2.0:addons/analysis/shared/properties.py:31, :35`.

## Robustness

### ROB-1 Refresh refuses unsafe states

`refresh-unsafe-state-guards` · Worth doing · Effort S · Scores 3, 4

**What and why.** Refresh and Auto-Refresh reload linked data
unconditionally. GeoTools' own Texture Cleanup can leave a linked ortho dirty
in memory, and a Metashape re-export then wipes that work with no user action.
Freeing a mesh in use by sculpt or render is a plausible crash. Raise
`RefreshError` in `ortho_image` when `texture_state.has_unsaved_edits(image)`
is true; require Object mode in `refresh_link`. In `auto_refresh_tick`, treat a
dirty image, a non-Object mode or a running render/bake job as a skip, not a
failure, so a retry still happens after the user saves.

**Caveats.** Auto-Refresh is opt-in, which lowers how often this happens, and
nobody has reproduced the crash. Don't add a `publish.is_running()` guard:
staging works from files, and the import would point up the module graph.
Test a dirty image and Vertex or Weight Paint mode (Sculpt is awkward to enter
in background mode).

**Evidence.** `geotools/blender/raster_io.py:191-200, :355-358`;
`geotools/blender/links.py:239-240, :263-279, :309, :491-494`;
`tests/blender/test_vectors.py:494` (only Edit Mode tested).

### ROB-2 Plain reports for sqlite and zip errors

`gpkg-zip-error-reports` · Worth doing · Effort S · Scores 3, 4

**What and why.** GeoPackage write-back and export show a raw traceback popup
when the file is locked, read-only or not a database, because
`sqlite3.Error` escapes `gpkg._transaction`. A non-zip `.3tz` does the same in
Preview. Map `sqlite3.Error` to `GpkgError` with plain text (wrapping connect,
`BEGIN IMMEDIATE` and the yield, re-raising `GpkgError` unchanged and guarding
`ROLLBACK`), and map `BadZipFile` to `PreviewError` in `extract_archive`.
About 20 lines.

**Caveats.** No data is lost (rollback works) and QGIS in WAL mode rarely
locks, so this is polish. The read paths already wrap sqlite errors. Leave the
connect timeout alone; the default is already 5 s. Test with a second
connection holding `BEGIN IMMEDIATE`, and a non-zip `.3tz`.

**Evidence.** `geotools/core/vector/gpkg.py:96-108`, `:48`;
`geotools/blender/vectors.py:1095, :1198`; `geotools/publish/preview.py:70`;
`geotools/blender/publish.py:826`.

### ROB-3 Confirm heavy live presets on large meshes

`heavy-preset-confirm` · Worth doing · Effort S · Scores 3, 4

**What and why.** One menu click can add sky-view factor, openness or
cumulative viewshed as a live modifier and freeze Blender for minutes, with no
way to cancel, and the freeze returns on every file open. Add `invoke()` to
`GEOTOOLS_OT_analysis_add` for presets with a Fast Bake path; above about 2M
triangles (about 30 s live), confirm with the triangle count (times observers
for the viewshed) and recommend Fast Bake. Put the threshold in a pure helper
with tests.

**Caveats.** A clock icon and a tooltip already warn, which keeps this from
scoring higher. Use `INVOKE_DEFAULT` only on those menu entries. Optional: a
Run Checks INFO for a heavy live modifier already on a large mesh, which covers
the freeze on reopening.

**Evidence.** `geotools/blender/analysis_presets.py:937` (`EXEC_DEFAULT`),
`:879-894`; `geotools/analysis/presets.py:149, :575, :592`;
`docs/analysis-presets.md` timings.

### ROB-4 Memory and size guard for DEM import

`dem-import-memory-guard` · Worth doing · Effort S-M · Scores 3, 4

**What and why.** A 20k by 20k LiDAR DEM at the default step of 1 needs well
over 64 GB, enough to get Blender killed and lose unsaved work. Add a pure
estimator, `dem_import_bytes(info, step, route)`, show the estimate in
`draw()`, and refuse in `execute()` and `refresh()` above about 80% of
`available_memory_bytes()` (or a fixed vertex/loop ceiling when that returns
`None`), naming the smallest step that fits. Also fix a real bug: the existing
guard limits samples to 2**31, but loops are four times the samples in
Blender's int32 counts, so it overflows from about 536M samples.

**Caveats.** Set the threshold conservatively so legitimate imports aren't
refused. Don't add a strided `read_band`: strip and tile decoding can't skip
columns, and it would destabilise a validated decoder. An automatic default
step, if wanted, belongs in `ImportHelper.check()` when the file changes.

**Evidence.** `geotools/blender/raster_io.py:83-110, :346-354, :414-420`;
`geotools/core/raster.py:163-175`; `geotools/core/sysmem.py`.

### ROB-5 Bundle tzdata for Windows

`bundle-tzdata` · Worth doing · Effort S · Scores 3, 4

**What and why.** A `.blend` saved with a named time zone such as
`Australia/Sydney` on Linux fails on Windows, where Blender ships no tzdata.
That is the same kind of portability gap the portable-paths work closed. Pin
the pure `tzdata` wheel in `[tool.geotools.wheels]` (the build already handles
`any`/`none` wheels), check it in `release.yml` and `deps.probe_all`, and
replace the "use a UTC offset" note with a "tzdata missing" install-integrity
message.

**Caveats.** It reverses a recorded design decision and adds a pin that needs
bumping several times a year, so the maintainer must approve it. Don't claim
"same results on every OS": `zoneinfo` still uses the system database on Linux
and macOS. Record the tz version only when tzdata is the source that loaded.
Keep the fixed-offset mode. Update `docs/design/m5-analysis.md:101-113,
:1170-1172` and `docs/guide/analysis.md:62-63`.

**Evidence.** `geotools/analysis/sun.py:33-37, :225-262`;
`pyproject.toml:11-20` (certifi is the precedent).

### ROB-6 Large-coordinate OBJ/PLY imports

`import-float64-shift` · Worth doing · Effort S (step 1), M (step 2) · Scores 3, 3

**What and why.** Blender's OBJ and PLY importers round to float32, which at a
northing of 5.4M gives 0.5 m steps. The current advice ("move the geometry
towards the origin") comes too late, because the precision is already lost.
Step 1 (S, the most important part): correct the messages in
`core/preflight.py` and the Limits section of `projects.md` to "re-export with
a local shift (Metashape's Shift field)", and add a cheap import-time warning
from the PLY header or the first few thousand OBJ `v` lines. Step 2 (M,
optional): shift in float64 before Blender sees the file.

**Caveats.** If the project already has an origin, the shift must be exactly
that E/N; a "round" shift would misplace data. Only an empty project gets a
rounded centre. Map through `AXIS_KEYWORDS` for Y-up files, keep the OBJ stem,
rewrite `mtllib` to an absolute path and keep `v`-line colours. A float32 PLY
gets a warning only. Route the shift through the re-origin guard (the do-now
item `import-no-silent-reorigin`, scheduled for 0.3.0). Parts of that item
could be deferred; see [Deferred parts of 0.3.0
items](#deferred-parts-of-030-items).

**Evidence.** `geotools/blender/source.py:118-130`;
`geotools/core/preflight.py:168-200`; `docs/guide/projects.md:154-158`;
`docs/guide/geo-formats.md:20-23` (vector and raster imports already subtract
in double precision).

## Interop and formats

### FMT-1 A stable GLB metadata namespace URI

`stable-xmp-namespace` · Worth doing · Effort S · Scores 3, 4

**What and why.** `GEOTOOLS_NAMESPACE` points at
`.../blob/main/docs/design/m4-glb-metadata.md#`, which ties a permanent
identifier to a branch, a milestone-named design note and its filename. It is
written into the `KHR_xmp_json_ld` context of every metadata GLB, so once files
are published, changing it breaks JSON-LD consumers that match on the full IRI.
Choose a URI meant to last (for example a dedicated
`docs/schema/geotools-xmp.md`, or a frozen-path note), update the constant,
section 5 and the example at `:222`, repoint the other references, and add a
test that the file the URI names exists.

**Caveats.** The choice of URI is the maintainer's; a w3id or Pages URL needs
public setup and approval. A namespace IRI doesn't have to resolve to work,
and nobody consumes it yet. If fixed for a release, it must go through the
release branch, then back to `develop`.

**Evidence.** `geotools/core/gltf_metadata.py:7, :90-92, :308`;
`docs/design/m4-glb-metadata.md:4, :222`; `geotools/blender/glb_export.py:5`;
`geotools/core/metadata_schema.py:6`; `docs/guide/glb-metadata.md:77`.

### FMT-2 RFC 7946 (WGS84) GeoJSON export

`geojson-rfc7946-export` · Worth doing · Effort S (M with the EPSG:4979 path) · Scores 3, 4

**What and why.** GeoJSON export writes the project CRS, which CesiumJS's
`GeoJsonDataSource`, MapLibre, deck.gl and geojson.io misplace or reject, so a
GeoTools GeoJSON can't sit next to its own tileset in a web viewer. Add
"Coordinates: Project CRS | WGS84 (RFC 7946)" to the export operator. Project
to EPSG:4326 with `reproject_points` offline, write no `crs` member, fix ring
winding, round to about 1e-8 degrees, fill NaN Z, and refuse for LOCAL and
ANCHOR projects.

**Caveats.** EPSG:4326 is a datum ensemble, so the report should include the
operation and accuracy. RFC 7946 wants ellipsoidal heights: either target
EPSG:4979 for 3D and compound CRSs, or keep project heights and say plainly
that the file is not conformant on Z. Projected output stays the default.

**Evidence.** `docs/guide/geo-formats.md:106-108`;
`docs/design/m8-geo-formats.md:250-251`; `geotools/blender/vectors.py:1129-1152`;
`geotools/core/vector/geojson.py:216-225, :270-308`.

### FMT-3 Lossless attribute round-trips

`attribute-roundtrip-fidelity` · Worth doing · Effort M · Scores 3, 4

**What and why.** Integer attributes outside int32 come back rounded (stored as
float32), which silently breaks QGIS joins, and survey E/N columns drop to
about 1 m precision. Import such integer columns as STRING and convert back on
export and GeoJSON write-back; warn per column at import when float32 changes a
REAL value by more than 1e-7 relative; keep declared types (DATE, DATETIME,
TEXT(n)) in a new options key and restore them on GPKG export. Also fix a
related bug: fids are written as int32, so a fid above 2^31 wraps and breaks
write-back matching (at least refuse it).

**Caveats.** Write-back only pushes columns the user ticks, so the risk to the
user's own file is lower than first claimed. GPKG write-back is already exact
through INTEGER affinity. Only values outside int32 are affected, not values
above 2^24. No "import as text" UI. Update the M8 design doc. Synthetic tests:
a 10-digit id, a 6-decimal easting, a DATE column.

**Evidence.** `geotools/core/vector/gpkg.py:271-282, :473`;
`geotools/core/vector/model.py:419-435`; `geotools/blender/vectors.py:290, :445`;
`docs/design/m8-geo-formats.md:339-345`.

### FMT-4 SVG legends for variant tilesets

`variant-legends` · Worth doing · Effort S (trimmed) · Scores 3, 3

**What and why.** A published variant such as `site.slope.3tz` is colour only,
and getting started sends new users to exactly that variant; the ramp stops
exist only in the private `build.json`. Add a bpy-free `core/legend.py` that
writes an SVG legend (label, units, range, scale, ramp stops or categories,
no-data swatch) beside the variant, record it in `build.json`, add a Last Build
button to open it, list the stops as text in Layers > Display, and add a Legend
paragraph to `layers.md`.

**Caveats.** No GDAL color-relief or QGIS colour-map exports (there is no
GeoTIFF export to match, and both need densified stops). No panel colour bar
(it would need `bpy.utils.previews`). Until this lands, `layers.md` should not
say Label and Units are "used in legends".

**Evidence.** `geotools/core/ramps.py:275-284`;
`geotools/core/presentation.py:55, :186-193, :226`;
`geotools/blender/presentation.py:798-853`; `docs/guide/layers.md:14-16, :31`.

### FMT-5 GeoTIFF export

`geotiff-export` · Later · Effort M-L · Scores 2, 3

**What and why.** Export analysis layers and surface height as a georeferenced
GeoTIFF: cast one vertical ray per cell, keep the top hit, and write height
plus FACE/CORNER (and TEXTURE) bands with nodata, on the site extent or a
linked raster's grid. The minimal writer already exists as a test helper and
would move into `core/geotiff.py`. Start with a design doc.

**Caveats.** The detailed M8 spec planned only import; the "in and out"
roadmap line overclaims. A top-hit north-up raster loses the vertical and
overhang results that matter at rock-shelter sites. The 0.3.0 docs should say
"GeoTIFF import". Add a one-off GDAL check of the output.

**Evidence.** `docs/ROADMAP.md:121`; `docs/guide/geo-formats.md:12`;
`geotools/core/geotiff.py` (readers only); `tests/blender/_geotiff.py:142-241`;
`geotools/blender/surface.py:238-300`.

### FMT-6 Height system and geoid separation

`height-system-geoid` · Later · Effort L · Scores 3, 3

**What and why.** Users are told to set the height offset to the geoid
separation at the site, but GeoTools gives no way to find it and records no
vertical datum. (a) Add an offline, GeoTools-only Height System property
(ellipsoidal or a vertical CRS), recorded in `build.json` and the XMP packet,
with "RL (AHD)"-style labels. (b) Add "Compute from geoid", which refuses
ballpark or missing-grid results. A cheap part can come first: a `projects.md`
note on how correct heights look in CesiumJS, and links to official geoid
calculators beside Height Offset.

**Caveats.** Both reviewers put it after 0.3.0. Computing needs the
online-access gate, because pyproj ships no geoid grids. It stores nothing new
in the GeoScene keys (SRID stays horizontal). It touches files the geodesy and
contours validation work used. Additive, so it can land in a 0.3.x.

**Evidence.** `docs/guide/projects.md:94-99`;
`geotools/blender/properties.py:71-74`; `docs/design/georeference-interop.md:42, :99`;
`geotools/core/gltf_metadata.py:304-355`; `geotools/core/reproject.py:116-165`.

### FMT-7 Provenance and units on exported vectors

`vector-export-provenance` · Later · Effort M · Scores 2, 3

**What and why.** Contours, sections and sightlines exported to GeoPackage or
GeoJSON say nothing about the tool, parameters, units or height datum. The
subset tools actually show: fill `gpkg_contents.description` and the
`gpkg_data_columns` titles and descriptions with units and the RL versus local
z datum.

**Caveats.** QGIS and ogrinfo don't show a free-form `gpkg_metadata` JSON row
or GeoJSON foreign members, so the full proposal adds little. Overwrites need
table-scoped cleanup of the metadata tables. Do it after the unit labels are
settled. Shares tables with [FMT-8](#fmt-8-dataset-metadata-and-stac).

**Evidence.** `geotools/blender/analysis_presets.py:36, :539` (preset
provenance exists); `geotools/core/vector/gpkg.py:376-394, :485-487`;
`geotools/blender/surface.py:177-186`.

### FMT-8 Dataset metadata and STAC

`dataset-metadata-stac` · Later · Effort M-L · Scores 2, 3

**What and why.** Published output carries no title, creator, licence or
attribution. A small slice: an optional credit/licence string written to the
GLB's `asset.copyright` and `dc:rights`, and recorded in `build.json`. The full
proposal adds GeoTools-only project metadata (not GeoScene keys) and a STAC
Item beside each publish.

**Caveats.** The `.3tz` itself would still carry no attribution, because
rusty-tiles writes the tiles and does not propagate `asset.copyright`; that
needs upstream work, and any upstream proposal is a draft for the maintainer's
approval only. STAC is a new published format with lasting compatibility
obligations, the bbox doesn't come from the placement probe yet, and the
`.3tz` isn't hashed.

**Evidence.** `docs/guide/publish.md:128-129`;
`geotools/core/gltf_metadata.py:304-355`; `geotools/core/gltf_writer.py:429`;
rusty-tiles `src/gltf_source/source.rs:1682-1702`.

### FMT-9 Surface-reading presets

`surface-reading-presets` · Later · Effort L · Scores 2, 3

**What and why.** The roadmap lists "Surface reading" (short-range ambient
occlusion and fine curvature for rock art) under M5, but the code has only a
PLANNED tuple. Real presets need normal-relative operators, not the existing
2.5D window and horizon helpers, and need the same validation as the terrain
presets.

**Caveats.** Both reviewers agreed not to build it for 0.3.0. It overlaps
Blender's own AO bake. It means regenerating the binary
`analysis_presets.blend`. The 0.3.0 docs fixes mark it in the roadmap as not
built yet.

**Evidence.** `docs/ROADMAP.md:317-323, :342-343, :359`;
`docs/analysis-presets.md:821-825`; `geotools/analysis/presets.py:59-65, :978-981`.

## UX and docs

### UX-1 Make Preview setup findable

`preview-setup-findable` · Worth doing · Effort S · Scores 3, 4

**What and why.** Preview tells users to "install them separately" without
saying where from. Add a "Set up Preview" subsection to `publish.md` (per-OS
CLI install, `npm install cesium@1.146.0`, the exact `Build/Cesium` folder),
two `wm.url_open` buttons in the Preview preferences, and one `resolve_cli()`
helper that tries `shutil.which('rusty-tiles')` and `~/.local/bin/rusty-tiles[.exe]`,
used at all three call sites. Blender started from a desktop launcher doesn't
inherit the shell PATH.

**Caveats.** No CLI version check: it would put a subprocess in poll and draw,
and would warn every user while releases/latest is the 0.4.0 CLI. Take the
Cesium version from rusty-tiles `origin/main` (the old local checkout says
1.143). Preview is optional, so nobody is blocked.

**Evidence.** `geotools/blender/preferences.py:28-36`;
`geotools/publish/preview.py:36-54`; `geotools/blender/publish.py:956-973`;
`docs/guide/publish.md:137-142`; `docs/guide/getting-started.md:225-227`.

### UX-2 Support entry points

`help-and-support-entry-points` · Worth doing · Effort S · Scores 3, 4

**What and why.** The repo is public and the audience is archaeologists, so
without guidance the first bug reports are likely to attach sensitive site
data, against the synthetic-only rule. Add `.github/ISSUE_TEMPLATE/bug.yml`
that asks for versions and says never to attach site meshes, textures,
coordinates or community data (reproduce with the synthetic demo, or attach
`build.json`); a "Getting help" section in `docs/guide/README.md`; and Guide
and Report an Issue buttons in About. A clean version links to its tag, a
`-dev` build to a branch, with a unit test.

**Caveats.** The template only takes effect once it is on `main`. Copy Support
Info is optional; if added, replace the home directory with `~` (the PROJ data
dir contains the user name). Drop the right-click manual map, or keep a few
entries with a test that the anchors exist.

**Evidence.** `geotools/blender/panels.py:794-813`;
`geotools/blender/preferences.py:28-35`; `.github/` has no `ISSUE_TEMPLATE`;
no `url_open` in `geotools/`.

### UX-3 Say why the last build failed

`last-build-failure-details` · Worth doing · Effort S · Scores 2, 4

**What and why.** When validation raises, the Last Build box shows only
"FAILED", and the tiling warning count is recorded but never drawn. Add
`validation_error`, `warning_count`, `warnings_first` and `cleanup_diagnostics`
to `BuildResult`, store them on build-profile properties next to
`last_validation`, and draw them with `wrapped()` (don't read `build.json` in
`draw()`). Add an "If validation fails" paragraph to `publish.md#last-build`
that points at Record (`build.json`) and `work/error.txt`.

**Caveats.** The reviewers split. Record already opens `build.json` with the
error and warnings, and failed jobs show their error for the session. The guide
paragraph is the minimum; the panel fields are low-risk polish. An "Open error
log" button is optional.

**Evidence.** `geotools/blender/publish.py:941-953`;
`geotools/publish/job.py:503-512, :532-541, :692, :1003-1011`.

### UX-4 Language sweep and lint

`ui-language-sweep` · Worth doing · Effort S · Scores 3, 4

**What and why.** Strings users read when a button is greyed out say "Set or
ingest a source collection first", and user-linked pages carry milestone and
issue codes, against the project's own UI rule. Reword the ten "ingest"
messages and the `properties.py:894` description (keep the
`geotools.ingest_source` idname); remove milestone jargon from
`texture-cleanup.md`, `guide/layers.md:16` and `analysis-presets.md`; add
tooltips for CSF and float32 (keep RL); widen the `common.py:40` pyproj-missing
note. Add an AST lint in `tests/core` over `poll_message_set`, report and RNA
description strings, plus a bare `M<number>` check on `docs/guide` and the
pages it links.

**Caveats.** Edit `analysis-presets.md` only after the terrain validation work
has merged. Branch from current `develop`.

**Evidence.** `geotools/blender/checks.py:22`, `layers.py:1093`,
`sun_hours.py:688`, `presentation.py:693`, `source.py:280`, `publish.py:415`;
`docs/design/ui-structure.md` (Rules); `geotools/blender/common.py:40`.

### UX-5 Issue-code reference page

`issue-code-reference` · Later · Effort M · Scores 2, 4

**What and why.** About 76 issue codes are emitted, and `publish.md` documents
17. A full `docs/guide/issues.md` with stable anchors, a "?" button in the
Checks detail box, and a coverage test. A cheaper subset: add the 15 or so
preflight, placement and CRS codes users actually meet to the `publish.md`
table.

**Caveats.** The messages already say what to do, and many have remedy
buttons. The help button depends on a docs-URL helper that doesn't exist yet
(see [UX-2](#ux-2-support-entry-points)). The coverage test must find codes
through the AST at call sites (`gpkg-srs`, for example, is a label, not a
code).

**Evidence.** `docs/guide/publish.md` ("Issues you may meet");
`geotools/core/preflight.py:283-360`; `geotools/core/placement.py:136-191`;
`geotools/blender/panels.py:565`.

## Performance

### PERF-1 Measure Publish's main-thread work at 10M triangles

`bench-publish-main-thread` · Worth doing · Effort M · Scores 3, 4

**What and why.** M2's exit is a 10M-triangle publish, but nobody has measured
the UI freeze before the worker starts, or peak RAM given `_merge`'s second
copy; the only figure is 100 s at 2M. First run `bench_inspect.py --triangles
10000000` for one data point. Then time each phase (`perf_counter`, plus the
RSS increase or a VmHWM reset) around `run_checks`, `save_edited_images`,
`image_records`, `layer_records` and `prepare_glb`/`_extract_scene`, and
record the results in a design doc table.

**Caveats.** `write_glb` runs on the worker, so it is out of scope. A
benchmark fixes nothing by itself; change the extraction path only if the
numbers show a severe freeze or OOM on 16-32 GB machines. Run on a 64 GB
machine.

**Evidence.** `docs/ROADMAP.md:207-209`;
`docs/design/m7-output-inspection.md:220-226`; `geotools/blender/publish.py:440-489, :532-534`;
`geotools/blender/export.py:1292-1295, :1520-1550`.

### PERF-2 Read image size without decoding

`image-size-without-decode` · Worth doing · Effort M · Scores 3, 3

**What and why.** Confirmed in Blender 5.2.4: reading `image.size`,
`is_float`, `file_format` or `depth` on an unloaded image decodes it (a blank
8K PNG took 1.4-8 s and about 256 MiB, kept for the session). Import and the
first Run Checks therefore decode every texture, against `summarise.py`'s
"cheap by design". Add a size and format sniffer next to the PNG and JPEG
header parsers in `core/images.py` for unloaded, unpacked file images, falling
back to RNA for packed, generated, dirty and tiled images, and route
`summarise.image_summary` and `texture_state.image_state` through it.

**Caveats.** The cost is paid once per session, Material Preview decodes
anyway, and packed GLB sources gain nothing. Leave the per-image tool UI as it
is. Test that `has_data` stays False after `summarise_collection` and
`changes_since`, including progressive and CMYK JPEGs.

**Evidence.** `geotools/blender/summarise.py:3-6, :114, :123`;
`geotools/blender/texture_state.py:216`; `geotools/blender/source.py:99-111, :211`;
`geotools/core/images.py:150-184`.

### PERF-3 Lower 16K texture-cleanup peak memory

`texture-cleanup-memory-16k` · Later · Effort M · Scores 2, 3

**What and why.** Texture Cleanup on a 16K image peaks around 12 GiB. Use int32
indices in `pad_islands`, scale in place in `keep_for_undo`, catch
`MemoryError` in the tools, and add measured 16K figures to the docs next to
the 8K ones.

**Caveats.** The single-image tools write only at the end, so a failure leaves
the image untouched. The real partial-write bug was scheduled for 0.3.0 as its
own item (`balance-apply-atomic`: make Apply Balance all-or-nothing). int32
saves only about 2 GiB of the 12 GiB peak. On Linux the OOM killer usually
acts before `MemoryError` is raised.

**Evidence.** `geotools/blender/texture_cleanup.py:338-346, :472-610`;
`geotools/core/texture_cleanup.py:379-388`; `geotools/core/gltf_writer.py:151`;
`docs/texture-cleanup.md:256-275`.

### PERF-4 Cache the macOS memory read in draw

`macos-vmstat-draw-cache` · Later · Effort S · Scores 2, 4

**What and why.** With Memory GiB on Auto, the Build Settings panel calls
`sysmem.available_memory_bytes()` on every redraw, and on macOS that forks
`vm_stat`. Add a short TTL cache for the draw path; publish keeps the fresh
read.

**Caveats.** The panel is closed by default and `vm_stat` takes milliseconds.
Skip the ctypes `host_statistics64` route, which Linux CI can't test. Worth
picking up if someone is already in `sysmem.py`.

**Evidence.** `geotools/blender/panels.py:752-760`;
`geotools/core/sysmem.py:23-31, :62-64`; `geotools/blender/properties.py:331-336`.

### PERF-5 Don't pin full RGBA buffers

`texture-layer-strided-views` · Later · Effort S · Scores 2, 4

**What and why.** Three texture-layer readers keep `pixels[::channels]` views,
which pin the full four-channel float32 buffers. Use `np.ascontiguousarray`
on the three readers and assert in a test that the kept arrays own their data.

**Caveats.** The default layer resolution is 1024 px, the real saving is about
2x rather than 4x because Blender keeps its own buffers, and 8K float layers
mostly hit the 32 MiB cap anyway. No benchmark needed.

**Evidence.** `geotools/blender/layers.py:479-482, :504`;
`geotools/core/ramps.py:82, :260-261`; `geotools/blender/glb_export.py:288-296, :395`;
`geotools/blender/presentation.py:462-470, :503-507`.

### PERF-6 Keep texture hashing out of draw

`texture-hashing-off-draw` · Later · Effort S · Scores 2, 4

**What and why.** The Publish panel draw path can SHA-256 packed images and
on-disk files. Part 2 is a small, safe tidy-up: pass the last record's states
into `image_records` at publish, as `changes_since` does, so the `same_file`
shortcut applies. Part 1 (compare only stat and size in draw) would change what
the panel means for packed images (always "may have changed").

**Caveats.** A first publish hashes anyway. The cost is about a second per GB
once per file open, and nobody has reported it.

**Evidence.** `geotools/blender/panels.py:707`;
`geotools/blender/texture_state.py:219-222, :258-260, :293-305`;
`geotools/blender/publish.py:489`.

### PERF-7 Refresh layers once per publish

`refresh-layers-once` · Later · Effort S · Scores 2, 4

**What and why.** Publish refreshes layers in `run_checks` and again in
`prepare_glb`. Skip `update_value_range` on the second pass, and apply the same
to File > Export.

**Caveats.** Still refresh the status on the second pass: the `IMAGE_UNSAVED`
status changes after `save_edited_images`, so a plain `refresh=False` would
break it. With default 1024 px layers the saving is tens of milliseconds.

**Evidence.** `geotools/blender/publish.py:440`; `geotools/blender/checks.py:77`;
`geotools/blender/glb_export.py:366-367, :383-395`; `geotools/blender/layers.py:784-800`.

### PERF-8 Redraw only the sidebar during publish

`sidebar-only-redraw` · Later · Effort S · Scores 2, 4

**What and why.** Publish progress redraws every UI region every 0.25 s for the
whole build. Add a `redraw_sidebar` helper used only in `update_from_job`.

**Caveats.** Leave the links timer alone: it redraws only after a real mesh
swap. Tiling runs on Rust threads, a static viewport redraw is cheap, and the
gain hasn't been measured.

**Evidence.** `geotools/blender/publish.py:55, :216-244`;
`geotools/blender/common.py:63-70`; `geotools/blender/links.py:328-341`.

## Testing and CI

### TEST-1 CI against a source-built rusty-tiles wheel

`ci-prerelease-rusty-tiles-wheel` · Worth doing · Effort M (upper end) · Scores 3, 4

**Done 2026-11-14, by another route.** rusty-tiles 0.4.1 is on PyPI, the pin
moved to it, and the `blender-wheel` job now gates with the released wheel and
`--expect available`, so no source-built job is needed. The text below is kept
for the record.

**What and why.** Every CI job runs with `--expect missing`, so Publish end to
end, Verify Placement, CRS verdicts, inspect/decode and vector publish have
only ever run on the maintainer's desktop. The `blender-wheel` job fails at
"Install and test in Blender" while the run reports success. Add a
non-gating Linux job that builds the wheel with maturin-action
(manylinux_2_28, `RUSTY_TILES_DISABLE_NATIVE_JPEG=1`, as rusty-tiles'
wheel-acceptance does) from a pinned rusty-tiles SHA with the 0.4.1 API (for
example rusty-tiles `34e22f19`, still versioned 0.4.0 with the 0.4.1 API), and
caches it per SHA. Then run `fetch_wheels.py --from-dir`, `build_extension.py`,
the Blender suite with `--expect available`, and the core tests with the wheel
installed. Add a small contract test for about six call sites.

**Caveats.** Delete the job when the pin moves to a released 0.4.1 wheel; if
that has happened, a plain job with the released wheel replaces it. The pinned
SHA must still report the version the existing pin and `--expect-version`
match. Expect the first run to turn up failures never seen on the desktop. A
source build avoids a cross-repo token. The release-day gate catches the same
failures, only later and all at once. Fix the stale `blender-wheel` comment
and `CONTRIBUTING.md:112`.

**Evidence.** `.github/workflows/ci.yml:84-111, :113-156, :158-170`;
`geotools/core/deps.py:27-36, :136-151`; `tests/blender/test_publish.py:220-475`;
`geotools/publish/job.py:613-623, :1004`; `geotools/core/output_tiles.py:176`.

### TEST-2 External validators in CI

`external-validators-in-ci` · Worth doing · Effort S · Scores 3, 4

**What and why.** "Standards-based output" is a hard rule, and the M4 exit
criterion has only been checked by hand. Add a separate Linux job (out of the
required core job) with Node 22 LTS, `npm ci` from a committed lockfile for
`3d-tiles-validator@0.6.1`, and the pinned Khronos glTF-Validator CLI binary
from GitHub releases with a checksum. Export `GEOTOOLS_TEST_3D_TILES_VALIDATOR`
and `GEOTOOLS_TEST_GLTF_VALIDATOR` so the metadata tests run, include the
validator's stderr in failure messages, and add a `--tilesetFile site.3tz`
check (numErrors == 0) to the publish end-to-end tests.

**Caveats.** The feasibility reviewer ran both validators against the GLB
writer locally: 0 errors, 0 warnings, so the GLB half works today without
rusty-tiles. The `.3tz` checks stay inactive until the wheel is available
([TEST-1](#test-1-ci-against-a-source-built-rusty-tiles-wheel)); verify them
once locally. The validator has native deps (sharp, better-sqlite3); the
glTF-Validator npm package is a library, not the CLI. No GDAL part.

**Evidence.** `tests/core/test_metadata_glb.py:5-9, :605-640`;
`tests/blender/test_glb_metadata.py:13, :418-447`; `docs/ROADMAP.md:274-275`;
`docs/design/m4-glb-metadata.md:308-316`.

### TEST-3 Getting-started acceptance test

`getting-started-acceptance-test` · Worth doing · Effort M (close to S) · Scores 3, 4

**What and why.** The getting-started guide is the README's first page, and no
test checks its numbers. Add `tests/blender/test_getting_started.py` with the
guide's values hard-coded and citing its line numbers: 14,400 faces and
14,641 vertices, E/N/A 500000/5400000/100, Verify Placement about
3.0000000/48.7530130/102.217 at the printed precision, exactly
`georef-height-shift` plus `crs-operation`, 28,800 triangles, 5 tiles, the six
staging files and "already exists".

**Caveats.** The mechanisms are tested elsewhere; this guards the walkthrough.
The tile count and check list could drift with rusty-tiles 0.4.1 final. The
publish, tile-count and staging part runs only where the wheel is available.
Don't change the demo to A=0: that breaks `test_drape_example` and the guide's
deliberate height-shift lesson. Fold in the `projects.md:73` rename ("Import
Mesh..." to "Import Source Mesh...") if the 0.3.0 docs fixes have not already
done it.

**Evidence.** `docs/guide/getting-started.md:75-86, :120-196`;
`tests/blender/test_drape_example.py:173-196`; `.github/workflows/release.yml:95-100`.

### TEST-4 Cover the Blender version range

`blender-version-range-ci` · Worth doing · Effort S · Scores 2, 4

**What and why.** The manifest allows 5.2.0 and later, Blender 5.3.0 was
released on 2026-11-10 (still CPython 3.13), and CI tests only 5.2.4. Make the
Linux Blender job a matrix: 5.2.4 required, 5.2.0 and 5.3.0 non-gating. Log
`sys.version_info` and fail clearly if it differs from the wheels' Python
(3.13). The SHA-256 checksums of the linux-x64 tarballs, from
download.blender.org:

- 5.2.0: `96f6c181a30f4950607839dc84d42a354b250d8a0231b098b59b7bc69c351c48`
- 5.3.0: `dacb8264197a3f7fda6001285a5a506fc97ee943881261c2c3bd63c882f22e05`

**Caveats.** Don't set `blender_version_max`. No weekly beta or daily job:
builder.blender.org builds can't be checksum-pinned, and scheduled workflows
only run from `main`. Whether the docs say "5.2 LTS" or "5.2 and later" is the
maintainer's decision.

**Evidence.** `geotools/blender_manifest.toml:12`; `.github/workflows/ci.yml:20-27`;
`release.yml:20-22`; `README.md:45`; `CONTRIBUTING.md:139-147`.

### TEST-5 A real SKIP outcome in the Blender runner

`blender-runner-skip-outcome` · Later · Effort S · Scores 2, 4

**What and why.** The in-Blender runner has only PASS and FAIL; about 43 tests
return early and count as passes. A `h.skip(reason)` outcome with counts
("412 passed, 37 skipped") would make wheel-gated skips visible. One part can
be done any time: remove the dead `_writer_ready()` guard in `test_export.py`,
which could silently disable the determinism checks.

**Caveats.** The main failure mode was disproved: the availability guards key
off the CLI `--expect` value, so they can't misfire in a run with
`--expect available`. About 13 of the 43 returns are real branches that test
missing mode and must stay passes.

**Evidence.** `tests/blender/run_suite.py:119, :133-147`;
`scripts/run_blender_tests.py:236-241`; `tests/blender/test_publish.py:476`;
`tests/blender/test_export.py:117-122, :145, :764`.

### TEST-6 Panel-draw budget test

`panel-draw-budget-test` · Later · Effort M-L · Scores 2, 3

**What and why.** Nothing stops expensive work creeping into panel `draw()`
at survey scale. If done: count forbidden work (subprocess, hashing,
`has_data` turning True) around StubLayout draws, with only a loose timing
bound.

**Caveats.** Headless CI has no real UILayout and `foreach_get` can't be
patched. As proposed, it would fail on day one against `vm_stat` and image
decoding in draw, so add it after or together with the draw-cost fixes
([PERF-2](#perf-2-read-image-size-without-decoding),
[PERF-4](#perf-4-cache-the-macos-memory-read-in-draw),
[PERF-6](#perf-6-keep-texture-hashing-out-of-draw)).

**Evidence.** `tests/blender/test_ui.py:86-140`; `docs/ROADMAP.md:37`;
`geotools/blender/inspect.py:1117-1119`; `geotools/blender/archaeo_ui.py:91-102`.

### TEST-7 Upgrade, removal and unregister hygiene

`lifecycle-upgrade-remove-hygiene` · Later · Effort M · Scores 2, 3

**What and why.** The lifecycle test checks only a hand-picked list of classes
and properties. Replace it with a generic diff of `bpy.app.handlers` and every
`geotools*` property before enable and after disable, plus hand-listed timers
(they can't be enumerated). Upgrade-in-place and remove phases wait for 0.3.1,
the first extension-to-extension upgrade.

**Caveats.** The unregister code is already symmetric. A CLI `install-file`
never loads the extension, so it can't reproduce the Windows `.pyd` lock.

**Evidence.** `tests/blender/test_lifecycle.py:9-19`;
`geotools/blender/publish.py:995-1013`; `geotools/blender/links.py:488-509`;
`scripts/run_blender_tests.py:158-180`.

### TEST-8 Paths with spaces and non-ASCII characters

`path-stress-ci` · Later · Effort S (salvage) · Scores 2, 4

**What and why.** No test uses paths with spaces, accents or OneDrive-style
folders. The cheap part, any time: add those cases, plus NFC/NFD handling on
macOS, to `tests/core/test_paths.py`, which already runs on all OSes. A full
stress run of the Blender suites from such a path is only meaningful once the
macOS and Windows jobs have wheels.

**Caveats.** The feasibility reviewer flagged a plausible `normcase` gap with
NFC/NFD on macOS. The native Rust and PROJ path handling isn't tested off Linux
until wheels exist there.

**Evidence.** `scripts/run_blender_tests.py:288` (ASCII temp prefix);
`tests/core/test_paths.py`, `tests/blender/test_paths.py` (no such cases).

### TEST-9 Real-BlenderGIS acceptance test in CI

`blendergis-acceptance-ci` · Later · Effort S · Scores 2, 4

**What and why.** The real-BlenderGIS acceptance test never runs in CI. Run
`-k blendergis_acceptance` by hand against BlenderGIS before each tag and after
georeference changes (such as Origin from Latitude/Longitude). Tighten the
always-true assertion at `test_georef_interop.py:191` so it compares every
scene key against the five GeoScene keys plus GeoTools' own groups; this
matters more than the CI job.

**Caveats.** The acceptance test doesn't exercise the lat/lon path, it's
unproven that BlenderGIS installs on 5.2.4, and scheduled workflows only run
from `main`.

**Evidence.** `tests/blender/test_blendergis_acceptance.py:1-16, :34-41`;
`tests/blender/test_georef_interop.py:191`; `geotools/core/geoscene.py:60-65`;
`ci.yml` never sets `GEOTOOLS_BLENDERGIS_ZIP`.

## Deferred parts of 0.3.0 items

These are pieces of do-now items that the review put off rather than
rejected. Check the merged 0.3.0 work first: some may have shipped.

- **Viewer compatibility table** (from `web-viewing-extract-and-viewer-page`).
  Later tier, effort L. 0.3.0 was scoped to a dated check in CesiumJS 1.146
  (Balanced/KTX2 and Lossless), plus QGIS if quick. The full table (ArcGIS,
  iTwin.js, deck.gl and others) goes stale quickly and amounts to public
  claims about other products, so any version of it needs dates and the
  versions tested.
- **Place imported objects at the project origin** (from
  `import-no-silent-reorigin`). When an offset file differs from the project
  origin, a second action next to "Use this offset file as the project
  origin": translate only the new root objects by (file offset minus project
  offset), computed in float64, with a warning when the difference is large,
  because object location is float32. The review allowed it to be deferred if
  it pushed the item to L. Relates to
  [ROB-6](#rob-6-large-coordinate-objply-imports).
- **Case (c) of the re-origin guard** (from `import-no-silent-reorigin`):
  don't apply an offset file when the source collection already held objects
  before the import. The review allowed it to be deferred on its own.

## Considered and rejected

Don't propose these again without new evidence.

- **Point-cloud publish.** A new product surface outside "Source = Blender mesh
  plus layers". A guide note on publishing a companion cloud with the
  rusty-tiles CLI, using the project's CRS and height offset, covers most of the
  need. (The proposal also cited a stale rusty-tiles signature, and a header-CRS
  warning would need a LAS VLR parser.)
- **Cache the linked-data object scan.** Measured at about 3 ms per header draw
  for 1k objects and 8 ms for 5k, so it matters only around 20k objects. A
  name-keyed cache goes stale after rename, undo, duplicate or append and would
  mislead users about link status.
- **An "also write folder" build-profile checkbox** for web viewing. Extract
  for Web covers it.
- **Preview CLI version check.** It puts a subprocess in poll and draw, and
  would warn every user while releases/latest is the 0.4.0 CLI.
- **Re-cutting `release/0.3.0`** and a CHANGELOG-ancestry drift detector. The
  rulesets forbid deleting and force-pushing the branch.
- **A 0.2 detection popup at register** (no context at startup), and a
  coexistence test that `Scene.geotools_analysis` survives (fails by design).
- **Triggering the release rehearsal with `workflow_dispatch`** (`main` has no
  workflow files), or reusing wheel-acceptance run 42992725222 (it lacks the
  0.4.1 API).
- **A `.orig` backup of source textures on publish**, and recording the
  original SHA in `build.json`. The direct fix was scheduled for 0.3.0 instead
  (`publish-never-overwrite-source-images`).
- **An `as_pointer()` check for Texture Cleanup undo.** Memfile undo
  reallocates IDs, so it would block valid undos.
- **A `publish.is_running()` guard in Refresh.** Staging works from files, and
  the import would point up the module graph.
- **A strided `read_band` for DEM import.** Strip and tile decoding can't skip
  columns, and it would destabilise a validated decoder.
- **An online-access gate on raster import** (rasters never reproject), and
  adding `extension validate` to CI (`build_extension.py` already runs it).
- **A "round" shift on import when the project already has an origin.** It
  would misplace data; the shift must equal the existing E/N.
- **GDAL color-relief and QGIS colour-map legend exports** (no GeoTIFF export
  to match), and a panel colour bar (needs `bpy.utils.previews`).
