"""Tests for gpkg/reader.py and gpkg/writer.py.

Creates a minimal GeoPackage in a temp file for each test, then exercises
the public API (list_layers, read_layer, read_crs, and all write operations).
No Blender dependency — plain Python + sqlite3 + stdlib only.
"""

import os
import sqlite3
import struct
import sys
import tempfile
import unittest

# Make 'addons.data.gpkg' importable when run from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import addons.data.gpkg.wkb as wkb
from addons.data.gpkg.reader import (
    LayerData,
    LayerInfo,
    list_layers,
    read_crs,
    read_layer,
)
from addons.data.gpkg.writer import (
    GPKGWriteError,
    delete_feature,
    insert_feature,
    update_feature_attributes,
    update_feature_geometry,
)

# ---------------------------------------------------------------------------
# Helper: build a minimal GPKG file
# ---------------------------------------------------------------------------

EPSG4326_WKT = (
    'GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563]],'
    'PRIMEM["Greenwich",0],UNIT["degree",0.017453292519943295]]'
)


def _make_gpkg(path: str) -> None:
    """Create a minimal GeoPackage with two feature layers.

    Layers:
      points  — 3 Point features (EPSG:4326)
      polys   — 2 Polygon features (EPSG:4326)
    """
    con = sqlite3.connect(path)
    con.execute("PRAGMA application_id = 0x47503130")  # 'GP10'
    con.execute("PRAGMA user_version = 10200")

    con.executescript("""
        CREATE TABLE gpkg_spatial_ref_sys (
            srs_name                 TEXT    NOT NULL,
            srs_id                   INTEGER NOT NULL PRIMARY KEY,
            organization             TEXT    NOT NULL,
            organization_coordsys_id INTEGER NOT NULL,
            definition               TEXT    NOT NULL,
            description              TEXT
        );
        CREATE TABLE gpkg_contents (
            table_name  TEXT     NOT NULL PRIMARY KEY,
            data_type   TEXT     NOT NULL,
            identifier  TEXT,
            description TEXT     DEFAULT '',
            last_change DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            min_x       REAL,
            min_y       REAL,
            max_x       REAL,
            max_y       REAL,
            srs_id      INTEGER,
            CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id)
        );
        CREATE TABLE gpkg_geometry_columns (
            table_name         TEXT NOT NULL,
            column_name        TEXT NOT NULL,
            geometry_type_name TEXT NOT NULL,
            srs_id             INTEGER NOT NULL,
            z                  TINYINT NOT NULL,
            m                  TINYINT NOT NULL,
            CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name)
        );
    """)

    # CRS: WGS84 (4326) + undefined (0) required by spec
    con.execute("""INSERT INTO gpkg_spatial_ref_sys VALUES
        ('Undefined cartesian SRS', -1, 'NONE', -1, 'undefined', 'undefined')""")
    con.execute("""INSERT INTO gpkg_spatial_ref_sys VALUES
        ('Undefined geographic SRS', 0, 'NONE', 0, 'undefined', 'undefined')""")
    con.execute(
        "INSERT INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
        ("WGS 84", 4326, "EPSG", 4326, EPSG4326_WKT, "WGS84 geographic"),
    )

    # ---- points layer ----
    con.execute("""
        CREATE TABLE points (
            fid     INTEGER PRIMARY KEY AUTOINCREMENT,
            geom    BLOB,
            name    TEXT,
            value   REAL
        )
    """)
    con.execute(
        "INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("points", "features", "points", "", None, -180, -90, 180, 90, 4326),
    )
    con.execute(
        "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
        ("points", "geom", "POINT", 4326, 0, 0),
    )

    points = [
        (
            wkb.encode({"type": "Point", "coordinates": [10.0, 20.0]}, srid=4326),
            "alpha",
            1.1,
        ),
        (
            wkb.encode({"type": "Point", "coordinates": [30.0, 40.0]}, srid=4326),
            "beta",
            2.2,
        ),
        (
            wkb.encode({"type": "Point", "coordinates": [50.0, 60.0]}, srid=4326),
            "gamma",
            3.3,
        ),
    ]
    con.executemany("INSERT INTO points (geom, name, value) VALUES (?,?,?)", points)

    # ---- polys layer ----
    ring = [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]
    ring2 = [[5.0, 5.0], [7.0, 5.0], [7.0, 7.0], [5.0, 7.0], [5.0, 5.0]]
    con.execute("""
        CREATE TABLE polys (
            fid     INTEGER PRIMARY KEY AUTOINCREMENT,
            geom    BLOB,
            label   TEXT
        )
    """)
    con.execute(
        "INSERT INTO gpkg_contents VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("polys", "features", "polys", "", None, 0, 0, 10, 10, 4326),
    )
    con.execute(
        "INSERT INTO gpkg_geometry_columns VALUES (?,?,?,?,?,?)",
        ("polys", "geom", "POLYGON", 4326, 0, 0),
    )

    polys = [
        (wkb.encode({"type": "Polygon", "coordinates": [ring]}, srid=4326), "square1"),
        (wkb.encode({"type": "Polygon", "coordinates": [ring2]}, srid=4326), "square2"),
    ]
    con.executemany("INSERT INTO polys (geom, label) VALUES (?,?)", polys)

    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestListLayers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_returns_two_layers(self):
        layers = list_layers(self.tmp.name)
        self.assertEqual(len(layers), 2)

    def test_layer_names(self):
        names = {l.name for l in list_layers(self.tmp.name)}
        self.assertEqual(names, {"points", "polys"})

    def test_layer_srid(self):
        by_name = {l.name: l for l in list_layers(self.tmp.name)}
        self.assertEqual(by_name["points"].srid, 4326)
        self.assertEqual(by_name["polys"].srid, 4326)

    def test_feature_count(self):
        by_name = {l.name: l for l in list_layers(self.tmp.name)}
        self.assertEqual(by_name["points"].feature_count, 3)
        self.assertEqual(by_name["polys"].feature_count, 2)

    def test_geom_type(self):
        by_name = {l.name: l for l in list_layers(self.tmp.name)}
        self.assertEqual(by_name["points"].geom_type, "POINT")
        self.assertEqual(by_name["polys"].geom_type, "POLYGON")


