"""GeoJSON and GeoPackage in and out of Blender, as linked sources.

Import: one mesh object per source layer and geometry kind (``<layer>`` for
polygons, ``<layer>.lines`` and ``<layer>.points`` when a layer mixes kinds),
in the project frame, with the feature attributes on the matching domain
(FACE, EDGE, POINT) and a ``fid`` INT attribute. Each object is linked to its
file (``links.GeoToolsLink``) and can be refreshed, filtered and written back.

Data in another CRS than the project's is reprojected into it with the bundled
pyproj (``crs_match.reproject``) before the project offset is subtracted, all
in binary64; the report (operation, accuracy, notes) goes into the import
report and the link, and Refresh reprojects the same way. Projected data in a
LOCAL or ANCHOR project is refused with the data's centre offered as the
project origin (``data_origin``).

Export: the evaluated mesh (modifiers applied, so a draped result keeps its
heights) to GeoJSON or GeoPackage with Z, in the project CRS.

Geometry is never written back to a source. Parsing, writing and CRS logic
live in ``core``; this module moves arrays in and out of Blender with
``foreach_get``/``foreach_set``. STRING attributes are the exception: Blender
5.2's ``foreach_set``/``foreach_get`` refuse them, so they are set one element
at a time (about 0.4 s per million elements; see the M8 design).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import bpy
import numpy as np
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from mathutils.geometry import tessellate_polygon

from ..core import crs_match, fingerprint
from ..core import reproject as core_reproject
from ..core.crs_match import DataCrs
from ..core.data_origin import NeedsProjectOrigin, check_local_project
from ..core.fingerprint import FileState, Fingerprint
from ..core.placement import Placement, PlacementMode
from ..core.vector import geojson, gpkg
from ..core.vector import mesh as vmesh
from ..core.vector.digest import diff, feature_digests
from ..core.vector.model import Column, ColumnType, Kind, VectorData, concat
from ..core.vector.options import VectorOptions, within_bbox
from ..core.vector.transform import bounds as data_bounds
from ..core.vector.transform import reproject_data
from . import data_origin, links
from .common import redraw_ui
from .links import RefreshError, RefreshResult
from .properties import FieldError, placement_from_props

log = logging.getLogger(__name__)

DOMAINS = {Kind.POLYGONS: "FACE", Kind.LINES: "EDGE", Kind.POINTS: "POINT"}
KIND_SUFFIX = {Kind.POLYGONS: "", Kind.LINES: ".lines", Kind.POINTS: ".points"}
FID = vmesh.FID

# Names Blender gives a meaning to, or reserves; a source column with one of
# these names is imported as "<name>_src".
RESERVED = frozenset(
    {
        FID,
        "position",
        "material_index",
        "sharp_face",
        "sharp_edge",
        "crease_vert",
        "crease_edge",
        "bevel_weight_vert",
        "bevel_weight_edge",
        "shade_smooth",
        "normal",
        "velocity",
        "id",
    }
)
MAX_NAME_BYTES = 63


class ImportRefused(RuntimeError):
    """The data cannot be placed in this project (CRS mismatch, unknown CRS ...)."""


# --- placement and CRS ----------------------------------------------------------------


@dataclass(slots=True)
class Frame:
    """How data coordinates map into this project's Blender frame."""

    offset: tuple[float, float, float]
    project: DataCrs | None
    placement: Placement
    warnings: list[str] = field(default_factory=list)


def project_frame(scene: bpy.types.Scene) -> Frame:
    try:
        placement = placement_from_props(scene.geotools)
    except FieldError as ex:
        raise ImportRefused(f"The project placement is not valid: {ex}") from None
    if PlacementMode(placement.mode) is PlacementMode.SOURCE_CRS:
        project = crs_match.project_crs(placement)
        if project is None:
            raise ImportRefused("Set the project CRS first (Project panel)")
        return Frame(tuple(placement.source_offset), project, placement)  # type: ignore[arg-type]
    return Frame((0.0, 0.0, 0.0), None, placement)


@dataclass(frozen=True, slots=True)
class CrsDecision:
    crs: DataCrs
    """The CRS recorded in the link: the data's, or the project's when the user confirmed it."""
    reproject: bool
    """True when the data is in another CRS and is reprojected into the project's."""


def accept_crs(frame: Frame, data_crs: DataCrs, what: str, assume_project_crs: bool) -> CrsDecision:
    """How data in ``data_crs`` goes into the project, or raise :class:`ImportRefused`.

    LOCAL and ANCHOR projects (``frame.project`` None) take the data as it is;
    :func:`check_local` then decides from its extent.
    """
    if frame.project is None:
        return CrsDecision(data_crs, False)
    verdict = crs_match.matches(frame.project, data_crs)
    if verdict is True:
        return CrsDecision(data_crs, False)
    if verdict is False:
        if not core_reproject.available():
            reason = "Reprojection is not available: pyproj did not load (see Check Dependencies)"
            raise ImportRefused(crs_match.mismatch_message(frame.project, data_crs, what, reason))
        return CrsDecision(data_crs, True)
    if not assume_project_crs:
        raise ImportRefused(crs_match.unknown_message(data_crs, what))
    return CrsDecision(
        DataCrs(frame.project.epsg, frame.project.definition, crs_match.SOURCE_USER), False
    )


def reproject_into(
    frame: Frame, data: VectorData, what: str, allow_network: bool
) -> tuple[VectorData, crs_match.ReprojectionReport | None]:
    """``data`` reprojected into the project CRS, or raise :class:`ImportRefused`."""
    assert frame.project is not None
    try:
        return reproject_data(data, frame.project, allow_network=allow_network)
    except crs_match.ReprojectionUnavailable as ex:
        raise ImportRefused(
            crs_match.mismatch_message(frame.project, data.crs, what, str(ex))
        ) from None
    except core_reproject.ReprojectionError as ex:
        raise ImportRefused(
            f"Cannot reproject {what} from {data.crs.describe()} to "
            f"{frame.project.describe()}: {ex}"
        ) from None


