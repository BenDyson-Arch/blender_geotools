#!/usr/bin/env bash
# Regenerate tests/data/validation/geodesy/formats/ (not run in CI).
#
# GDAL writes the synthetic sources, GeoTools imports and exports them, and GDAL
# reads GeoTools' files back. Needs Docker and uv. Run from the repository root:
#
#   scripts/validation/geodesy/run_formats.sh
set -euo pipefail

IMAGE=ghcr.io/osgeo/gdal:ubuntu-small-3.12.0
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
WORK="$ROOT/.scratch/geodesy-formats"
OUT="$ROOT/tests/data/validation/geodesy/formats"

rm -rf "$WORK"
mkdir -p "$WORK" "$OUT"
gdal() {
  docker run --rm --cpus 3 --memory 6g \
    -v "$HERE:/scripts:ro" -v "$WORK:/out" "$IMAGE" python3 -I /scripts/formats_gdal.py "$@" /out
}
gdal sources
uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \
  python "$HERE/formats_geotools.py" "$WORK"
gdal readback

# The fixtures: GDAL's files and what GDAL read. GeoTools' own outputs are not
# kept; the CI test writes them again and compares with gdal_readback.json.
cp "$WORK"/{rfc7946.geojson,legacy_epsg4326.geojson,wgs84.gpkg,mga56.gpkg} "$OUT/"
cp "$WORK"/{area,point,south_up,rotated,geographic,tiled_bigtiff}.tif "$OUT/"
cp "$WORK"/{gdal_sources.json,gdal_readback.json} "$OUT/"
echo "wrote $OUT"
