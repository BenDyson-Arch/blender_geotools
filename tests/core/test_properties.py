"""Property-based tests (hypothesis) of the invariants the core relies on.

Synthetic inputs only. The settings profiles are in ``conftest.py``: CI runs a
few derandomised examples per property; ``GEOTOOLS_HYPOTHESIS_PROFILE=thorough``
runs many random ones. A failure prints the shrunk example: add it as a
regression test beside the module's own tests.

Intervisibility symmetry is checked in the Blender suite
(``tests/blender/test_analysis_presets_2.py``): the Intervisibility preset is a
Geometry Nodes group, with no bpy-free code to test here.
"""

from __future__ import annotations

import math
import ntpath
import posixpath
import re
import struct
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp

from geotools.analysis import hydrology as hy
from geotools.analysis import slicing
from geotools.core import geoscene, offsets, paths
from geotools.core.facemap import build_face_map, load_face_map, save_face_map
from geotools.core.gltf_writer import PrimitiveData, SceneExport
from geotools.core.inspect import selected_rows, source_faces, tile_face_mask

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tests" / "blender"))
import _hydrology_surfaces as hs  # numpy only, shared with the Blender suite

finite = st.floats(allow_nan=False, allow_infinity=False)


# --- E/N <-> latitude/longitude ------------------------------------------------------------

# CRSs rusty-tiles 0.4.1 accepts (``crs_catalog.supported_codes``), of several
# projection methods and datums: transverse Mercator (GDA2020 MGA, NZTM2000, WGS 84
# UTM, GDA94 MGA, ETRS89 UTM, NAD83 UTM), Lambert conformal conic (Lambert-93,
# GDA2020 Lambert), Web Mercator and polar stereographic.
CRS_CODES = (
    *(f"EPSG:{7800 + zone}" for zone in (46, 50, 55, 56, 59)),
    "EPSG:2193",
    "EPSG:32633",
    "EPSG:32756",
    "EPSG:28356",
    "EPSG:25832",
    "EPSG:26915",
    "EPSG:2154",
    "EPSG:7845",
    "EPSG:3857",
    "EPSG:3031",
    "EPSG:3413",
)

ROUND_TRIP_M = 1e-3
"""A synced position round-trips to well under a millimetre (``geoscene.LAT_LON_TOLERANCE_M``)."""


def _area_of_use(code: str) -> tuple[float, float, float, float]:
    pyproj = pytest.importorskip("pyproj")
    area = pyproj.CRS.from_user_input(code).area_of_use
    return area.west, area.south, area.east, area.north


@given(
    code=st.sampled_from(CRS_CODES),
    u=st.floats(0.0, 1.0),
    v=st.floats(0.0, 1.0),
)
def test_en_lat_lon_round_trip(code: str, u: float, v: float) -> None:
    """Any site in a supported CRS's area of use goes E/N -> lat/lon -> E/N, and lat/lon
    -> E/N -> lat/lon, within a millimetre, with or without the rusty-tiles wheel."""
    west, south, east, north = _area_of_use(code)
    if west > east:  # across the antimeridian
        east += 360.0
    lon = west + u * (east - west)
    lon = lon - 360.0 if lon > 180.0 else lon
    lat = south + v * (north - south)
    en = geoscene.lon_lat_to_origin(code, lon, lat)
    assert en is not None
    lon_lat = geoscene.origin_lon_lat(code, *en)
    assert lon_lat is not None
    assert geoscene.ground_distance(lon, lat, *lon_lat) <= ROUND_TRIP_M
    back = geoscene.lon_lat_to_origin(code, *lon_lat)
    assert back is not None
    assert math.dist(en, back) <= ROUND_TRIP_M


# --- offset files --------------------------------------------------------------------------


def _bits(x: float) -> bytes:
    return struct.pack("<d", x)


