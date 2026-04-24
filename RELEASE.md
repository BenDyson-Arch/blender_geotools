# Release Guide

## Before tagging

1. Update the version tuple in `__init__.py`.
2. Add a dated entry to `CHANGELOG.md`.
3. Verify the README still matches the supported Blender version and install flow.
4. Run the focused validation checks used for recent import fixes.
5. Package the add-on as a `.zip` from the repository parent directory.

## Suggested release artifact name

`blender_geotools-<version>.zip`

## Suggested GitHub release notes structure

### Highlights

- New importers or analysis capabilities.
- Compatibility changes for the supported Blender release.
- Important bug fixes affecting import or write-back.

### Installation

- Download the attached `.zip`.
- In Blender, use **Edit → Preferences → Add-ons → Install from Disk…**.
- Enable **GeoTools** after installation.

### Verification

- Smoke-test one GeoJSON import.
- Smoke-test one GeoPackage import.
- Smoke-test one 3D Tiles or DEM import if those modules changed.
