"""Normalization dashboard and enriched review queue read models."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.db import Database
from app.modules.library.normalization_queries import (
    _query_aggregated_mentions,
    _query_docs_with_entities_count,
)
from app.modules.library.normalization_rules import (
    _canonical_name_map,
    _entity_config,
)


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
