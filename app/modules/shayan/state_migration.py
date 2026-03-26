"""Legacy Shayan state migration helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from app.db import Database


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def migrate_legacy_shayan_state_if_needed(db: Database, *, artifacts_dir: Path) -> Dict[str, Any]:
    """Best-effort one-time migration from legacy JSON files into PostgreSQL."""
    manifest_present = db.shayan_manifest_entry_count() > 0
    snapshot_present = db.get_latest_shayan_snapshot() is not None

    migrated_manifest = 0
    migrated_snapshot_entries = 0
    snapshot_id = None

    status_path = artifacts_dir / "status.json"
    if not manifest_present:
        status_payload = _read_json(status_path)
        if status_payload:
            migrated_manifest = int(db.replace_shayan_manifest_entries(status_payload))

    snapshot_path = artifacts_dir / "snapshots" / "latest.json"
    if not snapshot_present:
        snapshot_payload = _read_json(snapshot_path)
        snapshot_entries = snapshot_payload.get("entries") if isinstance(snapshot_payload, dict) else None
        if isinstance(snapshot_entries, dict) and snapshot_entries:
            snapshot_id = db.create_shayan_snapshot(
                snapshot_entries,
                source=str(snapshot_payload.get("source") or "legacy-file"),
                generated_at=str(snapshot_payload.get("generated_at") or ""),
            )
            migrated_snapshot_entries = len(snapshot_entries)

    migrated_any = migrated_manifest > 0 or migrated_snapshot_entries > 0
    return {
        "migrated": migrated_any,
        "manifest_entries": migrated_manifest,
        "snapshot_entries": migrated_snapshot_entries,
        "snapshot_id": snapshot_id,
        "status_path": str(status_path),
        "snapshot_path": str(snapshot_path),
    }
