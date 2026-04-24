"""conftest.py — stubs out Blender's bpy before any test module is imported.

Placed in tests/ (which has no __init__.py) so pytest can load this file
via --import-mode=importlib without first importing blender_geotools/__init__.py.
"""

import sys
from unittest.mock import MagicMock

_BPY_MODULES = [
    'bpy',
    'bpy.types',
    'bpy.props',
    'bpy.utils',
    'bpy.path',
    'bpy.app',
    'bpy.context',
    'bpy.ops',
    'mathutils',
]

for _mod in _BPY_MODULES:
    sys.modules.setdefault(_mod, MagicMock())