def check_local(frame: Frame, loaded: list[tuple[str, VectorData]]) -> None:
    """The LOCAL/ANCHOR rule for ``(what, data)`` pairs: warn, or raise ``NeedsProjectOrigin``.

    Layers in the same CRS are checked together, so a GeoPackage of several
    layers gets one suggested origin: the centre of all of them.
    """
    mode = PlacementMode(frame.placement.mode)
    groups: dict[str, list[tuple[str, VectorData]]] = {}
    for what, data in loaded:
        groups.setdefault(crs_match.to_text(data.crs), []).append((what, data))
    for group in groups.values():
        what = ", ".join(w for w, _d in group)
        extent = data_bounds([d for _w, d in group])
        frame.warnings.append(check_local_project(mode, group[0][1].crs, extent, what))


def site_bbox(scene: bpy.types.Scene, frame: Frame) -> tuple[float, float, float, float]:
    """XY bounds of the source collection, in the data's coordinates."""
    collection = scene.geotools.source_collection
    corners = []
    if collection is not None:
        for obj in collection.all_objects:
            if obj.type != "MESH":
                continue
            m = np.array(obj.matrix_world, dtype=np.float64)
            box = np.array([tuple(c) for c in obj.bound_box], dtype=np.float64)
            corners.append(box @ m[:3, :3].T + m[:3, 3])
    if not corners:
        raise ImportRefused("'Within Site Extent' needs a source collection with a mesh")
    pts = np.concatenate(corners)
    e, n = frame.offset[0], frame.offset[1]
    return (
        float(pts[:, 0].min()) + e,
        float(pts[:, 1].min()) + n,
        float(pts[:, 0].max()) + e,
        float(pts[:, 1].max()) + n,
    )


# --- reading ---------------------------------------------------------------------------


def read_source(kind: str, path: Path, layer: str, where: str) -> VectorData:
    """Read one layer. Raises ValueError (GeoJsonError, GpkgError) or OSError."""
    if kind == "GEOJSON":
        if where.strip():
            raise ValueError("SQL filters apply to GeoPackages only")
        return geojson.read(path)
    return gpkg.read_layer(path, layer, where)


def blender_names(columns: dict[str, Column]) -> dict[str, str]:
    """Blender attribute name -> source column name, renaming reserved and long names."""
    out: dict[str, str] = {}
    for source in columns:
        name = source
        if name in RESERVED or name.startswith("."):
            name = f"{name}_src"
        while len(name.encode("utf-8")) > MAX_NAME_BYTES:
            name = name[:-1]
        base, i = name, 1
        while name in out or name in RESERVED:
            name = f"{base[: MAX_NAME_BYTES - 4]}_{i}"
            i += 1
        out[name] = source
    return out


# --- building meshes -------------------------------------------------------------------


def _tessellate(rings: list[np.ndarray]) -> np.ndarray:
    tris = tessellate_polygon([ring.tolist() for ring in rings])
    return np.array(tris, dtype=np.int64).reshape(-1, 3)


def build_mesh(name: str, arrays: vmesh.MeshArrays) -> bpy.types.Mesh:
    """A new mesh from the arrays, with ``foreach_set`` only."""
    mesh = bpy.data.meshes.new(name)
    mesh.vertices.add(len(arrays.positions))
    mesh.vertices.foreach_set("co", np.ascontiguousarray(arrays.positions, np.float32).ravel())
    if arrays.kind is Kind.POLYGONS and len(arrays.loop_starts):
        mesh.loops.add(len(arrays.loop_vertices))
        mesh.loops.foreach_set("vertex_index", arrays.loop_vertices.astype(np.int32))
        mesh.polygons.add(len(arrays.loop_starts))
        mesh.polygons.foreach_set("loop_start", arrays.loop_starts.astype(np.int32))
        mesh.update(calc_edges=True)
    elif arrays.kind is Kind.LINES and len(arrays.edges):
        mesh.edges.add(len(arrays.edges))
        mesh.edges.foreach_set("vertices", arrays.edges.astype(np.int32).ravel())
        mesh.update()
    else:
        mesh.update()
    return mesh


def write_attributes(
    mesh: bpy.types.Mesh,
    kind: Kind,
    arrays: vmesh.MeshArrays,
    fids: np.ndarray,
    columns: dict[str, Column],
    names: dict[str, str],
) -> None:
    """The ``fid`` and the feature columns on the kind's domain, one value per element."""
    domain = DOMAINS[kind]
    element = arrays.element_feature
    attr = mesh.attributes.new(FID, "INT", domain)
    attr.data.foreach_set("value", fids[element].astype(np.int32))
    for name, source in names.items():
        column = columns[source]
        values = vmesh.stored_values(column)
        attr = mesh.attributes.new(name, vmesh.BLENDER_TYPES[column.type], domain)
        if column.type is ColumnType.STRING:
            data = attr.data
            per_element = [values[i] for i in element.tolist()]  # type: ignore[index]
            for item, value in zip(data, per_element, strict=True):
                item.value = value  # foreach_set refuses STRING attributes
        else:
            attr.data.foreach_set("value", np.asarray(values)[element])


def fid_range_error(fids: np.ndarray) -> str | None:
    if len(fids) and (fids.min() < -(2**31) or fids.max() > 2**31 - 1):
        return "Feature ids beyond 32-bit integers cannot be stored in Blender"
    return None


@dataclass(slots=True)
class Built:
    mesh: bpy.types.Mesh
    arrays: vmesh.MeshArrays
    fids: np.ndarray  # per feature of the kind
    columns: dict[str, Column]  # per feature of the kind
    names: dict[str, str]


