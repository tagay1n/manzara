"""Canonical registry creation, grouping, renaming, and merging."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.db import Database
from app.modules.library.normalization_queries import _runtime_snapshot_for_alias
from app.modules.library.normalization_rules import (
    _entity_config,
    _normalize_text,
)


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


def create_canonical_group(
    db: Database,
    entity_type: str,
    *,
    display_name: str,
    raw_names: List[str],
    suggestion_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """Create one canonical and retain a reviewed set of raw aliases."""
    _entity_config(entity_type)
    display = str(display_name or "").strip()
    if not display:
        raise ValueError("display_name is required")
    names = list(dict.fromkeys(str(item or "").strip() for item in raw_names))
    names = [item for item in names if item]
    if not names:
        raise ValueError("raw_names must be non-empty")
    if len(names) > 200:
        raise ValueError("raw_names may contain at most 200 aliases")
    snapshots = [_runtime_snapshot_for_alias(entity_type, raw) for raw in names]
    return db.create_normalization_group(
        entity_type,
        display,
        _normalize_text(display),
        snapshots,
        suggestion_ids=[int(item) for item in (suggestion_ids or [])],
    )


def list_canonical_aliases(
    db: Database, entity_type: str, *, canonical_id: int
) -> Dict[str, Any]:
    """Return the source spellings retained for one canonical."""
    _entity_config(entity_type)
    canonical = db.get_normalization_canonical(int(canonical_id))
    if not canonical or str(canonical.get("entity_type") or "") != entity_type:
        raise ValueError("Canonical not found for entity type")
    return {
        "available": True,
        "canonical": canonical,
        "items": db.list_normalization_aliases_for_canonical(
            entity_type, int(canonical_id)
        ),
    }


def rename_canonical(
    db: Database,
    entity_type: str,
    *,
    canonical_id: int,
    display_name: str,
) -> Dict[str, Any]:
    """Change the chosen display name without changing retained aliases."""
    _entity_config(entity_type)
    display = str(display_name or "").strip()
    if not display:
        raise ValueError("display_name is required")
    return db.rename_normalization_canonical(
        entity_type, int(canonical_id), display, _normalize_text(display)
    )


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
