"""GeoJSON and GeoPackage reading and writing (core.vector), on synthetic fixtures."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "blender"))
import _vector_fixtures as vf

from geotools.core.crs_match import SOURCE_RFC7946, DataCrs
from geotools.core.vector import ColumnType, Kind, geojson, gpkg, wkb
from geotools.core.vector.model import GeometryBuilder, segment_indices
from geotools.core.vector.options import VectorOptions, within_bbox

E0, N0 = vf.E0, vf.N0

GEOMETRIES = {
    "point2": {"type": "Point", "coordinates": [E0 + 1.5, N0 + 2.25]},
    "point3": {"type": "Point", "coordinates": [E0 + 1.5, N0 + 2.25, 31.125]},
    "multipoint3": {"type": "MultiPoint", "coordinates": [[E0, N0, 1.0], [E0 + 1, N0 + 1, 2.0]]},
    "line2": {"type": "LineString", "coordinates": [[E0, N0], [E0 + 3, N0], [E0 + 3, N0 + 4]]},
    "multiline3": {
        "type": "MultiLineString",
        "coordinates": [[[E0, N0, 1.0], [E0 + 1, N0, 2.0]], [[E0, N0 + 5, 3.0], [E0, N0 + 9, 4.0]]],
    },
    "polygon_hole": {
        "type": "Polygon",
        "coordinates": [vf.square(E0, N0, 10.0), vf.square(E0 + 2, N0 + 2, 3.0)[::-1]],
    },
    "multipolygon3": {
        "type": "MultiPolygon",
        "coordinates": [[vf.square(E0, N0, 2.0, 5.0)], [vf.square(E0 + 5, N0, 2.0, 6.0)]],
    },
}


def _expected_counts(geom: dict) -> dict[Kind, tuple[int, int, int]]:
    """parts, rings and coordinates per kind."""
    t, c = geom["type"], geom["coordinates"]
    if t == "Point":
        return {Kind.POINTS: (1, 1, 1)}
    if t == "MultiPoint":
        return {Kind.POINTS: (len(c), len(c), len(c))}
    if t == "LineString":
        return {Kind.LINES: (1, 1, len(c))}
    if t == "MultiLineString":
        return {Kind.LINES: (len(c), len(c), sum(map(len, c)))}
    if t == "Polygon":
        return {Kind.POLYGONS: (1, len(c), sum(map(len, c)))}
    return {Kind.POLYGONS: (len(c), sum(map(len, c)), sum(len(r) for p in c for r in p))}


def _flat(geom: dict) -> np.ndarray:
    pts = list(vf._positions(geom))
    out = np.full((len(pts), 3), np.nan)
    for i, p in enumerate(pts):
        out[i, : len(p)] = p
    return out


@pytest.mark.parametrize("name", sorted(GEOMETRIES))
def test_geojson_reads_each_geometry_type(tmp_path: Path, name: str) -> None:
    geom = GEOMETRIES[name]
    path = vf.write_geojson(tmp_path / "a.geojson", [vf.feature(geom, {"n": 1}, 7)])
    data = geojson.read(path)
    assert data.fid_field == "id" and data.fids.tolist() == [7]
    ((kind, (parts, rings, coords)),) = _expected_counts(geom).items()
    assert data.kinds() == [kind]
    g = data.geometries[kind]
    assert (g.n_features, g.n_parts, g.n_rings, len(g.coords)) == (1, parts, rings, coords)
    np.testing.assert_array_equal(g.coords, _flat(geom))
    assert g.has_z == name.endswith("3")


@pytest.mark.parametrize("name", sorted(GEOMETRIES))
@pytest.mark.parametrize("big_endian", [False, True])
def test_gpkg_reads_each_geometry_type(tmp_path: Path, name: str, big_endian: bool) -> None:
    geom = GEOMETRIES[name]
    path = vf.write_gpkg(
        tmp_path / "a.gpkg", "t", [(5, geom, {"n": 1})], {"n": "INTEGER"}, big_endian=big_endian
    )
    data = gpkg.read_layer(path, "t")
    assert data.fids.tolist() == [5] and data.fid_field == "fid"
    ((kind, (parts, rings, coords)),) = _expected_counts(geom).items()
    g = data.geometries[kind]
    assert (g.n_features, g.n_parts, g.n_rings, len(g.coords)) == (1, parts, rings, coords)
    np.testing.assert_array_equal(g.coords, _flat(geom))


@pytest.mark.parametrize("envelope", [0, 1, 2])
def test_gpkg_header_envelopes(tmp_path: Path, envelope: int) -> None:
    geom = GEOMETRIES["point3"]
    blob = vf.gpkg_geometry(geom, 7856, envelope)
    offset, empty, srs = wkb.gpkg_payload(blob)
    assert offset == 8 + (0, 32, 48)[envelope] and not empty and srs == 7856
    builder = GeometryBuilder()
    wkb.read_gpkg(builder, 0, blob)
    np.testing.assert_array_equal(builder.finish()[Kind.POINTS].coords, _flat(geom))


def test_gpkg_empty_null_and_plain_wkb(tmp_path: Path) -> None:
    blob = vf.gpkg_geometry(None, 7856)
    assert wkb.gpkg_payload(blob)[1] is True
    builder = GeometryBuilder()
    wkb.read_gpkg(builder, 0, blob)
    wkb.read_gpkg(builder, 1, vf.wkb(GEOMETRIES["line2"]))  # no GP header
    out = builder.finish()
    assert list(out) == [Kind.LINES] and out[Kind.LINES].rows.tolist() == [1]
    path = vf.write_gpkg(
        tmp_path / "a.gpkg",
        "t",
        [(1, None, {}), (2, GEOMETRIES["point2"], {})],
        {},
    )
    data = gpkg.read_layer(path, "t")
    assert data.fids.tolist() == [1, 2]
    assert data.geometries[Kind.POINTS].rows.tolist() == [1]


def test_wkb_ewkb_and_m_values() -> None:
    import struct

    # EWKB PointZ with an SRID, and an ISO PointM (M dropped).
    ewkb = struct.pack("<BIIddd", 1, 0x80000001 | 0x20000000, 7856, 1.0, 2.0, 3.0)
    iso_m = struct.pack("<BIddd", 1, 2001, 4.0, 5.0, 99.0)
    iso_zm = struct.pack("<BIdddd", 1, 3001, 6.0, 7.0, 8.0, 99.0)
    builder = GeometryBuilder()
    for row, blob in enumerate((ewkb, iso_m, iso_zm)):
        wkb.read_gpkg(builder, row, blob)
    coords = builder.finish()[Kind.POINTS].coords
    np.testing.assert_array_equal(coords[[0, 2]], [[1, 2, 3], [6, 7, 8]])
    assert coords[1, 0] == 4 and coords[1, 1] == 5 and np.isnan(coords[1, 2])
    with pytest.raises(wkb.WkbError):
        wkb.read_gpkg(GeometryBuilder(), 0, struct.pack("<BI", 1, 8) + b"\0" * 8)  # curve


def test_geometry_collection_is_split_by_kind(tmp_path: Path) -> None:
    collection = {
        "type": "GeometryCollection",
        "geometries": [GEOMETRIES["point2"], GEOMETRIES["line2"], GEOMETRIES["polygon_hole"]],
    }
    features = [
        vf.feature(collection, {"name": "mixed"}, 1),
        vf.feature(GEOMETRIES["point2"], {"name": "p"}, 2),
    ]
    for data in (
        geojson.read(vf.write_geojson(tmp_path / "a.geojson", features)),
        gpkg.read_layer(
            vf.write_gpkg(
                tmp_path / "a.gpkg",
                "t",
                [(f["id"], f["geometry"], f["properties"]) for f in features],
                {"name": "TEXT"},
            ),
            "t",
        ),
    ):
        assert data.kinds() == [Kind.POLYGONS, Kind.LINES, Kind.POINTS]
        assert data.geometries[Kind.POINTS].rows.tolist() == [0, 1]
        assert data.geometries[Kind.LINES].rows.tolist() == [0]
        assert data.columns_for(Kind.POINTS)["name"].values == ["mixed", "p"]
        assert data.fids_for(Kind.LINES).tolist() == [1]


def test_geojson_feature_bare_geometry_and_fids(tmp_path: Path) -> None:
    bare = tmp_path / "bare.geojson"
    bare.write_text(json.dumps(GEOMETRIES["line2"]))
    data = geojson.read(bare)
    assert data.fid_field == geojson.INDEX_FID and data.fids.tolist() == [0]
    single = tmp_path / "single.geojson"
    single.write_text(json.dumps(vf.feature(GEOMETRIES["point2"], {"a": 1}, 3)))
    assert geojson.read(single).fids.tolist() == [3]
    # Ids that are strings or repeated fall back to positions.
    dup = vf.write_geojson(
        tmp_path / "dup.geojson",
        [vf.feature(GEOMETRIES["point2"], {}, "a"), vf.feature(GEOMETRIES["point2"], {}, "b")],
    )
    assert geojson.read(dup).fids.tolist() == [0, 1]
    with pytest.raises(geojson.GeoJsonError):
        bad = tmp_path / "bad.geojson"
        bad.write_text('{"type": "Topology"}')
        geojson.read(bad)


def test_geojson_crs() -> None:
    assert geojson.crs_of({"type": "FeatureCollection"}) == DataCrs(
        "EPSG:4326", None, SOURCE_RFC7946
    )
    legacy = {"crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::7856"}}}
    assert geojson.crs_of(legacy).epsg == "EPSG:7856"
    odd = {"crs": {"type": "name", "properties": {"name": "my site grid"}}}
    assert geojson.crs_of(odd) == DataCrs(None, "my site grid", "geojson-crs-member")


def test_attribute_types(tmp_path: Path) -> None:
    point = GEOMETRIES["point2"]
    props = [
        {"i": 1, "f": 1.5, "b": True, "s": "a", "mix": 1, "big": 2**40, "obj": {"k": 1}, "n": None},
        {"i": None, "f": 2, "b": False, "s": None, "mix": "x", "big": 1, "obj": [1, 2], "n": None},
    ]
    data = geojson.read(
        vf.write_geojson(tmp_path / "a.geojson", [vf.feature(point, p) for p in props])
    )
    c = data.columns
    assert c["i"].type is ColumnType.INT and c["i"].null.tolist() == [False, True]
    assert c["f"].type is ColumnType.FLOAT and c["f"].values.tolist() == [1.5, 2.0]
    assert c["b"].type is ColumnType.BOOL and c["b"].values.tolist() == [True, False]
    assert c["s"].type is ColumnType.STRING and c["s"].values == ["a", ""]
    assert c["mix"].type is ColumnType.STRING and c["mix"].values == ["1", "x"]
    assert c["big"].type is ColumnType.FLOAT  # beyond Blender's int32
    assert c["obj"].values == ['{"k": 1}', "[1, 2]"]
    assert "n" not in c and data.skipped_columns == ["n"]

    gp = vf.write_gpkg(
        tmp_path / "a.gpkg",
        "t",
        [(1, point, {"i": 3, "r": 1, "b": 1, "t": "x", "d": "2026-01-01", "blob": b"\x00"})],
        {
            "i": "MEDIUMINT",
            "r": "REAL",
            "b": "BOOLEAN",
            "t": "TEXT(20)",
            "d": "DATE",
            "blob": "BLOB",
        },
    )
    c = gpkg.read_layer(gp, "t").columns
    assert {n: col.type for n, col in c.items()} == {
        "i": ColumnType.INT,
        "r": ColumnType.FLOAT,
        "b": ColumnType.BOOL,
        "t": ColumnType.STRING,
        "d": ColumnType.STRING,
    }
    assert c["r"].values.dtype == np.float64 and c["i"].source_type == "MEDIUMINT"


def test_gpkg_layers_and_crs(tmp_path: Path) -> None:
    path = vf.write_gpkg(tmp_path / "a.gpkg", "walls", [(1, GEOMETRIES["line2"], {})], {})
    (layer,) = gpkg.list_layers(path)
    assert (layer.name, layer.srs_id, layer.feature_count) == ("walls", 7856, 1)
    data = gpkg.read_layer(path, "walls")
    assert data.crs.epsg == "EPSG:7856" and data.crs.source == "gpkg-srs"
    undefined = vf.write_gpkg(
        tmp_path / "b.gpkg",
        "t",
        [(1, GEOMETRIES["point2"], {})],
        {},
        srs_id=-1,
        organization="NONE",
        definition="undefined",
    )
    assert not gpkg.read_layer(undefined, "t").crs.known
    with pytest.raises(gpkg.GpkgError):
        gpkg.read_layer(path, "nope")
    plain = tmp_path / "plain.sqlite"
    sqlite3.connect(plain).close()
    with pytest.raises(gpkg.GpkgError, match="not a GeoPackage"):
        gpkg.list_layers(plain)


def test_where_filter(tmp_path: Path) -> None:
    features = [(f["id"], f["geometry"], f["properties"]) for f in vf.contexts(3)]
    path = vf.write_gpkg(
        tmp_path / "a.gpkg",
        "contexts",
        features,
        {"context": "TEXT", "depth": "REAL", "phase": "INTEGER", "sealed": "BOOLEAN"},
    )
    data = gpkg.read_layer(path, "contexts", "phase = 1 AND depth > 0.5")
    assert data.fids.tolist() == [4, 7]
    assert gpkg.validate_where(path, "contexts", "context LIKE 'C00%'") is None
    assert gpkg.validate_where(path, "contexts", "") is None
    for bad in (
        "phase = 1; DROP TABLE contexts",
        "phase = 1 -- comment",
        "1) UNION SELECT * FROM gpkg_contents WHERE (1",
        "fid IN (SELECT srs_id FROM gpkg_spatial_ref_sys)",
        "no_such_column = 1",
        "phase = 'unclosed",
    ):
        assert gpkg.validate_where(path, "contexts", bad), bad
        with pytest.raises(gpkg.GpkgError):
            gpkg.read_layer(path, "contexts", bad)
    # Quoted text may hold what is refused outside quotes.
    assert gpkg.validate_where(path, "contexts", "context <> 'a;b--c(('") is None
    # The table is untouched.
    assert len(gpkg.read_layer(path, "contexts").fids) == 9


@pytest.mark.parametrize("name", sorted(GEOMETRIES))
def test_gpkg_write_round_trip(tmp_path: Path, name: str) -> None:
    geom = GEOMETRIES[name]
    src = vf.write_gpkg(
        tmp_path / "a.gpkg",
        "t",
        [(3, geom, {"label": "x", "v": 1.25, "k": 4, "ok": 1})],
        {"label": "TEXT", "v": "REAL", "k": "INTEGER", "ok": "BOOLEAN"},
    )
    data = gpkg.read_layer(src, "t")
    (kind,) = data.kinds()
    out = tmp_path / "out.gpkg"
    assert gpkg.write_layer(out, "copy", data, kind, data.crs) == 1
    back = gpkg.read_layer(out, "copy")
    a, b = data.geometries[kind], back.geometries[kind]
    if a.has_z:
        np.testing.assert_array_equal(a.coords, b.coords)
    else:
        np.testing.assert_array_equal(a.coords[:, :2], b.coords[:, :2])
        assert np.isnan(b.coords[:, 2]).all()
    for offsets in ("ring_offsets", "part_offsets", "feature_offsets"):
        np.testing.assert_array_equal(getattr(a, offsets), getattr(b, offsets))
    assert back.fids.tolist() == [3] and back.crs.epsg == "EPSG:7856"
    assert {n: c.python_value(0) for n, c in back.columns.items()} == {
        "label": "x",
        "v": 1.25,
        "k": 4,
        "ok": True,
    }
    (row,) = vf.rows(out, "SELECT geometry_type_name, z FROM gpkg_geometry_columns")
    multi = geom["type"].startswith("Multi") or geom["type"] == "MultiPoint"
    assert row[0].startswith("MULTI") == multi and row[1] == int(a.has_z)
    with pytest.raises(gpkg.GpkgError, match="already has"):
        gpkg.write_layer(out, "copy", data, kind, data.crs)
    gpkg.write_layer(out, "copy", data, kind, data.crs, overwrite=True)


def test_gpkg_writes_xyz_envelope_and_spatial_ref(tmp_path: Path) -> None:
    data = geojson.read(
        vf.write_geojson(tmp_path / "a.geojson", vf.contexts(2, z=40.0, holes=False))
    )
    out = tmp_path / "out.gpkg"
    gpkg.write_layer(out, "c", data, Kind.POLYGONS, DataCrs("EPSG:7856", "+proj=utm", "project"))
    (blob,) = vf.rows(out, "SELECT geom FROM c WHERE fid = 1")[0]
    assert blob[3] == 0x01 | (2 << 1)  # little-endian, XYZ envelope
    env = np.frombuffer(blob, "<f8", 6, 8)
    np.testing.assert_array_equal(env, [E0, E0 + 10, N0, N0 + 10, 40, 40])
    srs = vf.rows(out, "SELECT organization, organization_coordsys_id FROM gpkg_spatial_ref_sys")
    assert ("EPSG", 7856) in srs and ("EPSG", 4326) in srs
    assert vf.rows(out, "PRAGMA application_id")[0][0] == gpkg.APPLICATION_ID


def _contexts(tmp_path: Path):
    return geojson.read(
        vf.write_geojson(tmp_path / "a.geojson", vf.contexts(1, z=40.0, holes=False))
    )


def test_gpkg_srs_definition_is_wkt1(tmp_path: Path) -> None:
    """The standard asks for WKT in ``definition``: with pyproj it is the CRS's WKT1."""
    pyproj = pytest.importorskip("pyproj")
    out = tmp_path / "out.gpkg"
    project = DataCrs("EPSG:7856", "EPSG:7856", "project")
    gpkg.write_layer(out, "c", _contexts(tmp_path), Kind.POLYGONS, project)
    ((definition,),) = vf.rows(
        out, "SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id = 7856"
    )
    assert definition == pyproj.CRS("EPSG:7856").to_wkt("WKT1_GDAL")  # was "EPSG:7856"
    assert definition.startswith('PROJCS["GDA2020 / MGA zone 56"')
    assert pyproj.CRS(definition).to_epsg() == 7856
    # WKT1 holds it, so no CRS WKT extension.
    columns = {r[1] for r in vf.rows(out, "PRAGMA table_info(gpkg_spatial_ref_sys)")}
    assert "definition_12_063" not in columns
    assert not vf.rows(out, "SELECT 1 FROM sqlite_master WHERE name = 'gpkg_extensions'")
    assert gpkg.read_layer(out, "c").crs.epsg == "EPSG:7856"


