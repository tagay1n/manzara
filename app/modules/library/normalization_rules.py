"""Entity configuration, name normalization, and similarity rules."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable

ENTITY_TYPES = {"personality", "publisher"}


def _entity_config(entity_type: str) -> Dict[str, Any]:
    normalized = str(entity_type or "").strip().lower()
    if normalized == "personality":
        return {
            "entity_type": "personality",
            "schema_field": "author",
            "name_keys": ["name", "alternateName"],
            "marker_regex": r"(^|\\s)(улы|кызы|оглы|оглу|ович|евич|овна|евна)(\\s|$)",
            "marker_label": "patronymic",
            "marker_count_field": "marker_count",
            "model": "personality-normalizer",
        }
    if normalized == "publisher":
        return {
            "entity_type": "publisher",
            "schema_field": "publisher",
            "name_keys": ["name", "legalName", "alternateName"],
            "marker_regex": r"(^|\\s)(ооо|зао|ао|пао|ip|llc|ltd|inc|corp|company|press|publisher|publishing|нәшрият|нәшрияты|издательство|типография)(\\s|$)",
            "marker_label": "org_marker",
            "marker_count_field": "marker_count",
            "model": "publisher-normalizer",
        }
    raise ValueError("Unsupported entity_type")


def _normalize_text(value: Any) -> str:
    text_value = str(value or "").strip().lower()
    text_value = re.sub(r"[^0-9a-zа-яёәҗңөүһіїғқҫ]+", " ", text_value, flags=re.IGNORECASE)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value


def _confidence_band(score: float) -> str:
    if score >= 0.9:
        return "high"
    if score >= 0.75:
        return "medium"
    return "low"


def _similarity(left: str, right: str) -> float:
    left_norm = _normalize_text(left)
    right_norm = _normalize_text(right)
    if not left_norm and not right_norm:
        return 1.0
    if not left_norm or not right_norm:
        return 0.0
    seq = SequenceMatcher(None, left_norm, right_norm).ratio()
    left_tokens = {token for token in left_norm.split(" ") if token}
    right_tokens = {token for token in right_norm.split(" ") if token}
    union = left_tokens | right_tokens
    jac = 0.0 if not union else len(left_tokens & right_tokens) / len(union)
    return round((0.65 * seq) + (0.35 * jac), 4)


def _canonical_name_map(canonicals: Iterable[Dict[str, Any]]) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for item in canonicals:
        canonical_id = int(item.get("canonical_id") or 0)
        if canonical_id <= 0:
            continue
        result[canonical_id] = str(item.get("display_name") or "")
    return result
