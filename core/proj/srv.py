# Portions derived from BlenderGIS (https://github.com/domlysz/BlenderGIS)
# Copyright (C) domlysz — GNU General Public License v3.0
#
# MapTiler Coordinates API (successor to epsg.io)
# API docs: https://docs.maptiler.com/cloud/api/coordinates/

import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

USER_AGENT = "blender_geotools/1.0"
DEFAULT_TIMEOUT = 5    # seconds; used for search
REPROJ_TIMEOUT = 30    # seconds; used for point reprojection


class MapTilerError(RuntimeError):
    """Raised when the MapTiler API responds with an error or invalid payload."""


class MapTilerCoordinates:
    """
    Thin wrapper around the MapTiler Coordinates REST API.

    All network errors are raised to the caller — no silent swallowing.
    Use try/except at call sites to handle offline/missing-key situations.
    """

    def __init__(self, api_key: str):
        api_key = (api_key or "").strip()
        if not api_key:
            raise ValueError("MapTiler API key is required")
        self.api_key = api_key

    def _request_json(self, url: str, timeout: int) -> dict:
        rq = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            response = urlopen(rq, timeout=timeout)
            raw = response.read().decode("utf-8")
        except HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            raise MapTilerError(f"HTTP {exc.code} from MapTiler: {body or exc.reason}") from exc
        except URLError as exc:
            raise MapTilerError(f"Cannot reach MapTiler: {exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MapTilerError(f"MapTiler returned invalid JSON: {raw[:200]}") from exc

    # ------------------------------------------------------------------

    def search(self, query: str) -> list:
        """
        Search for coordinate systems by name or EPSG code.

        Returns a list of result dicts with at minimum:
            {'id': {'code': int}, 'name': str}

        Raises URLError / HTTPError on network failure.
        """
        q = quote(str(query), safe=":*")
        url = (
            f"https://api.maptiler.com/coordinates/search/{q}.json"
            f"?exports=true&key={self.api_key}"
        )
        log.debug("MapTiler search: %s", url)
        obj = self._request_json(url, DEFAULT_TIMEOUT)
        results = obj.get("results", [])
        log.debug("MapTiler returned %d results", len(results))
        return results

    def reproj_pt(self, epsg1: int, epsg2: int, x: float, y: float) -> tuple:
        """Reproject a single point via the MapTiler API. Returns (x, y)."""
        url = (
            f"https://api.maptiler.com/coordinates/transform/{x},{y}.json"
            f"?s_srs={epsg1}&t_srs={epsg2}&key={self.api_key}"
        )
        log.debug("MapTiler reproj: %s", url)
        payload = self._request_json(url, REPROJ_TIMEOUT)
        try:
            obj = payload["results"][0]
        except Exception as exc:
            raise MapTilerError(f"Unexpected MapTiler transform payload: {payload}") from exc
        if obj.get("x") is None or obj.get("y") is None:
            raise MapTilerError(
                "MapTiler could not compute the requested transform. "
                "The CRS pair may be unsupported for this location or outside the valid area of use."
            )
        return float(obj["x"]), float(obj["y"])

    def reproj_pts(self, epsg1: int, epsg2: int, points: list) -> list:
        """
        Reproject a list of (x, y) tuples via the MapTiler API.
        Batches requests at 50 points each.
        Returns list of (x, y) tuples.
        """
        if not points:
            return []
        if len(points) == 1:
            return [self.reproj_pt(epsg1, epsg2, *points[0])]

        url_template = (
            f"https://api.maptiler.com/coordinates/transform/{{pts}}.json"
            f"?s_srs={epsg1}&t_srs={epsg2}&key={self.api_key}"
        )
        precision = 4
        encoded = [",".join(str(round(v, precision)) for v in p) for p in points]
        results = []
        for i in range(0, len(encoded), 50):
            batch = encoded[i : i + 50]
            url = url_template.format(pts=";".join(batch))
            log.debug("MapTiler reproj batch: %s", url)
            payload = self._request_json(url, REPROJ_TIMEOUT)
            try:
                batch_results = payload["results"]
            except Exception as exc:
                raise MapTilerError(f"Unexpected MapTiler batch payload: {payload}") from exc
            for pt in batch_results:
                if pt.get("x") is None or pt.get("y") is None:
                    raise MapTilerError(
                        "MapTiler could not compute one or more transformed points. "
                        "The CRS pair may be unsupported for this location or outside the valid area of use."
                    )
                results.append((float(pt["x"]), float(pt["y"])))
        return results

    def test(self) -> dict:
        """Run a minimal health check against the Search and Transform endpoints."""
        search_results = self.search("EPSG:4326")
        reproj_result = self.reproj_pt(4326, 3857, 0.0, 0.0)
        return {
            "search_count": len(search_results),
            "reproj_result": reproj_result,
        }

    def get_esri_wkt(self, epsg: int) -> str | None:
        """Retrieve ESRI WKT for a given EPSG code."""
        try:
            results = self.search(str(epsg))
            return results[0]["exports"]["wkt"]
        except Exception:
            log.error("Could not fetch ESRI WKT for EPSG:%s", epsg, exc_info=True)
            return None


# Backward-compat alias (BlenderGIS used EPSGIO)
EPSGIO = MapTilerCoordinates