@given(
    values=st.tuples(finite, finite, finite),
    spelling=st.sampled_from([repr, lambda x: format(x, ".17g"), lambda x: format(x, ".17e")]),
    ending=st.sampled_from(["\n", "\r\n"]),
    final=st.booleans(),
)
def test_offset_file_round_trip(values, spelling, ending: str, final: bool) -> None:
    """Any binary64 E/N/A written with round-trip digits reads back bit for bit."""
    text = ending.join(f"{key}: {spelling(x)}" for key, x in zip("ENA", values, strict=True))
    text += ending if final else ""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "site_offset.txt"
        path.write_bytes(text.encode("utf-8"))
        read = offsets.read_offset_file(path)
    assert [_bits(x) for x in read] == [_bits(x) for x in values]


# --- portable paths ------------------------------------------------------------------------

# Names Windows allows (no reserved characters, no trailing dot or space) and, for
# POSIX, anything but "/", NUL and "\": in a // path "\" is a separator on every OS
# (the paths module's rule), so a POSIX file name with a backslash cannot be stored
# relative. "." and ".." steps are mixed in.
_NAME_CHARS = st.characters(
    codec="utf-8", exclude_categories=("Cs", "Cc"), exclude_characters='<>:"/\\|?*'
)
_WIN_NAME = st.text(_NAME_CHARS, min_size=1, max_size=8).filter(
    lambda s: s.rstrip(". ") == s and s not in (".", "..")
)
_POSIX_NAME = st.text(
    st.characters(codec="utf-8", exclude_categories=("Cs",), exclude_characters="/\\\0"),
    min_size=1,
    max_size=8,
).filter(lambda s: s not in (".", ".."))
_STEP = st.sampled_from([".", ".."])


def _lexical(module: ModuleType) -> ModuleType:
    """A copy of ``ntpath`` or ``posixpath`` that is not ``os.path``, so ``portable`` never
    falls back to ``realpath``: no disk or network lookup (a UNC server on Windows)."""
    copy = ModuleType(module.__name__)
    copy.__dict__.update(vars(module))
    return copy


NT, POSIX = _lexical(ntpath), _lexical(posixpath)


def _parts(name: st.SearchStrategy[str], max_size: int = 4) -> st.SearchStrategy[list[str]]:
    return st.lists(st.one_of(name, name, name, _STEP), max_size=max_size)


def _join(parts: list[str], seps: list[str]) -> str:
    return "".join(sep + part for sep, part in zip(seps, parts, strict=False))


@st.composite
def windows_folder_and_path(draw) -> tuple[str, str]:
    """An absolute folder and an absolute path on a drive or a UNC share, in any spelling."""
    sep = st.sampled_from(["\\", "/", "\\\\", "/\\"])
    if draw(st.booleans()):
        letter = draw(st.sampled_from("CcDdZz"))
        anchor = f"{letter}:"
        other = f"{draw(st.sampled_from('CcDdZz'))}:"
    else:
        server, share = draw(_WIN_NAME), draw(_WIN_NAME)
        anchor = f"\\\\{server}\\{share}"
        other = f"\\\\{draw(st.sampled_from([server, server.upper(), 'nas']))}\\{share}"
    folder_parts = draw(st.lists(_WIN_NAME, max_size=3))
    folder = anchor + _join(folder_parts, [draw(sep) for _ in folder_parts])
    # The path: under the folder (any case), or under another anchor.
    base = draw(st.sampled_from([folder, folder.upper(), folder.lower(), other])) + "\\"
    rest = draw(_parts(_WIN_NAME))
    path = base + _join(rest, [""] + [draw(sep) for _ in rest[1:]])
    return (folder if folder_parts else anchor + "\\"), path


@st.composite
def posix_folder_and_path(draw) -> tuple[str, str]:
    """An absolute folder and an absolute path (one leading "/": "//" is Blender's prefix)."""
    sep = st.sampled_from(["/", "//"])
    folder_parts = draw(st.lists(_POSIX_NAME, max_size=3))
    folder = "/" + "/".join(folder_parts)
    base = draw(st.sampled_from([folder, "/other" + folder]))
    rest = draw(_parts(_POSIX_NAME))
    path = base.rstrip("/") + "/" + _join(rest, [""] + [draw(sep) for _ in rest[1:]])
    return folder, path


def _same(a: str, b: str, pathmod: ModuleType) -> bool:
    return pathmod.normcase(pathmod.normpath(a)) == pathmod.normcase(pathmod.normpath(b))


