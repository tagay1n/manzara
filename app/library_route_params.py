"""Shared query/filter helpers for Library API route modules."""

from __future__ import annotations

from fastapi import Query


def q_text(default: str = "", *, max_length: int = 120):
    """Text query parameter with bounded length."""
    return Query(default, max_length=max_length)


def q_page(default: int = 1):
    """Pagination page query parameter."""
    return Query(default, ge=1)


def q_page_size(default: int = 25, *, max_value: int = 100):
    """Pagination page-size query parameter."""
    return Query(default, ge=1, le=max_value)


def q_non_negative(default: int = 0):
    """Non-negative integer query parameter."""
    return Query(default, ge=0)


def q_limit(default: int, *, minimum: int, maximum: int):
    """Bounded limit query parameter."""
    return Query(default, ge=minimum, le=maximum)


def q_ratio(default: float, *, minimum: float = 0.0, maximum: float = 1.0):
    """Bounded ratio query parameter."""
    return Query(default, ge=minimum, le=maximum)


def parse_csv_tokens(value: str) -> list[str]:
    """Parse comma-separated text into trimmed non-empty tokens."""
    return [item.strip() for item in str(value or "").split(",") if item.strip()]
