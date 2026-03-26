"""Panel payload builder for the Shayan module."""

from __future__ import annotations

from typing import Any, Dict, List

from app.db import Database
from app.modules.shayan.config import ShayanSettings

def build_shayan_panel(
    db: Database,
    shayan: ShayanSettings,
    tasks: List[Dict[str, Any]],
    workflows: List[Dict[str, Any]],
    *,
    title: str = "Shayan",
) -> Dict[str, Any]:
    """Build dashboard panel payload for Shayan."""
    _ = shayan
    latest_scan = db.get_latest_shayan_snapshot() or {}
    latest_download_runs = db.list_recent_runs_for_task("shayan.download_new", limit=10)
    latest_completed_download = next(
        (run for run in latest_download_runs if str(run.get("status") or "") == "completed"),
        {},
    )
    latest_download_artifacts = latest_completed_download.get("summary") or {}
    latest_download_payload = latest_download_artifacts.get("artifacts") or {}
    downloaded_last_run = int(latest_download_payload.get("downloaded") or 0)
    failed_last_run = int(latest_download_payload.get("failed") or 0)
    latest_upload_runs = db.list_recent_runs_for_task("shayan.upload_yadisk", limit=10)
    latest_completed_upload = next(
        (run for run in latest_upload_runs if str(run.get("status") or "") == "completed"),
        {},
    )
    latest_upload_artifacts = latest_completed_upload.get("summary") or {}
    latest_upload_payload = latest_upload_artifacts.get("artifacts") or {}
    uploaded_last_run = int(latest_upload_payload.get("uploaded") or 0)
    upload_failed_last_run = int(latest_upload_payload.get("failed") or 0)

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
            "downloaded_files_total": db.shayan_manifest_entry_count(),
            "uploaded_to_yadisk_total": db.shayan_manifest_yadisk_uploaded_count(),
            "newly_downloaded_last_run": downloaded_last_run,
            "failed_last_run": failed_last_run,
            "uploaded_last_run": uploaded_last_run,
            "upload_failed_last_run": upload_failed_last_run,
            "last_successful_run": db.last_successful_run("shayan"),
            "last_scan": latest_scan.get("generated_at") or latest_scan.get("created_at"),
        },
        "status_counts": db.run_count_by_status("shayan"),
        "workflows": workflow_items,
        "tasks": tasks,
    }
