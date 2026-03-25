"""Workflow definitions for Oscar flow."""

from __future__ import annotations

from typing import Any, Dict, List

from app.modules.oscar.tasks import (
    OSCAR_DISCOVER_SNAPSHOTS_TASK_ID,
    OSCAR_DOWNLOAD_RANGES_TASK_ID,
    OSCAR_EXPORT_PARQUET_TASK_ID,
    OSCAR_RESOLVE_OFFSETS_TASK_ID,
    OSCAR_UPLOAD_DATASET_TASK_ID,
)


OSCAR_PIPELINE_WORKFLOW_ID = "oscar.snapshot_pipeline"
OSCAR_PIPELINE_SCHEDULE_ID = "oscar.snapshot_pipeline.schedule"


def oscar_pipeline_workflow_bundle() -> Dict[str, Any]:
    """Return workflow bundle for next-snapshot Oscar pipeline."""
    workflow = {
        "workflow_id": OSCAR_PIPELINE_WORKFLOW_ID,
        "panel_id": "oscar",
        "title": "Oscar Snapshot Pipeline",
        "description": (
            "Discover snapshots, resolve offsets, download ranges, export parquet, "
            "and upload dataset for next snapshot."
        ),
        "enabled": 1,
    }

    steps: List[Dict[str, Any]] = [
        {
            "step_order": 1,
            "task_id": OSCAR_DISCOVER_SNAPSHOTS_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
        {
            "step_order": 2,
            "task_id": OSCAR_RESOLVE_OFFSETS_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
        {
            "step_order": 3,
            "task_id": OSCAR_DOWNLOAD_RANGES_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
        {
            "step_order": 4,
            "task_id": OSCAR_EXPORT_PARQUET_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
        {
            "step_order": 5,
            "task_id": OSCAR_UPLOAD_DATASET_TASK_ID,
            "step_type": "task",
            "condition_json": {},
        },
    ]

    schedule = {
        "schedule_id": OSCAR_PIPELINE_SCHEDULE_ID,
        "workflow_id": OSCAR_PIPELINE_WORKFLOW_ID,
        "schedule_type": "weekly",
        "day_of_week": 6,
        "time_of_day": "01:30",
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
