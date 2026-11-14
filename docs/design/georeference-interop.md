# Scene georeference: BlenderGIS GeoScene keys

GeoTools keeps a CRS project's CRS and origin in the scene's **GeoScene keys**:
the convention [BlenderGIS](https://github.com/domlysz/BlenderGIS) uses. Other
Blender GIS tools then see the same georeference that GeoTools publishes. They
do not need GeoTools, and GeoTools does not need them.

Code: `geotools/core/geoscene.py` holds the rules. It has no `bpy`, and
`tests/core/test_geoscene.py` tests it without Blender. The Blender glue is
`geotools/blender/scene_georef.py`. The tests are
`tests/blender/test_georef_interop.py` and the opt-in
`tests/blender/test_blendergis_acceptance.py`.

## Decisions (owner, binding)

- **Authority.** BlenderGIS's scene keys are the CRS and the origin. GeoTools
  reads and writes them directly and keeps no second copy that could drift.
- **Not a dependency.** GeoTools never imports BlenderGIS and works the same
  without it.
- **Licence.** BlenderGIS is GPL-3. None of its code is copied. The key names
  and what they mean are facts, and GeoTools implements them independently.
- **Vertical offset.** A new CRS project starts with A = 0, because GeoScene's
  Z is absolute height. Existing scenes keep their A. When A is not 0, the
  Project panel and Check warn that GeoScene tools will see every height
  shifted by A.
- **No new keys.** GeoTools adds no non-standard keys such as `crs z`. A and
  the height offset stay GeoTools properties.

## The convention

The keys are custom properties on the Scene ID (`scene["SRID"]` and so on):

| Key | Type | Meaning |
|---|---|---|
| `SRID` | string | The CRS: `"EPSG:7856"`, `"AUTH:code"`, or a proj4 string |
| `crs x`, `crs y` | double | The scene origin in CRS units |
| `latitude`, `longitude` | double | The same origin in WGS84 degrees, kept consistent with `crs x`/`crs y` when they can be computed |
| `scale`, `zoom` | | BlenderGIS's map viewer. GeoTools never touches them |

The frame: Blender X = E − `crs x`, Blender Y = N − `crs y`, +Y is grid north,
and there is no rotation. Z is absolute height. There is no Z origin, and the
vertical datum is not declared.

BlenderGIS treats a scene as georeferenced when it has a valid CRS and a
projected origin. A projected or geographic origin without a CRS, or a
geographic origin without a projected one when there is a CRS, counts as
"broken" there.

## Mapping to a GeoTools project

A SOURCE_CRS project maps one to one:

| GeoTools | GeoScene |
|---|---|
| CRS | `SRID` |
| Offset E, Offset N | `crs x`, `crs y` |
| Offset A | none (GeoScene Z is absolute). A GeoTools property; 0 to agree with GeoScene tools |
| Height offset | none. A GeoTools property |

`scene.geotools.crs`, `origin_e` and `origin_n` are `StringProperty` fields
with get and set callbacks. The Project panel, the EPSG picker, "use data
origin", the offset file, the importers, the analysis tools, Check and Publish
all read and write the project through these fields, or through
`properties.placement_from_props`, which reads them:

- **Keys are live** in a SOURCE_CRS project whose scene has a `SRID` key. The
  fields then read and write the keys. E and N show `repr` of the double, so
  they round-trip at full binary64 precision. BlenderGIS can store a whole
  number as an int, which reads as the same float. Text that is not a finite
  number cannot become a key, so it is refused and the field keeps its value.
- **Otherwise** the fields read and write GeoTools' own text:
  `scene.geotools.source_crs`, `source_offset_e` and `source_offset_n`, where
  earlier versions kept the project. That happens in a LOCAL or ANCHOR
  project, in a CRS project with no CRS yet, and when the CRS cannot be a
  GeoScene (see below). In these cases the text is the only copy. Whenever
  the keys become live, the text is moved into them and cleared.

How `SRID` is read (`geoscene.definition_of`): BlenderGIS also accepts a bare
number and `+init=epsg:nnnn` as EPSG codes, so GeoTools reads both as
`EPSG:nnnn`. Anything else is used as written, including a value BlenderGIS
cannot parse (such as WKT another tool stored). pyproj and rusty-tiles may
still read such a value. The key itself is never rewritten on read, by the
preset migration, or by an origin edit. It changes only when the user picks
another CRS.

## What GeoTools writes as `SRID`

BlenderGIS's CRS parser accepts a bare number, `AUTH:digits` with exactly one
colon, or a proj4 string whose every parameter starts with `+`. It raises on
anything else, and every BlenderGIS operator that reads the scene CRS fails
with it. A compound code such as `EPSG:7856+5711` is one example. So GeoTools
writes only (`geoscene.srid_for`):

- a plain horizontal `EPSG:nnnn`. With pyproj, a single code that pyproj knows
  is compound, vertical or 3D (for example `EPSG:5555` or `EPSG:4979`) is not
  written either;
- a proj4 string, when the project uses one.

The vertical datum stays on the GeoTools side, through A and the height offset.

## What GeoScene cannot express

- **LOCAL and ANCHOR projects.** There is no GeoScene for local metres or a
  WGS84 anchor with a heading, so GeoTools writes no keys for them. If the
  scene has keys anyway (from BlenderGIS, or from before a switch to LOCAL),
  GeoTools leaves them alone. The Project panel and Check warn that the
  project ignores the scene's georeference. **Use the Scene's BlenderGIS
  Georeference** switches to a CRS project that uses the keys, with A = 0.
- **A CRS BlenderGIS cannot parse:** WKT, a compound code, another authority.
  Choosing one in a CRS project removes the five keys GeoTools manages and
  moves the CRS and E/N back into GeoTools' own text. The removal is needed
  because stale keys would otherwise stay the authority. The panel says that
  other tools now see the scene as not georeferenced. Choosing an EPSG code or
  a PROJ string again writes the keys back.
- **A and the height offset.** GeoScene's Z is absolute. An A that is not 0 is
  kept, with the warning.

## Keeping lat/lon in sync

Whenever GeoTools sets or changes the CRS or the origin, it rewrites `latitude`
and `longitude` (`geoscene.sync_lat_lon`). It does this when a field is
edited, when a picker entry is chosen, during migration, and when a project
adopts or promotes a georeference.

- **Datum convention: rusty-tiles' own.** The origin goes through the bundled
  pyproj from the definition rusty-tiles resolves the CRS to
  (`resolve_source_crs`). GDA2020, GDA94 and NZGD2000 thereby take its null
  `+towgs84=0,0,0,0,0,0,0` shift to WGS84, which is how the published tiles
  are placed. If rusty-tiles is missing or refuses the CRS, pyproj's default
  operation is used. For those datums that is the same null shift. The
  acceptance test checks the result against BlenderGIS's own `reprojPt` to
  within 1e-9°.
- **Without pyproj, or with a CRS pyproj cannot read,** the old `latitude` and
  `longitude` are **deleted**, not left stale. This follows BlenderGIS's
  `setOriginPrj`: a failed reprojection deletes the out-of-sync geographic
  origin and keeps the projected one, which BlenderGIS treats as primary.
- Without a CRS and a complete projected origin there is nothing to compute
  from, so GeoTools leaves the keys alone, as BlenderGIS does.
- Reading never syncs. Lat/lon that BlenderGIS wrote stay as they are until
  GeoTools changes the CRS or the origin.
- A geographic origin on its own is never overwritten: see the next section.

**Changing the CRS** keeps the E/N numbers and recomputes lat/lon. This is a
deliberate difference from BlenderGIS. Its CRS setter reprojects the origin so
it stays on the same spot on Earth. In GeoTools, the CRS names what the
scene's coordinates are *in*, so changing it re-reads the same numbers.

## An origin from latitude/longitude

Some GeoScene tools set only a geographic origin. BlenderGIS can leave a scene
with `latitude`/`longitude` and no `crs x`/`crs y`, and iconem's
blender_ogc3dtiles writes `latitude`/`longitude` when it sets its own origin.
BlenderGIS's CRS switch projects such an origin into the new CRS. GeoTools
offers the same, but only when the user asks for it:

- **Nothing is invented over it.** In a SOURCE_CRS project, choosing a CRS (or
  migration) on a scene with `latitude`/`longitude` and no projected origin
  writes `SRID` but no zero E/N, and does not recompute `latitude`/`longitude`
  from E/N GeoTools held (`geoscene.reconcile`). Without lat/lon the old rule
  stands: E/N 0.
- **The offer** (`geoscene.lat_lon_origin`). A SOURCE_CRS project with a CRS
  GeoTools can write as `SRID`, and a scene with finite `latitude`/`longitude`
  but no projected origin (no `crs x`/`crs y`, no GeoTools E/N): the Project
  panel and Check warn (`georef-origin-lat-lon-only`) and offer **Origin from
  Latitude/Longitude**.
- **Disagreement.** When E/N exist and `latitude`/`longitude` lie more than
  1 cm from them (`LAT_LON_TOLERANCE_M`, the haversine distance between the
  keys' position and E/N located with `origin_lon_lat`), the panel and Check
  say by how many metres (`georef-origin-lat-lon-differ`) and offer the same
  operator. Nothing is changed until the user chooses. GeoTools keeps placing
  the scene by E/N.
- **The operator** (`geotools.origin_from_lat_lon`,
  `geoscene.set_origin_from_lat_lon`) projects `latitude`/`longitude` into the
  project CRS with pyproj (`always_xy`, the inverse of `origin_lon_lat`, so
  the same datum convention) and writes the doubles, unrounded, as a typed E/N
  is written: into `crs x`/`crs y`, then lat/lon are re-synced. It is
  undoable, reports the E/N it set, and asks first when it replaces an
  existing origin under a source collection with objects. Without pyproj it
  refuses and changes nothing.
- **LOCAL and ANCHOR** projects leave the keys alone, as before.
- **Height.** GeoScene has no height key, so the operator sets E/N only.

## Changing the origin (the rule)

BlenderGIS's own origin edits (`updOriginPrj`, `moveOriginPrj`, and the
panel's X/Y fields with "lock objects" on) translate the scene's top-level
objects by the opposite amount, so the objects stay on the same spot on Earth.
GeoTools' origin changes do not move objects: they define where the scene's
coordinates are.

- **Offset E/N fields and the offset file.** An explicit edit: the scene is
  re-read at the new origin, so its objects move on Earth. The field tooltips
  say so.
- **Use data origin.** It defines the origin for data that has none in the
  scene yet. It asks for confirmation first when the source collection has
  objects. **When the scene already has a complete GeoScene georeference**
  (`SRID`, `crs x`, `crs y`), it never gives the scene a new origin. The
  project adopts the existing georeference, and the data is imported against
  it. Objects another tool placed against that origin therefore stay where
  they are. The offer's button and the confirmation say so.
- **Migration and adoption** write the values the scene already used, so no
  placement changes.
- **A failed import** restores the mode, the keys and GeoTools' own text
  exactly (`scene_georef.snapshot`/`restore`).

**The rule:** GeoTools changes `crs x`/`crs y` only through an explicit edit
of the origin by the user (the E/N fields, an offset file, a confirmed "use
data origin" on a scene with no GeoScene origin, Origin from
Latitude/Longitude). It never changes them as a
side effect of an import, a CRS change, migration or a mode switch. Objects
therefore never become desynchronised from the keys without the user asking
for the scene to be re-read at a new origin.

## Migration

A `.blend` from an earlier GeoTools has its CRS and E/N in GeoTools' own text
and no keys. Migration runs on file load and when the extension is enabled.
It first migrates the old PROJ-string presets in that text to their EPSG codes
(`crs_picker.migrate_scene`). Then `geoscene.reconcile` runs for each
SOURCE_CRS scene:

- **No `SRID` yet, and the CRS can be a GeoScene:** the CRS becomes `SRID`, and
  E/N become `crs x`/`crs y` (0 if they were never set, as the project used
  them, unless the scene has `latitude`/`longitude`: see above). Origin keys
  that already exist win over the text.
- **A `SRID` exists:** the keys win. GeoTools' text fills only absent origin
  keys. Values that differ from the keys are dropped and named in the notice.
- **The CRS cannot be a GeoScene** (WKT, compound, empty): nothing changes, and
  the text stays the georeference.

The text is then cleared, so no second copy remains. Lat/lon are synced when a
key was written. A and the height offset are not touched. The Project panel
shows a one-time notice, kept until dismissed, that names what was taken from
where, for example: *"Taken from GeoTools' project settings into the scene's
GeoScene keys: CRS EPSG:7856 (SRID); origin E 502000.0 (crs x); origin N
6961000.0 (crs y). Offset A and the height offset stay GeoTools settings."*
Once migrated, the file has nothing left to migrate and shows no further
notice. The same reconcile runs when a project becomes SOURCE_CRS. In that
case a notice appears only when GeoTools' own values were dropped. A test
checks that a migrated scene publishes the same `build.json` placement and the
same tileset root transform.

LOCAL and ANCHOR scenes are left as they are.

## Not done

- GeoTools does not write `_RNA_UI` or the keys' UI data (descriptions and
  ranges). BlenderGIS still draws the keys without them.
- There is no automatic sync from lat/lon-only edits: GeoTools reports them
  and offers Origin from Latitude/Longitude, and never changes the origin on
  its own.
