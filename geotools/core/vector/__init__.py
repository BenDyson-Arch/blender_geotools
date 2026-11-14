"""Vector geo formats: GeoJSON and GeoPackage, read into and written from :class:`VectorData`.

Coordinates stay binary64 until they become Blender's float32 positions,
relative to the project frame. No Python loops over vertices: a loop over
features is acceptable where a format forces it (WKB records, polygon
tessellation), but coordinates go into preallocated numpy arrays. Tests use
synthetic fixtures only. Nothing here imports ``bpy``.
"""

from __future__ import annotations

from .model import Column, ColumnType, Geometries, GeometryBuilder, Kind, VectorData

__all__ = ["Column", "ColumnType", "Geometries", "GeometryBuilder", "Kind", "VectorData"]
