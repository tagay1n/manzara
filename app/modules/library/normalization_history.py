"""Normalization audit history and reversible event restoration."""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.db import Database
from app.modules.library.normalization_rules import _entity_config


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

    elif action in {"bulk_link_aliases", "bulk_reject_aliases"}:
        before_aliases = payload.get("before_aliases") or []
        after_aliases = payload.get("after_aliases") or []
        for index, after_alias in enumerate(after_aliases):
            before_alias = before_aliases[index] if index < len(before_aliases) else None
            _apply_alias_restore(
                db,
                entity_type=entity_type,
                before_alias=before_alias,
                after_alias=after_alias,
            )

    elif action == "create_canonical_group":
        before_by_name = {
            str(item.get("raw_name") or ""): item
            for item in (payload.get("before_aliases") or [])
        }
        for after_alias in payload.get("after_aliases") or []:
            raw_name = str(after_alias.get("raw_name") or "")
            _apply_alias_restore(
                db,
                entity_type=entity_type,
                before_alias=before_by_name.get(raw_name),
                after_alias=after_alias,
            )
        created_id = int(payload.get("created_canonical_id") or 0)
        if created_id > 0 and db.count_linked_aliases_for_canonical(created_id) == 0:
            db.delete_normalization_canonical(created_id)

    elif action == "rename_canonical":
        db.restore_normalization_canonical_snapshot(payload.get("before"))

    else:
        raise ValueError("Event cannot be undone")

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
