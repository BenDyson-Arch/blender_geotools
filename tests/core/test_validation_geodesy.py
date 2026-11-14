"""Geodesy validation: Measure against independent references (docs/validation/geodesy.md).

The reference values in tests/data/validation/geodesy/ come from
scripts/validation/geodesy/ (Krüger's series, GeographicLib 2.1, GDAL 3.12.0),
never from GeoTools. Tolerances are the doc's, each with its reason.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from geotools.analysis import site
from geotools.core.placement import Placement, PlacementMode

pyproj = pytest.importorskip("pyproj")

DATA = Path(__file__).resolve().parents[1] / "data" / "validation" / "geodesy"
MEASURE = json.loads((DATA / "measure.json").read_text())
SITES = {entry["name"]: entry for entry in MEASURE["sites"]}


def _placement(entry: dict, height_offset: float = 0.0) -> Placement:
    east, north = entry["origin"]
    return Placement(
        PlacementMode.SOURCE_CRS,
        source_crs=entry["crs"],
        source_offset=(east, north, 0.0),
        height_offset=height_offset,
    )


def _angle(a: float, b: float) -> float:
    return (a - b + 180.0) % 360.0 - 180.0


def _triangulate(corners: np.ndarray, cells: int) -> np.ndarray:
    s = np.linspace(0.0, 1.0, cells + 1)
    u, v = (w[..., None] for w in np.meshgrid(s, s, indexing="ij"))
    c0, c1, c2, c3 = corners
    xy = (1 - u) * (1 - v) * c0 + u * (1 - v) * c1 + u * v * c2 + (1 - u) * v * c3
    p = np.concatenate([xy, np.zeros(xy.shape[:2] + (1,))], axis=2)
    a, b, c, d = p[:-1, :-1], p[1:, :-1], p[1:, 1:], p[:-1, 1:]
    return np.concatenate([np.stack([a, b, c], 2), np.stack([a, c, d], 2)]).reshape(-1, 3, 3)


@pytest.mark.parametrize("name", SITES)
def test_read_point(name: str) -> None:
    """Latitude, longitude, scale and convergence against Krüger's series or a Jacobian.

    Tolerances: 1e-8 degrees of latitude and longitude (about 1 mm; the two agree to
    nanometres); 1e-7 in scale (the non-TM references take numerical derivatives,
    good to about 1e-8); 0.001 arc-seconds in convergence (likewise).
    """
    entry = SITES[name]
    placement = _placement(entry)
    for point in entry["points"]:
        reading = site.read_point(placement, tuple(point["local"]))
        assert reading.east == entry["origin"][0] + point["local"][0]
        assert reading.north == entry["origin"][1] + point["local"][1]
        assert reading.latitude == pytest.approx(point["lat"], abs=1e-8)
        assert reading.longitude == pytest.approx(point["lon"], abs=1e-8)
        assert reading.grid_scale == pytest.approx(point["scale"], abs=1e-7)
        assert _angle(reading.convergence, point["convergence"]) == pytest.approx(0.0, abs=3e-7)


@pytest.mark.parametrize("name", SITES)
def test_measure_against_geodesics(name: str) -> None:
    """Ellipsoid distance and true bearing of lines of 10 m to 50 km against GeographicLib.

    The grid chord is not the geodesic's image, so the survey reduction (Simpson's
    rule along the chord) differs from the geodesic by the arc-to-chord terms, which
    grow as the square of the length: the doc's table. Tolerances: 0.05 ppm up to
    10 km (0.2 ppm in Web Mercator, whose straight lines are rhumb lines), 0.4 ppm at
    50 km (8 ppm in Web Mercator at 60 N); bearings 0.05" up to 10 km, 1" at 50 km.
    The ground distance follows the documented convention (Gaussian mean radius at
    the middle, mean ellipsoidal height) exactly.
    """
    entry = SITES[name]
    mercator = entry["crs"] == "EPSG:3857"
    for line in entry["lines"]:
        placement = _placement(entry, height_offset=line["start_height"])
        m = site.measure(placement, tuple(line["start"]), tuple(line["end"]))
        length = m.grid_distance
        ellipsoid = length / m.grid_scale
        rel = abs(ellipsoid / line["ellipsoid"] - 1.0)
        bearing = abs(_angle(m.true_bearing, line["azimuth_middle"])) * 3600.0
        if length < 20_000:
            assert rel < (0.2e-6 if mercator else 0.05e-6), (length, rel)
            assert bearing < 0.05, (length, bearing)
        else:
            assert rel < (8e-6 if mercator else 0.4e-6), (length, rel)
            assert bearing < 1.0, (length, bearing)
        # The reference takes R at the geodesic's middle, GeoTools at the chord's.
        ground = m.ground_distance / line["ground_gaussian"]
        assert ground == pytest.approx(ellipsoid / line["ellipsoid"], rel=1e-9)
        assert m.height_difference == 37.5


@pytest.mark.parametrize("name", SITES)
def test_face_areas_against_geodesic_polygons(name: str) -> None:
    """Ground plan area of 1 km and 10 km squares against GeographicLib polygon areas.

    The CSF is taken at each triangle's centroid, so the error falls as the square
    of the triangle size: under 3e-7 for a 10 km square as two triangles, under
    5e-10 at 32 x 32 cells (2,048 triangles). At a height the reference scales the ellipsoid area by
    ((R + h) / R)^2, with R the Gaussian radius, as GeoTools does.
    """
    entry = SITES[name]
    for polygon in entry["polygons"]:
        placement = _placement(entry, height_offset=polygon["height"])
        corners = np.asarray(polygon["corners"])
        coarse = site.face_areas(placement, _triangulate(corners, 1))
        fine = site.face_areas(placement, _triangulate(corners, 32))
        assert coarse.ground_plan == pytest.approx(polygon["ground_area"], rel=3e-7)
        assert fine.ground_plan == pytest.approx(polygon["ground_area"], rel=5e-10)
        assert fine.ground_surface == pytest.approx(fine.ground_plan, rel=1e-12)  # flat


def test_sloped_surface_area_is_analytic() -> None:
    """A plane z = 0.3 x: grid surface = plan x sqrt(1 + 0.09); at ground level the
    horizontal is divided by the CSF and the vertical kept, so the slope steepens.

    The analytic value takes one CSF for the whole square; GeoTools takes one per
    triangle, and the CSF changes across it (k by 4e-6 over 1 km here, the height
    factor by 5e-5 over the 300 m of relief), so they agree to second order: 1e-8."""
    entry = SITES["mga56-edge"]
    placement = _placement(entry, height_offset=200.0)
    corners = np.array([[-500.0, -500.0], [500.0, -500.0], [500.0, 500.0], [-500.0, 500.0]])
    tris = _triangulate(corners, 4)
    tris[..., 2] = 0.3 * tris[..., 0]
    reading = site.face_areas(placement, tris)
    assert reading.grid_surface == pytest.approx(1e6 * math.sqrt(1.09), rel=1e-12)
    csf = site.read_point(placement, (0.0, 0.0, 0.0)).combined_scale
    expected = (1000.0 / csf) ** 2 * math.sqrt(1.0 + (0.3 * csf) ** 2)
    assert reading.ground_surface == pytest.approx(expected, rel=1e-8)


def test_gaussian_radius_against_geographiclib() -> None:
    """sqrt(M N) against M and N measured on GeographicLib geodesics (to 0.3 mm of 6.4e6 m,
    the precision of the finite differences in the reference)."""
    for case in MEASURE["radii"]:
        a, f = case["ellipsoid"]
        mine = float(site._gaussian_radius(a, f, np.array([case["lat"]]))[0])
        assert mine == pytest.approx(case["gaussian"], abs=5e-4)


def test_icsm_worked_example() -> None:
    """Flinders Peak to Buninyong, the ICSM GDA Technical Manual's example, in MGA zone 55.

    Published: ellipsoidal distance 54 972.271 m. GeoTools reduces the grid line with
    Simpson's rule (as the manual does) and lands within 1 mm, the rounding of the
    published values. Flinders Peak's latitude, longitude, k and convergence match
    Krüger's series.
    """
    icsm = MEASURE["icsm"]
    (fe, fn), (be, bn) = icsm["published"]["flinders_peak_en"], icsm["buninyong"]["en"]
    placement = Placement(
        PlacementMode.SOURCE_CRS, source_crs=icsm["crs"], source_offset=(fe, fn, 0.0)
    )
    m = site.measure(placement, (0.0, 0.0, 0.0), (be - fe, bn - fn, 0.0))
    assert m.ground_distance == pytest.approx(icsm["published"]["distance"], abs=1e-3)
    assert m.ground_distance == pytest.approx(icsm["geographiclib"]["distance"], abs=1e-3)
    assert abs(_angle(m.true_bearing, icsm["geographiclib"]["azimuth_middle"])) * 3600 < 0.5
    d, mi, s = icsm["published"]["azimuth_dms"]
    assert icsm["geographiclib"]["azimuth"] % 360 == pytest.approx(
        d + mi / 60 + s / 3600, abs=0.01 / 3600
    )
    reading = site.read_point(placement, (0.0, 0.0, 0.0))
    flinders = icsm["flinders_peak"]
    assert reading.latitude == pytest.approx(flinders["lat"], abs=1e-8)
    assert reading.longitude == pytest.approx(flinders["lon"], abs=1e-8)
    assert reading.grid_scale == pytest.approx(flinders["scale"], abs=1e-9)
    assert reading.convergence == pytest.approx(flinders["convergence"], abs=1e-8)


def test_float32_limits_at_large_coordinates() -> None:
    """A scene with its origin at the CRS origin: local positions are full MGA
    coordinates, held as float32 to 0.5 m. Readings say so, and E/N are exactly the
    float32 values (never better)."""
    placement = Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:7856")
    local = (503_210.37, 6_961_234.81, 10.0)
    reading = site.read_point(placement, local)
    assert reading.resolution == 0.5
    assert reading.east == float(np.float32(local[0]))
    assert reading.north == float(np.float32(local[1]))
    assert abs(reading.north - local[1]) <= 0.25
    offset = Placement(
        PlacementMode.SOURCE_CRS, source_crs="EPSG:7856", source_offset=(503_000.0, 6_961_000.0, 0)
    )
    near = site.read_point(offset, (210.37, 234.81, 10.0))
    assert near.resolution == pytest.approx(1.5e-5, rel=0.1)
    assert abs(near.north - local[1]) < 2e-5


# --- where publishing puts the source: rusty-tiles against the closed form ----------------

PLACEMENT = json.loads((DATA / "placement.json").read_text())


def _rusty_tiles_available() -> bool:
    from geotools.core import deps

    deps.reset_cache()
    return deps.probe_rusty_tiles().available


needs_rusty_tiles = pytest.mark.skipif(
    not _rusty_tiles_available(), reason="needs rusty-tiles 0.4.1"
)


def publish_to_ecef(tmp: Path, placement: Placement) -> np.ndarray:
    """Publish the reference grid (lossless) and return every vertex in ECEF, binary64.

    The vertices come back from the tiles in the order they went in (a 25-vertex
    mesh is one leaf); they are matched to the source by their nearest neighbour."""
    import rusty_tiles

    from geotools.core.gltf_writer import MaterialData, PrimitiveData, SceneExport, write_glb
    from geotools.core.output_tiles import decode_tile, list_tiles

    co = np.asarray(PLACEMENT["vertices"], dtype=np.float32)
    n = 5
    a = (np.arange(n - 1)[None, :] + n * np.arange(n - 1)[:, None]).ravel()
    tris = np.concatenate([np.stack([a, a + 1, a + n + 1], 1), np.stack([a, a + n + 1, a + n], 1)])
    prim = PrimitiveData(
        material=0,
        positions=co,
        indices=tris.astype(np.uint32),
        source_object=np.zeros(len(tris), np.int32),
        source_polygon=np.arange(len(tris), dtype=np.int32),
    )
    write_glb(SceneExport(("G",), (MaterialData("m"),), (), (prim,)), tmp / "source.glb")
    rusty_tiles.mesh_to_tileset(
        tmp / "source.glb",
        tmp / "site.3tz",
        profile="lossless",
        force=True,
        **placement.mesh_to_tileset_kwargs(),
    )
    out = np.full((len(co), 3), np.nan)
    for entry in list_tiles(tmp / "site.3tz"):
        if not entry.is_leaf:
            continue
        tile = decode_tile(tmp / "site.3tz", entry.uri)
        for primitive in tile.primitives:
            p = np.c_[primitive.positions, np.ones(len(primitive.positions))]
            world = (p @ tile.transform.T)[:, :3]
            for (i, j, k), source in zip(
                primitive.indices, tris[primitive.source_triangle], strict=True
            ):
                for got, want in zip((i, j, k), source, strict=True):
                    out[want] = world[got]
    return out


@needs_rusty_tiles
@pytest.mark.parametrize("entry", PLACEMENT["source_crs"], ids=lambda e: e["name"])
def test_source_crs_publishing_lands_where_the_closed_form_says(
    tmp_path: Path, entry: dict
) -> None:
    """Every published vertex (float32 scene position + binary64 offsets, through the
    project CRS to WGS 84 ECEF) within 1 mm of the reference.

    1 mm: rusty-tiles' allow-list is verified to 1 mm against GDAL; the reference's
    own ambiguity is 0.1 mm (GRS80 against WGS 84 under a null datum shift); a
    lossless tile holds positions as float32 in a frame at the tile's centre, about
    0.5 mm at 5 km. The face-matching loop above is over 32 triangles only."""
    placement = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs=entry["crs"],
        source_offset=tuple(entry["source_offset"]),
        height_offset=entry["height_offset"],
    )
    got = publish_to_ecef(tmp_path, placement)
    want = np.asarray(entry["ecef"])
    error = np.linalg.norm(got - want, axis=1)
    assert error.max() < 1e-3, error.max()


@needs_rusty_tiles
@pytest.mark.parametrize("entry", PLACEMENT["anchor"], ids=lambda e: e["name"])
def test_anchor_publishing_and_readout_match_the_closed_form(tmp_path: Path, entry: dict) -> None:
    """An ANCHOR scene: the published vertices, and Measure's latitude, longitude and
    height of each vertex, against anchor + ENU basis @ heading @ (local + offset)."""
    placement = Placement(
        PlacementMode.ANCHOR,
        anchor=tuple(entry["anchor"]),
        heading_degrees=entry["heading"],
        scene_offset=tuple(entry["scene_offset"]),
    )
    got = publish_to_ecef(tmp_path, placement)
    want = np.asarray(entry["ecef"])
    assert np.linalg.norm(got - want, axis=1).max() < 1e-3
    geodetic = np.asarray(entry["geodetic"])
    for local, (lon, lat, height) in zip(PLACEMENT["vertices"], geodetic, strict=True):
        reading = site.read_point(placement, tuple(local))
        assert reading.longitude == pytest.approx(lon, abs=1e-10)  # 0.01 mm
        assert reading.latitude == pytest.approx(lat, abs=1e-10)
        assert reading.height == pytest.approx(height, abs=1e-6)


@pytest.mark.parametrize("entry", PLACEMENT["source_crs"], ids=lambda e: e["name"])
def test_measure_readout_matches_the_publishing_geodetic(entry: dict) -> None:
    """Measure's latitude, longitude and ellipsoidal height of each vertex (pyproj's
    inverse projection) against the reference (Krüger or Newton): 1e-9 degrees."""
    placement = Placement(
        PlacementMode.SOURCE_CRS,
        source_crs=entry["crs"],
        source_offset=tuple(entry["source_offset"]),
        height_offset=entry["height_offset"],
    )
    for local, (lon, lat, height) in zip(PLACEMENT["vertices"], entry["geodetic"], strict=True):
        reading = site.read_point(placement, tuple(local))
        assert reading.longitude == pytest.approx(lon, abs=1e-9)
        assert reading.latitude == pytest.approx(lat, abs=1e-9)
        assert reading.height == pytest.approx(height, abs=1e-9)


# --- geo formats against GDAL 3.12.0 (scripts/validation/geodesy/run_formats.sh) -----------

FORMATS = DATA / "formats"
GDAL = json.loads((FORMATS / "gdal_sources.json").read_text())
READBACK = json.loads((FORMATS / "gdal_readback.json").read_text())
FORMAT_OFFSET = (502000.0, 6961000.0, 0.0)


def _gdal_vertices(entry: dict) -> np.ndarray:
    """GDAL's vertices, points first, then lines, then polygons (by the ``kind`` field)."""
    order = {"point": 0, "line": 1, "polygon": 2}
    features = sorted(entry["features"], key=lambda f: (order[f["kind"]], f["i"]))
    return np.array([p for feature in features for p in feature["xyz"]])


