"""Managed JSON-LD classification and genre terms."""

from __future__ import annotations

import json
from typing import Any

from app.modules.library.metadata_terms import defined_term, termset_name

from .evaluation_patch import _normalize_genre
from .evaluation_text import _clean_text

DDC_PROPERTY_NAME = "DDC"


UDC_PROPERTY_NAME = "UDC"


CATEGORY_PATH_TERMSET = "CategoryPath"


GENRE_TERMSET = "Genre"


MANAGED_TERMSETS = {
    DDC_PROPERTY_NAME.casefold(),
    UDC_PROPERTY_NAME.casefold(),
    CATEGORY_PATH_TERMSET.casefold(),
    GENRE_TERMSET.casefold(),
}


def _sync_auxiliary_terms_in_about(
    schema_org: dict[str, Any],
    applicable: bool,
    ddc: str | None,
    path: list[str] | None,
) -> tuple[dict[str, Any], list[str]]:
    updated = dict(schema_org)
    raw_about = updated.get("about")
    raw_genre = updated.get("genre")

    before_about = (
        json.dumps(raw_about, ensure_ascii=False, sort_keys=True)
        if raw_about is not None
        else None
    )
    before_genre = _normalize_genre(raw_genre)

    existing_about_items = (
        raw_about if isinstance(raw_about, list) else ([raw_about] if raw_about else [])
    )
    existing_genre_terms = _extract_about_term_values(
        existing_about_items, GENRE_TERMSET
    )
    existing_udc_terms = _extract_about_term_values(
        existing_about_items, UDC_PROPERTY_NAME
    )
    retained_about_items: list[Any] = []
    for item in existing_about_items:
        if _is_managed_about_term(item):
            continue
        if (
            isinstance(item, dict)
            and str(item.get("@type") or "").strip().casefold() == "definedterm"
        ):
            normalized_item = dict(item)
            normalized_item.pop("name", None)
            retained_about_items.append(normalized_item)
            continue
        retained_about_items.append(item)

    genres = _normalize_genre(raw_genre) or existing_genre_terms
    if genres:
        updated["genre"] = genres
    else:
        updated.pop("genre", None)

    for udc in existing_udc_terms:
        retained_about_items.append(_build_defined_term(udc, UDC_PROPERTY_NAME))

    if applicable and ddc and path:
        retained_about_items.append(_build_defined_term(ddc, DDC_PROPERTY_NAME))
        retained_about_items.append(
            _build_defined_term(" > ".join(path), CATEGORY_PATH_TERMSET)
        )

    if retained_about_items:
        updated["about"] = retained_about_items
    else:
        updated.pop("about", None)

    updated.pop("additionalProperty", None)

    applied: list[str] = []
    after_about = (
        json.dumps(updated.get("about"), ensure_ascii=False, sort_keys=True)
        if updated.get("about") is not None
        else None
    )
    if before_about != after_about:
        applied.append("about")
    if "additionalProperty" in schema_org:
        applied.append("additionalProperty")
    if before_genre != _normalize_genre(updated.get("genre")):
        applied.append("genre")
    return updated, applied


def _build_defined_term(term_code: str, termset: str) -> dict[str, Any]:
    return defined_term(term_code, termset)


def _is_managed_about_term(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    termset = termset_name(item.get("inDefinedTermSet"))
    return bool(termset and termset.casefold() in MANAGED_TERMSETS)


def _extract_about_term_values(items: list[Any], termset: str) -> list[str]:
    target_set = termset.casefold()
    values: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_set = termset_name(item.get("inDefinedTermSet"))
        if not item_set or item_set.casefold() != target_set:
            continue
        value = _clean_text(item.get("termCode"), max_len=500) or _clean_text(
            item.get("name"), max_len=500
        )
        if not value:
            continue
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(value)
    return values
