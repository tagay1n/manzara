"""Strict, flow-independent catalog commands and search fields."""

from typing import Any
import re

from app.catalog.schema_org import SUPPORTED_TYPES


class CatalogConflict(Exception):
    """The reviewed revision no longer describes the current record."""


class CatalogNotFound(Exception):
    """The requested catalog record does not exist."""


PUBLICATION_FIELDS = {
    "name", "work_type", "description", "edition", "date_published",
    "page_count", "inclusion", "classification_id", "collection_id", "languages",
}
ENTITY_FIELDS = {
    "display_name", "kind", "approval", "notes", "surname_full", "surname_initials",
    "name_full", "name_initials", "father_name_full", "father_name_initials", "title", "sex",
}
EDITABLE = {
    "publication": PUBLICATION_FIELDS,
    "entity": ENTITY_FIELDS,
    "document": {"selected", "complete", "restricted", "mime_type"},
    "collection": {"title", "notes", "include_in_library"},
    "classification_node": {"label_en", "label_tt", "parent_id"},
}
SEARCH_FIELDS = {
    "documents": {"md5", "title", "mime_type", "inclusion", "source_path", "language"},
    "publications": {"name", "work_type", "description", "edition", "date_published", "inclusion"},
    "entities": {"display_name", "kind", "approval", "notes"},
    "names": {"raw_name", "kind"},
    "collections": {"title", "notes"},
    "classifications": {"ddc", "label_en", "label_tt"},
}


def integer(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be nonblank text")
    return value.strip()


def validate_patch(kind: str, payload: Any) -> dict[str, Any]:
    if kind not in EDITABLE or not isinstance(payload, dict):
        raise ValueError("unsupported catalog patch")
    integer(payload.get("revision"), "revision")
    if set(payload) - EDITABLE[kind] - {"revision"} or len(payload) < 2:
        raise ValueError("patch contains unsupported or missing fields")
    result = dict(payload)
    for field in {"selected", "complete", "restricted", "include_in_library"} & result.keys():
        boolean(result[field], field)
    for field in {"classification_id", "collection_id", "parent_id", "page_count"} & result.keys():
        if result[field] is not None:
            integer(result[field], field)
    for field in {"name", "display_name", "title", "work_type", "label_en"} & result.keys():
        # Untitled publications are valid; a null name represents that explicitly.
        if field == "name" and result[field] is None:
            continue
        result[field] = nonblank(result[field], field)
    for field, choices in {
        "inclusion": {"pending", "included", "excluded"},
        "kind": {"person", "organization", "unknown"},
        "approval": {"unconfirmed", "confirmed"},
    }.items():
        if field in result and (not isinstance(result[field], str) or result[field] not in choices):
            raise ValueError(f"unsupported {field}")
    if "languages" in result and (
        not isinstance(result["languages"], list)
        or any(not isinstance(item, str) or not item.strip() for item in result["languages"])
    ):
        raise ValueError("languages must be an array of nonblank strings")
    if "work_type" in result and result["work_type"] not in SUPPORTED_TYPES:
        raise ValueError("unsupported work_type")
    if result.get("date_published") is not None and not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", result["date_published"]):
        raise ValueError("date_published must preserve year/month/day precision")
    for field, value in result.items():
        if field not in {"revision", "page_count", "parent_id", "classification_id", "collection_id",
                         "selected", "complete", "restricted", "include_in_library", "languages"}:
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{field} must be text or null")
    return result


def parse_filters(resource: str, filters: Any) -> list[dict[str, Any]]:
    if resource not in SEARCH_FIELDS or not isinstance(filters, list) or len(filters) > 12:
        raise ValueError("unsupported search filters")
    result = []
    for item in filters:
        if not isinstance(item, dict) or set(item) - {"field", "pattern", "ignore_case"}:
            raise ValueError("unsupported search filter")
        if not isinstance(item.get("field"), str) or item["field"] not in SEARCH_FIELDS[resource]:
            raise ValueError("unsupported search field")
        pattern = item.get("pattern")
        if not isinstance(pattern, str) or len(pattern) > 512:
            raise ValueError("pattern must be text of at most 512 characters")
        result.append({**item, "ignore_case": boolean(item.get("ignore_case", False), "ignore_case")})
    return result
