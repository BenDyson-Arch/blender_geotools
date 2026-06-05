# blender_geotools

GeoJSON, GeoPackage, DEM, and OGC 3D Tiles tools for Blender 5.1, with shared
CRS infrastructure and geospatial analysis utilities.

This repository is structured as a shared internal API with source and analysis
addons layered on top of it. The goal is to make it easier to add new importers,
linked-layer workflows, and analysis tools without wiring each feature directly
into the addon root.

This repository can still be used directly as a Blender add-on source tree and
packaged as a `.zip` for a GitHub release.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the extension model and registry
contracts.

---

## Requirements

- **Blender 5.1 or later** (ships Python 3.13)
- Optional: PyProj and/or GDAL for full CRS reprojection support (see below)

The addon loads and works for GeoJSON, GeoPackage, and 3D Tiles import **without
any external packages**. The built-in reprojection engine handles WGS84 ↔ Web
Mercator and WGS84 ↔ UTM without dependencies. PyProj or GDAL are required for
anything else (national grids, custom proj4 strings, etc.) and for the DEM importer.

---

## Installation

### From GitHub Releases

1. Download the release `.zip` from the GitHub Releases page.
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk…**
3. Select the downloaded `.zip`.
4. Enable **GeoTools** in the add-on list.

### From source

1. Download or clone this repository.
2. In Blender: **Edit → Preferences → Add-ons → Install from Disk…**
3. Select the `blender_geotools` folder (or a `.zip` of it).
4. Enable **GeoTools** in the addon list.

The addon panel appears in **View3D → Sidebar → GeoTools**.

---

## Installing optional dependencies (PyProj and GDAL)

Blender ships its own Python interpreter, isolated from any system Python you may have
installed. Packages must be installed into **Blender's Python**, not your system Python.

> **These commands must be run in a terminal opened as Administrator.**
> Right-click PowerShell or Command Prompt and choose **Run as administrator**.

### Find your Blender Python

Blender 5.x on Windows installs Python at:

```text
C:\Program Files\Blender Foundation\Blender 5.x\5.x\python\bin\python.exe
```

For **Blender 5.1** (the minimum supported version):

```text
C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe
```

All commands below use this path. Adjust the version numbers if yours differs.

---

### Install PyProj

PyProj is the recommended reprojection backend. It handles virtually any CRS pair
and is straightforward to install.

**Open PowerShell as Administrator, then run:**

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pip install pyproj
```

Verify the install:

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -c "import pyproj; print(pyproj.__version__)"
```

---

### Install GDAL

GDAL is the heavier option and is required by the DEM importer. On Windows, **do not**
install GDAL via plain `pip install gdal` because the PyPI package does not bundle the
GDAL binaries and will fail to import.

The reliable route on Windows is the unofficial Christoph Gohlke wheel.

**Step 1 — Check your Python version**

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" --version
```

This should print `Python 3.13.x`.

**Step 2 — Download the correct wheel**

Go to: https://github.com/cgohlke/geospatial-wheels/releases

Download the GDAL wheel matching your Python version and architecture.
For Python 3.13, 64-bit Windows, look for a file named:

```text
GDAL-3.x.x-cp313-cp313-win_amd64.whl
```

**Step 3 — Install the wheel**

Open PowerShell as Administrator, then run (adjust the path and filename):

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pip install "C:\Users\YourName\Downloads\GDAL-3.x.x-cp313-cp313-win_amd64.whl"
```

**Step 4 — Verify**

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -c "from osgeo import gdal; print(gdal.__version__)"
```

---

### Install NumPy (for DEM import)

NumPy is used by the DEM importer for mesh generation. It is often already present
in Blender's Python, but if not:

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.1\5.1\python\bin\python.exe" -m pip install numpy
```

---

### Checking dependency status inside Blender

After installing, restart Blender. The addon preferences panel shows which packages
were detected at startup:

**Edit → Preferences → Add-ons → GeoTools → dependency status**

```text
pyproj   ✓  3.7.0
gdal     ✓  3.9.2
numpy    ✓  1.26.4
```

A ✗ in red means the package was not found in Blender's Python.

---

## Architecture overview

The repo now has four main layers:

- `api/` — addon contracts, discovery, common vector settings, and tool registries
- `addons/` — all addon code, split into:
  - `addons/data/` for data-module manifests and implementations
  - `addons/analysis/` for per-tool analysis manifests plus shared analysis core
- `core/` — CRS, provenance, import utilities, and raycasting

Data import modules now live entirely under `addons/data/`. Each analysis tool is
its own addon under `addons/analysis/`, while shared analysis implementation lives
under `addons/analysis/shared/`. That keeps discovery, preferences, and UI
composition separate from the lower-level core infrastructure.

## Reprojection backend

The active backend can be forced in addon preferences. The default is **Auto**, which
picks the best available option in priority order:

| Priority | Backend | Supports |
|---|---|---|
| 1 | PyProj | Any valid CRS pair |
| 2 | GDAL | Any valid CRS pair |
| 3 | Built-in | WGS84 ↔ Web Mercator, WGS84 ↔ UTM only |
| 4 | MapTiler API | Fallback for single points; requires API key |

For most workflows (UTM → WGS84, national grid reprojection) PyProj alone is
sufficient and is the fastest to install.

---

## Release packaging

For a GitHub release, package the repository root so the archive contains the
add-on files directly under the `blender_geotools/` folder.

Example PowerShell command from the parent directory:

```powershell
Compress-Archive -Path .\blender_geotools\* -DestinationPath .\blender_geotools-0.2.0.zip -Force
```

---

## License

GNU General Public License v3.0.
Portions of `core/proj/` are derived from
[BlenderGIS](https://github.com/domlysz/BlenderGIS) © domlysz, GPL-3.0.