class TestReadCRS(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_crs_points(self):
        crs = read_crs(self.tmp.name, "points")
        self.assertEqual(crs, "EPSG:4326")

    def test_crs_nonexistent_layer(self):
        crs = read_crs(self.tmp.name, "does_not_exist")
        self.assertIsNone(crs)


class TestReadLayer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_read_all_points(self):
        data = read_layer(self.tmp.name, "points")
        self.assertIsInstance(data, LayerData)
        self.assertEqual(data.name, "points")
        self.assertEqual(data.srid, 4326)
        self.assertEqual(len(data.features), 3)

    def test_feature_structure(self):
        data = read_layer(self.tmp.name, "points")
        feat = data.features[0]
        self.assertIn("fid", feat)
        self.assertIn("geometry", feat)
        self.assertIn("properties", feat)

    def test_geometry_decoded(self):
        data = read_layer(self.tmp.name, "points")
        coords_set = {tuple(f["geometry"]["coordinates"]) for f in data.features}
        self.assertIn((10.0, 20.0), coords_set)
        self.assertIn((30.0, 40.0), coords_set)
        self.assertIn((50.0, 60.0), coords_set)

    def test_properties_decoded(self):
        data = read_layer(self.tmp.name, "points")
        names = {f["properties"]["name"] for f in data.features}
        self.assertEqual(names, {"alpha", "beta", "gamma"})

    def test_fid_is_integer(self):
        data = read_layer(self.tmp.name, "points")
        for f in data.features:
            self.assertIsInstance(f["fid"], int)

    def test_limit(self):
        data = read_layer(self.tmp.name, "points", limit=1)
        self.assertEqual(len(data.features), 1)

    def test_bbox_filter_includes(self):
        # bbox that covers only (10,20)
        data = read_layer(self.tmp.name, "points", bbox=(5.0, 15.0, 20.0, 25.0))
        self.assertEqual(len(data.features), 1)
        self.assertEqual(data.features[0]["geometry"]["coordinates"], [10.0, 20.0])

    def test_bbox_filter_excludes_all(self):
        data = read_layer(self.tmp.name, "points", bbox=(100.0, 100.0, 200.0, 200.0))
        self.assertEqual(len(data.features), 0)

    def test_read_polys(self):
        data = read_layer(self.tmp.name, "polys")
        self.assertEqual(len(data.features), 2)
        for f in data.features:
            self.assertEqual(f["geometry"]["type"], "Polygon")

    def test_geom_column_name(self):
        data = read_layer(self.tmp.name, "points")
        self.assertEqual(data.geom_column, "geom")

    def test_crs_wkt_present(self):
        data = read_layer(self.tmp.name, "points")
        self.assertIsNotNone(data.crs_wkt)
        self.assertIn("WGS", data.crs_wkt)


class TestUpdateGeometry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _fid_of(self, name: str) -> int:
        data = read_layer(self.tmp.name, "points")
        for f in data.features:
            if f["properties"]["name"] == name:
                return f["fid"]
        raise KeyError(name)

    def test_update_geometry(self):
        fid = self._fid_of("alpha")
        new_geom = {"type": "Point", "coordinates": [99.0, 88.0]}
        update_feature_geometry(self.tmp.name, "points", fid, new_geom, srid=4326)

        data = read_layer(self.tmp.name, "points")
        updated = next(f for f in data.features if f["fid"] == fid)
        self.assertEqual(updated["geometry"]["coordinates"], [99.0, 88.0])

    def test_update_nonexistent_fid_raises(self):
        with self.assertRaises(GPKGWriteError):
            update_feature_geometry(
                self.tmp.name,
                "points",
                9999,
                {"type": "Point", "coordinates": [0.0, 0.0]},
            )

    def test_update_to_none(self):
        fid = self._fid_of("beta")
        update_feature_geometry(self.tmp.name, "points", fid, None, srid=4326)
        data = read_layer(self.tmp.name, "points")
        updated = next(f for f in data.features if f["fid"] == fid)
        self.assertIsNone(updated["geometry"])


class TestUpdateAttributes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _fid_of(self, name: str) -> int:
        data = read_layer(self.tmp.name, "points")
        for f in data.features:
            if f["properties"]["name"] == name:
                return f["fid"]
        raise KeyError(name)

    def test_update_attribute(self):
        fid = self._fid_of("alpha")
        update_feature_attributes(self.tmp.name, "points", fid, {"name": "UPDATED"})
        data = read_layer(self.tmp.name, "points")
        updated = next(f for f in data.features if f["fid"] == fid)
        self.assertEqual(updated["properties"]["name"], "UPDATED")

    def test_update_numeric_attribute(self):
        fid = self._fid_of("beta")
        update_feature_attributes(self.tmp.name, "points", fid, {"value": 99.9})
        data = read_layer(self.tmp.name, "points")
        updated = next(f for f in data.features if f["fid"] == fid)
        self.assertAlmostEqual(updated["properties"]["value"], 99.9)

    def test_unknown_columns_silently_ignored(self):
        fid = self._fid_of("gamma")
        # Should not raise even though 'nonexistent_col' is not a real column
        update_feature_attributes(
            self.tmp.name, "points", fid, {"nonexistent_col": "boom", "name": "safe"}
        )
        data = read_layer(self.tmp.name, "points")
        updated = next(f for f in data.features if f["fid"] == fid)
        self.assertEqual(updated["properties"]["name"], "safe")

    def test_update_nonexistent_fid_raises(self):
        with self.assertRaises(GPKGWriteError):
            update_feature_attributes(self.tmp.name, "points", 9999, {"name": "x"})

    def test_empty_properties_noop(self):
        # Should not raise and should not modify any rows
        fid = self._fid_of("alpha")
        update_feature_attributes(self.tmp.name, "points", fid, {})
        # No assertion needed — just verify no exception


class TestInsertFeature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_insert_returns_fid(self):
        new_geom = {"type": "Point", "coordinates": [1.0, 2.0]}
        new_fid = insert_feature(
            self.tmp.name, "points", new_geom, {"name": "new", "value": 5.5}
        )
        self.assertIsInstance(new_fid, int)
        self.assertGreater(new_fid, 0)

    def test_inserted_feature_readable(self):
        new_geom = {"type": "Point", "coordinates": [7.0, 8.0]}
        new_fid = insert_feature(
            self.tmp.name, "points", new_geom, {"name": "inserted"}
        )
        data = read_layer(self.tmp.name, "points")
        fids = [f["fid"] for f in data.features]
        self.assertIn(new_fid, fids)
        inserted = next(f for f in data.features if f["fid"] == new_fid)
        self.assertEqual(inserted["geometry"]["coordinates"], [7.0, 8.0])
        self.assertEqual(inserted["properties"]["name"], "inserted")

    def test_insert_increments_count(self):
        before = len(read_layer(self.tmp.name, "points").features)
        insert_feature(
            self.tmp.name, "points", {"type": "Point", "coordinates": [0.0, 0.0]}, {}
        )
        after = len(read_layer(self.tmp.name, "points").features)
        self.assertEqual(after, before + 1)

    def test_insert_with_none_geometry(self):
        fid = insert_feature(self.tmp.name, "points", None, {"name": "null_geom"})
        data = read_layer(self.tmp.name, "points")
        inserted = next(f for f in data.features if f["fid"] == fid)
        self.assertIsNone(inserted["geometry"])

    def test_insert_updates_rtree_trigger_bounds(self):
        con = sqlite3.connect(self.tmp.name)
        con.execute(
            "CREATE VIRTUAL TABLE rtree_points_geom USING rtree(id, minx, maxx, miny, maxy)"
        )
        con.executescript("""
            CREATE TRIGGER rtree_points_geom_insert
            AFTER INSERT ON points
            WHEN NEW.geom NOT NULL AND NOT ST_IsEmpty(NEW.geom)
            BEGIN
              INSERT OR REPLACE INTO rtree_points_geom
              VALUES (
                NEW.fid,
                ST_MinX(NEW.geom),
                ST_MaxX(NEW.geom),
                ST_MinY(NEW.geom),
                ST_MaxY(NEW.geom)
              );
            END;
        """)
        con.commit()
        con.close()

        new_fid = insert_feature(
            self.tmp.name,
            "points",
            {"type": "Point", "coordinates": [7.25, 8.5]},
            {"name": "rtree_test"},
        )

        con = sqlite3.connect(self.tmp.name)
        row = con.execute(
            "SELECT minx, maxx, miny, maxy FROM rtree_points_geom WHERE id = ?",
            (new_fid,),
        ).fetchone()
        con.close()

        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 7.25, places=6)
        self.assertAlmostEqual(row[1], 7.25, places=6)
        self.assertAlmostEqual(row[2], 8.5, places=6)
        self.assertAlmostEqual(row[3], 8.5, places=6)


class TestDeleteFeature(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".gpkg", delete=False)
        self.tmp.close()
        _make_gpkg(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def _fid_of(self, name: str) -> int:
        data = read_layer(self.tmp.name, "points")
        for f in data.features:
            if f["properties"]["name"] == name:
                return f["fid"]
        raise KeyError(name)

    def test_delete_removes_feature(self):
        fid = self._fid_of("alpha")
        delete_feature(self.tmp.name, "points", fid)
        data = read_layer(self.tmp.name, "points")
        self.assertEqual(len(data.features), 2)
        fids = [f["fid"] for f in data.features]
        self.assertNotIn(fid, fids)

    def test_delete_nonexistent_fid_raises(self):
        with self.assertRaises(GPKGWriteError):
            delete_feature(self.tmp.name, "points", 9999)

    def test_delete_all_then_empty(self):
        data = read_layer(self.tmp.name, "points")
        for f in data.features:
            delete_feature(self.tmp.name, "points", f["fid"])
        data2 = read_layer(self.tmp.name, "points")
        self.assertEqual(len(data2.features), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
