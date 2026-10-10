"""Classification normalization and durable classification lookup."""

from __future__ import annotations

import re
from typing import Any

from app.modules.library.metadata_contract import is_english_facet

from .evaluation_text import _clean_text

DDC_RE = re.compile(r"^\d{3}(?:\.\d+)?$")


def _normalize_library_classification(
    raw_ddc: Any,
    raw_path: Any,
    applicable: bool,
) -> tuple[str | None, list[str] | None]:
    if not applicable:
        return None, None

    ddc = _normalize_ddc(raw_ddc)
    path = _normalize_classification_path(raw_path)
    if not ddc or not path:
        return None, None
    return ddc, path


def _normalize_ddc(value: Any) -> str | None:
    text = _clean_text(value, max_len=32)
    if not text:
        return None
    text = text.replace(" ", "")
    return text if DDC_RE.fullmatch(text) else None


def _normalize_classification_path(value: Any) -> list[str] | None:
    if isinstance(value, str):
        values = [v.strip() for v in value.split("->")]
    elif isinstance(value, list):
        values = [str(v).strip() for v in value]
    else:
        return None
    cleaned: list[str] = []
    for item in values:
        text = _clean_text(item, max_len=180)
        if not text:
            continue
        # Classification labels are expected in English for stable taxonomy keys.
        if not is_english_facet(text):
            return None
        cleaned.append(text)
    if len(cleaned) < 2 or len(cleaned) > 8:
        return None
    return cleaned
