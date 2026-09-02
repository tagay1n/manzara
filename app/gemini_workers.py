"""Shared worker-count contract for Gemini-backed tasks."""

from __future__ import annotations

import os
import re
import sys
import threading
from typing import Any, TextIO

from app.gemini_config import load_gemini_keys


GEMINI_WORKERS_ENV = "MANZARA_GEMINI_WORKERS"
GEMINI_WORKERS_DEFAULT = 1
_WORKER_LOG_LOCK = threading.Lock()
GEMINI_TASK_IDS = frozenset(
    {
        "library.metadata_extract",
        "maintenance.monocorpus_meta_evaluate",
        "library.collection_validate",
        "library.personality_suggestions_refresh",
        "library.publisher_suggestions_refresh",
    }
)


def configured_gemini_account_count() -> int:
    """Return the number of distinct configured accounts that have keys."""
    return len({item.account_id for item in load_gemini_keys()})


def validate_gemini_workers(value: Any, *, maximum: int | None = None) -> int:
    """Validate a strict integral worker count within configured capacity."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("workers must be an integer")
    limit = configured_gemini_account_count() if maximum is None else int(maximum)
    if limit < 1:
        raise ValueError("No Gemini accounts are configured")
    if value < 1 or value > limit:
        raise ValueError(f"workers must be between 1 and {limit}")
    return value


def resolve_gemini_workers(explicit: int | None = None) -> int:
    """Resolve CLI > environment > one, then validate against account count."""
    if explicit is not None:
        return validate_gemini_workers(explicit)
    raw = str(os.environ.get(GEMINI_WORKERS_ENV) or "").strip()
    if not raw:
        return validate_gemini_workers(GEMINI_WORKERS_DEFAULT)
    if not raw.isdigit():
        raise ValueError(f"{GEMINI_WORKERS_ENV} must be an integer")
    return validate_gemini_workers(int(raw))


def _safe_worker_id(value: str | None) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-._") or "coordinator"


def current_gemini_worker_id(prefix: str) -> str:
    """Return a stable one-based ID for the current executor thread."""
    match = re.search(r"(?:_|-)(\d+)$", threading.current_thread().name)
    index = int(match.group(1)) + 1 if match else 1
    return f"{_safe_worker_id(prefix)}-{index}"


def emit_gemini_worker_log(
    message: Any,
    *,
    worker_id: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """Emit atomic worker-attributed stdout lines for the task log collector."""
    output = stream or sys.stdout
    prefix = f"[worker={_safe_worker_id(worker_id)}]"
    physical_lines = str(message or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if physical_lines and physical_lines[-1] == "":
        physical_lines.pop()
    if not physical_lines:
        physical_lines = [""]
    payload = "".join(f"{prefix} {line}\n" for line in physical_lines)
    with _WORKER_LOG_LOCK:
        output.write(payload)
        output.flush()


__all__ = [
    "GEMINI_TASK_IDS",
    "GEMINI_WORKERS_DEFAULT",
    "GEMINI_WORKERS_ENV",
    "configured_gemini_account_count",
    "current_gemini_worker_id",
    "emit_gemini_worker_log",
    "resolve_gemini_workers",
    "validate_gemini_workers",
]
