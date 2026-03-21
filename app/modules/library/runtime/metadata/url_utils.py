"""Helpers for validating and normalizing URL values in metadata."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

MAX_METADATA_URL_LENGTH = 500
MAX_METADATA_URLS = 10
ALLOWED_URL_SCHEMES = {"http", "https"}


def normalize_url(value: Any, max_len: int = MAX_METADATA_URL_LENGTH) -> str | None:
    """Return a safe normalized URL or None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > max_len:
        return None
    if any(ch.isspace() for ch in text):
        return None
    parsed = urlparse(text)
    if parsed.scheme.casefold() not in ALLOWED_URL_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    return text


def normalize_url_list(value: Any, max_items: int = MAX_METADATA_URLS) -> list[str] | None:
    """Normalize list-like URL input while dropping invalid entries."""
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        url = normalize_url(item)
        if not url:
            continue
        key = url.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= max_items:
            break
    return out or None
