"""Bounded text cleanup, evidence excerpts, and response log formatting."""

from __future__ import annotations

import json
import re
from typing import Any

EXCERPT_PARTS = 3


EXCERPT_SEPARATOR = "\n\n[...]\n\n"


CODE_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", flags=re.DOTALL)


BLANK_LINES_RE = re.compile(r"\n{3,}")


WHITESPACE_RE = re.compile(r"\s+")


def _build_content_excerpt(text: str, max_chars: int) -> str | None:
    if max_chars <= 0:
        return None

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = CODE_FENCE_RE.sub("\n", normalized)
    normalized = BLANK_LINES_RE.sub("\n\n", normalized).strip()
    if not normalized:
        return None
    if len(normalized) <= max_chars:
        return normalized

    chunk = max_chars // EXCERPT_PARTS
    head = normalized[:chunk]
    mid_start = max(0, (len(normalized) // 2) - (chunk // 2))
    middle = normalized[mid_start : mid_start + chunk]
    tail = normalized[-chunk:]
    excerpt = EXCERPT_SEPARATOR.join([head, middle, tail])
    return excerpt[:max_chars]


def _format_response_for_log(response_text: str | None) -> str:
    """Pretty-print JSON responses for readable logs, fallback to plain text."""
    if response_text is None:
        return ""
    raw = response_text.strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _extract_candidate_strings(
    value: Any, dict_keys: tuple[str, ...] = ("name", "value")
) -> list[str]:
    values = value if isinstance(value, list) else [value]
    output: list[str] = []
    for item in values:
        if isinstance(item, str):
            if item.strip():
                output.append(item)
        elif isinstance(item, dict):
            for key in dict_keys:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    output.append(candidate)
                    break
    return output


def _drop_none_values(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _clean_text(value: Any, max_len: int = 1000) -> str | None:
    if value is None:
        return None
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    if not text:
        return None
    return text[:max_len]
