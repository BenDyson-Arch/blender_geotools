from __future__ import annotations

import importlib


def resolve_path(path: str):
    """Resolve a ``module:attribute`` path to the referenced Python object."""
    module_name, attr_name = path.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)
