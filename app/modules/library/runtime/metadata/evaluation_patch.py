"""Requested metadata gap normalization and schema patch application."""

from __future__ import annotations

import json
import re
from typing import Any

from app.modules.library.metadata_contract import is_english_facet
from app.modules.library.runtime.metadata.isbn_utils import canonicalize_isbn_values
from app.modules.library.runtime.metadata.schema import MetadataPatch
from app.modules.library.runtime.metadata.url_utils import normalize_url_list

from .evaluation_text import _clean_text, _extract_candidate_strings

YEAR_RE = re.compile(r"(1[5-9]\d{2}|20\d{2})")


INT_RE = re.compile(r"\d+")


def _collect_patch_fields(schema_org: dict | str | None) -> list[str]:
    schema = schema_org if isinstance(schema_org, dict) else {}
    fields = [
        "datePublished",
        "name",
        "author",
        "publisher",
        "genre",
        "description",
    ]
    if schema.get("@type") == "Book":
        fields.extend(("isbn", "numberOfPages"))
    return [
        name
        for name in fields
        if name == "genre" or _is_schema_field_missing(schema, name)
    ]


def _is_schema_field_missing(schema: dict[str, Any], field: str) -> bool:
    value = schema.get(field)
    if field == "publisher":
        if isinstance(value, dict):
            return not _clean_text(value.get("name"))
        return _is_missing(value)
    return _is_missing(value)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set)):
        return len(value) == 0
    if isinstance(value, dict):
        return len(value) == 0
    return False


def _normalize_metadata_patch(
    raw_patch: MetadataPatch | dict[str, Any] | None, doc: Any, config: dict
) -> MetadataPatch | None:
    if not isinstance(raw_patch, dict):
        if isinstance(raw_patch, MetadataPatch):
            raw_patch = json.loads(
                raw_patch.model_dump_json(
                    by_alias=True,
                    exclude_none=True,
                    ensure_ascii=False,
                )
            )
        else:
            raw_patch = {}

    patchable_fields = set(_collect_patch_fields(doc.schema_org))
    patch: dict[str, Any] = {}

    if "isbn" in patchable_fields:
        if isbn_values := _normalize_isbn_values(raw_patch.get("isbn")):
            patch["isbn"] = isbn_values

    if "datePublished" in patchable_fields:
        if date_published := _normalize_date_published(raw_patch.get("datePublished")):
            patch["datePublished"] = date_published

    if "numberOfPages" in patchable_fields:
        number_of_pages = _normalize_number_of_pages(raw_patch.get("numberOfPages"))
        if number_of_pages is not None:
            patch["numberOfPages"] = number_of_pages

    if "name" in patchable_fields:
        if name := _clean_text(raw_patch.get("name"), max_len=600):
            patch["name"] = name

    if "author" in patchable_fields:
        if author := _normalize_author(raw_patch.get("author")):
            patch["author"] = author

    if "publisher" in patchable_fields:
        if publisher := _normalize_publisher(raw_patch.get("publisher")):
            patch["publisher"] = publisher

    if "genre" in patchable_fields:
        if genre := _normalize_genre(raw_patch.get("genre")):
            patch["genre"] = genre

    if "description" in patchable_fields:
        if description := _clean_text(raw_patch.get("description"), max_len=5000):
            patch["description"] = description

    if not patch:
        return None
    return MetadataPatch.model_validate(patch)


def _normalize_isbn_values(value: Any) -> list[str] | None:
    return canonicalize_isbn_values(value)


def _normalize_date_published(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        year = int(value)
        return str(year) if 1500 <= year <= 2100 else None
    raw = _clean_text(value, max_len=40)
    if not raw:
        return None
    raw = raw.replace("/", "-")
    if re.fullmatch(r"\d{4}(-\d{2})?(-\d{2})?", raw):
        year = int(raw[:4])
        return raw if 1500 <= year <= 2100 else None
    match = YEAR_RE.search(raw)
    if not match:
        return None
    year = int(match.group(1))
    return str(year) if 1500 <= year <= 2100 else None


def _normalize_number_of_pages(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 20_000 else None
    if isinstance(value, float):
        int_val = int(value)
        return int_val if 1 <= int_val <= 20_000 else None
    raw = _clean_text(value, max_len=40)
    if not raw:
        return None
    match = INT_RE.search(raw)
    if not match:
        return None
    int_val = int(match.group(0))
    return int_val if 1 <= int_val <= 20_000 else None


def _normalize_author(value: Any) -> list[dict[str, str]] | None:
    names = _extract_candidate_strings(value, dict_keys=("name",))
    if not names:
        return None
    normalized = []
    seen = set()
    for name in names:
        clean = _clean_text(name, max_len=300)
        if not clean or not is_english_facet(clean):
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"@type": "Person", "name": clean})
    return normalized or None


def _normalize_publisher(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict):
        name = _clean_text(value.get("name"), max_len=400)
    else:
        name = _clean_text(value, max_len=400)
    if not name:
        return None
    return {"@type": "Organization", "name": name}


def _normalize_genre(value: Any) -> list[str] | None:
    genres = _extract_candidate_strings(value, dict_keys=("name",))
    seen = set()
    normalized: list[str] = []
    for genre in genres:
        clean = _clean_text(genre, max_len=120)
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    return normalized or None


def _apply_metadata_patch(
    schema_org: dict[str, Any], patch: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    updated = dict(schema_org)
    applied: list[str] = []
    for key, value in patch.items():
        if key == "genre":
            normalized = _normalize_genre(value)
            current = _normalize_genre(updated.get("genre"))
            if normalized and normalized != current:
                updated["genre"] = normalized
                applied.append("genre")
            continue
        if key == "publisher":
            current = updated.get("publisher")
            missing = _is_missing(current) or (
                isinstance(current, dict) and not _clean_text(current.get("name"))
            )
            if missing and value:
                updated["publisher"] = value
                applied.append("publisher")
            continue
        if _is_schema_field_missing(updated, key) and not _is_missing(value):
            updated[key] = value
            applied.append(key)
    return updated, applied


def _sanitize_schema_urls(schema_org: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Drop invalid URL values from schema.org payload."""
    updated = dict(schema_org)
    changed = False

    if "url" in updated:
        normalized = normalize_url_list(updated.get("url"))
        if normalized:
            if updated.get("url") != normalized:
                updated["url"] = normalized
                changed = True
        else:
            updated.pop("url", None)
            changed = True

    based_on = updated.get("isBasedOn")
    if isinstance(based_on, dict):
        normalized_based_on = dict(based_on)
        normalized_urls = normalize_url_list(based_on.get("url"))
        if normalized_urls:
            if based_on.get("url") != normalized_urls:
                normalized_based_on["url"] = normalized_urls
                changed = True
        elif "url" in normalized_based_on:
            normalized_based_on.pop("url", None)
            changed = True
        updated["isBasedOn"] = normalized_based_on

    return updated, changed
