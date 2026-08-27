"""Normalization workbench logic for personalities and publishers."""

from __future__ import annotations

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy import text

from app.db import Database
from app.gemini_config import load_required_gemini_model_pool
from app.gemini_model_pool import (
    GeminiModelPoolExhaustedError,
    GeminiModelResponseError,
    run_ordered_model_pool,
)
from app.gemini_runtime import GeminiRuntimeManager
from app.modules.library.stats import create_runtime_engine

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


def _mentions_cte_sql(entity_type: str) -> str:
    cfg = _entity_config(entity_type)
    schema_field = cfg["schema_field"]
    keys = [str(item) for item in cfg["name_keys"] if str(item)]
    coalesce_parts = [f"e.entity_item->>'{key}'" for key in keys]
    object_name_expr = "COALESCE(" + ", ".join(coalesce_parts + ["''"]) + ")"
    marker_regex = cfg["marker_regex"]

    return f"""
        WITH extracted AS (
            SELECT
                m.md5 AS md5,
                BTRIM(
                    CASE
                        WHEN jsonb_typeof(e.entity_item) = 'object' THEN {object_name_expr}
                        WHEN jsonb_typeof(e.entity_item) = 'string' THEN REGEXP_REPLACE(e.entity_item::text, '^"|"$', '', 'g')
                        ELSE ''
                    END
                ) AS raw_name
            FROM metadata m
            CROSS JOIN LATERAL (
                SELECT elem AS entity_item
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof((m.schema_org::jsonb)->'{schema_field}') = 'array'
                            THEN (m.schema_org::jsonb)->'{schema_field}'
                        ELSE '[]'::jsonb
                    END
                ) AS elem
                UNION ALL
                SELECT (m.schema_org::jsonb)->'{schema_field}' AS entity_item
                WHERE jsonb_typeof((m.schema_org::jsonb)->'{schema_field}') IN ('object', 'string')
            ) e
            WHERE m.lib IS TRUE
              AND m.schema_org IS NOT NULL
        ),
        mentions AS (
            SELECT
                md5,
                raw_name,
                BTRIM(
                    REGEXP_REPLACE(
                        LOWER(
                            REGEXP_REPLACE(
                                raw_name,
                                '[^0-9A-Za-zА-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]+',
                                ' ',
                                'g'
                            )
                        ),
                        '\\s+',
                        ' ',
                        'g'
                    )
                ) AS normalized_name,
                CASE
                    WHEN raw_name ~ '[A-Za-z]' AND raw_name ~ '[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]' THEN 'mixed'
                    WHEN raw_name ~ '[А-Яа-яЁёӘәҖҗҢңӨөҮүҺһІіҒғҚқҪҫ]' THEN 'cyrillic'
                    WHEN raw_name ~ '[A-Za-z]' THEN 'latin'
                    ELSE 'other'
                END AS script_label,
                (raw_name ~* '{marker_regex}') AS has_marker
            FROM extracted
            WHERE raw_name <> ''
        )
    """


