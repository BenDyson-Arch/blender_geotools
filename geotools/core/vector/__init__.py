"""Vector geo formats: GeoJSON and GeoPackage, read into and written from :class:`VectorData`.

See ``docs/design/m8-geo-formats.md`` ("Vectors"). Nothing here imports ``bpy``.
"""

from __future__ import annotations

from .model import Column, ColumnType, Geometries, GeometryBuilder, Kind, VectorData

__all__ = ["Column", "ColumnType", "Geometries", "GeometryBuilder", "Kind", "VectorData"]
