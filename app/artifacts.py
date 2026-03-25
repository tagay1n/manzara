"""Shared artifact-root path helpers."""

from __future__ import annotations

import os
from pathlib import Path


def artifacts_root() -> Path:
    """Return global artifacts root directory (default: ~/.manzara)."""
    raw = str(os.environ.get("MANZARA_ARTIFACTS_ROOT") or "~/.manzara").strip() or "~/.manzara"
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def flow_artifacts_dir(flow_name: str) -> Path:
    """Return per-flow artifact directory under global root."""
    safe = str(flow_name or "").strip().lower() or "misc"
    path = artifacts_root() / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_runs_dir() -> Path:
    """Return directory that stores per-run task logs."""
    path = artifacts_root() / "task_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path

