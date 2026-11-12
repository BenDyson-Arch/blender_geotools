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

See docs/design/m8-geo-formats.md ("LOCAL and ANCHOR projects"). No ``bpy``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import crs_catalog
from .crs_match import DataCrs, is_geographic
from .placement import PlacementMode

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