def _query_aggregated_mentions(
    entity_type: str,
    *,
    search: str = "",
    script_label: str = "",
    min_docs: int = 0,
    limit: int = 5000,
) -> tuple[list[Dict[str, Any]], str]:
    min_docs = max(0, int(min_docs))
    limit = max(1, min(20000, int(limit)))
    search_clean = str(search or "").strip().lower()
    script_clean = str(script_label or "").strip().lower()
    params = {
        "search": search_clean,
        "search_like": f"%{search_clean}%",
        "script_label": script_clean,
        "min_docs": min_docs,
        "limit": limit,
    }

    engine, config_source = create_runtime_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                {_mentions_cte_sql(entity_type)}
                , aggregated AS (
                    SELECT
                        raw_name,
                        normalized_name,
                        script_label,
                        COUNT(*) AS mentions_count,
                        COUNT(DISTINCT md5) AS docs_count,
                        SUM(CASE WHEN has_marker THEN 1 ELSE 0 END) AS marker_count
                    FROM mentions
                    WHERE (:search = '' OR LOWER(raw_name) LIKE :search_like OR normalized_name LIKE :search_like)
                      AND (:script_label = '' OR script_label = :script_label)
                    GROUP BY raw_name, normalized_name, script_label
                    HAVING COUNT(DISTINCT md5) >= :min_docs
                )
                SELECT
                    raw_name,
                    normalized_name,
                    script_label,
                    mentions_count,
                    docs_count,
                    marker_count
                FROM aggregated
                ORDER BY docs_count DESC, mentions_count DESC, raw_name ASC
                LIMIT :limit
                """
            ),
            params,
        ).mappings().all()
    engine.dispose()

    return [dict(row) for row in rows], str(config_source)


def _query_docs_with_entities_count(entity_type: str) -> int:
    engine, _ = create_runtime_engine()
    with engine.connect() as conn:
        value = conn.execute(
            text(
                f"""
                {_mentions_cte_sql(entity_type)}
                SELECT COUNT(DISTINCT md5) AS count
                FROM mentions
                """
            )
        ).scalar()
    engine.dispose()
    return int(value or 0)


def _runtime_snapshot_for_alias(entity_type: str, raw_name: str) -> Dict[str, Any]:
    rows, _ = _query_aggregated_mentions(
        entity_type,
        search=raw_name,
        limit=500,
    )
    for row in rows:
        if str(row.get("raw_name") or "") == raw_name:
            return row
    return {
        "raw_name": raw_name,
        "normalized_name": _normalize_text(raw_name),
        "script_label": "other",
        "mentions_count": 0,
        "docs_count": 0,
        "marker_count": 0,
    }


def _canonical_name_map(canonicals: Iterable[Dict[str, Any]]) -> Dict[int, str]:
    result: Dict[int, str] = {}
    for item in canonicals:
        canonical_id = int(item.get("canonical_id") or 0)
        if canonical_id <= 0:
            continue
        result[canonical_id] = str(item.get("display_name") or "")
    return result


def _suggestion_map(db: Database, entity_type: str) -> Dict[str, Dict[str, Any]]:
    suggestions = db.list_open_suggestions(entity_type, limit=5000)
    suggestions.sort(key=lambda item: (-float(item.get("confidence") or 0.0), int(item.get("suggestion_id") or 0)))
    result: Dict[str, Dict[str, Any]] = {}
    for item in suggestions:
        raw_name = str(item.get("raw_name") or "")
        if raw_name and raw_name not in result:
            result[raw_name] = item
    return result


def get_normalization_dashboard(db: Database, entity_type: str) -> Dict[str, Any]:
    """Return dashboard summary for normalization workbench."""
    cfg = _entity_config(entity_type)
    try:
        rows, config_source = _query_aggregated_mentions(entity_type, limit=7000)
        docs_with_entities = _query_docs_with_entities_count(entity_type)

        aliases = db.list_normalization_aliases(entity_type)
        alias_map = {str(item.get("raw_name") or ""): item for item in aliases}
        suggestion_map = _suggestion_map(db, entity_type)
        canonicals = db.list_normalization_canonicals(entity_type)

        total_aliases = len(rows)
        linked_count = 0
        rejected_count = 0
        pending_count = 0
        suggested_count = 0
        unreviewed_count = 0

        unresolved_preview: list[Dict[str, Any]] = []
        for row in rows:
            raw_name = str(row.get("raw_name") or "")
            alias = alias_map.get(raw_name)
            if alias:
                status = str(alias.get("decision_status") or "pending")
            elif raw_name in suggestion_map:
                status = "suggested"
            else:
                status = "unreviewed"

            if status == "linked":
                linked_count += 1
            elif status == "rejected":
                rejected_count += 1
            elif status == "pending":
                pending_count += 1
            elif status == "suggested":
                suggested_count += 1
                unresolved_preview.append(row)
            else:
                unreviewed_count += 1
                unresolved_preview.append(row)

        reviewed_count = linked_count + rejected_count + pending_count
        coverage_pct = round((reviewed_count / total_aliases) * 100.0, 2) if total_aliases > 0 else 0.0

        open_suggestions = db.list_open_suggestions(entity_type, limit=5000)
        band_counts = {"high": 0, "medium": 0, "low": 0}
        for item in open_suggestions:
            band = str(item.get("confidence_band") or "low")
            if band in band_counts:
                band_counts[band] += 1

        return {
            "available": True,
            "error": None,
            "entity_type": cfg["entity_type"],
            "config_source": config_source,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "stats": {
                "total_aliases": total_aliases,
                "total_mentions": sum(int(row.get("mentions_count") or 0) for row in rows),
                "docs_with_entities": docs_with_entities,
                "canonicals": len(canonicals),
                "linked": linked_count,
                "rejected": rejected_count,
                "pending": pending_count,
                "suggested": suggested_count,
                "unreviewed": unreviewed_count,
                "coverage_pct": coverage_pct,
            },
            "suggestions": {
                "open_total": len(open_suggestions),
                "high": band_counts["high"],
                "medium": band_counts["medium"],
                "low": band_counts["low"],
            },
            "top_unresolved": sorted(
                unresolved_preview,
                key=lambda item: (-int(item.get("docs_count") or 0), -int(item.get("mentions_count") or 0), str(item.get("raw_name") or "")),
            )[:12],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "entity_type": cfg["entity_type"],
            "config_source": None,
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "stats": {
                "total_aliases": 0,
                "total_mentions": 0,
                "docs_with_entities": 0,
                "canonicals": 0,
                "linked": 0,
                "rejected": 0,
                "pending": 0,
                "suggested": 0,
                "unreviewed": 0,
                "coverage_pct": 0.0,
            },
            "suggestions": {
                "open_total": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
            },
            "top_unresolved": [],
        }


def get_review_queue(
    db: Database,
    entity_type: str,
    *,
    status: str = "all",
    search: str = "",
    script_label: str = "",
    min_docs: int = 0,
    page: int = 1,
    page_size: int = 40,
) -> Dict[str, Any]:
    """Return review queue rows enriched with saved decisions and suggestions."""
    _entity_config(entity_type)
    page = max(1, int(page))
    page_size = max(1, min(200, int(page_size)))

    try:
        rows, config_source = _query_aggregated_mentions(
            entity_type,
            search=search,
            script_label=script_label,
            min_docs=min_docs,
            limit=8000,
        )
        aliases = db.list_normalization_aliases(entity_type)
        alias_map = {str(item.get("raw_name") or ""): item for item in aliases}
        suggestion_map = _suggestion_map(db, entity_type)
        canonicals = db.list_normalization_canonicals(entity_type)
        canonical_name_map = _canonical_name_map(canonicals)

        normalized_status = str(status or "all").strip().lower()

        items: List[Dict[str, Any]] = []
        for row in rows:
            raw_name = str(row.get("raw_name") or "")
            alias = alias_map.get(raw_name)
            suggestion = suggestion_map.get(raw_name)

            if alias:
                queue_status = str(alias.get("decision_status") or "pending")
            elif suggestion:
                queue_status = "suggested"
            else:
                queue_status = "unreviewed"

            if normalized_status not in {"all", ""} and queue_status != normalized_status:
                continue

            canonical_id = None
            canonical_name = None
            if alias and alias.get("canonical_id") is not None:
                canonical_id = int(alias.get("canonical_id") or 0)
                canonical_name = canonical_name_map.get(canonical_id)
            elif suggestion and suggestion.get("target_canonical_id") is not None:
                canonical_id = int(suggestion.get("target_canonical_id") or 0)
                canonical_name = canonical_name_map.get(canonical_id)

            items.append(
                {
                    "raw_name": raw_name,
                    "normalized_name": str(row.get("normalized_name") or ""),
                    "script_label": str(row.get("script_label") or "other"),
                    "docs_count": int(row.get("docs_count") or 0),
                    "mentions_count": int(row.get("mentions_count") or 0),
                    "marker_count": int(row.get("marker_count") or 0),
                    "queue_status": queue_status,
                    "canonical_id": canonical_id,
                    "canonical_name": canonical_name,
                    "decision_source": str(alias.get("source") or "") if alias else "",
                    "decision_reason": str(alias.get("reason") or "") if alias else "",
                    "decision_confidence": float(alias.get("confidence") or 0.0) if alias else None,
                    "suggestion": {
                        "suggestion_id": int(suggestion.get("suggestion_id") or 0),
                        "kind": str(suggestion.get("suggestion_kind") or ""),
                        "confidence": float(suggestion.get("confidence") or 0.0),
                        "band": str(suggestion.get("confidence_band") or ""),
                        "target_canonical_id": suggestion.get("target_canonical_id"),
                        "rationale": str(suggestion.get("rationale") or ""),
                    }
                    if suggestion
                    else None,
                }
            )

        total = len(items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        start = (page - 1) * page_size
        paged = items[start : start + page_size]

        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "items": paged,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "page": page,
            "page_size": page_size,
            "total": 0,
            "total_pages": 1,
            "items": [],
        }


def list_canonicals(
    db: Database,
    entity_type: str,
    *,
    search: str = "",
) -> Dict[str, Any]:
    """Return canonical registry rows."""
    _entity_config(entity_type)
    try:
        rows = db.list_normalization_canonicals(entity_type)
        search_clean = str(search or "").strip().lower()
        if search_clean:
            rows = [
                row
                for row in rows
                if search_clean in str(row.get("display_name") or "").lower()
                or search_clean in str(row.get("normalized_name") or "").lower()
            ]
        return {
            "available": True,
            "error": None,
            "items": rows,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "items": [],
        }


def create_canonical(
    db: Database,
    entity_type: str,
    *,
    display_name: str,
    notes: str = "",
) -> Dict[str, Any]:
    """Create canonical registry entry and record audit event."""
    _entity_config(entity_type)
    title = str(display_name or "").strip()
    if not title:
        raise ValueError("display_name is required")

    canonical = db.create_normalization_canonical(
        entity_type,
        title,
        _normalize_text(title),
        notes=str(notes or "").strip(),
    )
    event = db.create_normalization_event(
        entity_type,
        "create_canonical",
        {
            "canonical_id": canonical.get("canonical_id"),
            "canonical": canonical,
        },
    )
    return {
        "canonical": canonical,
        "event": event,
    }


def _upsert_alias_decision(
    db: Database,
    entity_type: str,
    *,
    raw_name: str,
    decision_status: str,
    canonical_id: Optional[int],
    source: str,
    confidence: Optional[float],
    reason: str,
) -> Dict[str, Any]:
    snapshot = _runtime_snapshot_for_alias(entity_type, raw_name)
    return db.upsert_normalization_alias(
        entity_type=entity_type,
        raw_name=raw_name,
        normalized_name=str(snapshot.get("normalized_name") or _normalize_text(raw_name)),
        script_label=str(snapshot.get("script_label") or "other"),
        docs_count=int(snapshot.get("docs_count") or 0),
        mentions_count=int(snapshot.get("mentions_count") or 0),
        marker_count=int(snapshot.get("marker_count") or 0),
        decision_status=decision_status,
        canonical_id=canonical_id,
        confidence=confidence,
        source=source,
        reason=str(reason or "").strip(),
    )


def link_alias(
    db: Database,
    entity_type: str,
    *,
    raw_name: str,
    canonical_id: int,
    source: str = "manual",
    confidence: Optional[float] = 1.0,
    reason: str = "",
    suggestion_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Link alias to canonical entity and append audit event."""
    _entity_config(entity_type)
    raw = str(raw_name or "").strip()
    if not raw:
        raise ValueError("raw_name is required")

    canonical = db.get_normalization_canonical(int(canonical_id))
    if not canonical or str(canonical.get("entity_type") or "") != entity_type:
        raise ValueError("Canonical not found for entity type")
    if str(canonical.get("status") or "") != "active":
        raise ValueError("Canonical is not active")

    before_alias = db.get_normalization_alias(entity_type, raw)
    after_alias = _upsert_alias_decision(
        db,
        entity_type,
        raw_name=raw,
        decision_status="linked",
        canonical_id=int(canonical_id),
        source=source,
        confidence=confidence,
        reason=reason,
    )

    if suggestion_ids:
        db.update_suggestion_statuses(suggestion_ids, "accepted")

    event = db.create_normalization_event(
        entity_type,
        "link_alias",
        {
            "raw_name": raw,
            "canonical_id": int(canonical_id),
            "before_alias": before_alias,
            "after_alias": after_alias,
            "suggestion_ids": [int(item) for item in (suggestion_ids or [])],
        },
    )
    return {
        "alias": after_alias,
        "event": event,
    }


