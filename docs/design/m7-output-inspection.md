# M7 design: output inspection

Status: built for M7 (see [ROADMAP.md](../ROADMAP.md#m7-output-inspection-needs-c-in-rusty-tiles-041)),
against rusty-tiles' decoded tile views (#369, `feat/decoded-tile-export`,
released in 0.4.1).

## Goal

Open published tiles back in Blender, over the source they were made from, and
trace any output face to the source faces it came from:

1. **Inspect Output** lists the tiles of the last published archive or a
   chosen `.3tz` and opens them as read-only objects, placed in the scene's
   frame so they overlay the source;
2. **Trace to Source** selects the source faces of selected output faces, and
   the reverse;
3. **Quality** shows a coarse tile's distance to the source, the texel density
   of each face, and levels of detail side by side.

Exit: open any tile of a published archive and trace it back to its source
faces. `tests/blender/test_inspect.py::test_exit_open_leaf_and_coarse_and_trace`
does that end to end on a synthetic publish, for a leaf and the coarse root.

## Layout

| Module | What it does |
|---|---|
| `core/output_tiles.py` | The **only** caller of `rusty_tiles.list_tiles` and `decode_tile`. Normalises their results into `TileEntry` and `DecodedTile`, resolving the feature IDs into per-face source IDs; `triangle_counts` reads tiles' GLB headers. An API change before 0.4.1 is a change here only. |
| `core/output_frame.py` | The publish placement, inverted: `SceneFrame.to_scene` from the tile frame to Blender world coordinates, for the placement build.json recorded. |
| `core/inspect.py` | The build record beside an archive (`OutputRecord`), a decoded tile as flat mesh arrays (`assemble`), tracing both ways, the face map check, texel density. |
| `blender/inspect.py` | Settings (`scene.geotools_inspect`), the operators and the mesh, image and material building. |
| `blender/inspect_ui.py` | Inspect Output, with Trace to Source and Quality, after Publish. |

`core` modules are bpy-free and tested with plain pytest
(`tests/core/test_output_tiles.py`, `test_output_frame.py`, `test_inspect.py`),
against fakes and, when the wheel is installed, the real rusty-tiles.

## The dependency: no fallback

GeoTools 0.4.0 requires rusty-tiles 0.4.1. `deps.REQUIRED_FUNCTIONS` adds
`list_tiles` and `decode_tile` to the CRS functions, so a wheel without them is
reported as not available ("has no list_tiles or decode_tile; GeoTools needs
rusty-tiles 0.4.1 or later"), in About and Check Dependencies, and every
inspection operator says the same. There is no GLB importer path.

## Reading tiles

`decode_tile(archive, uri, format="arrays")` is used, never `format="glb"`:
Blender's glTF importer may merge vertices that differ only in a custom
attribute, which would smear per-face IDs across neighbours. The arrays need no
importer at all.

Feature IDs arrive per vertex and are **rows of a property table**, not IDs: a
`source_primitive` value indexes the `source_primitive` table, whose
`primitive_index` column is the source primitive, and a `source_triangle`
value indexes the `source_triangle` table (`primitive_index`,
`triangle_index`). `decode_tile` resolves both through the tables at the first
corner of each face (rusty-tiles: "a face's ID is the value at any of its
corners") into per-face int64 arrays, checks that the two tables agree on the
primitive, and maps a null feature ID to −1. Those are exactly the face map's
`(primitive, triangle)` numbering (`core.facemap`).

Leaves carry both sets. Coarse tiles carry `source_primitive` only: one
representative source primitive per triangle.

Triangle counts in the list come from each tile's GLB JSON chunk (accessor
counts; `.3tz` members are stored, not compressed), so listing a 341-tile
archive takes 0.06 s and decodes nothing.

## Placement

Tile positions are float64 in the Z-up tile frame; `transform` maps the tile
frame to the tileset's world (ECEF for placed output). The source sits in
Blender where it was published, so the inverse uses the placement build.json
recorded (`placement`: the exact `mesh_to_tileset` keywords), never the scene's
current settings. Listing compares the two and notes any difference ("The
scene's placement changed since this publish (anchor): tiles are placed with
the recorded one").

| Mode | Scene position |
|---|---|
| LOCAL | `transform @ p`: the tileset world is the source frame (rusty-tiles turns glTF Y-up back to Z-up). |
| ANCHOR | `anchor⁻¹ @ transform @ p`, with `anchor` the rigid frame rebuilt from the recorded anchor, orientation and scene offset: scene offset, heading about up, then east-north-up at the anchor (the frame `core.georef` and `analysis.site` use). |
| SOURCE_CRS | `transform @ p` gives ECEF in binary64; one vectorised pyproj call maps it to the project CRS with ellipsoidal heights (EPSG:4978 to the CRS made 3D); then `x = E − offset E`, `y = N − offset N`, `z = h − height offset − offset A`. |

Every step is binary64; positions are cast to float32 once, into the mesh,
exactly as the source's own positions are stored.

Checks, made when the archive is listed and shown as notes:

- **ANCHOR**: the rebuilt matrix is compared with the archive's root
  transform over a 1 km box; over 1 mm means the record does not belong to the
  archive. The core tests check the rebuilt matrix against rusty-tiles' root
  transform (equal to 10⁻⁶ m).
- **SOURCE_CRS**: pyproj picks its own datum operation; rusty-tiles uses the
  one its allow-list names (`crs_operation` in build.json). rusty-tiles' ENU
  origin is the mapped centre of the source bounds, and `source.glb` beside
  the record gives those bounds, so taking the root transform's origin back
  through the frame must land on the bounds centre. A residual over 1 mm is
  removed as a translation (two datum operations differ by a near-constant
  shift over a site) and reported. Measured residuals: 0.000 mm for GDA2020
  MGA 56 (EPSG:7856), GDA94 MGA 56 (28356), ETRS89 UTM 32N (25832), NZTM2000
  (2193), Web Mercator (3857), WGS 84 UTM 56S (32756), and 0.002 mm for
  Lambert-93 (2154): pyproj chooses the same operations.
- A `.3tz` with no build record beside it (copied elsewhere, or not published
  by GeoTools) is placed with the scene's current placement, and says so.

### Accuracy

Leaf vertices against the source vertices their IDs name, 20,000 triangles on
100 m leaves, every placement and profile (measured with
`maximum_leaf_geometry_error` from rusty-tiles' report beside it):

| Profile | LOCAL, ANCHOR | SOURCE_CRS (MGA 56, UTM 56S) | rusty-tiles' `maximum_leaf_geometry_error` |
|---|---|---|---|
| `fast`, `balanced` | 1.953 mm | 3.306 mm | 1.953 mm; 3.305 mm |
| `quality`, `lossless` | 0 | 0.005 mm | 0 |

So the inverse adds at most 0.005 mm (float32 in the tile's east-north-up
frame, through pyproj and back); everything else is rusty-tiles' own position
quantisation in the lossy profiles, which its report states as
`maximum_leaf_geometry_error` and which the measured error matches exactly. The
same holds with the site 3 km from the scene origin. The Blender tests assert leaves land within
`maximum_leaf_geometry_error + 0.01 mm` (FAST, ANCHOR) and within 0.02 mm for
QUALITY in LOCAL, ANCHOR and SOURCE_CRS projects.

## Opening

A decoded tile becomes one mesh object, built with `foreach_set` only:
vertices (one per primitive vertex: rusty-tiles' leaves are unwelded), corner
vertices, `loop_start` and `material_index`; `source_primitive` and (leaves)
`source_triangle` as INT FACE attributes; TEXCOORD_0 as the UV map (v flipped
to Blender's convention); COLOR_0 as a colour attribute; normals as custom
normals, turned into the scene by the frame's rotation at the tile's centre.

Each texture page becomes a packed image (rows flipped: pages arrive top row
first), and each glTF material a Principled BSDF with its base colour factor,
metallic and roughness, and its base colour texture (Closest interpolation for
a nearest-filtered sampler). Materials and images are tagged so Remove Opened
Tiles deletes them with the objects.

Tiles go into a "GeoTools Output" collection, with locked transforms and a
`geotools_output` record (archive, URI, depth, role, geometric error, texels
per material slot). They are read-only in the sense that matters: nothing in
GeoTools writes them back, and they are not in the source collection, so they
are never published. **Open Tiles** takes the selected tile, every tile at one depth, or
every leaf, refuses more than the Limit (64 by default), and skips tiles
already open.

## Tracing

The face map (`facemap.npz` beside the archive) has one row per source
triangle; row `primitive_offsets[p] + t` is triangle `t` of primitive `p`,
with its object and Blender polygon.

- **Select Source Faces** (active output tile's selected faces): leaf faces
  give their rows directly; coarse faces select **every row of their
  representative primitive**, and the report says so ("Coarse tile: its faces
  carry only a representative source primitive, so every face of primitive 0
  is selected"). The source objects' polygons, vertices and edges are
  selected with `foreach_set` and the objects enter Edit Mode.
- **Select Output Faces** (selected source faces): the selected polygons
  become a row mask (`selected_rows`), and each opened tile of the listed
  archive selects the faces whose row is in it (leaves) or whose primitive has
  any selected row (coarse, reported as matched by representative primitive
  only).
- **Refusal**: before either, every face-map object's current topology digest
  (`facemap.topology_digest` of the original mesh) is compared with the one
  recorded at publish. A missing object, a changed digest or an entry recorded
  as `evaluated:` (modifiers that change topology, so the map numbers
  evaluated faces) refuses the trace with the reason and "Publish again to
  trace these faces".

Everything is numpy over the face map's arrays; the only loops are over
objects and tiles.

## Quality

### Distance to the source

A stock Geometry Nodes group (`GeoTools Output Distance`: Collection Info of
the source collection with a relative transform, Realize Instances, Geometry
Proximity to faces, Sample Nearest Surface of the face normal) stores each
face's signed distance from its centre to the nearest source face,
positive on the side the source faces. It is evaluated once, read into a FACE
attribute `surface_distance` and the modifier removed; values beyond Max
Distance (default four times the tile's geometric error) are no data. Faces are
coloured with the diverging ramp over ±the 98th percentile of |d|, and the
panel shows RMS and maximum beside the tile's geometric error.

It measures at face **centres**, not vertices, which is why the batch 2
Distance to Surface preset is not reused as it is: that preset measures at
vertices, and rusty-tiles' simplification keeps coarse vertices on the source,
so on the 2-million-triangle benchmark it gave an RMS of 0.6 mm for a root of
geometric error 11.4 m. A face centre is where a flat coarse triangle departs
most from a curved source. The group is the preset's own nodes on the FACE
domain; it reads the whole source collection, so no reference object is
needed, and a relative transform keeps it right for tiles moved side by side.

### Texel density

Per face: `UV area × page width × page height / 3D area`, in texels per m²,
from the tile's UVs, the page its material samples and its scene positions,
stored as a FACE attribute `texel_density` and coloured on a log viridis ramp.
The panel shows the median, the ground size of one texel at the median and
the 5–95% range. Untextured faces are no data. On the benchmark a leaf shows
17 texels/m² for a source of 4,096² texels over 1 km² (16.8): leaves keep the
source density.

### Levels

Opened tiles overlay one another in place. **One Depth** shows only the tiles
at a chosen depth, hiding the others, so levels can be toggled with the same
framing; **Side by Side** offsets each depth along +X by the opened tiles'
width plus a gap; **All** puts everything back. Trace and the distance view
work on moved tiles (face IDs and a relative transform).

## Timings

`scripts/bench_inspect.py` (2,000,000-triangle terrain, one 4,096² texture,
ANCHOR, `balanced`; 16-core Linux, Blender 5.2.4). Two runs: a quiet machine,
and one shared with other builds (the publish alone took 1.7 times as long):

| Step | Quiet (s) | Shared (s) |
|---|---|---|
| Publish (for reference) | 100 | 172 |
| List (341 tiles, with triangle counts) | 0.06 | 0.12 |
| Open the coarse root (6,144 triangles, its page) | 0.12 | 0.26 |
| Open the largest leaf (7,813 triangles, its page) | 0.09 | 0.15 |
| Open one depth (64 tiles, 393,179 triangles) | 3.1 | 4.7 |
| Select Source Faces from a whole leaf (3,969 polygons of a 1M-polygon source) | 1.2 | 2.2 |
| Select Output Faces into 66 opened tiles | 0.7 | 1.2 |
| Distance to the source (root, face centres) | | 1.2 |
| Texel density (leaf) | 0.01 | 0.02 |

On the shared run the root's face centres were 13.9 mm RMS and 54.7 mm at
most from the source, against a geometric error of 11.4 m.

Most of an Open is building and packing the page image; Select Source Faces is
mostly hashing the source's topology for the face map check.

## Decisions and deferrals

- **Arrays, not the GLB view**, for exact per-face IDs (above).
- **The recorded placement, not the scene's**, with the differences noted.
- **Coarse tiles trace to whole primitives.** rusty-tiles keeps one
  representative primitive per coarse triangle, so that is the finest
  answer; a source with one material is one primitive, and its coarse faces
  select everything. The report says so every time.
- **Sources published with topology-changing modifiers are refused** for
  tracing: their face map numbers evaluated polygons, which cannot be
  selected. Apply the modifiers or publish with Apply Modifiers off.
- **No write-back.** Opened tiles are views; rusty-tiles cannot re-encode an
  edited view, and edits belong on the source.
- **Distance at face centres** only; a denser sampling (or the source's
  distance to the coarse surface) is a later refinement if needed.
- **Tileset directories**: rusty-tiles reads them as well as `.3tz`, and one
  can be typed into File, but only a `.3tz` has a GeoTools record and face
  map beside it.
