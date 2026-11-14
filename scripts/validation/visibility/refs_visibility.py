# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy==2.5.4"]
# ///
"""Reference values for the viewshed, cumulative viewshed and intervisibility presets.

For each synthetic case in ``cases.py`` (ridge, dome, Gaussian hills, 20 km
level ground) this writes:

* the exact viewshed of the TIN's faces by GeoTools' own definition (face
  centroid + Ray Offset along the normal, Target Height up, front-facing when
  Target Height is 0), computed in binary64 without Blender (``cases.tin_margin``);
* the exact visibility of the grid nodes (the raster cell centres);
* GDAL's ``gdal_viewshed`` and GRASS's ``r.viewshed`` on the same DEM as a
  raster (pinned Docker images), with curvature off, and for the long-range
  case also with their default curvature and refraction;
* for the hills: the cumulative viewshed of four observers (exact, and the sum
  of four GDAL and GRASS viewsheds) and the intervisibility of six points
  (exact, and read from GDAL and GRASS viewsheds with target height = eye height).

Run from the repository root (needs Docker; about two minutes):

    uv run scripts/validation/visibility/refs_visibility.py

It writes ``tests/data/validation/visibility/visibility.npz`` and
``visibility.json``; the work files go to ``.scratch/validation/visibility``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _docker as D
import cases as C

REPO = HERE.parents[2]
OUT = REPO / "tests" / "data" / "validation" / "visibility"
WORK = REPO / ".scratch" / "validation" / "visibility"


def code(seen: np.ndarray, unsure: np.ndarray) -> np.ndarray:
    """1 seen, 0 hidden, -1 grazing (either answer is right)."""
    return np.where(unsure, -1, seen.astype(np.int8)).astype(np.int8)


def prepare(g: C.Grid) -> Path:
    work = WORK / g.name
    work.mkdir(parents=True, exist_ok=True)
    C.write_aaigrid(work / "dem.asc", g)
    D.gdal(work, "gdal_translate", "-q", "-a_srs", D.CRS, "dem.asc", "dem.tif")
    return work


def gdal_viewshed(work: Path, xy, eye: float, target: float, cc: float, md: float = 0.0):
    """GDAL's viewshed: 1 visible, 0 invisible, NaN out of range; rows north first."""
    out = f"gdal_{xy[0]:g}_{xy[1]:g}_{eye:g}_{target:g}_{cc:g}"
    D.gdal(
        work,
        "gdal_viewshed",
        "-q",
        "-ox",
        repr(float(xy[0])),
        "-oy",
        repr(float(xy[1])),
        "-oz",
        repr(eye),
        "-tz",
        repr(target),
        "-cc",
        repr(cc),
        "-md",
        repr(md),
        "-vv",
        "1",
        "-iv",
        "0",
        "-ov",
        "2",
        "dem.tif",
        out + ".tif",
    )
    D.gdal(work, "gdal_translate", "-q", "-of", "AAIGrid", out + ".tif", out + ".asc")
    v = D.read_aaigrid(work / (out + ".asc"))
    v[v == 2] = np.nan
    return v


def grass_viewshed(work: Path, jobs: list[tuple], curvature: bool = False) -> list[np.ndarray]:
    """GRASS r.viewshed -b for each (xy, eye, target, max_distance), one GRASS session."""
    lines = ["r.in.gdal -o input=/data/dem.tif output=dem --quiet", "g.region raster=dem"]
    flags = "-bcr" if curvature else "-b"
    names = []
    for k, (xy, eye, target, md) in enumerate(jobs):
        name = f"grass_{k}{'_c' if curvature else ''}"
        names.append(name)
        lines.append(
            f"r.viewshed {flags} input=dem output=v{k} coordinates={xy[0]!r},{xy[1]!r} "
            f"observer_elevation={eye!r} target_elevation={target!r} max_distance={md!r} "
            "memory=2000 --quiet --overwrite"
        )
        lines.append(
            f"r.out.gdal input=v{k} output=/data/{name}.asc format=AAIGrid --quiet --overwrite"
        )
    D.grass(work, "\n".join(lines) + "\n")
    return [D.read_aaigrid(work / f"{n}.asc") for n in names]


def nodes_of(raster: np.ndarray) -> np.ndarray:
    """Raster rows (north first) as node order (rows from the south)."""
    return raster[::-1].ravel()


def agreement(a: np.ndarray, b: np.ndarray, keep: np.ndarray) -> dict:
    keep = keep & np.isfinite(a) & np.isfinite(b)
    differ = keep & (a != b)
    return {
        "compared": int(keep.sum()),
        "differ": int(differ.sum()),
        "agree_pct": round(100.0 * (1 - differ.sum() / max(keep.sum(), 1)), 3),
    }