def _check_portable(folder: str, path: str, pathmod: ModuleType) -> None:
    """``portable`` then ``resolve`` names the same file; under the folder it is a // path."""
    stored = paths.portable(path, folder, pathmod=pathmod)
    assert _same(paths.resolve(stored, folder, pathmod=pathmod), path, pathmod), stored
    if paths.relative_to(path, folder, pathmod=pathmod) is not None:
        assert stored.startswith("//") and "\\" not in stored
    # Storing is idempotent: the stored text stores as itself.
    assert paths.portable(stored, folder, pathmod=pathmod) == stored


@given(windows_folder_and_path())
def test_portable_round_trip_windows(case: tuple[str, str]) -> None:
    _check_portable(*case, NT)


@given(posix_folder_and_path())
def test_portable_round_trip_posix(case: tuple[str, str]) -> None:
    _check_portable(*case, POSIX)


@given(
    pathmod=st.sampled_from([NT, POSIX]),
    parts=_parts(_WIN_NAME, max_size=5),
    seps=st.lists(st.sampled_from(["/", "\\", "//", "\\/"]), min_size=5, max_size=5),
)
def test_portable_keeps_blend_relative_paths(pathmod, parts: list[str], seps: list[str]) -> None:
    """A // path with either separator stores with "/" and resolves to the same file."""
    text = "//" + _join(parts, [""] + seps)
    folder = "C:\\proj\\site" if pathmod is NT else "/proj/site"
    stored = paths.portable(text, folder, pathmod=pathmod)
    assert stored.startswith("//") and "\\" not in stored
    resolved = paths.resolve(stored, folder, pathmod=pathmod)
    assert resolved == paths.resolve(text, folder, pathmod=pathmod)


# --- face map ------------------------------------------------------------------------------


@st.composite
def scenes(draw) -> SceneExport:
    """Primitives whose triangles come from a few objects' polygons, in any order."""
    names = draw(
        st.lists(
            st.text(st.characters(exclude_categories=("Cs", "Cc")), min_size=1, max_size=6),
            min_size=1,
            max_size=4,
            unique=True,
        )
    )
    primitives = []
    for _ in range(draw(st.integers(1, 4))):
        triangles = draw(st.integers(1, 12))
        objects = draw(hnp.arrays(np.int32, triangles, elements=st.integers(0, len(names) - 1)))
        polygons = draw(hnp.arrays(np.int32, triangles, elements=st.integers(0, 6)))
        primitives.append(
            PrimitiveData(
                material=None,
                positions=np.zeros((3, 3), np.float32),
                indices=np.tile(np.array([0, 1, 2], np.uint32), (triangles, 1)),
                source_object=objects,
                source_polygon=polygons,
            )
        )
    return SceneExport(tuple(names), (), (), tuple(primitives))


@given(scenes())
def test_face_map_is_a_bijection(scene: SceneExport) -> None:
    """Each source face owns a set of (primitive, triangle) pairs; the sets partition
    the triangles, and each set traces back to its face alone, before and after a save."""
    topology = {name: f"{i:064x}" for i, name in enumerate(scene.object_names)}
    built = build_face_map(scene, topology)
    with tempfile.TemporaryDirectory() as folder:
        save_face_map(built, Path(folder) / "facemap.npz")
        loaded = load_face_map(Path(folder) / "facemap.npz")
    assert loaded.object_names == built.object_names and loaded.topology == topology
    np.testing.assert_array_equal(loaded.primitive_offsets, built.primitive_offsets)

    primitive = np.concatenate([np.full(len(p.indices), k) for k, p in enumerate(scene.primitives)])
    triangle = np.concatenate([np.arange(len(p.indices)) for p in scene.primitives])
    objects, polygons = loaded.lookup_many(primitive, triangle)
    np.testing.assert_array_equal(
        objects, np.concatenate([p.source_object for p in scene.primitives])
    )
    np.testing.assert_array_equal(
        polygons, np.concatenate([p.source_polygon for p in scene.primitives])
    )

    owner = np.full(len(primitive), -1)
    faces = sorted(set(zip(objects.tolist(), polygons.tolist(), strict=True)))
    for k, (obj, polygon) in enumerate(faces):
        mask = np.zeros(polygon + 1, dtype=bool)
        mask[polygon] = True
        rows = selected_rows(loaded, {scene.object_names[obj]: mask})
        assert rows.any() and (owner[rows] == -1).all()
        owner[rows] = k
        np.testing.assert_array_equal(tile_face_mask(loaded, rows, primitive, triangle), rows)
        traced = source_faces(loaded, primitive[rows], triangle[rows])
        assert {n: p.tolist() for n, p in traced.polygons.items()} == {
            scene.object_names[obj]: [polygon]
        }
        assert traced.unknown == 0
    assert (owner >= 0).all()