def create_and_link_alias(
    db: Database,
    entity_type: str,
    *,
    raw_name: str,
    display_name: str,
    reason: str = "",
    suggestion_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Create canonical and link alias in one operation."""
    _entity_config(entity_type)
    raw = str(raw_name or "").strip()
    display = str(display_name or "").strip()
    if not raw:
        raise ValueError("raw_name is required")
    if not display:
        raise ValueError("display_name is required")

    before_alias = db.get_normalization_alias(entity_type, raw)
    canonical = db.create_normalization_canonical(
        entity_type,
        display,
        _normalize_text(display),
    )

    after_alias = _upsert_alias_decision(
        db,
        entity_type,
        raw_name=raw,
        decision_status="linked",
        canonical_id=int(canonical.get("canonical_id") or 0),
        source="manual_create",
        confidence=1.0,
        reason=reason,
    )

    if suggestion_ids:
        db.update_suggestion_statuses(suggestion_ids, "accepted")

    event = db.create_normalization_event(
        entity_type,
        "create_and_link_alias",
        {
            "raw_name": raw,
            "created_canonical": canonical,
            "created_canonical_id": canonical.get("canonical_id"),
            "before_alias": before_alias,
            "after_alias": after_alias,
            "suggestion_ids": [int(item) for item in (suggestion_ids or [])],
        },
    )
    return {
        "canonical": canonical,
        "alias": after_alias,
        "event": event,
    }


def reject_alias(
    db: Database,
    entity_type: str,
    *,
    raw_name: str,
    reason: str = "",
    suggestion_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Reject alias as non-canonical-worthy and append audit event."""
    _entity_config(entity_type)
    raw = str(raw_name or "").strip()
    if not raw:
        raise ValueError("raw_name is required")

    before_alias = db.get_normalization_alias(entity_type, raw)
    after_alias = _upsert_alias_decision(
        db,
        entity_type,
        raw_name=raw,
        decision_status="rejected",
        canonical_id=None,
        source="manual",
        confidence=1.0,
        reason=reason,
    )

    if suggestion_ids:
        db.update_suggestion_statuses(suggestion_ids, "rejected")

    event = db.create_normalization_event(
        entity_type,
        "reject_alias",
        {
            "raw_name": raw,
            "before_alias": before_alias,
            "after_alias": after_alias,
            "suggestion_ids": [int(item) for item in (suggestion_ids or [])],
        },
    )
    return {
        "alias": after_alias,
        "event": event,
    }


def merge_canonicals(
    db: Database,
    entity_type: str,
    *,
    source_canonical_id: int,
    target_canonical_id: int,
    reason: str = "",
) -> Dict[str, Any]:
    """Merge one canonical entity into another and log reversible event."""
    _entity_config(entity_type)
    source_id = int(source_canonical_id)
    target_id = int(target_canonical_id)
    if source_id == target_id:
        raise ValueError("source and target canonical ids must differ")

    source_before = db.get_normalization_canonical(source_id)
    target_before = db.get_normalization_canonical(target_id)
    if not source_before or not target_before:
        raise ValueError("Canonical not found")
    if str(source_before.get("entity_type") or "") != entity_type:
        raise ValueError("Source canonical entity_type mismatch")
    if str(target_before.get("entity_type") or "") != entity_type:
        raise ValueError("Target canonical entity_type mismatch")

    moved_aliases = db.reassign_aliases_between_canonicals(
        entity_type=entity_type,
        source_canonical_id=source_id,
        target_canonical_id=target_id,
    )

    source_after = db.update_normalization_canonical(
        source_id,
        {
            "status": "merged",
            "merged_into_id": target_id,
            "notes": str(reason or "").strip(),
        },
    )
    target_after = db.update_normalization_canonical(
        target_id,
        {
            "notes": str(target_before.get("notes") or "").strip(),
        },
    )

    event = db.create_normalization_event(
        entity_type,
        "merge_canonicals",
        {
            "source_before": source_before,
            "target_before": target_before,
            "source_after": source_after,
            "target_after": target_after,
            "moved_aliases": moved_aliases,
            "reason": str(reason or "").strip(),
        },
    )
    return {
        "source": source_after,
        "target": target_after,
        "moved_aliases_count": len(moved_aliases),
        "event": event,
    }


def _apply_alias_restore(
    db: Database,
    *,
    entity_type: str,
    before_alias: Optional[Dict[str, Any]],
    after_alias: Optional[Dict[str, Any]],
) -> None:
    if before_alias is None:
        if after_alias:
            db.delete_normalization_alias(entity_type, str(after_alias.get("raw_name") or ""))
        return
    db.restore_normalization_alias_snapshot(before_alias)


def undo_event(
    db: Database,
    entity_type: str,
    *,
    event_id: int,
) -> Dict[str, Any]:
    """Undo one normalization action event."""
    _entity_config(entity_type)
    event = db.get_normalization_event(int(event_id))
    if not event:
        raise ValueError("Event not found")
    if str(event.get("entity_type") or "") != entity_type:
        raise ValueError("Event entity_type mismatch")
    if bool(event.get("reverted")):
        raise ValueError("Event already reverted")

    payload = event.get("payload") or {}
    action = str(event.get("action") or "")

    if action in {"link_alias", "reject_alias", "create_and_link_alias"}:
        _apply_alias_restore(
            db,
            entity_type=entity_type,
            before_alias=payload.get("before_alias"),
            after_alias=payload.get("after_alias"),
        )
        if action == "create_and_link_alias":
            created_id = int(payload.get("created_canonical_id") or 0)
            if created_id > 0 and db.count_linked_aliases_for_canonical(created_id) == 0:
                db.delete_normalization_canonical(created_id)

    elif action == "merge_canonicals":
        db.restore_normalization_canonical_snapshot(payload.get("source_before"))
        db.restore_normalization_canonical_snapshot(payload.get("target_before"))
        for alias_snapshot in payload.get("moved_aliases") or []:
            db.restore_normalization_alias_snapshot(alias_snapshot)

    db.mark_normalization_event_reverted(int(event_id))
    return {
        "event": db.get_normalization_event(int(event_id)),
    }


def list_history(
    db: Database,
    entity_type: str,
    *,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return normalization audit event list."""
    _entity_config(entity_type)
    try:
        items = db.list_normalization_events(entity_type, limit=max(1, min(int(limit), 1000)))
        return {
            "available": True,
            "error": None,
            "items": items,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "items": [],
        }


def get_quality(
    db: Database,
    entity_type: str,
) -> Dict[str, Any]:
    """Return quality indicators for normalization progress."""
    _entity_config(entity_type)
    try:
        queue = get_review_queue(db, entity_type, status="all", page=1, page_size=10000)
        items = queue.get("items") or []
        total = len(items)
        unresolved = [item for item in items if item.get("queue_status") in {"unreviewed", "suggested"}]
        linked = [item for item in items if item.get("queue_status") == "linked"]
        rejected = [item for item in items if item.get("queue_status") == "rejected"]

        normalized_groups: Dict[str, int] = {}
        for item in items:
            key = str(item.get("normalized_name") or "")
            if not key:
                continue
            normalized_groups[key] = normalized_groups.get(key, 0) + 1
        duplicate_keys = sum(1 for value in normalized_groups.values() if value > 1)

        unresolved_docs = sum(int(item.get("docs_count") or 0) for item in unresolved)
        return {
            "available": True,
            "error": None,
            "stats": {
                "total_aliases": total,
                "linked_aliases": len(linked),
                "rejected_aliases": len(rejected),
                "unresolved_aliases": len(unresolved),
                "unresolved_docs_estimate": unresolved_docs,
                "duplicate_normalized_keys": duplicate_keys,
                "coverage_pct": round(((len(linked) + len(rejected)) / total) * 100.0, 2) if total > 0 else 0.0,
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "stats": {
                "total_aliases": 0,
                "linked_aliases": 0,
                "rejected_aliases": 0,
                "unresolved_aliases": 0,
                "unresolved_docs_estimate": 0,
                "duplicate_normalized_keys": 0,
                "coverage_pct": 0.0,
            },
        }


def get_merge_candidates(
    db: Database,
    entity_type: str,
    *,
    min_score: float = 0.84,
    limit: int = 80,
) -> Dict[str, Any]:
    """Return canonical merge candidates by normalized name similarity."""
    _entity_config(entity_type)
    min_score = max(0.0, min(1.0, float(min_score)))
    limit = max(1, min(300, int(limit)))
    try:
        canonicals = db.list_normalization_canonicals(entity_type)
        candidates: List[Dict[str, Any]] = []
        for index, left in enumerate(canonicals):
            for right in canonicals[index + 1 :]:
                score = _similarity(left.get("normalized_name"), right.get("normalized_name"))
                if score < min_score:
                    continue
                left_aliases = int(left.get("linked_aliases") or 0)
                right_aliases = int(right.get("linked_aliases") or 0)
                primary = left if left_aliases >= right_aliases else right
                candidates.append(
                    {
                        "score": round(score, 3),
                        "impact": left_aliases + right_aliases,
                        "recommended_primary_canonical_id": int(primary.get("canonical_id") or 0),
                        "left": {
                            "canonical_id": int(left.get("canonical_id") or 0),
                            "display_name": str(left.get("display_name") or ""),
                            "normalized_name": str(left.get("normalized_name") or ""),
                            "linked_aliases": left_aliases,
                        },
                        "right": {
                            "canonical_id": int(right.get("canonical_id") or 0),
                            "display_name": str(right.get("display_name") or ""),
                            "normalized_name": str(right.get("normalized_name") or ""),
                            "linked_aliases": right_aliases,
                        },
                    }
                )
        candidates.sort(key=lambda item: (-float(item.get("score") or 0.0), -int(item.get("impact") or 0)))
        return {
            "available": True,
            "error": None,
            "summary": {
                "candidate_count": len(candidates),
                "min_score": min_score,
            },
            "items": candidates[:limit],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "summary": {
                "candidate_count": 0,
                "min_score": min_score,
            },
            "items": [],
        }


def get_evidence(
    entity_type: str,
    *,
    raw_name: str,
    limit: int = 20,
) -> Dict[str, Any]:
    """Return sample documents where a raw alias appears."""
    _entity_config(entity_type)
    raw = str(raw_name or "").strip()
    if not raw:
        raise ValueError("raw_name is required")

    limit = max(1, min(200, int(limit)))
    try:
        engine, config_source = create_runtime_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"""
                    {_mentions_cte_sql(entity_type)}
                    SELECT
                        m.md5,
                        d.language,
                        d.ya_path,
                        d.mime_type,
                        d.document_url,
                        d.content_url
                    FROM mentions m
                    JOIN document d ON d.md5 = m.md5
                    WHERE m.raw_name = :raw_name
                    ORDER BY d.md5 ASC
                    LIMIT :limit
                    """
                ),
                {"raw_name": raw, "limit": limit},
            ).mappings().all()
        engine.dispose()
        return {
            "available": True,
            "error": None,
            "config_source": config_source,
            "raw_name": raw,
            "items": [dict(row) for row in rows],
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "config_source": None,
            "raw_name": raw,
            "items": [],
        }


def _parse_first_json_blob(value: str) -> Optional[Dict[str, Any]]:
    text_value = str(value or "").strip()
    if not text_value:
        return None
    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", text_value)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def _gemini_suggest(
    *,
    entity_type: str,
    raw_name: str,
    normalized_name: str,
    docs_count: int,
    mentions_count: int,
    marker_count: int,
    canonical_candidates: List[Dict[str, Any]],
    manager: GeminiRuntimeManager,
) -> Optional[Dict[str, Any]]:
    try:
        from google import genai

        candidates_block = "\n".join(
            [
                f"- id={item['canonical_id']} name={item['display_name']} normalized={item['normalized_name']} aliases={item['linked_aliases']}"
                for item in canonical_candidates[:10]
            ]
        )
        prompt = (
            "You are normalizing bibliographic entities. "
            "Return strict JSON with keys: suggestion_kind(link|create|reject), target_canonical_id(number or null), confidence(0..1), rationale(string).\n"
            f"Entity type: {entity_type}\n"
            f"Alias raw name: {raw_name}\n"
            f"Alias normalized: {normalized_name}\n"
            f"Docs: {docs_count}, Mentions: {mentions_count}, MarkerCount: {marker_count}\n"
            "Canonical candidates:\n"
            f"{candidates_block if candidates_block else '- none'}\n"
            "Rules: choose link only when semantically same, otherwise create or reject."
        )

        def parse_response(response: Any) -> Dict[str, Any]:
            raw_text = getattr(response, "text", None)
            if not raw_text and hasattr(response, "candidates"):
                candidates = getattr(response, "candidates", None) or []
            else:
                candidates = []
            for candidate in candidates:
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None) or []
                for part in parts:
                    text_part = getattr(part, "text", None)
                    if text_part:
                        raw_text = str(text_part)
                        break
                if raw_text:
                    break
            parsed = _parse_first_json_blob(raw_text or "")
            if not parsed:
                raise GeminiModelResponseError("response is not a JSON object")
            kind = str(parsed.get("suggestion_kind") or "").strip().lower()
            if kind not in {"link", "create", "reject"}:
                raise GeminiModelResponseError("invalid suggestion_kind")
            confidence = max(0.0, min(1.0, float(parsed.get("confidence") or 0.0)))
            target = parsed.get("target_canonical_id")
            target_id = int(target) if isinstance(target, (int, float, str)) and str(target).strip() else None
            if kind != "link":
                target_id = None
            return {
                "suggestion_kind": kind,
                "target_canonical_id": target_id,
                "confidence": confidence,
                "confidence_band": _confidence_band(confidence),
                "rationale": str(parsed.get("rationale") or ""),
            }

        result = run_ordered_model_pool(
            manager=manager,
            models=load_required_gemini_model_pool(),
            request=lambda model, api_key, _lease: genai.Client(api_key=api_key).models.generate_content(
                model=model, contents=prompt
            ),
            parse=parse_response,
            record_failure=lambda *_args: None,
            run_id=None,
        )
        return {**result.value, "model": result.model_name}
    except GeminiModelPoolExhaustedError:
        return None
    except Exception:
        raise


def _heuristic_suggestions(
    db: Database,
    entity_type: str,
    *,
    limit: int,
    use_gemini: bool,
    manager: Optional[GeminiRuntimeManager] = None,
    workers: int = 1,
) -> List[Dict[str, Any]]:
    canonicals = db.list_normalization_canonicals(entity_type)
    queue = get_review_queue(
        db,
        entity_type,
        status="all",
        page=1,
        page_size=10000,
    )
    items = queue.get("items") or []
    unresolved = [
        item
        for item in items
        if str(item.get("queue_status") or "") in {"unreviewed", "suggested"}
    ]
    unresolved.sort(key=lambda item: (-int(item.get("docs_count") or 0), -int(item.get("mentions_count") or 0)))

    suggestions: List[Dict[str, Any]] = []
    gemini_budget = 20
    gemini_jobs: List[tuple[int, Future[Optional[Dict[str, Any]]]]] = []
    executor = (
        ThreadPoolExecutor(max_workers=max(1, int(workers)), thread_name_prefix="normalization-worker")
        if use_gemini and manager is not None
        else None
    )

    for item in unresolved[: max(1, int(limit))]:
        raw_name = str(item.get("raw_name") or "")
        normalized_name = str(item.get("normalized_name") or "")
        docs_count = int(item.get("docs_count") or 0)
        mentions_count = int(item.get("mentions_count") or 0)
        marker_count = int(item.get("marker_count") or 0)

        best_canonical: Optional[Dict[str, Any]] = None
        best_score = 0.0
        ranked: List[Dict[str, Any]] = []
        for canonical in canonicals:
            score = _similarity(normalized_name, canonical.get("normalized_name"))
            if score <= 0.0:
                continue
            ranked.append({"canonical": canonical, "score": score})
            if score > best_score:
                best_score = score
                best_canonical = canonical
        ranked.sort(key=lambda row: -float(row.get("score") or 0.0))

        if best_canonical and best_score >= 0.92:
            kind = "link"
            confidence = max(0.92, min(0.99, best_score))
            target_id = int(best_canonical.get("canonical_id") or 0)
            rationale = "Exact or near-exact normalized match"
            model = "heuristic"
        elif best_canonical and best_score >= 0.8:
            kind = "link"
            confidence = max(0.78, min(0.9, best_score))
            target_id = int(best_canonical.get("canonical_id") or 0)
            rationale = "High lexical similarity to canonical"
            model = "heuristic"
        elif docs_count >= 2:
            kind = "create"
            confidence = 0.66
            target_id = None
            rationale = "Frequent unresolved alias should become canonical candidate"
            model = "heuristic"
        else:
            kind = "reject"
            confidence = 0.52
            target_id = None
            rationale = "Low-evidence alias, likely noise or formatting variant"
            model = "heuristic"

        gemini_future = None
        if use_gemini and gemini_budget > 0 and manager is not None and executor is not None:
            gemini_future = executor.submit(
                _gemini_suggest,
                entity_type=entity_type,
                raw_name=raw_name,
                normalized_name=normalized_name,
                docs_count=docs_count,
                mentions_count=mentions_count,
                marker_count=marker_count,
                canonical_candidates=[
                    {
                        "canonical_id": int(row["canonical"].get("canonical_id") or 0),
                        "display_name": str(row["canonical"].get("display_name") or ""),
                        "normalized_name": str(row["canonical"].get("normalized_name") or ""),
                        "linked_aliases": int(row["canonical"].get("linked_aliases") or 0),
                    }
                    for row in ranked[:10]
                ],
                manager=manager,
            )
            gemini_budget -= 1

        suggestion = {
            "raw_name": raw_name,
            "normalized_name": normalized_name,
            "target_canonical_id": int(target_id) if target_id else None,
            "suggestion_kind": kind,
            "confidence": round(float(confidence), 3),
            "confidence_band": _confidence_band(float(confidence)),
            "model": model,
            "rationale": rationale,
        }
        suggestions.append(suggestion)
        if gemini_future is not None:
            gemini_jobs.append((len(suggestions) - 1, gemini_future))

    try:
        for index, future in gemini_jobs:
            gemini_pick = future.result()
            if not gemini_pick:
                continue
            suggestion = suggestions[index]
            suggestion["suggestion_kind"] = str(
                gemini_pick.get("suggestion_kind") or suggestion["suggestion_kind"]
            )
            target = gemini_pick.get("target_canonical_id")
            suggestion["target_canonical_id"] = int(target) if target else None
            confidence = float(gemini_pick.get("confidence") or suggestion["confidence"])
            suggestion["confidence"] = round(confidence, 3)
            suggestion["confidence_band"] = _confidence_band(confidence)
            suggestion["rationale"] = str(
                gemini_pick.get("rationale") or suggestion["rationale"]
            )
            suggestion["model"] = str(gemini_pick.get("model") or "gemini")
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    return suggestions


def refresh_suggestions(
    db: Database,
    entity_type: str,
    *,
    limit: int = 120,
    use_gemini: bool = True,
    workers: int = 1,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Regenerate open suggestion set from unresolved queue."""
    _entity_config(entity_type)
    limit = max(1, min(1000, int(limit)))

    manager: Optional[GeminiRuntimeManager] = None
    if bool(use_gemini):
        manager = GeminiRuntimeManager(
            db,
            task_id=f"library.{entity_type}_suggestions_refresh",
            panel_id="library",
            should_stop=should_stop,
        )

    suggestions = _heuristic_suggestions(
        db,
        entity_type,
        limit=limit,
        use_gemini=bool(use_gemini),
        manager=manager,
        workers=workers,
    )
    db.replace_open_suggestions(entity_type, suggestions)

    counts = {"high": 0, "medium": 0, "low": 0}
    for item in suggestions:
        band = str(item.get("confidence_band") or "low")
        if band in counts:
            counts[band] += 1

    event = db.create_normalization_event(
        entity_type,
        "refresh_suggestions",
        {
            "limit": limit,
            "use_gemini": bool(use_gemini),
            "generated": len(suggestions),
            "workers": int(workers),
            "bands": counts,
        },
    )

    return {
        "generated": len(suggestions),
        "bands": counts,
        "event": event,
        "workers": int(workers),
    }


def list_suggestions(
    db: Database,
    entity_type: str,
    *,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return open suggestions with canonical display names."""
    _entity_config(entity_type)
    try:
        items = db.list_open_suggestions(entity_type, limit=max(1, min(int(limit), 1000)))
        canonicals = db.list_normalization_canonicals(entity_type)
        canonical_names = _canonical_name_map(canonicals)
        payload = []
        for item in items:
            row = dict(item)
            target_id = row.get("target_canonical_id")
            if target_id is not None:
                row["target_canonical_name"] = canonical_names.get(int(target_id), None)
            else:
                row["target_canonical_name"] = None
            payload.append(row)

        return {
            "available": True,
            "error": None,
            "items": payload,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "error": str(exc),
            "items": [],
        }


def bulk_link_aliases(
    db: Database,
    entity_type: str,
    *,
    raw_names: List[str],
    canonical_id: int,
) -> Dict[str, Any]:
    """Bulk-link a list of aliases to one canonical."""
    _entity_config(entity_type)
    rows = [str(item or "").strip() for item in raw_names if str(item or "").strip()]
    if not rows:
        raise ValueError("raw_names must be non-empty")

    before: List[Optional[Dict[str, Any]]] = []
    after: List[Dict[str, Any]] = []
    for raw in rows:
        before.append(db.get_normalization_alias(entity_type, raw))
        after.append(
            _upsert_alias_decision(
                db,
                entity_type,
                raw_name=raw,
                decision_status="linked",
                canonical_id=int(canonical_id),
                source="manual_bulk",
                confidence=1.0,
                reason="bulk_link",
            )
        )

    event = db.create_normalization_event(
        entity_type,
        "bulk_link_aliases",
        {
            "canonical_id": int(canonical_id),
            "raw_names": rows,
            "before_aliases": before,
            "after_aliases": after,
        },
    )
    return {
        "updated": len(after),
        "event": event,
    }


def bulk_reject_aliases(
    db: Database,
    entity_type: str,
    *,
    raw_names: List[str],
) -> Dict[str, Any]:
    """Bulk-reject a list of aliases."""
    _entity_config(entity_type)
    rows = [str(item or "").strip() for item in raw_names if str(item or "").strip()]
    if not rows:
        raise ValueError("raw_names must be non-empty")

    before: List[Optional[Dict[str, Any]]] = []
    after: List[Dict[str, Any]] = []
    for raw in rows:
        before.append(db.get_normalization_alias(entity_type, raw))
        after.append(
            _upsert_alias_decision(
                db,
                entity_type,
                raw_name=raw,
                decision_status="rejected",
                canonical_id=None,
                source="manual_bulk",
                confidence=1.0,
                reason="bulk_reject",
            )
        )

    event = db.create_normalization_event(
        entity_type,
        "bulk_reject_aliases",
        {
            "raw_names": rows,
            "before_aliases": before,
            "after_aliases": after,
        },
    )
    return {
        "updated": len(after),
        "event": event,
    }


__all__ = [
    "ENTITY_TYPES",
    "get_normalization_dashboard",
    "get_review_queue",
    "list_canonicals",
    "create_canonical",
    "link_alias",
    "create_and_link_alias",
    "reject_alias",
    "bulk_link_aliases",
    "bulk_reject_aliases",
    "merge_canonicals",
    "undo_event",
    "list_history",
    "get_quality",
    "get_merge_candidates",
    "get_evidence",
    "refresh_suggestions",
    "list_suggestions",
]