def boundary_distance(g: C.Grid, seen: np.ndarray) -> np.ndarray:
    """For each node, the distance in cells to the nearest node of the other visibility."""
    s = seen.reshape(g.ny + 1, g.nx + 1)
    dist = np.full(s.shape, np.inf)
    on = np.argwhere(s)
    off = np.argwhere(~s)
    if not len(on) or not len(off):
        return dist.ravel()
    # Brute force in chunks: the grids are small.
    for mine, other in ((on, off), (off, on)):
        for lo in range(0, len(mine), 4096):
            m = mine[lo : lo + 4096]
            d = np.sqrt(((m[:, None, :] - other[None, :, :]) ** 2).sum(-1)).min(axis=1)
            dist[m[:, 0], m[:, 1]] = d
    return dist.ravel()


def single(
    g: C.Grid,
    xy,
    eye_height: float,
    target: float,
    data: dict,
    summary: dict,
    key: str,
    md: float = 0.0,
    long: bool = False,
) -> None:
    work = prepare(g)
    eye = C.node_eye(g, xy, eye_height)
    seen, unsure = C.viewshed_faces(g, eye, target)
    data[f"{key}__exact_faces"] = code(seen, unsure)
    nseen, nunsure = C.viewshed_nodes(g, eye, target)
    data[f"{key}__exact_nodes"] = code(nseen, nunsure)
    gd = nodes_of(gdal_viewshed(work, xy, eye_height, target, 0.0, md))
    (gr,) = grass_viewshed(work, [(xy, eye_height, target, md if md else -1.0)])
    gr = nodes_of(gr)
    data[f"{key}__gdal"] = np.nan_to_num(gd, nan=2).astype(np.uint8)
    data[f"{key}__grass"] = np.nan_to_num(gr, nan=2).astype(np.uint8)
    near = boundary_distance(g, nseen)
    s = {
        "faces": len(seen),
        "faces_seen": int(seen.sum()),
        "faces_grazing": int(unsure.sum()),
        "gdal_vs_exact_nodes": agreement(gd, nseen.astype(float), ~nunsure),
        "grass_vs_exact_nodes": agreement(gr, nseen.astype(float), ~nunsure),
        "gdal_vs_grass": agreement(gd, gr, np.ones(len(gd), bool)),
    }
    for tool, values in (("gdal", gd), ("grass", gr)):
        bad = np.isfinite(values) & (values != nseen) & ~nunsure
        s[f"{tool}_differ_max_cells_from_boundary"] = float(near[bad].max()) if bad.any() else 0.0
        s[f"{tool}_differ_beyond_1_cell"] = int((near[bad] > 1.5).sum())
    if long:
        curved = nodes_of(gdal_viewshed(work, xy, eye_height, target, 0.85714, md))
        (gcurved,) = grass_viewshed(work, [(xy, eye_height, target, md)], curvature=True)
        gcurved = nodes_of(gcurved)
        data[f"{key}__gdal_curved"] = np.nan_to_num(curved, nan=2).astype(np.uint8)
        data[f"{key}__grass_curved"] = np.nan_to_num(gcurved, nan=2).astype(np.uint8)
        nodes = g.nodes()
        r = np.hypot(nodes[:, 0] - xy[0], nodes[:, 1] - xy[1])
        for tool, values in (("gdal", curved), ("grass", gcurved)):
            seen_r = r[values == 1]
            hidden_r = r[values == 0]
            s[f"{tool}_curved_farthest_seen_m"] = float(seen_r.max())
            s[f"{tool}_curved_nearest_hidden_m"] = float(hidden_r.min()) if len(hidden_r) else None
        s["horizon_k_1_7_m"] = C.horizon_distance(eye_height)
    if key.startswith("ridge"):
        nodes = g.nodes()
        row = np.abs(nodes[:, 1]) < 1e-9
        behind = row & (nodes[:, 0] > C.RIDGE["D"] + C.RIDGE["W"])
        for tool, values in (("gdal", gd), ("grass", gr), ("exact", nseen.astype(float))):
            hidden = nodes[behind & (values == 0), 0]
            s[f"{tool}_last_hidden_node_x"] = float(hidden.max()) if len(hidden) else None
        s["analytic_shadow_end_x"] = C.ridge_shadow_end(eye_height, target)
    summary[key] = s
    print(key, json.dumps(s))


