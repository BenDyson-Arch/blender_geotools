"""Per-feature digests, to count what a refresh added, removed and changed by fid.

A digest covers a feature's float32 positions (as Blender holds them) and its
attribute values as stored in Blender (see ``mesh.stored_values``). Positions
are combined order-independently, so it is computed the same way from the
mesh being replaced and from the new arrays. Everything is vectorised except
string values, which are hashed once per feature.
"""

from __future__ import annotations

import zlib

import numpy as np

from .model import ColumnType

_M1 = np.uint64(0xBF58476D1CE4E5B9)
_M2 = np.uint64(0x94D049BB133111EB)


def _mix(x: np.ndarray) -> np.ndarray:
    """splitmix64's finaliser, elementwise on uint64 (wrapping)."""
    x = x.astype(np.uint64, copy=True)
    x ^= x >> np.uint64(30)
    x *= _M1
    x ^= x >> np.uint64(27)
    x *= _M2
    x ^= x >> np.uint64(31)
    return x


def _sum_by(values: np.ndarray, owner: np.ndarray, n: int) -> np.ndarray:
    """Wrapping uint64 sum of ``values`` per ``owner`` (0..n-1)."""
    out = np.zeros(n, dtype=np.uint64)
    if not len(values):
        return out
    order = np.argsort(owner, kind="stable")
    o = owner[order]
    starts = np.flatnonzero(np.concatenate(([True], o[1:] != o[:-1])))
    out[o[starts]] = np.add.reduceat(values[order], starts)
    return out


def _value_bits(ctype: ColumnType, values: object) -> np.ndarray:
    if ctype is ColumnType.STRING:
        return np.fromiter(
            (zlib.crc32(v if isinstance(v, bytes) else str(v).encode()) for v in values),  # type: ignore[union-attr]
            dtype=np.uint64,
        )
    if ctype is ColumnType.FLOAT:
        arr = np.asarray(values, dtype=np.float32).copy()
        arr[np.isnan(arr)] = np.nan  # one NaN bit pattern
        return arr.view(np.uint32).astype(np.uint64)
    if ctype is ColumnType.INT:
        return np.asarray(values, dtype=np.int32).view(np.uint32).astype(np.uint64)
    return np.asarray(values, dtype=bool).astype(np.uint64)


def feature_digests(
    n_features: int,
    positions: np.ndarray,
    vertex_feature: np.ndarray,
    columns: dict[str, tuple[ColumnType, object]],
) -> np.ndarray:
    """One uint64 digest per feature.

    ``positions`` are float32 ``(v, 3)``, ``vertex_feature`` the feature of
    each vertex, and ``columns`` name -> (type, one stored value per feature).
    """
    bits = np.ascontiguousarray(positions, dtype=np.float32).view(np.uint32).astype(np.uint64)
    h = _mix(bits[:, 0] ^ (bits[:, 1] << np.uint64(32))) if len(bits) else np.zeros(0, np.uint64)
    if len(bits):
        h = _mix(h ^ bits[:, 2])
    digest = _sum_by(h, np.asarray(vertex_feature, dtype=np.int64), n_features)
    digest += _mix(np.bincount(vertex_feature, minlength=n_features).astype(np.uint64))
    for name in sorted(columns):
        ctype, values = columns[name]
        salt = np.uint64(zlib.crc32(f"{name}:{ctype}".encode()))
        digest += _mix(_value_bits(ctype, values) ^ (salt << np.uint64(32)))
    return digest


def diff(
    old_fids: np.ndarray, old_digests: np.ndarray, new_fids: np.ndarray, new_digests: np.ndarray
) -> tuple[int, int, int]:
    """``(added, removed, changed)`` feature counts between two digest tables."""
    old_fids = np.asarray(old_fids, dtype=np.int64)
    new_fids = np.asarray(new_fids, dtype=np.int64)
    common, oi, ni = np.intersect1d(old_fids, new_fids, assume_unique=True, return_indices=True)
    changed = int(np.count_nonzero(old_digests[oi] != new_digests[ni]))
    return len(new_fids) - len(common), len(old_fids) - len(common), changed
