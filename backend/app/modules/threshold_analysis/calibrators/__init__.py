"""Calibrator registry. Auto-discovers product calibrators in this package.

To add a product: drop a module here exposing a `CALIBRATOR` object with a
`product_types` list. Dispatch is by product type; unknown products fall back to
the default (Cash Bonds per-check distribution method).
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Dict

_REGISTRY: Dict[str, object] = {}
_DEFAULT = None


def _discover():
    global _DEFAULT
    if _REGISTRY:
        return
    for info in pkgutil.iter_modules(__path__):
        if info.name == "base":
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        cal = getattr(mod, "CALIBRATOR", None)
        if cal is None:
            continue
        for pt in getattr(cal, "product_types", []):
            _REGISTRY[pt] = cal
        if getattr(cal, "is_default", False):
            _DEFAULT = cal


def get_calibrator(product_type: str):
    _discover()
    return _REGISTRY.get(product_type, _DEFAULT)


def list_supported():
    _discover()
    return sorted(_REGISTRY.keys())
