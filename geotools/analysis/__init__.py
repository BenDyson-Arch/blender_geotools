"""M5 analysis (bpy-free): the sun, sun hours, and points, distances and areas on Earth.

- :mod:`.sun`: NREL SPA sun positions, vectorised over time, and time zones.
- :mod:`.site`: where scene points are on Earth (E, N, RL, latitude and
  longitude), grid convergence and the combined scale factor (pyproj).
- :mod:`.sun_hours`: time steps, sun and sky rays and their weights for sun
  hours and insolation; equinox and solstice moments.
- :mod:`.clearsky`: the Ineichen-Perez clear-sky model.

The design is in ``docs/design/m5-analysis.md``.
"""
