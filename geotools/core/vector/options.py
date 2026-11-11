"""Import options of a linked vector source (``GeoToolsLink.options``, as JSON).

They are stored so Refresh re-reads the file exactly as it was imported:

- ``kind``: which geometry kind this object holds (``points``, ``lines``,
  ``polygons``);
- ``where``: the GeoPackage attribute filter, an SQL ``WHERE`` clause;
- ``site_bbox``: ``[min_e, min_n, max_e, max_n]`` in the project CRS (the
  data's coordinates after any reprojection) for "within the site extent", or
  None;
- ``columns``: Blender attribute name -> source column name, for the columns
  imported or written back (they differ only when a source name is reserved
  in Blender, e.g. ``position`` becomes ``position_src``);
- ``allow_grid_downloads``: whether PROJ may download a transformation grid
  when the data is reprojected (off by default, so imports stay offline);
- ``reprojection``: the last reprojection report
  (``crs_match.report_to_dict``), or None when the data is in the project CRS.
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
    allow_grid_downloads: bool = False
    reprojection: dict[str, object] | None = None

    def to_text(self) -> str:
        return json.dumps(
            {
                "kind": self.kind.value,
                "where": self.where,
                "site_bbox": list(self.site_bbox) if self.site_bbox else None,
                "columns": self.columns,
                "allow_grid_downloads": self.allow_grid_downloads,
                "reprojection": self.reprojection,
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
        reprojection = raw.get("reprojection")
        return cls(
            kind=kind,
            where=str(raw.get("where") or ""),
            site_bbox=site_bbox,
            columns={str(k): str(v) for k, v in columns.items()}
            if isinstance(columns, dict)
            else {},
            allow_grid_downloads=bool(raw.get("allow_grid_downloads", False)),
            reprojection=reprojection if isinstance(reprojection, dict) else None,
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
