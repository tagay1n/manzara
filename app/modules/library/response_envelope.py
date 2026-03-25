"""Shared response envelope helpers for library API payloads."""

from __future__ import annotations

from typing import Any


def _config_source_value(config_source: Any) -> str | None:
    if config_source is None:
        return None
    text = str(config_source).strip()
    return text or None


def available_payload(*, config_source: Any = None, **payload: Any) -> dict[str, Any]:
    """Build a successful library payload with standard envelope fields."""
    return {
        "available": True,
        "error": None,
        "config_source": _config_source_value(config_source),
        **payload,
    }


def unavailable_payload(
    error: Exception | str,
    *,
    config_source: Any = None,
    **payload: Any,
) -> dict[str, Any]:
    """Build a failed library payload with standard envelope fields."""
    return {
        "available": False,
        "error": str(error),
        "config_source": _config_source_value(config_source),
        **payload,
    }


__all__ = ["available_payload", "unavailable_payload"]
