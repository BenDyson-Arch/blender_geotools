"""Projected data imported into a LOCAL or ANCHOR project, and the data's centre as origin.

A LOCAL or ANCHOR project takes imported coordinates as local metres. That is
fine for data already in small local coordinates, and for data whose CRS is
unknown (with a warning), but projected data with large coordinates (UTM, MGA:
hundreds of kilometres from the origin) would land far from the scene and lose
float32 precision. Such an import is refused, and the refusal carries a
:class:`DataOrigin`: the data's CRS and the centre of its extent, which the
Blender glue offers as a one-click "Use this data's centre as the project
origin". That switches the project to SOURCE_CRS:

- the CRS is the data's: ``EPSG:nnnn`` (or the data's definition when it has
  no code), and the project checks then say whether rusty-tiles supports it;
- the E/N offset is the centre of the data's extent, rounded to a whole metre;
- the A offset is 0 for a LOCAL or ANCHOR project that becomes a CRS project
  (heights stay absolute, as BlenderGIS and other GeoScene tools read them);
  a CRS project keeps its own A.

A SOURCE_CRS project with no CRS yet is offered the data's CRS the same way
(:func:`offer_project_crs`): the CRS only, when the project already has an
offset, else the CRS and the data's centre.

An offset file found beside an imported source mesh (:func:`check_offset_file`)
sets the project origin only when the project has none yet, or already has
the same one. Otherwise the mesh is imported unshifted and the file is
offered as the origin, never applied behind the user's back.

See docs/design/m8-geo-formats.md ("LOCAL and ANCHOR projects"). No ``bpy``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from . import crs_catalog
from .crs_match import DataCrs, is_geographic
from .geoscene import GeoScene
from .placement import PlacementMode, Vec3

LOCAL_RADIUS_M = 100_000.0
"""Data whose extent stays within this distance of the origin counts as local metres."""

Bounds = tuple[float, float, float, float]
"""min x, min y, max x, max y in the data's coordinates."""


@dataclass(frozen=True, slots=True)
class DataOrigin:
    """A SOURCE_CRS placement made from one dataset: its CRS and its centre."""

    source_crs: str
    """What the project's CRS becomes (``EPSG:nnnn``, or WKT or PROJ text)."""
    epsg: str | None
    crs_name: str | None
    """The CRS's name from the EPSG catalogue (``GDA2020 / MGA zone 56``), when known."""
    offset: tuple[float, float, float]
    """(E, N, 0.0): the extent's centre in whole metres. The 0.0 is never applied."""
    set_offset: bool = True
    """False to set only the CRS and keep the project's offset."""

    def describe(self) -> str:
        """``"EPSG:7856 (GDA2020 / MGA zone 56), offset E 502000, N 6961000"``.

        Offset A is not part of a data origin: a new CRS project starts at 0, an
        existing one keeps its own.
        """
        crs = self.crs_text()
        if not self.set_offset:  # "EPSG:7856 (...), keeping the project's offset"
            return f"{crs}, keeping the project's offset"
        e, n, _a = self.offset
        return f"{crs}, offset E {e:.0f}, N {n:.0f}"

    def crs_text(self) -> str:
        """``"EPSG:7856 (GDA2020 / MGA zone 56)"``, or the start of the definition."""
        if self.crs_name:
            crs = f"{self.epsg} ({self.crs_name})"
        elif self.epsg:
            crs = self.epsg
        else:
            flat = " ".join(self.source_crs.split())
            crs = flat if len(flat) <= 40 else flat[:37] + "..."
        return crs

    def to_dict(self) -> dict[str, object]:
        return {
            "source_crs": self.source_crs,
            "epsg": self.epsg,
            "crs_name": self.crs_name,
            "offset": list(self.offset),
            "set_offset": self.set_offset,
        }

    @classmethod
    def from_dict(cls, raw: object) -> DataOrigin | None:
        if not isinstance(raw, dict):
            return None
        try:
            e, n, a = (float(v) for v in raw["offset"])
            return cls(
                source_crs=str(raw["source_crs"]),
                epsg=raw.get("epsg") or None,
                crs_name=raw.get("crs_name") or None,
                offset=(e, n, a),
                set_offset=bool(raw.get("set_offset", True)),
            )
        except (KeyError, TypeError, ValueError):
            return None


class NeedsProjectOrigin(ValueError):
    """Projected data cannot go into a LOCAL or ANCHOR project as it is.

    ``origin`` is the placement that would take it: the data's CRS and centre.
    """

    def __init__(self, message: str, origin: DataOrigin) -> None:
        super().__init__(message)
        self.origin = origin


def farthest(bounds: Bounds) -> float:
    """Distance from the origin to the farthest corner of ``bounds``, in data units."""
    x0, y0, x1, y1 = bounds
    return max(math.hypot(x, y) for x in (x0, x1) for y in (y0, y1))


