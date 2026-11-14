"""Vector publish, Blender side (M8): a build profile of format Vectors.

The profile names a collection; each mesh object in it (imported or linked
GeoJSON and GeoPackage, draped by modifiers or not) is read the way Export
Vectors reads it, from the evaluated mesh unless the profile's Apply Modifiers
is off, into the project CRS with the project offset added back in binary64.
Faces become polygons, loose edges lines and loose vertices points, one
GeoPackage table per object and kind (``publish.vector.table_names``).

``geotools.publish`` (``blender/publish.py``) calls :func:`prepare` on the main
thread and hands the result to a ``BuildJob``, which writes the GeoPackage and
runs ``vector_to_3tz`` on its worker thread (``publish/vector.py``). The
progress, cancel, build record and Last Build UI are the mesh publish's.

No modelling happens here: draping and extruding are Geometry Nodes recipes
(docs/recipes/drape-contexts.md), and the publish reads their result.
"""

from __future__ import annotations

from dataclasses import dataclass

import bpy

from ..core import crs_catalog
from ..core.issues import Issue, Severity
from ..core.vector.model import Kind
from ..publish import vector as pv
from . import vectors
from .ui import wrapped


class VectorPublishError(ValueError):
    """The vector collection cannot be published as it is."""


def is_vector(profile: bpy.types.PropertyGroup | None) -> bool:
    return profile is not None and getattr(profile, "output_type", "3TZ") == "VECTOR"


def vector_objects(profile: bpy.types.PropertyGroup) -> list[bpy.types.Object]:
    """The mesh objects of the profile's vector collection (and its children), by name."""
    collection = profile.vector_collection
    if collection is None:
        return []
    return sorted((o for o in collection.all_objects if o.type == "MESH"), key=lambda o: o.name)


def vector_issues(context: bpy.types.Context, profile: bpy.types.PropertyGroup) -> list[Issue]:
    """Why the profile cannot publish now (errors), checked before anything runs."""
    issues: list[Issue] = []
    if profile.vector_collection is None:
        issues.append(
            Issue(
                Severity.ERROR,
                "vector-no-collection",
                "Choose the collection of vector objects to publish (Vectors, in Publish).",
            )
        )
    elif not vector_objects(profile):
        issues.append(
            Issue(
                Severity.ERROR,
                "vector-no-objects",
                f"The collection {profile.vector_collection.name} has no mesh objects to publish.",
            )
        )
    try:
        frame = vectors.project_frame(context.scene)
        pv.placement_kwargs(frame.placement)
    except (vectors.ImportRefused, ValueError) as ex:
        issues.append(Issue(Severity.ERROR, "vector-placement", str(ex)))
    else:
        if frame.project is not None:
            refused = crs_catalog.check_issues(frame.placement.source_crs)
            issues += [i for i in refused if i.severity == Severity.ERROR]
    return issues


@dataclass(frozen=True, slots=True)
class PreparedVectors:
    source: pv.VectorSource
    placement_kwargs: dict[str, object]
    objects: list[str]
    warnings: list[str]


def prepare(context: bpy.types.Context, profile: bpy.types.PropertyGroup) -> PreparedVectors:
    """Read the profile's vector objects into a :class:`~publish.vector.VectorSource`.

    Main thread only. Raises :class:`VectorPublishError` with a message for the
    user when there is nothing to publish or the placement cannot be used.
    """
    try:
        frame = vectors.project_frame(context.scene)
        placement = pv.placement_kwargs(frame.placement)
    except (vectors.ImportRefused, ValueError) as ex:
        raise VectorPublishError(str(ex)) from None
    objects = vector_objects(profile)
    if not objects:
        raise VectorPublishError("There are no mesh objects in the vector collection")

    from .export import _sync_edit_mode  # the mesh publish's edit-mode rule

    _sync_edit_mode(context, objects)
    apply = bool(profile.apply_modifiers)
    depsgraph = context.evaluated_depsgraph_get()
    pieces: list[tuple[str, Kind, object]] = []
    empty: list[str] = []
    for obj in objects:
        datas = vectors.mesh_input(obj, depsgraph, frame.offset, apply_modifiers=apply)
        if not datas:
            empty.append(obj.name)
        for data in datas:
            data = vectors.source_names(obj, data)
            (kind,) = data.kinds()
            pieces.append((obj.name, kind, data))
    if not pieces:
        raise VectorPublishError(
            "The vector objects have no faces, edges or vertices to publish (a closed "
            "volume has no outline: publish volumes with a .3tz mesh profile)"
        )
    tables = pv.table_names([(name, kind) for name, kind, _data in pieces])
    layers = tuple(
        pv.VectorLayer(table=table, data=data, kind=kind, object_name=name)  # type: ignore[arg-type]
        for table, (name, kind, data) in zip(tables, pieces, strict=True)
    )
    options: dict[str, object] = {
        **placement,
        "jobs": profile.jobs or None,
        "skip_invalid": bool(profile.vector_skip_invalid),
        # Byte-identical archives for the same features (no timings in the report).
        "reproducible": True,
    }
    source = pv.VectorSource(
        layers=layers, crs=frame.project, options=options, reuse=bool(profile.vector_reuse)
    )
    warnings = list(frame.warnings)
    if empty:
        warnings.append(f"Nothing to publish in {', '.join(empty)}")
    return PreparedVectors(
        source=source,
        placement_kwargs=placement,
        objects=[o.name for o in objects],
        warnings=warnings,
    )


# --- drawing (called from panels.py) ---------------------------------------------------


def draw_profile(
    layout: bpy.types.UILayout, context: bpy.types.Context, profile: bpy.types.PropertyGroup
) -> None:
    """The vector collection, in the Publish panel."""
    row = layout.row()
    row.alert = profile.vector_collection is None
    row.prop(profile, "vector_collection", icon="OUTLINER_COLLECTION")
    if profile.vector_collection is not None and not vector_objects(profile):
        layout.label(text="No mesh objects in it", icon="ERROR")


def draw_settings(
    layout: bpy.types.UILayout, context: bpy.types.Context, profile: bpy.types.PropertyGroup
) -> None:
    """Vector settings, in Build Settings."""
    layout.prop(profile, "vector_reuse")
    layout.prop(profile, "vector_skip_invalid")
    layout.prop(profile, "jobs")
    if context.scene is not None and context.scene.geotools.placement_mode == "ANCHOR":
        wrapped(layout, context, "Vectors need a Source CRS (or Local) project.", "ERROR")
