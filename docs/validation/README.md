# Validation

Independent evidence that each analysis tool is correct and follows the GIS
definition it claims, or a statement of where and why it differs. One page per
area: the definitions, reference tools and versions, cases, tolerances,
results and a verdict per tool.

## Terrain shape and archaeological visualisation

[terrain.md](terrain.md): slope, slope (percent), aspect (grid and true north),
profile, plan (tangential) and mean curvature, rugosity, TPI, TRI, local
relief model, sky-view factor and positive and negative openness, live and
fast (bake), against closed forms, GDAL 3.12.0, WhiteboxTools 2.4.0 and
rvt-py 2.2.1. All agree; plan-curvature naming, TPI sampling and the horizon
search region are documented convention differences. No bugs found. Resolved:
the plan curvature preset is labelled Plan (Tangential) Curvature and the TPI
default is 96 samples.

## Archaeoastronomy

[archaeoastronomy.md](archaeoastronomy.md): the horizon profile (curvature,
terrestrial refraction, the Cycles ray search), Bennett refraction, Meeus'
Moon and its parallax, the lunar standstills, the obliquity at ancient epochs,
rise and set azimuths, dated sunrises and Check Alignment, against closed
forms, Skyfield 1.53 with JPL DE440s and DE441, ERFA 2.0.1.5 (Vondrák 2011
long-term precession), astropy 7.1.0 and GRASS GIS 8.5.0 r.horizon. All agree.
Two bugs fixed: Compute Events failed at the ends of the epoch range, and the
horizon could miss a near crest by up to 0.1° with curvature on. Open: the
stated accuracy of Laskar's obliquity at the ends (42″ from Vondrák at
8000 BCE), the sun's parallax in the alignment declination, the horizon's
fine-step bias and fixed refraction conditions.
