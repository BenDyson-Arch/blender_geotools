# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy==2.5.4", "networkx==3.7"]
# ///
"""Reference values for least-cost routes (docs/validation/least-cost.md); not run in CI.

* **networkx 3.7** Dijkstra on the same directed graph, built here
  independently of GeoTools (plain Python, one triangle at a time): the
  vertices of the hills TIN plus ``k`` Steiner points per edge, every pair of
  nodes on a triangle linked both ways, each link costing the walking model's
  time for its plan run and rise times its face's cost factor, faces steeper
  than Max Slope left out. Times at a sample of nodes and the route from Start
  to End and back, for each model, for ``k`` 0 and 2, and with a cost field
  (a band at twice the cost, a wall with a gap at no data).
* **GRASS GIS 8.5.0 r.walk** (zero friction, 8 and 16 neighbours) on the same
  DEM as a raster whose cell centres are the TIN's vertices: the accumulated
  cost from Start at every cell.

The node numbering is the convention GeoTools documents: the vertices, then
for each edge in order of ``lo * V + hi`` (its vertex ids) its points from the
lower id's end. The test checks the sampled nodes' coordinates as well.

Run from the repository root (needs Docker)::

    uv run scripts/validation/least_cost/make_references.py

It writes ``tests/data/validation/least_cost/references.npz`` and
``references.json`` and works in ``.scratch/validation/least_cost/``. About
two minutes.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import networkx as nx
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "visibility"))

import _docker as D
import cases as C

REPO = HERE.parents[2]
OUT = REPO / "tests" / "data" / "validation" / "least_cost"
WORK = REPO / ".scratch" / "validation" / "least_cost"

H = 1.0  # the hills at 1 m: 8,192 triangles
START = (-28.0, -28.0)
END = (28.0, 26.0)
MAX_SLOPE = 30.0
SAMPLE = 400  # nodes whose times are kept per run

# --- the models, written out per link --------------------------------------------------------


def tobler(run: float, rise: float) -> float:
    v = 6.0 / 3.6 * math.exp(-3.5 * abs(rise / run + 0.05))
    return run / v


def langmuir(run: float, rise: float) -> float:
    # r.walk's walk_coeff 0.72,6.0,1.9998,-1.9998 and slope_factor -0.2125.
    if rise >= 0:
        return 0.72 * run + 6.0 * rise
    if rise / run > -0.2125:
        return 0.72 * run + 1.9998 * rise
    return 0.72 * run - 1.9998 * rise


def naismith(run: float, rise: float) -> float:
    return 0.72 * run + 6.0 * max(rise, 0.0)


def wheelchair(run: float, rise: float) -> float:
    s = rise / run
    if abs(s) > 1.0 / 12.0:
        return math.inf
    v = 1.12 - (1.12 - 0.59) / math.tan(math.radians(4.8)) * max(s, 0.0)
    return run / v


MODELS = {"TOBLER": tobler, "LANGMUIR": langmuir, "NAISMITH": naismith, "WHEELCHAIR": wheelchair}


def link_time(model, p, q) -> float:
    run = math.hypot(q[0] - p[0], q[1] - p[1])
    rise = q[2] - p[2]
    if run == 0.0:
        return 0.0 if rise == 0.0 else math.inf
    return model(run, rise)


def cost_field(cx: float, cy: float) -> float:
    """A band at twice the cost, and a wall of no data with a gap."""
    if -20.0 < cx < -16.0 and cy > -12.0:
        return math.nan
    if 0.0 < cx < 10.0:
        return 2.0
    return 1.0


# --- the graph, built independently ---------------------------------------------------------------


def build(g: C.Grid, k: int, model_name: str, costs: bool):
    """networkx DiGraph and node coordinates, plus the face factor array used."""
    pts = g.nodes()
    tris = g.triangles()
    n_v = len(pts)
    keys = sorted(
        {
            min(a, b) * n_v + max(a, b)
            for t in tris.tolist()
            for a, b in zip(t, t[1:] + t[:1], strict=True)
        }
    )
    edge_id = {key: i for i, key in enumerate(keys)}
    coords = [tuple(p) for p in pts.tolist()]
    for key in keys:
        lo, hi = divmod(key, n_v)
        a, b = pts[lo], pts[hi]
        for j in range(1, k + 1):
            coords.append(tuple((a + j / (k + 1) * (b - a)).tolist()))
    model = MODELS[model_name]
    cos_max = math.cos(math.radians(MAX_SLOPE))
    graph = nx.DiGraph()
    factors = []
    for t in tris.tolist():
        p0, p1, p2 = (pts[i] for i in t)
        n = np.cross(p1 - p0, p2 - p0)
        nz = n[2] / np.linalg.norm(n)
        c = (p0 + p1 + p2) / 3.0
        f = cost_field(c[0], c[1]) if costs else 1.0
        factors.append(f)
        if not (nz > 0 and nz >= cos_max - 1e-12) or not (f > 0):  # NaN fails too
            continue
        nodes = list(t)
        for a, b in zip(t, t[1:] + t[:1], strict=True):
            lo, hi = min(a, b), max(a, b)
            nodes += [n_v + edge_id[lo * n_v + hi] * k + j for j in range(k)]
        for u in nodes:
            for v in nodes:
                if u == v:
                    continue
                w = link_time(model, coords[u], coords[v]) * f
                if not math.isfinite(w):
                    continue
                if graph.has_edge(u, v) and graph[u][v]["weight"] <= w:
                    continue
                graph.add_edge(u, v, weight=w)
    return graph, np.array(coords), np.array(factors)


def vertex_at(g: C.Grid, xy) -> int:
    i = round((xy[0] - g.x0) / g.h)
    j = round((xy[1] - g.y0) / g.h)
    return j * (g.nx + 1) + i


# --- r.walk ---------------------------------------------------------------------------------------


def r_walk(g: C.Grid, start, knight: bool) -> np.ndarray:
    """r.walk's accumulated cost (s) from ``start`` at every cell, rows north first."""
    work = WORK / "rwalk"
    work.mkdir(parents=True, exist_ok=True)
    C.write_aaigrid(work / "dem.asc", g)
    D.gdal(work, "gdal_translate", "-q", "-a_srs", D.CRS, "dem.asc", "dem.tif")
    flags = "-k" if knight else ""
    tag = "cost16" if knight else "cost8"
    script = "\n".join(
        [
            "r.in.gdal -o input=/data/dem.tif output=dem --quiet --overwrite",
            "g.region raster=dem",
            "r.mapcalc expression='zero = 0' --quiet --overwrite",
            (
                f"r.walk {flags} elevation=dem friction=zero output=cost "
                f"start_coordinates={start[0]!r},{start[1]!r} --quiet --overwrite"
            ),
            (
                f"r.out.gdal -c input=cost output=/data/{tag}.asc format=AAIGrid "
                "createopt=DECIMAL_PRECISION=9 --quiet --overwrite"
            ),
        ]
    )
    D.grass(work, script + "\n")
    return D.read_aaigrid(work / f"{tag}.asc")