def build_kind(name: str, data: VectorData, kind: Kind, frame: Frame) -> Built:
    if kind in data.geometries:
        geoms = data.geometries[kind]
        fids = data.fids_for(kind)
        columns = data.columns_for(kind)
    else:  # the kind is gone from the file: an empty mesh
        from ..core.vector.model import Geometries

        geoms = Geometries(kind=kind)
        fids = np.zeros(0, dtype=np.int64)
        columns = {n: c.take(np.zeros(0, dtype=np.int64)) for n, c in data.columns.items()}
    problem = fid_range_error(fids)
    if problem:
        raise ValueError(problem)
    arrays = vmesh.mesh_arrays(geoms, frame.offset, _tessellate)
    names = blender_names(columns)
    mesh = build_mesh(name, arrays)
    write_attributes(mesh, kind, arrays, fids, columns, names)
    return Built(mesh, arrays, fids, columns, names)


def object_name(layer: str, kind: Kind, kinds: list[Kind]) -> str:
    return layer if len(kinds) == 1 or kind is Kind.POLYGONS else f"{layer}{KIND_SUFFIX[kind]}"


@dataclass(slots=True)
class ImportResult:
    """What :func:`import_vectors` made and what to tell the user."""

    objects: list[bpy.types.Object]
    warnings: list[str]
    notes: list[str]
    """Information lines: the reprojection used, with its accuracy and notes."""


def import_vectors(
    context: bpy.types.Context,
    kind: str,
    path: Path,
    layers: list[str],
    *,
    where: str = "",
    within_site: bool = False,
    assume_project_crs: bool = False,
    allow_grid_downloads: bool = False,
) -> ImportResult:
    """Import ``layers`` of ``path`` as linked objects.

    Data in another CRS is reprojected into the project CRS (PROJ may download
    grids only with ``allow_grid_downloads``). Raises :class:`ImportRefused`,
    ``NeedsProjectOrigin`` (projected data in a LOCAL or ANCHOR project),
    ValueError or OSError, before any object is made.
    """
    scene = context.scene
    frame = project_frame(scene)
    # Before reading: a write during the import then shows as a change on disk.
    print_ = fingerprint.take(path)
    loaded = []
    for layer in layers:
        data = read_source(kind, path, layer, where)
        what = f"{path.name}" if kind == "GEOJSON" else f"{path.name} ({layer})"
        decision = accept_crs(frame, data.crs, what, assume_project_crs)
        report = None
        if decision.reproject:
            data, report = reproject_into(frame, data, what, allow_grid_downloads)
        loaded.append((layer, what, data, decision.crs, report))
    if frame.project is None:
        check_local(frame, [(what, data) for _l, what, data, _c, _r in loaded])
    bbox = site_bbox(scene, frame) if within_site else None
    if bbox is not None:
        loaded = [
            (layer, what, data.select_rows(within_bbox(data, bbox)), crs, report)
            for layer, what, data, crs, report in loaded
        ]

    collection = context.collection or scene.collection
    objects = []
    warnings = list(dict.fromkeys(frame.warnings))
    notes: list[str] = []
    for layer, _what, data, crs, report in loaded:
        layer_name = data.name if kind == "GEOJSON" else layer
        kinds = data.kinds()
        if not kinds:
            warnings.append(f"{layer_name}: no features to import")
        if data.dropped_parts:
            warnings.append(f"{layer_name}: skipped {data.dropped_parts} unreadable geometries")
        if report is not None:
            notes.append(f"{layer_name}: {crs_match.report_text(report)}.")
            notes.extend(report.notes)
        for geometry_kind in kinds:
            name = object_name(layer_name, geometry_kind, kinds)
            built = build_kind(name, data, geometry_kind, frame)
            if built.arrays.dropped_parts:
                warnings.append(f"{name}: skipped {built.arrays.dropped_parts} degenerate parts")
            obj = bpy.data.objects.new(name, built.mesh)
            collection.objects.link(obj)
            link = obj.geotools_link
            link.kind = kind
            link.filepath = str(path)
            link.layer = layer_name
            link.fid_field = data.fid_field
            link.crs = crs_match.to_text(crs)
            link.options = VectorOptions(
                geometry_kind,
                where,
                bbox,
                built.names,
                allow_grid_downloads=allow_grid_downloads,
                reprojection=crs_match.report_to_dict(report) if report else None,
            ).to_text()
            links.mark_synced(link, print_)
            objects.append(obj)
    return ImportResult(objects, warnings, list(dict.fromkeys(notes)))


# --- refresh ---------------------------------------------------------------------------


def _read_attr(mesh: bpy.types.Mesh, name: str, domain: str) -> tuple[ColumnType, object] | None:
    attr = mesh.attributes.get(name)
    if attr is None or attr.domain != domain or attr.data_type not in vmesh.COLUMN_TYPES:
        return None
    ctype = vmesh.COLUMN_TYPES[attr.data_type]
    if ctype is ColumnType.STRING:
        return ctype, _Strings(attr)
    dtype = {ColumnType.FLOAT: np.float32, ColumnType.INT: np.int32, ColumnType.BOOL: bool}[ctype]
    values = np.empty(len(attr.data), dtype=dtype)
    attr.data.foreach_get("value", values)
    return ctype, values


class _Strings:
    """A STRING attribute's values, read one element at a time on demand."""

    def __init__(self, attr: bpy.types.Attribute) -> None:
        self.data = attr.data

    def __len__(self) -> int:
        return len(self.data)

    def __getitem__(self, i: int) -> bytes:
        return self.data[i].value