def _vertices(data) -> np.ndarray:
    """GeoTools' vertices in GDAL's feature order: points, then the line, then the polygon."""
    from geotools.core.vector.model import Kind

    return np.concatenate(
        [data.geometries[k].coords for k in (Kind.POINTS, Kind.LINES, Kind.POLYGONS)]
    )


def _project_crs():
    from geotools.core.crs_match import project_crs

    return project_crs(
        Placement(PlacementMode.SOURCE_CRS, source_crs="EPSG:7856", source_offset=FORMAT_OFFSET)
    )


def _read_vector(name: str):
    from geotools.core.vector import geojson, gpkg

    path = FORMATS / name
    return geojson.read(path) if name.endswith(".geojson") else gpkg.read_layer(path, "features")


@pytest.mark.parametrize(
    ("name", "epsg"),
    [
        ("rfc7946.geojson", "EPSG:4326"),  # RFC 7946: no crs member, WGS 84 lon/lat
        ("legacy_epsg4326.geojson", "EPSG:4326"),  # a URN naming EPSG:4326 (lat/lon axes)
        ("wgs84.gpkg", "EPSG:4326"),  # GeoPackage: x is longitude whatever the axes
        ("mga56.gpkg", "EPSG:7856"),
    ],
)
def test_vector_crs_axis_order_and_reprojection_match_gdal(name: str, epsg: str) -> None:
    """GeoTools reads the same CRS and the same numbers as GDAL, x = longitude or easting,
    and its reprojection into EPSG:7856 (pyproj, PROJ 9.8.1) matches GDAL's (PROJ 9.7.0)
    to 1 micrometre: both use EPSG's null GDA2020 to WGS 84 (2)."""
    from geotools.core.vector.transform import reproject_data

    data = _read_vector(name)
    assert data.crs.epsg == epsg
    gdal = GDAL["vectors"][name]
    assert gdal["read"]["crs"] == epsg
    assert np.array_equal(_vertices(data), _gdal_vertices(gdal["read"]))
    if epsg == "EPSG:4326":
        assert np.allclose(_vertices(data)[0, :2], (153.03, -27.47))  # not swapped
    moved, _report = reproject_data(data, _project_crs())
    np.testing.assert_allclose(_vertices(moved), _gdal_vertices(gdal["to_7856"]), rtol=0, atol=1e-6)


