"""Database-owned upstream metadata helpers for Library prompts."""

from __future__ import annotations

from typing import Any, Mapping


PROMPT_EXCLUDED_FIELDS = frozenset(
    {
        "available_pages",
        "doc_card_url",
        "download_code",
        "doc_url",
        "access",
        "lang",
    }
)


def sanitize_upstream_metadata(value: Any) -> dict[str, Any] | None:
    """Return prompt-safe source metadata without mutating persisted JSON."""
    if not isinstance(value, Mapping):
        return None
    sanitized = {
        str(key): item
        for key, item in value.items()
        if str(key) not in PROMPT_EXCLUDED_FIELDS
    }
    return sanitized or None


__all__ = ["PROMPT_EXCLUDED_FIELDS", "sanitize_upstream_metadata"]
