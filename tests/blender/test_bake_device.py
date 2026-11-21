"""The Bake Device preference (``blender.bake_device``): which Cycles device bakes use.

Without a Cycles GPU Auto and GPU bake on the CPU, and the comparison of
CPU and GPU results is a SKIP; on CI it is a SKIP unless GEOTOOLS_TEST_GPU
is set. With one (CUDA, OptiX, HIP,
Metal or oneAPI), the comparison sets it up in the throwaway profile's Cycles
preferences and bakes sun hours, Sky Visibility (fast) and a texture layer on
each device:

* the texture bake emits a stored value, so the texels are identical;
* sun hours and Sky Visibility hit-test the same rays (exact) and sum their
  weights in float32 in the shader, so they agree to float32 round-off: far
  below one ray's weight, so no sample's shadow can differ.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

import _harness as h
import bpy
import numpy as np
from test_analysis_presets import _grid, _layers
from test_analysis_presets import _reset as _reset_presets
from test_analysis_presets import _values as _preset_values
from test_analysis_presets_2 import _box, _object
from test_bake_texture import _bake_op, _pixels, _registry, _saved_file, _source
from test_sun_hours import _equator_equinox, _plane, _wall
from test_sun_hours import _reset as _reset_sun
from test_sun_hours import _run as _run_sun
from test_sun_hours import _values as _sun_values

GPU_KINDS = ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI")
"""Cycles' compute device types, the first present one is compared."""
FLOAT32_SUMS = {"rtol": 1e-5, "atol": 1e-6}
"""Ray sums agree between devices to float32 round-off of the shader's running sum."""


def _device():
    return h.submodule("blender.bake_device")


def _prefs():
    return bpy.context.preferences.addons[h.CONFIG.package].preferences


def _cycles_prefs():
    return bpy.context.preferences.addons["cycles"].preferences


def _gpu_kind() -> str | None:
    """The first compute device type with a present device, or None."""
    import _cycles

    # get_device_types() also has a sixth flag, HIP RT, which is not a device type.
    flags = _cycles.get_device_types()
    compiled = dict(zip(("CUDA", "OPTIX", "HIP", "METAL", "ONEAPI"), flags, strict=False))
    for kind in GPU_KINDS:
        if compiled.get(kind) and any(d[1] == kind for d in _cycles.available_devices(kind)):
            return kind
    return None


@contextlib.contextmanager
def _bake_device(value: str) -> Iterator[None]:
    prefs = _prefs()
    before = prefs.bake_device
    prefs.bake_device = value
    try:
        yield
    finally:
        prefs.bake_device = before


@contextlib.contextmanager
def _compute(kind: str, *, tick: bool = True) -> Iterator[None]:
    """Cycles' compute device type ``kind``, in the throwaway profile.

    ``tick``: list (and so tick) its devices, as opening Cycles' preferences
    does; otherwise none is listed.
    """
    prefs = _cycles_prefs()
    before = prefs.compute_device_type
    prefs.compute_device_type = kind
    if tick:
        prefs.refresh_devices()
    else:
        prefs.devices.clear()
    try:
        yield
    finally:
        prefs.compute_device_type = before


# --- the choice -------------------------------------------------------------------------------


def test_preference_and_cpu_fallback() -> None:
    device = _device()
    assert _prefs().bake_device == "AUTO"
    assert device.preference() == "AUTO"
    with _compute("NONE", tick=False):
        assert device.choose("CPU") == device.BakeDevice("CPU", "CPU", "CPU")
        assert device.choose("AUTO") == device.BakeDevice("CPU", "CPU", "CPU")
        chosen = device.choose("GPU")
        assert chosen.device == "CPU" and chosen.note == device.NO_GPU
    kind = _gpu_kind()
    if kind is not None:
        # A compute device type with no device listed: CPU, and nothing gets listed.
        with _compute(kind, tick=False):
            assert device.choose("AUTO").device == "CPU"
            assert len(_cycles_prefs().devices) == 0