# --- hydrology -----------------------------------------------------------------------------


def _vertical_hits(origins: np.ndarray, tri_xyz: np.ndarray, up: bool):
    """Nearest triangle straight below (or above) each origin: ``(face, point)``, -1 if none."""
    face = np.full(len(origins), -1, dtype=np.int64)
    point = np.zeros((len(origins), 3))
    a, b, c = tri_xyz[:, 0], tri_xyz[:, 1], tri_xyz[:, 2]
    det = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])
    ok = np.abs(det) > 1e-15
    for i, o in enumerate(origins):  # tests only: a small brute force
        px, py = o[0] - a[:, 0], o[1] - a[:, 1]
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (px * (c[:, 1] - a[:, 1]) - py * (c[:, 0] - a[:, 0])) / det
            v = (py * (b[:, 0] - a[:, 0]) - px * (b[:, 1] - a[:, 1])) / det
            inside = ok & (u >= -1e-12) & (v >= -1e-12) & (u + v <= 1 + 1e-12)
            zh = a[:, 2] + u * (b[:, 2] - a[:, 2]) + v * (c[:, 2] - a[:, 2])
        cand = inside & ((zh > o[2]) if up else (zh < o[2]))
        if cand.any():
            gap = np.where(cand, np.abs(zh - o[2]), np.inf)
            f = int(np.argmin(gap))
            face[i] = f
            point[i] = (o[0], o[1], zh[f])
    return face, point


