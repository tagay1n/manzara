"""Bounded text cleanup and evidence excerpts."""

from __future__ import annotations

import re
from typing import Any

from app.runtime_config import config_integer

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

    parts = config_integer("metadata", "evaluation_excerpt_parts", minimum=2, maximum=10)
    chunk = max(1, max_chars // parts)
    starts = [0, *[max(0, (len(normalized) * index // (parts - 1)) - (chunk // 2))
                   for index in range(1, parts - 1)], len(normalized) - chunk]
    excerpt = EXCERPT_SEPARATOR.join(normalized[start:start + chunk] for start in starts)
    return excerpt[:max_chars]


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