def test_gpkg_srs_definition_falls_back_to_wkt2(tmp_path: Path) -> None:
    """A CRS WKT1 cannot express (3D geographic) goes in the CRS WKT extension as WKT2."""
    pyproj = pytest.importorskip("pyproj")
    out = tmp_path / "out.gpkg"
    gpkg.write_layer(
        out, "c", _contexts(tmp_path), Kind.POLYGONS, DataCrs("EPSG:9989", None, "user")
    )
    ((definition, wkt2, epoch),) = vf.rows(
        out,
        "SELECT definition, definition_12_063, epoch FROM gpkg_spatial_ref_sys WHERE srs_id = 9989",
    )
    assert definition == "undefined" and epoch is None
    assert wkt2 == pyproj.CRS("EPSG:9989").to_wkt("WKT2_2019")
    # The column is NOT NULL with no default (Req 10), so the table was rebuilt,
    # and the rows already there have their WKT2 (or "undefined").
    column = next(
        r
        for r in vf.rows(out, "PRAGMA table_info(gpkg_spatial_ref_sys)")
        if r[1] == "definition_12_063"
    )
    assert column[3] == 1 and column[4] is None
    rows = dict(vf.rows(out, "SELECT srs_id, definition_12_063 FROM gpkg_spatial_ref_sys"))
    assert rows[4326] == pyproj.CRS("EPSG:4326").to_wkt("WKT2_2019")
    assert rows[0] == rows[-1] == "undefined"
    extensions = vf.rows(
        out, "SELECT table_name, column_name, extension_name, scope FROM gpkg_extensions"
    )
    assert sorted(extensions) == [
        ("gpkg_spatial_ref_sys", "definition_12_063", "gpkg_crs_wkt_1_1", "read-write"),
        ("gpkg_spatial_ref_sys", "epoch", "gpkg_crs_wkt_1_1", "read-write"),
    ]
    # A second layer, in a CRS WKT1 holds, has both.
    gpkg.write_layer(
        out, "d", _contexts(tmp_path), Kind.POLYGONS, DataCrs("EPSG:7856", None, "user")
    )
    ((definition, wkt2),) = vf.rows(
        out, "SELECT definition, definition_12_063 FROM gpkg_spatial_ref_sys WHERE srs_id = 7856"
    )
    assert definition.startswith('PROJCS["GDA2020 / MGA zone 56"')
    assert wkt2.startswith('PROJCRS["GDA2020 / MGA zone 56"')
    assert gpkg.read_layer(out, "c").crs.epsg == "EPSG:9989"
    assert gpkg.read_layer(out, "d").crs.epsg == "EPSG:7856"