@pytest.mark.parametrize("name", ["geotools.geojson", "geotools.gpkg"])
def test_geotools_exports_read_back_in_gdal(tmp_path: Path, name: str) -> None:
    """GeoTools' import and export pipeline (reprojection, float32 project frame, offset
    back in binary64), written as GeoJSON with the legacy crs member and as GeoPackage:
    GDAL found EPSG:7856 and read exactly these coordinates, and its transformation back
    to WGS 84 lands within 1e-8 degrees (1 mm; float32 at 5 km holds 0.5 mm) of the
    longitudes and latitudes GDAL wrote."""
    from dataclasses import replace

    from geotools.core.vector import geojson, gpkg
    from geotools.core.vector.mesh import from_local, to_local
    from geotools.core.vector.transform import reproject_data

    project = _project_crs()
    moved, _ = reproject_data(_read_vector("rfc7946.geojson"), project)
    exported = replace(
        moved,
        crs=project,
        geometries={
            kind: replace(g, coords=from_local(to_local(g.coords, FORMAT_OFFSET), FORMAT_OFFSET))
            for kind, g in moved.geometries.items()
        },
    )
    if name.endswith(".geojson"):
        geojson.write(tmp_path / name, exported, project)
        back = geojson.read(tmp_path / name)
    else:
        for kind in exported.kinds():
            gpkg.write_layer(tmp_path / name, f"features_{kind.value}", exported, kind, project)
        back = exported
    gdal = READBACK[name]
    assert gdal["read"]["crs"] == "EPSG:7856"
    assert back.crs.epsg == "EPSG:7856"
    assert np.array_equal(_vertices(back), _gdal_vertices(gdal["read"]))
    lon_lat = _gdal_vertices(gdal["to_4326"])
    original = _gdal_vertices(GDAL["vectors"]["rfc7946.geojson"]["read"])
    np.testing.assert_allclose(lon_lat[:, :2], original[:, :2], rtol=0, atol=1e-8)


