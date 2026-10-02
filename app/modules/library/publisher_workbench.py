"""Publisher registry read model and batch change-set validation."""

from __future__ import annotations

from typing import Any

from app.db import Database
from app.modules.library.normalization_queries import (
    _mentions_cte_sql,
)
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine
from sqlalchemy import text

_MAX_CHANGES = 200
_MAX_NAME = 240
_DOCUMENT_PAGE_SIZE = 10


def _name(value: Any, field: str = "display_name") -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{field} must be non-empty")
    if len(result) > _MAX_NAME:
        raise ValueError(f"{field} is too long")
    return result


def _id(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _names(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = [_name(item, field) for item in value]
    if len(set(result)) != len(result):
        raise ValueError(f"{field} contains duplicates")
    return result


def validate_change_set(payload: Any) -> dict[str, Any]:
    """Validate the deliberately small publisher-only external contract."""
    if not isinstance(payload, dict):
        raise ValueError("publisher change set must be an object")
    allowed = {"renames", "keeps", "merges", "snapshot_token"}
    extra = set(payload) - allowed
    if extra:
        raise ValueError("unsupported publisher change-set fields")
    renames_raw = payload.get("renames", [])
    keeps_raw = payload.get("keeps", [])
    merges_raw = payload.get("merges", [])
    if not all(isinstance(value, list) for value in (renames_raw, keeps_raw, merges_raw)):
        raise ValueError("publisher changes must be lists")
    if len(renames_raw) + len(keeps_raw) + len(merges_raw) > _MAX_CHANGES:
        raise ValueError("publisher change set is too large")

    renames: list[dict[str, Any]] = []
    rename_ids: set[int] = set()
    for item in renames_raw:
        if not isinstance(item, dict):
            raise ValueError("rename must be an object")
        canonical_id = _id(item.get("canonical_id"), "canonical_id")
        if canonical_id in rename_ids:
            raise ValueError("canonical_id appears more than once")
        rename_ids.add(canonical_id)
        renames.append({"canonical_id": canonical_id, "display_name": _name(item.get("display_name"))})

    keeps = _names(keeps_raw, "keeps")
    merge_members: set[tuple[str, Any]] = set()
    merges: list[dict[str, Any]] = []
    for item in merges_raw:
        if not isinstance(item, dict):
            raise ValueError("merge must be an object")
        canonical_ids_raw = item.get("canonical_ids", [])
        if not isinstance(canonical_ids_raw, list):
            raise ValueError("canonical_ids must be a list")
        canonical_ids = [_id(value, "canonical_id") for value in canonical_ids_raw]
        raw_names = _names(item.get("raw_names", []), "raw_names")
        if len(canonical_ids) != len(set(canonical_ids)):
            raise ValueError("canonical_ids contains duplicates")
        if len(canonical_ids) + len(raw_names) < 2:
            raise ValueError("merge needs at least two publishers")
        for member in [("canonical", value) for value in canonical_ids] + [("raw", value) for value in raw_names]:
            if member in merge_members:
                raise ValueError("publisher appears in multiple merge groups")
            merge_members.add(member)
        merges.append({"canonical_ids": canonical_ids, "raw_names": raw_names, "display_name": _name(item.get("display_name"))})
    if set(keeps) & {value for kind, value in merge_members if kind == "raw"}:
        raise ValueError("raw publisher appears in keep and merge")
    return {"renames": renames, "keeps": keeps, "merges": merges, "snapshot_token": str(payload.get("snapshot_token") or "")}


def get_publishers(db: Database) -> dict[str, Any]:
    """Project active publishers and unresolved exact raw metadata names."""
    from app.modules.library.publisher_merge_contract import build_inventory, inventory_fingerprint
    items = build_inventory(db)
    items.sort(key=lambda item: (not item['is_new'], item['display_name'].casefold(), item['key']))
    return {"available": True, "items": items,
            "new_count": sum(item['is_new'] for item in items),
            "publisher_count": sum(not item['is_new'] for item in items),
            "snapshot_token": inventory_fingerprint(items)}


def _publisher_names(db: Database, publisher_key: str) -> list[str]:
    key = str(publisher_key or "").strip()
    if key.startswith("raw:"):
        return [_name(key.removeprefix("raw:"), "publisher_key")]
    if not key.startswith("canonical:"):
        raise ValueError("publisher_key must identify a publisher row")
    raw_id = key.removeprefix("canonical:")
    if not raw_id.isascii() or not raw_id.isdecimal():
        raise ValueError("publisher_key must identify a publisher row")
    canonical_id = _id(int(raw_id), "publisher_key")
    canonical = next(
        (
            item
            for item in db.list_normalization_canonicals("publisher")
            if int(item.get("canonical_id") or 0) == canonical_id
            and item.get("status") == "active"
        ),
        None,
    )
    if canonical is None:
        raise ValueError("publisher canonical is missing or inactive")
    names = {_name(canonical.get("display_name"), "publisher name")}
    for alias in db.list_normalization_aliases("publisher"):
        if (
            int(alias.get("canonical_id") or 0) == canonical_id
            and alias.get("decision_status") == "linked"
        ):
            raw_name = str(alias.get("raw_name") or "").strip()
            if raw_name:
                names.add(raw_name)
    return sorted(names, key=str.casefold)


def list_publisher_documents(
    db: Database,
    publisher_key: str,
    *,
    page: int = 1,
) -> dict[str, Any]:
    """List a fixed-size page of documents for one publisher row."""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    names = _publisher_names(db, publisher_key)
    offset = (page - 1) * _DOCUMENT_PAGE_SIZE
    engine, config_source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql("publisher")}
                    SELECT DISTINCT m.md5, d.ya_path
                    FROM mentions m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.raw_name = ANY(:names)
                    ORDER BY d.ya_path ASC NULLS LAST, m.md5 ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {"names": names, "limit": _DOCUMENT_PAGE_SIZE + 1, "offset": offset},
            ).mappings().all()
    finally:
        dispose_runtime_engine(engine)
    items = [
        {
            "md5": str(row.get("md5") or ""),
            "label": str(row.get("ya_path") or row.get("md5") or ""),
        }
        for row in rows[:_DOCUMENT_PAGE_SIZE]
    ]
    return {
        "available": True,
        "config_source": config_source,
        "publisher_key": str(publisher_key).strip(),
        "page": page,
        "page_size": _DOCUMENT_PAGE_SIZE,
        "total": None,
        "has_more": len(rows) > _DOCUMENT_PAGE_SIZE,
        "items": items,
    }


def apply_publishers(db: Database, payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict) and payload.get('use_draft') is True:
        if set(payload) != {'use_draft', 'revision'} or type(payload['revision']) is not int or payload['revision'] < 0:
            raise ValueError('server draft apply requires an integral revision')
        return db.apply_publisher_change_set(payload)
    return db.apply_publisher_change_set(validate_change_set(payload))
