#!/usr/bin/env bash
# GeoTools writes GeoPackages and GeoJSON (CRS definitions as WKT, section
# chainage as M); GDAL 3.12.0 in Docker validates and reads them back.
# Not run in CI. Needs Docker and uv. From the repository root:
#
#   scripts/validation/exports/run.sh
set -euo pipefail

IMAGE=ghcr.io/osgeo/gdal:ubuntu-small-3.12.0
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../../.." && pwd)
WORK="$ROOT/.scratch/exports"

uv run --no-project --python 3.13 --with numpy==2.5.4 --with pyproj==3.8.0 \
  python "$HERE/write_geotools.py" "$WORK"
docker run --rm --cpus 3 --memory 6g \
  -v "$HERE:/scripts:ro" -v "$WORK:/data" "$IMAGE" python3 -I /scripts/read_gdal.py /data