def test_gpkg_wkt2_layer_in_a_gdal_geopackage(tmp_path: Path) -> None:
    """Adding the extension to a GeoPackage GDAL wrote keeps GDAL's layer and its CRS."""
    pytest.importorskip("pyproj")
    source = Path(__file__).parents[1] / "data/validation/geodesy/formats/mga56.gpkg"
    out = tmp_path / "mga56.gpkg"
    out.write_bytes(source.read_bytes())
    (gdal_layer,) = [layer.name for layer in gpkg.list_layers(out)]
    before = gpkg.read_layer(out, gdal_layer)
    gpkg.write_layer(
        out, "itrf", _contexts(tmp_path), Kind.POLYGONS, DataCrs("EPSG:9989", None, "user")
    )
    after = gpkg.read_layer(out, gdal_layer)
    assert after.crs.epsg == before.crs.epsg == "EPSG:7856"
    assert after.crs.definition == before.crs.definition  # GDAL's own WKT1 kept
    assert gpkg.read_layer(out, "itrf").crs.epsg == "EPSG:9989"


def test_gpkg_reads_wkt2_of_a_crs_without_a_code(tmp_path: Path) -> None:
    pyproj = pytest.importorskip("pyproj")
    out = tmp_path / "out.gpkg"
    text = pyproj.CRS("EPSG:9989").to_wkt("WKT2_2019").replace(',ID["EPSG",9989]', "")
    gpkg.write_layer(out, "c", _contexts(tmp_path), Kind.POLYGONS, DataCrs(None, text, "user"))
    ((organization, definition, wkt2),) = vf.rows(
        out,
        "SELECT organization, definition, definition_12_063 FROM gpkg_spatial_ref_sys "
        "WHERE srs_id = 100000",
    )
    assert (organization, definition) == ("NONE", "undefined")
    assert wkt2.startswith('GEOGCRS["ITRF2020"')
    crs = gpkg.read_layer(out, "c").crs
    assert crs.definition.startswith("GEOGCRS[")  # from definition_12_063


