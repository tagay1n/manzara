"""Shared worker-count contract for Gemini-backed tasks."""

from __future__ import annotations

import os
from typing import Any

from app.gemini_config import load_gemini_keys


GEMINI_WORKERS_ENV = "MANZARA_GEMINI_WORKERS"
GEMINI_WORKERS_DEFAULT = 1
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


__all__ = [
    "GEMINI_TASK_IDS",
    "GEMINI_WORKERS_DEFAULT",
    "GEMINI_WORKERS_ENV",
    "configured_gemini_account_count",
    "resolve_gemini_workers",
    "validate_gemini_workers",
]
