# Sidebar structure

The GeoTools tab of the 3D Viewport sidebar (N), after the 0.3.0 UI
consolidation. Use the panel and button names here in docs, so the docs and
the sidebar say the same thing.

## Panel tree

Top to bottom, in workflow order: set up the project, bring data in, analyse,
look after the layers and textures, check, publish, inspect the output.
"Closed" means closed by default. Every `bl_idname` is unchanged from before.

| Panel | Opens | `bl_idname` |
|---|---|---|
| **Project** | open | `GEOTOOLS_PT_project` |
| &nbsp;&nbsp;CRS Details | closed | `GEOTOOLS_PT_project_crs` |
| **Source** | open | `GEOTOOLS_PT_source` |
| **Linked Data** | closed | `GEOTOOLS_PT_linked_data` |
| **Analysis** | closed | `GEOTOOLS_PT_analysis` |
| &nbsp;&nbsp;Measure | open | `GEOTOOLS_PT_analysis_coordinates` |
| &nbsp;&nbsp;Presets | open | `GEOTOOLS_PT_analysis_presets` |
| &nbsp;&nbsp;Hydrology | closed | `GEOTOOLS_PT_analysis_hydrology` |
| &nbsp;&nbsp;Contours and Sections | closed | `GEOTOOLS_PT_analysis_contours` |
| &nbsp;&nbsp;Sun | closed | `GEOTOOLS_PT_analysis_sun` |
| &nbsp;&nbsp;&nbsp;&nbsp;Sun Hours | closed | `GEOTOOLS_PT_analysis_sun_hours` |
| &nbsp;&nbsp;&nbsp;&nbsp;Shadow Study | closed | `GEOTOOLS_PT_analysis_shadow_study` |
| &nbsp;&nbsp;Archaeoastronomy | closed | `GEOTOOLS_PT_analysis_archaeo` |
| **Layers** | open | `GEOTOOLS_PT_layers` |
| &nbsp;&nbsp;Bake (texture layers) | open | `GEOTOOLS_PT_layers_bake` |
| &nbsp;&nbsp;Display | closed | `GEOTOOLS_PT_layers_display` |
| &nbsp;&nbsp;Details | closed | `GEOTOOLS_PT_layers_details` |
| **Textures** | closed | `GEOTOOLS_PT_textures` |
| &nbsp;&nbsp;Paint | open | `GEOTOOLS_PT_textures_paint` |
| &nbsp;&nbsp;Cleanup | closed | `GEOTOOLS_PT_textures_cleanup` |
| &nbsp;&nbsp;Balance | closed | `GEOTOOLS_PT_textures_balance` |
| **Checks** | open | `GEOTOOLS_PT_checks` |
| &nbsp;&nbsp;Images | closed | `GEOTOOLS_PT_checks_images` |
| **Publish** | open | `GEOTOOLS_PT_publish` |
| &nbsp;&nbsp;Build Settings | closed | `GEOTOOLS_PT_publish_settings` |
| &nbsp;&nbsp;Last Build (after a build) | open | `GEOTOOLS_PT_publish_last` |
| **Inspect Output** | closed | `GEOTOOLS_PT_inspect` |
| &nbsp;&nbsp;Trace to Source | open | `GEOTOOLS_PT_inspect_trace` |
| &nbsp;&nbsp;Quality | closed | `GEOTOOLS_PT_inspect_quality` |
| **About** | closed | `GEOTOOLS_PT_about` |

Sun Hours and Shadow Study sit inside Sun because they use its date, time
zone, site and refraction; their panels say so instead of repeating those
settings. `tests/blender/test_checks.py::test_panels_order_and_draw` pins the
tree and the closed panels.

## What changed

Before, the top level was Project, Source, Linked Data, Layers, Textures,
Checks, Publish, Inspect Output, Analysis, About, and Analysis had eight
sub-panels side by side (Sun, Sun Hours, Shadow Study, Archaeoastronomy,
Coordinates, Presets, Hydrology, Contours and Sections), with Sun, Coordinates
and Presets open.

- Analysis moved up, after Linked Data and before Layers: its tools write
  the layers that Layers, Checks and Publish then use.
- Sun Hours and Shadow Study are sub-panels of Sun. Sun is closed by default.
- Analysis sub-panels are ordered Measure, Presets, Hydrology, Contours and
  Sections, Sun, Archaeoastronomy.
- In Publish, **Format** comes before **Output** and both are labelled above
  their field, so `.3tz .glb Vectors` is no longer cut off.

Renamed labels (old -> new). No property identifier or `bl_idname` changed,
so saved `.blend` files and scripts are unaffected.

| Where | Old | New |
|---|---|---|
| Analysis sub-panel | Coordinates | **Measure** |
| Source button, `geotools.ingest_source` | Import Mesh... / Ingest Source | **Import Source Mesh...** / Import Source Mesh |
| `geotools.layer_create_attributes` | Create Attributes (button: Create Empty Attributes) | **Create Empty Attributes** |
| `geotools.layer_write_presentation` | Write Presentation (button: Write Colours) | **Write Colours** |
| `geotools.contours` | Make Contours | **Create Contours** |
| `geotools.sections` | Make Sections | **Create Sections** |
| `geotools.inspect_open` | Open | **Open Tiles** |
| Sun > Over a Day, `frames_per_step` | Frames per Step | **Frame Step** |
| Archaeoastronomy > Horizon | Far Horizon for Sun Hours (a second copy of Sun Hours' **Use Horizon Profile**) | removed; a note points to Sun Hours |

Shadow Study no longer draws its own copy of **First Frame**; it says which
frame the keys start at (Sun > Over a Day > First Frame).

## Rules

- **Panels, buttons and property labels** are in Title Case ("Import Source
  Mesh", "Create Contours"). **Help text** (notes, hints, tooltips) is in
  sentence case; a full sentence ends with a full stop.
- **One word per thing:** *Import* brings a file in (not *ingest*);
  *Create* makes new objects or attributes (not *make*); *Compute* runs an
  analysis into layers or a result; *Source* is the source collection; a
  setting that lives in another panel is named as "set in Sun" or with its
  path (Sun > Over a Day > First Frame).
- **Each control is drawn once.** A tool that uses another panel's setting
  says so in a note rather than drawing it again. Sun Hours' date row is the
  exception on purpose: it is the start of the period it adds up.
- **Every property and operator the sidebar draws has a tooltip**, and so
  does every option of every GeoTools operator.
  `tests/blender/test_ui.py` draws each panel in several scene states and
  fails on a missing description or a lower-case word in an operator label.
- **Secondary sub-panels start closed.** Open by default: the setup and
  publishing steps (Project, Source, Layers, Checks, Publish), and the small
  entry points inside closed parents (Measure, Presets, Paint, Trace to
  Source, Last Build).
- Long hints go through `ui.wrapped`, never a single `label`, so they wrap
  to the sidebar width instead of being cut off.

Look at the result with `scripts/ui_screenshots.py --out DIR --states ready`
(and `montage` for every panel open).
