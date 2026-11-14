# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy==2.5.4", "networkx==3.7"]
# ///
"""Reference values for the Least-Cost Path preset.

* networkx Dijkstra on the same edge graph as GeoTools' triangulated grid
  (the hills of ``cases.py``, from Blender's float32 vertices), with the cost
  GeoTools documents: the mean of Tobler's time both ways along each edge,
  edges steeper than Max Slope removed. Its route and cost must be GeoTools'.
* The same graph directed, each edge costing Tobler's time in the direction
  walked: the true one-way quickest route, which GeoTools' symmetric cost
  cannot always find.
* GRASS ``r.walk`` (Aitken/Langmuir, Naismith-based) on the same DEM as a
  raster, with 8 and 16 (knight's move) neighbours and zero friction; on
  planar ramps (its time per metre against Tobler's) and on level ground at
  four bearings (the graph's directional bias against the raster's).

Run from the repository root (needs Docker):

    uv run scripts/validation/visibility/refs_paths.py

It writes ``tests/data/validation/visibility/paths.npz`` and ``paths.json``.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import networkx as nx
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import _docker as D
import cases as C

REPO = HERE.parents[2]
OUT = REPO / "tests" / "data" / "validation" / "visibility"
WORK = REPO / ".scratch" / "validation" / "visibility"


def graphs(g: C.Grid, max_slope: float):
    """(undirected symmetric-cost graph, directed Tobler graph, float32 nodes)."""
    co = np.asarray(g.nodes(), np.float32).astype(np.float64)
    e = C.grid_edges(g)
    a, b = co[e[:, 0]], co[e[:, 1]]
    run = np.hypot(*(b - a)[:, :2].T)
    rise = (b - a)[:, 2]
    up, down = C.tobler_time(run, rise), C.tobler_time(run, -rise)
    ok = np.degrees(np.arctan2(np.abs(rise), run)) <= max_slope
    sym = nx.Graph()
    sym.add_weighted_edges_from(zip(e[ok, 0], e[ok, 1], 0.5 * (up + down)[ok], strict=True))
    directed = nx.DiGraph()
    directed.add_weighted_edges_from(zip(e[ok, 0], e[ok, 1], up[ok], strict=True))
    directed.add_weighted_edges_from(zip(e[ok, 1], e[ok, 0], down[ok], strict=True))
    return sym, directed, co


def node_at(g: C.Grid, xy) -> int:
    i = round((xy[0] - g.x0) / g.h)
    j = round((xy[1] - g.y0) / g.h)
    return j * (g.nx + 1) + i


def walked(co: np.ndarray, path) -> float:
    """Tobler's time along a route, in the direction walked."""
    p = co[np.asarray(path)]
    d = np.diff(p, axis=0)
    return float(C.tobler_time(np.hypot(d[:, 0], d[:, 1]), d[:, 2]).sum())


def r_walk(g: C.Grid, start, stops, knight: bool, tag: str) -> list[float]:
    """GRASS r.walk's cumulative cost (s) from ``start`` at each stop (no friction)."""
    work = WORK / f"walk_{g.name}"
    work.mkdir(parents=True, exist_ok=True)
    C.write_aaigrid(work / "dem.asc", g)
    D.gdal(work, "gdal_translate", "-q", "-a_srs", D.CRS, "dem.asc", "dem.tif")
    flags = "-k" if knight else ""
    lines = [
        "r.in.gdal -o input=/data/dem.tif output=dem --quiet --overwrite",
        "g.region raster=dem",
        "r.mapcalc expression='zero = 0' --quiet --overwrite",
        (
            f"r.walk {flags} elevation=dem friction=zero output=cost "
            f"start_coordinates={start[0]!r},{start[1]!r} --quiet --overwrite"
        ),
    ]
    for k, (x, y) in enumerate(stops):
        lines.append(
            f"r.what map=cost coordinates={x!r},{y!r} separator=comma > /data/{tag}_{k}.txt"
        )
    D.grass(work, "\n".join(lines) + "\n")
    return [
        float((work / f"{tag}_{k}.txt").read_text().strip().split(",")[-1])
        for k in range(len(stops))
    ]


