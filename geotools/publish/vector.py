"""Vector publish: features to a GeoPackage, then rusty-tiles ``vector_to_3tz`` (M8).

bpy-free. The Blender side (``blender/vector_publish.py``) reads each object's
evaluated mesh into :class:`~geotools.core.vector.model.VectorData` in the
project CRS (the project offset added back in binary64), and hands them to a
``BuildJob`` as a :class:`VectorSource`. The job's worker thread writes them
with the vector exporter (:func:`write_source`, one GeoPackage table per object
and geometry kind) and tiles that file.

The CRS rusty-tiles uses is always passed as ``source_crs``
(:func:`placement_kwargs`), never read from the GeoPackage: the project's
``source_crs`` is exactly what the mesh publish passes (the EPSG picker's plain
``EPSG:nnnn``, or WKT or PROJ text), whatever ``gpkg_spatial_ref_sys`` says.

Heights: the GeoPackage holds Z in the project CRS's height (local z plus the
offset's A), and rusty-tiles adds ``height_offset``, the same rule as the mesh
publish (ellipsoidal height = z + A + height offset).

Republishing passes the previous archive as ``reuse_tileset`` (rusty-tiles
re-encodes only what changed). rusty-tiles refuses a previous archive that is
the output itself, so the job copies it into ``work/`` first. A previous
archive rusty-tiles cannot reuse (another encoder version, other settings) is
reported, and the job builds afresh.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..core.crs_match import DataCrs
from ..core.placement import Placement, PlacementMode
from ..core.vector import gpkg
from ..core.vector.model import Kind, VectorData

__all__ = [
    "LOCAL_CRS",
    "VectorLayer",
    "VectorSource",
    "is_reuse_refusal",
    "placement_kwargs",
    "summarise_vector_report",
    "table_names",
    "write_source",
]

LOCAL_CRS = "local"
"""rusty-tiles' ``source_crs`` for local metre XYZ (a LOCAL project)."""

KIND_TABLE_SUFFIX = {Kind.POLYGONS: "", Kind.LINES: "_lines", Kind.POINTS: "_points"}

# Messages of rusty-tiles 0.4 refusing a previous archive as a reuse baseline:
# "previous encoder differs ...", "previous encoder, CRS or conversion settings
# differ ...", "... without reuseTileset", "invalid previous payload record".
_REUSE_REFUSAL = re.compile(r"\bprevious\b|reusetileset|reuse_tileset|reuse-tileset", re.IGNORECASE)

_TABLE_CHARS = re.compile(r"[^A-Za-z0-9_]+")
MAX_TABLE_CHARS = 60


@dataclass(frozen=True, slots=True)
class VectorLayer:
    """One GeoPackage table: the features of one geometry kind of one object."""

    table: str
    data: VectorData  # project-CRS coordinates, binary64
    kind: Kind
    object_name: str

    @property
    def features(self) -> int:
        return self.data.geometries[self.kind].n_features


@dataclass(frozen=True, slots=True)
class VectorSource:
    """What a vector publish tiles; built on the main thread, written by the job."""

    layers: tuple[VectorLayer, ...]
    crs: DataCrs | None  # declared in the GeoPackage; None writes srs_id -1
    # vector_to_3tz keywords other than input, output, force, reuse and callback:
    # source_crs and height_offset (placement_kwargs), and settings such as jobs.
    options: Mapping[str, object] = field(default_factory=dict)
    reuse: bool = True  # pass the previous archive as reuse_tileset

    @property
    def features(self) -> int:
        return sum(layer.features for layer in self.layers)


def placement_kwargs(placement: Placement) -> dict[str, object]:
    """``source_crs`` and ``height_offset`` for ``rusty_tiles.vector_to_3tz``.

    SOURCE_CRS passes the project's own ``source_crs`` text (what the mesh
    publish passes) and its height offset; LOCAL publishes local metres
    (``source_crs="local"``). rusty-tiles cannot anchor vectors, so ANCHOR is
    refused. Raises ValueError, also when the placement itself is invalid.
    """
    mode = PlacementMode(placement.mode)
    if mode is PlacementMode.ANCHOR:
        raise ValueError(
            "Vector publishing needs a project CRS: rusty-tiles places vector features by "
            "their CRS and cannot anchor them. Set the project to Source CRS, or to Local "
            "for an unplaced tileset"
        )
    mesh = placement.mesh_to_tileset_kwargs()  # raises ValueError for an invalid placement
    if mode is PlacementMode.LOCAL:
        return {"source_crs": LOCAL_CRS}
    return {"source_crs": mesh["source_crs"], "height_offset": float(mesh["height_offset"])}