def test_device_restored_and_reported() -> None:
    """Every bake reports its device and puts the scene's back."""
    _reset_sun()
    _equator_equinox()
    _plane(size=2)
    scene = bpy.context.scene
    scene.cycles.device = "GPU"  # the user's, whether or not a GPU is there
    with _bake_device("CPU"):
        result = _run_sun(step_minutes=60, tolerance=0.0)
        assert (result["device"], result["device_short"]) == ("CPU", "CPU")
        assert scene.cycles.device == "GPU"
        fast = h.submodule("blender.preset_bake")
        summary = fast.compute(
            bpy.context, "sky_visibility", [bpy.data.objects["Ground"]], inputs={"Rays": 16}
        )
        assert summary["device"] == "CPU" and "on the CPU" in fast.fast_summary(summary)
        assert scene.cycles.device == "GPU"
    with _compute("NONE", tick=False), _bake_device("GPU"):
        result = _run_sun(step_minutes=60, tolerance=0.0)
        assert result["device"] == "CPU" and _device().NO_GPU in result["notes"]
        assert scene.cycles.device == "GPU"
    scene.cycles.device = "CPU"


# --- CPU and GPU agree --------------------------------------------------------------------------


def _on_each_device(run) -> dict[str, object]:
    """``run()`` with the Bake Device CPU, then GPU; their results by device."""
    out = {}
    for value in ("CPU", "GPU"):
        with _bake_device(value):
            out[value] = run(value)
    return out


def _sun_hours(device: str) -> tuple[np.ndarray, np.ndarray]:
    result = _run_sun(step_minutes=10, tolerance=0.0, insolation=True, diffuse=True)
    assert result["device"].startswith(device), result["device"]
    plane = bpy.data.objects["Ground"]
    return _sun_values(plane, "sun_hours").copy(), _sun_values(plane, "insolation").copy()


def _sky_visibility(device: str) -> np.ndarray:
    objects = [bpy.data.objects["Flat"], bpy.data.objects["Lid"]]
    fast = h.submodule("blender.preset_bake")
    result = fast.compute(
        bpy.context, "sky_visibility", objects, name="lidded", inputs={"Rays": 256}
    )
    assert result["device"].startswith(device), result["device"]
    return _preset_values(objects[0], "lidded")[0].copy()


def _texture(device: str) -> np.ndarray:
    used = []
    module = _device()
    real = module.use
    module.use = lambda scene, wanted=None: used.append(real(scene, wanted)) or used[-1]
    try:
        result, error = _bake_op(source_layer="slope")
    finally:
        module.use = real
    assert result == {"FINISHED"}, error
    assert [d.device for d in used] == [device], used
    L = _layers()
    item = L.find_layer(bpy.context.scene, "slope_tex")
    return _pixels(L.layer_image(item, bpy.data.objects["Strip"])).copy()


def test_cpu_and_gpu_agree() -> None:
    # CI runners can expose a GPU (Metal on the macOS arm64 runners) whose first
    # Cycles kernel compile outlasts the job's time limit, so on CI the comparison
    # runs only when asked for.
    if os.environ.get("CI") and not os.environ.get("GEOTOOLS_TEST_GPU"):
        raise h.Skipped("CPU/GPU comparison on CI needs GEOTOOLS_TEST_GPU=1")
    kind = _gpu_kind()
    if kind is None:
        raise h.Skipped("no Cycles GPU (CUDA, OptiX, HIP, Metal or oneAPI) on this machine")
    with _compute(kind):
        chosen = _device().choose("AUTO")
        assert chosen.device == "GPU", chosen
        assert chosen.short == f"GPU ({_device().GPU_TYPES[kind]})", chosen

        _reset_sun()
        _equator_equinox()
        _plane()
        _wall()
        sun = _on_each_device(_sun_hours)
        for column, name in enumerate(("sun hours", "insolation")):
            cpu, gpu = sun["CPU"][column], sun["GPU"][column]
            np.testing.assert_allclose(gpu, cpu, **FLOAT32_SUMS, err_msg=name)
        # One 10-minute ray is 1/6 h: the same samples are in shadow.
        assert np.abs(sun["GPU"][0] - sun["CPU"][0]).max() < 1e-3

        _reset_presets()
        flat = _grid("Flat", 8, 4.0, lambda x, y: 0.0 * x)
        co, faces = _box((-0.5, -0.5, 0.4), (0.5, 0.5, 0.5))
        _object("Lid", co, faces=faces, source=True)
        assert flat.name == "Flat"
        sky = _on_each_device(_sky_visibility)
        np.testing.assert_allclose(sky["GPU"], sky["CPU"], **FLOAT32_SUMS)

        with _saved_file():
            _registry(_source())
            texels = _on_each_device(_texture)
            np.testing.assert_array_equal(texels["GPU"], texels["CPU"])
