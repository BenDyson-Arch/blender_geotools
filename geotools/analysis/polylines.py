"""Undirected line segments chained into polylines, without Python loops over segments.

Contours, sections and flow paths all produce segments whose ends are shared
points (a contour crosses a mesh edge once, so the two triangles of that edge
share the crossing). :func:`chain` joins them into polylines: open where a
point ends a run (one segment, or three or more meeting), closed where the run
comes back to its start. Each polyline is returned once, in one direction.

The method works on half-edges (each segment in both directions). A half-edge
``u -> v`` continues with the other half-edge leaving ``v`` when ``v`` has
exactly two segments, so every run is a path or a cycle of half-edges, and its
reverse is another one. Pointer doubling (O(h log h) numpy) finds each path's
last half-edge and each half-edge's distance to it; a cycle is first cut
before its smallest half-edge. Of a run and its reverse, the one whose last
half-edge has the smaller id is kept.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class Chains:
    """Polylines over point ids.

    ``nodes[offsets[i]:offsets[i + 1]]`` are the points of polyline ``i`` in
    order; a closed polyline repeats its first point at the end. ``segments``
    gives, for each polyline step (``offsets[i]`` to ``offsets[i + 1] - 2``),
    the input segment it came from, in the same order (so
    ``segments[offsets[i] - i + k]`` is step ``k`` of polyline ``i``).
    """

    nodes: np.ndarray  # (p,) int64
    offsets: np.ndarray  # (c + 1,) int64
    closed: np.ndarray  # (c,) bool
    segments: np.ndarray  # (p - c,) int64

    @property
    def count(self) -> int:
        return len(self.closed)

    def lengths(self) -> np.ndarray:
        """Points per polyline (a closed one counts its first point twice)."""
        return np.diff(self.offsets)

    def polyline_of_step(self) -> np.ndarray:
        """The polyline of each step, in ``segments`` order."""
        return np.repeat(np.arange(self.count), self.lengths() - 1)


def _rounds(n: int) -> int:
    return max(int(np.ceil(np.log2(n + 1))), 1) + 1


def segment_ranges(starts: np.ndarray, lengths: np.ndarray) -> np.ndarray:
    """``concat(arange(s, s + n) for s, n in zip(starts, lengths))``, vectorised."""
    lengths = np.asarray(lengths, dtype=np.int64)
    total = int(lengths.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64)
    first = np.cumsum(lengths) - lengths
    return np.repeat(np.asarray(starts, dtype=np.int64) - first, lengths) + np.arange(total)


def chain(n_points: int, a: np.ndarray, b: np.ndarray) -> Chains:
    """Chain undirected segments ``(a[i], b[i])`` between points ``0..n_points-1``.

    Segments from a point to itself are ignored. A point with one segment, or
    three or more, ends the polylines that reach it.
    """
    a = np.asarray(a, dtype=np.int64)
    b = np.asarray(b, dtype=np.int64)
    keep = np.flatnonzero(a != b)
    a, b = a[keep], b[keep]
    m = len(a)
    if m == 0:
        empty = np.zeros(0, dtype=np.int64)
        return Chains(empty, np.zeros(1, np.int64), np.zeros(0, bool), empty)
    h_count = 2 * m
    tail = np.empty(h_count, dtype=np.int64)
    head = np.empty(h_count, dtype=np.int64)
    tail[0::2], head[0::2] = a, b
    tail[1::2], head[1::2] = b, a
    degree = np.bincount(tail, minlength=n_points)

    # The two half-edges leaving each point of degree 2.
    by_tail = np.argsort(tail, kind="stable")
    first_out = np.full(n_points, -1, dtype=np.int64)
    second_out = np.full(n_points, -1, dtype=np.int64)
    starts = np.searchsorted(tail[by_tail], np.arange(n_points))
    two = degree == 2
    first_out[two] = by_tail[starts[two]]
    second_out[two] = by_tail[starts[two] + 1]

    half = np.arange(h_count, dtype=np.int64)
    twin = half ^ 1
    v = head
    succ = np.full(h_count, -1, dtype=np.int64)
    through = two[v]
    o1 = first_out[v[through]]
    o2 = second_out[v[through]]
    succ[through] = np.where(o1 != twin[through], o1, o2)

    rounds = _rounds(h_count)
    # Cycles: half-edges whose successor chain never ends. Label each with the
    # smallest half-edge on it and cut the cycle before that one.
    step = np.where(succ < 0, half, succ)
    jump = step.copy()
    label = half.copy()
    for _ in range(rounds):
        label = np.minimum(label, label[jump])
        jump = jump[jump]
    in_cycle = step[jump] != jump
    cut = in_cycle & (succ == label)
    succ[cut] = -1

    # Paths: each half-edge's last half-edge and its distance to it.
    step = np.where(succ < 0, half, succ)
    dist = (succ >= 0).astype(np.int64)
    jump = step.copy()
    for _ in range(rounds):
        dist = dist + dist[jump]
        jump = jump[jump]
    last = jump
    kept = np.flatnonzero(last < last[twin])
    order = kept[np.lexsort((-dist[kept], last[kept]))]
    group = last[order]
    new = np.ones(len(order), dtype=bool)
    new[1:] = group[1:] != group[:-1]
    run_start = np.flatnonzero(new)
    steps_per_run = np.diff(np.append(run_start, len(order)))
    count = len(run_start)
    closed = in_cycle[order[run_start]]

    offsets = np.zeros(count + 1, dtype=np.int64)
    offsets[1:] = np.cumsum(steps_per_run + 1)
    nodes = np.empty(int(offsets[-1]), dtype=np.int64)
    run_of_step = np.repeat(np.arange(count), steps_per_run)
    nodes[np.arange(len(order)) + run_of_step] = tail[order]
    nodes[offsets[1:] - 1] = head[order[run_start + steps_per_run - 1]]
    return Chains(nodes, offsets, closed, keep[order >> 1])


def reverse(chains: Chains, flip: np.ndarray) -> Chains:
    """``chains`` with the polylines where ``flip`` is True reversed."""
    flip = np.asarray(flip, dtype=bool)
    if not flip.any():
        return chains
    lengths = chains.lengths()
    poly = np.repeat(np.arange(chains.count), lengths)
    pos = np.arange(len(chains.nodes)) - chains.offsets[poly]
    src = np.where(
        flip[poly], chains.offsets[poly] + lengths[poly] - 1 - pos, chains.offsets[poly] + pos
    )
    steps = lengths - 1
    step_poly = np.repeat(np.arange(chains.count), steps)
    step_start = chains.offsets[:-1] - np.arange(chains.count)
    spos = np.arange(len(chains.segments)) - step_start[step_poly]
    ssrc = np.where(
        flip[step_poly],
        step_start[step_poly] + steps[step_poly] - 1 - spos,
        step_start[step_poly] + spos,
    )
    return Chains(chains.nodes[src], chains.offsets, chains.closed, chains.segments[ssrc])


def edges(chains: Chains) -> np.ndarray:
    """``(steps, 2)`` point pairs of every polyline step, polyline by polyline, in order."""
    lengths = chains.lengths()
    starts = segment_ranges(chains.offsets[:-1], lengths - 1)
    return np.stack([chains.nodes[starts], chains.nodes[starts + 1]], axis=1)
