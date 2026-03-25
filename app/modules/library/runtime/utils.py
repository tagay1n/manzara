"""Compatibility export of shared runtime utility helpers."""

from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

shared_path = Path(__file__).resolve().parents[2] / "runtime_shared_utils.py"
module_name = "manzara_runtime_shared_utils"
if module_name in sys.modules:
    shared = sys.modules[module_name]
else:
    spec = spec_from_file_location(module_name, shared_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load shared runtime utils from {shared_path}")
    shared = module_from_spec(spec)
    sys.modules[module_name] = shared
    spec.loader.exec_module(shared)

for exported in getattr(shared, "__all__", []):
    globals()[exported] = getattr(shared, exported)
__all__ = list(getattr(shared, "__all__", []))