def table_names(names: list[tuple[str, Kind]]) -> list[str]:
    """Stable, distinct GeoPackage table names for ``(object name, kind)`` pairs.

    ``contexts`` stays ``contexts``; ``Site.contexts`` becomes ``Site_contexts``
    and its lines ``Site_contexts_lines``. An imported ``contexts.lines`` object
    is ``contexts_lines``, not ``contexts_lines_lines``. Reuse keys features by
    layer and source id, so a name must come out the same on every publish:
    duplicates get ``_2``, ``_3`` ... in the order given (the caller sorts by
    object name).
    """
    out: list[str] = []
    taken: set[str] = set()
    for name, kind in names:
        base = _TABLE_CHARS.sub("_", name).strip("_") or "layer"
        if base[0].isdigit():
            base = f"layer_{base}"
        suffix = KIND_TABLE_SUFFIX[kind]
        if suffix and base.lower().endswith(suffix):
            base = base[: -len(suffix)]
        base = base[: MAX_TABLE_CHARS - len(suffix)] + suffix
        table, n = base, 1
        while table.lower() in taken:  # SQLite table names are case-insensitive
            n += 1
            table = f"{base}_{n}"
        taken.add(table.lower())
        out.append(table)
    return out


def _without_measures(data: VectorData) -> VectorData:
    """``data`` with no M: rusty-tiles 0.4 refuses measured GeoPackage geometry."""
    if not any(g.measures is not None for g in data.geometries.values()):
        return data
    geometries = {k: replace(g, measures=None, measure_name="") for k, g in data.geometries.items()}
    return replace(data, geometries=geometries)


def write_source(source: VectorSource, path: Path) -> list[dict[str, object]]:
    """Write every layer of ``source`` into a new GeoPackage at ``path``.

    Returns one record per table (table, object, kind, features) for build.json.
    Measures (a section's chainage as M) are left out: rusty-tiles 0.4 refuses
    measured geometry, and 3D Tiles features have no per-vertex values anyway;
    the per-piece ``chainage_from`` and ``chainage_to`` columns are kept.
    """
    if not source.layers:
        raise ValueError("there are no vector features to publish")
    path = Path(path)
    path.unlink(missing_ok=True)
    records: list[dict[str, object]] = []
    for layer in source.layers:
        data = _without_measures(layer.data)
        count = gpkg.write_layer(path, layer.table, data, layer.kind, source.crs)
        records.append(
            {
                "table": layer.table,
                "object": layer.object_name,
                "kind": layer.kind.value,
                "features": count,
            }
        )
    return records


def is_reuse_refusal(error: BaseException) -> bool:
    """Whether rusty-tiles refused the previous archive as a reuse baseline."""
    return bool(_REUSE_REFUSAL.search(str(error)))


def _pick(mapping: object, *keys: str) -> dict[str, object]:
    if not isinstance(mapping, Mapping):
        return {}
    return {k: mapping[k] for k in keys if k in mapping}


def summarise_vector_report(report: Mapping[str, Any]) -> dict[str, object]:
    """The ``VectorResult.report`` fields that matter to a GeoTools user.

    rusty-tiles 0.4's report is camelCase (``featuresWithoutGeometry``); the
    summary keeps its names, with each layer cut down to its name, features,
    CRS and height rule, and ``reuse`` to its counts.
    """
    summary: dict[str, object] = {
        k: report[k]
        for k in (
            "features",
            "featuresWithoutGeometry",
            "skippedFeatures",
            "tiles",
            "leafTiles",
            "fragments",
            "lodLevels",
            "lodToleranceMetres",
        )
        if k in report
    }
    layers = report.get("layers")
    if isinstance(layers, list):
        summary["layers"] = [
            _pick(
                layer,
                "name",
                "features",
                "invalidFeatures",
                "sourceCrs",
                "crsOperation",
                "heightMode",
                "heightOffset",
            )
            for layer in layers
        ]
    reuse = report.get("reuse")
    if isinstance(reuse, Mapping):
        summary["reuse"] = _pick(
            reuse,
            "previousTileset",
            "requestedPreviousTileset",
            "incompatibleReason",
            "reusedSubtrees",
            "reusedTiles",
            "reusedContents",
            "publishedContents",
            "rebuiltContents",
        )
    return summary