def main() -> None:
    data: dict[str, np.ndarray] = {}
    summary: dict = {
        "versions": {"networkx": nx.__version__, "numpy": np.__version__},
        "case": {"h": H, "start": START, "end": END, "max_slope": MAX_SLOPE},
    }
    summary["versions"].update(D.versions(WORK))
    g = C.hills(H)
    s, e = vertex_at(g, START), vertex_at(g, END)
    rng = np.random.default_rng(109)
    runs = [(m, 2, False) for m in MODELS] + [("TOBLER", 0, False), ("TOBLER", 2, True)]
    summary["runs"] = {}
    for model, k, costs in runs:
        tag = f"{model.lower()}_k{k}{'_cost' if costs else ''}"
        graph, coords, factors = build(g, k, model, costs)
        times = nx.single_source_dijkstra_path_length(graph, s)
        reached = np.array(sorted(times), dtype=np.int64)
        sample = np.sort(rng.choice(reached, size=min(SAMPLE, len(reached)), replace=False))
        data[f"{tag}__nodes"] = sample
        data[f"{tag}__xyz"] = coords[sample]
        data[f"{tag}__time"] = np.array([times[i] for i in sample])
        run = {
            "links": graph.number_of_edges(),
            "nodes": len(coords),
            "reached": len(reached),
        }
        for name, a, b in (("there", s, e), ("back", e, s)):
            try:
                cost, path = nx.single_source_dijkstra(graph, a, b)
            except nx.NetworkXNoPath:
                run[name] = None
                continue
            data[f"{tag}__{name}"] = np.array(path, dtype=np.int64)
            run[name] = {"time": cost, "nodes": len(path)}
        summary["runs"][tag] = run
        print(tag, json.dumps(run), flush=True)
        if costs:
            data["cost_factors"] = factors

    for knight in (False, True):
        data[f"rwalk_{16 if knight else 8}"] = r_walk(g, START, knight)

    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / "references.npz", **data)
    (OUT / "references.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
