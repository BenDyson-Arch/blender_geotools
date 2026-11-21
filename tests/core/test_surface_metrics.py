"""Surface metrics at stated scales (``geotools.analysis.surface_metrics``), on synthetic surfaces."""

from __future__ import annotations

import math

import numpy as np
import pytest
from synthetic_surfaces import join, plane_xy, surface

from geotools.analysis import neighbourhood as nb
from geotools.analysis import surface_metrics as sm


def at(metrics_scale, centres, point):
    return int(np.argmin(np.linalg.norm(centres - np.asarray(point), axis=1)))


def test_plane_is_smooth_at_every_scale_and_normals_follow_the_tilt() -> None:
    tilt = math.radians(20)
    mesh = surface(plane_xy(lambda x, y: x * math.tan(tilt)), 300, 300, (0, 0.3), (0, 0.3))
    m = sm.surface_metrics(*mesh)
    centres = nb.face_samples(*mesh).points
    i = at(None, centres, [0.15, 0.15, 0.15 * math.tan(tilt)])
    for sc in m.scales:
        assert sc.status[i] == nb.RESOLVED, sc.scale
        assert sc.rms[i] < 1e-8  # the eigenvalue solve's rounding
        assert np.allclose(sc.normal[i], [-math.sin(tilt), 0, math.cos(tilt)], atol=1e-6)
    assert m.finest[i] == pytest.approx(0.01)
    assert m.scales[0].queries < len(centres)  # thinned at s/4


def test_sinusoid_roughness_grows_with_scale() -> None:
    amplitude, wavelength = 0.0005, 0.02
    mesh = surface(
        plane_xy(lambda x, y: amplitude * np.sin(2 * math.pi * x / wavelength)),
        400,
        400,
        (0, 0.4),
        (0, 0.4),
    )
    m = sm.surface_metrics(*mesh)
    centres = nb.face_samples(*mesh).points
    i = at(None, centres, [0.2, 0.2, 0.0])
    r1, r3, r10 = (sc.rms[i] for sc in m.scales)
    # 10 cm holds five waves: about A/√2. 1 cm holds half a wave: much less.
    assert r10 == pytest.approx(amplitude / math.sqrt(2), rel=0.05)
    assert r1 < r3 < r10 * 1.1
    assert r1 < 0.5 * r10


def test_finest_resolved_scale_follows_the_mesh() -> None:
    fine = surface(plane_xy(), 200, 200, (0, 0.2), (0, 0.2))  # 1 mm
    coarse = surface(plane_xy(), 40, 40, (0.2, 0.4), (0, 0.2))  # 5 mm
    m = sm.surface_metrics(*join(fine, coarse))
    centres = nb.face_samples(*join(fine, coarse)).points
    f = at(None, centres, [0.08, 0.1, 0])
    c = at(None, centres, [0.32, 0.1, 0])
    assert m.finest[f] == pytest.approx(0.01)
    assert m.finest[c] == pytest.approx(0.03)
    assert m.scales[0].status[c] & nb.COARSE
    assert np.isnan(m.scales[0].rms[c])
    # The unwelded seam at x = 0.2 is an open edge: 10 cm is not resolved within 2.5 cm.
    s = at(None, centres, [0.21, 0.1, 0])
    assert m.scales[2].status[s] & nb.EDGE
    assert m.edge_distance[s] == pytest.approx(0.01, abs=0.002)


def test_ceiling_of_a_half_cylinder_faces_down() -> None:
    radius = 1.0
    # Half a cylinder along +Y, seen from inside: a vault whose normals point at the axis.
    mesh = surface(
        lambda s, t: np.stack([radius * np.cos(s), t, radius * np.sin(s)], -1),
        600,
        200,
        (0.05, math.pi - 0.05),
        (0, 0.5),
    )
    smp = nb.face_samples(*mesh)
    if smp.normal[len(smp) // 2, 2] > 0:  # make the normals point into the vault
        mesh = (mesh[0], mesh[1], mesh[2].reshape(-1, 3)[:, ::-1].ravel())
    m = sm.surface_metrics(*mesh, scales=(0.03, 0.10))  # 4 mm edges: 1 cm is too fine
    centres = nb.face_samples(*mesh).points
    top = at(None, centres, [0.0, 0.25, radius])
    side = at(None, centres, [radius * math.cos(1.0), 0.25, radius * math.sin(1.0)])
    for sc in m.scales:
        assert sc.status[top] == nb.RESOLVED
        assert sc.normal[top, 2] == pytest.approx(-1.0, abs=1e-3)
        want = -np.array([math.cos(1.0), 0, math.sin(1.0)])
        assert np.allclose(sc.normal[side], want, atol=2e-3)
    # A plane through a ball on a cylinder of radius R misses it by about r²/(2R)
    # at the rim: the 10 cm roughness reads curvature, sub-millimetre here.
    assert 1e-4 < m.scales[1].rms[top] < 1e-3


def test_scales_must_be_positive() -> None:
    mesh = surface(plane_xy(), 4, 4)
    with pytest.raises(ValueError, match="positive"):
        sm.surface_metrics(*mesh, scales=(0.0,))