@pytest.mark.parametrize("raster", [r for r in GDAL["rasters"] if r["file"] != "rotated.tif"])
def test_geotiff_georeference_matches_gdal(raster: dict) -> None:
    """The geotransform (pixel corners, GDAL's convention, PixelIsPoint shifted by half a
    pixel as GDAL does), CRS, nodata and pixel values; and the DEM mesh's vertices are
    GDAL's pixel centres minus the project offset, as float32."""
    from geotools.core import geotiff
    from geotools.core.raster import dem_grid

    info = geotiff.read_info(FORMATS / raster["file"])
    assert info.geotransform == tuple(raster["geotransform"])
    assert info.pixel_is_point == (raster["area_or_point"] == "Point")
    assert info.crs.epsg == raster["crs"]
    assert info.nodata == raster["nodata"]
    band = geotiff.read_band(info)
    assert band.ravel().tolist() == raster["values"]
    gt = raster["geotransform"]
    offset = (gt[0], gt[3], 50.0)
    grid = dem_grid(info, band, offset)
    centres = np.array(raster["pixel_centres"]) - offset[:2]
    kept = grid.positions[:, :2].astype(np.float64)
    expected = centres.astype(np.float32).astype(np.float64)
    # Every mesh vertex is one of GDAL's pixel centres. The nodata pixel (row 1) is
    # dropped, and so is the edge vertex above it, whose every face touches it.
    gaps = np.abs(kept[:, None, :] - expected[None, :, :]).max(axis=2).min(axis=1)
    assert gaps.max() == 0.0
    nodata = expected[raster["values"].index(raster["nodata"])]
    assert not (kept == nodata).all(axis=1).any()
    assert len(kept) == len(centres) - 2


