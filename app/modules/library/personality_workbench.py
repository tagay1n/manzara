"""Canonical-only personality workbench projection and change validation."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.modules.library.publisher_workbench import _DOCUMENT_PAGE_SIZE, _id, _name
from app.modules.library.stats import create_runtime_engine, dispose_runtime_engine
from sqlalchemy import text


def validate_change_set(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - {"renames", "merges", "snapshot_token"}:
        raise ValueError("personality change set has unsupported fields")
    renames_raw = payload.get("renames", [])
    merges_raw = payload.get("merges", [])
    if not isinstance(renames_raw, list) or not isinstance(merges_raw, list):
        raise ValueError("personality changes must be lists")
    renames = []
    seen: set[int] = set()
    for item in renames_raw:
        if not isinstance(item, dict):
            raise ValueError("rename must be an object")
        canonical_id = _id(item.get("canonical_id"), "canonical_id")
        if canonical_id in seen:
            raise ValueError("canonical_id appears more than once")
        seen.add(canonical_id)
        renames.append({"canonical_id": canonical_id, "display_name": _name(item.get("display_name"))})
    merges = []
    for item in merges_raw:
        if not isinstance(item, dict) or set(item) - {"canonical_ids", "display_name"}:
            raise ValueError("merge must contain canonical_ids and display_name")
        ids = [_id(value, "canonical_id") for value in item.get("canonical_ids", [])]
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise ValueError("merge needs two distinct personalities")
        if seen.intersection(ids):
            raise ValueError("personality appears in multiple changes")
        seen.update(ids)
        merges.append({"canonical_ids": ids, "display_name": _name(item.get("display_name"))})
    return {"snapshot_token": str(payload.get("snapshot_token") or ""), "renames": renames, "merges": merges}


def get_personalities(db: Any) -> dict[str, Any]:
    canonicals = [row for row in db.list_normalization_canonicals("personality") if row.get("status") == "active" and row.get("identity_key")]
    aliases = db.list_normalization_aliases("personality")
    by_id: dict[int, list[dict[str, Any]]] = {int(row["canonical_id"]): [] for row in canonicals}
    for alias in aliases:
        canonical_id = int(alias.get("canonical_id") or 0)
        if canonical_id in by_id and alias.get("decision_status") == "linked" and alias.get("successful_model"):
            by_id[canonical_id].append(alias)
    items = []
    for canonical in canonicals:
        canonical_id = int(canonical["canonical_id"])
        linked = by_id[canonical_id]
        aliases_for_row = sorted({str(row.get("raw_name") or "").strip() for row in linked if str(row.get("raw_name") or "").strip()})
        items.append({"key": f"canonical:{canonical_id}", "canonical_id": canonical_id,
            "display_name": str(canonical.get("display_name") or ""), "aliases": aliases_for_row,
            "document_count": sum(int(row.get("docs_count") or 0) for row in linked),
            "is_new": False})
    items.sort(key=lambda item: (item["display_name"].casefold(), item["canonical_id"]))
    token = hashlib.sha256(json.dumps([(item["canonical_id"], item["display_name"], item["aliases"], item["document_count"]) for item in items], ensure_ascii=False).encode()).hexdigest()
    return {"available": True, "items": items, "personality_count": len(items), "snapshot_token": token}


def _names(db: Any, personality_key: str) -> list[str]:
    raw_id = str(personality_key).removeprefix("canonical:")
    if not str(personality_key).startswith("canonical:") or not raw_id.isdecimal():
        raise ValueError("personality_key must identify a canonical personality")
    canonical_id = int(raw_id)
    names = [str(row.get("raw_name") or "").strip() for row in db.list_normalization_aliases_for_canonical("personality", canonical_id)]
    return sorted({name for name in names if name})


def list_personality_documents(db: Any, personality_key: str, *, page: int = 1) -> dict[str, Any]:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    names = _names(db, personality_key)
    engine, config_source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                WITH relations AS (
                    SELECT m.md5, relation.key AS role, item.value AS item
                    FROM metadata m CROSS JOIN LATERAL jsonb_each(m.schema_org::jsonb) relation
                    CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(relation.value)='array' THEN relation.value WHEN jsonb_typeof(relation.value)='object' THEN jsonb_build_array(relation.value) ELSE '[]'::jsonb END) item
                    WHERE m.lib IS TRUE AND m.schema_org IS NOT NULL AND relation.key IN ('author','editor','translator','illustrator','contributor')
                ), people AS (
                    SELECT md5, BTRIM(item->>'name') raw_name FROM relations WHERE item->>'@type'='Person'
                    UNION ALL SELECT md5, BTRIM(nested.value->>'name') FROM relations
                    CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN item->>'@type'='Role' AND jsonb_typeof(item->'contributor')='array' THEN item->'contributor' WHEN item->>'@type'='Role' AND jsonb_typeof(item->'contributor')='object' THEN jsonb_build_array(item->'contributor') ELSE '[]'::jsonb END) nested
                    WHERE nested.value->>'@type'='Person'
                ) SELECT DISTINCT p.md5, d.ya_path FROM people p JOIN document d ON d.md5=p.md5
                WHERE p.raw_name = ANY(:names) ORDER BY d.ya_path ASC NULLS LAST, p.md5 ASC
                LIMIT :limit OFFSET :offset
            """), {"names": names, "limit": _DOCUMENT_PAGE_SIZE + 1, "offset": (page - 1) * _DOCUMENT_PAGE_SIZE}).mappings().all()
    finally:
        dispose_runtime_engine(engine)
    return {"available": True, "config_source": config_source, "personality_key": personality_key,
        "page": page, "page_size": _DOCUMENT_PAGE_SIZE, "has_more": len(rows) > _DOCUMENT_PAGE_SIZE,
        "items": [{"md5": str(row["md5"]), "label": str(row.get("ya_path") or row["md5"])} for row in rows[:_DOCUMENT_PAGE_SIZE]]}


def apply_personalities(db: Any, payload: Any) -> dict[str, Any]:
    validated = validate_change_set(payload)
    if validated["snapshot_token"] != get_personalities(db)["snapshot_token"]:
        raise ValueError("personality snapshot conflict; reload and try again")
    return db.apply_personality_change_set(validated)