def _box(lo, hi) -> tuple[np.ndarray, np.ndarray]:
    """A closed box, normals out, as triangles."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    pts = np.array([[x, y, z] for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)])
    quads = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return pts, np.array([t for q in quads for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))])


_HEIGHT = st.one_of(
    st.integers(-2, 2).map(float),  # ties: flats and lakes
    st.floats(-3.0, 3.0, allow_nan=False),
)


@st.composite
def hydrology_meshes(draw) -> tuple[np.ndarray, np.ndarray]:
    """A terrain grid, and maybe a closed box standing on it and a floating roof above it."""
    nx, ny = draw(st.integers(2, 7)), draw(st.integers(2, 7))
    z = draw(hnp.arrays(np.float64, (nx, ny), elements=_HEIGHT))
    pts, tris = hs.grid_mesh(z, np.arange(nx, dtype=np.float64), np.arange(ny, dtype=np.float64))
    pieces = [(pts, tris)]
    if draw(st.booleans()):
        x0, y0 = draw(st.floats(0.0, nx - 1.5)), draw(st.floats(0.0, ny - 1.5))
        size = draw(st.floats(0.3, 1.5))
        z0 = draw(st.floats(-3.0, 1.0))
        pieces.append(_box((x0, y0, z0), (x0 + size, y0 + size, z0 + draw(st.floats(0.5, 3.0)))))
    if draw(st.booleans()):
        x0, y0 = draw(st.floats(-0.5, nx - 1.0)), draw(st.floats(-0.5, ny - 1.0))
        z0, tilt = draw(st.floats(3.5, 5.0)), draw(st.floats(-1.0, 1.0))
        roof = np.array([[x0, y0, z0], [x0 + 1, y0, z0 + tilt], [x0 + 1, y0 + 1, z0 + tilt]])
        roof = np.vstack([roof, [x0, y0 + 1, z0]])
        flip = draw(st.booleans())  # an upward roof or an overhang's underside
        quad = [(0, 2, 1), (0, 3, 2)] if flip else [(0, 1, 2), (0, 2, 3)]
        pieces.append((roof, np.array(quad)))
    positions, triangles, count = [], [], 0
    for p, t in pieces:
        positions.append(p)
        triangles.append(t + count)
        count += len(p)
    return np.concatenate(positions), np.concatenate(triangles)


@given(mesh=hydrology_meshes(), area=st.sampled_from(hy.AREA_MODES))
def test_hydrology_conserves_mass(mesh, area: str) -> None:
    """No flow is created or lost: the routing is a forest, each face holds its own
    area plus its donors', and what leaves the surface is the whole contributing area."""
    positions, tris = mesh
    starts = np.arange(0, 3 * len(tris) + 1, 3)
    corner_verts = tris.ravel()
    edges, n_edges = hy.corner_edges(starts, corner_verts)
    surface = hy.surface_from_mesh(positions, starts, corner_verts, edges, n_edges)
    queries = hy.drop_queries(surface, 1e-4)
    tri_xyz = positions[tris]
    down = _vertical_hits(queries.origin, tri_xyz, up=False)
    up = _vertical_hits(queries.origin, tri_xyz, up=True)
    drops = hy.resolve_drops(queries, surface, *down, *up)
    result = hy.analyse(surface, drops, area=area)

    n = surface.n
    receiver = result.receiver
    assert ((receiver == hy.OCEAN) | ((receiver >= 0) & (receiver < n))).all()
    assert (receiver != np.arange(n)).all()
    root = np.where(receiver >= 0, receiver, np.arange(n))
    for _ in range(n):  # a forest: every face reaches an outlet within n steps
        root = root[root]
    assert (receiver[root] == hy.OCEAN).all()

    weights = np.maximum(surface.plan_areas, 0.0) if area == "PLAN" else surface.areas
    inflow = np.bincount(receiver[receiver >= 0], result.accumulation[receiver >= 0], n)
    np.testing.assert_allclose(result.accumulation, weights + inflow, rtol=1e-12, atol=1e-12)
    out = result.accumulation[receiver == hy.OCEAN].sum()
    assert out == pytest.approx(weights.sum(), rel=1e-12, abs=1e-12)


# --- contours ------------------------------------------------------------------------------


def _sphere(radii: np.ndarray, rings: int, segments: int) -> tuple[np.ndarray, np.ndarray]:
    """A closed UV sphere (a vertex at each pole) with its own radius at every vertex."""
    theta = np.pi * np.arange(1, rings) / rings
    phi = 2.0 * np.pi * np.arange(segments) / segments
    t, p = np.meshgrid(theta, phi, indexing="ij")
    unit = np.stack([np.sin(t) * np.cos(p), np.sin(t) * np.sin(p), np.cos(t)], -1).reshape(-1, 3)
    unit = np.vstack([[0.0, 0.0, 1.0], unit, [0.0, 0.0, -1.0]])
    positions = unit * radii[:, None]

    def ring(r: int) -> np.ndarray:
        return 1 + r * segments + np.arange(segments)

    def nxt(r: int) -> np.ndarray:
        return 1 + r * segments + (np.arange(segments) + 1) % segments

    south = len(unit) - 1
    tris = [np.stack([np.zeros(segments, int), ring(0), nxt(0)], 1)]
    for r in range(rings - 2):
        tris.append(np.stack([ring(r), ring(r + 1), nxt(r + 1)], 1))
        tris.append(np.stack([ring(r), nxt(r + 1), nxt(r)], 1))
    last = rings - 2
    tris.append(np.stack([ring(last), np.full(segments, south), nxt(last)], 1))
    return positions, np.concatenate(tris)


def _nice(values: st.SearchStrategy[float], lo: float, hi: float) -> st.SearchStrategy[float]:
    """``values``, or a multiple of 0.25 from ``lo`` to ``hi``: vertices exactly on levels."""
    quarters = st.integers(round(4 * lo), round(4 * hi)).map(lambda k: k / 4.0)
    return st.one_of(quarters, values)