def cumulative(g: C.Grid, data: dict, summary: dict) -> None:
    work = prepare(g)
    count = np.zeros(2 * g.nx * g.ny)
    unsure_any = np.zeros(len(count), bool)
    nodes_count = np.zeros((g.nx + 1) * (g.ny + 1))
    gd = np.zeros(len(nodes_count))
    for xy in C.HILLS_OBSERVERS:
        eye = C.node_eye(g, xy, C.EYE)
        seen, unsure = C.viewshed_faces(g, eye)
        count += seen
        unsure_any |= unsure
        nseen, _ = C.viewshed_nodes(g, eye)
        nodes_count += nseen
        gd += np.nan_to_num(nodes_of(gdal_viewshed(work, xy, C.EYE, 0.0, 0.0)))
    grs = grass_viewshed(work, [(xy, C.EYE, 0.0, -1.0) for xy in C.HILLS_OBSERVERS])
    gr = sum(np.nan_to_num(nodes_of(v)) for v in grs)
    data["cumulative__exact_faces"] = np.where(unsure_any, -1, count).astype(np.int8)
    data["cumulative__exact_nodes"] = nodes_count.astype(np.int8)
    data["cumulative__gdal"] = gd.astype(np.int8)
    data["cumulative__grass"] = gr.astype(np.int8)
    s = {
        "observers": len(C.HILLS_OBSERVERS),
        "faces_grazing_any": int(unsure_any.sum()),
        "counts": np.bincount(count.astype(int)).tolist(),
        "gdal_vs_exact_nodes": agreement(gd, nodes_count, np.ones(len(gd), bool)),
        "grass_vs_exact_nodes": agreement(gr, nodes_count, np.ones(len(gr), bool)),
        "gdal_minus_exact_nodes_abs_max": int(np.abs(gd - nodes_count).max()),
    }
    summary["cumulative"] = s
    print("cumulative", json.dumps(s))


def intervisibility(g: C.Grid, data: dict, summary: dict) -> None:
    work = prepare(g)
    pts = C.HILLS_POINTS
    n = len(pts)
    eyes = np.array([C.node_eye(g, p, C.EYE) for p in pts])
    exact, unsure, gdal_pairs, grass_pairs = [], [], [], []
    nodes = g.nodes()

    def at(values, xy):
        k = np.flatnonzero(
            (np.abs(nodes[:, 0] - xy[0]) < 1e-9) & (np.abs(nodes[:, 1] - xy[1]) < 1e-9)
        )
        return float(values[k[0]])

    gviews = [nodes_of(gdal_viewshed(work, p, C.EYE, C.EYE, 0.0)) for p in pts]
    rviews = [nodes_of(v) for v in grass_viewshed(work, [(p, C.EYE, C.EYE, -1.0) for p in pts])]
    for i in range(n):
        for j in range(i + 1, n):
            a, b = eyes[i], eyes[j]
            d = (b - a) / np.linalg.norm(b - a)
            margin = C.tin_margin(g, (a + C.OFFSET * d)[None], (b - C.OFFSET * d)[None])[0]
            exact.append(margin >= 0)
            unsure.append(abs(margin) < 1e-3)
            gdal_pairs.append(at(gviews[i], pts[j]))
            grass_pairs.append(at(rviews[i], pts[j]))
    data["intervisibility__exact"] = code(np.array(exact), np.array(unsure))
    data["intervisibility__gdal"] = np.array(gdal_pairs, np.int8)
    data["intervisibility__grass"] = np.array(grass_pairs, np.int8)
    s = {
        "pairs": len(exact),
        "visible": int(np.sum(exact)),
        "grazing": int(np.sum(unsure)),
        "gdal_agree": int(np.sum(np.array(gdal_pairs) == np.array(exact))),
        "grass_agree": int(np.sum(np.array(grass_pairs) == np.array(exact))),
    }
    summary["intervisibility"] = s
    print("intervisibility", json.dumps(s))


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    data: dict[str, np.ndarray] = {}
    summary: dict = {"versions": D.versions(WORK)}
    print(summary["versions"])
    single(C.ridge(), C.RIDGE["observer"], C.EYE, 0.0, data, summary, "ridge")
    single(C.ridge(), C.RIDGE["observer"], C.EYE, 0.5, data, summary, "ridge_target")
    single(C.dome(), C.DOME["observer"], C.DOME["eye"], 0.0, data, summary, "dome")
    hills = C.hills()
    single(hills, C.HILLS_OBSERVER, C.EYE, 0.0, data, summary, "hills")
    cumulative(hills, data, summary)
    intervisibility(hills, data, summary)
    g = C.long_range()
    single(
        g,
        C.LONG["observer"],
        C.LONG["eye"],
        0.0,
        data,
        summary,
        "long",
        md=C.LONG["max_distance"],
        long=True,
    )
    np.savez_compressed(OUT / "visibility.npz", **data)
    (OUT / "visibility.json").write_text(json.dumps(summary, indent=1) + "\n")
    print("wrote", OUT / "visibility.npz", (OUT / "visibility.npz").stat().st_size, "bytes")


if __name__ == "__main__":
    main()