def _element_vertices(mesh: bpy.types.Mesh, kind: Kind) -> tuple[np.ndarray, np.ndarray]:
    """``(vertex index, element index)`` pairs tying each vertex to an element."""
    if kind is Kind.POLYGONS:
        n = len(mesh.loops)
        verts = np.empty(n, dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", verts)
        totals = np.empty(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("loop_total", totals)
        return verts, np.repeat(np.arange(len(totals)), totals)
    if kind is Kind.LINES:
        edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", edges)
        return edges, np.repeat(np.arange(len(mesh.edges)), 2)
    n = len(mesh.vertices)
    return np.arange(n), np.arange(n)


def mesh_digests(mesh: bpy.types.Mesh, kind: Kind, names: dict[str, str]) -> tuple:
    """``(fids, digests)`` of the features in an imported mesh."""
    domain = DOMAINS[kind]
    fid = _read_attr(mesh, FID, domain)
    if fid is None or fid[0] is not ColumnType.INT:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.uint64)
    element_fid = np.asarray(fid[1], dtype=np.int64)
    fids, first, feature = np.unique(element_fid, return_index=True, return_inverse=True)
    positions = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", positions)
    verts, elements = _element_vertices(mesh, kind)
    vertex_feature = np.zeros(len(mesh.vertices), dtype=np.int64)
    if len(verts):
        vertex_feature[verts] = feature[elements]
    columns = {}
    for name, source in names.items():
        read = _read_attr(mesh, name, domain)
        if read is None:
            continue
        ctype, values = read
        if ctype is ColumnType.STRING:
            picked: object = [values[int(i)] for i in first]  # type: ignore[index]
        else:
            picked = np.asarray(values)[first]
        columns[source] = (ctype, picked)
    return fids, feature_digests(len(fids), positions.reshape(-1, 3), vertex_feature, columns)


def built_digests(built: Built) -> np.ndarray:
    columns = {
        source: (built.columns[source].type, vmesh.stored_values(built.columns[source]))
        for source in built.names.values()
    }
    return feature_digests(
        len(built.fids), built.arrays.positions, built.arrays.vertex_feature, columns
    )


def refresh_vectors(obj: bpy.types.Object) -> RefreshResult:
    """The refresher for GEOJSON and GPKG links (``links.REFRESHERS``)."""
    link = obj.geotools_link
    options = VectorOptions.from_text(link.options)
    path = links.link_path(link)
    scene = bpy.context.scene
    try:
        data = read_source(link.kind, path, link.layer, options.where)
    except (ValueError, OSError) as ex:
        raise RefreshError(f"Cannot read {path.name}: {ex}") from None
    try:
        frame = project_frame(scene)
    except ImportRefused as ex:
        raise RefreshError(str(ex)) from None
    stored = crs_match.from_text(link.crs)
    confirmed = stored is not None and stored.source == crs_match.SOURCE_USER
    message = ""
    try:
        decision = accept_crs(frame, data.crs, path.name, assume_project_crs=confirmed)
        report = None
        if decision.reproject:
            data, report = reproject_into(frame, data, path.name, options.allow_grid_downloads)
        if frame.project is None:
            check_local(frame, [(path.name, data)])
    except (ImportRefused, NeedsProjectOrigin) as ex:
        raise RefreshError(str(ex)) from None
    crs = decision.crs
    if report is not None:
        message = _reprojection_message(report, options.reprojection)
    options.reprojection = crs_match.report_to_dict(report) if report else None
    if options.site_bbox is not None:
        data = data.select_rows(within_bbox(data, options.site_bbox))
    old_fids, old_digests = (np.zeros(0, np.int64), np.zeros(0, np.uint64))
    if isinstance(obj.data, bpy.types.Mesh):
        old_fids, old_digests = mesh_digests(obj.data, options.kind, options.columns)
    try:
        built = build_kind(obj.data.name if obj.data else obj.name, data, options.kind, frame)
    except ValueError as ex:
        raise RefreshError(str(ex)) from None
    added, removed, changed = diff(old_fids, old_digests, built.fids, built_digests(built))
    links.replace_mesh(obj, built.mesh)
    link.crs = crs_match.to_text(crs)
    link.fid_field = data.fid_field
    options.columns = built.names
    link.options = options.to_text()
    return RefreshResult(added, removed, changed, message)


def _reprojection_message(report: crs_match.ReprojectionReport, stored: object) -> str:
    """Says the refresh reprojected as the import did, or what changed."""
    before = crs_match.report_from_dict(stored)
    if before is None:
        return f"{crs_match.report_text(report)}."
    if (before.operation, before.target) == (report.operation, report.target):
        return f"Reprojected as at import: {report.operation}."
    return (
        f"The reprojection changed: {crs_match.report_text(report)}, where the last sync used "
        f"{before.operation}."
    )


# --- write-back ------------------------------------------------------------------------


def writable_attributes(obj: bpy.types.Object) -> list[str]:
    """Attributes on the link's domain that can be written back (not fid or Blender's own)."""
    if not isinstance(obj.data, bpy.types.Mesh):
        return []
    domain = DOMAINS[VectorOptions.from_text(obj.geotools_link.options).kind]
    return sorted(
        a.name
        for a in obj.data.attributes
        if a.domain == domain
        and a.data_type in vmesh.COLUMN_TYPES
        and a.name != FID
        and not a.name.startswith(".")
        and not a.is_internal
        and not a.is_required
        and a.name not in RESERVED
    )


@dataclass(slots=True)
class WriteBack:
    features: int
    columns: list[str]
    added: list[str]
    missing: int
    inconsistent: int


def feature_values(
    mesh: bpy.types.Mesh, domain: str, name: str
) -> tuple[ColumnType, np.ndarray, list[object], int]:
    """``(type, fids, one value per fid, features whose elements disagree)``."""
    fid = _read_attr(mesh, FID, domain)
    read = _read_attr(mesh, name, domain)
    if fid is None or read is None:
        raise ValueError(f"{mesh.name} has no {domain} attribute {name!r} with fids")
    element_fid = np.asarray(fid[1], dtype=np.int64)
    fids, first, feature = np.unique(element_fid, return_index=True, return_inverse=True)
    ctype, values = read
    if ctype is ColumnType.STRING:
        picked = [values[int(i)].decode("utf-8", "replace") for i in first]  # type: ignore[index]
        return ctype, fids, picked, 0
    arr = np.asarray(values)
    first_values = arr[first]
    same = (arr == first_values[feature]) | (
        np.isnan(arr) & np.isnan(first_values[feature]) if ctype is ColumnType.FLOAT else False
    )
    inconsistent = len(np.unique(feature[~same]))
    if ctype is ColumnType.FLOAT:
        out = [None if np.isnan(v) else float(v) for v in first_values.astype(np.float64)]
    elif ctype is ColumnType.INT:
        out = [int(v) for v in first_values]
    else:
        out = [bool(v) for v in first_values]
    return ctype, fids, out, inconsistent


def write_back(obj: bpy.types.Object, names: list[str]) -> WriteBack:
    """Write the attributes ``names`` of ``obj`` back to its file by fid.

    Refused (ValueError) if the file changed since the last sync. Geometry is
    never written.
    """
    link = obj.geotools_link
    if link.kind not in {"GPKG", "GEOJSON"}:
        raise ValueError("Write-back works for GeoPackage and GeoJSON links")
    if obj.mode == "EDIT":
        raise ValueError(f"{obj.name} is in Edit Mode; leave it to write back")
    if not names:
        raise ValueError("Choose the attributes to write back")
    path = links.link_path(link)
    state, _current = fingerprint.check(path, Fingerprint.from_text(link.fingerprint))
    if state is FileState.MISSING:
        raise ValueError(f"{path.name} is missing")
    if state is FileState.CHANGED:
        links.set_status(link, "CHANGED", "The file changed on disk")
        raise ValueError(f"{path.name} changed since the last sync; Refresh first, then write back")
    options = VectorOptions.from_text(link.options)
    domain = DOMAINS[options.kind]
    mesh = obj.data
    allowed = set(writable_attributes(obj))
    fids = None
    columns: dict[str, tuple[ColumnType, list[object]]] = {}
    inconsistent = 0
    for name in names:
        if name not in allowed:
            raise ValueError(f"{name!r} is not an attribute that can be written back")
        ctype, f, values, bad = feature_values(mesh, domain, name)
        fids = f
        inconsistent = max(inconsistent, bad)
        columns[options.columns.get(name, name)] = (ctype, values)
    assert fids is not None
    if link.kind == "GPKG":
        updated, missing, added = gpkg.update_attributes(path, link.layer, fids, columns)
    else:
        values = {k: vals for k, (_t, vals) in columns.items()}
        updated, missing = geojson.rewrite_properties(path, link.fid_field, fids, values)
        added = [n for n in columns if n not in options.columns.values()]
    for name in names:
        options.columns.setdefault(name, name)
    link.options = options.to_text()
    links.mark_synced(link)
    return WriteBack(updated, list(columns), added, missing, inconsistent)


# --- export ----------------------------------------------------------------------------


def export_attributes(mesh: bpy.types.Mesh, domain: str) -> dict:
    out = {}
    for attr in mesh.attributes:
        if (
            attr.domain != domain
            or attr.data_type not in vmesh.COLUMN_TYPES
            or attr.name.startswith(".")
            or attr.is_internal
            or (attr.is_required and attr.name != FID)
            or (attr.name in RESERVED and attr.name != FID)
        ):
            continue
        read = _read_attr(mesh, attr.name, domain)
        if read is not None:
            out[attr.name] = read
    return out


def mesh_input(
    obj: bpy.types.Object,
    depsgraph,
    offset: tuple[float, float, float],
    *,
    apply_modifiers: bool = True,
):
    """The evaluated mesh of ``obj`` in source coordinates (binary64).

    ``apply_modifiers`` False reads the original mesh data instead (the build
    profile's Apply Modifiers option), still with the evaluated transform.
    """
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh() if apply_modifiers else obj.data
    try:
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        m = np.array(evaluated.matrix_world, dtype=np.float64)
        world = co.reshape(-1, 3).astype(np.float64) @ m[:3, :3].T + m[:3, 3]
        n_faces = len(mesh.polygons)
        starts = np.empty(n_faces, dtype=np.int32)
        totals = np.empty(n_faces, dtype=np.int32)
        mesh.polygons.foreach_get("loop_start", starts)
        mesh.polygons.foreach_get("loop_total", totals)
        loops = np.empty(len(mesh.loops), dtype=np.int32)
        loop_edges = np.empty(len(mesh.loops), dtype=np.int32)
        mesh.loops.foreach_get("vertex_index", loops)
        mesh.loops.foreach_get("edge_index", loop_edges)
        edges = np.empty(len(mesh.edges) * 2, dtype=np.int32)
        mesh.edges.foreach_get("vertices", edges)
        # STRING values are read lazily, one per feature, before to_mesh_clear below.
        face, edge, point = (export_attributes(mesh, d) for d in ("FACE", "EDGE", "POINT"))
        result = vmesh.MeshInput(
            positions=vmesh.from_local(world, offset),
            loop_starts=starts,
            loop_totals=totals,
            loop_vertices=loops,
            edges=edges.reshape(-1, 2),
            loop_edges=loop_edges,
            face_attrs=face,
            edge_attrs=edge,
            point_attrs=point,
        )
        return vmesh.vectors_from_mesh(result, obj.name, None)
    finally:
        if apply_modifiers:
            evaluated.to_mesh_clear()


def source_names(obj: bpy.types.Object, data: VectorData) -> VectorData:
    """Columns renamed back to their source names when ``obj`` is linked."""
    if not links.is_linked(obj):
        return data
    names = VectorOptions.from_text(obj.geotools_link.options).columns
    data.columns = {names.get(n, n): c for n, c in data.columns.items()}
    for name, column in data.columns.items():
        column.name = name
    return data


def export_vectors(
    context: bpy.types.Context,
    objects: list[bpy.types.Object],
    path: Path,
    file_format: str,
    layer: str,
    overwrite: bool,
) -> tuple[dict[Kind, int], list[str]]:
    """Export ``objects`` (evaluated) to ``path``; return features per kind and warnings."""
    frame = project_frame(context.scene)
    warnings = list(frame.warnings)
    if frame.project is None:
        warnings.append("The project is not in a CRS: coordinates are written as local metres")
    depsgraph = context.evaluated_depsgraph_get()
    by_kind: dict[Kind, list[VectorData]] = {}
    for obj in objects:
        for data in mesh_input(obj, depsgraph, frame.offset):
            data = source_names(obj, data)
            (kind,) = data.kinds()
            by_kind.setdefault(kind, []).append(data)
    if not by_kind:
        raise ValueError("Nothing to export: the objects have no faces, edges or vertices")
    merged = {kind: concat(datas, layer) for kind, datas in by_kind.items()}
    counts = {kind: merged[kind].n_rows for kind in merged}
    if file_format == "GEOJSON":
        geojson.write(path, concat(list(merged.values()), layer), frame.project)
        if frame.project is not None and frame.project.epsg != crs_match.WGS84:
            warnings.append("Written with the legacy 'crs' member (projected, outside RFC 7946)")
        return counts, warnings
    kinds = [k for k in (Kind.POLYGONS, Kind.LINES, Kind.POINTS) if k in merged]
    for kind in kinds:
        table = layer if len(kinds) == 1 else f"{layer}{KIND_SUFFIX[kind].replace('.', '_')}"
        gpkg.write_layer(path, table, merged[kind], kind, frame.project, overwrite=overwrite)
    return counts, warnings


# --- operators ------------------------------------------------------------------------


def _object_mode(cls: type, context: bpy.types.Context) -> bool:
    if context.scene is None:
        return False
    if context.mode != "OBJECT":
        cls.poll_message_set("Switch to Object Mode first")
        return False
    return True


def report_import(op: bpy.types.Operator, result: ImportResult, path: Path) -> None:
    """The import's summary, warnings and reprojection report, as operator reports."""
    objects = result.objects
    noun = "object" if len(objects) == 1 else "objects"
    text = f"Imported {len(objects)} linked {noun} from {path.name}"
    if result.notes:
        op.report({"INFO"}, " ".join(result.notes))
    if result.warnings:
        op.report({"WARNING"}, f"{text}. " + " ".join(result.warnings))
    else:
        op.report({"INFO"}, text)


class _ImportVectorBase:
    bl_options = {"REGISTER", "UNDO"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    within_site: BoolProperty(
        name="Within Site Extent",
        description="Only features whose bounds overlap the source collection's extent",
        default=False,
    )
    assume_project_crs: BoolProperty(
        name="Data Is in Project CRS",
        description="The file does not say which CRS it is in: take it as the project CRS",
        default=False,
    )
    allow_grid_downloads: BoolProperty(
        name="Allow Grid Downloads",
        description="When data in another CRS is reprojected, let PROJ download a "
        "transformation grid from cdn.proj.org if a more accurate operation needs one. Off: "
        "imports stay offline and reproducible",
        default=False,
    )

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _object_mode(cls, context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def _run(self, context, kind: str, layers: list[str], where: str = "") -> set[str]:
        path = Path(bpy.path.abspath(self.filepath))
        if not path.is_file():
            self.report({"ERROR"}, f"File not found: {path}")
            return {"CANCELLED"}
        try:
            result = import_vectors(
                context,
                kind,
                path,
                layers,
                where=where,
                within_site=self.within_site,
                assume_project_crs=self.assume_project_crs,
                allow_grid_downloads=self.allow_grid_downloads,
            )
        except NeedsProjectOrigin as ex:
            request = {
                "kind": kind,
                "filepath": str(path),
                "layer": ",".join(layers) if kind == "GPKG" else "",
                "where": where,
                "within_site": self.within_site,
                "allow_grid_downloads": self.allow_grid_downloads,
            }
            data_origin.offer(context, request, ex)
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        except ImportRefused as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, f"Cannot import {path.name}: {ex}")
            return {"CANCELLED"}
        select_imported(context, result.objects)
        report_import(self, result, path)
        redraw_ui(context)
        return {"FINISHED"}


def select_imported(context: bpy.types.Context, objects: list[bpy.types.Object]) -> None:
    view_layer = context.view_layer
    if view_layer is None or not objects:
        return
    for obj in view_layer.objects:
        obj.select_set(False)
    for obj in objects:
        if obj.name in view_layer.objects:
            obj.select_set(True)
    if objects[0].name in view_layer.objects:
        view_layer.objects.active = objects[0]
    context.scene.geotools_link_index = bpy.data.objects.find(objects[0].name)


class GEOTOOLS_OT_import_geojson(_ImportVectorBase, bpy.types.Operator):
    """Import a GeoJSON file as linked meshes in the project frame (points, lines, polygons)"""

    bl_idname = "geotools.import_geojson"
    bl_label = "Import GeoJSON"

    filter_glob: StringProperty(default="*.geojson;*.json", options={"HIDDEN"})

    def execute(self, context: bpy.types.Context) -> set[str]:
        return self._run(context, "GEOJSON", [""])


class GEOTOOLS_OT_import_gpkg(_ImportVectorBase, bpy.types.Operator):
    """Import GeoPackage feature tables as linked meshes in the project frame"""

    bl_idname = "geotools.import_gpkg"
    bl_label = "Import GeoPackage"

    filter_glob: StringProperty(default="*.gpkg", options={"HIDDEN"})
    layer: StringProperty(
        name="Layer", description="Feature table to import; empty for every feature table"
    )
    where: StringProperty(
        name="Filter",
        description="SQL WHERE clause on the table's columns, e.g. phase = 2 AND depth > 0.5. "
        "Run on a read-only connection",
    )

    def draw(self, context: bpy.types.Context) -> None:
        layout = self.layout
        layout.prop(self, "layer", placeholder="All layers")
        layout.prop(self, "where", placeholder="No filter")
        layout.prop(self, "within_site")
        layout.prop(self, "assume_project_crs")
        layout.prop(self, "allow_grid_downloads")

    def execute(self, context: bpy.types.Context) -> set[str]:
        path = Path(bpy.path.abspath(self.filepath))
        layers = [self.layer.strip()] if self.layer.strip() else []
        if not layers:
            try:
                layers = [info.name for info in gpkg.list_layers(path)]
            except gpkg.GpkgError as ex:
                self.report({"ERROR"}, str(ex))
                return {"CANCELLED"}
            if not layers:
                self.report({"ERROR"}, f"{path.name} has no feature tables")
                return {"CANCELLED"}
        return self._run(context, "GPKG", layers, self.where)


class GEOTOOLS_OT_link_filters(bpy.types.Operator):
    """Change the import filters of a linked GeoPackage or GeoJSON object and refresh it"""

    bl_idname = "geotools.link_filters"
    bl_label = "Filters"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty(name="Object", description="Linked object; empty for the selected link")
    where: StringProperty(
        name="Filter", description="SQL WHERE clause (GeoPackage only); empty for none"
    )
    within_site: BoolProperty(
        name="Within Site Extent",
        description="Only features whose bounds overlap the source collection's extent",
    )

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        obj = links.target_object(context, self.name)
        if obj is None:
            self.report({"ERROR"}, "Select a linked object first")
            return {"CANCELLED"}
        options = VectorOptions.from_text(obj.geotools_link.options)
        self.name = obj.name
        self.where = options.where
        self.within_site = options.site_bbox is not None
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context: bpy.types.Context) -> None:
        obj = bpy.data.objects.get(self.name)
        if obj is not None and obj.geotools_link.kind == "GPKG":
            self.layout.prop(self, "where", placeholder="No filter")
        self.layout.prop(self, "within_site")

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = links.target_object(context, self.name)
        if obj is None or obj.geotools_link.kind not in {"GPKG", "GEOJSON"}:
            self.report({"ERROR"}, "Select a linked GeoPackage or GeoJSON object first")
            return {"CANCELLED"}
        link = obj.geotools_link
        options = VectorOptions.from_text(link.options)
        where = self.where.strip()
        if where and link.kind != "GPKG":
            self.report({"ERROR"}, "SQL filters apply to GeoPackages only")
            return {"CANCELLED"}
        if where:
            problem = gpkg.validate_where(links.link_path(link), link.layer, where)
            if problem:
                self.report({"ERROR"}, f"Filter refused: {problem}")
                return {"CANCELLED"}
        try:
            bbox = (
                site_bbox(context.scene, project_frame(context.scene)) if self.within_site else None
            )
        except ImportRefused as ex:
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        previous = link.options
        options.where, options.site_bbox = where, bbox
        link.options = options.to_text()
        try:
            result = links.refresh_link(obj)
        except RefreshError as ex:
            link.options = previous
            self.report({"ERROR"}, str(ex))
            return {"CANCELLED"}
        self.report({"INFO"}, result.summary(obj.name))
        redraw_ui(context)
        return {"FINISHED"}


class GeoToolsWriteBackItem(bpy.types.PropertyGroup):
    """One attribute in the Write Back dialog (``name`` is the attribute)."""

    write: BoolProperty(name="Write", default=False)


class GEOTOOLS_OT_link_write_back(bpy.types.Operator):
    """Write chosen attributes back to the linked GeoPackage (UPDATE by fid) or GeoJSON (rewrite). Never geometry. Refused if the file changed since the last sync"""

    bl_idname = "geotools.link_write_back"
    bl_label = "Write Back Attributes"
    bl_options = {"REGISTER"}

    name: StringProperty(name="Object", description="Linked object; empty for the selected link")
    columns: StringProperty(
        name="Attributes", description="Attributes to write, separated by commas"
    )
    items: CollectionProperty(type=GeoToolsWriteBackItem, options={"SKIP_SAVE"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _object_mode(cls, context)

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        obj = links.target_object(context, self.name)
        if obj is None:
            self.report({"ERROR"}, "Select a linked object first")
            return {"CANCELLED"}
        self.name = obj.name
        self.items.clear()
        chosen = {c.strip() for c in self.columns.split(",") if c.strip()}
        for attr in writable_attributes(obj):
            item = self.items.add()
            item.name = attr
            item.write = attr in chosen
        if not len(self.items):
            self.report({"ERROR"}, f"{obj.name} has no attributes to write back")
            return {"CANCELLED"}
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context: bpy.types.Context) -> None:
        col = self.layout.column(align=True)
        for item in self.items:
            col.prop(item, "write", text=item.name)

    def execute(self, context: bpy.types.Context) -> set[str]:
        obj = links.target_object(context, self.name)
        if obj is None:
            self.report({"ERROR"}, "Select a linked object first")
            return {"CANCELLED"}
        if len(self.items):
            names = [item.name for item in self.items if item.write]
        else:
            names = [c.strip() for c in self.columns.split(",") if c.strip()]
        try:
            result = write_back(obj, names)
        except (ValueError, OSError) as ex:
            self.report({"ERROR"}, str(ex))
            redraw_ui(context)
            return {"CANCELLED"}
        text = f"Wrote {', '.join(result.columns)} to {result.features} features"
        notes = []
        if result.added:
            notes.append(f"added columns {', '.join(result.added)}")
        if result.missing:
            notes.append(f"{result.missing} fids not in the file")
        if result.inconsistent:
            notes.append(f"{result.inconsistent} features had differing values (first used)")
        if notes:
            self.report({"WARNING"}, f"{text}; " + "; ".join(notes))
        else:
            self.report({"INFO"}, text)
        redraw_ui(context)
        return {"FINISHED"}


EXPORT_FORMAT_ITEMS = (
    ("GPKG", "GeoPackage", "GeoPackage feature tables with Z, in the project CRS"),
    (
        "GEOJSON",
        "GeoJSON",
        (
            "GeoJSON with Z. In a projected project CRS this is outside RFC 7946 (which is "
            "WGS84 only): GeoTools writes the legacy crs member, which QGIS and GDAL read"
        ),
    ),
)
EXTENSIONS = {"GPKG": ".gpkg", "GEOJSON": ".geojson"}


class GEOTOOLS_OT_export_vector(bpy.types.Operator):
    """Export meshes (evaluated: modifiers applied) to GeoPackage or GeoJSON with Z, in the project CRS. Faces become polygons, loose edges lines, loose vertices points"""

    bl_idname = "geotools.export_vector"
    bl_label = "Export Vectors"
    bl_options = {"REGISTER"}

    filepath: StringProperty(subtype="FILE_PATH", options={"SKIP_SAVE"})
    filter_glob: StringProperty(default="*.gpkg;*.geojson;*.json", options={"HIDDEN"})
    file_format: EnumProperty(name="Format", items=EXPORT_FORMAT_ITEMS, default="GPKG")
    layer: StringProperty(
        name="Layer", description="Table name (and GeoJSON name); empty for the object's name"
    )
    overwrite: BoolProperty(
        name="Replace Table",
        description="Replace a GeoPackage table of the same name",
        default=True,
    )
    object_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _object_mode(cls, context)

    def _objects(self, context: bpy.types.Context) -> list[bpy.types.Object]:
        if self.object_name:
            obj = bpy.data.objects.get(self.object_name)
            return [obj] if obj is not None and obj.type == "MESH" else []
        selected = [o for o in context.selected_objects if o.type == "MESH"]
        if not selected and context.active_object is not None:
            selected = [context.active_object] if context.active_object.type == "MESH" else []
        return selected

    def invoke(self, context: bpy.types.Context, event: bpy.types.Event) -> set[str]:
        objects = self._objects(context)
        if not self.filepath:
            stem = objects[0].name if objects else "vectors"
            folder = Path(bpy.path.abspath("//")) if bpy.data.is_saved else Path.home()
            self.filepath = str(folder / (bpy.path.clean_name(stem) + EXTENSIONS[self.file_format]))
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def check(self, context: bpy.types.Context) -> bool:
        path = Path(self.filepath)
        ext = EXTENSIONS[self.file_format]
        if self.filepath and path.suffix.lower() != ext:
            self.filepath = str(path.with_suffix(ext))
            return True
        return False

    def execute(self, context: bpy.types.Context) -> set[str]:
        objects = self._objects(context)
        if not objects:
            self.report({"ERROR"}, "Select the mesh objects to export")
            return {"CANCELLED"}
        if not self.filepath:
            self.report({"ERROR"}, "No file chosen")
            return {"CANCELLED"}
        path = Path(bpy.path.abspath(self.filepath))
        layer = self.layer.strip() or (objects[0].name if len(objects) == 1 else path.stem)
        try:
            counts, warnings = export_vectors(
                context, objects, path, self.file_format, layer, self.overwrite
            )
        except (ImportRefused, ValueError, OSError) as ex:
            self.report({"ERROR"}, f"Export failed: {ex}")
            return {"CANCELLED"}
        parts = ", ".join(f"{n} {k.value}" for k, n in counts.items())
        text = f"Exported {parts} to {path.name}"
        self.report({"WARNING"} if warnings else {"INFO"}, " ".join([text, *warnings]))
        return {"FINISHED"}


# --- menus --------------------------------------------------------------------------


def _menu_import(self: bpy.types.Menu, context: bpy.types.Context) -> None:
    self.layout.operator(GEOTOOLS_OT_import_geojson.bl_idname, text="GeoJSON (.geojson)")
    self.layout.operator(GEOTOOLS_OT_import_gpkg.bl_idname, text="GeoPackage (.gpkg)")


def _menu_export(self: bpy.types.Menu, context: bpy.types.Context) -> None:
    op = self.layout.operator(GEOTOOLS_OT_export_vector.bl_idname, text="GeoPackage (.gpkg)")
    op.file_format = "GPKG"
    op = self.layout.operator(GEOTOOLS_OT_export_vector.bl_idname, text="GeoJSON (.geojson)")
    op.file_format = "GEOJSON"


classes = (
    GEOTOOLS_OT_import_geojson,
    GEOTOOLS_OT_import_gpkg,
    GEOTOOLS_OT_link_filters,
    GeoToolsWriteBackItem,
    GEOTOOLS_OT_link_write_back,
    GEOTOOLS_OT_export_vector,
)

_register_classes, _unregister_classes = bpy.utils.register_classes_factory(classes)


def register() -> None:
    _register_classes()
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)
    links.register_refresher("GEOJSON", refresh_vectors)
    links.register_refresher("GPKG", refresh_vectors)


def unregister() -> None:
    links.unregister_refresher("GPKG")
    links.unregister_refresher("GEOJSON")
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    _unregister_classes()