@st.composite
def closed_surfaces(draw) -> tuple[np.ndarray, np.ndarray]:
    rings, segments = draw(st.integers(3, 8)), draw(st.integers(3, 9))
    count = 2 + (rings - 1) * segments
    radii = draw(hnp.arrays(np.float64, count, elements=_nice(st.floats(0.5, 2.0), 0.5, 2.0)))
    positions, tris = _sphere(radii, rings, segments)
    if draw(st.booleans()):  # tilt it, so no ring of vertices is level
        angle = draw(st.floats(0.0, math.pi))
        c, s = math.cos(angle), math.sin(angle)
        positions = positions @ np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])
    offset = draw(st.tuples(*[_nice(st.floats(-1e3, 1e3), -3.0, 3.0)] * 3))
    return positions + np.array(offset), tris


@given(
    surface=closed_surfaces(),
    interval=_nice(st.floats(0.05, 2.0), 0.25, 1.0),
    base=_nice(st.floats(-10.0, 10.0), -1.0, 1.0),
    datum=st.one_of(st.just(0.0), st.floats(-1e4, 1e4)),
)
def test_contours_of_a_closed_surface_are_closed(surface, interval, base, datum) -> None:
    """Every contour of a closed surface is a closed ring, except at two documented
    degeneracies, both at a mesh vertex exactly on a level:

    - a saddle: three or more segments meet at the vertex, and the lines that reach it
      end there (``analysis.polylines``);
    - a touch: the surface reaches the level without crossing it, as a ridge or a
      valley along a mesh edge exactly on the level, whose contour is that edge, an
      open line (``slicing._distinct_segments``).

    So a line ends only at such a vertex, and with no vertex on a level all are closed.
    """
    positions, tris = surface
    try:
        lines = slicing.contours(positions, tris, interval, base=base, datum=datum)
    except slicing.SliceError:
        assume(False)
    z = positions[:, 2] + datum
    levels, _ = slicing.contour_levels(float(z.min()), float(z.max()), interval, base)
    if not np.isin(z, levels).any():
        assert lines.closed.all()
    starts = np.r_[0, np.flatnonzero(np.diff(lines.edge_line)) + 1]
    stops = np.r_[starts[1:] - 1, len(lines.edges) - 1] if len(starts) else starts
    elevation = lines.line_values["elevation"]
    for line in np.flatnonzero(~lines.closed):
        for tip in (lines.edges[starts[line], 0], lines.edges[stops[line], 1]):
            where = lines.positions[tip]
            if (lines.positions[lines.edges] == where).all(axis=2).sum() >= 3:
                continue  # a saddle
            (vertex,) = np.flatnonzero(
                (positions[:, :2] == where[:2]).all(axis=1) & (z == elevation[line])
            )
            around = z[np.unique(tris[(tris == vertex).any(axis=1)])]
            touch = (around <= elevation[line]).all() or (around >= elevation[line]).all()
            assert touch, f"a contour ends loose at {where}"


# --- the pins ------------------------------------------------------------------------------


def test_hypothesis_is_pinned() -> None:
    """Every CI job that runs the core tests installs the pinned hypothesis.

    ``tests/core/conftest.py`` imports hypothesis, so a job that runs any file
    under ``tests/core`` without it fails before collecting a test.
    """
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    (pin,) = re.findall(r'^\s+HYPOTHESIS_VERSION: "([^"]+)"$', ci, re.MULTILINE)
    jobs = re.split(r"^  (?=[\w-]+:$)", ci.split("\njobs:\n", 1)[1], flags=re.MULTILINE)
    runs_core = [j for j in jobs if re.search(r"-m pytest(?![\w-])", j)]
    assert len(runs_core) >= 3, "expected the core, macOS/Windows and wheel jobs to run pytest"
    for job in runs_core:
        name = job.split(":", 1)[0]
        assert '"hypothesis==${HYPOTHESIS_VERSION}"' in job, f"CI job {name}: install hypothesis"
    assert f"hypothesis=={pin}" in (REPO / "CONTRIBUTING.md").read_text(encoding="utf-8")
