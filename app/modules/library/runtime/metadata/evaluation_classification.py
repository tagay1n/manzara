"""Classification normalization and durable classification lookup."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app.modules.library.metadata_contract import is_english_facet
from app.modules.library.runtime.models import Classification

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


def _resolve_classification_id(
    session, ddc_raw: str | None, path_raw: list[str] | None
) -> int | None:
    """Resolve existing classification id or create a new pending one."""
    if not ddc_raw or not path_raw:
        return None
    ddc = _normalize_ddc(ddc_raw)
    path = _normalize_classification_path(path_raw)
    if not ddc or not path:
        return None
    path_key = _classification_path_key(path)

    stmt = select(Classification).where(
        Classification.ddc == ddc,
        Classification.path_en_key == path_key,
    )
    existing = session.scalars(stmt).first()
    if existing:
        return existing.id

    created = Classification(
        ddc=ddc,
        path_en=path,
        path_en_key=path_key,
        status="pending",
        created_by="gemini",
    )
    session.add(created)
    session.flush()
    return created.id


def _classification_path_key(path: list[str]) -> str:
    return "|".join([p.casefold() for p in path])
