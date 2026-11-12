"""M5 analysis (bpy-free): the sun, sun hours, points on Earth, hydrology, contours.

- :mod:`.sun`: NREL SPA sun positions, vectorised over time, and time zones.
- :mod:`.site`: where scene points are on Earth (E, N, RL, latitude and
  longitude), grid convergence and the combined scale factor (pyproj).
- :mod:`.sun_hours`: time steps, sun and sky rays and their weights for sun
  hours and insolation; equinox and solstice moments.
- :mod:`.clearsky`: the Ineichen-Perez clear-sky model.
- :mod:`.hydrology`: 3D surface hydrology on a mesh's faces: flow routing,
  accumulation, filling and depressions.
- :mod:`.slicing` and :mod:`.polylines`: contours and sections, chained into
  polylines.

The design is in ``docs/design/m5-analysis.md``.
"""
