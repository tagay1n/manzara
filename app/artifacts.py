"""Shared retention-oriented local storage path helpers."""

from __future__ import annotations

import os
from pathlib import Path


_STORAGE_GUIDE = """Manzara local storage layout

cache/
  Safe to remove while tasks are idle. Data will be downloaded or regenerated.
  cache/source-documents is automatically bounded by documents.cache_max_gib.

workspaces/
  Completed-run directories may be removed. Never remove an active run.

logs/
  Task run logs. Remove only when the corresponding run history is no longer needed.

durable/
  Exports, operational evidence, and migration snapshots. Keep unless intentionally
  discarding those artifacts.

private/
  Credentials and other secrets. Do not remove unless reauthentication is acceptable.
"""


def _ensure_storage_guide(root: Path) -> None:
    guide = root / "STORAGE_LAYOUT.txt"
    try:
        if not guide.exists() or guide.read_text(encoding="utf-8") != _STORAGE_GUIDE:
            guide.write_text(_STORAGE_GUIDE, encoding="utf-8")
    except OSError:
        # Storage paths remain usable when a read-only deployment cannot refresh
        # the human-facing guide.
        pass


def artifacts_root() -> Path:
    """Return global local-storage root directory (default: ~/.manzara)."""
    raw = str(os.environ.get("MANZARA_ARTIFACTS_ROOT") or "~/.manzara").strip() or "~/.manzara"
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    _ensure_storage_guide(path)
    return path


def _safe_parts(parts: tuple[object, ...]) -> tuple[str, ...]:
    resolved: list[str] = []
    for raw in parts:
        part = str(raw or "").strip().lower()
        if not part or part in {".", ".."} or "/" in part or "\\" in part:
            raise ValueError(f"Invalid artifact path part: {raw!r}")
        resolved.append(part)
    return tuple(resolved)


def _area_dir(area: str, *parts: object, private: bool = False) -> Path:
    path = artifacts_root().joinpath(area, *_safe_parts(parts))
    path.mkdir(parents=True, exist_ok=True)
    if private:
        private_root = artifacts_root() / "private"
        credentials_root = private_root / "credentials"
        for protected in (private_root, credentials_root, path):
            if protected.exists():
                protected.chmod(0o700)
    return path


def cache_dir(*parts: object) -> Path:
    """Return a regenerable cache directory."""
    return _area_dir("cache", *parts)


def workspace_dir(
    flow_name: object,
    operation: object,
    *,
    run_id: int | None = None,
) -> Path:
    """Return a task workspace, optionally scoped to a positive run id."""
    parts: tuple[object, ...] = (flow_name, operation)
    if run_id is not None:
        resolved_run_id = int(run_id)
        if resolved_run_id <= 0:
            raise ValueError("run_id must be positive")
        parts += (f"run-{resolved_run_id}",)
    return _area_dir("workspaces", *parts)


def durable_dir(*parts: object) -> Path:
    """Return a directory for user-retained or operational evidence."""
    return _area_dir("durable", *parts)


def durable_path(*parts: object) -> Path:
    """Resolve a durable path without creating its leaf directory."""
    return artifacts_root().joinpath("durable", *_safe_parts(parts))


def private_credentials_dir(scope: object) -> Path:
    """Return an owner-only credential directory for one integration scope."""
    return _area_dir("private", "credentials", scope, private=True)


def task_runs_dir() -> Path:
    """Return directory that stores per-run task logs."""
    return _area_dir("logs", "task-runs")


__all__ = [
    "artifacts_root",
    "cache_dir",
    "durable_dir",
    "durable_path",
    "private_credentials_dir",
    "task_runs_dir",
    "workspace_dir",
]
