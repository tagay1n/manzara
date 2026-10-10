"""Filesystem paths for task run logs and artifacts."""

from __future__ import annotations

import re
from pathlib import Path


def safe_task_slug(value: str) -> str:
    """Return the filesystem-safe task directory name used by the runtime."""
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    return re.sub(r"[^a-z0-9._-]+", "_", text)


def run_log_path(root: Path, task_id: str, run_id: int) -> Path:
    """Resolve one run log below the configured task-runs root."""
    resolved_run_id = int(run_id)
    if resolved_run_id <= 0:
        raise ValueError("run_id must be positive")
    return Path(root) / safe_task_slug(task_id) / f"run-{resolved_run_id}.log"


__all__ = [
    "run_log_path",
    "safe_task_slug",
]