def test_rotated_geotiff_is_refused_not_misplaced() -> None:
    from geotools.core import geotiff

    with pytest.raises(geotiff.GeoTiffError, match="rotated or sheared"):
        geotiff.read_info(FORMATS / "rotated.tif")


@pytest.mark.parametrize("name", ["mga56-central", "utm33n", "lambert93", "albers"])
def test_geoscene_latitude_longitude_sync(name: str) -> None:
    """BlenderGIS's ``latitude``/``longitude`` keys for the ``crs x``/``crs y`` origin, as
    GeoTools writes them, against the reference (Krüger or Newton) to 1e-9 degrees. Every
    datum here reaches WGS 84 by a null transformation, so the numbers carry over."""
    from geotools.core import geoscene

    entry = SITES[name]
    keys = {"SRID": entry["crs"], "crs x": entry["origin"][0], "crs y": entry["origin"][1]}
    assert geoscene.sync_lat_lon(keys)
    assert keys["longitude"] == pytest.approx(entry["points"][0]["lon"], abs=1e-9)
    assert keys["latitude"] == pytest.approx(entry["points"][0]["lat"], abs=1e-9)
    # The frame: E = crs x + x exactly, as a SOURCE_CRS project with that offset.
    store: dict = {}
    east = float(geoscene.get_origin(keys, store, "source_crs", "source_offset_e"))
    assert east == entry["origin"][0]
