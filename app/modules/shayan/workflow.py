"""Workflow and schedule definitions for the Shayan module."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.shayan.config import ShayanSettings


SHAYAN_WEEKLY_WORKFLOW_ID = "shayan.weekly_sync"
SHAYAN_WEEKLY_SCHEDULE_ID = "shayan.weekly_sync.schedule"


def shayan_workflow_bundle(_shayan: ShayanSettings) -> Dict[str, Any]:
    """Return workflow, steps, and schedule seed for weekly Shayan sync."""
    workflow = {
        "workflow_id": SHAYAN_WEEKLY_WORKFLOW_ID,
        "panel_id": "shayan",
        "title": "Weekly Sync",
        "description": "Scan catalog changes and download new content.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": "shayan.scan_changes",
            "step_type": "task",
            "condition_json": {},
        },
        {
            "step_order": 2,
            "task_id": "shayan.download_new",
            "step_type": "task",
            "condition_json": {
                "requires_scan_new_items_gt": 0,
            },
        },
    ]

    schedule = {
        "schedule_id": SHAYAN_WEEKLY_SCHEDULE_ID,
        "workflow_id": SHAYAN_WEEKLY_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 1,
        "time_of_day": "03:00",
        "timezone": "UTC",
        "enabled": 0,
        "overlap_policy": "skip",
        "catchup_policy": "once",
    }

    return {
        "workflow": workflow,
        "steps": steps,
        "schedule": schedule,
    }
