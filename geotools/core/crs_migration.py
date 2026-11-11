"""MIGRATION ONLY: the PROJ strings of the CRS presets GeoTools used to offer.

Before rusty-tiles 0.4.1 resolved EPSG codes beyond WGS84, GeoTools stored the
GDA2020 MGA zones and NZTM2000 as PROJ strings with a null ``+towgs84`` shift.
The EPSG picker replaced them; this table only lets a ``.blend`` saved with one
of those strings move to the EPSG code it stood for
(``crs_catalog.migrated``, which also checks that rusty-tiles resolves the
code to this very definition, so the placement cannot change). Nothing offers
these strings any more; do not add to the table. No ``bpy``.
"""

from __future__ import annotations

_MGA2020 = "+proj=utm +zone={zone} +south +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
_NZTM2000 = (
    "+proj=tmerc +lat_0=0 +lon_0=173 +k=0.9996 +x_0=1600000 +y_0=10000000 "
    "+ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs"
)

PRESET_CODES: dict[str, str] = {
    **{_MGA2020.format(zone=zone): f"EPSG:{7800 + zone}" for zone in range(46, 60)},
    _NZTM2000: "EPSG:2193",
}
"""A saved preset's PROJ string -> the EPSG code it stood for (GDA2020 / MGA zone
46 to 59: EPSG:7846 to 7859; NZGD2000 / NZTM2000: EPSG:2193)."""


def preset_code(definition: str | None) -> str | None:
    """The EPSG code a saved preset's PROJ string stood for, or None for anything else."""
    return PRESET_CODES.get((definition or "").strip())
