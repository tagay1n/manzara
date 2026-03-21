"""Workflow definitions for Maintenance/Library flows."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.maintenance.tasks import MONOCORPUS_META_EVALUATE_TASK_ID


LIBRARY_WORKFLOW_ID = "library.meta_evaluate"
LIBRARY_WORKFLOW_SCHEDULE_ID = "library.meta_evaluate.schedule"


def library_workflow_bundle() -> Dict[str, Any]:
    """Return workflow, steps, and schedule seed for monocorpus meta evaluate."""
    workflow = {
        "workflow_id": LIBRARY_WORKFLOW_ID,
        "panel_id": "library",
        "title": "Library",
        "description": "Run monocorpus metadata evaluation.",
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": MONOCORPUS_META_EVALUATE_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": LIBRARY_WORKFLOW_SCHEDULE_ID,
        "workflow_id": LIBRARY_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 7,
        "time_of_day": "04:00",
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
