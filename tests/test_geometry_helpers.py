import importlib.util
import pathlib
import sys
from unittest.mock import MagicMock


def _load_geometry_module():
    sys.modules.setdefault("bpy", MagicMock())
    sys.modules.setdefault("bmesh", MagicMock())

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "addons"
        / "data"
        / "geojson"
        / "geometry.py"
    )
    spec = importlib.util.spec_from_file_location("test_geometry_module", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sanitize_ring_rejects_two_point_closed_ring():
    geometry = _load_geometry_module()

    ring = [
        [2312771.1361233657, 4638145.749085042],
        [2312771.1366645875, 4638145.751574663],
        [2312771.1361233657, 4638145.749085042],
    ]

    assert geometry._sanitize_ring(ring, ccw=True) == []


def test_sanitize_ring_preserves_valid_triangle():
    geometry = _load_geometry_module()

    ring = [[0.0, 0.0], [1.0, 0.0], [0.0, 1.0], [0.0, 0.0]]

    assert geometry._sanitize_ring(ring, ccw=True) == [
        [0.0, 0.0],
        [1.0, 0.0],
        [0.0, 1.0],
    ]
