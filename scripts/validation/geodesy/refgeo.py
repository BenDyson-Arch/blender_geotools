"""Independent geodesy for the geodesy validation (not part of GeoTools).

Transverse Mercator by Krüger's series to sixth order in n (Karney 2011,
"Transverse Mercator with an accuracy of a few nanometers", J. Geodesy 85;
the formulation the ICSM GDA2020 Technical Manual uses), with the point scale
factor and the grid convergence in closed form. The inverse solves the
forward series by Newton's method, so no PROJ code is involved. Geodesics and
polygon areas come from GeographicLib.

Every angle is in degrees unless the name says radians.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

GRS80 = (6378137.0, 1.0 / 298.257222101)
WGS84 = (6378137.0, 1.0 / 298.257223563)


@dataclass(frozen=True)
class TransverseMercator:
    """A transverse Mercator grid: ellipsoid, central meridian, k0, false origin."""

    a: float
    f: float
    lon0: float
    k0: float
    false_e: float
    false_n: float

    @property
    def n(self) -> float:
        return self.f / (2.0 - self.f)

    @property
    def e(self) -> float:
        return math.sqrt(self.f * (2.0 - self.f))

    def _coeffs(self) -> tuple[float, list[float]]:
        n = self.n
        n2, n3, n4, n5, n6 = n**2, n**3, n**4, n**5, n**6
        big_a = self.a / (1.0 + n) * (1.0 + n2 / 4.0 + n4 / 64.0 + n6 / 256.0)
        alpha = [
            n / 2 - 2 * n2 / 3 + 5 * n3 / 16 + 41 * n4 / 180 - 127 * n5 / 288 + 7891 * n6 / 37800,
            13 * n2 / 48 - 3 * n3 / 5 + 557 * n4 / 1440 + 281 * n5 / 630 - 1983433 * n6 / 1935360,
            61 * n3 / 240 - 103 * n4 / 140 + 15061 * n5 / 26880 + 167603 * n6 / 181440,
            49561 * n4 / 161280 - 179 * n5 / 168 + 6601661 * n6 / 7257600,
            34729 * n5 / 80640 - 3418889 * n6 / 1995840,
            212378941 * n6 / 319334400,
        ]
        return big_a, alpha

    def forward(self, lon: float, lat: float) -> tuple[float, float, float, float]:
        """(E, N, point scale factor k, convergence gamma in degrees).

        gamma is the angle from true north to grid north, clockwise positive
        (Karney's sign): positive east of the central meridian in the north.
        """
        e = self.e
        big_a, alpha = self._coeffs()
        phi = math.radians(lat)
        lam = math.radians(lon - self.lon0)
        tau = math.tan(phi)
        sigma = math.sinh(e * math.atanh(e * tau / math.sqrt(1.0 + tau * tau)))
        taup = tau * math.sqrt(1.0 + sigma * sigma) - sigma * math.sqrt(1.0 + tau * tau)
        xip = math.atan2(taup, math.cos(lam))
        etap = math.asinh(math.sin(lam) / math.sqrt(taup * taup + math.cos(lam) ** 2))
        xi, eta = xip, etap
        p, q = 1.0, 0.0
        for j, aj in enumerate(alpha, start=1):
            xi += aj * math.sin(2 * j * xip) * math.cosh(2 * j * etap)
            eta += aj * math.cos(2 * j * xip) * math.sinh(2 * j * etap)
            p += 2 * j * aj * math.cos(2 * j * xip) * math.cosh(2 * j * etap)
            q += 2 * j * aj * math.sin(2 * j * xip) * math.sinh(2 * j * etap)
        east = self.false_e + self.k0 * big_a * eta
        north = self.false_n + self.k0 * big_a * xi
        gamma_p = math.atan(taup / math.sqrt(1.0 + taup * taup) * math.tan(lam))
        gamma_pp = math.atan2(q, p)
        k_p = (
            math.sqrt(1.0 - e * e * math.sin(phi) ** 2)
            * math.sqrt(1.0 + tau * tau)
            / math.sqrt(taup * taup + math.cos(lam) ** 2)
        )
        k_pp = big_a / self.a * math.sqrt(p * p + q * q)
        return east, north, self.k0 * k_p * k_pp, math.degrees(gamma_p + gamma_pp)

    def inverse(self, east: float, north: float) -> tuple[float, float]:
        """(lon, lat) by Newton's method on :meth:`forward` (to about 1e-12 m)."""
        lon, lat = self.lon0, (north - self.false_n) / 111_000.0
        lat = max(-89.0, min(89.0, lat))
        for _ in range(50):
            e0, n0, _k, _g = self.forward(lon, lat)
            de, dn = east - e0, north - n0
            if math.hypot(de, dn) < 1e-9:
                break
            h = 1e-7
            e1, n1, _, _ = self.forward(lon + h, lat)
            e2, n2, _, _ = self.forward(lon, lat + h)
            jac = np.array([[(e1 - e0) / h, (e2 - e0) / h], [(n1 - n0) / h, (n2 - n0) / h]])
            dlon, dlat = np.linalg.solve(jac, [de, dn])
            lon += float(dlon)
            lat += float(dlat)
        return lon, lat


def radii(a: float, f: float, lat: float) -> tuple[float, float]:
    """(M, N): meridian and prime-vertical radii of curvature."""
    e2 = f * (2.0 - f)
    w2 = 1.0 - e2 * math.sin(math.radians(lat)) ** 2
    return a * (1.0 - e2) / w2**1.5, a / math.sqrt(w2)


def euler_radius(a: float, f: float, lat: float, azimuth: float) -> float:
    """Radius of curvature of the normal section at ``azimuth`` (Euler's theorem)."""
    m, n = radii(a, f, lat)
    c, s = math.cos(math.radians(azimuth)), math.sin(math.radians(azimuth))
    return m * n / (n * c * c + m * s * s)


def ecef(a: float, f: float, lon: float, lat: float, h: float) -> tuple[float, float, float]:
    """Geodetic to geocentric Cartesian, closed form."""
    e2 = f * (2.0 - f)
    lo, la = math.radians(lon), math.radians(lat)
    n = a / math.sqrt(1.0 - e2 * math.sin(la) ** 2)
    return (
        (n + h) * math.cos(la) * math.cos(lo),
        (n + h) * math.cos(la) * math.sin(lo),
        (n * (1.0 - e2) + h) * math.sin(la),
    )


def enu_basis(lon: float, lat: float) -> np.ndarray:
    """Rows: east, north, up unit vectors in ECEF."""
    lo, la = math.radians(lon), math.radians(lat)
    return np.array(
        [
            [-math.sin(lo), math.cos(lo), 0.0],
            [-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la)],
            [math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)],
        ]
    )
