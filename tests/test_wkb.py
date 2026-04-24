"""Standalone round-trip tests for gpkg/wkb.py — no Blender required."""
import sys, os

# Run from the repo root so 'gpkg' is importable
sys.path.insert(0, os.path.dirname(__file__))

import gpkg.wkb as wkb

CASES = [
    {'type': 'Point',            'coordinates': [1.0, 2.0]},
    {'type': 'Point',            'coordinates': [1.0, 2.0, 3.0]},         # PointZ
    {'type': 'LineString',       'coordinates': [[0.0, 0.0], [1.0, 1.0], [2.0, 0.0]]},
    {'type': 'Polygon',          'coordinates': [[[0., 0.], [1., 0.], [1., 1.], [0., 1.], [0., 0.]]]},
    {'type': 'MultiPoint',       'coordinates': [[0., 0.], [1., 1.]]},
    {'type': 'MultiLineString',  'coordinates': [[[0., 0.], [1., 1.]], [[2., 2.], [3., 3.]]]},
    {'type': 'MultiPolygon',     'coordinates': [[[[0., 0.], [1., 0.], [1., 1.], [0., 1.], [0., 0.]]]]},
    {
        'type': 'GeometryCollection',
        'geometries': [
            {'type': 'Point',      'coordinates': [0., 0.]},
            {'type': 'LineString', 'coordinates': [[0., 0.], [1., 1.]]},
        ],
    },
    # PolygonZ with hole
    {
        'type': 'Polygon',
        'coordinates': [
            [[0., 0., 10.], [1., 0., 10.], [1., 1., 10.], [0., 1., 10.], [0., 0., 10.]],
            [[0.2, 0.2, 10.], [0.8, 0.2, 10.], [0.8, 0.8, 10.], [0.2, 0.8, 10.], [0.2, 0.2, 10.]],
        ],
    },
]

failures = 0
for geom in CASES:
    blob = wkb.encode(geom, srid=4326)
    result = wkb.parse(blob)
    tag = geom['type']
    if result != geom:
        print(f'FAIL  {tag}')
        print(f'      expected : {geom}')
        print(f'      got      : {result}')
        failures += 1
    else:
        print(f'OK    {tag}')

# Empty / null geometry
empty_blob = wkb.encode(None, srid=4326)
assert wkb.parse(empty_blob) is None, 'empty geometry should parse to None'
print('OK    None/empty geometry')

# Plain WKB fallback (no GPKG header)
import struct
plain_wkb = struct.pack('<BI', 1, 1) + struct.pack('<dd', 7.0, 8.0)
pt = wkb.parse(plain_wkb)
assert pt == {'type': 'Point', 'coordinates': [7.0, 8.0]}, f'plain WKB failed: {pt}'
print('OK    plain WKB fallback')

print()
if failures == 0:
    print('All tests passed.')
else:
    print(f'{failures} test(s) FAILED.')
    sys.exit(1)
