"""Canonical collection and review-proposal operations."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import text

from app.db import Database
from app.modules.library.collection_detection import (
    normalize_collection_text,
    title_core,
)
from app.modules.library.collection_constants import COLLECTIONS_PANEL_ID
from app.modules.library.stats import create_runtime_engine


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _set_search_path(conn: Any) -> None:
    import os
    import re

    schema = (
        str(os.environ.get("MANZARA_DB_SCHEMA") or "monocorpus").strip() or "monocorpus"
    )
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        schema = "monocorpus"
    conn.execute(text(f'SET search_path TO "{schema}", public'))


def _collection(
    row: Mapping[str, Any], *, item_count: int | None = None
) -> dict[str, Any]:
    return {
        "collection_id": int(row.get("collection_id") or 0),
        "title": str(row.get("title") or ""),
        "normalized_title": str(row.get("normalized_title") or ""),
        "include_in_library": bool(row.get("include_in_library")),
        "item_count": int(
            item_count if item_count is not None else row.get("item_count") or 0
        ),
        "metadata_template": row.get("metadata_template_json")
        if isinstance(row.get("metadata_template_json"), dict)
        else _json(row.get("metadata_template_json"), {}),
        "notes": str(row.get("notes") or ""),
        "applied_at": row.get("applied_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "status": "approved",
    }


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value else deepcopy(default)
    except (TypeError, json.JSONDecodeError):
        return deepcopy(default)


def get_collection_overview(top_limit: int = 12) -> dict[str, Any]:
    del top_limit
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            _set_search_path(conn)
            row = (
                conn.execute(
                    text(
                        """
                    SELECT
                        (SELECT COUNT(*) FROM library_collections) AS approved_collections,
                        (SELECT COUNT(*) FROM library_collection_items) AS items_linked,
                        (SELECT COUNT(*) FROM library_collection_proposals WHERE status = 'review_ready') AS suggested_collections,
                        (SELECT COUNT(*) FROM library_collection_proposals WHERE status = 'queued_validation') AS awaiting_validation,
                        (SELECT COUNT(*) FROM library_collection_proposals WHERE status = 'rejected') AS rejected_proposals
                    """
                    )
                )
                .mappings()
                .one()
            )
        return {
            "available": True,
            "error": None,
            "config_source": source,
            "stats": dict(row),
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "config_source": source,
            "stats": {},
        }
    finally:
        engine.dispose()


def list_collections(
    *,
    search: str = "",
    status: str = "",
    include: str = "all",
    page: int = 1,
    page_size: int = 25,
    sort: str = "updated_desc",
) -> dict[str, Any]:
    del status, include
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    order = {
        "title_asc": "c.title ASC, c.collection_id ASC",
        "items_desc": "item_count DESC, c.title ASC",
        "updated_asc": "c.updated_at ASC, c.collection_id ASC",
    }.get(sort, "c.updated_at DESC, c.collection_id DESC")
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            _set_search_path(conn)
            params = {
                "search": f"%{search.strip().lower()}%",
                "limit": page_size,
                "offset": (page - 1) * page_size,
            }
            total = conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_collections WHERE :search = '%%' OR LOWER(title) LIKE :search"
                ),
                params,
            ).scalar_one()
            rows = (
                conn.execute(
                    text(
                        f"""
                    SELECT c.*, COUNT(i.md5) AS item_count
                    FROM library_collections c
                    LEFT JOIN library_collection_items i ON i.collection_id = c.collection_id
                    WHERE :search = '%%' OR LOWER(c.title) LIKE :search
                    GROUP BY c.collection_id
                    ORDER BY {order}
                    LIMIT :limit OFFSET :offset
                    """
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        return {
            "available": True,
            "error": None,
            "config_source": source,
            "items": [_collection(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": int(total),
            "total_pages": max(1, math.ceil(int(total) / page_size)),
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "config_source": source,
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 1,
        }
    finally:
        engine.dispose()


def list_collection_proposals(
    *,
    status: str = "review_ready",
    search: str = "",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    allowed = {
        "queued_validation",
        "review_ready",
        "rejected",
        "ai_dismissed",
        "approved",
        "superseded",
        "validation_failed",
    }
    clean_status = status if status in allowed else "review_ready"
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            _set_search_path(conn)
            params = {
                "status": clean_status,
                "search": f"%{search.strip().lower()}%",
                "limit": page_size,
                "offset": (page - 1) * page_size,
            }
            total = conn.execute(
                text(
                    "SELECT COUNT(*) FROM library_collection_proposals WHERE status=:status AND (:search='%%' OR LOWER(proposed_title) LIKE :search)"
                ),
                params,
            ).scalar_one()
            rows = (
                conn.execute(
                    text(
                        """
                    SELECT p.*, COUNT(pi.md5) AS item_count,
                           COUNT(*) FILTER (WHERE pi.gemini_verdict='belongs') AS belongs_count,
                           COUNT(*) FILTER (WHERE pi.gemini_verdict='uncertain') AS uncertain_count
                    FROM library_collection_proposals p
                    LEFT JOIN library_collection_proposal_items pi ON pi.proposal_id=p.proposal_id
                    WHERE p.status=:status AND (:search='%%' OR LOWER(p.proposed_title) LIKE :search)
                    GROUP BY p.proposal_id
                    ORDER BY p.updated_at DESC, p.proposal_id DESC
                    LIMIT :limit OFFSET :offset
                    """
                    ),
                    params,
                )
                .mappings()
                .all()
            )
        items = [
            {
                "proposal_id": int(row["proposal_id"]),
                "proposal_type": row["proposal_type"],
                "target_collection_id": row["target_collection_id"],
                "title": row["proposed_title"],
                "status": row["status"],
                "confidence": float(
                    row["gemini_confidence"] or row["deterministic_score"] or 0
                ),
                "item_count": int(row["item_count"] or 0),
                "belongs_count": int(row["belongs_count"] or 0),
                "uncertain_count": int(row["uncertain_count"] or 0),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]
        return {
            "available": True,
            "error": None,
            "config_source": source,
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": int(total),
            "total_pages": max(1, math.ceil(int(total) / page_size)),
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "config_source": source,
            "items": [],
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 1,
        }
    finally:
        engine.dispose()


def get_collection_proposal_review(proposal_id: int) -> dict[str, Any]:
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            _set_search_path(conn)
            proposal = (
                conn.execute(
                    text(
                        "SELECT * FROM library_collection_proposals WHERE proposal_id=:id"
                    ),
                    {"id": int(proposal_id)},
                )
                .mappings()
                .first()
            )
            if not proposal:
                raise ValueError("Collection proposal not found")
            rows = (
                conn.execute(
                    text(
                        """
                    SELECT pi.*, f.title, f.work_type, f.publication_date, f.issue_number,
                           f.publishers_json, f.authors_json, f.genres_json, f.description,
                           m.lib
                    FROM library_collection_proposal_items pi
                    JOIN library_collection_document_features f ON f.md5=pi.md5
                    LEFT JOIN metadata m ON m.md5=pi.md5
                    WHERE pi.proposal_id=:id
                    ORDER BY COALESCE(pi.gemini_confidence,0) DESC, f.title, pi.md5
                    """
                    ),
                    {"id": int(proposal_id)},
                )
                .mappings()
                .all()
            )
        items = [
            {
                "md5": row["md5"],
                "title": row["title"],
                "work_type": row["work_type"],
                "publication_date": row["publication_date"],
                "issue_number": row["issue_number"],
                "publishers": _json(row["publishers_json"], []),
                "authors": _json(row["authors_json"], []),
                "genres": _json(row["genres_json"], []),
                "description": row["description"],
                "included": bool(row["lib"]),
                "verdict": row["gemini_verdict"],
                "confidence": row["gemini_confidence"],
                "rationale": row["gemini_rationale"],
                "model": row["gemini_model"],
                "selected_by_default": row["gemini_verdict"] == "belongs",
            }
            for row in rows
        ]
        return {
            "available": True,
            "error": None,
            "config_source": source,
            "proposal": {
                "proposal_id": int(proposal["proposal_id"]),
                "proposal_type": proposal["proposal_type"],
                "target_collection_id": proposal["target_collection_id"],
                "title": proposal["proposed_title"],
                "status": proposal["status"],
                "confidence": proposal["gemini_confidence"],
                "rationale": proposal["gemini_rationale"],
                "evidence": _json(proposal["evidence_json"], {}),
            },
            "items": items,
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "config_source": source,
            "proposal": None,
            "items": [],
        }
    finally:
        engine.dispose()


def decide_collection_proposal(
    db: Database,
    proposal_id: int,
    *,
    decision: str,
    selected_md5s: list[str] | None = None,
) -> dict[str, Any]:
    if decision not in {"approve", "reject"}:
        raise ValueError("decision must be approve or reject")
    selected = sorted(set(str(value) for value in (selected_md5s or []) if value))
    now = _now()
    engine, _ = create_runtime_engine()
    try:
        with engine.begin() as conn:
            _set_search_path(conn)
            proposal = (
                conn.execute(
                    text(
                        "SELECT * FROM library_collection_proposals WHERE proposal_id=:id FOR UPDATE"
                    ),
                    {"id": int(proposal_id)},
                )
                .mappings()
                .first()
            )
            if not proposal:
                raise ValueError("Collection proposal not found")
            if proposal["status"] not in {
                "review_ready",
                "validation_failed",
                "ai_dismissed",
            }:
                raise ValueError("Collection proposal is not awaiting review")
            available = set(
                conn.execute(
                    text(
                        "SELECT md5 FROM library_collection_proposal_items WHERE proposal_id=:id"
                    ),
                    {"id": int(proposal_id)},
                )
                .scalars()
                .all()
            )
            if decision == "reject":
                selected = []
            elif not set(selected).issubset(available):
                raise ValueError(
                    "selected_md5s contains documents outside this proposal"
                )
            elif proposal["proposal_type"] == "new_collection" and len(selected) < 2:
                raise ValueError(
                    "A new collection requires at least two selected documents"
                )
            elif proposal["proposal_type"] == "attach_to_collection" and not selected:
                raise ValueError("At least one document must be selected")

            collection_id = int(proposal["target_collection_id"] or 0)
            if decision == "approve" and proposal["proposal_type"] == "new_collection":
                title = str(
                    proposal["gemini_canonical_name"] or proposal["proposed_title"]
                )
                row = (
                    conn.execute(
                        text(
                            """INSERT INTO library_collections (title,normalized_title,include_in_library,metadata_template_json,notes,applied_at,created_at,updated_at) VALUES (:title,:normalized,1,'{}','',NULL,:now,:now) RETURNING collection_id"""
                        ),
                        {
                            "title": title,
                            "normalized": normalize_collection_text(title),
                            "now": now,
                        },
                    )
                    .mappings()
                    .one()
                )
                collection_id = int(row["collection_id"])
                conn.execute(
                    text(
                        """INSERT INTO library_collection_signatures (collection_id,signature_type,normalized_value,provenance,created_at,updated_at) VALUES (:id,'canonical_title',:value,'proposal_approval',:now,:now)"""
                    ),
                    {
                        "id": collection_id,
                        "value": title_core(title) or normalize_collection_text(title),
                        "now": now,
                    },
                )

            if decision == "approve":
                conflicts = (
                    conn.execute(
                        text(
                            "SELECT md5,collection_id FROM library_collection_items WHERE md5=ANY(:md5s) AND collection_id<>:collection_id"
                        ),
                        {"md5s": selected, "collection_id": collection_id},
                    )
                    .mappings()
                    .all()
                )
                if conflicts:
                    raise ValueError(
                        f"Membership conflict for {len(conflicts)} selected document(s)"
                    )
                for md5 in selected:
                    feature = (
                        conn.execute(
                            text(
                                "SELECT title,title_core FROM library_collection_document_features WHERE md5=:md5"
                            ),
                            {"md5": md5},
                        )
                        .mappings()
                        .one()
                    )
                    conn.execute(
                        text(
                            """INSERT INTO library_collection_items (collection_id,md5,item_title,created_at,updated_at) VALUES (:collection_id,:md5,:title,:now,:now) ON CONFLICT (md5) DO NOTHING"""
                        ),
                        {
                            "collection_id": collection_id,
                            "md5": md5,
                            "title": feature["title"],
                            "now": now,
                        },
                    )
                    if feature["title_core"]:
                        conn.execute(
                            text(
                                """INSERT INTO library_collection_signatures (collection_id,signature_type,normalized_value,provenance,created_at,updated_at) VALUES (:id,'member_title_core',:value,'proposal_approval',:now,:now) ON CONFLICT DO NOTHING"""
                            ),
                            {
                                "id": collection_id,
                                "value": feature["title_core"],
                                "now": now,
                            },
                        )
            conn.execute(
                text(
                    "UPDATE library_collection_proposal_items SET decision=CASE WHEN md5=ANY(:selected) THEN 'approved' ELSE 'rejected' END WHERE proposal_id=:id"
                ),
                {"selected": selected, "id": int(proposal_id)},
            )
            final_status = "approved" if decision == "approve" else "rejected"
            conn.execute(
                text(
                    "UPDATE library_collection_proposals SET status=:status,reviewed_at=:now,updated_at=:now WHERE proposal_id=:id"
                ),
                {"status": final_status, "now": now, "id": int(proposal_id)},
            )
        payload = {
            "proposal_id": int(proposal_id),
            "decision": decision,
            "collection_id": collection_id or None,
            "selected_count": len(selected),
        }
        db.insert_event(
            "library.collections.proposal_decided",
            task_id=None,
            run_id=None,
            panel_id=COLLECTIONS_PANEL_ID,
            payload=payload,
        )
        return {"ok": True, **payload}
    finally:
        engine.dispose()


def list_collection_items(collection_id: int, *, limit: int = 400) -> dict[str, Any]:
    engine, source = create_runtime_engine()
    try:
        with engine.connect() as conn:
            _set_search_path(conn)
            collection = (
                conn.execute(
                    text(
                        "SELECT c.*, (SELECT COUNT(*) FROM library_collection_items i WHERE i.collection_id=c.collection_id) item_count FROM library_collections c WHERE c.collection_id=:id"
                    ),
                    {"id": int(collection_id)},
                )
                .mappings()
                .first()
            )
            if not collection:
                raise ValueError("Collection not found")
            rows = (
                conn.execute(
                    text(
                        """SELECT i.md5,i.item_title,m.lib,m.schema_org FROM library_collection_items i LEFT JOIN metadata m ON m.md5=i.md5 WHERE i.collection_id=:id ORDER BY i.item_title,i.md5 LIMIT :limit"""
                    ),
                    {"id": int(collection_id), "limit": min(2000, max(1, int(limit)))},
                )
                .mappings()
                .all()
            )
        return {
            "available": True,
            "error": None,
            "config_source": source,
            "collection_id": int(collection_id),
            "collection": _collection(collection),
            "items": [
                {
                    "md5": row["md5"],
                    "item_title": row["item_title"],
                    "lib": bool(row["lib"]),
                    "schema_name": str(
                        (_json(row["schema_org"], {}) or {}).get("name") or ""
                    ),
                }
                for row in rows
            ],
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "config_source": source,
            "collection_id": int(collection_id),
            "collection": None,
            "items": [],
        }
    finally:
        engine.dispose()


def get_collection_review(collection_id: int, **_: Any) -> dict[str, Any]:
    payload = list_collection_items(collection_id, limit=2000)
    if not payload["available"]:
        return payload
    items = payload["items"]
    return {
        **payload,
        "summary": {
            "item_count": len(items),
            "included_count": sum(bool(item["lib"]) for item in items),
        },
        "samples": items[:8],
        "outliers": [],
        "outliers_total": 0,
        "merge_candidates": [],
    }


def get_collection_insights(**_: Any) -> dict[str, Any]:
    return {
        "available": True,
        "error": None,
        "clusters": [],
        "queue": {"total": 0, "items": []},
    }


def update_collection(
    db: Database, collection_id: int, updates: dict[str, Any]
) -> dict[str, Any]:
    allowed = {"title", "notes", "include_in_library"}
    clean = {key: value for key, value in updates.items() if key in allowed}
    if not clean:
        raise ValueError("No supported fields in request")
    now = _now()
    engine, _ = create_runtime_engine()
    try:
        with engine.begin() as conn:
            _set_search_path(conn)
            current = (
                conn.execute(
                    text(
                        "SELECT * FROM library_collections WHERE collection_id=:id FOR UPDATE"
                    ),
                    {"id": int(collection_id)},
                )
                .mappings()
                .first()
            )
            if not current:
                raise ValueError("Collection not found")
            title = str(clean.get("title", current["title"])).strip()
            notes = str(clean.get("notes", current["notes"] or "")).strip()
            include = int(
                bool(clean.get("include_in_library", current["include_in_library"]))
            )
            row = (
                conn.execute(
                    text(
                        "UPDATE library_collections SET title=:title,normalized_title=:normalized,notes=:notes,include_in_library=:include,updated_at=:now WHERE collection_id=:id RETURNING *"
                    ),
                    {
                        "title": title,
                        "normalized": normalize_collection_text(title),
                        "notes": notes,
                        "include": include,
                        "now": now,
                        "id": int(collection_id),
                    },
                )
                .mappings()
                .one()
            )
        db.insert_event(
            "library.collections.updated",
            task_id=None,
            run_id=None,
            panel_id=COLLECTIONS_PANEL_ID,
            payload={
                "collection_id": int(collection_id),
                "updated_fields": sorted(clean),
            },
        )
        return {
            "ok": True,
            "error": None,
            "collection": _collection(row),
            "updated_fields": sorted(clean),
        }
    finally:
        engine.dispose()


def merge_collections(
    db: Database, *, source_collection_id: int, target_collection_id: int
) -> dict[str, Any]:
    if int(source_collection_id) == int(target_collection_id):
        raise ValueError("source and target collections must be different")
    now = _now()
    engine, _ = create_runtime_engine()
    try:
        with engine.begin() as conn:
            _set_search_path(conn)
            existing = set(
                conn.execute(
                    text(
                        "SELECT collection_id FROM library_collections WHERE collection_id=ANY(:ids) FOR UPDATE"
                    ),
                    {"ids": [int(source_collection_id), int(target_collection_id)]},
                )
                .scalars()
                .all()
            )
            if existing != {int(source_collection_id), int(target_collection_id)}:
                raise ValueError("Collection not found")
            moved = conn.execute(
                text(
                    "UPDATE library_collection_items SET collection_id=:target,updated_at=:now WHERE collection_id=:source"
                ),
                {
                    "target": int(target_collection_id),
                    "source": int(source_collection_id),
                    "now": now,
                },
            ).rowcount
            conn.execute(
                text(
                    """INSERT INTO library_collection_signatures (collection_id,signature_type,normalized_value,provenance,created_at,updated_at) SELECT :target,signature_type,normalized_value,'collection_merge',created_at,:now FROM library_collection_signatures WHERE collection_id=:source ON CONFLICT DO NOTHING"""
                ),
                {
                    "target": int(target_collection_id),
                    "source": int(source_collection_id),
                    "now": now,
                },
            )
            conn.execute(
                text("DELETE FROM library_collections WHERE collection_id=:source"),
                {"source": int(source_collection_id)},
            )
        payload = {
            "source_collection_id": int(source_collection_id),
            "target_collection_id": int(target_collection_id),
            "moved_items": int(moved or 0),
        }
        db.insert_event(
            "library.collections.merged",
            task_id=None,
            run_id=None,
            panel_id=COLLECTIONS_PANEL_ID,
            payload=payload,
        )
        return {"ok": True, "error": None, **payload}
    finally:
        engine.dispose()


def apply_collection_overrides(*, collection_limit: int = 500) -> dict[str, Any]:
    now = _now()
    engine, source = create_runtime_engine()
    collections_applied = items_applied = forced = 0
    try:
        with engine.begin() as conn:
            _set_search_path(conn)
            rows = (
                conn.execute(
                    text(
                        "SELECT * FROM library_collections WHERE include_in_library=1 ORDER BY updated_at LIMIT :limit"
                    ),
                    {"limit": min(5000, max(1, int(collection_limit)))},
                )
                .mappings()
                .all()
            )
            for collection in rows:
                items = (
                    conn.execute(
                        text(
                            "SELECT i.md5,i.item_title,m.lib,m.schema_org FROM library_collection_items i JOIN metadata m ON m.md5=i.md5 WHERE i.collection_id=:id"
                        ),
                        {"id": collection["collection_id"]},
                    )
                    .mappings()
                    .all()
                )
                template = _json(collection["metadata_template_json"], {})
                for item in items:
                    schema = _json(item["schema_org"], {})
                    result = deepcopy(template or schema)
                    result["isPartOf"] = {
                        "@type": "Collection",
                        "name": collection["title"],
                    }
                    if item["item_title"]:
                        result["position"] = item["item_title"]
                    forced += int(not bool(item["lib"]))
                    items_applied += 1
                    conn.execute(
                        text(
                            "UPDATE metadata SET lib=TRUE,lib_eval_method='collection_override',schema_org=CAST(:schema AS JSON) WHERE md5=:md5"
                        ),
                        {
                            "schema": json.dumps(result, ensure_ascii=False),
                            "md5": item["md5"],
                        },
                    )
                conn.execute(
                    text(
                        "UPDATE library_collections SET applied_at=:now,updated_at=:now WHERE collection_id=:id"
                    ),
                    {"now": now, "id": collection["collection_id"]},
                )
                collections_applied += 1
        return {
            "available": True,
            "error": None,
            "config_source": source,
            "collections_applied": collections_applied,
            "items_applied": items_applied,
            "forced_include_count": forced,
        }
    except Exception as exc:
        return {
            "available": False,
            "error": str(exc),
            "config_source": source,
            "collections_applied": collections_applied,
            "items_applied": items_applied,
            "forced_include_count": forced,
        }
    finally:
        engine.dispose()


__all__ = [
    "apply_collection_overrides",
    "decide_collection_proposal",
    "get_collection_insights",
    "get_collection_overview",
    "get_collection_proposal_review",
    "get_collection_review",
    "list_collection_items",
    "list_collection_proposals",
    "list_collections",
    "merge_collections",
    "update_collection",
]
