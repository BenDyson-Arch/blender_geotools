"""Which Cycles device a GeoTools bake runs on: the Bake Device preference.

Every Cycles bake (texture bakes, sun hours, the horizon profile and the
presets' fast paths) sets ``scene.cycles.device`` here and nowhere else
(``tests/core/test_code_rules.py`` checks), and puts the user's device back
with the rest of its settings afterwards (``bake_texture._Saved``).

* **Auto** uses the GPU when Cycles' own preferences (Preferences > System >
  Cycles Render Devices) have a compute device type and at least one ticked
  device of that type that is present; otherwise the CPU.
* **GPU** is Auto that says so when it falls back to the CPU.
* **CPU** always uses the CPU.

Only reads Cycles' preferences. It never sets the compute device type or
ticks a device, and never calls the Cycles preferences' ``refresh_devices``
or ``get_device_list``: those add every device not yet listed, ticked, to
the user's preferences. ``_cycles.available_devices`` (no side effects) is
asked only when a device is ticked, so Cycles would enumerate the devices
for the render anyway. With the GPU type's devices Cycles also uses a ticked
CPU, as it does for renders.

Results agree between devices within each bake's tolerance: hit tests are
exact, but the shader's float32 sums may differ in the last bits
(``tests/blender/test_bake_device.py`` compares a small bake of each kind).
"""

from __future__ import annotations

from dataclasses import dataclass

import bpy

from .common import ADDON_PACKAGE

DEVICE_ITEMS = (
    (
        "AUTO",
        "Auto",
        (
            "The GPU when Cycles has an enabled compute device (Preferences > System), "
            "otherwise the CPU"
        ),
    ),
    ("GPU", "GPU", "The GPU set up in Cycles' preferences; the CPU (with a note) if none is"),
    ("CPU", "CPU", "Always the CPU"),
)

GPU_TYPES = {
    "CUDA": "CUDA",
    "OPTIX": "OptiX",
    "HIP": "HIP",
    "METAL": "Metal",
    "ONEAPI": "oneAPI",
}
"""Cycles' compute device types and their names in its preferences."""

NO_GPU = (
    "No GPU is enabled for Cycles (Preferences > System > Cycles Render Devices), "
    "so the bake used the CPU."
)


@dataclass(frozen=True)
class BakeDevice:
    """The device a bake uses."""

    device: str
    """``scene.cycles.device``: "CPU" or "GPU"."""
    label: str
    """For reports: "CPU", or "GPU (OptiX: NVIDIA GeForce RTX 4080 SUPER)"."""
    short: str
    """For panels: "CPU", or "GPU (OptiX)"."""
    note: str = ""
    """Why the GPU asked for is not used; empty otherwise."""


def gpu_devices() -> tuple[str, list[str], bool]:
    """Cycles' compute device type, its ticked devices that are present, and the CPU.

    Returns ``(type, names, cpu)``: ``type`` is "NONE" without a compute
    device type; ``names`` are the ticked devices of that type that
    ``_cycles`` finds; ``cpu`` is whether a ticked CPU joins them.
    """
    addon = bpy.context.preferences.addons.get("cycles")
    if addon is None:
        return "NONE", [], False
    prefs = addon.preferences
    kind = prefs.compute_device_type or "NONE"
    if kind not in GPU_TYPES:
        return "NONE", [], False
    ticked = {d.id: d.name for d in prefs.devices if d.use and d.type == kind}
    if not ticked:
        return kind, [], False
    import _cycles  # Cycles' own module; there whenever the add-on is

    present = _cycles.available_devices(kind)
    names = [ticked[d[2]] for d in present if d[1] == kind and d[2] in ticked]
    cpu_ids = {d[2] for d in present if d[1] == "CPU"}
    cpu = any(d.use and d.type == "CPU" and d.id in cpu_ids for d in prefs.devices)
    return kind, names, cpu


def preference() -> str:
    """The Bake Device preference: "AUTO", "GPU" or "CPU"."""
    addon = bpy.context.preferences.addons.get(ADDON_PACKAGE)
    return addon.preferences.bake_device if addon is not None else "AUTO"


def choose(wanted: str | None = None) -> BakeDevice:
    """The device for a bake, from ``wanted`` (default: the preference)."""
    wanted = wanted or preference()
    if wanted == "CPU":
        return BakeDevice("CPU", "CPU", "CPU")
    kind, names, cpu = gpu_devices()
    if not names:
        return BakeDevice("CPU", "CPU", "CPU", NO_GPU if wanted == "GPU" else "")
    label = f"GPU ({GPU_TYPES[kind]}: {', '.join(names)}{' + CPU' if cpu else ''})"
    return BakeDevice("GPU", label, f"GPU ({GPU_TYPES[kind]})")


def use(scene: bpy.types.Scene, wanted: str | None = None) -> BakeDevice:
    """Set ``scene.cycles.device`` for a bake; the caller has saved the user's."""
    chosen = choose(wanted)
    scene.cycles.device = chosen.device
    return chosen