def langmuir(run: float, rise: float, a=0.72, b=6.0, c=1.9998, d=-1.9998, sf=-0.2125) -> float:
    """r.walk's documented time (s) for one step: a ΔS + b ΔH up, c or d ΔH down."""
    if rise >= 0:
        return a * run + b * rise
    if rise / run >= sf:  # moderate downhill
        return a * run + c * rise
    return a * run + d * rise


def main() -> None:
    data: dict[str, np.ndarray] = {}
    summary: dict = {"versions": {"networkx": nx.__version__, "numpy": np.__version__}}
    summary["versions"].update(D.versions(WORK))

    g = C.hills()
    sym, directed, co = graphs(g, C.LCP["max_slope"])
    s, e = node_at(g, C.LCP["start"]), node_at(g, C.LCP["end"])
    route = nx.dijkstra_path(sym, s, e)
    cost = nx.path_weight(sym, route, "weight")
    best = nx.dijkstra_path(directed, s, e)
    best_back = nx.dijkstra_path(directed, e, s)
    open_sym, open_directed, _ = graphs(g, 90.0)
    route_open = nx.dijkstra_path(open_sym, s, e)
    data["hills__route"] = np.array(route, np.int64)
    data["hills__route_open"] = np.array(route_open, np.int64)
    summary["hills"] = {
        "nodes": int(sym.number_of_nodes()),
        "edges": int(sym.number_of_edges()),
        "edges_too_steep": int(len(C.grid_edges(g)) - sym.number_of_edges()),
        "symmetric_cost": cost,
        "route_vertices": len(route),
        "route_walked_time": walked(co, route),
        "route_walked_back_time": walked(co, route[::-1]),
        "directed_best_time": walked(co, best),
        "directed_best_back_time": walked(co, best_back),
        "route_length_m": float(np.linalg.norm(np.diff(co[route], axis=0), axis=1).sum()),
        "open_symmetric_cost": nx.path_weight(open_sym, route_open, "weight"),
        "open_walked_time": walked(co, route_open),
        "open_directed_best_time": walked(co, nx.dijkstra_path(open_directed, s, e)),
    }
    for knight in (False, True):
        (t,) = r_walk(g, C.LCP["start"], [C.LCP["end"]], knight, f"hills_{int(knight)}")
        summary["hills"][f"r_walk{'_knight' if knight else ''}_time"] = t

    # Planar ramps: r.walk's time per metre, against Langmuir's formula and Tobler.
    ramps = {}
    for slope in C.RAMP_SLOPES:
        r = C.walk_ramp(slope)
        (t,) = r_walk(r, (0.0, 0.0), [(50.0, 0.0)], False, "ramp")
        rise = 50.0 * math.tan(math.radians(slope))
        ramps[f"{slope:g}"] = {
            "r_walk_s_per_m": t / 50.0,
            "langmuir_s_per_m": sum(langmuir(1.0, rise / 50.0) for _ in range(50)) / 50.0,
            "tobler_s_per_m": float(C.tobler_time(50.0, rise)) / 50.0,
        }
    summary["ramps"] = ramps

    # Level ground: the edge graph's and the raster's bias with bearing.
    flat = C.grid(lambda x, y: 0.0 * x, -12.0, -12.0, 12.0, 12.0, 1.0, "flat")
    flat_sym, _, _ = graphs(flat, 90.0)
    ends = [(10.0, 0.0), (10.0, 5.0), (10.0, 10.0), (-10.0, 10.0)]
    straight = [math.hypot(*p) for p in ends]
    v0 = float(C.tobler_time(1.0, 0.0))  # s per metre on the level
    graph = [
        nx.dijkstra_path_length(flat_sym, node_at(flat, (0, 0)), node_at(flat, p)) for p in ends
    ]
    walk8 = r_walk(flat, (0.0, 0.0), ends, False, "flat8")
    walk16 = r_walk(flat, (0.0, 0.0), ends, True, "flat16")
    summary["bearings"] = [
        {
            "end": list(p),
            "straight_m": d,
            "graph_ratio": t / (d * v0),
            "r_walk_ratio": w / (d * 0.72),
            "r_walk_knight_ratio": k / (d * 0.72),
        }
        for p, d, t, w, k in zip(ends, straight, graph, walk8, walk16, strict=True)
    ]

    OUT.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(OUT / "paths.npz", **data)
    (OUT / "paths.json").write_text(json.dumps(summary, indent=1) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
