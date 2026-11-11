"""Import options of a linked vector source (``GeoToolsLink.options``, as JSON).

They are stored so Refresh re-reads the file exactly as it was imported:

- ``kind``: which geometry kind this object holds (``points``, ``lines``,
  ``polygons``);
- ``where``: the GeoPackage attribute filter, an SQL ``WHERE`` clause;
- ``site_bbox``: ``[min_e, min_n, max_e, max_n]`` in the data's coordinates
  for "within the site extent", or None;
- ``columns``: Blender attribute name -> source column name, for the columns
  imported or written back (they differ only when a source name is reserved
  in Blender, e.g. ``position`` becomes ``position_src``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np

from .model import Kind, VectorData


@dataclass(slots=True)
class VectorOptions:
    kind: Kind = Kind.POLYGONS
    where: str = ""
    site_bbox: tuple[float, float, float, float] | None = None
    columns: dict[str, str] = field(default_factory=dict)

    def to_text(self) -> str:
        return json.dumps(
            {
                "kind": self.kind.value,
                "where": self.where,
                "site_bbox": list(self.site_bbox) if self.site_bbox else None,
                "columns": self.columns,
            },
            sort_keys=True,
        )

    @classmethod
    def from_text(cls, text: str) -> VectorOptions:
        """Options from JSON; defaults for anything missing or invalid."""
        try:
            raw = json.loads(text) if text and text.strip() else {}
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        try:
            kind = Kind(raw.get("kind", Kind.POLYGONS.value))
        except ValueError:
            kind = Kind.POLYGONS
        bbox = raw.get("site_bbox")
        site_bbox = None
        if isinstance(bbox, list) and len(bbox) == 4:
            try:
                site_bbox = tuple(float(v) for v in bbox)  # type: ignore[assignment]
            except (TypeError, ValueError):
                site_bbox = None
        columns = raw.get("columns")
        return cls(
            kind=kind,
            where=str(raw.get("where") or ""),
            site_bbox=site_bbox,
            columns={str(k): str(v) for k, v in columns.items()}
            if isinstance(columns, dict)
            else {},
        )


def within_bbox(data: VectorData, bbox: tuple[float, float, float, float]) -> np.ndarray:
    """One bool per row: whether any of its geometry's bounds overlaps ``bbox``."""
    keep = np.zeros(data.n_rows, dtype=bool)
    min_x, min_y, max_x, max_y = bbox
    for geoms in data.geometries.values():
        b = geoms.feature_bounds()
        overlap = (b[:, 2] >= min_x) & (b[:, 0] <= max_x) & (b[:, 3] >= min_y) & (b[:, 1] <= max_y)
        keep[geoms.rows[overlap]] = True
    return keep
