"""Reviewed alias decisions and suggestion dismissal."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.db import Database
from app.modules.library.normalization_queries import _runtime_snapshot_for_alias
from app.modules.library.normalization_rules import (
    _entity_config,
    _normalize_text,
)


def dismiss_suggestion(
    db: Database,
    entity_type: str,
    *,
    suggestion_id: int,
) -> Dict[str, Any]:
    """Dismiss one proposal while leaving the alias unresolved."""
    _entity_config(entity_type)
    return db.dismiss_normalization_suggestion(entity_type, int(suggestion_id))


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


def bulk_link_aliases(
    db: Database,
    entity_type: str,
    *,
    raw_names: List[str],
    canonical_id: int,
    suggestion_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Bulk-link a list of aliases to one canonical."""
    _entity_config(entity_type)
    rows = [str(item or "").strip() for item in raw_names if str(item or "").strip()]
    if not rows:
        raise ValueError("raw_names must be non-empty")

    snapshots = [_runtime_snapshot_for_alias(entity_type, raw) for raw in rows]
    return db.link_normalization_alias_group(
        entity_type,
        int(canonical_id),
        snapshots,
        suggestion_ids=[int(item) for item in (suggestion_ids or [])],
    )


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
