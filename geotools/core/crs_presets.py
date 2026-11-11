"""CRS presets the bundled (portable) rusty-tiles accepts today.

rusty-tiles' wheel only resolves EPSG codes for WGS84 geographic, WGS84 UTM and
Mercator. Other projected CRSs work when written as a PROJ definition with an
explicit datum shift (``+towgs84``). rusty-tiles applies exactly the declared
transform and claims nothing about datum accuracy, so each preset states its
assumption.

A GDA2020-based CRS is close to, but not the same as, current WGS84: GDA2020 is
ITRF2014 at epoch 2020.0 and the Australian plate moves about 7 cm a year, so a
null shift is about 0.5 m from current WGS84 positions and growing. That is
EPSG's published GDA2020 to WGS84 null operation, which GDAL also uses.

When rusty-tiles 0.4.x ships its verified EPSG allow-list, these presets should
switch back to plain EPSG codes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CrsPreset:
    id: str  # stable identifier for UI enums
    label: str
    definition: str  # what is passed to rusty-tiles as source_crs
    epsg: str  # the EPSG code it stands in for
    note: str  # datum assumption and approximate accuracy against current WGS84


_GDA2020_NOTE = (
    "GDA2020 treated as WGS84 (null shift): about 0.5 m from current WGS84 "
    "positions, growing about 7 cm a year."
)


def _mga2020(zone: int) -> CrsPreset:
    return CrsPreset(
        id=f"MGA2020_{zone}",
        label=f"GDA2020 / MGA zone {zone}",
        definition=(
            f"+proj=utm +zone={zone} +south +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
        ),
        epsg=f"EPSG:{7800 + zone}",
        note=_GDA2020_NOTE,
    )


PRESETS: tuple[CrsPreset, ...] = (
    *(_mga2020(zone) for zone in range(46, 60)),
    CrsPreset(
        id="NZTM2000",
        label="NZGD2000 / New Zealand Transverse Mercator 2000",
        definition=(
            "+proj=tmerc +lat_0=0 +lon_0=173 +k=0.9996 +x_0=1600000 +y_0=10000000 "
            "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
        ),
        epsg="EPSG:2193",
        note=(
            "NZGD2000 treated as WGS84 (null shift), as EPSG's published null "
            "operation does; expect metre-level difference from current WGS84."
        ),
    ),
)

_BY_ID = {preset.id: preset for preset in PRESETS}
_BY_DEFINITION = {preset.definition: preset for preset in PRESETS}


def get(preset_id: str) -> CrsPreset | None:
    return _BY_ID.get(preset_id)


def for_definition(definition: str) -> CrsPreset | None:
    """The preset whose definition is exactly ``definition``, if any."""
    return _BY_DEFINITION.get(definition.strip())
