"""Publisher registry read model and batch change-set validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.db import Database
from app.modules.library.normalization_queries import _query_aggregated_mentions

_MAX_CHANGES = 200
_MAX_NAME = 240


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
    rows, config_source = _query_aggregated_mentions("publisher", limit=20000)
    canonicals = db.list_normalization_canonicals("publisher")
    aliases = db.list_normalization_aliases("publisher")
    active = {int(item["canonical_id"]): item for item in canonicals if item.get("status") == "active"}
    aliases_by_canonical: dict[int, set[str]] = {key: set() for key in active}
    linked_names: dict[str, int] = {}
    for alias in aliases:
        canonical_id = alias.get("canonical_id")
        if alias.get("decision_status") == "linked" and canonical_id is not None and int(canonical_id) in active:
            raw_name = str(alias.get("raw_name") or "").strip()
            if raw_name:
                aliases_by_canonical[int(canonical_id)].add(raw_name)
                linked_names[raw_name] = int(canonical_id)
    counts: dict[str, int] = {str(row.get("raw_name") or "").strip(): int(row.get("docs_count") or 0) for row in rows}
    items: list[dict[str, Any]] = []
    for canonical_id, canonical in active.items():
        display_name = str(canonical.get("display_name") or "").strip()
        names = aliases_by_canonical[canonical_id] | ({display_name} if display_name else set())
        items.append({"key": f"canonical:{canonical_id}", "canonical_id": canonical_id, "raw_name": None, "display_name": display_name, "aliases": sorted(names, key=str.casefold), "document_count": sum(counts.get(name, 0) for name in names), "is_new": False})
    for raw_name, count in counts.items():
        if raw_name and raw_name not in linked_names:
            items.append({"key": f"raw:{raw_name}", "canonical_id": None, "raw_name": raw_name, "display_name": raw_name, "aliases": [], "document_count": count, "is_new": True})
    items.sort(key=lambda item: (not bool(item["is_new"]), str(item["display_name"]).casefold(), str(item["key"])))
    token_source = [(item["key"], item["display_name"], item["aliases"], item["document_count"]) for item in items]
    return {"available": True, "config_source": config_source, "items": items, "new_count": sum(1 for item in items if item["is_new"]), "publisher_count": sum(1 for item in items if not item["is_new"]), "snapshot_token": hashlib.sha256(json.dumps(token_source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()}


def apply_publishers(db: Database, payload: Any) -> dict[str, Any]:
    return db.apply_publisher_change_set(validate_change_set(payload))