def is_local(bounds: Bounds) -> bool:
    """True if the whole extent lies within :data:`LOCAL_RADIUS_M` of the origin."""
    return farthest(bounds) <= LOCAL_RADIUS_M


def project_definition(data_crs: DataCrs) -> str:
    """What the project's CRS becomes for data in ``data_crs``: ``EPSG:nnnn`` or its text."""
    if data_crs.epsg:
        return data_crs.epsg
    return (data_crs.definition or "").strip()


def suggest_origin(data_crs: DataCrs, bounds: Bounds) -> DataOrigin:
    """The SOURCE_CRS placement for this data: its CRS and the centre of its extent."""
    source_crs = project_definition(data_crs)
    label = crs_catalog.name(source_crs) or None
    x0, y0, x1, y1 = bounds
    centre = (float(round((x0 + x1) / 2.0)), float(round((y0 + y1) / 2.0)), 0.0)
    return DataOrigin(source_crs, data_crs.epsg, label, centre)


def offer_project_crs(
    data_crs: DataCrs | None,
    bounds: Bounds | None,
    project_offset: tuple[float, float, float],
    what: str,
) -> None:
    """For a SOURCE_CRS project with no CRS: raise :class:`NeedsProjectOrigin` offering
    the data's CRS, or return when there is nothing to offer.

    Offered only for data in a known CRS that is not geographic (a geographic
    project CRS reads Blender metres as degrees). A project offset of zero E and N also
    gets the data's centre, as for a LOCAL project; any other offset is kept (as
    is a zero offset when there is no extent to centre on).
    """
    if data_crs is None or not data_crs.known or is_geographic(data_crs) is True:
        return
    keep = any(float(v) != 0.0 for v in project_offset[:2])
    if keep or bounds is None or not all(math.isfinite(v) for v in bounds):
        source_crs = project_definition(data_crs)
        label = crs_catalog.name(source_crs) or None
        offset = tuple(float(v) for v in project_offset)
        origin = DataOrigin(source_crs, data_crs.epsg, label, offset, set_offset=False)  # type: ignore[arg-type]
    else:
        origin = suggest_origin(data_crs, bounds)
    raise NeedsProjectOrigin(
        f"The project has no CRS yet, and {what} is in {data_crs.describe()}. Use the data's "
        f"CRS for the project (the project then uses {origin.describe()}), or set the "
        "project's CRS yourself.",
        origin,
    )


def check_local_project(
    mode: PlacementMode, data_crs: DataCrs | None, bounds: Bounds | None, what: str
) -> str:
    """The warning for importing ``what`` into a LOCAL or ANCHOR project.

    Raises :class:`NeedsProjectOrigin` when the data is in a known, not
    geographic, CRS and its extent reaches beyond :data:`LOCAL_RADIUS_M`.
    Otherwise the coordinates are used as local metres and the warning says so.
    """
    kind = "Local" if PlacementMode(mode) is PlacementMode.LOCAL else "Anchor"
    crs_text = data_crs.describe() if data_crs is not None else "no CRS"
    if (
        data_crs is not None
        and data_crs.known
        and bounds is not None
        and all(math.isfinite(v) for v in bounds)
        and not is_local(bounds)
        and is_geographic(data_crs) is not True
    ):
        origin = suggest_origin(data_crs, bounds)
        distance = farthest(bounds) / 1000.0
        raise NeedsProjectOrigin(
            f"{what} is in {crs_text}, with coordinates up to {distance:,.0f} km from the "
            f"origin, but this is {'a Local' if kind == 'Local' else 'an Anchor'} project, which takes coordinates as local metres. "
            f"Use this data's centre as the project origin (the project then uses "
            f"{origin.describe()}), or set the project's CRS and offset yourself.",
            origin,
        )
    return (
        f"{kind} project: {what}'s coordinates ({crs_text}) are used as local metres, "
        "unshifted. Set the project to a CRS with an offset to place it on Earth."
    )


# --- an offset file beside an imported source mesh ------------------------------------------


class OffsetAction(StrEnum):
    APPLY = "apply"
    """The project has no origin yet: the file becomes it (and the project a CRS project)."""
    MATCH = "match"
    """The project already uses exactly this offset: nothing changes, nothing is said."""
    OFFER = "offer"
    """The project has another origin: import unshifted and offer the file."""


Origin3 = tuple[float | None, float | None, float | None]
"""The project's E, N and A; None where a field holds no number (or E/N have no key)."""

FAR_LOCATION_STEP_M = 0.001
"""Object locations whose single-precision step exceeds this get a warning (from 16,384 m)."""


