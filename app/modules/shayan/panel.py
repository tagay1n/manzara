"""Panel payload builder for the Shayan module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from app.db import Database
from app.modules.shayan.config import ShayanSettings


def _read_json_file(path: Path) -> Dict[str, Any]:
    """Load JSON object from disk; return empty object if unavailable."""
    try:
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
        return {}
    except Exception:
        return {}


def _status_entry_count(status_payload: Dict[str, Any]) -> int:
    """Count entries in legacy status map or snapshot-like payload."""
    if "entries" in status_payload and isinstance(status_payload.get("entries"), dict):
        return len(status_payload["entries"])
    return len(status_payload)


def build_shayan_panel(
    db: Database,
    shayan: ShayanSettings,
    tasks: List[Dict[str, Any]],
    workflows: List[Dict[str, Any]],
    *,
    title: str = "Shayan",
) -> Dict[str, Any]:
    """Build dashboard panel payload for Shayan."""
    shayan_status = _read_json_file(shayan.status_file)
    shayan_summary = _read_json_file(shayan.summary_file)
    latest_snapshot = _read_json_file(shayan.latest_snapshot_file)

    workflow_items: List[Dict[str, Any]] = []
    for workflow in workflows:
        schedule: Dict[str, Any] | None = None
        if workflow.get("schedule_id"):
            schedule = {
                "schedule_id": workflow.get("schedule_id"),
                "schedule_type": workflow.get("schedule_type"),
                "day_of_week": workflow.get("day_of_week"),
                "time_of_day": workflow.get("time_of_day"),
                "timezone": workflow.get("timezone"),
                "enabled": bool(workflow.get("schedule_enabled", False)),
                "overlap_policy": workflow.get("overlap_policy"),
                "catchup_policy": workflow.get("catchup_policy"),
                "next_run_at": workflow.get("next_run_at"),
                "last_run_at": workflow.get("last_run_at"),
            }

        workflow_items.append(
            {
                "workflow_id": workflow["workflow_id"],
                "title": workflow["title"],
                "description": workflow.get("description") or "",
                "enabled": bool(workflow.get("enabled", True)),
                "run": {
                    "workflow_run_id": workflow.get("workflow_run_id"),
                    "status": workflow.get("run_status") or "idle",
                    "trigger_source": workflow.get("trigger_source"),
                    "started_at": workflow.get("started_at"),
                    "finished_at": workflow.get("finished_at"),
                    "error_text": workflow.get("error_text"),
                },
                "schedule": schedule,
            }
        )

    return {
        "panel_id": "shayan",
        "title": title,
        "stats": {
            "downloaded_files_total": _status_entry_count(shayan_status),
            "newly_downloaded_last_run": int(
                (shayan_summary.get("episodes") or {}).get("downloaded", 0)
            ),
            "failed_last_run": int((shayan_summary.get("episodes") or {}).get("failed", 0)),
            "last_successful_run": db.last_successful_run("shayan"),
            "last_scan": latest_snapshot.get("generated_at"),
        },
        "status_counts": db.run_count_by_status("shayan"),
        "workflows": workflow_items,
        "tasks": tasks,
    }
