"""Where the source sits on Earth, in rusty-tiles' own terms.

CONTRACT (M1). Implemented by the core agent; the Blender side codes against it.

Coordinates in Blender are Z-up: X = east, Y = north, Z = up. The GLB writer (M2)
always writes standard glTF Y-up, so rusty-tiles is always called with
``source_axes="y-up"``. ``blender_to_gltf`` is the single place that maps axes.

All numbers are Python floats (binary64). Never round-trip them through Blender
FloatProperty, which is single precision.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum

from .issues import Issue, Severity

Vec3 = tuple[float, float, float]


class PlacementMode(StrEnum):
    LOCAL = "local"  # local metres, no placement on Earth
    ANCHOR = "anchor"  # local metres placed rigidly at a WGS84 point
    SOURCE_CRS = "source_crs"  # coordinates in a declared horizontal CRS plus offset


# Keywords rusty-tiles refuses (its LEGACY_KEYWORDS, plus "web-mercator"), each with
# the explicit definition to suggest instead. Bare codes are handled separately.
_CRS_KEYWORDS: dict[str, str | None] = {
    "auto": None,
    "geographic": "EPSG:4326",
    "wgs84": "EPSG:4326",
    "webmercator": "EPSG:3857",
    "web-mercator": "EPSG:3857",
    "mercator": "EPSG:3857",
    "pseudo-mercator": "EPSG:3857",
    "pseudomercator": "EPSG:3857",
}
_BARE_CODE = re.compile(r"[0-9]+")


@dataclass(frozen=True, slots=True)
class Placement:
    mode: PlacementMode = PlacementMode.LOCAL
    # ANCHOR mode
    anchor: Vec3 = (0.0, 0.0, 0.0)  # lon deg, lat deg, ellipsoidal height m
    heading_degrees: float = 0.0  # rotation about local up, counter-clockwise
    scene_offset: Vec3 = (0.0, 0.0, 0.0)  # metres, Blender Z-up (E, N, U)
    # SOURCE_CRS mode
    source_crs: str = ""  # "EPSG:32756", WKT or PROJ; never a bare code or keyword
    source_offset: Vec3 = (0.0, 0.0, 0.0)  # E, N in CRS units; A in metres
    height_offset: float = 0.0  # metres added to height+A to get ellipsoidal height

    def validate(self) -> list[Issue]:
        """Mirror rusty-tiles' request rules so errors show before a build."""
        mode = PlacementMode(self.mode)
        if mode is PlacementMode.LOCAL:
            return []  # local output ignores every anchor and CRS field
        if mode is PlacementMode.ANCHOR:
            issues = _finite_issues(
                ("anchor", self.anchor, 3),
                ("heading", (self.heading_degrees,), 1),
                ("scene offset", self.scene_offset, 3),
            )
            if _all_finite(self.anchor, 3):
                issues += _anchor_range_issues(self.anchor)
            return issues
        return _crs_issues(self.source_crs) + _finite_issues(
            ("source offset", self.source_offset, 3),
            ("height offset", (self.height_offset,), 1),
        )

    def mesh_to_tileset_kwargs(self) -> dict[str, object]:
        """Exact placement keywords for ``rusty_tiles.mesh_to_tileset``.

        LOCAL -> {}; ANCHOR -> anchor, orientation_xyzw, scene_offset (glTF Y-up);
        SOURCE_CRS -> source_crs, source_axes="y-up", source_offset, height_offset.
        Raises ValueError if ``validate()`` reports any error.
        """
        errors = [i.message for i in self.validate() if i.severity is Severity.ERROR]
        if errors:
            raise ValueError(" ".join(errors))
        mode = PlacementMode(self.mode)
        if mode is PlacementMode.LOCAL:
            return {}
        if mode is PlacementMode.ANCHOR:
            return {
                "anchor": _vec3(self.anchor),
                "orientation_xyzw": heading_quaternion_xyzw(float(self.heading_degrees)),
                "scene_offset": blender_to_gltf(_vec3(self.scene_offset)),
            }
        return {
            "source_crs": self.source_crs.strip(),
            "source_axes": "y-up",
            "source_offset": _vec3(self.source_offset),
            "height_offset": float(self.height_offset),
        }


def blender_to_gltf(v: Vec3) -> Vec3:
    """Blender Z-up (x, y, z) to glTF Y-up (x, z, -y)."""
    x, y, z = v
    # 0.0 - y keeps a zero northing as +0.0 rather than -0.0.
    return (float(x), float(z), 0.0 - float(y))


def heading_quaternion_xyzw(heading_degrees: float) -> tuple[float, float, float, float]:
    """Active rotation about ENU up by ``heading_degrees``, scalar W last.

    Positive headings turn counter-clockwise seen from above, so +90 turns the
    source's east axis onto north. Checked against rusty-tiles' root transform.
    """
    half = math.radians(math.remainder(float(heading_degrees), 360.0)) / 2.0
    return (0.0, 0.0, math.sin(half), math.cos(half))


def _vec3(v: Vec3) -> Vec3:
    x, y, z = v
    return (float(x), float(y), float(z))


def _all_finite(values: object, length: int) -> bool:
    try:
        numbers = [float(v) for v in values]  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return False
    return len(numbers) == length and all(math.isfinite(v) for v in numbers)


def _finite_issues(*fields: tuple[str, object, int]) -> list[Issue]:
    return [
        Issue(
            Severity.ERROR,
            "placement-not-finite",
            f"The {name} must be {'a finite number' if length == 1 else 'finite numbers'}.",
        )
        for name, values, length in fields
        if not _all_finite(values, length)
    ]


def _anchor_range_issues(anchor: Vec3) -> list[Issue]:
    lon, lat, _height = (float(v) for v in anchor)
    issues = []
    if not -180.0 <= lon <= 180.0:
        issues.append(
            Issue(
                Severity.ERROR,
                "anchor-longitude-range",
                f"The anchor longitude {lon:g} is outside -180 to 180 degrees.",
            )
        )
    if not -90.0 <= lat <= 90.0:
        issues.append(
            Issue(
                Severity.ERROR,
                "anchor-latitude-range",
                f"The anchor latitude {lat:g} is outside -90 to 90 degrees.",
            )
        )
    return issues


def _crs_issues(source_crs: str) -> list[Issue]:
    definition = (source_crs or "").strip()
    if not definition or definition.lower() == "local":
        return [
            Issue(
                Severity.ERROR,
                "crs-missing",
                "Name the source CRS, for example EPSG:32756, or use local placement.",
            )
        ]
    if _BARE_CODE.fullmatch(definition):
        return [
            Issue(
                Severity.ERROR,
                "crs-bare-code",
                f'Write the source CRS as "EPSG:{definition}", not a bare code.',
            )
        ]
    keyword = definition.lower()
    if keyword in _CRS_KEYWORDS:
        suggestion = _CRS_KEYWORDS[keyword]
        hint = f", for example {suggestion}" if suggestion else ""
        return [
            Issue(
                Severity.ERROR,
                "crs-keyword",
                f'"{definition}" is not accepted; name the source CRS explicitly{hint}.',
            )
        ]
    return []
