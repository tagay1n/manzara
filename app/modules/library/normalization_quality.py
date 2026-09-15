"""Normalization coverage metrics and canonical merge recommendations."""

from __future__ import annotations

from typing import Any, Dict, List

from app.db import Database
from app.modules.library.normalization_rules import (
    _entity_config,
    _similarity,
)
from app.modules.library.normalization_views import get_review_queue


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
