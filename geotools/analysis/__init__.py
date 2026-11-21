"""Analysis maths (bpy-free): sun and Moon, sun hours, horizon and sky events, points on Earth, hydrology, least-cost routes, contours.

- :mod:`.sun`: NREL SPA sun positions, vectorised over time, and time zones.
- :mod:`.site`: where scene points are on Earth (E, N, RL, latitude and
  longitude), grid convergence and the combined scale factor (pyproj).
- :mod:`.sun_hours`: time steps, sun and sky rays and their weights for sun
  hours and insolation; equinox and solstice moments.
- :mod:`.clearsky`: the Ineichen-Perez clear-sky model.
- :mod:`.moon`: Meeus' lunar theory (ch. 47), apparent and topocentric.
- :mod:`.horizon`: the horizon from a point: ray fans, curvature, refraction
  and the profile.
- :mod:`.archaeo`: rise and set events at an epoch, dated sunrises, lunar
  standstills, the declination inversion and alignments.
- :mod:`.hydrology`: 3D surface hydrology on a mesh's faces: flow routing,
  accumulation, filling and depressions.
- :mod:`.least_cost`: directed least-cost routes and travel times over a
  triangulated surface (walking models, Steiner points, a vectorised Dijkstra).
- :mod:`.slicing` and :mod:`.polylines`: contours and sections, chained into
  polylines.
- :mod:`.neighbourhood`: balls on the 3D surface (a voxel hash), area-weighted
  plane and quadric fits, and the resolution rule for a scale.
- :mod:`.surface_metrics`: per-face roughness, normals and geometry quality at
  stated scales, each with its resolution status.

Scope: GeoTools computes only what needs the georeference (where the sun is
from this site at this moment, where a scene point is on Earth) or what
neither a GIS nor stock Blender does well (hydrology, directed least-cost
routes and neighbourhood statistics on the 3D surface).
Blender does the rest: its Sun light casts the shadows, Cycles casts the rays
of sun hours and the horizon (``blender.ray_bake``), and the Geometry Nodes
presets (:mod:`.presets`) compute terrain shape and visibility. Each tool's
accuracy against reference tools is on the validation pages of the docs site
(``docs/validation/``), with the reference tests beside them.
"""