def test_gpkg_srs_definition_without_pyproj(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without pyproj the definition is the CRS text, as before; readers use the code."""
    from geotools.core import deps

    missing = deps.DependencyStatus(False, error="pyproj is not installed", name="pyproj")
    monkeypatch.setattr(deps, "probe_pyproj", lambda: missing)
    out = tmp_path / "out.gpkg"
    project = DataCrs("EPSG:7856", "EPSG:7856", "project")
    gpkg.write_layer(out, "c", _contexts(tmp_path), Kind.POLYGONS, project)
    assert vf.rows(out, "SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id = 7856") == [
        ("EPSG:7856",)
    ]
    gpkg.write_layer(out, "d", _contexts(tmp_path), Kind.POLYGONS, DataCrs("EPSG:7855", None, "u"))
    assert vf.rows(out, "SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id = 7855") == [
        ("undefined",)
    ]


def test_geojson_write_round_trip(tmp_path: Path) -> None:
    features = [vf.feature(g, {"name": n}, i) for i, (n, g) in enumerate(GEOMETRIES.items())]
    src = geojson.read(vf.write_geojson(tmp_path / "a.geojson", features))
    out = tmp_path / "out.geojson"
    geojson.write(out, src, src.crs)
    raw = json.loads(out.read_text())
    assert raw["crs"]["properties"]["name"] == "urn:ogc:def:crs:EPSG::7856"
    back = geojson.read(out)
    assert back.fids.tolist() == src.fids.tolist()
    for kind in src.kinds():
        a, b = src.geometries[kind], back.geometries[kind]
        np.testing.assert_array_equal(a.ring_offsets, b.ring_offsets)
        np.testing.assert_array_equal(a.coords[:, :2], b.coords[:, :2])
    assert back.columns["name"].values == list(GEOMETRIES)
    wgs = geojson.to_dict(src, DataCrs("EPSG:4326", None, SOURCE_RFC7946))
    assert "crs" not in wgs


def test_geojson_closes_open_rings_when_writing(tmp_path: Path) -> None:
    builder = GeometryBuilder()
    builder.add_polygon(0, [np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])])
    from geotools.core.vector.model import VectorData

    data = VectorData("t", np.array([1]), {}, builder.finish(), DataCrs(None, None, "user"))
    ring = geojson.to_dict(data, None)["features"][0]["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 4


def test_geojson_rewrite_keeps_unknown_members(tmp_path: Path) -> None:
    features = vf.contexts(2, holes=False)
    for f in features:
        f["custom"] = {"keep": True}
    path = vf.write_geojson(tmp_path / "a.geojson", features, extra={"name": "dig", "x-tool": 1})
    before = json.loads(path.read_text())
    updated, missing = geojson.rewrite_properties(
        path, "id", np.array([3, 1, 99]), {"depth": [9.5, 1.0, 0.0], "note": ["c", None, "x"]}
    )
    assert (updated, missing) == (2, 1)
    after = json.loads(path.read_text())
    assert after["x-tool"] == 1 and after["name"] == "dig" and after["crs"] == before["crs"]
    assert [f["id"] for f in after["features"]] == [f["id"] for f in before["features"]]
    assert after["features"][2]["properties"]["depth"] == 9.5
    assert after["features"][2]["properties"]["note"] == "c"
    assert after["features"][0]["properties"]["note"] is None
    assert after["features"][0]["custom"] == {"keep": True}
    assert after["features"][1] == before["features"][1]
    assert path.read_text().startswith('{\n  "type"')  # indentation kept
    by_index = geojson.rewrite_properties(path, geojson.INDEX_FID, np.array([0]), {"a": [1]})
    assert by_index == (1, 0)


def test_update_attributes(tmp_path: Path) -> None:
    features = [(f["id"], f["geometry"], f["properties"]) for f in vf.contexts(2, holes=False)]
    path = vf.write_gpkg(
        tmp_path / "a.gpkg", "contexts", features, {"context": "TEXT", "depth": "REAL"}
    )
    # A QGIS-style R-tree trigger calling SpatiaLite functions plain SQLite lacks.
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE log (minx REAL, empty INTEGER)")
    con.execute(
        "CREATE TRIGGER t AFTER UPDATE ON contexts BEGIN "
        "INSERT INTO log VALUES (ST_MinX(NEW.geom), ST_IsEmpty(NEW.geom)); END"
    )
    con.commit()
    con.close()
    geom_before = vf.rows(path, "SELECT fid, geom FROM contexts ORDER BY fid")
    updated, missing, added = gpkg.update_attributes(
        path,
        "contexts",
        np.array([2, 4, 77]),
        {
            "depth": (ColumnType.FLOAT, [1.5, None, 0.0]),
            "cut": (ColumnType.BOOL, [True, False, True]),
        },
    )
    assert (updated, missing, added) == (2, 1, ["cut"])
    got = vf.rows(path, "SELECT fid, depth, cut FROM contexts ORDER BY fid")
    assert got == [(1, 0.25, None), (2, 1.5, 1), (3, 0.75, None), (4, None, 0)]
    assert vf.rows(path, "SELECT fid, geom FROM contexts ORDER BY fid") == geom_before
    assert vf.rows(path, "SELECT minx, empty FROM log") == [(E0, 0), (E0 + 20, 0)]
    with pytest.raises(gpkg.GpkgError):
        gpkg.update_attributes(path, "contexts", np.array([1]), {"geom": (ColumnType.INT, [1])})


def test_update_attributes_rolls_back(tmp_path: Path) -> None:
    path = vf.write_gpkg(
        tmp_path / "a.gpkg", "t", [(1, GEOMETRIES["point2"], {"v": 1.0})], {"v": "REAL"}
    )
    con = sqlite3.connect(path)
    con.execute("CREATE TRIGGER fail BEFORE UPDATE ON t BEGIN SELECT RAISE(ABORT, 'no'); END")
    con.commit()
    con.close()
    with pytest.raises(sqlite3.Error):
        gpkg.update_attributes(path, "t", np.array([1]), {"w": (ColumnType.INT, [5])})
    # The ADD COLUMN was rolled back with the failed UPDATE.
    assert [r[1] for r in vf.rows(path, "PRAGMA table_info(t)")] == ["fid", "geom", "v"]


def test_options_and_site_filter(tmp_path: Path) -> None:
    opts = VectorOptions(Kind.LINES, "phase = 1", (1.0, 2.0, 3.0, 4.0), {"a_": "a"})
    assert VectorOptions.from_text(opts.to_text()) == opts
    assert VectorOptions.from_text("") == VectorOptions()
    assert VectorOptions.from_text('{"kind": "nope"}').kind is Kind.POLYGONS
    data = geojson.read(vf.write_geojson(tmp_path / "a.geojson", vf.contexts(3)))
    keep = within_bbox(data, (E0 - 1, N0 - 1, E0 + 25, N0 + 5))
    assert data.fids[keep].tolist() == [1, 4]
    sub = data.select_rows(keep)
    assert sub.fids.tolist() == [1, 4]
    assert sub.geometries[Kind.POLYGONS].rows.tolist() == [0, 1]
    assert sub.columns["context"].values == ["C001", "C004"]
    np.testing.assert_array_equal(
        sub.geometries[Kind.POLYGONS].coords,
        data.geometries[Kind.POLYGONS].select(keep).coords,
    )


def test_segment_indices() -> None:
    assert segment_indices(np.array([5, 0, 10]), np.array([3, 0, 2])).tolist() == [
        5,
        6,
        7,
        10,
        11,
    ]
    assert segment_indices(np.array([], dtype=np.int64), np.array([], dtype=np.int64)).size == 0