@dataclass(frozen=True, slots=True)
class OffsetFileCheck:
    """What :func:`check_offset_file` decided about one offset file."""

    action: OffsetAction
    offset: Vec3
    """The file's E, N, A."""
    project: Origin3
    """The project's E, N, A when the file was read."""
    reason: str = ""
    """For an offer: what the project already has, as a clause."""
    shift: Vec3 | None = None
    """For an offer in a CRS project with a numeric origin: file minus project, the
    float64 move that places objects imported against the file at the project origin."""

    def message(self, name: str) -> str:
        """The import report and the Source panel's text for an offer."""
        e, n, a = self.offset
        place = (
            ", place the imported objects at the project origin" if self.shift is not None else ""
        )
        return (
            f"{name} holds E {_num(e)}, N {_num(n)}, A {_num(a)}, but {self.reason}. The mesh "
            "was imported unshifted, so its coordinates are relative to the file's offset, and "
            f"the project origin was left as it is. Use this offset file as the project origin{place}, "
            "or set the origin yourself."
        )


def _num(value: float | None) -> str:
    if value is None:
        return "not set"
    return f"{value:.0f}" if float(value).is_integer() else repr(float(value))


def _origin_text(origin: Origin3) -> str:
    e, n, a = origin
    return f"E {_num(e)}, N {_num(n)}, A {_num(a)}"


def _is_zero(value: float | None) -> bool:
    return value is None or value == 0.0


def check_offset_file(
    mode: PlacementMode | str, scene: GeoScene, project: Origin3, offset: Vec3
) -> OffsetFileCheck:
    """Whether an offset file found beside an imported mesh may set the project origin.

    ``scene`` is the scene's GeoScene keys and ``project`` GeoTools' E, N and A
    (in a CRS project with live keys, E and N are ``crs x``/``crs y``).
    Comparisons are exact, in float64, A included.

    - **Match:** a CRS project whose E, N and A are the file's. Several chunks
      sharing one offset file, or a chunk imported again, change nothing.
    - **Apply** when the project has no origin yet: a Local project with no
      GeoScene keys and E, N, A all 0, or a CRS project whose E, N and A are 0
      (or E/N not set) and whose scene has no geographic origin. Also a Local
      project whose complete GeoScene keys are the file's E/N (the file only
      adds A), or whose E, N, A from before are the file's.
    - **Offer** otherwise: (a) complete GeoScene keys with another ``crs x``/
      ``crs y``; (b) a CRS project with another non-zero origin; an Anchor
      project (never silently switched to a CRS project); a Local project
      that holds other keys or an E/N/A from before; a scene with only a
      latitude/longitude origin.
    """
    mode = PlacementMode(str(getattr(mode, "value", mode)).lower())
    file = (float(offset[0]), float(offset[1]), float(offset[2]))
    project = tuple(None if v is None else float(v) for v in project)  # type: ignore[assignment]

    def offer(reason: str, shift: Vec3 | None = None) -> OffsetFileCheck:
        return OffsetFileCheck(OffsetAction.OFFER, file, project, reason, shift)

    if mode is PlacementMode.ANCHOR:
        return offer("this is an Anchor project, which the file would make a CRS project")
    if mode is PlacementMode.SOURCE_CRS:
        if project == file:
            return OffsetFileCheck(OffsetAction.MATCH, file, project)
        if scene.latitude is not None and scene.longitude is not None and not scene.has_origin:
            return offer(
                f"the scene has a geographic origin (latitude {scene.latitude:.7f}°, "
                f"longitude {scene.longitude:.7f}°) and no E/N"
            )
        if all(_is_zero(v) for v in project):
            return OffsetFileCheck(OffsetAction.APPLY, file, project)
        shift = None
        if all(v is not None for v in project):
            shift = tuple(f - p for f, p in zip(file, project, strict=True))  # type: ignore[operator]
        return offer(f"the project's origin is {_origin_text(project)}", shift)  # type: ignore[arg-type]
    # LOCAL
    if scene.complete:
        if (scene.x, scene.y) == file[:2]:
            return OffsetFileCheck(OffsetAction.APPLY, file, project)
        return offer(f"the scene has a BlenderGIS (GeoScene) georeference ({scene.describe()})")
    if scene.present:
        return offer(f"the scene has BlenderGIS (GeoScene) keys ({scene.describe()})")
    if not all(_is_zero(v) for v in project) and project != file:
        return offer(f"this Local project holds the origin {_origin_text(project)} from before")
    return OffsetFileCheck(OffsetAction.APPLY, file, project)


def float32_step(value: float) -> float:
    """The spacing of single-precision floats at ``value``: how finely Blender keeps it."""
    value = abs(float(value))
    if value == 0.0 or not math.isfinite(value):
        return 0.0
    _mantissa, exponent = math.frexp(value)  # value = m * 2**exponent, 0.5 <= m < 1
    return math.ldexp(1.0, max(exponent - 24, -149))
