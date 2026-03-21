"""ISBN helpers for canonicalization in schema.org metadata flows."""

from __future__ import annotations

from typing import Any

import isbnlib


def canonicalize_isbn_values(value: Any) -> list[str] | None:
    """Return unique canonical ISBN values (10/13), or None when none are valid."""
    result: list[str] = []
    seen: set[str] = set()
    for raw in _iter_isbn_candidates(value):
        normalized_raw = _normalize_isbn_text(raw)
        scraped = isbnlib.get_isbnlike(normalized_raw, level="strict") or []
        if not scraped:
            # Fallback for noisy values where strict scraping fails.
            scraped = [normalized_raw]
        for token in scraped:
            canonical = isbnlib.canonical(_normalize_isbn_text(token))
            if not canonical:
                continue
            if isbnlib.is_isbn10(canonical):
                normalized = canonical.upper()
            elif isbnlib.is_isbn13(canonical):
                normalized = canonical
            else:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
    return result or None


def _iter_isbn_candidates(value: Any) -> list[str]:
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        raw = None
        if isinstance(item, dict):
            raw = item.get("value") or item.get("name")
        else:
            raw = item
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            out.append(text)
    return out


def _normalize_isbn_text(text: str) -> str:
    """Normalize common OCR/script confusions before isbnlib parsing."""
    return (
        text.replace("Х", "X")
        .replace("х", "x")
        .replace("—", "-")
        .replace("–", "-")
    )
